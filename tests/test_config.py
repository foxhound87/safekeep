import os
import tempfile
import unittest

from safekeep.config import (
    ConfigError,
    dest_path,
    discover_projects,
    parse_config,
    parse_sync,
    validate_dests,
)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOGGER = 'safekeep'


def read_example(name):
    with open(os.path.join(ROOT, 'examples', name)) as f:
        return f.read()


class SyncParseTest(unittest.TestCase):
    def test_esempio_sync_valido(self):
        with self.assertNoLogs(LOGGER, 'WARNING'):
            cfg = parse_sync(read_example('sync.example'), origin='sync.example')
        self.assertEqual(cfg.name, 'myapp')
        self.assertEqual([r.pattern for r in cfg.defaults], ['*.log', '.env.local'])
        self.assertEqual(len(cfg.rules), 11)
        self.assertEqual(cfg.rules[0].pattern, '/.env')
        self.assertTrue(cfg.rules[0].include)
        self.assertEqual(cfg.rules[10].pattern, '*.tmp')
        self.assertFalse(cfg.rules[10].include)

    def test_dest_e_minus_dest_in_sync_invalida(self):
        # le destinazioni vivono solo in ~/.safekeep: nel .sync è riga invalida
        text = "name: x\ndest: /d1\n-dest: /d2\nexclude: *.log\n"
        with self.assertLogs(LOGGER, 'WARNING') as cm:
            cfg = parse_sync(text, log_level='info', origin='p.sync')
        self.assertIn('p.sync:2', cm.output[0])
        self.assertIn('p.sync:3', cm.output[1])
        self.assertEqual(cfg.name, 'x')            # il resto della config resta valido
        self.assertEqual([r.pattern for r in cfg.rules], ['*.log'])

    def test_dest_in_sync_debug_fatale(self):
        with self.assertRaises(ConfigError) as cm:
            parse_sync("name: x\ndest: /d1\n", log_level='debug', origin='p.sync')
        self.assertIn('p.sync:2', str(cm.exception))

    def test_riga_nuda_info_warning_e_skip(self):
        text = "name: x\nriga invalida qui\nexclude: *.log\n"
        with self.assertLogs(LOGGER, 'WARNING') as cm:
            cfg = parse_sync(text, log_level='info', origin='p.sync')
        self.assertIn('p.sync:2', cm.output[0])
        self.assertEqual(cfg.name, 'x')
        self.assertEqual([r.pattern for r in cfg.rules], ['*.log'])

    def test_riga_nuda_debug_fatale(self):
        text = "name: x\nriga invalida qui\n"
        with self.assertRaises(ConfigError) as cm:
            parse_sync(text, log_level='debug', origin='p.sync')
        self.assertIn('p.sync:2', str(cm.exception))

    def test_defaults_e_ordine_libero_delle_chiavi(self):
        text = ("exclude: *.tmp\n"
                "defaults: exclude: *.log\n"
                "name: y\n")
        cfg = parse_sync(text)
        self.assertEqual([r.pattern for r in cfg.defaults], ['*.log'])
        self.assertEqual(cfg.name, 'y')

    def test_commenti_e_righe_vuote(self):
        cfg = parse_sync("# solo commenti\n\n   \nname: z\n")
        self.assertEqual(cfg.name, 'z')
        self.assertEqual(cfg.rules, [])


