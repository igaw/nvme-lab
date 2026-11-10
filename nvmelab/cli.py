"""lab: drive the nvme-cli test VMs from a workstation.

Hosts are the [hosts.*] of lab.toml. The ssh key comes from
LAB_SSH_KEY or ~/.config/nvme-lab/config.toml (ssh_key = "..."), else
from ssh's own config.
"""

import argparse
import datetime
import json
import os
import shlex
import subprocess
import sys
from pathlib import Path

from . import (boot, ci, config, dashboard, faults, inventory, parity,
               remote, scenario, sync)
from .config import SCRATCH, ConfigError
from .slots import Slot


def cmd_hosts(cfg, args):
    for h in cfg.hosts.values():
        flags = ' [ci]' if h.ci else ''
        print(f'{h.name:<10} {h.login:<22} {h.description}{flags}')
    return 0


def cmd_inventory(cfg, args):
    hosts = [cfg.host(n) for n in args.host] or list(cfg.hosts.values())
    results = inventory.probe_all(cfg, hosts)
    if args.json:
        print(json.dumps(results, indent=2))
    elif args.markdown:
        print(inventory.render_markdown(results, datetime.date.today()))
    else:
        print(inventory.render_text(results), end='')
    return 1 if any('error' in r for r in results.values()) else 0


def cmd_ssh(cfg, args):
    host = cfg.host(args.host)
    # Like ssh: the words of the command are joined with spaces and run
    # by the remote shell.
    command = ' '.join(args.command) if args.command else None
    argv = remote.ssh_argv(cfg, host, command)
    if command is None:
        os.execvp(argv[0], argv)
    return subprocess.run(argv).returncode


def cmd_sync(cfg, args):
    sync.sync(cfg, cfg.host(args.host), tree=args.tree, name=args.name,
              ref=args.ref, worktree=args.worktree)
    return 0


def cmd_ci(cfg, args):
    host = cfg.host(args.host) if args.host else cfg.ci_host()
    src = sync.scratch_dir(args.name)
    command = ' '.join(args.command)
    if not command:
        raise ConfigError('no command (lab ci IMAGE NAME -- COMMAND)')
    argv = ci.ci_argv(cfg, host, args.image, src, command, pull=args.pull)
    if args.dry_run:
        print(shlex.join(argv))
        return 0
    return subprocess.run(argv).returncode


def cmd_parity(cfg, args):
    host = cfg.host(args.host) if args.host else cfg.ci_host()
    jobs = parity.select(args.only)
    log_dir = args.log_dir or parity.default_log_dir(args.name)
    if args.dry_run:
        for job in jobs:
            print(f'{job.name}: '
                  f'{shlex.join(parity.job_argv(cfg, host, args.name, job))}')
        return 0
    return parity.run(cfg, host, args.name, jobs, log_dir, tree=args.tree,
                      ref=args.ref, worktree=args.worktree, pull=args.pull)


def cmd_scratch(cfg, args):
    host = cfg.host(args.host)
    if args.rm:
        dirs = [sync.scratch_dir(n) for n in args.rm]
        return remote.run(cfg, host, ['rm', '-rf', '--', *dirs]).returncode
    return remote.run(cfg, host, f'cd {SCRATCH} 2>/dev/null && '
                      'du -sh -- * 2>/dev/null || true').returncode


def positive(text):
    try:
        n = int(text)
    except ValueError:
        n = 0
    if n < 1:
        raise argparse.ArgumentTypeError(f"'{text}' is not a number >= 1")
    return n


def boot_hosts(cfg, args):
    host = cfg.host(args.host) if args.host else cfg.ci_host()
    return host, cfg.host(args.target)


def cmd_boot_setup(cfg, args):
    host, target = boot_hosts(cfg, args)
    boot.setup(cfg, host, target, recreate=args.recreate, count=args.slots)
    return 0


def nbft_options(args):
    """nbft_table()'s options of 'lab boot run' or 'lab boot nbft', or
    None when the case has no NBFT."""
    if not (args.nbft or args.nbft_ssns or args.nbft_discovery
            or args.nbft_no_root):
        return None
    return dict(root=not args.nbft_no_root, ssns=args.nbft_ssns,
                discovery=args.nbft_discovery)


