"""Daemon fswatch: spawn, dispatch eventi, timer, anti-flood e pending
(SPEC.md §6 flusso evento→sync, §8.3 pending/backoff, §10 anti-flood,
§20 monitor a polling delle root su volume di rete).

Un solo processo, nessun thread: `step()` fa `select` su stdout di fswatch con
timeout pari alla scadenza più vicina (reconcile giornaliero, retry dest
absent, respawn fswatch, turni di poll). Le regex `-e` sono un pre-filtro
grezzo: la verità su cosa copiare resta al matcher Python (SPEC.md §6.4). Il
monitor di fswatch viene da `safekeep.platform` (SPEC.md §14.1); le root su
SMB/CIFS/NFS vengono escluse dall'argv (fswatch non ne riceverebbe eventi) e
passano al poller (§20.1/§20.2).
"""
import logging
import logging.handlers
import os
import select
import signal
import subprocess
import sys
import time
from dataclasses import replace

from .config import (
    ConfigError,
    dest_path,
    dests_for_source,
    discover_projects,
    parse_config,
    parse_sync,
    route_matches,
    source_root_for,
    validate_dests,
)
from .copier import (
    DEST_DOWN_ERRNOS,
    copy_one,
    log_copy_error,
    needs_copy,
    prune_project,
    reconcile_project,
)
from .matcher import BUILTIN_RULES, Matcher, _glob, parse_rule
from .poll import Poller
from .platform import LOG_PATH, fswatch_monitor, init_system, is_network_fs
from .s3dest import S3Destination, secret_perms_insecure
from .volumes import backoff_schedule, dest_state, partition_dests

log = logging.getLogger('safekeep')

FLOOD_LIMIT = 5000        # path per progetto in un batch → collapse a reconcile (§10.2)
DEDUP_WINDOW = 2.0        # secondi: stessa path entro la finestra → un solo evento
RECONCILE_INTERVAL = 24 * 3600
DEATH_WINDOW = 60         # finestra di conteggio delle morti di fswatch
MAX_DEATHS = 5            # 5 morti in 60s → esco con codice ≠ 0 (l'init system ci riprova)
RESPAWN_CAP = 60          # backoff respawn 1 → 60s

SKIP = 'skip'
SYNC_FILE = 'sync-file'
WALK_DIR = 'walk-dir'
RELOAD_SYNC = 'reload-sync'

_NO_ENTRY = object()          # sentinella: nessun `.sync` visto (≠ blob None = assente)

# SPEC.md §4.1: il file `.sync` viene SEMPRE copiato nel backup (serve per
# ricostruire la config sul destino) — layer più basso, esplicito, così non
# dipende dalle regole del progetto (semantica allow-list, §4.3).
SYNC_RULES = [parse_rule('include: .sync')]

# Modalità auto-discovery (SPEC.md §6): pre-filtro `-e` per la `$HOME` — non
# osserviamo le directory rumorose né i nostri stessi log.
HOME_EXCLUDES = ('Library', '.Trash', '.cache', '.local/state/safekeep',
                 'Desktop', 'Documents', 'Downloads', 'Movies', 'Music',
                 'Pictures', 'Public')        # TCC: anche gli eventi arrivano filtrati
HOME_EXCLUDE_REGEXES = ['(^|/)' + _glob(p) + r'(/|$)' for p in HOME_EXCLUDES]


# ---------------------------------------------------------------- funzioni pure

def normalize(path):
    """Path canonico SENZA risolvere il symlink finale: un link sorgente va
    copiato come link (SPEC.md §7.3). Risolve solo i genitori, così il path
    degli eventi FSEvents (`/private/var/...`) e la config (`/var/...`)
    finiscono nello stesso namespace della root del progetto."""
    parent = os.path.realpath(os.path.dirname(path))
    return os.path.join(parent, os.path.basename(path))


class Project:
    """Un progetto seguito: directory con `.sync` (SPEC.md §4.1)."""

    def __init__(self, root, name, matcher, excludes=None, source_root=None):
        self.root = os.path.realpath(root)
        # radice `source` del config che contiene il progetto: da qui si calcola
        # il path dest in layout `relative` (SPEC.md §4.3), così i progetti non
        # si sovrappongono; senza source nota vale la root del progetto
        self.source_root = os.path.realpath(source_root) if source_root else self.root
        self.name = name
        self.matcher = matcher
        self.excludes = excludes or []      # regole exclude statiche (per fswatch -e)

    @property
    def sync_path(self):
        return os.path.join(self.root, '.sync')


