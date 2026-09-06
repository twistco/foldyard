#!/usr/bin/env bash
# test_network.sh — a HOST-RUN (Mac) test + diagnostics harness for the locked-down lima+wall
# posture. Run it after `fy up` from this dir:  bash test_network.sh
#
# It goes deeper than `fy doctor` (which checks host-side daemons): it exercises the actual
# egress paths that only exist once the VM + wall + box are live — the hops that have never run
# on real hardware and are the ones most likely to need tuning. Each check prints PASS/FAIL/… and
# WHY it matters, so a paste-back of the output is a full diagnosis. Read-only + non-destructive.
#
# Layout: A) prerequisites  B) VM→Mac routing  C) the wall (fail-closed)  D) the box egress paths
#         E) the no_proxy stack caveat. Nothing here mutates state; safe to re-run.
set -uo pipefail

PROJECT="$(sed -n 's/^name = "\(.*\)"/\1/p' foldyard.toml | head -1)"   # [project].name comes first
PROJECT="${PROJECT:-fy-wall-example}"
MACHINE="$PROJECT"                                 # [machine].name matches [project].name here
GW="192.168.5.2"                                   # config.LIMA_HOST_GATEWAY
# The Mac proxy port: FY_PROXY_PORT env wins, else this project's allocated band base from the
# cross-project registry (foldyard's ports.py) — must match what the wall was provisioned with.
PROXY_PORT="${FY_PROXY_PORT:-$(python3 -c "import json,os;print(json.load(open(os.path.expanduser('~/.foldyard/ports.json')))['$PROJECT'])" 2>/dev/null || echo 41000)}"

pass() { printf '  \033[32m✓ PASS\033[0m  %s\n' "$1"; }
fail() { printf '  \033[31m✗ FAIL\033[0m  %s\n' "$1"; FAILED=$((FAILED + 1)); }
info() { printf '  \033[2m·\033[0m %s\n' "$1"; }
hdr()  { printf '\n\033[1m▶ %s\033[0m\n' "$1"; }
FAILED=0

# limactl shell as the DEFAULT user (the walled uid); -q keeps lima's own chatter out.
vm()     { limactl shell -q "$MACHINE" -- "$@" 2>/dev/null; }
# a command inside the dev box (over the VM's rootless podman socket). The box's real name is
# resolved once below (foldyard suffixes worktrees); falls back to a name match.
boxexec() { vm podman exec "$BOX_NAME" "$@" 2>/dev/null; }

# ── A. prerequisites ──────────────────────────────────────────────────────────────────
hdr "A. prerequisites"
if ! command -v limactl >/dev/null 2>&1; then
    fail "limactl not found — install Lima: brew install lima"; echo; exit 1
fi
pass "limactl present"
if [ "$(limactl list --format '{{.Status}}' "$MACHINE" 2>/dev/null)" = "Running" ]; then
    pass "lima machine '$MACHINE' is running"
else
    fail "lima machine '$MACHINE' is NOT running — run \`fy up\` first"; echo; exit 1
fi
# Resolve the actual dev-box container name (may be absent if you haven't run `fy box up`).
BOX_NAME="$(vm podman ps --format '{{.Names}}' | grep -E 'devbox' | head -1)"
if [ -n "$BOX_NAME" ]; then
    pass "dev box container: $BOX_NAME"
else
    info "no dev box running (\`fy box up\` to start one) — box checks (D) will be skipped"
fi

# ── B. VM → Mac routing (the load-bearing hop; config.host_alias) ───────────────────────
hdr "B. VM → Mac proxy routing (Lima host gateway $GW)"
# B1: from the VM user directly (usernet host-forward). This is what proxy env on the podman
#     service relies on for pulls/builds.
if vm curl -sS --max-time 8 -x "http://$GW:$PROXY_PORT" -o /dev/null -w '%{http_code}' \
        https://github.com | grep -qE '^(200|301|302)'; then
    pass "VM user reaches the Mac proxy at $GW:$PROXY_PORT (github.com via CONNECT)"
else
    fail "VM user CANNOT reach the Mac proxy at $GW:$PROXY_PORT — is \`fy host\`/\`fy up\` running,"
    info "     and is github.com allowlisted? See: fy tui (Network Log) / fy doctor"
fi
# B2: from INSIDE a rootless container in the VM (container → pasta → VM → usernet → Mac). This
#     is the genuinely-unproven hop; a bare TCP connect to the proxy port is enough to prove it.
if vm podman run --rm docker.io/library/alpine:latest \
        sh -c "nc -z -w5 $GW $PROXY_PORT" >/dev/null 2>&1; then
    pass "a rootless CONTAINER reaches $GW:$PROXY_PORT (pasta → usernet → Mac)"
else
    fail "a container CANNOT reach $GW:$PROXY_PORT — the box's egress won't work."
    info "     Try FY_HOST_ALIAS to override the address, or check Lima's usernet."
fi

# ── C. the wall (fail-closed enforcement) ───────────────────────────────────────────────
hdr "C. the in-VM egress wall (nftables default-deny)"
if vm sudo nft list table inet fy_wall >/dev/null 2>&1; then
    UID_RULE="$(vm sudo nft list table inet fy_wall | grep -o 'skuid != [{][^}]*[}]' | head -1)"
    pass "nft table inet fy_wall present (walled ${UID_RULE:-uid ?})"
    # The walled set must cover the SUBUID range, not just the bare uid — else a --network=host
    # container as a non-root container user egresses with a subuid and sails past (see C4).
    if echo "$UID_RULE" | grep -q -- '-'; then
        pass "  walled set includes the container subuid range (host-network bypass closed)"
    else
        info "  walled set is uid-only (no /etc/subuid entry?) — C4 checks the host-network path"
    fi
