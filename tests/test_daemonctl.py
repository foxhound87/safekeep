"""Test di `safekeep daemon` (daemonctl, piano 0.7.0): rendering golden,
preflight piattaforma con mock, comandi launchd/systemd, package data e
wrapper deprecati.

**Mai launchctl/systemctl reali**: ogni subprocess passa da `daemonctl._run`
(mockkato) o da subprocess con PATH/HOME finti; nessun test carica o ferma
servizi sulla macchina.
"""
import contextlib
import getpass
import io
import os
import plistlib
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from safekeep import cli, daemonctl

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CLI = os.path.join(REPO, 'bin', 'safekeep.py')


def cp(rc=0, out='', err=''):
    return subprocess.CompletedProcess([], rc, stdout=out, stderr=err)


def argvs_of(run_mock):
    return [c.args[0] for c in run_mock.call_args_list]


class DaemonctlBase(unittest.TestCase):
    """HOME temporaneo + shim `safekeep` finto + patch delle piattaforme."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.home = os.path.join(self.tmp, 'home')
        os.makedirs(self.home)
        self.shim = os.path.join(self.tmp, 'bin', 'safekeep')
        os.makedirs(os.path.dirname(self.shim))
        with open(self.shim, 'w') as fh:
            fh.write('#!/bin/sh\nexit 0\n')
        os.chmod(self.shim, 0o755)

    def which_table(self, *missing):
        table = {'fswatch': '/usr/bin/fswatch',
                 'safekeep': self.shim,
                 'plutil': '/usr/bin/plutil'}
        for name in missing:
            table.pop(name, None)
        return table

    @contextlib.contextmanager
    def patched(self, darwin=True, linux=None, wsl=False, systemd=True,
                linger=None, which=None, run=None, hint=None):
        """Patch di `daemonctl` per entrambe le piattaforme + `_run` finto."""
        if linux is None:
            linux = not darwin
        table = self.which_table() if which is None else which
        run_patcher = mock.patch.object(
            daemonctl, '_run', side_effect=run or (lambda argv: cp()))
        patchers = [
            mock.patch.dict(os.environ, {'HOME': self.home}),
            mock.patch.object(daemonctl, 'is_darwin', return_value=darwin),
            mock.patch.object(daemonctl, 'is_linux', return_value=linux),
            mock.patch.object(daemonctl, 'is_wsl', return_value=wsl),
            mock.patch.object(daemonctl, 'systemd_user_available',
                              return_value=systemd),
            mock.patch.object(daemonctl, 'linger_enabled', return_value=linger),
            mock.patch.object(daemonctl.shutil, 'which',
                              side_effect=lambda n: table.get(n)),
        ]
        if hint is not None:
            patchers.append(mock.patch.object(daemonctl, 'install_hint',
                                              return_value=hint))
        with contextlib.ExitStack() as stack:
            for patcher in patchers:
                stack.enter_context(patcher)
            yield stack.enter_context(run_patcher)   # già rimosso da patchers

    @staticmethod
    def capture(func, *args, **kwargs):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = func(*args, **kwargs)
        return code, out.getvalue(), err.getvalue()

    def plist_file(self):
        return os.path.join(self.home, 'Library', 'LaunchAgents',
                            'com.safekeep.agent.plist')

    def unit_file(self):
        return os.path.join(self.home, '.config', 'systemd', 'user',
                            'safekeep.service')

    def cfg_file(self):
        return os.path.join(self.home, '.safekeep')

    def write_unit(self, darwin, text='[Unit]\nDescription=safekeep\n'):
        path = self.plist_file() if darwin else self.unit_file()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'w') as fh:
            fh.write(text)
        return path


class RenderingTest(DaemonctlBase):
    """Golden del rendering: plist validabile, nessun placeholder residuo."""

    def test_plist_golden(self):
        with self.patched(darwin=True):
            text = daemonctl.render(exec_path='/x/bin/safekeep',
                                    home='/Users/u',
                                    path_env='/opt/hb/bin:/usr/bin')
        for ph in daemonctl.PLACEHOLDERS:
            self.assertNotIn(ph, text, f'placeholder residuo: {ph}')
        plist = plistlib.loads(text.encode())
        self.assertEqual(plist['Label'], 'com.safekeep.agent')
        self.assertEqual(plist['ProgramArguments'], ['/x/bin/safekeep', 'run'],
                         'ExecStart = shim assoluto + sottocomando run')
        self.assertIs(plist['RunAtLoad'], True)
        self.assertIs(plist['KeepAlive'], True)
        self.assertEqual(plist['StandardOutPath'],
                         '/Users/u/.local/state/safekeep/out.log')
        self.assertEqual(plist['EnvironmentVariables']['PATH'],
                         '/opt/hb/bin:/usr/bin')
        self.assertEqual(plist['EnvironmentVariables']['PYTHONUNBUFFERED'], '1')

    def test_service_golden(self):
        with self.patched(darwin=False):
            text = daemonctl.render(exec_path='/x/bin/safekeep',
                                    home='/home/u', path_env='/usr/bin')
        for ph in daemonctl.PLACEHOLDERS:
            self.assertNotIn(ph, text, f'placeholder residuo: {ph}')
        for needle in ('ExecStart=/x/bin/safekeep run',
                       'Environment=PATH=/usr/bin',
                       'Environment=PYTHONUNBUFFERED=1',
                       'Restart=always', 'RestartSec=2',
                       'StartLimitIntervalSec=0', 'WantedBy=default.target'):
            self.assertIn(needle, text, needle)
        self.assertNotIn('/Users/', text, 'path assoluto nel template')

    def test_render_default_risolve_shim_e_home(self):
        with self.patched(darwin=True):
            text = daemonctl.render()
        self.assertIn(self.shim, text, '__EXEC__ = shutil.which("safekeep")')
        self.assertIn(self.home, text, '__HOME__ = $HOME')
        plist = plistlib.loads(text.encode())
        self.assertEqual(plist['ProgramArguments'][1], 'run')

    def test_path_env_con_dir_fswatch_e_senza_duplicati(self):
        with self.patched(darwin=False, which={'fswatch': '/usr/bin/fswatch',
                                               'safekeep': self.shim}):
            text = daemonctl.render()
        path_val = next(l.removeprefix('Environment=PATH=')
                        for l in text.splitlines()
                        if l.startswith('Environment=PATH='))
        self.assertTrue(path_val.startswith('/usr/bin:'), path_val)
        self.assertEqual(path_val, ':'.join(
            dict.fromkeys(path_val.split(':'))), 'nessun duplicato nel PATH')
        for d in daemonctl.SYSTEM_DIRS:
            self.assertIn(d, path_val)


class PackageDataTest(DaemonctlBase):
    """Task 2: i template e l'esempio sono package data leggibili."""

    def test_template_e_example_leggibili(self):
        example = daemonctl.resource_text('safekeep.example')
        self.assertIn('dest:', example)
        self.assertIn('REPLACEME', example)
        for name in ('com.safekeep.agent.plist.tmpl', 'safekeep.service.tmpl'):
            text = daemonctl.resource_text(name)
            self.assertIn('__EXEC__', text, name)
            self.assertIn('__PATH__', text, name)
            self.assertNotIn('/Users/', text, f'path assoluto in {name}')

    def test_example_embedded_e_in_sync_con_quello_del_repo(self):
        with open(daemonctl.example_path(), encoding='utf-8') as fh:
            embedded = fh.read()
        with open(os.path.join(REPO, 'examples', 'safekeep.example'),
                  encoding='utf-8') as fh:
            repo = fh.read()
        self.assertEqual(embedded, repo,
                         'safekeep/data/safekeep.example ed examples/ devono '
                         'restare identici (duplice sorgente)')
        self.assertTrue(os.path.exists(daemonctl.example_path()))


