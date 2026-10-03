"""Config globale `~/.safekeep` e file `.sync` di progetto (SPEC.md §3 e §4).

Le destinazioni vivono SOLO nel config globale: un `.sync` contiene solo
regole — le chiavi `name:`/`defaults:`/`include:`/`exclude:` e le righe nude
stile gitignore (una riga nuda = `include:`, `!riga` = `exclude:`, SPEC.md §4.2).
Una riga a forma di chiave non nota (`dest:`, `-dest:`, …) resta invalida: con
`log_level: info` genera solo un warning e viene saltata, con `log_level: debug`
è fatale (SPEC.md §4.2). Il config globale è invece sempre fail-fast.
"""
import logging
import os
import re

from .matcher import BUILTIN_RULES, Matcher, parse_rule, strip_comment

log = logging.getLogger('safekeep')


class ConfigError(ValueError):
    """Errore di config: il messaggio contiene sempre l'origine e la riga."""


class GlobalConfig:
    def __init__(self):
        self.sources = []             # radici osservate (ripetibili, opzionali)
        self.dests = []               # destinazioni globali (ripetibili) — le UNICHE
        self.layout = 'relative'      # relative | full
        self.defaults = []            # regole a bassa priorità (list[Rule])
        self.log_level = 'info'       # debug | info | warn | error


class SyncConfig:
    def __init__(self):
        self.name = None
        self.defaults = []            # defaults: <regola> (list[Rule])
        self.rules = []               # include:/exclude: in ordine (list[Rule])
        self.discarded = 0            # righe scartate come invalide (per doctor)


_SYNC_KEY = re.compile(r'(name|defaults|include|exclude)\s*:\s*(.*)')
_CONF_KEY = re.compile(r'([A-Za-z_]+)\s*:\s*(.*)')
# Riga a forma di chiave (`dest:`, `-dest:`, `foo:`): NON è una riga nuda.
_KEY_SHAPE = re.compile(r'[A-Za-z_-][A-Za-z0-9_-]*\s*:')


def bare_rule(line):
    """Riga nuda stile gitignore → regola (SPEC.md §4.2): `pattern` include,
    `!pattern` exclude. Attenzione: è l'**inverso di gitignore** — qui la riga
    elenca i file da COPIARE. None se il pattern è vuoto."""
    neg = line.startswith('!')
    pat = line[1:].strip() if neg else line
    if not pat:
        return None
    return parse_rule(f'{"exclude" if neg else "include"}: {pat}')


def parse_sync(text, *, log_level='info', origin='<.sync>'):
    """Parse di un file `.sync` → SyncConfig. Nessuna dest: le destinazioni
    vivono solo in `~/.safekeep`, quindi `dest:`/`-dest:` restano righe invalide
    (SPEC.md §4.2)."""
    cfg = SyncConfig()

    def invalid(lineno, detail):
        cfg.discarded += 1
        msg = f'{origin}:{lineno}: {detail}'
        if log_level == 'debug':
            raise ConfigError(msg)
        log.warning('%s', msg)

    for lineno, raw in enumerate(text.splitlines(), 1):
        line = strip_comment(raw).strip()
        if not line:
            continue
        m = _SYNC_KEY.match(line)
        if not m:
            if _KEY_SHAPE.match(line):
                # chiave ignota / dest: → riga invalida (SPEC.md §4.2)
                invalid(lineno, f'riga non valida: {raw.strip()}')
                continue
            try:
                rule = bare_rule(line)      # riga nuda stile gitignore
            except ValueError as e:
                # pattern da `.sync` non attendibile (regex invalida, NUL, …):
                # warning + skip con info, ConfigError con debug (CR-03)
                invalid(lineno, str(e))
                continue
            if rule is None:
                invalid(lineno, 'pattern vuoto')
                continue
            cfg.rules.append(rule)          # stessa lista, stesso ordine
            continue
        key, val = m.group(1), m.group(2).strip()
        if key in ('include', 'exclude', 'defaults'):
            try:
                rule = parse_rule(val if key == 'defaults' else line)
            except ValueError as e:
                invalid(lineno, str(e))
                continue
            if rule is None:
                invalid(lineno, 'regola vuota')
                continue
            (cfg.defaults if key == 'defaults' else cfg.rules).append(rule)
        elif key == 'name':
            if val:
                cfg.name = val
            else:
                invalid(lineno, 'name vuoto')
        elif not val:
            invalid(lineno, f'{key} senza valore')
    return cfg


