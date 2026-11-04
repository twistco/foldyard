#!/usr/bin/env bash
# Runs in the L1 as the unprivileged tester, from `just nested-test` (tests/tools/nested.py):
# rebuilds the commit from ~/nested/foldyard.pack and runs the host tier the way `lima-host-e2e`
# does in .github/workflows/foldyard-e2e.yml. Each run starts from no VMs and no state.
#
#   run-tier.sh <commit> [tests/test_*_e2e.py …]   # no modules: CI's shards, in CI's order
set -uo pipefail
sha="$1"
shift

N="$HOME/nested"
src="$N/src"
export PATH="/usr/local/bin:/usr/bin:/bin" # nothing a run can write: its home outlives it
export XDG_RUNTIME_DIR="/run/user/$(id -u)"
export DBUS_SESSION_BUS_ADDRESS="unix:path=$XDG_RUNTIME_DIR/bus"
export FOLDYARD_E2E=1
export FOLDYARD_ENGINE=podman
export MACHINE_BACKEND=lima
export MACHINE_CPUS=2
export FOLDYARD_STATE_DIR="$N/state"
export FY_BASE_VM_NAME=fy-ci-base
export FY_BASE_VM_STAMP="$N/fy-ci-base-vm.json"
export UV_PROJECT_ENVIRONMENT="$N/venv"
unset FY_E2E_MODULES CONTAINER_HOST

say() { echo "▶ [$(date +%H:%M:%S)] $*"; }
stop_vms() {
  local v
  for v in $(limactl list -q 2>/dev/null); do
    limactl stop "$v" >/dev/null 2>&1
  done
}

say "clearing the last run"
stop_vms
for v in $(limactl list -q 2>/dev/null); do
  limactl delete -f "$v" >/dev/null 2>&1
done
rm -rf "$src" "$N/state" "$N/fyex-machine" "$N/out" "$HOME/fy-e2e" "$HOME/.foldyard" "$FY_BASE_VM_STAMP"
mkdir -p "$N/out" "$N/tmp"

say "rebuilding ${sha:0:12}"
git init -q "$src" || exit 1
git -C "$src" index-pack --stdin <"$N/foldyard.pack" >/dev/null || exit 1
git -C "$src" update-ref refs/heads/nested "$sha" || exit 1
git -C "$src" checkout -q nested || exit 1
git -C "$src" log --oneline -1

say "baking the session's base VM"
bake() { (cd "$N/tmp" && uv run --project "$src" foldyard machine base bake); }
if ! timeout --kill-after=30 360 bash -c "$(declare -f bake); N=$N src=$src bake"; then
  say "the bake failed or passed 6 min (a stalled mirror, or an L2 that won't boot); retrying once"
  limactl delete -f "$FY_BASE_VM_NAME" >/dev/null 2>&1
  bake || {
    tail -n 5 "$HOME/.lima/$FY_BASE_VM_NAME/serial.log" 2>/dev/null
    exit 1
  }
fi

say "creating the machine"
cp -r "$src/example" "$N/fyex-machine"
(
  cd "$N/fyex-machine" || exit 1
  git init -q && git add -A
  git -c user.email=e2e@foldyard -c user.name=e2e commit -qm init
  uv run --project "$src" foldyard config adopt &&
    uv run --project "$src" foldyard machine ensure
) || exit 1
export CONTAINER_HOST="unix://$HOME/.lima/foldyard-example/sock/podman.sock"

cd "$src" || exit 1
if [ "$#" -gt 0 ]; then
  groups="selected $*"
else
  groups="$(python3 - <<'PY'
import re
t = open(".github/workflows/foldyard-e2e.yml").read()
job = t[t.index("lima-host-e2e:") : t.index("wsl2-host-e2e:")]
for name, body in re.findall(r"- name: (L\d)\s+modules:\n((?:\s+- tests/\S+\n)+)", job):
    print(name, " ".join(re.findall(r"tests/\S+", body)))
PY
)" || {
    say "couldn't read CI's shards from foldyard-e2e.yml"
    exit 1
  }
  # A bare `pytest` below would run the whole unit suite and look like a passing host tier.
  [ -n "$groups" ] || {
    say "found no shards in foldyard-e2e.yml's lima-host-e2e"
    exit 1
  }
fi

rc=0
while read -r group modules; do
  say "$group: $modules"
  export FY_E2E_MODULES="$modules"
  # shellcheck disable=SC2086 # split on purpose, as in CI
  timeout 3000 uv run --group e2e pytest -v --junitxml="$N/out/$group.xml" $modules </dev/null
  status=$?
  [ "$status" -eq 0 ] || rc=$status
  python3 tests/tools/e2e_timings.py "$N/out/$group.xml" --title "$group" 2>/dev/null
  say "$group: exit $status"
done <<<"$groups"

say "stopping the L2s"
stop_vms
say "done: exit $rc"
exit "$rc"
