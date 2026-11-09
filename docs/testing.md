# Testing nvme-cli with the lab

How the lab VMs are used for nvme-cli work, by people and by coding
agents alike. Everything is driven from the workstation with `bin/lab`;
nothing needs to be installed on the VMs beyond what `setup/` does.

## The VMs

| Host | What it is for |
|---|---|
| `nvme` | NVMe-oF host: nvme-cli master in `/usr`, blktests, nvmf-failover, tlshd. Docker with nvme-cli's CI images (`lab ci`). |
| `nvmet` | NVMe/TCP target: `nvmet-target`, namespaces on `vdb`/`vdc`. |
| `nvme-bsd` | FreeBSD 15: nvme-cli's FreeBSD port; has a QEMU NVMe disk (`nda0`), so real ioctls work. |

`lab hosts` lists them, `docs/inventory.md` has the details
(`lab inventory --markdown > docs/inventory.md` after changing a VM).

## Workstation setup

```
ln -s ~/nvme-lab/bin/lab ~/bin/lab           # or put bin/ on PATH
mkdir -p ~/.config/nvme-lab
echo 'ssh_key = "~/.ssh/nvme-cli-test"' > ~/.config/nvme-lab/config.toml
lab inventory                                # checks every login
```

The key's public half goes into `/root/.ssh/authorized_keys` on each
VM. `LAB_SSH_KEY` overrides the config file for one shell. Python
3.11 or newer, standard library only.

## Rules

- **Own trees go to `/root/scratch/NAME`** (`lab sync`), one per
  branch or PR, e.g. `/root/scratch/pr-4376`. `/root/work` belongs
  to the setup (`setup/build.sh` fetches there); don't develop in it.
- **Remove scratch trees when done**: `lab scratch HOST` lists them,
  `lab scratch HOST --rm NAME` removes one. Don't remove trees of
  others without asking.
- **The target**: change it only with `nvmet-target add-subsys` /
  `del-subsys` (a scratch subsystem on its own port); the default
  target (`nvmet-target up`) is shared, leave it as it is.
- **No secrets in git.** DH-CHAP and TLS keys are made on the VMs (see
  README, "Secrets").
- **Kernels**: a new kernel only with `setup/kernel.sh`; trixie's
  kernel stays as the fallback in grub.
- **Report what ran**: a result in a PR or issue comes from a finished
  run on a named VM and commit, not from an expectation.

## Typical runs

### A branch on the host

```
cd ~/nvme-cli                    # or a worktree of the branch
lab sync nvme -n pr-4376         # HEAD; -w adds uncommitted changes
lab ssh nvme 'cd /root/scratch/pr-4376 && meson setup .build && \
    meson compile -C .build && meson test -C .build'
```

`lab sync` pushes the commit with git and checks it out detached, so
the tree on the VM is a real git checkout (scripts that call git
work) and a second sync is incremental; the build directory stays.

### CI parity before a push

nvme-cli's CI runs in its own images (`ghcr.io/linux-nvme/*`); `lab ci`
runs a command in one of them on a synced tree:

```
lab ci debian pr-4376 -- scripts/build.sh -b release -c gcc
lab ci tumbleweed pr-4376 -- scripts/build.sh -b release -c gcc
lab ci debian pr-4376 -- scripts/build.sh -m muon
lab ci debian pr-4376 -- scripts/update-docs.sh    # man/html pages
```

All of nvme-cli's CI-parity runs at once, each in its own tree
(`build.sh` owns the `.build-ci` of its tree):

```
lab parity pr-4376                 # in the nvme-cli tree
lab parity pr-4376 -r my-branch    # a branch that isn't checked out
lab parity pr-4376 --only val      # just the valgrind job
```

