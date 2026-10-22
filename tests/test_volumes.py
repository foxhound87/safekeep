import errno
import os
import tempfile
import unittest
from unittest import mock

from safekeep.volumes import (
    BACKOFF_CAP,
    backoff_schedule,
    dest_state,
    network_error,
    partition_dests,
)


class DestStateTest(unittest.TestCase):
    def test_path_esistente_ok(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(dest_state(tmp), 'ok')
        self.assertEqual(dest_state(__file__), 'ok')

    def test_path_assente_absent(self):
        self.assertEqual(dest_state('/percorso/che/non/esiste/mai'), 'absent')


class NetworkErrorTest(unittest.TestCase):
    """Probe di raggiungibilità (§20.6): NAS montato ma server morto."""

    def test_volume_raggiungibile_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(network_error(tmp))

    def test_errore_di_rete_torna_losoerror(self):
        boom = OSError(errno.ENOTCONN, 'Socket is not connected')
        with mock.patch('safekeep.volumes.os.listdir', side_effect=boom):
            err = network_error('/Volumes/nas')
        self.assertIs(err, boom)

    def test_errore_non_di_rete_none(self):
        # ENOENT/EACCES non sono "rete": li copre dest_state o il check TCC
        with mock.patch('safekeep.volumes.os.listdir',
                        side_effect=OSError(errno.ENOENT, 'No such file')):
            self.assertIsNone(network_error('/percorso/spento'))


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
