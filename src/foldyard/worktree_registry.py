"""The host's own record of which worktrees belong to a project — worktree name → real path.

Nothing on the mount can say this. The worktrees root is box-writable and the box holds the
engine socket, so "a dir with ``.git`` + a running ``<prefix>-<name>-devbox``" — what the host
used to count as a live worktree — is something the box can manufacture: a stray dir, a symlink
to another checkout on the host (whose ADOPTED config and state dir would then be reconciled for
a box-controlled worktree), or a ``[machine].worktrees_root`` pointed somewhere else entirely.

So the host keeps the list itself. Only ``fy worktree add`` (host-only) writes it; ``fy worktree
remove`` drops the entry. An entry counts while its directory is still the real, non-symlinked
path that was recorded and still holds a ``.git``. The store is keyed by the MAIN checkout's real
path — like :func:`foldyard.configpin.pin_dir`, never by anything the config declares.

It also records each registered worktree's PORT OFFSET (:func:`offsets`), in a file beside the
registry rather than in it: an older supervisor still running after an editable upgrade reads the
registry as name → path and drops anything else.

Stdlib-only: the supervisor's reconcile tick reads it."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
from collections.abc import Callable, Collection, Iterable, Mapping
from pathlib import Path

SLOTS = range(1, 90)  # a worktree's offsets: 0 is main's, and past 89 is the next daemon's span


def _root() -> Path:
    """Where every project's registry lives. ``FOLDYARD_WORKTREE_REGISTRY`` wins (tests); else
    beside the adopted-config store, under the same base (see ``configpin._pin_root``)."""
    if env := os.environ.get("FOLDYARD_WORKTREE_REGISTRY"):
        return Path(env).expanduser()
    base = os.environ.get("FOLDYARD_STATE_DIR")
    return (Path(base).expanduser().resolve() if base else Path.home() / ".foldyard") / "worktrees"


def store_file(main: Path) -> Path:
    """The registry file for the project whose main checkout is ``main``. The basename is only
    there to make the dir browsable; the hash of the real path is what identifies it."""
    real = os.path.realpath(main)
    key = hashlib.sha256(real.encode()).hexdigest()[:16]
    name = re.sub(r"[^A-Za-z0-9_.-]+", "-", Path(real).name) or "repo"
    return _root() / f"{name}-{key}.json"


def _load(main: Path) -> dict[str, str]:
    """The raw entries. A missing or damaged file registers NOTHING — failing closed means the
    operator is asked to re-register, never that the host acts on a dir it can't vouch for."""
    try:
        data = json.loads(store_file(main).read_text())
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {k: v for k, v in data.items() if isinstance(k, str) and isinstance(v, str)}


def readable(main: Path) -> bool:
    """Whether the registry says who is registered: there is none yet, or it reads whole. Where it
    doesn't, :func:`names` is empty, which must not be told as "nothing is registered"."""
    try:
        data = json.loads(store_file(main).read_text())
    except FileNotFoundError:
        return True
    except (OSError, ValueError):
        return False
    return isinstance(data, dict) and all(isinstance(v, str) for v in data.values())


def _save(main: Path, entries: dict[str, str]) -> None:
    path = store_file(main)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(entries, indent=2, sort_keys=True) + "\n")
    os.replace(tmp, path)


def _is_real(path: Path) -> bool:
    """``path`` is itself its real path (no symlink anywhere along it) and holds a ``.git``."""
    return os.path.realpath(path) == str(path) and (path / ".git").exists()


def register(main: Path, name: str, path: Path) -> None:
    """Record ``path`` as worktree ``name``, by its REAL path. Refuses ``path`` itself being a
    symlink — that is the component the box can swap (an ancestor outside the worktrees root is the
    operator's own layout, and resolving it here keeps the later :func:`checkouts` check exact)."""
    if path.is_symlink():
        raise ValueError(f"{path} is a symlink — register the real directory")
    real = Path(os.path.realpath(path))
    if not _is_real(real):
        raise ValueError(f"{path} is not a checkout (no .git)")
    entries = _load(main)
    entries[name] = str(real)
    _save(main, entries)


def unregister(main: Path, name: str) -> None:
    entries = _load(main)
    if entries.pop(name, None) is not None:
        _save(main, entries)


