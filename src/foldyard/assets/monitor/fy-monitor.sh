#!/bin/bash
# fy-monitor — foldyard's guest-monitor helper (ADR-0031). Root-owned at
# /usr/local/libexec/fy-monitor, written by the monitor boot script from the HOST's recording,
# never from the repo mount. Three verbs, all run as root by systemd or the boot script:
#
#   boot     every boot: config, policy, units, the inbox; start Tetragon if installed
#   install  the inbox .path unit fired: verify the delivered release, install it, start it
#   report   after Tetragon starts: wait for the policy to load, record the outcome
#
# Everything the host needs to judge goes in /run/fy-monitor/ (world-readable: the host reads it
# over ssh as the VM user, without root). Never exits non-zero from `boot`: a failure is a
# report line, not a failed Lima boot.
set -uo pipefail

ID='@@ID@@'
VERSION='@@VERSION@@'
STATE=/run/fy-monitor
LIB=/var/lib/fy-monitor
INBOX=$LIB/inbox
STAGE=$LIB/stage
DELIVERY=$INBOX/tetragon.tar.gz
POLICY=fy-observe

case "$(uname -m)" in
    x86_64) ARCH=amd64 SHA='@@SHA_X86_64@@' ;;
    aarch64) ARCH=arm64 SHA='@@SHA_AARCH64@@' ;;
    *) ARCH='' SHA='' ;;
esac

# One key per file, replaced atomically, so a reader never sees half a report.
put() {
    install -d -m 0755 "$STATE"
    printf '%s\n' "$2" >"$STATE/$1.tmp"
    chmod 0644 "$STATE/$1.tmp"
    mv -f "$STATE/$1.tmp" "$STATE/$1"
}

installed() { [ -r "$LIB/installed" ] && [ "$(cat "$LIB/installed")" = "$SHA" ]; }

start_tetragon() {
    systemctl daemon-reload
    systemctl enable tetragon.service >/dev/null 2>&1
    put policy "loading"
    if systemctl restart tetragon.service; then
        systemctl start --no-block fy-monitor-report.service
    else
        put policy "failed: tetragon.service did not start"
    fi
}

verb_boot() {
    if [ -z "$ARCH" ]; then
        put artifact "failed: unsupported architecture $(uname -m)"
        return 0
    fi
    install -d -m 0755 "$LIB"
    install -d -m 0700 "$STAGE"
    # The inbox is the ONE thing the VM user (the host's ssh identity, and the box's uid) may
    # write. Whatever lands there is copied into root-owned space and checked against $SHA
    # before anything reads it as an archive.
    install -d -o "$FY_MONITOR_USER" -g "$FY_MONITOR_USER" -m 0700 "$INBOX"
    systemctl daemon-reload
    systemctl enable --now fy-monitor-install.path >/dev/null 2>&1
    if installed; then
        put artifact "installed $VERSION"
        start_tetragon
    else
        put artifact "awaiting $VERSION $ARCH"
        put policy "not installed"
    fi
}

verb_install() {
    [ -n "$ARCH" ] || return 0
    # A regular file only, never a link (the inbox owner could point one anywhere root can read).
    if [ ! -f "$DELIVERY" ] || [ -L "$DELIVERY" ]; then
        rm -f "$DELIVERY"
        return 0
    fi
    install -d -m 0700 "$STAGE"
    rm -rf "${STAGE:?}"/*
    timeout 300 cp --no-preserve=all "$DELIVERY" "$STAGE/tetragon.tar.gz"
    rm -f "$DELIVERY"
    # Verify the ROOT-OWNED copy: the inbox can change after this point and it does not matter.
    if ! echo "$SHA  $STAGE/tetragon.tar.gz" | sha256sum -c --status -; then
        rm -f "$STAGE/tetragon.tar.gz"
        put artifact "rejected: checksum mismatch (want $VERSION $ARCH)"
        return 0
    fi
    put artifact "installing $VERSION"
    if ! tar -C "$STAGE" -xzf "$STAGE/tetragon.tar.gz"; then
        put artifact "failed: could not unpack $VERSION"
        return 0
    fi
    src="$STAGE/tetragon-$VERSION-$ARCH/usr/local"
    rm -rf /usr/local/lib/tetragon
    cp -R "$src/lib/tetragon" /usr/local/lib/tetragon
    install -m 0755 "$src/bin/tetragon" "$src/bin/tetra" /usr/local/bin/
    rm -rf "${STAGE:?}"/*
    printf '%s\n' "$SHA" >"$LIB/installed"
    put artifact "installed $VERSION"
    start_tetragon
}

verb_report() {
    # Tetragon answers its socket before the policy's probes are attached; wait for the policy.
    for _ in $(seq 1 120); do
        if ! systemctl is-active --quiet tetragon.service; then
            put policy "failed: tetragon.service is $(systemctl is-active tetragon.service)"
            return 0
        fi
        if /usr/local/bin/tetra tracingpolicy list 2>/dev/null | grep -Eq "^[0-9]+ +$POLICY +enabled"; then
            put policy "loaded $POLICY"
            return 0
        fi
        sleep 5
    done
    put policy "failed: $POLICY not loaded after 600s"
}

case "${1:-}" in
    boot) verb_boot ;;
    install) verb_install ;;
    report) verb_report ;;
    *) echo "usage: fy-monitor boot|install|report  (id $ID)" >&2; exit 2 ;;
esac
