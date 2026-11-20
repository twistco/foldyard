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
4. `podman system migrate`, then restart the API services in migrate's new user namespace;
   compare with the snapshot, start what ran before.

Once the stack is stopped, the run's context (target, baseline, which containers ran) is saved in
the project's host state dir until the end, so a run cut short (the transaction outlasting the
wait, a failed dnf, a VM that won't stop) is resumed by running it again, against the original
baseline. The guest's own record says where it got to; one that reads `failed` or `running` is
re-run even when the release number has already moved.

**The target never passes the Fedora the installed Lima's own podman template uses.** That is the
Lima version check: a release Lima ships in its template is one its guest agent and boot setup are
made for (ADR-0025 saw a newer guest under an older Lima boot degraded), and it follows Lima
forward with no table to keep. It is also the default: the VM is brought to what a new VM gets.
"""

from __future__ import annotations

import json
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


def dump(s: Snapshot) -> str:
    """A snapshot back in :data:`SNAPSHOT`'s listing, as a context saves it."""
    lines = [f"release {s.release[0]} {s.release[1]}"] if s.release else []
    lines.append(f"free {s.free}")
    lines += [f"volume {n} {b}" for n, b in sorted(s.volumes.items())]
    lines += [f"image {i}" for i in sorted(s.images)]
    lines += [f"container {c} {st}" for c, st in sorted(s.containers.items())]
    if s.ended:
        lines.append("end")
    return "\n".join(lines) + "\n"


def merged(original: Snapshot, now: Snapshot) -> Snapshot:
    """A retry's baseline: ``now``'s volume sizes (the stack has run since), but every volume,
    image and container ``original`` had, so one a failed transaction lost still shows missing."""
    return Snapshot(
        release=now.release,
        free=now.free,
        volumes={**original.volumes, **now.volumes},
        images=original.images | now.images,
        containers={**original.containers, **now.containers},
        ended=now.ended,
    )


def _size(n: int) -> str:
    v = float(n)
    for unit in ("B", "KiB", "MiB", "GiB"):
        if v < 1024 or unit == "GiB":
            return f"{v:.1f} {unit}" if unit != "B" else f"{n} B"
        v /= 1024
    return f"{n} B"


def differences(before: Snapshot, after: Snapshot, sizes: bool = True) -> list[str]:
    """What the store lost or changed. A container's state may change (they were stopped).
    ``sizes=False`` compares only what's there, for a store whose stack runs again."""
    out = []
    for name, size in sorted(before.volumes.items()):
        if name not in after.volumes:
            out.append(f"volume {name} is missing")
        elif sizes and after.volumes[name] != size:
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


def context_path(name: str) -> Path:
    """Where an upgrade under way keeps what a rerun needs (its target, the store's baseline,
    which containers ran): the project's host state dir, outside the repo mount."""
    from . import config

    return config.state_dir() / f"upgrade-{name}.json"


def forget(name: str) -> None:
    """Drop an upgrade's context with the VM it belongs to (`fy machine rm`/`recreate`): a new
    VM has no record of it, so a later upgrade would otherwise refuse over it for good."""
    context_path(name).unlink(missing_ok=True)


def _save(path: Path, ctx: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(ctx))
    tmp.replace(path)


def _saved(context: Path, name: str, to: int, running: list[str], baseline: Snapshot,
           guest: Callable[[str], subprocess.CompletedProcess]) -> bool:  # fmt: skip
    """Save what a rerun needs; when it can't be saved, the stack (stopped by now) is started
    again and the run goes no further: an upgrade that couldn't be resumed isn't begun."""
    try:
        _save(context, {"machine": name, "target": to, "running": running,
                        "baseline": dump(baseline)})  # fmt: skip
    except OSError as e:
        back = _restart(guest, running)
        print(f"✗ couldn't save the upgrade's context ({context}: {e}); the containers were")
        print("  started again and nothing else changed" + (
            f", except these, which didn't start: {', '.join(c[:12] for c in back)}" if back else ""
        ))  # fmt: skip
        return False
    return True


def _load(path: Path, name: str) -> dict | None:
    try:
        ctx = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    ok = (
        isinstance(ctx, dict)
        and ctx.get("machine") == name
        and isinstance(ctx.get("target"), int)
        and isinstance(ctx.get("running"), list)
        and isinstance(ctx.get("baseline"), str)
        and ctx.get("phase", "") in ("", "check")
    )
    return ctx if ok else None


def _record(state: str) -> tuple[str, int | None]:
    """The guest's record (``done 45``, ``failed 45: …``, ``running 45``) as (kind, release)."""
    parts = state.split()
    if not parts:
        return "", None
    n = parts[1].rstrip(":") if len(parts) > 1 else ""
    return parts[0], int(n) if n.isdigit() else None