It syncs `pr-4376-deb`, `-tw`, `-muon` and `-val` and runs, in parallel,
debian and tumbleweed `build.sh -b release -c gcc`, debian `build.sh -m
muon`, and debian `build.sh -b release -c clang -x` under valgrind (with
`ulimit -n 4096`: valgrind refuses docker's huge fd limit). It prints one
PASS/FAIL line per job with the test counts, and keeps the full logs in
`~/.cache/nvme-lab/parity/pr-4376/` and the trees for the next run.

`--pull` gets the newest image first (CI always uses the newest);
`--dry-run` prints the command. `build.sh` removes its build directory
first, so these runs don't disturb a `.build` of your own. Containers
on the default Docker network have no DNS on `nvme`: a command that
needs the network (apt, pip) runs with `lab ssh nvme 'docker run
--network host ...'`.

### FreeBSD

```
lab sync nvme-bsd -n pr-4376
lab ssh nvme-bsd 'cd /root/scratch/pr-4376 && meson setup .build && \
    ninja -C .build && meson test -C .build && ./.build/nvme list'
```

This is what nvme-cli's `build-freebsd.yml` does; `nda0` is a real
(emulated) NVMe controller for ioctl tests.

### NVMe/TCP between `nvme` and `nvmet`

```
lab ssh nvmet nvmet-target add-subsys nqn.pr-4376 --no-auth \
    --host "$(lab ssh nvme cat /etc/nvme/hostnqn)"
lab ssh nvmet nvmet-target status        # shows the port, from 4421
lab ssh nvme nvme connect -t tcp -a nvmet -s 4421 -n nqn.pr-4376
...
lab ssh nvme nvme disconnect -n nqn.pr-4376
lab ssh nvmet nvmet-target del-subsys nqn.pr-4376
```

DH-CHAP and TLS: README, "Secrets". blktests (`/root/work/blktests`)
and nvmf-failover (`nvmf-failover`) are installed on `nvme`.

### Booting from NVMe/TCP (dracut)

`lab boot` boots QEMU guests whose root file system is on an NVMe/TCP
namespace of `nvmet`, through an initrd built by dracut: the whole
pre-OS path of nvme-cli (dracut's nvmf module, `nvme connect-all`, the
host identity, NBFT boots) on a real kernel, with no hardware. A test
case is a scenario (`boot/scenarios/*.toml`): how to boot, which paths
to break, and what the booted root must see.

#### How to test

**Set up once** (and again after a reboot of the VMs: nvmet forgets its
configfs, the containers stop), then sync the nvme-cli trees to test:

```
lab boot setup --slots 8                      # containers, subsystems, roots
lab sync nvme -C ~/nvme-cli -r upstream/master -n master
for v in v3.2 v2.16 v2.13 v2.8; do lab sync nvme -C ~/nvme-cli -r $v -n ${v/./}; done
lab boot test --list                          # the scenarios and their tags
```

Sync trees with `lab sync` from a checkout that has the tags: it records
the version (`.git/lab-describe` of the synced tree), which picks the
scenarios for a tree and its known failures. The versions to test are
in "nvme-cli versions" below. In zsh, run such loops with `bash -c`
where a variable holds several options (zsh doesn't split `$var`).

**A dracut change** (a checkout of dracut, its `modules.d/74nvmf`):
every scenario with every nvme-cli version, 8 boots at a time, and the
results on the dashboard:

```
lab boot test -j 8 --nvme master --nvme v32 --nvme v216 --nvme v213 \
    --nvme v28 --dracut-module ~/dracut/modules.d/74nvmf --skip-tag pre-os \
    --dashboard --dashboard-link 'dracut #2820=https://github.com/dracut-ng/dracut/pull/2820'
```

About 5 minutes for some 90 boots once the trees are built (each tree
is built once per change, side by side). `--skip-tag pre-os` leaves out
the scenarios of the pre-OS model of nvme-cli#4328, which need an
nvme-cli with it (and dracut passing `--owner=pre-os`): run them with
such a tree, `--tag pre-os`. For a pull
request, use a worktree of its branch (`git worktree add /tmp/pr BRANCH`)
and its `modules.d/74nvmf`. Open the printed `file://` URL in a browser.

**An nvme-cli change**: sync the branch as one more tree and run it,
alone or next to the releases:

```
lab sync nvme -C ~/nvme-cli -n pr-4384 [-w]   # -w: uncommitted changes too
lab boot test -j 8 --nvme pr-4384 --dracut-module ~/dracut/modules.d/74nvmf
```

Without `--dracut-module`, the module of the container's dracut package
is used, which has none of the nvme-cli 3 fixes of dracut#2820.

**Some scenarios only**: by name or tag, e.g. `lab boot test --nvme
master nbft-hostnqn` or `--tag nbft --tag multipath`, `--skip-tag fault`.

**One case by hand**, e.g. while writing a scenario: `lab boot run NAME`
takes the same options as a scenario's `[boot]` and `[[fault]]`:

```
lab boot run c1 --nvme master --dracut-module ~/dracut/modules.d/74nvmf \
    --nbft --nbft-ssns extra,nonboot --fault tcp-refuse:root2 [--slot 2]
lab boot nbft /tmp/t/NBFT --nbft-ssns 4499/NQN,unavailable
nvme nbft show --nbft-path /tmp/t -s --hfi -d # what libnvme reads of it
```

**A case that failed**: everything is in `/root/scratch/lab-boot/out/NAME`
on `nvme` (NAME is `PREFIX-TREE-SCENARIO` in a matrix, else
`PREFIX-SCENARIO`; the prefix is `--prefix`, default `t`):

| File | What |
|---|---|
| `log` | the install of the nvme-cli tree; a build or install error ends here (exit 2: "could not be set up") |
| `dracut.log`, `lsinitrd.txt` | the initrd's build and contents |
| `cmdline` | the kernel command line |
| `NBFT` | the table the guest got, with `--nbft` |
| `console.txt` | the guest's serial console: kernel, dracut, `nvme connect-all` |
| `state.txt` | what the root saw after switch-root: a `ctrl` line per controller (transport, NQN, state, trsvcid, ctrl_loss_tmo, hostnqn), a `registry` line per registry entry |
| `slot` | the slot it booted in |

The dashboard links these files for every case that did not simply
pass. To tell a flaky case from a broken one, run it again alone, and
several copies at once: copy its TOML N times into a directory and run
`lab boot test -j 8 --nvme TREE /tmp/copies/*.toml`.

**A new scenario**: a TOML file in `boot/scenarios/` (format below and
in `nvmelab/scenario.py`). `make check` loads every shipped scenario and
checks its options, placeholders and faults; then run it on the lab with
the versions it applies to. A known failure gets `xfail` (always) or
`xfail_on` (some releases) with the reason and a link; XPASS, a known
failure that passes, fails the run so that the scenario gets updated.

**Clean up**: `lab boot teardown` removes the containers, the
subsystems and `/root/scratch/lab-boot` with all results (ask first).
The dashboard's history (`/root/scratch/lab-dashboard`) and the synced
trees stay; `lab scratch nvme --rm TREE` removes a tree.

#### The environment

`setup --slots N` makes N boot environments that run side by side; the
CI host carries 8 (a guest takes 2 CPUs and 1 GB; 16 CPUs, 15 GB; the
CPU only runs out while trees are built). Each slot has:

- a container on `nvme`, `lab-boot-N`: openSUSE Tumbleweed with dracut,
  a kernel and QEMU, the host's `/dev/kvm`. The first is built, the
  others start from a copy of it (the image `nvme-lab/lab-boot`);
- two subsystems on `nvmet` that let in only the slot's host NQN:
  `root`, exported on two ports (the paths `root` and `root2`, native
  multipath), with the root file system on its namespace, and `extra`
  (path `extra`), with no root, for the cases with more than one
  subsystem. Their ports are in `slot-N/target.env`;
- its own host identity, so the target's access lists keep the slots
  apart (slot 1 keeps the identity of the lab before slots).

The target is set up with nvme-target-control (blktests,
`contrib/nvme-target-control`; `target_control` in
`~/.config/nvme-lab/config.toml` or `LAB_TARGET_CONTROL`, default the
checkout in `~/blktests`); its ports have service IDs from 4430 on.
After switch-root, the root's `/init` writes the report of `state.txt`
(the registry is in `/run`, which moves along) and powers the guest
off.

A boot (`lab boot run`, or a scenario of `lab boot test`) uploads the
scripts of `boot/`, installs the nvme-cli tree (built in
`TREE/.lab-boot` with `--sysconfdir=/etc`, which all slots share; the
build always uses the tree's own libnvme, and an install first removes
what the last tree installed), puts dracut's nvmf module in place (the
package's, or `--dracut-module DIR`), builds the initrd and boots it. A
boot that fails powers off (`rd.shell=0 rd.emergency=poweroff`) instead
of waiting in dracut's emergency shell. `lab boot run` prints
`RESULT=PASS` and the root's report, or `RESULT=FAIL`.

`--source` is where the initrd learns the target from: `cmdline`
(`rd.nvmf.discover=`, the default), `static` (`nvme-fabrics.conf`,
nvme-cli 3), `legacy` (`discovery.conf`, nvme-cli 2) or `none`
(`--append` gives it; the default with an NBFT). `static` and `legacy`
need `--hostonly`: only a hostonly initrd carries the host's
`/etc/nvme`. Without it, the host identity goes on the kernel command
line (`rd.nvmf.hostnqn=`), unless `--no-host-identity`, and nowhere
else: dracut runs with `--no-hostonly-cmdline`, because Tumbleweed turns
`hostonly_cmdline` on and dracut would write the container's identity
into the initrd's `etc/cmdline.d` anyway. dracut's nvmf module up to at
least dracut 112 can't write the identity (no `/etc/nvme` in a
non-hostonly initrd) and doesn't read `nvme-fabrics.conf`; dracut#2820
fixes both, hence the module overlay in the examples.

#### NBFT boot

`--nbft` gives the guest an NBFT, the ACPI table in which boot firmware
hands its NVMe-oF connections to the OS. No firmware writes it: lab
builds the table (`nvmelab/nbft.py`) and QEMU passes it on with
`-acpitable`. So this tests the OS side of an NBFT boot (dracut's NBFT
parsing, `nvme connect-all --nbft`), not firmware booting from NVMe/TCP.

The table has the guest's NIC as its HFI (MAC 52:54:00:12:34:56, PCI
slot 3, 10.0.2.15/24 via 10.0.2.2), the slot's host NQN and ID, and the
root subsystem as its first SSNS. The SSNS of the subsystems of setup
carry their namespace's UUID as NID, and every SSNS lists its HFI as
firmware does (libnvme 1, i.e. nvme-cli 2, crashes on an SSNS whose HFI
list names no HFI). The kernel command line then has no `ip=`: the
network and the target come from the NBFT. More descriptors:

- `--nbft-ssns [TRADDR:]TRSVCID/NQN[,FLAG...]`: another SSNS, on the
  root's target address by default; `extra[,FLAG...]` is the extra
  subsystem. FLAGs (SSNS flags of the Boot Specification): `nonboot`,
  `discovered`, `separate-dc`, `available`, `unavailable`. Any other
  subsystem must exist for the slot's host NQN unless the case is about
  one that doesn't.
- `--nbft-discovery [TRADDR:]TRSVCID`: a discovery descriptor.
- `--nbft-no-root`: no SSNS for the root, e.g. to find it through a
  discovery descriptor.

`lab boot nbft FILE` writes the table of a case to a local file (name it
`NBFT` for `nvme nbft show --nbft-path`); `--target-addr` stands in for
setup's target, `--slot` picks the slot's identity and ports.

#### Faults

`--fault KIND:PATH` (repeatable; `[[fault]]` in a scenario) breaks a
path from before the guest starts until it is off again, and puts it
back afterwards, also when the run fails:

| Fault | What | Done by |
|---|---|---|
| `port-removed:PATH` | the target port is gone, connects are refused | nvme-target-control `remove-port`, `add-ports` afterwards |
| `ana:STATE:PATH` | the port's ANA group is in STATE (`inaccessible`, `non-optimized`, ...) | nvme-target-control `set-ana`, `optimized` afterwards |
| `tcp-refuse:PATH` | the CI host refuses the guest's connections to the port (TCP reset) | nvmf-failover `tcp-refuse` |
| `tcp-drop:PATH` | the CI host drops what the port sends; connects wait for their SYN retries | nvmf-failover `tcp-port` |

