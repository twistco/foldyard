#!/bin/bash
# fy machine wall — the nftables egress wall for the REAL lima machine VM.
#
# Runs INSIDE the guest as root (machine.py streams it over stdin via
# `limactl shell <name> sudo bash -s -- …` — never staged in the guest's /tmp, so a
# non-root guest process can't swap the file between copy and root execution). NOT the
# standalone proof rig — that lives at
# docs/lima-network-forcing-kit/ (its own throwaway VM + red-team battery; the packaged
# `fy wall` verb was retired). This script enforces the SAME model on
# the project's real machine, with one architectural difference: there is NO in-VM proxy. The
# chokepoint stays the Mac-side mitmdump egress proxy (allowlist + keyless injection + network
# log all live on the Mac, and real credentials NEVER enter the VM); the wall just makes the VM
# fail-closed — the walled uid's only way out is the Mac proxy at the Lima host gateway.
#
# Walled principals: the Lima default user (resolved from SUDO_UID — `limactl shell` logs in as
# that user, sudo records it) AND its rootless-podman SUBUID range (from /etc/subuid). Normal
# container egress (the dev box, stack services, builds, pulls — via pasta/slirp4netns) NATs out
# as the user's own uid, so the uid rule catches it; but a `--network=host` container process
# running as a non-root CONTAINER user egresses with a host-visible SUBUID (no pasta NAT hop), so
# the wall must default-deny the subuid range too — else `podman run --network=host --user 1000 …`
# tunnels straight past it. The moat: the user has no path to VM-root beyond sudo… which Lima
# grants — so the wall is enforcement against the BOX (whose container has no sudo/rootful socket),
# and defense-in-depth for the VM user. The security argument is ADR-0009: no in-VM wall holds
# against VM-root; the backstop is the Mac-side chokepoint.
#
# Usage (all as root in the guest):
#   machine-wall.sh install <host_gateway_ip> "<tcp_port_ranges>" <proxy_url>
#       e.g. install 192.168.5.2 "41000-41089, 41100-41189" http://192.168.5.2:41000
#       (the ranges/URL are the project's allocated daemon port band — foldyard's ports.py)
#   machine-wall.sh uninstall
#   machine-wall.sh status
#
# Idempotent: re-running install rewrites config + restarts the unit (rule reload, no drift).
set -euo pipefail

CMD="${1:?usage: machine-wall.sh install|uninstall|status …}"

_wall_user() {
    # The walled uid: FY_WALL_UID env override, else the sudo caller (the Lima default user).
    if [ -n "${FY_WALL_UID:-}" ]; then
        echo "$FY_WALL_UID"
    elif [ -n "${SUDO_UID:-}" ] && [ "$SUDO_UID" != "0" ]; then
        echo "$SUDO_UID"
    else
        echo "✗ can't resolve the walled uid (run via sudo, or set FY_WALL_UID)" >&2
        exit 1
    fi
}

case "$CMD" in
install)
    GW="${2:?install needs <host_gateway_ip>}"
    PORTS="${3:?install needs \"<tcp_port_ranges>\" (nft set elements)}"
    PROXY_URL="${4:?install needs <proxy_url>}"
    WALL_UID="$(_wall_user)"
    WALL_HOME="$(getent passwd "$WALL_UID" | cut -d: -f6)"
    WALL_NAME="$(getent passwd "$WALL_UID" | cut -d: -f1)"
    # Fail fast on a uid with no passwd entry: an empty WALL_HOME would otherwise send the
    # proxy-env `install -d`/`chown -R` below at "/.config/…", and an empty WALL_NAME would
    # break the /etc/subuid lookup.
    if [ -z "$WALL_HOME" ] || [ -z "$WALL_NAME" ]; then
        echo "✗ uid $WALL_UID has no passwd entry (getent) — can't resolve its home/name" >&2
        exit 1
    fi

    # The walled user's rootless-podman SUBUID range (/etc/subuid: LOGIN:START:COUNT, keyed by
    # name OR uid). Container processes map into this range, and a --network=host container skips
    # the pasta NAT, so its packets carry a subuid, not WALL_UID — the wall must cover both.
    # SUB is the ", start-end" tail spliced into the nft skuid set; empty (no entry) degrades to
    # the old uid-only rule, so a VM without /etc/subuid still installs.
    SUB=""
    if [ -r /etc/subuid ]; then
        sub_line="$(awk -F: -v u="$WALL_NAME" -v i="$WALL_UID" '$1==u || $1==i {print; exit}' /etc/subuid)"
        if [ -n "$sub_line" ]; then
            sub_start="$(echo "$sub_line" | cut -d: -f2)"
            sub_count="$(echo "$sub_line" | cut -d: -f3)"
            if [ -n "$sub_start" ] && [ -n "$sub_count" ] && [ "$sub_count" -gt 0 ] 2>/dev/null; then
                SUB=", ${sub_start}-$((sub_start + sub_count - 1))"
            fi
        fi
    fi

    # The wall's `log` lines land in dmesg (recourse via fy-wall-denied).
    modprobe nf_log_syslog 2>/dev/null || true

    # CRITICAL: no ROOTFUL podman socket may be reachable from a container — container-root over a
    # rootful socket is VM-root, i.e. `nft flush` and the wall is gone. foldyard only ever uses the
    # user's rootless socket, so masking the system one costs nothing. (Same rule as the proof rig.)
    systemctl disable --now podman.socket podman.service 2>/dev/null || true
    systemctl mask podman.socket 2>/dev/null || true
    rm -f /run/podman/podman.sock 2>/dev/null || true

    install -d /etc/fy-wall
    # NB: NOT `flush ruleset` (the proof rig owns its whole VM; here netavark/pasta may hold nft
    # state we must not nuke). Own tables only; the unit's ExecStartPre clears stale copies.
    cat >/etc/fy-wall/wall.nft <<EOF
