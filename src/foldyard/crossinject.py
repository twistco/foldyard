"""Masking another checkout's dirs in a dev box that is ALREADY running (#91, the follow-up).

A box gets its masks (:mod:`foldyard.crossmask`) at create, so a worktree added while it runs was
unmasked in it until a recreate. Mount propagation can't fix that: crun's binds come up private,
so nothing mounted in the VM reaches a running container. What does: foldyard's own helper
(``assets/crossmask/inject.py``), run in the VM as the VM user over the backend's ssh, joins the
box's namespaces (the VM user owns them — no root, no sudo) and attaches a read-only clone of the
note volume on each target, refusing a symlink anywhere on the path. The result is the same
read-only mount a create would have made, and the box's root can't take it off.

:func:`inject` does that for a box; :func:`check` asks the same helper which targets ARE masked,
since ``inspect``'s ``Mounts`` never shows a mount added after create (in a box, the box reads its
own mount table instead). Both are host-side reads and one ssh call per box. A mount added this way
lasts as long as the box's processes: a restart of the box or the VM drops it, and the create that
follows a ``fy box down`` carries every mask anyway.

It refuses — and the callers fall back to naming the box and the recreate — in a box (only your
computer reaches the VM), under the gVisor posture (runsc's own kernel never sees a mount made in
the VM), for a rootful engine (its containers' namespaces are root's), and on a kernel without the
new mount API (Linux 5.12+)."""

from __future__ import annotations

import json
import shlex
import subprocess
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path

from . import config, crossmask, machine, mountwrite
from .crossmask import Mask
from .machine_backend import SshTarget
from .sandbox import _ssh

HELPER = Path(__file__).resolve().parent / "assets" / "crossmask" / "inject.py"
PYTHON = "/usr/bin/python3"  # both backends' guests have it (Fedora on Lima, CoreOS on podman)
TIMEOUT_S = 30.0  # one ssh round trip + a few seconds' wait for a dir to reach the VM
_OWN_MOUNTINFO = Path("/proc/self/mountinfo")

_REFUSALS = {
    "in-box": "only your computer can add a mount to a running box",
    "gvisor": "it runs under gVisor, whose own kernel never sees a mount added in the VM",
    "no-ssh": "there's no ssh route into the VM",
    "no-box": "its process can't be read from the engine",
    "no-volume": "the note volume can't be created",
    "rootful": "the VM's engine runs it as root, and adding a mount to it would need root in the "
    "VM, which foldyard never takes",
    "enosys": "the VM's kernel lacks the new mount API (Linux 5.12 or later)",
    "stale": "it restarted while foldyard was looking",
}
_STATUSES = {
    "symlink": "a symlink on the path (refused, never followed)",
    "missing": "the dir didn't reach the VM",
    "moved": "the dir was renamed while foldyard masked it, so the mount was taken off again",
    "moved-kept": "the dir was renamed while foldyard masked it, and the mount stayed on it",
    "not-a-dir": "not a directory",
    "invalid": "not a path a box can mount",
    "unmasked": "not masked",
    "writable": "mounted writable",
}


@dataclass(frozen=True)
class Injected:
    """What :func:`inject` did for one box: the masks it ``done``, those ``left`` with why, and —
    when it couldn't run at all — ``refused`` (why, as a sentence fragment; ``""`` when it ran).
    A target that was already masked is in neither list."""

    done: list[Mask]
    left: list[tuple[Mask, str]]
    refused: str


@dataclass(frozen=True)
class _Box:
    pid: int
    cid: str
    runtime: str
    image: str


def _engine_read(engine: str, args: list[str], env: dict, timeout: float) -> str:
    """One engine read's stdout, ``""`` when it fails or doesn't answer in ``timeout``."""
    try:
        out = subprocess.run(
            [engine, *args], env=env, capture_output=True, text=True, timeout=timeout
        )
    except (subprocess.TimeoutExpired, OSError):
        return ""
    return out.stdout.strip() if out.returncode == 0 else ""


def _box(engine: str, box: str, env: dict, timeout: float) -> _Box | None:
    fmt = "{{.State.Pid}}\t{{.Id}}\t{{.OCIRuntime}}\t{{.ImageName}}"
    parts = _engine_read(engine, ["inspect", box, "--format", fmt], env, timeout).split("\t")
    if len(parts) != 4 or not parts[0].isdigit() or int(parts[0]) <= 1 or not parts[1]:
        return None  # podman reports pid 0 for a box that isn't running
    return _Box(int(parts[0]), parts[1], parts[2], parts[3])


def _source(engine: str, env: dict, timeout: float) -> str:
    """The note volume's ``_data`` dir as the VM's engine sees it; ``""`` when there is none."""
    args = ["volume", "inspect", crossmask.note_volume(), "--format", "{{.Mountpoint}}"]
    src = _engine_read(engine, args, env, timeout)
    return src if src.startswith("/") else ""


