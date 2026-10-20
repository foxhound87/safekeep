"""Rilevamento piattaforma (SPEC.md §14.1): l'unico posto che legge `sys.platform`.

Funzioni/constanti minime: monitor fswatch atteso, hint di installazione,
init system, limite di watch di inotify, log corrente, linger systemd,
rilevamento WSL. Tutto letto **a chiamata** (mai a import), così i test
patchano `sys.platform` ed esercitano entrambi i rami anche girando su una
sola piattaforma.

I nomi dei monitor sono quelli stampati da `fswatch -M`: `fsevents_monitor`
(FSEvents, macOS) e `inotify_monitor` (inotify, Linux) — attenzione, su Linux
il nome NON è `inotify` (SPEC.md §14.2).
"""
import getpass
import os
import platform as _platform
import re
import shutil
import subprocess
import sys
from datetime import datetime

INOTIFY_LIMIT_PATH = '/proc/sys/fs/inotify/max_user_watches'
INOTIFY_MIN_WATCHES = 16384        # sotto questa soglia un home medio lo supera
LOG_PATH = '~/.local/state/safekeep/safekeep.log'   # log corrente (SPEC.md §3)
LINGER_DIR = '/var/lib/systemd/linger'
SYSTEMD_RUN_PATH = '/run/systemd/system'            # assente su WSL1/systemd off
# EPERM (Errno 1) / EACCES (Errno 13) come li scrive il logger: codice errno
# + testo del kernel (SPEC.md §14.4)
PERM_ERR_RE = re.compile(r'\[Errno (?:1|13)\]|Operation not permitted|Permission denied')
# suffisso del throttle (SPEC.md §10.1, 0.5.1): la riga rappresenta se stessa
# + i N fallimenti compressi → count_24h la conta come N+1, non 1 (onesto)
REPEAT_RE = re.compile(r'ripetuto (\d+) volte')


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


def recent_permission_errors(path=None, now=None, window=86400):
    """`(conteggio, ultimo timestamp)` degli errori di permesso nelle ultime
    `window` secondi del log corrente, o None se il log non c'è/non è leggibile
    (SPEC.md §14.4: check non fatale, finestra 24h — segue l'incidente EPERM).

    Il timestamp è il prefisso `%(asctime)s` della riga (`YYYY-MM-DD HH:MM:SS`,
    prime 19 posizioni). Le righe col suffisso del throttle `— ripetuto N volte`
    (§10.1) valgono **N + 1**: la riga stessa + i N fallimenti compressi, così
    il conteggio resta il numero vero di errori anche con le righe throttolate.
    **Limite dichiarato**: una riga senza timestamp
    parsificabile o fuori dalla finestra (clock skew incluso) non viene contata —
    non essendo sicuri che sia recente, non la contiamo: il conteggio è un minimo."""
    if path is None:
        path = os.path.expanduser(LOG_PATH)
    if now is None:
        now = datetime.now()
    count, last = 0, None
    try:
        with open(path, encoding='utf-8', errors='replace') as fh:
            for line in fh:
                if not PERM_ERR_RE.search(line):
                    continue
                try:
                    ts = datetime.strptime(line[:19], '%Y-%m-%d %H:%M:%S')
                except ValueError:
                    continue        # timestamp illeggibile → non sicuri, salta
                age = (now - ts).total_seconds()
                if not 0 <= age <= window:
                    continue        # troppo vecchio (o nel futuro: clock skew)
                rep = REPEAT_RE.search(line)
                count += 1 + (int(rep.group(1)) if rep else 0)
                if last is None or ts > last:
                    last = ts
    except OSError:
        return None
    return count, last


DAEMON_LABEL = 'com.safekeep.agent'          # label launchd dell'agent (§8.1)


def daemon_exe(label=DAEMON_LABEL):
    """→ eseguibile assoluto del processo launchd `label`, o None se non
    determinabile (SPEC.md §8.4/§14.4, 0.5.1: job non caricato, processo non
    trovato, `launchctl`/`ps` assenti o falliti, non-Darwin). Solo lettura.

    Serve al check `doctor` (§14.4): un processo può girare su un eseguibile
    **cancellato** da un upgrade sotto un daemon vivo (brew upgrade → path
    Cellar sparito → TCC non identifica il processo → EPERM su ogni copia).
    """
    if not is_darwin():
        return None
    try:
        job = subprocess.run(
            ['launchctl', 'print', f'gui/{os.getuid()}/{label}'],
            capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    pid = re.search(r'^\s*pid = (\d+)', job.stdout, re.M)
    if job.returncode != 0 or pid is None:
        return None                      # job non caricato o senza processo
    try:
        ps = subprocess.run(['ps', '-p', pid.group(1), '-o', 'comm='],
                            capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    exe = ps.stdout.strip().splitlines()[0].strip() if ps.stdout.strip() else ''
    return exe if ps.returncode == 0 and exe else None


def linger_enabled(user=None, linger_dir=None):
    """`True`/`False` se l'utente è in `/var/lib/systemd/linger/`, None se la
    directory non c'è (niente systemd → non determinabile, SPEC.md §14.4).
    Legge la directory direttamente: stessa informazione di
    `loginctl show-user $USER -p Linger`, senza root."""
    if linger_dir is None:
        linger_dir = LINGER_DIR
    try:
        entries = os.listdir(linger_dir)
    except OSError:
        return None
    if user is None:
        try:
            user = getpass.getuser()
        except OSError:
            return None
    return user in entries


def is_wsl():
    """True dentro WSL (Windows Subsystem for Linux) — SPEC.md §17.1.

    `sys.platform` resta `linux` su WSL: il rilevamento passa dall'env
    `WSL_DISTRO_NAME` (ogni distro WSL lo settà) oppure dal kernel
    (`platform.release()` contiene `microsoft` su WSL1 e WSL2)."""
    if os.environ.get('WSL_DISTRO_NAME'):
        return True
    return 'microsoft' in _platform.release().lower()


def wsl_distro():
    """Nome della distro WSL (env `WSL_DISTRO_NAME`), vuoto se assente —
    SPEC.md §17.1."""
    return os.environ.get('WSL_DISTRO_NAME', '')


def systemd_user_available(path=None):
    """True se c'è un init systemd vivo (`/run/systemd/system`): assente su
    WSL1 o con systemd spento in WSL2 — SPEC.md §17.2."""
    if path is None:
        path = SYSTEMD_RUN_PATH
    return os.path.exists(path)
