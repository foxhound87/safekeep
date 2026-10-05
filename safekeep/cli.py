"""CLI di safekeep: `run`, `sync-once`, `status` e `doctor` (SPEC.md §9).

Entry point del package (`safekeep.cli:main`): `bin/safekeep.py` è solo lo
shim che usano launchd/systemd e i test.
"""
import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime

from safekeep import __version__
from safekeep.config import ConfigError, discover_projects, parse_sync
from safekeep.copier import TMP_INFIX
from safekeep.daemon import Daemon, setup_logging
from safekeep.platform import (
    INOTIFY_MIN_WATCHES,
    LOG_PATH,
    daemon_exe,
    fswatch_monitor,
    inotify_limit,
    install_hint,
    is_darwin,
    is_linux,
    is_wsl,
    linger_enabled,
    recent_permission_errors,
    systemd_user_available,
    wsl_distro,
)
from safekeep.volumes import dest_state

# Radice del repo quando si gira da checkout; in un install pip punta altrove
# (site-packages) e i riferimenti a file del repo degradano (vedi cmd_doctor).
REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
EXAMPLE = os.path.join(REPO, 'examples', 'safekeep.example')
REPO_URL = 'https://github.com/foxhound87/safekeep'


def build_parser():
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument('--config', metavar='PATH',
                        help='config globale (default: ~/.safekeep)')
    common.add_argument('-v', action='store_true', help='log verboso (debug)')

    parser = argparse.ArgumentParser(
        prog='safekeep',
        description='safekeep — backup event-driven per macOS e Linux (vedi SPEC.md)')
    sub = parser.add_subparsers(dest='comando', required=True)
    sub.add_parser('run', parents=[common],
                   help='daemon: reconcile iniziale + watch fswatch + dispatch eventi + timer 24h')
    once = sub.add_parser('sync-once', parents=[common],
                          help='un singolo passaggio: walk sorgente, copia ciò che differisce, esce')
    once.add_argument('--project', metavar='PATH',
                      help='solo questo progetto (path della root o name)')
    once.add_argument('--dry-run', action='store_true',
                      help='stampa cosa copierebbe, senza copiare')
    once.add_argument('--prune', action='store_true',
                      help='rimuove dalla dest i file il cui sorgente esiste ancora ma '
                           'non è più incluso dal matcher (default: mai cancellare)')
    sub.add_parser('status', parents=[common],
                   help='config, progetti e stato delle dest — sola lettura')
    doc = sub.add_parser('doctor', parents=[common],
                         help='diagnostica: config, fswatch + monitor di piattaforma, '
                              'python, errori di permesso recenti nel log, TCC/plist '
                              'ed exe del daemon (macOS), inotify/systemd/linger (Linux) '
                              '— exit ≠ 0 se un check fatale fallisce')
    doc.add_argument('--json', action='store_true',
                     help='stessi check, stesso exit code, ma un documento JSON su '
                          'stdout (SPEC.md §9.1)')
    return parser


def cmd_sync_once(args):
    daemon = Daemon(args.config)
    daemon.load_config()
    setup_logging('debug' if args.v else daemon.cfg.log_level)
    daemon.validate_dests()               # CR-02: dest dentro source/$HOME → exit ≠ 0
    daemon.load_projects()
    rows = daemon.sync_once(dry_run=args.dry_run, only=args.project,
                            prune=args.prune)
    if args.project and not rows:
        print(f'progetto non trovato: {args.project}', file=sys.stderr)
        return 1
    if not rows:
        print('nessun progetto trovato (nessun .sync scoperto)', file=sys.stderr)
    for project, stats, absent in rows:
        if args.dry_run:
            for src, dst in stats.get('pianificati', []):
                print(f'  {src} -> {dst}')
        verb = 'da copiare' if args.dry_run else 'copie'
        line = (f'{project.name}: {stats.get("copiati", 0)} {verb}, '
                f'{stats.get("skippati", 0)} già aggiornati, '
                f'{stats.get("esclusi", 0)} esclusi, '
                f'{stats.get("errati", 0)} errori, dest assenti: {absent}')
        if args.prune:
            verb = 'da rimuovere' if args.dry_run else 'rimossi'
            line += f', {verb}: {stats.get("rimossi", 0)}'
        print(line)
    return 0


