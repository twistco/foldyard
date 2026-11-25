#!/usr/bin/env bash
# The git-guard spike (docs/git-guard-scope.md), start to finish, on a throwaway VM cloned from
# the baked base (`fy machine base bake`), which it only reads. Needs Lima and a stamped
# foldyard-base; touches no project VM.
#
#   docs/git-guard-spike-kit/run.sh [vm-name]     # default fy-spike-gitguard; deleted at the end
#
# Guest root is used freely here: this is a spike VM, not one foldyard provisions.
set -euo pipefail
VM=${1:-fy-spike-gitguard}
KIT=$(cd "$(dirname "$0")" && pwd)
S=$(cd "$(mktemp -d "${TMPDIR:-/tmp}/fy-gitguard.XXXXXX")" && pwd -P)
IMG=localhost/foldyard-base-devbox:latest
cp "$KIT/guard.sh" "$KIT/attack.sh" "$S/" # the guest reads them through the share
sh "$KIT/reset.sh" "$S"

g() { limactl shell "$VM" -- sh -c "$1"; }
show() { sed "s|$S|S|g" | cut -c1-110; }
BOX="podman run --rm --user 0 --security-opt label=disable -v $S:$S --entrypoint sh $IMG"
PRIV="podman run --rm --privileged --user 0 --security-opt label=disable -v $S:$S --entrypoint sh $IMG"
round() { echo "== $1"; g "$2 $S/attack.sh $S" | show; }
config_bound() { g "findmnt -rn -o TARGET $S/repo/.git/config >/dev/null && echo bound || echo 'NOT bound'"; }

# A VM already called that is somebody's: refused, before the trap that deletes ours.
if limactl list -q 2>/dev/null | grep -qx "$VM"; then
    echo "a VM called $VM already exists" >&2
    rm -rf "$S"
    exit 1
fi
trap 'limactl delete -f "$VM" >/dev/null 2>&1 || true; rm -rf "$S"' EXIT
limactl clone foldyard-base "$VM" --tty=false --mount-only "$S:w" --mount-type virtiofs >/dev/null
chmod 700 "$HOME/.lima/$VM/sock" 2>/dev/null || true # Lima 2.1.3's clone leaves it 0000
limactl start "$VM" --tty=false >/dev/null 2>&1
g 'uname -r; podman --version'

round "control: box-shaped container, no guard" "$BOX"
round "control: --privileged sibling, no guard" "$PRIV"
sh "$KIT/reset.sh" "$S" # the sibling's non-recursive bind wrote to .git/config

g "podman run -d --name oldbox --init --user 0 --security-opt label=disable -v $S:$S --entrypoint sleep $IMG infinity >/dev/null"
g "sudo sh $S/guard.sh $S"
echo "== guard applied while podman's namespace already exists (oldbox is running)"
round "A1: new box-shaped container" "$BOX"
round "A2: new --privileged sibling" "$PRIV"
round "A3: oldbox, started before the guard" "podman exec oldbox sh"
echo "== after A3, .git/config is $(config_bound) in the guest's own namespace"

g "podman rm -f oldbox >/dev/null; podman system migrate; sudo sh $S/guard.sh $S config"
g "podman info >/dev/null; echo \"   pause process: \$(pgrep -a catatonit)\"; echo '   guard mounts in the guest:'; findmnt -rn -o TARGET,OPTIONS | grep '$S/' | cut -d, -f1" | show
echo "== podman's namespace made again, after the guard (as a boot-time guard would find it)"
round "B1: new box-shaped container" "$BOX"
round "B2: new --privileged sibling" "$PRIV"
echo "== B3: bind-nonrecursive mount at create"
g "podman run --rm --privileged --security-opt label=disable --mount type=bind,src=$S,dst=$S,bind-nonrecursive=true --entrypoint sh $IMG -c 'echo x >> $S/repo/.git/config && echo LANDED || echo refused' 2>&1 || true" | show
echo "== B4: --pid=host, through /proc/1/root"
g "podman run --rm --privileged --pid=host --security-opt label=disable --entrypoint sh $IMG -c 'echo x >> /proc/1/root$S/repo/.git/config && echo LANDED || echo refused' 2>&1 || true" | show

echo "== C: your computer writes .git/config"
python3 -I -c 'import sys; open(sys.argv[1], "a").write("# in place\n")' "$S/repo/.git/config"
echo "   after an in-place append: $(config_bound)"
git -C "$S/repo" config spike.host 1 # what Fork's push -u does: config.lock, renamed over config
echo "   after git config (a rename): $(config_bound), per findmnt"
round "C1: new box-shaped container" "$BOX"
echo "== C2: a box sets core.hooksPath"
g "$BOX -c 'git -c safe.directory=* -C $S/repo config core.hooksPath /tmp/evil 2>&1 && echo LANDED'" | show || true
echo "   your computer's git reads core.hooksPath=$(git -C "$S/repo" config --get core.hooksPath || echo '(unset)')"
echo "   guard mounts in the guest:"
g "findmnt -rn -o TARGET,OPTIONS | grep '$S/' | cut -d, -f1" | show
