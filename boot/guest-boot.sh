#!/bin/sh
# In the lab-boot container: boot the Debian guest of a shutdown case
# (its disk from guest-image.sh), let it power off and check how.
#
#   guest-boot.sh --name NAME --base DIR --target TRADDR:TRSVCID --nqn NQN
#                 --data-uuid UUID [--timeout SECONDS] [--slot N]
#
# First puts a fresh ext4 file system with UUID on the namespace of NQN
# (the guest's data disk), connected as the container's host, the one the
# guest has too. Then boots the guest from /scratch/lab-boot/out/NAME/
# disk.qcow2, with the kernel and initrd of the image in --base. The guest
# mounts the file system, reports and powers off (guest-image.sh); QEMU
# is killed after TIMEOUT seconds (default 600).
#
# Results in /scratch/lab-boot/out/NAME: console.txt, cmdline, and
# state.txt, one fact per line:
#   mounted yes|no              the guest saw /srv/data mounted
#   poweroff yes|no             it ran systemctl poweroff
#   powered_off yes|no          the kernel powered the guest off
#   shutdown_seconds N          from poweroff to "reboot: Power down"
#   order umount=T disconnect=T ifdown=T   first kernel time of each step
#                               after poweroff (- if none)
#   error LINE                  a console line after poweroff that shows
#                               the shutdown went wrong
#   event LINE                  a shutdown step, in console order
#   guest LINE                  the guest's report at the mount
# Prints RESULT=PASS when the guest mounted and powered off, else
# RESULT=FAIL; nvmelab/guest.py judges the rest. Exits 0 (PASS), 1 (FAIL)
# or 2 (could not be set up).
set -u

NAME='' BASE='' TARGET='' NQN='' DATA_UUID='' TIMEOUT=600 SLOT=1
usage() { sed -n '5,6p' "$0" >&2; exit 2; }
while [ $# -gt 0 ]; do
    case $1 in
        --name) NAME=$2; shift ;;
        --base) BASE=$2; shift ;;
        --target) TARGET=$2; shift ;;
        --nqn) NQN=$2; shift ;;
        --data-uuid) DATA_UUID=$2; shift ;;
        --timeout) TIMEOUT=$2; shift ;;
        --slot) SLOT=$2; shift ;;
        *) usage ;;
    esac
    shift
done
for v in "$NAME" "$BASE" "$TARGET" "$NQN" "$DATA_UUID"; do
    [ -n "$v" ] || usage
done
TRADDR=${TARGET%:*} TRSVCID=${TARGET##*:}
OUT=/scratch/lab-boot/out/$NAME
[ -f "$OUT/disk.qcow2" ] || { echo "no disk in $OUT" >&2; exit 2; }
echo "$SLOT" > "$OUT/slot"
fail() { echo "SETUP FAILED: $*" | tee -a "$OUT/log" >&2; exit 2; }

# The data file system
{
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
    wipefs -q -a "/dev/$BLK"
    mkfs.ext4 -q -F -L lab-data -U "$DATA_UUID" "/dev/$BLK" || exit 1
    nvme disconnect -n "$NQN" >/dev/null
    echo "data: /dev/$BLK on $NQN, UUID $DATA_UUID"
} >> "$OUT/log" 2>&1 || fail "the data file system, see $OUT/log"

CMDLINE="console=ttyS0 root=/dev/vda rw net.ifnames=0 panic=-1"
# The kernel's messages of the shutdown (EXT4 and nvme info) and systemd's
# status on the console, in order
CMDLINE="$CMDLINE loglevel=7 printk.devkmsg=on systemd.show_status=1"
echo "$CMDLINE" > "$OUT/cmdline"
timeout "$TIMEOUT" qemu-system-x86_64 -enable-kvm -m 1024 -smp 2 \
    -nographic -no-reboot -kernel "$BASE/vmlinuz" \
    -initrd "$BASE/initrd.img" -append "$CMDLINE" \
    -drive "file=$OUT/disk.qcow2,if=virtio,format=qcow2" \
    -netdev user,id=n0 -device virtio-net-pci,netdev=n0 \
    > "$OUT/console.raw" 2>&1
QEMU=$?
# No CRs, no terminal control sequences (CSI, OSC), no login prompt in
# front of a kernel message
tr -d '\r' < "$OUT/console.raw" | sed -e 's/\x1b\[[0-9;?!]*[a-zA-Z]//g' \
    -e 's/\x1b\][^\x07\x1b]*\(\x07\|\x1b\\\)//g' -e 's/\x1b[c78=>]//g' \
    -e 's/^guest login: *//' > "$OUT/console.txt"
rm -f "$OUT/console.raw"
C=$OUT/console.txt

# The kernel time of a console line, "[  83.123456] ..."
ktime() { grep -o '\[ *[0-9]*\.[0-9]*\]' | head -1 | tr -d '[] '; }
after=$(grep -n 'LAB-EVENT poweroff' "$C" | head -1 | cut -d: -f1)
start=$(grep 'LAB-EVENT poweroff' "$C" | head -1 | ktime)
end=$(grep 'reboot: Power down' "$C" | tail -1 | ktime)
first() {   # the kernel time of the first line after poweroff matching $1
    [ -n "$after" ] || { echo -; return; }
    t=$(tail -n "+$after" "$C" | grep -E "$1" | head -1 | ktime)
    echo "${t:--}"
}
{
    grep -q 'LAB-EVENT mounted' "$C" && echo "mounted yes" || echo "mounted no"
    [ -n "$after" ] && echo "poweroff yes" || echo "poweroff no"
    [ -n "$end" ] && echo "powered_off yes" || echo "powered_off no"
    if [ -n "$start" ] && [ -n "$end" ]; then
        echo "shutdown_seconds $(echo "$end $start" | awk '{ printf "%d", $1 - $2 + 0.5 }')"
    fi
    echo "order umount=$(first 'EXT4-fs \(nvme.*unmounting filesystem')" \
        "disconnect=$(first 'nvme nvme[0-9]+: Removing ctrl')" \
        "ifdown=$(first 'LAB-EVENT ifdown-done eth0')"
    if [ -n "$after" ]; then
        tail -n "+$after" "$C" | grep -E \
            'error -101|nvme_keep_alive_end_io|no available path|I/O tag [0-9]+ .* timeout|blocked for more than|A stop job is running|Failed unmounting|timed out' \
            | head -20 | sed 's/^/error /'
        tail -n "+$after" "$C" | grep -E \
            'LAB-EVENT|unmounting filesystem|Removing ctrl|srv-data|networking\.service|nvmf-autoconnect|nvme-discoverd|nvmf-connect|lvm2|reboot: Power down' \
            | head -60 | sed 's/^/event /'
    fi
    sed -n '/LAB-GUEST-STATE-BEGIN/,/LAB-GUEST-STATE-END/p' "$C" \
        | sed '/LAB-GUEST-STATE-/d; s/^/guest /'
} > "$OUT/state.txt"
[ "$QEMU" = 124 ] && echo "timeout: QEMU killed after ${TIMEOUT}s" >> "$OUT/log"

if grep -qx 'mounted yes' "$OUT/state.txt" \
        && grep -qx 'powered_off yes' "$OUT/state.txt"; then
    echo "RESULT=PASS $NAME"
    cat "$OUT/state.txt"
    exit 0
fi
echo "RESULT=FAIL $NAME, see $OUT/console.txt"
cat "$OUT/state.txt"
exit 1
