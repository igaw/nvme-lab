#!/bin/sh
# In nvme-cli's Debian CI image (trixie, the guest's release): build a
# dracut tree for the Debian guest of the hand-over scenarios and install
# it into a staging directory.
#
#   dracut-build.sh TREE
#
# Installs to TREE/.lab-debian-stage, which guest-root.sh copies into the
# guest over Debian's dracut packages (they bring dracut's runtime
# dependencies). dracut's modules depend on its core (initqueue is a
# module of its own since dracut 107): a module is tested with the dracut
# it comes from.
set -eu

[ $# -eq 1 ] || { echo "usage: $0 TREE" >&2; exit 2; }
TREE=$1 STAGE=$1/.lab-debian-stage

export DEBIAN_FRONTEND=noninteractive
apt-get -qq update >/dev/null
apt-get -qq install -y make pkg-config libkmod-dev >/dev/null
cd "$TREE"
./configure --prefix=/usr --sysconfdir=/etc --libdir=/usr/lib \
    --disable-documentation > .lab-configure.log 2>&1 \
    || { cat .lab-configure.log; exit 1; }
make -j"$(nproc)" > .lab-make.log 2>&1 || { tail -30 .lab-make.log; exit 1; }
rm -rf "$STAGE"
make install DESTDIR="$STAGE" > .lab-install.log 2>&1 \
    || { tail -30 .lab-install.log; exit 1; }
echo "installed $(find "$STAGE" -type f | wc -l) files to $STAGE"
