"""Test della CLI (`--help`, `status`, `doctor`) su fixture temporanee (SPEC.md §9).

Ogni run è un subprocess con HOME finto: niente scritture nella home reale.
"""
import argparse
import contextlib
import getpass
import io
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from unittest import mock

from safekeep import cli, platform as plat
from safekeep.platform import fswatch_monitor

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CLI = os.path.join(REPO, 'bin', 'safekeep.py')

# Check di `doctor` condivisi da tutte le piattaforme (SPEC.md §9)
BASE_MARKERS = ('config', 'dest non annidate', 'fswatch', 'python', 'residui')
# SPEC.md §14.4: TCC e plist sono Darwin-only, inotify e unit systemd Linux-only
DARWIN_MARKERS = ('TCC', 'plist')
LINUX_MARKERS = ('inotify', 'unit systemd')


def write(path, text=''):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'w') as f:
        f.write(text)
    return path


class CliTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.home = os.path.join(self.tmp, 'home')        # HOME finto
        os.makedirs(self.home)
        self.src = os.path.join(self.tmp, 'src')
        self.dest = os.path.join(self.tmp, 'dst')
        os.makedirs(self.dest)
        write(os.path.join(self.src, 'proj', '.sync'), 'include: *.md\n')
        write(os.path.join(self.src, 'proj', 'docs/note.md'), 'x')
        self.cfg = write(os.path.join(self.tmp, 'safekeep.cfg'),
                         f'source: {self.src}\ndest: {self.dest}\n')

    def cli(self, *args):
        env = dict(os.environ, HOME=self.home)
        return subprocess.run([sys.executable, CLI, *args], capture_output=True,
                              text=True, env=env, cwd=REPO, timeout=60)


class StatusTest(CliTestCase):
    def test_help_mostra_status_e_doctor(self):
        r = self.cli('--help')
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn('status', r.stdout)
        self.assertIn('doctor', r.stdout)

    def test_status_stampa_tutto_e_non_scrive(self):
        r = self.cli('status', '--config', self.cfg)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn(self.cfg, r.stdout)
        self.assertIn(self.src, r.stdout)                 # source
        self.assertIn('regole', r.stdout)                 # progetto + N regole
        self.assertIn('[ok]', r.stdout)                   # dest_state
        self.assertEqual(os.listdir(self.dest), [], 'status è sola lettura')


