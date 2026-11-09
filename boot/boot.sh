#!/bin/sh
# In the lab-boot container: build an initrd with dracut and boot a QEMU
# guest from the NVMe/TCP root that mkroot.sh made.
#
#   boot.sh --name NAME --target TRADDR:TRSVCID --root-uuid UUID
#           [--nvme-tree DIR | --nvme-package] [--dracut-module DIR]
#           [--source cmdline|static|legacy|none] [--hostonly]
#           [--no-host-identity] [--append ARGS] [--timeout SECONDS]
#           [--nbft FILE] [--slot N]
#   boot.sh --build-only --nvme-tree DIR
#
# --nvme-tree      build the nvme-cli tree in DIR and install it
#                  (default: keep the nvme that is installed)
# --nvme-package   reinstall the distribution's nvme-cli
#                  Both first remove what the last tree installed (its
#                  meson install log is kept in $INSTALLED), so files of
#                  an earlier tree, e.g. a dracut configuration, don't
#                  reach the initrd.
# --dracut-module  use DIR as dracut's 74nvmf module (default: the
#                  package's)
# --source         where the initrd learns the target from:
#   cmdline  rd.nvmf.discover= on the kernel command line (default)
#   static   /etc/nvme/nvme-fabrics.conf (nvme-cli 3), needs --hostonly
#   legacy   /etc/nvme/discovery.conf (nvme-cli 2), needs --hostonly
#   none     nothing: give it with --append
# --hostonly       a hostonly initrd: it carries the container's
#                  /etc/nvme files. Otherwise the host identity goes on
#                  the kernel command line (rd.nvmf.hostnqn/hostid).
# --no-host-identity  not even that: the initrd has no host identity
# --slot           the slot of 'lab boot' this container belongs to
#                  (default 1), recorded in the results
# --build-only     only build the tree of --nvme-tree, install nothing,
#                  boot nothing (no --name, --target, --root-uuid needed)
# --nbft           give the guest FILE as its NBFT ACPI table. Its HFI
#                  must be the guest's NIC (GUEST_MAC, 10.0.2.15/24 via
#                  10.0.2.2); the network setup comes from it, no ip=.
#
# Results in /scratch/lab-boot/out/NAME: log, dracut.log, lsinitrd.txt,
# console.txt, slot, state.txt (the NVMe controllers and nvme-cli registry
# entries the root saw, see mkroot.sh) and NBFT. Prints RESULT=PASS and
# the state, or RESULT=FAIL; exits 0 on PASS, 1 on FAIL, 2 when the case
# could not be set up.
set -u

NAME='' TARGET='' UUID='' TREE='' PACKAGE='' MODULE='' HOSTONLY='' APPEND=''
IDENTITY=1
# What the last tree installed (meson's install log)
INSTALLED=/root/lab-boot-installed.txt
SOURCE=cmdline TIMEOUT=240 NBFT='' SLOT=1 BUILD_ONLY=''
# The guest's NIC; nvmelab/boot.py writes the same into the HFI of an NBFT.
GUEST_MAC=52:54:00:12:34:56 GUEST_PCI_DEV=3
usage() { sed -n '4,9p' "$0" >&2; exit 2; }
while [ $# -gt 0 ]; do
    case $1 in
        --name) NAME=$2; shift ;;
        --target) TARGET=$2; shift ;;
        --root-uuid) UUID=$2; shift ;;
        --nvme-tree) TREE=$2; shift ;;
        --nvme-package) PACKAGE=1 ;;
        --dracut-module) MODULE=$2; shift ;;
        --source) SOURCE=$2; shift ;;
        --hostonly) HOSTONLY=1 ;;
        --no-host-identity) IDENTITY='' ;;
        --append) APPEND=$2; shift ;;
        --timeout) TIMEOUT=$2; shift ;;
        --nbft) NBFT=$2; shift ;;
        --slot) SLOT=$2; shift ;;
        --build-only) BUILD_ONLY=1 ;;
        *) usage ;;
    esac
    shift
done
# Remove the files the last tree installed, but not the host identity.
uninstall_tree() {
    [ -f "$INSTALLED" ] || return 0
    grep -v '^#' "$INSTALLED" | while read -r f; do
        case $f in
            /etc/nvme/hostnqn | /etc/nvme/hostid) ;;
            *) rm -f "$f" ;;
        esac
    done
    rm -f "$INSTALLED"
}

# Build TREE in its build directory, DIR/.lab-boot, which all slots share
# (their containers are copies of one): one slot at a time, also for the
# install, which writes the build directory's install log.
# The tree belongs to root on the host; meson asks git for the version.
# sysconfdir: Tumbleweed's meson defaults to /usr/etc, dracut installs
# /etc/nvme. With INSTALL, install it too and keep the install log.
# nvme-cli 2 always builds its own libnvme (--force-fallback-for): a
# libnvme another tree installed must not end up in the build.
build_tree() {
    git config --global --add safe.directory '*'
    # shellcheck disable=SC2016 # expanded by the inner shell
    flock "$TREE/.lab-boot.lock" sh -c '
        BUILD=$1/.lab-boot
        if [ ! -f "$BUILD/build.ninja" ]; then
            meson setup "$BUILD" "$1" --prefix=/usr --sysconfdir=/etc \
                --wrap-mode=default --force-fallback-for=libnvme \
                --buildtype=release -Ddocs=false || exit 2
        fi
        ninja -C "$BUILD" >/dev/null || exit 2
        [ -n "$2" ] || exit 0
        meson install -C "$BUILD" --no-rebuild --quiet || exit 2
        cp "$BUILD/meson-logs/install-log.txt" "$2"' build "$TREE" "${1:-}"
}

