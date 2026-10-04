import errno
import os
import re
import shutil
import sys
import tempfile
import time
import unittest
from unittest import mock

from safekeep import copier
from safekeep.config import ConfigError, dest_path, parse_config
from safekeep.copier import reconcile_project
from safekeep.daemon import (
    FLOOD_LIMIT,
    RELOAD_SYNC,
    SKIP,
    SYNC_FILE,
    WALK_DIR,
    Daemon,
    Project,
    classify,
    dedup,
    dedup_flush,
    dedup_roots,
    exclude_regexes,
    find_project,
    load_project,
    normalize,
    parse_events,
    plan_batch,
)
from safekeep.matcher import BUILTIN_RULES, Matcher, parse_rules


def write(path, text=''):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'w') as f:
        f.write(text)
    return path


class TmpTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def path(self, *parts):
        return os.path.join(self.tmp, *parts)


class ParseEventsTest(unittest.TestCase):
    def test_path_con_spazi_e_unicode(self):
        data = '/tmp/dir con spazi/файл н우미.md\0/tmp/altro\0'.encode()
        paths, residuo = parse_events(data)
        self.assertEqual(paths, ['/tmp/dir con spazi/файл н우미.md', '/tmp/altro'])
        self.assertEqual(residuo, b'')

    def test_batch_multipli_e_residuo_vuoto(self):
        paths, residuo = parse_events(b'/a\0/b\0/c\0')
        self.assertEqual(paths, ['/a', '/b', '/c'])
        self.assertEqual(residuo, b'')

    def test_chunk_spezzato_a_meta_path(self):
        data = '/tmp/primo.md\0/tmp/héllo wörld.md\0'.encode()
        cut = len(data) - 15                      # a metà del secondo path
        paths, residuo = parse_events(data[:cut])
        self.assertEqual(paths, ['/tmp/primo.md'])
        self.assertIsInstance(residuo, bytes)
        paths, residuo = parse_events(data[cut:], residuo)
        self.assertEqual(paths, ['/tmp/héllo wörld.md'])
        self.assertEqual(residuo, b'')

    def test_chunk_spezzato_in_un_carattere_utf8_multibyte(self):
        data = '/tmp/файл.md\0'.encode()
        cut = data.index(b'\xd1') + 1             # nel mezzo di 'ф' (2 byte)
        paths, residuo = parse_events(data[:cut])
        self.assertEqual(paths, [])
        paths, residuo = parse_events(data[cut:], residuo)
        self.assertEqual(paths, ['/tmp/файл.md'])


class DedupTest(unittest.TestCase):
    def test_finestra_temporale(self):
        state = {}
        self.assertEqual(dedup(state, ['/a', '/b'], now=100.0), ['/a', '/b'])
        self.assertEqual(dedup(state, ['/a'], now=101.0), [])      # dentro i 2s
        self.assertEqual(dedup(state, ['/a'], now=102.9), [])      # last=101.0 → 1.9s
        self.assertEqual(dedup(state, ['/a'], now=104.9), ['/a'])  # 2.0s ≥ finestra
        # l'ultima occorrenza (104.9) estende la finestra
        self.assertEqual(dedup(state, ['/a'], now=105.5), [])
        self.assertEqual(dedup(state, ['/a'], now=107.4), [])      # 1.9s da 105.5
        self.assertEqual(dedup(state, ['/a'], now=109.5), ['/a'])  # 2.1s da 107.4

    def test_pulizia_delle_entry_scadute(self):
        state = {}
        dedup(state, ['/vecchio'], now=1.0)
        dedup(state, [], now=100.0)
        self.assertNotIn('/vecchio', state, 'le entry scadute vengono potate')

    def test_default_usa_il_tempo_reale(self):
        state = {}
        self.assertEqual(dedup(state, ['/x']), ['/x'])   # now=None → monotonic


class DedupScadenzaTest(unittest.TestCase):
    """CR-04: una soppressione NON perde l'evento — emissione di coda (trailing edge)."""

    def test_scadenza_fissa_non_estesa_dagli_arrivi_successivi(self):
        state = {}
        self.assertEqual(dedup(state, ['/a'], now=100.0), ['/a'])   # emesso
        self.assertEqual(dedup(state, ['/a'], now=101.0), [])       # scadenza 102.0
        self.assertEqual(dedup(state, ['/a'], now=101.5), [])       # non la estende
        self.assertEqual(dedup_flush(state, now=101.9), [])         # non ancora scaduta
        self.assertEqual(dedup_flush(state, now=102.0), ['/a'])     # emesso comunque
        self.assertEqual(state['/a'], (101.5, None))
        self.assertEqual(dedup_flush(state, now=103.0), [])         # una sola volta