table inet fy_wall {
  chain output {
    type filter hook output priority 0; policy accept;
    oifname "lo" accept
    ct state established,related accept
    meta skuid != { $WALL_UID$SUB } accept comment "exempt system uids (root/services) — NOT the walled user or its container subuids"
    ip daddr { 127.0.0.0/8, 10.0.0.0/8, 172.16.0.0/12, 192.168.0.0/16 } udp dport 53 accept comment "DNS to LOCAL resolvers only"
    ip daddr { 127.0.0.0/8, 10.0.0.0/8, 172.16.0.0/12, 192.168.0.0/16 } tcp dport 53 accept comment "a public-IP :53 stream is an exfil tunnel, not resolution — deny it"
    ip daddr $GW tcp dport { $PORTS } accept comment "Mac-side foldyard daemons (proxy, minters)"
    log prefix "fy-wall-deny " counter
    meta l4proto tcp reject with tcp reset comment "REJECT fast, not a silent hang"
    reject
  }
  chain forward {
    type filter hook forward priority 0; policy accept;
    ct state established,related accept
    ip daddr { 127.0.0.0/8, 10.0.0.0/8, 172.16.0.0/12, 192.168.0.0/16 } accept
    log prefix "fy-wall-deny-fwd " counter comment "bridged (rootful) container egress"
    reject
  }
}
table ip6 fy_wall6 {
  chain output {
    type filter hook output priority 0; policy accept;
    meta skuid != { $WALL_UID$SUB } accept comment "system uids only — walled user + container subuids fall through"
    oifname "lo" accept
    log prefix "fy-wall-deny6 " counter comment "force the walled uid onto the v4 proxy path"
    reject
  }
}
EOF

    cat >/etc/systemd/system/fy-wall.service <<'EOF'
[Unit]
Description=foldyard egress wall (default-deny the podman user; Mac proxy is the only way out)
After=network-pre.target
[Service]
Type=oneshot
RemainAfterExit=yes
ExecStartPre=-/usr/sbin/nft delete table inet fy_wall
ExecStartPre=-/usr/sbin/nft delete table ip6 fy_wall6
ExecStart=/usr/sbin/nft -f /etc/fy-wall/wall.nft
ExecStop=-/usr/sbin/nft delete table inet fy_wall
ExecStop=-/usr/sbin/nft delete table ip6 fy_wall6
[Install]
WantedBy=multi-user.target
EOF

    # Proxy env for the VM user's own session + user services. This is what lets podman PULLS and
    # image-build RUN steps (both egress as the walled uid) out through the Mac proxy — podman
    # propagates the service's proxy env into containers/builds by default. Uses the MAIN proxy
    # port: VM-level operations aren't per-worktree (each box still gets its own FY_PROXY).
    #  - environment.d → the systemd --user manager (the rootless podman service),
    #  - profile.d     → interactive `limactl shell` sessions (diagnosis parity).
    # $GW (the Mac) is in NO_PROXY so a container's DIRECT calls to the Mac-side daemons — e.g. the
    # gcp metadata-emulator hitting GCP_MINTER_URL=http://$GW:<minter> (gcp.py derive_env) — go
    # straight there instead of being tunnelled through the mitmdump proxy, which runs ON the Mac
    # and can't reach $GW (Lima's internal usernet address) from there. The wall already scopes
    # $GW to the daemon ports, so direct egress to it is safe. Using $GW as the PROXY is unaffected
    # (NO_PROXY filters request destinations, not the proxy connection itself).
    install -d "$WALL_HOME/.config/environment.d"
    cat >"$WALL_HOME/.config/environment.d/90-fy-wall-proxy.conf" <<EOF
