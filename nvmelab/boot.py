"""'lab boot': boot a QEMU guest from an NVMe/TCP root, with dracut.

'lab boot setup' creates a container on the CI host (Tumbleweed:
dracut, a kernel, QEMU, nvme-cli's build tools; the host's /dev/kvm), a
subsystem on the target host for the guest's host NQN only, exported on
two ports (two paths, for multipath), and a small root file system on
its namespace. The target is set up with nvme-target-control
(target.py). With --slots N, it makes N such environments, which boot
side by side (slots.py; 'lab boot test -j N'). 'lab boot run' builds an
initrd and boots it: the case passes when the guest's /init on the
NVMe/TCP root runs. A run can install an nvme-cli tree that 'lab sync'
put on the CI host, and overlay dracut's nvmf module with a local
directory, and give the guest an NBFT that points at the root
subsystem ('--nbft'; nbft.py writes it, QEMU passes it on as an ACPI
table). 'lab boot nbft' writes such a table to a local file. Setup also
makes a second subsystem for the guest, with no root on it, for the
cases with more than one subsystem. The root's /init reports the NVMe
controllers and the nvme-cli registry it finds after switch-root. A run
can break paths while the guest boots (faults.py): on the target with
nvme-target-control, on the network with nvmf-failover. 'lab boot test'
runs the scenarios of boot/scenarios (scenario.py). 'lab boot teardown'
removes it all.

The scripts that do the work are in boot/ of this repo.
"""

import shlex
import subprocess
import sys
import uuid
from dataclasses import dataclass, field
from pathlib import Path

import hashlib
import io
import queue
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from . import faults, guest, nbft, remote, scenario, sync
from .config import ConfigError
from .slots import (BOOT_DIR, CONTAINER_DIR, IMAGE, OLD_CONTAINER, OLD_NQNS,
                    Slot, slots)
from .target import TargetControl

SCRIPTS = Path(__file__).resolve().parent.parent / 'boot'

# The paths of setup: the root subsystem on two ports, the extra one (no
# root, for the cases with more than one subsystem) on one
ROOT_PORTS, EXTRA_PORTS = 2, 1
ROOT_UUID = 'b0936c83-d08a-4c81-bb63-efc6e619581f'
SOURCES = ('cmdline', 'static', 'legacy', 'none')
NETWORKS = ('networkd', 'legacy', 'nm')

# The guest's NIC and QEMU's user networking (boot.sh): the HFI of an NBFT
GUEST_MAC = '52:54:00:12:34:56'
GUEST_PCI_DEV = 3
GUEST_IP, GUEST_PREFIX, GUEST_GATEWAY = '10.0.2.15', 24, '10.0.2.2'
# The guest's NIC: QEMU's user network (slirp), or a tap device on the
# host (labtapN for slot N), which can carry a VLAN: slirp drops tagged
# frames. The tap's subnets, 10.100.N.0/24 untagged and 10.101.N.0/24 on
# the VLAN, reach the target through the host's NAT (ensure_tap_network).
NICS = ('user', 'tap')
TAP_NET = '10.100.0.0/15'


def guest_net(slot, nic='user', vlan=0):
    """(address, prefix, gateway) of the guest's NIC in SLOT; the
    gateway is the host's end of the tap."""
    if nic == 'user':
        return GUEST_IP, GUEST_PREFIX, GUEST_GATEWAY
    net = 101 if vlan else 100
    return f'10.{net}.{slot.n}.2', 24, f'10.{net}.{slot.n}.1'


def check_nic(nic, vlan):
    if nic not in NICS:
        raise ConfigError(f"bad nic '{nic}': {', '.join(NICS)}")
    if not 0 <= vlan <= 4094:
        raise ConfigError(f'bad vlan {vlan}: 1-4094, 0 for none')
    if vlan and nic != 'tap':
        raise ConfigError('a vlan needs nic = "tap": the user network '
                          'drops tagged frames')


TAP_NETWORK_FILE = '/etc/systemd/network/10-lab-tap.network'
TAP_NETWORK = """\
# nvme-lab: the guests' taps (labtapN) and their VLANs are lab boot's
[Match]
Name=labtap*

[Link]
Unmanaged=yes
"""


