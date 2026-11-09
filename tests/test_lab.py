"""Unit tests of the lab tool. Run: make check (or python3 -m unittest)."""

import contextlib
import io
import shlex
import struct
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from nvmelab import (boot, ci, cli, config, dashboard, faults, guest,  # noqa: E402,E501
                     inventory, nbft, parity, remote, scenario, slots,
                     sync, target)
from nvmelab.config import ConfigError  # noqa: E402

SLOT = slots.Slot(1)

LAB = """
[hosts.a]
description = "host a"
ci = true
[hosts.b]
address = "10.0.0.2"
user = "test"
"""


def write(dir, name, text):
    p = Path(dir) / name
    p.write_text(text, encoding='utf-8')
    return p


class ConfigTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.lab = write(self.tmp.name, 'lab.toml', LAB)
        self.none = Path(self.tmp.name) / 'missing.toml'

    def tearDown(self):
        self.tmp.cleanup()

    def test_hosts(self):
        cfg = config.load(self.lab, self.none, env={})
        self.assertEqual(cfg.host('a').login, 'root@a.lan')
        self.assertEqual(cfg.host('b').login, 'test@10.0.0.2')
        self.assertEqual(cfg.host('10.0.0.2').name, 'b')
        self.assertEqual(cfg.ci_host().name, 'a')
        self.assertIsNone(cfg.ssh_key)

    def test_unknown_host(self):
        cfg = config.load(self.lab, self.none, env={})
        with self.assertRaisesRegex(ConfigError, "unknown host 'c'"):
            cfg.host('c')

    def test_unknown_key(self):
        lab = write(self.tmp.name, 'bad.toml', '[hosts.a]\naddres = "x"\n')
        with self.assertRaisesRegex(ConfigError, 'addres'):
            config.load(lab, self.none, env={})

    def test_no_hosts(self):
        lab = write(self.tmp.name, 'empty.toml', '')
        with self.assertRaisesRegex(ConfigError, 'no \\[hosts'):
            config.load(lab, self.none, env={})

    def test_ssh_key(self):
        user = write(self.tmp.name, 'user.toml', 'ssh_key = "/k/user"\n')
        cfg = config.load(self.lab, user, env={})
        self.assertEqual(cfg.ssh_key, '/k/user')
        cfg = config.load(self.lab, user, env={'LAB_SSH_KEY': '/k/env'})
        self.assertEqual(cfg.ssh_key, '/k/env')
        cfg = config.load(self.lab, user, env={'LAB_SSH_KEY': '~/k'})
        self.assertEqual(cfg.ssh_key, str(Path('~/k').expanduser()))

    def test_lab_config_env(self):
        cfg = config.load(None, self.none, env={'LAB_CONFIG': str(self.lab)})
        self.assertIn('b', cfg.hosts)

    def test_repo_lab_toml(self):
        cfg = config.load(config.LAB_FILE, self.none, env={})
        self.assertTrue(cfg.hosts)
        cfg.ci_host()


class RemoteTest(unittest.TestCase):
    def setUp(self):
        self.cfg = config.Config(ssh_key='/k')
        self.host = config.Host('a', 'a.lan')

    def test_list_command_is_quoted(self):
        argv = remote.ssh_argv(self.cfg, self.host, ['echo', 'a b', "it's"])
        self.assertEqual(argv[-2:], ['--', "echo 'a b' 'it'\"'\"'s'"])
        self.assertEqual(shlex.split(argv[-1]), ['echo', 'a b', "it's"])

    def test_string_command_is_shell_code(self):
        argv = remote.ssh_argv(self.cfg, self.host, 'ls | head')
        self.assertEqual(argv[-1], 'ls | head')

    def test_options(self):
        argv = remote.ssh_argv(self.cfg, self.host, 'true')
        self.assertIn('BatchMode=yes', argv)
        self.assertEqual(argv[argv.index('-i') + 1], '/k')
        self.assertIn('root@a.lan', argv)

    def test_login_is_interactive(self):
        argv = remote.ssh_argv(config.Config(), self.host)
        self.assertNotIn('BatchMode=yes', argv)
        self.assertNotIn('-i', argv)
        self.assertEqual(argv[-1], 'root@a.lan')


class InventoryTest(unittest.TestCase):
    OUT = 'host\tx\ndisk\tvda 1 GiB\nzzz\tlast\ndisk\tvdb 2 GiB\nbad line\n'

    def test_parse(self):
        facts = inventory.parse(self.OUT)
        self.assertEqual(facts['disk'], ['vda 1 GiB', 'vdb 2 GiB'])
        self.assertNotIn('bad line', facts)

    def test_order(self):
        facts = inventory.parse('zzz\t1\nrepo\tr\nhost\th\n')
        self.assertEqual([k for k, _ in inventory.ordered(facts)],
                         ['host', 'repo', 'zzz'])

    def test_markdown(self):
        md = inventory.render_markdown({'a': {'tools': ['x|y'],
                                              'disk': ['1', '2']}},
                                       '2026-11-05')
        self.assertIn('## a', md)
        self.assertIn('| tools | x\\|y |', md)
        self.assertIn('| disk | 1<br>2 |', md)

    def test_text(self):
        text = inventory.render_text({'a': {'error': ['timeout']}})
        self.assertIn('=== a', text)
        self.assertIn('timeout', text)

    def test_probe_out_one_line(self):
        # A value of several lines (one address per line from ip or
        # ifconfig on a host with several) is one line of the inventory.
        src = inventory.PROBE.read_text(encoding='utf-8')
        start = src.index('out()\n')
        func = src[start:src.index('\n}\n', start) + 3]
        script = func + 'out address "$(printf \'%s\\n\' 10.0.0.1 10.0.0.2)"\n'
        p = subprocess.run(['sh', '-c', script], capture_output=True,
                           encoding='utf-8', timeout=10)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(p.stdout, 'address\t10.0.0.1 10.0.0.2\n')

    def test_probe_runs_here(self):
        # The probe is read only; it must run on any POSIX system.
        p = subprocess.run(['sh', str(inventory.PROBE)], capture_output=True,
                           encoding='utf-8', timeout=60)
        self.assertEqual(p.returncode, 0, p.stderr)
        facts = inventory.parse(p.stdout)
        self.assertIn('host', facts)
        self.assertIn('os', facts)
        for line in p.stdout.splitlines():
            self.assertEqual(line.count('\t'), 1, line)
            self.assertEqual(line, line.strip(), line)


class CiTest(unittest.TestCase):
    def test_image_ref(self):
        self.assertEqual(ci.image_ref('debian'),
                         'ghcr.io/linux-nvme/debian:latest')
        self.assertEqual(ci.image_ref('fedora:41'),
                         'ghcr.io/linux-nvme/fedora:41')
        self.assertEqual(ci.image_ref('docker.io/x/y:1'), 'docker.io/x/y:1')

    def test_docker_argv(self):
        argv = ci.docker_argv('debian', '/root/scratch/t', 'make', pull=True)
        self.assertEqual(argv[:5], ['docker', 'run', '--rm', '--pull',
                                    'always'])
        self.assertIn('/root/scratch/t:/src', argv)
        self.assertTrue(argv[-1].endswith('&& make'))


