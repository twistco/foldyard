#!/bin/bash
# fy-forwarder @@ID@@
# foldyard's attributing-forwarder boot provisioning (ADR-0034). The HOST records this in the
# instance's lima.yaml as its own `provision: mode: system` script, beside foldyard's other boot
# scripts and under its own marker (so turning the forwarder on never changes their ids and never
# makes anyone else's VM stale); Lima runs it as root on every boot. Rendered from
# assets/forwarder/guest-boot.sh; the id above hashes the rendered content.
#
# It ALWAYS exits 0: Lima marks the whole boot failed on a non-zero script. What it applied, and
# what went wrong, is in /run/fy-fwd/ for the host to read; the host fails the launch on a
# forwarder that isn't serving.
set -uo pipefail
install -d -m 0755 /run/fy-fwd
exec >/run/fy-fwd/boot.log 2>&1
chmod 0644 /run/fy-fwd/boot.log
trap 'printf "%s\n" "applied @@ID@@" >/run/fy-fwd/applied; chmod 0644 /run/fy-fwd/applied' EXIT
MODE='@@MODE@@'
user='{{.User}}'
uid='{{.UID}}'
if [ -z "$user" ]; then
    user="$(awk '/NOPASSWD/ {print $1; exit}' /etc/sudoers.d/90-cloud-init-users 2>/dev/null)"
fi
if [ -z "$uid" ] && [ -n "$user" ]; then
    uid="$(id -u "$user")"
fi

state() {
    printf '%s\n' "$1" >/run/fy-fwd/state.tmp
    chmod 0644 /run/fy-fwd/state.tmp
    mv -f /run/fy-fwd/state.tmp /run/fy-fwd/state
}

remove() {
    systemctl disable --now fy-fwd.service >/dev/null 2>&1
    rm -f /etc/systemd/system/fy-fwd.service /usr/local/libexec/fy-fwd \
        /usr/local/libexec/fy-fwd-wait /usr/share/containers/oci/hooks.d/fy-fwd-wait.json \
        /etc/containers/containers.conf.d/93-fy-fwd-bridge.conf \
        /etc/fy-fwd/forwarder.key
    rmdir /etc/fy-fwd 2>/dev/null
    # The policy rules before the table: rules left without it would route the walled
    # principals' connections to lo with nothing to catch them.
    for prio in $(seq 4747 4810); do
        ip rule del priority "$prio" 2>/dev/null
    done
    ip route flush table 4747 2>/dev/null
    nft delete table ip fy_fwd 2>/dev/null
    rm -f /run/fy-fwd/netns /run/fy-fwd/stats
    systemctl daemon-reload
}

if [ "$MODE" = "off" ]; then
    # Turned off after having been on: remove what an earlier boot installed.
    remove
    state off
    echo "forwarder: off (removed)"
    exit 0
fi

if [ -z "$uid" ] || [ ! -x /usr/bin/python3 ] || ! command -v nft >/dev/null; then
    state "failed: no VM user uid, python3 or nft in this guest"
    echo "forwarder: not installed (uid='$uid')"
    exit 0
fi

# The walled principals, as the VM firewall names them: the VM user and its subuid range.
walled="$uid"
name="$(id -nu "$uid" 2>/dev/null)"
sub="$(awk -F: -v u="$name" -v i="$uid" '$1==u || $1==i {print $2 ":" $3; exit}' /etc/subuid 2>/dev/null)"
if [ -n "$sub" ]; then
    walled="$walled, ${sub%%:*}-$((${sub%%:*} + ${sub##*:} - 1))"
fi

# 1. The forwarder and its key. The key is the host's (the proxy verifies every stamp with it) and
#    root-only here: the VM user — the box's uid — can't read it, so it can't forge a stamp.
#    Written with the shell's own printf (a builtin: the key never appears in a process's
#    arguments).
install -d -m 0755 /usr/local/libexec
cat >/usr/local/libexec/fy-fwd.tmp <<'__FY_FWD__'
@@FWD@@
__FY_FWD__
chmod 0755 /usr/local/libexec/fy-fwd.tmp
mv -f /usr/local/libexec/fy-fwd.tmp /usr/local/libexec/fy-fwd
install -d -m 0700 /etc/fy-fwd
(umask 077 && printf '%s\n' '@@KEY@@' >/etc/fy-fwd/forwarder.key)

# 2. The OCI hook that holds a container's start until the forwarder serves the rootless namespace
#    it will use (assets/forwarder/wait.sh), and bridge as the default network: a plain
#    `podman run` otherwise gets a pasta helper of its own, which the forwarder can't attribute.
cat >/usr/local/libexec/fy-fwd-wait.tmp <<'__FY_FWD_WAIT__'
@@WAIT@@
__FY_FWD_WAIT__
chmod 0755 /usr/local/libexec/fy-fwd-wait.tmp
mv -f /usr/local/libexec/fy-fwd-wait.tmp /usr/local/libexec/fy-fwd-wait
# podman reads hooks from /usr/share/containers/oci/hooks.d only (its default `hooks_dir`; the
# /etc directory is not read unless containers.conf names it, and naming it there would replace
# whatever list another drop-in set).
install -d -m 0755 /usr/share/containers/oci/hooks.d /etc/containers/containers.conf.d
cat >/usr/share/containers/oci/hooks.d/fy-fwd-wait.json <<'__FY_FWD_HOOK__'
{
  "version": "1.0.0",
  "hook": {"path": "/usr/local/libexec/fy-fwd-wait"},
  "when": {"always": true},
  "stages": ["prestart"]
}
__FY_FWD_HOOK__
cat >/etc/containers/containers.conf.d/93-fy-fwd-bridge.conf <<'__FY_FWD_CONF__'
# foldyard: containers default to the bridge network, where the forwarder sees each one's own
# address (ADR-0034).
[containers]
netns = "bridge"
__FY_FWD_CONF__
chmod 0644 /usr/share/containers/oci/hooks.d/fy-fwd-wait.json \
    /etc/containers/containers.conf.d/93-fy-fwd-bridge.conf

# 3. The unit: NEVER enabled — this script starts it at every boot, once everything above is
#    current (an enabled unit would start early with the previous boot's copy).
printf '%s\n' \
    '[Unit]' \
    'Description=foldyard attributing forwarder (ADR-0034)' \
    'After=network-online.target' \
    '' \
    '[Service]' \
    "ExecStart=/usr/bin/python3 -I /usr/local/libexec/fy-fwd --id @@ID@@ --uid $uid --walled '$walled' --gw @@GW@@ --proxy-ports '@@PROXY_PORTS@@' --plain-ports '@@PLAIN_PORTS@@'" \
    'Restart=always' \
    'RestartSec=1' >/etc/systemd/system/fy-fwd.service
systemctl daemon-reload
rm -f /etc/systemd/system/multi-user.target.wants/fy-fwd.service
state "starting @@ID@@"
if systemctl restart fy-fwd.service; then
    echo "forwarder: started (walled $walled)"
else
    state "failed: fy-fwd.service did not start"
fi
# The rootless podman service reads containers.conf at its start: one already running (a boot
# that started it before this script) keeps pasta as its default until restarted.
sudo -u "#$uid" XDG_RUNTIME_DIR="/run/user/$uid" \
    systemctl --user try-restart podman.service 2>/dev/null || true
exit 0