PATH is `root`, `root2` or `extra`. The guest's connections leave QEMU's
user networking on the CI host, so the network faults run there, with
nftables, from the nvmf-failover checkout of `nvmf_failover` in the
config or `LAB_NVMF_FAILOVER` (default `~/nvmf-failover`; `tcp-refuse`
needs a checkout with it), synced to the CI host for the run. setup and
teardown remove what an interrupted run left (the `inet nvmf_failover`
table).

#### Scenarios

```toml
name = "NBFT boot, one of two root paths refused"
tags = ["nbft", "multipath", "fault"]
xfail_on = { "v3.2" = "why this release fails, with a link" }

[boot]                      # the options of 'lab boot run'
nbft_ssns = ["{root2}/{root_nqn}"]
append = ["rd.nvmf.discover=tcp,{traddr},,{extra}"]

[[fault]]
kind = "tcp-refuse"
path = "root2"

[expect]
result = "pass"             # the root's /init ran; or "fail"
controllers = ["root"]      # the subsystems with controllers
paths = { root = 1 }        # how many controllers
owners = { root = "pre-os" }          # registry owner, "" = none
ctrl_loss_tmo = { root = "off" }      # as sysfs shows it
hostnqn = { root = "{hostnqn}" }
pre_os_conf = 1             # connections the initrd recorded in
                            # /run/nvme/pre-os.conf.d
```

