"""Config globale `~/.safekeep` e file `.sync` di progetto (SPEC.md §3 e §4).

Le destinazioni vivono SOLO nel config globale: un `.sync` contiene solo
regole (`name:`, `defaults:`, `include:`/`exclude:`) — una riga `dest:` o
chiave ignota in un `.sync` è una riga invalida: con `log_level: info`
genera solo un warning e viene saltata, con `log_level: debug` è fatale
(SPEC.md §5). Il config globale è invece sempre fail-fast.
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
        self.sources = []             # radici osservate (ripetibili)
        self.dests = []               # destinazioni globali (ripetibili) — le UNICHE
        self.layout = 'relative'      # relative | full
        self.defaults = []            # regole a bassa priorità (list[Rule])
        self.log_level = 'info'       # debug | info | warn | error


class SyncConfig:
    def __init__(self):
        self.name = None
        self.defaults = []            # defaults: <regola> (list[Rule])
        self.rules = []               # include:/exclude: in ordine (list[Rule])


_SYNC_KEY = re.compile(r'(name|defaults|include|exclude)\s*:\s*(.*)')
_CONF_KEY = re.compile(r'([A-Za-z_]+)\s*:\s*(.*)')


def parse_sync(text, *, log_level='info', origin='<.sync>'):
    """Parse di un file `.sync` → SyncConfig. Nessuna dest: le destinazioni
    vivono solo in `~/.safekeep`, quindi `dest:`/`-dest:` sono righe invalide
    come le righe nude (SPEC.md §4.2)."""
    cfg = SyncConfig()

    def invalid(lineno, detail):
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
            invalid(lineno, f'riga non valida: {raw.strip()}')
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
    """Parse del config globale → GlobalConfig. Errori fatali sempre (fail-fast)."""
    cfg = GlobalConfig()
    for lineno, raw in enumerate(text.splitlines(), 1):
        line = strip_comment(raw).strip()
        if not line:
            continue
        m = _CONF_KEY.match(line)
        if not m:
            raise ConfigError(f'{origin}:{lineno}: riga non valida: {raw.strip()}')
        key, val = m.group(1), m.group(2).strip()
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


def discover_projects(sources):
    """Directory che contengono `.sync` sotto ogni radice (scan con pruning dei builtin)."""
    prune = Matcher(BUILTIN_RULES)
    found = []
    for src in sources:
        root = os.path.abspath(os.path.expanduser(src))
        if not os.path.isdir(root):
            log.warning('source inesistente o non è una directory: %s', src)
            continue
        for dirpath, dirnames, filenames in os.walk(root):
            rel = os.path.relpath(dirpath, root).replace(os.sep, '/')
            if rel != '.' and not prune.evaluate(rel, True):
                dirnames[:] = []        # pota: niente scan sotto dir esclusa
                continue
            if '.sync' in filenames:
                found.append(dirpath)   # dir senza .sync non è progetto
            dirnames.sort()
    return found
