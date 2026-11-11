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
from collections.abc import Callable
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
    kind: str = "vm"  # vm · folder · adoption
    path: str = ""  # an adoption's folder under the adoption root: what `fy prune` removes
    prunable: bool = False  # `fy prune` may remove it (only an adoption whose checkout is gone)


# Folders foldyard itself keeps straight under ~/.foldyard, beside each project's state folder.
# tests/test_projects.py holds this to every such name in the package.
OWN_FOLDERS = ("adopted", "cache", "logs", "worktrees")


@dataclass
class Listing:
    projects: list[Project]
    leftovers: list[Leftover]
    base_vm: dict | None  # the base VM's status, when there is one
    lima_unavailable: bool = False  # `limactl list` didn't answer: Lima VM states are unknown


# ── the host's own record ─────────────────────────────────────────────────────────────────────


def _lima_instances() -> dict[str, dict] | None:
    """Every Lima VM by name; ``None`` when `limactl list` couldn't answer."""
    from .machine_backend import LimaBackend

    rows = LimaBackend()._listing()
    if rows is None:
        return None
    return {str(row.get("name")): row for row in rows if row.get("name")}


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


def _lima_vm(name: str, row: dict | None, listed: bool = True) -> Vm:
    """``listed`` False: `limactl list` didn't answer, so a VM it didn't name is unknown, not
    missing."""
    from .machine_backend import LimaBackend

    if row is None:
        return Vm(name, "lima", "missing" if listed else "unknown")
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


def _vm(instances: dict[str, dict] | None) -> Vm:
    """The bound project's VM. Lima from the one listing; podman machine by asking it."""
    name, backend = config.machine_name(), config.machine_backend()
    if backend == "lima":
        if instances is None:
            return _lima_vm(name, None, listed=False)
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


def _describe(
    checkout: Path, instances: dict[str, dict] | None, installed: str, fp: str
) -> Project:
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


def _left_adoptions() -> list[Leftover]:
    """Adoptions, of a main checkout or a worktree, whose checkout is gone or no longer holds a
    `foldyard.toml`. Only a checkout whose folder is gone is prunable: a box can delete the file,
    and forgetting the adoption then would make your next review a whole file, not a diff."""
    from . import configpin

    try:
        pins = sorted(configpin._pin_root().iterdir())
    except OSError:
        return []
    out = []
    for pin in pins:
        checkout, worktree = _adopted_where(pin)
        if not checkout or (Path(checkout) / "foldyard.toml").is_file():
            continue
        gone = not os.path.lexists(checkout)
        what = "worktree" if worktree else "checkout"
        out.append(
            Leftover(
                f"adopted config for {_short(checkout)}",
                f"the {what} is gone" if gone else "its folder no longer holds a foldyard.toml",
                "`fy prune`" if gone else "",
                kind="adoption",
                path=str(pin),
                prunable=gone,
            )
        )
    return out


def _adopted_where(pin: Path) -> tuple[str, str]:
    """The checkout an adoption folder's marker names, and its worktree (``""`` for main);
    ``("", "")`` when there's no readable marker."""
    from . import configpin

    try:
        doc = json.loads((pin / configpin.ADOPTED_MARKER).read_text())
    except (OSError, ValueError):
        return "", ""
    if not isinstance(doc, dict):
        return "", ""
    checkout, worktree = doc.get("checkout"), doc.get("worktree")
    return (
        checkout if isinstance(checkout, str) else "",
        worktree if isinstance(worktree, str) else "",
    )


def _unused_folders(claimed: set[str]) -> list[Leftover]:
    """Folders in ~/.foldyard that are neither foldyard's own nor the state folder of an adopted
    project: a project you stopped using (its `host.env` may still hold secrets), or something
    another tool put there. Never pruned. Not judged under `FOLDYARD_STATE_DIR`, which relocates
    one project's state, not the root."""
    if os.environ.get("FOLDYARD_STATE_DIR"):
        return []
    root = Path.home() / ".foldyard"
    try:
        entries = sorted(root.iterdir())
    except OSError:
        return []
    out = []
    for entry in entries:
        if entry.name in OWN_FOLDERS or entry.name in claimed:
            continue
        if entry.is_symlink() or not entry.is_dir():
            continue
        secrets = (entry / "host.env").is_file()
        out.append(
            Leftover(
                f"folder {_short(str(entry))}",
                f"{_size(_tree_size(entry))}{', holds host.env' if secrets else ''}; "
                "no adopted project uses it",
                kind="folder",
                path=str(entry),
            )
        )
    return out


