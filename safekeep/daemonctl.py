"""Installazione e gestione dell'agent di boot — `safekeep daemon` (SPEC §14.3).

Preflight piattaforma, rendering dei template embedded (`safekeep/data/`) e
comandi `install | start | stop | status | uninstall` su launchd (macOS) e
systemd user (Linux, WSL2 con systemd-on).

Design (piano 0.7.0): ExecStart = shim `safekeep` sul PATH (`shutil.which`)
→ `safekeep run` — il loop daemonico è il sottocomando già esistente, il
template non contiene interprete (lo shim sopravvive a `pipx reinstall`).
I template sono package data: sorgente unica, `install.sh` è un wrapper
deprecato. Solo user service, mai system/root.

Test: **mai launchctl/systemctl reali** — `_run` è l'unico punto subprocess
ed è mockkato in tests/test_daemonctl.py.
"""
import getpass
import os
import shutil
import subprocess
import sys
from importlib import resources

from safekeep.platform import (
    DAEMON_LABEL,
    install_hint,
    is_darwin,
    is_linux,
    is_wsl,
    linger_enabled,
    systemd_user_available,
)

# Path scritti da `install` e diagnosticati da `doctor` — costanti condivise,
# così i due non possono divergere (task 4 del piano).
PLIST_PATH = '~/Library/LaunchAgents/com.safekeep.agent.plist'
UNIT_PATH = '~/.config/systemd/user/safekeep.service'
SERVICE = 'safekeep'                # nome della unit systemd
CONFIG_PATH = '~/.safekeep'
STATE_DIR = '~/.local/state/safekeep'
# placeholder residuo = unit non renderizzata (doctor la considera fatale)
PLACEHOLDERS = ('__REPO__', '__PYTHON__', '__EXEC__', '__HOME__', '__PATH__')
# launchd/systemd non ereditano il PATH di login (rischio #2): fswatch va
# injectato, il resto è il PATH di sistema di fallback
SYSTEM_DIRS = ('/usr/local/bin', '/usr/bin', '/bin', '/usr/sbin', '/sbin')


def unit_path():
    """Path assoluto della unit per la piattaforma corrente (expanda di ~)."""
    return os.path.expanduser(PLIST_PATH if is_darwin() else UNIT_PATH)


def resource_text(name):
    """Testo di un file in `safekeep/data/` (package data via
    `importlib.resources`, leggibile anche da site-packages)."""
    return (resources.files('safekeep').joinpath('data').joinpath(name)
            .read_text(encoding='utf-8'))


def example_path():
    """Path su disco dell'esempio di config embedded (per l'hint `cp` di
    `doctor` — un install pip non ha il repo, solo il package)."""
    return str(resources.files('safekeep').joinpath('data')
               .joinpath('safekeep.example'))


def _run(argv):
    """Unico punto subprocess: i test lo mockkano (mai servizi reali)."""
    return subprocess.run(argv, capture_output=True, text=True, timeout=30)


def preflight():
    """→ lista errori (vuota = pronto per installare), nessuna scrittura.

    fswatch (con hint brew/apt per piattaforma), shim `safekeep` sul PATH,
    python ≥ 3.9, OS supportato e — su WSL (Windows Subsystem for Linux) —
    systemd attivo: senza systemd l'installazione è un errore chiaro con le
    istruzioni per /etc/wsl.conf, nessun fallback bash profile (decisione)."""
    errs = []
    if not (is_darwin() or is_linux()):
        # nessun lettore di piattaforma qui: resta nel solo platform.py
        errs.append('OS non supportato (servono macOS o Linux)')
    if shutil.which('fswatch') is None:
        errs.append(f'fswatch mancante — installa con: {install_hint()}')
    if sys.version_info < (3, 9):
        errs.append(f'python {sys.version.split()[0]} < 3.9 — serve ≥ 3.9')
    if shutil.which('safekeep') is None:
        errs.append('shim `safekeep` non trovato nel PATH — serve per ExecStart: '
                    'pipx install safekeep (o pip install --user safekeep con '
                    '~/.local/bin nel PATH di login)')
    if is_wsl() and not systemd_user_available():
        errs.append(
            'WSL (Windows Subsystem for Linux) senza systemd — abilita systemd '
            'in /etc/wsl.conf:\n'
            '    [boot]\n'
            '    systemd=true\n'
            '  poi riavvia WSL da Windows (wsl --shutdown) e rilancia\n'
            '  `safekeep daemon install` — oppure avvia a mano: safekeep run\n'
            '  (niente fallback su bash profile)')
    return errs


def render(exec_path=None, home=None, path_env=None):
    """Template della piattaforma con i placeholder resi — nessun residuo."""
    name = ('com.safekeep.agent.plist.tmpl' if is_darwin()
            else 'safekeep.service.tmpl')
    text = resource_text(name)
    if exec_path is None:
        exec_path = shutil.which('safekeep') or ''
    if home is None:
        home = os.path.expanduser('~')
    if path_env is None:
        dirs = []
        fswatch_dir = os.path.dirname(shutil.which('fswatch') or '')
        for d in (fswatch_dir,) + SYSTEM_DIRS:
            if d and d not in dirs:
                dirs.append(d)
        path_env = ':'.join(dirs)
    return (text.replace('__EXEC__', exec_path)
                .replace('__HOME__', home)
                .replace('__PATH__', path_env))


