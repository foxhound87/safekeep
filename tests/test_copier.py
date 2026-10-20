import os
import shutil
import tempfile
import unittest
from unittest import mock

from safekeep import copier
from safekeep.config import ConfigError, dest_path, validate_dests
from safekeep.copier import copy_one, needs_copy, prune_project, reconcile_project
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


class DestEscapeTest(CopierTestCase):
    """CR-01: mai scrivere fuori dalla dest via componente-symlink (SPEC.md §7.2)."""

    def test_dir_symlink_sorgente_diventata_dir_il_file_resta_nella_dest(self):
        ext = os.path.join(self.tmp, 'external')
        os.makedirs(ext)
        dst_root = os.path.join(self.tmp, 'dst')
        src_link = os.path.join(self.tmp, 'src/linkdir')
        os.makedirs(os.path.dirname(src_link))
        os.symlink(ext, src_link)
        # prima passata: la dir-symlink viene ricreata come link (SPEC.md §7.3)
        self.assertTrue(copy_one(src_link, os.path.join(dst_root, 'proj/linkdir'),
                                 dst_root))
        self.assertTrue(os.path.islink(os.path.join(dst_root, 'proj/linkdir')))
        # la sorgente diventa una directory reale: il figlio NON deve finire in ext
        os.unlink(src_link)
        os.makedirs(src_link)
        write(os.path.join(src_link, 'x.md'), 'contenuto')
        self.assertTrue(copy_one(os.path.join(src_link, 'x.md'),
                                 os.path.join(dst_root, 'proj/linkdir/x.md'),
                                 dst_root))
        dst_link = os.path.join(dst_root, 'proj/linkdir')
        self.assertFalse(os.path.islink(dst_link),
                         'il componente di dest viene ripristinato come dir reale')
        self.assertEqual(read(os.path.join(dst_link, 'x.md')), 'contenuto')
        self.assertEqual(os.listdir(ext), [], 'mai scritto fuori dest')

    def test_dst_costruita_fuori_dest_bloccata_e_niente_scrittura(self):
        src = self.paths('src/a.txt')[0]
        write(src, 'x')
        fuori = self.paths('fuori/a.txt')[0]
        self.assertFalse(copy_one(src, fuori, self.paths('dst')[0]))
        self.assertFalse(os.path.exists(fuori))
        self.assertFalse(os.path.exists(os.path.dirname(fuori)),
                         'niente makedirs fuori dalla dest')

    def test_copia_normale_con_dest_root_invariata(self):
        dst_root = self.paths('dst')[0]
        src, dst = self.paths('src/a.txt', 'dst/deep/a.txt')
        write(src, 'contenuto', mtime=1_000_000_000)
        self.assertTrue(copy_one(src, dst, dst_root))
        self.assertEqual(read(dst), 'contenuto')
        self.assertEqual(int(os.stat(dst).st_mtime), 1_000_000_000)


class ErroriPerFileTest(CopierTestCase):
    """CR-05: un errore I/O resta confinato al proprio file (SPEC.md §10.1)."""

    def setUp(self):
        super().setUp()
        self.src_root = os.path.join(self.tmp, 'src')
        self.dst_root = os.path.join(self.tmp, 'dst')
        write(os.path.join(self.src_root, 'a.txt'), 'A')
        write(os.path.join(self.src_root, 'sub/b.txt'), 'B')
        write(os.path.join(self.src_root, 'docs/c.md'), 'C')
        matcher = Matcher(BUILTIN_RULES,
                          parse_rules(['include: *.txt', 'include: *.md']))
        self.kwargs = dict(source_root=self.src_root, dest_root=self.dst_root,
                           matcher=matcher, layout='relative', dest_path_fn=dest_path)

    def test_errore_su_un_file_gli_altri_vengono_copiati(self):
        real = copy_one

        def boom(src, dst, dest_root=None):
            if os.path.basename(src) == 'b.txt':
                raise OSError(13, 'Permission denied')
            return real(src, dst, dest_root)

        stats = {}
        with mock.patch.object(copier, 'copy_one', new=boom):
            copied = reconcile_project(stats=stats, **self.kwargs)
        self.assertEqual(copied, 2)
        self.assertEqual(stats['errati'], 1)
        self.assertNotIn('dest_pendente', stats)
        self.assertTrue(os.path.exists(os.path.join(self.dst_root, 'a.txt')))
        self.assertTrue(os.path.exists(os.path.join(self.dst_root, 'docs/c.md')))
        self.assertFalse(os.path.exists(os.path.join(self.dst_root, 'sub/b.txt')))

    def test_errore_di_mount_dest_in_pending_e_walk_fermato(self):
        def boom(*args, **kwargs):
            raise OSError(28, 'No space left on device')

        stats = {}
        with mock.patch.object(copier, 'copy_one', new=boom):
            copied = reconcile_project(stats=stats, **self.kwargs)
        self.assertEqual(copied, 0)
        self.assertEqual(stats['errati'], 1)
        self.assertTrue(stats['dest_pendente'],
                        'ENOSPC → il caller mette la dest in pending (§8.3)')
        self.assertFalse(os.path.exists(self.dst_root),
                         'il walk si ferma: niente scritture a metà')

    def test_reconcile_ripetuti_stessi_errori_pochissime_righe(self):
        """SPEC.md §10.1 (0.5.1): 5 reconcile con gli stessi 3 file negati
        (EPERM) → 3 righe di log (una per (path, errno)), non 15. I conteggi
        in memoria restano intatti: 3 errori per passata."""
        copier._copy_errors.clear()
        self.addCleanup(copier._copy_errors.clear)

        def boom(*args, **kwargs):
            raise OSError(1, 'Operation not permitted')

        with mock.patch.object(copier, 'copy_one', new=boom), \
                mock.patch.object(copier.log, 'error') as errlog:
            for _ in range(5):
                stats = {}
                reconcile_project(stats=stats, **self.kwargs)
                self.assertEqual(stats['errati'], 3,
                                 'il conteggio non passa dal log')
        self.assertEqual(errlog.call_count, 3,
                         'una riga per (path, errno), le 4 passate successive '
                         'rientrano nell\'intervallo di throttle')