class DoctorTest(CliTestCase):
    def test_doctor_gira_tutti_i_check(self):
        r = self.cli('doctor', '--config', self.cfg)
        self.assertIn(r.returncode, (0, 1), r.stdout + r.stderr)
        markers = BASE_MARKERS + (DARWIN_MARKERS if sys.platform == 'darwin'
                                  else LINUX_MARKERS)
        for marker in markers:
            self.assertIn(marker, r.stdout, f'check mancante: {marker}')
        for line in r.stdout.splitlines():
            if line.strip():
                self.assertIn(line[0], ('✔', '✗'), f'riga senza esito: {line!r}')

    def test_doctor_monitor_della_piattaforma(self):
        # SPEC.md §14.4: il monitor atteso va cercato in `fswatch -M`
        if shutil.which('fswatch') is None:
            self.skipTest('fswatch non installato')
        r = self.cli('doctor', '--config', self.cfg)
        self.assertIn(f'fswatch monitor: {fswatch_monitor()} presente', r.stdout)

    @unittest.skipUnless(sys.platform == 'darwin', 'check TCC macOS')
    def test_doctor_tcc_e_darwin_only(self):
        r = self.cli('doctor', '--config', self.cfg)
        self.assertIn('TCC', r.stdout)

    @unittest.skipIf(sys.platform == 'darwin', 'check inotify Linux')
    def test_doctor_inotify_e_linux_only(self):
        r = self.cli('doctor', '--config', self.cfg)
        self.assertIn('inotify', r.stdout)
        self.assertNotIn('TCC', r.stdout, 'TCC è un meccanismo di macOS')
        for line in r.stdout.splitlines():
            if line.startswith('✗') and 'inotify' in line:
                self.assertIn('warning non fatale', line)

    @unittest.skipUnless(shutil.which('fswatch'), 'fswatch non installato')
    def test_doctor_limite_basso_non_e_fatale(self):
        """SPEC.md §14.4: su Linux il limite di watch basso è informativo —
        doctor esce 0, non 1. I rami Linux si esercitano patchando l'helper,
        senza girare su Linux."""
        args = argparse.Namespace(config=self.cfg, v=False)
        buf = io.StringIO()
        with mock.patch.object(cli, 'is_linux', return_value=True), \
                mock.patch.object(cli, 'is_darwin', return_value=False), \
                mock.patch.object(cli, 'inotify_limit', return_value=8192), \
                contextlib.redirect_stdout(buf):
            code = cli.cmd_doctor(args)
        out = buf.getvalue()
        self.assertIn('max_user_watches=8192', out)
        self.assertIn('warning non fatale', out)
        self.assertIn('unit systemd', out)
        self.assertNotIn('TCC', out, 'TCC è un meccanismo di macOS')
        self.assertEqual(code, 0, out)

    def test_doctor_config_invalido_esce_1(self):
        bad = write(os.path.join(self.tmp, 'bad.cfg'), 'riga nuda invalida\n')
        r = self.cli('doctor', '--config', bad)
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertIn('config non valida', r.stdout)

    def test_doctor_dest_dentro_source_esce_1(self):
        bad = write(os.path.join(self.tmp, 'loop.cfg'),
                    f'source: {self.src}\ndest: {self.src}/backup\n')
        r = self.cli('doctor', '--config', bad)
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertIn('loop di copia', r.stdout)

    def test_doctor_config_assente_warning_con_hint_non_fatale(self):
        # nessun ~/.safekeep nel HOME finto: warning con hint, non fatale
        r = self.cli('doctor')
        self.assertIn('config assente', r.stdout)
        self.assertIn('safekeep.example', r.stdout)
        if shutil.which('fswatch'):     # unica dipendenza esterna del resto
            self.assertEqual(r.returncode, 0, r.stdout)


class AutoDiscoveryCliTest(CliTestCase):
    """Config SENZA `source:` → modalità auto-discovery (SPEC.md §6)."""

    def auto_cfg(self):
        return write(os.path.join(self.tmp, 'auto.cfg'), f'dest: {self.dest}\n')

    def test_status_mostra_modalita_e_progetti_scoperti(self):
        write(os.path.join(self.home, 'proj2', '.sync'), 'include: *.md\n')
        r = self.cli('status', '--config', self.auto_cfg())
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn('auto-discovery', r.stdout)
        self.assertIn('proj2', r.stdout)                 # progetto scoperto
        self.assertNotIn('modalità  source', r.stdout)

    def test_status_source_mode_invariato(self):
        r = self.cli('status', '--config', self.cfg)     # config CON source
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn('modalità  source', r.stdout)
        self.assertNotIn('auto-discovery', r.stdout)

    def test_doctor_senza_source_info_verde(self):
        r = self.cli('doctor', '--config', self.auto_cfg())
        self.assertIn(r.returncode, (0, 1), r.stdout + r.stderr)
        self.assertIn('auto-discovery', r.stdout, 'nessuna source = info, non warning')
        for line in r.stdout.splitlines():
            if 'config valida' in line:
                self.assertTrue(line.startswith('✔'), line)
                break
        else:
            self.fail('check config mancante')