class DedupLoopTest(TmpTestCase):
    """CR-04: la scadenza del dedup entra nel timeout del `select` → senza nuovi
    eventi l'ultima modifica viene comunque copiata (niente thread)."""

    def test_step_sveglia_alla_scadenza_e_copia(self):
        src = self.path('src')
        proj = os.path.join(src, 'proj')
        dest = self.path('dst')
        os.makedirs(dest)                       # dest esiste ⇒ dest_state == ok
        write(os.path.join(proj, '.sync'), 'include: *.md\n')
        target = write(os.path.join(proj, 'note.md'), 'v1')
        cfg_path = write(self.path('safekeep.cfg'), f'source: {src}\ndest: {dest}\n')
        clock = [100.0]
        daemon = Daemon(cfg_path,
                        argv=[sys.executable, '-c', 'import time; time.sleep(60)'],
                        clock=lambda: clock[0])
        daemon.load_config()
        daemon.load_projects()
        self.assertTrue(daemon.spawn())
        self.addCleanup(daemon.close_proc)
        dst = os.path.join(dest, 'proj/note.md')

        daemon.run_batch([target])                       # t=100 → copia v1
        with open(dst) as fh:
            self.assertEqual(fh.read(), 'v1')

        write(target, 'v2-piu-lungo')                    # size diversa → needs_copy
        clock[0] = 101.0
        daemon.run_batch([target])                       # soppresso (finestra 2s)
        with open(dst) as fh:
            self.assertEqual(fh.read(), 'v1', 'evento soppresso ma non perso')
        self.assertEqual(daemon.next_deadline(), 1.0,    # scadenza 102.0 nel select
                         daemon.seen)

        clock[0] = 102.0
        daemon.step(0.05)                                # sveglia → flush → copia
        with open(dst) as fh:
            self.assertEqual(fh.read(), 'v2-piu-lungo')


class SyncMalevoloTest(TmpTestCase):
    """CR-03: `.sync` non attendibile → warning + skip (info) o ConfigError (debug)."""

    def cfg(self, level='info'):
        return parse_config(f'dest: {self.path("dst")}\nlog_level: {level}\n')

    def project_dir(self):
        proj = self.path('src', 'proj')
        os.makedirs(proj, exist_ok=True)
        return proj

    def test_regex_invalida_info_salta_la_riga_senza_crash(self):
        proj = self.project_dir()
        write(os.path.join(proj, '.sync'), '[z-a]*\n')
        project = load_project(proj, self.cfg())         # nessuna eccezione
        self.assertIsNotNone(project)
        self.assertNotIn('[z-a]*', [r.pattern for r in project.matcher.rules])

    def test_regex_invalida_debug_e_fatale(self):
        proj = self.project_dir()
        write(os.path.join(proj, '.sync'), '[z-a]*\n')
        with self.assertRaises(ConfigError):
            load_project(proj, self.cfg('debug'))

    def test_sync_non_utf8_info_salta_il_progetto(self):
        proj = self.project_dir()
        with open(os.path.join(proj, '.sync'), 'wb') as fh:
            fh.write(b'\xff\xfe include: *.md\n')
        self.assertIsNone(load_project(proj, self.cfg()))
        with self.assertRaises(ConfigError):
            load_project(proj, self.cfg('debug'))

    def test_nul_nel_pattern_warning_e_riga_saltata(self):
        proj = self.project_dir()
        write(os.path.join(proj, '.sync'), 'include: a\x00b\n')
        project = load_project(proj, self.cfg())
        self.assertIsNotNone(project)
        self.assertNotIn('a\x00b', [r.pattern for r in project.matcher.rules])
        with self.assertRaises(ConfigError):
            load_project(proj, self.cfg('debug'))


