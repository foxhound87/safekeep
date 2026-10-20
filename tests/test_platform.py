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

    def test_install_sh_interprete_stabile(self):
        """SPEC.md §8.4 (0.5.1): il default renderizzato è lo shim di sistema
        /usr/bin/python3, MAI un path Cellar di Homebrew — un `brew upgrade`
        cancella il Cellar sotto il processo vivo e TCC non lo identifica più."""
        with open(os.path.join(REPO, 'install.sh'), encoding='utf-8') as fh:
            text = fh.read()
        self.assertIn('SAFEKEEP_PYTHON:-/usr/bin/python3', text)
        self.assertNotIn('/opt/homebrew/bin/python3', text,
                         'il default non deve più puntare al Cellar di brew')
        self.assertIn('py_ok', text, "validazione ≥ 3.9 dell'interprete")
        for name in ('com.safekeep.agent.plist', 'com.safekeep.trend.plist'):
            with open(os.path.join(REPO, 'launchd', name), encoding='utf-8') as fh:
                tpl = fh.read()
            self.assertIn('default: /usr/bin/python3', tpl, name)


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

    def test_suffisso_ripetuto_del_throttle_contato_come_N_piu_1(self):
        """SPEC.md §10.1/§14.4 (0.5.1): una riga `— ripetuto N volte`
        rappresenta se stessa + i N fallimenti compressi → count_24h resta il
        numero VERO di errori anche con le righe throttolate."""
        text = (
            f'{self._stamp(self.NOW - timedelta(hours=2))} ERROR copia fallita '
            "a → b: [Errno 1] Operation not permitted: '/x.tmp.1'\n"
            f'{self._stamp(self.NOW - timedelta(hours=1))} ERROR copia fallita '
            "a → b: [Errno 1] Operation not permitted: '/x.tmp.1' — ripetuto 49 volte\n"
        )
        count, last = plat.recent_permission_errors(self._log(text), now=self.NOW)
        self.assertEqual(count, 1 + 1 + 49, 'riga base + (riga suffisso + 49)')
        self.assertEqual(last, self.NOW - timedelta(hours=1))

    def test_riga_senza_suffisso_restano_una_ciascuna(self):
        text = (f'{self._stamp(self.NOW - timedelta(hours=2))} ERROR copia fallita '
                "a → b: [Errno 1] Operation not permitted: '/x'\n"
                f'{self._stamp(self.NOW - timedelta(hours=1))} ERROR copia fallita '
                "a → b: [Errno 1] Operation not permitted: '/y'\n")
        count, _ = plat.recent_permission_errors(self._log(text), now=self.NOW)
        self.assertEqual(count, 2)


class DaemonExeTest(unittest.TestCase):
    """SPEC.md §14.4 (0.5.1): exe del processo launchd, None se non
    determinabile — Darwin-only, solo lettura, casi finti."""

    LAUNCHCTL_OK = subprocess.CompletedProcess(
        [], 0, stdout='\tstate = running\n\tpid = 4242\n', stderr='')
    PS_OK = subprocess.CompletedProcess(
        [], 0, stdout='/opt/shim/python3\n', stderr='')

    def run_mock(self, results):
        with mock.patch('sys.platform', 'darwin'), \
                mock.patch.object(plat.subprocess, 'run',
                                  side_effect=results) as run:
            return plat.daemon_exe(), run

    def test_non_darwin_ritorna_none_senza_lanciare_niente(self):
        with mock.patch('sys.platform', 'linux'), \
                mock.patch.object(plat.subprocess, 'run') as run:
            self.assertIsNone(plat.daemon_exe())
            run.assert_not_called()

    def test_job_non_caricato_o_senza_pid(self):
        for result in (subprocess.CompletedProcess([], 1, stdout='slot absent', stderr=''),
                       subprocess.CompletedProcess([], 0, stdout='state = not running\n',
                                                   stderr='')):
            with self.subTest(result=result.stdout):
                exe, run = self.run_mock([result])
                self.assertIsNone(exe)
        # job ok ma `ps` fallito → non determinabile
        exe, _ = self.run_mock([self.LAUNCHCTL_OK,
                                subprocess.CompletedProcess([], 1, stdout='', stderr='x')])
        self.assertIsNone(exe)

    def test_launchctl_o_ps_assenti(self):
        exe, run = self.run_mock([OSError('launchctl mancante')])
        self.assertIsNone(exe)

    def test_eseguibile_letto_da_ps(self):
        exe, run = self.run_mock([self.LAUNCHCTL_OK, self.PS_OK])
        self.assertEqual(exe, '/opt/shim/python3')
        self.assertEqual(run.call_args_list[1].args[0][:4],
                         ['ps', '-p', '4242', '-o'])

    def test_ps_senza_output(self):
        exe, _ = self.run_mock([self.LAUNCHCTL_OK,
                                subprocess.CompletedProcess([], 0, stdout='\n',
                                                            stderr='')])
        self.assertIsNone(exe)


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


