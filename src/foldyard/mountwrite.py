"""Host-side writes into the repo mount that never follow a symlink the box planted, and reads
of a file there that the box can't make hang (:func:`read_regular`).

Anything under a checkout — its dirs, ``.git/``, the dev-VM dir — is box-writable, so any path
there may be a symlink to a file the operator owns (``~/.ssh/authorized_keys``, the project's
``host.env``). ``Path.write_text`` follows it, and the supervisor writes into the mount every
tick. :func:`write` walks each directory below ``root`` with ``O_NOFOLLOW`` (creating the missing
ones), writes a fresh temp file with ``O_EXCL | O_NOFOLLOW``, and renames it over the target — a
rename REPLACES a symlink rather than writing through it. A symlinked directory is an error.

``root`` itself is trusted: it is the checkout (or git dir) the host already chose.
Stdlib-only and import-light: the supervisor's tick uses it, and so does ``config``'s hot path
(every TOML a checkout holds is read through :func:`read_regular`)."""

from __future__ import annotations

import errno
import os
import stat
from pathlib import Path

# A file of a checkout's that the host reads whole — a config, a compose file, a Dockerfile — is
# refused above this size. Far beyond any real one (a large compose file is tens of KB), while a
# box that grows one without end can't have the supervisor read it into memory every tick.
TEXT_LIMIT = 16 << 20


def read_regular(path: Path | str, *, limit: int, follow: bool = True) -> bytes:
    """The bytes of the regular file at ``path``, for a path the box can write. Raises
    ``OSError`` for anything else, ``FileNotFoundError`` when nothing is there, its ``strerror``
    saying why (``not a regular file``, ``over 16 MiB``, ``Permission denied``…).

    ``Path.read_bytes`` there lets the box hang the host: a FIFO with no writer blocks the
    ``open`` itself (the supervisor's tick never returns), and a link to ``/dev/zero`` reads
    without end. So the open is ``O_NONBLOCK`` (and ``O_NOCTTY``: a link to a terminal mustn't
    become the daemon's controlling one), then :func:`read_opened`.

    ``follow`` keeps today's behaviour where a link was followed (a ``foldyard.toml`` linked to a
    monorepo's shared one): the check is on the file the link reaches, so a link to a device or
    a FIFO is refused all the same. ``follow=False`` refuses a link at ``path`` itself
    (``ELOOP``)."""
    flags = os.O_RDONLY | os.O_NONBLOCK | os.O_NOCTTY | os.O_CLOEXEC
    fd = os.open(path, flags if follow else flags | os.O_NOFOLLOW)
    try:
        return read_opened(fd, limit=limit, name=str(path))
    finally:
        os.close(fd)


def read_opened(fd: int, *, limit: int, name: str) -> bytes:
    """The bytes of the file open on ``fd`` (opened ``O_NONBLOCK``, by :func:`read_regular` or a
    caller walking a directory by fd), which must be a regular file (``EINVAL``) holding at most
    ``limit`` bytes (``EFBIG``, checked as it is read too: the box can append meanwhile).

    ``/dev/null`` reads as empty: people link ``foldyard.local.toml`` to it, it was read (and
    adopted) as empty before, and a read of it ends at once. Any other device is refused."""
    st = os.fstat(fd)
    if not stat.S_ISREG(st.st_mode):
        if stat.S_ISCHR(st.st_mode) and st.st_rdev == _dev_null():
            return b""
        raise OSError(errno.EINVAL, "not a regular file", name)
    too_big = OSError(errno.EFBIG, f"over {_size(limit)}", name)
    if st.st_size > limit:
        raise too_big
    chunks: list[bytes] = []
    size = 0
    while chunk := os.read(fd, min(1 << 20, limit + 1 - size)):
        chunks.append(chunk)
        size += len(chunk)
        if size > limit:
            raise too_big
    return b"".join(chunks)


def _size(limit: int) -> str:
    return f"{limit >> 20} MiB" if limit >= 1 << 20 and not limit % (1 << 20) else f"{limit} bytes"


_DEV_NULL: list[int | None] = []


def _dev_null() -> int | None:
    """``/dev/null``'s device number, looked up once (None where there is none)."""
    if not _DEV_NULL:
        try:
            _DEV_NULL.append(os.stat(os.devnull).st_rdev)
        except OSError:
            _DEV_NULL.append(None)
    return _DEV_NULL[0]