class PreflightTest(DaemonctlBase):
    def test_pronto_ok_vuoto(self):
        with self.patched(darwin=True):
            self.assertEqual(daemonctl.preflight(), [])

    def test_fswatch_mancante_hint_della_piattaforma(self):
        with self.patched(darwin=True, which=self.which_table('fswatch'),
                          hint='HINT-FSWATCH-PIATTAFORMA'):
            errs = daemonctl.preflight()
        joined = ' '.join(errs)
        self.assertIn('fswatch mancante', joined)
        self.assertIn('HINT-FSWATCH-PIATTAFORMA', joined)

    def test_shim_safekeep_mancante_hint_pipx(self):
        with self.patched(darwin=True, which=self.which_table('safekeep')):
            errs = daemonctl.preflight()
        joined = ' '.join(errs)
        self.assertIn('shim `safekeep` non trovato', joined)
        self.assertIn('pipx install safekeep', joined)

    def test_wsl_senza_systemd_errore_con_istruzioni(self):
        with self.patched(darwin=False, wsl=True, systemd=False) as run:
            errs = daemonctl.preflight()
            code, _, err = self.capture(daemonctl.install)
        joined = ' '.join(errs)
        for needle in ('WSL', '/etc/wsl.conf', 'systemd=true', 'wsl --shutdown',
                       'safekeep run'):
            self.assertIn(needle, joined, needle)
        self.assertEqual(code, 1, err)
        self.assertFalse(os.path.exists(self.unit_file()),
                         'nessuna scrittura se il preflight fallisce')
        self.assertFalse(os.path.exists(self.cfg_file()))
        run.assert_not_called()

    def test_wsl_con_systemd_ok_come_linux(self):
        with self.patched(darwin=False, wsl=True, systemd=True):
            self.assertEqual(daemonctl.preflight(), [])

    def test_os_non_supportato(self):
        with self.patched(darwin=False, linux=False):
            errs = daemonctl.preflight()
        self.assertIn('OS non supportato', ' '.join(errs))

    def test_preflight_non_scrive_niente(self):
        with self.patched(darwin=True, which=self.which_table('fswatch')):
            self.capture(daemonctl.install)
        self.assertFalse(os.path.exists(self.cfg_file()))
        self.assertFalse(os.path.exists(self.plist_file()))