def ensure_tap_network(cfg, host):
    """What the guests on taps need on HOST: systemd-networkd leaves the
    taps alone (a catch-all .network, e.g. Name=!lo with DHCP, takes them
    over and removes the address boot.sh gives them), 8021q for the
    VLANs, the forwarding of their subnets (Docker's FORWARD policy is
    DROP, its DOCKER-USER chain comes first) and NAT to the target, which
    knows no route back. Idempotent, and under a lock: the boots of a
    run call it side by side."""
    rules = [('filter', 'DOCKER-USER', f'-s {TAP_NET} -j ACCEPT'),
             ('filter', 'DOCKER-USER', f'-d {TAP_NET} -j ACCEPT'),
             ('nat', 'POSTROUTING',
              f'-s {TAP_NET} ! -d {TAP_NET} -j MASQUERADE')]
    unit = shlex.quote(TAP_NETWORK)
    script = [f'{{ [ ! -d /etc/systemd/network ] || printf %s {unit} '
              f'| cmp -s - {TAP_NETWORK_FILE} || {{ printf %s {unit} '
              f'> {TAP_NETWORK_FILE} && {{ networkctl reload 2>/dev/null '
              f'|| true; }}; }}; }}',
              'modprobe 8021q']
    for table, chain, rule in rules:
        script.append(f'{{ iptables -t {table} -C {chain} {rule} 2>/dev/null '
                      f'|| iptables -t {table} -I {chain} {rule}; }}')
    remote.run(cfg, host, ['flock', '/run/lab-boot-tap.lock', 'sh', '-c',
                           ' && '.join(script)], check=True)


def upload_argv(cfg, host, dest):
    """ssh argv that unpacks a tar from stdin into DEST on HOST."""
    return remote.ssh_argv(cfg, host, f'rm -rf {shlex.quote(dest)} && '
                           f'mkdir -p {shlex.quote(dest)} && '
                           f'tar -C {shlex.quote(dest)} -xf -')


def upload(cfg, host, src, dest):
    """Copy the directory SRC to DEST on HOST, with tar (scp flattens)."""
    if not Path(src).is_dir():
        raise ConfigError(f'not a directory: {src}')
    tar = subprocess.Popen(['tar', '-C', str(src), '-cf', '-', '.'],
                           stdout=subprocess.PIPE)
    subprocess.run(upload_argv(cfg, host, dest), stdin=tar.stdout,
                   check=True)
    tar.stdout.close()
    if tar.wait():
        raise ConfigError(f'tar of {src} failed')


def exec_argv(slot, script, *args):
    """The docker exec that runs SCRIPT of boot/ in SLOT's container."""
    return ['docker', 'exec', slot.container, 'sh',
            f'{slot.container_dir}/scripts/{script}', *args]


@dataclass
class Env:
    """What setup made for a slot: the target's address, the name of the
    target host, and the paths as {name: (port ID, service ID)}: root,
    root2 (the root subsystem's second port), extra."""
    traddr: str
    target_host: str
    paths: dict = field(default_factory=dict)
    slot: Slot = Slot()

    @property
    def target(self):
        """(TRADDR, TRSVCID) of the root subsystem's first path."""
        return self.traddr, self.trsvcid('root')

    def trsvcid(self, path):
        return self.path(path)[1]

    def path(self, name):
        if name not in self.paths:
            raise ConfigError(f"no path '{name}' (paths: "
                              f"{', '.join(self.paths)}); run 'lab boot "
                              'setup\' again?')
        return self.paths[name]

    def text(self):
        def ports(names):
            return ','.join(':'.join(self.paths[n]) for n in names
                            if n in self.paths)
        return (f'SLOT={self.slot.n}\nTRADDR={self.traddr}\n'
                f'TARGET_HOST={self.target_host}\n'
                f"ROOT_PORTS={ports(['root', 'root2'])}\n"
                f"EXTRA_PORTS={ports(['extra'])}\n")


def path_names(ports, names):
    """{name: (port ID, service ID)} for PORTS, a list of such pairs."""
    return dict(zip(names, ports))


def parse_env(text):
    """The target.env that setup wrote, as an Env."""
    values = dict(line.split('=', 1) for line in text.split() if '=' in line)
    if 'ROOT_PORTS' not in values:
        raise ConfigError("no target, or one of an older lab: run 'lab boot "
                          "setup'")

    def ports(value):
        return [tuple(p.split(':', 1)) for p in value.split(',') if p]

    env = Env(values['TRADDR'], values.get('TARGET_HOST', 'nvmet'),
              slot=Slot(int(values.get('SLOT', 1))))
    env.paths |= path_names(ports(values['ROOT_PORTS']), ['root', 'root2'])
    env.paths |= path_names(ports(values.get('EXTRA_PORTS', '')), ['extra'])
    return env


def resolve_ssns_spec(spec, extra, slot=Slot()):
    """'extra[,FLAG...]' of --nbft-ssns: the extra subsystem of SLOT at
    its port EXTRA."""
    name, sep, flags = spec.partition(',')
    if name != 'extra':
        return spec
    if not extra:
        raise ConfigError("no extra subsystem: run 'lab boot setup' again")
    return f'{extra}/{slot.extra_nqn}{sep}{flags}'


