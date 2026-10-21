"""Ripristino `restore` (SPEC.md §19): inventario della dest, matching sui
path originali, piano dry-run ed esecuzione copy-only dentro le source.

Semantica: **ultima copia** (niente storia, §10.1/§18.2), nessuna
cancellazione, nessuna scrittura senza `--force` (la decisione spetta alla
CLI). Containment **invertito** rispetto al CR-01 (§7.2/§11): il target è il
path originale e restore scrive solo dentro le source del progetto — a
differenza di `_make_contained` (lato dest) qui non si rimuove mai un
componente-symlink: la sorgente è roba dell'utente, si rifiuta e basta.
"""
import errno
import fnmatch
import logging
import os
from collections import namedtuple

from .config import dest_path as fs_dest_path
from .copier import MTIME_TOLERANCE, copy_one, needs_copy
from .platform import NETWORK_FSTYPES, fs_fstype
from .volumes import dest_state

log = logging.getLogger('safekeep')

GLOB_CHARS = '*?['

# backup = path (fs) o key (S3); rel = path relativo al prefisso del progetto
# ('/' come separatore, in entrambi i layout); info = (size, metadata) per S3,
# None per fs
Entry = namedtuple('Entry', 'backup target project rel info')


class RestoreError(ValueError):
    """Errore di restore non-I/O (origine non determinabile, config, usage):
    la CLI stampa il messaggio su stderr ed esce ≠ 0."""


def candidates(cfg, s3=None):
    """Origini disponibili, in ordine di config: [(kind, origin, label)].

    label: `local` = prima `dest:`, `nas` = seconda `dest:`, `destN` le altre,
    `s3` = dest S3 (§19.3). Una dest non montata non è un candidato — ma
    `pick_source` la fallisce in modo esplicito (mai ignorata in silenzio).
    """
    out = []
    for i, d in enumerate(cfg.dests):
        if dest_state(d) == 'ok':
            label = ['local', 'nas'][i] if i < 2 else f'dest{i + 1}'
            out.append(('fs', d, label))
    if s3 is not None:
        out.append(('s3', s3, 's3'))
    return out


def pick_source(cfg, s3=None, from_=None):
    """→ (kind, origin) secondo §19.3: `--from` esplicito oppure auto-detect
    del candidato unico; ≥2 candidati → errore con l'elenco; una `dest:`
    dichiarata ma non montata → errore (montare e riprovare)."""
    absent = [d for d in cfg.dests if dest_state(d) != 'ok']
    if from_ is None and absent:
        raise RestoreError('dest non montata: ' + ', '.join(absent)
                           + ' — monta il volume e riprova (SPEC §19.3)')
    if from_ is not None:
        fixed = {'local': 0, 'nas': 1}.get(from_)
        if fixed is not None:
            if fixed >= len(cfg.dests):
                raise RestoreError(f'nessuna dest {from_!r} nel config — '
                                   f'candidati: {_labels(cfg, s3)}')
            d = cfg.dests[fixed]
            if dest_state(d) != 'ok':
                raise RestoreError(f'dest {d} non montata: '
                                   'monta il volume e riprova (SPEC §19.3)')
            return 'fs', d
        if s3 is None:
            raise RestoreError(f'nessuna dest S3 nel config — '
                               f'candidati: {_labels(cfg, s3)}')
        return 's3', s3
    cands = candidates(cfg, s3)
    if not cands:
        raise RestoreError('nessuna origine di restore disponibile '
                           '(nessuna dest nel config)')
    if len(cands) > 1:
        raise RestoreError('origine ambigua: ' + _labels(cfg, s3)
                           + ' — ripeti con --from {local,s3,nas}')
    return cands[0][0], cands[0][1]


def _labels(cfg, s3):
    cands = candidates(cfg, s3)
    return ', '.join(label for _, _, label in cands) or 'nessuna'


# --------------------------------------------------------------- inventario

def inventory(projects, kind, origin, layout):
    """→ (entries, orfani): voci di backup sotto il prefisso di un progetto
    (col mapping inverso dest→target, §19.5) e voci orfane (warn + skip)."""
    if kind == 's3':
        return _inventory_s3(origin, projects, layout)
    return _inventory_fs(origin, projects, layout)


def _project_prefixes(projects, kind, origin, layout):
    """[(prefisso, project)]: dir dest (fs) o key prefix (S3) di ogni progetto."""
    out = []
    for project in projects:
        if kind == 's3':
            prefix = origin.dest_path(origin.root, project.source_root,
                                      project.root, layout)
            prefix = prefix.rstrip('/') + '/'
        else:
            prefix = os.path.realpath(fs_dest_path(
                origin, project.source_root, project.root, layout))
        out.append((prefix, project))
    return out


def _inventory_fs(dest_root, projects, layout):
    """Walk della dest con pruning delle dir-symlink (foglie, come
    `prune_project`, §9): sotto il prefisso di un progetto → entry,
    altrove → orfano."""
    prefixes = _project_prefixes(projects, 'fs', dest_root, layout)
    entries, orphans = [], []
    for dirpath, dirnames, filenames in os.walk(dest_root):
        for name in list(dirnames):
            full = os.path.join(dirpath, name)
            if os.path.islink(full):
                dirnames.remove(name)       # dir-symlink: foglia, mai scendere
                _place(full, prefixes, entries, orphans)
        for name in filenames:
            _place(os.path.join(dirpath, name), prefixes, entries, orphans)
    return entries, orphans


def _place(path, prefixes, entries, orphans):
    real = os.path.realpath(path)
    for proj_dst, project in prefixes:
        if real == proj_dst or real.startswith(proj_dst + os.sep):
            rel = os.path.relpath(real, proj_dst).replace(os.sep, '/')
            target = os.path.normpath(os.path.join(
                project.root, rel.replace('/', os.sep)))
            entries.append(Entry(path, target, project, rel, None))
            return
    orphans.append(path)


