"""Heal the SHARED git index after a box-side git moved HEAD (ADR-0021's follow-up).

The per-kernel index split (the box's git shim) ended the cross-kernel index corruption, but
left a deterministic illusion on the Mac: a box-side commit/checkout moves the SHARED HEAD
while the host's ``.git/index`` still describes the old one, so every host-side ``git status``
(and GUI) shows the gap as phantom staged deletions/modifications until a manual ``git reset``.
The supervisor sweeps this module once per tick to fast-forward that pure staleness away —
safe now precisely because the split made the host the shared index's ONLY writer kernel.

Heal conditions are deliberately strict (no false positives over convenience):

* Only when the BOX moved HEAD — attributed via ``<gitdir>/fy-box-head``, which the box shim
  stamps after any of its commands changes HEAD. "Index still matches the old HEAD" is ALSO
  the signature of a deliberate host-side ``git reset --soft``, so an unattributed move only
  resyncs the marker and never touches the index.
* Staged host work is carried forward entry-by-entry (git's own plumbing), and only when the
  new HEAD didn't touch those paths — a genuine overlap logs once and leaves everything alone.
* Never during an in-progress operation (merge/rebase/…), never against ``index.lock``, and
  the index is only replaced under our own ``index.lock`` (git's protocol, same kernel).

Sync files (derived state, safe to delete): ``<gitdir>/index.fy-head`` — the HEAD the shared
index's state was last intentional at; ``<gitdir>/fy-box-head`` — written by the box shim.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from . import config

SYNC_FILE = "index.fy-head"  # next to <gitdir>/index (the box's twin is index-box.head)
BOX_STAMP = "fy-box-head"  # written by the box git shim when a box command moves HEAD
IN_PROGRESS = (
    "rebase-merge",
    "rebase-apply",
    "MERGE_HEAD",
    "CHERRY_PICK_HEAD",
    "REVERT_HEAD",
    "BISECT_LOG",
)
ANCESTOR_SCAN = 32  # how far back a lost sync point is searched for (first-run / multi-hop)

# Conflict warnings repeat every tick while the state persists — log each (rec, cur) pair once.
_warned: dict[str, tuple[str, str]] = {}


def _git(repo: Path, *args: str, input_bytes: bytes | None = None, env: dict | None = None):
    """Raw git in ``repo``. FY_GIT_SHIM_OFF pins the REAL binary + the SHARED index even when
    this code runs where the box shim owns PATH (the in-box test suite) — on the Mac it's inert."""
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        input=input_bytes,
        text=input_bytes is None,
        env={**os.environ, "FY_GIT_SHIM_OFF": "1", **(env or {})},
    )


def _rev(repo: Path, spec: str) -> str | None:
    out = _git(repo, "rev-parse", "-q", "--verify", spec)
    return out.stdout.strip() if out.returncode == 0 else None


def _clean_vs(repo: Path, commit: str) -> bool:
    """True iff the shared index has nothing staged relative to ``commit``'s tree."""
    return _git(repo, "diff-index", "--cached", "--quiet", commit).returncode == 0


def _read(path: Path) -> str:
    try:
        return path.read_text().strip()
    except OSError:
        return ""


def _record(gitdir: Path, head: str) -> None:
    try:
        (gitdir / SYNC_FILE).write_text(head + "\n")
    except OSError:
        pass


def _matching_ancestor(repo: Path, cur: str) -> str | None:
    """A recent ancestor of HEAD whose tree the index exactly matches — pure staleness with a
    lost/multi-hop sync point, never a state anyone deliberately staged (see the shim's twin)."""
    out = _git(repo, "rev-list", f"-{ANCESTOR_SCAN}", cur)
    if out.returncode != 0:
        return None
    for commit in out.stdout.split():
        if _clean_vs(repo, commit):
            return commit
    return None


def _carry_entries(repo: Path, rec: str, cur: str) -> bytes | None:
    """The ``update-index --index-info`` payload that re-stages the host's staged-vs-``rec``
    work on top of ``cur``'s tree — or None when any staged path was ALSO changed by the new
    HEAD (a genuine overlap we refuse to guess about). Handles adds, mods, and staged deletes."""
    out = _git(repo, "diff-index", "--cached", "--name-only", "-z", rec)
    if out.returncode != 0:
        return None
    paths = [p for p in out.stdout.split("\0") if p]
    keep: list[str] = []
    removals: list[str] = []
    for p in paths:
        in_index = _rev(repo, f":0:{p}")
        in_new = _rev(repo, f"{cur}:{p}")
        if in_index == in_new:
            continue  # already absorbed into the new HEAD
        if _rev(repo, f"{rec}:{p}") != in_new:
            return None  # both sides touched it — never merge silently
        (removals if in_index is None else keep).append(p)
    payload = b""
    if keep:
        entries = _git(repo, "ls-files", "-z", "-s", "--", *keep)
        if entries.returncode != 0:
            return None
        payload += entries.stdout.encode()
    for p in removals:  # a staged deletion = the path absent from the index: a zero-mode entry
        payload += f"0 {'0' * len(cur)}\t{p}\0".encode()
    return payload


