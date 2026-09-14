#!/bin/bash
# fy-provision @@ID@@
# foldyard guest boot provisioning. The HOST records this in the instance's lima.yaml as a
# `provision: mode: system` script; Lima runs it as root on EVERY boot, after cloud-init. Root
# in the guest is boot-time only — the host never runs sudo here (machine.py). Rendered from
# assets/machine-wall/guest-boot.sh; the id above hashes the rendered content and is how the
# host tells a stale recording from the current one.
set -uo pipefail
install -d -m 0755 /run/fy-wall
# The only boot diagnostics a sudo-less user can read (cloud-init's own log is root-only).
exec >/run/fy-wall/boot.log 2>&1
chmod 0644 /run/fy-wall/boot.log
set -e

# 1. The sudo grant. cloud-init re-creates `NOPASSWD:ALL` for the Lima user on every boot (the
#    instance id changes each boot), so narrow it on every boot — to Lima's own non-passwordless
#    form: shutdown only, which a graceful `limactl stop` still needs. The Lima user is the uid
#    the dev box runs as; a container escape must land on a user with no path to root.
#    `{{.User}}` / `{{.UID}}` are Lima template fields, rendered into this script at every start
#    (Lima's cidata env is internal; a script that references it draws a warning on every
#    limactl call); the sudoers file cloud-init just wrote is the fallback source of the user.
sudoers=/etc/sudoers.d/90-cloud-init-users
user='{{.User}}'
uid='{{.UID}}'
if [ -z "$user" ] && [ -r "$sudoers" ]; then
    user="$(awk '/NOPASSWD/ {print $1; exit}' "$sudoers")"
fi
if [ -z "$user" ]; then
    echo "sudo: could not resolve the Lima user; grant left as cloud-init wrote it" >&2
    exit 1
fi
if [ -z "$uid" ]; then
    uid="$(id -u "$user")"
fi
printf '%s ALL=(ALL) NOPASSWD:/sbin/shutdown -h now\n' "$user" >"$sudoers.fy"
visudo -cf "$sudoers.fy" >/dev/null
install -m 0440 "$sudoers.fy" "$sudoers"
rm -f "$sudoers.fy"
echo "sudo: '$user' may only 'shutdown -h now' without a password"

# 2. The wall script, root-owned in the guest — from this recording, never from the repo mount.
install -d -m 0755 /usr/local/libexec
cat >@@WALL_PATH@@.tmp <<'__FY_WALL_ASSET__'
@@WALL_ASSET@@
__FY_WALL_ASSET__
chmod 0755 @@WALL_PATH@@.tmp
mv -f @@WALL_PATH@@.tmp @@WALL_PATH@@

# 3. Apply the wall state the host recorded (`[machine].wall` + this project's port band).
FY_WALL_UID="$uid" @@WALL_PATH@@ @@WALL_ARGS@@

# 4. Report, LAST: an absent report means "not applied", which the host fails closed on.
printf '%s\n' '@@WANT@@' >/run/fy-wall/state.tmp
chmod 0644 /run/fy-wall/state.tmp
mv -f /run/fy-wall/state.tmp /run/fy-wall/state
echo "applied: @@WANT@@"
