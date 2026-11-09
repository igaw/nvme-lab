"""Shutdown scenarios: the OS shutdown of a Debian guest with an NVMe/TCP
data mount (nvme-cli#3309).

The boot scenarios end at switch-root; the shutdown of a running system
needs a real one. A scenario with a [guest] table (scenario.py) boots a
Debian 13 guest with systemd from a local disk, with ifupdown's
networking.service, as on a Debian server. nvme-cli connects the extra
subsystem of the slot at boot the way a user sets it up (its config
file and its autoconnect units, or nvme-discoverd), fstab mounts the
file system on it, and the guest powers off with systemctl. The case
passes when that happens in time and the console shows no sign of a
shutdown that lost the network under the mount (BAD_SIGNS).

The pieces, in boot/:
- guest-base.sh builds the guest once, on the CI host (GUEST_DIR), with
  debootstrap in nvme-cli's Debian CI image; Debian's nvme-cli (2.13)
  and lvm2 are in it as packages to install.
- guest-build.sh builds an nvme-cli tree in that image, for the guest
  (TREE/.lab-debian-FLAVOR-stage); FLAVOR is legacy (autoconnect) or
  discoverd.
- guest-image.sh makes the disk of a case, a qcow2 overlay, through
  qemu-nbd on the CI host: nvme-cli, its config, fstab, the guest's
  lab-shutdown.service.
- guest-boot.sh, in the slot's container, makes the data file system,
  boots the guest and writes what happened to state.txt.
"""

import shlex
import subprocess

from . import remote, sync
from .config import ConfigError
from .slots import BOOT_DIR, CONTAINER_DIR

GUEST_DIR = f'{BOOT_DIR}/debian'
GUEST_DIR_CONTAINER = f'{CONTAINER_DIR}/debian'
BUILD_IMAGE = 'ghcr.io/linux-nvme/debian:latest'
DATA_UUID = '5a3e9f0c-7d2b-4e61-9c8a-1f4b6d2e8a70'
# The nvme-cli of Debian 13, the guest's package
PACKAGE_MAJOR = 2
# What guest-boot.sh reports as "error" lines: a shutdown that took the
# network away under a mount (nvme-cli#3309: error -101, keep-alive
# failures, I/O that finds no path), I/O timeouts and stuck tasks or
# stop jobs. The defaults of the scenarios.
BAD_SIGNS = ('error -101', 'nvme_keep_alive_end_io', 'no available path',
             'I/O tag timeout', 'blocked for more than', 'A stop job',
             'Failed unmounting', 'timed out')
SHUTDOWN_LIMIT = 60
TIMEOUT = 300


def major(tree, s):
    """The nvme-cli major version scenario S gets with TREE."""
    return PACKAGE_MAJOR if tree.package else tree.major


def flavor(s):
    return 'discoverd' if s.guest.get('connect') == 'discoverd' else 'legacy'


def config_file(s, tree):
    return s.guest.get('config') or (
        'nvme-fabrics.conf' if major(tree, s) == 3 else 'discovery.conf')


def scripts_dir(slot):
    return f'{slot.dir}/scripts'


def ensure_base(cfg, host, slot):
    """Build the guest once (guest-base.sh does nothing when it exists)."""
    done = remote.run(cfg, host, ['sh', f'{scripts_dir(slot)}/guest-base.sh',
                                  GUEST_DIR], capture_output=True,
                      encoding='utf-8', errors='replace')
    if done.returncode:
        raise ConfigError('building the Debian guest failed:\n'
                          f'{done.stdout[-2000:]}{done.stderr[-2000:]}')


def build_argv(slot, tree, kind):
    """Build TREE as FLAVOR KIND in the Debian CI image."""
    script = f'{CONTAINER_DIR}/slot-{slot.n}/scripts/guest-build.sh'
    return ['docker', 'run', '--rm', '--network', 'host', '-v',
            '/root/scratch:/scratch', BUILD_IMAGE, 'sh', script,
            f'/scratch/{sync.check_name(tree)}', kind]


