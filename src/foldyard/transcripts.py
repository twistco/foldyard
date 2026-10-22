"""`foldyard transcripts [dest]` — copy the dev box's agent transcripts out to a
stable host archive (RUN ON THE HOST).

Faithful port of the `transcripts` recipe. In-box agents write session transcripts to
gitignored bound-out dirs inside the repo. Those dirs survive `nuke`, but they're fragile
for long-term keeping (`git clean -fdx` wipes them, the worktree may be removed, they carry
two-hop ownership). This copies Claude's transcripts into its native host history dir, and a
configured Codex's into foldyard's own per-project archive (never Codex's native history — see
:func:`codex_archive_home`) — or both into a dir you pass / set in FOLDYARD_TRANSCRIPTS_ARCHIVE.

SAFE by construction: we only ever read an agent's transcript subtree (Claude ``projects/``
or Codex ``sessions/``) — never sibling credential/config files or settings — and never through a
link: the checkout is box-writable, so the walk opens every component without following one (see
``_open_source``). Of Claude's tree only the checkout's OWN project folder is copied: the box
names the folders, and one named after another project would be resumed there (see
:func:`claude_project_dir`). The copy is ADDITIVE (rsync's ``-rtu`` semantics, NO delete), so
the archive only grows and nothing there is removed or overwritten by an older copy (DRY_RUN=1
previews).

RUN ON THE HOST: the archive lives in the host's home, which the box can't reach.
Becomes part of the `claude-code` plugin later (ADR-0015). Stdlib only.
"""

from __future__ import annotations

import contextlib
import errno
import os
import secrets
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from . import boxmounts, config, stack


def _err(*a: object) -> None:
    print(*a, file=sys.stderr, flush=True)


def _box_running(engine: str, box: str, env: dict) -> bool:
    out = subprocess.run(
        [engine, "ps", "-q", "-f", f"name=^{box}$", "-f", "status=running"],
        env=env,
        capture_output=True,
        text=True,
    )
    return bool(out.stdout.strip())


def _archive_dest(dest: str = "") -> Path:
    """The durable host archive: an explicit ``dest`` / ``$FOLDYARD_TRANSCRIPTS_ARCHIVE``,
    else the host's own ``~/.claude/projects`` (so ``claude --resume`` lists in-box sessions)."""
    return Path(
        dest or os.environ.get("FOLDYARD_TRANSCRIPTS_ARCHIVE") or (Path.home() / ".claude/projects")
    ).expanduser()


def codex_archive_home() -> Path:
    """Where box Codex sessions are archived by default: a Codex home of foldyard's own, per
    project (``~/.foldyard/<project>/transcripts/codex``), never Codex's native history on your
    computer — see the note on Codex below. Shaped as a ``CODEX_HOME`` (sessions under
    ``sessions/``), so ``CODEX_HOME=<this> codex resume`` lists them."""
    return config.state_dir() / "transcripts" / "codex"


def _explicit_archive(dest: str = "") -> bool:
    return bool(dest or os.environ.get("FOLDYARD_TRANSCRIPTS_ARCHIVE"))


def _codex_archive_dest(dest: str = "") -> Path:
    """foldyard's per-project Codex archive, or a distinct child of an explicit shared archive.
    Never ``~/.codex/sessions``: there is deliberately no setting that archives into it."""
    if _explicit_archive(dest):
        return _archive_dest(dest) / "codex-sessions"
    return codex_archive_home() / "sessions"


# ── the copy: a no-follow walk, in Python, instead of rsync ──────────────────────────────────
#
# The source is box-writable: the bound-out dirs live in the checkout, so the box can swap any part
# of `<dev_vm_dir>/.devbox-claude/projects` — or anything inside it — for a symlink to a directory
# on your computer, and a host-side copy that follows it would lift ssh keys, cloud credentials or
# another checkout into the archive, where host tools read it. rsync can't be made safe here: it
# follows a link anywhere in the SOURCE ARGUMENT (the root, its parents), and inside the tree it
# lstat()s an entry and opens it by path later, so a swap between the two is followed too. Pinning
# the root as an fd doesn't help on macOS either (`/dev/fd/N` there is no directory rsync can walk).
#
# So the host walks the tree itself, by directory fd: every component from the checkout down — and
# every entry inside — is opened relative to its parent's fd with O_NOFOLLOW, so a link is never
# traversed however late it is swapped in. The anchor (the checkout the host resolved itself, or a
# configured override's parent) is the operator's path: its parents are followed (a symlinked
# workspace dir, macOS's /var), but a checkout's own last component is not — a worktree's sits in
# the box-writable worktrees root — and nothing below it is. Files are opened O_NONBLOCK and
# re-checked with fstat, so a FIFO swapped in after the walk looked can't hang the supervisor.
# Semantics are rsync's `-rtu --no-perms`, which this replaces: additive (never deletes), a file is
# copied when the archive has none, an OLDER one, or one of the same mtime but another size; the
# copy carries the source's mtime; symlinks and special files are skipped. New files are 0600 and
# new dirs 0700 — a transcript is private — and each is written to a temp name and renamed into
# place, so the archive never holds a half-written file and a name that is a link in the archive
# is replaced, not written through. It also drops rsync as a host prerequisite.

RC_REFUSED = 1  # a link (or non-directory) on the way to the source: nothing was copied
RC_PARTIAL = 23  # some files couldn't be copied (unreadable, over the cap, no room) — rsync's 23
RC_VANISHED = 24  # some files changed or vanished mid-walk — rsync's 24

