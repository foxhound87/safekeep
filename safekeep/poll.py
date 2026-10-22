"""Monitor a polling per le root su volume di rete (SPEC.md §20).

FSEvents (macOS) e inotify (Linux) NON emettono eventi per i file che cambiano
su un mount SMB/CIFS/NFS: fswatch non li vedrebbe mai. Le root di rete vengono
quindi escluse dall'argv di fswatch e camminate qui a intervalli regolari
(`poll_interval`, §20.3).

Meccanismo: snapshot per root `{relpath: (st_mtime_ns, st_size)}` letto con
`os.lstat` (mai seguire i symlink — §7.3) con lo stesso pruning dello scan di
discovery (§4.1, `config.discovery_prune`); la diff fra due snapshot produce i
path da passare a `Daemon.run_batch` — lo stesso dispatch degli eventi fswatch,
zero logica di copia duplicata. I path rimossi alla sorgente vengono ignorati
(semantica mai-cancellare, §1).

Mount smontato / server morto: un `OSError` nel walk (root inclusa) mette la
root in `poll_pending` con `backoff_schedule()` (§8.3) e PRESERVA lo snapshot
pre-smontaggio: il primo walk riuscito dopo il remount diffà contro quello e fa
il catch-up automatico. L'errore resta confinato alla propria root (CR-05).
"""
import logging
import os
import time

from .config import discovery_prune
from .volumes import backoff_schedule

log = logging.getLogger('safekeep')


def _raise(e):
    raise e


def walk_snapshot(root, prune):
    """Snapshot di tutto ciò che il poller segue sotto `root` (con pruning).

    → `{relpath: (st_mtime_ns, st_size)}` su `os.lstat` (i symlink, anche
    rotti, entrano come foglie col loro mtime). Un file sparito fra readdir e
    stat salta (al prossimo turno); ogni ALTRIO `OSError` (root smontata,
    server morto, timeout) viene rilanciato al chiamante: è il segnale che
    mette la root in pending.
    """
    snap = {}
    for dirpath, dirnames, filenames in os.walk(root, onerror=_raise):
        rel = os.path.relpath(dirpath, root).replace(os.sep, '/')
        dirnames[:] = sorted(
            name for name in dirnames
            if not prune(name if rel == '.' else rel + '/' + name))
        for name in filenames:
            full = os.path.join(dirpath, name)
            try:
                st = os.lstat(full)
            except FileNotFoundError:
                continue            # sparito fra readdir e stat: niente da dire
            relpath = name if rel == '.' else rel + '/' + name
            snap[relpath] = (st.st_mtime_ns, st.st_size)
    return snap


class Poller:
    """Polling delle root di rete: snapshot/diff + pending con backoff (§20.2).

    `clock` iniettato come nel daemon (prassi dei test); `interval` viene
    impostato dal daemon al load del config (`poll_interval`, §20.3).
    """

    def __init__(self, interval=60, clock=time.monotonic):
        self.interval = interval
        self.clock = clock
        self.prune = discovery_prune(False)
        self.snapshots = {}         # root → snapshot (None = baseline da fare)
        self.next = {}              # root → deadline del prossimo walk
        self.pending = {}           # root → {'next': ..., 'sched': iteratore}

    def sync_roots(self, roots, auto=False):
        """Allinea il set delle root polled a `roots` (avvio/rescan/reload §20.1).

        Root già note → snapshot e pending restano intatti (il catch-up dopo un
        remount dipende dallo snapshot pre-smontaggio); root nuove → snapshot
        None (baseline al primo walk, senza dispatch: il reconcile iniziale ha
        già copiato); root non più discovery → rimosse con tutto il loro stato.
        """
        self.prune = discovery_prune(auto)
        wanted = {os.path.realpath(r) for r in roots}
        for root in [r for r in list(self.snapshots) if r not in wanted]:
            del self.snapshots[root]
            self.next.pop(root, None)
            self.pending.pop(root, None)
        for root in sorted(wanted):
            if root not in self.snapshots:
                self.snapshots[root] = None
                self.next[root] = self.clock()    # baseline appena possibile

    def due(self):
        """Root scadute e non in pending (un pending ha la sua scadenza)."""
        now = self.clock()
        return [r for r, d in self.next.items()
                if d <= now and r not in self.pending]

    def tick(self):
        """Un turno: walk delle root scadute → diff → path da sincronizzare.

        Mai due walk della stessa root in sovrapposizione: la scadenza riparte
        a **fine walk** (§20.2). I retry dei pending scaduti vengono fatti qui,
        con lo stesso walk.
        """
        paths = []
        for root in self.due():
            paths += self._walk(root)
        for root in [r for r, e in list(self.pending.items())
                     if self.clock() >= e['next']]:
            paths += self._walk(root)
        return paths

    def _walk(self, root):
        try:
            snap = walk_snapshot(root, self.prune)
        except OSError as e:
            entry = self.pending.get(root)
            sched = entry['sched'] if entry is not None else backoff_schedule()
            if entry is None:
                entry = {'sched': sched}
                self.pending[root] = entry
            # il backoff riprende da dove era: niente retry-loop a 1Hz (CR-05)
            entry['next'] = self.clock() + next(sched)
            self.next.pop(root, None)
            log.warning('poll: walk di %s fallito (%s): riprovo con backoff', root, e)
            return []
        was_pending = self.pending.pop(root, None) is not None
        old = self.snapshots.get(root)
        self.snapshots[root] = snap
        self.next[root] = self.clock() + self.interval   # intervallo da fine walk
        if old is None:
            return []                   # baseline: il reconcile iniziale ha già copiato
        changed = sorted(os.path.join(root, relpath)
                         for relpath, meta in snap.items() if old.get(relpath) != meta)
        if was_pending:
            log.info('poll: %s di nuovo raggiungibile: catch-up di %d path',
                     root, len(changed))
        return changed
