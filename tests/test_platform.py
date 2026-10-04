"""Helper di piattaforma e agent systemd (SPEC.md §14).

Entrambi i rami (Darwin/Linux) si esercitano patchando `sys.platform`, così la
suite copre il design di portabilità anche girando su una sola piattaforma.
"""
import os
import shutil
import subprocess
import tempfile
import unittest
from datetime import datetime, timedelta
from unittest import mock

from safekeep import platform as plat

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class MonitorTest(unittest.TestCase):
    def test_monitor_dichiarato_per_piattaforma(self):
        with mock.patch('sys.platform', 'darwin'):
            self.assertEqual(plat.fswatch_monitor(), 'fsevents_monitor')
        with mock.patch('sys.platform', 'linux'):
            self.assertEqual(plat.fswatch_monitor(), 'inotify_monitor')

    def test_monitor_atteso_e_in_fswatch_M(self):
        """Guardrail contro il nome sbagliato: `-m inotify` (senza `_monitor`)
        non è un alias accettato da fswatch — qui si verifica che il nome che
        passiamo in `-m` sia proprio uno di quelli che `fswatch -M` elenca."""
        exe = shutil.which('fswatch')
        if exe is None:
            self.skipTest('fswatch non installato')
        out = subprocess.run([exe, '-M'], capture_output=True, text=True,
                             timeout=15).stdout
        self.assertIn(plat.fswatch_monitor(), out)


class InitSystemTest(unittest.TestCase):
    def test_init_system_per_piattaforma(self):
        with mock.patch('sys.platform', 'darwin'):
            self.assertEqual(plat.init_system(), 'launchd')
        with mock.patch('sys.platform', 'linux'):
            self.assertEqual(plat.init_system(), 'systemd')

    def test_is_darwin_e_is_linux(self):
        with mock.patch('sys.platform', 'darwin'):
            self.assertTrue(plat.is_darwin())
            self.assertFalse(plat.is_linux())
        with mock.patch('sys.platform', 'linux'):
            self.assertTrue(plat.is_linux())
            self.assertFalse(plat.is_darwin())


class InstallHintTest(unittest.TestCase):
    def test_darwin_usa_brew(self):
        with mock.patch('sys.platform', 'darwin'):
            self.assertEqual(plat.install_hint(), 'brew install fswatch')

    def test_linux_rileva_il_package_manager(self):
        casi = (('pacman', 'pacman -S fswatch'),
                ('apt-get', 'sudo apt-get install fswatch'),
                ('dnf', 'sudo dnf install fswatch'))
        for pm, atteso in casi:
            with self.subTest(pm=pm), \
                    mock.patch('sys.platform', 'linux'), \
                    mock.patch('shutil.which',
                               side_effect=lambda name, pm=pm:
                               '/usr/bin/' + name if name == pm else None):
                self.assertEqual(plat.install_hint(), atteso)

    def test_linux_senza_package_manager_noto(self):
        with mock.patch('sys.platform', 'linux'), \
                mock.patch('shutil.which', return_value=None):
            self.assertIn('fswatch', plat.install_hint())


class InotifyLimitTest(unittest.TestCase):
    """SPEC.md §14.4: check informativo non fatale sul limite di watch."""

    def _fake(self, text):
        fd, path = tempfile.mkstemp()
        self.addCleanup(os.unlink, path)
        with os.fdopen(fd, 'w') as fh:
            fh.write(text)
        return path

    def test_lettura_da_file_finto(self):
        self.assertEqual(plat.inotify_limit(self._fake('524288\n')), 524288)
        self.assertEqual(plat.inotify_limit(self._fake('8192')), 8192)

    def test_valore_invalido_o_file_mancante(self):
        self.assertIsNone(plat.inotify_limit(self._fake('non-è-un-numbero\n')))
        self.assertIsNone(plat.inotify_limit('/proc/assente/inotify'))

    def test_costanti(self):
        self.assertEqual(plat.INOTIFY_MIN_WATCHES, 16384)
        self.assertEqual(plat.INOTIFY_LIMIT_PATH,
                         '/proc/sys/fs/inotify/max_user_watches')


class PiattaformaCentralizzataTest(unittest.TestCase):
    def test_sys_platform_letto_solo_nell_helper(self):
        """SPEC.md §14.1: un solo posto legge `sys.platform`, niente check sparsi."""
        hits = []
        pkg = os.path.join(REPO, 'safekeep')
        for name in sorted(os.listdir(pkg)):
            if not name.endswith('.py'):
                continue
            with open(os.path.join(pkg, name), encoding='utf-8') as fh:
                for i, line in enumerate(fh, 1):
                    if 'sys.platform' in line:
                        hits.append(f'{name}:{i}')
        self.assertTrue(hits, 'nessun riferimento a sys.platform trovato')
        self.assertTrue(all(h.startswith('platform.py:') for h in hits), hits)


