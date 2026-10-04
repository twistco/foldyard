#!/bin/bash
# fy-monitor @@ID@@
# foldyard's guest-monitor boot provisioning (ADR-0031). The HOST records this in the instance's
# lima.yaml as a SECOND `provision: mode: system` script, beside foldyard's fy-provision one (so
# turning the monitor on never changes that script's id); Lima runs it as root on every boot.
# Rendered from assets/monitor/guest-boot.sh; the id above hashes the rendered content.
#
# It ALWAYS exits 0: Lima marks the whole boot failed on a non-zero script, and the monitor only
# observes. What it applied, and what went wrong, is in /run/fy-monitor/ for the host to read.
set -uo pipefail
install -d -m 0755 /run/fy-monitor
exec >/run/fy-monitor/boot.log 2>&1
chmod 0644 /run/fy-monitor/boot.log
trap 'printf "%s\n" "applied @@ID@@" >/run/fy-monitor/applied; chmod 0644 /run/fy-monitor/applied' EXIT
MODE='@@MODE@@'
user='{{.User}}'
uid='{{.UID}}'
if [ -z "$user" ]; then
    user="$(awk '/NOPASSWD/ {print $1; exit}' /etc/sudoers.d/90-cloud-init-users 2>/dev/null)"
fi
if [ -z "$uid" ] && [ -n "$user" ]; then
    uid="$(id -u "$user")"
fi

if [ "$MODE" = "off" ]; then
    # Turned off after having been on: remove what an earlier boot installed. The release stays
    # cached nowhere in the guest; turning it back on re-delivers it.
    systemctl disable --now fy-monitor-relay.service tetragon.service fy-monitor-install.path \
        fy-monitor-report.service fy-monitor-install.service 2>/dev/null
    rm -f /etc/systemd/system/tetragon.service /etc/systemd/system/fy-monitor-*.service \
        /etc/systemd/system/fy-monitor-install.path /usr/local/libexec/fy-monitor \
        /usr/local/libexec/fy-monitor-relay /usr/local/bin/tetragon /usr/local/bin/tetra
    rm -rf /etc/fy-monitor
    # NOT /var/run/tetragon: Tetragon mounts the cgroup2 hierarchy at /var/run/tetragon/cgroup2,
    # and it is still mounted here (an earlier boot left the unit enabled, so it started before
    # this script ran) — `rm -rf` would descend into it and rmdir every empty system cgroup. It is
    # on tmpfs; the next shutdown clears it (seen live, 2026-10-04).
    rm -rf /etc/tetragon /usr/local/lib/tetragon /var/lib/fy-monitor /var/log/tetragon
    systemctl daemon-reload
    printf 'off\n' >/run/fy-monitor/artifact
    printf 'off\n' >/run/fy-monitor/policy
    chmod 0644 /run/fy-monitor/artifact /run/fy-monitor/policy
    echo "monitor: off (removed)"
    exit 0
fi

# 1. The helper, root-owned, from this recording.
install -d -m 0755 /usr/local/libexec
cat >/usr/local/libexec/fy-monitor.tmp <<'__FY_MONITOR_HELPER__'
@@HELPER@@
__FY_MONITOR_HELPER__
chmod 0755 /usr/local/libexec/fy-monitor.tmp
mv -f /usr/local/libexec/fy-monitor.tmp /usr/local/libexec/fy-monitor

# 1b. The relay (assets/monitor/relay.py) and the key it signs the spool with. The key is the
#     host's (it verifies each spool line with it) and root-only here: the VM user — the box's
#     uid — reads the spool but can't forge a line of it. Written with the shell's own printf
#     (a builtin: the key never appears in a process's arguments).
cat >/usr/local/libexec/fy-monitor-relay.tmp <<'__FY_MONITOR_RELAY__'
@@RELAY@@
__FY_MONITOR_RELAY__
chmod 0755 /usr/local/libexec/fy-monitor-relay.tmp
mv -f /usr/local/libexec/fy-monitor-relay.tmp /usr/local/libexec/fy-monitor-relay
install -d -m 0700 /etc/fy-monitor
(umask 077 && printf '%s\n' '@@RELAY_KEY@@' >/etc/fy-monitor/relay.key)