def load_project(root, cfg):
    """Progetto dalla sua directory con `.sync`; None se il `.sync` manca o è
    illeggibile (un `.sync` è "file altrui": con `log_level: info` un file
    non validi/saltabile avvisa e salta, con `debug` è fatale, CR-03)."""
    sync_path = os.path.join(root, '.sync')
    try:
        with open(sync_path, encoding='utf-8') as fh:
            text = fh.read()
    except OSError:
        return None
    except UnicodeDecodeError as e:
        if cfg.log_level == 'debug':
            raise ConfigError(f'{sync_path}: .sync non UTF-8 valido: {e}') from None
        log.warning('%s: .sync non UTF-8 valido, progetto saltato: %s', sync_path, e)
        return None
    try:
        sync = parse_sync(text, log_level=cfg.log_level, origin=sync_path)
    except ConfigError:
        raise                             # log_level: debug → fatale (SPEC.md §4.2)
    except ValueError as e:               # difesa: eccezione di parse non prevista
        if cfg.log_level == 'debug':
            raise ConfigError(f'{sync_path}: {e}') from None
        log.warning('%s: progetto saltato: %s', sync_path, e)
        return None
    layers = [SYNC_RULES, BUILTIN_RULES, cfg.defaults, sync.defaults, sync.rules]
    excludes = [r for layer in layers for r in layer if not r.include]
    name = sync.name or os.path.basename(os.path.normpath(root))
    # auto-discovery (nessuna `source`): la radice da cui calcolare il path
    # dest è $HOME, così due progetti sotto la home non si sovrappongono
    # (SPEC.md §3 layout `relative`)
    roots = cfg.sources or [os.path.expanduser('~')]
    return Project(root, name, Matcher(*layers), excludes,
                   source_root=source_root_for(roots, root))


def parse_events(data, residual=b''):
    """Path NUL-delimitati (fswatch -0): (paths, residuo).

    Un chunk che si ferma a metà di un path lascia i byte residui per il
    prossimo wake del loop — path con spazi/newline/unicode sicuri.
    """
    *complete, residuo = (residual + data).split(b'\0')
    return [os.fsdecode(p) for p in complete if p], residuo


def dedup(state, paths, now=None, window=DEDUP_WINDOW):
    """Finestra di dedup: `state` è un dict path → (ultimo arrivo, scadenza)
    aggiornato in place. Ritorna i path non visti negli ultimi `window` secondi.

    Una soppressione NON perde l'evento (CR-04): la prima soppressione dopo un
    emissione programma una scadenza fissa `emissione + window` che gli arrivi
    successivi NON estendono — alla scadenza `dedup_flush` emette il path
    comunque (trailing edge). Le entry scadute e senza scadenza pendente
    vengono potate.
    """
    now = time.monotonic() if now is None else now
    out = []
    for path in paths:
        entry = state.get(path)
        last = entry[0] if entry else None
        if last is None or now - last >= window:
            out.append(path)
            state[path] = (now, None)          # emesso all'arrivo: niente scadenza
        else:
            # soppresso: la scadenza è quella dell'emissione precedente (ferma)
            deadline = entry[1] if entry[1] is not None else last + window
            state[path] = (now, deadline)
    for path, (last, deadline) in list(state.items()):
        # con una scadenza pendente l'entry resta: l'emissione va ancora fatta
        if now - last >= window and deadline is None:
            del state[path]
    return out


def dedup_flush(state, now=None):
    """Path con scadenza passata: vengono emessi comunque (CR-04).

    Chiamato a ogni wake del loop: la scadenza entra nel `timeout` del `select`
    (vedi `Daemon.next_deadline`), quindi senza nuovi batch l'ultima modifica di
    un path viene comunque processata entro `window` secondi — niente thread.
    """
    now = time.monotonic() if now is None else now
    due = [p for p, (_, deadline) in state.items()
           if deadline is not None and now >= deadline]
    for path in due:
        state[path] = (state[path][0], None)   # emesso: niente nuova scadenza
    return due


def find_project(path, projects):
    """Il progetto la cui root è la più profonda che contiene `path`; None se fuori."""
    path = normalize(path)
    best, best_len = None, -1
    for project in projects:
        root = project.root
        if (path == root or path.startswith(root + os.sep)) and len(root) > best_len:
            best, best_len = project, len(root)
    return best


def dedup_roots(roots):
    """Radici watch: quelle annidate sotto un'altra già coperta vengono saltate.

    `fswatch -r` copre già il sottoalbero → niente doppioni in argv e niente
    progetti scoperti due volte da radici sovrapposte. Ritorna i path come
    sono scritti nel config (l'ordinamento è per profondità, il più corto
    prima).
    """
    pairs = sorted(((os.path.abspath(os.path.expanduser(r)), r) for r in roots),
                   key=lambda p: len(p[0]))
    out, covered = [], []
    for norm, orig in pairs:
        if any(norm == c or norm.startswith(c + os.sep) for c in covered):
            continue
        out.append(orig)
        covered.append(norm)
    return out


def exclude_regexes(rules):
    """Pre-filtro per `fswatch -e` (SPEC.md §6.4): una regex per regola
    `exclude:` non ancorata, che matcha un segmento intero del path.

    Solo i casi traducibili senza la root: pattern ancorati (`/foo`, `docs/a`)
    e con `**` vengono omessi. Sovrafiltrare perderebbe eventi, sotto-filtrare
    no (la verità resta il matcher Python). Compatibile POSIX ERE.
    """
    out, seen = [], set()
    for rule in rules:
        if rule.include or rule.anchored or '**' in rule.pattern or rule.pattern in seen:
            continue
        seen.add(rule.pattern)
        body = _glob(rule.pattern[:-1] if rule.dir_only else rule.pattern)
        # foo/ → il segmento deve essere seguito da / (un FILE chiamato foo
        # resta visibile al matcher); altro → segmento finale del path
        out.append('(^|/)' + body + ('/' if rule.dir_only else '(/|$)'))
    return out