def nbft_table(target, root=True, ssns=(), discovery=(), extra=None,
               slot=Slot(), nic='user', vlan=0):
    """The NBFT of a boot case in SLOT: the guest's NIC as the HFI, the
    root subsystem (unless not ROOT) and the SSNS specs of 'lab boot run
    --nbft-ssns' as the SSNS descriptors, discovery controllers given as
    [TRADDR:]TRSVCID. EXTRA: the port of the extra subsystem. NIC and
    VLAN: the guest's NIC (guest_net()) and the HFI's VLAN."""
    traddr, trsvcid = target
    ssns = [resolve_ssns_spec(s, extra, slot) for s in ssns]
    check_nic(nic, vlan)
    ipaddr, prefix, gateway = guest_net(slot, nic, vlan)
    hfi = nbft.Hfi(mac=GUEST_MAC, ipaddr=ipaddr, prefix=prefix,
                   gateway=gateway, vlan=vlan, pci_sbdf=GUEST_PCI_DEV << 3)
    table = nbft.Table(host_nqn=slot.host_nqn, host_id=slot.host_id,
                       hfis=[hfi])
    if root:
        table.ssns.append(nbft.Ssns(traddr, trsvcid, slot.root_nqn))
    try:
        table.ssns += [nbft.parse_ssns_spec(s, traddr) for s in ssns]
        # The namespace identifier of the namespaces of setup: the target
        # gives them the subsystem's UUID (nvme-target-control).
        for s in table.ssns:
            ns_uuid = {slot.root_nqn: slot.root_subsys_uuid,
                       slot.extra_nqn: slot.extra_subsys_uuid}.get(s.nqn)
            if ns_uuid:
                s.nid_type, s.nid = 3, uuid.UUID(ns_uuid).bytes
        for d in discovery:
            addr, _, port = d.rpartition(':')
            if not port.isdigit():
                raise nbft.NbftError(f"bad --nbft-discovery '{d}': "
                                     '[TRADDR:]TRSVCID')
            table.discovery.append(
                nbft.Discovery(f'nvme+tcp://{addr or traddr}:{port}/'))
        if not table.ssns and not table.discovery:
            raise nbft.NbftError('an NBFT without SSNS and discovery '
                                 'descriptors points at nothing')
        return nbft.build(table)
    except nbft.NbftError as e:
        raise ConfigError(str(e)) from None


def run_argv(name, target, tree=None, package=False, module=None,
             source='cmdline', hostonly=False, append=None, timeout=None,
             nbft_file=None, host_identity=True, network='networkd',
             ip_dev=None, nic='user', vlan=0, nm_generator=None,
             slot=Slot()):
    """The docker exec of one boot case in SLOT; MODULE is the overlay's
    directory and NBFT_FILE the table in the container."""
    if source not in SOURCES:
        raise ConfigError(f"bad --source '{source}': {', '.join(SOURCES)}")
    if source in ('static', 'legacy') and not hostonly:
        raise ConfigError(f'--source {source} needs --hostonly: only a '
                          "hostonly initrd carries the host's /etc/nvme")
    if tree and package:
        raise ConfigError('--nvme and --nvme-package exclude each other')
    if network not in NETWORKS:
        raise ConfigError(f"bad network '{network}': {', '.join(NETWORKS)}")
    args = ['--name', sync.check_name(name), '--target', ':'.join(target),
            '--root-uuid', ROOT_UUID, '--source', source,
            '--slot', str(slot.n)]
    if tree:
        args += ['--nvme-tree', f'/scratch/{sync.check_name(tree)}']
    if package:
        args.append('--nvme-package')
    if module:
        args += ['--dracut-module', module]
    if hostonly:
        args.append('--hostonly')
    if not host_identity:
        args.append('--no-host-identity')
    if append:
        args += ['--append', append]
    if timeout:
        args += ['--timeout', str(timeout)]
    if nbft_file:
        args += ['--nbft', nbft_file]
    if network != 'networkd':
        args += ['--network', network]
    if ip_dev:
        if not re.fullmatch(r'[A-Za-z0-9.:_-]+', ip_dev):
            raise ConfigError(f"bad ip_dev '{ip_dev}'")
        args += ['--ip-dev', ip_dev]
    if nm_generator:
        args += ['--nm-generator', nm_generator]
    check_nic(nic, vlan)
    if nic != 'user':
        ipaddr, prefix, gateway = guest_net(slot, nic, vlan)
        args += ['--nic', nic, '--guest-ip', f'{ipaddr}/{prefix}',
                 '--gateway', gateway]
        if vlan:
            args += ['--vlan', str(vlan)]
    return exec_argv(slot, 'boot.sh', *args)


