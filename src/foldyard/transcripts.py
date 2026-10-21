"""`foldyard transcripts [dest]` — copy the dev box's agent transcripts out to a
stable host archive (RUN ON THE HOST).

Faithful port of the `transcripts` recipe. In-box agents write session transcripts to
gitignored bound-out dirs inside the repo. Those dirs survive `nuke`, but they're fragile
for long-term keeping (`git clean -fdx` wipes them, the worktree may be removed, they carry
two-hop ownership). This copies configured agents' transcripts into their native host history
dirs — or a dir you pass / set in FOLDYARD_TRANSCRIPTS_ARCHIVE.

SAFE by construction: we only ever read an agent's transcript subtree (Claude ``projects/``
or Codex ``sessions/``) — never sibling credential/config files or settings — and never through a
link: the checkout is box-writable, so the walk opens every component without following one (see
``_open_source``). The copy is ADDITIVE (rsync's ``-rtu`` semantics, NO delete), so the archive
only grows and nothing there is removed or overwritten by an older copy (DRY_RUN=1 previews).

RUN ON THE HOST: the archive lives in the host's home, which the box can't reach.
Becomes part of the `claude-code` plugin later (ADR-0015). Stdlib only.
"""

from __future__ import annotations

import contextlib
import errno
import os
import secrets
import shutil
import stat
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from . import config, stack


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


def _codex_archive_dest(dest: str = "") -> Path:
    """Codex's native history, or a distinct child of an explicit shared archive."""
    if dest or os.environ.get("FOLDYARD_TRANSCRIPTS_ARCHIVE"):
        return _archive_dest(dest) / "codex-sessions"
    return Path.home() / ".codex/sessions"


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


@dataclass
class _Tally:
    copied: int = 0
    bytes: int = 0
    skipped: list[str] = field(default_factory=list)  # not plain files/dirs: never followed
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


def _mirror(src_fd: int, dest_dir: Path, dry: bool) -> _Tally:
    """Additively copy the tree under ``src_fd`` into ``dest_dir`` (see the block comment above)."""
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
        _mirror_dir(src_fd, dst_fd, "", 0, dry, tally)
    finally:
        if dst_fd is not None:
            os.close(dst_fd)
    return tally


def _mirror_dir(
    src_fd: int, dst_fd: int | None, rel: str, depth: int, dry: bool, tally: _Tally
) -> None:
    for name in sorted(os.listdir(src_fd)):
        path = f"{rel}{name}"
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


def _report_tally(tally: _Tally, dry: bool) -> None:
    """The interactive paths' account of one copy (the sweep stays silent and reports edges)."""
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
    dest: str = "",
    what: str = "transcripts",
    quiet: bool = False,
    announce: bool = True,
) -> tuple[int, str]:
    """Additively copy one bound-out transcript tree into its archive. Returns ``(rc, detail)``:
    0 for success or nothing to do, else :data:`RC_REFUSED` / :data:`RC_PARTIAL` /
    :data:`RC_VANISHED` / 1 (the archive couldn't be written at all), with a one-line reason.
    ``quiet`` prints nothing (the sweep reports on edges); ``announce`` prints the ▶/✓ lines."""
    dry = bool(os.environ.get("DRY_RUN"))
    dest_dir = _archive_dest(dest)
    try:
        src_fd = _open_source(anchor, src_dir)
    except FileNotFoundError:
        return 0, ""  # nothing bound out yet
    except _Refused as r:
        detail = f"{r.path} {r.why} — a link in the box-writable path is never followed"
        if not quiet:
            _err(f"✗ not archiving {what}: {detail}.")
        return RC_REFUSED, detail
    except OSError as e:
        detail = f"{src_dir}: {e.strerror or e}"
        if not quiet:
            _err(f"✗ archiving {what} failed: {detail}")
        return 1, detail
    try:
        if not os.listdir(src_fd):
            return 0, ""  # nothing worth keeping
        if announce and not quiet:
            print(f"▶ archiving {what} → {dest_dir}" + ("   (DRY RUN — no changes)" if dry else ""))
        tally = _mirror(src_fd, dest_dir, dry)
    except OSError as e:
        detail = f"{dest_dir}: {e.strerror or e}"
        if not quiet:
            _err(f"✗ archiving {what} failed: {detail}")
        return 1, detail
    finally:
        os.close(src_fd)
    rc = tally.rc
    if not quiet:
        _report_tally(tally, dry)
        if rc == 0 and announce:
            print(f"✓ {what} {'would be ' if dry else ''}archived → {dest_dir}")
        elif rc:
            _err(f"✗ archiving {what} incomplete (rc={rc}).")
    return rc, ("" if rc == 0 else tally.problem())