# 2. Tetragon's configuration and policy: replaced whole on every boot, so nothing an earlier
#    boot (or anyone else) left in either directory survives.
rm -rf /etc/tetragon
install -d -m 0755 /etc/tetragon/tetragon.conf.d /etc/tetragon/tetragon.tp.d
conf() { printf '%s\n' "$2" >"/etc/tetragon/tetragon.conf.d/$1"; }
conf bpf-lib /usr/local/lib/tetragon/bpf/
conf server-address unix:///var/run/tetragon/tetragon.sock
# The health server listens on every interface by default — and the box reaches the VM's own
# address (spike finding 4). Loopback is out of its reach.
conf health-server-address 127.0.0.1:6789
conf gops-address ''
conf metrics-server ''
conf export-filename /var/log/tetragon/tetragon.log
conf export-file-max-size-mb 10
conf export-file-max-backups 5
conf export-file-compress true
# Namespace inodes on every event: attribution keys on the mount namespace, resolved against
# podman's own scopes — never on Tetragon's container-id guess (spike finding 5).
conf enable-process-ns true
conf log-level info
conf log-format text
cat >/etc/tetragon/tetragon.tp.d/fy-observe.yaml <<'__FY_MONITOR_POLICY__'
@@POLICY@@
__FY_MONITOR_POLICY__

# 2b. Tetragon and the relay are NEVER enabled units: this script starts them, every boot, once
#     their config above is current. Enabled, they started early in boot with the PREVIOUS boot's
#     config — a policy change only took effect minutes later, when this script restarted them
#     (live, 2026-10-04: an old policy's feedback loop ran ~90 s into a boot that had fixed it).
#     Early boot is the price: nothing a box can do runs before this script anyway.
systemctl disable tetragon.service fy-monitor-relay.service >/dev/null 2>&1
rm -f /etc/systemd/system/multi-user.target.wants/tetragon.service \
    /etc/systemd/system/multi-user.target.wants/fy-monitor-relay.service

# 3. The units: Tetragon itself (foldyard's own unit, not the release's), the installer the
#    inbox triggers, and the one-shot that records whether the policy loaded.
cat >/etc/systemd/system/tetragon.service <<'__FY_UNIT__'
[Unit]
Description=Tetragon eBPF collector (foldyard guest monitor)
After=network.target local-fs.target
StartLimitBurst=10
StartLimitIntervalSec=2min

[Service]
Environment="PATH=/usr/local/lib/tetragon/:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
ExecStart=/usr/local/bin/tetragon
Restart=on-failure
RestartSec=5
__FY_UNIT__
cat >/etc/systemd/system/fy-monitor-install.path <<'__FY_UNIT__'
[Unit]
Description=foldyard guest monitor: a delivered release is waiting

[Path]
PathExists=/var/lib/fy-monitor/inbox/tetragon.tar.gz
Unit=fy-monitor-install.service

[Install]
WantedBy=multi-user.target
__FY_UNIT__
cat >/etc/systemd/system/fy-monitor-install.service <<'__FY_UNIT__'
[Unit]
Description=foldyard guest monitor: verify and install the delivered release

[Service]
Type=oneshot
ExecStart=/usr/local/libexec/fy-monitor install
__FY_UNIT__
cat >/etc/systemd/system/fy-monitor-report.service <<'__FY_UNIT__'
[Unit]
Description=foldyard guest monitor: record whether the policy loaded
After=tetragon.service

[Service]
Type=oneshot
ExecStart=/usr/local/libexec/fy-monitor report
__FY_UNIT__
# The relay's one argument is the VM user's uid: podman's scopes live in its delegated subtree.
printf '%s\n' \
    '[Unit]' \
    'Description=foldyard guest monitor: sign and spool events for the host' \
    'After=tetragon.service' \
    '' \
    '[Service]' \
    "ExecStart=/usr/bin/python3 /usr/local/libexec/fy-monitor-relay $uid" \
    'Restart=always' \
    'RestartSec=5' >/etc/systemd/system/fy-monitor-relay.service

# 4. Apply: start Tetragon if this release is already installed, else wait for the host.
FY_MONITOR_USER="$user" /usr/local/libexec/fy-monitor boot
echo "monitor: $(cat /run/fy-monitor/artifact 2>/dev/null)"
exit 0
