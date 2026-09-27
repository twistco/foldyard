"""Heal the SHARED git index after a box-side git moved HEAD (ADR-0021's follow-up).

The per-kernel index split (the box's git shim) ended the cross-kernel index corruption, but
left a deterministic illusion on the host: a box-side commit/checkout moves the SHARED HEAD
while the host's ``.git/index`` still describes the old one, so every host-side ``git status``
(and GUI) shows the gap as phantom staged deletions/modifications until a manual ``git reset``.

**The box proposes, the host only renames.** Working out the healed index needs git, and git in
the checkout reads the checkout's own config — box-writable, and able to name commands
(``core.fsmonitor``, filter drivers…). So the host runs NO git here, and nothing else: the box's
shim, whose git is legitimately the box's, builds the healed index from a copy of the shared one
and leaves it next to it; this module installs it — under ``index.lock``, git's own protocol, on
the host's own kernel — only if nothing moved since:

* the shared index must still be byte-for-byte the one the proposal was built from (its blob hash
  is in the proposal's name), so host staging since then is never lost — the box re-proposes;
* HEAD must still be the proposal's HEAD (read as DATA: loose refs / ``packed-refs``), so a
  ``reset --soft`` since is never undone.

The strict conditions of the heal itself (only a BOX HEAD move, staged host work carried only
when disjoint, overlap refused) are the shim's (``git-index-shim.sh`` ``fy_propose``). The box
could write ``.git/index`` itself, so a proposal grants it nothing; what this module guarantees
is the host side: no process, no write outside the checkout's own git dir (reached from the
trusted main checkout without following a symlink), and nothing installed that wasn't verified.

Files next to the shared index (all derived state, safe to delete):
``index.fy-head`` — the HEAD the shared index was last intentional at (written here);
``index.fy-proposed.<head>.<base>.<ff|carry>`` / ``index.fy-record.<head>`` /
``index.fy-refused.<rec>.<head>.<base>`` — the box's offers (new names, never rewritten: a name
the host replaces reads as missing from the box for up to ~1 s over virtiofs — ADR-0021).
"""

from __future__ import annotations

import hashlib
import os
import re
import stat
from pathlib import Path

from . import config, mountwrite

SYNC_FILE = "index.fy-head"  # next to <gitdir>/index (the box's twin is index-box.head)
IN_PROGRESS = (
    "rebase-merge",
    "rebase-apply",
    "MERGE_HEAD",
    "CHERRY_PICK_HEAD",
    "REVERT_HEAD",
    "BISECT_LOG",
    "index.lock",
)
MAX_INDEX = 256 << 20  # a proposal bigger than any real index is not read into the supervisor

_OID = r"(?:[0-9a-f]{40}|[0-9a-f]{64})"
_PROPOSED = re.compile(rf"index\.fy-proposed\.({_OID})\.({_OID})\.(ff|carry)")
_RECORD = re.compile(rf"index\.fy-record\.({_OID})")
_REFUSED = re.compile(rf"index\.fy-refused\.({_OID})\.({_OID})\.({_OID})")

_NOFOLLOW_DIR = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW

# Refusals repeat every tick while the state persists — log each (rec, head) pair once.
_warned: dict[str, tuple[str, str]] = {}


def _open_dir(dfd: int, parts: list[str] | tuple[str, ...]) -> int:
    """``dfd``/parts… as a directory fd, following no symlink on the way (``OSError`` if one is)."""
    fd = os.dup(dfd)
    try:
        for part in parts:
            sub = os.open(part, _NOFOLLOW_DIR, dir_fd=fd)
            os.close(fd)
            fd = sub
    except BaseException:
        os.close(fd)
        raise
    return fd


def _read_at(dfd: int, rel: str, limit: int = 1 << 16) -> bytes | None:
    """A regular file below ``dfd`` read without following any symlink, or None."""
    *dirs, name = rel.split("/")
    try:
        d = _open_dir(dfd, dirs)
    except OSError:
        return None
    try:
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=d)
    except OSError:
        return None
    finally:
        os.close(d)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode) or os.fstat(fd).st_size > limit:
            return None
        chunks = []
        while chunk := os.read(fd, 1 << 20):
            chunks.append(chunk)
        return b"".join(chunks)
    finally:
        os.close(fd)