def bound_out_dir(checkout: Path, here: str) -> Path:
    """The host-side dir a box binds Claude's ``projects/`` out to. It survives ``nuke`` (a
    plain host dir), but is fragile to ``git clean -fdx`` and worktree removal — which is
    why destructive ops :func:`sync_archive` it to the durable store first."""
    return checkout / here / ".devbox-claude/projects"


def codex_bound_out_dir(checkout: Path, here: str) -> Path:
    """The host-side directory bound to Codex's resumable ``~/.codex/sessions`` tree."""
    return checkout / here / ".devbox-codex/sessions"


def sync_archive(src_dir: Path, *, anchor: Path, dest: str = "", what: str = "transcripts") -> int:
    """Additively copy one bound-out agent transcript dir → its durable host archive BEFORE a
    destructive op deletes the source. Returns 0 on success or nothing-to-do, non-zero when a copy
    was refused or incomplete. Never raises.

    ``anchor`` is the checkout ``src_dir`` lives in, as the HOST resolved it: everything below it is
    box-writable, so no link there is followed (a link on the way to ``src_dir`` refuses the copy;
    one inside it is skipped). No-op (0) inside the box (the archive lives in the host's home,
    unreachable there) and when ``src_dir`` is missing/empty."""
    if config.in_box():
        return 0
    return _archive(src_dir, anchor=anchor, dest=dest, what=what)[0]


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


