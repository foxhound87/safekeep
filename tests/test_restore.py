"""Test di `safekeep.restore` (SPEC.md §19): inventario + mapping entrambi i
layout, matching (assoluto/relativo/glob/no-match), dry-run vs `--force`
(contenuto, mtime, symlink, zero residui tmp), containment invertito,
origine ambigua/non montata, menu interattivo con `input` patchato, adapter
S3 mock e round-trip hermetico (sync → cancella sorgente → restore → identico).

I test CLI girano **in-process** con HOME finto (prassi di tests/test_cli.py):
le fixtura vivono tutte in un `mktemp -d`, nessuna home reale toccata.
"""
import io
import os
import shutil
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

from safekeep import cli, restore
from safekeep.config import dest_path, parse_config, parse_rule
from safekeep.copier import TMP_INFIX, reconcile_project
from safekeep.matcher import Matcher
from safekeep.s3dest import S3Destination

try:
    import botocore  # noqa: F401
    HAVE_BOTOCORE = True
except ImportError:
    HAVE_BOTOCORE = False


def write(path, text=''):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'w') as f:
        f.write(text)
    return path


def make_project(root, source_root=None):
    return SimpleNamespace(root=os.path.realpath(root),
                           source_root=os.path.realpath(source_root or root),
                           name=os.path.basename(os.path.normpath(root)))


