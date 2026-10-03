import os
import re
import shutil
import sys
import tempfile
import time
import unittest

from safekeep.config import parse_config
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
        self.project = load_project(self.root, parse_config(''))

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
    def test_argv_esatto_dallo_spec(self):
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
        argv = daemon.fswatch_argv()
        self.assertEqual(argv[:7],
                         ['fswatch', '-0', '-m', 'fsevents_monitor', '-r', '-l', '1.0'])
        regexes = [argv[i + 1] for i, a in enumerate(argv) if a == '-e']
        self.assertTrue(regexes)
        self.assertTrue(any(re.search(rx, '/qualcuno/node_modules/x') for rx in regexes))
        self.assertTrue(any('skipme/' in rx for rx in regexes))
        self.assertEqual(argv[argv.index('--') + 1:], [src])


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


if __name__ == '__main__':
    unittest.main()
