"""The two foldyard-owned host directories beside the checkout, one per direction (ADR-0033).

* **to-box** (``<state dir>/to-box``): written by your computer only, mounted READ-ONLY into the VM
  and, from there, into every box at :data:`TO_BOX_IN_BOX`. Read-only is worth something only where
  it is enforced OUTSIDE the guest — root in the guest can remount a guest-side ``ro`` — which on
  Lima means ``vz`` + virtiofs or ``qemu`` + 9p (:data:`ENFORCING_MOUNT_TYPE`); foldyard pins that
  ``mountType`` with the vmType and mounts no to-box on any other pair. Its first tenant is the
  guest monitor's signed file sink (``fy monitor hook add NAME --to-box``). It must never hold a
  secret: every box of the project can read it.
* **from-box** (``<state dir>/from-box``): box-writable, one slot per checkout — ``main/`` and
  ``worktrees/<name>/``, like the posture dirs, so a worktree named ``main`` can't collide. The box
  binds its agents' transcript dirs out into its own slot; the host reads them as untrusted input
  (the transcript sync's no-follow walk, anchored at the from-box root, whose parent — the state
  dir — the box can't reach). Slots are made with :func:`mountwrite.makedirs`, never
  ``Path.mkdir(parents=True)``: any box can plant a slot as a symlink before its first `fy box up`.

Both exist on the **lima** backend only. On podman-machine a mount-set change is a machine
RECREATE (`podman machine set` has no volume flag) and no provider enforces read-only outside the
guest, so nothing about that VM's mounts changes: transcripts stay bound out in the checkout.

Only the two subdirectories are mounted, never the state dir itself (``host.env``, the keys, the
allow-store): ``fy verify`` exempts exactly these two mountpoints. Stdlib + ``config`` only — the
plugins call it from ``box_args``, and the in-box ``fy verify`` reads :func:`vm_paths`."""

from __future__ import annotations

import os
import shutil
import stat
from pathlib import Path

from . import config, mountwrite

# Where the box sees the to-box (read-only bind). Fixed, so a tenant's box-side path is the same in
# every project.
TO_BOX_IN_BOX = "/run/foldyard/to-box"
# Baked into the box at create (`fy box up`): the two VM paths, for the in-box `fy verify` — it
# can't resolve your computer's state dir (FOLDYARD_STATE_DIR may move it). Unset ⇒ not mounted.
ENV_TO_BOX = "FY_TO_BOX"
ENV_FROM_BOX = "FY_FROM_BOX"

# vmType → the Lima mountType whose `writable: false` reaches a share server outside the guest
# (ADR-0033 § Read-only enforcement): vz's Virtualization.framework share (`readOnly`), QEMU's 9p
# server (`readonly=on`, EROFS for every write op). Not qemu + virtiofs (virtiofsd runs without
# `--readonly`), not krunkit, not reverse-sshfs.
ENFORCING_MOUNT_TYPE = {"vz": "virtiofs", "qemu": "9p"}


def enabled() -> bool:
    """Does this project's VM get the two mounts? The lima backend only (see the module doc)."""
    return config.machine_backend() == "lima"


def to_box_dir() -> Path:
    return config.state_dir() / "to-box"


def from_box_dir() -> Path:
    return config.state_dir() / "from-box"


def mount_type_for(vmtype: str) -> str:
    """The mountType foldyard pins for ``vmtype`` — the one that enforces read-only outside the
    guest — or ``""`` for a vmType with none (no to-box there, and nothing pinned)."""
    return ENFORCING_MOUNT_TYPE.get(vmtype, "")


def to_box_enforced(vmtype: str, mount_type: str) -> bool:
    """Is a read-only mount on this pair refused OUTSIDE the guest?"""
    want = mount_type_for(vmtype)
    return bool(want) and mount_type == want


def slot_rel(worktree: str = "") -> Path:
    """A checkout's slot under from-box: ``main`` for the main checkout, ``worktrees/<name>``."""
    if not worktree:
        return Path("main")
    # `fy worktree add`'s own rule: one plain path component. Anything else never had a checkout.
    if (
        worktree in (".", "..")
        or "/" in worktree
        or "\0" in worktree
        or Path(worktree).is_absolute()
    ):
        raise ValueError(f"worktree name {worktree!r} can't name a from-box slot")
    return Path("worktrees") / worktree


def claude_rel(worktree: str = "") -> Path:
    return slot_rel(worktree) / "claude" / "projects"


