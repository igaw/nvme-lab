#!/bin/sh
# What the initrd hands over (nvme-lab): after nvmf's cleanup hook, the
# registry entries with their uevent stamps, the kernel's current uevent
# seqnum, and whether the shutdown initramfs was asked for.
for e in /run/nvme/registry/*; do
    [ -d "$e" ] || continue
    echo "labfc: registry ${e##*/} owner=$(cat "$e/owner" 2> /dev/null) seqnum=$(cat "$e/seqnum" 2> /dev/null)" > /dev/kmsg
done
[ -d /run/nvme/registry ] || echo "labfc: no registry" > /dev/kmsg
echo "labfc: uevent_seqnum $(cat /sys/kernel/uevent_seqnum) need_shutdown $([ -e /run/initramfs/.need_shutdown ] && echo yes || echo no)" > /dev/kmsg
