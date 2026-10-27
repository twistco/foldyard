"""foldyard's mask injector: a read-only mask over a RUNNING box's view of another checkout's dir.

Run IN THE GUEST, as the VM user (never root), by ``python3 -I -`` with this file on stdin —
``crossinject`` on your computer sends it over the backend's ssh. foldyard's own packaged code in
the VM, stdlib-only:

    inject <pid> <container id> <source> <target>...
    check  <pid> <container id> <source> <target>...

``pid`` is the box's init (``podman inspect --format '{{.State.Pid}}'``), its parent must be the
conmon of ``container id``; ``source`` is the note volume's ``_data`` dir as podman sees it;
each ``target`` an absolute path as the box sees it. One JSON object on stdout: ``{"refused":
<reason or null>, "detail": ..., "results": {target: status}}``.

A box's mounts are fixed at create, and mount propagation never reaches a running crun container
(its binds come up private). So this joins the BOX's user namespace (the VM user owns it, so
every capability in it is ours) and its conmon's mount namespace, where the volume is visible;
clones the volume's mount there (``open_tree``), makes the clone read-only, nosuid, nodev and
private (``mount_setattr``); then, in the box's own mount namespace, resolves the target with
``openat2(RESOLVE_NO_SYMLINKS)`` — a symlink at any component is refused, never followed — and
attaches the clone on it (``move_mount``). The box's root has no ``CAP_SYS_ADMIN`` in that
namespace, so it can't unmount or remount it, exactly as with a create-time ``:ro`` mask.

The box can rename the directory between the resolve and the attach; the mount then sits on the
renamed directory. So every attach is checked by resolving the target again: unless it resolves to
the very mount just made (its mount id), that mount is detached again and the target reported
``moved``. ``check`` asks the same question without changing anything: does the target resolve,
without a symlink, to the volume (same device and inode) on a read-only mount?

Refusals (``refused``): ``usage``, ``arch``, ``stale`` (the pid is not that container's init),
``rootful`` (the box's user namespace isn't the VM user's: a rootful engine would need root),
``enosys`` (a kernel without the new mount API — 5.12 or later has every call used here).
"""

from __future__ import annotations

import ctypes
import errno
import fcntl
import itertools
import json
import os
import platform
import struct
import sys
import time

# The new mount API's numbers are the same on every architecture (the unified table from 424).
_NR = {"open_tree": 428, "move_mount": 429, "openat2": 437, "mount_setattr": 442}
_ARCHES = ("x86_64", "aarch64")
AT_FDCWD = -100
AT_EMPTY_PATH = 0x1000
OPEN_TREE_CLONE = 1
MOVE_MOUNT_F_EMPTY_PATH = 0x4
MOVE_MOUNT_T_EMPTY_PATH = 0x40
MOUNT_ATTR_RDONLY, MOUNT_ATTR_NOSUID, MOUNT_ATTR_NODEV = 0x1, 0x2, 0x4
MS_PRIVATE = 1 << 18
RESOLVE_NO_MAGICLINKS, RESOLVE_NO_SYMLINKS = 0x02, 0x04
MNT_DETACH = 0x2
NS_GET_OWNER_UID = 0xB704  # _IO(0xb7, 0x4)
ST_RDONLY = 1
MISSING_WAIT_S = 5.0  # the host just made the dir: a guest over virtiofs may not see it yet
MISSING_STEP_S = 0.25

# Statuses, per target.
MASKED = "masked"  # already masked: the volume, read-only
INJECTED = "injected"  # masked by this run
UNMASKED = "unmasked"  # check: not the volume
WRITABLE = "writable"  # the volume, but writable: no mask
MISSING = "missing"
SYMLINK = "symlink"  # a symlink somewhere on the path: refused
NOT_DIR = "not-a-dir"
MOVED = "moved"  # renamed away during the attach; the mount made was detached again
MOVED_KEPT = "moved-kept"  # …and couldn't be detached: it sits on the renamed dir
INVALID = "invalid"  # not an absolute, normalised path


class Refused(Exception):
    """The whole run can't go ahead: ``reason`` is one of the codes in the module docstring."""

    def __init__(self, reason: str, detail: str = "") -> None:
        super().__init__(reason, detail)
        self.reason, self.detail = reason, detail


def valid_target(t: str) -> bool:
    """Absolute and normalised: no empty, ``.`` or ``..`` component, no trailing slash."""
    if not t.startswith("/") or t == "/" or "\x00" in t:
        return False
    return all(p not in ("", ".", "..") for p in t[1:].split("/"))


