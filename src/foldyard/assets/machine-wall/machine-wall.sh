#!/bin/bash
# fy machine wall — the nftables egress wall for the REAL lima machine VM.
#
# Runs INSIDE the guest as root, at BOOT: foldyard's boot provisioning (`guest-boot.sh`,
# recorded in the instance's lima.yaml; Lima runs it as root on every boot) installs this file
# root-owned at /usr/local/libexec/fy-machine-wall from its own embedded copy — never from the
# repo mount, never via sudo from the VM user, who has no sudo (the same boot script narrows
# Lima's grant to `shutdown` only). NOT the standalone proof rig — that lives at
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
# tunnels straight past it. The moat: the user has NO path to VM-root — Lima's passwordless
# sudo grant is narrowed to `shutdown` at every boot — so the wall is enforcement against the
# box AND against a container escape that lands as the VM user; only a guest-KERNEL exploit
# reaches VM-root. The security argument is ADR-0009: no in-VM wall holds against VM-root; the
# backstop is the host-side chokepoint, which holds no less for it.
#
# Usage (as root in the guest; the walled uid comes from FY_WALL_UID, which the boot script
# sets from Lima's own record of the user — SUDO_UID is the fallback for a manual run):
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

# The guest's OWN trust store: what the VM user's podman verifies image PULLS against (Go reads
# the system bundle), so a registry host the proxy decrypts no longer fails "unknown authority".
# With the CA it goes in; without one (no CA yet, or the wall off) ours comes out. One fixed file
# name, so a rotated CA replaces the old one rather than joining it. Fedora (Lima's podman
# template) has update-ca-trust; a Debian-family guest, update-ca-certificates.
_guest_trust() {
    local ca="${1:-}" anchor update
    if command -v update-ca-trust >/dev/null; then
        anchor=/etc/pki/ca-trust/source/anchors/fy-proxy-ca.pem
        update=(update-ca-trust extract)
    elif command -v update-ca-certificates >/dev/null; then
        anchor=/usr/local/share/ca-certificates/fy-proxy-ca.crt
        update=(update-ca-certificates --fresh)
    else
        if [ -n "$ca" ]; then
            echo "⚠ proxy CA: no update-ca-trust in this guest; its podman's pulls won't trust it" >&2
        fi
        return 0
    fi
    rm -f "$anchor" || return 1
    if [ -n "$ca" ]; then
        install -m 0644 "$ca" "$anchor" || return 1
    fi
    "${update[@]}"
}

# Build RUN steps: podman applies the CA drop-in's MOUNTS to a build's RUN step but not its env,
# and a build has no other way in that isn't a line in the consumer's Dockerfile or baked into the
# image. So crun is fronted by a wrapper: a drop-in points podman's `crun` at it (the NAME stays
# `crun`, so podman's per-runtime behaviour is unchanged, and the real crun is the fallback), and
# on a create whose spec mounts the CA the helper adds the four variables the spec doesn't already
# set — an image's own ENV, a Dockerfile ENV and a create's explicit env still win, exactly as for
# the drop-in. Every create passes through it (a build's RUN step from the host or the box, a
# docker-compat create); nothing lands in an image, since the spec is the runtime's, not a layer.
# Anything unexpected leaves the spec as the engine wrote it, and crun runs regardless.
_oci_wrapper() {
    local crun
    crun="$(command -v crun || true)"
    if [ -z "$crun" ] || [ ! -x /usr/bin/python3 ]; then
        echo "⚠ proxy CA: no crun or python3 in this guest; build steps won't trust it" >&2
        return 0
    fi
    install -d -m 0755 /usr/local/libexec/fy-oci
    cat >/usr/local/libexec/fy-oci/ca-env.py <<'__FY_CA_ENV__'
"""foldyard (fy-machine-wall): add the proxy CA's env to a container spec that mounts the CA.

Called by the crun wrapper beside it with crun's own argv. Only a create (or run) names a bundle;
anything else, or anything unexpected, leaves the spec untouched."""

import json
import os
import sys

CA = "/etc/fy-proxy-ca.pem"
BUNDLE = "/etc/fy-proxy-ca-combined.pem"
ENV = (
    ("NODE_EXTRA_CA_CERTS", CA),
    ("SSL_CERT_FILE", BUNDLE),
    ("REQUESTS_CA_BUNDLE", BUNDLE),
    ("GIT_SSL_CAINFO", BUNDLE),
)


def bundle_of(argv):
    for i, arg in enumerate(argv):
        if arg in ("--bundle", "-b") and i + 1 < len(argv):
            return argv[i + 1]
        if arg.startswith("--bundle="):
            return arg[len("--bundle=") :]
    return None


def patch(path):
    with open(path) as fh:
        spec = json.load(fh)
    if not any(m.get("destination") == BUNDLE for m in spec.get("mounts") or []):
        return
    process = spec["process"]
    env = process.get("env") or []
    have = set(e.split("=", 1)[0] for e in env)
    add = [name + "=" + value for name, value in ENV if name not in have]
    if not add:
        return
    process["env"] = env + add
    tmp = path + ".fy-tmp"
    with open(tmp, "w") as fh:
        json.dump(spec, fh)
    os.chmod(tmp, os.stat(path).st_mode & 0o7777)
    os.replace(tmp, path)


def main(argv):
    bundle = bundle_of(argv)
    if bundle:
        try:
            patch(os.path.join(bundle, "config.json"))
        except Exception:
            pass  # never in the container's way: the spec stays as the engine wrote it


if __name__ == "__main__":
    main(sys.argv[1:])
__FY_CA_ENV__
    sed "s|__FY_CRUN__|$crun|" >/usr/local/libexec/fy-oci/crun <<'__FY_OCI__'
#!/bin/sh
# foldyard (fy-machine-wall): crun, with the proxy CA's env added to a create whose spec mounts
# the CA (ca-env.py). Only a create names a bundle; every other call goes straight to crun.
case " $* " in
*" --bundle "* | *" --bundle="* | *" -b "*)
    /usr/bin/python3 -I /usr/local/libexec/fy-oci/ca-env.py "$@" || :
    ;;