def classify(path, project):
    """Azione per un path arrivato da fswatch (SPEC.md §6.5)."""
    if project is None:
        return SKIP
    if os.path.basename(path) == '.sync':
        return RELOAD_SYNC            # regole cambiate o rimosse → reload + reconcile
    if not os.path.lexists(path):
        return SKIP                   # sparito → policy mai-cancellare
    rel = os.path.relpath(normalize(path), project.root)
    if rel == os.pardir or rel.startswith(os.pardir + os.sep):
        return SKIP                   # fuori dal progetto (path via symlink)
    if os.path.isdir(path) and not os.path.islink(path):
        return WALK_DIR if project.matcher.evaluate(rel, True) else SKIP
    return SYNC_FILE if project.matcher.evaluate(rel, False) else SKIP


def plan_batch(paths, projects):
    """→ (reload_paths, {progetto: 'reconcile' | [(path, azione)]}).

    Più di FLOOD_LIMIT path distinti per progetto in un batch → la coda
    incrementale viene buttata a favore di un reconcile completo (§10.2).
    """
    reloads, per = [], {}
    for path in paths:
        path = normalize(path)
        project = find_project(path, projects)
        if project is not None:
            per.setdefault(project, []).append(path)
        elif os.path.basename(path) == '.sync':
            reloads.append(path)      # `.sync` di un progetto non ancora scoperto
    plan = {}
    for project, plist in per.items():
        plan[project] = ('reconcile' if len(plist) > FLOOD_LIMIT
                         else [(p, classify(p, project)) for p in plist])
    return reloads, plan


def setup_logging(level='info', logfile=None):
    """Log su file rotante (5 × 5 MB) + stderr (SPEC.md §3). Idempotente."""
    logger = logging.getLogger('safekeep')
    logger.setLevel(getattr(logging, (level or 'info').upper(), logging.INFO))
    if getattr(logger, '_safekeep_handlers', False):
        return
    logger._safekeep_handlers = True
    fmt = logging.Formatter('%(asctime)s %(levelname)s %(message)s')
    if logfile is None:
        logfile = os.path.expanduser(LOG_PATH)
    try:
        os.makedirs(os.path.dirname(logfile), exist_ok=True)
        handler = logging.handlers.RotatingFileHandler(
            logfile, maxBytes=5 * 1024 * 1024, backupCount=5)
    except OSError as e:
        log.warning('log su file non disponibile (%s): %s', logfile, e)
    else:
        handler.setFormatter(fmt)
        logger.addHandler(handler)
    stream = logging.StreamHandler(sys.stderr)
    stream.setFormatter(fmt)
    logger.addHandler(stream)


# --------------------------------------------------------------------- daemon

