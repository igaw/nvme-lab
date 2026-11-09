"""Boot scenarios: a boot case and what the booted root must see.

A scenario is a TOML file in boot/scenarios/:

    name = "NBFT boot, one of two root paths refused"
    tags = ["nbft", "multipath", "fault"]
    xfail = "why it fails today, with a link"   # optional
    xfail_on = { "v3.2" = "why this release fails" }   # optional

    [boot]                      # the options of 'lab boot run'
    source = "none"             # cmdline, static, legacy, none
    hostonly = false
    host_identity = true        # rd.nvmf.hostnqn/hostid (no hostonly)
    nbft = true
    nbft_ssns = ["{root2}/{root_nqn}", "extra,nonboot"]
    nbft_discovery = []
    nbft_no_root = false
    append = ["rd.nvmf.discover=tcp,{traddr},,{extra}"]
    timeout = 240

    [[fault]]                   # while the guest boots, see faults.py
    kind = "tcp-refuse"         # port-removed, ana, tcp-refuse, tcp-drop
    path = "root2"              # root, root2, extra
    state = "inaccessible"      # ana only

    [expect]
    result = "pass"             # pass: the root's /init ran; or fail
    controllers = ["root", "extra"]
    paths = { root = 1 }
    owners = { root = "pre-os", extra = "" }
    ctrl_loss_tmo = { root = "off" }
    hostnqn = { root = "{hostnqn}" }
    pre_os_conf = 1

Placeholders, in append, nbft_ssns, nbft_discovery and the expected host
NQNs: {traddr}, {trsvcid} (the root's first path), {root2} (its second
path), {extra} (the extra subsystem's port), {root_nqn}, {extra_nqn},
{hostnqn} and {hostid} (the guest's identity, the one the target lets
in), {other_hostnqn} and {other_hostid} (an identity the target doesn't
know). source defaults to cmdline, or to none with an NBFT.

expect.controllers is the set of subsystems that have a controller after
switch-root: root, extra (the subsystems of 'lab boot setup') or an NQN.
paths counts the controllers of a subsystem. owners, ctrl_loss_tmo and
hostnqn are what every controller of a subsystem has: its nvme-cli
registry owner ("" for unowned), its ctrl_loss_tmo as sysfs shows it
("off" for -1) and its host NQN. Each is checked only when given,
against the state the root's /init reports (state.txt, see
boot/mkroot.sh). pre_os_conf is the number of connections the initrd
recorded in /run/nvme/pre-os.conf.d for nvme-discoverd (nvme-cli#4328).

A scenario with xfail is expected to fail for the reason given: it is
reported as XFAIL while it does, and as XPASS, a failure, once it passes,
so that its expectation gets updated. xfail_on does the same for the
nvme-cli releases given by their tags, e.g. a release with a bug that is
fixed later; it applies to a tree at exactly that tag ('lab sync'
records it). A key may also name a tree by its name (e.g. suite-master,
until a fix is merged) or "package", the distribution's nvme-cli.

The tags nvme-cli-2 and nvme-cli-3 say which major version a scenario
needs: lab boot test leaves it out for a tree of the other one.

A scenario with a [guest] table is a shutdown scenario (guest.py): no
NVMe/TCP root and no dracut, but a Debian 13 guest with systemd on a
local disk that mounts a file system of the extra subsystem from fstab,
the way a user sets it up, and powers off:

    [guest]
    connect = "autoconnect"     # or discoverd (nvme-cli 3)
    config = "discovery.conf"   # or nvme-fabrics.conf; default: by the
                                # tree's major version
    fstab = "nofail,_netdev"    # the mount options
    lvm2 = false                # install lvm2 too
    wait_online = false         # nvmf-autoconnect.service wants
                                # network-online.target (a drop-in)
    modules_load = false        # nvme-fabrics in /etc/modules-load.d
    network = "ifupdown"        # or networkmanager: who configures eth0
    uptime = 5                  # seconds mounted before the power-off
    dirty_mb = 256              # data the unmount has to write back
    shutdown_limit = 60         # seconds the power-off may take
    timeout = 300               # QEMU is killed after this

It passes when the guest mounted the file system and powered off within
shutdown_limit, without a sign of a broken shutdown on the console (see
guest.BAD_SIGNS). [boot], [[fault]] and the [expect] keys other than
result don't apply to it.

With root = "nvme", it is a hand-over scenario (nvme-cli#4328): the
guest's root is on the slot's NVMe/TCP root subsystem, booted through
Debian's dracut with the nvmf module under test (--dracut-module), and
nvme-discoverd takes the pre-OS connections over:

    [guest]
    root = "nvme"
    paths = 2                   # the root's paths (root, root2)
    lose = true                 # delete the last path's controller, as
                                # the kernel gives a path up; it must
                                # come back as pre-os
    restart = true              # restart nvme-discoverd
    shutdown_limit = 60

With root = "fc", the root stays on the local disk, and the pre-OS
connection is NVMe/FC: nvme-lab's labfc module sets up a target on
nvme-fcloop in the initrd, and dracut's FC path connects it
(rd.nvmf.discover=fc,auto); lose deletes its I/O controller.

It passes when every controller of the root has the owner pre-os and
ctrl_loss_tmo off after the boot, the loss and the restart, its
connection units never disconnect it (no ExecStop=), pre-os.conf names
the connections, a restart changes nothing, and the shutdown unmounts
the root before the initramfs disconnects its controllers, in time and
without a sign of a broken shutdown.
"""

