#!/bin/sh
# The initrd waits for the FC connection: the root is local and would
# not wait for it.
for c in /sys/class/nvme/nvme*; do
    [ "$(cat "$c/subsysnqn" 2> /dev/null)" = nqn.2026-11.io.lab:fc-data ] \
        && [ "$(cat "$c/cntrltype" 2> /dev/null)" = io ] && exit 0
done
exit 1
