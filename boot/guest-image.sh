#!/bin/sh
# On the CI host: the disk of one shutdown case, a qcow2 overlay of the
# Debian guest of guest-base.sh, set up through qemu-nbd.
#
#   guest-image.sh --base DIR --out DIR --nbd N --hostnqn NQN --hostid ID
#                  --nvme package|STAGE --connect autoconnect|discoverd
#                  --config discovery.conf|nvme-fabrics.conf
#                  --target TRADDR:TRSVCID --data-uuid UUID
#                  --fstab OPTIONS [--lvm2] [--wait-online]
#                  [--uptime SECONDS] [--dirty-mb MB]
#
# --base     guest-base.sh's directory
# --out      the case's results directory: gets disk.qcow2 and log
# --nbd      the /dev/nbdN to use (one per slot)
# --nvme     Debian's nvme-cli package, or the DESTDIR install of a tree
#            (guest-build.sh)
# --connect  how nvme-cli connects at boot: its autoconnect units
#            (nvmf-autoconnect.service, enabled here), or nvme-discoverd
# --config   the file that names the discovery controller at TARGET
# --fstab    the mount options of the data file system (UUID) on
#            /srv/data, e.g. "nofail" or "nofail,_netdev"
# --lvm2     install Debian's lvm2 too (its units scan the namespace)
# --wait-online  a drop-in that makes nvmf-autoconnect.service want
#            network-online.target: it is only ordered after it, so
#            without a _netdev mount (which wants it) it races the network
# --uptime   seconds the guest waits with the mount before it powers off
# --dirty-mb data the guest writes to /srv/data and keeps in the page
#            cache (default 256): the unmount writes it back, which takes
#            long enough that a network that goes down early catches it
#
# The guest's lab-shutdown.service waits for /srv/data, reports, writes
# to it, waits UPTIME seconds and runs "systemctl poweroff". It and the
# ifupdown hooks mark their steps in the kernel log ("LAB-EVENT ..."), so
# the console has them in order with the kernel's messages.
set -eu

BASE='' OUT='' NBD='' HOSTNQN='' HOSTID='' NVME='' CONNECT='' CONFIG=''
TARGET='' DATA_UUID='' FSTAB='' LVM2='' WAIT_ONLINE='' UPTIME=5 DIRTY_MB=256
usage() { sed -n '5,10p' "$0" >&2; exit 2; }
while [ $# -gt 0 ]; do
    case $1 in
        --base) BASE=$2; shift ;;
        --out) OUT=$2; shift ;;
        --nbd) NBD=$2; shift ;;
        --hostnqn) HOSTNQN=$2; shift ;;
        --hostid) HOSTID=$2; shift ;;
        --nvme) NVME=$2; shift ;;
        --connect) CONNECT=$2; shift ;;
        --config) CONFIG=$2; shift ;;
        --target) TARGET=$2; shift ;;
        --data-uuid) DATA_UUID=$2; shift ;;
        --fstab) FSTAB=$2; shift ;;
        --lvm2) LVM2=1 ;;
        --wait-online) WAIT_ONLINE=1 ;;
        --uptime) UPTIME=$2; shift ;;
        --dirty-mb) DIRTY_MB=$2; shift ;;
        *) usage ;;
    esac
    shift
done
for v in "$BASE" "$OUT" "$NBD" "$HOSTNQN" "$HOSTID" "$NVME" "$CONNECT" \
         "$CONFIG" "$TARGET" "$DATA_UUID" "$FSTAB"; do
    [ -n "$v" ] || usage