class LoopDestTest(CliTestCase):
    """CR-02: dest dentro la sorgente → `run` e `sync-once` escono con ≠ 0."""

    def loop_cfg(self):
        return write(os.path.join(self.tmp, 'loop.cfg'),
                     f'source: {self.src}\ndest: {self.src}/backup\n')

    def test_run_dest_dentro_source_esce_1_e_non_copia(self):
        r = self.cli('run', '--config', self.loop_cfg())
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn('loop di copia', r.stderr)
        self.assertFalse(os.path.exists(os.path.join(self.src, 'backup')),
                         'nessuna copia prima del validate')

    def test_sync_once_dest_dentro_source_esce_1_e_non_copia(self):
        r = self.cli('sync-once', '--config', self.loop_cfg())
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn('loop di copia', r.stderr)
        self.assertFalse(os.path.exists(os.path.join(self.src, 'backup')))

    def test_sync_once_auto_dest_dentro_home_esce_1(self):
        # nessuna `source` → base del validate = $HOME (auto-discovery)
        cfg = write(os.path.join(self.tmp, 'auto-loop.cfg'),
                    f'dest: {self.home}/backup\n')
        r = self.cli('sync-once', '--config', cfg)
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn('loop di copia', r.stderr)


class StatusConfigCattivoTest(CliTestCase):
    def test_status_config_non_utf8_esce_1(self):
        bad = os.path.join(self.tmp, 'bin.cfg')
        with open(bad, 'wb') as fh:
            fh.write(b'source: \xff\xfe\ndest: /d\n')
        r = self.cli('status', '--config', bad)
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn('errore di config', r.stderr)


class DoctorSyncTest(CliTestCase):
    """SPEC.md §9.2 (CR-03): doctor segnala i `.sync` rotti senza essere fatale."""

    def test_sync_senza_regole_valide_warning_non_fatale(self):
        rotto = write(os.path.join(self.src, 'rotto', '.sync'), '[z-a]*\n')
        r = self.cli('doctor', '--config', self.cfg)
        line = next(l for l in r.stdout.splitlines()
                    if 'nessuna regola valida' in l)
        self.assertTrue(line.startswith('✗'), line)
        self.assertIn(rotto, line)
        self.assertIn('warning non fatale', line)

    def test_sync_non_utf8_segnalato(self):
        path = os.path.join(self.src, 'bin', '.sync')
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'wb') as fh:
            fh.write(b'\xff\xfe\n')
        r = self.cli('doctor', '--config', self.cfg)
        self.assertIn('.sync non validi', r.stdout)
        self.assertIn(path, r.stdout)

    def test_righe_scartate_diventano_warning(self):
        write(os.path.join(self.src, 'misto', '.sync'),
              'include: *.md\n[z-a]*\ninclude: *.txt\n')
        r = self.cli('doctor', '--config', self.cfg)
        self.assertIn('righe scartate', r.stdout)
        self.assertIn('warning non fatale', r.stdout)


class PruneCliTest(CliTestCase):
    def test_help_mostra_prune(self):
        r = self.cli('sync-once', '--help')
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn('--prune', r.stdout)

    def test_prune_rimuove_solo_i_file_esclusi(self):
        sync = os.path.join(self.src, 'proj', '.sync')
        write(sync, 'include: *.md\ninclude: *.txt\n')
        write(os.path.join(self.src, 'proj', 'keep.md'), 'k')
        write(os.path.join(self.src, 'proj', 'old.txt'), 'vecchio')
        r = self.cli('sync-once', '--config', self.cfg)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertTrue(os.path.exists(os.path.join(self.dest, 'proj/old.txt')))
        # residui: sorgente sparita e file fuori dal prefisso del progetto
        write(os.path.join(self.dest, 'proj/gone.md'), 'sorgente sparita')
        write(os.path.join(self.dest, 'fuori.md'), 'fuori dal prefisso')

        write(sync, 'include: *.md\n')                 # le regole cambiano
        r = self.cli('sync-once', '--prune', '--config', self.cfg)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn('rimossi: 1', r.stdout)
        self.assertFalse(os.path.exists(os.path.join(self.dest, 'proj/old.txt')))
        for keep in ('proj/keep.md', 'proj/gone.md', 'proj/.sync', 'fuori.md'):
            self.assertTrue(os.path.exists(os.path.join(self.dest, keep)), keep)