class InstallTest(DaemonctlBase):
    def test_darwin_scrive_plist_e_non_avvia(self):
        with self.patched(darwin=True) as run:
            code, out, err = self.capture(daemonctl.install)
        self.assertEqual(code, 0, err)
        with open(self.plist_file(), encoding='utf-8') as fh:
            text = fh.read()
        for ph in daemonctl.PLACEHOLDERS:
            self.assertNotIn(ph, text, ph)
        plist = plistlib.loads(text.encode())
        self.assertEqual(plist['ProgramArguments'], [self.shim, 'run'])
        argv_list = argvs_of(run)
        self.assertIn(['plutil', '-lint', self.plist_file()], argv_list)
        self.assertFalse([a for a in argv_list if a[0] == 'launchctl'],
                         'install NON avvia l\'agent (poi: daemon start)')
        self.assertEqual(os.stat(self.cfg_file()).st_mode & 0o777, 0o600)
        self.assertIn('Full Disk Access', out)
        self.assertIn('daemon start', out)
        self.assertIn('creata', out)

    def test_config_non_sovrascritta_e_sempre_chmod_600(self):
        with open(self.cfg_file(), 'w') as fh:
            fh.write('dest: /mia/dest\ntesto mio\n')
        os.chmod(self.cfg_file(), 0o644)
        with self.patched(darwin=True):
            code, out, _ = self.capture(daemonctl.install)
        self.assertEqual(code, 0)
        with open(self.cfg_file(), encoding='utf-8') as fh:
            self.assertEqual(fh.read(), 'dest: /mia/dest\ntesto mio\n',
                             'mai sovrascrivere un config esistente')
        self.assertEqual(os.stat(self.cfg_file()).st_mode & 0o777, 0o600)
        self.assertNotIn('creata', out)

    def test_dry_run_non_scrive_nulla(self):
        with self.patched(darwin=True) as run:
            code, out, _ = self.capture(daemonctl.install, dry_run=True)
        self.assertEqual(code, 0)
        self.assertFalse(os.path.exists(self.plist_file()))
        self.assertFalse(os.path.exists(self.cfg_file()))
        self.assertIn('installerei', out)
        self.assertIn('--dry-run', out)
        run.assert_not_called()

    def test_unit_precedente_con_residui_segnalata_e_sostituita(self):
        with open(os.path.join(REPO, 'safekeep', 'data',
                               'com.safekeep.agent.plist.tmpl'),
                  encoding='utf-8') as fh:
            template = fh.read()          # copia grezza (install manuale)
        path = self.write_unit(darwin=True, text=template)
        with self.patched(darwin=True):
            code, out, _ = self.capture(daemonctl.install)
        self.assertEqual(code, 0)
        with open(path, encoding='utf-8') as fh:
            text = fh.read()
        for ph in daemonctl.PLACEHOLDERS:
            self.assertNotIn(ph, text, 'la unit vecchia va sostituita')
        self.assertIn('residui', out)
        self.assertIn('sostituita', out)

    def test_linux_scrive_unit_e_daemon_reload(self):
        with self.patched(darwin=False, systemd=True, linger=False) as run:
            code, out, err = self.capture(daemonctl.install)
        self.assertEqual(code, 0, err)
        with open(self.unit_file(), encoding='utf-8') as fh:
            text = fh.read()
        self.assertIn(f'ExecStart={self.shim} run', text)
        for ph in daemonctl.PLACEHOLDERS:
            self.assertNotIn(ph, text, ph)
        argv_list = argvs_of(run)
        self.assertIn(['systemctl', '--user', 'daemon-reload'], argv_list)
        self.assertFalse([a for a in argv_list
                          if a[:2] == ['systemctl', '--user'] and 'enable' in a],
                         'install NON avvia l\'unit')
        self.assertIn('linger NON abilitato', out)
        self.assertIn('loginctl enable-linger', out)

    def test_enable_linger_esegue_loginctl(self):
        with self.patched(darwin=False, systemd=True,
                          linger=False) as run:
            code, out, _ = self.capture(daemonctl.install, enable_linger=True)
        self.assertEqual(code, 0)
        self.assertIn(['loginctl', 'enable-linger', getpass.getuser()],
                      argvs_of(run))
        self.assertIn('linger abilitato', out)

    def test_linger_gia_abilitato_nessun_warning(self):
        with self.patched(darwin=False, systemd=True, linger=True):
            code, out, _ = self.capture(daemonctl.install)
        self.assertEqual(code, 0)
        self.assertNotIn('linger NON abilitato', out)
        self.assertNotIn('loginctl', out)

    def test_linux_senza_systemd_warning_non_fatale(self):
        # non-WSL senza systemd (container/CI): unit scritta, avvio manuale —
        # exit 0 (il WSL senza systemd, invece, è un errore: vedi preflight)
        with self.patched(darwin=False, systemd=False, linger=None) as run:
            code, out, _ = self.capture(daemonctl.install)
        self.assertEqual(code, 0)
        self.assertTrue(os.path.exists(self.unit_file()))
        self.assertIn('systemd non disponibile', out)
        self.assertIn('safekeep run', out)
        self.assertFalse(argvs_of(run), 'niente systemctl senza systemd')

    def test_render_rotto_non_viene_scritto(self):
        # difesa: se il render lasciasse un placeholder residuo, l'unit NON
        # va scritta (mai installare un template non renderizzato)
        with self.patched(darwin=True), \
                mock.patch.object(daemonctl, 'render',
                                  return_value='[bad] __EXEC__ non reso'):
            code, _, err = self.capture(daemonctl.install)
        self.assertEqual(code, 1)
        self.assertIn('non renderizzato', err)
        self.assertFalse(os.path.exists(self.plist_file()))

    def test_install_era_idempotente_due_volte(self):
        with self.patched(darwin=True):
            code1, _, _ = self.capture(daemonctl.install)
            code2, out2, _ = self.capture(daemonctl.install)
        self.assertEqual((code1, code2), (0, 0))
        self.assertNotIn('sostituita', out2, 'seconda run pulita, senza warning')
        self.assertIn('installato', out2)


