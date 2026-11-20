"""`fy machine upgrade`: the project VM's Fedora moved forward IN PLACE, its podman store kept.

A VM keeps the Fedora it was created with; a newer Lima changes only what new VMs get, and
`fy machine recreate` would throw away the store (images, the stack's volumes, agent logins). This
upgrades the disk instead (docs/guest-os.md, where a clone of a real project VM went from Fedora
44 to 45 in 153 s with every volume, image and container kept):

1. read the guest, as the VM user: its release, free room, and which containers run; refuse
   before anything changes when there's nothing to do, too little room, or no release to go to;
2. stop the containers (a database gets its shutdown, not a power cut), then snapshot what must
   survive (every volume and its size, every image, every container): the store at rest, so a
   database's last checkpoint isn't read as a change; stop the VM, and record a
   one-off root boot script under its own marker (root in the guest is boot-time only:
   machine.py's provisioning section) that runs `dnf distro-sync --releasever=N`;
3. boot it, read the guest's report, stop, drop the script, and start the VM as any start does
   (`machine.ensure`: foldyard's own provisioning, the wall, the checks), on the new kernel;
4. `podman system migrate`, then stop the API service so the socket restarts it in migrate's new
   user namespace; compare with the snapshot, start what ran before.

**The target never passes the Fedora the installed Lima's own podman template uses.** That is the
Lima version check: a release Lima ships in its template is one its guest agent and boot setup are
made for (ADR-0025 saw a newer guest under an older Lima boot degraded), and it follows Lima
forward with no table to keep. It is also the default: the VM is brought to what a new VM gets.
"""

from __future__ import annotations

import re
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

MARKER = "# fy-upgrade "
STATE = "/var/lib/fy-upgrade/state"
# Room the upgrade needs on the guest's root: the downloaded packages and the new ones beside the
# old until the transaction ends (Fedora 44 to 45 was ~1 GB of downloads).
MIN_FREE = 5 * 1024**3
# How long the upgrade boot may take before `limactl start` gives up (its default is 10 min; a
# slow mirror can take longer, and the guest's own report says how it ended either way).
UPGRADE_WAIT = "60m"
# Fedora upgrades up to two releases at a time.
MAX_STEP = 2

# What must survive, as the VM user sees it. One `podman unshare du` for every volume: their files
# belong to the user namespace's uids. Any failure ends it early, and only a listing that reaches
# `end` counts (a failed ssh reads as empty output, which would otherwise match anything).
SNAPSHOT = r"""
set -eo pipefail
. /etc/os-release; echo "release ${ID:-?} ${VERSION_ID:-0}"
echo "free $(df --output=avail -B1 / | tail -1 | tr -d ' ')"
podman volume ls --format '{{.Name}} {{.Mountpoint}}' >/tmp/fy-upgrade-volumes
if [ -s /tmp/fy-upgrade-volumes ]; then
    podman unshare du -sb $(cut -d' ' -f2 /tmp/fy-upgrade-volumes) >/tmp/fy-upgrade-sizes
    while read -r name mp; do
        echo "volume $name $(awk -v p="$mp" '$2 == p {print $1}' /tmp/fy-upgrade-sizes)"
    done </tmp/fy-upgrade-volumes
fi
rm -f /tmp/fy-upgrade-volumes /tmp/fy-upgrade-sizes
podman images -a --no-trunc --format 'image {{.ID}}' | sort -u
podman ps -a --no-trunc --format 'container {{.ID}} {{.State}}'
echo end
"""


def _err(*a: object) -> None:
    print(*a, file=sys.stderr, flush=True)


def _asset() -> Path:
    return Path(__file__).resolve().parent / "assets" / "upgrade" / "guest-upgrade.sh"


def render(release: int) -> str:
    """The guest's boot script for ``release`` (a Lima template: ``{{.User}}`` only)."""
    return _asset().read_text().replace("@@REL@@", str(release))


