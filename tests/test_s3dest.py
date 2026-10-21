"""Test di `safekeep.s3dest` (SPEC.md §18): key mapping, needs_copy via
metadata, symlink zero-byte, mapping errori → pending, cache HeadBucket,
extra mancante.

Tutti con client botocore MOCKkato: nessuna rete. Senza botocore (extra non
installato, caso CI di default) la classe è interamente skip.
"""
import errno
import io
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

from safekeep.config import ConfigError, S3Config
from safekeep.s3dest import (
    HEAD_CACHE_TTL,
    S3Destination,
    S3ExtraMissing,
    have_botocore,
    secret_perms_insecure,
)

try:
    import botocore.exceptions as bce
    HAVE_BOTOCORE = True
except ImportError:
    HAVE_BOTOCORE = False

SKIP = unittest.skipUnless(HAVE_BOTOCORE, 'botocore non installato (extra safekeep[s3])')


def make_cfg(**kw):
    base = dict(endpoint='https://s3.example.com:443', bucket='bkt',
                prefix='safekeep', access_key='AK', secret_key='SK')
    base.update(kw)
    return S3Config(**base)


def client_error(code, status):
    return bce.ClientError(
        {'Error': {'Code': str(code), 'Message': str(code)},
         'ResponseMetadata': {'HTTPStatusCode': status}},
        'TestOp')


def make_dest(**kw):
    dest = S3Destination(make_cfg(**kw))
    dest._client = mock.Mock()          # client finto: zero rete
    return dest


@SKIP
class KeyMappingTest(unittest.TestCase):
    def test_root_e_parse(self):
        dest = make_dest()
        self.assertEqual(dest.root, 's3://bkt/safekeep')
        self.assertEqual(dest._parse('s3://bkt/safekeep/a/b.md'),
                         ('bkt', 'safekeep/a/b.md'))
        self.assertEqual(S3Destination._parse('s3://bkt'), ('bkt', ''))

    def test_root_senza_prefix(self):
        self.assertEqual(make_dest(prefix='').root, 's3://bkt')

    def test_dest_path_relative_specchio_della_fs(self):
        # SPEC §18.2: stesso identico calcolo di config.dest_path
        from safekeep.config import dest_path as fs_dest_path
        dest = make_dest()
        got = dest.dest_path(dest.root, '/Users/u', 'myapp/docs/a.md', 'relative')
        self.assertEqual(got, 's3://bkt/safekeep/myapp/docs/a.md')
        # lo stesso input sulla dest fs dà lo stesso relativo
        self.assertEqual(fs_dest_path('/backup', '/Users/u', 'myapp/docs/a.md',
                                      'relative'),
                         '/backup/myapp/docs/a.md')

    def test_dest_path_full(self):
        dest = make_dest()
        got = dest.dest_path(dest.root, '/Users/u', '/Users/u/p/a.md', 'full')
        self.assertEqual(got, 's3://bkt/safekeep/Users/u/p/a.md')

    def test_dest_path_layout_invalido(self):
        with self.assertRaises(ValueError):
            make_dest().dest_path('s3://bkt', '/u', 'a.md', 'sideways')


