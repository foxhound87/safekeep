import os
import tempfile
import unittest
from unittest import mock

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
        # le righe nude (forma principale) finiscono nella stessa lista delle chiavi
        self.assertEqual([r.pattern for r in cfg.rules],
                         ['.env', '.env.*', '.vault/', '*.md', 'docs/**',
                          'docs/vendor/CHANGELOG.md', 'node_modules/', '.git/',
                          'node_modules/LICENSE-key.txt', '*.swp', '*.tmp'])
        self.assertEqual([r.include for r in cfg.rules],
                         [True, True, True, True, True, False,
                          False, False, True, False, False])
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

    def test_chiave_sconosciuta_info_warning_e_skip(self):
        # a forma di chiave ⇒ mai una riga nuda: warning + skip (SPEC.md §4.2)
        text = "name: x\nunknown: y\nexclude: *.log\n"
        with self.assertLogs(LOGGER, 'WARNING') as cm:
            cfg = parse_sync(text, log_level='info', origin='p.sync')
        self.assertIn('p.sync:2', cm.output[0])
        self.assertEqual(cfg.name, 'x')
        self.assertEqual([r.pattern for r in cfg.rules], ['*.log'])

    def test_chiave_sconosciuta_debug_fatale(self):
        text = "name: x\nunknown: y\n"
        with self.assertRaises(ConfigError) as cm:
            parse_sync(text, log_level='debug', origin='p.sync')
        self.assertIn('p.sync:2', str(cm.exception))

    def test_riga_nuda_diventa_include(self):
        cfg = parse_sync("docs\n*.md\n")
        self.assertTrue(all(r.include for r in cfg.rules))
        self.assertEqual([r.pattern for r in cfg.rules], ['docs', '*.md'])

    def test_riga_nuda_con_bang_diventa_exclude(self):
        cfg = parse_sync("*.md\n!docs/vendor/*.md\n")
        self.assertTrue(cfg.rules[0].include)
        self.assertFalse(cfg.rules[1].include)
        self.assertEqual(cfg.rules[1].pattern, 'docs/vendor/*.md')

    def test_righe_nude_nello_stesso_ordine_delle_chiavi(self):
        # lista unica, last-match-wins: il ! sotto include: lo batte
        cfg = parse_sync("include: *.md\nsecret.txt\n!secret.txt\n")
        self.assertEqual([r.pattern for r in cfg.rules],
                         ['*.md', 'secret.txt', 'secret.txt'])
        self.assertEqual([r.include for r in cfg.rules], [True, True, False])

    def test_riga_nuda_vuota_o_solo_bang_invalida(self):
        with self.assertLogs(LOGGER, 'WARNING') as cm:
            cfg = parse_sync("!\nok.md\n", log_level='info', origin='p.sync')
        self.assertIn('p.sync:1', cm.output[0])
        self.assertEqual([r.pattern for r in cfg.rules], ['ok.md'])

    def test_righe_nude_del_config_utente_reale(self):
        # REGRESSIONE: il .sync reale dell'utente (righe nude gitignore-style)
        # veniva scartato per intero → allow-list vuota → la copia partiva
        # comunque sui symlink, non sui file richiesti (SPEC.md §4.2/§5)
        text = ".env\n.env.*\n.vault\n*.md\n"
        with self.assertNoLogs(LOGGER, 'WARNING'):
            cfg = parse_sync(text, origin='/Users/cla/projects/.sync')
        self.assertEqual([r.pattern for r in cfg.rules],
                         ['.env', '.env.*', '.vault', '*.md'])
        self.assertTrue(all(r.include for r in cfg.rules))

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
        self.assertEqual(cfg.discarded, 0)


class SyncInvalidoTest(unittest.TestCase):
    """REGRESSIONE CR-03: pattern da `.sync` non attendibile (regex invalida,
    NUL) → warning + riga scartata con `log_level: info`, `ConfigError` con
    `debug` (SPEC.md §4.2) — mai un traceback da `re.error`."""

    def test_regex_invalida_warning_e_riga_scartata(self):
        text = 'include: [z-a]*\ninclude: *.md\n'
        with self.assertLogs(LOGGER, 'WARNING') as cm:
            cfg = parse_sync(text, log_level='info', origin='p.sync')
        self.assertIn('p.sync:1', cm.output[0])
        self.assertIn('regex non valido', cm.output[0])
        self.assertEqual([r.pattern for r in cfg.rules], ['*.md'])
        self.assertEqual(cfg.discarded, 1)            # conteggio per doctor

    def test_riga_nuda_regex_invalida_warning_e_skip(self):
        # le righe nude passano da `bare_rule`: devono gestirsi allo stesso modo
        with self.assertLogs(LOGGER, 'WARNING') as cm:
            cfg = parse_sync('[z-a]*\n*.md\n', log_level='info', origin='p.sync')
        self.assertIn('p.sync:1', cm.output[0])
        self.assertEqual([r.pattern for r in cfg.rules], ['*.md'])
        self.assertEqual(cfg.discarded, 1)

    def test_regex_invalida_debug_fatale(self):
        with self.assertRaises(ConfigError) as cm:
            parse_sync('include: [z-a]*\n', log_level='debug', origin='p.sync')
        self.assertIn('p.sync:1', str(cm.exception))

    def test_pattern_nul_warning_e_skip(self):
        with self.assertLogs(LOGGER, 'WARNING') as cm:
            cfg = parse_sync('include: a\0b\n*.md\n', log_level='info', origin='p.sync')
        self.assertIn('p.sync:1', cm.output[0])
        self.assertIn('NUL', cm.output[0])
        self.assertEqual([r.pattern for r in cfg.rules], ['*.md'])
        self.assertEqual(cfg.discarded, 1)