def _sweep_plan() -> tuple[Path, list[tuple[str, float, Path, str]]]:
    """The bound checkout, and ``(label, interval, bound-out source, archive dest)`` for each
    configured agent whose auto-sync is ON, resolved under the CURRENTLY BOUND config — so the
    caller's ``with config.using(worktree_config(wt))`` is what points this at the right
    checkout."""
    checkout = config.repo_root()
    here = config.dev_vm_rel()
    plan: list[tuple[str, float, Path, str]] = []
    if config.claude_enabled() and (secs := config.claude_transcript_sync_seconds()):
        plan.append(("Claude", secs, bound_out_dir(checkout, here), ""))
    if config.codex_enabled() and (secs := config.codex_transcript_sync_seconds()):
        plan.append(
            ("Codex", secs, codex_bound_out_dir(checkout, here), str(_codex_archive_dest()))
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


def sweep(log, wt: str = "", *, tick_seconds: float = 2.0, notify=None) -> None:
    """One supervisor tick's auto-sync pass for the BOUND worktree's configured agents. Off (a
    no-op with no state touched) unless that checkout's ADOPTED config sets a positive
    ``transcript_sync_seconds`` — so a working-tree edit needs ``fy config adopt`` first, like every
    other host-side read. Never raises: one agent's failure must not wedge the reconcile loop."""
    if config.in_box():  # the archive is in the host's home, unreachable from the box
        return
    try:
        checkout, plan = _sweep_plan()
    except Exception as e:  # a config hiccup must never wedge the reconcile loop
        log(f"transcript-sync: sweep failed: {type(e).__name__}: {e}")
        return
    for label, interval, src, dest in plan:
        key = (wt, label)
        try:
            if not _due(key, interval, tick_seconds):
                continue
            rc, detail = _archive(src, anchor=checkout, dest=dest, what=label, quiet=True)
            if rc in BENIGN_RC:
                rc = 0
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
    src = Path(os.environ.get("DEVBOX_TRANSCRIPTS") or bound_out_dir(checkout, here))
    rc = sync_archive(src, anchor=checkout, what=what, dest=dest)

    if config.codex_enabled():
        codex_src = Path(
            os.environ.get("DEVBOX_CODEX_TRANSCRIPTS") or codex_bound_out_dir(checkout, here)
        )
        codex_what = "Codex transcripts" if what == "transcripts" else f"{what} (Codex transcripts)"
        codex_rc = sync_archive(
            codex_src,
            anchor=checkout,
            what=codex_what,
            dest=str(_codex_archive_dest(dest)),
        )
        rc = rc or codex_rc
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
    # The Claude bound-out dir on the host, honouring the same override as `devbox up`.
    src_dir = Path(os.environ.get("DEVBOX_TRANSCRIPTS") or bound_out_dir(checkout, here))
    # Default into the host's own history so `claude --resume` lists these in-box sessions.
    claude_dest = _archive_dest(dest)
    dry = bool(os.environ.get("DRY_RUN"))

    rc = _sync_source(
        src_dir,
        checkout,
        claude_dest,
        dry,
        engine=engine,
        engine_env=engine_env,
        box=box,
        box_home_cmd='printf %s "${CLAUDE_CONFIG_DIR:-$HOME/.claude}"',
        box_rel="projects",
        label="Claude transcripts",
        # A Codex-only project has no Claude source; continue on to its configured agent.
        missing_ok=config.codex_enabled(),
    )

    # A [codex] table opts into Codex installation and transcript persistence. Keep its archive in
    # Codex's native history directory so `codex resume` can see the sessions. For the legacy
    # explicit-destination form, use a child dir to avoid mixing two agents' unrelated layouts.
    if config.codex_enabled():
        codex_src = Path(
            os.environ.get("DEVBOX_CODEX_TRANSCRIPTS") or codex_bound_out_dir(checkout, here)
        )
        codex_dest = _codex_archive_dest(dest)
        codex_rc = _sync_source(
            codex_src,
            checkout,
            codex_dest,
            dry,
            engine=engine,
            engine_env=engine_env,
            box=box,
            box_home_cmd='printf %s "${CODEX_HOME:-$HOME/.codex}"',
            box_rel="sessions",
            label="Codex transcripts",
            missing_ok=rc == 0,
        )
        rc = rc or codex_rc
    return rc


def _sync_source(
    src_dir: Path,
    checkout: Path,
    dest_dir: Path,
    dry: bool,
    *,
    engine: str,
    engine_env: dict | None,
    box: str,
    box_home_cmd: str,
    box_rel: str,
    label: str,
    missing_ok: bool = False,
) -> int:
    """Sync one agent's bound history, falling back to its live in-box directory."""
    # Stage the source. Prefer the bound-out dir (already on the host, no engine needed; reached
    # from the checkout without following a link — see `_open_source`); if it's empty, fall back to
    # pulling the live projects/ tree out of the running box over the socket with `<engine> cp` —
    # projects/ ONLY, so creds never come along.
    tmp: str | None = None
    if _has_entries(checkout, src_dir):
        stage, anchor, origin = src_dir, checkout, "bound-out dir"
    elif engine_env is not None and _box_running(engine, box, engine_env):
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
        stage, anchor, origin = Path(tmp), Path(tmp), f"live box {box} ({box_src})"
    else:
        if missing_ok:
            print(f"✓ no {label.lower()} yet — nothing to do.")
            return 0
        _err(f"✗ nothing to copy: {src_dir} is empty and the box ({box}) isn't running.")
        return 1

    try:
        if not _has_entries(anchor, stage):
            print(f"✓ no {label.lower()} yet (source is empty) — nothing to do.")
            return 0

        print(f"▶ source: {origin}")
        print(f"  from:   {stage}")
        print(f"  to:     {dest_dir}" + ("   (DRY RUN — no changes)" if dry else ""))
        rc, _ = _archive(stage, anchor=anchor, dest=str(dest_dir), what=label, announce=False)
        if rc == 0:
            print(f"✓ {label} {'would be ' if dry else ''}synced → {dest_dir}")
        return rc
    finally:
        if tmp:
            shutil.rmtree(tmp, ignore_errors=True)