def parse(argv: list[str]) -> tuple[str, int, str, str, list[str]]:
    if len(argv) < 5 or argv[0] not in ("inject", "check"):
        raise Refused("usage", "inject|check <pid> <container id> <source> <target>...")
    mode, pid, cid, source, *targets = argv
    if not pid.isdigit() or int(pid) <= 1:
        raise Refused("usage", f"not a pid: {pid!r}")
    if not cid or not all(c in "0123456789abcdef" for c in cid):
        raise Refused("usage", f"not a container id: {cid!r}")
    if not source.startswith("/"):
        raise Refused("usage", f"not an absolute source: {source!r}")
    return mode, int(pid), cid, source, targets


def conmon_of(comm: str, cmdline: bytes, cid: str) -> bool:
    """Whether a process (its ``comm`` and ``cmdline``) is the conmon of container ``cid``:
    conmon names the container in its own argv (``-c <id>``)."""
    if comm.strip() != "conmon":
        return False
    args = cmdline.split(b"\x00")
    return any(a == b"-c" and b == cid.encode() for a, b in itertools.pairwise(args))


def ppid_of(stat: str) -> int:
    """The parent pid in a ``/proc/<pid>/stat`` line (the comm may hold spaces and parens)."""
    return int(stat.rsplit(")", 1)[1].split()[1])


# ── the syscall layer ─────────────────────────────────────────────────────────────────


