import os
import tempfile
import unittest

from safekeep.volumes import BACKOFF_CAP, backoff_schedule, dest_state, partition_dests


class DestStateTest(unittest.TestCase):
    def test_path_esistente_ok(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(dest_state(tmp), 'ok')
        self.assertEqual(dest_state(__file__), 'ok')

    def test_path_assente_absent(self):
        self.assertEqual(dest_state('/percorso/che/non/esiste/mai'), 'absent')


class BackoffTest(unittest.TestCase):
    def test_primi_valori_e_cap_a_60(self):
        it = backoff_schedule()
        self.assertEqual([next(it) for _ in range(12)],
                         [1, 2, 4, 8, 16, 32, 60, 60, 60, 60, 60, 60])
        self.assertEqual(BACKOFF_CAP, 60)
        self.assertTrue(all(0 < v <= BACKOFF_CAP for v in
                            [next(it) for _ in range(50)]))


class PartitionTest(unittest.TestCase):
    def test_split_ready_absent(self):
        with tempfile.TemporaryDirectory() as tmp:
            mounted = os.path.join(tmp, 'montato')
            os.mkdir(mounted)
            missing = os.path.join(tmp, 'smontato')
            ready, absent = partition_dests([mounted, missing, '/nessuno/qui'])
            self.assertEqual(ready, [mounted])
            self.assertEqual(absent, [missing, '/nessuno/qui'])

    def test_tutte_ready_o_tutte_absent(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(partition_dests([tmp]), ([tmp], []))
        self.assertEqual(partition_dests(['/a', '/b']), ([], ['/a', '/b']))


if __name__ == '__main__':
    unittest.main()