esac
exec __FY_CRUN__ "$@"
__FY_OCI__
    chmod 0644 /usr/local/libexec/fy-oci/ca-env.py
    chmod 0755 /usr/local/libexec/fy-oci/crun
    cat >/etc/containers/containers.conf.d/91-fy-proxy-ca-runtime.conf.tmp <<EOF
# foldyard: crun behind the proxy-CA env wrapper, for build steps (fy-machine-wall).
[engine.runtimes]
crun = ["/usr/local/libexec/fy-oci/crun", "$crun"]
EOF
    chmod 0644 /etc/containers/containers.conf.d/91-fy-proxy-ca-runtime.conf.tmp
    mv -f /etc/containers/containers.conf.d/91-fy-proxy-ca-runtime.conf.tmp \
        /etc/containers/containers.conf.d/91-fy-proxy-ca-runtime.conf
    echo "✓ proxy CA: build steps trust it too (containers.conf.d/91-fy-proxy-ca-runtime.conf)"
}

case "$CMD" in
install)
    GW="${2:?install needs <host_gateway_ip>}"
    PORTS="${3:?install needs \"<tcp_port_ranges>\" (nft set elements)}"
    PROXY_URL="${4:?install needs <proxy_url>}"
    CA_SRC="${5:-}" # the proxy CA the boot script embedded; absent when none exists yet
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
    # Every directory created here is created OWNED BY THE USER: this runs as root at boot, and
    # a root-owned ~/.config (what a bare `install -d` leaves behind on a fresh image where nothing
    # made it yet) makes every later user-level step fail — Lima's `systemctl --user enable
    # podman.socket` (the API socket never comes up, `limactl start` times out) and the sandbox
    # posture's own drop-ins. Fedora 44 images happened to pre-create the dir; Fedora 45 doesn't.
    WALL_GID="$(id -g "$WALL_UID")"
    for d in "$WALL_HOME/.config" "$WALL_HOME/.config/environment.d"; do
        [ -d "$d" ] || install -d -o "$WALL_UID" -g "$WALL_GID" "$d"
    done
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
    # The proxy CA for every container this podman creates. The proxy env above reaches them all
    # (podman propagates it), and the host proxy decrypts every host not on `passthrough` — so a
    # container that doesn't trust its CA fails TLS on any other host. A system containers.conf
    # drop-in mounts the CA + a combined bundle (the guest's roots + the CA) into each container
    # and sets the env that points the common clients at them; `append` keeps podman's default
    # env. An image's own ENV, and a create's explicit env (the box's), still win over it.
    # Root-owned files, so the VM user — the box's uid — can't swap the trust. Distinct paths
    # from the box's own CA files, which its bootstrap writes. Build RUN steps get the mounts but
    # not the env (podman applies no default env to builds) — _oci_wrapper adds it.
    rm -f /etc/containers/containers.conf.d/90-fy-proxy-ca.conf
    rm -f /etc/containers/containers.conf.d/91-fy-proxy-ca-runtime.conf
    rm -rf /usr/local/libexec/fy-oci
    [ -s "$CA_SRC" ] || CA_SRC=""
    # The guest's own store first: the combined bundle below is cut from its roots, which must no
    # longer hold a rotated-out CA (they hold this one twice after it — harmless).
    _guest_trust "$CA_SRC" || echo "⚠ proxy CA: the guest's own store wasn't updated" >&2
    if [ -n "$CA_SRC" ]; then
        install -m 0644 "$CA_SRC" /etc/fy-wall/proxy-ca.pem
        roots=""
        # Fedora 44 has only the extracted bundle; older Fedora + Debian-family the others.
        for b in /etc/pki/ca-trust/extracted/pem/tls-ca-bundle.pem /etc/pki/tls/certs/ca-bundle.crt \
            /etc/ssl/certs/ca-certificates.crt /etc/ssl/cert.pem; do
            if [ -r "$b" ]; then roots="$b"; break; fi
        done
        cat ${roots:+"$roots"} /etc/fy-wall/proxy-ca.pem >/etc/fy-wall/proxy-ca-combined.pem
        chmod 0644 /etc/fy-wall/proxy-ca-combined.pem
        labelled=1
        # SELinux refuses a container's read of an unlabelled bind source (EACCES inside it).
        if command -v selinuxenabled >/dev/null && selinuxenabled; then
            chcon -t container_file_t /etc/fy-wall/proxy-ca.pem /etc/fy-wall/proxy-ca-combined.pem \
                || labelled=0
        fi
        if [ "$labelled" = 1 ]; then
            install -d -m 0755 /etc/containers/containers.conf.d
            cat >/etc/containers/containers.conf.d/90-fy-proxy-ca.conf.tmp <<'EOF'