def cmd_status(args):
    """Sola lettura: config, modalità, source/progetti con N regole, dest con dest_state."""
    daemon = Daemon(args.config)
    daemon.load_config()
    print(f'config    {daemon.config_path}')
    print(f'layout    {daemon.cfg.layout}')
    if daemon.cfg.sources:
        print('modalità  source')
        for src in daemon.cfg.sources:
            print(f'source    {src}')
    else:
        print(f'modalità  auto-discovery da {os.path.expanduser("~")}')
    daemon.load_projects()
    for project in sorted(daemon.projects, key=lambda p: p.root):
        print(f'progetto  {project.name} — {len(project.matcher.rules)} regole '
              f'— {project.root}')
    if not daemon.projects:
        print('progetto  nessuno (.sync non trovato)')
    for dest in daemon.cfg.dests:
        print(f'dest      {dest} [{dest_state(dest)}]')
    return 0


def check_sync_files(daemon):
    """SPEC.md §9.2 (CR-03): ogni `.sync` scoperto deve parsificare e avere
    ≥1 regola valida. Non fatale: il conteggio delle righe scartate diventa
    un warning di `doctor`.

    → (progetti totali, `.sync` rotti con motivo, righe scartate totali)."""
    roots, auto = daemon.discovery_roots()
    total, broken, discarded = 0, [], 0
    for root in discover_projects(roots, auto=auto):
        total += 1
        path = os.path.join(root, '.sync')
        try:
            with open(path, encoding='utf-8') as fh:
                text = fh.read()
        except (OSError, UnicodeError) as e:   # OSError + UnicodeDecodeError
            broken.append(f'{path}: {e}')
            continue
        # log_level fisso a info: il parse di doctor NON deve mai essere fatale
        sync = parse_sync(text, log_level='info', origin=path)
        discarded += sync.discarded
        if not (sync.rules or sync.defaults):
            broken.append(f'{path}: nessuna regola valida')
    return total, broken, discarded


