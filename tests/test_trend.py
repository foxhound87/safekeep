"""SPEC.md §9.2: `bin/safekeep-trend.sh` — wrapper trend errori di permesso.

Ogni run è un subprocess con HOME finto: niente scritture nella home reale e
niente tocco ai log reali. Copre: header scritto una sola volta (idempotente),
riga parseabile a 4 campi, riga appendata anche quando `doctor` esce 1, override
del path CSV via env.
"""
import os
import shutil
import subprocess
import tempfile
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TREND = os.path.join(REPO, 'bin', 'safekeep-trend.sh')
HEADER = 'timestamp,exit,count_24h,last'


@unittest.skipUnless(shutil.which('bash'), 'bash non disponibile')
class TrendTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.home = os.path.join(self.tmp, 'home')        # HOME finto
        os.makedirs(self.home)
        self.csv = os.path.join(self.home, '.local', 'state', 'safekeep',
                                'permission-trend.csv')

    def trend(self, *args, csv=None):
        env = dict(os.environ, HOME=self.home)
        if csv is not None:
            env['SAFEKEEP_TREND_CSV'] = csv
        return subprocess.run(['bash', TREND, *args], capture_output=True,
                              text=True, env=env, cwd=REPO, timeout=120)

    def lines(self, path=None):
        with open(path or self.csv, encoding='utf-8') as fh:
            return fh.read().splitlines()

    def test_header_e_riga_parseabile(self):
        r = self.trend()
        self.assertEqual(r.returncode, 0, r.stderr)
        lines = self.lines()
        self.assertEqual(lines[0], HEADER)
        self.assertEqual(len(lines), 2)                   # header + 1 run
        row = lines[1].split(',')
        self.assertEqual(len(row), 4)                     # split campi, niente quoting
        self.assertEqual(row[1], '0')                     # exit di doctor (JSON §9.1)
        self.assertEqual(int(row[2]), int(row[2]))        # count_24h intero
        self.assertTrue(row[0].startswith('20'))          # timestamp ISO 8601

    def test_run_multipla_non_duplica_header(self):
        self.assertEqual(self.trend().returncode, 0)
        self.assertEqual(self.trend().returncode, 0)
        lines = self.lines()
        self.assertEqual(len(lines), 3)                   # header + 2 run
        self.assertEqual(lines.count(HEADER), 1)          # header una sola volta
        self.assertEqual(lines[0], HEADER)

    def test_doctor_exit_1_genera_comunque_riga(self):
        # config inesistente passata esplicitamente → check fatale → exit 1,
        # ma l'exit code è un dato: la riga va scritta (SPEC §9.2)
        missing = os.path.join(self.tmp, 'non-esiste.cfg')
        r = self.trend('--config', missing)
        self.assertEqual(r.returncode, 0, r.stderr)
        row = self.lines()[1].split(',')
        self.assertEqual(row[1], '1')
        self.assertEqual(int(row[2]), int(row[2]))

    def test_override_csv_path(self):
        alt = os.path.join(self.tmp, 'alt', 'trend.csv')
        r = self.trend(csv=alt)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(os.path.exists(alt))
        self.assertFalse(os.path.exists(self.csv))        # default non toccato
        self.assertEqual(self.lines(alt)[0], HEADER)


if __name__ == '__main__':
    unittest.main()