def codex_rel(worktree: str = "") -> Path:
    return slot_rel(worktree) / "codex" / "sessions"


def overlaps(a: Path, b: Path) -> bool:
    """Is one of ``a``/``b`` inside (or equal to) the other?"""
    return a == b or a.is_relative_to(b) or b.is_relative_to(a)


def nested(dirs: list[Path], mounts: list[Path]) -> list[tuple[Path, Path]]:
    """``(dir, mount)`` pairs where one of foldyard's two directories overlaps a writable mount.
    A read-only mount inside a writable one stays writable through the parent (Lima: "do not
    specify directories that overlap"), and a from-box inside the checkout is the checkout again —
    so `FOLDYARD_STATE_DIR` pointed into the checkout or the worktrees root is refused."""
    # Resolved first: a worktrees root that is a symlink to a directory holding the state dir
    # overlaps it all the same (FOLDYARD_STATE_DIR is resolved already; the roots may not be).
    return [(d, m) for d in dirs for m in mounts if overlaps(d.resolve(), m.resolve())]


def prepare() -> None:
    """Make the two roots on your computer (host-only: their parent, the state dir, is outside
    every mount). A VM can't start sharing a directory that doesn't exist."""
    for d in (to_box_dir(), from_box_dir()):
        d.mkdir(mode=0o700, parents=True, exist_ok=True)


def ensure_bind_source(path: str | Path) -> None:
    """Make a host directory a box binds in. Under the from-box root: :func:`mountwrite.makedirs`
    from that root — the box can plant any part of a slot as a symlink, and a following mkdir
    would create directories wherever it points. Anywhere else (the legacy in-checkout bind-out, an
    operator's ``DEVBOX_TRANSCRIPTS``): as before."""
    path = Path(path)
    root = from_box_dir()
    if path.is_relative_to(root) and path != root:
        mountwrite.makedirs(root, path.relative_to(root))
    else:
        path.mkdir(parents=True, exist_ok=True)


def remove_slot(worktree: str) -> str | None:
    """Delete a removed worktree's from-box slot (after its transcripts were archived). Never
    follows a link the box planted: the walk goes from the from-box root by fd, a slot that is a
    link is unlinked (not followed), and the tree itself is removed only where Python's ``rmtree``
    is fd-based (``rmtree.avoids_symlink_attacks``) — else refused. ``None`` on success or nothing
    there; otherwise why not. The main checkout's slot is never removed here."""
    if not worktree:
        raise ValueError("the main checkout's from-box slot is not a worktree's")
    rel = slot_rel(worktree)
    try:
        root = os.open(from_box_dir(), os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except FileNotFoundError:
        return None
    except OSError as e:
        return f"{from_box_dir()}: {e.strerror or e}"
    try:
        try:
            parent = os.open("worktrees", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=root)
        except FileNotFoundError:
            return None
        except OSError as e:
            return f"{from_box_dir() / rel.parent}: {e.strerror or e} (not followed)"
        try:
            try:
                st = os.stat(rel.name, dir_fd=parent, follow_symlinks=False)
            except FileNotFoundError:
                return None
            if not stat.S_ISDIR(st.st_mode):
                os.unlink(rel.name, dir_fd=parent)  # a planted link: unlinked, never followed
                return None
            if not shutil.rmtree.avoids_symlink_attacks:
                return (
                    f"{from_box_dir() / rel}: this platform's rmtree can follow a symlink planted "
                    "mid-delete — left in place; delete it by hand"
                )
            shutil.rmtree(rel.name, dir_fd=parent)
            return None
        except OSError as e:
            return f"{from_box_dir() / rel}: {e.strerror or e}"
        finally:
            os.close(parent)
    finally:
        os.close(root)


def vm_paths() -> tuple[str, str]:
    """``(from-box, to-box)`` as VM paths (== host paths), ``""`` for one that isn't mounted.
    In the box: what `fy box up` baked (:data:`ENV_FROM_BOX`, :data:`ENV_TO_BOX`). On your computer:
    both roots on the lima backend — `verify` exempts them by exact mountpoint and looks for the
    to-box in the VM's own mount table, so a pair with no to-box costs nothing."""
    if config.in_box():
        return os.environ.get(ENV_FROM_BOX, ""), os.environ.get(ENV_TO_BOX, "")
    if not enabled():
        return "", ""
    return str(from_box_dir()), str(to_box_dir())
