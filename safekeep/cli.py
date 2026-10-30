"""CLI di safekeep: `run`, `sync-once`, `status`, `doctor` e `daemon` (SPEC.md §9).

Entry point del package (`safekeep.cli:main`): `bin/safekeep.py` è solo lo
shim che usano launchd/systemd e i test.
"""
import argparse
import json
import logging
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime

try:
    import readline          # editing delle frecce nei menu interattivi (opzionale)
except ImportError:
    pass

from safekeep import __version__
from safekeep import daemonctl
from safekeep import restore
from safekeep.config import (
    ConfigError, discover_projects, matching_route, parse_sync)
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
    is_network_fs,
    is_wsl,
    linger_enabled,
    recent_permission_errors,
    systemd_user_available,
    wsl_distro,
)
from safekeep.s3dest import S3ExtraMissing, secret_perms_insecure
from safekeep.volumes import dest_state, network_error

# Repo upstream: solo nell'hint degradato di `cmd_doctor` quando l'esempio
# embedded non fosse leggibile (install pip rotto — package data mancante).
REPO_URL = 'https://github.com/foxhound87/safekeep'

log = logging.getLogger('safekeep')


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
                           'non è più incluso dal matcher (default: mai cancellare; '
                           'solo dest fs — la dest S3 non viene toccata, SPEC §18.2)')
    res = sub.add_parser('restore', parents=[common],
                         help="ripristina dall'ultima copia in una dest: dry-run di "
                              'default, --force per scrivere; PATH/GLOB o menu '
                              'interattivo (SPEC §19)')
    res.add_argument('--dest', metavar='NOME',
                     help='origine = destinazione nominata `dest <nome>:` oppure '
                          'target S3 di una route (s3:<prefisso> / '
                          's3://bucket/prefisso): ha PRECEDENZA su --from '
                          '(SPEC §19.3/§21.4); nome inesistente → errore con '
                          "l'elenco dei nomi disponibili")
    res.add_argument('--from', dest='from_', choices=('local', 's3', 'nas'),
                     help='origine del ripristino: local = prima dest:, nas = seconda '
                          'dest:, s3 = dest S3 (default: auto-detect del candidato '
                          'unico, SPEC §19.3 — con --dest presente non vale)')
    res.add_argument('--force', action='store_true',
                     help='esegue le copie (default: dry-run — nessuna scrittura, '
                          'nemmeno in interattivo)')
    res.add_argument('--if-dir-exists', action='store_true',
                     help='ripristina un file solo se la sua directory di '
                          'destinazione immediata esiste già: altrimenti salta, '
                          'senza creare cartelle mancanti (default: le parent '
                          'vengono create, SPEC §19.6)')
    res.add_argument('targets', nargs='*', metavar='PATH|GLOB',
                     help='path assoluto, relativo alla radice source o glob fnmatch '
                          'sui path originali (niente argomenti → menu interattivo, '
                          'solo da terminale)')
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
    daem = sub.add_parser(
        'daemon',
        description="installa e gestisce l'agent di boot (launchd su macOS, "
                    'systemd user su Linux/WSL2 con systemd-on) — ExecStart = '
                    'shim `safekeep` sul PATH → `safekeep run`',
        help="installa e gestisce l'agent di boot (launchd su macOS, systemd "
             "user su Linux/WSL2) — ExecStart: safekeep run")
    dsub = daem.add_subparsers(dest='daemon_cmd', required=True)
    dinst = dsub.add_parser(
        'install',
        help='preflight (fswatch/shim/python/WSL-systemd) + render della unit da '
             'template embedded + scrittura — idempotente, NON avvia')
    dinst.add_argument('--enable-linger', action='store_true',
                       help='Linux: esegue `loginctl enable-linger $USER` '
                            '(default: solo hint — linger = unit user attive '
                            'anche senza login)')
    dinst.add_argument('--dry-run', action='store_true',
                       help='mostra cosa installerebbe, senza scrivere nulla')
    dsub.add_parser('start',
                    help="carica e avvia l'agent (launchctl bootstrap / "
                         'systemctl --user enable --now)')
    dsub.add_parser('stop',
                    help="ferma l'agent (launchctl bootout / "
                         'systemctl --user disable --now)')
    dsub.add_parser('status',
                    help="stato dell'agent — sola lettura, exit 0 se in "
                         'esecuzione, 1 altrimenti')
    dun = dsub.add_parser('uninstall',
                          help='rimuove la unit — ~/.safekeep e i log restano '
                               'intatti')
    dun.add_argument('--purge', action='store_true',
                     help='rimuove anche ~/.safekeep e ~/.local/state/safekeep/')
    return parser


