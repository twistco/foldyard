"""Masks over OTHER checkouts' dependency dirs in a dev box (issue #91).

Every box binds the main checkout and the whole worktrees root, so it sees every sibling checkout,
but ``[box].shadow_volumes`` shadows only its OWN checkout's dirs. An install run from main's box
in a worktree (or from a worktree's box in main) used to land on your computer's disk: slow over
the VM mount, shared with your own tools, and none of it seen by ``fy doctor``.

So each box mounts one small read-only volume over each declared dir of every OTHER checkout it
can see. An install there fails straight away with a read-only error, and the one file in the
volume (:data:`NOTE_FILE`) says where to run it: in that checkout's own box. The box's own
checkout keeps its real shadow volumes, unchanged. One volume per project serves every mask
(:func:`note_volume`), so the note is generic; foldyard rewrites it at every box create.

Which checkouts exist comes from the host's worktree registry when the box is created (never a
listing of the box-writable worktrees root). A box's mounts are fixed at creation, so a worktree
added later isn't masked in a box that was already running: ``fy worktree add``, ``fy box up``
and ``fy doctor`` say so (:func:`unmasked`) and never recreate a box themselves. A checkout the
host never registered (a plain ``git worktree add``) is masked in no box; the box's own doctor tells
it apart by the registry the host baked in at create (:func:`unregistered`).

Stdlib + ``config`` + ``worktree_registry`` only: the doctor row imports it."""

from __future__ import annotations

import json
import os
import posixpath
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path

from . import config, worktree_registry

# The one file in the note volume — what `ls` shows in a masked dir.
NOTE_FILE = "RUN-IT-IN-ITS-OWN-BOX"
# Where the filler container sees the note volume while foldyard writes it.
FILL_AT = "/fy-note"
# A mount target the engine can't parse unambiguously: `-v src:dst:ro` splits on ':', and an engine
# that reads mount options comma-separated (`--mount`) on ','. A worktree name may hold either.
_UNSAFE = frozenset(":,\n\r\x00")
# What the host bakes into every box at create: each registered worktree's port offset
# (``box._create_env``). Its names are the registry the box itself can't read (:func:`baked_names`).
SNAPSHOT_ENV = "FY_WORKTREE_OFFSETS"


def note_volume(prefix: str | None = None) -> str:
    """The project's one note volume. Not ``<prefix>-devbox-…``: `fy worktree remove` sweeps that
    prefix for a worktree's own shadow volumes, and this one is shared by every box."""
    return f"{prefix or config.project_prefix()}-elsewhere"


@dataclass(frozen=True)
class Mask:
    """One masked dir: ``path`` (absolute, as the box sees it) in checkout ``checkout`` (a worktree
    name, ``""`` for the main checkout)."""

    checkout: str
    path: str


def _norm(d: str) -> str | None:
    """A checkout-relative ``[box].shadow_volumes`` entry, normalised (``./a/./b`` → ``a/b``), or
    None for one that can't name a dir INSIDE a checkout: empty, any ``..``, or the checkout root
    itself (``.``, ``./.``) — a mask there would make a whole other checkout read-only."""
    rel = d.strip().strip("/")
    if not rel or ".." in rel.split("/"):
        return None
    rel = posixpath.normpath(rel)
    return None if rel == "." else rel


def _seen_at(path: Path, roots: Iterable[Path]) -> Path | None:
    """Where the box sees ``path`` (a registered worktree's REAL path): under the first of
    ``roots`` (the box's bind sources, mounted at their own paths) that holds it, by real path; None
    when no bind covers it — the box can't reach it, so there is nothing to mask."""
    real = Path(os.path.realpath(path))
    for root in roots:
        for base in (root, Path(os.path.realpath(root))):
            try:
                return root / real.relative_to(base)
            except ValueError:
                continue
    return None


def targets(
    own: str | Path,
    main: Path,
    wt_root: Path,
    worktrees: Mapping[str, Path],
    dirs: Iterable[str],
) -> tuple[list[Mask], list[str]]:
    """``(masks, refused)`` for a box whose own checkout is ``own``: each of ``dirs`` in every
    OTHER checkout the box binds — the main checkout, and each of ``worktrees`` (name → path)
    under the main checkout or ``wt_root``. ``refused`` lists targets whose path an engine can't
    take as a mount destination (:data:`_UNSAFE`); they stay unmasked and the caller says so."""
    rels = [r for r in (_norm(d) for d in dirs) if r]
    if not rels:
        return [], []
    own_real = os.path.realpath(own)
    own_seen = str(own).rstrip("/")
    others: list[tuple[str, Path]] = [("", main)]
    for name in sorted(worktrees):
        seen = _seen_at(worktrees[name], (main, wt_root))
        if seen is not None:
            others.append((name, seen))
    masks: list[Mask] = []
    refused: list[str] = []
    for name, path in others:
        if os.path.realpath(path) == own_real:
            continue  # its own checkout: shadowed for real, by its own volumes
        for rel in rels:
            target = f"{path}/{rel}"
            if own_seen == target or own_seen.startswith(target + "/"):
                continue  # the box's own checkout sits inside this dir — never cover it
            if _UNSAFE & set(target):
                refused.append(target)
            else:
                masks.append(Mask(name, target))
    return masks, refused


