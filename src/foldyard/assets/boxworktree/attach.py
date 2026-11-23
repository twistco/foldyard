"""foldyard's volume helper for a box-made worktree: a volume over a dir of a RUNNING box.

Run in a one-off container beside the box (``boxworktree._helper``), which shares the box's pid
namespace and nothing of the VM's: with the volumes mounted under ``/fy``, its shell opens
``/proc/1/ns/mnt`` (fd 3) and ``/fy`` (fd 4), then chroots into the box's root for the box's own
python, as ``python -I -`` with this file on stdin. Stdlib-only:

    attach <target>...     the volume at /fy/<n> over the nth target
    detach <target>...     take that volume off it again

A box's mounts are fixed at create and its root has no ``CAP_SYS_ADMIN``; this container has it in
the same user namespace, for the one call. Each target is an absolute path as the box sees it. One
JSON object on stdout: ``{"refused": <reason or null>, "results": {target: status}}``.

A target is resolved with ``openat2(RESOLVE_NO_SYMLINKS)``: a link at any component is refused. A
dir something ELSE is mounted on is left alone (``busy``): the read-only mask of a worktree your
computer has registered is never covered, nor a volume of the box's own.
"""

from __future__ import annotations

import ctypes
import errno
import json
import os
import struct
import sys

# The new mount API's numbers are the same on every architecture (the unified table from 424).
_NR = {"open_tree": 428, "move_mount": 429, "openat2": 437, "mount_setattr": 442}
AT_FDCWD = -100
AT_EMPTY_PATH = 0x1000
OPEN_TREE_CLONE = 1
MOVE_MOUNT_F_EMPTY_PATH = 0x4
MOVE_MOUNT_T_EMPTY_PATH = 0x40
MOUNT_ATTR_NOSUID, MOUNT_ATTR_NODEV = 0x2, 0x4
MS_PRIVATE = 1 << 18
RESOLVE_NO_MAGICLINKS, RESOLVE_NO_SYMLINKS = 0x02, 0x04
MNT_DETACH = 0x2
BOX_NS, VOLUMES = 3, 4  # the fds the container's shell hands over

# Statuses, per target.
ATTACHED = "attached"  # by this run
ALREADY = "already"  # the volume was there
DETACHED = "detached"
ABSENT = "absent"  # detach: the volume isn't on it
BUSY = "busy"  # something else is mounted there
MISSING = "missing"
SYMLINK = "symlink"
NOT_DIR = "not-a-dir"
INVALID = "invalid"


def valid_target(t: str) -> bool:
    """Absolute and normalised: no empty, ``.`` or ``..`` component, no trailing slash."""
    if not t.startswith("/") or t == "/" or "\x00" in t:
        return False
    return all(p not in ("", ".", "..") for p in t[1:].split("/"))


