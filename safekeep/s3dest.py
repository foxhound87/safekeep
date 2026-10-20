"""Destinazione S3-compatible (SPEC.md §18): client botocore lazy, key
mapping, metadata (mtime ns/mode/symlink) e mapping errori → pending.

Solo upload + needs_copy: **nessuna cancellazione** (§18.2), nessun
versioning, una sola dest S3. botocore arriva dall'extra `safekeep[s3]` e
viene importato SOLO qui, alla prima costruzione di `S3Destination` — senza
`s3_*` in config il runtime resta stdlib-only.
"""
import errno
import logging
import os
import time

from .config import ConfigError, S3Config
from .copier import MTIME_TOLERANCE, _stable

log = logging.getLogger('safekeep')

HEAD_CACHE_TTL = 30.0        # secondi di cache del HeadBucket (§18.3)
CONNECT_TIMEOUT = 10         # secondi: un PUT/Head lento non blocca per sempre
READ_TIMEOUT = 60
MAX_ATTEMPTS = 2             # tentativi botocore totali (rete, non logica)
# codici ClientError che dicono "riprova dopo" (throttling/5xx) → ENODEV
RETRYABLE_CODES = frozenset({
    'SlowDown', 'Throttling', 'ThrottlingException', 'RequestTimeout',
    'RequestTimeoutException', 'InternalError', 'ServiceUnavailable', '503',
})

_BC = None                   # cache dell'import lazy: (session, config, exceptions)


class S3ExtraMissing(ConfigError):
    """Config `s3_*` presente ma l'extra `safekeep[s3]` (botocore) mancante.

    Subclass di ConfigError: `run`/`sync-once`/`status` escono ≠ 0 con hint;
    `doctor` la tratta come warning non fatale (SPEC.md §18).
    """


def _import_botocore():
    """Import lazy + cached di botocore → (botocore.session, .config, .exceptions)."""
    global _BC
    if _BC is None:
        try:
            import botocore.session
            import botocore.config
            import botocore.exceptions
        except ImportError as e:
            raise S3ExtraMissing(
                'dest S3 configurata ma botocore non importabile: '
                f'{e} — installa l\'extra: pipx install "safekeep[s3]" '
                '(SPEC.md §18.1)') from None
        _BC = (botocore.session, botocore.config, botocore.exceptions)
    return _BC


def have_botocore():
    try:
        _import_botocore()
    except S3ExtraMissing:
        return False
    return True


def secret_perms_insecure(config_path, scfg: S3Config) -> bool:
    """True se il secret inline è nel file E i permessi del file sono > 0600
    (SPEC.md §18.4). Un secret da variabile d'env (`s3_secret_key_env`) non
    sta nel file: nessun controllo."""
    if scfg.secret_env or not scfg.secret_key:
        return False
    try:
        return (os.stat(config_path).st_mode & 0o777) > 0o600
    except OSError:
        return False


