#!/bin/sh
# In the lab-boot container: build an initrd with dracut and boot a QEMU
# guest from the NVMe/TCP root that mkroot.sh made.
#
#   boot.sh --name NAME --target TRADDR:TRSVCID --root-uuid UUID
#           [--nvme-tree DIR | --nvme-package] [--dracut-module DIR]
#           [--source cmdline|static|legacy|none] [--hostonly]
#           [--no-host-identity] [--append ARGS] [--timeout SECONDS]
#           [--nbft FILE] [--network networkd|legacy|nm] [--ip-dev DEV]
#           [--nic user|tap --guest-ip IP/PREFIX --gateway GW [--vlan ID]]
#           [--nm-generator FILE]
#           [--slot N]
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
#                  package's), and the modules in DIR.siblings, if there
#                  (network-legacy, systemd-networkd), as those
# --source         where the initrd learns the target from:
#   cmdline  rd.nvmf.discover= on the kernel command line (default)
#   static   /etc/nvme/nvme-fabrics.conf (nvme-cli 3), needs --hostonly
#   legacy   /etc/nvme/discovery.conf (nvme-cli 2), needs --hostonly
#   none     nothing: give it with --append
# --hostonly       a hostonly initrd: it carries the container's
#                  /etc/nvme files. Otherwise the host identity goes on
#                  the kernel command line (rd.nvmf.hostnqn/hostid).
# --no-host-identity  not even that: the initrd has no host identity
# --network        the initrd's network module: networkd (systemd-networkd,
#                  the default), legacy (network-legacy: dhclient, ifup)
#                  or nm (network-manager)
# --ip-dev         the interface of the guest's ip= (without --nbft):
#                  eth0 (the default) or its MAC address in the BOOTIF
#                  form, 52-54-00-12-34-56 (network-legacy only)
# --nic            the guest's NIC: user (QEMU's user network, 10.0.2.15/24
#                  via 10.0.2.2, the default) or tap: the tap device
#                  labtapSLOT on the host, made for the boot, with GW
#                  (--gateway) on it and the guest at --guest-ip; with
#                  --vlan ID, GW is on the VLAN interface labtapSLOT.ID
#                  and the guest's network is on VLAN ID (the NBFT's HFI
#                  is boot.py's). The host forwards and NATs the tap's
#                  subnets to the target (boot.py, ensure_tap_network).
# --nm-generator   use FILE as nm-initrd-generator (a NetworkManager tree's,
#                  boot.py build_nm) instead of the package's
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
SOURCE=cmdline TIMEOUT=240 NBFT='' SLOT=1 BUILD_ONLY='' NETWORK=networkd IP_DEV=eth0
NIC=user GUEST_IP=10.0.2.15/24 GATEWAY=10.0.2.2 VLAN=0
NM_GENERATOR=''
# The dracut modules a --dracut-module's tree may bring along
SIBLINGS="35network-legacy 11systemd-networkd 35network-manager"
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
        --network) NETWORK=$2; shift ;;
        --ip-dev) IP_DEV=$2; shift ;;
        --nic) NIC=$2; shift ;;
        --guest-ip) GUEST_IP=$2; shift ;;
        --gateway) GATEWAY=$2; shift ;;
        --vlan) VLAN=$2; shift ;;
        --nm-generator) NM_GENERATOR=$2; shift ;;
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

# The distribution's nvme-cli and libnvme anew. A tree's install wrote
# over their files and uninstall_tree removed them: without this a slot
# whose last tree was only built has no nvme, which the scripts that run
# in the container need (mkroot.sh, guest-handover.sh).
reinstall_package() {
    # shellcheck disable=SC2046 # one word per package
    zypper -q --non-interactive install -y -f \
        $(rpm -qa --qf '%{NAME}\n' | grep -E '^(nvme-cli|libnvme)') \
        > /dev/null
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
    build_tree || exit
    reinstall_package
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
        reinstall_package || exit 2
    fi
    nvme version
} > "$OUT/log" 2>&1 || fail "installing nvme-cli, see $OUT/log"