done
TRADDR=${TARGET%:*} TRSVCID=${TARGET##*:}
[ -f "$BASE/base.qcow2" ] || { echo "no guest image in $BASE" >&2; exit 2; }

rm -rf "$OUT"
mkdir -p "$OUT"
exec > "$OUT/log" 2>&1
DEV=/dev/nbd$NBD MNT=$OUT/mnt DISK=$OUT/disk.qcow2

[ -e /sys/module/nbd ] || modprobe nbd nbds_max=16 max_part=0
[ -b "$DEV" ] || { echo "no $DEV"; exit 2; }
# A device a killed run left connected
qemu-nbd -d "$DEV" >/dev/null 2>&1 || true

# qemu-nbd -d returns before the server wrote everything to the qcow2
# and exited: wait for it, or the guest may boot without the last writes.
disconnect() {
    sync
    blockdev --flushbufs "$DEV" 2>/dev/null || true
    qemu-nbd -d "$DEV" >/dev/null 2>&1 || true
    [ -f "$OUT/qemu-nbd.pid" ] || return 0
    pid=$(cat "$OUT/qemu-nbd.pid")
    i=0
    while kill -0 "$pid" 2>/dev/null; do
        i=$((i + 1))
        [ $i -lt 300 ] || { echo "qemu-nbd $pid does not exit"; return 1; }
        sleep 0.1
    done
    rm -f "$OUT/qemu-nbd.pid"
}
# Processes a package's scripts left running in the guest's tree would
# keep it busy: an unmount that fails leaves writes in the cache that
# never reach the qcow2.
cleanup() {
    for p in /proc/[0-9]*; do
        [ "$(readlink "$p/root")" = "$MNT" ] && kill -9 "${p#/proc/}"
    done 2>/dev/null
    for m in dev/pts dev proc sys; do
        umount "$MNT/$m" 2>/dev/null || true
    done
    if mountpoint -q "$MNT" && ! umount "$MNT"; then
        echo "cannot unmount $MNT"
        return 1
    fi
    disconnect
    rmdir "$MNT" 2>/dev/null || true
}
trap cleanup EXIT
fail() { echo "SETUP FAILED: $*"; exit 2; }

# Relative to the overlay, so that the container finds it under /scratch
BACKING=$(realpath --relative-to="$OUT" "$BASE/base.qcow2")
(cd "$OUT" && qemu-img create -q -f qcow2 -F qcow2 -b "$BACKING" disk.qcow2)
qemu-nbd -c "$DEV" --pid-file="$OUT/qemu-nbd.pid" "$DISK"
i=0
while [ "$(cat "/sys/block/nbd$NBD/size" 2>/dev/null || echo 0)" = 0 ]; do
    i=$((i + 1))
    [ $i -lt 50 ] || fail "$DEV has no size"
    sleep 0.1
done
mkdir -p "$MNT"
mount "$DEV" "$MNT"
for m in proc sys dev dev/pts; do
    mount --bind "/$m" "$MNT/$m"
done
in_guest() { chroot "$MNT" "$@"; }

mkdir -p "$MNT/etc/nvme"
echo "$HOSTNQN" > "$MNT/etc/nvme/hostnqn"
echo "$HOSTID" > "$MNT/etc/nvme/hostid"

echo "== nvme-cli: $NVME"
if [ "$NVME" = package ]; then
    in_guest sh -c 'dpkg -i /var/cache/lab-debs/nvme-cli/*.deb' \
        || fail "installing Debian's nvme-cli"
else
    [ -d "$NVME" ] || fail "no install of a tree in $NVME"
    cp -a "$NVME/." "$MNT/"
    in_guest ldconfig
fi
in_guest sh -c 'ldd /usr/sbin/nvme | grep "not found"' \
    && fail "nvme misses libraries in the guest"
in_guest nvme version || fail "nvme does not run in the guest"

if [ -n "$LVM2" ]; then
    echo "== lvm2"
    in_guest sh -c 'dpkg -i /var/cache/lab-debs/lvm2/*.deb' \
        || fail "installing lvm2"
fi

echo "== $CONFIG: $TRADDR:$TRSVCID"
rm -rf "$MNT/etc/nvme/discovery.conf" "$MNT/etc/nvme/nvme-fabrics.conf" \
    "$MNT/etc/nvme/nvme-fabrics.conf.d" "$MNT/etc/nvme/config.json"
case $CONFIG in
    discovery.conf)
        echo "--transport=tcp --traddr=$TRADDR --trsvcid=$TRSVCID" \
            > "$MNT/etc/nvme/discovery.conf" ;;
    nvme-fabrics.conf)
        printf '[Discovery Controller]\ncontroller = transport=tcp;traddr=%s;trsvcid=%s\n' \
            "$TRADDR" "$TRSVCID" > "$MNT/etc/nvme/nvme-fabrics.conf" ;;
    *) fail "bad --config $CONFIG" ;;
esac
cat "$MNT/etc/nvme/$CONFIG"