def _route(engine: str, box: str, env: dict, timeout: float) -> tuple[SshTarget, _Box] | str:
    """The ssh route and the box's facts, or the refusal code."""
    if config.in_box():
        return "in-box"
    target = machine.BACKEND.ssh_target(machine.MACHINE)
    if target is None:
        return "no-ssh"
    facts = _box(engine, box, env, timeout)
    if facts is None:
        return "no-box"
    if "runsc" in facts.runtime:
        return "gvisor"
    return target, facts


def _call(
    target: SshTarget, mode: str, facts: _Box, source: str, paths: Sequence[str], timeout: float
) -> tuple[str, dict[str, str]]:
    """``(refusal, results)`` from one run of the helper in the guest; the refusal is a sentence
    fragment, ``""`` when it ran."""
    argv = [mode, str(facts.pid), facts.cid, source, *paths]
    script = f"exec {PYTHON} -I - " + " ".join(shlex.quote(a) for a in argv)
    try:
        res = _ssh(target, script, stdin=HELPER.read_bytes(), timeout=timeout)
    except subprocess.TimeoutExpired:
        return f"the VM didn't answer over ssh within {int(timeout)}s", {}
    except OSError as e:
        return f"ssh into the VM failed ({e})", {}
    try:
        answer = json.loads(res.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        answer = None
    if not isinstance(answer, dict) or not isinstance(answer.get("results"), dict):
        lines = (res.stderr or res.stdout).strip().splitlines()
        why = lines[-1] if lines else f"exit {res.returncode}"
        return f"foldyard's helper in the VM failed ({why})", {}
    if answer.get("refused"):
        code = str(answer["refused"])
        return _REFUSALS.get(code, f"{code}: {answer.get('detail') or 'refused'}"), {}
    return "", {str(k): str(v) for k, v in answer["results"].items()}


def _why(status: str) -> str:
    if status.startswith("error: "):
        return status[len("error: ") :]
    return _STATUSES.get(status, status)


def inject(
    engine: str,
    box: str,
    env: dict,
    masks: Iterable[Mask],
    *,
    main: Path,
    wt_root: Path,
    ensure_note: Callable[[str], object],
    timeout: float = TIMEOUT_S,
) -> Injected:
    """Mask each of ``masks`` in the running ``box``: the dir made on your computer first (a no-
    follow walk from its bind root — a link the box planted refuses that mask), then one helper run
    in the VM for all of them. ``ensure_note(image)`` makes the note volume when there is none yet
    (with the box's own image, which is certainly there). Never raises for a refusal."""
    masks = list(masks)
    if not masks:
        return Injected([], [], "")

    def refuse(why: str) -> Injected:
        return Injected([], [(m, why) for m in masks], why)

    route = _route(engine, box, env, timeout)
    if isinstance(route, str):
        return refuse(_REFUSALS[route])
    target, facts = route
    source = _source(engine, env, timeout)
    if not source:
        ensure_note(facts.image)
        source = _source(engine, env, timeout)
    if not source:
        return refuse(_REFUSALS["no-volume"])
    left: list[tuple[Mask, str]] = []
    ready: list[Mask] = []
    for m in masks:
        where = crossmask.host_dir(m.path, main, wt_root)
        if where is None:
            left.append((m, "outside the checkouts the box binds"))
            continue
        try:
            mountwrite.makedirs(where[0], where[1], 0o755)
        except (OSError, ValueError) as e:
            left.append((m, f"a symlink or a file on the path on your computer ({e})"))
            continue
        ready.append(m)
    if not ready:
        return Injected([], left, "")
    refused, results = _call(target, "inject", facts, source, [m.path for m in ready], timeout)
    if refused:
        return Injected([], left + [(m, refused) for m in ready], refused)
    done: list[Mask] = []
    for m in ready:
        status = results.get(m.path, "no answer")
        if status == "injected":
            done.append(m)
        elif status != "masked":
            left.append((m, _why(status)))
    return Injected(done, left, "")


def check(
    engine: str, box: str, env: dict, paths: Iterable[str], *, timeout: float, mine: bool
) -> set[str] | None:
    """Of ``paths``, those the running ``box`` really has masked, from the kernel rather than the
    engine's record — or None when that can't be read (the caller falls back to ``inspect``). In a
    box only its own box (``mine``) can be read: its own mount table. REPORTING only."""
    paths = list(paths)
    if not paths:
        return set()
    if config.in_box():
        if not mine:
            return None
        try:
            text = _OWN_MOUNTINFO.read_text()
        except OSError:
            return None
        return crossmask.mountinfo_masked(text, paths, crossmask.note_volume())
    route = _route(engine, box, env, timeout)
    if isinstance(route, str):
        return None
    target, facts = route
    source = _source(engine, env, timeout)
    if not source:
        return None
    refused, results = _call(target, "check", facts, source, paths, timeout)
    if refused:
        return None
    return {p for p in paths if results.get(p) == "masked"}
