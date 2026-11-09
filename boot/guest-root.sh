#!/bin/sh
# On the CI host: the root of a hand-over case, the Debian guest of
# guest-base.sh on NVMe/TCP: nvme-cli with nvme-discoverd, Debian's dracut
# with the nvmf module under test, and the guest's side of the test.
#
#   guest-root.sh --base DIR --out DIR --nbd N --hostnqn NQN --hostid ID
#                 --nvme STAGE --dracut STAGE
#                 [--lose-trsvcid PORT] [--restart] [--uptime SECONDS]
#
# --base     guest-base.sh's directory
# --out      the case's results directory: gets root.tar, vmlinuz,
#            initrd.img and log
# --nbd      the /dev/nbdN to use (one per slot)
# --nvme     the DESTDIR install of a tree (guest-build.sh, discoverd)
# --dracut   the DESTDIR install of the dracut tree to test
#            (dracut-build.sh), on this host
# --lose-trsvcid   after the boot, delete the root's controller on this
#            port, as the kernel does when it gives a path up (FC's
#            dev_loss_tmo), and wait for nvme-discoverd to connect it again
# --restart  then restart nvme-discoverd
# --uptime   seconds the guest waits before it powers off (default 5)
#
# The initrd is built in the guest's root, with that dracut and its kernel,
# to /boot/initrd.img-KVER, where dracut-shutdown.service finds it to go
# back to the initramfs at the shutdown. The network is the initrd's:
# ifupdown leaves eth0 alone ("manual"), as a root on the network wants;
# otherwise networking.service takes it down under the root.
#
# The guest's lab-handover.service reports what the hand-over left
# (LAB-HANDOVER-BEGIN PHASE ... LAB-HANDOVER-END PHASE on the serial
# console) after the boot, the loss and the restart, then powers off.
set -eu

BASE='' OUT='' NBD='' HOSTNQN='' HOSTID='' NVME='' MODULE='' LOSE='' RESTART=''
UPTIME=5
usage() { sed -n '6,8p' "$0" >&2; exit 2; }
while [ $# -gt 0 ]; do
    case $1 in
        --base) BASE=$2; shift ;;
        --out) OUT=$2; shift ;;
        --nbd) NBD=$2; shift ;;
        --hostnqn) HOSTNQN=$2; shift ;;
        --hostid) HOSTID=$2; shift ;;
        --nvme) NVME=$2; shift ;;
        --dracut) MODULE=$2; shift ;;
        --lose-trsvcid) LOSE=$2; shift ;;
        --restart) RESTART=1 ;;
        --uptime) UPTIME=$2; shift ;;
        *) usage ;;
    esac
    shift
done
for v in "$BASE" "$OUT" "$NBD" "$HOSTNQN" "$HOSTID" "$NVME" "$MODULE"; do
    [ -n "$v" ] || usage
done
[ -f "$BASE/base.qcow2" ] || { echo "no guest image in $BASE" >&2; exit 2; }

rm -rf "$OUT"
mkdir -p "$OUT"
exec > "$OUT/log" 2>&1
DEV=/dev/nbd$NBD MNT=$OUT/mnt DISK=$OUT/disk.qcow2

[ -e /sys/module/nbd ] || modprobe nbd nbds_max=16 max_part=0
[ -b "$DEV" ] || { echo "no $DEV"; exit 2; }
qemu-nbd -d "$DEV" >/dev/null 2>&1 || true

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
    rm -f "$DISK"
}
trap cleanup EXIT
fail() { echo "SETUP FAILED: $*"; exit 2; }

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
[ -d "$NVME" ] || fail "no install of a tree in $NVME"
cp -a "$NVME/." "$MNT/"
in_guest ldconfig
in_guest nvme version || fail "nvme does not run in the guest"
in_guest test -x /usr/sbin/nvme-discoverd || fail "the tree has no nvme-discoverd"

echo "== dracut"
# Replaces initramfs-tools; its own initrd is built below
# dpkg, not apt: apt picks the first alternative of a dependency
# ("iputils-arping | ... | systemd"), which is not in the image, though
# systemd is installed
in_guest sh -c 'dpkg --purge --force-depends initramfs-tools \
    initramfs-tools-core initramfs-tools-bin >/dev/null 2>&1; \
    DEBIAN_FRONTEND=noninteractive dpkg -i /var/cache/lab-debs/dracut/*.deb' \
    || fail "installing Debian's dracut"
in_guest dpkg-query -W dracut dracut-network
# The dracut under test over the package's files; the package's own
# modules go, as the tree's numbering may differ
[ -x "$MODULE/usr/bin/dracut" ] || fail "no dracut install in $MODULE"
rm -rf "$MNT/usr/lib/dracut/modules.d"
cp -a "$MODULE/." "$MNT/"
chown -R 0:0 "$MNT/usr/lib/dracut"
in_guest dracut --version
rm -rf "$MNT/var/cache/lab-debs"

echo "== configuration"
ROOT_DEV=$(sed -n 's/^\([^ ]*\) \/ .*/\1/p' "$MNT/etc/fstab")
echo "root: $ROOT_DEV -> the NVMe/TCP namespace (root= on the command line)"
sed -i '\| / |d' "$MNT/etc/fstab"
printf 'auto lo\niface lo inet loopback\n\n# The initrd configures eth0; the root is behind it\niface eth0 inet manual\n' \
    > "$MNT/etc/network/interfaces"
systemctl --root="$MNT" enable nvme-discoverd.service \
    || fail "no nvme-discoverd.service"