MESON_LOG = """\
 1/2 unit - x - a OK 0.1s
Ok:                281
Expected Fail:     10
Fail:              0
Unexpected Pass:   0
Skipped:           0
Timeout:           0
"""

MESON_TWO_LOGS = MESON_LOG + """\
Ok:                279
Expected Fail:     10
Fail:              1
Skipped:           2
"""

MUON_LOG = """\
running tests for project 'nvme-cli'
finished 138 tests, 2 expected fail, 0 fail, 2 skipped
"""


class ParityTest(unittest.TestCase):
    def test_jobs(self):
        self.assertEqual([j.name for j in parity.JOBS],
                         ['deb', 'tw', 'muon', 'val'])
        val = parity.select(['val'])[0]
        self.assertIn('ulimit -n 4096 && ', val.command)
        self.assertIn(' -x', val.command)

    def test_select(self):
        self.assertEqual(parity.select([]), list(parity.JOBS))
        self.assertEqual([j.name for j in parity.select(['val', 'deb'])],
                         ['deb', 'val'])
        with self.assertRaisesRegex(ConfigError, "unknown job 'x'"):
            parity.select(['x'])

    def test_tree_name(self):
        job = parity.select(['tw'])[0]
        self.assertEqual(parity.tree_name('pr-1', job), 'pr-1-tw')
        with self.assertRaises(ConfigError):
            parity.tree_name('../x', job)

    def test_default_log_dir(self):
        self.assertEqual(parity.default_log_dir(
            'pr-1', env={'XDG_CACHE_HOME': '/c'}),
            Path('/c/nvme-lab/parity/pr-1'))

    def test_summarize_meson(self):
        self.assertEqual(parity.summarize(MESON_LOG),
                         '281 ok, 10 expected fail, 0 fail')

    def test_summarize_last_meson_summary(self):
        self.assertEqual(parity.summarize(MESON_TWO_LOGS),
                         '279 ok, 10 expected fail, 1 fail, 2 skipped')

    def test_summarize_muon(self):
        self.assertEqual(parity.summarize(MUON_LOG),
                         '138 tests, 2 expected fail, 0 fail, 2 skipped')

    def test_summarize_none(self):
        self.assertIsNone(parity.summarize('error: no compiler\n'))


class SyncTest(unittest.TestCase):
    def test_names(self):
        for good in ('wt-4351', 'a.b_c', 'X1'):
            self.assertEqual(sync.scratch_dir(good), '/root/scratch/' + good)
        for bad in ('', '.', '..', '../etc', 'a/b', '-rf', '.git', 'a b'):
            with self.assertRaises(ConfigError, msg=bad):
                sync.scratch_dir(bad)

    def test_changes(self):
        with tempfile.TemporaryDirectory() as d:
            def git(*args):
                subprocess.run(['git', '-C', d, '-c', 'user.name=t',
                                '-c', 'user.email=t@t', *args], check=True,
                               capture_output=True)
            git('init', '-q')
            for f in ('keep', 'mod', 'gone', 'spa ce'):
                write(d, f, f)
            write(d, '.gitignore', 'ignored\n')
            git('add', '.')
            git('commit', '-qm', 'init')
            write(d, 'mod', 'changed')
            write(d, 'spa ce', 'changed')
            write(d, 'new', 'new')
            write(d, 'staged', 'staged')
            write(d, 'ignored', 'x')
            git('add', 'staged')
            (Path(d) / 'gone').unlink()

            copy, delete = sync.changes(d)
            self.assertEqual(sorted(copy.decode().split('\0')[:-1]),
                             ['mod', 'new', 'spa ce', 'staged'])
            self.assertEqual(delete, b'gone\0')


