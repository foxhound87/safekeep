"""Matcher gitignore-like con semantica last-match-wins (SPEC.md §4.3 e §5).

Ordine di priorità dei layer (dal basso all'alto): builtin → globali →
defaults del progetto → regole del `.sync`. L'ultima regola che matcha il
path (o un suo genitore directory) decide il verdict; nessun match →
semantica allow-list: file NON copiato, directory attraversata.
"""
import re


def strip_comment(line):
    """Rimuove i commenti: `# ...` a inizio riga (escape `\\#`) o ` # ...` inline."""
    if line.lstrip().startswith('#'):
        return ''
    return re.split(r'\s#', line, maxsplit=1)[0]


def _charclass(pat, i):
    """`[abc]`/`[!abc]` a pat[i] → (regex, indice dopo) o None se malformed."""
    j = i + 1
    neg = False
    if j < len(pat) and pat[j] in '!^':
        neg = True
        j += 1
    if j < len(pat) and pat[j] == ']':   # ']' come primo carattere è letterale
        j += 1
    while j < len(pat) and pat[j] != ']':
        j += 1
    if j >= len(pat):
        return None
    body = re.sub(r'([\\\[\]])', r'\\\1', pat[i + 1:j])
    return ('[^' + body + ']' if neg else '[' + body + ']'), j + 1


def _glob(pat):
    """Pattern gitignore → corpo di regex (ancorata dal chiamante)."""
    out = []
    i, n = 0, len(pat)
    while i < n:
        if pat[i] == '\\' and i + 1 < n:        # \x escape generico
            out.append(re.escape(pat[i + 1]))
            i += 2
        elif pat[i:i + 2] == '**':
            if pat[i:i + 3] == '**/':           # a/**/b → segmenti multipli opzionali
                out.append('(?:[^/]+/)*')
                i += 3
            else:
                out.append('.*')                # ** → qualsiasi segmenti (include /)
                i += 2
        elif pat[i] == '*':                     # * entro un singolo segmento
            out.append('[^/]*')
            i += 1
        elif pat[i] == '?':
            out.append('[^/]')
            i += 1
        elif pat[i] == '[':
            cls = _charclass(pat, i)
            if cls is None:
                out.append('\\[')
                i += 1
            else:
                out.append(cls[0])
                i = cls[1]
        else:
            out.append(re.escape(pat[i]))
            i += 1
    return ''.join(out)


class Rule:
    """Singola regola `include:`/`exclude:` con pattern gitignore-like."""

    def __init__(self, pattern, include):
        if '\0' in pattern:
            # un pattern con NUL arriva da un `.sync` non attendibile e finirebbe
            # in argv di fswatch → ValueError "embedded null byte" (CR-03)
            raise ValueError(f'pattern contiene un carattere NUL: {pattern!r}')
        self.pattern = pattern
        self.include = include
        self.dir_only = pattern.endswith('/')          # foo/ → solo directory
        p = pattern[:-1] if self.dir_only else pattern
        self.anchored = '/' in p                       # /foo o slash interna → alla root
        if p.startswith('/'):
            p = p[1:]
        # dir/** matcha anche dir stessa, così il pruning la esclude per intero
        body = _glob(p[:-3]) + '(?:/.*)?' if p.endswith('/**') else _glob(p)
        try:
            self._re = re.compile('(?:' + body + r')\Z')
        except re.error as e:
            # `re.error` NON è un ValueError: un pattern malevolo/esotico
            # (es. `[z-a]*`) va convertito così il parser lo gestisce come
            # riga invalida invece di far crashare il daemon (CR-03)
            raise ValueError(f'pattern regex non valido: {pattern!r} ({e})') from None

    def matches(self, path, is_dir=False):
        path = path.strip('/')
        if not path:
            return False
        parts = path.split('/')
        last = len(parts) - 1
        for i, part in enumerate(parts):
            if i == last and self.dir_only and not is_dir:
                continue                             # foo/ non matcha un file chiamato foo
            candidate = '/'.join(parts[:i + 1]) if self.anchored else part
            if self._re.match(candidate):
                return True                          # path o genitore directory
        return False


def parse_rule(line):
    """Rule da `include: <pattern>`/`exclude: <pattern>`; None per riga vuota/commento."""
    s = strip_comment(line).strip()
    if not s:
        return None
    m = re.match(r'(include|exclude)\s*:\s*(.*)\Z', s)
    if not m:
        raise ValueError(f'riga non valida (atteso include:/exclude:): {line.strip()!r}')
    pattern = m.group(2).strip()
    if not pattern:
        raise ValueError(f'pattern vuoto: {line.strip()!r}')
    return Rule(pattern, m.group(1) == 'include')


def parse_rules(lines, origin='<rules>'):
    """Layer di regole da una lista di righe; riga invalida → errore con numero di riga."""
    rules = []
    for lineno, raw in enumerate(lines, 1):
        try:
            rule = parse_rule(raw)
        except ValueError as e:
            raise ValueError(f'{origin}:{lineno}: {e}') from None
        if rule is not None:
            rules.append(rule)
    return rules


class Matcher:
    """Layer di regole in ordine di priorità (bassa → alta), last-match-wins."""

    def __init__(self, *layers):
        self.rules = [rule for layer in layers for rule in layer]

    def evaluate(self, relpath: str, is_dir: bool = False) -> bool:
        """True = copiare. Ultima regola che matcha decide; nessun match →
        allow-list (SPEC.md §4.3): file → False (non copiato), directory →
        True (attraversata: si pota solo su un'esclusione esplicita)."""
        for rule in reversed(self.rules):
            if rule.matches(relpath, is_dir):
                return rule.include
        return is_dir


# Regole builtin (bassa priorità, sempre presenti — SPEC.md §5)
BUILTIN_RULES = parse_rules([
    'exclude: .git/',
    'exclude: node_modules/',
    'exclude: .venv/',
    'exclude: __pycache__/',
    'exclude: .DS_Store',
    'exclude: *.swp',
], origin='builtin')
