#!/bin/sh
# On the CI host: the Debian 13 (trixie) guest of the shutdown scenarios,
# built once with debootstrap in nvme-cli's Debian CI image.
#
#   guest-base.sh DIR
#
# Writes DIR/base.qcow2 (an ext4 file system on the whole disk: the guest
# boots with root=/dev/vda, no partitions, no boot loader), the guest's
# kernel and initrd (DIR/vmlinuz, DIR/initrd.img, from the image) and
# DIR/version. The network is ifupdown's (networking.service) with a
# static eth0 for QEMU's user networking, as on a Debian server. The
# packages of Debian's nvme-cli (2.13) and lvm2 are in the image, not
# installed, under /var/cache/lab-debs/{nvme-cli,lvm2}: guest-image.sh
# installs them for a case that wants them. Does nothing when
# DIR/base.qcow2 exists; remove DIR to build it again.
set -eu

[ $# -eq 1 ] || { echo "usage: $0 DIR" >&2; exit 2; }
DIR=$1
IMAGE=ghcr.io/linux-nvme/debian:latest
[ -f "$DIR/base.qcow2" ] && exit 0

# The runtime libraries an nvme-cli tree built in the CI image may link
PACKAGES="systemd-sysv udev dbus kmod ifupdown iproute2 iputils-ping \
linux-image-amd64 e2fsprogs procps util-linux mount uuid-runtime \
libjson-c5 libssl3t64 libkeyutils1 libdbus-1-3 liburing2 libarchive13t64"

mkdir -p "$DIR"
rm -rf "$DIR/tmp"
mkdir -p "$DIR/tmp"
# apt is slow with the huge nofile limit docker gives a container
# shellcheck disable=SC2016 # expanded in the container
docker run --rm --privileged --network host --ulimit nofile=1024:1024 \
    -v "$DIR/tmp:/work" "$IMAGE" \
    sh -euc '
    export DEBIAN_FRONTEND=noninteractive
    apt-get -q update >/dev/null
    apt-get -q install -y debootstrap >/dev/null
    R=/work/root
    debootstrap --variant=minbase --include="$(echo $1 | tr " " ,)" \
        trixie "$R" http://deb.debian.org/debian > /work/debootstrap.log
    echo guest > "$R/etc/hostname"
    printf "/dev/vda / ext4 rw,errors=remount-ro 0 1\n" > "$R/etc/fstab"
    printf "auto lo\niface lo inet loopback\n\nauto eth0\niface eth0 inet static\n    address 10.0.2.15/24\n    gateway 10.0.2.2\n" \
        > "$R/etc/network/interfaces"
    # Debian'"'"'s nvme-cli and lvm2, to install when a case wants them
    # The cache has the packages of debootstrap
    chroot "$R" apt-get clean
    for p in nvme-cli lvm2; do
        chroot "$R" apt-get -q -d install -y "$p" >/dev/null
        mkdir -p "$R/var/cache/lab-debs/$p"
        mv "$R"/var/cache/apt/archives/*.deb "$R/var/cache/lab-debs/$p/"
    done
    chroot "$R" apt-get clean
    {
        echo "kernel $(ls "$R/lib/modules")"
        for d in "$R"/var/cache/lab-debs/*/*.deb; do
            echo "deb ${d##*/}"
        done
    } > /work/version
    # The host owns the result
    ' guest "$PACKAGES"

ROOT=$DIR/tmp/root
cp -L "$ROOT/vmlinuz" "$DIR/vmlinuz"
cp -L "$ROOT/initrd.img" "$DIR/initrd.img"
mkfs.ext4 -q -F -L lab-guest -d "$ROOT" "$DIR/base.raw" 4G
qemu-img convert -O qcow2 "$DIR/base.raw" "$DIR/base.qcow2.new"
mv "$DIR/tmp/version" "$DIR/version"
rm -rf "$DIR/base.raw" "$DIR/tmp"
mv "$DIR/base.qcow2.new" "$DIR/base.qcow2"
cat "$DIR/version"
