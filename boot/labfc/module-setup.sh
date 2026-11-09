#!/bin/bash
# nvme-lab: an NVMe/FC target in the initrd, on nvme-fcloop, for the FC
# hand-over scenarios (guest-root.sh --fc). The initrd's nvmf module
# connects it through dracut's FC path (95-nvmf-initqueue.rules), as on
# a host that boots with FC storage.

check() {
    return 255 # only when asked for
}

depends() {
    echo nvmf
}

installkernel() {
    instmods configfs nvmet nvmet-fc nvme-fc nvme-fcloop
}

install() {
    inst_multiple dd mkdir ln
    inst_hook pre-trigger 10 "$moddir/labfc-target.sh"
    inst_hook initqueue/finished 90 "$moddir/labfc-finished.sh"
}