@SKIP
class NeedsCopyTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.src = os.path.join(self.tmp, 'note.md')
        with open(self.src, 'w') as f:
            f.write('abc')

    def head(self, size, md):
        return {'ContentLength': size, 'Metadata': md}

    def test_oggetto_assente_copia(self):
        dest = make_dest()
        dest._client.head_object.side_effect = client_error(404, 404)
        self.assertTrue(dest.needs_copy(self.src, 's3://bkt/safekeep/note.md'))

    def test_size_e_mtime_uguali_saltato(self):
        s = os.stat(self.src)
        dest = make_dest()
        dest._client.head_object.return_value = self.head(
            s.st_size, {'mtime': str(s.st_mtime_ns), 'mode': oct(s.st_mode)})
        self.assertFalse(dest.needs_copy(self.src, 's3://bkt/safekeep/note.md'))

    def test_metadata_case_del_server_normalizzate(self):
        # Silo restituisce le x-amz-meta-* col case canonizzato ("Mtime"):
        # la lettura le normalizza, idempotenza intatta (bug reale §18.2)
        s = os.stat(self.src)
        dest = make_dest()
        dest._client.head_object.return_value = self.head(
            s.st_size, {'Mtime': str(s.st_mtime_ns), 'Mode': oct(s.st_mode)})
        self.assertFalse(dest.needs_copy(self.src, 's3://bkt/safekeep/note.md'))

    def test_mtime_entro_la_tolleranza_saltato(self):
        # tolleranza 1s (MTIME_TOLERANCE): 0.5s di scarto → nessuna copia
        s = os.stat(self.src)
        dest = make_dest()
        dest._client.head_object.return_value = self.head(
            s.st_size, {'mtime': str(s.st_mtime_ns - 5 * 10 ** 8)})
        self.assertFalse(dest.needs_copy(self.src, 's3://bkt/safekeep/note.md'))

    def test_mtime_fuori_tolleranza_copia(self):
        s = os.stat(self.src)
        dest = make_dest()
        dest._client.head_object.return_value = self.head(
            s.st_size, {'mtime': str(s.st_mtime_ns - 2 * 10 ** 9)})
        self.assertTrue(dest.needs_copy(self.src, 's3://bkt/safekeep/note.md'))

    def test_size_diverso_copia(self):
        s = os.stat(self.src)
        dest = make_dest()
        dest._client.head_object.return_value = self.head(
            s.st_size + 1, {'mtime': str(s.st_mtime_ns)})
        self.assertTrue(dest.needs_copy(self.src, 's3://bkt/safekeep/note.md'))

    def test_metadata_mancanti_copia(self):
        s = os.stat(self.src)
        dest = make_dest()
        dest._client.head_object.return_value = self.head(s.st_size, {})
        self.assertTrue(dest.needs_copy(self.src, 's3://bkt/safekeep/note.md'))

    def test_sorgente_sparita_niente_da_copiare(self):
        dest = make_dest()
        self.assertFalse(dest.needs_copy(self.src + '.fantasma',
                                         's3://bkt/safekeep/x.md'))

    def test_symlink_target_uguale_saltato_diverso_copia(self):
        link = os.path.join(self.tmp, 'link')
        os.symlink('note.md', link)
        dest = make_dest()
        dest._client.head_object.return_value = self.head(
            0, {'symlink': 'note.md', 'mtime': '1', 'mode': '0o120777'})
        self.assertFalse(dest.needs_copy(link, 's3://bkt/safekeep/link'))
        dest._client.head_object.return_value = self.head(
            0, {'symlink': 'altro.md'})
        self.assertTrue(dest.needs_copy(link, 's3://bkt/safekeep/link'))


@SKIP
class CopyTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.src = os.path.join(self.tmp, 'note.md')
        with open(self.src, 'w') as f:
            f.write('abc')

    def test_copy_file_con_metadata_mtime_e_mode(self):
        dest = make_dest()
        s = os.stat(self.src)
        self.assertTrue(dest.copy(self.src, 's3://bkt/safekeep/note.md'))
        kwargs = dest._client.put_object.call_args[1]
        self.assertEqual(kwargs['Bucket'], 'bkt')
        self.assertEqual(kwargs['Key'], 'safekeep/note.md')
        self.assertEqual(kwargs['Body'].name, self.src)   # fh aperto, chiuso dal PUT
        self.assertEqual(kwargs['Metadata']['mtime'], str(s.st_mtime_ns))
        self.assertEqual(kwargs['Metadata']['mode'], oct(s.st_mode))

    def test_copy_symlink_zero_byte_con_target(self):
        link = os.path.join(self.tmp, 'link')
        os.symlink('note.md', link)
        dest = make_dest()
        self.assertTrue(dest.copy(link, 's3://bkt/safekeep/link'))
        args, kwargs = dest._client.put_object.call_args
        self.assertEqual(kwargs['Bucket'], 'bkt')
        self.assertEqual(kwargs['Key'], 'safekeep/link')
        self.assertEqual(kwargs['Body'], b'')
        self.assertEqual(kwargs['Metadata']['symlink'], 'note.md')

    def test_copy_sorgente_sparita_false(self):
        dest = make_dest()
        self.assertFalse(dest.copy(self.src + '.fantasma',
                                   's3://bkt/safekeep/x.md'))
        self.assertFalse(dest._client.put_object.called)