def cmd_doctor(args):
    """Un check per riga con esito ✔/✗; exit 1 se almeno un check fatale fallisce.

    Con `--json` le righe non vengono stampate: la stessa sequenza di check finisce
    in un documento JSON su stdout, con lo stesso exit code (SPEC.md §9.1)."""
    fatal = False
    checks, ids = [], {}

    def out(ok, msg, is_fatal=False):
        nonlocal fatal
        if args.json:
            # id = slug del messaggio (prefisso fino a ':' o '('), id uguali
            # ripetuti suffissati _2/_3 così restano chiave usabile (SPEC §9.1)
            base = re.split(r'[:\(]', msg, maxsplit=1)[0]
            ident = re.sub(r'[^a-z0-9]+', '_', base.lower()).strip('_') or 'check'
            ids[ident] = ids.get(ident, 0) + 1
            if ids[ident] > 1:
                ident = f'{ident}_{ids[ident]}'
            checks.append({'id': ident,
                           'status': 'ok' if ok else ('fail' if is_fatal else 'warn'),
                           'message': msg})
        else:
            print(('✔ ' if ok else '✗ ') + msg)
        if not ok and is_fatal:
            fatal = True

    cfg_path = os.path.expanduser(args.config or '~/.safekeep')
    cfg = None
    daemon = None
    if not os.path.exists(cfg_path):
        if os.path.exists(EXAMPLE):
            hint = f'creala: cp {EXAMPLE} {cfg_path}'
        else:
            # install pip: niente repo, niente esempi → hint degradato
            hint = (f'clone {REPO_URL} and copy examples/safekeep.example to '
                    f'{cfg_path}')
        out(False, f'config assente: {cfg_path} ({hint})',
            is_fatal=bool(args.config))
    else:
        try:
            daemon = Daemon(cfg_path)
            cfg = daemon.load_config()
        except ConfigError as e:
            daemon = None
            out(False, f'config non valida: {e}', is_fatal=True)
        else:
            # nessuna `source` → auto-discovery da $HOME: info, non warning (SPEC §6)
            mode = (f'{len(cfg.sources)} source' if cfg.sources
                    else f'auto-discovery da {os.path.expanduser("~")}')
            out(True, f'config valida: {cfg_path} ({mode}, {len(cfg.dests)} dest)')

    if daemon is not None:
        # base = source, oppure $HOME in auto-mode: copre anche "dest dentro $HOME"
        try:
            daemon.validate_dests()
        except ConfigError as e:
            out(False, str(e), is_fatal=True)
        else:
            out(True, 'dest non annidate nelle source/$HOME (nessun loop di copia)')

    if daemon is not None:
        total, broken, discarded = check_sync_files(daemon)
        if broken:
            detail = f' ({discarded} righe scartate)' if discarded else ''
            out(False, '.sync non validi: ' + '; '.join(broken) + detail
                + ' — warning non fatale')
        elif discarded:
            out(False, f'.sync: {discarded} righe scartate in {total} progetto/i '
                       '— warning non fatale')
        elif total:
            out(True, f'.sync: {total} progetto/i parsificati con regole valide')
        else:
            out(True, '.sync: nessun progetto scoperto (niente da validare)')

    exe = shutil.which('fswatch')
    if exe is None:
        out(False, f'fswatch non trovato — installa con: {install_hint()}',
            is_fatal=True)
    else:
        version = None
        try:
            proc = subprocess.run([exe, '--version'], capture_output=True,
                                  text=True, timeout=15)
            lines = (proc.stdout + proc.stderr).strip().splitlines()
            version = lines[0] if lines else None
        except (OSError, subprocess.SubprocessError):
            pass
        out(version is not None,
            f'fswatch: {version}' if version else 'fswatch --version senza output',
            is_fatal=True)
        # monitor atteso per piattaforma (SPEC.md §14.2/§14.4)
        expected = fswatch_monitor()
        try:
            monitors = subprocess.run([exe, '-M'], capture_output=True,
                                      text=True, timeout=15)
            present = expected in monitors.stdout
        except (OSError, subprocess.SubprocessError):
            present = False
        out(present,
            f'fswatch monitor: {expected} presente' if present else
            f'fswatch monitor: {expected} ASSENTE in `fswatch -M`',
            is_fatal=True)

    out(sys.version_info >= (3, 9),
        f'python {sys.version.split()[0]} (≥ 3.9)', is_fatal=True)

    # Errori di permesso recenti nel log corrente (SPEC.md §14.4): sintomo
    # delle ultime 24h, non stato del sistema → sempre NON fatale
    log = os.path.expanduser(LOG_PATH)
    recent = recent_permission_errors()
    if recent is None:
        out(True, f'errori di permesso recenti: log assente — check saltato ({log})')
    elif recent[0] > 0:
        out(False, f'errori di permesso recenti: {recent[0]} nelle ultime 24h '
                   f'(ultimo: {recent[1]}) — log: {log} — warning non fatale: '
                   'vedi i pending con `safekeep sync-once --dry-run`, poi '
                   'riconcedere i permessi al volume (FDA/Full Disk Access)')
    else:
        out(True, f'errori di permesso recenti: nessuno nelle ultime 24h — log: {log}')

    # WSL (SPEC.md §17.2): tre righe informative — nessuna è mai fatale
    if is_wsl():
        out(True, f'WSL rilevato: {wsl_distro() or "sconosciuta"} '
                  '(supporto best-effort, SPEC §17)')
        if not systemd_user_available():
            out(False, 'systemd user non disponibile (WSL1 o systemd spento) — '
                       'abilita systemd in /etc/wsl.conf: [boot] systemd=true '
                       '(poi wsl --shutdown da Windows) oppure esegui '
                       '`safekeep run` a mano — warning non fatale')
        if daemon is not None:
            sotto_mnt = [p for p in list(daemon.cfg.sources)
                         + list(daemon.cfg.dests) if p.startswith('/mnt/')]
            if sotto_mnt:
                out(True, f'drvfs: {len(sotto_mnt)} path sotto /mnt/ — I/O lento '
                          'e case-insensitive di default: valuta una dest su ext4 '
                          'nativa della distro (SPEC §17.2)')

    # Linux: il limite di watch di inotify è il rischio #1 sugli alberi grandi
    # (SPEC.md §14.4) — informativo, NON fatale
    if is_linux():
        limit = inotify_limit()
        if limit is None:
            out(True, 'inotify: limite di watch non leggibile — check saltato')
        elif limit < INOTIFY_MIN_WATCHES:
            out(False, f'inotify: fs.inotify.max_user_watches={limit} '
                       f'(< {INOTIFY_MIN_WATCHES}) — su alberi grandi (~25k file) i '
                       'watch finiscono: sudo sysctl -w fs.inotify.max_user_watches=524288 '
                       '— warning non fatale')
        else:
            out(True, f'inotify: fs.inotify.max_user_watches={limit} '
                      f'(≥ {INOTIFY_MIN_WATCHES})')

    # TCC (Transparency, Consent and Control) è un meccanismo di macOS (SPEC.md §11)
    if is_darwin():
        docs = os.path.expanduser('~/Documents')
        if not os.path.isdir(docs):
            out(True, 'TCC (Transparency, Consent and Control): ~/Documents assente — probe saltato')
        else:
            try:
                os.listdir(docs)
            except PermissionError:
                out(False, 'TCC: PermissionError su ~/Documents — serve FDA '
                           '(Full Disk Access) per i progetti in cartelle protette')
            else:
                out(True, 'TCC: ~/Documents leggibile')

    if cfg is not None:
        residui = [os.path.join(dirpath, name)
                   for dest in cfg.dests
                   for dirpath, _, names in os.walk(dest)
                   for name in names
                   if TMP_INFIX in name]
        out(not residui,
            'residui *.safekeep.tmp.*: nessuno' if not residui else
            'residui *.safekeep.tmp.*: ' + ', '.join(residui))

    if is_darwin():
        plist = os.path.expanduser('~/Library/LaunchAgents/com.safekeep.agent.plist')
        if not os.path.exists(plist):
            out(True, 'plist non installato (nessun agent da validare)')
        elif shutil.which('plutil') is None:
            out(True, 'plutil assente — lint del plist saltato')
        else:
            try:
                lint = subprocess.run(['plutil', '-lint', plist],
                                      capture_output=True, text=True, timeout=15)
                detail = (lint.stdout + lint.stderr).strip() or plist
                out(lint.returncode == 0, f'plist lint: {detail}', is_fatal=True)
            except (OSError, subprocess.SubprocessError) as e:
                out(False, f'plist lint non eseguibile: {e}', is_fatal=True)
    else:
        # Linux: user unit systemd al posto del plist (SPEC.md §14.3)
        unit = os.path.expanduser('~/.config/systemd/user/safekeep.service')
        if not os.path.exists(unit):
            out(True, 'unit systemd non installata (nessun agent da validare)')
        else:
            try:
                with open(unit, encoding='utf-8') as fh:
                    text = fh.read()
            except (OSError, UnicodeError) as e:
                out(False, f'unit systemd illeggibile: {unit}: {e}', is_fatal=True)
            else:
                if '__REPO__' in text or '__PYTHON__' in text:
                    out(False, f'unit systemd: {unit} contiene ancora i placeholder '
                               'del template — rilancia install.sh', is_fatal=True)
                else:
                    out(True, f'unit systemd: {unit}')
            # linger: solo dove la unit è installata (SPEC.md §14.4) — non fatale
            enabled = linger_enabled()
            if enabled is None:
                out(True, 'linger: non verificabile (/var/lib/systemd/linger '
                          'assente) — check saltato')
            elif enabled:
                out(True, 'linger: abilitato (agent al boot anche senza login)')
            else:
                out(False, "linger: NON abilitato — l'agent non partirà al boot "
                           'senza login → loginctl enable-linger $USER — '
                           'warning non fatale')

    # exe del daemon ancora su disco (SPEC.md §8.4/§14.4, 0.5.1): un processo
    # può girare su un eseguibile cancellato da un upgrade sotto un daemon vivo
    # → TCC (Transparency, Consent and Control) non lo identifica più e ogni
    # copia fallisce con EPERM. Guarda il PROCESSO, non il plist (funziona
    # anche col plist renderizzato col vecchio default). Non fatale: sintomo
    # con rimedio manuale di una riga, e doctor deve girare anche in quello stato.
    if is_darwin():
        exe = daemon_exe()
        if exe is None:
            out(True, 'exe del daemon: processo non determinabile — check saltato')
        elif os.path.exists(exe):
            out(True, f'exe del daemon: {exe} presente su disco')
        else:
            out(False, f'exe del daemon: {exe} NON esiste su disco — il processo '
                       'gira su un eseguibile cancellato (upgrade sotto un daemon '
                       'vivo, SPEC §8.4) → launchctl kickstart -k '
                       'gui/$(id -u)/com.safekeep.agent — warning non fatale')

    code = 1 if fatal else 0
    if args.json:
        count, last = recent if recent is not None else (0, None)
        print(json.dumps({
            'safekeep': __version__,
            'timestamp': datetime.now().isoformat(timespec='seconds'),
            'exit': code,
            'checks': checks,
            'permission_errors': {
                'count_24h': count,
                'last': last.strftime('%Y-%m-%d %H:%M:%S') if last else None,
            },
        }, ensure_ascii=False, indent=2))
    return code


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        if args.comando == 'run':
            return Daemon(args.config).run(verbose=args.v)
        if args.comando == 'status':
            return cmd_status(args)
        if args.comando == 'doctor':
            return cmd_doctor(args)
        return cmd_sync_once(args)
    except ConfigError as e:
        print(f'errore di config: {e}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