class DoctorPermessiRecentiTest(CliTestCase):
    """SPEC.md §14.4: errori di permesso nel log corrente — finestra 24h,
    warning non fatale, log assente ⇒ check saltato."""

    def log_path(self):
        return os.path.join(self.home, '.local/state/safekeep/safekeep.log')

    @staticmethod
    def stamp(delta):
        return (datetime.now() - delta).strftime('%Y-%m-%d %H:%M:%S,000')

    def riga(self, delta, msg='[Errno 1] Operation not permitted: /x.tmp.1'):
        return f'{self.stamp(delta)} ERROR copia fallita a → b: {msg}\n'

    def riga_doctor(self, stdout):
        self.assertIn('errori di permesso', stdout, stdout)
        return next(l for l in stdout.splitlines() if 'errori di permesso' in l)

    def test_errore_recente_warning_non_fatale(self):
        write(self.log_path(), self.riga(timedelta(hours=3)))
        r = self.cli('doctor', '--config', self.cfg)
        line = self.riga_doctor(r.stdout)
        self.assertTrue(line.startswith('✗'), line)
        for needle in ('nelle ultime 24h', 'warning non fatale',
                       'sync-once --dry-run', 'Full Disk Access',
                       self.log_path()):
            self.assertIn(needle, line, needle)
        self.assertEqual(r.returncode, 0, r.stdout)      # NON fatale

    def test_errore_vecchio_verde(self):
        write(self.log_path(),
              self.riga(timedelta(days=3), '[Errno 13] Permission denied'))
        r = self.cli('doctor', '--config', self.cfg)
        line = self.riga_doctor(r.stdout)
        self.assertTrue(line.startswith('✔'), line)
        self.assertIn('nessuno nelle ultime 24h', line)

    def test_log_assente_check_saltato(self):
        r = self.cli('doctor', '--config', self.cfg)     # nessun log nel HOME finto
        line = self.riga_doctor(r.stdout)
        self.assertTrue(line.startswith('✔'), line)
        self.assertIn('check saltato', line)


@unittest.skipUnless(shutil.which('fswatch'), 'fswatch non installato')
class DoctorLingerTest(CliTestCase):
    """SPEC.md §14.4: linger solo dove c'è la unit systemd, warning non fatale."""

    def doctor_linux(self, unit=True, linger=()):
        if unit:
            write(os.path.join(self.home, '.config/systemd/user/safekeep.service'),
                  '[Unit]\nDescription=safekeep\n')
        linger_dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, linger_dir, ignore_errors=True)
        for name in linger:
            open(os.path.join(linger_dir, name), 'w').close()
        args = argparse.Namespace(config=self.cfg, v=False)
        buf = io.StringIO()
        with mock.patch.dict(os.environ, {'HOME': self.home}), \
                mock.patch.object(cli, 'is_linux', return_value=True), \
                mock.patch.object(cli, 'is_darwin', return_value=False), \
                mock.patch.object(plat, 'LINGER_DIR', linger_dir), \
                contextlib.redirect_stdout(buf):
            code = cli.cmd_doctor(args)
        return code, buf.getvalue()

    @staticmethod
    def riga_linger(out):
        return next(l for l in out.splitlines() if 'linger' in l)

    def test_unit_assenta_niente_linger(self):
        code, out = self.doctor_linux(unit=False)
        self.assertNotIn('linger', out, 'il check esiste solo con la unit installata')
        self.assertEqual(code, 0, out)

    def test_linger_disabilitato_warning_non_fatale(self):
        code, out = self.doctor_linux(unit=True)
        line = self.riga_linger(out)
        self.assertTrue(line.startswith('✗'), line)
        self.assertIn('loginctl enable-linger', line)
        self.assertIn('warning non fatale', line)
        self.assertEqual(code, 0, out)                   # NON fatale

    def test_linger_abilitato_verde(self):
        code, out = self.doctor_linux(unit=True, linger=(getpass.getuser(),))
        line = self.riga_linger(out)
        self.assertTrue(line.startswith('✔'), line)
        self.assertEqual(code, 0, out)


