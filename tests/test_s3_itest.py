"""Integrazione reale con un bucket S3-compatible (SPEC.md §18).

**Saltato** se mancano le env vars: NESSUNA credenziale è hardcodata qui
(le credenziali vivono solo nell'ambiente di chi lancia il test).

    SAFEKEEP_S3_TEST_ENDPOINT=...  SAFEKEEP_S3_TEST_BUCKET=... \
    SAFEKEEP_S3_TEST_ACCESS_KEY=...  SAFEKEEP_S3_TEST_SECRET_KEY=... \
    [SAFEKEEP_S3_TEST_REGION=us-east-1] [SAFEKEEP_S3_TEST_PREFIX=safekeep] \
    python3 -m unittest tests.test_s3_itest -v

Flusso: config temp col blocco `s3_*` → reconcile di un mini-progetto →
HeadObject verifica key + metadata (mtime ns, mode, symlink) → secondo
reconcile → 0 copie → file toccato → 1 copia.

**NESSUNA DeleteObject**: il prefix è monouso (timestamp + pid), gli oggetti
creati dal test restano nel bucket.
"""
import os
import shutil
import tempfile
import unittest
from datetime import datetime, timezone

from safekeep.daemon import Daemon

ENV = 'SAFEKEEP_S3_TEST_'
REQUIRED = ('ENDPOINT', 'BUCKET', 'ACCESS_KEY', 'SECRET_KEY')
MISSING = [k for k in REQUIRED if not os.environ.get(ENV + k)]

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


@unittest.skipIf(MISSING,
                 f'mancano env {ENV}*: {MISSING or "-"} — test di integrazione saltato')
@unittest.skipUnless(HAVE_BOTOCORE, 'botocore non installato (extra safekeep[s3])')
class S3IntegrazioneTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

        # prefix monouso: nessuna DeleteObject, nessuna rischio su dati reali
        stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
        base = (os.environ.get(ENV + 'PREFIX') or 'safekeep-itest').strip('/')
        self.prefix = f'{base}/itest-{stamp}-{os.getpid()}'

        self.src = os.path.join(self.tmp, 'src')
        proj = os.path.join(self.src, 'proj')
        write(os.path.join(proj, '.sync'), 'include: *.md\n')
        write(os.path.join(proj, 'a.md'), 'contenuto v1')
        os.symlink('a.md', os.path.join(proj, 'link.md'))

        self.cfg_path = write(
            os.path.join(self.tmp, 'safekeep'),
            f'source: {self.src}\n'
            f's3_endpoint: {os.environ[ENV + "ENDPOINT"]}\n'
            f's3_bucket: {os.environ[ENV + "BUCKET"]}\n'
            f's3_prefix: {self.prefix}\n'
            f's3_region: {os.environ.get(ENV + "REGION", "us-east-1")}\n'
            f's3_access_key: {os.environ[ENV + "ACCESS_KEY"]}\n'
            f's3_secret_key: {os.environ[ENV + "SECRET_KEY"]}\n'
            f's3_addressing: path\n')
        os.chmod(self.cfg_path, 0o600)

        self.daemon = Daemon(self.cfg_path)
        self.daemon.load_config()
        self.daemon.load_projects()
        self.assertEqual(len(self.daemon.projects), 1)

    def key(self, rel):
        return f'{self.prefix}/proj/{rel}'

    def head(self, rel):
        """HeadObject reale: (size, metadata) oppure None se assente."""
        return self.daemon.s3._head_object(
            os.environ[ENV + 'BUCKET'], self.key(rel))

    def sync(self):
        rows = self.daemon.sync_once()
        self.assertEqual(len(rows), 1)
        project, stats, _ = rows[0]
        self.assertEqual(stats.get('errati', 0), 0,
                         f'errori durante il reconcile: {stats}')
        return stats

    def test_flusso_upload_idempotenza_e_ricopia(self):
        # 1) primo reconcile: oggetti creati
        self.assertEqual(self.daemon.s3.state(), 'ok',
                         'HeadBucket: il bucket va raggiungibile')
        stats = self.sync()
        self.assertEqual(stats.get('copiati', 0), 3, stats)   # .sync, a.md, link.md

        # 2) HeadObject: key giusta + metadata mtime/mode/symlink
        proj = os.path.join(self.src, 'proj')
        src = os.path.join(proj, 'a.md')
        st = os.stat(src)
        size, md = self.head('a.md')
        self.assertEqual(size, st.st_size)
        self.assertEqual(int(md['mtime']), st.st_mtime_ns)     # ns, §18.2
        self.assertEqual(int(md['mode'], 8), st.st_mode)

        link_size, link_md = self.head('link.md')
        self.assertEqual(link_size, 0, 'symlink = object zero-byte')
        self.assertEqual(link_md.get('symlink'), 'a.md')

        sync_size, _ = self.head('.sync')
        self.assertGreater(sync_size, 0)

        # 3) secondo reconcile: tutto aggiornato → 0 copie (idempotenza)
        stats = self.sync()
        self.assertEqual(stats.get('copiati', 0), 0, stats)
        self.assertEqual(stats.get('skippati', 0), 3, stats)

        # 4) tocco il file (size diverso) → 1 sola copia
        write(src, 'contenuto v2 piu lungo di prima')
        stats = self.sync()
        self.assertEqual(stats.get('copiati', 0), 1, stats)
        size, md = self.head('a.md')
        self.assertEqual(size, os.stat(src).st_size)
        self.assertEqual(int(md['mtime']), os.stat(src).st_mtime_ns)


if __name__ == '__main__':
    unittest.main()
