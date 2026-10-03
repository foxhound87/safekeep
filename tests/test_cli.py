"""Test della CLI (`--help`, `status`, `doctor`) su fixture temporanee (SPEC.md §9).

Ogni run è un subprocess con HOME finto: niente scritture nella home reale.
"""
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CLI = os.path.join(REPO, 'bin', 'safekeep.py')


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
        for marker in ('config', 'dest non annidate', 'fswatch', 'python',
                       'TCC', 'residui', 'plist'):
            self.assertIn(marker, r.stdout, f'check mancante: {marker}')
        for line in r.stdout.splitlines():
            if line.strip():
                self.assertIn(line[0], ('✔', '✗'), f'riga senza esito: {line!r}')

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


if __name__ == '__main__':
    unittest.main()
