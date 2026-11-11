"""`fy ls`: every foldyard project on this computer, its VM, host supervisor, boxes and agents —
what is running, what wants attention, and what is left over.

Read-only. Nothing here starts a VM, a supervisor or a box, and it changes no state of its own.

**Two tiers.** What the host keeps on disk answers for every project in THIS process: the adopted
config (`configpin`) names each project's VM and state dir, one ``limactl list`` gives every Lima
VM, and the supervisor's lock, readiness record and heartbeat are files. Read under
``config.using(<that checkout's adopted config>)``, every ``config.X()`` resolves for that project
(module constants such as ``machine.MACHINE`` don't, so none is used here). What only the engine
knows — which boxes and stacks are up, the agents in them — comes from a child
``fy ls --here --json`` run in each checkout whose VM is ALREADY running (as `fy agent ls` asks
each project), never from one that would have to boot it.

**What it never decides.** A Lima VM that foldyard made (its boot script is recorded in the
VM's ``lima.yaml``, ``# fy-provision``) and no adopted project names is listed as left over, never
removed. Lima VMs without that script aren't foldyard's and aren't listed.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from pathlib import Path

from . import config

# A project's child (`fy ls --here --json`) asks the engine once and each running box's tmux.
_CHILD_TIMEOUT = 60


def _err(*a: object) -> None:
    print(*a, file=sys.stderr, flush=True)


@dataclass
class Note:
    """Something about a project (or the computer) that wants a look: ``level`` is ``✗`` (broken
    now) or ``⚠`` (will bite, or costs you); ``fix`` the command that addresses it, run in the
    project's checkout."""

    level: str
    text: str
    fix: str = ""


@dataclass
class Vm:
    name: str
    backend: str
    status: str  # Lima's own words (Running, Stopped, Broken…), "missing", or "unknown"
    cpus: int | None = None
    memory: int | None = None  # bytes
    disk_used: int | None = None  # bytes actually on disk (sparse images count what they hold)
    foldyards: bool = False  # its lima.yaml carries foldyard's boot script

    @property
    def running(self) -> bool:
        return self.status.lower() == "running"


@dataclass
class Workspace:
    name: str  # "main" or the worktree's
    box: bool
    containers: int | None  # the stack's; None = the engine didn't answer


@dataclass
class Project:
    project: str
    checkout: str
    vm: Vm
    supervisor: str  # up · starting · failing · stale · down · unknown
    workspaces: list[Workspace] | None = None  # None = not asked (VM not running, or no answer)
    agents_live: int | None = None
    agents_total: int | None = None
    unavailable: str = ""  # why the engine-side facts are missing
    notes: list[Note] = field(default_factory=list)
    below_floor: bool = False  # older than its min_foldyard_version: its child would be refused


@dataclass
class Leftover:
    what: str
    why: str
    fix: str = ""


@dataclass
class Listing:
    projects: list[Project]
    leftovers: list[Leftover]
    base_vm: dict | None  # the base VM's status, when there is one


# ── the host's own record ─────────────────────────────────────────────────────────────────────


def _lima_instances() -> dict[str, dict]:
    from .machine_backend import LimaBackend

    return {str(row.get("name")): row for row in LimaBackend()._instances() if row.get("name")}


def _disk_used(instance_dir: object) -> int | None:
    """Bytes the instance's files hold on disk (allocated blocks: a sparse disk image counts what
    it holds, not its size), top level only — the disks live there."""
    if not isinstance(instance_dir, str) or not instance_dir:
        return None
    total = 0
    try:
        with os.scandir(instance_dir) as entries:
            for entry in entries:
                try:
                    st = entry.stat(follow_symlinks=False)
                except OSError:
                    continue
                if entry.is_file(follow_symlinks=False):
                    total += getattr(st, "st_blocks", 0) * 512 or st.st_size
    except OSError:
        return None
    return total


def _lima_vm(name: str, row: dict | None) -> Vm:
    from .machine_backend import LimaBackend

    if row is None:
        return Vm(name, "lima", "missing")
    cpus, memory = row.get("cpus"), row.get("memory")
    return Vm(
        name,
        "lima",
        str(row.get("status") or "unknown"),
        cpus if isinstance(cpus, int) else None,
        memory if isinstance(memory, int) else None,
        _disk_used(row.get("dir")),
        bool(LimaBackend().provision_id(name)),
    )


