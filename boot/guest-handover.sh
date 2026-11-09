#!/bin/sh
# In the lab-boot container: boot the Debian guest of a hand-over case
# (guest-root.sh) from the slot's NVMe/TCP root, through dracut, let it
# run and power off, and check how.
#
#   guest-handover.sh --name NAME --target TRADDR:TRSVCID --nqn NQN
#                     --root-uuid UUID [--path2 TRSVCID] [--timeout SECONDS]
#                     [--slot N]
#   guest-handover.sh --name NAME --fc [--timeout SECONDS] [--slot N]
#
# Puts root.tar of /scratch/lab-boot/out/NAME on the root subsystem NQN
# (an ext4 file system with UUID, connected as the container's host, the
# guest's too), boots the kernel and initrd next to it with the root on
# NVMe/TCP from the command line (rd.nvmf.discover=, one per path: TARGET,
# and --path2 for a multipath root), and afterwards puts the slot's
# busybox root back (mkroot.sh), which the boot scenarios need.
#
# With --fc, the root is the local disk.qcow2 of guest-root.sh --fc, and
# the pre-OS connection is the NVMe/FC one the initrd makes to its own
# fcloop target (rd.nvmf.discover=fc,auto).
#
# Results in /scratch/lab-boot/out/NAME: console.txt, cmdline, and
# state.txt, one fact per line:
#   booted yes|no               the guest's report after the boot is there
#   phase NAME LINE             a line of the guest's report of a phase
#                               (boot, lost, restart): ctrl, preos, unit,
#                               service, need_shutdown lines
#   poweroff yes|no, powered_off yes|no, shutdown_seconds N
#   root_umount T               the kernel time the root was unmounted
#   pre_os_disconnect T         the time the shutdown initramfs
#                               disconnected the first pre-OS controller
#   removed T NAME              each "Removing ctrl" after poweroff
#   error LINE                  a console line after poweroff that shows
#                               the shutdown went wrong
#   event LINE                  a step of the run, in console order
# Exits 0 when the guest booted and powered off, 1 otherwise, 2 if it
# could not be set up.
set -u

NAME='' TARGET='' NQN='' ROOT_UUID='' PATH2='' TIMEOUT=600 SLOT=1 FC=''
usage() { sed -n '6,8p' "$0" >&2; exit 2; }
while [ $# -gt 0 ]; do
    case $1 in
        --name) NAME=$2; shift ;;
        --target) TARGET=$2; shift ;;
        --nqn) NQN=$2; shift ;;
        --root-uuid) ROOT_UUID=$2; shift ;;
        --path2) PATH2=$2; shift ;;
        --fc) FC=1 ;;
        --timeout) TIMEOUT=$2; shift ;;
        --slot) SLOT=$2; shift ;;
        *) usage ;;
    esac
    shift
done
[ -n "$NAME" ] || usage
[ -n "$FC" ] || for v in "$TARGET" "$NQN" "$ROOT_UUID"; do
    [ -n "$v" ] || usage