HTTP_PROXY=$PROXY_URL
HTTPS_PROXY=$PROXY_URL
http_proxy=$PROXY_URL
https_proxy=$PROXY_URL
NO_PROXY=localhost,127.0.0.1,$GW
no_proxy=localhost,127.0.0.1,$GW
EOF
    chown -R "$WALL_UID" "$WALL_HOME/.config/environment.d"
    cat >/etc/profile.d/fy-wall-proxy.sh <<EOF
export HTTP_PROXY=$PROXY_URL HTTPS_PROXY=$PROXY_URL
export http_proxy=$PROXY_URL https_proxy=$PROXY_URL
export NO_PROXY=localhost,127.0.0.1,$GW no_proxy=localhost,127.0.0.1,$GW
EOF
    # Nudge the user manager to re-read environment.d; restart the rootless podman API socket so
    # in-flight pulls pick the env up. Best-effort: a stopped user manager just reads it on boot.
    sudo -u "#$WALL_UID" XDG_RUNTIME_DIR="/run/user/$WALL_UID" \
        systemctl --user daemon-reexec 2>/dev/null || true
    sudo -u "#$WALL_UID" XDG_RUNTIME_DIR="/run/user/$WALL_UID" \
        systemctl --user try-restart podman.service podman.socket 2>/dev/null || true

    # Recourse: "why did X mysteriously fail?" — the denies, with dest IPs, from dmesg.
    cat >/usr/local/bin/fy-wall-denied <<'EOF'
#!/bin/bash
echo "== fy-wall REJECTs (direct egress that ignored the proxy; dest IPs) =="
dmesg 2>/dev/null | grep -E "fy-wall-deny" | tail -25 || echo "  (none — needs nf_log_syslog)"
echo
echo "Cooperative clients (curl, uv, git, …) go out via the proxy env and are allowlisted on"
echo "the MAC (fy allow / the Network Log TUI) — blocked HOSTNAMES show there, not here."
EOF
    chmod 0755 /usr/local/bin/fy-wall-denied

    systemctl daemon-reload
    systemctl enable fy-wall.service >/dev/null 2>&1
    systemctl restart fy-wall.service
    echo "✓ fy-wall installed: uid $WALL_UID${SUB:+ +subuid$SUB} default-deny; open: lo, local-DNS, $GW tcp {$PORTS}"
    ;;

uninstall)
    systemctl disable --now fy-wall.service 2>/dev/null || true
    rm -f /etc/systemd/system/fy-wall.service
    systemctl daemon-reload
    nft delete table inet fy_wall 2>/dev/null || true
    nft delete table ip6 fy_wall6 2>/dev/null || true
    rm -rf /etc/fy-wall /etc/profile.d/fy-wall-proxy.sh /usr/local/bin/fy-wall-denied
    for home in $(getent passwd | awk -F: '$3 >= 1000 {print $6}'); do
        rm -f "$home/.config/environment.d/90-fy-wall-proxy.conf"
    done
    # podman.socket stays masked — foldyard never needs the rootful socket, and unmasking it
    # silently on uninstall would reopen the container-root→VM-root hole. `systemctl unmask
    # podman.socket` by hand if you truly want it back.
    echo "✓ fy-wall removed (rootful podman.socket left masked — see comment in this script)"
    ;;

status)
    # systemctl is-enabled/is-active print their state text ("disabled", "inactive", "masked", …)
    # on stdout even when exiting non-zero, so an inline `|| echo fallback` would DOUBLE the value.
    # Capture once; fall back only when the capture is empty (systemctl absent / unknown unit).
    enabled="$(systemctl is-enabled fy-wall.service 2>/dev/null || true)"
    active="$(systemctl is-active fy-wall.service 2>/dev/null || true)"
    echo "unit:     enabled=${enabled:-absent} active=${active:-inactive}"
    if nft list table inet fy_wall >/dev/null 2>&1; then
        sku=$(nft list table inet fy_wall | grep -o 'skuid != [{][^}]*[}]' | head -1)
        echo "nft:      table inet fy_wall PRESENT (walled: ${sku:-?})"
    else
        echo "nft:      table inet fy_wall ABSENT — the wall is NOT enforcing"
    fi
    rootful="$(systemctl is-enabled podman.socket 2>/dev/null || true)"
    echo "rootful:  podman.socket ${rootful:-absent}"
    echo "recent denies (dmesg):"
    dmesg 2>/dev/null | grep -E "fy-wall-deny" | tail -5 || true
    ;;

*)
    echo "✗ unknown command '$CMD' — install|uninstall|status" >&2
    exit 1
    ;;
esac
