#!/usr/bin/env bash
# Runs ON THE MAC. Proves the container-egress wall: every container on the walled bridge is
# transparently forced through the proxy, and a SIBLING box spawned via the podman socket is
# contained the same way. Assertions key on the proxy's ALLOW/BLOCK log (deterministic).
#   bash test-cwall.sh [instance-name]   (default: wallt)
set -uo pipefail
NAME="${1:-wallt}"
pass=0; fail=0
ck(){ if eval "$2"; then echo "  PASS: $1"; pass=$((pass+1)); else echo "  FAIL: $1"; fail=$((fail+1)); fi; }
root(){ limactl shell "$NAME" sudo bash -lc "$1"; }
# one-shot container on the walled bridge, NO proxy env (transparent)
crun(){ limactl shell "$NAME" sudo podman run --rm --network wallnet docker.io/library/alpine:latest sh -c "$1" >/dev/null 2>&1; }
logcount(){ root "grep -c -- '$1' /var/log/transproxy.log 2>/dev/null" 2>/dev/null | tr -dc '0-9'; }

root "echo filtered > /etc/wall/mode" >/dev/null

echo "== (a) container (NO proxy env) is transparently forced through the proxy =="
a0=$(logcount 'ALLOW example.com')
crun "wget -T10 -q -O /dev/null http://example.com; true"
a=$(logcount 'ALLOW example.com')
ck "container egress to allowlisted host hit the proxy (ALLOW logged ${a0:-0}->${a:-0})" "[ ${a:-0} -gt ${a0:-0} ]"

echo "== (b) container blocked from a non-allowlisted host =="
b0=$(logcount 'BLOCK example.org')
crun "wget -T8 -q -O /dev/null http://example.org; true"
b=$(logcount 'BLOCK example.org')
ck "container blocked from example.org (BLOCK logged ${b0:-0}->${b:-0})" "[ ${b:-0} -gt ${b0:-0} ]"

echo "== (c) a SIBLING box spawned via the podman socket is ALSO contained =="
bb0=$(logcount 'BLOCK example.org')
limactl shell "$NAME" sudo podman run --rm --network wallnet \
  -v /run/podman/podman.sock:/run/podman/podman.sock quay.io/podman/stable \
  sh -c "podman --url unix:///run/podman/podman.sock run --rm --network wallnet docker.io/library/alpine:latest sh -c 'wget -T8 -q -O /dev/null http://example.org; true'" >/dev/null 2>&1
bb=$(logcount 'BLOCK example.org')
ck "sibling box's egress was caught by the same wall (BLOCK logged ${bb0:-0}->${bb:-0})" "[ ${bb:-0} -gt ${bb0:-0} ]"

echo "== (d) no bypass via a non-80/443 port =="
rc=$(limactl shell "$NAME" sudo podman run --rm --network wallnet docker.io/library/alpine:latest \
       sh -c "wget -T6 -q -O /dev/null http://example.com:81; echo rc=\$?" 2>&1 | grep -oE 'rc=[0-9]+' | tail -1 | tr -dc '0-9')
ck "container egress on a non-redirected port is dropped (rc=${rc:-?})" "[ -n \"${rc:-}\" ] && [ \"$rc\" != 0 ]"

echo "== (e) runtime toggle filtered <-> passthrough =="
root "echo passthrough > /etc/wall/mode" >/dev/null
p0=$(logcount 'ALLOW example.org')
crun "wget -T8 -q -O /dev/null http://example.org; true"
p=$(logcount 'ALLOW example.org')
ck "example.org now ALLOWed in passthrough (ALLOW logged ${p0:-0}->${p:-0})" "[ ${p:-0} -gt ${p0:-0} ]"
root "echo filtered > /etc/wall/mode" >/dev/null

echo
echo "RESULT: $pass passed, $fail failed"
[ "$fail" = 0 ]
