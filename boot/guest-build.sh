#!/bin/sh
# In nvme-cli's Debian CI image (trixie, the guest's release): build an
# nvme-cli tree for the Debian guest of the shutdown scenarios and
# install it into a staging directory.
#
#   guest-build.sh TREE FLAVOR
#
# FLAVOR: legacy (nvme-cli 3 without nvme-discoverd, so with the
# autoconnect units; nvme-cli 2 has nothing else) or discoverd (nvme-cli 3
# with nvme-discoverd). Builds in TREE/.lab-debian-FLAVOR and installs
# with DESTDIR to TREE/.lab-debian-FLAVOR-stage, which guest-image.sh
# copies into the guest. nvme-cli 2 builds its own libnvme, as in
# boot.sh.
set -eu

[ $# -eq 2 ] || { echo "usage: $0 TREE legacy|discoverd" >&2; exit 2; }
TREE=$1 FLAVOR=$2
BUILD=$TREE/.lab-debian-$FLAVOR STAGE=$TREE/.lab-debian-$FLAVOR-stage

git config --global --add safe.directory '*'
OPTS=''
if grep -q "'nvme-discoverd'" "$TREE/meson_options.txt" 2>/dev/null; then
    case $FLAVOR in
        discoverd) OPTS='-Dnvme-discoverd=enabled' ;;
        legacy) OPTS='-Dnvme-discoverd=disabled' ;;
        *) echo "bad flavor $FLAVOR" >&2; exit 2 ;;
    esac
elif [ "$FLAVOR" = discoverd ]; then
    echo "$TREE has no nvme-discoverd" >&2
    exit 2
fi
exec 9> "$TREE/.lab-debian.lock"
flock 9
if [ ! -f "$BUILD/build.ninja" ]; then
    # The libdir of Debian's ld.so; the image's meson takes lib64
    # shellcheck disable=SC2086
    meson setup "$BUILD" "$TREE" --prefix=/usr --sysconfdir=/etc \
        --libdir=lib/x86_64-linux-gnu \
        --wrap-mode=default --force-fallback-for=libnvme \
        --buildtype=release -Ddocs=false $OPTS
fi
ninja -C "$BUILD" >/dev/null
rm -rf "$STAGE"
DESTDIR=$STAGE meson install -C "$BUILD" --no-rebuild --quiet
# The guest gets its own host identity
rm -f "$STAGE/etc/nvme/hostnqn" "$STAGE/etc/nvme/hostid"
echo "installed $(find "$STAGE" -type f | wc -l) files to $STAGE"