else
    fail "nft table inet fy_wall ABSENT — the wall is NOT enforcing. Is [machine].wall = true?"
    info "     It self-heals on machine start: fy down && fy up (or limactl stop/start $MACHINE)"
fi
# C1: the VM user's DIRECT (proxy-ignoring) egress must be REJECTED — the fail-closed property.
if vm curl -sS --max-time 6 --noproxy '*' -o /dev/null https://1.1.1.1 2>/dev/null; then
    fail "DIRECT egress from the VM user SUCCEEDED — the wall is not fail-closed!"
else
    pass "direct (proxy-ignoring) egress from the VM user is refused"
fi
# C2: a raw TCP stream to a PUBLIC IP on port 53 must be refused — the wall allows :53 only to
# LOCAL resolvers, so a public-IP :53 connect is an exfil tunnel, not DNS (the port-53 hole).
if vm bash -c 'exec 3<>/dev/tcp/1.1.1.1/53' 2>/dev/null; then
    fail "TCP to 1.1.1.1:53 CONNECTED — port-53 exfil tunnel is OPEN (wall too permissive)"
else
    pass "raw TCP to a public IP:53 refused (only local-resolver DNS is allowed)"
fi
# C3: local-resolver DNS must still WORK (the restriction mustn't break name resolution).
if vm getent hosts github.com >/dev/null 2>&1; then
    pass "DNS resolution via the local resolver still works"
else
    info "DNS resolution failed — if the VM uses a PUBLIC resolver, widen the wall's :53 rule"
fi
# C4: the subuid bypass — a rootless --network=host container as a non-root container user must
# STILL be caught by the wall (it egresses with a subuid, no pasta NAT). Only meaningful if an
# image is already present locally (the wall blocks pulling one), so skip cleanly otherwise.
C4_IMG="$(vm podman images -q 2>/dev/null | head -1)"
if [ -n "$C4_IMG" ]; then
    if vm podman run --rm --network=host --user 1000 "$C4_IMG" \
            sh -c 'wget -q -T 6 -O /dev/null https://1.1.1.1 || curl -sS --max-time 6 -o /dev/null https://1.1.1.1' 2>/dev/null; then
        fail "host-network container (subuid) reached the internet DIRECT — subuid bypass OPEN!"
    else
        pass "host-network container egress is caught by the wall (subuid range covered)"
    fi
else
    info "C4 (subuid/host-network bypass) skipped — no local image to run; see nft set in C above"
fi
info "recourse if a legit host is blocked:  limactl shell $MACHINE sudo fy-wall-denied"

# ── D. the dev box egress paths ─────────────────────────────────────────────────────────
if [ -n "$BOX_NAME" ]; then
    hdr "D. dev box egress"
    # D1: box direct egress refused (fail-closed, from the box's own network namespace).
    if boxexec curl -sS --max-time 6 --noproxy '*' -o /dev/null https://1.1.1.1; then
        fail "the BOX made a direct (proxy-ignoring) request — wall breach!"
    else
        pass "box direct egress refused (matches \`fy verify\`'s wall check)"
    fi
    # D2: box egress THROUGH the proxy to an allowlisted host works.
    if boxexec curl -sS --max-time 10 -o /dev/null -w '%{http_code}' https://github.com \
            | grep -qE '^(200|301|302)'; then
        pass "box egress to github.com works (via the proxy, allowlisted)"
    else
        fail "box egress to github.com FAILED — check the proxy CA + allowlist"
    fi
    # D3: a NON-allowlisted host is blocked (default_deny in action).
    CODE="$(boxexec curl -sS --max-time 10 -o /dev/null -w '%{http_code}' https://example.com)"
    if [ "$CODE" = "403" ]; then
        pass "non-allowlisted example.com is blocked (403 at the proxy) — enforced allowlist works"
    else
        info "example.com returned '$CODE' (expected 403 under default_deny — allowlisted already?)"
    fi
else
    hdr "D. dev box egress — SKIPPED (no box; run \`fy box up\`)"
fi

# ── E. the no_proxy stack caveat ────────────────────────────────────────────────────────
hdr "E. stack no_proxy caveat (worker → api by service name)"
if vm podman ps --format '{{.Names}}' | grep -qi worker; then
    WNAME="$(vm podman ps --format '{{.Names}}' | grep -i worker | head -1)"
    if vm podman logs --tail 8 "$WNAME" 2>/dev/null | grep -q "reached api"; then
        pass "worker reaches api intra-stack (no_proxy bypass working)"
    elif vm podman logs --tail 8 "$WNAME" 2>/dev/null | grep -q "api call FAILED"; then
        fail "worker's api call FAILED — the no_proxy caveat: 'api' is routing via the proxy."
        info "     Fix: no_proxy must list in-stack service names (see compose.yml's worker)."
    else
        info "worker running ($WNAME) but no verdict yet — give it a few seconds and re-run"
    fi
else
    info "worker service not up (\`fy up\` brings the stack) — caveat check skipped"
fi

# ── summary ─────────────────────────────────────────────────────────────────────────────
echo
if [ "$FAILED" -eq 0 ]; then
    printf '\033[32m✓ all network checks passed — the locked-down posture is working end to end.\033[0m\n'
else
    printf '\033[31m✗ %s check(s) FAILED — paste this whole output back for diagnosis.\033[0m\n' "$FAILED"
fi
exit "$FAILED"
