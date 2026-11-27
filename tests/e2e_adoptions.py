"""What the live modules adopted on your computer, forgotten when each module ends.

A module that drives the real CLI adopts its throwaway copy of the example into the REAL adoption
root (`~/.foldyard/adopted/`), as an operator's `fy config adopt` would, and its `fy worktree add`
adopts each worktree beside it. Nothing forgot those, and pytest deletes its old temp folders, so
`fy ls` came to list 82 adoptions whose checkout was gone (2026-11-11).

So `test_e2e._adopt_on_host` records the folder each copy sits in, and conftest's
`forget_live_adoptions` calls :func:`forget_recorded` after the module's own teardown (its
`fy down`). Every adoption whose checkout lies under a recorded folder goes: the copy's and its
worktrees'. A supervisor still running from the copy then logs that nothing is adopted and runs
nothing; the next module's `fy up` replaces it.

The host tier makes every copy at one path (tests/e2e_host.py), so what your computer keys by that
path (adoptions, the worktree registry) is cleared before each new copy as well
(:func:`forget_checkout_state`).

This reads the real root itself: conftest's `isolated_config_pin` points `configpin.pin_dir` at a
per-test folder in this process, live modules included.
"""

from __future__ import annotations

import json
import shutil
import tempfile
from pathlib import Path

from foldyard import configpin

# The folders this module's copies were made in.
_bases: set[Path] = set()


def record(repo: Path) -> None:
    """Remember the folder ``repo`` was copied into, so its adoptions are forgotten later."""
    base = repo.resolve().parent
    # A copy straight under your home would make the home the folder to clear, and every
    # project of yours with it; one straight in the temp folder, every other run's adoptions.
    shared = {Path.home(), Path("/"), Path(tempfile.gettempdir()), Path("/tmp")}
    if base in {p.resolve() for p in shared}:  # not `assert`: `python -O` would drop the guard
        raise AssertionError(f"no copy may sit in {base}")
    _bases.add(base)


def _checkout(pin: Path) -> Path | None:
    try:
        doc = json.loads((pin / configpin.ADOPTED_MARKER).read_text())
    except (OSError, ValueError):
        return None
    checkout = doc.get("checkout") if isinstance(doc, dict) else None
    return Path(checkout).resolve() if isinstance(checkout, str) and checkout else None


def _drop(pin: Path) -> None:
    """As `configpin.forget` does: a link is unlinked, never followed; else the marker first."""
    if pin.is_symlink():
        pin.unlink()
        return
    (pin / configpin.ADOPTED_MARKER).unlink(missing_ok=True)
    shutil.rmtree(pin)


def forget_recorded(root: Path | None = None) -> list[Path]:
    """Forget every adoption under ``root`` (the real one by default) whose checkout lies in a
    recorded folder, then the record. Returns what was dropped."""
    bases = set(_bases)
    _bases.clear()
    if not bases:
        return []
    try:
        pins = sorted((root or configpin._pin_root()).iterdir())
    except OSError:
        return []
    dropped = []
    for pin in pins:
        checkout = _checkout(pin)
        if checkout and any(checkout.is_relative_to(base) for base in bases):
            _drop(pin)
            dropped.append(pin)
    return dropped


def forget_checkout_state(checkout: Path, root: Path | None = None) -> list[Path]:
    """Everything your computer keys by ``checkout``'s path, gone: its adoptions and its
    worktrees' (under ``root``, the real adoption root by default), and its worktree registry with
    the offsets and pooled ports recorded beside it. For a copy made where an earlier one was (the
    host tier's one checkout, tests/e2e_host.py): that state outlives the files, so a later module's
    `fy worktree add feat` found an earlier module's `feat` already registered. Nothing keyed by
    another path is touched. Returns what went."""
    from foldyard import worktree_registry

    worktrees = Path(f"{checkout}-worktrees").resolve()
    mine = checkout.resolve()
    gone: list[Path] = []
    try:
        pins = sorted((root or configpin._pin_root()).iterdir())
    except OSError:
        pins = []
    for pin in pins:
        found = _checkout(pin)
        if found is not None and (found == mine or found.is_relative_to(worktrees)):
            _drop(pin)
            gone.append(pin)
    store = worktree_registry.store_file(checkout)
    try:
        beside = sorted(store.parent.iterdir())
    except OSError:
        beside = []
    for path in beside:  # the registry, and its .offsets / .ports / lock files: one checkout's
        if path.name == store.name or path.name.startswith(f"{store.stem}."):
            path.unlink(missing_ok=True)
            gone.append(path)
    return gone