def _write_config():
    """~/.safekeep: creata da example embedded SOLO se assente (mai
    sovrascritta), poi chmod 600 sempre (un secret S3 > 0600 = rischio #5).

    → (path, creato_ora)."""
    cfg = os.path.expanduser(CONFIG_PATH)
    created = not os.path.exists(cfg)
    if created:
        os.makedirs(os.path.dirname(cfg), exist_ok=True)
        with open(cfg, 'w', encoding='utf-8') as fh:
            fh.write(resource_text('safekeep.example'))
    os.chmod(cfg, 0o600)
    return cfg, created


def install(enable_linger=False, dry_run=False):
    """Preflight → config → render → scrittura unit → lint → hint linger.
    Idempotente e **non avvia** l'agent (poi `safekeep daemon start`)."""
    errs = preflight()
    if errs:
        for err in errs:
            print(f'✗ {err}', file=sys.stderr)
        return 1

    path = unit_path()
    old = ''
    if os.path.exists(path):
        try:
            with open(path, encoding='utf-8') as fh:
                old = fh.read()
        except (OSError, UnicodeError):
            old = ''
    residui = [p for p in PLACEHOLDERS if p in old]
    cfg = os.path.expanduser(CONFIG_PATH)
    cfg_exists = os.path.exists(cfg)

    if dry_run:
        print(f'preflight ok — installerei: {path}'
              + (' (nuovo)' if not old else ' (sostituendo la unit esistente)'))
        if residui:
            print(f'→ la unit esistente contiene placeholder residui '
                  f'({", ".join(residui)}): verrebbe sostituita')
        print(f'config: {cfg}'
              + (' (creata da example embedded, chmod 600)' if not cfg_exists
                 else ' (invariata, chmod 600)'))
        print('nessuna modifica eseguita (--dry-run) — avvio con: '
              'safekeep daemon start')
        return 0

    try:
        cfg, cfg_created = _write_config()
    except OSError as e:
        print(f'✗ impossibile preparare {cfg}: {e}', file=sys.stderr)
        return 1

    text = render()
    residui_render = [p for p in PLACEHOLDERS if p in text]
    if residui_render:                     # template corrotto: mai scrivere
        print(f'✗ template non renderizzato ({", ".join(residui_render)})',
              file=sys.stderr)
        return 1
    if residui:
        print(f'→ unit esistente con placeholder residui '
              f'({", ".join(residui)}, vecchia install): sostituita')
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'w', encoding='utf-8') as fh:
        fh.write(text)
    kind = 'plist' if is_darwin() else 'unit'
    print(f'→ {kind} installato: {path}')

    if is_darwin():
        if shutil.which('plutil'):
            lint = _run(['plutil', '-lint', path])
            if lint.returncode != 0:
                detail = (lint.stdout + lint.stderr).strip() or path
                print(f'✗ plutil -lint fallito: {detail}', file=sys.stderr)
                return 1
        print('→ macOS: autorizza l\'interprete safekeep e fswatch in '
              'Impostazioni → Privacy e sicurezza → Accesso completo al disco '
              '(Full Disk Access, FDA — permesso TCC/Transparency, Consent & '
              'Control, non automatizzabile); dopo ogni `pipx reinstall` '
              'riautorizza il nuovo path (safekeep doctor lo verifica)')
    else:
        if systemd_user_available():
            reload = _run(['systemctl', '--user', 'daemon-reload'])
            if reload.returncode != 0:
                print('⚠ systemctl --user daemon-reload non riuscito — esegui '
                      'a mano: systemctl --user daemon-reload', file=sys.stderr)
        else:
            # non-WSL senza systemd (container/CI): la unit resta scritta ma
            # non caricabile → avvio manuale (il WSL senza systemd è già
            # caduto nel preflight, con errore chiaro)
            print('⚠ systemd non disponibile: unit scritta ma non caricabile'
                  ' — avvio manuale: safekeep run (config: '
                  f'{cfg})')
        if enable_linger:
            proc = _run(['loginctl', 'enable-linger', getpass.getuser()])
            if proc.returncode != 0:
                print('⚠ loginctl enable-linger non riuscito: '
                      + ((proc.stdout + proc.stderr).strip() or 'errore'),
                      file=sys.stderr)
            else:
                print('→ linger abilitato (agent al boot anche senza login)')
        elif linger_enabled() is False:
            print('⚠ linger NON abilitato: al boot senza login la unit non '
                  'parte → loginctl enable-linger $USER (oppure: safekeep '
                  'daemon install --enable-linger)')

    if cfg_created:
        print(f'→ creata {cfg} (da example embedded, chmod 600)')
    print('installato. Prossimi passi:')
    print(f'  1. Configura source/dest reali in {cfg}')
    print('  2. Diagnostica: safekeep doctor')
    print('  3. Avvia:       safekeep daemon start   (a boot da solo: RunAtLoad)')
    return 0