class Daemon:
    def __init__(self, config_path=None, argv=None, clock=time.monotonic):
        self.config_path = os.path.expanduser(config_path or '~/.safekeep')
        self.argv = argv                 # override per i test (fswatch finto)
        self.clock = clock
        self.cfg = None
        self.s3 = None                  # S3Destination | None (SPEC.md §18)
        self._s3_cache = {}             # s3://bucket/prefisso → S3Destination (§21.5)
        self._routes_warned = set()     # route inutilizzate già avvisate (§12)
        self.s3_secret_perms_bad = False    # secret inline con perms > 0600
        self._s3_perms_warned = False       # warning una-tantum già emesso
        self.projects = []
        self.proc = None
        self.buf = b''                   # residuo del parser a metà path
        self.seen = {}                   # dedup: path → ultimo visto
        self.sync_seen = {}              # reload: path .sync → blob (None = assente)
        self.pending = {}                # dest → next/sched/projects (backoff)
        self.watch_roots = []            # radici locali → argv fswatch (§20.1)
        self.net_roots = []              # radici su volume di rete → polling (§20)
        self.poller = Poller(clock=clock)    # monitor a polling (§20.2)
        self.deaths = []                 # timestamp delle morti di fswatch
        self.fatal = False
        self.spawn_at = 0.0
        self.spawn_delay = 1
        self.last_spawn = 0.0
        self.next_reconcile = clock() + RECONCILE_INTERVAL

    # --- setup -----------------------------------------------------------

    def load_config(self):
        try:
            with open(self.config_path, encoding='utf-8') as fh:
                text = fh.read()
        except OSError as e:
            raise ConfigError(f'{self.config_path}: {e.strerror or e}') from None
        except UnicodeDecodeError as e:
            # il config globale è comunque un file letto da disco: fallisce pulito (CR-03)
            raise ConfigError(f'{self.config_path}: non è UTF-8 valido: {e}') from None
        self.cfg = parse_config(text, origin=self.config_path)
        # polling root di rete (§20.3): l'intervallo viene dal config
        self.poller.interval = self.cfg.poll_interval
        # dest S3 (SPEC §18): S3ExtraMissing(ConfigError) se manca l'extra,
        # e controllo chmod 600 del secret inline (§18.4 — il warning
        # runtime va emesso DOPO setup_logging, vedi log_s3_secret_perms)
        self.s3 = S3Destination(self.cfg.s3) if self.cfg.s3 else None
        if self.s3 is not None:
            self._s3_cache[self.s3.root] = self.s3   # globale = prima entry cache (§21.5)
        self.s3_secret_perms_bad = (
            self.s3 is not None
            and secret_perms_insecure(self.config_path, self.cfg.s3))
        return self.cfg

    def log_s3_secret_perms(self):
        """Warning una-tantum se `s3_secret_key` inline è leggibile da altri
        utenti (permessi del config > 0600, SPEC.md §18.4).

        Va chiamato DOPO `setup_logging`: prima dell'init degli handler il
        logger ha solo il NullHandler del package e il warning sparirebbe."""
        if self.s3_secret_perms_bad and not self._s3_perms_warned:
            self._s3_perms_warned = True
            log.warning('s3_secret_key in %s con permessi > 0600: il secret '
                        'è leggibile da altri utenti — rimedio: chmod 600 %s',
                        self.config_path, self.config_path)

    def validate_dests(self):
        """CR-02: dest dentro la sorgente → ConfigError, subito (SPEC.md §11/§12).

        Base = `source` se presenti, altrimenti `$HOME` (modalità
        auto-discovery): così viene bloccata anche una dest dentro la `$HOME`,
        dove finirebbero comunque le copie dei progetti scoperti lì.
        """
        validate_dests(self.cfg.sources or [os.path.expanduser('~')], self.cfg.dests)

    def discovery_roots(self):
        """`(roots, auto)`: modalità source → le `source` (deduplicate);
        auto-discovery → `$HOME` con pruning dedicato (SPEC.md §6)."""
        if self.cfg.sources:
            return dedup_roots(self.cfg.sources), False
        return [os.path.expanduser('~')], True

    def load_projects(self):
        self.projects = []
        roots, auto = self.discovery_roots()
        self.classify_roots(roots, auto)
        for root in discover_projects(roots, auto=auto):
            project = load_project(root, self.cfg)
            if project is not None:
                self.projects.append(project)
        self.warn_unused_routes()
        return self.projects

    def unused_routes(self):
        """Route che non matchano NESSUNA root nota (SPEC.md §12) → list[Route].

        Radici note = le `source` del config (o `$HOME` in auto-discovery) più
        le source root E le project root (dir del `.sync`) dei progetti
        scoperti — stesso doppio livello del match (§21.3). La usano
        `warn_unused_routes` (warning di log, una-tantum per riga per daemon)
        e `doctor` (riga di check): stessa logica, mai duplicata due volte.
        """
        if not self.cfg.routes:
            return []
        roots = {os.path.realpath(os.path.abspath(os.path.expanduser(r)))
                 for r in (self.cfg.sources or [os.path.expanduser('~')])}
        roots |= {p.source_root for p in self.projects}
        roots |= {p.root for p in self.projects}
        return [r for r in self.cfg.routes
                if not any(route_matches(os.path.realpath(r.src), root)
                           for root in roots)]

    def warn_unused_routes(self):
        """SPEC.md §12: una route che non matcha NESSUNA root nota avvisa
        (warning non fatale): in auto-discovery la root può non essere ancora
        agganciata e al rescan 24h la route può diventare attiva.

        Warning una-tantum per riga per daemon: `load_projects` gira anche a
        ogni reload di `.sync`.
        """
        for r in self.unused_routes():
            src = os.path.realpath(r.src)
            if (src, r.lineno) not in self._routes_warned:
                self._routes_warned.add((src, r.lineno))
                log.warning('route %s (riga %d) non corrisponde ad alcuna root '
                            'nota (source o project): nessun progetto la usa — '
                            'al prossimo rescan potrebbe agganciarsi (SPEC.md §12)',
                            r.src, r.lineno)

    def classify_roots(self, roots, auto=False):
        """Split radici locali (fswatch) vs su volume di rete (polling, §20.1).

        Eseguito a ogni avvio, rescan di discovery e reload della config.
        **Sticky**: una root già classificata di rete resta sul poller anche
        se durante uno smontaggio il mount sparisce e il path risolve al fs
        locale sottostante — a ri-classificarla locale si perderebbero
        snapshot e pending, e con essi il catch-up post-remount (§20.2).
        Zero radici locali → fswatch non viene spawnato (§20.1).
        """
        prev_net = {os.path.realpath(r) for r in self.net_roots}
        self.watch_roots, self.net_roots = [], []
        for root in roots:
            if is_network_fs(root) or os.path.realpath(root) in prev_net:
                self.net_roots.append(root)
            else:
                self.watch_roots.append(root)
        self.poller.sync_roots(self.net_roots, auto=auto)
        if self.net_roots:
            log.info('%d radice/i su volume di rete: polling ogni %ds (§20); '
                     '%d radice/i locale/i per fswatch',
                     len(self.net_roots), self.poller.interval,
                     len(self.watch_roots))
        if self.proc is not None and not self.watch_roots and not self.argv:
            log.info('nessuna radice locale rimasta: fswatch viene fermato (§20.1)')
            self.close_proc()

    # --- destinazioni per progetto (routing §21) -------------------------

    def s3_for_root(self, root):
        """La S3Destination con quell'etichetta `s3://bucket/prefisso`, o None."""
        d = self._s3_cache.get(root)
        if d is None and self.s3 is not None and getattr(self.s3, 'root', None) == root:
            d = self._s3_cache[root] = self.s3
        return d

    def s3_dest(self, bucket, prefix):
        """S3Destination per (bucket, prefix), cached per-route (§21.5).

        Il client botocore è condiviso per bucket con la dest globale (credenziali
        ed endpoint sono unici per config, §21.6): un bucket diverso da
        `s3_bucket` crea il proprio client lazily — mai client multipli per lo
        stesso bucket. Senza extra `safekeep[s3]` → S3ExtraMissing fail-fast,
        come da sempre (§18.1).
        """
        root = 's3://' + bucket + ('/' + prefix if prefix else '')
        d = self.s3_for_root(root)
        if d is None:
            shared = None
            scfg = getattr(self.s3, 'cfg', None)
            if scfg is not None and scfg.bucket == bucket:
                shared = self.s3.client()          # stesso client per bucket
            d = S3Destination(replace(self.cfg.s3, bucket=bucket, prefix=prefix),
                              client=shared)
            self._s3_cache[root] = d
        return d

    def project_targets(self, project):
        """`(dest_fs, dest_s3)` del progetto (SPEC.md §21.3): solo le dest della
        route che vince sui DUE livelli — source root e project root del
        `.sync` (longest-prefix-wins); nessuna route → TUTTE le destinazioni
        (default retrocompat 0.10.0). `dest_s3` è la lista di destinazioni S3
        (cached per bucket/prefix, §21.5)."""
        fs, s3specs = dests_for_source(project.source_root, self.cfg,
                                       project_root=project.root)
        return fs, [self.s3_dest(bucket, prefix) for bucket, prefix in s3specs]

    def reconcile_all(self):
        total = sum(self.reconcile(p) for p in self.projects)
        log.info('reconcile: %d copie su %d progetto/i', total, len(self.projects))
        return total

    def reconcile(self, project):
        """Walk + copie su ogni dest pronta del PROGETTO (§10.1/§21.3: solo le
        dest della sua route, o tutte se senza route); dest assente o con errore
        di mount → pending con backoff (§8.3, CR-05). Le dest S3 del progetto
        (§18/§21.5) seguono lo stesso percorso: HeadBucket offline o errore di
        rete → pending."""
        dests, s3dests = self.project_targets(project)
        ready, absent = partition_dests(dests)
        for dest in absent:
            self.mark_pending(project, dest)
        copied = 0
        for dest in ready:
            stats = {}
            try:
                copied += reconcile_project(project.root, dest, project.matcher,
                                            self.cfg.layout, dest_path,
                                            stats=stats,
                                            dest_base=project.source_root)
            except OSError as e:
                log.warning('reconcile %s → %s fallito: %s', project.name, dest, e)
                self.mark_pending(project, dest)
                continue
            if stats.get('dest_pendente'):
                # ENODEV/EBUSY/ENOSPC: questa dest va in pending, le altre
                # proseguono (il detail è già stato loggato dal copier)
                self.mark_pending(project, dest)
        for s3 in s3dests:
            if s3.state() != 'ok':
                self.mark_pending(project, s3.root)   # offline → §8.3
                continue
            stats = {}
            try:
                copied += reconcile_project(
                    project.root, s3.root, project.matcher,
                    self.cfg.layout, s3.dest_path, stats=stats,
                    dest_base=project.source_root,
                    copy_fn=s3.copy, needs_copy_fn=s3.needs_copy)
            except OSError as e:
                # errore di rete nato a metà walk (ENODEV → pending) oppure
                # auth/richiesta (EACCES → solo log): mai crash del daemon
                log.warning('reconcile %s → %s fallito: %s',
                            project.name, s3.root, e)
                self.mark_pending(project, s3.root)
            else:
                if stats.get('dest_pendente'):
                    self.mark_pending(project, s3.root)
        return copied

    def sync_once(self, dry_run=False, only=None, prune=False):
        """Passaggio singolo per la CLI: [(project, stats, dest_assenti)].
        `prune=True` rimuove dalla dest i file non più inclusi con sorgente
        ancora presente (`--prune`, SPEC.md §9): **solo sulle dest fs** —
        la dest S3 non viene mai toccata (§18.2, nessuna cancellazione)."""
        only_abs = os.path.realpath(os.path.expanduser(only)) if only else None
        rows = []
        for project in self.projects:
            if only and project.root != only_abs and project.name != only:
                continue
            dests, s3dests = self.project_targets(project)
            ready, absent = partition_dests(dests)
            stats = {}
            for dest in ready:
                try:
                    reconcile_project(project.root, dest, project.matcher,
                                      self.cfg.layout, dest_path,
                                      stats=stats, dry_run=dry_run,
                                      dest_base=project.source_root)
                except OSError as e:
                    log.warning('reconcile %s → %s fallito: %s', project.name, dest, e)
                if prune:
                    stats['rimossi'] = stats.get('rimossi', 0) + prune_project(
                        project.root, dest, project.matcher, self.cfg.layout,
                        dest_path, dest_base=project.source_root, dry_run=dry_run)
            for s3 in s3dests:
                try:
                    reconcile_project(
                        project.root, s3.root, project.matcher,
                        self.cfg.layout, s3.dest_path,
                        stats=stats, dry_run=dry_run,
                        dest_base=project.source_root,
                        copy_fn=s3.copy, needs_copy_fn=s3.needs_copy)
                except OSError as e:
                    log.warning('reconcile %s → %s fallito: %s',
                                project.name, s3.root, e)
                # nessun prune su S3 (SPEC §18.2): `--prune` tocca solo le dest fs
            rows.append((project, stats, len(absent)))
        return rows

    def mark_pending(self, project, dest):
        entry = self.pending.get(dest)
        if entry is None:
            sched = backoff_schedule()
            entry = {'next': self.clock() + next(sched), 'sched': sched, 'projects': set()}
            self.pending[dest] = entry
        entry['projects'].add(project)

    def dest_ready(self, dest) -> bool:
        """Stato di una dest ai fini del retry pending (SPEC.md §8.3/§18.3):
        fs → stat del mountpoint (`dest_state`), S3 → HeadBucket con cache 30s
        (l'etichetta `s3://bucket/prefisso` risale la cache per-route, §21.5)."""
        s3 = self.s3_for_root(dest)
        if s3 is not None:
            return s3.state() == 'ok'
        return dest_state(dest) == 'ok'

    # --- fswatch ---------------------------------------------------------

    def fswatch_argv(self):
        """`fswatch -0 -m <monitor> -r -l 1.0 -E -e <regex> -- <roots>` (§6.4, §14.2):
        il monitor è dichiarato per piattaforma, mai il default implicito;
        `-E` = regex ERE (Extended Regular Expressions): senza, fswatch legge le
        `-e` come BRE (Basic Regular Expressions) dove `(`/`|` sono letterali e
        NESSUNA esclusione filtra (con un `.sync` in `$HOME` i path di `Library`
        entrano nel matcher e il daemon satura in `walk_dir`)."""
        argv = ['fswatch', '-0', '-m', fswatch_monitor(), '-r', '-l', '1.0', '-E']
        for regex in self.exclude_regexes():
            argv += ['-e', regex]
        # solo le radici LOCALI: fswatch non riceve eventi da mount di rete
        # (§20.1) — quelle di rete le segue il poller
        return argv + ['--'] + self.watch_roots

    def exclude_regexes(self):
        rules = [r for p in self.projects for r in p.excludes]
        regexes = exclude_regexes(rules)
        if not self.cfg.sources:
            regexes += HOME_EXCLUDE_REGEXES   # auto-discovery: si guarda la $HOME
        return regexes

    def spawn(self):
        argv = self.argv or self.fswatch_argv()
        log.info('avvio: %s', ' '.join(argv))
        try:
            self.proc = subprocess.Popen(argv, stdout=subprocess.PIPE)
        except OSError as e:
            log.error('impossibile avviare fswatch: %s', e)
            self.fatal = True
            return False
        os.set_blocking(self.proc.stdout.fileno(), False)
        self.buf = b''
        self.last_spawn = self.clock()
        return True

    def close_proc(self):
        proc, self.proc = self.proc, None
        if proc is None:
            return
        try:
            proc.terminate()
            proc.wait(timeout=3)
        except Exception:
            try:
                proc.kill()
                proc.wait(timeout=3)
            except Exception:
                pass
        try:
            proc.stdout.close()
        except OSError:
            pass

    def on_death(self):
        """EOF/uscita di fswatch: respawn con backoff 1→60s; 5 morti in 60s → fatal."""
        now = self.clock()
        self.close_proc()
        self.deaths = [t for t in self.deaths if now - t < DEATH_WINDOW]
        self.deaths.append(now)
        if len(self.deaths) >= MAX_DEATHS:
            log.error('fswatch morto %d volte in %ds: esco con codice != 0 '
                      '(%s ci riproverà)',
                      len(self.deaths), DEATH_WINDOW, init_system())
            self.fatal = True
            return
        if now - self.last_spawn >= DEATH_WINDOW:
            self.spawn_delay = 1        # era stabile: ricomincia da 1s
        self.spawn_at = now + self.spawn_delay
        log.warning('fswatch è uscito: riavvio tra %ds', self.spawn_delay)
        self.spawn_delay = min(self.spawn_delay * 2, RESPAWN_CAP)

    def _read_available(self):
        """Tutto ciò che è disponibile sullo stdout, senza bloccare: (data, eof)."""
        data = b''
        while True:
            try:
                chunk = os.read(self.proc.stdout.fileno(), 65536)
            except (BlockingIOError, InterruptedError):
                break
            if not chunk:
                return data, True       # fswatch morto
            data += chunk
        return data, False

    # --- loop ------------------------------------------------------------

    def next_deadline(self):
        """Secondi fino alla scadenza più vicina (reconcile 24h, retry dest,
        respawn, scadenza dedup — CR-04: è ciò che fa svegliare il `select`
        per emettere un path soppresso — e turni del polling, §20.2)."""
        now = self.clock()
        deadlines = [self.next_reconcile] + [e['next'] for e in self.pending.values()]
        deadlines += [d for _, d in self.seen.values() if d is not None]
        deadlines += list(self.poller.next.values())
        deadlines += [e['next'] for e in self.poller.pending.values()]
        if self.proc is None:
            deadlines.append(self.spawn_at)
        return max(0.0, min(deadlines) - now)

    def check_timers(self):
        """Reconcile giornaliero + retry delle dest absent con backoff (§8.3).

        Il ciclo 24h fa anche il **rescan di discovery**: un `.sync` creato
        mentre il daemon era spento (evento perso) viene agganciato qui (§6).
        """
        now = self.clock()
        if now >= self.next_reconcile:
            self.next_reconcile = now + RECONCILE_INTERVAL
            self.load_projects()
            self.reconcile_all()
        for dest in [d for d, e in self.pending.items() if now >= e['next']]:
            entry = self.pending[dest]
            if not self.dest_ready(dest):
                entry['next'] = now + next(entry['sched'])
                continue
            log.info('dest %s di nuovo pronta: reconcile di %d progetto/i',
                     dest, len(entry['projects']))
            del self.pending[dest]
            for project in entry['projects']:
                self.reconcile(project)
            if dest in self.pending:
                # il mount c'è ma la copia è fallita di nuovo (ENODEV/EBUSY/ENOSPC):
                # il backoff riprende da dove era — niente retry-loop a 1Hz (CR-05)
                failed = self.pending[dest]
                failed['sched'] = entry['sched']
                failed['next'] = self.clock() + next(entry['sched'])

    def step(self, timeout=None):
        """Un passo del loop: timer/pending, scadenze dedup, poll delle root di
        rete, respawn, select su fswatch, batch.

        Con `self.argv` (override dei test) il fswatch finto viene spawnato
        anche senza radici locali; il daemon reale spawna fswatch **solo** se
        c'è almeno una radice locale (§20.1) — altrimenti vive di soli timer.
        """
        self.check_timers()
        self.flush_dedup()               # CR-04: emissione "di coda" alla scadenza
        self.poll_tick()                 # §20.2: walk/diff delle root su volume di rete
        if self.proc is None and (self.watch_roots or self.argv):
            wait = self.next_deadline()         # include spawn_at + timer
            if wait > 0:
                time.sleep(wait if timeout is None else min(wait, timeout))
                if self.clock() < self.spawn_at:
                    return                      # backoff respawn non scaduto
            if not self.spawn():
                return                          # fswatch mancante → fatal
        if self.proc is None:
            # nessuna radice locale (§20.1): fswatch non serve — vivo di soli
            # timer (poll/pending/reconcile), qui si dorme fino al prossimo
            wait = self.next_deadline()
            if wait > 0:
                time.sleep(wait if timeout is None else min(wait, timeout))
            return
        if timeout is None:
            timeout = self.next_deadline()
        ready, _, _ = select.select(
            [self.proc.stdout.fileno()], [], [], max(0.0, timeout))
        if not ready:
            return
        data, eof = self._read_available()
        if data:
            paths, self.buf = parse_events(data, self.buf)
            if paths:
                self.run_batch(paths)
        if eof:
            self.on_death()

    def flush_dedup(self):
        """Emissione alla scadenza del dedup (CR-04): i path soppressi con
        `deadline` passata vengono processati anche senza nuovi batch."""
        due = dedup_flush(self.seen, now=self.clock())
        if due:
            log.debug('%d path emessi alla scadenza della finestra di dedup', len(due))
            self.dispatch(due)

    def poll_tick(self):
        """Turno del monitor a polling (§20.2): le root di rete scadute vengono
        camminate (snapshot/diff) e la diff passa allo stesso dispatch degli
        eventi fswatch — zero logica di copia duplicata."""
        paths = self.poller.tick()
        if paths:
            log.debug('poll: %d path cambiati su volumi di rete', len(paths))
            self.run_batch(paths)

    def run_batch(self, paths):
        paths = dedup(self.seen, paths, now=self.clock())
        if paths:
            self.dispatch(paths)

    def dispatch(self, paths):
        reloads, plan = plan_batch(paths, self.projects)
        if paths and not reloads and not plan:
            # batch interamente fuori da ogni progetto noto: durante uno
            # smontaggio la discovery scarta la root e il catch-up del poll
            # (§20.2) verrebbe perso per sempre — un rescan prima di buttare
            # i path: la root è di nuovo raggiungibile al momento del diff
            self.load_projects()
            reloads, plan = plan_batch(paths, self.projects)
        for path in reloads:
            self.reload(path)
        for project, actions in plan.items():
            if actions == 'reconcile':
                log.warning('%s: oltre %d path nel batch: collapse a reconcile',
                            project.name, FLOOD_LIMIT)
                self.reconcile(project)
                continue
            for path, action in actions:
                if action == SYNC_FILE:
                    self._copy(path, project, is_link_dir=os.path.islink(path))
                elif action == WALK_DIR:
                    self.walk_dir(path, project)
                elif action == RELOAD_SYNC:
                    self.reload(path)

    def reload(self, sync_path):
        """Un `.sync` è cambiato: ricarica i progetti e reconcilia quello
        interessato (le regole sono cambiate). Gli eventi di questo batch
        potrebbero usare matcher vecchi: il reconcile subito dopo convergere.

        Il rescan completo copre anche il `.sync` di un progetto **nuovo** (un
        evento su `<dir>/.sync` fuori da ogni progetto noto → discovery di quel
        progetto + reconcile): così l'auto-discovery aggancia un `.sync` creato
        dopo l'avvio istantaneamente.

        Cache sui contenuti (`sync_seen`): editor e tool di sync toccano
        `.sync` senza cambiarne le regole — a ogni evento il rescan di
        discovery e il reconcile rifarebbero l'intero walk delle radici per
        nulla. Il reload parte solo se il blob è nuovo/cambiato/sparito.
        """
        try:
            with open(sync_path, 'rb') as fh:
                blob = fh.read()
        except OSError:
            blob = None               # `.sync` assente: reload solo la prima volta
        if self.sync_seen.get(sync_path, _NO_ENTRY) == blob:
            log.debug('.sync invariato in %s: reload saltato', sync_path)
            return
        self.sync_seen[sync_path] = blob
        self.load_projects()
        root = os.path.dirname(sync_path)
        for project in self.projects:
            if project.root == root:
                log.info('.sync cambiato in %s: reconcile del progetto', root)
                self.reconcile(project)
                return
        log.info('nessun progetto seguito in %s (.sync rimosso o nuovo)', root)

    def _copy(self, src, project, is_link_dir=False):
        """Copia su ogni dest pronta del PROGETTO (routing §21.3: solo le dest
        della sua route, o tutte se senza route); dest assente o errore di
        mount → pending (§8.3); altri errori I/O → solo log (l'evento non deve
        mettere la dest in pending: al prossimo reconcile il file viene
        ritentato, CR-05)."""
        # rel rispetto alla radice `source` (non del progetto): il path dest
        # include il segmento del progetto, come in reconcile (SPEC.md §4.3)
        rel = os.path.relpath(normalize(src), project.source_root)
        dests, s3dests = self.project_targets(project)
        for dest in dests:
            if dest_state(dest) != 'ok':
                self.mark_pending(project, dest)
                continue
            dst = dest_path(dest, project.source_root, rel, self.cfg.layout)
            try:
                if is_link_dir:
                    copy_one(src, dst, dest)    # symlink: sempre ricreato come link
                elif needs_copy(src, dst) and copy_one(src, dst, dest):
                    log.debug('copiato %s → %s', src, dst)
            except OSError as e:
                if e.errno in DEST_DOWN_ERRNOS:
                    log.warning('errore di mount/rete %s → %s: %s: dest in pending',
                                src, dst, e)
                    self.mark_pending(project, dest)
                else:
                    log_copy_error(src, dst, e)
        for s3 in s3dests:
            # HeadBucket con cache 30s: offline → pending senza toccare la rete
            # a ogni evento (SPEC §18.3)
            if s3.state() != 'ok':
                self.mark_pending(project, s3.root)
                continue
            dst = s3.dest_path(s3.root, project.source_root, rel, self.cfg.layout)
            try:
                if is_link_dir:
                    s3.copy(src, dst)            # symlink: object zero-byte
                elif s3.needs_copy(src, dst) and s3.copy(src, dst):
                    log.debug('copiato %s → %s', src, dst)
            except OSError as e:
                if e.errno in DEST_DOWN_ERRNOS:
                    log.warning('errore di rete %s → %s: %s: dest S3 in pending',
                                src, dst, e)
                    self.mark_pending(project, s3.root)
                else:
                    log_copy_error(src, dst, e)

    def walk_dir(self, dirpath, project):
        """Mini-walk: i figli di una directory appena creata potrebbero non
        avere eventi propri; pruning delle dir con verdict escluso + filtro
        del matcher sui file (allow-list, SPEC.md §4.3)."""
        dirpath = normalize(dirpath)
        for dirpath2, dirnames, filenames in os.walk(dirpath):
            base = os.path.relpath(dirpath2, project.root)
            base = '' if base == os.curdir else base.replace(os.sep, '/') + '/'
            keep = []
            for name in sorted(dirnames):
                full = os.path.join(dirpath2, name)
                if os.path.islink(full):
                    # foglia (come in classify): valutata come FILE, non come
                    # directory — vedi reconcile_project
                    if project.matcher.evaluate(base + name, False):
                        self._copy(full, project, is_link_dir=True)
                    continue
                if project.matcher.evaluate(base + name, True):
                    keep.append(name)
            dirnames[:] = keep                # dir esclusa → sottoalbero potato
            for name in filenames:
                if project.matcher.evaluate(base + name, False):   # allow-list (§4.3)
                    self._copy(os.path.join(dirpath2, name), project)

    # --- run -------------------------------------------------------------

    def _on_signal(self, signum, frame):
        # solleva per interrompere subito il select: senza eccezione PEP 475
        # lo ritenterebbe e il shutdown aspetterebbe tutta la scadenza
        raise KeyboardInterrupt

    def run(self, verbose=False):
        """Setup → reconcile iniziale → watch → dispatch (SPEC.md §6).

        Exit 0 su SIGTERM/SIGINT pulito, 1 se la config è invalida, se una dest
        è dentro la sorgente (CR-02) o se fswatch muore 5 volte in 60s.
        """
        signal.signal(signal.SIGTERM, self._on_signal)
        signal.signal(signal.SIGINT, self._on_signal)
        try:
            self.load_config()
            setup_logging('debug' if verbose else self.cfg.log_level)
            self.log_s3_secret_perms()    # §18.4: warning una-tantum, dopo gli handler
            self.validate_dests()         # CR-02: dest dentro source/$HOME → blocca
            roots, auto = self.discovery_roots()
            if auto:
                log.info('modalità auto-discovery: scan di %s alla ricerca di .sync',
                         roots[0])
            else:
                log.info('modalità source: %d radice/i da osservare', len(roots))
            self.load_projects()
            log.info('%d progetto/i: %s', len(self.projects),
                     ', '.join(p.root for p in self.projects) or '-')
            self.next_reconcile = self.clock() + RECONCILE_INTERVAL
            self.reconcile_all()
            while not self.fatal:
                self.step()
        except KeyboardInterrupt:
            log.info('shutdown su segnale')
        finally:
            self.close_proc()
            logging.shutdown()
        return 1 if self.fatal else 0