import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from . import faults
from .config import ConfigError

DIR = Path(__file__).resolve().parent.parent / 'boot' / 'scenarios'

BOOT_KEYS = {'source': str, 'hostonly': bool, 'host_identity': bool,
             'nbft': bool, 'nbft_ssns': list, 'nbft_discovery': list,
             'nbft_no_root': bool, 'append': list, 'timeout': int}
EXPECT_KEYS = {'result': str, 'controllers': list, 'paths': dict,
               'owners': dict, 'ctrl_loss_tmo': dict, 'hostnqn': dict,
               'pre_os_conf': int}
FAULT_KEYS = {'kind': str, 'path': str, 'state': str}
GUEST_KEYS = {'connect': str, 'config': str, 'fstab': str, 'lvm2': bool,
              'wait_online': bool, 'modules_load': bool, 'dirty_mb': int,
              'uptime': int, 'shutdown_limit': int, 'timeout': int,
              'root': str, 'paths': int, 'lose': bool, 'restart': bool,
              'network': str}
GUEST_NETWORK = ('ifupdown', 'networkmanager')
GUEST_ROOT = ('local', 'nvme', 'fc')
GUEST_CONNECT = ('autoconnect', 'discoverd')
GUEST_CONFIG = ('discovery.conf', 'nvme-fabrics.conf')
TOP_KEYS = {'name', 'tags', 'xfail', 'xfail_on', 'boot', 'fault', 'expect',
            'guest'}

# An identity the target doesn't let in
OTHER_HOST_ID = '0f1e2d3c-4b5a-4968-8778-695a4b3c2d1e'
OTHER_HOST_NQN = f'nqn.2014-08.org.nvmexpress:uuid:{OTHER_HOST_ID}'


@dataclass
class Scenario:
    id: str                     # the file name without .toml
    name: str
    tags: list = field(default_factory=list)
    xfail: str | None = None
    xfail_on: dict = field(default_factory=dict)
    boot: dict = field(default_factory=dict)
    faults: list = field(default_factory=list)
    expect: dict = field(default_factory=dict)
    guest: dict | None = None   # a shutdown scenario's [guest]


def check_table(path, table, keys, what):
    if not isinstance(table, dict):
        raise ConfigError(f'{path}: {what} must be a table')
    for key, value in table.items():
        if key not in keys:
            raise ConfigError(f"{path}: unknown key '{key}' in {what}")
        if not isinstance(value, keys[key]):
            raise ConfigError(f"{path}: {what}.{key} must be a "
                              f'{keys[key].__name__}')