if [ -n "$BUILD_ONLY" ]; then
    [ -n "$TREE" ] || usage
    # Nothing of another tree's install for meson to find
    uninstall_tree
    build_tree
    exit
fi
if [ -z "$NAME" ] || [ -z "$TARGET" ] || [ -z "$UUID" ]; then
    usage
fi
TRADDR=${TARGET%:*} TRSVCID=${TARGET##*:}
if [ -n "$NBFT" ] && [ ! -f "$NBFT" ]; then
    echo "no NBFT $NBFT" >&2
    exit 2
fi

OUT=/scratch/lab-boot/out/$NAME
rm -rf "$OUT"
mkdir -p "$OUT"
echo "$SLOT" > "$OUT/slot"
fail() { echo "SETUP FAILED: $*" | tee -a "$OUT/log" >&2; exit 2; }

{
    if [ -n "$TREE" ] || [ -n "$PACKAGE" ]; then
        uninstall_tree
    fi
    if [ -n "$TREE" ]; then
        build_tree "$INSTALLED" || exit 2
        ldconfig
    elif [ -n "$PACKAGE" ]; then
        zypper -q --non-interactive install -y -f nvme-cli || exit 2
    fi
    nvme version
} > "$OUT/log" 2>&1 || fail "installing nvme-cli, see $OUT/log"

rm -rf /usr/lib/dracut/modules.d/74nvmf
cp -a "${MODULE:-/root/74nvmf.package}" /usr/lib/dracut/modules.d/74nvmf \
    || fail "no dracut module ${MODULE:-/root/74nvmf.package}"

rm -rf /etc/nvme/discovery.conf /etc/nvme/config.json \
    /etc/nvme/nvme-fabrics.conf /etc/nvme/nvme-fabrics.conf.d \
    /etc/nvme/*.converted
# The container has one kernel, the one of the kernel-default package.
for KVER in /lib/modules/*; do KVER=${KVER##*/}; break; done
CMDLINE="console=ttyS0 net.ifnames=0 root=UUID=$UUID rw init=/init panic=-1"
CMDLINE="$CMDLINE rd.retry=10 rd.neednet=1"
# A boot that fails powers off instead of waiting in dracut's emergency
# shell until the timeout
CMDLINE="$CMDLINE rd.shell=0 rd.emergency=poweroff"
if [ -z "$NBFT" ]; then
    CMDLINE="$CMDLINE ip=10.0.2.15::10.0.2.2:255.255.255.0:guest:eth0:none"
fi
case $SOURCE in
    cmdline)
        CMDLINE="$CMDLINE rd.nvmf.discover=tcp,$TRADDR,,$TRSVCID" ;;
    static)
        printf '[Discovery Controller]\ncontroller = transport=tcp;traddr=%s;trsvcid=%s\n' \
            "$TRADDR" "$TRSVCID" > /etc/nvme/nvme-fabrics.conf ;;
    legacy)
        echo "--transport=tcp --traddr=$TRADDR --trsvcid=$TRSVCID" \
            > /etc/nvme/discovery.conf ;;
    none) ;;
    *) usage ;;
esac
if [ -n "$HOSTONLY" ]; then
    HO="--hostonly --hostonly-cmdline"
else
    # --no-hostonly-cmdline: distributions turn hostonly_cmdline on
    # (Tumbleweed's 01-dist.conf), and dracut then writes the container's
    # host identity into etc/cmdline.d even without hostonly.
    HO="--no-hostonly --no-hostonly-cmdline"
    if [ -n "$IDENTITY" ]; then
        CMDLINE="$CMDLINE rd.nvmf.hostnqn=$(cat /etc/nvme/hostnqn)"
        CMDLINE="$CMDLINE rd.nvmf.hostid=$(cat /etc/nvme/hostid)"
    fi
fi
CMDLINE="$CMDLINE $APPEND"
echo "$CMDLINE" > "$OUT/cmdline"

# shellcheck disable=SC2086
dracut --force $HO --add "nvmf systemd-networkd" \
    --add-drivers "nvme_tcp nvme_fabrics virtio_net virtio_pci ext4" \
    --kver "$KVER" "$OUT/initrd.img" > "$OUT/dracut.log" 2>&1 \
    || fail "dracut, see $OUT/dracut.log"
lsinitrd "$OUT/initrd.img" > "$OUT/lsinitrd.txt" 2>&1

if [ -n "$NBFT" ]; then
    cp "$NBFT" "$OUT/NBFT"
    set -- -acpitable "file=$NBFT"
else
    set --
fi
timeout "$TIMEOUT" qemu-system-x86_64 -enable-kvm -m 1024 -smp 2 \
    -nographic -no-reboot -kernel "/boot/vmlinuz-$KVER" \
    -initrd "$OUT/initrd.img" -append "$CMDLINE" \
    -netdev user,id=n0 \
    -device "virtio-net-pci,netdev=n0,mac=$GUEST_MAC,addr=$GUEST_PCI_DEV" \
    "$@" > "$OUT/console.txt" 2>&1

# The report of the root's /init (mkroot.sh), without the console's CRs
sed -n '/LAB-BOOT-STATE-BEGIN/,/LAB-BOOT-STATE-END/p' "$OUT/console.txt" \
    | tr -d '\r' | sed '/LAB-BOOT-STATE-/d' > "$OUT/state.txt"
if grep -a -q LAB-BOOT-ROOT-OK "$OUT/console.txt"; then
    # 75 MB per case fill the disk in a few matrix runs: a passed case
    # keeps lsinitrd.txt, a failed one its initrd too
    rm -f "$OUT/initrd.img"
    echo "RESULT=PASS $NAME"
    cat "$OUT/state.txt"
    exit 0
fi
echo "RESULT=FAIL $NAME, see $OUT/console.txt"
exit 1