def parse_config(text, origin='config'):
    """Parse del config globale → GlobalConfig. Errori fatali sempre (fail-fast).

    `source` è opzionale (nessuna riga → modalità auto-discovery da `$HOME`,
    SPEC.md §6); `dest` è obbligatoria: è l'unico posto dove vivono le
    destinazioni (SPEC.md §3)."""
    cfg = GlobalConfig()
    for lineno, raw in enumerate(text.splitlines(), 1):
        line = strip_comment(raw).strip()
        if not line:
            continue
        m = _CONF_KEY.match(line)
        if not m:
            raise ConfigError(f'{origin}:{lineno}: riga non valida: {raw.strip()}')
        key, val = m.group(1), m.group(2).strip()
        if '\0' in val:
            # un valore con NUL dal config globale esploderebbe più avanti in
            # modo opaco (os.path/Popen): fail-fast qui, con numero di riga (CR-03)
            raise ConfigError(f'{origin}:{lineno}: valore con carattere NUL: {key}')
        if key == 'source':
            if not val:
                raise ConfigError(f'{origin}:{lineno}: source vuota')
            cfg.sources.append(os.path.expanduser(val))
        elif key == 'dest':
            if not val:
                raise ConfigError(f'{origin}:{lineno}: dest vuota')
            cfg.dests.append(os.path.expanduser(val))
        elif key == 'layout':
            if val not in ('relative', 'full'):
                raise ConfigError(f'{origin}:{lineno}: layout non valido: {val!r} (relative|full)')
            cfg.layout = val
        elif key == 'defaults':
            try:
                rule = parse_rule(val)
            except ValueError as e:
                raise ConfigError(f'{origin}:{lineno}: {e}') from None
            if rule is None:
                raise ConfigError(f'{origin}:{lineno}: defaults senza regola')
            cfg.defaults.append(rule)
        elif key == 'log_level':
            if val not in ('debug', 'info', 'warn', 'error'):
                raise ConfigError(f'{origin}:{lineno}: log_level non valido: {val!r}')
            cfg.log_level = val
        else:
            raise ConfigError(f'{origin}:{lineno}: chiave sconosciuta: {key}')
    if not cfg.dests:
        raise ConfigError(f'{origin}: nessuna dest: serve almeno una riga '
                          f'`dest: <path>` (esempio: examples/safekeep.example)')
    return cfg


def source_root_for(sources, path):
    """La `source` configurata più profonda che contiene `path`; `path` se nessuna.

    È la radice da cui si calcola il path dest in layout `relative` (SPEC.md §4.3):
    il path dest include così il segmento del progetto (`backup/myapp/docs/a.md`)
    e due progetti sotto la stessa sorgente non si sovrappongono.
    """
    p = os.path.realpath(os.path.abspath(os.path.expanduser(path)))
    best, best_len = p, -1
    for src in sources:
        s = os.path.realpath(os.path.abspath(os.path.expanduser(src)))
        if (p == s or p.startswith(s + os.sep)) and len(s) > best_len:
            best, best_len = s, len(s)
    return best


