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

import re
import shlex
import subprocess
from pathlib import Path

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
# stop jobs, a failed unmount of the data file system (not of /run/lock,
# which Debian's lvm2 can keep busy). The defaults of the scenarios.
BAD_SIGNS = ('error -101', 'nvme_keep_alive_end_io', 'no available path',
             'I/O tag timeout', 'blocked for more than', 'A stop job',
             'Failed unmounting srv-data', 'timed out')
SHUTDOWN_LIMIT = 60
TIMEOUT = 300


def major(tree, s):
    """The nvme-cli major version scenario S gets with TREE."""
    return PACKAGE_MAJOR if tree.package else tree.major


# The NVMe/FC target of boot/labfc (labfc-target.sh)
LABFC_NQN = 'nqn.2026-11.io.lab:fc-data'


def handover_kind(s):
    """'nvme' or 'fc' for a hand-over scenario, None otherwise."""
    root = s.guest.get('root') if s.guest is not None else None
    return root if root in ('nvme', 'fc') else None


def flavor(s):
    if s.guest.get('legacy_units'):
        return 'both'
    if handover_kind(s) or s.guest.get('connect') == 'discoverd':
        return 'discoverd'
    return 'legacy'


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


def run(cfg, host, s, tree, slot, env, name, out, module=None):
    """Make the disk of shutdown case NAME (scenario S, TREE) and boot it
    in SLOT; write the output to OUT. Returns guest-boot.sh's exit
    status (2: could not be set up)."""
    from .boot import exec_argv
    g = s.guest
    if handover_kind(s):
        return run_handover(cfg, host, s, tree, slot, env, name, out, module)
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
             '--dirty-mb', str(g.get('dirty_mb', 256)),
             '--network', g.get('network', 'ifupdown')]
    if g.get('lvm2'):
        image.append('--lvm2')
    if g.get('wait_online'):
        image.append('--wait-online')
    if g.get('modules_load'):
        image.append('--modules-load')
    if g.get('legacy_units'):
        image.append('--legacy-units')
    if g.get('probe_units'):
        image += ['--probe-units', ' '.join(g['probe_units'])]
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
    problems += probe_problems(s, facts)
    want = s.expect.get('result', 'pass')
    if want == 'fail':
        return [] if problems else ['the shutdown worked, expected it not to']
    return problems


def probe_problems(s, facts):
    """The guest.probe_units whose Result= in the guest's report (lines
    'probe UNIT result=RESULT') is not the expected one ('!RESULT': any
    other)."""
    got = {}
    for line in facts['guest']:
        words = line.split()
        if len(words) == 3 and words[0] == 'probe' and \
                words[2].startswith('result='):
            got[words[1]] = words[2][len('result='):]
    problems = []
    for unit, want in s.guest.get('probe_units', {}).items():
        if unit not in got:
            problems.append(f'{unit}: no probe result from the guest')
        elif want.startswith('!') and got[unit] == want[1:]:
            problems.append(f'{unit}: Result={got[unit]}, expected another')
        elif not want.startswith('!') and got[unit] != want:
            problems.append(f'{unit}: Result={got[unit]}, expected {want}')
    return problems