def load(path):
    path = Path(path)
    try:
        data = tomllib.loads(path.read_text(encoding='utf-8'))
    except (OSError, tomllib.TOMLDecodeError) as e:
        raise ConfigError(f'{path}: {e}') from None
    unknown = set(data) - TOP_KEYS
    if unknown:
        raise ConfigError(f"{path}: unknown key '{sorted(unknown)[0]}'")
    s = Scenario(id=path.stem, name=data.get('name', path.stem),
                 tags=data.get('tags', []), xfail=data.get('xfail'),
                 xfail_on=data.get('xfail_on', {}),
                 boot=data.get('boot', {}), expect=data.get('expect', {}))
    if not isinstance(s.xfail_on, dict) or not all(
            isinstance(v, str) for v in s.xfail_on.values()):
        raise ConfigError(f'{path}: xfail_on must map tags to reasons')
    check_table(path, s.boot, BOOT_KEYS, 'boot')
    check_table(path, s.expect, EXPECT_KEYS, 'expect')
    if s.expect.get('result', 'pass') not in ('pass', 'fail'):
        raise ConfigError(f'{path}: expect.result must be pass or fail')
    if 'guest' in data:
        s.guest = data['guest']
        check_table(path, s.guest, GUEST_KEYS, 'guest')
        if s.guest.get('connect', 'autoconnect') not in GUEST_CONNECT:
            raise ConfigError(f"{path}: guest.connect must be one of "
                              f"{', '.join(GUEST_CONNECT)}")
        if s.guest.get('network', 'ifupdown') not in GUEST_NETWORK:
            raise ConfigError(f"{path}: guest.network must be one of "
                              f"{', '.join(GUEST_NETWORK)}")
        if s.guest.get('root', 'local') not in GUEST_ROOT:
            raise ConfigError(f"{path}: guest.root must be one of "
                              f"{', '.join(GUEST_ROOT)}")
        if s.guest.get('root') in ('nvme', 'fc') and \
                s.guest.get('connect', 'discoverd') != 'discoverd':
            raise ConfigError(f'{path}: a root on NVMe/TCP is handed over '
                              'to nvme-discoverd (guest.connect)')
        if s.guest.get('config', GUEST_CONFIG[0]) not in GUEST_CONFIG:
            raise ConfigError(f"{path}: guest.config must be one of "
                              f"{', '.join(GUEST_CONFIG)}")
        if s.boot or data.get('fault') or set(s.expect) - {'result'}:
            raise ConfigError(f'{path}: a [guest] scenario has no [boot], '
                              '[[fault]] or [expect] other than result')
    for f in data.get('fault', []):
        check_table(path, f, FAULT_KEYS, 'fault')
        try:
            s.faults.append(faults.make(f.get('kind'), f.get('path'),
                                        f.get('state')))
        except ConfigError as e:
            raise ConfigError(f'{path}: {e}') from None
    return s


def find(names=(), tags=(), skip_tags=()):
    """The scenarios NAMES (ids or paths; default: all of DIR) that have
    one of TAGS (default: any) and none of SKIP_TAGS."""
    if names:
        paths = [Path(n) if n.endswith('.toml') else DIR / f'{n}.toml'
                 for n in names]
    else:
        paths = sorted(DIR.glob('*.toml'))
    scenarios = [load(p) for p in paths]
    if tags:
        scenarios = [s for s in scenarios if set(tags) & set(s.tags)]
    if skip_tags:
        scenarios = [s for s in scenarios if not set(skip_tags) & set(s.tags)]
    return scenarios


def placeholders(env):
    """The values of the placeholders for the slot ENV (boot.Env)."""
    slot = env.slot
    values = {'traddr': env.traddr, 'root_nqn': slot.root_nqn,
              'extra_nqn': slot.extra_nqn, 'hostnqn': slot.host_nqn,
              'hostid': slot.host_id, 'other_hostnqn': OTHER_HOST_NQN,
              'other_hostid': OTHER_HOST_ID}
    for name, key in (('root', 'trsvcid'), ('root2', 'root2'),
                      ('extra', 'extra')):
        if name in env.paths:
            values[key] = env.paths[name][1]
    return values


def expand(value, values):
    """VALUE with the placeholders of VALUES filled in."""
    try:
        return value.format(**values)
    except KeyError as e:
        raise ConfigError(f"unknown placeholder {e} in '{value}' (or no such "
                          "path: run 'lab boot setup' again)") from None
    except (IndexError, ValueError) as e:
        raise ConfigError(f"bad placeholder in '{value}': {e}") from None


def run_options(s, env):
    """The options of boot.run() for scenario S on the target ENV:
    run_argv()'s, plus nbft_options and fault_specs."""
    values = placeholders(env)
    b = s.boot
    nbft = None
    if b.get('nbft') or b.get('nbft_ssns') or b.get('nbft_discovery') \
            or b.get('nbft_no_root'):
        nbft = dict(root=not b.get('nbft_no_root', False),
                    ssns=[expand(v, values) for v in b.get('nbft_ssns', [])],
                    discovery=[expand(v, values)
                               for v in b.get('nbft_discovery', [])])
    append = ' '.join(expand(v, values) for v in b.get('append', []))
    return dict(source=b.get('source',
                             'none' if nbft is not None else 'cmdline'),
                hostonly=b.get('hostonly', False),
                host_identity=b.get('host_identity', True),
                append=append or None, timeout=b.get('timeout'),
                nbft_options=nbft, fault_specs=s.faults)