def dest_path(dest_root, source_root, relpath, layout):
    """Path di destinazione (SPEC.md §4.3): relative → dest/<relativo alla
    radice `source>`, full → dest/<path assoluto senza / iniziale>.

    `source_root` è la radice `source` del config che contiene il progetto
    (`source_root_for`), NON la root del progetto: con una dest condivisa,
    `~/Code/myapp/docs/a.md` → `backup/myapp/docs/a.md` e due progetti
    (`projA`, `projB`) con lo stesso file relativo non si sovrappongono.
    """
    if layout not in ('relative', 'full'):
        raise ValueError(f'layout non valido: {layout!r} (relative|full)')
    src = os.path.abspath(os.path.expanduser(source_root))
    p = relpath if os.path.isabs(relpath) else os.path.join(src, relpath)
    p = os.path.abspath(os.path.expanduser(p))
    rel = os.path.relpath(p, os.sep) if layout == 'full' else os.path.relpath(p, src)
    return os.path.normpath(os.path.join(os.path.expanduser(dest_root), rel))


def validate_dests(sources, dests):
    """Errore se una dest è dentro una sorgente → loop di copia infinito (SPEC.md §11)."""
    def real(p):
        return os.path.realpath(os.path.abspath(os.path.expanduser(p)))
    for dest in dests:
        d = real(dest)
        for src in sources:
            s = real(src)
            if d == s or d.startswith(s + os.sep):
                raise ConfigError(f'dest {d} è dentro la sorgente {s}: loop di copia infinito')


# Modalità auto-discovery (SPEC.md §6): dir da potare durante lo scan di $HOME.
# Le nascoste (iniziano con `.`) sono potate a monte, qui elencate per chiarezza
# `.Trash` e `.cache` (e per `venv`, non coperto dai builtin).
# Ultime sette: cartelle protette dalla privacy di macOS (TCC = Transparency,
# Consent and Control). Senza "Accesso completo al disco" l'`opendir()` su una
# di queste resta bloccato nel kernel — niente errore, niente prompt per un
# agent launchd — e il discovery non finirebbe mai (SPEC.md §6).
HOME_PRUNE_DIRS = frozenset({
    'Library', '.Trash', '.cache', 'node_modules', '.git', '.venv',
    '__pycache__', 'venv', 'Applications',
    'Desktop', 'Documents', 'Downloads', 'Movies', 'Music', 'Pictures',
    'Public',
})


def home_prune(rel):
    """Pruning dell'auto-discovery: True = non scendere in questa directory.

    `rel` è il path relativo alla radice con `/` come separatore; la radice
    stessa (rel '.') non viene mai potata.
    """
    name = rel.rsplit('/', 1)[-1]
    return name.startswith('.') or name in HOME_PRUNE_DIRS


def discover_projects(sources, auto=False):
    """Directory che contengono `.sync` sotto ogni radice (scan con pruning).

    `auto=True` (modalità auto-discovery, scan della `$HOME`): si pota su
    `home_prune` — nessuna directory nascosta e niente `Library`/`.Trash`/…,
    così `.sync` dentro una di queste NON diventa progetto (SPEC.md §6).
    `auto=False` (modalità source): si pota sul verdict *esclusa* delle regole
    builtin. Scan ordinato (root prima dei sotto-progetti): i `.sync` annidati
    restano progetti a sé (SPEC.md §4.1).
    Il pruning è applicato ai *figli* prima di scendere: `os.walk` apre la
    directory (`opendir`) solo quando ci si entra, così una dir potata — in
    particolare una cartella protetta da TCC — non viene mai nemmeno aperta.
    """
    builtin = Matcher(BUILTIN_RULES)
    prune = home_prune if auto else (lambda rel: not builtin.evaluate(rel, True))
    found = []
    for src in sources:
        root = os.path.abspath(os.path.expanduser(src))
        if not os.path.isdir(root):
            log.warning('source inesistente o non è una directory: %s', src)
            continue
        for dirpath, dirnames, filenames in os.walk(root):
            rel = os.path.relpath(dirpath, root).replace(os.sep, '/')
            if '.sync' in filenames:
                found.append(dirpath)   # dir senza .sync non è progetto
            # pota i figli PRIMA di scendere: os.walk apre (opendir) una dir
            # solo quando ci si entra, così una dir esclusa — in particolare
            # una cartella protetta da TCC — non viene mai nemmeno aperta
            dirnames[:] = sorted(
                name for name in dirnames
                if not prune(name if rel == '.' else rel + '/' + name))
    return found
