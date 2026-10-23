#!/bin/sh
# fy-fwd-wait — foldyard's attributing forwarder's OCI hook (ADR-0034), root-owned at
# /usr/local/libexec/fy-fwd-wait and named by /usr/share/containers/oci/hooks.d/fy-fwd-wait.json, both
# written by the forwarder's boot script. The runtime runs it before each container starts, as
# the VM user inside podman's own mount namespace, where the rootless network namespace's bind
# mount is visible.
#
# The first container after the last bridge container stopped is the one that brings the rootless
# namespace back, and the forwarder needs a moment to follow it there; until it does, that
# container's first connection to the proxy is refused. So this waits, at most 3 s, until the
# forwarder reports (/run/fy-fwd/netns, world-readable) the namespace that exists NOW.
# Reliability only, never security: a container that skips it races and fails closed.
#
# Always exits 0: a failing hook would stop the container, and the forwarder is not this hook's
# to enforce.
ns=""
for f in /run/user/*/containers/networks/rootless-netns/rootless-netns; do
    if [ -e "$f" ]; then
        ns=$f
        break
    fi
done
[ -n "$ns" ] || exit 0
want="$(stat -L -c %i "$ns" 2>/dev/null)" || exit 0
i=0
while [ "$(cat /run/fy-fwd/netns 2>/dev/null)" != "$want" ] && [ "$i" -lt 300 ]; do
    sleep 0.01
    i=$((i + 1))
done
exit 0