# foldyard: the egress proxy's CA in every container of the walled VM (fy-machine-wall).
[containers]
env = [
  "NODE_EXTRA_CA_CERTS=/etc/fy-proxy-ca.pem",
  "SSL_CERT_FILE=/etc/fy-proxy-ca-combined.pem",
  "REQUESTS_CA_BUNDLE=/etc/fy-proxy-ca-combined.pem",
  "GIT_SSL_CAINFO=/etc/fy-proxy-ca-combined.pem",
  {append=true},
]
volumes = [
  "/etc/fy-wall/proxy-ca.pem:/etc/fy-proxy-ca.pem:ro",
  "/etc/fy-wall/proxy-ca-combined.pem:/etc/fy-proxy-ca-combined.pem:ro",
  {append=true},
]
EOF
            chmod 0644 /etc/containers/containers.conf.d/90-fy-proxy-ca.conf.tmp
            mv -f /etc/containers/containers.conf.d/90-fy-proxy-ca.conf.tmp \
                /etc/containers/containers.conf.d/90-fy-proxy-ca.conf
            echo "✓ proxy CA: every container trusts it (containers.conf.d/90-fy-proxy-ca.conf)"
            _oci_wrapper
        else
            echo "⚠ proxy CA: could not label it for containers (chcon); they won't trust it" >&2
        fi
    else
        echo "⚠ proxy CA: none embedded; containers won't trust the proxy" >&2
    fi

    # Nudge the user manager to re-read environment.d; restart the rootless podman API socket so
    # in-flight pulls pick the env up (and the service reads the CA drop-in). Best-effort: a
    # stopped user manager just reads it on boot.
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
    rm -f /etc/containers/containers.conf.d/90-fy-proxy-ca.conf
    rm -f /etc/containers/containers.conf.d/91-fy-proxy-ca-runtime.conf
    rm -rf /usr/local/libexec/fy-oci
    _guest_trust "" || echo "⚠ proxy CA: still in the guest's own store" >&2
    for home in $(getent passwd | awk -F: '$3 >= 1000 {print $6}'); do
        rm -f "$home/.config/environment.d/90-fy-wall-proxy.conf"
    done
    # A rootless podman service already running keeps the proxy env and the CA defaults it
    # started with — the first unwalled boot then still dialled the (stopped) proxy. Restart it
    # as install does. Best-effort, and only for a uid we can name.
    if WALL_UID="$(_wall_user 2>/dev/null)"; then
        sudo -u "#$WALL_UID" XDG_RUNTIME_DIR="/run/user/$WALL_UID" \
            systemctl --user daemon-reexec 2>/dev/null || true
        sudo -u "#$WALL_UID" XDG_RUNTIME_DIR="/run/user/$WALL_UID" \
            systemctl --user try-restart podman.service podman.socket 2>/dev/null || true
    fi
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
