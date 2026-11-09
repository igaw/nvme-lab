#!/bin/sh
# An NVMe/FC target on nvme-fcloop, set up before udev's trigger: adding
# the remote port with the discovery role makes nvme-fc send the
# nvmediscovery event that dracut's FC path connects on.
LABFC_NQN=nqn.2026-11.io.lab:fc-data
LABFC_HOST_WWNN=0x10001100aa000001
LABFC_HOST_WWPN=0x20001100aa000001
LABFC_TGT_WWNN=0x10001100ab000001
LABFC_TGT_WWPN=0x20001100ab000001

modprobe -a configfs nvmet nvmet-fc nvme-fc nvme-fcloop
[ -d /sys/kernel/config/nvmet ] || mount -t configfs none /sys/kernel/config
N=/sys/kernel/config/nvmet
S=$N/subsystems/$LABFC_NQN
# /run moves to the real root: the namespace outlives the initrd
dd if=/dev/zero of=/run/lab-fc.img bs=1M count=64 2> /dev/null
mkdir -p "$S/namespaces/1"
echo 1 > "$S/attr_allow_any_host"
echo /run/lab-fc.img > "$S/namespaces/1/device_path"
echo 1 > "$S/namespaces/1/enable"
L=/sys/class/fcloop/ctl
echo "wwnn=$LABFC_HOST_WWNN,wwpn=$LABFC_HOST_WWPN" > $L/add_local_port
echo "wwnn=$LABFC_TGT_WWNN,wwpn=$LABFC_TGT_WWPN" > $L/add_target_port
mkdir -p "$N/ports/1"
echo fc > "$N/ports/1/addr_trtype"
echo fc > "$N/ports/1/addr_adrfam"
echo "nn-$LABFC_TGT_WWNN:pn-$LABFC_TGT_WWPN" > "$N/ports/1/addr_traddr"
ln -s "$S" "$N/ports/1/subsystems/"
echo "wwnn=$LABFC_TGT_WWNN,wwpn=$LABFC_TGT_WWPN,lpwwnn=$LABFC_HOST_WWNN,lpwwpn=$LABFC_HOST_WWPN,roles=0x60" \
    > $L/add_remote_port
echo "labfc: NVMe/FC target $LABFC_NQN on fcloop" > /dev/kmsg