class Kernel:
    """The box's namespaces and the few calls this needs. Everything that touches the kernel is
    here, so the logic in :func:`run` is testable without one."""

    def __init__(self, pid: int, cid: str, source: str) -> None:
        if platform.machine() not in _ARCHES:
            raise Refused("arch", platform.machine())
        self._libc = ctypes.CDLL(None, use_errno=True)
        # Our own /proc, held before any setns: the box's /proc is its own pid namespace's.
        self._fdinfo = os.open("/proc/self/fdinfo", os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
        self._fds = os.open("/proc/self/fd", os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
        try:
            with open(f"/proc/{pid}/stat") as f:
                parent = ppid_of(f.read())
            with open(f"/proc/{parent}/comm") as f:
                comm = f.read()
            with open(f"/proc/{parent}/cmdline", "rb") as f:
                cmdline = f.read()
        except (OSError, ValueError, IndexError) as e:
            raise Refused("stale", f"pid {pid}: {e}") from None
        if not conmon_of(comm, cmdline, cid):
            raise Refused("stale", f"pid {pid}'s parent is not the conmon of {cid[:12]}")
        try:
            user = os.open(f"/proc/{pid}/ns/user", os.O_RDONLY | os.O_CLOEXEC)
            self._box = os.open(f"/proc/{pid}/ns/mnt", os.O_RDONLY | os.O_CLOEXEC)
            self._conmon = os.open(f"/proc/{parent}/ns/mnt", os.O_RDONLY | os.O_CLOEXEC)
        except PermissionError as e:
            raise Refused("rootful", f"can't open the box's namespaces: {e}") from None
        owner = struct.unpack("I", fcntl.ioctl(user, NS_GET_OWNER_UID, b"\0" * 4))[0]
        if owner != os.getuid():
            raise Refused("rootful", f"the box's user namespace belongs to uid {owner}")
        try:
            os.setns(user, os.CLONE_NEWUSER)
            os.setns(self._conmon, os.CLONE_NEWNS)
        except PermissionError as e:
            raise Refused("rootful", f"can't join the box's namespaces: {e}") from None
        self._source = os.open(source, os.O_PATH | os.O_DIRECTORY | os.O_CLOEXEC)
        st = os.fstat(self._source)
        self.source_id = (st.st_dev, st.st_ino)
        os.setns(self._box, os.CLONE_NEWNS)

    def _sys(self, name: str, *args: object) -> int:
        r = self._libc.syscall(_NR[name], *args)
        if r < 0:
            e = ctypes.get_errno()
            if e == errno.ENOSYS:
                raise Refused("enosys", f"{name}: the kernel has no such call")
            raise OSError(e, f"{name}: {os.strerror(e)}")
        return r

    def resolve(self, target: str) -> int:
        """An ``O_PATH`` fd on ``target`` in the box's mount namespace, refusing every symlink."""
        how = struct.pack(
            "QQQ",
            os.O_PATH | os.O_DIRECTORY | os.O_CLOEXEC,
            0,
            RESOLVE_NO_SYMLINKS | RESOLVE_NO_MAGICLINKS,
        )
        buf = ctypes.create_string_buffer(how, len(how))
        return self._sys("openat2", AT_FDCWD, target.encode(), buf, len(how))

    def identity(self, fd: int) -> tuple[tuple[int, int], bool]:
        st = os.fstat(fd)
        return (st.st_dev, st.st_ino), bool(os.fstatvfs(fd).f_flag & ST_RDONLY)

    def mount_id(self, fd: int) -> int:
        info = os.open(str(fd), os.O_RDONLY | os.O_CLOEXEC, dir_fd=self._fdinfo)
        try:
            text = os.read(info, 4096).decode()
        finally:
            os.close(info)
        for line in text.splitlines():
            key, _, value = line.partition(":")
            if key == "mnt_id":
                return int(value)
        raise OSError(errno.EINVAL, "no mnt_id in fdinfo")

    def clone(self) -> int:
        """A detached, read-only, private clone of the source's mount, made where it lives."""
        os.setns(self._conmon, os.CLONE_NEWNS)
        try:
            flags = OPEN_TREE_CLONE | os.O_CLOEXEC | AT_EMPTY_PATH
            tree = self._sys("open_tree", self._source, b"", flags)
            attr = struct.pack(
                "QQQQ", MOUNT_ATTR_RDONLY | MOUNT_ATTR_NOSUID | MOUNT_ATTR_NODEV, 0, MS_PRIVATE, 0
            )
            buf = ctypes.create_string_buffer(attr, len(attr))
            self._sys("mount_setattr", tree, b"", AT_EMPTY_PATH, buf, len(attr))
            return tree
        finally:
            os.setns(self._box, os.CLONE_NEWNS)

    def attach(self, tree: int, fd: int) -> None:
        flags = MOVE_MOUNT_F_EMPTY_PATH | MOVE_MOUNT_T_EMPTY_PATH
        self._sys("move_mount", tree, b"", fd, b"", flags)

    def detach(self, tree: int) -> bool:
        """Unmount the mount ``tree`` was attached as, wherever it now sits: through our own
        ``/proc/self/fd/<tree>`` (held from before the setns), never a path the box can move."""
        here = os.open("/", os.O_PATH | os.O_DIRECTORY | os.O_CLOEXEC)
        try:
            os.fchdir(self._fds)
            if self._libc.umount2(str(tree).encode(), MNT_DETACH) == 0:
                return True
            return False
        finally:
            os.fchdir(here)
            os.close(here)

    def close(self, fd: int) -> None:
        os.close(fd)

    def sleep(self, s: float) -> None:
        time.sleep(s)


# ── the logic ─────────────────────────────────────────────────────────────────────────


def probe(k, target: str) -> str:
    try:
        fd = k.resolve(target)
    except OSError as e:
        return {errno.ELOOP: SYMLINK, errno.ENOENT: MISSING, errno.ENOTDIR: NOT_DIR}.get(
            e.errno or 0, f"error: {e.strerror}"
        )
    try:
        ident, ro = k.identity(fd)
    finally:
        k.close(fd)
    if ident != k.source_id:
        return UNMASKED
    return MASKED if ro else WRITABLE


def _probe_waiting(k, target: str) -> str:
    """:func:`probe`, giving a dir the host has just made a few seconds to reach the guest."""
    waited = 0.0
    while (state := probe(k, target)) == MISSING and waited < MISSING_WAIT_S:
        k.sleep(MISSING_STEP_S)
        waited += MISSING_STEP_S
    return state


def inject_one(k, target: str) -> str:
    state = _probe_waiting(k, target)
    if state not in (UNMASKED, WRITABLE):
        return state
    tree = k.clone()
    try:
        try:
            fd = k.resolve(target)
        except OSError:
            return probe(k, target)  # gone or swapped for a link since: say which
        try:
            k.attach(tree, fd)
        finally:
            k.close(fd)
        # The box may have renamed the dir between the resolve and the attach: the mount then
        # sits on the renamed one. Only the very mount just made, at the target, counts.
        try:
            again = k.resolve(target)
        except OSError:
            again = None
        if again is not None:
            try:
                landed = k.mount_id(again) == k.mount_id(tree)
            finally:
                k.close(again)
            if landed and probe(k, target) == MASKED:
                return INJECTED
        return MOVED if k.detach(tree) else MOVED_KEPT
    finally:
        k.close(tree)


def run(mode: str, targets: list[str], k) -> dict[str, str]:
    results: dict[str, str] = {}
    for t in targets:
        if not valid_target(t):
            results[t] = INVALID
        elif mode == "check":
            results[t] = probe(k, t)
        else:
            try:
                results[t] = inject_one(k, t)
            except OSError as e:
                results[t] = f"error: {e.strerror or e}"
    return results


def main(argv: list[str], kernel=Kernel) -> dict:
    try:
        mode, pid, cid, source, targets = parse(argv)
        k = kernel(pid, cid, source)
        return {"refused": None, "detail": "", "results": run(mode, targets, k)}
    except Refused as r:
        return {"refused": r.reason, "detail": r.detail, "results": {}}
    except OSError as e:
        return {"refused": "error", "detail": str(e), "results": {}}


if __name__ == "__main__":
    print(json.dumps(main(sys.argv[1:])))
