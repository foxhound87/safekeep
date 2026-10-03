import os
import shutil
import tempfile
import unittest
from unittest import mock

from safekeep import copier
from safekeep.config import ConfigError, dest_path, validate_dests
from safekeep.copier import copy_one, needs_copy, reconcile_project
from safekeep.matcher import BUILTIN_RULES, Matcher, parse_rules

LOGGER = 'safekeep'


def write(path, text, mtime=None):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'w') as f:
        f.write(text)
    if mtime is not None:
        os.utime(path, (mtime, mtime))
    return path


def read(path):
    with open(path) as f:
        return f.read()


class CopierTestCase(unittest.TestCase):
    """Ogni test gira in un tmpdir; tearDown verifica che NON resti alcun
    residuo `*.safekeep.tmp.*` (tmp + rename puliti)."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def tearDown(self):
        leftovers = []
        for dirpath, _, filenames in os.walk(self.tmp):
            leftovers += [os.path.join(dirpath, n) for n in filenames
                          if copier.TMP_INFIX in n]
        self.assertEqual(leftovers, [], f'tmp orfani: {leftovers}')

    def paths(self, *rel):
        return [os.path.join(self.tmp, r) for r in rel]


class CopyOneTest(CopierTestCase):
    def test_copia_normale_contenuto_e_mtime(self):
        src, dst = self.paths('src/a.txt', 'dst/deep/a.txt')
        write(src, 'contenuto', mtime=1_000_000_000)
        self.assertTrue(copy_one(src, dst))
        self.assertEqual(read(dst), 'contenuto')
        self.assertEqual(int(os.stat(dst).st_mtime), 1_000_000_000)   # mtime preservato

    def test_src_instabile_ritenta_e_poi_false(self):
        src, dst = self.paths('src/a.txt', 'dst/a.txt')
        write(src, 'parziale')
        sleeps = []

        def churn(_):
            sleeps.append(1)
            with open(src, 'ab') as f:               # il file cresce fra i due stat
                f.write(b'x')

        with mock.patch.object(copier.time, 'sleep', churn):
            self.assertFalse(copy_one(src, dst))
        self.assertEqual(len(sleeps), 1 + copier.STABILITY_RETRIES)   # 1 + 2 ritentativi
        self.assertFalse(os.path.exists(dst))          # niente copia a metà

    def test_src_cancellato_durante_la_copia_dst_invariato(self):
        src, dst = self.paths('src/a.txt', 'dst/a.txt')
        write(src, 'nuovo')
        write(dst, 'vecchio backup', mtime=1_000_000_000)

        def vanish(_):
            os.unlink(src)

        with mock.patch.object(copier.time, 'sleep', vanish):
            self.assertFalse(copy_one(src, dst))
        self.assertEqual(read(dst), 'vecchio backup')  # mai cancellare
        self.assertEqual(int(os.stat(dst).st_mtime), 1_000_000_000)

    def test_oserror_di_io_si_propaga(self):
        src, dst = self.paths('src/a.txt', 'dst/a.txt')
        write(src, 'x')
        with mock.patch.object(copier.shutil, 'copyfile',
                               side_effect=OSError(28, 'No space left on device')):
            with self.assertRaises(OSError) as cm:
                copy_one(src, dst)
        self.assertEqual(cm.exception.errno, 28)

    def test_symlink_ricreato_come_symlink(self):
        src, dst = self.paths('src/link', 'dst/link')
        os.makedirs(os.path.dirname(src), exist_ok=True)
        os.symlink('rel/target.txt', src)             # relativo, conservato
        self.assertTrue(copy_one(src, dst))
        self.assertTrue(os.path.islink(dst))
        self.assertEqual(os.readlink(dst), 'rel/target.txt')

    def test_symlink_rotto_ricreato_rotto(self):
        src, dst = self.paths('src/broken', 'dst/broken')
        os.makedirs(os.path.dirname(src), exist_ok=True)
        os.symlink('che/non/esiste', src)
        self.assertTrue(copy_one(src, dst))
        self.assertTrue(os.path.islink(dst))
        self.assertFalse(os.path.exists(dst))          # rotto anche a valle
        self.assertEqual(os.readlink(dst), 'che/non/esiste')

    def test_symlink_sovrascrive_un_file_esistente(self):
        src, dst = self.paths('src/link', 'dst/link')
        os.makedirs(os.path.dirname(src), exist_ok=True)
        os.symlink('t', src)
        write(dst, 'vecchio')
        self.assertTrue(copy_one(src, dst))
        self.assertTrue(os.path.islink(dst))


class NeedsCopyTest(CopierTestCase):
    def test_uguale_false_e_dst_mancante_true(self):
        src, dst = self.paths('src/a.txt', 'dst/a.txt')
        write(src, 'abc', mtime=1_000_000_000)
        self.assertTrue(needs_copy(src, dst))          # dst non esiste
        write(dst, 'abc', mtime=1_000_000_000)
        self.assertFalse(needs_copy(src, dst))

    def test_size_diverso_true(self):
        src, dst = self.paths('src/a.txt', 'dst/a.txt')
        write(src, 'abcde', mtime=1_000_000_000)
        write(dst, 'abc', mtime=1_000_000_000)
        self.assertTrue(needs_copy(src, dst))

    def test_mtime_entro_tolleranza_false(self):
        src, dst = self.paths('src/a.txt', 'dst/a.txt')
        write(src, 'abc', mtime=1_000_000_000)
        write(dst, 'abc', mtime=1_000_000_000 + 0.5)   # jitter da volume a bassa risoluzione
        self.assertFalse(needs_copy(src, dst))

    def test_mtime_fuori_tolleranza_true(self):
        src, dst = self.paths('src/a.txt', 'dst/a.txt')
        write(src, 'abc', mtime=2_000_000_000)
        write(dst, 'abc', mtime=1_000_000_000)
        self.assertTrue(needs_copy(src, dst))

    def test_src_assente_false(self):
        src, _ = self.paths('src/mai', 'dst/a.txt')
        self.assertFalse(needs_copy(src, os.path.join(self.tmp, 'dst/a.txt')))


class ReconcileTest(CopierTestCase):
    def setUp(self):
        super().setUp()
        self.src_root = os.path.join(self.tmp, 'src')
        self.dst_root = os.path.join(self.tmp, 'dst')
        write(os.path.join(self.src_root, 'a.txt'), 'A')
        write(os.path.join(self.src_root, 'sub/b.txt'), 'B')
        write(os.path.join(self.src_root, 'docs/c.md'), 'C')
        write(os.path.join(self.src_root, 'skipme.txt'), 'NO')
        write(os.path.join(self.src_root, 'node_modules/pkg/index.js'), 'NO')
        # allow-list (SPEC.md §4.3): ogni file da copiare va coperto da un include
        matcher = Matcher(BUILTIN_RULES, parse_rules(
            ['include: *.txt', 'include: *.md', 'exclude: skipme.txt']))
        self.kwargs = dict(source_root=self.src_root, dest_root=self.dst_root,
                           matcher=matcher, layout='relative', dest_path_fn=dest_path)

    def test_tre_copie_un_escluso_e_idempotente(self):
        self.assertEqual(reconcile_project(**self.kwargs), 3)
        for rel in ('a.txt', 'sub/b.txt', 'docs/c.md'):
            self.assertTrue(os.path.exists(os.path.join(self.dst_root, rel)), rel)
        self.assertFalse(os.path.exists(os.path.join(self.dst_root, 'skipme.txt')))
        self.assertFalse(os.path.exists(os.path.join(self.dst_root, 'node_modules')))
        self.assertEqual(reconcile_project(**self.kwargs), 0)      # seconda run: 0 copie

    def test_symlink_ricreati_come_symlink(self):
        os.symlink('a.txt', os.path.join(self.src_root, 'alias.txt'))     # file link
        os.symlink('docs', os.path.join(self.src_root, 'docslink'))       # dir link
        # 3 file + il file-link: `alias.txt` matcha `include: *.txt`
        self.assertEqual(reconcile_project(**self.kwargs), 4)
        self.assertEqual(os.readlink(os.path.join(self.dst_root, 'alias.txt')), 'a.txt')

    def test_dir_symlink_non_inclusa_non_e_copiata(self):
        # REGRESSIONE (copia-eccessiva): la dir-symlink è una FOGLIA, va
        # valutata come file. Con `evaluate(r, True)` il default allow-list
        # "nessun match → True" la copiava e creava l'intero scheletro di
        # cartelle figlie nella dest, ignorando ogni include.
        os.symlink('docs', os.path.join(self.src_root, 'buildlink'))
        self.assertEqual(reconcile_project(**self.kwargs), 3)
        self.assertFalse(os.path.lexists(os.path.join(self.dst_root, 'buildlink')))

    def test_dir_symlink_inclusa_viene_copiata_come_link(self):
        matcher = Matcher(BUILTIN_RULES,
                          parse_rules(['include: *.md', 'include: docslink']))
        os.symlink('docs', os.path.join(self.src_root, 'docslink'))
        self.assertEqual(reconcile_project(source_root=self.src_root,
                                           dest_root=self.dst_root,
                                           matcher=matcher, layout='relative',
                                           dest_path_fn=dest_path), 2)
        self.assertTrue(os.path.islink(os.path.join(self.dst_root, 'docslink')))

    def test_dest_dentro_src_bloccata_a_monte(self):
        # il validate a monte impedisce il loop: reconcile non ricece mai questo caso
        with self.assertRaises(ConfigError):
            validate_dests([self.src_root],
                           [os.path.join(self.src_root, 'backup')])


if __name__ == '__main__':
    unittest.main()