class GlobalConfigTest(unittest.TestCase):
    def test_esempio_config_valido(self):
        cfg = parse_config(read_example('safekeep.example'), origin='safekeep.example')
        self.assertEqual(cfg.sources, ['/Users/REPLACEME/Code', '/Users/REPLACEME/Projects'])
        self.assertEqual(cfg.dests, ['/Volumes/BackupUSB/backup', '/Volumes/NAS/generale'])
        self.assertEqual(cfg.layout, 'relative')
        self.assertEqual(cfg.log_level, 'info')
        self.assertEqual([r.pattern for r in cfg.defaults], ['*.log', '.cache/'])

    def test_default_di_base(self):
        cfg = parse_config('')
        self.assertEqual(cfg.sources, [])
        self.assertEqual(cfg.dests, [])
        self.assertEqual(cfg.layout, 'relative')
        self.assertEqual(cfg.log_level, 'info')
        self.assertEqual(cfg.defaults, [])

    def test_chiavi_ripetibili_e_valori(self):
        # stile ~/.safekeep: source: e dest: ripetibili, resto invariato
        text = ('source: /a\nsource: /b\ndest: /d1\ndest: /d2\n'
                'layout: full\nlog_level: warn\n'
                'defaults: exclude: *.log\ndefaults: include: keep.log\n')
        cfg = parse_config(text, origin='~/.safekeep')
        self.assertEqual(cfg.sources, ['/a', '/b'])
        self.assertEqual(cfg.dests, ['/d1', '/d2'])
        self.assertEqual(cfg.layout, 'full')
        self.assertEqual(cfg.log_level, 'warn')
        self.assertEqual([(r.pattern, r.include) for r in cfg.defaults],
                         [('*.log', False), ('keep.log', True)])

    def test_riga_invalida_e_chiave_sconosciuta_fatali_con_riga(self):
        with self.assertRaises(ConfigError) as cm:
            parse_config('layout: relative\nriga nuda\n', origin='cfg')
        self.assertIn('cfg:2', str(cm.exception))
        with self.assertRaises(ConfigError) as cm:
            parse_config('source: /a\nfrobnicate: 1\n', origin='cfg')
        self.assertIn('cfg:2', str(cm.exception))
        # chiave ignota → sempre fatale con numero di riga (fail-fast)
        with self.assertRaises(ConfigError) as cm:
            parse_config('source: /a\nchiave_da_vecchio_config: yes\n', origin='cfg')
        self.assertIn('cfg:2', str(cm.exception))

    def test_valori_invalidi_fatali(self):
        for text in ['layout: sideways\n', 'log_level: shout\n',
                     'defaults: niente\n', 'dest:\n']:
            with self.subTest(text=text):
                with self.assertRaises(ConfigError):
                    parse_config(text, origin='cfg')


class DestPathTest(unittest.TestCase):
    def test_relative(self):
        self.assertEqual(dest_path('/backup', '/Users/u', 'myapp/docs/a.md', 'relative'),
                         '/backup/myapp/docs/a.md')
        self.assertEqual(dest_path('/backup', '/Users/u', '/Users/u/myapp/a.md', 'relative'),
                         '/backup/myapp/a.md')
        self.assertEqual(dest_path('/backup', '/Users/u', 'myapp', 'relative'),
                         '/backup/myapp')

    def test_full(self):
        self.assertEqual(dest_path('/backup', '/Users/u', 'myapp/docs/a.md', 'full'),
                         '/backup/Users/u/myapp/docs/a.md')
        self.assertEqual(dest_path('/backup', '/Users/u', '/Users/u/myapp/a.md', 'full'),
                         '/backup/Users/u/myapp/a.md')

    def test_layout_invalido(self):
        with self.assertRaises(ValueError):
            dest_path('/backup', '/Users/u', 'x', 'sideways')


class ValidateDestsTest(unittest.TestCase):
    def test_dest_dentro_source_errore(self):
        with self.assertRaises(ConfigError) as cm:
            validate_dests(['/src'], ['/src/backup'])
        self.assertIn('/src', str(cm.exception))
        with self.assertRaises(ConfigError):
            validate_dests(['/src'], ['/src'])

    def test_dest_fuori_source_ok(self):
        validate_dests(['/src'], ['/other/backup'])
        validate_dests(['/src'], ['/src2/backup'])   # prefisso letterale non basta


class DiscoveryTest(unittest.TestCase):
    @staticmethod
    def _touch(base, rel):
        p = os.path.join(base, rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, 'w'):
            pass

    def test_source_con_sync_alla_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._touch(tmp, '.sync')
            self._touch(tmp, 'proj1/.sync')
            self._touch(tmp, 'proj1/sub/.sync')       # sotto-progetto
            self._touch(tmp, 'plain/notes.txt')       # senza .sync → non è progetto
            self._touch(tmp, '.git/hidden/.sync')     # pruned dai builtin
            self._touch(tmp, 'node_modules/pkg/.sync')
            self.assertEqual(discover_projects([tmp]),
                             [tmp, os.path.join(tmp, 'proj1'), os.path.join(tmp, 'proj1', 'sub')])

    def test_source_senza_sync_alla_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._touch(tmp, 'a/.sync')
            self._touch(tmp, 'a/b/.sync')
            self._touch(tmp, 'plain/x.txt')
            self.assertEqual(discover_projects([tmp]),
                             [os.path.join(tmp, 'a'), os.path.join(tmp, 'a', 'b')])

    def test_source_assente(self):
        with self.assertLogs(LOGGER, 'WARNING'):
            self.assertEqual(discover_projects(['/percorso/che/non/esiste']), [])


if __name__ == '__main__':
    unittest.main()