def _inventory_s3(s3, projects, layout):
    """Enumerazione S3 (`iter_keys`, §18/§19): key sotto il prefix del progetto
    → entry; altrove → orfano."""
    prefixes = _project_prefixes(projects, 's3', s3, layout)
    rest = s3.root[len('s3://'):]            # 'bkt/pref' → key prefix 'pref/'
    root_key = rest.partition('/')[2]
    pfx = root_key.rstrip('/') + '/' if root_key else ''
    entries, orphans = [], []
    for key, size, md in s3.iter_keys(pfx):
        for proj_pfx, project in prefixes:
            if key.startswith(proj_pfx):
                rel = key[len(proj_pfx):]
                target = os.path.normpath(os.path.join(
                    project.root, rel.replace('/', os.sep)))
                entries.append(Entry(key, target, project, rel, (size, md)))
                break
        else:
            orphans.append(key)
    return entries, orphans


# ----------------------------------------------------------------- matching

def matches(target, source_root, patterns):
    """§19.2: ogni argomento è path assoluto, path relativo alla radice
    `source` del progetto, o glob `fnmatch` (su entrambe le forme)."""
    rel = os.path.relpath(target, source_root)
    for pat in patterns:
        p = os.path.expanduser(pat)
        if any(c in p for c in GLOB_CHARS):
            if fnmatch.fnmatch(target, p) or fnmatch.fnmatch(rel, p):
                return True
        else:
            n = os.path.normpath(p)
            if n == target or (not os.path.isabs(p) and n == rel):
                return True
    return False


def select(entries, patterns):
    """Entry il cui target matcha almeno un argomento (§19.2, nessun → exit 1)."""
    return [e for e in entries
            if matches(e.target, e.project.source_root, patterns)]


def first_level(entries):
    """→ [(voce, [entry...])]: raggruppamento per prima componente del `rel`
    (voci di primo livello del backup, §19.4), in ordine di apparizione."""
    groups = {}
    for e in entries:
        groups.setdefault(e.rel.split('/', 1)[0], []).append(e)
    return list(groups.items())


# -------------------------------------------------------------------- piano

def need_restore(entry):
    """True se il backup differisce dal target (o manca) — semantica §10.1."""
    if os.path.islink(entry.backup):
        # symlink: confronto del target del link (needs_copy seguirebbe il
        # link e un backup rotto sembrerebbe "già aggiornato")
        try:
            return not (os.path.islink(entry.target)
                        and os.readlink(entry.target) == os.readlink(entry.backup))
        except OSError:
            return True
    if entry.info is not None:               # S3: (size, metadata lowercase)
        size, md = entry.info
        if md.get('symlink') is not None:    # object zero-byte = symlink (§18.2)
            try:
                return os.readlink(entry.target) != md['symlink']
            except OSError:
                return True
        try:
            d = os.stat(entry.target)
        except FileNotFoundError:
            return True
        if d.st_size != size:
            return True
        try:
            return abs(int(md.get('mtime')) - d.st_mtime_ns) > int(MTIME_TOLERANCE * 1e9)
        except (TypeError, ValueError):
            return True
    return needs_copy(entry.backup, entry.target)


def plan(entries):
    """→ (righe, sintesi): `<backup> -> <target> (crea|sovrascrive)` per le
    entry che differiscono; già identiche → conteggiate in `aggiornati`."""
    rows, created, overwritten, updated = [], 0, 0, 0
    for e in entries:
        if not need_restore(e):
            updated += 1
            continue
        if os.path.lexists(e.target):
            rows.append((e.backup, e.target, '(sovrascrive)'))
            overwritten += 1
        else:
            rows.append((e.backup, e.target, '(crea)'))
            created += 1
    return rows, {'da_copiare': created + overwritten, 'crea': created,
                  'sovrascrive': overwritten, 'aggiornati': updated}


# ---------------------------------------------------------------- esecuzione

def execute(entries, s3=None):
    """Copia i backup sui target — chiamata SOLO con `--force` (§19.1).

    Containment invertito (§11/§19.5): il parent (realpath) del target deve
    stare dentro la (realpath) della source del progetto, altrimenti EACCES +
    skip. fs → `copy_one` (atomico tmp + `os.replace`, §7.2); S3 → `download`.
    Mai delete. Ritorna (ok, errati)."""
    ok = err = 0
    for e in entries:
        src_root = os.path.realpath(e.project.source_root)
        parent = os.path.realpath(os.path.dirname(os.path.abspath(e.target)))
        if not (parent == src_root or parent.startswith(src_root + os.sep)):
            log.error('scrittura fuori dalle source bloccata: %s', e.target)
            err += 1
            continue
        try:
            if s3 is not None:
                s3.download(e.backup, e.target)
            else:
                copy_one(e.backup, e.target)
        except OSError as ex:
            log.error('restore fallito %s → %s: %s', e.backup, e.target, ex)
            if ex.errno in (errno.EACCES, errno.EPERM):
                log.error('permesso negato sul target: su macOS serve FDA '
                          '(Full Disk Access) per le cartelle protette '
                          '(SPEC §11/§14.4)')
            err += 1
            continue
        ok += 1
    return ok, err


def warn_network_sources(projects):
    """Info §19/§14.1: una source su filesystem di rete (smbfs/cifs/nfs) —
    I/O lento e semantica mount diversa da una fs locale."""
    for project in projects:
        fst = fs_fstype(project.source_root)
        if fst in NETWORK_FSTYPES:
            log.info('sorgente su filesystem di rete (%s): %s',
                     fst, project.source_root)
