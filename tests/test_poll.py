"""Test del monitor a polling per le root su volume di rete (SPEC.md §20).

Il Poller viene testato con un clock finto (prassi del daemon): nessun test
dorme davvero. L'integrazione Daemon↔Poller finge `is_network_fs` con un mock
— le root "di rete" sono tmpdir reali, il dispatch è quello vero
(dedup → plan → classify → copia).
"""
import errno
import os
import shutil
import tempfile
import unittest
from unittest import mock

from safekeep import daemon as daemon_mod
from safekeep.daemon import Daemon
from safekeep.poll import Poller


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


class PollerTest(TmpTestCase):
    """Snapshot/diff, pruning, pending con backoff e catch-up (§20.2)."""

    def poller(self, root, interval=5, auto=False):
        self.clock = [100.0]
        poller = Poller(interval=interval, clock=lambda: self.clock[0])
        poller.sync_roots([root], auto=auto)
        return poller

    def test_baseline_non_emette(self):
        root = self.path('nas')
        write(os.path.join(root, 'proj/note.md'), 'v1')
        poller = self.poller(root)
        self.assertEqual(poller.tick(), [], 'il primo walk è una baseline (il reconcile iniziale ha già copiato)')
        self.assertEqual(poller.snapshots[os.path.realpath(root)],
                         {'proj/note.md': (os.lstat(os.path.join(root, 'proj/note.md')).st_mtime_ns, 2)})

    def test_diff_emette_solo_i_cambiati(self):
        root = self.path('nas')
        write(os.path.join(root, 'proj/a.md'), 'v1')
        write(os.path.join(root, 'proj/b.md'), 'v1')
        poller = self.poller(root)
        poller.tick()                                   # baseline
        self.clock[0] += 6
        write(os.path.join(root, 'proj/a.md'), 'v2-piu-lungo')
        write(os.path.join(root, 'proj/nuovo.md'), 'v1')
        emitted = poller.tick()
        self.assertEqual(emitted,
                         sorted([os.path.join(os.path.realpath(root), 'proj/a.md'),
                                 os.path.join(os.path.realpath(root), 'proj/nuovo.md')]),
                         'solo i path nuovi o cambiati (mtime_ns/size)')
        self.clock[0] += 6
        self.assertEqual(poller.tick(), [], 'nessun cambio → nessun dispatch')

    def test_intervallo_parte_a_fine_walk(self):
        root = self.path('nas')
        os.makedirs(root)
        poller = self.poller(root, interval=5)
        poller.tick()
        self.assertEqual(poller.next[os.path.realpath(root)], self.clock[0] + 5,
                         'la scadenza riparte a fine walk: niente sovrapposizione')

    def test_pruning_git_e_node_modules(self):
        root = self.path('nas')
        write(os.path.join(root, 'proj/.git/x.md'), 'v1')
        write(os.path.join(root, 'proj/node_modules/y.md'), 'v1')
        write(os.path.join(root, 'proj/z.md'), 'v1')
        poller = self.poller(root)
        poller.tick()                                   # baseline
        snap = poller.snapshots[os.path.realpath(root)]
        self.assertEqual(sorted(snap), ['proj/z.md'],
                         'lo scan di poll pota come discover_projects (§4.1)')
        self.clock[0] += 6
        write(os.path.join(root, 'proj/.git/x.md'), 'v2-piu-lungo')
        self.assertEqual(poller.tick(), [], 'una modifica sotto .git non viene emessa')

    def test_symlink_rotto_emesso_come_foglia(self):
        root = self.path('nas')
        os.makedirs(root)
        link = os.path.join(root, 'link.md')
        os.symlink('/percorso/che/non/esiste', link)
        poller = self.poller(root)
        poller.tick()                                   # baseline: lstat, mai follow
        self.assertIn('link.md', poller.snapshots[os.path.realpath(root)])
        self.clock[0] += 6
        os.unlink(link)
        os.symlink('/altro/percorso', link)
        self.assertEqual(poller.tick(),
                         [os.path.join(os.path.realpath(root), 'link.md')],
                         'un symlink cambia target → emesso (copiato come link)')

    def test_root_sparita_pending_backoff_crescente(self):
        root = self.path('nas')
        write(os.path.join(root, 'a.md'), 'v1')
        poller = self.poller(root)
        poller.tick()                                   # baseline
        snap_pre = dict(poller.snapshots[os.path.realpath(root)])
        shutil.rmtree(root)                             # volume smontato / server morto

        self.clock[0] += 6
        self.assertEqual(poller.tick(), [])
        entry = poller.pending[os.path.realpath(root)]
        self.assertEqual(entry['next'] - self.clock[0], 1, 'primo retry con backoff 1s')

        self.clock[0] = entry['next']
        self.assertEqual(poller.tick(), [])             # secondo fallimento
        entry = poller.pending[os.path.realpath(root)]
        self.assertEqual(entry['next'] - self.clock[0], 2,
                         'il backoff NON riparte da 1s (CR-05)')
        self.assertEqual(poller.snapshots[os.path.realpath(root)], snap_pre,
                         'lo snapshot pre-smontaggio è preservato (catch-up)')

    def test_remount_catch_up_contro_lo_snapshot_pre_smontaggio(self):
        root = self.path('nas')
        write(os.path.join(root, 'a.md'), 'v1')
        write(os.path.join(root, 'via.md'), 'v1')
        poller = self.poller(root)
        poller.tick()                                   # baseline
        shutil.rmtree(root)
        self.clock[0] += 6
        poller.tick()                                   # → pending (backoff 1s)
        os.makedirs(root)
        write(os.path.join(root, 'a.md'), 'v2-cambiato')
        write(os.path.join(root, 'nuovo.md'), 'v1')
        # via.md (cancellata con il volume) NON ricreata: i rimossi vanno
        # ignorati (§1, mai-cancellare) — non deve comparire nella diff
        self.clock[0] = poller.pending[os.path.realpath(root)]['next']
        emitted = poller.tick()                         # walk riuscito → catch-up
        self.assertEqual(emitted,
                         sorted([os.path.join(os.path.realpath(root), 'a.md'),
                                 os.path.join(os.path.realpath(root), 'nuovo.md')]),
                         'catch-up su ciò che è cambiato while il mount era giù')
        self.assertNotIn(os.path.realpath(root), poller.pending)
        self.assertEqual(poller.next[os.path.realpath(root)], self.clock[0] + 5)

    def test_sync_roots_sostituisce_le_root(self):
        net1, net2 = self.path('nas1'), self.path('nas2')
        os.makedirs(net1)
        poller = self.poller(net1)
        poller.tick()                                   # baseline su net1
        poller.sync_roots([net2])                       # rescan: net1 esce, net2 entra
        self.assertEqual(list(poller.snapshots), [os.path.realpath(net2)])
        self.assertEqual(poller.snapshots[os.path.realpath(net2)], None,
                         'root nuova → baseline al prossimo walk')
        self.assertEqual(poller.tick(), [])

    def test_walk_snapshot_errore_mid_walk_e_confina_alla_root(self):
        # un OSError NON-ENOENT (server morto) nel mezzo del walk sale al
        # chiamante: è il segnale che mette la root in pending con backoff
        root = self.path('nas')
        write(os.path.join(root, 'a.md'), 'v1')
        poller = Poller(interval=5, clock=lambda: 0.0)
        poller.sync_roots([root])
        with mock.patch('safekeep.poll.walk_snapshot',
                        side_effect=OSError(errno.ENOTCONN, 'Socket is not connected')):
            self.assertEqual(poller.tick(), [])
        self.assertIn(os.path.realpath(root), poller.pending)