def write(root: Path, rel: str | Path, data: bytes, *, mode: int | None = None) -> None:
    """Write ``data`` to ``root/rel`` atomically, following no symlink below ``root``, as a file
    with permission bits ``mode`` (default: ``0o644`` less the umask). Raises
    ``ValueError`` for a ``rel`` that isn't a plain relative path inside ``root``, and ``OSError``
    (``ELOOP``/``ENOTDIR``) when a directory along it is a symlink or not a directory.

    Atomic for a reader on THIS side. A reader in the VM over the share sees the name MISSING for
    about a second after the replace on virtiofs (test_mount_visibility_e2e, test_mounts_e2e's
    freshness test), so a guest-side reader must take a missing file as "not changed yet, read
    again", never as "gone" (ADR-0033 § Freshness)."""
    parts = Path(rel).parts
    if not parts or Path(rel).is_absolute() or ".." in parts:
        raise ValueError(f"{rel!r} is not a path inside {root}")
    dfd = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in parts[:-1]:
            try:
                os.mkdir(part, 0o755, dir_fd=dfd)
            except FileExistsError:
                pass
            sub = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=dfd)
            os.close(dfd)
            dfd = sub
        name = parts[-1]
        import secrets  # lazy: ~1.6 ms of imports that config's hot path, reading, never needs

        tmp = f".{name}.{os.getpid()}.{secrets.token_hex(4)}.tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644, dir_fd=dfd)
        try:
            with os.fdopen(fd, "wb") as fh:
                if mode is not None:  # on the new file's own fd: nothing the box placed is touched
                    os.fchmod(fh.fileno(), mode)
                fh.write(data)
            os.replace(tmp, name, src_dir_fd=dfd, dst_dir_fd=dfd)
        except BaseException:
            try:
                os.unlink(tmp, dir_fd=dfd)
            except OSError:
                pass
            raise
    finally:
        os.close(dfd)


def makedirs(root: Path, rel: str | Path, mode: int = 0o700) -> None:
    """Make the directory ``root/rel`` and every missing one between, following no symlink below
    ``root``: the directory-only twin of :func:`write`, for host-made dirs in a box-writable tree
    (ADR-0033's from-box slots). Each component is created under its parent's fd and opened
    ``O_NOFOLLOW | O_DIRECTORY``, so a link the box planted anywhere on the way refuses the walk
    (``OSError``: ``ELOOP``/``ENOTDIR``) instead of creating directories wherever it points —
    which ``Path.mkdir(parents=True)`` would. ``root``'s own last component is not followed
    either. Raises ``ValueError`` for a ``rel`` that isn't a plain relative path."""
    parts = Path(rel).parts
    if not parts or Path(rel).is_absolute() or ".." in parts:
        raise ValueError(f"{rel!r} is not a path inside {root}")
    dfd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in parts:
            try:
                os.mkdir(part, mode, dir_fd=dfd)
            except FileExistsError:
                pass
            sub = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=dfd)
            os.close(dfd)
            dfd = sub
    finally:
        os.close(dfd)


def _open_dir(root: Path, parts: tuple[str, ...]) -> int:
    """An fd on the EXISTING directory ``root/<parts>``, each component opened under its parent's
    fd with ``O_NOFOLLOW``: a planted link anywhere below ``root`` raises ``ELOOP``/``ENOTDIR``."""
    dfd = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in parts:
            sub = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=dfd)
            os.close(dfd)
            dfd = sub
    except BaseException:
        os.close(dfd)
        raise
    return dfd


def listdir(root: Path, rel: str | Path) -> list[str]:
    """The names in the directory ``root/rel``, following no symlink below ``root`` (an empty
    ``rel`` is ``root`` itself). Raises ``ValueError`` for a path leaving ``root``."""
    parts = Path(rel).parts
    if Path(rel).is_absolute() or ".." in parts:
        raise ValueError(f"{rel!r} is not a path inside {root}")
    dfd = _open_dir(root, parts)
    try:
        return os.listdir(dfd)
    finally:
        os.close(dfd)


def chmod_dir(root: Path, rel: str | Path, mode: int) -> None:
    """Set the mode of the EXISTING directory ``root/rel``, following no symlink below ``root``."""
    parts = Path(rel).parts
    if not parts or Path(rel).is_absolute() or ".." in parts:
        raise ValueError(f"{rel!r} is not a path inside {root}")
    dfd = _open_dir(root, parts)
    try:
        os.fchmod(dfd, mode)
    finally:
        os.close(dfd)


def unlink(root: Path, rel: str | Path) -> None:
    """Remove the file ``root/rel``, following no symlink below ``root``: a planted link as the
    LAST component is removed itself (``unlink`` never follows it), one on the way refuses."""
    parts = Path(rel).parts
    if not parts or Path(rel).is_absolute() or ".." in parts:
        raise ValueError(f"{rel!r} is not a path inside {root}")
    dfd = _open_dir(root, parts[:-1])
    try:
        os.unlink(parts[-1], dir_fd=dfd)
    finally:
        os.close(dfd)


def write_file(path: Path, data: bytes, within: Path | None = None) -> None:
    """:func:`write` for a full ``path``: walked from ``within`` when it lies under it (so every
    directory in between is checked), else from its own parent."""
    base = within if within is not None and path.is_relative_to(within) else path.parent
    write(base, path.relative_to(base), data)