def target_address(cfg, host, target_host):
    """The target's address as the CI host resolves it: the guest's user
    networking goes out through the CI host."""
    addr = remote.run(cfg, host, ['getent', 'ahostsv4', target_host.address],
                      check=True, capture_output=True, encoding='utf-8')
    return addr.stdout.split()[0]


def existing_slots(cfg, host):
    """The slots setup made on HOST, from their directories."""
    out = remote.run(cfg, host, f'ls -d {BOOT_DIR}/slot-* 2>/dev/null || :',
                     capture_output=True, encoding='utf-8')
    found = [int(m.group(1)) for m in
             map(re.compile(r'/slot-(\d+)$').search, out.stdout.split()) if m]
    return [Slot(n) for n in sorted(found)]


def remove_slot(cfg, host, tc, slot):
    """Remove SLOT's container, subsystems and directory."""
    remote.run(cfg, host, ['docker', 'rm', '-f', slot.container],
               capture_output=True)
    for nqn in (slot.root_nqn, slot.extra_nqn):
        tc.cleanup(nqn, check=False)
    remote.run(cfg, host, ['rm', '-rf', '--', slot.dir], check=True)


def remove_old_lab(cfg, host, target_host, tc):
    """What the lab made before slots: one container, two subsystems."""
    remote.run(cfg, host, ['docker', 'rm', '-f', OLD_CONTAINER],
               capture_output=True)
    for nqn in OLD_NQNS:
        remote.run(cfg, target_host, ['nvmet-target', 'del-subsys', nqn],
                   capture_output=True)
        tc.cleanup(nqn, check=False)
    remote.run(cfg, host, f'rm -rf {BOOT_DIR}/scripts {BOOT_DIR}/target.env',
               check=True)


def setup_container(cfg, host, slot, recreate=False, image=None):
    upload(cfg, host, SCRIPTS, f'{slot.dir}/scripts')
    argv = ['sh', f'{slot.dir}/scripts/container.sh', slot.container,
            slot.host_nqn, slot.host_id]
    if recreate:
        argv.append('--recreate')
    if image:
        argv += ['--image', image]
    remote.run(cfg, host, argv, check=True)


def setup_slot(cfg, host, tc, traddr, target_host, slot):
    """SLOT's subsystems, target.env and root file system."""
    for nqn in (slot.root_nqn, slot.extra_nqn):
        tc.cleanup(nqn, check=False)
    tc.setup(slot.root_nqn, slot.root_subsys_uuid, slot.host_nqn,
             ports=ROOT_PORTS)
    tc.setup(slot.extra_nqn, slot.extra_subsys_uuid, slot.host_nqn,
             ports=EXTRA_PORTS)
    env = Env(traddr, target_host.name, slot=slot)
    env.paths |= path_names(tc.ports(slot.root_nqn), ['root', 'root2'])
    env.paths |= path_names(tc.ports(slot.extra_nqn), ['extra'])
    write_env(cfg, host, env)
    make_root(cfg, host, slot, env)
    print(f'lab-boot slot {slot.n}: root on {slot.root_nqn} at '
          f"{traddr}:{env.trsvcid('root')} and :{env.trsvcid('root2')}, "
          f"extra at :{env.trsvcid('extra')}", flush=True)


def mkroot_sum():
    return hashlib.sha256((SCRIPTS / 'mkroot.sh').read_bytes()).hexdigest()


def make_root(cfg, host, slot, env):
    """SLOT's root file system, by mkroot.sh of this checkout; its
    checksum goes next to it, see ensure_root()."""
    remote.run(cfg, host, exec_argv(slot, 'mkroot.sh', env.traddr,
                                    env.trsvcid('root'), slot.root_nqn,
                                    ROOT_UUID), check=True)
    remote.run(cfg, host, f'echo {mkroot_sum()} > {slot.dir}/mkroot.sum',
               check=True)


def ensure_root(cfg, host, slot):
    """Make SLOT's root anew if mkroot.sh changed since it was made: the
    root's /init writes the report the scenarios check, and a root of an
    older mkroot.sh would report without what a newer one added. Call
    after the scripts were uploaded."""
    out = remote.run(cfg, host, ['cat', f'{slot.dir}/mkroot.sum'],
                     capture_output=True, encoding='utf-8')
    if out.stdout.strip() == mkroot_sum():
        return
    print(f'lab-boot slot {slot.n}: mkroot.sh changed, making the root '
          'anew', flush=True)
    make_root(cfg, host, slot, read_env(cfg, host, slot))


