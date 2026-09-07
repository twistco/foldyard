#!/usr/bin/env bash
# Container-egress transparent wall — runs on the Mac. Uses its OWN Lima VM ('wallt') so it
# doesn't clash with the explicit kit's 'wall' VM.
#   bash run-cwall.sh            # start (or reuse) + install + test
#   bash run-cwall.sh teardown   # graceful stop + delete
set -euo pipefail
cd "$(dirname "$0")"
NAME=wallt

if [ "${1:-}" = "teardown" ]; then
  limactl stop "$NAME" 2>/dev/null || true
  limactl delete "$NAME" 2>/dev/null || true
  echo "torn down."
  exit 0
fi

command -v limactl >/dev/null || { echo "limactl not found — 'brew install lima'"; exit 1; }

if limactl list -q 2>/dev/null | grep -qx "$NAME"; then
  echo "instance '$NAME' exists — reusing it"
  state=$(limactl list --format '{{.Status}}' "$NAME" 2>/dev/null || echo "")
  [ "$state" = Running ] || limactl start "$NAME"
else
  limactl start --name="$NAME" --tty=false ../wall.yaml    # reuse the parent kit's Lima config
fi

limactl copy ./tproxy.py        "$NAME:/tmp/tproxy.py"
limactl copy ./install-cwall.sh "$NAME:/tmp/install-cwall.sh"
limactl shell "$NAME" sudo bash /tmp/install-cwall.sh
echo
bash ./test-cwall.sh "$NAME"
echo
echo "Toggle:   limactl shell $NAME sudo wall-mode passthrough   (or: filtered)"
echo "Recourse: limactl shell $NAME sudo wall-denied"
echo "Teardown: bash run-cwall.sh teardown"