class ReconcilePendingTest(TmpTestCase):
    """CR-05: errore di mount → dest in pending (§8.3); altri errori → solo log."""

    def build(self):
        src = self.path('src')
        proj = os.path.join(src, 'proj')
        dest = self.path('dst')
        os.makedirs(dest)
        write(os.path.join(proj, '.sync'), 'include: *.md\n')
        write(os.path.join(proj, 'a.md'), 'x')
        cfg_path = write(self.path('safekeep.cfg'), f'source: {src}\ndest: {dest}\n')
        daemon = Daemon(cfg_path)
        daemon.load_config()
        daemon.load_projects()
        return daemon, dest

    def test_errore_di_mount_mette_la_dest_in_pending(self):
        daemon, dest = self.build()

        def boom(*args, **kwargs):
            raise OSError(errno.ENOSPC, 'No space left on device')

        with mock.patch.object(copier, 'copy_one', new=boom):
            daemon.reconcile(daemon.projects[0])
        self.assertIn(dest, daemon.pending)

    def test_eacces_non_mette_la_dest_in_pending(self):
        daemon, dest = self.build()

        def boom(*args, **kwargs):
            raise OSError(errno.EACCES, 'Permission denied')

        with mock.patch.object(copier, 'copy_one', new=boom):
            daemon.reconcile(daemon.projects[0])
        self.assertNotIn(dest, daemon.pending,
                         'un errore su un file non blocca la dest intera')
        self.assertFalse(os.path.exists(os.path.join(dest, 'proj/a.md')))

    def test_backoff_non_resetta_a_1s_dopo_un_nuovo_fallimento(self):
        # REGRESSIONE (CR-05): mount ok ma copia fallita di nuovo → se il
        # retry ripartisse da 1s il daemon looperebbe a 1Hz fino al prossimo errore
        daemon, dest = self.build()
        project = daemon.projects[0]
        daemon.mark_pending(project, dest)
        entry = daemon.pending[dest]
        for _ in range(6):
            next(entry['sched'])                # 1,2,4,8,16,32 già consumati
        entry['next'] = daemon.clock()          # scaduto adesso

        def boom(*args, **kwargs):
            raise OSError(errno.ENODEV, 'No such device')

        with mock.patch.object(copier, 'copy_one', new=boom):
            daemon.check_timers()
        retry = daemon.pending[dest]
        self.assertGreater(retry['next'] - daemon.clock(), 1,
                           'il backoff riprende da dove era, non da 1s')


class FindProjectTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.outer_root = os.path.join(self.tmp, 'src', 'proj')
        self.inner_root = os.path.join(self.outer_root, 'sub')
        os.makedirs(self.inner_root)
        self.outer = Project(self.outer_root, 'outer', Matcher(BUILTIN_RULES))
        self.inner = Project(self.inner_root, 'inner', Matcher(BUILTIN_RULES))
        self.projects = [self.outer, self.inner]

    def test_progetto_annidato_piu_profondo(self):
        path = os.path.join(self.inner_root, 'a.md')
        self.assertIs(find_project(path, self.projects), self.inner)
        self.assertIs(find_project(path, [self.inner, self.outer]), self.inner)

    def test_file_nella_root_esterna(self):
        path = os.path.join(self.outer_root, 'b.md')
        self.assertIs(find_project(path, self.projects), self.outer)

    def test_path_fuori_dalle_source(self):
        path = os.path.join(self.tmp, 'altro', 'x.md')
        self.assertIsNone(find_project(path, self.projects))
        self.assertIsNone(find_project('/percorso/assoluta/x.md', self.projects))

    def test_root_stessa_del_progetto(self):
        self.assertIs(find_project(self.outer_root, self.projects), self.outer)

    def test_normalizza_il_path(self):
        messy = os.path.join(self.outer_root, 'sub', '..', 'b.md')
        self.assertIs(find_project(messy, self.projects), self.outer)


class ClassifyTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.root = os.path.join(self.tmp, 'proj')
        os.makedirs(os.path.join(self.root, 'docs'))
        os.makedirs(os.path.join(self.root, 'skipme'))
        write(os.path.join(self.root, '.sync'),
              'name: pippo\ninclude: *.md\nexclude: skipme/\nexclude: *.txt\n')
        write(os.path.join(self.root, 'a.md'))
        write(os.path.join(self.root, 'b.txt'))
        self.project = load_project(self.root, parse_config('dest: /d\n'))

    def test_file_incluso_sync(self):
        self.assertEqual(classify(os.path.join(self.root, 'a.md'), self.project),
                         SYNC_FILE)

    def test_file_escluso_skip(self):
        self.assertEqual(classify(os.path.join(self.root, 'b.txt'), self.project),
                         SKIP)

    def test_sync_reload(self):
        self.assertEqual(classify(os.path.join(self.root, '.sync'), self.project),
                         RELOAD_SYNC)

    def test_sync_remosso_reload(self):
        os.unlink(os.path.join(self.root, '.sync'))
        self.assertEqual(classify(os.path.join(self.root, '.sync'), self.project),
                         RELOAD_SYNC)

    def test_sync_sempre_incluso_nonostante_lallow_list(self):
        # SPEC §4.1: .sync va sempre copiato (regola implicita, non serve include)
        self.assertTrue(self.project.matcher.evaluate('.sync', False))
        self.assertFalse(self.project.matcher.evaluate('.env', False))  # invece no

    def test_directory_inclusa_walk(self):
        self.assertEqual(classify(os.path.join(self.root, 'docs'), self.project),
                         WALK_DIR)

    def test_directory_esclusa_skip(self):
        self.assertEqual(classify(os.path.join(self.root, 'skipme'), self.project),
                         SKIP)

    def test_path_sparito_skip(self):
        self.assertEqual(classify(os.path.join(self.root, 'gone.md'), self.project),
                         SKIP)

    def test_senza_progetto_skip(self):
        self.assertEqual(classify(os.path.join(self.root, 'a.md'), None), SKIP)