class WslTest(unittest.TestCase):
    """SPEC.md §17.1: rilevamento WSL via env o kernel, letto a chiamata."""

    def test_env_wsl_distro_name(self):
        with mock.patch.dict(os.environ, {'WSL_DISTRO_NAME': 'Ubuntu'}):
            self.assertTrue(plat.is_wsl())
            self.assertEqual(plat.wsl_distro(), 'Ubuntu')

    def test_kernel_microsoft_senza_env(self):
        for release in ('5.15.167.4-microsoft-standard-WSL2',   # WSL2
                        '4.4.0-19041-Microsoft'):              # WSL1 (maiuscolo)
            with self.subTest(release=release), \
                    mock.patch.dict(os.environ, {'WSL_DISTRO_NAME': ''}), \
                    mock.patch.object(plat, '_platform') as p:
                p.release.return_value = release
                self.assertTrue(plat.is_wsl())
                self.assertEqual(plat.wsl_distro(), '')

    def test_non_wsl(self):
        with mock.patch.dict(os.environ, {'WSL_DISTRO_NAME': ''}), \
                mock.patch.object(plat, '_platform') as p:
            p.release.return_value = '24.6.0'             # macOS, niente microsoft
            self.assertFalse(plat.is_wsl())
            self.assertEqual(plat.wsl_distro(), '')

    def test_systemd_user_available(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertTrue(plat.systemd_user_available(path=d))
        self.assertFalse(
            plat.systemd_user_available(path='/non/esiste/qui'),
            '/run/systemd/system assente = WSL1 o systemd spento (SPEC §17.2)')


@unittest.skipUnless(shutil.which('fswatch'), 'fswatch non installato')
class InstallShSenzaSystemdTest(unittest.TestCase):
    """SPEC.md §17.3: `install.sh` su Linux senza systemd → unit NON installata,
    istruzioni di avvio manuale ed exit 0 (nessun errore duro)."""

    def test_ramo_linux_senza_systemd_esce_0(self):
        if os.path.isdir('/run/systemd/system'):
            self.skipTest('questa macchina ha systemd: ramo diverso')
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        home, fake_bin = os.path.join(tmp, 'home'), os.path.join(tmp, 'bin')
        os.makedirs(home)
        os.makedirs(fake_bin)
        uname = os.path.join(fake_bin, 'uname')            # uname finto → Linux
        with open(uname, 'w') as fh:
            fh.write('#!/bin/bash\necho Linux\n')
        os.chmod(uname, 0o755)
        env = dict(os.environ, HOME=home,
                   PATH=fake_bin + os.pathsep + os.environ['PATH'])
        r = subprocess.run(['bash', os.path.join(REPO, 'install.sh')],
                           capture_output=True, text=True, env=env, timeout=60)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn('systemd non disponibile', r.stdout)
        self.assertIn('safekeep.py run', r.stdout)         # avvio manuale
        self.assertIn('systemd=true', r.stdout)            # hint /etc/wsl.conf
        self.assertTrue(os.path.exists(os.path.join(home, '.safekeep')),
                        'la config viene creata comunque')
        self.assertFalse(os.path.exists(
            os.path.join(home, '.config/systemd/user/safekeep.service')),
            'niente unit senza systemd')


if __name__ == '__main__':
    unittest.main()