def parse_state(text):
    """state.txt: {controller: {'nqn':, 'owner':, and the KEY=VALUE fields
    of its ctrl line}}; owner None: no registry entry or no owner."""
    ctrls, owners = {}, {}
    for line in text.splitlines():
        words = line.split()
        if len(words) >= 4 and words[0] == 'ctrl':
            info = {'transport': words[2], 'nqn': words[3]}
            info |= dict(w.split('=', 1) for w in words[4:] if '=' in w)
            ctrls[words[1]] = info
        elif len(words) == 3 and words[0] == 'registry' \
                and words[2].startswith('owner='):
            owners[words[1]] = words[2][len('owner='):] or None
    for c, info in ctrls.items():
        info['owner'] = owners.get(c)
    return ctrls


def parse_pre_os_conf(text):
    """state.txt: the connections the initrd recorded in pre-os.conf.d,
    [(file, controller line)]; a file holds the connections of a host."""
    conf = []
    for line in text.splitlines():
        words = line.split(maxsplit=2)
        if len(words) == 3 and words[0] == 'pre-os-conf':
            conf.append((words[1], words[2]))
    return conf


def verdict(s, passed, state, roles, values=None, pre_os_conf=None):
    """What is wrong with the run of S (empty: nothing). PASSED: the
    root's /init ran; STATE: parse_state(); ROLES: {nqn: role}; VALUES:
    the placeholders, for the expected host NQNs; PRE_OS_CONF:
    parse_pre_os_conf()."""
    errors = []
    want = s.expect.get('result', 'pass')
    if (want == 'pass') != passed:
        errors.append(f"boot {'passed' if passed else 'failed'}, "
                      f'expected {want}')
    if not passed:
        return errors
    by_subsys = {}
    for ctrl, info in sorted(state.items()):
        by_subsys.setdefault(roles.get(info['nqn'], info['nqn']),
                             []).append((ctrl, info))
    if 'controllers' in s.expect:
        want = sorted(set(s.expect['controllers']))
        if sorted(by_subsys) != want:
            errors.append(f'controllers of {sorted(by_subsys)}, expected '
                          f'{want}')
    for subsys, count in s.expect.get('paths', {}).items():
        got = len(by_subsys.get(subsys, []))
        if got != count:
            errors.append(f'{subsys}: {got} paths, expected {count}')
    for key, what in (('owners', 'owner'), ('ctrl_loss_tmo', 'ctrl_loss_tmo'),
                      ('hostnqn', 'hostnqn')):
        for subsys, want in s.expect.get(key, {}).items():
            if values is not None:
                want = expand(str(want), values)
            for ctrl, info in by_subsys.get(subsys, []):
                got = info.get(what) or ''
                if got != str(want):
                    errors.append(f"{subsys} {ctrl}: {what} '{got}', "
                                  f"expected '{want}'")
    if 'pre_os_conf' in s.expect:
        got = len(pre_os_conf or {})
        if got != s.expect['pre_os_conf']:
            errors.append(f'pre-os.conf: {got} connections, expected '
                          f"{s.expect['pre_os_conf']}")
    return errors


def xfail_reason(s, keys=None):
    """Why S is expected to fail on the nvme-cli tree KEYS names (its
    release tag and label, see boot.Tree.keys; a single tag; or None for
    none), or None."""
    if isinstance(keys, str):
        keys = (keys,)
    for key in keys or ():
        if key in s.xfail_on:
            return s.xfail_on[key]
    return s.xfail


def applies(s, major):
    """Whether S applies to an nvme-cli of the MAJOR version (None:
    unknown, everything applies)."""
    if major is None:
        return True
    return not any(t.startswith('nvme-cli-') and t != f'nvme-cli-{major}'
                   for t in s.tags)


def outcome(s, errors, keys=None):
    """PASS, FAIL, XFAIL or XPASS, and whether that counts as failed."""
    if xfail_reason(s, keys):
        return ('XFAIL', False) if errors else ('XPASS', True)
    return ('FAIL', True) if errors else ('PASS', False)
