#!/bin/sh
# In the lab-boot container: put the test root on the NVMe/TCP namespace.
#
#   mkroot.sh TRADDR TRSVCID NQN UUID
#
# An ext4 file system with the given UUID, busybox, an os-release (systemd
# switches only into a root that has one) and an /init that reports what
# the initrd handed over and powers the guest off. Its report, between
# LAB-BOOT-STATE-BEGIN and LAB-BOOT-STATE-END, has one line per NVMe
# controller, "ctrl NAME TRANSPORT SUBSYSNQN KEY=VALUE..." with its
# state, trsvcid, ctrl_loss_tmo, hostnqn and host_iface, and one per entry of the
# nvme-cli registry (/run moves along at switch_root), "registry NAME
# owner=OWNER" (empty: unowned), and one per connection the initrd
# recorded for the OS environment (nvme-cli#4328, /run/nvme/pre-os.conf.d),
# "pre-os-conf FILE CONTROLLER-LINE" (a file per host), and one per name
# nbftN the kernel resolves, "iface-name nbftN LINK" with the interface
# that has it: LINK is nbftN when the initrd renamed the interface to it,
# another when nbftN is an alternative name (the kernel's name lookup
# resolves both, sysfs only shows the first). Then it prints
# "LAB-BOOT-ROOT-OK <kernel command line>".
set -eu

[ $# -eq 4 ] || { echo "usage: $0 TRADDR TRSVCID NQN UUID" >&2; exit 2; }
TRADDR=$1 TRSVCID=$2 NQN=$3 UUID=$4
MNT=/mnt/lab-boot-root

nvme disconnect -n "$NQN" >/dev/null 2>&1 || true
nvme connect -t tcp -a "$TRADDR" -s "$TRSVCID" -n "$NQN" 2>&1 \
    | grep -v libkmod || true
sleep 2
CTRL=$(nvme list-subsys -o json \
    | jq -r ".[].Subsystems[] | select(.NQN==\"$NQN\") | .Paths[0].Name")
if [ -z "$CTRL" ] || [ "$CTRL" = null ]; then
    echo "not connected" >&2
    exit 1
fi
for NS in "/sys/class/nvme/$CTRL/"nvme*n1; do NS=${NS##*/}; break; done
# With native multipath the block device is nvmeXnY, not nvmeXcZnY.
BLK=$(echo "$NS" | sed 's/c[0-9]*n/n/')
# The container's /dev does not follow devices that appear later.
# Always made anew: a node an earlier connect left has an old
# device number, which the kernel, shared by all slots, may have
# given to another slot's namespace since
IFS=: read -r MAJOR MINOR < "/sys/class/block/$BLK/dev"
rm -f "/dev/$BLK"
mknod "/dev/$BLK" b "$MAJOR" "$MINOR"

mkfs.ext4 -q -F -L lab-boot-root -U "$UUID" "/dev/$BLK"
mkdir -p "$MNT"
mount "/dev/$BLK" "$MNT"
# systemd moves /dev, /proc, /sys and /run to the new root only if their
# mount points exist.
mkdir -p "$MNT/bin" "$MNT/proc" "$MNT/sys" "$MNT/dev" "$MNT/run" \
    "$MNT/etc" "$MNT/usr/lib"
printf 'NAME=lab-boot-root\nID=lab-boot-root\n' > "$MNT/usr/lib/os-release"
ln -sf ../usr/lib/os-release "$MNT/etc/os-release"
cp /usr/bin/busybox-static "$MNT/bin/busybox"
ln -sf busybox "$MNT/bin/sh"
cat > "$MNT/init" <<'EOF'
#!/bin/sh
B=/bin/busybox
$B mount -t proc proc /proc
# No kernel messages in the middle of the report on the serial console
echo 1 > /proc/sys/kernel/printk
echo LAB-BOOT-STATE-BEGIN
attr() { $B cat "$1/$2" 2>/dev/null | $B tr -d ' \n'; }
for c in /sys/class/nvme/nvme*; do
    [ -e "$c" ] || continue
    trsvcid=$(attr "$c" address | $B sed -n 's/.*trsvcid=\([0-9]*\).*/\1/p')
    echo "ctrl ${c##*/} $(attr "$c" transport) $(attr "$c" subsysnqn)" \
        "state=$(attr "$c" state) trsvcid=$trsvcid" \
        "ctrl_loss_tmo=$(attr "$c" ctrl_loss_tmo) hostnqn=$(attr "$c" hostnqn)" \
        "host_iface=$(attr "$c" address | $B sed -n 's/.*host_iface=\([^,]*\).*/\1/p')"
done
for n in 0 1 2 3; do
    link=$($B ip -o link show dev "nbft$n" 2>/dev/null \
        | $B sed -n 's/^[0-9]*: \([^:@]*\).*/\1/p')
    [ -z "$link" ] || echo "iface-name nbft$n $link"
done
for e in /run/nvme/registry/*; do
    [ -d "$e" ] || continue
    echo "registry ${e##*/} owner=$($B cat "$e/owner" 2>/dev/null)"
done
for f in /run/nvme/pre-os.conf.d/*.conf; do
    [ -f "$f" ] || continue
    $B grep '^controller' "$f" | $B tr -d ' ' | while read -r c; do
        echo "pre-os-conf ${f##*/} $c"
    done
done
echo LAB-BOOT-STATE-END
echo "LAB-BOOT-ROOT-OK $($B cat /proc/cmdline)"
# Let the serial line drain: sysrq prints at its own log level
$B sleep 1
echo o > /proc/sysrq-trigger
/bin/busybox sleep 30
EOF
chmod +x "$MNT/init"
umount "$MNT"
nvme disconnect -n "$NQN" >/dev/null
echo "root: /dev/$BLK on $NQN, UUID $UUID"
