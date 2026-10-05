"""Copia atomica, stability check e reconcile (SPEC.md §7 e §10).

Semantica: solo copia/aggiorna, mai cancellare. `copy_one` restituisce False
quando la copia non è fattibile in questo momento (sorgente instabile o
sparita, oppure scrittura fuori dest) mentre `OSError` di I/O
(ENOSPC/EIO/EBUSY/ENODEV) viene rilanciato com'è: a decidere il retry è il
caller. Nel reconcile ogni errore resta confinato al proprio file (CR-05).
"""
import errno
import logging
import os
import shutil
import time

log = logging.getLogger('safekeep')

STABILITY_PAUSE = 0.2      # secondi fra i due stat (SPEC.md §7.1)
STABILITY_RETRIES = 2      # ritentativi extra se il file cambia fra i due stat
MTIME_TOLERANCE = 1.0      # secondi: tolleranza mtime per volumi a risoluzione limitata
TMP_INFIX = '.safekeep.tmp.'
# errori che dicono "questa dest non è usabile adesso" → pending + backoff (§8.3)
MOUNT_ERRNOS = (errno.ENODEV, errno.EBUSY, errno.ENOSPC)
# throttle delle copie fallite (SPEC.md §10.1, 0.5.1): stessa (path, errno) →
# una riga + `— ripetuto N volte` ogni THROTTLE_INTERVAL, non una riga a fallimento
THROTTLE_INTERVAL = 300.0       # secondi
_copy_errors = {}               # (src, errno) → [ultimo log monotonic, non loggati]


def log_copy_error(src, dst, e, now=None):
    """`copia fallita src → dst: e` con dedup per **(path, errno)** (SPEC.md §10.1).

    Prima occorrenza: riga storica invariata. Poi al massimo una riga ogni
    `THROTTLE_INTERVAL` per chiave, col suffisso `— ripetuto N volte` (N =
    fallimenti non loggati da quell'ultima riga). `recent_permission_errors`
    (§14.4) somma N+1 su queste righe: `count_24h` resta il conteggio vero,
    sono solo le righe sul file a essere di meno. I conteggi in memoria
    (`stats['errati']`, `sync-once`) non leggono da qui.
    """
    if now is None:
        now = time.monotonic()
    key = (src, e.errno)
    entry = _copy_errors.get(key)
    if entry is None or now - entry[0] >= THROTTLE_INTERVAL:
        pending = 0 if entry is None else entry[1]
        if pending:
            log.error('copia fallita %s → %s: %s — ripetuto %d volte',
                      src, dst, e, pending)
        else:
            log.error('copia fallita %s → %s: %s', src, dst, e)
        _copy_errors[key] = [now, 0]
    else:
        entry[1] += 1


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


def _inside(path, root):
    return path == root or path.startswith(root + os.sep)


def _make_contained(dst, dest_root):
    """True se la directory di `dst` sta dentro `dest_root` (CR-01, SPEC.md §7.2).

    Un componente-symlink della dest che punta fuori è un artefatto di copie
    precedenti (§7.3 ricrea i link sorgente): è roba della dest → viene rimosso
    e ricreato come directory reale. Se dopo il ripristino la directory risolve
    ancora fuori → False: mai scrivere fuori dest.
    """
    base = os.path.abspath(os.path.expanduser(dest_root))
    target = os.path.dirname(os.path.abspath(dst))
    rel = os.path.relpath(target, base)
    if rel == os.pardir or rel.startswith(os.pardir + os.sep):
        return False                       # dst fuori dalla dest per costruzione
    root = os.path.realpath(base)
    cur = base
    for part in rel.split(os.sep):
        if part in ('', os.curdir):
            continue
        cur = os.path.join(cur, part)
        if os.path.islink(cur) and not _inside(os.path.realpath(cur), root):
            log.warning('symlink di dest fuori da %s: lo rimuovo (roba della dest): %s',
                        root, cur)
            try:
                os.unlink(cur)             # unlink NON segue il link: safe
            except OSError as e:
                log.error('impossibile rimuovere il symlink %s: %s', cur, e)
    return _inside(os.path.realpath(target), root)


def _copy_symlink(src, dst, dest_root=None):
    """Ricrea il symlink senza seguirlo (SPEC.md §7.3), anche se rotto."""
    try:
        target = os.readlink(src)
    except OSError:
        return False                      # symlink sparito fra islink e readlink
    if dest_root is not None and not _make_contained(dst, dest_root):
        log.error('scrittura fuori da dest bloccata: %s', dst)
        return False
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