class ExcludeRegexesTest(unittest.TestCase):
    def setUp(self):
        self.rules = parse_rules([
            'exclude: node_modules/',
            'include: *.md',
            'exclude: *.swp',
            'exclude: /radice/',
            'exclude: docs/vendor/',
        ], origin='t')
        self.rxs = exclude_regexes(self.rules)

    def test_converte_solo_le_exclude_non_ancorate(self):
        # /radice/ e docs/vendor/ (ancorati) non sono traducibili senza la root
        self.assertEqual(len(self.rxs), 2)

    def test_matcha_il_segmento_node_modules(self):
        self.assertTrue(any(re.search(rx, '/node_modules/x') for rx in self.rxs))
        self.assertTrue(any(re.search(rx, '/a/b/node_modules/c/d') for rx in self.rxs))

    def test_non_matcha_un_prefisso_di_segmento(self):
        self.assertFalse(any(re.search(rx, '/src/node_modules_bak/x')
                             for rx in self.rxs))

    def test_dir_only_non_matcha_un_file_omonimo(self):
        # il FILE chiamato node_modules resta visibile al matcher Python
        rx = next(rx for rx in self.rxs if 'node_modules' in rx)
        self.assertIsNone(re.search(rx, '/src/node_modules'))
        self.assertIsNotNone(re.search(rx, '/src/node_modules/qualcosa'))

    def test_basename_swp(self):
        rx = next(rx for rx in self.rxs if 'swp' in rx)
        self.assertIsNotNone(re.search(rx, '/qualche/dir/file.swp'))
        self.assertIsNone(re.search(rx, '/qualche/dir/file.swp2'))


class PlanBatchTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.root = os.path.join(self.tmp, 'proj')
        os.makedirs(self.root)
        self.project = Project(self.root, 'p', Matcher(BUILTIN_RULES))

    def test_anti_flood_5001_path_collapse_a_reconcile(self):
        paths = [os.path.join(self.root, f'f{i}.md') for i in range(FLOOD_LIMIT + 1)]
        reloads, plan = plan_batch(paths, [self.project])
        self.assertEqual(reloads, [])
        self.assertEqual(plan[self.project], 'reconcile')

    def test_sotto_la_soglia_restano_le_azioni(self):
        paths = [os.path.join(self.root, f'f{i}.md') for i in range(FLOOD_LIMIT)]
        _, plan = plan_batch(paths, [self.project])
        self.assertIsInstance(plan[self.project], list)
        self.assertEqual(len(plan[self.project]), FLOOD_LIMIT)

    def test_sync_sconosciuto_va_nei_reloads(self):
        orphan = os.path.join(self.tmp, 'newproj', '.sync')
        write(orphan, '')
        reloads, plan = plan_batch([orphan], [self.project])
        self.assertEqual(reloads, [normalize(orphan)])
        self.assertEqual(plan, {})

    def test_sync_di_un_progetto_noto_resta_nelle_azioni(self):
        sync = os.path.join(self.root, '.sync')
        write(sync, '')
        reloads, plan = plan_batch([sync], [self.project])
        self.assertEqual(reloads, [])
        self.assertEqual(plan[self.project], [(normalize(sync), RELOAD_SYNC)])