def names(main: Path) -> list[str]:
    """Every registered worktree's name — even one whose checkout is gone."""
    return sorted(_load(main))


def is_recorded(main: Path, name: str) -> bool:
    """Is there an entry for ``name`` at all — even one whose checkout is gone? (``fy worktree
    remove`` clears those too.)"""
    return name in _load(main)


def checkouts(main: Path) -> dict[str, Path]:
    """The registered worktrees the host may act on: name → recorded path, for every entry whose
    directory is still exactly what was recorded."""
    return {name: Path(p) for name, p in _load(main).items() if _is_real(Path(p))}


def allocate(
    names: Iterable[str],
    recorded: Mapping[str, int],
    pinned: Mapping[str, int],
    hashed: Callable[[str], int],
) -> dict[str, int]:
    """The offset each of ``names`` not in ``pinned`` holds: what's ``recorded`` for it while that
    is still its own, else its ``hashed`` slot while that is free, else a free slot. Pure.

    A worktree's offset was its hashed slot, unrecorded, and two names on one slot shared every
    port (Tangible, 2026-10-26). Taking the hashed slot first means a worktree that never collided
    keeps the ports it always had, and a box created before this keeps working; only the second of
    a colliding pair — by name — moves. Everyone whose slot is free claims it before anyone who
    has to move picks a slot, so a move never lands on another worktree's hashed slot. Pinned
    values are never handed out, and a recorded one a pin has since taken moves. Past 89 names
    some are left out, and the caller falls back to the hash."""
    unpinned = sorted(n for n in names if n not in pinned)
    held = set(pinned.values())
    got: dict[str, int] = {}
    for name in unpinned:
        mine = recorded.get(name)
        if (
            isinstance(mine, int)
            and not isinstance(mine, bool)
            and mine in SLOTS
            and mine not in held
        ):
            got[name] = mine
            held.add(mine)
    fresh = [n for n in unpinned if n not in got]
    want = {n: hashed(n) for n in fresh}
    for name in fresh:
        if want[name] in SLOTS and want[name] not in held:
            got[name] = want[name]
            held.add(want[name])
    for name in (n for n in fresh if n not in got):
        start = want[name] if want[name] in SLOTS else SLOTS[0]
        ring = [*range(start, SLOTS[-1] + 1), *range(SLOTS[0], start)]
        free = next((s for s in ring if s not in held), None)
        if free is None:
            break
        got[name] = free
        held.add(free)
    return got


def offsets_file(main: Path) -> Path:
    store = store_file(main)
    return store.with_name(f"{store.stem}.offsets.json")


def _read_offsets(text: str) -> dict[str, int]:
    try:
        data = json.loads(text or "{}")
    except ValueError:
        return {}
    if not isinstance(data, dict):
        return {}
    return {
        k: v
        for k, v in data.items()
        if isinstance(k, str) and isinstance(v, int) and not isinstance(v, bool)
    }


def offsets(main: Path, pinned: Mapping[str, int], hashed: Callable[[str], int]) -> dict[str, int]:
    """Every registered worktree's port offset — its pin, else the one recorded for it, recording
    one (:func:`allocate`) for any that has none: one just registered, or every worktree the first
    time a foldyard that records them runs. An entry whose worktree was unregistered is dropped,
    freeing its slot. Read without the lock while nothing needs writing, which is every call but
    the few that allocate; flock-guarded when something does, so two ``fy`` runs can't hand one
    slot to two worktrees."""
    names = sorted(_load(main))
    path = offsets_file(main)
    try:
        recorded = _read_offsets(path.read_text())
    except OSError:
        recorded = {}
    got = allocate(names, recorded, pinned, hashed)
    if got != recorded:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a+", encoding="utf-8") as fh:
            fcntl.flock(fh, fcntl.LOCK_EX)
            fh.seek(0)
            recorded = _read_offsets(fh.read())
            got = allocate(names, recorded, pinned, hashed)
            if got != recorded:
                fh.seek(0)
                fh.truncate()
                json.dump(got, fh, indent=2, sort_keys=True)
                fh.write("\n")
    return {**got, **{n: v for n, v in pinned.items() if n in names}}