class Kernel:
    """The few calls this needs, so the logic below is testable without one. The volumes are
    cloned BEFORE the box's namespace is joined: they are mounted only in this container's."""

    def __init__(self, count: int) -> None:
        self._libc = ctypes.CDLL(None, use_errno=True)
        self._vols = [
            os.open(str(n), os.O_PATH | os.O_DIRECTORY, dir_fd=VOLUMES) for n in range(count)
        ]
        self._trees = [self._clone(fd) for fd in self._vols]
        os.setns(BOX_NS, os.CLONE_NEWNS)

    def _sys(self, name: str, *args: object) -> int:
        r = self._libc.syscall(_NR[name], *args)
        if r < 0:
            e = ctypes.get_errno()
            raise OSError(e, f"{name}: {os.strerror(e)}")
        return r

    def _clone(self, fd: int) -> int:
        tree = self._sys("open_tree", fd, b"", OPEN_TREE_CLONE | os.O_CLOEXEC | AT_EMPTY_PATH)
        attr = struct.pack("QQQQ", MOUNT_ATTR_NOSUID | MOUNT_ATTR_NODEV, 0, MS_PRIVATE, 0)
        buf = ctypes.create_string_buffer(attr, len(attr))
        self._sys("mount_setattr", tree, b"", AT_EMPTY_PATH, buf, len(attr))
        return tree

    def resolve(self, target: str) -> int:
        how = struct.pack(
            "QQQ",
            os.O_PATH | os.O_DIRECTORY | os.O_CLOEXEC,
            0,
            RESOLVE_NO_SYMLINKS | RESOLVE_NO_MAGICLINKS,
        )
        buf = ctypes.create_string_buffer(how, len(how))
        return self._sys("openat2", AT_FDCWD, target.encode(), buf, len(how))

    def is_volume(self, n: int, fd: int) -> bool:
        a, b = os.fstat(fd), os.fstat(self._vols[n])
        return (a.st_dev, a.st_ino) == (b.st_dev, b.st_ino)

    def mount_id(self, fd: int) -> int:
        with open(f"/proc/self/fdinfo/{fd}") as f:
            for line in f:
                key, _, value = line.partition(":")
                if key == "mnt_id":
                    return int(value)
        raise OSError(errno.EINVAL, "no mnt_id in fdinfo")

    def attach(self, n: int, fd: int) -> None:
        flags = MOVE_MOUNT_F_EMPTY_PATH | MOVE_MOUNT_T_EMPTY_PATH
        self._sys("move_mount", self._trees[n], b"", fd, b"", flags)

    def detach(self, fd: int) -> None:
        if self._libc.umount2(f"/proc/self/fd/{fd}".encode(), MNT_DETACH) != 0:
            e = ctypes.get_errno()
            raise OSError(e, f"umount: {os.strerror(e)}")

    def close(self, fd: int) -> None:
        os.close(fd)


def _resolve(k, target: str) -> tuple[int, str]:
    """``(fd, "")``, or ``(-1, status)``."""
    try:
        return k.resolve(target), ""
    except OSError as e:
        known = {errno.ELOOP: SYMLINK, errno.ENOENT: MISSING, errno.ENOTDIR: NOT_DIR}
        return -1, known.get(e.errno or 0, f"error: {e.strerror}")


def _mounted(k, target: str, fd: int) -> bool:
    """Whether ``target`` is itself a mount point: its mount isn't its parent's."""
    parent = k.resolve(target.rsplit("/", 1)[0] or "/")
    try:
        return k.mount_id(parent) != k.mount_id(fd)
    finally:
        k.close(parent)


def attach_one(k, n: int, target: str) -> str:
    fd, status = _resolve(k, target)
    if status:
        return status
    try:
        if k.is_volume(n, fd):
            return ALREADY
        if _mounted(k, target, fd):
            return BUSY
        k.attach(n, fd)
    finally:
        k.close(fd)
    again, status = _resolve(k, target)
    if status:
        return status
    try:
        return ATTACHED if k.is_volume(n, again) else "error: the dir moved meanwhile"
    finally:
        k.close(again)


def detach_one(k, n: int, target: str) -> str:
    fd, status = _resolve(k, target)
    if status:
        return status
    try:
        if not k.is_volume(n, fd):
            return BUSY if _mounted(k, target, fd) else ABSENT
        k.detach(fd)
        return DETACHED
    finally:
        k.close(fd)


def run(mode: str, targets: list[str], k) -> dict[str, str]:
    one = attach_one if mode == "attach" else detach_one
    results: dict[str, str] = {}
    for n, t in enumerate(targets):
        try:
            results[t] = one(k, n, t) if valid_target(t) else INVALID
        except OSError as e:
            results[t] = f"error: {e.strerror or e}"
    return results


def main(argv: list[str], kernel=Kernel) -> dict:
    if len(argv) < 2 or argv[0] not in ("attach", "detach"):
        return {"refused": "attach|detach <target>...", "results": {}}
    mode, *targets = argv
    try:
        return {"refused": None, "results": run(mode, targets, kernel(len(targets)))}
    except OSError as e:
        return {"refused": str(e.strerror or e), "results": {}}


if __name__ == "__main__":
    print(json.dumps(main(sys.argv[1:])))