@SKIP
class ErrorMappingTest(unittest.TestCase):
    """SPEC §18.3: rete/5xx/throttling → ENODEV (pending), auth → EACCES."""

    def setUp(self):
        self.dest = make_dest()

    def call_raising(self, exc):
        def boom():
            raise exc
        with self.assertRaises(OSError) as cm:
            self.dest._call(boom)
        return cm.exception

    def test_dns_e_endpoint_irraggiungibile_enodev(self):
        e = self.call_raising(bce.EndpointConnectionError(
            endpoint_url='https://s3.example.com:443'))
        self.assertEqual(e.errno, errno.ENODEV)
        self.assertIn('irraggiungibile', str(e))

    def test_5xx_throttling_enodev_pending(self):
        for code, status in (('SlowDown', 503), ('ServiceUnavailable', 503),
                             ('InternalError', 500), ('503', 503)):
            with self.subTest(code=code):
                e = self.call_raising(client_error(code, status))
                self.assertEqual(e.errno, errno.ENODEV)

    def test_auth_errata_eacces_solo_log(self):
        for code, status in (('AccessDenied', 403),
                             ('InvalidAccessKeyId', 403),
                             ('SignatureDoesNotMatch', 403),
                             ('InvalidArgument', 400)):
            with self.subTest(code=code):
                e = self.call_raising(client_error(code, status))
                self.assertEqual(e.errno, errno.EACCES)

    def test_stato_offline_su_rete_morta(self):
        self.dest._client.head_bucket.side_effect = bce.EndpointConnectionError(
            endpoint_url='https://s3.example.com:443')
        self.assertEqual(self.dest.state(), 'offline')

    def test_head_object_404_none_gli_altri_mappati(self):
        self.dest._client.head_object.side_effect = client_error(404, 404)
        self.assertIsNone(self.dest._head_object('bkt', 'k'))
        self.dest._client.head_object.side_effect = client_error(403, 403)
        with self.assertRaises(OSError) as cm:
            self.dest._head_object('bkt', 'k')
        self.assertEqual(cm.exception.errno, errno.EACCES)


@SKIP
class StateCacheTest(unittest.TestCase):
    def test_head_bucket_in_cache_30s(self):
        dest = make_dest()
        self.assertEqual(dest.state(), 'ok')
        self.assertEqual(dest.state(), 'ok')
        self.assertEqual(dest._client.head_bucket.call_count, 1)
        dest._head_bucket(force=True)              # doctor usa la stessa via
        self.assertEqual(dest._client.head_bucket.call_count, 2)

    def test_offline_in_cache_anche_lui(self):
        dest = make_dest()
        dest._client.head_bucket.side_effect = bce.EndpointConnectionError(
            endpoint_url='https://x')
        self.assertEqual(dest.state(), 'offline')
        self.assertEqual(dest.state(), 'offline')
        self.assertEqual(dest._client.head_bucket.call_count, 1)

    def test_cache_scaduta_dopo_ttl(self):
        dest = make_dest()
        fake = mock.Mock()
        fake.monotonic.side_effect = [100.0, 100.0,       # state: now, stamp
                                      100.5,              # state: ora in cache
                                      131.0, 131.0]       # state: TTL scaduto
        with mock.patch('safekeep.s3dest.time', fake):
            self.assertEqual(dest.state(), 'ok')    # riempie la cache
            self.assertEqual(dest.state(), 'ok')    # dentro TTL → cache
            self.assertEqual(dest.state(), 'ok')    # TTL scaduto → nuovo HEAD
        self.assertEqual(dest._client.head_bucket.call_count, 2)

    def test_endpoint_label(self):
        self.assertEqual(make_dest().endpoint_label, 'https://s3.example.com:443')
        self.assertEqual(make_dest(endpoint='').endpoint_label, 'AWS default')


class ExtraEMissingTest(unittest.TestCase):
    """Extra mancante: S3ExtraMissing(ConfigError) con hint (SPEC §18) —
    gira ANCHE senza botocore (è il caso d'uso)."""

    def test_s3extramissing_e_configerror(self):
        self.assertTrue(issubclass(S3ExtraMissing, ConfigError))

    def test_costruzione_senza_botocore_hint(self):
        saved = {k: v for k, v in list(sys.modules.items())
                 if k == 'botocore' or k.startswith('botocore.')}
        for k in saved:
            del sys.modules[k]
        try:
            sys.modules['botocore'] = None          # import → ImportError
            with mock.patch('safekeep.s3dest._BC', None):
                with self.assertRaises(S3ExtraMissing) as cm:
                    S3Destination(make_cfg())
                msg = str(cm.exception)
                self.assertIn('safekeep[s3]', msg)
                self.assertIn('pipx install', msg)
                self.assertFalse(have_botocore())
        finally:
            for k in [k for k in sys.modules if k == 'botocore'
                      or k.startswith('botocore.')]:
                del sys.modules[k]
            sys.modules.update(saved)

    @unittest.skipUnless(HAVE_BOTOCORE, 'botocore non installato')
    def test_have_botocore_verde_con_extra(self):
        self.assertTrue(have_botocore())


class SecretPermsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.cfgfile = os.path.join(self.tmp, 'safekeep')
        with open(self.cfgfile, 'w') as f:
            f.write('x\n')

    def test_permessi_larghi_insecure(self):
        os.chmod(self.cfgfile, 0o644)
        self.assertTrue(secret_perms_insecure(self.cfgfile, make_cfg()))

    def test_chmod_600_ok(self):
        os.chmod(self.cfgfile, 0o600)
        self.assertFalse(secret_perms_insecure(self.cfgfile, make_cfg()))

    def test_secret_da_env_non_controllato(self):
        os.chmod(self.cfgfile, 0o644)
        cfg = make_cfg(secret_key='risolto', secret_env='VAR')
        self.assertFalse(secret_perms_insecure(self.cfgfile, cfg))


@SKIP
class IterKeysDownloadTest(unittest.TestCase):
    """SPEC §19: iter_keys (list paginato + metadata) e download (tmp+replace,
    chmod/utime da x-amz-meta-*, symlink da x-amz-meta-symlink)."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def test_iter_keys_paginato_con_metadata(self):
        dest = make_dest()
        dest._client.list_objects_v2.side_effect = [
            {'Contents': [{'Key': 'safekeep/a.md', 'Size': 3}],
             'NextContinuationToken': 't1'},
            {'Contents': [{'Key': 'safekeep/link.md', 'Size': 0}],
             'IsTruncated': False},
        ]
        dest._client.head_object.side_effect = [
            {'ContentLength': 3, 'Metadata': {'mtime': '1'}},
            {'ContentLength': 0, 'Metadata': {'symlink': 'a.md'}},
        ]
        got = list(dest.iter_keys('safekeep/'))
        self.assertEqual(got, [('safekeep/a.md', 3, {'mtime': '1'}),
                               ('safekeep/link.md', 0, {'symlink': 'a.md'})])
        self.assertEqual(dest._client.list_objects_v2.call_count, 2)
        second = dest._client.list_objects_v2.call_args_list[1][1]
        self.assertEqual(second['ContinuationToken'], 't1')

    def test_iter_keys_key_sparito_saltata(self):
        dest = make_dest()
        dest._client.list_objects_v2.return_value = {
            'Contents': [{'Key': 'safekeep/x.md', 'Size': 1}]}
        dest._client.head_object.side_effect = client_error(404, 404)
        self.assertEqual(list(dest.iter_keys('safekeep/')), [])

    def test_download_file_contenuto_mtime_mode(self):
        dest = make_dest()
        ns = 1_700_000_000_123_456_789
        dest._client.get_object.return_value = {
            'Metadata': {'mtime': str(ns), 'mode': '0o100640'},
            'Body': io.BytesIO(b'contenuto backup')}
        dst = os.path.join(self.tmp, 'sub', 'a.md')
        self.assertTrue(dest.download('safekeep/a.md', dst))
        with open(dst, 'rb') as f:
            self.assertEqual(f.read(), b'contenuto backup')
        st = os.stat(dst)
        self.assertEqual(st.st_mtime_ns, ns)
        self.assertEqual(st.st_mode & 0o777, 0o640)
        kwargs = dest._client.get_object.call_args[1]
        self.assertEqual(kwargs, {'Bucket': 'bkt', 'Key': 'safekeep/a.md'})

    def test_download_symlink_da_metadata(self):
        dest = make_dest()
        dest._client.get_object.return_value = {
            'Metadata': {'symlink': 'a.md', 'mtime': '1', 'mode': '0o120777'},
            'Body': io.BytesIO(b'')}
        dst = os.path.join(self.tmp, 'link.md')
        self.assertTrue(dest.download('safekeep/link.md', dst))
        self.assertTrue(os.path.islink(dst))
        self.assertEqual(os.readlink(dst), 'a.md')

    def test_download_errore_rete_oserror_e_niente_residui(self):
        dest = make_dest()
        dest._client.get_object.side_effect = bce.EndpointConnectionError(
            endpoint_url='https://x')
        dst = os.path.join(self.tmp, 'y.md')
        with self.assertRaises(OSError) as cm:
            dest.download('safekeep/y.md', dst)
        self.assertEqual(cm.exception.errno, errno.ENODEV)
        self.assertFalse(os.path.exists(dst))
        self.assertEqual(os.listdir(self.tmp), [])


if __name__ == '__main__':
    unittest.main()
