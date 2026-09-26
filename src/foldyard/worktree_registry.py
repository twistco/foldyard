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

Stdlib-only: the supervisor's reconcile tick reads it."""

from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path


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


def is_recorded(main: Path, name: str) -> bool:
    """Is there an entry for ``name`` at all — even one whose checkout is gone? (``fy worktree
    remove`` clears those too.)"""
    return name in _load(main)


def checkouts(main: Path) -> dict[str, Path]:
    """The registered worktrees the host may act on: name → recorded path, for every entry whose
    directory is still exactly what was recorded."""
    return {name: Path(p) for name, p in _load(main).items() if _is_real(Path(p))}


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