class BootTest(unittest.TestCase):
    TARGET = ('192.0.2.30', '4421')

    def test_parse_env(self):
        env = boot.parse_env('TRADDR=192.0.2.30\nTARGET_HOST=nvmet\n'
                             'ROOT_PORTS=3:4421,4:4422\nEXTRA_PORTS=5:4423\n')
        self.assertEqual(env.target, self.TARGET)
        self.assertEqual(env.target_host, 'nvmet')
        self.assertEqual(env.paths, {'root': ('3', '4421'),
                                     'root2': ('4', '4422'),
                                     'extra': ('5', '4423')})
        self.assertEqual(boot.parse_env(env.text()), env)
        with self.assertRaisesRegex(ConfigError, 'no path'):
            boot.parse_env('TRADDR=x\nROOT_PORTS=3:4421\n').path('root2')
        # nothing, or the target.env of an older lab
        for text in ('', 'TRADDR=192.0.2.30\nTRSVCID=4421\n'):
            with self.assertRaisesRegex(ConfigError, 'lab boot setup'):
                boot.parse_env(text)

    def test_nbft_table_extra(self):
        t = NbftTest.decode(boot.nbft_table(
            self.TARGET, ssns=['extra,nonboot', 'extra'], extra='4422'))
        self.assertEqual([(s['trsvcid'], s['nqn'], s['flags'])
                          for s in t['ssns'][1:]],
                         [('4422', SLOT.extra_nqn, 0x3),
                          ('4422', SLOT.extra_nqn, 0x1)])
        with self.assertRaisesRegex(ConfigError, 'setup.*again'):
            boot.nbft_table(self.TARGET, ssns=['extra'])

    def test_run_argv(self):
        argv = boot.run_argv('c1', self.TARGET, tree='wt-1',
                             module='/scratch/lab-boot/modules/c1',
                             source='static', hostonly=True,
                             append='rd.debug', timeout=60)
        self.assertEqual(argv[:4], ['docker', 'exec', 'lab-boot-1', 'sh'])
        self.assertEqual(argv[4], '/scratch/lab-boot/slot-1/scripts/boot.sh')
        self.assertEqual(argv[argv.index('--slot') + 1], '1')
        argv3 = boot.run_argv('c1', self.TARGET, slot=slots.Slot(3))
        self.assertEqual(argv3[2], 'lab-boot-3')
        self.assertEqual(argv3[4], '/scratch/lab-boot/slot-3/scripts/boot.sh')
        self.assertEqual(argv3[argv3.index('--slot') + 1], '3')
        opts = argv[5:]
        self.assertEqual(opts[opts.index('--target') + 1],
                         '192.0.2.30:4421')
        self.assertEqual(opts[opts.index('--nvme-tree') + 1], '/scratch/wt-1')
        self.assertEqual(opts[opts.index('--source') + 1], 'static')
        self.assertEqual(opts[opts.index('--root-uuid') + 1], boot.ROOT_UUID)
        self.assertIn('--hostonly', opts)
        self.assertEqual(opts[opts.index('--append') + 1], 'rd.debug')
        self.assertEqual(opts[opts.index('--timeout') + 1], '60')

    def test_run_argv_defaults(self):
        opts = boot.run_argv('c1', self.TARGET)[5:]
        self.assertEqual(opts[opts.index('--source') + 1], 'cmdline')
        for flag in ('--nvme-tree', '--nvme-package', '--dracut-module',
                     '--hostonly', '--append', '--timeout'):
            self.assertNotIn(flag, opts)

    def test_run_argv_checks(self):
        for kwargs, msg in ((dict(source='static'), 'needs --hostonly'),
                            (dict(source='legacy'), 'needs --hostonly'),
                            (dict(source='bogus'), "bad --source"),
                            (dict(tree='t', package=True), 'exclude')):
            with self.subTest(kwargs=kwargs):
                with self.assertRaisesRegex(ConfigError, msg):
                    boot.run_argv('c1', self.TARGET, **kwargs)
        with self.assertRaisesRegex(ConfigError, 'bad scratch name'):
            boot.run_argv('../c1', self.TARGET)
        with self.assertRaisesRegex(ConfigError, 'bad scratch name'):
            boot.run_argv('c1', self.TARGET, tree='../t')

    def test_run_argv_nbft(self):
        opts = boot.run_argv('c1', self.TARGET, source='none',
                             nbft_file='/scratch/lab-boot/nbft/c1')[5:]
        self.assertEqual(opts[opts.index('--nbft') + 1],
                         '/scratch/lab-boot/nbft/c1')
        self.assertNotIn('--nbft', boot.run_argv('c1', self.TARGET))

    def test_nbft_table(self):
        t = NbftTest.decode(boot.nbft_table(
            self.TARGET, ssns=['4422/nqn.x:extra,nonboot,unavailable',
                               '192.0.2.31:4423/nqn.x:other'],
            discovery=['4420', '192.0.2.32:8009']))
        self.assertEqual(t['host_nqn'], SLOT.host_nqn)
        self.assertEqual(t['host_id'], SLOT.host_id)
        hfi, = t['hfis']
        self.assertEqual((hfi['mac'], hfi['ipaddr'], hfi['prefix'],
                          hfi['gateway']),
                         (boot.GUEST_MAC, boot.GUEST_IP, boot.GUEST_PREFIX,
                          boot.GUEST_GATEWAY))
        self.assertEqual(hfi['pci_sbdf'], boot.GUEST_PCI_DEV << 3)
        self.assertEqual([(s['traddr'], s['trsvcid'], s['nqn'], s['flags'])
                          for s in t['ssns']],
                         [('192.0.2.30', '4421', SLOT.root_nqn, 0x1),
                          ('192.0.2.30', '4422', 'nqn.x:extra', 0x103),
                          ('192.0.2.31', '4423', 'nqn.x:other', 0x1)])
        self.assertEqual([d['uri'] for d in t['discovery']],
                         ['nvme+tcp://192.0.2.30:4420/',
                          'nvme+tcp://192.0.2.32:8009/'])

    def test_nbft_table_nid(self):
        t = NbftTest.decode(boot.nbft_table(
            self.TARGET, ssns=['extra', '4499/nqn.x:gone'], extra='4422'))
        self.assertEqual([s['nidt'] for s in t['ssns']], [3, 3, 0])

    def test_nbft_table_no_root(self):
        t = NbftTest.decode(boot.nbft_table(self.TARGET, root=False,
                                            discovery=['4421']))
        self.assertEqual(t['ssns'], [])
        with self.assertRaisesRegex(ConfigError, 'points at nothing'):
            boot.nbft_table(self.TARGET, root=False)
        with self.assertRaisesRegex(ConfigError, 'bad --nbft-discovery'):
            boot.nbft_table(self.TARGET, discovery=['x'])

    def test_slots(self):
        a, b = slots.slots(2)
        self.assertEqual(a.host_id, '6a1b2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d')
        for attr in ('container', 'dir', 'root_nqn', 'extra_nqn', 'host_id',
                     'root_subsys_uuid', 'extra_subsys_uuid'):
            self.assertNotEqual(getattr(a, attr), getattr(b, attr), attr)
        self.assertEqual(b.roles, {b.root_nqn: 'root', b.extra_nqn: 'extra'})
        self.assertEqual(b.nqn_of('root2'), b.root_nqn)
        self.assertEqual(b.nqn_of('extra'), b.extra_nqn)
        with self.assertRaises(ValueError):
            slots.slots(0)

    def test_nbft_table_slot(self):
        slot = slots.Slot(2)
        t = NbftTest.decode(boot.nbft_table(self.TARGET, ssns=['extra'],
                                            extra='4422', slot=slot))
        self.assertEqual((t['host_nqn'], t['host_id']),
                         (slot.host_nqn, slot.host_id))
        self.assertEqual([s['nqn'] for s in t['ssns']],
                         [slot.root_nqn, slot.extra_nqn])

    def test_env_slot(self):
        env = boot.parse_env('SLOT=2\nTRADDR=192.0.2.30\n'
                             'ROOT_PORTS=4:4433,5:4434\nEXTRA_PORTS=6:4435\n')
        self.assertEqual(env.slot, slots.Slot(2))
        self.assertEqual(boot.parse_env(env.text()), env)
        # a target.env of before slots is slot 1
        self.assertEqual(boot.parse_env('TRADDR=x\nROOT_PORTS=1:4430\n').slot,
                         slots.Slot(1))
        values = scenario.placeholders(env)
        self.assertEqual((values['root_nqn'], values['hostnqn'],
                          values['root2']),
                         (env.slot.root_nqn, env.slot.host_nqn, '4434'))

    def test_parse_describe(self):
        self.assertEqual(sync.parse_describe('v2.16\nv2.16\n'), ('v2.16', 2))
        self.assertEqual(sync.parse_describe('\nv3.2\n'), (None, 3))
        self.assertEqual(sync.parse_describe(''), (None, None))

    def test_expected_duration(self):
        def load(text):
            p = Path(self.tmp.name) / 'x.toml'
            p.write_text(text, encoding='utf-8')
            return scenario.load(p)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        quick = load('name = "q"\n')
        fault = load('[[fault]]\nkind = "tcp-drop"\npath = "root2"\n')
        failing = load('[expect]\nresult = "fail"\n')
        shutdown = load('[guest]\n')
        order = sorted([quick, fault, failing, shutdown],
                       key=boot.expected_duration, reverse=True)
        self.assertEqual(order, [shutdown, failing, fault, quick])

    def test_test_needs_slots(self):
        orig = boot.existing_slots
        try:
            boot.existing_slots = lambda cfg, host: slots.slots(2)
            with self.assertRaisesRegex(ConfigError, '--slots 3'):
                boot.test(None, None, [], jobs=3)
            boot.existing_slots = lambda cfg, host: []
            with self.assertRaisesRegex(ConfigError, 'lab boot setup'):
                boot.test(None, None, [])
        finally:
            boot.existing_slots = orig

    def test_upload_argv(self):
        cfg = config.Config(hosts={}, ssh_key=None)
        host = config.Host('a', 'a.lan')
        argv = boot.upload_argv(cfg, host, '/root/scratch/lab-boot/x y')
        self.assertEqual(argv[-1], "rm -rf '/root/scratch/lab-boot/x y' && "
                         "mkdir -p '/root/scratch/lab-boot/x y' && "
                         "tar -C '/root/scratch/lab-boot/x y' -xf -")

    def test_scripts_exist(self):
        for script in ('container.sh', 'mkroot.sh', 'boot.sh',
                       'guest-base.sh', 'guest-build.sh', 'guest-image.sh',
                       'guest-boot.sh'):
            self.assertTrue((boot.SCRIPTS / script).is_file(), script)


