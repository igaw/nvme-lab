#!/bin/sh
# In the NetworkManager build container (lab-nm-build, boot.py): build
# nm-initrd-generator of the NetworkManager tree DIR, for the boots with
# network = "nm" and 'lab boot test --nm' (boot.sh --nm-generator).
#
#   nm-build.sh DIR
#
# The build directory is DIR/.lab-nm; prints the generator's path. The
# container is Tumbleweed, as the lab-boot containers that run the
# generator: it is built against the distribution's libnvme3-devel (and
# libnvme-devel for the fallback), as a package would be.
set -eu

[ $# -eq 1 ] || { echo "usage: $0 DIR" >&2; exit 2; }
TREE=$1
BUILD=$TREE/.lab-nm
# The build directory of a configuration this script no longer makes
[ "$(cat "$BUILD/.lab-nm-config" 2>/dev/null)" = 2 ] || rm -rf "$BUILD"

if [ ! -e /.lab-nm-deps ]; then
    zypper -q mr -e repo-source >/dev/null
    zypper -q --non-interactive refresh >/dev/null
    # busybox-gawk conflicts with gawk, gettext-tools (msgfmt) with the
    # image's gettext: --force-resolution replaces them.
    zypper -q --non-interactive si -d --force-resolution NetworkManager \
        >/dev/null
    zypper -q --non-interactive install -y --force-resolution \
        gettext-tools libnvme3-devel meson ninja gcc git >/dev/null
    touch /.lab-nm-deps
fi

git config --global --add safe.directory '*'
# Only what nm-initrd-generator needs; the NBFT reader is the point.
if [ ! -f "$BUILD/build.ninja" ]; then
    # The package's paths: with meson's default prefix (/usr/local) the
    # generator writes its connections where NetworkManager doesn't look.
    meson setup "$BUILD" "$TREE" --prefix=/usr --sysconfdir=/etc \
        --localstatedir=/var --libexecdir=/usr/libexec -Druntime_dir=/run \
        -Dnbft=true -Dclat=false -Dman=false \
        -Dintrospection=false -Ddocs=false -Dqt=false -Dnmtui=false \
        -Dtests=no -Dvapi=false > "$TREE/.lab-nm-setup.log" 2>&1 || {
        tail -20 "$TREE/.lab-nm-setup.log" >&2
        exit 2
    }
    echo 2 > "$BUILD/.lab-nm-config"
fi
ninja -C "$BUILD" src/nm-initrd-generator/nm-initrd-generator \
    > "$TREE/.lab-nm-build.log" 2>&1 || {
    grep -B3 -A12 'error:' "$TREE/.lab-nm-build.log" | head -60 >&2
    exit 2
}
grep -E '#define WITH_NBFT' "$BUILD/config.h" >&2
echo "$BUILD/src/nm-initrd-generator/nm-initrd-generator"
