"""Destinazioni non montate: stato, backoff e split (SPEC.md §8.3)."""
import os

from .copier import NETWORK_ERRNOS

BACKOFF_CAP = 60          # secondi massimi fra due retry (niente busy-loop)


def dest_state(path) -> str:
    """`ok` se il mountpoint esiste (volume montato), `absent` altrimenti.

    Solo `os.path.exists`: nessun processo figlio, nessun `mount` da scrapare.
    """
    return 'ok' if os.path.exists(path) else 'absent'


def network_error(path):
    """Probe I/O di una path montata: l'`OSError` di rete se irraggiungibile.

    Un NAS con server morto resta montato: il mountpoint esiste (`dest_state`
    == `ok`) ma ogni I/O fallisce con `NETWORK_ERRNOS` (§20.4). Un solo
    `listdir` lo rileva — pensato per i comandi one-shot (`status`/`doctor`),
    mai dentro il loop del daemon.

    → `None` se raggiungibile o se l'errore non è di rete (ENOENT, EACCES:
    quelli li copre `dest_state` / i check TCC)."""
    try:
        os.listdir(path)
    except OSError as e:
        return e if e.errno in NETWORK_ERRNOS else None
    return None


def backoff_schedule():
    """1, 2, 4, 8, 16, 32, 60, 60, … — iteratore infinito con cap a 60s."""
    delay = 1
    while True:
        yield min(delay, BACKOFF_CAP)
        delay *= 2


def partition_dests(dests):
    """Split delle dest in (ready, absent) sullo stato corrente del mountpoint."""
    ready, absent = [], []
    for d in dests:
        (ready if dest_state(d) == 'ok' else absent).append(d)
    return ready, absent