def _vm(instances: dict[str, dict]) -> Vm:
    """The bound project's VM. Lima from the one listing; podman machine by asking it."""
    name, backend = config.machine_name(), config.machine_backend()
    if backend == "lima":
        return _lima_vm(name, instances.get(name))
    from .machine_backend import get_backend

    state = get_backend(backend).state(name)
    return Vm(name, backend, {"running": "Running", "stopped": "Stopped"}.get(state, "unknown"))


def _supervisor(vm: Vm, notes: list[Note], fingerprint: str) -> str:
    """The bound project's supervisor, from its lock, readiness record and heartbeat."""
    from . import supervisor

    if not supervisor._supervisor_running():
        if vm.running:
            notes.append(
                Note(
                    "✗",
                    "the VM runs without its host supervisor: boxes have no egress",
                    "fy host restart",
                )
            )
        return "down"
    pid, holder_fp = supervisor._holder_info()
    record = supervisor._read_ready()
    served = set((record or {}).get("worktrees") or [""])
    verdict, lines = supervisor.readiness(record, pid, served)
    if holder_fp is not None and holder_fp != fingerprint:
        # Different, not necessarily older: the fingerprint hashes the code, so a source run
        # (`just run ls`) differs from every supervisor an installed `fy` started.
        notes.append(
            Note(
                "⚠",
                "the host supervisor runs different foldyard code than this `fy` (started before "
                "an upgrade?)",
                "fy host restart",
            )
        )
    if verdict == "fail":
        notes.extend(Note("✗", f"supervisor: {line}", "fy host") for line in lines)
        return "failing"
    if verdict == "wait":
        ticking = all("isn't ticking" not in line for line in lines)
        if not ticking:
            notes.append(Note("✗", f"supervisor: {lines[0]}", "fy host restart"))
        return "starting" if ticking else "stale"
    return "up"


def _version_notes(installed: str) -> list[Note]:
    from . import compat

    minimum, recommended = config.min_foldyard_version(), config.recommended_foldyard_version()
    have = compat._parse(installed)
    if have is None:
        return []
    floor, want = compat._parse(minimum), compat._parse(recommended)
    if floor is not None and have < floor:
        return [Note("✗", f"needs foldyard ≥ {minimum} (you have {installed})", _upgrade())]
    if want is not None and have < want:
        return [Note("⚠", f"recommends foldyard {recommended} (you have {installed})", _upgrade())]
    return []


def _upgrade() -> str:
    from . import compat

    return compat.upgrade_hint()


def _worktree_notes(checkout: Path) -> list[Note]:
    from . import worktree_registry

    recorded = worktree_registry._load(checkout)
    real = worktree_registry.checkouts(checkout)
    return [
        Note(
            "⚠",
            f"worktree {name} is registered at {path}, which is gone",
            f"fy worktree remove {name}",
        )
        for name, path in sorted(recorded.items())
        if name not in real
    ]


def _describe(checkout: Path, instances: dict[str, dict], installed: str, fp: str) -> Project:
    """One adopted project, from the host's own record — no engine, no child process."""
    from . import configpin

    toml = configpin.adopted_toml(checkout)
    if toml is None:  # adopted_checkouts found a marker, but its snapshot didn't read
        return Project(
            checkout.name,
            str(checkout),
            Vm("?", "?", "unknown"),
            "unknown",
            unavailable="its adopted config can't be read",
            notes=[Note("✗", "its adopted config can't be read", "fy config status")],
        )
    cfg = config.Config(repo_root=checkout, worktree="", toml=toml)
    with config.using(cfg):
        try:
            name = config.project()
            vm = _vm(instances)
        except SystemExit as e:  # a name _safe_name refuses
            why = str(e)
            return Project(
                checkout.name,
                str(checkout),
                Vm("?", "?", "unknown"),
                "unknown",
                notes=[Note("✗", why)],
            )
        notes = _version_notes(installed)
        below_floor = any(n.level == "✗" for n in notes)  # the only ✗ _version_notes gives
        state = _supervisor(vm, notes, fp)
        if configpin.inspect(cfg).changed:
            notes.append(Note("⚠", "foldyard.toml changed since it was adopted", "fy config diff"))
        notes.extend(_worktree_notes(checkout))
        if vm.status == "missing":
            notes.append(Note("⚠", f"its VM {vm.name} doesn't exist (yet)", "fy up"))
        elif vm.status.lower() == "broken":
            notes.append(Note("✗", f"its VM {vm.name} stopped answering (Broken)", "fy up"))
    return Project(name, str(checkout), vm, state, notes=notes, below_floor=below_floor)


