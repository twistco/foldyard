#!/usr/bin/env bash
# Prototype: upgrade a foldyard Lima VM's Fedora in place, hands-off, keeping its disk (and so its
# podman store: images, volumes, containers). Root in a foldyard guest exists only at boot, so the
# upgrade runs as a one-off recorded `provision: mode: system` script under its own marker, which
# is removed again afterwards.
#
#   upgrade-inplace.sh <vm> [release]      (default release: 45)
set -euo pipefail
VM="$1"
SIMG="${SENTINEL_IMAGE:-docker.io/library/alpine:3.20}"
REL="${2:-45}"
MARK="# fy-upgrade"
LOG="${LOG:-/dev/stderr}"
t0=$(date +%s)
say() { printf '[%4ss] %s\n' "$(($(date +%s) - t0))" "$*" | tee -a "$LOG" >&2; }

in_vm() { limactl shell --workdir / "$VM" -- "$@"; }

# The guest side, run as root at boot. Idempotent: a second boot after success does nothing.
read -r -d '' GUEST <<'EOS' || true
#!/bin/bash
# fy-upgrade @@REL@@
set -uo pipefail
S=/var/lib/fy-upgrade
mkdir -p "$S"
[ -f "$S/done-@@REL@@" ] && exit 0
. /etc/os-release
[ "$VERSION_ID" = "@@REL@@" ] && { touch "$S/done-@@REL@@"; exit 0; }
exec >>"$S/log" 2>&1
echo "== $(date -u +%FT%TZ) upgrading Fedora $VERSION_ID -> @@REL@@"
# Nothing of the VM user's runs while its packages are swapped: its podman service, containers,
# conmon and pasta all live under the user manager.
user='{{.User}}'
uid="$(id -u "$user")"
# Containers stopped cleanly first (a database gets its shutdown, not a power cut), then the user
# manager masked for this boot: a login (Lima's readiness checks, a client of the podman socket)
# would otherwise start it, and podman with it, mid-transaction.
runuser -u "$user" -- env XDG_RUNTIME_DIR="/run/user/$uid" podman stop -a -t 30 >/dev/null 2>&1 || true
systemctl mask --runtime "user@$uid.service"
systemctl stop "user@$uid.service" || true
echo "user@$uid: $(systemctl is-active "user@$uid.service"), masked for this boot"
if dnf -y --releasever=@@REL@@ --setopt=install_weak_deps=False distro-sync --allowerasing; then
    touch "$S/done-@@REL@@"
    echo "== $(date -u +%FT%TZ) ok"
else
    echo "== $(date -u +%FT%TZ) FAILED ($?)"
    touch "$S/failed-@@REL@@"
fi
systemctl unmask --runtime "user@$uid.service"
exit 0
EOS
GUEST="${GUEST//@@REL@@/$REL}"

snapshot() { # what must survive, as the VM user sees it
    in_vm sh -c '
        . /etc/os-release; echo "os $VERSION_ID"; echo "kernel $(uname -r)"
        echo "podman $(podman --version | cut -d" " -f3)"
        podman volume ls --format "volume {{.Name}}" | sort
        podman images --format "image {{.ID}} {{.Repository}}:{{.Tag}}" | sort
        podman ps -a --format "container {{.Names}} {{.Image}}" | sort
        printf "sentinel "; podman volume exists fy-upgrade-sentinel && podman run --rm --network none --entrypoint sha256sum -v fy-upgrade-sentinel:/s:ro "$1" /s/blob | cut -d" " -f1
    ' _ "$SIMG" 2>/dev/null
}

say "preflight: $VM"
in_vm sh -c '. /etc/os-release; echo "Fedora $VERSION_ID, $(df -h / | awk "NR==2{print \$4}") free on /"' | tee -a "$LOG" >&2
if [ "${SENTINEL:-1}" = 1 ]; then
    in_vm sh -c 'podman volume exists fy-upgrade-sentinel || { podman volume create fy-upgrade-sentinel >/dev/null && podman run --rm --network none --entrypoint sh -v fy-upgrade-sentinel:/s "$1" -c "head -c 67108864 /dev/urandom > /s/blob"; }' _ "$SIMG" >/dev/null
fi
snapshot >"$LOG.before"
say "before: $(grep -c '^volume' "$LOG.before") volumes, $(grep -c '^image' "$LOG.before") images, $(grep -c '^container' "$LOG.before") containers, $(grep '^os\|^podman' "$LOG.before" | tr '\n' ' ')"

say "1/4 stop, record the upgrade boot script"
limactl stop "$VM" >/dev/null 2>&1
entry="$(python3 -I -c 'import json,sys; print(json.dumps({"mode": "system", "script": sys.stdin.read()}))' <<<"$GUEST")"
limactl edit "$VM" --tty=false --set ".provision += [$entry]" >/dev/null

say "2/4 boot: the upgrade runs as root before the VM reports ready"
limactl start "$VM" --tty=false --timeout 60m >/dev/null 2>&1 || say "  (limactl start returned non-zero; reading the guest's own record)"
in_vm sh -c 'tail -3 /var/lib/fy-upgrade/log 2>/dev/null; ls /var/lib/fy-upgrade' | tee -a "$LOG" >&2
in_vm test -f "/var/lib/fy-upgrade/done-$REL" || { say "✗ the upgrade didn't finish: its script stays recorded, the VM is as the log says"; exit 1; }

say "3/4 stop, drop the boot script, boot the new kernel"
limactl stop "$VM" >/dev/null 2>&1
limactl edit "$VM" --tty=false --set ".provision = [(.provision // [])[] | select((.script // \"\") | test(\"$MARK\") | not)]" >/dev/null
limactl start "$VM" --tty=false --timeout 15m >/dev/null 2>&1

say "4/4 podman system migrate, then compare"
in_vm podman system migrate >/dev/null 2>&1 || true
snapshot >"$LOG.after"
say "after: $(grep '^os\|^kernel\|^podman' "$LOG.after" | tr '\n' ' ')"
if diff <(grep -v '^os\|^kernel\|^podman' "$LOG.before") <(grep -v '^os\|^kernel\|^podman' "$LOG.after") >"$LOG.diff"; then
    say "✓ every volume, image and container is still there, and the sentinel's sha256 matches"
else
    say "✗ differences:"; cat "$LOG.diff" >&2
fi
say "done in $(($(date +%s) - t0)) s"