def run_handover(cfg, host, s, tree, slot, env, name, out, module):
    """Make the root of hand-over case NAME (scenario S, TREE, dracut's
    nvmf MODULE, a local directory) and boot it from the root subsystem
    of SLOT; write the output to OUT. Returns guest-handover.sh's exit
    status (2: could not be set up)."""
    from .boot import ROOT_UUID, exec_argv
    g = s.guest
    if tree.package or not tree.name:
        raise ConfigError('a hand-over case needs an nvme-cli tree (--nvme)')
    if not module:
        raise ConfigError('a hand-over case needs --dracut-module')
    paths = g.get('paths', 1)
    outdir = f'{BOOT_DIR}/out/{sync.check_name(name)}'
    tree_dir = dracut_tree(module)
    src = f'{BOOT_DIR}/dracut/{sync.check_name(name)}'
    upload_tracked(cfg, host, tree_dir, src)
    built = remote.run(cfg, host, dracut_build_argv(slot, name),
                       capture_output=True, encoding='utf-8',
                       errors='replace')
    if built.returncode:
        out.write(f'building dracut of {tree_dir} failed:\n'
                  f'{built.stdout[-2000:]}{built.stderr[-2000:]}')
        return 2
    image = ['sh', f'{scripts_dir(slot)}/guest-root.sh',
             '--base', GUEST_DIR, '--out', outdir, '--nbd', str(slot.n - 1),
             '--hostnqn', slot.host_nqn, '--hostid', slot.host_id,
             '--nvme', stage_dir(tree.name, 'discoverd'),
             '--dracut', f'{src}/.lab-debian-stage',
             '--uptime', str(g.get('uptime', 5))]
    fc = handover_kind(s) == 'fc'
    if fc:
        image += ['--fc', f'{scripts_dir(slot)}/labfc']
    if g.get('lose') and fc:
        image += ['--lose-nqn', LABFC_NQN]
    elif g.get('lose'):
        last = 'root2' if paths > 1 else 'root'
        image += ['--lose-trsvcid', env.trsvcid(last)]
    if g.get('restart'):
        image.append('--restart')
    try:
        done = remote.run(cfg, host, image)
    finally:
        # The tree and its build are in the root now: 25 MB a case
        remote.run(cfg, host, ['rm', '-rf', '--', src])
    if done.returncode:
        log = remote.run(cfg, host, f'tail -20 {shlex.quote(outdir)}/log',
                         capture_output=True, encoding='utf-8',
                         errors='replace')
        out.write(log.stdout)
        return 2
    args = ['--name', name, '--timeout', str(g.get('timeout', TIMEOUT)),
            '--slot', str(slot.n)]
    if fc:
        args.append('--fc')
    else:
        args += ['--target', f"{env.traddr}:{env.trsvcid('root')}",
                 '--nqn', slot.root_nqn, '--root-uuid', ROOT_UUID]
    if paths > 1 and not fc:
        args += ['--path2', env.trsvcid('root2')]
    done = remote.run(cfg, host, exec_argv(slot, 'guest-handover.sh', *args),
                      stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                      encoding='utf-8', errors='replace')
    out.write(done.stdout)
    return done.returncode


def parse_handover(text):
    """guest-handover.sh's state.txt: {key: value} of its facts, the
    lists of its error, event and removed lines, and {phase: {kind:
    [words]}} of the guest's reports."""
    facts = {'error': [], 'event': [], 'removed': [], 'phases': {}}
    for line in text.splitlines():
        key, _, value = line.partition(' ')
        if key == 'phase':
            phase, _, rest = value.partition(' ')
            kind, _, words = rest.partition(' ')
            facts['phases'].setdefault(phase, {}).setdefault(
                kind, []).append(words.split())
        elif key in ('error', 'event', 'removed'):
            facts[key].append(value)
        elif key:
            facts[key] = value
    return facts


def _ktime(line):
    """The kernel time of a console line, or None."""
    m = re.search(r'\[\s*(\d+\.\d+)\]', line)
    return float(m.group(1)) if m else None


def _kv(words):
    return dict(w.split('=', 1) for w in words if '=' in w)