def _git_dir(repo: Path, main: Path) -> str | None:
    """``repo``'s git dir as a path RELATIVE to the trusted ``main`` checkout — ``.git`` for main,
    ``.git/worktrees/<name>`` for a worktree, ``<name>`` read from its box-writable ``.git`` file.
    None when that file is missing, a symlink, or names nothing."""
    if Path(repo) == Path(main):
        return ".git"
    try:
        fd = os.open(Path(repo) / ".git", os.O_RDONLY | os.O_NOFOLLOW)
    except OSError:
        return None
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            return None
        text = os.read(fd, 4096).decode("utf-8", "replace").strip()
    finally:
        os.close(fd)
    if not text.startswith("gitdir:"):
        return None
    # Only the NAME is taken from the file: the dir is always `<main>/.git/worktrees/<name>`,
    # opened from main without following a symlink — so no content of this file can point the
    # heal anywhere else (a path to another checkout's git dir names nothing that exists here).
    name = os.path.normpath(text.removeprefix("gitdir:").strip()).rsplit(os.sep, 1)[-1]
    if name in ("", ".", ".."):
        return None
    return f".git/worktrees/{name}"


def _resolve_head(gfd: int, cfd: int) -> str | None:
    """HEAD's commit id, read as data: ``HEAD`` in the git dir, refs from the common dir (loose,
    then ``packed-refs``). None for unborn, unreadable, or a reftable repo (not parsed: no heal)."""
    if _read_at(cfd, "reftable/tables.list") is not None:
        return None
    raw = _read_at(gfd, "HEAD")
    for _ in range(5):  # symbolic refs chain at most a few hops
        if raw is None:
            return None
        text = raw.decode("utf-8", "replace").strip()
        if re.fullmatch(_OID, text):
            return text
        if not text.startswith("ref: "):
            return None
        ref = text.removeprefix("ref: ").strip()
        if not ref.startswith("refs/") or ".." in ref.split("/"):
            return None
        raw = _read_at(cfd, ref)
        if raw is None:
            packed = _read_at(cfd, "packed-refs", limit=MAX_INDEX) or b""
            for line in packed.decode("utf-8", "replace").splitlines():
                oid, _, name = line.partition(" ")
                if name == ref and re.fullmatch(_OID, oid):
                    return oid
            return None
    return None


def _blob_id(data: bytes, like: str) -> str:
    """``git hash-object`` of ``data`` in the hash ``like`` is written in (sha1 or sha256)."""
    algo = hashlib.sha256 if len(like) == 64 else hashlib.sha1
    return algo(b"blob %d\0" % len(data) + data).hexdigest()


def _unlink(gfd: int, name: str) -> None:
    try:
        os.unlink(name, dir_fd=gfd)
    except OSError:
        pass


def _install(main: Path, gitrel: str, gfd: int, cfd: int, name: str, base: str, head: str) -> bool:
    """Under ``index.lock``: install proposal ``name`` if the shared index is still ``base`` and
    HEAD still ``head``. The bytes installed are the ones read and checked here (a no-follow read
    of a regular file that looks like an index), written by :mod:`mountwrite` — never a rename of
    the box's file, which could be swapped for a symlink between the check and the rename.

    The lock guards the index, not refs: a ``reset --soft`` moves HEAD without it. So HEAD is read
    again under the lock, and once more after the write, which puts the host's index back if HEAD
    moved meanwhile — the window left is the instant after that read, as for git's own writers."""
    try:
        lock = os.open("index.lock", os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644, dir_fd=gfd)
    except OSError:
        return False  # someone's mid-write — never fight git for its own lock
    try:
        current = _read_at(gfd, "index", limit=MAX_INDEX)
        if current is None or _blob_id(current, base) != base:
            return False
        proposal = _read_at(gfd, name, limit=MAX_INDEX)
        if proposal is None or not proposal.startswith(b"DIRC"):
            return False
        if _resolve_head(gfd, cfd) != head:
            return False
        mountwrite.write(main, f"{gitrel}/index", proposal)
        if _resolve_head(gfd, cfd) != head:
            mountwrite.write(main, f"{gitrel}/index", current)
            return False
        return True
    finally:
        os.close(lock)
        _unlink(gfd, "index.lock")


def _record(main: Path, gitrel: str, head: str) -> None:
    try:  # the git dir is on the mount: never through a planted symlink (mountwrite)
        mountwrite.write(main, f"{gitrel}/{SYNC_FILE}", (head + "\n").encode())
    except (OSError, ValueError):
        pass


