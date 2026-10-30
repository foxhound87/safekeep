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
from dataclasses import dataclass, field

from .matcher import BUILTIN_RULES, Matcher, parse_rule, strip_comment

log = logging.getLogger('safekeep')


class ConfigError(ValueError):
    """Errore di config: il messaggio contiene sempre l'origine e la riga."""


@dataclass
class S3Config:
    """Blocco `s3_*` del config globale (SPEC.md §18.1). Solo una dest S3.

    `secret_key` è già **risolto** a parse-time: da `s3_secret_key` oppure
    dalla variabile `s3_secret_key_env` (assente → ConfigError fail-fast).
    """
    endpoint: str = None            # None → endpoint AWS default
    bucket: str = None
    prefix: str = ''                # normalizzato: niente / iniziale/finali
    region: str = 'us-east-1'
    access_key: str = None
    secret_key: str = None
    secret_env: str = None          # nome var se il secret viene da env
    addressing: str = 'path'        # path (default) | virtual
    tls_check: bool = True


@dataclass
class Route:
    """Riga `route <src>: <dest-list>` già validata (SPEC.md §21.2/§21.3).

    `dests` e `s3` sono i target risolti in ordine di config: path fs (con
    `~` espanso) e `(bucket, prefix)` S3. `lineno` serve alla diagnostica
    (route inutilizzata, §12).
    """
    src: str                        # sorgente normalizzata (abspath, no fs)
    dests: list = field(default_factory=list)   # path fs (str), ordine config
    s3: list = field(default_factory=list)      # (bucket, prefix), ordine config
    lineno: int = 0


class GlobalConfig:
    def __init__(self):
        self.sources = []             # radici osservate (ripetibili, opzionali)
        self.dests = []               # destinazioni globali (ripetibili) — le UNICHE
        self.dest_names = {}          # nome → path (dest <name>:, §21.2)
        self.routes = []              # list[Route] (§21), ordine di config
        self.s3 = None                # S3Config | None (una sola dest S3, §18)
        self.layout = 'relative'      # relative | full
        self.defaults = []            # regole a bassa priorità (list[Rule])
        self.log_level = 'info'       # debug | info | warn | error
        self.poll_interval = 60       # secondi fra due turni di polling (§20.3)


class SyncConfig:
    def __init__(self):
        self.name = None
        self.defaults = []            # defaults: <regola> (list[Rule])
        self.rules = []               # include:/exclude: in ordine (list[Rule])
        self.discarded = 0            # righe scartate come invalide (per doctor)


_SYNC_KEY = re.compile(r'(name|defaults|include|exclude)\s*:\s*(.*)')
# Chiave globale: inizia con lettera/_, poi [A-Za-z0-9_] (serve a `s3_endpoint`,
# cifra inclusa); una riga che non matcha resta "riga non valida" (fail-fast).
_CONF_KEY = re.compile(r'([A-Za-z_][A-Za-z0-9_]*)\s*:\s*(.*)')
# Riga a forma di chiave (`dest:`, `-dest:`, `foo:`): NON è una riga nuda.
_KEY_SHAPE = re.compile(r'[A-Za-z_-][A-Za-z0-9_-]*\s*:')
# `dest <name>: <path>` e `route <src>: <dest-list>` (SPEC.md §21.2): il ramo
# dedicato deve matchare PRIMA di _CONF_KEY, che di queste righe non cattura
# nemmeno il `:` (c'è un nome/`~` dopo la chiave) e le respingerebbe come
# righe invalide. Il `\s+` dopo la chiave tiene fuori `dest:`/`dest5:` —
# gestite da _CONF_KEY come sempre (le forme legacy restano invariate).
_DEST_NAMED = re.compile(r'dest\s+(.*)')
_ROUTE_LINE = re.compile(r'route\s+(.*)')
# nome di una destinazione nominata: token semplice, niente spazi/coloni
_DEST_NAME = re.compile(r'[A-Za-z0-9_-]+$')