echo "== connect: $CONNECT"
units() {
    for u in nvmf-autoconnect.service nvme-discoverd.service; do
        echo "$u: $(systemctl --root="$MNT" is-enabled "$u" 2>&1)"
    done
}
echo "installed:"
units
case $CONNECT in
    autoconnect)
        systemctl --root="$MNT" enable nvmf-autoconnect.service \
            || fail "no nvmf-autoconnect.service" ;;
    discoverd)
        systemctl --root="$MNT" enable nvme-discoverd.service \
            || fail "no nvme-discoverd.service"
        systemctl --root="$MNT" disable nvmf-autoconnect.service 2>/dev/null \
            || true ;;
    *) fail "bad --connect $CONNECT" ;;
esac
echo "now:"
units
if [ -n "$WAIT_ONLINE" ]; then
    d=$MNT/etc/systemd/system/nvmf-autoconnect.service.d
    mkdir -p "$d"
    printf '[Unit]\nWants=network-online.target\n' > "$d/lab-wait-online.conf"
    echo "nvmf-autoconnect.service wants network-online.target"
fi

echo "== fstab: $FSTAB"
mkdir -p "$MNT/srv/data"
echo "UUID=$DATA_UUID /srv/data ext4 $FSTAB 0 2" >> "$MNT/etc/fstab"
cat "$MNT/etc/fstab"

# The guest's side of the test
cat > "$MNT/usr/local/sbin/lab-shutdown" <<EOF
#!/bin/sh
# Wait for the data mount, report, then power off (nvme-lab).
mark() { echo "LAB-EVENT \$*" > /dev/kmsg; }
i=0
until mountpoint -q /srv/data; do
    i=\$((i + 1))
    if [ \$i -gt 120 ]; then
        mark "nomount"
        systemctl poweroff --no-block
        exit 0
    fi
    sleep 1
done
mark "mounted"
{
    echo LAB-GUEST-STATE-BEGIN
    for c in /sys/class/nvme/nvme*; do
        [ -e "\$c" ] || continue
        echo "ctrl \${c##*/} \$(cat \$c/transport) \$(cat \$c/subsysnqn)" \
            "state=\$(cat \$c/state) ctrl_loss_tmo=\$(cat \$c/ctrl_loss_tmo 2>/dev/null)"
    done
    for e in /run/nvme/registry/*; do
        [ -d "\$e" ] || continue
        echo "registry \${e##*/} owner=\$(cat "\$e/owner" 2>/dev/null)"
    done
    findmnt -n -o SOURCE,TARGET,OPTIONS /srv/data | sed 's/^/mount /'
    for u in nvmf-autoconnect.service nvme-discoverd.service \
             networking.service lvm2-monitor.service; do
        echo "unit \$u \$(systemctl is-active \$u)"
    done
    echo LAB-GUEST-STATE-END
} > /dev/ttyS0
# Data the unmount has to write back: no writeback before it
sysctl -q -w vm.dirty_ratio=90 vm.dirty_background_ratio=80 \
    vm.dirty_expire_centisecs=360000 vm.dirty_writeback_centisecs=0
dd if=/dev/zero of=/srv/data/lab-data bs=1M count=$DIRTY_MB 2>/dev/null
mark "dirty \$(grep -E '^Dirty:' /proc/meminfo | tr -s ' ')"
sleep $UPTIME
mark "poweroff"
systemctl poweroff --no-block
EOF
chmod +x "$MNT/usr/local/sbin/lab-shutdown"
cat > "$MNT/etc/systemd/system/lab-shutdown.service" <<'EOF'
[Unit]
Description=nvme-lab: wait for /srv/data, then power off
After=network-online.target remote-fs.target local-fs.target

[Service]
Type=simple
ExecStart=/usr/local/sbin/lab-shutdown

[Install]
WantedBy=multi-user.target
EOF
systemctl --root="$MNT" enable lab-shutdown.service
# if-down.d runs before ifupdown takes the interface down, if-post-down.d
# after
for d in if-down.d:ifdown-start if-post-down.d:ifdown-done; do
    f=$MNT/etc/network/${d%%:*}/lab-mark
    # shellcheck disable=SC2016 # $IFACE is the hook's
    printf '#!/bin/sh\necho "LAB-EVENT %s $IFACE" > /dev/kmsg\n' \
        "${d##*:}" > "$f"
    chmod +x "$f"
done
trap - EXIT
cleanup || fail "cleaning up the guest's disk"
[ ! -f "$OUT/qemu-nbd.pid" ] || fail "qemu-nbd still runs"
echo "== image ready"