class NbftTest(unittest.TestCase):
    """The table is checked by decoding it here, along the lines of
    libnvme's nbft.c (and on the lab with 'nvme nbft show')."""

    @staticmethod
    def decode(b):
        def ref(off):
            return struct.unpack_from('<IH', b, off)

        def string(off):
            o, n = ref(off)
            assert b[o + n] == 0, 'string not terminated'
            return b[o:o + n].decode() if n else None

        def ip(off):
            o, n = ref(off)
            assert n == 16
            return str(nbft.ipaddress.ip_address(b[o:o + 16]).ipv4_mapped
                       or nbft.ipaddress.ip_address(b[o:o + 16]))

        def ip_at(off):
            a = nbft.ipaddress.ip_address(b[off:off + 16])
            return str(a.ipv4_mapped or a)

        assert sum(b) & 0xff == 0, 'bad checksum'
        sig, length, major = struct.unpack_from('<4sIB', b, 0)
        assert (sig, length, major) == (b'NBFT', len(b), 1)
        heap_off, heap_len = struct.unpack_from('<II', b, 36)
        assert heap_off + heap_len == len(b)
        c = struct.unpack_from(nbft.CONTROL, b, 64)
        assert c[0] == nbft.DESC_CONTROL and c[5] & 1
        host = b[c[7]:c[7] + 32]
        assert host[0] == nbft.DESC_HOST and host[1] & 1
        t = dict(host_id=str(nbft.uuid.UUID(bytes=host[2:18])),
                 host_nqn=string(c[7] + 18), hfis=[], ssns=[], discovery=[])
        hfio, hfil, _, nhfi = c[11:15]
        for i in range(nhfi):
            h = b[hfio + i * hfil:hfio + (i + 1) * hfil]
            assert h[0] == nbft.DESC_HFI and h[1] == i + 1 and h[3] == 3
            to, tl = ref(hfio + i * hfil + 16)
            assert tl == 128 and b[to] == nbft.DESC_HFI_TRINFO
            tcp = struct.unpack_from(nbft.HFI_TCP, b, to)
            t['hfis'].append(dict(
                flags=tcp[5], pci_sbdf=tcp[6],
                mac=':'.join(f'{x:02x}' for x in tcp[7]),
                ipaddr=ip_at(to + 20), prefix=tcp[11],
                gateway=ip_at(to + 37)))
        so, sl, _, ns = c[15:19]
        for i in range(ns):
            o = so + i * sl
            s = struct.unpack_from(nbft.SSNS, b, o)
            assert s[0] == nbft.DESC_SSNS and s[1] == i + 1
            so2, sl2 = ref(o + 48)
            t['ssns'].append(dict(
                flags=s[2], traddr=ip(o + 10), trsvcid=string(o + 16),
                nsid=s[12], hfi=s[16], discovery=s[5],
                nqn=string(o + 54), hfis=list(b[so2:so2 + sl2]),
                nidt=s[13]))
        do, dl, _, nd = c[23:27]
        for i in range(nd):
            o = do + i * dl
            assert b[o] == nbft.DESC_DISCOVERY and b[o + 2] == i + 1
            t['discovery'].append(dict(uri=string(o + 6),
                                       nqn=string(o + 12), hfi=b[o + 3]))
        return t

    def table(self, **kwargs):
        hfi = nbft.Hfi(mac='52:54:00:aa:bb:cc', ipaddr='192.0.2.15',
                       prefix=24, gateway='192.0.2.1')
        return nbft.Table(host_nqn='nqn.2014-08.org.nvmexpress:uuid:x',
                          host_id='6a1b2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d',
                          hfis=[hfi], **kwargs)

    def test_sizes(self):
        for fmt, size in nbft.SIZES.items():
            self.assertEqual(struct.calcsize(fmt), size, fmt)

    def test_build(self):
        t = self.table(
            ssns=[nbft.Ssns('192.0.2.30', '4421', 'nqn.x:root'),
                  nbft.Ssns('2001:db8::30', '4422', 'nqn.x:v6', nsid=7,
                            flags=('nonboot', 'unavailable'),
                            discovery=1)],
            discovery=[nbft.Discovery('nvme+tcp://192.0.2.30:8009/')])
        d = self.decode(nbft.build(t))
        self.assertEqual(d['host_id'], '6a1b2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d')
        self.assertEqual(d['host_nqn'], 'nqn.2014-08.org.nvmexpress:uuid:x')
        self.assertEqual(d['hfis'], [dict(
            flags=nbft.HFI_TCP_VALID | nbft.HFI_TCP_GLOBAL_ROUTE,
            pci_sbdf=0, mac='52:54:00:aa:bb:cc', ipaddr='192.0.2.15',
            prefix=24, gateway='192.0.2.1')])
        self.assertEqual(d['ssns'], [
            dict(flags=0x1, traddr='192.0.2.30', trsvcid='4421', nsid=1,
                 hfi=1, discovery=0, nqn='nqn.x:root', hfis=[1], nidt=0),
            dict(flags=0x103, traddr='2001:db8::30', trsvcid='4422', nsid=7,
                 hfi=1, discovery=1, nqn='nqn.x:v6', hfis=[1], nidt=0)])
        self.assertEqual(d['discovery'], [dict(
            uri='nvme+tcp://192.0.2.30:8009/', nqn=nbft.DISCOVERY_NQN,
            hfi=1)])

    def test_secondary_hfis_start_with_the_primary(self):
        t = self.table(ssns=[nbft.Ssns('192.0.2.30', '4421', 'nqn.x:r',
                                       hfi=2, secondary_hfis=(1, 2))])
        t.hfis.append(nbft.Hfi(mac='52:54:00:aa:bb:cd', ipaddr='192.0.2.16',
                               prefix=24))
        self.assertEqual(self.decode(nbft.build(t))['ssns'][0]['hfis'],
                         [2, 1])

    def test_build_dhcp(self):
        t = self.table(ssns=[nbft.Ssns('192.0.2.30', '4421', 'nqn.x:r')])
        t.hfis[0].dhcp_server = '192.0.2.1'
        flags = self.decode(nbft.build(t))['hfis'][0]['flags']
        self.assertTrue(flags & nbft.HFI_TCP_DHCP_OVERRIDE)

    def test_build_checks(self):
        for kwargs, msg in (
                (dict(ssns=[nbft.Ssns('192.0.2.30', '1', 'n', hfi=2)]),
                 'no HFI 2'),
                (dict(ssns=[nbft.Ssns('192.0.2.30', '1', 'n',
                                      discovery=1)]),
                 'no discovery descriptor 1'),
                (dict(ssns=[nbft.Ssns('192.0.2.30', '1', 'n',
                                      flags=('bogus',))]),
                 "bad SSNS flag 'bogus'"),
                (dict(ssns=[nbft.Ssns('192.0.2.30', '1', 'n',
                                      flags=('available', 'unavailable'))]),
                 'exclude each other'),
                (dict(ssns=[nbft.Ssns('nope', '1', 'n')]),
                 'bad IP address')):
            with self.subTest(msg=msg):
                with self.assertRaisesRegex(nbft.NbftError, msg):
                    nbft.build(self.table(**kwargs))
        t = self.table()
        t.hfis[0].mac = '52:54:00'
        with self.assertRaisesRegex(nbft.NbftError, 'bad MAC'):
            nbft.build(t)
        t = self.table()
        t.hfis = []
        with self.assertRaisesRegex(nbft.NbftError, 'needs an HFI'):
            nbft.build(t)

    def test_parse_ssns_spec(self):
        s = nbft.parse_ssns_spec('4422/nqn.x:a,nonboot', '192.0.2.30')
        self.assertEqual((s.traddr, s.trsvcid, s.nqn, s.flags),
                         ('192.0.2.30', '4422', 'nqn.x:a', ('nonboot',)))
        s = nbft.parse_ssns_spec('192.0.2.31:4423/nqn.x:b', '192.0.2.30')
        self.assertEqual((s.traddr, s.trsvcid), ('192.0.2.31', '4423'))
        for bad in ('nqn.x', '4422/', 'x/nqn.x', '4422/nqn.x,bogus'):
            with self.subTest(spec=bad):
                with self.assertRaises(nbft.NbftError):
                    nbft.parse_ssns_spec(bad, '192.0.2.30')


