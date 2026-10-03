"""Daemon fswatch: spawn, dispatch eventi, timer, anti-flood e pending
(SPEC.md §6 flusso evento→sync, §8.3 pending/backoff, §10 anti-flood).

Un solo processo, nessun thread: `step()` fa `select` su stdout di fswatch con
timeout pari alla scadenza più vicina (reconcile giornaliero, retry dest
absent, respawn fswatch). Le regex `-e` sono un pre-filtro grezzo: la verità
su cosa copiare resta al matcher Python (SPEC.md §6.4).
"""
import logging
import logging.handlers
import os
import select
import signal
import subprocess
import sys
import time

from .config import (
    ConfigError,
    dest_path,
    discover_projects,
    parse_config,
    parse_sync,
    source_root_for,
)
from .copier import copy_one, needs_copy, reconcile_project
from .matcher import BUILTIN_RULES, Matcher, _glob, parse_rule
from .volumes import backoff_schedule, dest_state, partition_dests

log = logging.getLogger('safekeep')

FLOOD_LIMIT = 5000        # path per progetto in un batch → collapse a reconcile (§10.2)
DEDUP_WINDOW = 2.0        # secondi: stessa path entro la finestra → un solo evento
RECONCILE_INTERVAL = 24 * 3600
DEATH_WINDOW = 60         # finestra di conteggio delle morti di fswatch
MAX_DEATHS = 5            # 5 morti in 60s → esco con codice ≠ 0 (launchd ci riprova)
RESPAWN_CAP = 60          # backoff respawn 1 → 60s

SKIP = 'skip'
SYNC_FILE = 'sync-file'
WALK_DIR = 'walk-dir'
RELOAD_SYNC = 'reload-sync'

# SPEC.md §4.1: il file `.sync` viene SEMPRE copiato nel backup (serve per
# ricostruire la config sul destino) — layer più basso, esplicito, così non
# dipende dalle regole del progetto (semantica allow-list, §4.3).
SYNC_RULES = [parse_rule('include: .sync')]