def _stop_stack(
    guest: Callable[[str], subprocess.CompletedProcess], running: list[str]
) -> tuple[Snapshot, str] | None:
    """Stop every container, then read the store at rest: the baseline (a database writes its
    last checkpoint as it stops, so a snapshot of a running stack would read its shutdown as a
    changed volume). ``None``, with the ones that stopped started again, when they didn't all
    stop or the store can't be read whole."""
    _err("▶ stopping the containers")
    stopped = guest("podman stop -a -t 30 >/dev/null")
    at_rest = guest(SNAPSHOT)
    baseline = parse_snapshot(at_rest.stdout)
    if at_rest.returncode != 0:
        baseline.ended = False
    if stopped.returncode != 0 or not baseline.complete or baseline.running:
        still = ", ".join(c[:12] for c in baseline.running) or "unknown"
        _restart(guest, [c for c in running if c not in baseline.running])
        print(f"✗ the containers didn't all stop (still running: {still}); they were started")
        print("  again and nothing else changed")
        return None
    return baseline, at_rest.stdout


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
    context: Path | None = None,
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
    if context is None:
        context = context_path(name)
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

    ctx = _load(context, name)
    if ctx is not None:
        # An earlier run got as far as stopping the stack: carry on from there, against ITS
        # baseline and its running set (this run would only see everything stopped).
        to, running = int(ctx["target"]), list(ctx["running"])
        baseline = parse_snapshot(ctx["baseline"])
        state = _upgrade_state(guest)
        if ctx.get("phase") == "check":
            # Upgraded, rebooted and dropped already; the last run's check failed. Check again
            # as the VM is now, its stack running: what's there, not the sizes. Never fall
            # through to the transaction: a record that can't be read (a failed ssh reads as "")
            # mustn't send an upgraded VM through it and two restarts again.
            if not state.startswith(f"done {to}"):
                print(
                    f"✗ couldn't confirm the upgrade to Fedora {to} in '{name}' to check it again"
                )
                print(
                    f"  (its record read: {state or 'nothing'}). Run `fy machine upgrade` again; if"
                )
                print(f"  the VM was recreated or its record is gone, remove {context}.")
                return 1
            _err(f"▶ checking the upgrade to Fedora {to} again")
            return _verify(name, to, baseline, running, guest, context, sizes=False)
        _err(f"▶ resuming the upgrade to Fedora {to} (its record: {state or 'none yet'})")
        if not state.startswith((f"done {to}", "running ")):
            # The transaction runs again. A failed one started the stack again, and it may have
            # run since: stop it cleanly before the VM is powered off, and compare against the
            # store as it is now, at rest, not the first attempt's.
            now = _snapshot(guest)
            # The original set (one that didn't come back after the failure is still owed a
            # start), and what runs now.
            running = list(dict.fromkeys([*running, *now.running]))
            got = _stop_stack(guest, running)
            if got is None:
                return 1
            baseline = merged(baseline, got[0])
            if not _saved(context, name, to, running, baseline, guest):
                return 1
    else:
        before = _snapshot(guest)
        if not before.complete:
            print(
                f"✗ couldn't read machine '{name}' whole (its release, volumes, images, containers)"
            )
            return 1
        state = _upgrade_state(guest)
        kind, left = _record(state)
        if kind == "running":
            # Without its context, but a transaction may really still be running: never start
            # another over it.
            print(f"✗ an upgrade is still running in '{name}' ({state}). Its log:")
            print(f"    limactl shell {name} cat /var/lib/fy-upgrade/log")
            print("  Run `fy machine upgrade` again once it has ended.")
            return 1
        if kind == "failed":
            # A transaction this VM's own record says didn't end well: its release number may
            # already read the target, so it is re-run whatever the release says, but still
            # never past Lima's template, and only as the release asked for, if one was.
            ceiling = template()
            if left is None or ceiling is None or left > ceiling or release not in (None, left):
                print(f"✗ machine '{name}': an earlier upgrade didn't finish ({state}), and it")
                print("  can't be run again as asked: its release is unreadable, past what this")
                print("  Lima's podman template uses, or not the `--release` given.")
                return 1
            to = left
            _err(f"▶ an earlier upgrade to Fedora {to} didn't finish ({state}): running it again")
        else:
            got = target(before.release, template(), release)
            if isinstance(got, str):
                print(("✓ " if got.startswith("already") else "✗ ") + f"machine '{name}': {got}")
                return 0 if got.startswith("already") else 1
            to = got
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
        running = before.running
        got = _stop_stack(guest, running)
        if got is None:
            return 1
        baseline = got[0]
        if not _saved(context, name, to, running, baseline, guest):
            return 1
        state = ""

    if not state.startswith((f"done {to}", "running ")):
        _err(f"▶ recording the upgrade, then booting it (to Fedora {to}; this is the long part)")
        if backend.state(name) == "running" and not backend.stop(name):
            print(f"✗ couldn't stop '{name}' to record the upgrade; `fy machine upgrade` resumes")
            return 1
        if not backend.set_provision(name, render(to), MARKER):
            print(f"✗ couldn't record the upgrade on '{name}'; `fy machine upgrade` resumes")
            return 1
        backend.start(name, wait=UPGRADE_WAIT)
        state = _upgrade_state(guest)
    # Only the guest's own word ends the wait: "" is a record that couldn't be read, never "done".
    for _ in range(int(POLL_FOR / POLL_EVERY)):
        if state.startswith((f"done {to}", "failed ")):
            break
        sleep(POLL_EVERY)
        state = _upgrade_state(guest)
    if not state.startswith((f"done {to}", "failed ")):
        print(f"✗ the upgrade in '{name}' hasn't reported an end ({state or 'no record read'});")
        print("  it is left running, never stopped mid-transaction. Its log:")
        print(f"    limactl shell {name} cat /var/lib/fy-upgrade/log")
        print("  `fy machine upgrade` again resumes once it ends.")
        return 1

    _err("▶ dropping the upgrade script and starting the VM as usual")
    if not backend.stop(name):
        print(
            f"✗ '{name}' didn't stop after the upgrade boot: it still runs the old kernel, and the"
        )
        print(f"  upgrade script stays recorded. Stop it (`limactl stop {name}`), then")
        print("  `fy machine upgrade` again resumes.")
        return 1
    if not backend.drop_provision(name, MARKER):
        print(
            f"✗ couldn't remove the upgrade script from '{name}' (left stopped, so it doesn't run"
        )
        print(f"  again): remove the provision entry whose script holds '{MARKER.strip()}' with")
        print(f"  `limactl edit {name}`, then `fy machine upgrade` again.")
        return 1
    ensure()
    if not state.startswith(f"done {to}"):
        back = _restart(guest, running)
        print(f"✗ the upgrade didn't finish: {state}")
        print("  The guest may be partly upgraded; `fy machine upgrade` again retries (dnf")
        print("  distro-sync completes it). Its log:")
        print(f"    limactl shell {name} cat /var/lib/fy-upgrade/log")
        if back:
            print(f"  Not running again: {', '.join(c[:12] for c in back)}")
        return 1

    guest("podman system migrate >/dev/null 2>&1")
    # Migrate replaces the pause process, and with it the rootless user namespace every podman
    # call joins. An API service already running stays in the old one, so containers it then
    # creates (`fy up`'s) sit where no later CLI call can signal them: exec, health checks and
    # stop all fail with EPERM (seen live on a project VM). podman.service is socket-activated:
    # stopped, its socket starts it again in the new namespace. The gVisor posture's
    # podman-runsc.service isn't, so it is restarted (its filter follows: Requires=).
    guest(
        "systemctl --user stop podman.service >/dev/null 2>&1; "
        "systemctl --user try-restart podman-runsc.service >/dev/null 2>&1; true"
    )
    # From here on, a rerun only checks again: the transaction, reboot and drop are done, and
    # the stack is about to run again, so it must neither stop the VM nor compare sizes.
    try:
        _save(context, {"machine": name, "target": to, "running": running,
                        "baseline": dump(baseline), "phase": "check"})  # fmt: skip
    except OSError as e:
        # The check runs now either way. Should it fail too, a rerun resumes from the earlier
        # save, which goes through the finishing restart again: say so.
        _err(f"⚠ couldn't record that the upgrade is done ({context}: {e}); if this check fails,")
        _err("  stop the containers before running `fy machine upgrade` again")
    return _verify(name, to, baseline, running, guest, context, sizes=True)