def start():
    """macOS: `launchctl bootstrap gui/UID` (idempotente); Linux:
    `systemctl --user enable --now`."""
    path = unit_path()
    if not os.path.exists(path):
        print(f'✗ unit non installata: {path} — esegui: safekeep daemon install',
              file=sys.stderr)
        return 1
    if is_darwin():
        target = f'gui/{os.getuid()}'
        if _run(['launchctl', 'print', f'{target}/{DAEMON_LABEL}']).returncode == 0:
            print(f'→ già caricato e in esecuzione: {DAEMON_LABEL}')
            return 0
        proc = _run(['launchctl', 'bootstrap', target, path])
        if proc.returncode != 0:
            print('✗ launchctl bootstrap fallito: '
                  + ((proc.stdout + proc.stderr).strip() or path),
                  file=sys.stderr)
            return 1
        print(f'→ agent avviato: {path}')
        return 0
    proc = _run(['systemctl', '--user', 'enable', '--now', SERVICE])
    if proc.returncode != 0:
        print('✗ systemctl --user enable --now fallito: '
              + ((proc.stdout + proc.stderr).strip() or SERVICE),
              file=sys.stderr)
        return 1
    print(f'→ agent avviato — stato: safekeep daemon status (log: journalctl '
          f'--user -u {SERVICE} -f)')
    return 0


def stop():
    """macOS: `launchctl bootout gui/UID/label`; Linux:
    `systemctl --user disable --now`. Exit 1 se non era caricato/in esecuzione."""
    if is_darwin():
        proc = _run(['launchctl', 'bootout', f'gui/{os.getuid()}/{DAEMON_LABEL}'])
        if proc.returncode != 0:
            print('⚠ agent non caricato o già fermo: '
                  + ((proc.stdout + proc.stderr).strip() or DAEMON_LABEL),
                  file=sys.stderr)
            return 1
        print(f'→ agent fermato: {DAEMON_LABEL}')
        return 0
    proc = _run(['systemctl', '--user', 'disable', '--now', SERVICE])
    if proc.returncode != 0:
        print('⚠ unit non attiva o già ferma: '
              + ((proc.stdout + proc.stderr).strip() or SERVICE),
              file=sys.stderr)
        return 1
    print(f'→ agent fermato: {SERVICE}')
    return 0


def status():
    """Sola lettura. macOS: `launchctl print` ridotto a running/sì-no;
    Linux: `systemctl --user status --no-pager`. Exit 0 se in esecuzione,
    1 altrimenti (mai, per questo comando, operazioni di servizio)."""
    path = unit_path()
    if not os.path.exists(path):
        print(f'non installato: {path} — esegui: safekeep daemon install')
        return 1
    if is_darwin():
        proc = _run(['launchctl', 'print', f'gui/{os.getuid()}/{DAEMON_LABEL}'])
        if proc.returncode != 0:
            print(f'non caricato: {path} — esegui: safekeep daemon start')
            return 1
        running = 'state = running' in proc.stdout
        print('in esecuzione' if running else 'caricato ma non in esecuzione')
        return 0 if running else 1
    proc = _run(['systemctl', '--user', 'status', SERVICE, '--no-pager'])
    if proc.stdout:
        print(proc.stdout, end='' if proc.stdout.endswith('\n') else '\n')
    ok = proc.returncode == 0
    print('in esecuzione' if ok else
          f'non in esecuzione (systemctl --user status: exit {proc.returncode})')
    return 0 if ok else 1


def uninstall(purge=False):
    """Rimuove la unit (bootout/disable best-effort + file). NON tocca
    `~/.safekeep` né `~/.local/state/safekeep/` salvo `--purge` (default OFF,
    allineamento con `uninstall.sh -p`). Niente operazioni su S3: solo file
    locali."""
    path = unit_path()
    if is_darwin():
        _run(['launchctl', 'bootout', f'gui/{os.getuid()}/{DAEMON_LABEL}'])  # best-effort
    else:
        _run(['systemctl', '--user', 'disable', '--now', SERVICE])          # best-effort
    removed = os.path.exists(path)
    if removed:
        os.remove(path)
        print(f'→ rimosso {path}')
    else:
        print(f'→ nessuna unit da rimuovere ({path} assente)')
    if not is_darwin() and systemd_user_available():
        _run(['systemctl', '--user', 'daemon-reload'])                       # best-effort

    cfg = os.path.expanduser(CONFIG_PATH)
    state = os.path.expanduser(STATE_DIR)
    if purge:
        if os.path.exists(cfg):
            os.remove(cfg)
        if os.path.isdir(state):
            shutil.rmtree(state, ignore_errors=True)
        print(f'→ rimossi anche {cfg} e i log in {state}')
    else:
        print(f'→ conservati {cfg} e i log in {state} (usa --purge per '
              'rimuoverli anche)')
    return 0