Placeholders: `{traddr}`, `{trsvcid}` (the root's first path), `{root2}`,
`{extra}`, `{root_nqn}`, `{extra_nqn}`, `{hostnqn}`/`{hostid}` (the
slot's identity), `{other_hostnqn}`/`{other_hostid}` (one the target
doesn't know). The full format: `lab boot test --help`.

Tags say what a scenario is about or needs: `cmdline`, `static`,
`legacy`, `nbft`, `multipath`, `fault`, `hostnqn`, `pre-os` (the pre-OS
model of nvme-cli#4328: every connection of the initrd has the owner
`pre-os` and `ctrl_loss_tmo` off, and is recorded in `pre-os.conf`;
needs an nvme-cli with it), `handover`, `shutdown`, `guest`, and
`nvme-cli-2` /
`nvme-cli-3`: a scenario with one of these runs only with trees of that
major version. Each scenario gets a PASS, FAIL, XFAIL or XPASS line with
what differed, then a summary per tree; the exit status is 0 unless one
failed or passed unexpectedly. The output of each case is printed as a
block when it is done; the slowest scenarios start first.

They cover the corner cases of nvme-cli#4328 that the initrd decides: a
multipath root (one of its paths refused, removed or inaccessible at
boot), the only root path gone, the NBFT's host identity against the
system's (#4384), an initrd without a host identity, the NBFT's
non-bootable, unavailable and missing entries, a root found through a
discovery descriptor, `rd.nvmf.nonbft`, nvme-cli 2 without a registry,
and the registry owners and `ctrl_loss_tmo` of the pre-OS connections.
What nvme-discoverd does after switch-root (adoption, reconnects)
needs the OS, which the busybox root doesn't run: nvme-cli's mock
scenarios and the hand-over scenarios cover that; the OS shutdown with
a data mount has its own
scenarios (below).

#### Shutdown (Debian guest, nvme-cli#3309)

The busybox root ends where the OS starts. The shutdown scenarios
(`boot/scenarios/shutdown-*.toml`, tag `shutdown`, a `[guest]` table)
boot a whole system instead: Debian 13 with systemd on a local disk,
ifupdown's `networking.service` with a static `eth0`, as on a Debian
server. nvme-cli connects the slot's `extra` subsystem at boot the way a
user sets it up (`discovery.conf` or `nvme-fabrics.conf`, its
`nvmf-autoconnect.service` as installed, or `nvme-discoverd`), fstab
mounts the file system on it on `/srv/data` (`nofail`, with or without
`_netdev`), the guest writes data that stays in the page cache
(`dirty_mb`, 256 MB) and powers off with `systemctl poweroff`. The case
passes when the guest mounted the file system and powered off within
`shutdown_limit` (60s), and the console after the power-off shows none
of: `error -101`, `nvme_keep_alive_end_io`, `no available path`, I/O
timeouts, blocked tasks, stop jobs that wait, failed unmounts. QEMU is
killed after `timeout` (300s).

```
lab boot test -j 5 --tag shutdown --nvme-package          # Debian's 2.13
lab boot test -j 8 --tag shutdown --nvme master --nvme v32 --nvme v216 \
    --nvme v213 --nvme v28
```

`--nvme-package` is Debian's nvme-cli here (2.13-2, the report's), not
Tumbleweed's. A tree is built for the guest in nvme-cli's Debian CI
image (`TREE/.lab-debian-legacy`, `-discoverd` for nvme-cli 3 with
`nvme-discoverd`) and installed into the guest's disk. The guest is
built once, in `/root/scratch/lab-boot/debian` (debootstrap, ~450 MB;
remove the directory to build it again); a case's disk is a qcow2
overlay of it, set up through `qemu-nbd` on the CI host
(`/dev/nbd<slot-1>`) and removed after the run.

`state.txt` of a case: `mounted`, `poweroff`, `powered_off`,
`shutdown_seconds`, `order umount=T disconnect=T ifdown=T` (the kernel
time of the first unmount of the data file system, controller removal
and `eth0` down after the power-off; the guest's ifupdown hooks log
`LAB-EVENT ifdown-start/-done`), the `error` lines, the shutdown steps
in console order (`event`) and what the guest saw at the mount (`guest`:
controllers, registry, mount, units). The console has the kernel's info
messages (`loglevel=7`) and systemd's status lines.

Scenario keys: `connect` (`autoconnect`, `discoverd`), `config`
(default by version), `fstab`, `wait_online` (a drop-in that makes
`nvmf-autoconnect.service` want `network-online.target`, as the report's
`nvmeOF-boot-pause.service` did), `modules_load` (`nvme_fabrics` in
`/etc/modules-load.d`), `lvm2` (install Debian's lvm2), `uptime`,
`dirty_mb`, `shutdown_limit`, `timeout`.

What they found (2026-11-09, nvme-cli 2.8 to master and Debian's 2.13,
as `xfail` in the scenarios): with `_netdev` the shutdown is clean
(`shutdown-netdev`, and `shutdown-discoverd-modules`: unmount,
disconnect, ifdown). Without it, `nvmf-autoconnect.service` races the
network at boot (it is ordered after `network-online.target` but
doesn't want it) and mostly mounts nothing (`shutdown-nofail`); once it
connects (`shutdown-nofail-online`), the network goes down under the
unmount, as in #3309: keep-alive timeouts, blocked jbd2, a power-off
only after `ctrl_loss_tmo`. With lvm2, `lvm2-monitor.service` stops
after the network with the controller still connected and blocks
(`shutdown-lvm2`). nvme-discoverd's connection units fail on a host
where nothing loads `nvme-fabrics` (`shutdown-discoverd`), and without
`_netdev` they disconnect before the unmount: `no available path -
failing I/O` (`shutdown-discoverd-nofail`, #4332 case B).

nvme-cli branch `shutdown-units` fixes three of them:
`nvme-discoverd.service` loads `nvme-fabrics`,
`nvmf-autoconnect.service` wants `network-online.target`, and it
disconnects its controllers when it stops, after the `_netdev`
unmounts and before the network. `shutdown-lvm2` and
`shutdown-discoverd` pass with it; they stay expected failures on the
releases, Debian's package and `suite-master` (`xfail_on`, which names a
tree by its release tag, its name or `package`). A mount without
`_netdev` is not supported: with the fixes it connects, and the
disconnect runs before the unmount.

#### Hand-over (a Debian root on NVMe-oF, nvme-cli#4328)

The hand-over scenarios (`boot/scenarios/handover-*.toml`, tag
`handover`, `root` in `[guest]`) put the Debian guest's root on NVMe-oF
and boot it through dracut, with nvme-discoverd in the real root: the
pre-OS model of nvme-cli#4328 end to end, on a real kernel and systemd.

```
lab boot test -j 3 --tag handover --nvme TREE \
    --dracut-module ~/dracut/modules.d/74nvmf
```

- `root = "nvme"`: the root is on the slot's root subsystem (one or two
  paths, `paths`), booted from the command line (`rd.nvmf.discover=`).
  The root subsystem gets the guest's root for the case and the busybox
  root back afterwards.

A case builds the dracut tree of `--dracut-module` (the module is
tested with its own dracut: dracut-ng's nvmf module needs its
`initqueue` module, which Debian's dracut 106 lacks) in nvme-cli's
Debian CI image (`boot/dracut-build.sh`, the tracked files of the tree
with its changes), and the tree's nvme-cli with nvme-discoverd.
`boot/guest-root.sh` installs both into the guest over Debian's dracut
packages, builds the initrd in the guest's root (`/boot/initrd.img-KVER`,
where `dracut-shutdown.service` finds it), and leaves `eth0` to the
initrd (`manual`), as a root on the network wants.

The guest reports after the boot, after `lose` (the controller of the
last path is deleted, as when the kernel gives a path up) and after
`restart` (nvme-discoverd), then powers off. `state.txt`: `booted`,
`phase PHASE ...` (the report: `ctrl` with owner and `ctrl_loss_tmo`,
`preos` lines of `pre-os.conf`, nvme-discoverd's `unit`s with whether
they have an `ExecStop=`, `service`s, `need_shutdown`), `poweroff`,
`powered_off`, `shutdown_seconds`, `root_umount`, `pre_os_disconnect`
(the first controller removal after the root's unmount, when dracut's
shutdown hook ran), `removed`, `error` and `event` lines.

A case passes when the root's controllers have the owner `pre-os` and
`ctrl_loss_tmo` off in every phase, their units have no `ExecStop=`,
`pre-os.conf` names the connections, the lost path comes back, a
restart changes nothing, and the shutdown initramfs disconnects them
after the root is unmounted, cleanly and in time. Both pass with
nvme-cli branch `pre-os-owner` and dracut branch `nvmf-pre-os-owner`.

#### Dashboard

`lab boot test --dashboard [DIR]` adds the run to nvme-cli's dashboard
(`scripts/dashboard/nvme-dashboard` of the checkout in `nvme_cli` of the
config or `LAB_NVME_CLI`, default `~/nvme-cli`) and puts the site into
DIR (default `~/.cache/nvme-lab/dashboard`): open `DIR/index.html` in a
browser. The run is a record of the workflow "Boot tests": a section per
nvme-cli version, a test per scenario (XFAIL as "failed as expected",
XPASS as failed), and for a case that did not simply pass its console,
log, dracut log and state; the dracut module is named by its checkout.
The site is built on the CI host, which has Jinja2; its history and logs
stay there, in `/root/scratch/lab-dashboard`, so every run adds to the
last ones. `--dashboard-link TEXT=URL` puts a link on the page.

#### nvme-cli versions

dracut gets nvme-cli from the distribution, so a change to dracut's
nvmf module is tested against the releases distributions ship that also
get a current dracut (decided 2026-11-06, from repology):

| nvme-cli | Why |
|---|---|
| master, 3.2 | current; Tumbleweed, Arch |
| 2.16 | last 2.x: RHEL 9/10, Fedora 43/44, Ubuntu 26.04, SLFO |
| 2.13 | Debian 13 |
| 2.8 | oldest 2.x in a supported LTS: Ubuntu 24.04, SLE 15 SP6 |

Known results by version (as `xfail_on` in the scenarios): 3.0 to 3.2
connect NBFT boot paths as the system's host (`nbft-hostnqn`, fixed by
nvme-cli#4384); 2.8 and 2.9 ignore NBFT discovery descriptors
(`nbft-discovery`, since 2.10). `nbft-nonboot` on 2.8 has failed once
under load (EADDRNOTAVAIL: 2.8 has no retry without `host_traddr`).

Not tested: 1.x and 2.x before 2.5 (RHEL 8, Ubuntu 22.04, Debian 12),
which also keep their old dracut. The module's version boundaries are
2.5 (NBFT support, `connect-all --nbft`, the long option names), 2.7 (the
NetApp rule's name) and 3.0 (`nvme-fabrics.conf`, the registry).

What it can't do: firmware that boots from NVMe/TCP, security profiles
(DH-CHAP/TLS) and extended info descriptors in the NBFT, and the OS
after switch-root (the root is busybox). The initrd uses systemd and
`systemd-networkd` with a static `ip=`: Tumbleweed has no `dhclient`
for dracut's `network-legacy`.

### A quick look

```
lab inventory nvme               # what's there, read only
lab ssh nvme-bsd                 # interactive login
lab ssh nvme dmesg -T \| tail    # the remote shell runs the words
```