def template_release(rendered: str) -> int | None:
    """The Fedora release a Lima template's images name (``…/fedora/linux/releases/45/…``)."""
    found = {int(m) for m in re.findall(r"/fedora/linux/releases/(\d+)/", rendered)}
    return max(found) if found else None


def lima_template_release() -> int | None:
    """:func:`template_release` of the installed Lima's podman template, as Lima resolves it."""
    from .machine_backend import LimaBackend

    res = subprocess.run(
        ["limactl", "template", "copy", "--embed-all", LimaBackend.TEMPLATE, "-"],
        capture_output=True,
        text=True,
    )
    return template_release(res.stdout) if res.returncode == 0 else None


def target(guest: tuple[str, int] | None, template: int | None, want: int | None) -> int | str:
    """The release to upgrade to, or why not (a message; "already …" is not a failure)."""
    if guest is None:
        return "can't reach the VM to read its release"
    distro, have = guest
    if distro != "fedora":
        return f"the VM runs {distro} {have}: only a Fedora guest (Lima's podman template) upgrades"
    if template is None:
        return "can't read which Fedora Lima's podman template uses (`limactl template copy`)"
    to = want if want is not None else template
    if to > template:
        return (
            f"Fedora {to} is newer than the {template} this Lima's podman template uses: upgrade "
            f"Lima first, so its guest agent and boot setup are made for Fedora {to}"
        )
    if have >= to:
        return f"already on Fedora {have}"
    if to - have > MAX_STEP:
        return (
            f"Fedora {have} to {to} is more than two releases: upgrade to {have + MAX_STEP} first"
        )
    return to


@dataclass
class Snapshot:
    release: tuple[str, int] | None = None
    free: int = 0
    volumes: dict[str, int] = field(default_factory=dict)
    images: frozenset[str] = frozenset()
    containers: dict[str, str] = field(default_factory=dict)
    ended: bool = False

    @property
    def complete(self) -> bool:
        """The whole listing arrived, and every volume's size was read."""
        return self.ended and self.release is not None and -1 not in self.volumes.values()

    @property
    def running(self) -> list[str]:
        return [c for c, s in self.containers.items() if s == "running"]


def parse_snapshot(text: str) -> Snapshot:
    s = Snapshot()
    images = set()
    for line in text.splitlines():
        parts = line.split()
        if len(parts) == 3 and parts[0] == "release":
            s.release = (parts[1], int(parts[2]) if parts[2].isdigit() else 0)
        elif len(parts) == 2 and parts[0] == "free" and parts[1].isdigit():
            s.free = int(parts[1])
        elif parts[:1] == ["volume"] and len(parts) >= 2:
            s.volumes[parts[1]] = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else -1
        elif len(parts) == 2 and parts[0] == "image":
            images.add(parts[1])
        elif len(parts) == 3 and parts[0] == "container":
            s.containers[parts[1]] = parts[2]
        elif parts == ["end"]:
            s.ended = True
    s.images = frozenset(images)
    return s


def _size(n: int) -> str:
    v = float(n)
    for unit in ("B", "KiB", "MiB", "GiB"):
        if v < 1024 or unit == "GiB":
            return f"{v:.1f} {unit}" if unit != "B" else f"{n} B"
        v /= 1024
    return f"{n} B"


def differences(before: Snapshot, after: Snapshot) -> list[str]:
    """What the store lost or changed. A container's state may change (they were stopped)."""
    out = []
    for name, size in sorted(before.volumes.items()):
        if name not in after.volumes:
            out.append(f"volume {name} is missing")
        elif after.volumes[name] != size:
            out.append(f"volume {name} was {_size(size)}, is {_size(after.volumes[name])}")
    out += [f"image {i} is missing" for i in sorted(before.images - after.images)]
    out += [
        f"container {c} is missing" for c in sorted(set(before.containers) - set(after.containers))
    ]
    return out


# After `limactl start` gives up on the upgrade boot (it can, on a VM that IS up, while its boot
# scripts run), the guest's own record is read this often, for this long, before the run leaves
# it be: a VM is never stopped while its package transaction may still be running.
POLL_EVERY = 10.0
POLL_FOR = 3600.0


