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


if __name__ == '__main__':
    unittest.main()
