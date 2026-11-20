#!/bin/bash
# fy-upgrade @@REL@@
# foldyard's one-off boot script for `fy machine upgrade` (docs/guest-os.md): Fedora upgraded in
# place to @@REL@@, as root, on the boot the host records it for; the host drops it again after.
# Idempotent: a boot after success, or on @@REL@@ already, only reports. Never exits non-zero: a
# failure is the report, read by the host, not a failed Lima boot.
set -uo pipefail
S=/var/lib/fy-upgrade
install -d -m 0755 "$S"
report() { printf '%s\n' "$1" >"$S/state.tmp" && chmod 0644 "$S/state.tmp" && mv -f "$S/state.tmp" "$S/state"; }
. /etc/os-release
if [ "${ID:-}" = fedora ] && [ "${VERSION_ID:-0}" -ge @@REL@@ ]; then
    report "done @@REL@@"
    exit 0
fi
touch "$S/log" && chmod 0644 "$S/log"
exec >>"$S/log" 2>&1
echo "== $(date -u +%FT%TZ) Fedora ${VERSION_ID:-?} -> @@REL@@"
report "running @@REL@@"
# The host stopped every container before this boot. The VM user's manager stays down for the
# whole transaction: a login (Lima's readiness checks, a client of the forwarded podman socket)
# would otherwise start it, and podman with it, while podman's own packages are being replaced.
user='{{.User}}'
uid="$(id -u "$user")"
systemctl mask --runtime "user@$uid.service"
systemctl stop "user@$uid.service" || true
dnf -y --releasever=@@REL@@ --setopt=install_weak_deps=False distro-sync --allowerasing
rc=$?
systemctl unmask --runtime "user@$uid.service"
if [ "$rc" = 0 ]; then
    echo "== $(date -u +%FT%TZ) ok"
    report "done @@REL@@"
else
    echo "== $(date -u +%FT%TZ) dnf exited $rc"
    report "failed @@REL@@: dnf exited $rc (the log: $S/log)"
fi
exit 0
