"""Rilevamento piattaforma (SPEC.md §14.1): l'unico posto che legge `sys.platform`.

Funzioni/constanti minime: monitor fswatch atteso, hint di installazione,
init system, limite di watch di inotify. Tutto letto **a chiamata** (mai a
import), così i test patchano `sys.platform` ed esercitano entrambi i rami
anche girando su una sola piattaforma.

I nomi dei monitor sono quelli stampati da `fswatch -M`: `fsevents_monitor`
(FSEvents, macOS) e `inotify_monitor` (inotify, Linux) — attenzione, su Linux
il nome NON è `inotify` (SPEC.md §14.2).
"""
import shutil
import sys

INOTIFY_LIMIT_PATH = '/proc/sys/fs/inotify/max_user_watches'
INOTIFY_MIN_WATCHES = 16384        # sotto questa soglia un home medio lo supera


def is_darwin():
    return sys.platform == 'darwin'


def is_linux():
    return sys.platform == 'linux'


def fswatch_monitor():
    """Monitor fswatch per la piattaforma (SPEC.md §14.2), usato da
    `daemon.fswatch_argv()` e dal check monitor di `doctor`."""
    return 'fsevents_monitor' if is_darwin() else 'inotify_monitor'


def init_system():
    """`launchd` (Darwin) o `systemd` (Linux) — SPEC.md §14.3."""
    return 'launchd' if is_darwin() else 'systemd'


def install_hint():
    """Come installare `fswatch` su questa macchina (SPEC.md §14.4)."""
    if is_darwin():
        return 'brew install fswatch'
    for pm, cmd in (('pacman', 'pacman -S fswatch'),
                    ('apt-get', 'sudo apt-get install fswatch'),
                    ('dnf', 'sudo dnf install fswatch')):
        if shutil.which(pm):
            return cmd
    return 'installa fswatch col package manager della tua distro'


def inotify_limit(path=INOTIFY_LIMIT_PATH):
    """`fs.inotify.max_user_watches`, o None se il file non c'è o è illeggibile
    (SPEC.md §14.4: su Linux è il rischio #1 sugli alberi grandi)."""
    try:
        with open(path) as fh:
            return int(fh.read().strip())
    except (OSError, ValueError):
        return None