def run_args(volume: str, masks: Iterable[Mask]) -> list[str]:
    """The ``-v`` args mounting ``volume`` read-only over each mask."""
    out: list[str] = []
    for m in masks:
        out += ["-v", f"{volume}:{m.path}:ro"]
    return out


def baked_names() -> frozenset[str] | None:
    """In a box: the worktrees your computer's registry held when it created this box — the names
    in :data:`SNAPSHOT_ENV` (``box._create_env`` bakes every registered worktree's port offset).
    None when the box carries none, or none that parses: one created by a foldyard from before it.
    The host set it; nothing in the box re-reads the registry. For REPORTING only."""
    try:
        data = json.loads(os.environ.get(SNAPSHOT_ENV) or "null")
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    return frozenset(k for k in data if isinstance(k, str))


def _on_disk(main: Path) -> dict[str, Path]:
    """The checkouts under the worktrees root as the box sees them. Box-writable: REPORTING only."""
    wt_root = config.worktrees_root(main)
    try:
        entries = list(wt_root.iterdir()) if wt_root.is_dir() else []
    except OSError:
        return {}
    return {d.name: d for d in entries if d.is_dir() and (d / ".git").exists()}


def registered(main: Path) -> dict[str, Path]:
    """The worktrees a box created now would mask: the host's registry (name → path). In the box
    there is no registry, so the checkouts under the worktrees root that the host had registered
    when it created this box (:func:`baked_names`) stand in — every one of them in a box from
    before that snapshot. For REPORTING only (the doctor row); nothing is created from it."""
    if not config.in_box():
        return worktree_registry.checkouts(main)
    found = _on_disk(main)
    names = baked_names()
    return found if names is None else {n: p for n, p in found.items() if n in names}


def unregistered(main: Path) -> dict[str, Path]:
    """In a box: the checkouts under the worktrees root your computer had NOT registered when it
    created this box (a plain `git worktree add`, say). A box masks only registered worktrees, so
    this box doesn't mask them and a recreate alone wouldn't either: `fy worktree add <name>` on
    your computer registers one first. ``{}`` on your computer (its doctor's `worktrees` row names
    them) and in a box without the snapshot, which can't tell. REPORTING only, like
    :func:`registered`."""
    if not config.in_box() or (names := baked_names()) is None:
        return {}
    return {n: p for n, p in _on_disk(main).items() if n not in names}


def unmasked(mounts: object, masks: Iterable[Mask], volume: str) -> list[Mask]:
    """Of ``masks``, those a running box does NOT have: no read-only mount of ``volume`` at that
    destination in its inspected ``Mounts`` (a worktree added after the box was created). Only an
    explicit ``RW: false`` counts: a writable mount (or one the engine doesn't describe) would let
    an install land in the shared note volume, which is no mask."""
    have = {
        str(m.get("Destination") or "").rstrip("/")
        for m in (mounts if isinstance(mounts, list) else [])
        if isinstance(m, dict)
        and m.get("Type") == "volume"
        and m.get("Name") == volume
        and m.get("RW") is False
    }
    return [m for m in masks if m.path not in have]


def unshadowed(mounts: object, checkout: str, dirs: Iterable[str]) -> list[str]:
    """The box's OWN declared dirs that aren't a volume mount in its inspected ``Mounts`` — a
    ``[box].shadow_volumes`` entry adopted after the box was created."""
    vols = {
        str(m.get("Destination") or "").rstrip("/")
        for m in (mounts if isinstance(mounts, list) else [])
        if isinstance(m, dict) and m.get("Type") == "volume"
    }
    rels = [r for r in (_norm(d) for d in dirs) if r]
    return [r for r in rels if f"{checkout.rstrip('/')}/{r}" not in vols]


def note_text(main: Path, wt_root: Path) -> str:
    """The note in every masked dir. Generic — one volume serves every mask in the project — but
    exact: the two command forms, and where each checkout lives."""
    return f"""\
This directory belongs to ANOTHER checkout of this project than the one this dev box works in,
so foldyard mounted it read-only here. An install in it from this box (pnpm install, uv sync, …)
would land on your computer's disk, shared with your own tools, instead of in that checkout's
own shadow volume ([box].shadow_volumes) — so it fails here instead.

Run the command in that checkout's own box. From the directory you want it run in (`fy box exec`
runs it at your current directory, in the box you name):

  a worktree ({wt_root}/<name>/…):
      WORKTREE=<name> fy box exec '<command>'
  the main checkout ({main}/…):
      WORKTREE= fy box exec '<command>'

That box must be running. If it isn't, start it on your computer (not in a box):

      WORKTREE=<name> fy box up      (a worktree)
      fy box up                      (the main checkout)

Or work in that checkout from its own box all along: WORKTREE=<name> fy box shell.
"""


def fill_script() -> str:
    """The filler container's script: the note from stdin, readable by every box user, swapped in
    whole. The stale temp file from an interrupted run goes first: it is 0444, and the filler's
    root has no CAP_DAC_OVERRIDE to write through that."""
    note = f"{FILL_AT}/{NOTE_FILE}"
    tmp = f"{note}.tmp"
    return f"rm -f {tmp} && cat > {tmp} && chmod 0444 {tmp} && mv -f {tmp} {note}"