def setup(cfg, host, target_host, recreate=False, count=1):
    """COUNT slots. The first container is built from the CI image,
    the others start from a copy of it (IMAGE)."""
    wanted = slots(count)
    traddr = target_address(cfg, host, target_host)
    tc = TargetControl(cfg, target_host, traddr)
    remove_old_lab(cfg, host, target_host, tc)
    for slot in existing_slots(cfg, host):
        if slot not in wanted:
            remove_slot(cfg, host, tc, slot)
    setup_container(cfg, host, wanted[0], recreate=recreate)
    if count > 1:
        remote.run(cfg, host, ['docker', 'commit', wanted[0].container,
                               IMAGE], check=True, capture_output=True)
        for slot in wanted[1:]:
            setup_container(cfg, host, slot, recreate=True, image=IMAGE)
    # A fault of an interrupted run
    faults.clear_network(cfg, host)
    ensure_tap_network(cfg, host)
    for slot in wanted:
        setup_slot(cfg, host, tc, traddr, target_host, slot)


def write_env(cfg, host, env):
    remote.run(cfg, host, f'cat > {env.slot.dir}/target.env',
               input=env.text().encode(), check=True)


def read_env(cfg, host, slot=Slot()):
    """parse_env() of SLOT's target.env on HOST."""
    out = remote.run(cfg, host, ['cat', f'{slot.dir}/target.env'],
                     capture_output=True, encoding='utf-8')
    return parse_env(out.stdout)


def nbft_path(name):
    """The NBFT of case NAME: on the CI host, in the container."""
    name = sync.check_name(name)
    return f'{BOOT_DIR}/nbft/{name}', f'{CONTAINER_DIR}/nbft/{name}'


# The modules next to a --dracut-module in its tree (modules.d) that boot.sh
# uses with it: the network modules of the boots (network = "legacy" and
# networkd), whose handling of what nvmf writes is part of the test.
SIBLINGS = ('35network-legacy', '11systemd-networkd', '35network-manager')


def upload_siblings(cfg, host, module, dest):
    """SIBLINGS of MODULE's tree to DEST, for boot.sh; none: no DEST."""
    remote.run(cfg, host, ['rm', '-rf', dest], check=True)
    for m in SIBLINGS:
        src = Path(module).resolve().parent / m
        if src.is_dir():
            upload(cfg, host, src, f'{dest}/{m}')


def run(cfg, host, name, module=None, nbft_options=None, fault_specs=(),
        slot=Slot(), out=None, synced=False, **kwargs):
    """Boot case NAME in SLOT. NBFT_OPTIONS: the dict of nbft_table()'s
    options, or None for no NBFT. FAULT_SPECS: the faults to put down
    while the guest boots (faults.parse()). OUT: a text stream for the
    output (default: stdout). SYNCED: the nvmf-failover tree is on the CI
    host already. Returns boot.sh's exit status."""
    env = read_env(cfg, host, slot)
    target, extra = env.target, env.paths.get('extra', (None, None))[1]
    overlay = table = None
    # The scripts of this checkout, not the ones of the last setup
    upload(cfg, host, SCRIPTS, f'{slot.dir}/scripts')
    ensure_root(cfg, host, slot)
    if module:
        dest = f'{BOOT_DIR}/modules/{sync.check_name(name)}'
        upload(cfg, host, module, dest)
        overlay = f'{CONTAINER_DIR}/modules/{name}'
        upload_siblings(cfg, host, module, f'{dest}.siblings')
    if kwargs.get('nic', 'user') == 'tap':
        ensure_tap_network(cfg, host)
    if nbft_options is not None:
        data = nbft_table(target, extra=extra, slot=slot,
                          nic=kwargs.get('nic', 'user'),
                          vlan=kwargs.get('vlan', 0), **nbft_options)
        on_host, table = nbft_path(name)
        remote.run(cfg, host, f'mkdir -p {BOOT_DIR}/nbft && '
                   f'cat > {shlex.quote(on_host)}', input=data, check=True)
    argv = run_argv(name, target, module=overlay, nbft_file=table,
                    slot=slot, **kwargs)
    with faults.Faults(cfg, host, env, fault_specs, synced=synced, out=out,
                       on_change=lambda e: write_env(cfg, host, e)):
        if out is None:
            return remote.run(cfg, host, argv).returncode
        done = remote.run(cfg, host, argv, stdout=subprocess.PIPE,
                          stderr=subprocess.STDOUT, encoding='utf-8',
                          errors='replace')
        out.write(done.stdout)
        return done.returncode


