"""Copia atomica, stability check e reconcile (SPEC.md §7 e §10).

Semantica: solo copia/aggiorna, mai cancellare. `copy_one` restituisce False
quando la copia non è fattibile in questo momento (sorgente instabile o
sparita) mentre `OSError` di I/O (ENOSPC/EIO/EBUSY/ENODEV) viene rilanciato
com'è: a decidere il retry è il caller.
"""
import logging
import os
import shutil
import time

log = logging.getLogger('safekeep')

STABILITY_PAUSE = 0.2      # secondi fra i due stat (SPEC.md §7.1)
STABILITY_RETRIES = 2      # ritentativi extra se il file cambia fra i due stat
MTIME_TOLERANCE = 1.0      # secondi: tolleranza mtime per volumi a risoluzione limitata
TMP_INFIX = '.safekeep.tmp.'


def _tmp_path(dst):
    """Tmp sempre nella cartella di dst (stesso filesystem ⇒ os.replace atomico)."""
    return dst + TMP_INFIX + str(os.getpid())


def _remove(path):
    try:
        os.unlink(path)
    except OSError:
        pass


def _fsync_best_effort(path):
    try:
        fd = os.open(path, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    except OSError as e:
        log.debug('fsync best-effort non riuscito su %s: %s', path, e)


def _stable(src):
    """Due `os.stat` separati con (size, mtime_ns) identici ⇒ file fermo."""
    for _ in range(1 + STABILITY_RETRIES):
        try:
            s1 = os.stat(src)
        except FileNotFoundError:
            return False
        time.sleep(STABILITY_PAUSE)
        try:
            s2 = os.stat(src)
        except FileNotFoundError:
            return False
        if (s1.st_size, s1.st_mtime_ns) == (s2.st_size, s2.st_mtime_ns):
            return True
        log.debug('stability check fallito su %s: il file è in scrittura', src)
    return False


def _copy_symlink(src, dst):
    """Ricrea il symlink senza seguirlo (SPEC.md §7.3), anche se rotto."""
    try:
        target = os.readlink(src)
    except OSError:
        return False                      # symlink sparito fra islink e readlink
    dirname = os.path.dirname(dst)
    if dirname:
        os.makedirs(dirname, exist_ok=True)
    tmp = _tmp_path(dst)
    _remove(tmp)                          # residuo di un crash precedente
    try:
        os.symlink(target, tmp)
        os.replace(tmp, dst)
    except OSError:
        _remove(tmp)
        raise
    log.debug('symlink %s → %s (target %r)', src, dst, target)
    return True


def copy_one(src: str, dst: str) -> bool:
    """Copia atomica src → dst. True se copiato, False se da ritentare più tardi.

    - src sparito (FileNotFoundError) → False silenzioso: la policy "mai
      cancellare" conserva già il backup esistente;
    - OSError di I/O → rilanciato così com'è (il caller decide il retry);
    - chmod/utime non riusciti (volumi exFAT) → non fatali (log debug).
    """
    if os.path.islink(src):
        return _copy_symlink(src, dst)
    if not _stable(src):
        return False                      # instabile o sparito
    dirname = os.path.dirname(dst)
    if dirname:
        os.makedirs(dirname, exist_ok=True)
    tmp = _tmp_path(dst)
    _remove(tmp)                          # residuo di un crash precedente
    try:
        shutil.copyfile(src, tmp)         # solo bytes: ENOSPC/EIO salgono
        try:
            shutil.copystat(src, tmp)     # mtime + permessi
        except OSError as e:
            log.debug('copystat non riuscito su %s: %s', tmp, e)
        _fsync_best_effort(tmp)
        os.replace(tmp, dst)
    except FileNotFoundError:
        _remove(tmp)
        log.debug('sorgente sparita durante la copia: %s', src)
        return False
    except OSError:
        _remove(tmp)
        raise
    return True


def needs_copy(src: str, dst: str) -> bool:
    """True se dst mancante o (size, mtime) diversi — tolleranza mtime 1s."""
    try:
        s = os.stat(src)
    except FileNotFoundError:
        return False                      # niente da copiare
    try:
        d = os.stat(dst)
    except FileNotFoundError:
        return True
    if s.st_size != d.st_size:
        return True
    return abs(s.st_mtime - d.st_mtime) > MTIME_TOLERANCE


def reconcile_project(source_root, dest_root, matcher, layout, dest_path_fn,
                      stats=None, dry_run=False, dest_base=None) -> int:
    """Walk della sorgente con pruning delle dir con verdict escluso; copia ogni
    file con verdict incluso che differisce (un file non matchato da nessuna
    regola NON viene copiato: allow-list, SPEC.md §4.3). Ritorna il numero di copie.

    `dest_base`: radice da cui calcolare il path dest (default: la radice del
    walk). Con layout `relative` passa la `source` del config che contiene il
    progetto (SPEC.md §4.3): il path dest include il segmento del progetto e
    due progetti diversi non si sovrappongono. `stats` (opzionale, dict
    aggiornato in place): `copiati` si somma su più dest (passa lo stesso dict
    per ogni dest), `esclusi` = file con verdict escluso incontrati nel walk
    (indipendente dalla dest), `pianificati` = [(src, dst)] con `dry_run=True`.
    `dry_run`: conta ed elenca senza copiare.
    """
    root = os.path.abspath(os.path.expanduser(source_root))
    base_root = os.path.abspath(os.path.expanduser(dest_base)) if dest_base else root
    prefix = os.path.relpath(root, base_root)   # radice del walk rispetto a base_root
    copied = 0
    excluded = 0
    plan = stats.setdefault('pianificati', []) if (stats is not None and dry_run) else None
    for dirpath, dirnames, filenames in os.walk(root):
        rel = os.path.relpath(dirpath, root)
        base = '' if rel == os.curdir else rel.replace(os.sep, '/') + '/'
        keep = []
        for name in sorted(dirnames):
            full = os.path.join(dirpath, name)
            r = base + name
            if os.path.islink(full):
                # dir symlink: nessun descend, ricreata come symlink (SPEC.md §7.3).
                # È una FOGLIA: va valutata come file, altrimenti il default
                # allow-list "nessun match → True" (che serve a far attraversare
                # le directory) la copierebbe ignorando ogni include (§4.3).
                if matcher.evaluate(r, False):
                    dst = dest_path_fn(dest_root, base_root,
                                       os.path.join(prefix, r), layout)
                    if dry_run or copy_one(full, dst):
                        copied += 1
                        if plan is not None:
                            plan.append((full, dst))
                continue
            if matcher.evaluate(r, True):        # False → pota il sottoalbero
                keep.append(name)
        dirnames[:] = keep
        for name in filenames:
            r = base + name
            if not matcher.evaluate(r, False):
                excluded += 1                    # non incluso → nessuna copia
                continue
            src = os.path.join(dirpath, name)
            dst = dest_path_fn(dest_root, base_root,
                               os.path.join(prefix, r), layout)
            if needs_copy(src, dst) and (dry_run or copy_one(src, dst)):
                copied += 1
                if plan is not None:
                    plan.append((src, dst))
    if stats is not None:
        stats['copiati'] = stats.get('copiati', 0) + copied
        stats['esclusi'] = excluded
    return copied