# Modalità auto-discovery (SPEC.md §6): pre-filtro `-e` per la `$HOME` — non
# osserviamo le directory rumorose né i nostri stessi log.
HOME_EXCLUDES = ('Library', '.Trash', '.cache', '.local/state/safekeep')
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
    """Progetto dalla sua directory con `.sync`; None se il `.sync` manca."""
    sync_path = os.path.join(root, '.sync')
    try:
        with open(sync_path) as fh:
            text = fh.read()
    except OSError:
        return None
    sync = parse_sync(text, log_level=cfg.log_level, origin=sync_path)
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
    """Finestra di dedup: `state` è un dict path → ultimo visto (aggiornato in
    place). Ritorna i path non visti negli ultimi `window` secondi; l'ultima
    occorrenza vince (estende la finestra) e le entry scadute vengono potate.
    """
    now = time.monotonic() if now is None else now
    out = []
    for path in paths:
        last = state.get(path)
        state[path] = now
        if last is None or now - last >= window:
            out.append(path)
    for path, seen in list(state.items()):
        if now - seen >= window:
            del state[path]
    return out


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
        logfile = os.path.expanduser('~/.local/state/safekeep/safekeep.log')
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
        self.projects = []
        self.proc = None
        self.buf = b''                   # residuo del parser a metà path
        self.seen = {}                   # dedup: path → ultimo visto
        self.pending = {}                # dest → next/sched/projects (backoff)
        self.deaths = []                 # timestamp delle morti di fswatch
        self.fatal = False
        self.spawn_at = 0.0
        self.spawn_delay = 1
        self.last_spawn = 0.0
        self.next_reconcile = clock() + RECONCILE_INTERVAL

    # --- setup -----------------------------------------------------------

    def load_config(self):
        try:
            with open(self.config_path) as fh:
                text = fh.read()
        except OSError as e:
            raise ConfigError(f'{self.config_path}: {e.strerror or e}') from None
        self.cfg = parse_config(text, origin=self.config_path)
        return self.cfg

    def discovery_roots(self):
        """`(roots, auto)`: modalità source → le `source` (deduplicate);
        auto-discovery → `$HOME` con pruning dedicato (SPEC.md §6)."""
        if self.cfg.sources:
            return dedup_roots(self.cfg.sources), False
        return [os.path.expanduser('~')], True

    def load_projects(self):
        self.projects = []
        roots, auto = self.discovery_roots()
        for root in discover_projects(roots, auto=auto):
            project = load_project(root, self.cfg)
            if project is not None:
                self.projects.append(project)
        return self.projects

    def reconcile_all(self):
        total = sum(self.reconcile(p) for p in self.projects)
        log.info('reconcile: %d copie su %d progetto/i', total, len(self.projects))
        return total

    def reconcile(self, project):
        """Walk + copie su ogni dest pronta (§10.1); dest assente → pending."""
        ready, absent = partition_dests(self.cfg.dests)
        for dest in absent:
            self.mark_pending(project, dest)
        stats, copied = {}, 0
        for dest in ready:
            try:
                copied += reconcile_project(project.root, dest, project.matcher,
                                            self.cfg.layout, dest_path,
                                            stats=stats,
                                            dest_base=project.source_root)
            except OSError as e:
                log.warning('reconcile %s → %s fallito: %s', project.name, dest, e)
                self.mark_pending(project, dest)
        return copied

    def sync_once(self, dry_run=False, only=None):
        """Passaggio singolo per la CLI: [(project, stats, dest_assenti)]."""
        only_abs = os.path.realpath(os.path.expanduser(only)) if only else None
        rows = []
        for project in self.projects:
            if only and project.root != only_abs and project.name != only:
                continue
            ready, absent = partition_dests(self.cfg.dests)
            stats = {}
            for dest in ready:
                try:
                    reconcile_project(project.root, dest, project.matcher,
                                      self.cfg.layout, dest_path,
                                      stats=stats, dry_run=dry_run,
                                      dest_base=project.source_root)
                except OSError as e:
                    log.warning('reconcile %s → %s fallito: %s', project.name, dest, e)
            rows.append((project, stats, len(absent)))
        return rows

    def mark_pending(self, project, dest):
        entry = self.pending.get(dest)
        if entry is None:
            sched = backoff_schedule()
            entry = {'next': self.clock() + next(sched), 'sched': sched, 'projects': set()}
            self.pending[dest] = entry
        entry['projects'].add(project)

    # --- fswatch ---------------------------------------------------------

    def fswatch_argv(self):
        """`fswatch -0 -m fsevents_monitor -r -l 1.0 -e <regex> -- <roots>` (§6.4)."""
        argv = ['fswatch', '-0', '-m', 'fsevents_monitor', '-r', '-l', '1.0']
        for regex in self.exclude_regexes():
            argv += ['-e', regex]
        return argv + ['--'] + self.discovery_roots()[0]

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
                      '(launchd con KeepAlive ci riproverà)',
                      len(self.deaths), DEATH_WINDOW)
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
        """Secondi fino alla scadenza più vicina (reconcile 24h, retry dest, respawn)."""
        now = self.clock()
        deadlines = [self.next_reconcile] + [e['next'] for e in self.pending.values()]
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
            if dest_state(dest) == 'ok':
                log.info('dest %s di nuovo montata: reconcile di %d progetto/i',
                         dest, len(entry['projects']))
                del self.pending[dest]
                for project in entry['projects']:
                    self.reconcile(project)
            else:
                entry['next'] = now + next(entry['sched'])

    def step(self, timeout=None):
        """Un passo del loop: timer/pending, respawn, select su fswatch, batch."""
        self.check_timers()
        if self.proc is None:
            wait = self.next_deadline()         # include spawn_at + timer
            if wait > 0:
                time.sleep(wait if timeout is None else min(wait, timeout))
                if self.clock() < self.spawn_at:
                    return                      # backoff respawn non scaduto
            if not self.spawn():
                return                          # fswatch mancante → fatal
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

    def run_batch(self, paths):
        paths = dedup(self.seen, paths, now=self.clock())
        if not paths:
            return
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
        """
        self.load_projects()
        root = os.path.dirname(sync_path)
        for project in self.projects:
            if project.root == root:
                log.info('.sync cambiato in %s: reconcile del progetto', root)
                self.reconcile(project)
                return
        log.info('nessun progetto seguito in %s (.sync rimosso o nuovo)', root)

    def _copy(self, src, project, is_link_dir=False):
        """Copia su ogni dest pronta; dest assente o errore I/O → pending (§8.3)."""
        # rel rispetto alla radice `source` (non del progetto): il path dest
        # include il segmento del progetto, come in reconcile (SPEC.md §4.3)
        rel = os.path.relpath(normalize(src), project.source_root)
        for dest in self.cfg.dests:
            if dest_state(dest) != 'ok':
                self.mark_pending(project, dest)
                continue
            dst = dest_path(dest, project.source_root, rel, self.cfg.layout)
            try:
                if is_link_dir:
                    copy_one(src, dst)          # symlink: sempre ricreato come link
                elif needs_copy(src, dst) and copy_one(src, dst):
                    log.debug('copiato %s → %s', src, dst)
            except OSError as e:
                log.warning('copia fallita %s → %s: %s', src, dst, e)
                self.mark_pending(project, dest)

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
                    if project.matcher.evaluate(base + name, True):
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

        Exit 0 su SIGTERM/SIGINT pulito, 1 se la config è invalida o fswatch
        muore 5 volte in 60s.
        """
        signal.signal(signal.SIGTERM, self._on_signal)
        signal.signal(signal.SIGINT, self._on_signal)
        try:
            self.load_config()
            setup_logging('debug' if verbose else self.cfg.log_level)
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
