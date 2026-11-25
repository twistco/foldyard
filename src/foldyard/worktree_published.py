"""Which worktrees your computer registered, published in the to-box for its boxes to read.

The registry (:mod:`foldyard.worktree_registry`) is your computer's own and no box sees it. All a
box had was what it was created with, so it couldn't tell a worktree registered since from a plain
``git worktree add`` one, and ``fy worktree remove`` in a box refused both. This is the registry's
names, with each worktree's pooled ports and port offset (for a box made before it, which would
otherwise refuse it: ``stack._pooled``; what a box was given at create comes first), one file per
main checkout in the to-box (ADR-0033), which a box can only read. Written by ``fy worktree add`` /
``remove`` and, when it differs, by each supervisor tick.

A box reads no list as UNKNOWN, never as "none registered": there may be no to-box, or the foldyard
on your computer may be from before this. It guides a command in the box, and nothing on your
computer is decided from it."""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import os
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path

from . import boxmounts, config, worktree_registry

_DIR = "registered-worktrees"
_LOOKS, _WAIT_S = 4, 0.5  # a file replaced on your computer is missing in the guest for ~1 s (vz)


def path(main: Path, to_box: Path) -> Path:
    """``main``'s list under ``to_box``. By the checkout's path, which a box sees at the same one:
    two clones of a project share a state dir, so a to-box."""
    return to_box / _DIR / f"{hashlib.sha256(str(main).encode()).hexdigest()[:16]}.json"


@dataclass(frozen=True)
class Facts:
    names: frozenset[str]
    ports: dict[str, dict[str, int]] = field(default_factory=dict)  # absent: not known
    offsets: dict[str, int] = field(default_factory=dict)


def _allocations() -> tuple[dict, dict]:
    """Each registered worktree's pooled ports and offset. Empty when they can't be worked out (a
    config that doesn't parse, say): a box then refuses one it wasn't made with, as without this."""
    from . import stack

    try:
        return stack.worktree_ports(), stack.worktree_offsets()
    except Exception:
        return {}, {}


def publish(main: Path) -> None:
    """Write ``main``'s registered worktrees to the to-box, if that changed. On your computer only,
    and only into a to-box that is there. Best-effort: where it can't be written the old list is
    taken away, as a stale one would call a worktree registered since unregistered."""
    to_box = boxmounts.to_box_dir()
    if config.in_box() or not to_box.is_dir():
        return
    file = path(main, to_box)
    tmp = file.with_suffix(f".{os.getpid()}.tmp")
    try:
        with _lock(main):
            _write(main, file, tmp)
    except OSError:
        for stale in (tmp, file):
            try:
                stale.unlink(missing_ok=True)
            except OSError:
                pass


@contextlib.contextmanager
def _lock(main: Path) -> Iterator[None]:
    """One publisher at a time, from reading the registry to replacing the file: the supervisor's
    older list must not land on the one a `fy worktree add` just wrote. Beside the registry, which
    no box sees."""
    store = worktree_registry.store_file(main)
    store.parent.mkdir(parents=True, exist_ok=True)
    with open(store.with_name(f"{store.stem}.published.lock"), "a", encoding="utf-8") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        yield


def _write(main: Path, file: Path, tmp: Path) -> None:
    if not worktree_registry.readable(main):
        raise OSError("the registry can't be read")  # unknown, so no list
    ports, offsets = _allocations()
    names = worktree_registry.names(main)
    data = {
        "main": str(main),
        "registered": names,
        "ports": {n: ports[n] for n in names if n in ports},
        "offsets": {n: offsets[n] for n in names if n in offsets},
    }
    text = json.dumps(data, indent=2) + "\n"
    if file.is_file() and file.read_text() == text:
        return
    file.parent.mkdir(exist_ok=True)
    tmp.write_text(text)
    os.replace(tmp, file)


def _ints(data: object) -> dict[str, int] | None:
    """``data`` as name → int, or None when any of it isn't."""
    if not isinstance(data, dict):
        return None
    got: dict[str, int] = {}
    for key, value in data.items():
        if not isinstance(key, str) or not isinstance(value, int) or isinstance(value, bool):
            return None
        got[key] = value
    return got


def _parsed(data: object, main: Path) -> Facts | None:
    if not isinstance(data, dict) or data.get("main") != str(main):
        return None
    listed = data.get("registered")
    if not isinstance(listed, list):
        return None
    names = frozenset(n for n in listed if isinstance(n, str))
    if len(names) != len(listed):
        return None
    ports: dict[str, dict[str, int]] = {}
    raw = data.get("ports")
    for name, entry in (raw if isinstance(raw, dict) else {}).items():
        # One with any value that isn't a port is unknown, never on its band.
        got = _ints(entry)
        if isinstance(name, str) and got is not None and all(0 < p < 65536 for p in got.values()):
            ports[name] = got
    return Facts(names, ports, _ints(data.get("offsets")) or {})


def facts(main: Path) -> Facts | None:
    """In a box: what your computer has published for ``main``, or None for unknown."""
    file = path(main, Path(boxmounts.TO_BOX_IN_BOX))
    for look in range(_LOOKS):
        if look:
            time.sleep(_WAIT_S)
        try:
            return _parsed(json.loads(file.read_text()), main)
        except FileNotFoundError:
            if not file.parent.is_dir():
                return None  # none was ever published here
        except (OSError, ValueError):
            return None
    return None


def read(main: Path) -> frozenset[str] | None:
    """In a box: the names your computer has registered for ``main``, or None for unknown."""
    known = facts(main)
    return None if known is None else known.names