def copy_one(src: str, dst: str, dest_root=None) -> bool:
    """Copia atomica src → dst. True se copiato, False se da ritentare più tardi.

    - src sparito (FileNotFoundError) → False silenzioso: la policy "mai
      cancellare" conserva già il backup esistente;
    - `dest_root` passato ⇒ containment check prima di ogni scrittura: mai
      scrivere fuori dalla dest via symlink (CR-01);
    - OSError di I/O → rilanciato così com'è (il caller decide il retry);
    - chmod/utime non riusciti (volumi exFAT) → non fatali (log debug).
    """
    if os.path.islink(src):
        return _copy_symlink(src, dst, dest_root)
    if not _stable(src):
        return False                      # instabile o sparito
    if dest_root is not None and not _make_contained(dst, dest_root):
        log.error('scrittura fuori da dest bloccata: %s', dst)
        return False
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
    per ogni dest), `skippati` = file già aggiornati, `errati` = copie fallite,
    `esclusi` = file con verdict escluso incontrati nel walk (indipendente
    dalla dest), `pianificati` = [(src, dst)] con `dry_run=True`, e
    `dest_pendente` = True se un errore di mount (ENODEV/EBUSY/ENOSPC) ha
    fermato la passata: il caller metta quella dest in pending (CR-05).
    `dry_run`: conta ed elenca senza copiare.

    Nessun errore I/O di un singolo file abortisce il walk: viene loggato e
    contato, e gli altri file vengono processati comunque.
    """
    root = os.path.abspath(os.path.expanduser(source_root))
    base_root = os.path.abspath(os.path.expanduser(dest_base)) if dest_base else root
    prefix = os.path.relpath(root, base_root)   # radice del walk rispetto a base_root
    copied = 0
    excluded = 0
    skipped = 0
    errors = 0
    mount_error = None
    plan = stats.setdefault('pianificati', []) if (stats is not None and dry_run) else None

    def failed(e, src, dst):
        """Conteggia l'errore; True ⇒ errore di mount: la dest va in pending."""
        nonlocal errors, mount_error
        errors += 1
        if e.errno in MOUNT_ERRNOS:
            mount_error = e
            log.error('errore di mount su %s → %s: %s: la dest va in pending',
                      src, dst, e)
            return True
        log_copy_error(src, dst, e)
        return False

    for dirpath, dirnames, filenames in os.walk(root):
        if mount_error is not None:
            break                          # dest in pending: il resto verrà copiato al remount
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
                    try:
                        if dry_run or copy_one(full, dst, dest_root):
                            copied += 1
                            if plan is not None:
                                plan.append((full, dst))
                    except OSError as e:
                        if failed(e, full, dst):
                            break
                continue
            if matcher.evaluate(r, True):        # False → pota il sottoalbero
                keep.append(name)
        dirnames[:] = keep
        if mount_error is not None:
            break                          # errore di mount nella dir-symlink
        for name in filenames:
            r = base + name
            if not matcher.evaluate(r, False):
                excluded += 1                    # non incluso → nessuna copia
                continue
            src = os.path.join(dirpath, name)
            dst = dest_path_fn(dest_root, base_root,
                               os.path.join(prefix, r), layout)
            try:
                if needs_copy(src, dst):
                    if dry_run or copy_one(src, dst, dest_root):
                        copied += 1
                        if plan is not None:
                            plan.append((src, dst))
                else:
                    skipped += 1                 # già aggiornato su questa dest
            except OSError as e:
                if failed(e, src, dst):
                    break
    if stats is not None:
        stats['copiati'] = stats.get('copiati', 0) + copied
        stats['skippati'] = stats.get('skippati', 0) + skipped
        stats['errati'] = stats.get('errati', 0) + errors
        stats['esclusi'] = excluded
        if mount_error is not None:
            stats['dest_pendente'] = True
    return copied


def prune_project(source_root, dest_root, matcher, layout, dest_path_fn,
                  dest_base=None, dry_run=False) -> int:
    """Rimuove dalla dest i file il cui sorgente esiste ancora ma che il
    matcher ora esclude (`sync-once --prune`, SPEC.md §9).

    Solo sotto il prefisso del progetto (niente tocco fuori) e solo dove la
    corrispondenza è certa: sorgente sparita o diventata directory → file
    intatto (policy "mai cancellare"). Ritorna il numero di rimozioni
    (anche con `dry_run=True`, dove conta quelle che verrebbe rimosso).
    """
    root = os.path.abspath(os.path.expanduser(source_root))
    base_root = os.path.abspath(os.path.expanduser(dest_base)) if dest_base else root
    prefix = os.path.relpath(root, base_root)
    proj_dst = dest_path_fn(dest_root, base_root, prefix, layout)
    # difesa: mai camminare (e tantomeno cancellare) fuori dalla dest (CR-01)
    if not _inside(os.path.realpath(proj_dst),
                   os.path.realpath(os.path.abspath(os.path.expanduser(dest_root)))):
        log.warning('prefisso progetto fuori dalla dest: prune saltato: %s', proj_dst)
        return 0

    def pruneable(rel):
        src = os.path.join(root, rel.replace('/', os.sep))
        if not (os.path.isfile(src) or os.path.islink(src)):
            return False                    # sorgente sparita o diventata dir → intatto
        return not matcher.evaluate(rel, False)

    removed = 0
    for dirpath, dirnames, filenames in os.walk(proj_dst):
        rel_dir = os.path.relpath(dirpath, proj_dst)
        base = '' if rel_dir == os.curdir else rel_dir.replace(os.sep, '/') + '/'
        # le dir-symlink della dest sono foglie come lo erano alla sorgente
        for name in list(dirnames):
            full = os.path.join(dirpath, name)
            if os.path.islink(full) and pruneable(base + name):
                removed += _drop(full, dry_run)
        for name in filenames:
            full = os.path.join(dirpath, name)
            if pruneable(base + name):
                removed += _drop(full, dry_run)
    return removed


def _drop(path, dry_run):
    if dry_run:
        log.info('prune (dry-run): rimuoverei %s', path)
        return 1
    try:
        os.unlink(path)
    except OSError as e:
        log.warning('prune: impossibile rimuovere %s: %s', path, e)
        return 0
    log.info('prune: rimosso %s', path)
    return 1
