"""Map mount-namespace inodes to podman container ids from KERNEL state, not names a client picks.

Run as root in the guest:  python3 resolve.py <uid>

Walks the Lima user's delegated cgroup subtree,
``/sys/fs/cgroup/user.slice/user-<uid>.slice/user@<uid>.service``, top-down. The OUTERMOST
``libpod-<64 hex>.scope`` on any path owns everything below it (crun's sub-cgroup, whatever it is
named), and each member process's ``/proc/<pid>/ns/mnt`` maps to that scope's id. A scope's name
comes from the id podman generated, which an API client cannot choose; a client CAN name the
sub-cgroup (``run.oci.systemd.subgroup`` — even ``libpod-<another id>.scope``) and the slices
above (``--cgroup-parent=x.slice``), which is why the walk never descends past the first scope and
never reads a name below it. An inode claimed by two scopes is ambiguous, never resolved.
"""

import json
import os
import re
import sys

SCOPE = re.compile(r"^libpod-([0-9a-f]{64})\.scope$")


def scopes(uid: int) -> dict[str, str]:
    """``{scope directory: container id}`` for the outermost podman scope on every path."""
    out = {}
    for root, dirs, _files in os.walk(
        f"/sys/fs/cgroup/user.slice/user-{uid}.slice/user@{uid}.service"
    ):
        for name in list(dirs):
            m = SCOPE.match(name)
            if m:
                out[os.path.join(root, name)] = m.group(1)
                dirs.remove(name)  # never look for scopes BELOW a scope: those names are chosen
    return out


def members(scope_dir: str) -> list[int]:
    pids = []
    for root, _dirs, files in os.walk(scope_dir):
        if "cgroup.procs" in files:
            with open(os.path.join(root, "cgroup.procs")) as f:
                pids += [int(x) for x in f.read().split()]
    return pids


def resolve(uid: int) -> dict[int, list[str]]:
    by_inode: dict[int, set[str]] = {}
    for scope_dir, cid in scopes(uid).items():
        for pid in members(scope_dir):
            try:
                link = os.readlink(f"/proc/{pid}/ns/mnt")
            except OSError:
                continue  # exited between the read and the readlink
            inode = int(link[link.index("[") + 1 : -1])
            by_inode.setdefault(inode, set()).add(cid)
    return {k: sorted(v) for k, v in by_inode.items()}


if __name__ == "__main__":
    print(json.dumps(resolve(int(sys.argv[1])), indent=1))