class ScenarioTest(unittest.TestCase):
    TARGET = ('192.0.2.30', '4421')
    ROLES = {SLOT.root_nqn: 'root', SLOT.extra_nqn: 'extra'}

    @staticmethod
    def env():
        return boot.parse_env('TRADDR=192.0.2.30\nTARGET_HOST=nvmet\n'
                              'ROOT_PORTS=3:4421,4:4422\nEXTRA_PORTS=5:4423\n')

    def write(self, text):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        p = Path(self.tmp.name) / 'case.toml'
        p.write_text(text, encoding='utf-8')
        return p

    def test_shipped_scenarios(self):
        scenarios = scenario.find()
        self.assertTrue(scenarios)
        env = self.env()
        for s in scenarios:
            if s.guest is not None:
                continue
            with self.subTest(scenario=s.id):
                opts = scenario.run_options(s, env)
                argv = boot.run_argv(
                    s.id, env.target, source=opts['source'],
                    hostonly=opts['hostonly'], append=opts['append'],
                    host_identity=opts['host_identity'])
                self.assertIn('--source', argv)
                if opts['nbft_options'] is not None:
                    NbftTest.decode(boot.nbft_table(
                        env.target, extra='4423', **opts['nbft_options']))
                for f in opts['fault_specs']:
                    env.path(f['path'])
                # the expected host NQNs expand
                scenario.verdict(s, True, {}, self.ROLES,
                                 scenario.placeholders(env))

    def test_find_tags(self):
        ids = {s.id for s in scenario.find(tags=['nbft'])}
        self.assertIn('nbft', ids)
        self.assertNotIn('cmdline', ids)
        ids = {s.id for s in scenario.find(tags=['nbft'],
                                           skip_tags=['pre-os'])}
        self.assertIn('nbft', ids)
        self.assertNotIn('pre-os-nbft', ids)

    def test_load_errors(self):
        for text, msg in (('bogus = 1\n', "unknown key 'bogus'"),
                          ('[boot]\nsorce = "x"\n', "unknown key 'sorce'"),
                          ('[boot]\nhostonly = "yes"\n', 'must be a bool'),
                          ('[expect]\nresult = "maybe"\n', 'pass or fail'),
                          ('[[fault]]\nkind = "nope"\npath = "root"\n',
                           "bad fault 'nope'"),
                          ('[[fault]]\nkind = "ana"\npath = "root2"\n',
                           'bad state'),
                          ('[[fault]]\nkind = "tcp-drop"\n', 'no path'),
                          ('name = \n', 'case.toml')):
            with self.subTest(text=text):
                with self.assertRaisesRegex(ConfigError, msg):
                    scenario.load(self.write(text))

    def test_run_options(self):
        s = scenario.load(self.write(
            '[boot]\nnbft_ssns = ["extra,nonboot", "{root2}/{root_nqn}"]\n'
            'nbft_discovery = ["{trsvcid}"]\nhost_identity = false\n'
            'append = ["rd.nvmf.discover=tcp,{traddr},,{extra}", '
            '"rd.nvmf.hostnqn={other_hostnqn}"]\n'
            '[[fault]]\nkind = "port-removed"\npath = "root2"\n'))
        opts = scenario.run_options(s, self.env())
        self.assertEqual(opts['source'], 'none')
        self.assertFalse(opts['host_identity'])
        self.assertEqual(opts['append'],
                         'rd.nvmf.discover=tcp,192.0.2.30,,4423 '
                         f'rd.nvmf.hostnqn={scenario.OTHER_HOST_NQN}')
        self.assertEqual(opts['nbft_options'],
                         dict(root=True,
                              ssns=['extra,nonboot', f'4422/{SLOT.root_nqn}'],
                              discovery=['4421']))
        self.assertEqual(opts['fault_specs'], [
            {'kind': 'port-removed', 'path': 'root2', 'state': None}])
        s = scenario.load(self.write('[boot]\nappend = ["{nope}"]\n'))
        with self.assertRaisesRegex(ConfigError, 'unknown placeholder'):
            scenario.run_options(s, self.env())
        s = scenario.load(self.write('name = "x"\n'))
        opts = scenario.run_options(s, self.env())
        self.assertEqual((opts['source'], opts['append'],
                          opts['nbft_options'], opts['host_identity']),
                         ('cmdline', None, None, True))

    STATE = (f'ctrl nvme0 tcp {SLOT.root_nqn} state=live trsvcid=4421 '
             f'ctrl_loss_tmo=600 hostnqn={SLOT.host_nqn}\n'
             f'ctrl nvme1 tcp {SLOT.root_nqn} state=live trsvcid=4422 '
             f'ctrl_loss_tmo=off hostnqn={SLOT.host_nqn}\n'
             f'ctrl nvme2 tcp {SLOT.extra_nqn}\n'
             'registry nvme0 owner=pre-os\nregistry nvme1 owner=pre-os\n'
             'registry nvme2 owner=\n')

    def test_parse_state(self):
        state = scenario.parse_state(self.STATE)
        self.assertEqual(state['nvme0'], {
            'transport': 'tcp', 'nqn': SLOT.root_nqn, 'state': 'live',
            'trsvcid': '4421', 'ctrl_loss_tmo': '600',
            'hostnqn': SLOT.host_nqn, 'owner': 'pre-os'})
        self.assertEqual(state['nvme2'], {'transport': 'tcp',
                                          'nqn': SLOT.extra_nqn,
                                          'owner': None})

    def test_verdict(self):
        s = scenario.load(self.write(
            '[expect]\ncontrollers = ["root", "extra"]\n'
            'paths = { root = 2 }\n'
            'owners = { root = "pre-os", extra = "" }\n'
            'hostnqn = { root = "{hostnqn}" }\n'))
        values = scenario.placeholders(self.env())
        good = scenario.parse_state(self.STATE)
        self.assertEqual(scenario.verdict(s, True, good, self.ROLES, values),
                         [])
        self.assertEqual(scenario.verdict(s, False, {}, self.ROLES, values),
                         ['boot failed, expected pass'])
        bad = scenario.parse_state(
            f'ctrl nvme0 tcp {SLOT.root_nqn} hostnqn=nqn.x\n'
            'registry nvme0 owner=nbft\n')
        self.assertEqual(scenario.verdict(s, True, bad, self.ROLES, values), [
            "controllers of ['root'], expected ['extra', 'root']",
            'root: 1 paths, expected 2',
            "root nvme0: owner 'nbft', expected 'pre-os'",
            f"root nvme0: hostnqn 'nqn.x', expected '{SLOT.host_nqn}'"])
        s = scenario.load(self.write(
            '[expect]\nctrl_loss_tmo = { root = "off" }\n'))
        self.assertEqual(scenario.verdict(s, True, good, self.ROLES), [
            "root nvme0: ctrl_loss_tmo '600', expected 'off'"])
        s = scenario.load(self.write('[expect]\nresult = "fail"\n'))
        self.assertEqual(scenario.verdict(s, False, {}, self.ROLES), [])
        self.assertEqual(scenario.verdict(s, True, good, self.ROLES),
                         ['boot passed, expected fail'])

    GUEST_STATE = ('mounted yes\npoweroff yes\npowered_off yes\n'
                   'shutdown_seconds 4\n'
                   'order umount=30.1 disconnect=30.5 ifdown=31.0\n'
                   'event [   30.100000] EXT4-fs (nvme1n1): unmounting\n'
                   'guest ctrl nvme1 tcp nqn.x state=live\n')

    def test_guest_scenarios(self):
        shipped = [s for s in scenario.find(tags=['shutdown'])]
        self.assertTrue(shipped)
        self.assertTrue(all(s.guest is not None for s in shipped))
        for text, msg in (('[guest]\nconnect = "udev"\n', 'guest.connect'),
                          ('[guest]\nconfig = "x.conf"\n', 'guest.config'),
                          ('[guest]\nlvm = true\n', "unknown key 'lvm'"),
                          ('[guest]\n[boot]\nhostonly = true\n',
                           'no \\[boot\\]'),
                          ('[guest]\n[expect]\npaths = { root = 1 }\n',
                           'no \\[boot\\]')):
            with self.subTest(text=text):
                with self.assertRaisesRegex(ConfigError, msg):
                    scenario.load(self.write(text))
        s = scenario.load(self.write('[guest]\nconnect = "discoverd"\n'))
        package, v3 = boot.Tree(package=True), boot.Tree('t', major=3)
        self.assertEqual(guest.major(package, s), guest.PACKAGE_MAJOR)
        self.assertEqual(guest.flavor(s), 'discoverd')
        self.assertEqual(guest.config_file(s, package), 'discovery.conf')
        self.assertEqual(guest.config_file(s, v3), 'nvme-fabrics.conf')
        tagged = scenario.load(self.write('tags = ["nvme-cli-3"]\n[guest]\n'))
        self.assertFalse(scenario.applies(
            tagged, boot.scenario_major(package, tagged)))
        self.assertTrue(scenario.applies(
            tagged, boot.scenario_major(v3, tagged)))

    def test_guest_verdict(self):
        s = scenario.load(self.write('[guest]\nshutdown_limit = 10\n'))
        facts = guest.parse_state(self.GUEST_STATE)
        self.assertEqual(facts['order'],
                         'umount=30.1 disconnect=30.5 ifdown=31.0')
        self.assertEqual(len(facts['event']), 1)
        self.assertEqual(guest.verdict(s, facts), [])
        slow = guest.parse_state(self.GUEST_STATE.replace(
            'shutdown_seconds 4', 'shutdown_seconds 95') +
            'error [  120.1] nvme nvme1: failed nvme_keep_alive_end_io '
            'error=10\n')
        self.assertEqual(guest.verdict(s, slow), [
            'the power-off took 95s, more than 10s',
            'console: [  120.1] nvme nvme1: failed nvme_keep_alive_end_io '
            'error=10'])
        hung = guest.parse_state('mounted yes\npoweroff yes\n'
                                 'powered_off no\n')
        self.assertEqual(guest.verdict(s, hung), [
            f'no power-off: QEMU was killed after {guest.TIMEOUT}s'])
        self.assertEqual(guest.verdict(s, guest.parse_state('mounted no\n')),
                         ['the guest did not mount /srv/data'])

    def test_xfail_on(self):
        s = scenario.load(self.write('[xfail_on]\n"v3.2" = "bug"\n'))
        self.assertEqual(scenario.outcome(s, ['e'], 'v3.2'), ('XFAIL', False))
        self.assertEqual(scenario.outcome(s, [], 'v3.2'), ('XPASS', True))
        self.assertEqual(scenario.outcome(s, ['e'], None), ('FAIL', True))
        self.assertEqual(scenario.outcome(s, ['e'], 'v3.1'), ('FAIL', True))
        self.assertEqual(scenario.xfail_reason(s, 'v3.2'), 'bug')
        with self.assertRaisesRegex(ConfigError, 'xfail_on'):
            scenario.load(self.write('xfail_on = "v3.2"\n'))

    def test_xfail_on_tree_name(self):
        s = scenario.load(self.write(
            '[xfail_on]\n"suite-master" = "not merged"\n"package" = "old"\n'))
        self.assertEqual(scenario.xfail_reason(
            s, boot.Tree('suite-master').keys), 'not merged')
        self.assertEqual(scenario.xfail_reason(
            s, boot.Tree(package=True).keys), 'old')
        self.assertIsNone(scenario.xfail_reason(
            s, boot.Tree('shutdown-units').keys))
        self.assertEqual(boot.Tree('t', version='v3.2').keys, ('v3.2', 't'))

    def test_applies(self):
        s = scenario.load(self.write('tags = ["nbft", "nvme-cli-3"]\n'))
        self.assertTrue(scenario.applies(s, 3))
        self.assertFalse(scenario.applies(s, 2))
        self.assertTrue(scenario.applies(s, None))
        s = scenario.load(self.write('tags = ["nbft"]\n'))
        self.assertTrue(scenario.applies(s, 2))

    def test_outcome(self):
        s = scenario.load(self.write('name = "x"\n'))
        self.assertEqual(scenario.outcome(s, []), ('PASS', False))
        self.assertEqual(scenario.outcome(s, ['e']), ('FAIL', True))
        s = scenario.load(self.write('xfail = "not yet"\n'))
        self.assertEqual(scenario.outcome(s, ['e']), ('XFAIL', False))
        self.assertEqual(scenario.outcome(s, []), ('XPASS', True))