def _vanished_pins() -> list[Leftover]:
    """Adoptions of a main checkout that's gone (or no longer holds a `foldyard.toml`)."""
    from . import configpin

    try:
        pins = sorted(configpin._pin_root().iterdir())
    except OSError:
        return []
    out = []
    for pin in pins:
        try:
            doc = json.loads((pin / configpin.ADOPTED_MARKER).read_text())
        except (OSError, ValueError):
            continue
        if not isinstance(doc, dict) or doc.get("worktree"):
            continue
        checkout = doc.get("checkout")
        if not isinstance(checkout, str) or not checkout:
            continue
        if not (Path(checkout) / "foldyard.toml").is_file():
            out.append(
                Leftover(
                    f"adopted config for {_short(checkout)}",
                    "the checkout is gone",
                    _short(str(pin)),
                )
            )
    return out


def _base_vm(instances: dict[str, dict]) -> dict | None:
    from . import basevm

    row, stamp = instances.get(basevm.NAME), basevm.read_stamp()
    if row is None and stamp is None:
        return None
    age = basevm.age_days(stamp) if stamp else None
    return {
        "name": basevm.NAME,
        "status": str(row.get("status")) if row else "missing",
        "baked_days_ago": round(age, 1) if age is not None else None,
        "disk_used": _disk_used(row.get("dir")) if row else None,
    }


def _unclaimed(instances: dict[str, dict], claimed: set[str]) -> list[Leftover]:
    """foldyard's Lima VMs that no adopted project names. foldyard records its boot script in
    every VM it starts (``machine._record_provisioning``), so a VM without one isn't foldyard's —
    your own Lima VMs — and stays out of this list."""
    from . import basevm

    out = []
    for name, row in sorted(instances.items()):
        if name in claimed or name == basevm.NAME:
            continue
        vm = _lima_vm(name, row)
        if not vm.foldyards:
            continue
        size = f", {_size(vm.disk_used)} on disk" if vm.disk_used else ""
        out.append(
            Leftover(
                f"VM {name} ({vm.status.lower()}{size})",
                "foldyard made it, and no adopted project uses it",
                f"`limactl delete {'--force ' if vm.running else ''}{name}`",
            )
        )
    return out


# ── the engine's side, from a child in each checkout ─────────────────────────────────────────


def here() -> dict:
    """This project's engine-side facts (the child's half of `fy ls`): its workspaces' boxes and
    stacks, and the agent sessions in those boxes. Never boots the VM: a stopped one is the
    reason they're missing."""
    from . import agents, box, devmode, stack

    if (reason := stack.machine_down()) is not None:
        return {"unavailable": reason}
    wss = devmode.workspaces()
    engine, env = box.engine_env()
    boxes = [
        agents.Box(
            f"{ws['project']}-devbox", "" if ws["name"] == "main" else ws["name"], engine, env
        )
        for ws in wss
        if ws.get("devbox")
    ]
    with ThreadPoolExecutor(max_workers=8) as pool:
        sessions = [s for got in pool.map(agents.sessions_in, boxes) for s in got]
    answered = all(ws.get("containers") is not None for ws in wss)
    return {
        "unavailable": "" if answered else "the engine didn't answer",
        "workspaces": [
            {"name": ws["name"], "box": bool(ws.get("devbox")), "containers": ws.get("containers")}
            for ws in wss
            if ws.get("registered")
        ],
        "agents_live": sum(1 for s in sessions if s.alive),
        "agents_total": len(sessions),
    }


def _ask(p: Project) -> None:
    """Fill ``p``'s engine-side facts from a child run in its checkout."""
    from . import agents

    try:
        out = subprocess.run(
            agents._self("ls", "--here", "--json"),
            cwd=p.checkout,
            env=agents._child_env(),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=_CHILD_TIMEOUT,
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        p.unavailable = f"couldn't ask ({type(e).__name__})"
        return
    try:
        doc = json.loads(out.stdout) if out.returncode == 0 else None
    except ValueError:
        doc = None
    if not isinstance(doc, dict):
        tail = (out.stderr.strip().splitlines() or [""])[-1]
        p.unavailable = agents._printable(tail, 160) or f"couldn't ask (exit {out.returncode})"
        return
    p.unavailable = agents._printable(doc.get("unavailable") or "", 200)
    rows = doc.get("workspaces")
    if isinstance(rows, list):
        p.workspaces = [
            Workspace(
                agents._printable(r.get("name"), 64),
                bool(r.get("box")),
                r["containers"] if isinstance(r.get("containers"), int) else None,
            )
            for r in rows
            if isinstance(r, dict) and r.get("name")
        ]
    for key in ("agents_live", "agents_total"):
        if isinstance(doc.get(key), int):
            setattr(p, key, doc[key])


# ── the listing ──────────────────────────────────────────────────────────────────────────────


def collect() -> Listing:
    """Every adopted project, what's left over, and the base VM."""
    import foldyard

    from . import configpin, supervisor

    instances = _lima_instances()
    fingerprint = supervisor._code_fingerprint()
    projects = [
        _describe(c, instances, foldyard.__version__, fingerprint)
        for c in configpin.adopted_checkouts()
    ]
    # Below a project's floor its child would only be refused by it, saying nothing else.
    ask = [p for p in projects if p.vm.running and not p.below_floor]
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(_ask, ask))
    claimed = {p.vm.name for p in projects if p.vm.backend == "lima"}
    leftovers = _unclaimed(instances, claimed) + _vanished_pins()
    return Listing(projects, leftovers, _base_vm(instances))


