#!/bin/sh
# Create a container of 'lab boot' on the CI host: openSUSE Tumbleweed
# with dracut, a kernel, QEMU and the tools to build nvme-cli. It boots a
# QEMU guest from an NVMe/TCP root (see boot.sh), with the host's
# /dev/kvm. Each slot of 'lab boot' has its own container.
#
#   container.sh NAME HOSTNQN HOSTID [--recreate] [--image IMAGE]
#
# HOSTNQN/HOSTID become the container's /etc/nvme/hostnqn and hostid:
# the identity dracut puts into a hostonly initrd, and the one the target
# lets in. /root/scratch is /scratch in the container. --image starts the
# container from IMAGE, a copy of a container made here (docker commit),
# instead of installing the packages again.
set -eu

CI_IMAGE=ghcr.io/linux-nvme/tumbleweed:latest
# The initrd uses systemd and systemd-networkd with a static ip=, or
# network-legacy (boot.sh --network legacy), which needs dhclient
# (dhcp-client) and arping (iputils).
PACKAGES="kernel-default dracut qemu-x86 busybox-static nvme-cli \
e2fsprogs jq iproute2 kmod systemd-networkd meson ninja gcc git \
pkgconf-pkg-config dhcp-client iputils"

usage() {
    echo "usage: $0 NAME HOSTNQN HOSTID [--recreate] [--image IMAGE]" >&2
    exit 2
}
[ $# -ge 3 ] || usage
NAME=$1 HOSTNQN=$2 HOSTID=$3
shift 3
RECREATE='' IMAGE=''
while [ $# -gt 0 ]; do
    case $1 in
        --recreate) RECREATE=1 ;;
        --image) IMAGE=${2:?}; shift ;;
        *) usage ;;
    esac
    shift
done

if [ -n "$RECREATE" ]; then
    docker rm -f "$NAME" >/dev/null 2>&1 || true
fi

if docker inspect "$NAME" >/dev/null 2>&1; then
    # A container of an earlier setup, maybe stopped by a reboot
    docker start "$NAME" >/dev/null
elif [ -n "$IMAGE" ]; then
    docker run -d --name "$NAME" --privileged --network host \
        --device /dev/kvm -v /root/scratch:/scratch "$IMAGE" \
        sleep infinity >/dev/null
else
    docker run -d --name "$NAME" --privileged --network host \
        --device /dev/kvm -v /root/scratch:/scratch "$CI_IMAGE" \
        sleep infinity >/dev/null
    # The image has busybox-gawk, which conflicts with the gawk that
    # dracut needs: --force-resolution replaces it.
    # shellcheck disable=SC2086
    docker exec "$NAME" sh -c "zypper -q --non-interactive refresh && \
        zypper -q --non-interactive install -y --no-recommends \
        --force-resolution $PACKAGES" >/dev/null
    # The package's nvmf module, for the runs without an overlay.
    docker exec "$NAME" cp -a /usr/lib/dracut/modules.d/74nvmf \
        /root/74nvmf.package
fi

# The packages added to PACKAGES since the container was made
# shellcheck disable=SC2086
docker exec "$NAME" sh -c "rpm -q $PACKAGES >/dev/null 2>&1 || \
    zypper -q --non-interactive install -y --no-recommends \
    --force-resolution $PACKAGES" >/dev/null

docker exec "$NAME" sh -c "mkdir -p /etc/nvme && \
    echo '$HOSTNQN' > /etc/nvme/hostnqn && echo '$HOSTID' > /etc/nvme/hostid"
docker exec "$NAME" sh -c 'echo "kernel: $(ls /lib/modules)"; \
    rpm -q dracut nvme-cli qemu-x86'