def cmd_boot_run(cfg, args):
    host = cfg.host(args.host) if args.host else cfg.ci_host()
    nbft = nbft_options(args)
    # With an NBFT, the initrd learns the target from it.
    source = args.source or ('none' if nbft is not None else 'cmdline')
    options = dict(tree=args.nvme, package=args.nvme_package,
                   source=source, hostonly=args.hostonly,
                   host_identity=not args.no_host_identity,
                   append=' '.join(args.append) or None,
                   timeout=args.timeout, network=args.network,
                   nic=args.nic or ('tap' if args.vlan else 'user'),
                   vlan=args.vlan)
    fault_specs = [faults.parse(f) for f in args.fault]
    slot = Slot(args.slot)
    if args.dry_run:
        if nbft is not None:
            # check it
            boot.nbft_table(('192.0.2.1', '4420'), extra='4421', **nbft)
        argv = boot.run_argv(args.name, ('TRADDR', 'TRSVCID'),
                             module=args.dracut_module and
                             f'{boot.CONTAINER_DIR}/modules/{args.name}',
                             nbft_file=nbft is not None and
                             boot.nbft_path(args.name)[1], slot=slot,
                             **options)
        print(shlex.join(remote.ssh_argv(cfg, host, argv)))
        for f in fault_specs:
            print(f"fault: {f['kind']} on {f['path']}")
        return 0
    if args.nm:
        options['nm_generator'] = boot.build_nm(cfg, host, args.nm, slot)
    return boot.run(cfg, host, args.name, module=args.dracut_module,
                    nbft_options=nbft, fault_specs=fault_specs, slot=slot,
                    **options)


def cmd_boot_test(cfg, args):
    scenarios = scenario.find(args.scenario, args.tag, args.skip_tag)
    if not scenarios:
        raise ConfigError('no scenario selected')
    if args.list:
        width = max(len(s.id) for s in scenarios)
        for s in scenarios:
            xfail = ' (xfail)' if s.xfail else ''
            if s.xfail_on:
                xfail += f" (xfail on {', '.join(s.xfail_on)})"
            print(f"{s.id:{width}}  {s.name}{xfail} [{', '.join(s.tags)}]")
        return 0
    host = cfg.host(args.host) if args.host else cfg.ci_host()
    started = datetime.datetime.now(datetime.timezone.utc)
    cases = boot.test(cfg, host, scenarios, prefix=args.prefix,
                      module=args.dracut_module, trees=args.nvme,
                      package=args.nvme_package, jobs=args.jobs,
                      nm=args.nm)
    if args.dashboard:
        links = [dashboard.parse_link(lk) for lk in args.dashboard_link]
        site = dashboard.publish(cfg, host, cases, started, args.dashboard,
                                 module=args.dracut_module, links=links)
        print(f'dashboard: {site.as_uri()}/index.html')
    return 1 if any(c.bad for c in cases) else 0


def cmd_boot_nbft(cfg, args):
    if args.target_addr:
        target, extra = tuple(args.target_addr.rsplit(':', 1)), None
        if len(target) != 2 or not target[1].isdigit():
            raise ConfigError(f"bad --target-addr '{args.target_addr}': "
                              'TRADDR:TRSVCID')
    else:
        host = cfg.host(args.host) if args.host else cfg.ci_host()
        env = boot.read_env(cfg, host, Slot(args.slot))
        target, extra = env.target, env.paths.get('extra', (0, None))[1]
    data = boot.nbft_table(target, extra=extra, slot=Slot(args.slot),
                           **nbft_options(args))
    Path(args.file).write_bytes(data)
    print(f'{args.file}: NBFT, {len(data)} bytes')
    return 0


def add_nbft_arguments(b):
    b.add_argument('--nbft-ssns', action='append', default=[],
                   metavar='SPEC', help='one more SSNS descriptor: '
                   '[TRADDR:]TRSVCID/NQN[,FLAG...] or extra[,FLAG...] (the '
                   "extra subsystem of setup; TRADDR: the target's; FLAG: "
                   'nonboot, discovered, separate-dc, available, '
                   'unavailable)')
    b.add_argument('--nbft-discovery', action='append', default=[],
                   metavar='[TRADDR:]TRSVCID',
                   help='a discovery descriptor')
    b.add_argument('--nbft-no-root', action='store_true',
                   help='no SSNS descriptor for the root subsystem')