def read_state(cfg, host, name):
    """The state.txt of case NAME: what the root's /init saw."""
    out = remote.run(cfg, host, ['cat', '--', f'{BOOT_DIR}/out/'
                                 f'{sync.check_name(name)}/state.txt'],
                     capture_output=True, encoding='utf-8')
    return out.stdout


@dataclass
class Tree:
    """An nvme-cli to boot with: a tree of 'lab sync' (NAME), the
    distribution's package, or what is installed (neither)."""
    name: str | None = None
    package: bool = False
    version: str | None = None      # the release tag the tree is at
    major: int | None = None
    nm: str | None = None           # the NetworkManager tree of --nm

    @property
    def label(self):
        return self.name or ('package' if self.package else 'installed')

    @property
    def keys(self):
        """What a scenario's xfail_on names this tree by: its release
        tag, its label (the tree's name, or "package"), and the
        NetworkManager it boots with: "nm-package", or "nm-" and the name
        of the tree of --nm."""
        nm = f'nm-{self.nm}' if self.nm else 'nm-package'
        return tuple(k for k in (self.version, self.label, nm) if k)


def tree_info(cfg, host, name):
    """The Tree NAME, with the version 'lab sync' recorded."""
    d = sync.scratch_dir(sync.check_name(name))
    out = remote.run(cfg, host, f'cat {d}/.git/lab-describe 2>/dev/null',
                     capture_output=True, encoding='utf-8')
    version, major = sync.parse_describe(out.stdout)
    return Tree(name, version=version, major=major)


NM_BUILDER = 'lab-nm-build'
NM_BUILD_IMAGE = 'ghcr.io/linux-nvme/tumbleweed:latest'


def build_nm(cfg, host, tree, slot=Slot()):
    """nm-initrd-generator of the NetworkManager TREE (lab sync), built
    in the container NM_BUILDER (made on first use, Tumbleweed as the
    slots) with SLOT's uploaded scripts. Returns its path in the
    containers (/scratch/TREE/...)."""
    name = sync.check_name(tree)
    remote.run(cfg, host, f'docker inspect {NM_BUILDER} >/dev/null 2>&1 '
               f'&& docker start {NM_BUILDER} >/dev/null || docker run -d '
               f'--name {NM_BUILDER} --network host -v /root/scratch:/scratch '
               f'{NM_BUILD_IMAGE} sleep infinity >/dev/null', check=True)
    done = remote.run(cfg, host, ['docker', 'exec', NM_BUILDER, 'sh',
                                  f'{slot.container_dir}/scripts/nm-build.sh',
                                  f'/scratch/{name}'],
                      capture_output=True, encoding='utf-8', errors='replace')
    if done.returncode != 0:
        raise ConfigError(f'building NetworkManager {tree} failed:\n'
                          f'{done.stderr}')
    print(f'build NetworkManager {tree}: ok, {done.stderr.strip()}',
          flush=True)
    return done.stdout.strip().splitlines()[-1]


def build_argv(slot, tree):
    """Build the tree in SLOT's container, install nothing."""
    return exec_argv(slot, 'boot.sh', '--build-only', '--nvme-tree',
                     f'/scratch/{sync.check_name(tree)}')


def expected_duration(s):
    """A rough rank of how long scenario S boots: a shutdown scenario
    boots a whole system, a boot that must fail waits for dracut to give
    up, faults cost extra steps."""
    return (s.guest is not None, s.expect.get('result', 'pass') == 'fail',
            bool(s.faults), bool(s.boot.get('timeout')))


@dataclass
class Case:
    """The result of a scenario with a tree."""
    tree: Tree
    scenario: object
    name: str
    result: str                 # PASS, FAIL, XFAIL, XPASS
    bad: bool                   # counts as failed
    errors: list = field(default_factory=list)
    reason: str | None = None   # why it is expected to fail
    duration: float = 0.0


