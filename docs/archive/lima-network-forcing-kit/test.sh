#!/usr/bin/env bash
# Runs ON THE MAC. Drives the guest via `limactl shell` to prove the forcing thesis.
#   bash test.sh [instance-name]   (default: wall)
set -uo pipefail
NAME="${1:-wall}"
PX="http://127.0.0.1:8080"
pass=0; fail=0
ck(){ if eval "$2"; then echo "  PASS: $1"; pass=$((pass+1)); else echo "  FAIL: $1"; fail=$((fail+1)); fi; }

root(){ limactl shell "$NAME" sudo bash -lc "$1"; }
# cooperative agent: uses the proxy.   raw agent: ignores it (the bypass attempt).
coop(){ limactl shell "$NAME" sudo -iu agent env https_proxy="$PX" http_proxy="$PX" \
          curl -s -m15 -o /dev/null -w '%{http_code}' "$1" 2>/dev/null | tr -dc '0-9'; }
raw(){  limactl shell "$NAME" sudo -iu agent curl --noproxy '*' -s -m8 -o /dev/null -w '%{http_code}' "$1" 2>/dev/null | tr -dc '0-9'; }
logcount(){ root "grep -c -- '$1' /var/log/transproxy.log 2>/dev/null" 2>/dev/null | tr -dc '0-9'; }

echo "== (a) agent reaches allowlisted host THROUGH the proxy =="
root "echo filtered > /etc/wall/mode" >/dev/null
code=$(coop https://example.com); logged=$(logcount 'ALLOW example.com')
ck "cooperative agent reaches example.com via proxy + proxy logged it (http=${code:-000} logged=${logged:-0})" \
   "[ \"${code:-000}\" = 200 ] && [ \"${logged:-0}\" -ge 1 ]"

echo "== (b) non-allowlisted host BLOCKED by the proxy (filtered) =="
code=$(coop https://example.org); blk=$(logcount 'BLOCK example.org')
ck "agent blocked from example.org (http=${code:-000} blocked=${blk:-0})" \
   "[ \"${code:-000}\" != 200 ] && [ \"${blk:-0}\" -ge 1 ]"

echo "== (c) agent canNOT bypass by ignoring the proxy (fail-closed default-deny) =="
code=$(raw https://example.com)
ck "agent direct (no proxy) to example.com is dropped (http=${code:-000})" "[ \"${code:-000}\" != 200 ]"

echo "== (d) the wall is OUT OF THE AGENT'S REACH =="
out=$(limactl shell "$NAME" sudo -iu agent bash -lc 'nft flush ruleset 2>&1; sudo -n true 2>&1' 2>/dev/null)
ck "agent cannot flush nft and has no sudo" \
   "echo \"$out\" | grep -qiE 'not permitted|permission denied|not in the sudoers|password is required'"

echo "== (e) optional: rootless CONTAINER egress is caught by the same wall =="
if limactl shell "$NAME" sudo -iu agent podman image exists docker.io/library/alpine:latest 2>/dev/null; then
  # --http-proxy=false: don't inherit the agent's proxy env, so this tests the WALL (direct egress
  # dropped), not the proxy. wget then tries example.org directly and the wall drops it.
  cout=$(limactl shell "$NAME" sudo -iu agent bash -lc "podman run --rm --http-proxy=false docker.io/library/alpine:latest sh -c 'wget -T8 -q -O /dev/null http://example.org; echo rc=\$?' 2>&1")
  rc=$(echo "$cout" | grep -oE 'rc=[0-9]+' | tail -1 | tr -dc '0-9')
  ck "agent's container is blocked from non-allowlisted host (rc=${rc:-?})" "[ -n \"${rc:-}\" ] && [ \"$rc\" != 0 ]"
  [ -z "${rc:-}" ] && echo "    (podman output: $(echo "$cout" | tr '\n' ' ' | tail -c 200))"
else
  echo "  SKIP: alpine not pre-pulled (see install WARN)"
fi

echo "== (f) RUNTIME toggle filtered <-> passthrough (no restart, no recreate) =="
root "echo passthrough > /etc/wall/mode" >/dev/null
code=$(coop https://example.org); pt=$(logcount 'ALLOW example.org:.*passthrough')
ck "example.org now PASSES via the proxy in passthrough (http=${code:-000} logged=${pt:-0})" \
   "[ \"${code:-000}\" = 200 ] && [ \"${pt:-0}\" -ge 1 ]"
root "echo filtered > /etc/wall/mode" >/dev/null
code=$(coop https://example.org)
ck "flipping back to filtered blocks it again instantly (http=${code:-000})" "[ \"${code:-000}\" != 200 ]"

echo "== (g) rootless --network host does NOT escape (still egresses as the agent uid) =="
rc=$(limactl shell "$NAME" sudo -iu agent bash -lc "podman run --rm --network host --http-proxy=false docker.io/library/alpine:latest sh -c 'wget -T8 -q -O /dev/null http://example.org; echo rc=\$?' 2>&1" | grep -oE 'rc=[0-9]+' | tail -1 | tr -dc '0-9')
ck "agent --network host container is still walled (blocked from example.org, rc=${rc:-?})" \
   "[ -n \"${rc:-}\" ] && [ \"$rc\" != 0 ]"

echo "== (h) foundation: agent is rootless (no VM-root path), no rootful socket, no sudo =="
rootless=$(limactl shell "$NAME" sudo -iu agent bash -lc "podman info --format '{{.Host.Security.Rootless}}'" 2>/dev/null | tr -d '[:space:]')
sock=$(limactl shell "$NAME" sudo -iu agent bash -lc 'test -S /run/podman/podman.sock && echo PRESENT || echo ABSENT' 2>/dev/null | tr -dc 'A-Z')
sudoout=$(limactl shell "$NAME" sudo -iu agent bash -lc 'sudo -n true 2>&1 || echo NOSUDO' 2>/dev/null)
ck "rootless=$rootless rootful-socket=$sock no-sudo=$(echo "$sudoout" | grep -o NOSUDO)" \
   "[ \"$rootless\" = true ] && [ \"$sock\" = ABSENT ] && echo \"$sudoout\" | grep -q NOSUDO"

echo
echo "RESULT: $pass passed, $fail failed"
[ "$fail" = 0 ]
