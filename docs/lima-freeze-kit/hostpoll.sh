#!/bin/bash
# Host-side poller: every ~0.5 s, one JSON line with the netstack's TCP counters (from the
# instance's own gvisor-tap-vsock /stats endpoint) and the hostagent's TCP sockets by state.
# hostpoll.sh <instance dir> <out.jsonl> [interval]
dir=$1 out=$2 iv=${3:-0.5}
pid=$(cat "$dir/ha.pid")
ep="$dir/default_ep.sock"
while :; do
  t=$(python3 -c 'import time;print(round(time.time(),3))')
  st=$(curl -s --max-time 2 --unix-socket "$ep" http://lima/stats | python3 -c '
import json,sys
try:
    d=json.load(sys.stdin)["TCP"]
except Exception:
    print("{}"); sys.exit()
print(json.dumps({k:v for k,v in d.items() if v}))')
  socks=$(lsof -nP -a -p "$pid" -iTCP 2>/dev/null | awk 'NR>1{print $NF}' | sort | uniq -c | awk '{printf "\"%s\":%s,", $2, $1}')
  echo "{\"t\":$t,\"stats\":$st,\"socks\":{${socks%,}}}" >> "$out"
  sleep "$iv"
done