def test_one(cfg, host, s, tree, slot, name, out, **options):
    """Boot scenario S with TREE in SLOT as case NAME and write its
    verdict to OUT. Returns the outcome, whether it counts as failed and
    what was wrong."""
    env = read_env(cfg, host, slot)
    label = f' [{tree.label}]' if options.pop('show_tree', False) else ''
    print(f'--- {s.id}{label}: {s.name} (slot {slot.n})', file=out,
          flush=True)
    if s.guest is not None:
        return test_guest(cfg, host, s, tree, slot, env, name, out, label,
                          options.get('module'))
    rc = run(cfg, host, name, slot=slot, out=out, tree=tree.name,
             package=tree.package, **options,
             **scenario.run_options(s, env))
    if rc not in (0, 1):
        errors = [f'could not be set up (exit {rc}), see '
                  f'{BOOT_DIR}/out/{name}/log']
    else:
        text = read_state(cfg, host, name)
        state = scenario.parse_state(text)
        # read again: a fault may have moved a path to another port
        env = read_env(cfg, host, slot)
        errors = scenario.verdict(s, rc == 0, state, slot.roles,
                                  scenario.placeholders(env),
                                  scenario.parse_pre_os_conf(text),
                                  scenario.parse_iface_names(text))
    # A case that could not be set up is no expected failure
    result, bad = (('FAIL', True) if rc not in (0, 1)
                   else scenario.outcome(s, errors, tree.keys))
    print(f'{result} {s.id}{label}', file=out, flush=True)
    if result == 'XFAIL':
        print(f'    expected: {scenario.xfail_reason(s, tree.keys)}',
              file=out)
    if result == 'XPASS':
        print('    passes now, update the scenario: '
              f'{scenario.xfail_reason(s, tree.keys)}', file=out)
    for e in errors:
        print(f'    {e}', file=out)
    return result, bad, errors


def test_guest(cfg, host, s, tree, slot, env, name, out, label,
               module=None):
    """test_one() of a shutdown or hand-over scenario (guest.py)."""
    rc = guest.run(cfg, host, s, tree, slot, env, name, out, module)
    handover = guest.handover_kind(s)
    if rc not in (0, 1):
        errors = [f'could not be set up (exit {rc}), see '
                  f'{BOOT_DIR}/out/{name}/log']
        facts = {}
    elif handover:
        facts = guest.parse_handover(read_state(cfg, host, name))
        facts['order'] = (f"umount={facts.get('root_umount', '-')} "
                          f"pre-os-disconnect="
                          f"{facts.get('pre_os_disconnect', '-')}")
        errors = guest.handover_verdict(
            s, facts, guest.LABFC_NQN if handover == 'fc' else slot.root_nqn)
    else:
        facts = guest.parse_state(read_state(cfg, host, name))
        errors = guest.verdict(s, facts)
    result, bad = (('FAIL', True) if rc not in (0, 1)
                   else scenario.outcome(s, errors, tree.keys))
    print(f'{result} {s.id}{label}', file=out, flush=True)
    if facts.get('order'):
        print(f"    shutdown: {facts.get('shutdown_seconds', '-')}s, "
              f"order {facts['order']}", file=out)
    if result == 'XFAIL':
        print(f'    expected: {scenario.xfail_reason(s, tree.keys)}',
              file=out)
    if result == 'XPASS':
        print('    passes now, update the scenario: '
              f'{scenario.xfail_reason(s, tree.keys)}', file=out)
    for e in errors:
        print(f'    {e}', file=out)
    return result, bad, errors


def scenario_major(tree, s):
    """The nvme-cli major version scenario S runs with TREE."""
    return guest.major(tree, s) if s.guest is not None else tree.major