def stage_dir(tree, kind):
    return f'{sync.scratch_dir(sync.check_name(tree))}/.lab-debian-{kind}-stage'


def run(cfg, host, s, tree, slot, env, name, out):
    """Make the disk of shutdown case NAME (scenario S, TREE) and boot it
    in SLOT; write the output to OUT. Returns guest-boot.sh's exit
    status (2: could not be set up)."""
    from .boot import exec_argv
    g = s.guest
    target = f"{env.traddr}:{env.trsvcid('extra')}"
    outdir = f'{BOOT_DIR}/out/{sync.check_name(name)}'
    image = ['sh', f'{scripts_dir(slot)}/guest-image.sh',
             '--base', GUEST_DIR, '--out', outdir, '--nbd', str(slot.n - 1),
             '--hostnqn', slot.host_nqn, '--hostid', slot.host_id,
             '--nvme', 'package' if tree.package
             else stage_dir(tree.name, flavor(s)),
             '--connect', g.get('connect', 'autoconnect'),
             '--config', config_file(s, tree), '--target', target,
             '--data-uuid', DATA_UUID, '--fstab', g.get('fstab', 'nofail'),
             '--uptime', str(g.get('uptime', 5)),
             '--dirty-mb', str(g.get('dirty_mb', 256))]
    if g.get('lvm2'):
        image.append('--lvm2')
    if g.get('wait_online'):
        image.append('--wait-online')
    done = remote.run(cfg, host, image)
    if done.returncode:
        log = remote.run(cfg, host, f'tail -20 {shlex.quote(outdir)}/log',
                         capture_output=True, encoding='utf-8',
                         errors='replace')
        out.write(log.stdout)
        return 2
    try:
        argv = exec_argv(slot, 'guest-boot.sh', '--name', name,
                         '--base', GUEST_DIR_CONTAINER, '--target', target,
                         '--nqn', slot.extra_nqn, '--data-uuid', DATA_UUID,
                         '--timeout', str(g.get('timeout', TIMEOUT)),
                         '--slot', str(slot.n))
        done = remote.run(cfg, host, argv, stdout=subprocess.PIPE,
                          stderr=subprocess.STDOUT, encoding='utf-8',
                          errors='replace')
        out.write(done.stdout)
        return done.returncode
    finally:
        # The overlay is the only big file of a case
        remote.run(cfg, host, ['rm', '-f', f'{outdir}/disk.qcow2'])


def parse_state(text):
    """guest-boot.sh's state.txt: {key: value} of its facts, and the
    lists of its error, event and guest lines."""
    facts = {'error': [], 'event': [], 'guest': []}
    for line in text.splitlines():
        key, _, value = line.partition(' ')
        if key in ('error', 'event', 'guest'):
            facts[key].append(value)
        elif key:
            facts[key] = value
    return facts


def verdict(s, facts):
    """What is wrong with the run of shutdown scenario S (empty:
    nothing). FACTS: parse_state() of its state.txt."""
    problems = []
    limit = s.guest.get('shutdown_limit', SHUTDOWN_LIMIT)
    if facts.get('mounted') != 'yes':
        problems.append('the guest did not mount /srv/data')
    elif facts.get('poweroff') != 'yes':
        problems.append('the guest did not run systemctl poweroff')
    elif facts.get('powered_off') != 'yes':
        problems.append('no power-off: QEMU was killed after '
                        f"{s.guest.get('timeout', TIMEOUT)}s")
    elif int(facts.get('shutdown_seconds', '0')) > limit:
        problems.append(f"the power-off took {facts['shutdown_seconds']}s, "
                        f'more than {limit}s')
    for line in facts['error'][:3]:
        problems.append(f'console: {line}')
    if len(facts['error']) > 3:
        problems.append(f"and {len(facts['error']) - 3} more such lines")
    want = s.expect.get('result', 'pass')
    if want == 'fail':
        return [] if problems else ['the shutdown worked, expected it not to']
    return problems