class BaseFs(unittest.TestCase):
    """Albero src/proj (.sync, docs/a.md, link.md) + backup in dest/proj/."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        # realpath delle radici: su macOS il tmp vive sotto /var → /private/var
        # e i target ricostruiti sono realpathizzati (come nel daemon)
        self.src = os.path.realpath(os.path.join(self.tmp, 'src'))
        self.dest = os.path.realpath(os.path.join(self.tmp, 'dest'))
        self.proj_root = os.path.join(self.src, 'proj')
        write(os.path.join(self.proj_root, '.sync'), 'include: *.md\n')
        write(os.path.join(self.proj_root, 'docs', 'a.md'), 'v1 sorgente')
        os.symlink('docs/a.md', os.path.join(self.proj_root, 'link.md'))
        # backup (layout relative: dest_path(dest, src, projroot) = dest/proj)
        write(os.path.join(self.dest, 'proj', 'docs', 'a.md'), 'v1 backup')
        os.symlink('docs/a.md', os.path.join(self.dest, 'proj', 'link.md'))
        self.project = make_project(self.proj_root, self.src)

    def inventory(self, layout='relative'):
        return restore.inventory([self.project], 'fs', self.dest, layout)


class InventoryMappingTest(BaseFs):
    def test_mapping_inverse_relative(self):
        entries, orphans = self.inventory('relative')
        self.assertEqual(orphans, [])
        by_rel = {e.rel: e for e in entries}
        self.assertIn('docs/a.md', by_rel)
        self.assertIn('link.md', by_rel)
        e = by_rel['docs/a.md']
        self.assertEqual(e.target,
                         os.path.realpath(os.path.join(self.proj_root, 'docs', 'a.md')))
        self.assertEqual(e.backup,
                         os.path.join(self.dest, 'proj', 'docs', 'a.md'))

    def test_mapping_inverse_full_stesso_target(self):
        # layout full: prefix = dest/<assoluto senza /> — serve un backup
        # costruito secondo quel layout; il target resta il path originale
        prefix = dest_path(self.dest, self.src, self.proj_root, 'full')
        write(os.path.join(prefix, 'docs', 'a.md'), 'v1 backup full')
        entries, orphans = self.inventory('full')
        targets = {e.target for e in entries}
        self.assertIn(os.path.realpath(os.path.join(self.proj_root, 'docs', 'a.md')),
                      targets)
        # il backup costruito in layout relative è orfano in questo layout
        self.assertEqual(len(orphans), 2)
        self.assertTrue(all(o.startswith(self.dest + os.sep + 'proj')
                            for o in orphans))

    def test_dir_symlink_e_foglia(self):
        os.symlink('docs', os.path.join(self.dest, 'proj', 'dirlink'))
        entries, _ = self.inventory()
        by_rel = {e.rel: e for e in entries}
        self.assertIn('dirlink', by_rel)
        self.assertEqual(by_rel['dirlink'].target,
                         os.path.join(self.proj_root, 'dirlink'))

    def test_orfani_senza_progetto(self):
        write(os.path.join(self.dest, 'stray', 'x.md'), 'x')
        entries, orphans = self.inventory()
        self.assertEqual(len(orphans), 1)
        self.assertTrue(orphans[0].endswith(os.path.join('stray', 'x.md')))
        self.assertTrue(all('stray' not in e.rel for e in entries))


class MatchingTest(BaseFs):
    def setUp(self):
        super().setUp()
        self.entries, _ = self.inventory()
        self.abs_target = os.path.join(self.proj_root, 'docs', 'a.md')

    def test_path_assoluto(self):
        got = restore.select(self.entries, [self.abs_target])
        self.assertEqual([e.rel for e in got], ['docs/a.md'])

    def test_path_relativo_alla_source_root(self):
        got = restore.select(self.entries, ['proj/docs/a.md'])
        self.assertEqual([e.rel for e in got], ['docs/a.md'])

    def test_glob_fnmatch_relativo_e_assoluto(self):
        got = restore.select(self.entries, ['proj/docs/*.md'])
        self.assertEqual([e.rel for e in got], ['docs/a.md'])
        got = restore.select(self.entries, [self.proj_root + '/*'])
        self.assertEqual(sorted(e.rel for e in got), ['docs/a.md', 'link.md'])

    def test_no_match(self):
        self.assertEqual(restore.select(self.entries, ['nope.md', '*.zzz']), [])

    def test_path_assoluto_via_symlink_alias(self):
        # path scritto via symlink (macOS /var → /private/var): deve matchare
        # lo stesso — sia forma assoluta sia glob (prefisso risolto, §19.2)
        alias = os.path.join(self.tmp, 'src-alias')
        os.symlink(self.src, alias)
        got = restore.select(self.entries,
                             [os.path.join(alias, 'proj', 'docs', 'a.md')])
        self.assertEqual([e.rel for e in got], ['docs/a.md'])
        got = restore.select(self.entries,
                             [os.path.join(alias, 'proj', 'docs', '*.md')])
        self.assertEqual([e.rel for e in got], ['docs/a.md'])


class PlanExecuteTest(BaseFs):
    def setUp(self):
        super().setUp()
        self.entries, _ = self.inventory()
        self.target = os.path.join(self.proj_root, 'docs', 'a.md')
        # la "sorgente" viene cancellata: il target è la cosa da ripristinare
        os.remove(self.target)
        os.remove(os.path.join(self.proj_root, 'link.md'))

    def test_piano_crea_e_sintesi(self):
        rows, syn = restore.plan(self.entries)
        self.assertEqual(syn['crea'], 2)
        self.assertEqual(syn['da_copiare'], 2)
        self.assertTrue(all(n == '(crea)' for _, _, n in rows))

    def test_piano_sovrascrive_e_aggiornati(self):
        write(self.target, 'v2 sorgente')
        rows, syn = restore.plan(self.entries)
        nota = {t: n for _, t, n in rows}
        self.assertEqual(nota[self.target], '(sovrascrive)')
        self.assertEqual(syn['sovrascrive'], 1)
        # il piano NON scrive nulla (dry-run, §19.1)
        with open(self.target) as f:
            self.assertEqual(f.read(), 'v2 sorgente')

    def test_force_scrive_contenuto_mtime_e_symlink(self):
        backup = os.path.join(self.dest, 'proj', 'docs', 'a.md')
        bstat = os.stat(backup)
        ok, err, skipped = restore.execute(self.entries)
        self.assertEqual((ok, err, skipped), (2, 0, 0))
        with open(self.target) as f:
            self.assertEqual(f.read(), 'v1 backup')
        self.assertEqual(os.stat(self.target).st_mtime_ns, bstat.st_mtime_ns)
        link = os.path.join(self.proj_root, 'link.md')
        self.assertTrue(os.path.islink(link))
        self.assertEqual(os.readlink(link), 'docs/a.md')
        # zero residui tmp (§7.2)
        for dirpath, _, names in os.walk(self.src):
            self.assertFalse(any(TMP_INFIX in n for n in names))

    def test_contenuto_diverso_sovrascritto(self):
        write(self.target, 'v2 sorgente')
        ok, err, skipped = restore.execute(self.entries)
        self.assertEqual((ok, err, skipped), (2, 0, 0))
        with open(self.target) as f:
            self.assertEqual(f.read(), 'v1 backup')

    def test_containment_rifiuta_target_fuori_dalle_source(self):
        outside = os.path.join(self.tmp, 'outside.md')
        e = restore.Entry(os.path.join(self.dest, 'proj', 'docs', 'a.md'),
                          outside, self.project, 'docs/a.md', None)
        ok, err, skipped = restore.execute([e])
        self.assertEqual((ok, err, skipped), (0, 1, 0))
        self.assertFalse(os.path.exists(outside))

    def test_symlink_rotto_backup_vs_target_assente(self):
        e = restore.Entry(os.path.join(self.dest, 'proj', 'link.md'),
                          os.path.join(self.proj_root, 'link.md'),
                          self.project, 'link.md', None)
        self.assertTrue(restore.need_restore(e))

    def test_idempotenza_dopo_restore(self):
        restore.execute(self.entries)
        again, syn = restore.plan(self.entries)
        self.assertEqual(again, [])
        self.assertEqual(syn['da_copiare'], 0)
        self.assertEqual(syn['aggiornati'], 2)


class RoundTripTest(unittest.TestCase):
    """sync → cancella la sorgente → restore --force → identico (hermetico:
    solo tmpdir, gira sempre — niente env gate necessario)."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.src = os.path.join(self.tmp, 'src')
        self.dest = os.path.join(self.tmp, 'dest')
        root = os.path.join(self.src, 'proj')
        write(os.path.join(root, '.sync'), 'include: *.md\n')
        write(os.path.join(root, 'a.md'), 'contenuto v1')
        write(os.path.join(root, 'skip.txt'), 'non copiato')
        os.symlink('a.md', os.path.join(root, 'link.md'))
        self.project = make_project(root, self.src)
        matcher = Matcher([parse_rule('include: *.md')])
        reconcile_project(self.project.root, self.dest, matcher, 'relative',
                          dest_path, dest_base=self.project.source_root)

    def test_round_trip(self):
        a = os.path.join(self.project.root, 'a.md')
        link = os.path.join(self.project.root, 'link.md')
        expected = os.stat(a).st_mtime_ns
        os.remove(a)
        os.remove(link)
        entries, orphans = restore.inventory([self.project], 'fs',
                                             self.dest, 'relative')
        self.assertEqual(orphans, [])
        matched = restore.select(entries, [a, link])
        self.assertEqual(len(matched), 2)
        ok, err, skipped = restore.execute(matched)
        self.assertEqual((ok, err, skipped), (2, 0, 0))
        with open(a) as f:
            self.assertEqual(f.read(), 'contenuto v1')
        self.assertEqual(os.stat(a).st_mtime_ns, expected)
        self.assertEqual(os.readlink(link), 'a.md')