class GlobalConfigTest(unittest.TestCase):
    def test_esempio_config_valido(self):
        cfg = parse_config(read_example('safekeep.example'), origin='safekeep.example')
        # `source:` nel le righe sono commentate: il default è auto-discovery (§6)
        self.assertEqual(cfg.sources, [])
        self.assertEqual(cfg.dests, ['/Volumes/BackupUSB/backup', '/Volumes/NAS/generale'])
        self.assertEqual(cfg.layout, 'relative')
        self.assertEqual(cfg.log_level, 'info')
        self.assertEqual([r.pattern for r in cfg.defaults], ['*.log', '.cache/'])

    def test_default_di_base(self):
        cfg = parse_config('dest: /d\n')
        self.assertEqual(cfg.sources, [])
        self.assertEqual(cfg.dests, ['/d'])
        self.assertEqual(cfg.layout, 'relative')
        self.assertEqual(cfg.log_level, 'info')
        self.assertEqual(cfg.defaults, [])

    def test_senza_source_e_valida_modalita_auto_discovery(self):
        cfg = parse_config('dest: /d\nlog_level: warn\n', origin='cfg')
        self.assertEqual(cfg.sources, [])        # nessun source ≠ errore

    def test_senza_dest_errore_con_hint(self):
        for text in ['', 'source: /a\n', 'layout: full\n']:
            with self.subTest(text=text):
                with self.assertRaises(ConfigError) as cm:
                    parse_config(text, origin='cfg')
                msg = str(cm.exception)
                self.assertIn('dest', msg)
                self.assertIn('safekeep.example', msg)   # hint su dove guardare

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

    def test_auto_scopre_i_progetti_sotto_home(self):
        # modalità auto-discovery: scan di $HOME con pruning dedicato
        with tempfile.TemporaryDirectory() as tmp:
            self._touch(tmp, 'Code/myapp/.sync')
            self._touch(tmp, 'Code/myapp/docs/a.md')
            self._touch(tmp, 'notes/.sync')
            self._touch(tmp, 'plain/x.txt')               # senza .sync → non progetto
            self.assertEqual(discover_projects([tmp], auto=True),
                             [os.path.join(tmp, 'Code', 'myapp'),
                              os.path.join(tmp, 'notes')])

    def test_auto_pruning_delle_dir_rumorose_e_nascoste(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._touch(tmp, 'Library/.sync')             # macOS Library: pruned
            self._touch(tmp, 'Library/Containers/x/.sync')
            self._touch(tmp, 'node_modules/pkg/.sync')
            self._touch(tmp, 'venv/lib/.sync')
            self._touch(tmp, '.Trash/x/.sync')
            self._touch(tmp, '.config/app/.sync')
            # DECISIONE: anche una dir nascosta è pruned → il suo `.sync`
            # NON diventa progetto (niente `.git`, `.venv`, `.local`, …)
            self._touch(tmp, '.hidden/proj/.sync')
            self.assertEqual(discover_projects([tmp], auto=True), [])

    def test_auto_non_apre_le_cartelle_tcc_protette(self):
        # Reale: senza Full Disk Access l'`opendir()` su `~/Desktop` resta
        # sospeso nel kernel per un agent launchd → la dir va potata PRIMA che
        # os.walk la apra, non dopo (l'assert è su "aperta", non su "scoperta":
        # anche il pruning di prima generazione la lasciava scoperta).
        # `Applications` è potato per lo stesso motivo strutturale: rami
        # enormi (QtWebEngine/IBKR) senza alcun senso come progetto.
        tcc = {'Desktop', 'Documents', 'Downloads', 'Movies', 'Music',
               'Pictures', 'Public', 'Applications'}
        with tempfile.TemporaryDirectory() as tmp:
            self._touch(tmp, 'Desktop/proj/.sync')          # TCC: non scoperto
            self._touch(tmp, 'Documents/proj/.sync')        # idem
            self._touch(tmp, 'a/Documents/proj/.sync')      # match sul basename
            self._touch(tmp, 'Applications/proj/.sync')     # potato: non scoperto
            self._touch(tmp, 'projects/proj/.sync')         # normale: scoperto
            aperte = []
            real_walk = os.walk

            def spy(top):
                for entry in real_walk(top):
                    aperte.append(entry[0])
                    yield entry

            with mock.patch('os.walk', spy):
                found = discover_projects([tmp], auto=True)
            self.assertEqual(found, [os.path.join(tmp, 'projects', 'proj')])
            self.assertEqual([p for p in aperte
                              if os.path.basename(p) in tcc], [],
                             'una cartella TCC non deve essere nemmeno aperta')

    def test_auto_sotto_progetto_annidato(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._touch(tmp, 'proj/.sync')
            self._touch(tmp, 'proj/sub/.sync')            # sotto-progetto (§4.1)
            self.assertEqual(discover_projects([tmp], auto=True),
                             [os.path.join(tmp, 'proj'),
                              os.path.join(tmp, 'proj', 'sub')])

    def test_auto_e_source_stesso_scan_senza_pruning_nascosto(self):
        # in modalità source la dir nascosta NON è pruned dai builtin
        with tempfile.TemporaryDirectory() as tmp:
            self._touch(tmp, '.hidden/proj/.sync')
            self._touch(tmp, 'Library/.sync')
            self.assertEqual(discover_projects([tmp]),
                             [os.path.join(tmp, '.hidden', 'proj'),
                              os.path.join(tmp, 'Library')])


if __name__ == '__main__':
    unittest.main()
