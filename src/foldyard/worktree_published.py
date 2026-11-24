"""Which worktrees your computer registered, published in the to-box for its boxes to read.

The registry (:mod:`foldyard.worktree_registry`) is your computer's own and no box sees it. All a
box had was what it was created with, so it couldn't tell a worktree registered since from a plain
``git worktree add`` one, and ``fy worktree remove`` in a box refused both. This is the registry's
NAMES, one file per main checkout in the to-box (ADR-0033), which a box can only read. Written by
``fy worktree add`` / ``remove`` and, when it differs, by each supervisor tick.

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
from pathlib import Path

from . import boxmounts, config, worktree_registry

_DIR = "registered-worktrees"
_LOOKS, _WAIT_S = 4, 0.5  # a file replaced on your computer is missing in the guest for ~1 s (vz)


def path(main: Path, to_box: Path) -> Path:
    """``main``'s list under ``to_box``. By the checkout's path, which a box sees at the same one:
    two clones of a project share a state dir, so a to-box."""
    return to_box / _DIR / f"{hashlib.sha256(str(main).encode()).hexdigest()[:16]}.json"


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
    data = {"main": str(main), "registered": worktree_registry.names(main)}
    text = json.dumps(data, indent=2) + "\n"
    if file.is_file() and file.read_text() == text:
        return
    file.parent.mkdir(exist_ok=True)
    tmp.write_text(text)
    os.replace(tmp, file)


def read(main: Path) -> frozenset[str] | None:
    """In a box: the names your computer has registered for ``main``, or None for unknown."""
    file = path(main, Path(boxmounts.TO_BOX_IN_BOX))
    for look in range(_LOOKS):
        if look:
            time.sleep(_WAIT_S)
        try:
            data = json.loads(file.read_text())
        except FileNotFoundError:
            if not file.parent.is_dir():
                return None  # none was ever published here
            continue
        except (OSError, ValueError):
            return None
        names = data.get("registered") if isinstance(data, dict) else None
        if not isinstance(names, list) or data.get("main") != str(main):
            return None
        known = frozenset(n for n in names if isinstance(n, str))
        return known if len(known) == len(names) else None
    return None