class DaemonPollTest(TmpTestCase):
    """Integrazione Daemon↔Poller: split root, no-spawn, copia via dispatch."""

    def build(self, net_only=True):
        src = self.path('src')
        proj = os.path.join(src, 'proj')
        dest = self.path('dst')
        os.makedirs(dest)
        write(os.path.join(proj, '.sync'), 'include: *.md\n')
        write(os.path.join(proj, 'note.md'), 'v1')
        local = self.path('local')
        os.makedirs(local)
        sources = f'source: {src}\n' + ('' if net_only else f'source: {local}\n')
        cfg_path = write(self.path('safekeep.cfg'),
                         sources + f'dest: {dest}\npoll_interval: 5\n')
        net = os.path.realpath(src)
        patcher = mock.patch('safekeep.daemon.is_network_fs',
                             side_effect=lambda p: os.path.realpath(p) == net)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.clock = [100.0]
        daemon = Daemon(cfg_path, clock=lambda: self.clock[0])
        daemon.load_config()
        daemon.load_projects()
        return daemon, dest, os.path.join(proj, 'note.md'), local

    def test_split_root_argv_senza_le_root_di_rete(self):
        daemon, _, _, local = self.build(net_only=False)
        self.assertEqual(daemon.watch_roots, [local], 'la root locale resta a fswatch')
        self.assertEqual([os.path.basename(r) for r in daemon.net_roots], ['src'])
        argv = daemon.fswatch_argv()
        self.assertEqual(argv[argv.index('--') + 1:], [local],
                         'le root di rete sono escluse dall\'argv (§20.1)')

    def test_zero_root_locali_fswatch_non_spawnato(self):
        daemon, dest, target, _ = self.build()
        for _ in range(3):
            daemon.step(0.05)
        self.assertIsNone(daemon.proc,
                          'zero root locali → fswatch non viene spawnato (§20.1)')

    def test_poll_copia_il_file_modificato_sul_mount(self):
        daemon, dest, target, _ = self.build()
        batches = []
        orig = daemon.run_batch

        def spy(paths):
            batches.append(list(paths))
            return orig(paths)

        daemon.run_batch = spy
        daemon.step(0.05)                               # primo turno: baseline, nessun dispatch
        self.assertEqual(batches, [])
        self.clock[0] += 6                              # scade l'intervallo
        write(target, 'v2-piu-lungo')
        daemon.step(0.05)
        self.assertEqual(len(batches), 1)
        self.assertEqual(batches[0], [os.path.realpath(target)],
                         'il diff passa allo stesso dispatch degli eventi fswatch')
        dst = os.path.join(dest, 'proj/note.md')
        with open(dst) as fh:
            self.assertEqual(fh.read(), 'v2-piu-lungo')
        self.clock[0] += 6                              # nessun altro cambio
        daemon.step(0.05)
        self.assertEqual(len(batches), 1, 'nessuna modifica → zero dispatch')

    def test_root_sparita_pending_non_crasha_il_loop(self):
        daemon, dest, target, _ = self.build()
        daemon.step(0.05)                               # baseline
        net_root = os.path.realpath(os.path.dirname(os.path.dirname(target)))
        shutil.rmtree(net_root)                         # volume smontato / server morto
        self.clock[0] += 6
        daemon.step(0.05)                               # walk fallito → pending
        self.assertIn(net_root, daemon.poller.pending)
        self.assertIsNone(daemon.proc)
        daemon.step(0.05)                               # nessun crash, nessun busy-loop


