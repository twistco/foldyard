#!/usr/bin/env bash
# One-shot: boot a native Lima VM on the Mac, install the wall, run the proof.
#   bash run.sh            # start (or reuse) + install + test  — idempotent
#   bash run.sh teardown   # graceful stop + delete
# Use `bash run.sh` (not ./run.sh) so a missing exec bit (core.fileMode) never bites.
set -euo pipefail
cd "$(dirname "$0")"
NAME=wall

if [ "${1:-}" = "teardown" ]; then
  limactl stop "$NAME" 2>/dev/null || true
  limactl delete "$NAME" 2>/dev/null || true
  echo "torn down."
  exit 0
fi

# --- preflight ---------------------------------------------------------------------------
command -v limactl >/dev/null || { echo "limactl not found — 'brew install lima'"; exit 1; }
if [ "$(uname -s)" = Darwin ]; then
  [ "$(uname -m)" = arm64 ] || echo "WARN: not Apple Silicon — vmType vz needs Apple Silicon."
  major=$(sw_vers -productVersion | cut -d. -f1)
  [ "${major:-0}" -ge 13 ] || echo "WARN: macOS ${major} < 13 — vmType vz needs macOS 13+ (NOT an M3 requirement)."
fi

# --- create or reuse the instance (idempotent) -------------------------------------------
if limactl list -q 2>/dev/null | grep -qx "$NAME"; then
  echo "instance '$NAME' already exists — reusing it"
  state=$(limactl list --format '{{.Status}}' "$NAME" 2>/dev/null || echo "")
  [ "$state" = Running ] || limactl start "$NAME"
else
  limactl start --name="$NAME" --tty=false ./wall.yaml
fi

# --- (re)install the wall (install.sh is idempotent) + run the proof ----------------------
limactl copy ./proxy.py        "$NAME:/tmp/proxy.py"
limactl copy ./install.sh      "$NAME:/tmp/install.sh"
limactl copy ./agent-claude.sh "$NAME:/tmp/agent-claude.sh"   # opt-in red-team helper (not run here)
limactl shell "$NAME" sudo bash /tmp/install.sh
echo
bash ./test.sh "$NAME"
echo
echo "Toggle at runtime:  limactl shell $NAME sudo wall-mode passthrough   (or: filtered)"
echo "Red-team inside:    limactl shell $NAME   then  sudo bash /tmp/agent-claude.sh setup  (see README §Red-team)"
echo "Teardown:           bash run.sh teardown"