class AgentTemplateTest(unittest.TestCase):
    """SPEC.md §14.3: la unit systemd è un template con gli stessi placeholder
    del plist e copre RunAtLoad/KeepAlive."""

    def test_unit_systemd_template(self):
        path = os.path.join(REPO, 'systemd', 'safekeep.service')
        with open(path, encoding='utf-8') as fh:
            text = fh.read()
        for needle in ('ExecStart=__PYTHON__ __REPO__/bin/safekeep.py run',
                       'Restart=always',
                       'RestartSec=2',
                       'StartLimitIntervalSec=0',
                       'WantedBy=default.target'):
            self.assertIn(needle, text, needle)
        self.assertNotIn('/Users/', text, 'path assoluto nel template')

    def test_gli_script_si_ramificano_per_os(self):
        for name in ('install.sh', 'uninstall.sh'):
            with self.subTest(script=name), \
                    open(os.path.join(REPO, name), encoding='utf-8') as fh:
                text = fh.read()
            self.assertIn('uname -s', text, name)          # ramo Darwin/Linux
            self.assertIn('launchctl', text, name)         # ramo macOS invariato
            self.assertIn('systemctl --user', text, name)  # ramo Linux

    def test_il_plist_macos_non_e_toccato(self):
        with open(os.path.join(REPO, 'launchd', 'com.safekeep.agent.plist'),
                  encoding='utf-8') as fh:
            text = fh.read()
        for needle in ('RunAtLoad', 'KeepAlive', '__REPO__', '__PYTHON__'):
            self.assertIn(needle, text, needle)


class RecentPermissionErrorsTest(unittest.TestCase):
    """SPEC.md §14.4: errori di permesso recenti nel log, finestra 24h,
    non fatale; righe senza timestamp sicuro non vengono contate."""

    NOW = datetime(2026, 10, 4, 20, 0, 0)

    def _log(self, text):
        fd, path = tempfile.mkstemp(suffix='.log')
        self.addCleanup(os.unlink, path)
        with os.fdopen(fd, 'w') as fh:
            fh.write(text)
        return path

    @staticmethod
    def _stamp(dt):
        return dt.strftime('%Y-%m-%d %H:%M:%S,000')   # formato %(asctime)s

    def test_ricente_vecchio_e_normale_nello_stesso_log(self):
        text = (
            f'{self._stamp(self.NOW - timedelta(hours=2))} ERROR copia fallita '
            "a → b: [Errno 1] Operation not permitted: '/x.tmp.1'\n"
            f'{self._stamp(self.NOW - timedelta(days=3))} ERROR copia fallita '
            "a → b: [Errno 13] Permission denied: '/y'\n"
            f'{self._stamp(self.NOW - timedelta(minutes=5))} INFO reconcile: 0 copie\n'
        )
        count, last = plat.recent_permission_errors(self._log(text), now=self.NOW)
        self.assertEqual(count, 1, 'solo la riga nelle ultime 24h')
        self.assertEqual(last, self.NOW - timedelta(hours=2))

    def test_log_assente(self):
        self.assertIsNone(
            plat.recent_permission_errors('/non/esiste/safekeep.log'),
            'nessun log ⇒ check saltato, non warning')

    def test_timestamp_illeggibile_non_e_contato(self):
        text = ("senza-data ERROR [Errno 1] Operation not permitted: '/x'\n"
                + f'{self._stamp(self.NOW - timedelta(hours=1))} ERROR '
                  "[Errno 1] Operation not permitted: '/y'\n"
                + f'{self._stamp(self.NOW + timedelta(hours=1))} ERROR '
                  "[Errno 1] Operation not permitted: '/z'\n")
        count, _ = plat.recent_permission_errors(self._log(text), now=self.NOW)
        self.assertEqual(count, 1, 'non sicuri (o clock skew) ⇒ non contiamo')

    def test_copre_eperm_e_eacces(self):
        for riga in ('[Errno 1] Operation not permitted',
                     'qualcosa Operation not permitted',
                     '[Errno 13] Permission denied',
                     'qualcosa Permission denied'):
            with self.subTest(riga=riga):
                self.assertIsNotNone(plat.PERM_ERR_RE.search(riga))
        self.assertIsNone(plat.PERM_ERR_RE.search('INFO reconcile: 0 copie'))


class LingerTest(unittest.TestCase):
    """SPEC.md §14.4: linger systemd, directory finta al posto di
    /var/lib/systemd/linger."""

    def _dir(self, *entries):
        path = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, path, ignore_errors=True)
        for name in entries:
            open(os.path.join(path, name), 'w').close()
        return path

    def test_abilitato_disabilitato_e_dir_assente(self):
        d = self._dir('utente')
        self.assertTrue(plat.linger_enabled(user='utente', linger_dir=d))
        self.assertFalse(plat.linger_enabled(user='altro', linger_dir=d))
        self.assertIsNone(
            plat.linger_enabled(user='utente',
                                linger_dir=os.path.join(os.sep, 'niente', 'qui')),
            'niente systemd ⇒ non determinabile, check saltato')

    def test_user_default_e_costante(self):
        self.assertEqual(plat.LINGER_DIR, '/var/lib/systemd/linger')
        with mock.patch('getpass.getuser', return_value='chi'):
            self.assertTrue(plat.linger_enabled(linger_dir=self._dir('chi')))
            self.assertFalse(plat.linger_enabled(linger_dir=self._dir()))


if __name__ == '__main__':
    unittest.main()