class PickSourceTest(unittest.TestCase):
    def cfg(self, *dests):
        return SimpleNamespace(dests=list(dests))

    def test_candidato_unico(self):
        dest = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, dest, ignore_errors=True)
        kind, origin = restore.pick_source(self.cfg(dest))
        self.assertEqual((kind, origin), ('fs', dest))

    def test_ambiguo_errore_con_elenco(self):
        d1 = tempfile.mkdtemp()
        d2 = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d1, ignore_errors=True)
        self.addCleanup(shutil.rmtree, d2, ignore_errors=True)
        with self.assertRaises(restore.RestoreError) as cm:
            restore.pick_source(self.cfg(d1, d2))
        self.assertIn('ambigua', str(cm.exception))
        self.assertIn('local', str(cm.exception))
        self.assertIn('nas', str(cm.exception))
        self.assertIn('--from', str(cm.exception))
        # e con --from la scelta risolve
        self.assertEqual(restore.pick_source(self.cfg(d1, d2), from_='nas'),
                         ('fs', d2))

    def test_dest_non_montata_errore(self):
        assente = os.path.join(tempfile.mkdtemp(), 'non-mountato')
        with self.assertRaises(restore.RestoreError) as cm:
            restore.pick_source(self.cfg(assente))
        self.assertIn('non montata', str(cm.exception))

    def test_from_s3_senza_config_errore(self):
        dest = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, dest, ignore_errors=True)
        with self.assertRaises(restore.RestoreError):
            restore.pick_source(self.cfg(dest), from_='s3')
        s3 = SimpleNamespace(root='s3://bkt')
        self.assertEqual(restore.pick_source(self.cfg(dest), from_='s3', s3=s3),
                         ('s3', s3))

    def test_from_nas_mancante_errore_con_elenco(self):
        dest = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, dest, ignore_errors=True)
        with self.assertRaises(restore.RestoreError) as cm:
            restore.pick_source(self.cfg(dest), from_='nas')
        self.assertIn('local', str(cm.exception))

    def test_solo_s3_auto_detect(self):
        s3 = SimpleNamespace(root='s3://bkt/pre')
        self.assertEqual(restore.pick_source(self.cfg(), s3=s3), ('s3', s3))