# What is NOT a failure when the source is a LIVE transcript tree: a file vanishing between the
# listing and the copy, which a session file being rotated mid-sync produces routinely. The sweep
# runs against a tree the agent is actively writing; without this its notification would be noise.
# (rsync folded real per-file errors into its 23 as well, so the sweep used to ignore 23 too; this
# walk tells the two apart, so an unreadable or oversized file is reported now — once, on the edge.)
#
# Scoped to the sweep ON PURPOSE — the destructive-op path stays strict. There the tree IS
# quiescent, so 23/24 mean a real partial archive, and that rc is what makes `worktree remove`
# refuse to delete the checkout it just failed to save.
BENIGN_RC = frozenset({0, RC_VANISHED})

# Caps on what the box can make the host write. The box can already fill the disk through the
# checkout itself, so these don't stop a determined fill; they keep the ARCHIVE COPY from doubling
# it, or being the write that takes the disk to zero. A per-run total cap would refuse a legitimate
# first archive of a long-lived worktree, so the guard on volume is the free space instead.
# - MAX_FILE_BYTES: the largest session JSONL on one heavy user's machine (2026-10) is ~200 MB
#   (inline images push the outliers there); 4 GiB is twenty times that.
# - MIN_FREE_BYTES: never leave the archive's filesystem with less than this after a copy.
# - MAX_DEPTH: the agents' trees are 2–4 levels deep (Codex: sessions/YYYY/MM/DD); this bounds the
#   recursion and the fds held open (two per level) against a box-made million-deep tree.
MAX_FILE_BYTES = 4 << 30
MIN_FREE_BYTES = 1 << 30
MAX_DEPTH = 32

_CHUNK = 1 << 20
_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
# O_NONBLOCK: opening a FIFO for reading otherwise blocks until a writer appears.
_FILE_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC
# What opening a symlink (or a non-directory) with the flags above fails with: Linux and macOS say
# ELOOP for O_NOFOLLOW on a link, FreeBSD EMLINK; O_DIRECTORY on anything else is ENOTDIR.
_NOT_FOLLOWED = frozenset({errno.ELOOP, errno.EMLINK, errno.ENOTDIR})


class _Refused(Exception):
    def __init__(self, path: Path, why: str) -> None:
        super().__init__(f"{path} {why}")
        self.path, self.why = path, why


# ── which folders of the box's tree reach your history ──────────────────────────────────────
#
# Claude Code files each session under `projects/<name>/`, the name being the session's cwd with
# every character but [A-Za-z0-9] turned into `-`, and a Claude on your computer resumes the folder
# named after ITS cwd. The box chooses every name in the bound-out tree, so a folder named after
# another project on your computer would be loaded by `claude --resume` there: the box's text in a
# host agent's context, for a project the box was never given. Box sessions reaching resumes of the
# SAME checkout is the feature; anything else is refused.
#
# The rule is the exact name of the checkout's own folder, computed here from the checkout path the
# host resolved — the box mounts every checkout at that same path, so an in-box session started at
# the checkout's root has exactly this name. Not a prefix: `/` and `-` both become `-`, so
# `<checkout>-old` (a sibling checkout), `<checkout>-worktrees-feat` (a worktree in the default
# layout, which archives into its own folder) and `<checkout>/sub` all start with it. Sessions a box
# starts in a SUBDIRECTORY, or in /tmp, are refused with the rest: proving that a name is some real
# directory inside the checkout would need a search the box could race, and the bound-out dirs on
# hand (2026-10) hold none — only the root's folder, plus /tmp scratch runs. Refused names are
# skipped, never failures, and reported by name: `fy transcripts` and box down print them, the
# sweep logs each one once.
#
# Codex has the same exposure and no folder to check: its tree is dated (`sessions/YYYY/MM/DD/
# rollout-….jsonl`) and the session's directory is recorded INSIDE each file — `cwd` in the first
# (`session_meta`) line and again in every `turn_context` line, with `runtime_workspace_roots` and
# git fields beside it — which Codex indexes into its own state database for `codex resume`. A
# host-side check would mean parsing every line of every file the box writes, on every pass, against
# a list of fields Codex grows between releases (0.157 → 0.160 added some), and still guessing which
# of them its picker filters on. So Codex sessions are archived unfiltered — and so NEVER into your
# native history (`~/.codex/sessions`), where a session claiming another directory would be offered
# by `codex resume` in that other project. They go to a Codex home of foldyard's own, one per
# project (:func:`codex_archive_home`), or under `codex-sessions/` of an explicit archive dir.
# There is no opt-in to the native dir: it would re-open exactly this. Resuming one from the
# per-project home (`CODEX_HOME=<it> codex resume`) still puts box-written text in front of a Codex
# on your computer — for the project that box was given, which is the trade-off Claude's archive
# makes too.

_NAME_MAX = 200  # Claude Code's own cap; a longer name is cut and suffixed with a hash of the path


def claude_project_dir(path: str) -> str:
    """Claude Code's folder name under ``projects/`` for a session started in ``path`` — its own
    rule (2.1.x): every UTF-16 code unit outside [A-Za-z0-9] becomes ``-``; past 200 characters
    the name is cut there and ``-<the path's Java-style string hash, absolute, base 36>`` added."""
    raw = path.encode("utf-16-le")
    units = [int.from_bytes(raw[i : i + 2], "little") for i in range(0, len(raw), 2)]
    name = "".join(chr(u) if u < 128 and chr(u).isalnum() else "-" for u in units)
    if len(name) <= _NAME_MAX:
        return name
    h = 0
    for u in units:
        h = (h * 31 + u) & 0xFFFFFFFF
    h = abs(h - (1 << 32) if h >= 1 << 31 else h)
    digits = ""
    while True:
        h, d = divmod(h, 36)
        digits = "0123456789abcdefghijklmnopqrstuvwxyz"[d] + digits
        if not h:
            break
    return f"{name[:_NAME_MAX]}-{digits}"