rm -rf /usr/lib/dracut/modules.d/74nvmf
cp -a "${MODULE:-/root/74nvmf.package}" /usr/lib/dracut/modules.d/74nvmf \
    || fail "no dracut module ${MODULE:-/root/74nvmf.package}"
# The modules of the module's tree that come along (MODULE.siblings, see
# boot.py), else the package's: saved before the first overlay.
for m in $SIBLINGS; do
    [ -d "/root/package-modules/$m" ] || {
        mkdir -p /root/package-modules
        cp -a "/usr/lib/dracut/modules.d/$m" /root/package-modules/
    }
    rm -rf "/usr/lib/dracut/modules.d/$m"
    if [ -n "$MODULE" ] && [ -d "$MODULE.siblings/$m" ]; then
        cp -a "$MODULE.siblings/$m" /usr/lib/dracut/modules.d/
    else
        cp -a "/root/package-modules/$m" /usr/lib/dracut/modules.d/
    fi
done
# The package's nm-initrd-generator, saved before the first replacement,
# or --nm-generator's
NM_GEN=/usr/libexec/nm-initrd-generator
if [ -x "$NM_GEN" ]; then
    [ -e /root/package-modules/nm-initrd-generator ] || {
        mkdir -p /root/package-modules
        cp -a "$NM_GEN" /root/package-modules/nm-initrd-generator
    }
    cp -a "${NM_GENERATOR:-/root/package-modules/nm-initrd-generator}" \
        "$NM_GEN" || fail "no nm-initrd-generator $NM_GENERATOR"
fi

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
    # A prefix of 24, as boot.py gives
    if [ "$VLAN" != 0 ]; then
        CMDLINE="$CMDLINE ip=${GUEST_IP%/*}::$GATEWAY:255.255.255.0:guest:$IP_DEV.$VLAN:none"
        CMDLINE="$CMDLINE vlan=$IP_DEV.$VLAN:$IP_DEV"
    else
        CMDLINE="$CMDLINE ip=${GUEST_IP%/*}::$GATEWAY:255.255.255.0:guest:$IP_DEV:none"
    fi
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
case $NETWORK in
    networkd) NETMOD=systemd-networkd ;;
    legacy) NETMOD=network-legacy ;;
    nm) NETMOD=network-manager ;;
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
dracut --force $HO --add "nvmf $NETMOD" \
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
case $NIC in
    user) NETDEV=user,id=n0 ;;
    tap)
        # The tap of the slot, anew: one an interrupted run left may
        # carry an old VLAN or address
        TAP=labtap$SLOT
        ip link del "$TAP" 2>/dev/null
        ip tuntap add dev "$TAP" mode tap || fail "no tap $TAP"
        trap 'ip link del "$TAP" 2>/dev/null' EXIT
        ip link set "$TAP" up
        if [ "$VLAN" != 0 ]; then
            ip link add link "$TAP" name "$TAP.$VLAN" type vlan id "$VLAN" \
                || fail "no VLAN $VLAN on $TAP"
            ip link set "$TAP.$VLAN" up
            ip addr add "$GATEWAY/${GUEST_IP#*/}" dev "$TAP.$VLAN"
        else
            ip addr add "$GATEWAY/${GUEST_IP#*/}" dev "$TAP"
        fi
        NETDEV=tap,id=n0,ifname=$TAP,script=no,downscript=no
        ;;
    *) usage ;;
esac
timeout "$TIMEOUT" qemu-system-x86_64 -enable-kvm -m 1024 -smp 2 \
    -nographic -no-reboot -kernel "/boot/vmlinuz-$KVER" \
    -initrd "$OUT/initrd.img" -append "$CMDLINE" \
    -netdev "$NETDEV" \
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