class PickDestTest(unittest.TestCase):
    """`--dest <nome>` (SPEC §19.3/§21.4): origine univoca con precedenza su
    `--from` — fs nominato montato, S3 solo per target dichiarati in una
    route, ogni errore con l'elenco dei nomi disponibili."""

    # blocco s3_* valido: serve a parse per le route S3 (§21.2/§21.7b)
    S3 = 's3_bucket: bkt\ns3_access_key: AK\ns3_secret_key: SK\n'

    @staticmethod
    def cfg(text):
        return parse_config(text, origin='test')

    @staticmethod
    def tmpdir():
        return tempfile.mkdtemp()

    def test_fs_nominato_montato(self):
        dest = self.tmpdir()
        self.addCleanup(shutil.rmtree, dest, ignore_errors=True)
        cfg = self.cfg(f'dest nas: {dest}\n')
        self.assertEqual(restore.pick_dest(cfg, 'nas'), ('fs', dest))

    def test_fs_non_montato_errore_chiaro(self):
        assente = os.path.join(self.tmpdir(), 'volume-assente')
        cfg = self.cfg(f'dest nas: {assente}\n')
        with self.assertRaises(restore.RestoreError) as cm:
            restore.pick_dest(cfg, 'nas')
        self.assertIn('non montata', str(cm.exception))
        self.assertIn(assente, str(cm.exception))

    def test_nome_inesistente_errore_con_elenco(self):
        dest = self.tmpdir()
        self.addCleanup(shutil.rmtree, dest, ignore_errors=True)
        cfg = self.cfg(f'dest nas: {dest}\n')
        with self.assertRaises(restore.RestoreError) as cm:
            restore.pick_dest(cfg, 'zeta')
        msg = str(cm.exception)
        self.assertIn('inesistente', msg)
        self.assertIn('nas', msg)              # l'elenco dei nomi disponibili

    def test_s3_forme_breve_e_uri_della_route(self):
        cfg = self.cfg(self.S3 + 'route /src: s3:pre/code, s3://other/old\n')
        self.assertEqual(restore.pick_dest(cfg, 's3:pre/code'),
                         ('s3', ('bkt', 'pre/code')))
        self.assertEqual(restore.pick_dest(cfg, 's3://other/old'),
                         ('s3', ('other', 'old')))
        # stessa route citata nell'altra forma (bucket globale, §21.7b)
        self.assertEqual(restore.pick_dest(cfg, 's3://bkt/pre/code'),
                         ('s3', ('bkt', 'pre/code')))

    def test_s3_fuori_da_route_errore_con_elenco(self):
        cfg = self.cfg(self.S3 + 'route /src: s3:pre/code\n')
        with self.assertRaises(restore.RestoreError) as cm:
            restore.pick_dest(cfg, 's3:altro')
        msg = str(cm.exception)
        self.assertIn('inesistente', msg)
        self.assertIn('s3:pre/code', msg)      # solo i target dichiarati

    def test_s3_senza_blocco_s3_errore(self):
        # nessuna s3_* → nessuna route S3 possibile: il token resta un nome
        # inesistente (la dest anonima non è citabile, §21.2)
        dest = self.tmpdir()
        self.addCleanup(shutil.rmtree, dest, ignore_errors=True)
        cfg = self.cfg(f'dest: {dest}\n')
        with self.assertRaises(restore.RestoreError) as cm:
            restore.pick_dest(cfg, 's3:pre/code')
        self.assertIn('inesistente', str(cm.exception))