systemctl --root="$MNT" disable nvmf-autoconnect.service 2>/dev/null || true
systemctl --root="$MNT" enable dracut-shutdown.service \
    || fail "no dracut-shutdown.service"

# The guest's side of the test
cat > "$MNT/usr/local/sbin/lab-handover" <<EOF
#!/bin/sh
# Report what the hand-over left, break a path, restart nvme-discoverd,
# power off (nvme-lab).
mark() { echo "LAB-EVENT \$*" > /dev/kmsg; }
attr() { cat "\$1/\$2" 2>/dev/null | tr -d ' \\n'; }
report() {
    {
        echo "LAB-HANDOVER-BEGIN \$1"
        for c in /sys/class/nvme/nvme*; do
            [ -e "\$c" ] || continue
            trsvcid=\$(attr "\$c" address | sed -n 's/.*trsvcid=\\([0-9]*\\).*/\\1/p')
            echo "ctrl \${c##*/} \$(attr "\$c" transport) \$(attr "\$c" subsysnqn)" \\
                "trsvcid=\$trsvcid state=\$(attr "\$c" state)" \\
                "ctrl_loss_tmo=\$(attr "\$c" ctrl_loss_tmo)" \\
                "owner=\$(cat /run/nvme/registry/\${c##*/}/owner 2>/dev/null)"
        done
        for f in /run/nvme/pre-os.conf.d/*.conf; do
            [ -f "\$f" ] || continue
            grep '^controller' "\$f" | tr -d ' ' | sed "s|^|preos \${f##*/} |"
        done
        for u in \$(systemctl list-units --plain --no-legend --all \\
                    'nvme-discoverd-*.service' | awk '{ print \$1 }'); do
            stop=\$(systemctl show -p ExecStop --value "\$u")
            echo "unit \$u \$(systemctl is-active "\$u")" \\
                "execstop=\$([ -n "\$stop" ] && echo yes || echo no)" \\
                "\$(systemctl show -p Description --value "\$u" | sed 's/ /_/g')"
        done
        for s in nvme-discoverd dracut-shutdown networking; do
            echo "service \$s \$(systemctl is-active \$s.service)"
        done
        echo "need_shutdown \$([ -e /run/initramfs/.need_shutdown ] && echo yes || echo no)"
        echo "LAB-HANDOVER-END \$1"
    } > /dev/ttyS0
    # Let the console drain before the next kernel message
    sleep 1
}
# nvme-discoverd takes the connections over
sleep 10
report boot
LOSE=$LOSE
if [ -n "\$LOSE" ]; then
    for c in /sys/class/nvme/nvme*; do
        [ "\$(attr "\$c" cntrltype)" = io ] || continue
        attr "\$c" address | grep -q "trsvcid=\$LOSE" || continue
        mark "lose \${c##*/}"
        echo 1 > "\$c/delete_controller"
        break
    done
    # nvme-discoverd connects it again (no more than a minute)
    i=0
    while [ \$i -lt 60 ]; do
        found=''
        for c in /sys/class/nvme/nvme*; do
            [ "\$(attr "\$c" cntrltype)" = io ] || continue
            attr "\$c" address | grep -q "trsvcid=\$LOSE" && found=\${c##*/}
        done
        [ -n "\$found" ] && break
        i=\$((i + 1))
        sleep 1
    done
    mark "back \${found:-none} after \${i}s"
    sleep 2
    report lost
fi
if [ -n "$RESTART" ]; then
    mark "restart nvme-discoverd"
    systemctl restart nvme-discoverd.service
    sleep 10
    report restart
fi
dd if=/dev/zero of=/var/tmp/lab-data bs=1M count=32 2>/dev/null
sleep $UPTIME
mark "poweroff"
systemctl poweroff --no-block
EOF
chmod +x "$MNT/usr/local/sbin/lab-handover"
cat > "$MNT/etc/systemd/system/lab-handover.service" <<'EOF'
[Unit]
Description=nvme-lab: report the hand-over, then power off
After=nvme-discoverd.service multi-user.target

[Service]
Type=simple
ExecStart=/usr/local/sbin/lab-handover

[Install]
WantedBy=multi-user.target
EOF
systemctl --root="$MNT" enable lab-handover.service

echo "== initrd"
KVER=$(ls "$MNT/lib/modules")
in_guest dracut --force --no-hostonly --no-hostonly-cmdline \
    --add "nvmf systemd-networkd" \
    --add-drivers "nvme_tcp nvme_fabrics virtio_net virtio_pci ext4" \
    --kver "$KVER" "/boot/initrd.img-$KVER" > "$OUT/dracut.log" 2>&1 \
    || fail "dracut, see $OUT/dracut.log"
in_guest lsinitrd "/boot/initrd.img-$KVER" > "$OUT/lsinitrd.txt" 2>&1 || true
grep -q 'nvmf-shutdown\|74nvmf' "$OUT/lsinitrd.txt" \
    || echo "warning: no nvmf module in the initrd"
cp "$MNT/boot/initrd.img-$KVER" "$OUT/initrd.img"
cp -L "$MNT/boot/vmlinuz-$KVER" "$OUT/vmlinuz"

echo "== root.tar"
for m in dev/pts dev proc sys; do
    umount "$MNT/$m" 2>/dev/null || true
done
tar -C "$MNT" --one-file-system --numeric-owner -cpf "$OUT/root.tar" . \
    || fail "tar of the root"
ls -la "$OUT/root.tar"
trap - EXIT
cleanup || fail "cleaning up"
echo "== root ready"