def _install_index(repo: Path, gitdir: Path, cur: str, carry: bytes) -> bool:
    """Build the fast-forwarded index in a temp file and install it under our own
    ``index.lock`` — git's own single-kernel protocol, so concurrent host git waits/fails
    politely instead of interleaving. False (retry next tick) if the lock is contended."""
    lock = gitdir / "index.lock"
    try:
        fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except OSError:
        return False  # someone's mid-write — never fight git for its own lock
    tmp = gitdir / "index.fy-tmp"
    try:
        env = {"GIT_INDEX_FILE": str(tmp)}
        if _git(repo, "read-tree", cur, env=env).returncode != 0:
            return False
        if carry:
            r = _git(repo, "update-index", "-z", "--index-info", input_bytes=carry, env=env)
            if r.returncode != 0:
                return False
        # Best-effort stat-cache refresh so the next `git status` doesn't re-hash the world;
        # rc deliberately ignored (carried entries that differ from the worktree "need update").
        _git(repo, "update-index", "-q", "--refresh", env=env)
        os.replace(tmp, gitdir / "index")
        return True
    finally:
        tmp.unlink(missing_ok=True)
        os.close(fd)
        lock.unlink(missing_ok=True)


def heal_checkout(repo: Path) -> str | None:
    """One conditional heal pass over ``repo``'s shared index. Returns a log-worthy message
    when something was healed or refused, None on the (overwhelmingly common) quiet no-op."""
    out = _git(repo, "rev-parse", "--absolute-git-dir")
    if out.returncode != 0:
        return None
    gitdir = Path(out.stdout.strip())
    if any((gitdir / m).exists() for m in IN_PROGRESS) or (gitdir / "index.lock").exists():
        return None  # an operation is in flight — its index state is git's, not ours
    cur = _rev(repo, "HEAD")
    if cur is None:
        return None  # unborn
    rec = _read(gitdir / SYNC_FILE)
    if rec == cur:
        return None
    if rec and _rev(repo, f"{rec}^{{commit}}") is None:
        rec = ""  # sync point predates a history rewrite — no rec at all
    box_moved = _read(gitdir / BOX_STAMP) == cur
    if not box_moved:
        # The host itself moved HEAD (commit, reset --soft, checkout…) — its own git already
        # put the index in the state it wanted. Only resync the marker; NEVER touch the index.
        _record(gitdir, cur)
        return None
    if _clean_vs(repo, cur):
        _record(gitdir, cur)  # index already matches the new HEAD — marker only
        return None
    base = rec if rec and _clean_vs(repo, rec) else _matching_ancestor(repo, cur)
    if base is not None:
        carry = b""  # pure staleness — the new HEAD's tree IS the whole index
    elif rec:
        carried = _carry_entries(repo, rec, cur)
        if carried is None:
            if _warned.get(str(gitdir)) != (rec, cur):
                _warned[str(gitdir)] = (rec, cur)
                return (
                    f"NOT healing {repo}: the box moved HEAD ({rec[:12]}… → {cur[:12]}…) but "
                    "staged host-side changes overlap it — resolve by hand (`git reset` keeps "
                    "all files, then re-stage)"
                )
            return None
        carry = carried
    else:
        _record(gitdir, cur)  # staged work, no sync point to diff against — assume intentional
        return None
    if not _install_index(repo, gitdir, cur, carry):
        return None  # contended/failed — state untouched, next tick retries
    _record(gitdir, cur)
    what = "staged work carried forward" if carry else "stale index fast-forwarded"
    # base (when set) is the commit the index actually matched — it may be an ancestor found
    # independently of a stale/lost rec, so it names the true fast-forward source; rec covers
    # the carry path (base is None there).
    src = (base or rec or "?")[:12]
    return f"{repo.name}: {what} after a box-side HEAD move ({src}… → {cur[:12]}…)"


def _checkouts() -> list[Path]:
    """The main checkout + every existing worktree (mirrors devmode.worktree_keys — heal is
    state-driven, so a checkout whose box is DOWN still gets its last commits' staleness fixed)."""
    from . import devmode  # lazy: keep import cost off anyone importing githeal alone

    main = devmode.main_repo()
    out = [main]
    wt_root = config.worktrees_root(main)
    if wt_root.is_dir():
        out += sorted(d for d in wt_root.iterdir() if d.is_dir() and (d / ".git").exists())
    return out


def sweep(log) -> None:
    """One supervisor tick's heal pass over every checkout. Gated on the index split being on
    (with the split OFF the box writes the shared index itself, and an unattended host-side
    writer would re-arm the very cross-kernel race the split removed). Never raises."""
    try:
        if not config.box_git_index_split():
            return
        checkouts = _checkouts()
    except Exception as e:  # a heal hiccup must never wedge the reconcile loop
        log(f"git-heal: sweep failed: {e}")
        return
    for co in checkouts:
        try:
            msg = heal_checkout(co)
            if msg:
                log(f"git-heal: {msg}")
        except Exception as e:  # one checkout's hiccup must not skip the rest
            log(f"git-heal: sweep failed: {e}")