class FirstLevelMenuTest(unittest.TestCase):
    def test_first_level_raggruppa(self):
        p = make_project('/tmp/p')
        entries = [restore.Entry('b/docs/a.md', '/p/docs/a.md', p, 'docs/a.md', None),
                   restore.Entry('b/docs/b.md', '/p/docs/b.md', p, 'docs/b.md', None),
                   restore.Entry('b/top.md', '/p/top.md', p, 'top.md', None)]
        groups = restore.first_level(entries)
        self.assertEqual([name for name, _ in groups], ['docs', 'top.md'])
        self.assertEqual(len(groups[0][1]), 2)

    def test_menu_selezione_multipla_tutto_annulla(self):
        with mock.patch('builtins.input', side_effect=['1 3']):
            self.assertEqual(cli._menu('t:', ['a', 'b', 'c']), [0, 2])
        with mock.patch('builtins.input', side_effect=['a']):
            self.assertEqual(cli._menu('t:', ['a', 'b']), [0, 1])
        with mock.patch('builtins.input', side_effect=['q']):
            self.assertIsNone(cli._menu('t:', ['a']))
        with mock.patch('builtins.input', side_effect=['99', 'x', '2']):
            self.assertEqual(cli._menu('t:', ['a', 'b']), [1])
        with mock.patch('builtins.input', side_effect=EOFError):
            self.assertIsNone(cli._menu('t:', ['a']))


class FakeS3:
    """Adapter S3 finto con la stessa interfaccia usata da restore: root,
    dest_path (formula reale), iter_keys, download."""

    def __init__(self, root, keys):
        self.root = root                     # 's3://bkt/pre'
        self._keys = keys                    # key → (size, md, content)
        self.downloaded = []

    def dest_path(self, dest_root, source_root, relpath, layout):
        return S3Destination.dest_path(self, dest_root, source_root,
                                       relpath, layout)

    def iter_keys(self, prefix):
        for key in sorted(self._keys):
            if key.startswith(prefix):
                size, md, _ = self._keys[key]
                yield key, size, md

    def download(self, key, dst):
        # come il vero: tmp + os.replace (mai seguire il symlink finale)
        self.downloaded.append(key)
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        tmp = dst + '.faketmp'
        with open(tmp, 'w') as f:
            f.write(self._keys[key][2])
        os.replace(tmp, dst)
        return True


class InventoryS3Test(BaseFs):
    def fake_s3(self, root='s3://bkt/pre', extra=None):
        ns = 1_700_000_000_000_000_000
        keys = {'pre/proj/docs/a.md': (3, {'mtime': str(ns), 'mode': '0o100644'},
                                        'v1 backup'),
                'pre/proj/link.md': (0, {'symlink': 'docs/a.md'}, ''),
                'pre/altro/x.md': (1, {}, 'orfano')}
        keys.update(extra or {})
        return FakeS3(root, keys)

    def test_mapping_e_orfani_s3(self):
        s3 = self.fake_s3()
        entries, orphans = restore.inventory([self.project], 's3', s3,
                                             'relative')
        self.assertEqual(orphans, ['pre/altro/x.md'])
        by_rel = {e.rel: e for e in entries}
        self.assertEqual(set(by_rel), {'docs/a.md', 'link.md'})
        self.assertEqual(by_rel['docs/a.md'].target,
                         os.path.realpath(os.path.join(self.proj_root, 'docs', 'a.md')))

    def test_piano_s3_aggiornati_e_crea(self):
        # a.md: mtime del backup ≠ mtime della sorgente reale → la sorgente
        # va cancellata (→ crea); link.md: target presente e identico → aggiornato
        s3 = self.fake_s3()
        os.remove(os.path.join(self.proj_root, 'docs', 'a.md'))
        entries, _ = restore.inventory([self.project], 's3', s3, 'relative')
        rows, syn = restore.plan(entries)
        self.assertEqual(syn['aggiornati'], 1)        # link.md identico
        self.assertEqual(syn['crea'], 1)              # docs/a.md target assente
        self.assertEqual(syn['da_copiare'], 1)
        self.assertEqual(rows[0][2], '(crea)')

    def test_execute_s3_usa_download_e_containment(self):
        s3 = self.fake_s3()
        os.remove(os.path.join(self.proj_root, 'docs', 'a.md'))
        entries, _ = restore.inventory([self.project], 's3', s3, 'relative')
        ok, err, skipped = restore.execute(entries, s3=s3)
        self.assertEqual((ok, err, skipped), (2, 0, 0))
        self.assertEqual(sorted(s3.downloaded),
                         ['pre/proj/docs/a.md', 'pre/proj/link.md'])
        with open(os.path.join(self.proj_root, 'docs', 'a.md')) as f:
            self.assertEqual(f.read(), 'v1 backup')
        # containment vale anche per S3
        outside = restore.Entry('pre/proj/docs/a.md',
                                os.path.join(self.tmp, 'no.md'),
                                self.project, 'docs/a.md', None)
        self.assertEqual(restore.execute([outside], s3=s3), (0, 1, 0))