class NetworkErrnoTest(TmpTestCase):
    """§20.4: NETWORK_ERRNOS → dest in pending, non log-loop."""

    def build_daemon(self):
        src = self.path('src')
        proj = os.path.join(src, 'proj')
        dest = self.path('dst')
        os.makedirs(dest)
        write(os.path.join(proj, '.sync'), 'include: *.md\n')
        write(os.path.join(proj, 'a.md'), 'v1')
        cfg_path = write(self.path('safekeep.cfg'), f'source: {src}\ndest: {dest}\n')
        daemon = Daemon(cfg_path)
        daemon.load_config()
        daemon.load_projects()
        return daemon, daemon.projects[0], dest, os.path.join(proj, 'a.md')

    def test_enotconn_in_copy_mette_la_dest_in_pending(self):
        daemon, project, dest, target = self.build_daemon()

        def boom(*args, **kwargs):
            raise OSError(errno.ENOTCONN, 'Socket is not connected')

        with mock.patch.object(daemon_mod, 'copy_one', new=boom):
            daemon._copy(target, project)
        self.assertIn(dest, daemon.pending,
                      'server NAS morto → pending + backoff (§8.3/§20.4)')

    def test_eacces_restano_solo_log(self):
        daemon, project, dest, target = self.build_daemon()

        def boom(*args, **kwargs):
            raise OSError(errno.EACCES, 'Permission denied')

        with mock.patch.object(daemon_mod, 'copy_one', new=boom):
            daemon._copy(target, project)
        self.assertNotIn(dest, daemon.pending,
                         'un errore di permesso su un file non blocca la dest intera')


if __name__ == '__main__':
    unittest.main()