class ComandiTest(DaemonctlBase):
    """start/stop/status/uninstall — solo `_run` mockkato, mai servizi reali."""

    def test_start_darwin_bootstrap_dopo_print(self):
        self.write_unit(darwin=True)

        def run(argv):
            return cp(1) if argv[1] == 'print' else cp(0)

        with self.patched(darwin=True, run=run) as r:
            code, out, _ = self.capture(daemonctl.start)
        self.assertEqual(code, 0)
        target = f'gui/{os.getuid()}'
        self.assertEqual(argvs_of(r), [
            ['launchctl', 'print', f'{target}/{daemonctl.DAEMON_LABEL}'],
            ['launchctl', 'bootstrap', target, self.plist_file()]])
        self.assertIn('avviato', out)

    def test_start_darwin_gia_caricato_idempotente(self):
        self.write_unit(darwin=True)
        with self.patched(darwin=True, run=lambda argv: cp(0)) as r:
            code, out, _ = self.capture(daemonctl.start)
        self.assertEqual(code, 0)
        self.assertEqual(len(r.call_args_list), 1, 'solo il print, niente bootstrap')
        self.assertIn('già caricato', out)

    def test_start_linux_enable_now(self):
        self.write_unit(darwin=False)
        with self.patched(darwin=False) as r:
            code, out, _ = self.capture(daemonctl.start)
        self.assertEqual(code, 0)
        self.assertEqual(argvs_of(r),
                         [['systemctl', '--user', 'enable', '--now', 'safekeep']])
        self.assertIn('avviato', out)

    def test_start_senza_unit_hint_install(self):
        with self.patched(darwin=True) as r:
            code, _, err = self.capture(daemonctl.start)
        self.assertEqual(code, 1)
        self.assertIn('daemon install', err)
        r.assert_not_called()

    def test_stop_darwin_bootout_e_linux_disable(self):
        for darwin, argv in (
                (True, ['launchctl', 'bootout',
                        f'gui/{os.getuid()}/{daemonctl.DAEMON_LABEL}']),
                (False, ['systemctl', '--user', 'disable', '--now', 'safekeep'])):
            with self.subTest(darwin=darwin), \
                    self.patched(darwin=darwin, run=lambda a: cp(0)) as r:
                code, out, _ = self.capture(daemonctl.stop)
            self.assertEqual(code, 0)
            self.assertEqual(argvs_of(r), [argv])
            self.assertIn('fermato', out)

    def test_stop_non_caricato_esce_1(self):
        with self.patched(darwin=True, run=lambda a: cp(1, err='no service')):
            code, _, err = self.capture(daemonctl.stop)
        self.assertEqual(code, 1)
        self.assertIn('no service', err)

    def test_status_senza_unit_esce_1(self):
        with self.patched(darwin=True) as r:
            code, out, _ = self.capture(daemonctl.status)
        self.assertEqual(code, 1)
        self.assertIn('non installato', out)
        r.assert_not_called()

    def test_status_darwin_running_si_no(self):
        self.write_unit(darwin=True)
        casi = ((cp(0, out='\tstate = running\n\tpid = 1\n'), 0, 'in esecuzione'),
                (cp(0, out='\tstate = waiting\n'), 1, 'non in esecuzione'),
                (cp(1, err='not loaded'), 1, 'non caricato'))
        for result, atteso, marker in casi:
            with self.subTest(marker=marker), \
                    self.patched(darwin=True, run=lambda a, r=result: r):
                code, out, _ = self.capture(daemonctl.status)
            self.assertEqual(code, atteso, out)
            self.assertIn(marker, out)

    def test_status_linux_delega_a_systemctl(self):
        self.write_unit(darwin=False)
        for rc, atteso in ((0, 0), (3, 1)):
            with self.subTest(rc=rc), \
                    self.patched(darwin=False,
                                 run=lambda a, rc=rc: cp(rc, out='active (running)')):
                code, out, _ = self.capture(daemonctl.status)
            self.assertEqual(code, atteso, out)
        with self.patched(darwin=False, run=lambda a: cp(0)) as r:
            self.capture(daemonctl.status)
        self.assertEqual(argvs_of(r),
                         [['systemctl', '--user', 'status', 'safekeep',
                           '--no-pager']])

    def test_uninstall_conserva_config_e_log(self):
        path = self.write_unit(darwin=True)
        with open(self.cfg_file(), 'w') as fh:
            fh.write('dest: /d\n')
        state = os.path.join(self.home, '.local/state/safekeep')
        os.makedirs(state)
        with open(os.path.join(state, 'safekeep.log'), 'w') as fh:
            fh.write('log\n')
        with self.patched(darwin=True) as r:
            code, out, _ = self.capture(daemonctl.uninstall)
        self.assertEqual(code, 0)
        self.assertFalse(os.path.exists(path))
        self.assertTrue(os.path.exists(self.cfg_file()), 'config intatta')
        self.assertTrue(os.path.exists(os.path.join(state, 'safekeep.log')),
                        'log intatti')
        self.assertEqual(argvs_of(r), [
            ['launchctl', 'bootout', f'gui/{os.getuid()}/{daemonctl.DAEMON_LABEL}']])
        self.assertIn('--purge', out)

    def test_uninstall_purge_rimuove_config_e_log(self):
        path = self.write_unit(darwin=False)
        with open(self.cfg_file(), 'w') as fh:
            fh.write('dest: /d\n')
        state = os.path.join(self.home, '.local/state/safekeep')
        os.makedirs(state)
        with self.patched(darwin=False, systemd=True) as r:
            code, out, _ = self.capture(daemonctl.uninstall, purge=True)
        self.assertEqual(code, 0)
        self.assertFalse(os.path.exists(path))
        self.assertFalse(os.path.exists(self.cfg_file()))
        self.assertFalse(os.path.exists(state))
        self.assertIn('rimossi anche', out)
        self.assertIn(['systemctl', '--user', 'disable', '--now', 'safekeep'],
                      argvs_of(r))
        self.assertIn(['systemctl', '--user', 'daemon-reload'], argvs_of(r))

    def test_uninstall_senza_unit_esce_zero(self):
        with self.patched(darwin=True) as r:
            code, out, _ = self.capture(daemonctl.uninstall)
        self.assertEqual(code, 0, 'idempotente: niente unit = nessun errore')
        self.assertIn('nessuna unit', out)
        self.assertEqual(len(r.call_args_list), 1, 'bootout best-effort')