done
TRADDR=${TARGET%:*} TRSVCID=${TARGET##*:}
OUT=/scratch/lab-boot/out/$NAME
HERE=$(dirname "$0")
ROOTFILE=root.tar
[ -z "$FC" ] || ROOTFILE=disk.qcow2
for f in $ROOTFILE vmlinuz initrd.img; do
    [ -f "$OUT/$f" ] || { echo "no $f in $OUT" >&2; exit 2; }
done
echo "$SLOT" > "$OUT/slot"
fail() { echo "SETUP FAILED: $*" | tee -a "$OUT/log" >&2; exit 2; }

restore_root() {
    [ -z "$FC" ] || return 0
    sh "$HERE/mkroot.sh" "$TRADDR" "$TRSVCID" "$NQN" "$ROOT_UUID" \
        >> "$OUT/log" 2>&1 || echo "could not restore the busybox root" >&2
}

# The root file system
[ -n "$FC" ] || {
    nvme disconnect -n "$NQN" >/dev/null 2>&1 || true
    nvme connect -t tcp -a "$TRADDR" -s "$TRSVCID" -n "$NQN" 2>&1 \
        | grep -v libkmod
    sleep 2
    CTRL=$(nvme list-subsys -o json | jq -r \
        ".[].Subsystems[] | select(.NQN==\"$NQN\") | .Paths[0].Name")
    [ -n "$CTRL" ] && [ "$CTRL" != null ] || { echo "not connected"; exit 1; }
    for NS in "/sys/class/nvme/$CTRL/"nvme*n1; do NS=${NS##*/}; break; done
    BLK=$(echo "$NS" | sed 's/c[0-9]*n/n/')
    if [ ! -b "/dev/$BLK" ]; then
        IFS=: read -r MAJOR MINOR < "/sys/class/block/$BLK/dev"
        mknod "/dev/$BLK" b "$MAJOR" "$MINOR"
    fi
    MNT=/mnt/lab-handover-root
    mkdir -p "$MNT"
    wipefs -q -a "/dev/$BLK"
    mkfs.ext4 -q -F -L lab-handover -U "$ROOT_UUID" "/dev/$BLK" || exit 1
    mount "/dev/$BLK" "$MNT" || exit 1
    tar -C "$MNT" --numeric-owner -xpf "$OUT/root.tar" || { umount "$MNT"; exit 1; }
    umount "$MNT" || exit 1
    nvme disconnect -n "$NQN" >/dev/null
    echo "root: /dev/$BLK on $NQN, UUID $ROOT_UUID"
} >> "$OUT/log" 2>&1 || { restore_root; fail "the root file system, see $OUT/log"; }
rm -f "$OUT/root.tar"

HOSTNQN=$(cat /etc/nvme/hostnqn) HOSTID=$(cat /etc/nvme/hostid)
if [ -n "$FC" ]; then
    CMDLINE="console=ttyS0 root=/dev/vda rw net.ifnames=0 panic=-1"
    CMDLINE="$CMDLINE rd.nvmf.discover=fc,auto"
    set -- -drive "file=$OUT/disk.qcow2,if=virtio,format=qcow2"
    ROOTBLK='vda'
else
    CMDLINE="console=ttyS0 root=UUID=$ROOT_UUID rw net.ifnames=0 panic=-1"
    CMDLINE="$CMDLINE rd.neednet=1 ip=10.0.2.15::10.0.2.2:255.255.255.0:guest:eth0:none"
    CMDLINE="$CMDLINE rd.nvmf.discover=tcp,$TRADDR,,$TRSVCID"
    [ -z "$PATH2" ] || CMDLINE="$CMDLINE rd.nvmf.discover=tcp,$TRADDR,,$PATH2"
    set --
    ROOTBLK='nvme[0-9]+n[0-9]+'
fi
CMDLINE="$CMDLINE rd.nvmf.hostnqn=$HOSTNQN rd.nvmf.hostid=$HOSTID"
CMDLINE="$CMDLINE rd.retry=10 rd.shell=0 rd.emergency=poweroff"
CMDLINE="$CMDLINE loglevel=7 printk.devkmsg=on systemd.show_status=1"
echo "$CMDLINE" > "$OUT/cmdline"
timeout "$TIMEOUT" qemu-system-x86_64 -enable-kvm -m 1024 -smp 2 \
    -nographic -no-reboot -kernel "$OUT/vmlinuz" \
    -initrd "$OUT/initrd.img" -append "$CMDLINE" \
    -netdev user,id=n0 -device virtio-net-pci,netdev=n0 "$@" \
    > "$OUT/console.raw" 2>&1
QEMU=$?
tr -d '\r' < "$OUT/console.raw" | sed -e 's/\x1b\[[0-9;?!]*[a-zA-Z]//g' \
    -e 's/\x1b\][^\x07\x1b]*\(\x07\|\x1b\\\)//g' -e 's/\x1b[c78=>]//g' \
    -e 's/^guest login: *//' > "$OUT/console.txt"
rm -f "$OUT/console.raw" "$OUT/vmlinuz" "$OUT/disk.qcow2"
C=$OUT/console.txt
restore_root

ktime() { grep -o '\[ *[0-9]*\.[0-9]*\]' | head -1 | tr -d '[] '; }
after=$(grep -n 'LAB-EVENT poweroff' "$C" | head -1 | cut -d: -f1)
start=$(grep 'LAB-EVENT poweroff' "$C" | head -1 | ktime)
end=$(grep 'reboot: Power down' "$C" | tail -1 | ktime)
since() { [ -n "$after" ] && tail -n "+$after" "$C"; }
{
    grep -q 'LAB-HANDOVER-BEGIN boot' "$C" && echo "booted yes" || echo "booted no"
    for p in boot lost restart; do
        sed -n "/LAB-HANDOVER-BEGIN $p/,/LAB-HANDOVER-END $p/p" "$C" \
            | sed '/LAB-HANDOVER-/d' | sed "s/^/phase $p /"
    done
    [ -n "$after" ] && echo "poweroff yes" || echo "poweroff no"
    [ -n "$end" ] && echo "powered_off yes" || echo "powered_off no"
    if [ -n "$start" ] && [ -n "$end" ]; then
        echo "shutdown_seconds $(echo "$end $start" | awk '{ printf "%d", $1 - $2 + 0.5 }')"
    fi
    # The root's file system, unmounted in the shutdown initramfs
    t=$(since | grep -E "EXT4-fs \\($ROOTBLK\\): unmounting filesystem" | head -1 | ktime)
    echo "root_umount ${t:--}"
    # nvmf-shutdown.sh's lines have no kernel time, and the console may
    # show them after the kernel's: the first removal after the root's
    # unmount, if the hook ran
    root_t=$(since | grep -E "EXT4-fs \\($ROOTBLK\\): unmounting filesystem" | head -1 | ktime)
    t=''
    if [ -n "$root_t" ] && since | grep -q 'nvmf: disconnect'; then
        t=$(since | grep 'Removing ctrl' | while read -r l; do
            lt=$(echo "$l" | ktime)
            awk -v a="$lt" -v b="$root_t" 'BEGIN { exit !(a >= b) }' \
                && { echo "$lt"; break; }
        done)
    fi
    echo "pre_os_disconnect ${t:--}"
    since | grep 'Removing ctrl' | while read -r l; do
        echo "removed $(echo "$l" | ktime) $(echo "$l" | sed -n 's/.*\(nvme[0-9]*\): Removing ctrl.*/\1/p')"
    done
    since | grep -E \
        'nvme_keep_alive_end_io|no available path|I/O tag [0-9]+ .* timeout|blocked for more than|A stop job is running|Failed unmounting|timed out|Cannot umount|I/O error' \
        | head -20 | sed 's/^/error /'
    grep -E 'LAB-EVENT|nvmf: disconnect|Removing ctrl|unmounting filesystem|nvme-discoverd|Switching root|reboot: Power down' "$C" \
        | head -80 | sed 's/^/event /'
} > "$OUT/state.txt"
[ "$QEMU" = 124 ] && echo "timeout: QEMU killed after ${TIMEOUT}s" >> "$OUT/log"

if grep -qx 'booted yes' "$OUT/state.txt" \
        && grep -qx 'powered_off yes' "$OUT/state.txt"; then
    echo "RESULT=PASS $NAME"
    cat "$OUT/state.txt"
    exit 0
fi
echo "RESULT=FAIL $NAME, see $OUT/console.txt"
cat "$OUT/state.txt"
exit 1