def _snapshot(guest: Callable[[str], subprocess.CompletedProcess]) -> Snapshot:
    res = guest(SNAPSHOT)
    snap = parse_snapshot(res.stdout)
    if res.returncode != 0:
        snap.ended = False
    return snap


def run(
    release: int | None = None,
    assume_yes: bool = False,
    *,
    backend=None,
    guest: Callable[[str], subprocess.CompletedProcess] | None = None,
    template: Callable[[], int | None] | None = None,
    ensure: Callable[[], object] | None = None,
    host_wall: Callable[[], bool] | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> int:
    from . import config, machine

    if backend is None:
        if config.in_box():
            print("✗ run on your computer — the box can't upgrade its own machine")
            return 1
        machine._refuse_unusable()
        backend = machine.BACKEND
    name = machine.MACHINE
    if guest is None:
        guest = _guest(backend, name)
    if template is None:
        template = lima_template_release
    if host_wall is None:
        host_wall = machine._host_wall_wanted
    if ensure is None:
        from . import stack

        main = stack.main_repo()
        ensure = lambda: machine.ensure(main, stack.worktrees_root(main))  # noqa: E731
    if host_wall():
        # The upgrade boot fetches from the Fedora mirrors, which the host wall rightly refuses,
        # and booting outside the wall's slice would leave the VM unwalled for the whole of it.
        print("✗ `[machine] host_firewall` is on: the upgrade needs the Fedora mirrors, which the")
        print("  host wall refuses. Not supported yet; `fy machine recreate` starts over instead.")
        return 1
    if not backend.exists(name):
        print(f"✗ no machine '{name}' to upgrade (`fy up` creates one on the template's Fedora)")
        return 1
    if backend.state(name) != "running":
        ensure()

    before = _snapshot(guest)
    if not before.complete:
        print(f"✗ couldn't read machine '{name}' whole (its release, volumes, images, containers)")
        return 1
    to = target(before.release, template(), release)
    if isinstance(to, str):
        print(("✓ " if to.startswith("already") else "✗ ") + f"machine '{name}': {to}")
        return 0 if to.startswith("already") else 1
    if before.free < MIN_FREE:
        print(f"✗ {_size(before.free)} free in the VM: the upgrade wants {_size(MIN_FREE)}")
        return 1
    have = before.release[1] if before.release else 0
    print(f"This upgrades machine '{name}' from Fedora {have} to {to} in place, keeping its")
    print(
        f"{len(before.volumes)} volumes, {len(before.images)} images and "
        f"{len(before.containers)} containers."
    )
    print(f"It stops every container ({len(before.running)} running, started again after) and")
    print("restarts the VM twice; the upgrade itself runs at the first boot, as root.")
    if not assume_yes:
        try:
            answer = input("Proceed? (y/N): ").strip()
        except EOFError:
            answer = ""
        if answer not in ("y", "Y"):
            print("Aborted.")
            return 0

    sys.stdout.flush()  # the plan before the progress lines (stderr)
    _err("▶ stopping the containers")
    stopped = guest("podman stop -a -t 30 >/dev/null")
    # The baseline is the store at rest: a database writes its last checkpoint as it stops, so a
    # snapshot of a running stack would read its shutdown as a changed volume.
    baseline = _snapshot(guest)
    if stopped.returncode != 0 or not baseline.complete or baseline.running:
        still = ", ".join(c[:12] for c in baseline.running) or "unknown"
        print(f"✗ the containers didn't all stop (still running: {still}); nothing else changed")
        return 1
    _err(f"▶ recording the upgrade, then booting it (Fedora {have} → {to}; this is the long part)")
    if not backend.stop(name) or not backend.set_provision(name, render(to), MARKER):
        print(f"✗ couldn't record the upgrade on '{name}' (it is unchanged: `fy up` starts it)")
        return 1
    backend.start(name, wait=UPGRADE_WAIT)
    state = _upgrade_state(guest)
    for _ in range(int(POLL_FOR / POLL_EVERY)):
        if not state.startswith("running"):
            break
        sleep(POLL_EVERY)
        state = _upgrade_state(guest)
    if state.startswith("running"):
        print(f"✗ the upgrade is still running in '{name}' after {UPGRADE_WAIT} and an hour more;")
        print("  it is left to finish (never stopped mid-transaction). Its log:")
        print(f"    limactl shell {name} cat /var/lib/fy-upgrade/log")
        print("  Run `fy machine upgrade` again once it ends: it picks up from the guest's record.")
        return 1
    _err("▶ dropping the upgrade script and starting the VM as usual")
    if not backend.stop(name):
        print(f"✗ '{name}' didn't stop after the upgrade boot; it still runs the old kernel and")
        print(f"  the upgrade script stays recorded. Stop it (`limactl stop {name}`), then run")
        print("  `fy machine upgrade` again.")
        return 1
    if not backend.drop_provision(name, MARKER):
        print(
            f"✗ couldn't remove the upgrade script from '{name}' (left stopped, so it doesn't run"
        )
        print(f"  again): remove the provision entry whose script holds '{MARKER.strip()}' with")
        print(f"  `limactl edit {name}`, then `fy up`.")
        return 1
    ensure()
    if not state.startswith(f"done {to}"):
        print(f"✗ the upgrade didn't finish: {state or 'no report from the guest'}")
        print(f"  The guest's log: limactl shell {name} cat /var/lib/fy-upgrade/log")
        return 1

    guest("podman system migrate >/dev/null 2>&1")
    # Migrate replaces the pause process, and with it the rootless user namespace every podman
    # call joins. The API service (socket-activated: foldyard's own start already woke it) stays
    # in the old one, so containers it then creates (`fy up`'s) sit where no later CLI call can
    # signal them: exec, health checks and stop all fail with EPERM (seen live on a project VM).
    # Stopped, the socket starts it again on the next connection, in the new namespace.
    guest("systemctl --user stop podman.service >/dev/null 2>&1; true")
    after = _snapshot(guest)
    diffs = [] if after.complete else ["the store couldn't be read whole afterwards"]
    diffs += differences(baseline, after)
    if after.release != ("fedora", to):
        diffs.insert(0, f"the guest reports {after.release}, not Fedora {to}")
    back = _restart(guest, before.running)
    if back:
        diffs.append(f"not running again: {', '.join(c[:12] for c in back)} (`podman start` them)")
    if diffs:
        print(f"✗ machine '{name}' upgraded, but not everything checks out:")
        for d in diffs:
            print(f"    {d}")
        return 1
    print(
        f"✓ machine '{name}' is on Fedora {to}: {len(after.volumes)} volumes (sizes unchanged), "
        f"{len(after.images)} images and {len(after.containers)} containers kept; "
        f"{len(before.running)} started again."
    )
    return 0


def _upgrade_state(guest: Callable[[str], subprocess.CompletedProcess]) -> str:
    return guest(f"cat {STATE} 2>/dev/null").stdout.strip()


def _restart(guest: Callable[[str], subprocess.CompletedProcess], ids: list[str]) -> list[str]:
    """Start ``ids`` again; the ones that aren't running afterwards."""
    if not ids:
        return []
    guest("podman start " + " ".join(ids) + " >/dev/null")
    running = set(guest("podman ps --no-trunc --format '{{.ID}}'").stdout.split())
    return [c for c in ids if c not in running]


def _guest(backend, name: str) -> Callable[[str], subprocess.CompletedProcess]:
    """The VM user's shell over the backend's ssh (a failure reads as empty output)."""
    from .sandbox import _ssh

    def run_script(script: str) -> subprocess.CompletedProcess:
        target = backend.ssh_target(name)
        if target is None:
            return subprocess.CompletedProcess([], 255, "", "no ssh route")
        try:
            return _ssh(target, script, timeout=900)
        except subprocess.TimeoutExpired:
            return subprocess.CompletedProcess([], 124, "", "timed out")

    return run_script
