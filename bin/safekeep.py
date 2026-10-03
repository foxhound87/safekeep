#!/usr/bin/env python3
"""CLI di safekeep: `run`, `sync-once`, `status` e `doctor` (SPEC.md §9)."""
import argparse
import os
import shutil
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from safekeep.config import ConfigError, validate_dests
from safekeep.copier import TMP_INFIX
from safekeep.daemon import Daemon, setup_logging
from safekeep.volumes import dest_state

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def build_parser():
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument('--config', metavar='PATH',
                        help='config globale (default: ~/.safekeep)')
    common.add_argument('-v', action='store_true', help='log verboso (debug)')

    parser = argparse.ArgumentParser(
        prog='safekeep',
        description='safekeep — backup event-driven per macOS (vedi SPEC.md)')
    sub = parser.add_subparsers(dest='comando', required=True)
    sub.add_parser('run', parents=[common],
                   help='daemon: reconcile iniziale + watch fswatch + dispatch eventi + timer 24h')
    once = sub.add_parser('sync-once', parents=[common],
                          help='un singolo passaggio: walk sorgente, copia ciò che differisce, esce')
    once.add_argument('--project', metavar='PATH',
                      help='solo questo progetto (path della root o name)')
    once.add_argument('--dry-run', action='store_true',
                      help='stampa cosa copierebbe, senza copiare')
    sub.add_parser('status', parents=[common],
                   help='config, progetti e stato delle dest — sola lettura')
    sub.add_parser('doctor', parents=[common],
                   help='diagnostica: config, fswatch, TCC, plist — exit ≠ 0 se un check fatale fallisce')
    return parser


def cmd_sync_once(args):
    daemon = Daemon(args.config)
    daemon.load_config()
    setup_logging('debug' if args.v else daemon.cfg.log_level)
    daemon.load_projects()
    rows = daemon.sync_once(dry_run=args.dry_run, only=args.project)
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
        print(f'{project.name}: {stats.get("copiati", 0)} {verb}, '
              f'{stats.get("esclusi", 0)} esclusi, dest assenti: {absent}')
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


def cmd_doctor(args):
    """Un check per riga con esito ✔/✗; exit 1 se almeno un check fatale fallisce."""
    fatal = False

    def out(ok, msg, is_fatal=False):
        nonlocal fatal
        print(('✔ ' if ok else '✗ ') + msg)
        if not ok and is_fatal:
            fatal = True

    cfg_path = os.path.expanduser(args.config or '~/.safekeep')
    cfg = None
    if not os.path.exists(cfg_path):
        example = os.path.join(REPO, 'examples', 'safekeep.example')
        out(False, f'config assente: {cfg_path} (creala: cp {example} {cfg_path})',
            is_fatal=bool(args.config))
    else:
        try:
            cfg = Daemon(cfg_path).load_config()
        except ConfigError as e:
            out(False, f'config non valida: {e}', is_fatal=True)
        else:
            # nessuna `source` → auto-discovery da $HOME: info, non warning (SPEC §6)
            mode = (f'{len(cfg.sources)} source' if cfg.sources
                    else f'auto-discovery da {os.path.expanduser("~")}')
            out(True, f'config valida: {cfg_path} ({mode}, {len(cfg.dests)} dest)')

    if cfg is not None:
        try:
            validate_dests(cfg.sources, cfg.dests)
        except ConfigError as e:
            out(False, str(e), is_fatal=True)
        else:
            out(True, 'dest non annidate nelle source (nessun loop di copia)')

    exe = shutil.which('fswatch')
    if exe is None:
        out(False, 'fswatch non trovato — installa con: brew install fswatch',
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
        try:
            monitors = subprocess.run([exe, '-M'], capture_output=True,
                                      text=True, timeout=15)
            has_fsevents = 'fsevents_monitor' in monitors.stdout
        except (OSError, subprocess.SubprocessError):
            has_fsevents = False
        out(has_fsevents,
            'fswatch monitor: fsevents_monitor presente' if has_fsevents else
            'fswatch monitor: fsevents_monitor ASSENTE in `fswatch -M`',
            is_fatal=True)

    out(sys.version_info >= (3, 9),
        f'python {sys.version.split()[0]} (≥ 3.9)', is_fatal=True)

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

    return 1 if fatal else 0


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
