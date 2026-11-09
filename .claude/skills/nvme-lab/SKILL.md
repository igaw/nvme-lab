---
name: nvme-lab
description: Run nvme-cli builds and tests on the lab VMs (nvme host, nvmet NVMe/TCP target, nvme-bsd FreeBSD) with the `lab` tool from ~/nvme-lab. Use for CI parity before pushing an nvme-cli PR (build.sh in the CI images), FreeBSD builds/tests, NVMe/TCP connect/DH-CHAP/TLS tests, booting a QEMU guest from NVMe/TCP through dracut (boot scenarios, NBFT boots with a generated NBFT table), blktests or nvmf-failover runs, anything that needs root or a real/emulated NVMe device, and for checking what is on the VMs.
---

# nvme-lab

The lab tool is `~/nvme-lab/bin/lab` (repo igaw/nvme-lab). The full guide
is `~/nvme-lab/docs/testing.md`; read it when a run isn't covered here.

## Commands

```
L=~/nvme-lab/bin/lab
$L hosts                                   # nvme, nvmet, nvme-bsd
$L inventory [HOST...]                     # read only; first thing in a session
$L sync HOST [-C TREE] [-n NAME] [-w]      # git tree -> /root/scratch/NAME
$L ci IMAGE NAME [--pull] -- COMMAND       # CI image on nvme, tree NAME
$L parity NAME [-C TREE] [-r REF] [-w] [--only JOB...] [--pull]
                                           # CI parity: 4 jobs in parallel
$L ssh HOST [COMMAND...]                   # remote shell runs the words
$L scratch HOST [--rm NAME...]             # list / remove scratch trees
$L boot setup [--slots N] | run NAME [--slot N] [...] | teardown
                                           # boot QEMU from an NVMe/TCP root
$L boot test [SCENARIO...] [-j N] [--tag T] [--skip-tag T] [--nvme TREE]...
             [--dracut-module DIR] [--dashboard [DIR]] [--list]
                                           # boot scenarios with verdicts
$L boot nbft FILE [--nbft-ssns ...]        # write the NBFT of a case
```

- `sync` pushes HEAD (or `-r REF`) with git and checks it out detached:
  a real git checkout, incremental, build dir kept. `-w` also copies
  uncommitted changes. NAME defaults to the tree's directory name; use
  the branch or PR (`pr-4376`).
- CI parity for an nvme-cli PR before pushing: `lab parity N` (run in
  the nvme-cli tree, or `-C TREE`; `-r BRANCH` for a branch that isn't
  checked out). It syncs one tree per job (`N-deb`, `N-tw`, `N-muon`,
  `N-val`: `build.sh` owns `.build-ci` in its tree) and runs in
  parallel: debian and tumbleweed `build.sh -b release -c gcc`, debian
  `build.sh -m muon`, and debian `build.sh -b release -c clang -x`
  (valgrind, with `ulimit -n 4096`: valgrind refuses docker's huge fd
  limit). One PASS/FAIL line per job with the test counts; full logs in
  `~/.cache/nvme-lab/parity/N/JOB.log`. The trees stay for the next run.
  `--only JOB...` for a subset, `--dry-run` shows the commands.
- `ci` images: debian, tumbleweed, fedora, ... (`ghcr.io/linux-nvme/*`),
  for single runs (`lab ci debian N -- scripts/update-docs.sh`).
  `--pull` first if the image may be old. Default Docker network on
  nvme has no DNS: for apt/pip use `docker run --network host` via ssh.
- nvme has docker, not podman. Screenshots of a static HTML page (no
  browser on the workstation): copy the site to `/root/scratch/N` and run
  the bundled Chromium of `mcr.microsoft.com/playwright/python:v1.58.0-noble`
  (`docker run --rm -v /root/scratch/N:/w IMAGE sh -c '/ms-playwright/chromium-*/chrome-linux64/chrome
  --headless --no-sandbox --hide-scrollbars --window-size=1280,2600
  --screenshot=/w/shot.png file:///w/site/index.html'`; dark scheme: add
  `--force-dark-mode --blink-settings=preferredColorScheme=0`). The image
  has no playwright Python package and no PyPI access.
- FreeBSD: `lab sync nvme-bsd -n N`, then `meson setup .build && ninja
  -C .build && meson test -C .build` in `/root/scratch/N`.