class FaultsTest(unittest.TestCase):
    def test_parse(self):
        self.assertEqual(faults.parse('tcp-refuse:root2'),
                         {'kind': 'tcp-refuse', 'path': 'root2',
                          'state': None})
        self.assertEqual(faults.parse('ana:inaccessible:root2'),
                         {'kind': 'ana', 'path': 'root2',
                          'state': 'inaccessible'})
        for bad in ('tcp-refuse', 'nope:root', 'ana:root2',
                    'ana:bogus:root2', 'port-removed:x:root'):
            with self.subTest(spec=bad):
                with self.assertRaises(ConfigError):
                    faults.parse(bad)

    def test_failover_argv(self):
        self.assertEqual(
            faults.failover_argv('tcp-drop', '192.0.2.30', '4422', 'down'),
            'PYTHONPATH=/root/scratch/lab-boot-failover/src python3 -m '
            'nvmf_failover.cli fault tcp-port 192.0.2.30:4422 down')

    def test_down_and_up(self):
        """The faults go down in order and up in reverse, also when one
        fails to go down."""
        calls = []
        env = ScenarioTest.env()

        class FakeTarget:
            def remove_port(self, port):
                calls.append(('remove-port', port))

            def add_port(self, nqn):
                calls.append(('add-port', nqn))
                return '4'

            def trsvcid(self, port):
                return '4422'

            def set_ana(self, port, state):
                calls.append(('set-ana', port, state))
                if state == 'change':
                    raise ConfigError('set-ana failed')

        f = faults.Faults(None, None, env, [
            faults.make('port-removed', 'root2'),
            faults.make('ana', 'extra', 'inaccessible')])
        f.target = FakeTarget
        with contextlib.redirect_stdout(io.StringIO()):
            with f:
                calls.append('boot')
        self.assertEqual(calls, [('remove-port', '4'),
                                 ('set-ana', '5', 'inaccessible'), 'boot',
                                 ('set-ana', '5', 'optimized'),
                                 ('add-port', SLOT.root_nqn)])
        calls.clear()
        f = faults.Faults(None, None, env, [
            faults.make('port-removed', 'root2'),
            faults.make('ana', 'extra', 'change')])
        f.target = FakeTarget
        with contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaisesRegex(ConfigError, 'set-ana failed'):
                with f:
                    calls.append('boot')
        self.assertEqual(calls, [('remove-port', '4'),
                                 ('set-ana', '5', 'change'),
                                 ('add-port', SLOT.root_nqn)])