class CliRestoreTest(unittest.TestCase):
    """CLI in-process con HOME finto (prassi test_cli.py)."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.home = os.path.join(self.tmp, 'home')
        os.makedirs(self.home)
        self.src = os.path.realpath(os.path.join(self.tmp, 'src'))
        self.dest = os.path.realpath(os.path.join(self.tmp, 'dest'))
        self.dest2 = os.path.realpath(os.path.join(self.tmp, 'dest2'))
        root = os.path.join(self.src, 'proj')
        write(os.path.join(root, '.sync'), 'include: *.md\n')
        write(os.path.join(root, 'a.md'), 'v1')
        write(os.path.join(self.dest, 'proj', 'a.md'), 'v1 backup')
        self.target = os.path.join(root, 'a.md')
        self.cfg = write(os.path.join(self.tmp, 'safekeep'),
                         f'source: {self.src}\ndest: {self.dest}\n')

    def run_cli(self, *argv, tty=False, inputs=None, cfg=None):
        stdin = mock.Mock()
        stdin.isatty = lambda: tty
        ctx = [mock.patch.dict(os.environ, {'HOME': self.home}),
               mock.patch.object(sys, 'stdin', stdin),
               mock.patch('sys.stdout', io.StringIO()),
               mock.patch('sys.stderr', io.StringIO())]
        if inputs is not None:
            ctx.append(mock.patch('builtins.input', side_effect=inputs))
        with ctx[0], ctx[1], ctx[2] as out, ctx[3] as err:
            for c in ctx[4:]:
                c.start()
                self.addCleanup(c.stop)
            code = cli.main(['restore', '--config', cfg or self.cfg, *argv])
        return code, out.getvalue(), err.getvalue()

    def test_no_args_non_tty_errore(self):
        code, _, err = self.run_cli()
        self.assertEqual(code, 1)
        self.assertIn('terminale interattivo', err)

    def test_dry_run_non_scrive(self):
        os.remove(self.target)
        code, out, _ = self.run_cli(self.target)
        self.assertEqual(code, 0)
        self.assertIn('(crea)', out)
        self.assertIn('dry-run', out)
        self.assertFalse(os.path.exists(self.target))

    def test_force_scrive(self):
        os.remove(self.target)
        code, out, _ = self.run_cli('--force', self.target)
        self.assertEqual(code, 0)
        with open(self.target) as f:
            self.assertEqual(f.read(), 'v1 backup')
        self.assertIn('1 ripristinati, 0 errori', out)

    def test_glob_no_match_exit1(self):
        code, _, err = self.run_cli('*.zzz')
        self.assertEqual(code, 1)
        self.assertIn('nessun file corrispondente', err)

    def test_interattivo_menu_e_dry_run(self):
        os.remove(self.target)
        code, out, _ = self.run_cli(tty=True, inputs=['1', '1'])
        self.assertEqual(code, 0)
        self.assertIn('progetto:', out)
        self.assertIn('(crea)', out)
        self.assertFalse(os.path.exists(self.target))   # niente --force

    def test_interattivo_q_annulla(self):
        code, _, err = self.run_cli(tty=True, inputs=['q'])
        self.assertEqual(code, 1)
        self.assertIn('annullato', err)

    def test_interattivo_force(self):
        os.remove(self.target)
        code, out, _ = self.run_cli('--force', tty=True, inputs=['1', 'a'])
        self.assertEqual(code, 0)
        self.assertTrue(os.path.exists(self.target))

    def test_origine_ambigua_errore(self):
        os.makedirs(self.dest2, exist_ok=True)
        cfg2 = write(os.path.join(self.tmp, 'safekeep2'),
                     f'source: {self.src}\ndest: {self.dest}\ndest: {self.dest2}\n')
        stdin = mock.Mock()
        stdin.isatty = lambda: False
        with mock.patch.dict(os.environ, {'HOME': self.home}), \
                mock.patch.object(sys, 'stdin', stdin), \
                mock.patch('sys.stderr', io.StringIO()) as err:
            code = cli.main(['restore', '--config', cfg2, self.target])
        self.assertEqual(code, 1)
        self.assertIn('ambigua', err.getvalue())
        self.assertIn('--from', err.getvalue())

    def test_origine_ambigua_interattivo_menu(self):
        os.makedirs(self.dest2, exist_ok=True)
        write(os.path.join(self.dest2, 'proj', 'a.md'), 'v1 backup2')
        cfg2 = write(os.path.join(self.tmp, 'safekeep2'),
                     f'source: {self.src}\ndest: {self.dest}\ndest: {self.dest2}\n')
        os.remove(self.target)
        stdin = mock.Mock()
        stdin.isatty = lambda: True
        with mock.patch.dict(os.environ, {'HOME': self.home}), \
                mock.patch.object(sys, 'stdin', stdin), \
                mock.patch('builtins.input', side_effect=['2', '1', '1']), \
                mock.patch('sys.stdout', io.StringIO()) as out:
            code = cli.main(['restore', '--config', cfg2, '--force'])
        self.assertEqual(code, 0)
        self.assertIn('nas', out.getvalue())            # livello origine
        with open(self.target) as f:
            self.assertEqual(f.read(), 'v1 backup2')    # seconda dest scelta

    def _dest_cfg(self):
        """Config con due dest **nominate** `a`/`b` e SENZA route (§21.2)."""
        os.makedirs(self.dest2, exist_ok=True)
        return write(os.path.join(self.tmp, 'dest-nomi'),
                     f'source: {self.src}\ndest a: {self.dest}\n'
                     f'dest b: {self.dest2}\n')

    def test_dest_precedenza_su_from(self):
        # --dest b vince su --from local (classe): l'origine è la dest b (§19.3)
        write(os.path.join(self.dest2, 'proj', 'a.md'), 'backup della b')
        os.remove(self.target)
        code, out, _ = self.run_cli('--dest', 'b', '--from', 'local',
                                    '--force', self.target,
                                    cfg=self._dest_cfg())
        self.assertEqual(code, 0, out)
        self.assertIn(self.dest2, out)              # riga del piano sulla dest b
        with open(self.target) as f:
            self.assertEqual(f.read(), 'backup della b')   # non quello di `a`

    def test_dest_nome_inesistente_errore_con_elenco(self):
        code, _, err = self.run_cli('--dest', 'zeta', self.target,
                                    cfg=self._dest_cfg())
        self.assertEqual(code, 1)
        self.assertIn("inesistente: 'zeta'", err)
        self.assertIn('disponibili: a, b', err)     # elenco dei nomi validi

    def test_dest_non_montato_errore(self):
        assente = os.path.join(self.tmp, 'volume-assente')
        cfg = write(os.path.join(self.tmp, 'dest-gone'),
                    f'source: {self.src}\ndest nas: {assente}\n')
        code, _, err = self.run_cli('--dest', 'nas', self.target, cfg=cfg)
        self.assertEqual(code, 1)
        self.assertIn('non montata', err)
        self.assertIn(assente, err)

    @unittest.skipUnless(HAVE_BOTOCORE, 'botocore non installato (extra safekeep[s3])')
    def test_dest_s3_di_route_con_mock(self):
        # --dest s3:pre/code → origine = S3Destination per-route (§21.5), con
        # precedenza su --from local anche per la classe S3 (§19.3)
        fake = FakeS3('s3://bkt/pre/code',
                      {'pre/code/proj/a.md': (10, {}, 'backup da s3')})
        os.remove(self.target)
        cfg = write(os.path.join(self.tmp, 's3-route.cfg'),
                    f'source: {self.src}\ndest: {self.dest}\n'
                    's3_bucket: bkt\ns3_access_key: AK\ns3_secret_key: SK\n'
                    f'route {self.src}: s3:pre/code\n')
        os.chmod(cfg, 0o600)
        with mock.patch.object(cli.Daemon, 's3_dest', return_value=fake):
            code, out, err = self.run_cli('--dest', 's3:pre/code',
                                          '--from', 'local', '--force',
                                          self.target, cfg=cfg)
        self.assertEqual(code, 0, out + err)
        self.assertIn('pre/code/proj/a.md', out)    # riga del piano su S3
        self.assertEqual(fake.downloaded, ['pre/code/proj/a.md'])
        with open(self.target) as f:
            self.assertEqual(f.read(), 'backup da s3')

    def _nested_backup(self):
        """Backup `proj/sub/b.md` la cui source+parent `proj/sub/` non esiste."""
        write(os.path.join(self.dest, 'proj', 'sub', 'b.md'), 'v1 backup b')
        write(os.path.join(self.src, 'proj', 'sub', 'b.md'), 'b')
        shutil.rmtree(os.path.join(self.src, 'proj', 'sub'))
        return os.path.join(self.src, 'proj', 'sub', 'b.md')

    def test_if_dir_exists_skip_dry_run_e_sintesi(self):
        target = self._nested_backup()
        code, out, _ = self.run_cli('--if-dir-exists', target)
        self.assertEqual(code, 0)
        self.assertIn('directory destinazione assente', out)
        self.assertIn('0 da copiare, 1 saltati', out)
        self.assertIn('0 già aggiornati', out)
        self.assertFalse(os.path.exists(os.path.dirname(target)))

    def test_if_dir_exists_tutti_saltati_exit0(self):
        target = self._nested_backup()
        code, out, _ = self.run_cli('--force', '--if-dir-exists', target)
        self.assertEqual(code, 0)
        self.assertIn('0 ripristinati, 0 errori', out)
        self.assertIn('1 saltati (directory destinazione assente)', out)
        self.assertFalse(os.path.exists(target))

    def test_if_dir_exists_interattivo_skip_nel_piano(self):
        target = self._nested_backup()
        code, out, _ = self.run_cli('--if-dir-exists', tty=True,
                                    inputs=['1', 'a'])
        self.assertEqual(code, 0)
        self.assertIn('directory destinazione assente', out)
        self.assertFalse(os.path.exists(target))


class IfDirExistsTest(BaseFs):
    """`--if-dir-exists` (§19.6): skip se il parent del target manca."""

    def setUp(self):
        super().setUp()
        self.entries, _ = self.inventory()
        self.target = os.path.join(self.proj_root, 'docs', 'a.md')
        os.remove(self.target)
        os.remove(os.path.join(self.proj_root, 'link.md'))

    def test_flag_assente_comportamento_invariato(self):
        # niente flag: la parent `docs/` mancante viene creata come prima
        shutil.rmtree(os.path.join(self.proj_root, 'docs'))
        ok, err, skipped = restore.execute(self.entries)
        self.assertEqual((ok, err, skipped), (2, 0, 0))
        with open(self.target) as f:
            self.assertEqual(f.read(), 'v1 backup')

    def test_parent_esistente_copia(self):
        ok, err, skipped = restore.execute(self.entries, if_dir_exists=True)
        self.assertEqual((ok, err, skipped), (2, 0, 0))   # docs/ e proj/ esistono
        with open(self.target) as f:
            self.assertEqual(f.read(), 'v1 backup')

    def test_parent_mancante_skip_e_niente_dir(self):
        shutil.rmtree(os.path.join(self.proj_root, 'docs'))
        rows, syn = restore.plan(self.entries, if_dir_exists=True)
        nota = {t: n for _, t, n in rows}
        self.assertEqual(nota[self.target], restore.SKIP_NO_PARENT)
        self.assertEqual(syn['saltati'], 1)                # solo docs/a.md
        self.assertEqual(syn['da_copiare'], 1)             # link.md in proj/
        ok, err, skipped = restore.execute(self.entries, if_dir_exists=True)
        self.assertEqual((ok, err, skipped), (1, 0, 1))
        self.assertFalse(os.path.exists(os.path.dirname(self.target)))
        self.assertTrue(os.path.islink(os.path.join(self.proj_root, 'link.md')))

    def test_dry_run_mostra_skip(self):
        shutil.rmtree(os.path.join(self.proj_root, 'docs'))
        rows, syn = restore.plan(self.entries, if_dir_exists=True)
        self.assertTrue(any('directory destinazione assente' in n
                            for _, _, n in rows))
        self.assertEqual(syn['saltati'], 1)

    def test_s3_stessa_semantica(self):
        ns = 1_700_000_000_000_000_000
        keys = {'pre/proj/docs/a.md': (3, {'mtime': str(ns), 'mode': '0o100644'},
                                       'v1 backup'),
                'pre/proj/link.md': (0, {'symlink': 'docs/a.md'}, '')}
        s3 = FakeS3('s3://bkt/pre', keys)
        shutil.rmtree(os.path.join(self.proj_root, 'docs'))
        entries, _ = restore.inventory([self.project], 's3', s3, 'relative')
        ok, err, skipped = restore.execute(entries, s3=s3, if_dir_exists=True)
        self.assertEqual((ok, err, skipped), (1, 0, 1))
        self.assertEqual(s3.downloaded, ['pre/proj/link.md'])   # a.md non scaricato
        self.assertFalse(os.path.exists(os.path.dirname(self.target)))


if __name__ == '__main__':
    unittest.main()