def test(cfg, host, scenarios, prefix='t', module=None, trees=(),
         package=False, jobs=1, nm=None):
    """Run SCENARIOS with each of TREES (names of 'lab sync'; none: the
    package with PACKAGE, else what is installed), JOBS at a time, each
    in a free slot; print a verdict for each. A scenario for another
    nvme-cli major version than a tree's is left out for it. The trees
    are built first, once each. NM: a NetworkManager tree whose
    nm-initrd-generator the boots use. Returns the Cases, in the order of
    the trees and scenarios."""
    available = existing_slots(cfg, host)
    if not available:
        raise ConfigError("no slot: run 'lab boot setup' first")
    if jobs > len(available):
        raise ConfigError(f'-j {jobs}, but setup made {len(available)} '
                          f"slot(s): 'lab boot setup --slots {jobs}'")
    infos = ([tree_info(cfg, host, t) for t in trees] if trees
             else [Tree(package=package)])
    for t in infos:
        t.nm = nm
    many = len(infos) > 1
    cases = [(t, s) for t in infos for s in scenarios
             if scenario.applies(s, scenario_major(t, s))]
    # The longest first, so that they don't end the run alone
    cases.sort(key=lambda c: expected_duration(c[1]), reverse=True)

    free = queue.Queue()
    for slot in available[:jobs]:
        # The scripts of this checkout, also for the builds below
        upload(cfg, host, SCRIPTS, f'{slot.dir}/scripts')
        ensure_root(cfg, host, slot)
        free.put(slot)
    lock = threading.Lock()

    def emit(out):
        if out is not None:
            with lock:
                print(out.getvalue(), end='', flush=True)

    def build(tree):
        slot = free.get()
        try:
            done = remote.run(cfg, host, build_argv(slot, tree.name),
                              capture_output=True, encoding='utf-8',
                              errors='replace')
        finally:
            free.put(slot)
        with lock:
            state = 'ok' if done.returncode == 0 else 'FAILED'
            version = f' ({tree.version})' if tree.version else ''
            print(f'build {tree.label}{version}: {state}', flush=True)
            if done.returncode:
                print(done.stdout[-2000:], done.stderr[-2000:], sep='')
        return done.returncode == 0

    # The trees of boot scenarios build in the containers, the ones of
    # shutdown scenarios in the Debian CI image, per flavor.
    built = [t for t in infos if t.name and any(
        c[0] is t and c[1].guest is None for c in cases)]
    with ThreadPoolExecutor(max_workers=max(1, min(jobs, len(built)))) as p:
        ok = dict(zip((t.name for t in built), p.map(build, built)))

    def build_guest(key):
        tree, kind = key
        slot = free.get()
        try:
            done = remote.run(cfg, host, guest.build_argv(slot, tree.name,
                                                          kind),
                              capture_output=True, encoding='utf-8',
                              errors='replace')
        finally:
            free.put(slot)
        with lock:
            state = 'ok' if done.returncode == 0 else 'FAILED'
            print(f'build {tree.label} for the Debian guest ({kind}): '
                  f'{state}', flush=True)
            if done.returncode:
                print(done.stdout[-2000:], done.stderr[-2000:], sep='')
        return done.returncode == 0

    guest_builds = list({(t.name, guest.flavor(s)): (t, guest.flavor(s))
                         for t, s in cases
                         if s.guest is not None and t.name}.values())
    if any(s.guest is not None for _, s in cases):
        guest.ensure_base(cfg, host, available[0])
    with ThreadPoolExecutor(
            max_workers=max(1, min(jobs, len(guest_builds)))) as p:
        ok_guest = dict(zip(((t.name, k) for t, k in guest_builds),
                            p.map(build_guest, guest_builds)))

    if any(f['kind'] in faults.NETWORK for _, s in cases for f in s.faults):
        faults.sync_failover(cfg, host)
    options = dict(module=module, synced=True, show_tree=many)
    if nm:
        options['nm_generator'] = build_nm(cfg, host, nm, available[0])

    def one(case):
        tree, s = case
        name = sync.check_name(f'{prefix}-{tree.name}-{s.id}' if many
                               else f'{prefix}-{s.id}')
        out = io.StringIO() if jobs > 1 else None
        stream = out or sys.stdout
        built_ok = (ok_guest.get((tree.name, guest.flavor(s)))
                    if s.guest is not None else ok.get(tree.name))
        if tree.name and not built_ok:
            print(f'FAIL {s.id} [{tree.label}]\n    the build failed',
                  file=stream)
            emit(out)
            return Case(tree, s, name, 'FAIL', True, ['the build failed'])
        slot = free.get()
        start = time.monotonic()
        try:
            result, bad, errors = test_one(cfg, host, s, tree, slot, name,
                                           stream, **dict(options))
        except ConfigError as e:
            print(f'FAIL {s.id}\n    {e}', file=stream)
            result, bad, errors = 'FAIL', True, [str(e)]
        finally:
            free.put(slot)
        emit(out)
        return Case(tree, s, name, result, bad, errors,
                    scenario.xfail_reason(s, tree.keys),
                    time.monotonic() - start)

    with ThreadPoolExecutor(max_workers=jobs) as pool:
        results = list(pool.map(one, cases))
    order = {(t.label, s.id): i for i, (t, s) in
             enumerate((t, s) for t in infos for s in scenarios)}
    results.sort(key=lambda c: order[(c.tree.label, c.scenario.id)])

    def summary(rows):
        counts = {}
        for c in rows:
            counts[c.result] = counts.get(c.result, 0) + 1
        failed = [c.scenario.id for c in rows if c.bad]
        text = ', '.join(f'{n} {r}' for r, n in sorted(counts.items()))
        return (f'{len(rows)} scenarios: {text}'
                f"{'; failed: ' + ' '.join(failed) if failed else ''}")

    for t in infos:
        line = summary([c for c in results if c.tree is t])
        print(f'{t.label}: {line}' if many else line)
    if many:
        print(summary(results))
    return results


def teardown(cfg, host, target_host):
    traddr = target_address(cfg, host, target_host)
    tc = TargetControl(cfg, target_host, traddr)
    for slot in existing_slots(cfg, host):
        remove_slot(cfg, host, tc, slot)
    remove_old_lab(cfg, host, target_host, tc)
    faults.clear_network(cfg, host)
    remote.run(cfg, host, ['docker', 'rmi', '-f', IMAGE],
               capture_output=True)
    remote.run(cfg, host, ['rm', '-rf', '--', BOOT_DIR], check=True)