class CliDaemonTest(unittest.TestCase):
    """Task 3: sottocomandi `daemon` nel parser + dispatch."""

    def cli(self, *args):
        return subprocess.run([sys.executable, CLI, *args], capture_output=True,
                              text=True, timeout=60)

    def test_daemon_help_mostra_i_sottocomandi(self):
        r = self.cli('daemon', '--help')
        self.assertEqual(r.returncode, 0, r.stderr)
        for cmd in ('install', 'start', 'stop', 'status', 'uninstall'):
            self.assertIn(cmd, r.stdout, cmd)
        # normalizza: argparse wrappa il description su più righe
        self.assertIn('safekeep run', ' '.join(r.stdout.split()),
                      'ExecStart documentato')

    def test_daemon_install_help_flag(self):
        r = self.cli('daemon', 'install', '--help')
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn('--enable-linger', r.stdout)
        self.assertIn('--dry-run', r.stdout)

    def test_daemon_uninstall_help_purge(self):
        r = self.cli('daemon', 'uninstall', '--help')
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn('--purge', r.stdout)

    def test_arg_parsing_per_sottocomando(self):
        args = cli.build_parser().parse_args(['daemon', 'install',
                                              '--enable-linger'])
        self.assertEqual((args.comando, args.daemon_cmd), ('daemon', 'install'))
        self.assertTrue(args.enable_linger)
        self.assertFalse(args.dry_run)
        args = cli.build_parser().parse_args(['daemon', 'uninstall', '--purge'])
        self.assertTrue(args.purge)
        args = cli.build_parser().parse_args(['daemon', 'status'])
        self.assertEqual(args.daemon_cmd, 'status')

    def test_dispatch_install_e_status(self):
        with mock.patch.object(cli.daemonctl, 'install',
                               return_value=7) as inst, \
                mock.patch.object(cli.daemonctl, 'status',
                                  return_value=0) as stat:
            self.assertEqual(cli.main(['daemon', 'install', '--dry-run']), 7)
            self.assertEqual(cli.main(['daemon', 'status']), 0)
        inst.assert_called_once_with(enable_linger=False, dry_run=True)
        stat.assert_called_once_with()

    def test_dispatch_uninstall_purge_e_start(self):
        with mock.patch.object(cli.daemonctl, 'uninstall',
                               return_value=0) as un, \
                mock.patch.object(cli.daemonctl, 'start',
                                  return_value=1) as st:
            self.assertEqual(cli.main(['daemon', 'uninstall', '--purge']), 0)
            self.assertEqual(cli.main(['daemon', 'start']), 1)
        un.assert_called_once_with(purge=True)
        st.assert_called_once_with()