def _verify(name: str, to: int, baseline: Snapshot, running: list[str],
            guest: Callable[[str], subprocess.CompletedProcess], context: Path,
            sizes: bool) -> int:  # fmt: skip
    """The store against the baseline, the release, and the original running set started
    again; the context removed only once all of it holds."""
    after = _snapshot(guest)
    diffs = [] if after.complete else ["the store couldn't be read whole afterwards"]
    diffs += differences(baseline, after, sizes=sizes)
    if after.release != ("fedora", to):
        diffs.insert(0, f"the guest reports {after.release}, not Fedora {to}")
    back = _restart(guest, running)
    if back:
        diffs.append(f"not running again: {', '.join(c[:12] for c in back)} (`podman start` them)")
    if diffs:
        # Kept: a rerun resumes here (the record says done, so no new transaction) and checks
        # again against the same baseline.
        print(f"✗ machine '{name}' upgraded, but not everything checks out:")
        for d in diffs:
            print(f"    {d}")
        print("  Put it right and run `fy machine upgrade` again to check again, or remove")
        print(f"  {context} to accept it as it is.")
        return 1
    context.unlink(missing_ok=True)  # verified and restored: nothing left to resume
    print(
        f"✓ machine '{name}' is on Fedora {to}: {len(after.volumes)} volumes "
        f"({'sizes unchanged' if sizes else 'all there'}), "
        f"{len(after.images)} images and {len(after.containers)} containers kept; "
        f"{len(running)} started again."
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