class FswatchArgvTest(unittest.TestCase):
    def _daemon(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        src = os.path.join(tmp, 'src')
        os.makedirs(os.path.join(src, 'proj'))
        write(os.path.join(src, 'proj', '.sync'), 'exclude: skipme/\n')
        cfg_path = os.path.join(tmp, 'safekeep.cfg')
        write(cfg_path, f'source: {src}\ndest: {tmp}/dst\n')
        daemon = Daemon(cfg_path)
        daemon.load_config()
        daemon.load_projects()
        return daemon, src

    def test_argv_esatto_dallo_spec(self):
        daemon, src = self._daemon()
        argv = daemon.fswatch_argv()
        # posizioni fisse: solo il monitor (-m) è per-piattaforma (SPEC §14.2)
        self.assertEqual(argv[:3], ['fswatch', '-0', '-m'])
        self.assertIn(argv[3], ('fsevents_monitor', 'inotify_monitor'))
        self.assertEqual(argv[4:7], ['-r', '-l', '1.0'])
        regexes = [argv[i + 1] for i, a in enumerate(argv) if a == '-e']
        self.assertTrue(regexes)
        self.assertTrue(any(re.search(rx, '/qualcuno/node_modules/x') for rx in regexes))
        self.assertTrue(any('skipme/' in rx for rx in regexes))
        self.assertEqual(argv[argv.index('--') + 1:], [src])

    def test_monitor_dichiarato_per_piattaforma(self):
        # SPEC §14.1/§14.2: il monitor è esplicito su entrambi i rami, letto
        # a chiamata — così entrambi si testano anche girando su macOS
        daemon, _ = self._daemon()
        with mock.patch('sys.platform', 'darwin'):
            self.assertEqual(daemon.fswatch_argv()[3], 'fsevents_monitor')
        with mock.patch('sys.platform', 'linux'):
            self.assertEqual(daemon.fswatch_argv()[3], 'inotify_monitor')


class WalkDirTest(TmpTestCase):
    """Mini-walk su una dir appena creata: filtro allow-list sui file figli."""

    def test_copia_solo_i_file_inclusi(self):
        src = self.path('src')
        proj = os.path.join(src, 'proj')
        dest = self.path('dst')
        os.makedirs(dest)                       # dest esiste ⇒ dest_state == ok
        write(os.path.join(proj, '.sync'), 'include: *.md\n')
        write(os.path.join(proj, 'docs/note.md'), 'n')
        write(os.path.join(proj, 'docs/raw.txt'), 'x')
        cfg_path = self.path('safekeep.cfg')
        write(cfg_path, f'source: {src}\ndest: {dest}\n')
        daemon = Daemon(cfg_path)
        daemon.load_config()
        project = load_project(proj, daemon.cfg)
        self.assertIsNotNone(project)
        daemon.walk_dir(os.path.join(proj, 'docs'), project)
        # layout relative: i path dest sono relativi alla radice `source`
        # (SPEC.md §4.3) → il segmento del progetto resta nel path dest
        self.assertTrue(os.path.exists(os.path.join(dest, 'proj/docs/note.md')))
        self.assertFalse(os.path.exists(os.path.join(dest, 'docs/note.md')))
        self.assertFalse(os.path.exists(os.path.join(dest, 'proj/docs/raw.txt')))

    def test_dir_symlink_non_inclusa_non_e_copiata(self):
        # stessa foglia di reconcile_project: senza include la dir-symlink
        # non va copiata (regressione: creava lo scheletro di cartelle in dest)
        src = self.path('src2')
        proj = os.path.join(src, 'proj')
        dest = self.path('dst2')
        os.makedirs(dest)
        write(os.path.join(proj, '.sync'), 'include: *.md\n')
        write(os.path.join(proj, 'docs/note.md'), 'n')
        os.makedirs(os.path.join(proj, 'build'))
        os.symlink(os.path.join(proj, 'docs'),
                   os.path.join(proj, 'build/docslink'))
        cfg_path = write(self.path('safekeep2.cfg'), f'source: {src}\ndest: {dest}\n')
        daemon = Daemon(cfg_path)
        daemon.load_config()
        project = load_project(proj, daemon.cfg)
        daemon.walk_dir(proj, project)
        self.assertTrue(os.path.exists(os.path.join(dest, 'proj/docs/note.md')))
        self.assertFalse(os.path.lexists(os.path.join(dest, 'proj/build/docslink')))


class RigheNudeRegressionTest(TmpTestCase):
    """REGRESSIONE: `.sync` con righe nude stile gitignore (la configurazione
    reale dell'utente). Prima del fix tutte e quattro le righe venivano scartate
    → allow-list vuota → la dest si riempiva comunque dello scheletro delle
    cartelle figlie di ogni dir-symlink dell'albero (niente file richiesti)."""

    SYNC_TEXT = '.env\n.env.*\n.vault\n*.md\n'

    def setUp(self):
        super().setUp()
        self.src = self.path('src')
        self.proj = os.path.join(self.src, 'projects')
        self.dest = self.path('dst')
        os.makedirs(self.dest)
        write(os.path.join(self.proj, '.sync'), self.SYNC_TEXT)
        write(os.path.join(self.proj, '.env'), 'S=1')
        write(os.path.join(self.proj, '.env.local'), 'S=2')
        write(os.path.join(self.proj, '.vault/note.txt'), 'vault')
        write(os.path.join(self.proj, 'alpha/README.md'), '# a')
        write(os.path.join(self.proj, 'beta/src/main.py'), 'print(1)')
        write(os.path.join(self.proj, 'beta/src/data.json'), '{}')
        os.makedirs(os.path.join(self.proj, 'beta/build/x'))
        os.symlink(os.path.join(self.proj, 'beta/src'),
                   os.path.join(self.proj, 'beta/build/srclink'))
        cfg = parse_config(f'dest: {self.dest}\n')
        self.project = load_project(self.proj, cfg)
        self.assertIsNotNone(self.project)
        self.cfg = cfg

    def plan(self):
        stats = {}
        reconcile_project(self.project.root, self.dest, self.project.matcher,
                          self.cfg.layout, dest_path, stats=stats,
                          dry_run=True, dest_base=self.project.source_root)
        return sorted(os.path.relpath(s, self.project.root)
                      for s, _ in stats['pianificati'])

    def test_pianifica_solo_i_file_richiesti_e_zero_codice(self):
        self.assertEqual(self.plan(),
                         ['.env', '.env.local', '.sync',
                          '.vault/note.txt', 'alpha/README.md'])

    def test_copia_reale_niente_codice_ne_scheletro_build(self):
        reconcile_project(self.project.root, self.dest, self.project.matcher,
                          self.cfg.layout, dest_path,
                          dest_base=self.project.source_root)
        copied = sorted(os.path.relpath(os.path.join(dp, f), self.dest)
                        for dp, _, fs in os.walk(self.dest) for f in fs)
        self.assertEqual(copied,
                         ['.env', '.env.local', '.sync',
                          '.vault/note.txt', 'alpha/README.md'])
        self.assertFalse(os.path.lexists(os.path.join(self.dest, 'beta/build/srclink')))


class DestLayoutTest(TmpTestCase):
    """Layout `relative`: il path dest è relativo alla radice `source` che
    contiene il progetto (SPEC.md §4.3, examples/safekeep.example) — due
    progetti con lo stesso file relativo NON si sovrappongono."""

    def _proj(self, root):
        write(os.path.join(root, '.sync'), 'include: *.md\n')
        write(os.path.join(root, 'docs/note.md'), 'x')

    def _daemon(self, dest, *config_lines):
        os.makedirs(dest, exist_ok=True)          # dest esiste ⇒ dest_state ok
        cfg_path = write(self.path('safekeep.cfg'),
                         '\n'.join(config_lines) + '\n')
        daemon = Daemon(cfg_path)
        daemon.load_config()
        daemon.load_projects()
        return daemon

    def test_due_progetti_stesso_file_dest_distinte(self):
        src, dest = self.path('src'), self.path('dst')
        self._proj(os.path.join(src, 'projA'))
        self._proj(os.path.join(src, 'projB'))
        daemon = self._daemon(dest, f'source: {src}', f'dest: {dest}')
        daemon.sync_once()
        self.assertTrue(os.path.exists(os.path.join(dest, 'projA/docs/note.md')))
        self.assertTrue(os.path.exists(os.path.join(dest, 'projB/docs/note.md')))
        self.assertFalse(os.path.exists(os.path.join(dest, 'docs/note.md')),
                         'senza il prefisso della radice i due progetti collidono')

    def test_caso_myapp_della_documentazione(self):
        # ~/Code/myapp/docs/a.md con source ~/Code → backup/myapp/docs/a.md
        code, dest = self.path('Code'), self.path('backup')
        self._proj(os.path.join(code, 'myapp'))
        daemon = self._daemon(dest, f'source: {code}', f'dest: {dest}')
        daemon.sync_once()
        self.assertTrue(os.path.exists(os.path.join(dest, 'myapp/docs/note.md')))

    def test_layout_full_invariato(self):
        code, full = self.path('Code'), self.path('full')
        myapp = os.path.join(code, 'myapp')
        self._proj(myapp)
        daemon = self._daemon(full, f'source: {code}', f'dest: {full}',
                              'layout: full')
        daemon.sync_once()
        expected = os.path.join(full, os.path.relpath(
            os.path.join(os.path.realpath(myapp), 'docs', 'note.md'), os.sep))
        self.assertTrue(os.path.exists(expected), expected)


FAKE_FSWATCH = """\
import sys, time
with open(sys.argv[1]) as fh:
    paths = [line.rstrip('\\n') for line in fh if line.strip()]
sys.stdout.write('\\0'.join(paths) + '\\0')
sys.stdout.flush()
time.sleep(60)
"""


class SmokeLoopTest(unittest.TestCase):
    """Smoke con fswatch finto: il loop copia i path degli eventi."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def test_eventi_reale_vengono_processati(self):
        src_proj = os.path.join(self.tmp, 'src', 'proj')
        dest = os.path.join(self.tmp, 'dst')
        os.makedirs(dest)               # la dest esiste ⇒ dest_state == ok
        write(os.path.join(src_proj, '.sync'), 'include: *.md\nexclude: *.txt\n')
        write(os.path.join(src_proj, 'b.md'), 'b')
        write(os.path.join(src_proj, 'c.txt'), 'c')
        cfg_path = os.path.join(self.tmp, 'safekeep.cfg')
        write(cfg_path, f'source: {self.tmp}/src\ndest: {dest}\nlayout: relative\n')
        events = write(os.path.join(self.tmp, 'events.txt'),
                       f'{src_proj}/b.md\n{src_proj}/c.txt\n')
        script = write(os.path.join(self.tmp, 'fake_fswatch.py'), FAKE_FSWATCH)

        daemon = Daemon(cfg_path, argv=[sys.executable, '-u', script, events])
        daemon.load_config()
        daemon.load_projects()
        self.assertTrue(daemon.spawn())
        self.addCleanup(daemon.close_proc)

        # b.md NON c'è ancora sul dest: nessun reconcile è stato eseguito,
        # quindi la presenza proviene SOLO dall'evento fswatch
        deadline = time.time() + 10
        copied = False
        # layout relative: path dest relativi alla radice source (SPEC.md §4.3)
        while time.time() < deadline and not copied:
            daemon.step(0.1)
            copied = os.path.exists(os.path.join(dest, 'proj', 'b.md'))
        self.assertTrue(copied, 'levento su b.md deve partire la copia')
        # c.txt è escluso dal matcher Python: il filtro resta lì
        self.assertFalse(os.path.exists(os.path.join(dest, 'proj', 'c.txt')))

    def test_morte_fswatch_respawn_e_fatalita(self):
        script = write(os.path.join(self.tmp, 'dying.py'),
                       'import sys\nsys.exit(1)\n')
        cfg_path = os.path.join(self.tmp, 'safekeep.cfg')
        write(cfg_path, f'source: {self.tmp}/src\ndest: {self.tmp}/dst\n')
        os.makedirs(os.path.join(self.tmp, 'src'))
        clock = [0.0]
        daemon = Daemon(cfg_path, argv=[sys.executable, script],
                        clock=lambda: clock[0])
        daemon.load_config()
        daemon.load_projects()
        for _ in range(30):
            if daemon.fatal:
                break
            # salta il backoff respawn: ogni morte parte con il clock a spawn_at
            if daemon.proc is None:
                clock[0] = max(clock[0], daemon.spawn_at)
            daemon.step(timeout=0.05)
        self.assertTrue(daemon.fatal, '5 morti in 60s devono essere fatali')
        self.assertIsNone(daemon.proc)


class DedupRootsTest(unittest.TestCase):
    """Radici watch annidate → saltate (fswatch -r copre già il sottoalbero)."""

    def test_radice_annidata_sotto_unaltra_salta(self):
        self.assertEqual(dedup_roots(['/a/b/c', '/a/b']), ['/a/b'])
        self.assertEqual(dedup_roots(['/a/b', '/a/b/c']), ['/a/b'])

    def test_prefisso_letterale_non_basta(self):
        self.assertEqual(dedup_roots(['/a', '/ab']), ['/a', '/ab'])

    def test_stessa_radice_due_volte(self):
        self.assertEqual(dedup_roots(['/a', '/a']), ['/a'])


class HomeTestCase(TmpTestCase):
    """$HOME finto: auto-discovery e watch root leggono `expanduser('~')`."""

    def setUp(self):
        super().setUp()
        old = os.environ.get('HOME')

        def restore():
            if old is None:
                os.environ.pop('HOME', None)
            else:
                os.environ['HOME'] = old

        self.addCleanup(restore)
        self.home = self.path('home')
        os.makedirs(self.home)
        self.dest = self.path('dst')
        os.makedirs(self.dest)              # dest esiste ⇒ dest_state == ok
        os.environ['HOME'] = self.home

    def daemon(self):
        cfg_path = write(self.path('safekeep.cfg'), f'dest: {self.dest}\n')
        daemon = Daemon(cfg_path)
        daemon.load_config()
        daemon.load_projects()
        return daemon


class AutoDiscoveryTest(HomeTestCase):
    """Modalità auto-discovery: nessuna `source:` → watch root = $HOME (SPEC §6)."""

    def test_watch_root_e_exclude_dedicate(self):
        daemon = self.daemon()
        roots, auto = daemon.discovery_roots()
        self.assertTrue(auto, 'nessuna source ⇒ modalità auto-discovery')
        self.assertEqual(roots, [self.home])
        argv = daemon.fswatch_argv()
        self.assertEqual(argv[argv.index('--') + 1:], [self.home])
        regexes = [argv[i + 1] for i, a in enumerate(argv) if a == '-e']
        for path in ('/u/Library/Notes/x', '/u/.Trash/x', '/u/.cache/x',
                     '/u/.local/state/safekeep/safekeep.log',
                     '/u/Desktop/a.md', '/u/Documents/a.md', '/u/Downloads/a.md',
                     '/u/Movies/a.md', '/u/Music/a.md', '/u/Pictures/a.md',
                     '/u/Public/a.md'):
            self.assertTrue(any(re.search(rx, path) for rx in regexes),
                            f'exclude mancante per {path}')

    def test_pruning_library_e_dir_nascoste(self):
        write(os.path.join(self.home, 'Library/.sync'), 'include: *.md\n')
        write(os.path.join(self.home, '.hidden/.sync'), 'include: *.md\n')
        write(os.path.join(self.home, 'node_modules/x/.sync'), 'include: *.md\n')
        self.assertEqual(self.daemon().projects, [])

    def test_progetto_sotto_home_scoperto(self):
        write(os.path.join(self.home, 'Code/myapp/.sync'), 'include: *.md\n')
        write(os.path.join(self.home, 'Code/myapp/sub/.sync'), 'include: *.md\n')
        projects = self.daemon().projects
        self.assertEqual([p.root for p in projects],
                         [os.path.realpath(os.path.join(self.home, 'Code/myapp')),
                          os.path.realpath(os.path.join(self.home, 'Code/myapp/sub'))])

    def test_evento_su_sync_nuovo_progetto_scopra_e_reconcilia(self):
        daemon = self.daemon()
        self.assertEqual(daemon.projects, [])
        sync = write(os.path.join(self.home, 'newproj/.sync'), 'include: *.md\n')
        write(os.path.join(self.home, 'newproj/docs/note.md'), 'x')
        daemon.run_batch([sync])            # path fuori da ogni progetto noto
        self.assertEqual([p.root for p in daemon.projects],
                         [os.path.realpath(os.path.join(self.home, 'newproj'))])
        # layout relative con radice $HOME: il segmento resta nel path dest
        self.assertTrue(os.path.exists(os.path.join(self.dest, 'newproj/docs/note.md')))

    def test_rescan_discovery_al_timer_24h(self):
        daemon = self.daemon()
        self.assertEqual(daemon.projects, [])
        write(os.path.join(self.home, 'late/.sync'), 'include: *.md\n')
        write(os.path.join(self.home, 'late/a.md'), 'x')
        daemon.next_reconcile = daemon.clock() - 1        # ciclo 24h scaduto
        daemon.check_timers()
        self.assertEqual(len(daemon.projects), 1, 'il rescan recupera il .sync perso')
        self.assertTrue(os.path.exists(os.path.join(self.dest, 'late/a.md')))


class WatchRootsSourceModeTest(TmpTestCase):
    """Backward compat: con `source:` presenti le watch roots sono le source."""

    def daemon(self, *sources):
        dest = self.path('dst')
        os.makedirs(dest, exist_ok=True)
        cfg_path = write(self.path('safekeep.cfg'),
                         ''.join(f'source: {s}\n' for s in sources)
                         + f'dest: {dest}\n')
        daemon = Daemon(cfg_path)
        daemon.load_config()
        daemon.load_projects()
        return daemon

    def test_source_mode_invariata(self):
        src = self.path('src')
        write(os.path.join(src, 'proj/.sync'), 'include: *.md\n')
        daemon = self.daemon(src)
        roots, auto = daemon.discovery_roots()
        self.assertFalse(auto, 'con source il comportamento resta quello di prima')
        self.assertEqual(roots, [src])
        argv = daemon.fswatch_argv()
        self.assertEqual(argv[argv.index('--') + 1:], [src])

    def test_source_annidate_niente_doppioni(self):
        outer = self.path('outer')
        write(os.path.join(outer, 'sub/proj/.sync'), 'include: *.md\n')
        daemon = self.daemon(os.path.join(outer, 'sub'), outer)
        roots, _ = daemon.discovery_roots()
        self.assertEqual(roots, [outer], 'la radice annidata è coperta da quella esterna')
        argv = daemon.fswatch_argv()
        self.assertEqual(argv[argv.index('--') + 1:], [outer])
        self.assertEqual(len(daemon.projects), 1, 'progetto scoperto una volta sola')


if __name__ == '__main__':
    unittest.main()