# Chiavi del blocco S3 (SPEC.md §18.1): `s3_*` fuori da questo insieme è
# errore fail-fast, come ogni altra chiave ignota del config globale.
_S3_KEYS = frozenset({
    's3_endpoint', 's3_bucket', 's3_prefix', 's3_region', 's3_access_key',
    's3_secret_key', 's3_secret_key_env', 's3_addressing', 's3_tls_check',
})


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
    SPEC.md §6); serve almeno una `dest:` **oppure** `s3_bucket` (dest
    S3-compatible, SPEC.md §18 — backward compat: senza `s3_*` nulla cambia)."""
    cfg = GlobalConfig()
    s3raw = {}                      # chiave → (valore, riga) del blocco s3_*
    routes_raw = []                 # (riga, resto) delle righe `route` (§21.2)
    for lineno, raw in enumerate(text.splitlines(), 1):
        line = strip_comment(raw).strip()
        if not line:
            continue
        m = _ROUTE_LINE.match(line)
        if m:
            # risoluzione differita (dopo il ciclo): nomi dest e `s3_bucket`
            # possono essere dichiarati DOPO la route (forward reference)
            routes_raw.append((lineno, m.group(1)))
            continue
        m = _DEST_NAMED.match(line)
        if m and not m.group(1).lstrip().startswith(':'):
            _parse_named_dest(cfg, m.group(1), lineno, origin, raw)
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
        elif key == 'route':
            # `route: <dest-list>` senza sorgente: la forma corretta ha uno
            # spazio dopo la chiave ed è gestita da _ROUTE_LINE sopra
            raise ConfigError(f'{origin}:{lineno}: route senza sorgente '
                              f'(serve `route <src>: <dest-list>`)')
        elif key.startswith('s3_'):
            if key not in _S3_KEYS:
                raise ConfigError(f'{origin}:{lineno}: chiave s3 sconosciuta: {key}')
            s3raw[key] = (val, lineno)   # ultima occorrenza vince (come layout)
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
        elif key == 'poll_interval':
            # intero ≥ 1: sotto il minuto un turno di polling su un NAS è solo
            # I/O di rete (§20.3) — fail-fast con numero di riga, come ogni
            # altra chiave del config
            try:
                seconds = int(val)
            except ValueError:
                seconds = 0
            if seconds < 1:
                raise ConfigError(f'{origin}:{lineno}: poll_interval non valido: '
                                  f'{val!r} (intero ≥ 1)')
            cfg.poll_interval = seconds
        else:
            raise ConfigError(f'{origin}:{lineno}: chiave sconosciuta: {key}')
    cfg.s3 = _build_s3(s3raw, origin)
    _resolve_routes(cfg, routes_raw, origin)
    if not cfg.dests and (cfg.s3 is None or not cfg.s3.bucket):
        raise ConfigError(f'{origin}: nessuna dest: serve almeno una riga '
                          f'`dest: <path>` oppure `s3_bucket: <nome>` '
                          f'(esempio: examples/safekeep.example)')
    return cfg


def _parse_named_dest(cfg, rest, lineno, origin, raw):
    """`dest <name>: <path>` → destinazione nominata (SPEC.md §21.2).

    Il nome la rende citabile nelle route; il path (con `~` espanso) finisce
    in `cfg.dests` esattamente come una `dest:` anonima. Fail-fast con riga.
    """
    if '\0' in rest:
        raise ConfigError(f'{origin}:{lineno}: valore con carattere NUL: dest')
    name, colon, path = rest.partition(':')
    if not colon:
        raise ConfigError(f'{origin}:{lineno}: destinazione nominata malformata '
                          f'(serve `dest <nome>: <path>`): {raw.strip()}')
    name = name.strip()
    if not _DEST_NAME.match(name):
        raise ConfigError(f'{origin}:{lineno}: nome dest non valido: {name!r} '
                          f'(ammissibili: A-Za-z0-9_-)')
    path = path.strip()
    if not path:
        raise ConfigError(f'{origin}:{lineno}: dest {name!r} senza percorso')
    path = os.path.expanduser(path)
    if name in cfg.dest_names:
        raise ConfigError(f'{origin}:{lineno}: dest {name!r} già dichiarata')
    cfg.dest_names[name] = path
    cfg.dests.append(path)


def _s3_route_prefix(raw, lineno, origin):
    """Prefisso S3 di una route: LETTERALE (SPEC.md §21.2) — `s3_prefix`
    globale NON si applica — normalizzato come §18.1 (niente `/` ai bordi)."""
    prefix = raw.strip().strip('/')
    if not prefix:
        raise ConfigError(f'{origin}:{lineno}: prefisso S3 vuoto nella route')
    if '..' in prefix.split('/'):
        raise ConfigError(f'{origin}:{lineno}: prefisso S3 con "..": '
                          f'{raw!r} non è ammesso (SPEC.md §21.2)')
    return prefix


def _resolve_routes(cfg, raw_routes, origin):
    """Valida e risolve le righe `route` → `cfg.routes` (SPEC.md §21.3).

    Va DOPO il ciclo principale di `parse_config` (e dopo `_build_s3`): i
    nomi dest e `s3_bucket` possono essere dichiarati dopo la route.
    Ogni errore riporta origine e riga, come tutto il config globale.
    """
    if not raw_routes:
        return
    seen = {}                       # sorgente normalizzata → riga di dichiarazione
    for lineno, rest in raw_routes:
        if '\0' in rest:
            raise ConfigError(f'{origin}:{lineno}: valore con carattere NUL: route')
        src_raw, colon, destlist = rest.partition(':')
        if not colon:
            raise ConfigError(f'{origin}:{lineno}: route malformata '
                              f'(serve `route <src>: <dest-list>`)')
        src_raw = src_raw.strip()
        if not src_raw:
            raise ConfigError(f'{origin}:{lineno}: route senza sorgente')
        src = os.path.expanduser(src_raw)
        if not os.path.isabs(src):
            raise ConfigError(f'{origin}:{lineno}: route sorgente non assoluta: '
                              f'{src_raw!r}')
        src = os.path.abspath(src)
        if src in seen:
            # stessa sorgente dichiarata due volte → ambiguità (§21.3); due
            # route che si SOVRAPPONGONO ma non coincidono restano legittime
            # (longest-prefix-wins, §21.3) — solo l'uguaglianza è errore
            raise ConfigError(f'{origin}:{lineno}: route duplicata per {src!r} '
                              f'(già a riga {seen[src]}): nessuna ambiguità '
                              f'tollerata (SPEC.md §21.3)')
        seen[src] = lineno
        if not destlist.strip():
            raise ConfigError(f'{origin}:{lineno}: route {src!r} senza destinazioni')
        route = Route(src=src, lineno=lineno)
        seen_tok = set()             # duplicati nella lista → set, ordine config
        for tok in (t.strip() for t in destlist.split(',')):
            if not tok:
                raise ConfigError(f'{origin}:{lineno}: route con destinazione vuota')
            if tok in seen_tok:
                continue
            seen_tok.add(tok)
            if tok.startswith('s3://'):
                bucket, slash, prefix = tok[len('s3://'):].partition('/')
                if not bucket or not slash:
                    raise ConfigError(f'{origin}:{lineno}: route s3:// malformata: '
                                      f'{tok!r} (serve `s3://<bucket>/<prefisso>`)')
                if cfg.s3 is None:
                    # §21.2: s3_access_key obbligatorio se esiste una route S3
                    raise ConfigError(f'{origin}:{lineno}: route S3 senza '
                                      f'credenziali: serve il blocco s3_* con '
                                      f's3_access_key (SPEC.md §18.1/§21.2)')
                route.s3.append((bucket, _s3_route_prefix(prefix, lineno, origin)))
            elif tok.startswith('s3:'):
                if cfg.s3 is None:
                    # forma breve: il bucket è PER DEFINIZIONE il globale (§21.7b)
                    raise ConfigError(f'{origin}:{lineno}: route {tok!r} senza '
                                      f's3_bucket: serve `s3_bucket: <nome>` nel '
                                      f'config (SPEC.md §21.2)')
                route.s3.append((cfg.s3.bucket,
                                 _s3_route_prefix(tok[3:], lineno, origin)))
            else:
                if tok not in cfg.dest_names:
                    raise ConfigError(f'{origin}:{lineno}: route verso '
                                      f'destinazione inesistente: {tok!r} — '
                                      f'dichiarala con `dest {tok}: <path>` '
                                      f'(SPEC.md §21.3)')
                route.dests.append(cfg.dest_names[tok])
        cfg.routes.append(route)


def route_matches(src, root):
    """True se `root` sta sotto `src`: confronto per SEGMENTI, `/` matcha tutto.

    Regola UNICA del match §21.3: la usa `dests_for_source` E il warning
    route inutilizzata (§12), così i due non divergono mai. Entrambi i lati
    sono già normalizzati dai caller (realpath, vedi `dests_for_source`).
    """
    return root == src or src == os.sep or root.startswith(src + os.sep)


def dests_for_source(root, cfg, project_root=None):
    """`(dest_fs, dest_s3)` per la sorgente `root` (SPEC.md §21.3).

    - `dest_fs`: path delle destinazioni filesystem **in ordine di config**;
    - `dest_s3`: target S3 come `(bucket, prefix)` in ordine di config.

    Matching *longest-prefix-wins* su **DUE LIVELLI** (§21.3): la route
    matcha se il suo sorgente è prefisso della source root `root` **O** della
    project root `project_root` (directory del `.sync`, opzionale), e fra
    tutti i match — su entrambi i livelli — vince il sorgente più lungo. Il
    secondo livello serve in auto-discovery, dove la source root di ogni
    progetto è `$HOME` e il routing per-progetto (`route ~/code: nas`) può
    agganciare solo le project root. Confronto per SEGMENTI (`/a/bc` non
    matcha `/a/b`), prefisso più lungo vincente. Nessuna route che matcha →
    TUTTE le destinazioni (anonime + nominate + S3 globale): retrocompat
    identica a 0.10.0, il routing è opt-in.

    Normalizzazione: `root`, `project_root` E il sorgente di ogni route
    vengono confrontati dopo **realpath + abspath + expanduser** su ENTRAMBI
    i lati. Il daemon passa le radici già realpathizzate (`Project.source_root`
    e `Project.root`), mentre la route arriva dal config col path come
    scritto (es. `/tmp`, che su macOS è un symlink per `/private/tmp`): con
    l'abspath di sola fase 1 il match falliva SILENTIOSAMENTE e il progetto
    andava in broadcast.

    Funzione pura solo per la logica (nessuna esistenza richiesta: realpath
    risolve i symlink esistenti e tiene il resto); il caller (daemon) passa
    le radici scoperte; la scoperta resta fuori da qui.
    """
    def canon(p):
        return os.path.realpath(os.path.abspath(os.path.expanduser(p)))

    candidates = [canon(root)]
    if project_root is not None:
        q = canon(project_root)
        if q not in candidates:
            candidates.append(q)
    best, best_src = None, None
    for r in cfg.routes:
        src = os.path.realpath(r.src)     # r.src è già expanduser+abspath a parse
        if (any(route_matches(src, c) for c in candidates)
                and (best_src is None or len(src) > len(best_src))):
            best, best_src = r, src
    if best is None:
        return (list(cfg.dests),
                [(cfg.s3.bucket, cfg.s3.prefix)] if cfg.s3 else [])
    return list(best.dests), list(best.s3)


def _build_s3(raw, origin):
    """Validazione fail-fast del blocco `s3_*` → S3Config (SPEC.md §18.1).

    `raw` = {chiave: (valore, riga)}: ogni errore riporta origine e riga.
    Ritorna None se nessuna chiave `s3_*` presente (config solo-fs invariata).
    """
    if not raw:
        return None

    def err(key, detail):
        # riga del campo mancante, altrimenti quella di s3_bucket (l'inizio
        # del blocco incompleto), altrimenti la prima riga s3_* presente
        entry = raw.get(key) or raw.get('s3_bucket') or next(iter(raw.values()))
        raise ConfigError(f'{origin}:{entry[1]}: {detail}')

    def get(key, default=''):
        return raw[key][0] if key in raw else default

    bucket = get('s3_bucket').strip()
    if not bucket:
        err('s3_bucket',
            's3_bucket vuota o assente: serve `s3_bucket: <nome>` '
            '(SPEC.md §18.1, almeno una dest: oppure s3_bucket)')
    access = get('s3_access_key').strip()
    if not access:
        err('s3_access_key',
            f's3_bucket senza s3_access_key: serve `s3_access_key: <chiave>` '
            f'(bucket {bucket!r})')
    secret, env = get('s3_secret_key'), get('s3_secret_key_env').strip()
    if secret and env:
        err('s3_secret_key',
            's3_secret_key e s3_secret_key_env insieme: ne va scelta una sola '
            '(SPEC.md §18.4)')
    if not secret and not env:
        err('s3_secret_key',
            's3_bucket senza credenziali: serve `s3_secret_key: <secret>` '
            '(chmod 600 il config) oppure `s3_secret_key_env: <NOME_VAR>`')
    if env:
        secret = os.environ.get(env)
        if secret is None:
            err('s3_secret_key_env',
                f's3_secret_key_env: variabile d\'ambiente {env!r} assente')
    prefix = get('s3_prefix').strip().strip('/')
    if '..' in prefix.split('/'):
        err('s3_prefix', f's3_prefix con "..": {get("s3_prefix")!r} non è ammesso')
    addressing = get('s3_addressing', 'path').strip()
    if addressing not in ('path', 'virtual'):
        err('s3_addressing',
            f's3_addressing non valido: {addressing!r} (path|virtual)')
    tls = get('s3_tls_check', 'true').strip()
    if tls not in ('true', 'false'):
        err('s3_tls_check', f's3_tls_check non valido: {tls!r} (true|false)')
    endpoint = get('s3_endpoint').strip()
    if endpoint and not endpoint.startswith(('http://', 'https://')):
        err('s3_endpoint',
            f's3_endpoint senza scheme http(s): {endpoint!r} '
            f'(esempio: https://s3.example.com:443)')
    if endpoint.startswith('http://'):
        log.warning('%s:%d: s3_endpoint cleartext %s: il secret e i dati '
                    'viaggiano in chiaro — usa https:// (SPEC.md §18.1)',
                    origin, raw['s3_endpoint'][1], endpoint)
    return S3Config(
        endpoint=endpoint or None,
        bucket=bucket,
        prefix=prefix,
        region=get('s3_region').strip() or 'us-east-1',
        access_key=access,
        secret_key=secret,
        secret_env=env or None,
        addressing=addressing,
        tls_check=(tls == 'true'),
    )


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


def discovery_prune(auto=False):
    """Callable di pruning dello scan di discovery (SPEC.md §4.1/§20.2).

    `auto=True` (auto-discovery dalla `$HOME`) → `home_prune`; `auto=False`
    (modalità source) → verdict *esclusa* delle regole builtin. Una sola
    fonte di verità: lo usa `discover_projects` E il poller delle root di
    rete, così un `.git`/`node_modules` non viene camminato in nessuno dei due.
    """
    builtin = Matcher(BUILTIN_RULES)
    return home_prune if auto else (lambda rel: not builtin.evaluate(rel, True))


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
    prune = discovery_prune(auto)
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