def cmd_sync_once(args):
    daemon = Daemon(args.config)
    daemon.load_config()
    setup_logging('debug' if args.v else daemon.cfg.log_level)
    daemon.log_s3_secret_perms()          # §18.4: warning una-tantum, dopo gli handler
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


def _menu(title, items):
    """Menu numerati per livello (SPEC §19.4): numeri anche multipli (es. `1 3`),
    `a` = tutto, `q` = annulla (→ None). Su EOF (pipe chiusa) → None."""
    print(title)
    for i, label in enumerate(items, 1):
        print(f'  {i}. {label}')
    while True:
        try:
            line = input('> ').strip()
        except EOFError:
            return None
        if line == 'q':
            return None
        if line == 'a':
            return list(range(len(items)))
        try:
            picked = [int(x) - 1 for x in line.split()]
        except ValueError:
            picked = []
        if picked and all(0 <= i < len(items) for i in picked):
            return picked
        print('inserisci un numero (es. 1 3), a (tutto) o q (annulla)')


def _interactive_select(entries):
    """Menu per livelli (SPEC §19.4): progetto → voci di primo livello del
    backup. Ritorna le entry selezionate, oppure None (q/annulla)."""
    by_project = {}
    for e in entries:
        by_project.setdefault(e.project, []).append(e)
    projects = sorted(by_project, key=lambda p: p.root)
    picked = _menu('progetto:', [f'{p.name} — {p.root}' for p in projects])
    if picked is None:
        return None
    pool = [e for i in picked for e in by_project[projects[i]]]
    groups = restore.first_level(pool)
    picked = _menu('voci di primo livello del backup:',
                   [name for name, _ in groups])
    if picked is None:
        return None
    return [e for i in picked for e in groups[i][1]]


def cmd_restore(args):
    """Ripristino dall'ultima copia (SPEC §19): dry-run di default, `--force`
    per scrivere; PATH/GLOB non interattivo oppure menu per livelli da TTY.
    `--dest <nome>` (§21.4) identifica l'origine in modo univoco e ha
    precedenza su `--from`/auto-detect (§19.3)."""
    daemon = Daemon(args.config)
    daemon.load_config()
    setup_logging('debug' if args.v else daemon.cfg.log_level)
    daemon.log_s3_secret_perms()
    interactive = not args.targets and sys.stdin.isatty()
    if not args.targets and not interactive:
        print('restore: serve un PATH/GLOB oppure un terminale interattivo '
              '(SPEC §19.2)', file=sys.stderr)
        return 1
    try:
        if args.dest is not None:
            if args.from_ is not None:
                log.warning('--dest e --from insieme: --dest ha precedenza '
                            '(SPEC §19.3)')
            kind, origin = restore.pick_dest(daemon.cfg, args.dest)
            if kind == 's3':
                # bucket/prefisso della route → S3Destination per-route (§21.5)
                origin = daemon.s3_dest(*origin)
        else:
            from_ = args.from_
            if from_ is None and interactive:
                cands = restore.candidates(daemon.cfg, daemon.s3)
                if len(cands) > 1:
                    # livello "origine" (§19.4): solo se ambigua (≥2 candidati)
                    labels = [f'{label} — {o.root if kind == "s3" else o}'
                              for kind, o, label in cands]
                    picked = _menu('origine del ripristino:', labels)
                    if picked is None:
                        print('restore: annullato', file=sys.stderr)
                        return 1
                    from_ = cands[picked[0]][2]
            kind, origin = restore.pick_source(daemon.cfg, daemon.s3, from_)
    except restore.RestoreError as e:
        print(f'restore: {e}', file=sys.stderr)
        return 1
    origin_label = origin.root if kind == 's3' else origin
    daemon.load_projects()
    entries, orphans = restore.inventory(daemon.projects, kind, origin,
                                         daemon.cfg.layout)
    for o in orphans:
        log.warning('orfano (nessun progetto corrispondente): %s — saltato', o)
    restore.warn_network_sources(daemon.projects)
    if not entries:
        print(f'restore: nessuna voce di backup in {origin_label}', file=sys.stderr)
        return 1
    if interactive:
        matched = _interactive_select(entries)
        if matched is None:
            print('restore: annullato', file=sys.stderr)
            return 1
    else:
        matched = restore.select(entries, args.targets)
        if not matched:
            print('restore: nessun file corrispondente: '
                  + ', '.join(args.targets), file=sys.stderr)
            return 1
    rows, syn = restore.plan(matched, if_dir_exists=args.if_dir_exists)
    for backup, target, nota in rows:
        print(f'  {backup} -> {target} {nota}')
    line = (f"ripristino: {syn['da_copiare']} "
            f"{'da copiare' if not args.force else 'copie'}")
    if syn['saltati']:
        line += (f", {syn['saltati']} saltati "
                 '(directory destinazione assente)')
    line += f", {syn['aggiornati']} già aggiornati"
    if not args.force:
        line += ' — dry-run: nessuna scrittura (usa --force per ripristinare)'
    print(line)
    if not args.force:
        return 0
    ok, err, skipped = restore.execute(matched,
                                       origin if kind == 's3' else None,
                                       if_dir_exists=args.if_dir_exists)
    line = f'ripristino: {ok} ripristinati, {err} errori'
    if skipped:
        line += f', {skipped} saltati (directory destinazione assente)'
    print(line)
    return 0 if err == 0 else 1