def _tree_size(root: Path) -> int:
    """Bytes a folder's files hold on disk, links not followed."""
    total = 0
    for dirpath, _dirs, files in os.walk(root):
        for name in files:
            try:
                st = os.lstat(os.path.join(dirpath, name))
            except OSError:
                continue
            total += getattr(st, "st_blocks", 0) * 512 or st.st_size
    return total


def _base_vm(instances: dict[str, dict] | None) -> dict | None:
    from . import basevm

    row, stamp = (instances or {}).get(basevm.NAME), basevm.read_stamp()
    if row is None and stamp is None:
        return None
    age = basevm.age_days(stamp) if stamp else None
    if row:
        status = str(row.get("status"))
    else:  # stamped, but not in the listing, or no listing at all
        status = "missing" if instances is not None else "unknown"
    return {
        "name": basevm.NAME,
        "status": status,
        "baked_days_ago": round(age, 1) if age is not None else None,
        "disk_used": _disk_used(row.get("dir")) if row else None,
    }


def _unclaimed(
    instances: dict[str, dict], claimed: set[str], unresolved: list[str]
) -> list[Leftover]:
    """foldyard's Lima VMs that no adopted project names. foldyard records its boot script in
    every VM it starts (``machine._record_provisioning``), so a VM without one isn't foldyard's —
    your own Lima VMs — and stays out of this list. While an adopted project's VM can't be named
    (``unresolved``: its config doesn't read), any of these may be its, so none gets a delete."""
    from . import basevm

    out = []
    for name, row in sorted(instances.items()):
        if name in claimed or name == basevm.NAME:
            continue
        vm = _lima_vm(name, row)
        if not vm.foldyards:
            continue
        size = f", {_size(vm.disk_used)} on disk" if vm.disk_used else ""
        if unresolved:
            why = (
                "foldyard made it and no project names it, but it may be the VM of "
                + ", ".join(unresolved)
                + ", whose adopted config can't be read"
            )
            fix = ""
        else:
            why = "foldyard made it, and no adopted project uses it"
            fix = f"`limactl delete {'--force ' if vm.running else ''}{name}`"
        out.append(Leftover(f"VM {name} ({vm.status.lower()}{size})", why, fix, kind="vm"))
    return out


# ── the engine's side, from a child in each checkout ─────────────────────────────────────────


def here() -> dict:
    """This project's engine-side facts (the child's half of `fy ls`): its workspaces' boxes and
    stacks, and the agent sessions in those boxes. Never boots the VM: a stopped one is the
    reason they're missing."""
    from . import agents, devmode, stack

    if (reason := stack.machine_down()) is not None:
        return {"unavailable": reason}
    wss = devmode.workspaces()
    if not all(ws.get("containers") is not None for ws in wss):
        # Its boxes then read "none" and its agents 0: unknown, so nothing is said.
        return {"unavailable": "the engine didn't answer"}
    # devmode's read-only probe env, never `box.engine_env()`: that resolves the stack, which
    # writes `core.fileMode` into the checkout's .git/config.
    engine, env = config.engine(), devmode._engine_env()
    boxes = [
        agents.Box(
            f"{ws['project']}-devbox", "" if ws["name"] == "main" else ws["name"], engine, env
        )
        for ws in wss
        if ws.get("devbox")
    ]
    with ThreadPoolExecutor(max_workers=8) as pool:
        sessions = [s for got in pool.map(agents.sessions_in, boxes) for s in got]
    return {
        "unavailable": "",
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


def collect(*, ask_children: bool = True) -> Listing:
    """Every adopted project, what's left over, and the base VM. Without ``ask_children`` the
    engine-side facts are left unasked (`fy prune` needs only what's on disk)."""
    import foldyard

    from . import configpin, supervisor

    instances = _lima_instances()
    fingerprint = supervisor._code_fingerprint()
    projects = [
        _describe(c, instances, foldyard.__version__, fingerprint)
        for c in configpin.adopted_checkouts()
    ]
    # Below a project's floor its child would only be refused by it, saying nothing else.
    ask = [p for p in projects if p.vm.running and not p.below_floor] if ask_children else []
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(_ask, ask))
    claimed = {p.vm.name for p in projects if p.vm.backend == "lima"}
    unresolved = [p.project for p in projects if p.vm.name == "?"]
    unclaimed = _unclaimed(instances, claimed, unresolved) if instances is not None else []
    leftovers = unclaimed + _unused_folders({p.project for p in projects}) + _left_adoptions()
    return Listing(projects, leftovers, _base_vm(instances), lima_unavailable=instances is None)


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


_LIMA_SILENT = (
    "⚠ `limactl list` didn't answer: Lima VMs read as unknown, and none is listed as left over."
)