class DoctorHintTest(DaemonctlBase):
    """Task 4: l'hint del config mancante viene dall'example embedded,
    non più 'clone the repo'."""

    def test_hint_da_example_embedded_senza_repo(self):
        env = dict(os.environ, HOME=self.home)
        r = subprocess.run([sys.executable, CLI, 'doctor'], capture_output=True,
                           text=True, env=env, cwd=REPO, timeout=60)
        self.assertIn(r.returncode, (0, 1), r.stdout + r.stderr)
        self.assertIn('safekeep/data/safekeep.example', r.stdout,
                      'hint = cp dell\'esempio embedded (package data)')
        self.assertIn(self.home + '/.safekeep', r.stdout)
        self.assertNotIn('clone', r.stdout,
                         'niente più "clone the repo" per un install pip')


class WrapperTest(unittest.TestCase):
    """Task 5: install.sh/uninstall.sh sono wrapper deprecati che delegano
    (mai servizi reali: il PATH ha solo uno shim finto o è vuoto)."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.home = os.path.join(self.tmp, 'home')
        os.makedirs(self.home)
        self.fake_bin = os.path.join(self.tmp, 'bin')
        os.makedirs(self.fake_bin)
        shim = os.path.join(self.fake_bin, 'safekeep')
        with open(shim, 'w') as fh:
            fh.write('#!/bin/sh\necho "$@"\n')
        os.chmod(shim, 0o755)
        self.empty_bin = os.path.join(self.tmp, 'empty')
        os.makedirs(self.empty_bin)

    def run_sh(self, script, *args, path=None):
        env = dict(os.environ, HOME=self.home)
        env['PATH'] = path if path is not None else (
            self.fake_bin + os.pathsep + os.environ['PATH'])
        return subprocess.run(['/bin/bash', os.path.join(REPO, script), *args],
                              capture_output=True, text=True, env=env,
                              timeout=60)

    def test_install_sh_delega_a_daemon_install(self):
        r = self.run_sh('install.sh')
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(r.stdout.strip(), 'daemon install',
                         'shim finto riceve: safekeep daemon install')
        self.assertIn('deprecato', r.stderr)
        self.assertIn('0.8.0', r.stderr)
        self.assertIn('safekeep daemon start', r.stderr)

    def test_install_sh_senza_safekeep_hint_pipx(self):
        r = self.run_sh('install.sh', path=self.empty_bin)
        self.assertEqual(r.returncode, 1)
        self.assertIn('pipx install safekeep', r.stderr)

    def test_uninstall_sh_delega_e_passa_purge(self):
        r = self.run_sh('uninstall.sh')
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(r.stdout.strip(), 'daemon uninstall')
        self.assertIn('deprecato', r.stderr)
        r = self.run_sh('uninstall.sh', '-p')
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(r.stdout.strip(), 'daemon uninstall --purge')

    def test_uninstall_sh_senza_safekeep_hint_pipx(self):
        r = self.run_sh('uninstall.sh', path=self.empty_bin)
        self.assertEqual(r.returncode, 1)
        self.assertIn('pipx install safekeep', r.stderr)

    def test_wrapper_tengono_legacy_trend(self):
        for script, nuovo in (('install.sh', 'safekeep daemon install'),
                              ('uninstall.sh', 'safekeep daemon uninstall')):
            with self.subTest(script=script), \
                    open(os.path.join(REPO, script), encoding='utf-8') as fh:
                text = fh.read()
            self.assertIn('deprecato', text, script)
            self.assertIn(nuovo, text, script)
            self.assertIn('--trend', text, 'trend legacy resta nel wrapper')
        self.assertTrue(os.path.exists(
            os.path.join(REPO, 'launchd', 'com.safekeep.trend.plist')),
            'il template del trend resta nel repo (fuori scope 0.7.0)')
        self.assertFalse(os.path.exists(
            os.path.join(REPO, 'launchd', 'com.safekeep.agent.plist')),
            'il template principale vive ora nel package')


if __name__ == '__main__':
    unittest.main()