- NVMe/TCP: `nvmet-target add-subsys NQN --no-auth --host HOSTNQN` on
  nvmet (prints the port, from 4421), `nvme connect -t tcp -a nvmet -s
  PORT -n NQN` on nvme, then `disconnect` and `del-subsys`.

## Boot tests (dracut, NVMe/TCP root, NBFT)

`lab boot` boots QEMU guests from an NVMe/TCP root through a dracut
initrd; a test case is a scenario, `boot/scenarios/*.toml` (how to boot,
which paths to break, what the root must see after switch-root). The
how-to is docs/testing.md, "Booting from NVMe/TCP", section "How to
test"; read it before a run that isn't one of these:

- Set up once, and after a VM reboot: `lab boot setup --slots 8` (8
  slots, each its own container, subsystems `root` (paths `root`,
  `root2`) and `extra` on nvmet via nvme-target-control, root fs, host
  identity). Check `lab scratch nvme` for synced trees first.
- Trees: `lab sync nvme -C ~/nvme-cli -r v2.16 -n v216` (from a checkout
  with the tags: sync records the version, which picks scenarios and
  known failures). Versions for dracut changes: master, 3.2, 2.16,
  2.13, 2.8 (docs, "nvme-cli versions").
- A dracut change: `lab boot test -j 8 --nvme master --nvme v32 --nvme
  v216 --nvme v213 --nvme v28 --dracut-module DIR/modules.d/74nvmf
  --skip-tag pre-os --dashboard [--dashboard-link 'TEXT=URL']`; ~5 min.
  `pre-os` scenarios only with an nvme-cli tree of the pre-OS model
  (nvme-cli#4328) and a dracut that passes `--owner=pre-os`.
  Tell the user the printed file:// URL of the dashboard.
- An nvme-cli change: sync the branch (`-w` for uncommitted changes) as
  one more `--nvme` tree.
- One case by hand: `lab boot run NAME` with a scenario's options
  (`--nbft`, `--nbft-ssns extra,nonboot`, `--fault tcp-refuse:root2`,
  `--slot N`, ...); `lab boot nbft FILE` + `nvme nbft show --nbft-path`
  for the table.
- A failure: `/root/scratch/lab-boot/out/NAME` on nvme (`log`: build or
  install, exit 2 = could not be set up; `console.txt`; `state.txt`: what
  the root saw; `cmdline`, `NBFT`, `slot`). Investigate, don't rerun
  until green; to check flakiness run it alone and as N copies (`-j 8`
  over copied TOMLs).
- A new case is a new scenario file, never a hand-made run: `make check`
  validates the shipped scenarios; known failures get `xfail` or
  `xfail_on` (releases) with reason and link; XPASS fails the run.
- `lab boot teardown` removes all results: ask first. The dashboard
  history (/root/scratch/lab-dashboard) and synced trees survive it.
- Shutdown of a running OS with an NVMe/TCP data mount (nvme-cli#3309):
  `lab boot test --tag shutdown [--nvme-package | --nvme TREE...]`, a
  Debian 13 guest with systemd and ifupdown (docs, "Shutdown").
  `--nvme-package` means Debian's 2.13 there. Results as above, plus
  `state.txt` with the shutdown order and the bad console lines.
- Faults: `port-removed` and `ana:STATE` on the target
  (nvme-target-control from ~/blktests), `tcp-refuse`/`tcp-drop` on the
  CI host (nvmf-failover from ~/nvmf-failover, nftables); always put
  back after the boot.

## Rules

- Own work only in `/root/scratch/NAME`; `/root/work` belongs to the
  setup. Remove your scratch trees when done (`lab scratch HOST --rm`);
  never remove other trees without asking the user.
- nvmet: by hand only `add-subsys`/`del-subsys`; leave the default target
  alone. The subsystems of `lab boot` belong to `lab boot setup` and
  `teardown` (nvme-target-control, ports from 4430).
- No keys or secrets in any repo; DH-CHAP/TLS keys are made on the VMs.
- Report only finished runs, with host and commit; quote the failing
  output. A wrong-looking result is investigated, not retried until green.
- `pkill -f` over ssh matches the ssh session itself: use `pkill -x` or
  a PID.
- If `lab` fails to log in, say so; the key is in
  `~/.config/nvme-lab/config.toml` (`ssh_key`).
- Tools that will be used again belong in nvme-lab (with a test, `make
  check`); one-off scripts don't.