def dest_name_map(cfg):
    """path → nome della dest nominata (SPEC §21.2): il primo nome dichiarato
    per un path vince — solo per l'etichettatura in `status`/`doctor`."""
    out = {}
    for name, path in cfg.dest_names.items():
        out.setdefault(path, name)
    return out


def route_label(cfg, project):
    """Destinazioni del progetto come etichetta compatta (SPEC §21.4): nomi
    delle dest fs + `s3://bucket/prefisso` per i target S3; `tutte` se
    nessuna route matcha il progetto (broadcast retrocompat §21.1)."""
    route = matching_route(project.source_root, cfg, project_root=project.root)
    if route is None:
        return 'tutte'
    names = dest_name_map(cfg)
    parts = [names.get(path, path) for path in route.dests]
    parts += [f's3://{bucket}/{prefix}' if prefix else f's3://{bucket}'
              for bucket, prefix in route.s3]
    return ', '.join(parts)


def cmd_status(args):
    """Sola lettura: config, modalità, source/progetti con N regole, routing
    dei progetti (solo con route, §21.4), dest con dest_state."""
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
    # routing (§21.4): SOLO se il config ha route — senza, l'output resta
    # identico a 0.10.0 (broadcast implicito, §21.1). Il progetto senza route
    # che matcha va su tutte le dest: `tutte`.
    if daemon.cfg.routes:
        for project in sorted(daemon.projects, key=lambda p: p.root):
            print(f'routing   {project.name} → '
                  f'[{route_label(daemon.cfg, project)}]')
    names = dest_name_map(daemon.cfg)     # etichetta del nome accanto al path (§21.4)
    for dest in daemon.cfg.dests:
        # etichetta `nas`/`local` dalla fstype detect (§20.6): solo lettura,
        # nessun processo figlio (fs_fstype legge /proc/mounts o `mount`)
        label = 'nas' if is_network_fs(dest) else 'local'
        state = dest_state(dest)
        # mount c'è ma il server è morto: il mountpoint risolve, ogni I/O dà
        # ENOTCONN → `unreachable` invece dell'ingannevole `[ok]` (§20.6)
        if state == 'ok' and network_error(dest) is not None:
            state = 'unreachable'
        print(f'dest {names.get(dest, ""):<5}{dest} [{state}] [{label}]')
    if daemon.s3 is not None:
        # HeadBucket con cache 30s (§18.3): ok/offline, senza toccare la
        # config — `status` resta sola lettura
        print(f's3        {daemon.s3.root} [{daemon.s3.state()}] '
              f'— {daemon.s3.endpoint_label} (region {daemon.s3.cfg.region})')
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
    s3_missing = None
    if not os.path.exists(cfg_path):
        # Hint dall'example EMBEDDED (package data, task 4): un install pip non
        # ha il repo → niente più "clone the repo"
        embedded = daemonctl.example_path()
        if os.path.exists(embedded):
            hint = f'creala: cp {embedded} {cfg_path}'
        else:
            # install rotto (niente package data): fallback minimo
            hint = (f'clone {REPO_URL} and copy examples/safekeep.example to '
                    f'{cfg_path}')
        out(False, f'config assente: {cfg_path} ({hint})',
            is_fatal=bool(args.config))
    else:
        try:
            daemon = Daemon(cfg_path)
            cfg = daemon.load_config()
        except S3ExtraMissing as e:
            # extra `safekeep[s3]` mancante: riga NON fatale con hint (§18).
            # `load_config` ha già validato il resto → daemon/cfg restano
            # utilizzabili per tutti gli altri check, solo senza dest S3.
            cfg = daemon.cfg
            s3_missing = e
        except ConfigError as e:
            daemon = None
            out(False, f'config non valida: {e}', is_fatal=True)

    if cfg is not None:
        # nessuna `source` → auto-discovery da $HOME: info, non warning (SPEC §6)
        mode = (f'{len(cfg.sources)} source' if cfg.sources
                else f'auto-discovery da {os.path.expanduser("~")}')
        out(True, f'config valida: {cfg_path} ({mode}, {len(cfg.dests)} dest)')
        if s3_missing is not None:
            out(False, f'{s3_missing} — warning non fatale', is_fatal=False)

    if daemon is not None:
        # base = source, oppure $HOME in auto-mode: copre anche "dest dentro $HOME"
        try:
            daemon.validate_dests()
        except ConfigError as e:
            out(False, str(e), is_fatal=True)
        else:
            out(True, 'dest non annidate nelle source/$HOME (nessun loop di copia)')

    # --- dest S3 (SPEC.md §18): tutti check NON fatali ---------------------
    if cfg is not None and cfg.s3 is not None:
        s3 = cfg.s3
        orig = (f'variabile d\'ambiente {s3.secret_env!r}' if s3.secret_env
                else 'file di config')
        out(True, f's3 credenziali risolte da {orig} (bucket {s3.bucket})')
        if s3.secret_env:
            out(True, f's3 secret non nel file (da {s3.secret_env}): '
                      'chmod 600 non pertinente')
        elif secret_perms_insecure(cfg_path, s3):
            out(False, f's3_secret_key in {cfg_path} con permessi > 0600 — '
                       f'remedio: chmod 600 {cfg_path} — warning non fatale')
        else:
            out(True, f's3_secret_key: permessi del config ≤ 0600')
        if s3.endpoint and s3.endpoint.startswith('http://'):
            out(False, f's3 endpoint cleartext {s3.endpoint}: il secret e i '
                       'dati viaggiano in chiaro — usa https:// — '
                       'warning non fatale')
        else:
            out(True, f's3 endpoint: {s3.endpoint or "AWS default"} '
                      f'(region {s3.region}, addressing {s3.addressing})')
    if daemon is not None and daemon.s3 is not None:
        # extra installato: HeadBucket con cache 30s (§18.3)
        state = daemon.s3.state()
        if state == 'ok':
            out(True, f's3 HeadBucket {daemon.s3.root}: ok')
        else:
            out(False, f's3 HeadBucket {daemon.s3.root}: offline — endpoint '
                       f'{daemon.s3.endpoint_label} non raggiungibile — '
                       'warning non fatale')

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

    # Routing destinazioni (SPEC §21): solo con route nel config — senza, la
    # riga non c'è e l'output di doctor resta identico a 0.10.0. `load_projects`
    # scopre le radici note per il match sui DUE livelli (§21.3); doctor non
    # configura mai il logging (§9.1), quindi il warning di log del daemon non
    # esce da qui: la riga di check è l'unico output, mai duplicata.
    if cfg is not None and daemon is not None and cfg.routes:
        daemon.load_projects()
        unused = daemon.unused_routes()
        if unused:
            detail = '; '.join(f'{r.src} (riga {r.lineno})' for r in unused)
            out(False, f'route inutilizzate: {detail} — nessuna root nota '
                       '(source o project) la usa: al prossimo rescan potrebbe '
                       'agganciarsi (SPEC §12) — warning non fatale')
        else:
            out(True, f'route: {len(cfg.routes)} dichiarate, tutte agganciate '
                      'a una root nota (source o project)')
        # dest nominate citate in route ma NON montate: volume assente (§8.3),
        # condizione disgiunta dal check volumi qui sotto (che copre i mount
        # presenti ma irraggiungibili, dest_state == ok, §20.6) — nessuna
        # collisione fra i due
        names = dest_name_map(cfg)
        cited = {p for r in cfg.routes for p in r.dests}
        absent = [d for d in cfg.dests if d in cited and dest_state(d) != 'ok']
        if absent:
            out(False, 'dest di route non montate: '
                + ', '.join(f'{names.get(d, d)} ({d})' for d in absent)
                + ' — monta il volume e riprova — warning non fatale')
        if daemon.projects:
            out(True, 'routing: ' + ', '.join(
                f'{p.name} → [{route_label(cfg, p)}]'
                for p in sorted(daemon.projects, key=lambda p: p.root)))
        else:
            out(True, 'routing: nessun progetto scoperto')

    # Volumi di rete (SPEC §20.6): split radici locali (fswatch) vs radici su
    # volume di rete (monitor a polling) — tutti NON fatali
    if daemon is not None:
        roots, _auto = daemon.discovery_roots()
        net = [r for r in roots if is_network_fs(r)]
        local = [r for r in roots if r not in net]
        out(True, f'fswatch: root locali {len(local)}')
        if net:
            out(True, f'sorgenti su volume di rete: {len(net)} '
                      f'(poll ogni {daemon.cfg.poll_interval}s): ' + ', '.join(net))
        else:
            out(True, 'sorgenti su volume di rete: 0 (niente da pollare)')
        # montate ma irraggiungibili (server morto): fstype dice "di rete" e
        # il mountpoint esiste, ma ogni I/O dà ENOTCONN (§20.4/§20.6)
        down = [r for r in net if network_error(r) is not None]
        down += [d for d in cfg.dests
                 if dest_state(d) == 'ok' and is_network_fs(d)
                 and network_error(d) is not None]
        if down:
            out(False, f'volumi di rete non raggiungibili: {len(down)} '
                       f'({", ".join(down)}) — montaggio/serve attivo? — '
                       'warning non fatale')
        else:
            out(True, 'volumi di rete: tutti raggiungibili')

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

    # macOS + path sotto /Volumes (§20.6): EACCES recenti sui volumi
    # rimovibili sono quasi sempre il permesso TCC "Interagisci con i volumi
    # rimovibili" mancante — hint NON fatale, con rimedio di una riga
    if is_darwin() and recent is not None and recent[0] > 0 and cfg is not None:
        rimovibili = [p for p in list(cfg.sources) + list(cfg.dests)
                      if p.startswith('/Volumes/')]
        if rimovibili:
            out(False, f'errori di permesso recenti con {len(rimovibili)} path '
                       'sotto /Volumes — su macOS apri System Settings → '
                       'Privacy & Security → «Interagisci con i volumi '
                       'rimovibili» e aggiungi safekeep (launchd) — '
                       'warning non fatale')

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
        plist = os.path.expanduser(daemonctl.PLIST_PATH)
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
        # Linux: user unit systemd al posto del plist (SPEC.md §14.3) — stesso
        # path scritto da daemonctl (costanti condivise, task 4)
        unit = os.path.expanduser(daemonctl.UNIT_PATH)
        if not os.path.exists(unit):
            out(True, 'unit systemd non installata (nessun agent da validare)')
        else:
            try:
                with open(unit, encoding='utf-8') as fh:
                    text = fh.read()
            except (OSError, UnicodeError) as e:
                out(False, f'unit systemd illeggibile: {unit}: {e}', is_fatal=True)
            else:
                residui = [p for p in daemonctl.PLACEHOLDERS if p in text]
                if residui:
                    out(False, f'unit systemd: {unit} contiene ancora i '
                               f'placeholder del template '
                               f'({", ".join(residui)}) — rilancia '
                               f'safekeep daemon install', is_fatal=True)
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


def cmd_daemon(args):
    """Dispatch dei sottocomandi `safekeep daemon` (daemonctl, SPEC §14.3)."""
    if args.daemon_cmd == 'install':
        return daemonctl.install(enable_linger=args.enable_linger,
                                 dry_run=args.dry_run)
    if args.daemon_cmd == 'uninstall':
        return daemonctl.uninstall(purge=args.purge)
    if args.daemon_cmd == 'start':
        return daemonctl.start()
    if args.daemon_cmd == 'stop':
        return daemonctl.stop()
    return daemonctl.status()


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        if args.comando == 'daemon':
            return cmd_daemon(args)
        if args.comando == 'run':
            return Daemon(args.config).run(verbose=args.v)
        if args.comando == 'status':
            return cmd_status(args)
        if args.comando == 'doctor':
            return cmd_doctor(args)
        if args.comando == 'restore':
            return cmd_restore(args)
        return cmd_sync_once(args)
    except ConfigError as e:
        print(f'errore di config: {e}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