def heal_checkout(repo: Path, main: Path | None = None) -> str | None:
    """One pass over ``repo``'s shared index: act on what the box offered. ``main`` is the
    trusted main checkout (``repo`` itself when omitted). Returns a log-worthy message when
    something was healed or refused, None on the (overwhelmingly common) quiet no-op."""
    main = Path(main or repo)
    gitrel = _git_dir(repo, main)
    if gitrel is None:
        return None
    try:
        root = os.open(main, os.O_RDONLY | os.O_DIRECTORY)
    except OSError:
        return None
    try:
        gfd = _open_dir(root, gitrel.split("/"))
        cfd = _open_dir(root, [".git"])
    except OSError:
        os.close(root)
        return None
    try:
        return _heal(repo, main, gitrel, gfd, cfd)
    finally:
        os.close(gfd)
        os.close(cfd)
        os.close(root)


def _heal(repo: Path, main: Path, gitrel: str, gfd: int, cfd: int) -> str | None:
    names = os.listdir(gfd)
    offers = [n for n in names if n.startswith("index.fy-") and n != SYNC_FILE]
    if not offers:
        return None
    if any(n in IN_PROGRESS for n in names):
        return None  # an operation is in flight — its index state is git's, not ours
    head = _resolve_head(gfd, cfd)
    if head is None:
        return None
    current = _read_at(gfd, "index", limit=MAX_INDEX)

    def mtime(n: str) -> float:
        try:
            return os.stat(n, dir_fd=gfd, follow_symlinks=False).st_mtime
        except OSError:
            return 0.0

    message = None
    for name in sorted(offers, key=mtime, reverse=True):  # newest first
        if m := _RECORD.fullmatch(name):
            if m[1] == head:
                _record(main, gitrel, head)  # the box saw the index intentional at HEAD
            _unlink(gfd, name)
        elif m := _PROPOSED.fullmatch(name):
            rec_head, base, kind = m.groups()
            if (
                message is None
                and rec_head == head
                and _install(main, gitrel, gfd, cfd, name, base, head)
            ):
                _record(main, gitrel, head)
                current = _read_at(gfd, "index", limit=MAX_INDEX)
                what = (
                    "staged work carried forward"
                    if kind == "carry"
                    else "stale index fast-forwarded"
                )
                message = f"{Path(repo).name}: {what} after a box-side HEAD move (→ {head[:12]}…)"
            _unlink(gfd, name)  # installed, or built on a state that's gone — the box re-proposes
        elif m := _REFUSED.fullmatch(name):
            rec, ref_head, base = m.groups()
            if ref_head != head or current is None or _blob_id(current, base) != base:
                _unlink(gfd, name)  # judged an index/HEAD that's gone — the box re-evaluates
            elif message is None and _warned.get(str(repo)) != (rec, head):
                _warned[str(repo)] = (rec, head)
                message = (
                    f"NOT healing {repo}: the box moved HEAD ({rec[:12]}… → {head[:12]}…) but "
                    "staged host-side changes overlap it — resolve by hand (`git reset` keeps "
                    "all files, then re-stage)"
                )
    return message


def _checkouts() -> tuple[Path, list[Path]]:
    """The main checkout, and it + every REGISTERED worktree (as devmode.worktree_keys — heal is
    state-driven, so a checkout whose box is DOWN still gets its last commits' staleness fixed).
    Never a listing of the box-writable worktrees root."""
    from . import devmode, worktree_registry  # lazy: keep import cost off importing githeal alone

    main = devmode.main_repo()
    return main, [main, *(p for _, p in sorted(worktree_registry.checkouts(main).items()))]


def sweep(log) -> None:
    """One supervisor tick's heal pass over every checkout. Gated on the index split being on
    (with the split OFF the box writes the shared index itself, and an unattended host-side
    writer would re-arm the very cross-kernel race the split removed). Never raises."""
    try:
        if not config.box_git_index_split():
            return
        main, checkouts = _checkouts()
    except Exception as e:  # a heal hiccup must never wedge the reconcile loop
        log(f"git-heal: sweep failed: {e}")
        return
    for co in checkouts:
        try:
            msg = heal_checkout(co, main)
            if msg:
                log(f"git-heal: {msg}")
        except Exception as e:  # one checkout's hiccup must not skip the rest
            log(f"git-heal: sweep failed: {e}")