def _short(path: str) -> str:
    home = str(Path.home())
    return "~" + path[len(home) :] if path == home or path.startswith(home + os.sep) else path


def _size(n: int | None) -> str:
    if n is None:
        return "?"
    value = float(n)
    for unit in ("B", "KiB", "MiB"):
        if value < 1024:
            return f"{value:.0f} {unit}" if unit != "MiB" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} GiB"


def _vm_cell(vm: Vm) -> str:
    status = vm.status.lower()
    if not vm.running:
        return status
    shape = []
    if vm.cpus:
        shape.append(f"{vm.cpus} CPU")
    if vm.memory:
        shape.append(f"{vm.memory / 2**30:.0f} GiB")
    return f"{status} ({', '.join(shape)})" if shape else status


def _boxes_cell(p: Project) -> str:
    if p.workspaces is None:
        return "—" if not p.vm.running else "?"
    up = [w.name for w in p.workspaces if w.box]
    return ", ".join(up) if up else "none"


def _agents_cell(p: Project) -> str:
    if p.agents_live is None:
        return "—" if not p.vm.running else "?"
    return str(p.agents_live)


def render(listing: Listing) -> str:
    if not listing.projects and not listing.leftovers and listing.base_vm is None:
        return (
            "No projects yet: a project is listed once its config is adopted on this computer "
            "(`fy up` in its checkout asks)."
        )
    head = ("PROJECT", "VM", "DISK", "SUPERVISOR", "BOXES UP", "AGENTS", "CHECKOUT")
    rows = [head] + [
        (
            p.project,
            _vm_cell(p.vm),
            _size(p.vm.disk_used) if p.vm.disk_used is not None else "—",
            p.supervisor,
            _boxes_cell(p),
            _agents_cell(p),
            _short(p.checkout),
        )
        for p in listing.projects
    ]
    widths = [max(len(r[i]) for r in rows) for i in range(len(head))]
    lines = ["  ".join(c.ljust(w) for c, w in zip(r, widths, strict=True)).rstrip() for r in rows]
    noted = [p for p in listing.projects if p.notes or (p.vm.running and p.unavailable)]
    if noted:
        lines += ["", "Needs attention (run the fix in the project's checkout):"]
        for p in noted:
            lines.append(f"  {p.project}  {_short(p.checkout)}")
            for n in p.notes:
                lines.append(f"    {n.level} {n.text}" + (f" — `{n.fix}`" if n.fix else ""))
            if p.vm.running and p.unavailable:
                lines.append(f"    ⚠ boxes and agents unknown: {p.unavailable}")
    if listing.leftovers:
        lines += ["", "Left over (listed, never removed):"]
        for left in listing.leftovers:
            lines.append(f"  {left.what}: {left.why}" + (f" — {left.fix}" if left.fix else ""))
    if (base := listing.base_vm) is not None:
        age = base.get("baked_days_ago")
        baked = f", baked {age:.0f} days ago" if isinstance(age, (int, float)) else ""
        used = f", {_size(base['disk_used'])} on disk" if base.get("disk_used") else ""
        lines += [
            "",
            f"Base VM {base['name']}: {str(base['status']).lower()}{baked}{used} "
            "(`fy machine base status` says whether it's current)",
        ]
    return "\n".join(lines)


def to_json(listing: Listing) -> str:
    return json.dumps(asdict(listing), indent=2)


def ls(*, here_only: bool, as_json: bool) -> int:
    if config.in_box():
        _err("✗ `fy ls` lists this computer's projects — run it on your computer, not in a box.")
        return 1
    if here_only:
        print(json.dumps(here()))
        return 0
    listing = collect()
    print(to_json(listing) if as_json else render(listing))
    return 0