class S3Destination:
    """Una destinazione S3-compatible: `state()`, `needs_copy()`, `copy()`.

    La semantica replica le dest fs (SPEC.md §18.2): idempotenza via
    (size, mtime ns con tolleranza 1s), symlink = object zero-byte col
    target in `x-amz-meta-symlink`, stability check invariato.
    """

    def __init__(self, scfg: S3Config):
        _, _, exc = _import_botocore()      # fail-fast se l'extra manca (§18)
        self.cfg = scfg
        self._exc = exc
        self._client = None
        self._head = (0.0, None)            # (monotonic, esito HeadBucket | None)
        self.root = 's3://' + scfg.bucket + (
            '/' + scfg.prefix if scfg.prefix else '')

    # --- client ----------------------------------------------------------

    def client(self):
        if self._client is None:
            session, botoconfig, _ = _import_botocore()
            kwargs = dict(
                region_name=self.cfg.region,
                config=botoconfig.Config(
                    signature_version='s3v4',        # SIGV4 = Signature Version 4
                    connect_timeout=CONNECT_TIMEOUT,
                    read_timeout=READ_TIMEOUT,
                    retries={'max_attempts': MAX_ATTEMPTS},
                    s3={'addressing_style': self.cfg.addressing},
                ),
                verify=self.cfg.tls_check,           # false = self-signed (§18.1)
            )
            if self.cfg.endpoint:
                kwargs['endpoint_url'] = self.cfg.endpoint
            self._client = session.get_session().create_client(
                's3',
                aws_access_key_id=self.cfg.access_key,
                aws_secret_access_key=self.cfg.secret_key,
                **kwargs)
        return self._client

    # --- errori (SPEC.md §18.3) ------------------------------------------

    def _oserror(self, e):
        """botocore → OSError: rete/5xx/throttling → ENODEV (pending §8.3),
        auth/richiesta malformata → EACCES (solo log, come una fs non
        leggibile). Ogni eccezione botocore diventa OSError: `reconcile_project`
        confina gli OSError per-file, così un errore S3 non abortisce mai il
        walk né fa crashare il daemon."""
        if isinstance(e, self._exc.ClientError):
            resp = getattr(e, 'response', {}) or {}
            status = (resp.get('ResponseMetadata') or {}).get('HTTPStatusCode', 0)
            code = str((resp.get('Error') or {}).get('Code', ''))
            if status >= 500 or code in RETRYABLE_CODES:
                raise OSError(errno.ENODEV, f'S3: {code or status}: {e}') from None
            raise OSError(errno.EACCES, f'S3: {code or status}: {e}') from None
        # BotoCoreError: timeout, DNS/endpoint irraggiungibile, conn. chiusa…
        raise OSError(errno.ENODEV, f'S3 irraggiungibile: {e}') from None

    def _call(self, fn, *args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except self._exc.BotoCoreError as e:
            self._oserror(e)
        except self._exc.ClientError as e:
            self._oserror(e)

    # --- HeadBucket con cache (§18.3) ------------------------------------

    def state(self) -> str:
        """`ok` se il bucket è raggiungibile, `offline` altrimenti (cache 30s)."""
        return 'ok' if self._head_bucket() else 'offline'

    def _head_bucket(self, force=False):
        now = time.monotonic()
        cached = self._head[1]
        if not force and cached is not None and now - self._head[0] < HEAD_CACHE_TTL:
            return cached
        try:
            self._call(self.client().head_bucket, Bucket=self.cfg.bucket)
        except OSError as e:
            log.debug('HeadBucket %s fallito: %s', self.cfg.bucket, e)
            ok = False
        else:
            ok = True
        self._head = (time.monotonic(), ok)
        return ok

    @property
    def endpoint_label(self) -> str:
        return self.cfg.endpoint or 'AWS default'

    # --- key mapping (SPEC.md §18.2) -------------------------------------

    def dest_path(self, dest_root, source_root, relpath, layout):
        """Path dest `s3://bucket/<prefix>/<rel>` — specchio di
        `config.dest_path` con separatori `/` (nessun normpath che collassa
        `://`). Stesso identico calcolo delle dest fs: `layout` globale,
        radice = la `source` che contiene il progetto (§3)."""
        if layout not in ('relative', 'full'):
            raise ValueError(f'layout non valido: {layout!r} (relative|full)')
        src = os.path.abspath(os.path.expanduser(source_root))
        p = relpath if os.path.isabs(relpath) else os.path.join(src, relpath)
        p = os.path.abspath(os.path.expanduser(p))
        rel = os.path.relpath(p, os.sep) if layout == 'full' else os.path.relpath(p, src)
        rel = rel.replace(os.sep, '/').lstrip('/')
        base = dest_root.rstrip('/')
        return f'{base}/{rel}' if rel and rel != '.' else base

    @staticmethod
    def _parse(dst):
        """`s3://bucket/key…` → (bucket, key…)."""
        rest = dst[len('s3://'):]
        bucket, sep, key = rest.partition('/')
        return bucket, key

    # --- HeadObject / needs_copy (§18.2) ---------------------------------

    def _head_object(self, bucket, key):
        """(ContentLength, metadata) dell'oggetto, oppure None se assente."""
        try:
            r = self.client().head_object(Bucket=bucket, Key=key)
        except self._exc.ClientError as e:
            resp = getattr(e, 'response', {}) or {}
            status = (resp.get('ResponseMetadata') or {}).get('HTTPStatusCode', 0)
            code = str((resp.get('Error') or {}).get('Code', ''))
            if status == 404 or code in ('404', 'NoSuchKey', 'NotFound'):
                return None                  # non esiste → va copiato
            self._oserror(e)
        except self._exc.BotoCoreError as e:
            self._oserror(e)
        # Silo/MinIO restituiscono le x-amz-meta-* con case del server
        # canonizzato ("Mtime", "Mode") → chiavi lowercase a lettura
        return r['ContentLength'], {k.lower(): v
                                    for k, v in (r.get('Metadata') or {}).items()}

    def needs_copy(self, src, dst) -> bool:
        """True se l'oggetto manca o (size, mtime ns) diversi — tolleranza
        1s (`MTIME_TOLERANCE`), stessa semantica di `copier.needs_copy`."""
        try:
            s = os.stat(src)
        except FileNotFoundError:
            return False                      # niente da copiare
        bucket, key = self._parse(dst)
        head = self._head_object(bucket, key)
        if head is None:
            return True
        size, md = head
        if os.path.islink(src):
            # symlink = object zero-byte + target in metadata (mai seguito)
            try:
                target = os.readlink(src)
            except OSError:
                return False                  # sparito fra stat e readlink
            return md.get('symlink') != target
        if size != s.st_size:
            return True
        try:
            return abs(s.st_mtime_ns - int(md.get('mtime'))) > int(MTIME_TOLERANCE * 1e9)
        except (TypeError, ValueError):
            return True                       # metadata assente/corrotto → copia

    # --- copy (§18.2) ----------------------------------------------------

    def _put(self, bucket, key, body, metadata):
        self._call(self.client().put_object, Bucket=bucket, Key=key,
                   Body=body, Metadata=metadata)

    def copy(self, src, dst) -> bool:
        """Copia src → object. True se copiato, False se da ritentare più
        tardi (sparito o instabile) — stessa semantica di `copier.copy_one`.

        Un object-PUT è atomico di per sé: niente tmp, niente replace.
        """
        bucket, key = self._parse(dst)
        if os.path.islink(src):
            try:
                target = os.readlink(src)
                s = os.lstat(src)
            except OSError:
                return False
            self._put(bucket, key, b'', {'symlink': target,
                                         'mtime': str(s.st_mtime_ns),
                                         'mode': oct(s.st_mode)})
            log.debug('symlink %s → s3://%s/%s (target %r)', src, bucket, key, target)
            return True
        if not _stable(src):
            return False                      # instabile o sparito
        try:
            s = os.stat(src)
            fh = open(src, 'rb')
        except FileNotFoundError:
            return False                      # sparito durante la copia
        with fh:
            # ponytail: PUT singolo in streaming, no multipart — file > 5 GB
            # falliscono (EntityTooLarge); se serviranno, il rimedio è
            # s3transfer (extra `safekeep[s3]`) col TransferManager (§18.5)
            self._put(bucket, key, fh,
                      {'mtime': str(s.st_mtime_ns), 'mode': oct(s.st_mode)})
        log.debug('copiato %s → s3://%s/%s', src, bucket, key)
        return True
