"""Host-side writes into the repo mount that never follow a symlink the box planted.

Anything under a checkout — its dirs, ``.git/``, the dev-VM dir — is box-writable, so any path
there may be a symlink to a file the operator owns (``~/.ssh/authorized_keys``, the project's
``host.env``). ``Path.write_text`` follows it, and the supervisor writes into the mount every
tick. :func:`write` walks each directory below ``root`` with ``O_NOFOLLOW`` (creating the missing
ones), writes a fresh temp file with ``O_EXCL | O_NOFOLLOW``, and renames it over the target — a
rename REPLACES a symlink rather than writing through it. A symlinked directory is an error.

``root`` itself is trusted: it is the checkout (or git dir) the host already chose.
Stdlib-only: the supervisor's tick uses it."""

from __future__ import annotations

import os
import secrets
from pathlib import Path


def write(root: Path, rel: str | Path, data: bytes) -> None:
    """Write ``data`` to ``root/rel`` atomically, following no symlink below ``root``. Raises
    ``ValueError`` for a ``rel`` that isn't a plain relative path inside ``root``, and ``OSError``
    (``ELOOP``/``ENOTDIR``) when a directory along it is a symlink or not a directory."""
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
        tmp = f".{name}.{os.getpid()}.{secrets.token_hex(4)}.tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644, dir_fd=dfd)
        try:
            with os.fdopen(fd, "wb") as fh:
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


def write_file(path: Path, data: bytes, within: Path | None = None) -> None:
    """:func:`write` for a full ``path``: walked from ``within`` when it lies under it (so every
    directory in between is checked), else from its own parent."""
    base = within if within is not None and path.is_relative_to(within) else path.parent
    write(base, path.relative_to(base), data)