def render(listing: Listing, *, verbose: bool = False) -> str:
    if not listing.projects and not listing.leftovers and listing.base_vm is None:
        empty = (
            "No projects yet: a project is listed once its config is adopted on this computer "
            "(`fy up` in its checkout asks)."
        )
        # Nothing found is not nothing there when the search for left-over VMs never ran.
        return empty + (f"\n\n{_LIMA_SILENT}" if listing.lima_unavailable else "")
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
    if listing.lima_unavailable:
        lines += ["", _LIMA_SILENT]
    if listing.leftovers:
        lines += ["", "Left over (`fy prune` forgets adoptions; the rest is yours to remove):"]
        adoptions = [lo for lo in listing.leftovers if lo.kind == "adoption"]
        shown = (
            listing.leftovers
            if verbose
            else [lo for lo in listing.leftovers if lo not in adoptions]
        )
        for left in shown:
            lines.append(f"  {left.what}: {left.why}" + (f" — {left.fix}" if left.fix else ""))
        if adoptions and not verbose:
            lines += _adoption_counts(adoptions)
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


def _adoption_counts(adoptions: list[Leftover]) -> list[str]:
    gone = sum(lo.prunable for lo in adoptions)
    emptied = len(adoptions) - gone
    out = []
    if gone:
        out.append(
            f"  {_plural(gone, 'adoption')} whose checkout is gone — `fy prune` forgets them"
            " (`fy ls --verbose` lists them)"
        )
    if emptied:
        out.append(
            f"  {_plural(emptied, 'adoption')} whose folder no longer holds a foldyard.toml"
            " (`fy ls --verbose` lists them)"
        )
    return out


def _plural(n: int, noun: str) -> str:
    return f"{n} {noun}" + ("" if n == 1 else "s")


def to_json(listing: Listing, *, verbose: bool = False) -> str:
    doc = asdict(listing)
    doc["adoptions_left_over"] = sum(lo.kind == "adoption" for lo in listing.leftovers)
    if not verbose:
        doc["leftovers"] = [lo for lo in doc["leftovers"] if lo["kind"] != "adoption"]
    return json.dumps(doc, indent=2)


def ls(*, here_only: bool, as_json: bool, verbose: bool = False) -> int:
    if config.in_box():
        _err("✗ `fy ls` lists this computer's projects — run it on your computer, not in a box.")
        return 1
    if here_only:
        print(json.dumps(here()))
        return 0
    listing = collect()
    print(to_json(listing, verbose=verbose) if as_json else render(listing, verbose=verbose))
    return 0


def prune(*, yes: bool = False, ask: Callable[[str], bool] | None = None) -> int:
    """`fy prune`: forget the adoptions whose checkout is gone, after showing them and asking.
    What else is left over (VMs, state folders, an adoption whose folder only lost its
    `foldyard.toml`) is named with how to remove it, never removed: those hold data or secrets."""
    if config.in_box():
        _err("✗ `fy prune` tidies this computer's foldyard state — run it on your computer.")
        return 1
    leftovers = collect(ask_children=False).leftovers
    doomed = sorted((lo for lo in leftovers if lo.prunable), key=lambda lo: lo.what)
    rest = [lo for lo in leftovers if not lo.prunable]
    if rest:
        print("Not removed by `fy prune` (yours to decide):")
        for lo in rest:
            fix = f" — {lo.fix}" if lo.fix and lo.fix != "`fy prune`" else ""
            print(f"  {lo.what}: {lo.why}{fix}")
        print("")
    if not doomed:
        print("Nothing to prune: every adoption's checkout is still there.")
        return 0
    print(f"{_plural(len(doomed), 'adoption')} whose checkout is gone:")
    for lo in doomed:
        print(f"  {lo.what.removeprefix('adopted config for ')}")
    sys.stdout.flush()  # before any refusal on stderr, so a pipe keeps the order
    if not yes:
        if ask is None:
            if not sys.stdin.isatty():
                _err("✗ Nothing removed: there's no terminal to confirm on.")
                _err("  `fy prune --yes` removes them without asking.")
                return 1
            from .agents import _ask

            ask = _ask
        these = "this" if len(doomed) == 1 else "these"
        if not ask(f"Forget {these} {_plural(len(doomed), 'adoption')}? (y/N): "):
            print("Nothing removed.")
            return 0
    return _forget(doomed)


def _forget(doomed: list[Leftover]) -> int:
    from . import configpin

    forgot = kept = 0
    for lo in doomed:
        checkout, _ = _adopted_where(Path(lo.path))
        if not checkout or os.path.lexists(checkout):  # back (or changed) since it was listed
            kept += 1
            continue
        configpin.drop(Path(lo.path))
        forgot += 1
    print(f"✓ Forgot {_plural(forgot, 'adoption')}.")
    if kept:
        print(f"  Kept {kept}: changed since it was listed (its checkout is back?).")
    return 0