class PruneTest(CopierTestCase):
    """`sync-once --prune`: cancellazione solo certa e solo sotto il prefisso."""

    def setUp(self):
        super().setUp()
        self.src_root = os.path.join(self.tmp, 'src')
        self.proj = os.path.join(self.src_root, 'proj')
        self.dst_root = os.path.join(self.tmp, 'dst')
        write(os.path.join(self.proj, '.sync'), 'include: *.md\n')
        write(os.path.join(self.proj, 'keep.md'), 'k')
        write(os.path.join(self.proj, 'old.txt'), 'vecchio')
        # dest popolata da una passata precedente con regole diverse + residui
        write(os.path.join(self.dst_root, 'proj/keep.md'), 'k')
        write(os.path.join(self.dst_root, 'proj/old.txt'), 'vecchio')
        write(os.path.join(self.dst_root, 'proj/gone.md'), 'sorgente sparita')
        write(os.path.join(self.dst_root, 'proj/.sync'), 'include: *.md\n')
        write(os.path.join(self.dst_root, 'fuori.md'), 'fuori dal prefisso')
        # `.sync` è sempre incluso (SYNC_RULES, SPEC.md §4.1): qui va esplicitato
        matcher = Matcher(BUILTIN_RULES,
                          parse_rules(['include: *.md', 'include: .sync']))
        self.kwargs = dict(source_root=self.proj, dest_root=self.dst_root,
                           matcher=matcher, layout='relative', dest_path_fn=dest_path,
                           dest_base=self.src_root)

    def test_prune_rimuove_solo_gli_esclusi_con_sorgente_presente(self):
        self.assertEqual(prune_project(**self.kwargs), 1)
        self.assertFalse(os.path.exists(os.path.join(self.dst_root, 'proj/old.txt')))
        for keep in ('proj/keep.md', 'proj/gone.md', 'proj/.sync', 'fuori.md'):
            self.assertTrue(os.path.exists(os.path.join(self.dst_root, keep)), keep)

    def test_dry_run_conta_senza_rimuovere(self):
        self.assertEqual(prune_project(dry_run=True, **self.kwargs), 1)
        self.assertTrue(os.path.exists(os.path.join(self.dst_root, 'proj/old.txt')))


class ThrottleLogTest(CopierTestCase):
    """SPEC.md §10.1 (0.5.1): stessa (path, errno) → ≤1 riga ogni
    THROTTLE_INTERVAL con `— ripetuto N volte`, non una riga a fallimento."""

    def setUp(self):
        super().setUp()
        copier._copy_errors.clear()
        self.addCleanup(copier._copy_errors.clear)

    @staticmethod
    def err(no=1):
        return OSError(no, 'Operation not permitted')

    def test_50_failimenti_stesso_path_una_sola_riga(self):
        with self.assertLogs(LOGGER, 'ERROR') as cm:
            for _ in range(50):
                copier.log_copy_error('/s/a', '/d/a', self.err(), now=1000.0)
        self.assertEqual(len(cm.output), 1, cm.output)
        self.assertNotIn('ripetuto', cm.output[0], 'nessun suffisso sulla 1ª riga')

    def test_dopo_intervallo_una_riga_con_il_conteggio(self):
        now = 1000.0
        copier.log_copy_error('/s/a', '/d/a', self.err(), now=now)
        with mock.patch.object(copier.log, 'error') as errlog:   # 9 dentro la finestra
            for _ in range(9):
                copier.log_copy_error('/s/a', '/d/a', self.err(), now=now)
            errlog.assert_not_called()
        with self.assertLogs(LOGGER, 'ERROR') as cm:             # finestra scaduta
            copier.log_copy_error('/s/a', '/d/a', self.err(),
                                  now=now + copier.THROTTLE_INTERVAL + 1)
            for _ in range(5):                                   # poi di nuovo silenzio
                copier.log_copy_error('/s/a', '/d/a', self.err(),
                                      now=now + copier.THROTTLE_INTERVAL + 1)
        self.assertEqual(len(cm.output), 1, cm.output)
        self.assertIn('ripetuto 9 volte', cm.output[0])

    def test_chiavi_distinte_indipendenti(self):
        now = 1000.0
        with self.assertLogs(LOGGER, 'ERROR') as cm:
            copier.log_copy_error('/s/a', '/d/a', self.err(1), now=now)
            copier.log_copy_error('/s/a', '/d/a', self.err(1), now=now)
            copier.log_copy_error('/s/b', '/d/b', self.err(1), now=now)
            copier.log_copy_error('/s/a', '/d/a', self.err(13), now=now)
        self.assertEqual(len(cm.output), 3, cm.output)      # (a,1) una + b + (a,13)


if __name__ == '__main__':
    unittest.main()