class DoctorWslTest(CliTestCase):
    """SPEC.md §17.2: le tre righe WSL di `doctor` sono informative e non fatali."""

    def doctor_wsl(self, distro='Ubuntu', systemd=False, dest='/mnt/c/backup'):
        cfg = write(os.path.join(self.tmp, 'wsl.cfg'), f'dest: {dest}\n')
        args = argparse.Namespace(config=cfg, v=False)
        buf = io.StringIO()
        with mock.patch.dict(os.environ, {'HOME': self.home}), \
                mock.patch.object(cli, 'is_wsl', return_value=True), \
                mock.patch.object(cli, 'wsl_distro', return_value=distro), \
                mock.patch.object(cli, 'systemd_user_available',
                                  return_value=systemd), \
                contextlib.redirect_stdout(buf):
            code = cli.cmd_doctor(args)
        return code, buf.getvalue()

    @unittest.skipUnless(shutil.which('fswatch'), 'fswatch non installato')
    def test_wsle_righe_con_hint_systemd_e_drvfs(self):
        code, out = self.doctor_wsl()
        self.assertIn('✔ WSL rilevato: Ubuntu', out)
        hint = next(l for l in out.splitlines()
                    if 'systemd user non disponibile' in l)
        self.assertTrue(hint.startswith('✗'), hint)
        for needle in ('/etc/wsl.conf', 'systemd=true', 'safekeep run',
                       'warning non fatale'):
            self.assertIn(needle, hint, needle)
        drvfs = next(l for l in out.splitlines() if 'drvfs' in l)
        self.assertIn('/mnt/', drvfs)
        self.assertEqual(code, 0, out)                     # mai fatale

    @unittest.skipUnless(shutil.which('fswatch'), 'fswatch non installato')
    def test_systemd_attivo_nessun_hint(self):
        code, out = self.doctor_wsl(systemd=True)
        self.assertNotIn('systemd user non disponibile', out)
        self.assertIn('WSL rilevato', out)
        self.assertEqual(code, 0, out)

    @unittest.skipUnless(shutil.which('fswatch'), 'fswatch non installato')
    def test_nessun_path_sotto_mnt_niente_drvfs(self):
        code, out = self.doctor_wsl(dest=os.path.join(self.tmp, 'dst'))
        self.assertNotIn('drvfs', out)
        self.assertEqual(code, 0, out)

    def test_detection_reale_via_env_fino_a_doctor(self):
        """Detection reale (env) fino all'output di doctor: niente patch."""
        env = dict(os.environ, HOME=self.home, WSL_DISTRO_NAME='Debian')
        r = subprocess.run([sys.executable, CLI, 'doctor', '--config', self.cfg],
                           capture_output=True, text=True, env=env, cwd=REPO,
                           timeout=60)
        self.assertIn('WSL rilevato: Debian', r.stdout)

    @unittest.skipUnless(shutil.which('fswatch'), 'fswatch non installato')
    def test_senza_wsl_nulla_cambia(self):
        """Su Linux/macOS non-WSL nessuna riga WSL compare (SPEC §17)."""
        env = {k: v for k, v in os.environ.items() if k != 'WSL_DISTRO_NAME'}
        env['HOME'] = self.home
        r = subprocess.run([sys.executable, CLI, 'doctor', '--config', self.cfg],
                           capture_output=True, text=True, env=env, cwd=REPO,
                           timeout=60)
        self.assertNotIn('WSL', r.stdout)
        self.assertNotIn('drvfs', r.stdout)


if __name__ == '__main__':
    unittest.main()