class TargetTest(unittest.TestCase):
    def test_config_text(self):
        cfg = config.Config(hosts={}, ssh_key='/k/id')
        text = target.config_text(cfg, config.Host('nvmet', 'nvmet.lan'),
                                  '192.0.2.30')
        import tomllib
        data = tomllib.loads(text)['target']
        self.assertEqual(data['remote'], 'root@nvmet.lan')
        self.assertEqual(data['traddr'], '192.0.2.30')
        self.assertEqual(data['trsvid'], target.TRSVID)
        self.assertIn('/k/id', data['ssh_options'])

    def test_tool_paths(self):
        with tempfile.TemporaryDirectory() as d:
            lab = write(d, 'lab.toml', LAB)
            user = write(d, 'user.toml', 'target_control = "/opt/ntc"\n')
            cfg = config.load(lab, user, env={})
            self.assertEqual(cfg.target_control, '/opt/ntc')
            self.assertTrue(cfg.nvmf_failover.endswith('/nvmf-failover'))
            cfg = config.load(lab, user, env={'LAB_NVMF_FAILOVER': '/x/nf'})
            self.assertEqual(cfg.nvmf_failover, '/x/nf')

    def test_missing_tool(self):
        cfg = config.Config(hosts={}, target_control='/nonexistent/tool')
        with self.assertRaisesRegex(ConfigError, 'no nvme-target-control'):
            target.TargetControl(cfg, config.Host('t', 't.lan'), 'x')