# ── pooled ports: a [ports] key whose worktrees take a port from a fixed list ─────────────────
#
# Recorded per worktree in a third file beside the registry (an older supervisor reads the registry
# as name → path, and the offsets file as name → int). Written only by `fy worktree add`; a record
# whose worktree is no longer registered is ignored, and dropped at the next claim.


def pick_ports(
    pools: Mapping[str, range],
    taken: Mapping[str, Collection[int]],
    mine: Mapping[str, int],
) -> dict[str, int] | None:
    """A port from each of ``pools`` that isn't ``taken``: the worktree's own (``mine``) while it is
    still in the pool and free, else the lowest free one. ``None`` when any pool is full — all the
    keys or none, so a worktree never publishes some ports from a pool and the rest from nowhere.
    Pure."""
    got: dict[str, int] = {}
    for key, pool in pools.items():
        held = taken.get(key, ())
        own = mine.get(key)
        if own is not None and own in pool and own not in held:
            got[key] = own
            continue
        free = next((p for p in pool if p not in held), None)
        if free is None:
            return None
        got[key] = free
    return got


def ports_file(main: Path) -> Path:
    store = store_file(main)
    return store.with_name(f"{store.stem}.ports.json")


def _read_ports(text: str) -> dict[str, dict[str, int]]:
    try:
        data = json.loads(text or "{}")
    except ValueError:
        return {}
    if not isinstance(data, dict):
        return {}
    return {
        name: {
            k: v
            for k, v in ports.items()
            if isinstance(k, str) and isinstance(v, int) and not isinstance(v, bool)
        }
        for name, ports in data.items()
        if isinstance(name, str) and isinstance(ports, dict)
    }


def recorded_ports(main: Path) -> dict[str, dict[str, int]]:
    """Each registered worktree's pooled ports, as ``fy worktree add`` recorded them. A worktree
    with none publishes on its band (it was registered before the pool)."""
    try:
        recorded = _read_ports(ports_file(main).read_text())
    except OSError:
        return {}
    names = set(_load(main))
    return {name: ports for name, ports in recorded.items() if name in names and ports}


def claim_ports(
    main: Path,
    name: str,
    pools: Mapping[str, range],
    band: Mapping[str, Collection[int]],
) -> dict[str, int] | None:
    """Record a port from each of ``pools`` for registered worktree ``name`` (:func:`pick_ports`)
    and return them, or ``None``, recording nothing, when a pool is full. ``band`` holds the ports
    other worktrees publish on without a record — registered before the pool, so on their band,
    which may lie inside it. flock-guarded: two ``fy worktree add`` can't take one port.

    The lock is a file of its own, and the record is replaced whole (a temp file and
    ``os.replace``): :func:`recorded_ports` reads without the lock, and a record truncated in place
    read as ``{}`` for a moment — every worktree back on its band. Who is registered is read once
    the lock is held, so a claim finished while this one waited is never dropped as unregistered."""
    path = ports_file(main)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path.with_name(f"{path.name}.lock"), "a", encoding="utf-8") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        names = set(_load(main))
        try:
            text = path.read_text()
        except OSError:
            text = ""
        recorded = {n: p for n, p in _read_ports(text).items() if n in names}
        taken = {key: set(band.get(key, ())) for key in pools}
        for other, ports in recorded.items():
            if other != name:
                for key, port in ports.items():
                    if key in taken:
                        taken[key].add(port)
        got = pick_ports(pools, taken, recorded.get(name, {}))
        if got is None:
            return None
        recorded[name] = {**recorded.get(name, {}), **got}
        tmp = path.with_name(f"{path.name}.tmp")
        try:
            tmp.write_text(json.dumps(recorded, indent=2, sort_keys=True) + "\n")
            os.replace(tmp, path)
        finally:
            tmp.unlink(missing_ok=True)
    return got


def unregistered(main: Path, wt_root: Path) -> list[str]:
    """Checkout dirs under ``wt_root`` the host does NOT recognise — for REPORTING only (the doctor
    row, the supervisor's log line), so a worktree that went quiet says why. Never a list to act
    on: it is exactly the box-writable evidence the registry exists to stop trusting."""
    if not wt_root.is_dir():
        return []
    known = {os.path.realpath(p) for p in checkouts(main).values()}
    return sorted(
        d.name
        for d in wt_root.iterdir()
        if d.is_dir() and (d / ".git").exists() and os.path.realpath(d) not in known
    )