@dataclass
class _Tally:
    copied: int = 0
    bytes: int = 0
    skipped: list[str] = field(default_factory=list)  # not plain files/dirs: never followed
    refused: list[str] = field(default_factory=list)  # top-level names that aren't the project's
    vanished: int = 0
    errors: list[str] = field(default_factory=list)

    @property
    def rc(self) -> int:
        return RC_PARTIAL if self.errors else RC_VANISHED if self.vanished else 0

    def problem(self) -> str:
        more = f" (+{len(self.errors) - 1} more)" if len(self.errors) > 1 else ""
        return f"{self.errors[0]}{more}" if self.errors else "files changed mid-copy"


def _lstat_at(dir_fd: int, name: str) -> os.stat_result:
    """The walk's look at an entry before it opens it (a seam: tests make it lie, to show the open
    itself refuses a link swapped in after the look)."""
    return os.stat(name, dir_fd=dir_fd, follow_symlinks=False)


def _why(dir_fd: int, name: str) -> str:
    try:
        is_link = stat.S_ISLNK(_lstat_at(dir_fd, name).st_mode)
    except OSError:
        is_link = False
    return "is a symlink" if is_link else "is not a directory"


def _open_source(anchor: Path, src: Path) -> int:
    """An fd on ``src``, reached from ``anchor`` one component at a time without following a link.

    ``anchor`` is the checkout the host resolved itself; every component below it is box-writable,
    and so is its own last component for a worktree (the worktrees root is mounted into the box), so
    the walk starts at its PARENT. A ``src`` outside it (a ``DEVBOX_TRANSCRIPTS`` override) anchors
    at its own parent instead — the operator named that path, but the dir the box writes into is
    still never followed. Raises :class:`_Refused` for a link or non-directory on the way,
    ``FileNotFoundError`` when a component is missing (nothing bound out yet)."""
    try:
        rel = src.relative_to(anchor)
    except ValueError:
        rel = None
    if rel is None or ".." in rel.parts:
        anchor, rel = src.parent, Path(src.name)
    elif anchor.name:  # start above the checkout, so its own name is opened without following
        anchor, rel = anchor.parent, Path(anchor.name) / rel
    fd = os.open(anchor, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    here = anchor
    try:
        for part in rel.parts:
            here = here / part
            try:
                child = os.open(part, _DIR_FLAGS, dir_fd=fd)
            except OSError as e:
                if e.errno in _NOT_FOLLOWED:
                    raise _Refused(here, _why(fd, part)) from None
                raise
            os.close(fd)
            fd = child
    except BaseException:
        os.close(fd)
        raise
    return fd


def _has_entries(anchor: Path, src: Path) -> bool:
    """Is there anything bound out at ``src``? A refused path counts as yes, so the copy that
    follows reports it rather than the caller quietly treating it as empty."""
    try:
        fd = _open_source(anchor, src)
    except _Refused:
        return True
    except OSError:
        return False
    try:
        return bool(os.listdir(fd))
    finally:
        os.close(fd)


def _mirror(src_fd: int, dest_dir: Path, dry: bool, *, only: str | None = None) -> _Tally:
    """Additively copy the tree under ``src_fd`` into ``dest_dir`` (see the block comment above).
    ``only``: the one top-level FOLDER copied (a Claude tree's own project); every other top-level
    entry is refused, by name. ``None`` copies the whole tree."""
    tally = _Tally()
    if dry:
        try:
            dst_fd: int | None = os.open(dest_dir, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
        except FileNotFoundError:
            dst_fd = None  # nothing archived yet: everything would be new
    else:
        dest_dir.mkdir(parents=True, exist_ok=True)
        dst_fd = os.open(dest_dir, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        _mirror_dir(src_fd, dst_fd, "", 0, dry, tally, only)
    finally:
        if dst_fd is not None:
            os.close(dst_fd)
    return tally


def _mirror_dir(
    src_fd: int,
    dst_fd: int | None,
    rel: str,
    depth: int,
    dry: bool,
    tally: _Tally,
    only: str | None = None,
) -> None:
    held = only is not None and depth == 0
    for name in sorted(os.listdir(src_fd)):
        path = f"{rel}{name}"
        if held and name != only:
            tally.refused.append(name)
            continue
        try:
            mode = _lstat_at(src_fd, name).st_mode
        except FileNotFoundError:
            tally.vanished += 1
            continue
        except OSError as e:
            tally.errors.append(f"{path}: {e.strerror or e}")
            continue
        if stat.S_ISDIR(mode):
            if depth >= MAX_DEPTH:
                tally.skipped.append(f"{path}/ (deeper than {MAX_DEPTH} levels)")
            else:
                _mirror_subdir(src_fd, dst_fd, name, path, depth, dry, tally)
        elif held:  # the project's name, but Claude only ever makes a folder there
            tally.refused.append(f"{name} (not a folder)")
        elif stat.S_ISREG(mode):
            _copy_file(src_fd, dst_fd, name, path, dry, tally)
        else:
            tally.skipped.append(path)  # a symlink, FIFO, socket or device: never opened


def _mirror_subdir(
    src_fd: int,
    dst_fd: int | None,
    name: str,
    path: str,
    depth: int,
    dry: bool,
    tally: _Tally,
) -> None:
    try:
        child = os.open(name, _DIR_FLAGS, dir_fd=src_fd)
    except FileNotFoundError:
        tally.vanished += 1
        return
    except OSError as e:
        if e.errno in _NOT_FOLLOWED:
            tally.skipped.append(path)  # swapped for a link since the look: not followed
        else:
            tally.errors.append(f"{path}: {e.strerror}")
        return
    child_dst: int | None = None
    try:
        if dst_fd is not None:
            if not dry:
                with contextlib.suppress(FileExistsError):
                    os.mkdir(name, 0o700, dir_fd=dst_fd)
            try:
                child_dst = os.open(name, _DIR_FLAGS, dir_fd=dst_fd)
            except FileNotFoundError:
                if not dry:
                    raise
            except OSError as e:
                if e.errno not in _NOT_FOLLOWED:
                    raise
                tally.errors.append(f"{path}: the archive's entry is not a plain directory")
                return
        _mirror_dir(child, child_dst, f"{path}/", depth + 1, dry, tally)
    except OSError as e:
        tally.errors.append(f"{path}: {e.strerror or e}")
    finally:
        os.close(child)
        if child_dst is not None:
            os.close(child_dst)


def _copy_file(
    src_fd: int, dst_fd: int | None, name: str, path: str, dry: bool, tally: _Tally
) -> None:
    try:
        fd = os.open(name, _FILE_FLAGS, dir_fd=src_fd)
    except FileNotFoundError:
        tally.vanished += 1
        return
    except OSError as e:
        if e.errno in _NOT_FOLLOWED:
            tally.skipped.append(path)  # swapped for a link since the look: not followed
        else:
            tally.errors.append(f"{path}: {e.strerror}")
        return
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):  # swapped for a FIFO/device since the look
            tally.skipped.append(path)
            return
        if st.st_size > MAX_FILE_BYTES:
            tally.errors.append(f"{path}: {st.st_size} bytes, over the {MAX_FILE_BYTES}-byte cap")
            return
        if dst_fd is not None:
            try:
                held = _lstat_at(dst_fd, name)
            except FileNotFoundError:
                held = None
            if held is not None:
                if not stat.S_ISREG(held.st_mode):
                    tally.errors.append(f"{path}: the archive's entry is not a plain file")
                    return
                if held.st_mtime_ns > st.st_mtime_ns or (
                    held.st_mtime_ns == st.st_mtime_ns and held.st_size == st.st_size
                ):
                    return  # the archive's copy is current (or newer): `-u`
        if dry:
            tally.copied, tally.bytes = tally.copied + 1, tally.bytes + st.st_size
            return
        assert dst_fd is not None  # only a dry run walks without an archive dir
        fs = os.fstatvfs(dst_fd)
        if fs.f_bavail * fs.f_frsize - st.st_size < MIN_FREE_BYTES:
            tally.errors.append(
                f"{path}: copying it would leave under {MIN_FREE_BYTES} bytes free in the archive"
            )
            return
        _write_into(dst_fd, name, fd, st)
        tally.copied, tally.bytes = tally.copied + 1, tally.bytes + st.st_size
    except OSError as e:
        tally.errors.append(f"{path}: {e.strerror or e}")
    finally:
        os.close(fd)


def _write_into(dst_fd: int, name: str, src: int, st: os.stat_result) -> None:
    """Copy ``st.st_size`` bytes of ``src`` to a temp name in the archive dir, stamp the source's
    mtime, and rename it over ``name`` — a whole file or nothing, and never through a link. Reads
    no further than the size the mtime belongs to: a line appended since lands next pass, as the
    source's mtime will have moved on."""
    tmp = f".fy-tmp-{secrets.token_hex(8)}"
    out = os.open(
        tmp,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
        0o600,
        dir_fd=dst_fd,
    )
    try:
        try:
            left = st.st_size
            while left > 0:
                chunk = os.read(src, min(_CHUNK, left))
                if not chunk:
                    break  # truncated under us: keep what there was
                view = memoryview(chunk)
                while view:
                    view = view[os.write(out, view) :]
                left -= len(chunk)
            os.utime(out, ns=(st.st_atime_ns, st.st_mtime_ns))
        finally:
            os.close(out)
        os.rename(tmp, name, src_dir_fd=dst_fd, dst_dir_fd=dst_fd)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp, dir_fd=dst_fd)
        raise


def _refusal(names: list[str], only: str) -> str:
    shown = ", ".join(names[:5]) + (" …" if len(names) > 5 else "")
    return (
        f"not archiving {shown} — only this checkout's own Claude project folder ({only}) is: "
        "another name would be resumed by a Claude on your computer in another project"
    )


def _report_tally(tally: _Tally, dry: bool, only: str | None = None) -> None:
    """The interactive paths' account of one copy (the sweep stays silent and reports edges)."""
    if tally.refused and only is not None:
        _err(f"⚠ {_refusal(tally.refused, only)}.")
    if dry:
        print(
            f"  would copy {tally.copied} file{'s' if tally.copied != 1 else ''} "
            f"({tally.bytes} bytes)"
        )
    if tally.skipped:
        shown = ", ".join(tally.skipped[:5]) + (" …" if len(tally.skipped) > 5 else "")
        _err(
            f"⚠ skipped {len(tally.skipped)} entries that aren't plain files or directories "
            f"(symlinks, pipes…), never followed: {shown}"
        )
    if tally.vanished:
        _err(f"⚠ {tally.vanished} files changed or vanished mid-copy; the next pass picks them up.")
    for line in tally.errors[:5]:
        _err(f"✗ not archived: {line}")
    if len(tally.errors) > 5:
        _err(f"✗ …and {len(tally.errors) - 5} more not archived.")


def _archive(
    src_dir: Path,
    *,
    anchor: Path,
    project_of: Path | None,
    dest: str = "",
    what: str = "transcripts",
    quiet: bool = False,
    announce: bool = True,
) -> tuple[int, str, list[str]]:
    """Additively copy one bound-out transcript tree into its archive. Returns ``(rc, detail,
    refused)``: rc 0 for success or nothing to do, else :data:`RC_REFUSED` / :data:`RC_PARTIAL` /
    :data:`RC_VANISHED` / 1 (the archive couldn't be written at all), with a one-line reason; and
    the top-level names left out because they aren't ``project_of``'s Claude project folder (a
    policy skip, not a failure). ``project_of`` is the checkout whose sessions these are, as the
    HOST resolved it — required, so no Claude caller can forget it; ``None`` only for a tree with
    no project folders (Codex's). ``quiet`` prints nothing (the sweep reports on edges);
    ``announce`` prints the ▶/✓ lines."""
    dry = bool(os.environ.get("DRY_RUN"))
    only = None if project_of is None else claude_project_dir(str(project_of))
    dest_dir = _archive_dest(dest)
    try:
        src_fd = _open_source(anchor, src_dir)
    except FileNotFoundError:
        return 0, "", []  # nothing bound out yet
    except _Refused as r:
        detail = f"{r.path} {r.why} — a link in the box-writable path is never followed"
        if not quiet:
            _err(f"✗ not archiving {what}: {detail}.")
        return RC_REFUSED, detail, []
    except OSError as e:
        detail = f"{src_dir}: {e.strerror or e}"
        if not quiet:
            _err(f"✗ archiving {what} failed: {detail}")
        return 1, detail, []
    try:
        if not os.listdir(src_fd):
            return 0, "", []  # nothing worth keeping
        if announce and not quiet:
            print(f"▶ archiving {what} → {dest_dir}" + ("   (DRY RUN — no changes)" if dry else ""))
        tally = _mirror(src_fd, dest_dir, dry, only=only)
    except OSError as e:
        detail = f"{dest_dir}: {e.strerror or e}"
        if not quiet:
            _err(f"✗ archiving {what} failed: {detail}")
        return 1, detail, []
    finally:
        os.close(src_fd)
    rc = tally.rc
    if not quiet:
        _report_tally(tally, dry, only)
        if rc == 0 and announce:
            print(f"✓ {what} {'would be ' if dry else ''}archived → {dest_dir}")
        elif rc:
            _err(f"✗ archiving {what} incomplete (rc={rc}).")
    return rc, ("" if rc == 0 else tally.problem()), tally.refused


def bound_out_dir(checkout: Path, here: str) -> Path:
    """The LEGACY host-side dir a box binds Claude's ``projects/`` out to, inside the checkout —
    a box created before the from-box (ADR-0033), or one on the podman backend, which keeps it.
    Fragile to ``git clean -fdx`` and worktree removal, which is why destructive ops
    :func:`sync_archive` it to the durable store first."""
    return checkout / here / ".devbox-claude/projects"


def codex_bound_out_dir(checkout: Path, here: str) -> Path:
    """The LEGACY host-side directory bound to the box Codex's ``~/.codex/sessions`` tree."""
    return checkout / here / ".devbox-codex/sessions"


# ── where a checkout's transcripts are: the from-box slot, then the legacy bind-out ──────────
#
# A box on the from-box layout (ADR-0033, the lima backend) binds its agents' trees out to the
# checkout's slot under `<state dir>/from-box/` — out of the checkout, so `git clean`, grep and
# `worktree remove` never see them. For one release the host ALSO reads the legacy bind-out inside
# the checkout (a box not yet recreated, the podman backend, a session written before the switch);
# the next release drops it. Both are box-writable and read by the same no-follow walk: the
# from-box one anchored at the from-box root, whose parent — the state dir — no box can reach,
# so even the root's own name is opened without following a link.


def sources(
    checkout: Path, here: str, worktree: str = "", *, codex: bool = False
) -> list[tuple[Path, Path]]:
    """``(source, anchor)`` for one agent's bound-out tree, newest layout first. An operator's
    ``DEVBOX_TRANSCRIPTS`` / ``DEVBOX_CODEX_TRANSCRIPTS`` is the one place the box was told to bind
    to, so it replaces both."""
    override = os.environ.get("DEVBOX_CODEX_TRANSCRIPTS" if codex else "DEVBOX_TRANSCRIPTS")
    if override:
        return [(Path(override), checkout)]
    legacy = (codex_bound_out_dir if codex else bound_out_dir)(checkout, here)
    try:
        rel = boxmounts.codex_rel(worktree) if codex else boxmounts.claude_rel(worktree)
    except ValueError:  # a worktree name no slot can carry: it never had one
        return [(legacy, checkout)]
    root = boxmounts.from_box_dir()
    return [(root / rel, root), (legacy, checkout)]


def archive_legacy(checkout: Path, here: str) -> None:
    """`fy box up` on the from-box layout: archive whatever the legacy bind-out in the checkout
    still holds — never moved into the from-box — and say it can be deleted."""
    trees: list[tuple[Path, str, Path | None, str]] = [
        (bound_out_dir(checkout, here), "Claude transcripts (old layout)", checkout, "")
    ]
    if config.codex_enabled():
        trees.append(
            (
                codex_bound_out_dir(checkout, here),
                "Codex transcripts (old layout)",
                None,
                str(_codex_archive_dest()),
            )
        )
    for src, what, project_of, dest in trees:
        if not _has_entries(checkout, src):
            continue
        rc, detail, _ = _archive(
            src, anchor=checkout, project_of=project_of, dest=dest, what=what, quiet=True
        )
        if rc in BENIGN_RC:  # one line, not the archive's ▶/✓ pair: this runs at every `box up`
            print(
                f"ℹ transcripts now bind out to foldyard's from-box; the old {src} is archived "
                "and can be deleted."
            )
        else:
            print(f"⚠ {src} (the old layout) could not be fully archived ({detail}) — keep it.")


def sync_archive(
    src_dir: Path,
    *,
    anchor: Path,
    project_of: Path | None,
    dest: str = "",
    what: str = "transcripts",
) -> int:
    """Additively copy one bound-out agent transcript dir → its durable host archive BEFORE a
    destructive op deletes the source. Returns 0 on success or nothing-to-do, non-zero when a copy
    was refused or incomplete. Never raises.

    ``anchor`` is the checkout ``src_dir`` lives in, as the HOST resolved it: everything below it is
    box-writable, so no link there is followed (a link on the way to ``src_dir`` refuses the copy;
    one inside it is skipped). ``project_of`` is the checkout whose Claude project folder is the
    only top-level entry copied (``None`` for Codex's tree — see :func:`claude_project_dir`).
    No-op (0) inside the box (the archive lives in the host's home, unreachable there) and when
    ``src_dir`` is missing/empty."""
    if config.in_box():
        return 0
    return _archive(src_dir, anchor=anchor, project_of=project_of, dest=dest, what=what)[0]


# ── the supervisor's per-interval auto-sync (`[claude]/[codex] transcript_sync_seconds`) ──────
#
# Why a HOST-side timer and not something in the box: the box cannot reach the host's home, so it
# can never push. It doesn't have to — `FY_TRANSCRIPTS` bind-mounts the bound-out dir into the box
# at the agent's own `projects/`/`sessions/` path, so an in-box write lands on the host path as it
# happens. The sweep is therefore a HOST-LOCAL copy between two host paths: no engine call, no box
# round-trip. It is also why polling is right rather than a watcher — host-side inotify doesn't fire
# for guest writes (virtiofs, podman#22343), the very gap that makes hot reload miss host edits.
#
# What it buys, given the data is already on the host: the bound-out dir is fragile (a `git clean
# -fdx` wipes it, `worktree remove` takes it away) and invisible to `claude --resume`, which reads
# the native `~/.claude/projects`. Promoting continuously means a crashed box or a reclaimed
# worktree costs you nothing, instead of losing everything since the last `fy box down`.

# Per-(worktree, agent) auto-sync state: the tick countdown, the interval it was computed for, and
# the last verdict + failure streak that make reporting EDGE-triggered. Module-global, like
# supervisor._probe_state — there is one supervisor process.
_sweep_state: dict[tuple[str, str], dict] = {}

# Consecutive failed passes before the log line escalates to a push notification. A failed ARCHIVE
# sync degrades nothing the box does — the bound-out dir still holds every transcript, live — so
# this is deliberately not urgent, and deliberately not a `fy doctor` row.
NOTIFY_AFTER_FAILURES = 3


_Plan = list[tuple[str, float, list[tuple[Path, Path]], str, Path | None]]


def _sweep_plan() -> tuple[Path, _Plan]:
    """The bound checkout, and ``(label, interval, [(source, anchor)…], archive dest, project_of)``
    for each configured agent whose auto-sync is ON, resolved under the CURRENTLY BOUND config — so
    the caller's ``with config.using(worktree_config(wt))`` is what points this at the right
    checkout (and its from-box slot)."""
    checkout = config.repo_root()
    here = config.dev_vm_rel()
    wt = config.active_worktree()
    plan: _Plan = []
    if config.claude_enabled() and (secs := config.claude_transcript_sync_seconds()):
        plan.append(("Claude", secs, sources(checkout, here, wt), "", checkout))
    if config.codex_enabled() and (secs := config.codex_transcript_sync_seconds()):
        plan.append(
            (
                "Codex",
                secs,
                sources(checkout, here, wt, codex=True),
                str(_codex_archive_dest()),
                None,
            )
        )
    return checkout, plan


def _due(key: tuple[str, str], interval: float, tick_seconds: float) -> bool:
    """Is this agent due this tick? The interval is expressed in TICKS — ``interval // tick``,
    floored, minimum 1 — so a configured 15s on a 2s tick runs every 7 ticks (14s effective).
    Counting ticks rather than holding a monotonic deadline is what makes that floor exact; it also
    means a slow tick (a reconcile pass with capability probes due can take seconds) stretches the
    real interval rather than firing a catch-up burst. Re-arms from zero when the configured
    interval changes, so an adopted config edit takes effect on the next tick."""
    ticks = max(1, int(interval // tick_seconds))
    state = _sweep_state.setdefault(key, {"left": 0, "ticks": ticks, "failing": False, "streak": 0})
    if state["ticks"] != ticks:
        state["ticks"], state["left"] = ticks, 0
    if state["left"] > 0:
        state["left"] -= 1
        return False
    state["left"] = ticks - 1
    return True


def _report(log, notify, key: tuple[str, str], label: str, wt: str, rc: int, detail: str) -> None:
    """Report one pass on its EDGES only — the same discipline supervisor.run_capability_probes
    uses. A healthy sweep says NOTHING (it runs every few seconds forever, and the supervisor tees
    stdout to host-supervisor.log); a broken one logs once when it breaks and once when it heals,
    then escalates to the operator only if it stays broken."""
    state = _sweep_state[key]
    where = f" ({wt})" if wt else ""
    if rc == 0:
        if state["failing"]:
            log(f"transcript-sync: {label}{where} recovered")
        state["failing"], state["streak"] = False, 0
        return
    state["streak"] += 1
    if not state["failing"]:
        state["failing"] = True
        log(f"transcript-sync: {label}{where} FAILED — {detail}")
    # Exactly ON the threshold, so a sync that stays broken notifies once, not every interval.
    if notify and state["streak"] == NOTIFY_AFTER_FAILURES:
        notify(
            "foldyard: transcript sync failing",
            f"{label}{where}: {detail} — archive is going stale. Run `fy transcripts`.",
        )


def _report_refused(
    log, key: tuple[str, str], label: str, wt: str, refused: list[str], only: str
) -> None:
    """Name each refused folder ONCE while it stays (they persist in the bound-out dir, so every
    pass refuses them again). Not a failure: no FAILED edge, no notification — the archive of the
    checkout's own folder is healthy. Remembers only what this pass refused, so what is held is
    bounded by the listing itself; a name removed and planted again is named again."""
    state = _sweep_state[key]
    seen: set[str] = state.get("refused", set())
    new = [name for name in refused if name not in seen]
    if new:
        where = f" ({wt})" if wt else ""
        log(f"transcript-sync: {label}{where} {_refusal(new, only)}")
    state["refused"] = set(refused)


def sweep(log, wt: str = "", *, tick_seconds: float = 2.0, notify=None) -> None:
    """One supervisor tick's auto-sync pass for the BOUND worktree's configured agents. Off (a
    no-op with no state touched) unless that checkout's ADOPTED config sets a positive
    ``transcript_sync_seconds`` — so a working-tree edit needs ``fy config adopt`` first, like every
    other host-side read. Never raises: one agent's failure must not wedge the reconcile loop."""
    if config.in_box():  # the archive is in the host's home, unreachable from the box
        return
    try:
        _checkout, plan = _sweep_plan()
    except Exception as e:  # a config hiccup must never wedge the reconcile loop
        log(f"transcript-sync: sweep failed: {type(e).__name__}: {e}")
        return
    for label, interval, srcs, dest, project_of in plan:
        key = (wt, label)
        try:
            if not _due(key, interval, tick_seconds):
                continue
            rc, detail, refused = 0, "", []
            for src, anchor in srcs:  # the from-box slot, then the legacy bind-out
                got, why, held = _archive(
                    src, anchor=anchor, project_of=project_of, dest=dest, what=label, quiet=True
                )
                if got not in BENIGN_RC and not rc:
                    rc, detail = got, why
                refused += [name for name in held if name not in refused]
            if project_of is not None:
                _report_refused(log, key, label, wt, refused, claude_project_dir(str(project_of)))
        except Exception as e:  # one agent's hiccup must not skip the other
            rc, detail = 1, f"{type(e).__name__}: {e}"
        _report(log, notify, key, label, wt, rc, detail)


def sync_current(env: dict, *, what: str = "transcripts", dest: str = "") -> int:
    """Best-effort archive of the CURRENT checkout's configured agents' bound-out transcripts.

    Used by box-down / nuke, which keep the bound-out dirs but should still promote them to the
    durable store. Honours the same ``DEVBOX_TRANSCRIPTS``, ``DEVBOX_CODEX_TRANSCRIPTS``, and
    ``HERE`` overrides the box used at up-time. Each agent is attempted independently so one
    failed or absent source never prevents the other from being archived.
    """
    checkout = Path(env.get("FOLDYARD_CHECKOUT") or stack.main_repo())
    here = env.get("HERE") or config.dev_vm_rel()
    wt = env.get("WORKTREE", "")
    rc = 0
    for src, anchor in sources(checkout, here, wt):  # the from-box slot, then the legacy dir
        got = sync_archive(src, anchor=anchor, project_of=checkout, what=what, dest=dest)
        rc = rc or got

    if config.codex_enabled():
        codex_what = "Codex transcripts" if what == "transcripts" else f"{what} (Codex transcripts)"
        for src, anchor in sources(checkout, here, wt, codex=True):
            got = sync_archive(
                src,
                anchor=anchor,
                project_of=None,  # dated, no project folders: see the residual-risk note above
                what=codex_what,
                dest=str(_codex_archive_dest(dest)),
            )
            rc = rc or got
    return rc


def transcripts(dest: str = "") -> int:
    # Same box-detection `verify`/the recipe use: IN_DEVBOX, or the preset in-box socket.
    if config.in_box():
        _err("✗ run this ON YOUR COMPUTER — the archive is in its home, unreachable from the box.")
        return 1
    # The bound-out dirs are host-side; the engine is only the fallback for a RUNNING box, which a
    # stopped VM rules out — so resolve without booting it, and skip that fallback.
    down = stack.machine_down()
    ctx = stack.resolve(no_machine=down is not None)
    env = ctx.env
    engine_env = None if down else env
    engine = config.engine()
    box = f"{ctx.project}-devbox"
    checkout = Path(env["FOLDYARD_CHECKOUT"])
    here = env.get("HERE") or config.dev_vm_rel()
    wt = env.get("WORKTREE", "")
    # Default into the host's own history so `claude --resume` lists these in-box sessions.
    claude_dest = _archive_dest(dest)
    dry = bool(os.environ.get("DRY_RUN"))

    rc = _sync_source(
        sources(
            checkout, here, wt
        ),  # the from-box slot, then the legacy bind-out (or the override)
        claude_dest,
        dry,
        engine=engine,
        engine_env=engine_env,
        box=box,
        box_home_cmd='printf %s "${CLAUDE_CONFIG_DIR:-$HOME/.claude}"',
        box_rel="projects",
        label="Claude transcripts",
        project_of=checkout,
        # A Codex-only project has no Claude source; continue on to its configured agent.
        missing_ok=config.codex_enabled(),
    )

    # A [codex] table opts into Codex installation and transcript persistence. Its archive is a
    # Codex home of foldyard's own (never ~/.codex/sessions — see the note on Codex above), which
    # `CODEX_HOME=<it> codex resume` reads; an explicit archive dir takes a child of its own.
    if config.codex_enabled():
        codex_dest = _codex_archive_dest(dest)
        codex_rc = _sync_source(
            sources(checkout, here, wt, codex=True),
            codex_dest,
            dry,
            engine=engine,
            engine_env=engine_env,
            box=box,
            box_home_cmd='printf %s "${CODEX_HOME:-$HOME/.codex}"',
            box_rel="sessions",
            label="Codex transcripts",
            project_of=None,
            missing_ok=rc == 0,
            hint=None if _explicit_archive(dest) else _codex_resume_hint(checkout),
        )
        rc = rc or codex_rc
    return rc


def _codex_resume_hint(checkout: Path) -> str:
    home = shlex.quote(str(codex_archive_home()))
    return (
        f"  resume one: cd {shlex.quote(str(checkout))} && CODEX_HOME={home} codex resume\n"
        "  (a Codex home of its own: log in there once; your ~/.codex config is not read."
        " A resumed session's text was written by the box.)"
    )


def _sync_source(
    srcs: list[tuple[Path, Path]],
    dest_dir: Path,
    dry: bool,
    *,
    engine: str,
    engine_env: dict | None,
    box: str,
    box_home_cmd: str,
    box_rel: str,
    label: str,
    project_of: Path | None,
    missing_ok: bool = False,
    hint: str | None = None,
) -> int:
    """Sync one agent's bound history — every bound-out ``(source, anchor)`` that holds anything,
    newest layout first — falling back to its live in-box directory when none does. ``hint`` is
    printed after a real (not dry-run) sync: how to read what was archived."""
    # Prefer the bound-out dirs (already on the host, no engine needed; reached from their anchor
    # without following a link — see `_open_source`); if they're all empty, fall back to pulling
    # the live projects/ tree out of the running box over the socket with `<engine> cp` —
    # projects/ ONLY, so creds never come along.
    held = [(src, anchor) for src, anchor in srcs if _has_entries(anchor, src)]
    if held:
        rc = 0
        for src, anchor in held:
            got = _sync_stage(src, anchor, "bound-out dir", dest_dir, dry, label, project_of, hint)
            rc = rc or got
        return rc
    src_dir = srcs[0][0]
    tmp: str | None = None
    if engine_env is not None and _box_running(engine, box, engine_env):
        # Resolve where Claude actually writes IN the box: the image bakes HOME=/home/vscode,
        # so even as root ~/.claude is /home/vscode/.claude. Honour CLAUDE_CONFIG_DIR too.
        out = subprocess.run(
            [engine, "exec", box, "sh", "-c", box_home_cmd],
            env=engine_env,
            capture_output=True,
            text=True,
        )
        box_agent_home = out.stdout.strip()
        if not box_agent_home:
            box_agent_home = f"/home/vscode/{'.claude' if box_rel == 'projects' else '.codex'}"
        tmp = tempfile.mkdtemp()
        box_src = f"{box_agent_home}/{box_rel}"
        _err(f"+ {engine} cp {box}:{box_src}/. {tmp}")
        cp = [engine, "cp", f"{box}:{box_src}/.", tmp]
        if subprocess.run(cp, env=engine_env).returncode != 0:
            _err(f"✗ couldn't read {label.lower()} from {box}:{box_src}.")
            shutil.rmtree(tmp, ignore_errors=True)
            return 1
        # The stage is the host's own tmp dir, but what `cp` put in it came from the box (links
        # included, pointing at host paths): the same no-follow walk copies it out.
        stage, origin = Path(tmp), f"live box {box} ({box_src})"
    else:
        if missing_ok:
            print(f"✓ no {label.lower()} yet — nothing to do.")
            return 0
        _err(f"✗ nothing to copy: {src_dir} is empty and the box ({box}) isn't running.")
        return 1

    try:
        if not _has_entries(stage, stage):
            print(f"✓ no {label.lower()} yet (source is empty) — nothing to do.")
            return 0
        return _sync_stage(stage, stage, origin, dest_dir, dry, label, project_of, hint)
    finally:
        if tmp:
            shutil.rmtree(tmp, ignore_errors=True)


def _sync_stage(
    stage: Path,
    anchor: Path,
    origin: str,
    dest_dir: Path,
    dry: bool,
    label: str,
    project_of: Path | None,
    hint: str | None,
) -> int:
    print(f"▶ source: {origin}")
    print(f"  from:   {stage}")
    print(f"  to:     {dest_dir}" + ("   (DRY RUN — no changes)" if dry else ""))
    # Held to the CHECKOUT's folder even when the stage is the box-cp tmp dir: the names in it
    # are the box's either way.
    rc, _, _ = _archive(
        stage,
        anchor=anchor,
        project_of=project_of,
        dest=str(dest_dir),
        what=label,
        announce=False,
    )
    if rc == 0:
        print(f"✓ {label} {'would be ' if dry else ''}synced → {dest_dir}")
        if hint and not dry:
            print(hint)
    return rc