def cmd_boot_teardown(cfg, args):
    host, target = boot_hosts(cfg, args)
    boot.teardown(cfg, host, target)
    return 0


def parser():
    p = argparse.ArgumentParser(
        prog='lab', description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('--config', help='lab.toml to use (default: the one of '
                   'this repo, or LAB_CONFIG)')
    sub = p.add_subparsers(dest='cmd', required=True, metavar='COMMAND')

    s = sub.add_parser('hosts', help='list the hosts')
    s.set_defaults(fn=cmd_hosts)

    s = sub.add_parser('inventory', help='show what is on the hosts '
                       '(read only)')
    s.add_argument('host', nargs='*', help='default: all')
    fmt = s.add_mutually_exclusive_group()
    fmt.add_argument('--json', action='store_true')
    fmt.add_argument('--markdown', action='store_true',
                     help='for docs/inventory.md')
    s.set_defaults(fn=cmd_inventory)

    s = sub.add_parser('ssh', help='log in, or run a command')
    s.add_argument('host')
    s.add_argument('command', nargs=argparse.REMAINDER)
    s.set_defaults(fn=cmd_ssh)

    s = sub.add_parser('sync', help=f'put a git tree on a host, in '
                       f'{SCRATCH}/NAME',
                       description=sync.__doc__,
                       formatter_class=argparse.RawDescriptionHelpFormatter)
    s.add_argument('host')
    s.add_argument('-C', '--tree', default='.',
                   help='the local git tree (default: .)')
    s.add_argument('-n', '--name', help='scratch name (default: the '
                   "tree's directory name)")
    s.add_argument('-r', '--ref', default='HEAD',
                   help='commit to put there (default: HEAD)')
    s.add_argument('-w', '--worktree', action='store_true',
                   help='also the uncommitted changes')
    s.set_defaults(fn=cmd_sync)

    s = sub.add_parser('ci', help="run a command in an nvme-cli CI image "
                       "on a synced tree",
                       usage='lab ci [-h] [--host HOST] [--pull] [--dry-run] '
                       'IMAGE NAME -- COMMAND...',
                       description=ci.__doc__ + '\nCOMMAND is shell code, '
                       'run in the tree. Example:\n  lab ci debian wt-4351 '
                       '-- scripts/build.sh -b release -c gcc',
                       formatter_class=argparse.RawDescriptionHelpFormatter)
    s.add_argument('image', help='debian, tumbleweed, fedora, ... or a '
                   'full image ref')
    s.add_argument('name', help=f'the tree, {SCRATCH}/NAME')
    s.set_defaults(command=[])
    s.add_argument('--host', help='default: the host with ci = true')
    s.add_argument('--pull', action='store_true',
                   help='pull the newest image first')
    s.add_argument('--dry-run', action='store_true',
                   help='print the ssh command, run nothing')
    s.set_defaults(fn=cmd_ci)

    s = sub.add_parser('parity', help='the CI-parity runs of an nvme-cli '
                       'tree, in parallel',
                       description=parity.__doc__,
                       formatter_class=argparse.RawDescriptionHelpFormatter)
    s.add_argument('name', help='base name of the trees, NAME-JOB in '
                   f'{SCRATCH}')
    s.add_argument('-C', '--tree', default='.',
                   help='the local git tree (default: .)')
    s.add_argument('-r', '--ref', default='HEAD',
                   help='commit to test (default: HEAD)')
    s.add_argument('-w', '--worktree', action='store_true',
                   help='also the uncommitted changes')
    s.add_argument('--only', nargs='+', metavar='JOB',
                   help='run only these jobs: ' +
                   ', '.join(j.name for j in parity.JOBS))
    s.add_argument('--pull', action='store_true',
                   help='pull the newest images first')
    s.add_argument('--log-dir', help='default: '
                   '~/.cache/nvme-lab/parity/NAME')
    s.add_argument('--host', help='default: the host with ci = true')
    s.add_argument('--dry-run', action='store_true',
                   help='print the ssh commands, sync and run nothing')
    s.set_defaults(fn=cmd_parity)

    s = sub.add_parser('boot', help='boot a QEMU guest from an NVMe/TCP '
                       'root, with dracut',
                       description=boot.__doc__,
                       formatter_class=argparse.RawDescriptionHelpFormatter)
    bsub = s.add_subparsers(dest='boot_cmd', required=True,
                            metavar='SUBCOMMAND')
    b = bsub.add_parser('setup', help='create the container, the target '
                        'subsystem and the root file system')
    b.add_argument('--host', help='default: the host with ci = true')
    b.add_argument('--target', default='nvmet',
                   help='the NVMe/TCP target host (default: nvmet)')
    b.add_argument('--recreate', action='store_true',
                   help='create the container anew')
    b.add_argument('--slots', type=positive, default=1, metavar='N',
                   help='N environments that boot side by side, each with '
                   'its own container, subsystems and host identity '
                   '(default: 1)')
    b.set_defaults(fn=cmd_boot_setup)
    b = bsub.add_parser('run', help='build an initrd and boot it')
    b.add_argument('--slot', type=positive, default=1, metavar='N',
                   help='the slot to boot in (default: 1)')
    b.add_argument('name', help='the case, results in '
                   f'{boot.BOOT_DIR}/out/NAME')
    b.add_argument('--host', help='default: the host with ci = true')
    nvme = b.add_mutually_exclusive_group()
    nvme.add_argument('--nvme', metavar='TREE', help='build and install '
                      f'the nvme-cli tree {SCRATCH}/TREE (lab sync)')
    nvme.add_argument('--nvme-package', action='store_true',
                      help="reinstall the distribution's nvme-cli")
    b.add_argument('--dracut-module', metavar='DIR', help="a local "
                   "directory to use as dracut's 74nvmf module")
    b.add_argument('--nm', metavar='TREE', help='boot with '
                   'nm-initrd-generator of the NetworkManager tree '
                   f'{SCRATCH}/TREE (lab sync), built in a Tumbleweed '
                   "container, instead of the package's")
    b.add_argument('--source', choices=boot.SOURCES,
                   help='where the initrd learns the target: cmdline: '
                   'rd.nvmf.discover= (the default); static: '
                   'nvme-fabrics.conf; legacy: discovery.conf; none: from '
                   'the NBFT (the default with one) or --append')
    b.add_argument('--hostonly', action='store_true',
                   help='a hostonly initrd (needed by static and legacy)')
    b.add_argument('--nic', choices=boot.NICS,
                   help="the guest's NIC: user (QEMU's user network, the "
                   'default) or tap (a tap device on the host, needed for '
                   'a VLAN)')
    b.add_argument('--vlan', type=int, default=0,
                   help="the VLAN of the guest's network, also the NBFT "
                   "HFI's (implies --nic tap)")
    b.add_argument('--network', choices=boot.NETWORKS, default='networkd',
                   help="the initrd's network module: networkd "
                   '(systemd-networkd, the default), legacy '
                   '(network-legacy) or nm (network-manager)')
    b.add_argument('--no-host-identity', action='store_true',
                   help='no rd.nvmf.hostnqn/hostid on the command line of '
                   'an initrd that is not hostonly')
    b.add_argument('--fault', action='append', default=[],
                   metavar='KIND:PATH', help='break a path while the guest '
                   'boots: port-removed, ana:STATE, tcp-refuse or tcp-drop, '
                   'on root, root2 or extra (repeatable)')
    b.add_argument('--append', nargs='*', default=[], metavar='ARG',
                   help='more kernel command line')
    b.add_argument('--timeout', type=int, help='seconds for the boot '
                   '(default: 240)')
    b.add_argument('--nbft', action='store_true',
                   help="give the guest an NBFT: the guest's NIC, the root "
                   'subsystem (implied by the other --nbft options)')
    add_nbft_arguments(b)
    b.add_argument('--dry-run', action='store_true',
                   help='print the ssh command, run nothing')
    b.set_defaults(fn=cmd_boot_run)
    b = bsub.add_parser('test', help='run boot scenarios and check what '
                        'the root sees',
                        description=scenario.__doc__,
                        formatter_class=argparse.RawDescriptionHelpFormatter)
    b.add_argument('scenario', nargs='*', help='scenario names (files in '
                   'boot/scenarios without .toml) or paths (default: all)')
    b.add_argument('--tag', action='append', default=[],
                   help='only the scenarios with this tag (repeatable)')
    b.add_argument('--skip-tag', action='append', default=[],
                   help='not the scenarios with this tag (repeatable), '
                   'e.g. pre-os without the dracut prototype')
    b.add_argument('--prefix', default='t', help='the cases are named '
                   'PREFIX-SCENARIO (default: t)')
    b.add_argument('--host', help='default: the host with ci = true')
    nvme = b.add_mutually_exclusive_group()
    nvme.add_argument('--nvme', metavar='TREE', action='append',
                      default=[], help='build and install the nvme-cli '
                      f'tree {SCRATCH}/TREE (lab sync); repeat it for a '
                      'matrix: every scenario with every tree, the '
                      'version-specific ones only with their trees')
    nvme.add_argument('--nvme-package', action='store_true',
                      help="reinstall the distribution's nvme-cli")
    b.add_argument('--dracut-module', metavar='DIR', help="a local "
                   "directory to use as dracut's 74nvmf module")
    b.add_argument('--nm', metavar='TREE', help='boot with '
                   'nm-initrd-generator of the NetworkManager tree '
                   f'{SCRATCH}/TREE (lab sync), built in a Tumbleweed '
                   "container, instead of the package's")
    b.add_argument('-j', '--jobs', type=positive, default=1, metavar='N',
                   help='boot N scenarios at a time, one per slot of setup '
                   '(default: 1)')
    b.add_argument('--list', action='store_true',
                   help='list the selected scenarios, run nothing')
    b.add_argument('--dashboard', nargs='?', const=True, metavar='DIR',
                   help="add the results to nvme-cli's dashboard and put "
                   'the site into DIR (default: '
                   f'{dashboard.DEFAULT_DIR}), for a browser')
    b.add_argument('--dashboard-link', action='append', default=[],
                   metavar='TEXT=URL', help='a link on the dashboard, e.g. '
                   'to the dracut pull request tested (repeatable)')
    b.set_defaults(fn=cmd_boot_test)
    b = bsub.add_parser('nbft', help="write the NBFT of 'lab boot run "
                        "--nbft' to a file")
    b.add_argument('file', help="the table, e.g. for 'nvme nbft show "
                   "--nbft-path DIR' (name it NBFT)")
    b.add_argument('--host', help='default: the host with ci = true')
    b.add_argument('--slot', type=positive, default=1, metavar='N',
                   help="the slot whose identity and ports the table has")
    b.add_argument('--target-addr', metavar='TRADDR:TRSVCID',
                   help='the root subsystem (default: the one of '
                   "'lab boot setup')")
    add_nbft_arguments(b)
    b.set_defaults(fn=cmd_boot_nbft, nbft=True)
    b = bsub.add_parser('teardown', help='remove the container, the '
                        'subsystem and the results')
    b.add_argument('--host', help='default: the host with ci = true')
    b.add_argument('--target', default='nvmet')
    b.set_defaults(fn=cmd_boot_teardown)

    s = sub.add_parser('scratch', help=f'list or remove the trees in '
                       f'{SCRATCH}')
    s.add_argument('host')
    s.add_argument('--rm', nargs='+', metavar='NAME',
                   help='remove these trees')
    s.set_defaults(fn=cmd_scratch)

    return p


def main(argv=None):
    # Everything after the first '--' is the command, argparse doesn't
    # see it: 'lab ci debian x --dry-run -- cmd --help' works.
    argv = sys.argv[1:] if argv is None else list(argv)
    tail = None
    if '--' in argv:
        i = argv.index('--')
        argv, tail = argv[:i], argv[i + 1:]
    args = parser().parse_args(argv)
    if tail is not None:
        args.command = (getattr(args, 'command', None) or []) + tail
    try:
        cfg = config.load(args.config)
        return args.fn(cfg, args)
    except ConfigError as e:
        print(f'lab: {e}', file=sys.stderr)
        return 2
    except subprocess.CalledProcessError as e:
        print(f'lab: failed: {shlex.join(map(str, e.cmd))}', file=sys.stderr)
        return e.returncode or 1
    except KeyboardInterrupt:
        return 130