class DashboardTest(unittest.TestCase):
    STARTED = __import__('datetime').datetime(2026, 11, 6, 20, 1, 2)

    def cases(self):
        def sc(sid, xfail=None):
            return scenario.Scenario(id=sid, name=sid, xfail=xfail)
        v216 = boot.Tree('v216', version='v2.16', major=2)
        master = boot.Tree('master', major=3)
        return [
            boot.Case(v216, sc('nbft'), 't-v216-nbft', 'PASS', False,
                      duration=12.4),
            boot.Case(v216, sc('ctl'), 't-v216-ctl', 'XFAIL', False,
                      ["root nvme0: ctrl_loss_tmo '600'"], 'not yet', 20),
            boot.Case(master, sc('nbft'), 't-master-nbft', 'FAIL', True,
                      ['boot failed, expected pass'], None, 30),
            boot.Case(master, sc('ctl'), 't-master-ctl', 'XPASS', True,
                      [], 'not yet', 9)]

    def test_record(self):
        rec = dashboard.record(self.cases(), self.STARTED, 'dracut: x')
        self.assertEqual((rec['workflow'], rec['run_id'], rec['date'],
                          rec['outcome'], rec['jobs'], rec['warnings']),
                         ('Boot tests', '20261106-200102',
                          '2026-11-06 20:01:02', 'fail', [], ['dracut: x']))
        v216, master = rec['sections']
        self.assertEqual((v216['name'], v216['outcome'], v216['counts']),
                         ('nvme-cli v2.16', 'pass',
                          {'pass': 1, 'fail': 0, 'expected fail': 1}))
        self.assertEqual((master['name'], master['outcome'],
                          master['problems']),
                         ('nvme-cli master', 'fail',
                          ['2 failed: nbft, ctl']))
        passed, xfail = v216['tests']
        self.assertEqual(passed, {'test': 'nbft', 'config': 'v216',
                                  'status': 'pass', 'duration': '12s'})
        self.assertEqual((xfail['status'], xfail['reason']),
                         ('expected fail', 'not yet'))
        self.assertEqual(xfail['files'][0],
                         {'href': 'logs/t-v216-ctl/console.txt',
                          'name': 'console'})
        failed, xpass = master['tests']
        self.assertEqual((failed['status'], failed['reason']),
                         ('fail', 'boot failed, expected pass'))
        self.assertEqual(xpass['status'], 'fail')
        self.assertIn('expected to fail: not yet', xpass['reason'])
        self.assertEqual(rec['problems'],
                         ['nvme-cli master: 2 failed: nbft, ctl'])
        json_text = __import__('json').dumps(rec)
        self.assertIn('Boot tests', json_text)

    def test_record_all_good(self):
        rec = dashboard.record(self.cases()[:2], self.STARTED)
        self.assertEqual((rec['outcome'], rec['conclusion'],
                          rec['warnings']), ('pass', 'success', []))
        self.assertEqual(dashboard.record([], self.STARTED)['problems'],
                         ['no results'])

    def test_site_argv(self):
        cmd = dashboard.site_argv('20261106-200102',
                                  [('dracut #2820', 'https://x/2820')])
        self.assertIn('/root/scratch/lab-dashboard-tool/scripts/dashboard/'
                      'nvme-dashboard site -o /root/scratch/lab-dashboard/'
                      'site', cmd)
        self.assertIn('--primary \'Boot tests\'', cmd)
        self.assertIn("--link 'dracut #2820=https://x/2820'", cmd)
        self.assertIn('--history /root/scratch/lab-dashboard/site/'
                      'history.json', cmd)

    def test_describe_module(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(dashboard.describe_module(d),
                             f'dracut module {d}')
            subprocess.run(['git', 'init', '-q', '-b', 'pr', d], check=True)
            (Path(d) / 'm.sh').write_text('x\n', encoding='utf-8')
            git = ['git', '-C', d, '-c', 'user.name=t', '-c',
                   'user.email=t@t']
            subprocess.run(git + ['add', '.'], check=True)
            subprocess.run(git + ['commit', '-q', '-m', 'fix it'], check=True)
            text = dashboard.describe_module(d)
            self.assertRegex(text, r'^dracut pr [0-9a-f]+ fix it$')
            (Path(d) / 'm.sh').write_text('y\n', encoding='utf-8')
            self.assertTrue(dashboard.describe_module(d).endswith(
                ' (modified)'))

    def test_parse_link(self):
        self.assertEqual(dashboard.parse_link('a=https://b'),
                         ('a', 'https://b'))
        for bad in ('a', '=x', 'a='):
            with self.assertRaises(ConfigError):
                dashboard.parse_link(bad)


class CliTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.lab = str(write(self.tmp.name, 'lab.toml', LAB))

    def tearDown(self):
        self.tmp.cleanup()

    def run_cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = cli.main(['--config', self.lab, *argv])
        return rc, out.getvalue(), err.getvalue()

    def test_hosts(self):
        rc, out, _ = self.run_cli('hosts')
        self.assertEqual(rc, 0)
        self.assertIn('root@a.lan', out)
        self.assertIn('[ci]', out)

    def test_ci_dry_run_keeps_options_after_dashdash(self):
        rc, out, _ = self.run_cli('ci', 'debian', 't', '--dry-run', '--',
                                  'make', '--help')
        self.assertEqual(rc, 0)
        argv = shlex.split(out)
        self.assertEqual(argv[argv.index('--') - 1], 'root@a.lan')
        docker = shlex.split(argv[-1])
        self.assertEqual(docker[-1], 'git config --global --add '
                         'safe.directory /src && make --help')

    def test_boot_run_dry_run(self):
        rc, out, _ = self.run_cli('boot', 'run', 'c1', '--nvme', 'wt',
                                  '--dracut-module', '/tmp/m', '--source',
                                  'legacy', '--hostonly', '--dry-run')
        self.assertEqual(rc, 0)
        argv = shlex.split(out)
        self.assertEqual(argv[argv.index('--') - 1], 'root@a.lan')
        inner = shlex.split(argv[-1])
        self.assertEqual(inner[inner.index('--dracut-module') + 1],
                         '/scratch/lab-boot/modules/c1')
        self.assertEqual(inner[inner.index('--nvme-tree') + 1], '/scratch/wt')

    def test_boot_run_nbft_dry_run(self):
        rc, out, _ = self.run_cli('boot', 'run', 'c1', '--nbft-ssns',
                                  '4422/nqn.x:extra,nonboot', '--dry-run')
        self.assertEqual(rc, 0)
        inner = shlex.split(shlex.split(out)[-1])
        self.assertEqual(inner[inner.index('--nbft') + 1],
                         '/scratch/lab-boot/nbft/c1')
        # the target comes from the NBFT
        self.assertEqual(inner[inner.index('--source') + 1], 'none')

    def test_boot_run_nbft_with_source(self):
        rc, out, _ = self.run_cli('boot', 'run', 'c1', '--nbft', '--source',
                                  'cmdline', '--dry-run')
        self.assertEqual(rc, 0)
        inner = shlex.split(shlex.split(out)[-1])
        self.assertEqual(inner[inner.index('--source') + 1], 'cmdline')

    def test_boot_run_nbft_bad_spec(self):
        rc, _, err = self.run_cli('boot', 'run', 'c1', '--nbft-ssns',
                                  '4422/nqn.x,bogus', '--dry-run')
        self.assertEqual(rc, 2)
        self.assertIn("bad SSNS flag 'bogus'", err)

    def test_boot_nbft(self):
        f = Path(self.tmp.name) / 'NBFT'
        rc, out, _ = self.run_cli('boot', 'nbft', str(f), '--target-addr',
                                  '192.0.2.30:4421', '--nbft-ssns',
                                  '4422/nqn.x:extra')
        self.assertEqual(rc, 0)
        t = NbftTest.decode(f.read_bytes())
        self.assertEqual([(s['traddr'], s['trsvcid'], s['nqn'])
                          for s in t['ssns']],
                         [('192.0.2.30', '4421', SLOT.root_nqn),
                          ('192.0.2.30', '4422', 'nqn.x:extra')])

    def test_boot_nbft_bad_target(self):
        rc, _, err = self.run_cli('boot', 'nbft', 'x', '--target-addr', 'y')
        self.assertEqual(rc, 2)
        self.assertIn('bad --target-addr', err)

    def test_boot_test_list(self):
        rc, out, _ = self.run_cli('boot', 'test', '--list', '--tag', 'nbft')
        self.assertEqual(rc, 0)
        self.assertIn('nbft-nonboot', out)
        self.assertNotIn('static-hostonly', out)
        rc, _, err = self.run_cli('boot', 'test', '--list', '--tag', 'nope')
        self.assertEqual(rc, 2)
        self.assertIn('no scenario selected', err)

    def test_boot_run_slot_dry_run(self):
        rc, out, _ = self.run_cli('boot', 'run', 'c1', '--slot', '2',
                                  '--dry-run')
        self.assertEqual(rc, 0)
        inner = shlex.split(shlex.split(out)[-1])
        self.assertEqual(inner[2], 'lab-boot-2')
        self.assertEqual(inner[inner.index('--slot') + 1], '2')

    def test_boot_bad_slot_count(self):
        with contextlib.redirect_stderr(io.StringIO()) as err:
            with self.assertRaises(SystemExit):
                cli.main(['--config', self.lab, 'boot', 'setup', '--slots',
                          '0'])
        self.assertIn('not a number >= 1', err.getvalue())

    def test_boot_run_static_needs_hostonly(self):
        rc, _, err = self.run_cli('boot', 'run', 'c1', '--source', 'static',
                                  '--dry-run')
        self.assertEqual(rc, 2)
        self.assertIn('needs --hostonly', err)

    def test_parity_dry_run(self):
        rc, out, _ = self.run_cli('parity', 'pr-1', '--dry-run')
        self.assertEqual(rc, 0)
        lines = out.splitlines()
        self.assertEqual([ln.split(':')[0] for ln in lines],
                         ['deb', 'tw', 'muon', 'val'])
        for line, tree in zip(lines, ('deb', 'tw', 'muon', 'val')):
            argv = shlex.split(line.split(': ', 1)[1])
            self.assertEqual(argv[argv.index('--') - 1], 'root@a.lan')
            docker = shlex.split(argv[-1])
            self.assertIn(f'/root/scratch/pr-1-{tree}:/src', docker)
        self.assertIn('ghcr.io/linux-nvme/tumbleweed:latest',
                      shlex.split(shlex.split(
                          lines[1].split(': ', 1)[1])[-1]))

    def test_parity_only(self):
        rc, out, _ = self.run_cli('parity', 'pr-1', '--only', 'muon',
                                  '--dry-run')
        self.assertEqual(rc, 0)
        self.assertEqual([ln.split(':')[0] for ln in out.splitlines()],
                         ['muon'])

    def test_parity_unknown_job(self):
        rc, _, err = self.run_cli('parity', 'pr-1', '--only', 'nope',
                                  '--dry-run')
        self.assertEqual(rc, 2)
        self.assertIn("unknown job 'nope'", err)

    def test_ci_needs_command(self):
        rc, _, err = self.run_cli('ci', 'debian', 't', '--dry-run')
        self.assertEqual(rc, 2)
        self.assertIn('no command', err)

    def test_bad_scratch_name(self):
        rc, _, err = self.run_cli('ci', 'debian', '../x', '--', 'true')
        self.assertEqual(rc, 2)
        self.assertIn('bad scratch name', err)

    def test_unknown_host(self):
        rc, _, err = self.run_cli('ssh', 'nope', 'true')
        self.assertEqual(rc, 2)
        self.assertIn("unknown host 'nope'", err)


if __name__ == '__main__':
    unittest.main()
