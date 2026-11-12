"""Destinazioni non montate: stato, backoff e split (SPEC.md §8.3)."""
import errno
import os
import time

from .copier import NETWORK_ERRNOS

BACKOFF_CAP = 60          # secondi massimi fra due retry (niente busy-loop)
PROBE_TTL = 2.0           # secondi: il probe I/O non gira a ogni evento
_probe_cache = {}         # path → (monotonic, stato)


def _probe(path):
    """Apre la dir e legge UNA entry: `None` se utilizzabile, altrimenti l'OSError.

    Un `listdir` completo su un volume pieno costa: `scandir` + una sola entry
    fa scattare lo stesso EACCES/EPERM (TCC) o gli errno di rete.
    """
    try:
        with os.scandir(path) as it:
            next(it, None)
    except OSError as e:
        return e
    return None


def dest_state(path) -> str:
    """`ok` se il mountpoint esiste ED è utilizzabile, `absent` se non esiste,
    `not-accessible` se esiste ma ogni I/O è rifiutato (EACCES/EPERM: volume
    montato senza il permesso TCC «volumi rimovibili», macOS 26+).

    Il solo `os.path.exists` dice "montato" anche per un volume che rifiuta
    ogni I/O: il probe reale distingue i due casi (il mountpoint c'è ma la
    dest non è scrivibile → `not-accessible`).

    TTL di `PROBE_TTL` secondi: `dest_state` gira nel hot path del daemon (un
    check per evento) e il probe non deve diventare il costo dominante.
    """
    now = time.monotonic()
    hit = _probe_cache.get(path)
    if hit is not None and now - hit[0] < PROBE_TTL:
        return hit[1]
    state = _dest_state(path)
    _probe_cache[path] = (now, state)
    return state


def _dest_state(path) -> str:
    try:
        os.stat(path)
    except FileNotFoundError:
        return 'absent'
    except OSError as e:
        # il mountpoint stesso non è ispezionabile: EACCES/EPERM → non accessibile
        return ('not-accessible' if e.errno in (errno.EACCES, errno.EPERM)
                else 'absent')
    err = _probe(path)
    if err is not None and err.errno in (errno.EACCES, errno.EPERM):
        return 'not-accessible'
    return 'ok'


def clear_probe_cache():
    """Svuota la cache dei probe (test: lo stato di una dest può cambiare)."""
    _probe_cache.clear()


def network_error(path):
    """Probe I/O di una path montata: l'`OSError` di rete se irraggiungibile.

    Un NAS con server morto resta montato: il mountpoint esiste (`dest_state`
    == `ok`) ma ogni I/O fallisce con `NETWORK_ERRNOS` (§20.4). Un solo probe
    lo rileva — pensato per i comandi one-shot (`status`/`doctor`), mai dentro
    il loop del daemon.

    → `None` se raggiungibile o se l'errore non è di rete (ENOENT, EACCES:
    quelli li copre `dest_state` / i check TCC)."""
    err = _probe(path)
    if err is None or err.errno not in NETWORK_ERRNOS:
        return None
    return err


def backoff_schedule():
    """1, 2, 4, 8, 16, 32, 60, 60, … — iteratore infinito con cap a 60s."""
    delay = 1
    while True:
        yield min(delay, BACKOFF_CAP)
        delay *= 2


def partition_dests(dests):
    """Split delle dest in (ready, absent) sullo stato corrente del mountpoint.

    `not-accessible` (montata ma non scrivibile) va nel ramo "absent": il
    daemon la mette in pending con backoff (§8.3) invece di riprovare ogni file.
    """
    ready, absent = [], []
    for d in dests:
        (ready if dest_state(d) == 'ok' else absent).append(d)
    return ready, absent