def handover_verdict(s, facts, root_nqn):
    """What is wrong with the run of hand-over scenario S (empty:
    nothing). FACTS: parse_handover(); ROOT_NQN: the slot's root."""
    g = s.guest
    problems = []
    if facts.get('booted') != 'yes':
        return ['the guest did not boot from NVMe/TCP into its report']
    paths = g.get('paths', 1)
    names = {}
    for phase in ('boot', 'lost', 'restart'):
        if phase == 'lost' and not g.get('lose') or \
                phase == 'restart' and not g.get('restart'):
            continue
        rep = facts['phases'].get(phase)
        if not rep:
            problems.append(f'{phase}: no report')
            continue
        root = [w for w in rep.get('ctrl', []) if len(w) > 2 and
                w[2] == root_nqn]
        names[phase] = sorted(w[0] for w in root)
        if len(root) != paths:
            problems.append(f'{phase}: {len(root)} controllers of the root, '
                            f'expected {paths}')
        for w in root:
            kv = _kv(w)
            if kv.get('owner') != 'pre-os':
                problems.append(f"{phase}: {w[0]} owner '{kv.get('owner')}', "
                                'expected pre-os')
            if kv.get('ctrl_loss_tmo') != 'off':
                problems.append(f"{phase}: {w[0]} ctrl_loss_tmo "
                                f"'{kv.get('ctrl_loss_tmo')}', expected off")
        for w in rep.get('unit', []):
            kv = _kv(w)
            if root_nqn in ' '.join(w) and kv.get('execstop') != 'no':
                problems.append(f'{phase}: unit {w[0]} of the root '
                                'disconnects on stop')
        if not rep.get('preos'):
            problems.append(f'{phase}: nothing in pre-os.conf')
        svc = {w[0]: w[1] for w in rep.get('service', []) if len(w) > 1}
        if svc.get('nvme-discoverd') != 'active':
            problems.append(f"{phase}: nvme-discoverd is "
                            f"{svc.get('nvme-discoverd')}")
        if phase == 'boot' and rep.get('need_shutdown') != [['yes']]:
            problems.append('boot: the initrd did not ask for the shutdown '
                            'initramfs (need_shutdown)')
    if 'restart' in names and 'lost' in names and \
            names['restart'] != names['lost']:
        problems.append(f"restart: the root's controllers changed from "
                        f"{names['lost']} to {names['restart']}")
    elif 'restart' in names and names['restart'] != names.get('boot'):
        if 'lost' not in names:
            problems.append(f"restart: the root's controllers changed "
                            f"from {names['boot']} to {names['restart']}")
    # The shutdown
    limit = g.get('shutdown_limit', SHUTDOWN_LIMIT)
    if facts.get('poweroff') != 'yes':
        problems.append('the guest did not run systemctl poweroff')
    elif facts.get('powered_off') != 'yes':
        problems.append('no power-off: QEMU was killed after '
                        f"{g.get('timeout', TIMEOUT)}s")
    elif int(facts.get('shutdown_seconds', '0')) > limit:
        problems.append(f"the power-off took {facts['shutdown_seconds']}s, "
                        f'more than {limit}s')
    umount, disc = facts.get('root_umount', '-'), \
        facts.get('pre_os_disconnect', '-')
    if disc == '-':
        problems.append('the shutdown initramfs disconnected no pre-OS '
                        'controller')
    elif umount == '-':
        problems.append('the root was not unmounted before its controllers '
                        'were disconnected')
    elif float(disc) < float(umount):
        problems.append(f'the pre-OS controllers were disconnected at '
                        f'{disc}, before the root was unmounted at {umount}')
    early = [r for r in facts['removed']
             if umount != '-' and float(r.split()[0]) < float(umount)]
    for r in early[:2]:
        problems.append(f'a controller was removed before the root was '
                        f'unmounted: {r}')
    # A keep-alive in flight when the shutdown initramfs deletes the
    # controller fails with a transport error (FC aborts it): teardown,
    # not a lost network, which times out before any disconnect
    errors = [line for line in facts['error']
              if not (disc != '-' and 'nvme_keep_alive_end_io' in line and
                      _ktime(line) is not None and
                      _ktime(line) >= float(disc))]
    for line in errors[:3]:
        problems.append(f'console: {line}')
    if len(errors) > 3:
        problems.append(f"and {len(errors) - 3} more such lines")
    return problems


def dracut_tree(module):
    """The dracut tree of the nvmf module directory MODULE
    (TREE/modules.d/74nvmf): a module is tested with its own dracut."""
    tree = Path(module).resolve().parent.parent
    if not (tree / 'dracut.sh').is_file():
        raise ConfigError(f'{module} is not in a dracut tree: a hand-over '
                          'case builds the dracut of the module')
    return tree


def upload_tracked(cfg, host, tree, dest):
    """Copy the files git tracks in TREE (with their changes in the work
    tree) to DEST on HOST."""
    from .boot import upload_argv
    files = subprocess.run(['git', '-C', str(tree), 'ls-files', '-z'],
                           check=True, capture_output=True).stdout
    tar = subprocess.Popen(['tar', '-C', str(tree), '--null', '-T', '-',
                            '-cf', '-'], stdin=subprocess.PIPE,
                           stdout=subprocess.PIPE)
    ssh = subprocess.Popen(upload_argv(cfg, host, dest), stdin=tar.stdout)
    tar.stdout.close()
    tar.communicate(files)
    if ssh.wait() or tar.returncode:
        raise ConfigError(f'copying {tree} to {host.name} failed')


def dracut_build_argv(slot, name):
    """Build the dracut tree of case NAME in the Debian CI image."""
    script = f'{CONTAINER_DIR}/slot-{slot.n}/scripts/dracut-build.sh'
    return ['docker', 'run', '--rm', '--network', 'host', '-v',
            '/root/scratch:/scratch', BUILD_IMAGE, 'sh', script,
            f'/scratch/lab-boot/dracut/{sync.check_name(name)}']
