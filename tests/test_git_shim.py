"""The box git shim (assets/box/git-index-shim.sh), driven against REAL git in scratch repos.

Exercises the ADR-0021 behaviours end-to-end: the index split itself, and the staleness
self-heal — "host" commands run the real git binary (shared index), "box" commands run the
shim, exactly the two-writer topology of the shared checkout. Runs anywhere git exists
(including inside the dev box: the fixture resolves the REAL binary past any installed shim).
"""

from __future__ import annotations

import itertools
import os
import re
import shutil
import stat
import subprocess
import time
from pathlib import Path

import pytest

# Monotonic FUTURE mtimes for every Rig.write (see Rig.write): starts an hour ahead so file
# mtimes always exceed any index file's mtime, keeping git's racy-clean protection permanently
# armed. Class-wide is fine — xdist workers are separate processes.
_write_clock = itertools.count(int(time.time()) + 3600, 2)

SHIM = (
    Path(__file__).resolve().parents[1]
    / "src"
    / "foldyard"
    / "assets"
    / "box"
    / "git-index-shim.sh"
)


def _real_git() -> Path:
    """The real git binary — skipping any foldyard shim already installed on PATH (the dev box)."""
    for d in os.environ.get("PATH", "").split(os.pathsep):
        cand = Path(d) / "git"
        if not cand.is_file() or not os.access(cand, os.X_OK):
            continue
        try:
            head = cand.read_bytes()[:512]
        except OSError:
            continue
        if b"FY_GIT_SHIM" in head or b"foldyard git shim" in head:
            continue
        return cand.resolve()
    pytest.skip("no real git on PATH")


class Rig:
    """A scratch repo with the two writers: ``host()`` = real git (shared index),
    ``box()`` = the shim (index-box). Same working tree, same refs — the shared checkout."""

    def __init__(self, tmp: Path):
        real = _real_git()
        shim_bin = tmp / "bin"
        real_bin = tmp / "realbin"
        shim_bin.mkdir()
        real_bin.mkdir()
        self.shim = shim_bin / "git"
        shutil.copyfile(SHIM, self.shim)
        self.shim.chmod(self.shim.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        (real_bin / "git").symlink_to(real)
        self.real = real
        cfg = tmp / "gitconfig"
        cfg.write_text("[user]\n\tname = t\n\temail = t@t\n[init]\n\tdefaultBranch = main\n")
        self.env = {
            **{k: v for k, v in os.environ.items() if not k.startswith(("GIT_", "FY_GIT"))},
            "PATH": f"{shim_bin}:{real_bin}:/usr/bin:/bin",
            "GIT_CONFIG_GLOBAL": str(cfg),
            "GIT_CONFIG_NOSYSTEM": "1",
        }
        # As the box bootstrap does: the ref check's hooks dir, beside the shim.
        subprocess.run(
            [str(self.shim)], env={**self.env, "FY_GIT_SHIM_INSTALL_HOOKS": "1"}, check=True
        )
        self.hooks = tmp / "libexec" / "foldyard-git-hooks"
        self.repo = tmp / "repo"
        self.repo.mkdir()
        self.host("init", "-q", ".")

    def _run(self, exe, *args: str, env_extra: dict | None = None):
        return subprocess.run(
            [str(exe), *args],
            cwd=self.repo,
            env={**self.env, **(env_extra or {})},
            capture_output=True,
            text=True,
        )

    def host(self, *args: str):
        """The Mac side: real git, shared index."""
        r = self._run(self.real, *args)
        assert r.returncode == 0, f"host git {args}: {r.stderr}"
        return r

    def box(self, *args: str, env: dict | None = None):
        """The box side: the shim (may legitimately fail — callers assert)."""
        return self._run(self.shim, *args, env_extra=env)

    # ── plumbing helpers ────────────────────────────────────────────────────────────────
    @property
    def gitdir(self) -> Path:
        return self.repo / ".git"

    def head(self) -> str:
        return self.host("rev-parse", "HEAD").stdout.strip()

    def write(self, name: str, content: str) -> None:
        """Write + stamp a unique FUTURE mtime. The suite rewrites same-size files at machine
        speed, which lands in git's racy-stat blind spot: with the right (load-dependent)
        timestamp alignment, add/status trusted the stale cached stat and treated a really-
        changed file as unchanged — the rare pre-push failures where a box `git add` staged
        nothing or a healed status showed a phantom ` M`. A future mtime keeps the entry
        permanently racy, so git always re-hashes content instead of trusting stat."""
        p = self.repo / name
        p.write_text(content)
        t = next(_write_clock)
        os.utime(p, (t, t))

    def host_commit(self, msg: str, **files: str) -> str:
        for name, content in files.items():
            self.write(name, content)
        self.host("add", "--", *files)
        self.host("commit", "-qm", msg)
        return self.head()


@pytest.fixture
def rig(tmp_path):
    r = Rig(tmp_path)
    r.host_commit("init", fileA="a1\n", fileB="b1\n")
    return r


def test_split_box_commit_uses_own_index_and_stamps(rig):
    # A box commit lands in shared refs, writes index-box — never git-writing the shared index
    # (the split) — and stamps both sync files: the attribution the heals depend on. The shared
    # index is then healed to the new HEAD by the shim itself, under index.lock (githeal's twin).
    rig.write("fileC", "c1\n")
    assert rig.box("add", "fileC").returncode == 0
    assert rig.box("commit", "-qm", "box").returncode == 0
    head = rig.head()
    assert (rig.gitdir / "index-box").exists()
    assert rig.host("status", "--porcelain").stdout == ""  # healed at once, not on a tick
    assert (rig.gitdir / "index-box.head").read_text().strip() == head
    assert (rig.gitdir / "fy-box-head").read_text().strip() == head
    assert rig.box("status", "--porcelain").stdout == ""  # own commit: no illusion, no heal needed


def test_heal_after_host_commit(rig):
    # THE headline fix: host commits (modify + add) while the box index is seeded at the old
    # HEAD → box status used to show `MM` + staged-D/?? phantoms; now it self-heals to clean.
    rig.box("status", "--porcelain")  # seed index-box + sync point at the initial commit
    rig.host_commit("host", fileA="a2\n", fileC="c1\n")
    out = rig.box("status", "--porcelain")
    assert out.returncode == 0 and out.stdout == ""
    assert (rig.gitdir / "index-box.head").read_text().strip() == rig.head()


def test_heal_multi_hop_box_commit_then_host_commit(rig):
    # Box commits C2, host commits C3 on top: the box index (== C2's tree) is clean against
    # its recorded sync point, so it fast-forwards through the host commit too.
    rig.write("fileC", "c1\n")
    rig.box("add", "fileC")
    rig.box("commit", "-qm", "box C2")
    rig.host("reset", "-q")  # host absorbs C2 (its own ritual — not under test here)
    rig.host_commit("host C3", fileA="a3\n")
    out = rig.box("status", "--porcelain")
    assert out.returncode == 0 and out.stdout == ""


def test_box_soft_reset_is_never_healed_away(rig):
    # `git reset --soft HEAD~` deliberately leaves the index at the undone commit's content —
    # the same state-signature as staleness. Side attribution (fy-box-head) must keep the
    # heal's hands off, and the follow-up commit (the whole point of a soft reset) must pass.
    rig.write("fileC", "c1\n")
    rig.box("add", "fileC")
    rig.box("commit", "-qm", "to be squashed")
    assert rig.box("reset", "-q", "--soft", "HEAD~").returncode == 0
    out = rig.box("status", "--porcelain")
    assert "A  fileC" in out.stdout  # the soft reset's staged state survives
    assert rig.box("commit", "-qm", "squashed").returncode == 0  # guard must not misfire
    assert rig.box("status", "--porcelain").stdout == ""


def test_stale_with_staged_work_warns_and_blocks_commit(rig):
    # The one unhealable state: host moved HEAD AND the box's staged work touches a path the move
    # changed (disjoint staged work is carried). Status still runs (warn on stderr), but commit is
    # refused — it would revert the host's change or silently merge two.
    rig.box("status", "--porcelain")  # sync point at C1
    rig.write("fileA", "a-box\n")
    rig.box("add", "fileA")  # genuine box-side staged work, on the path the host's commit changes
    rig.host_commit("host", fileA="a2\n")
    st = rig.box("status", "--porcelain")
    assert st.returncode == 0 and "index-box is stale" in st.stderr
    commit = rig.box("commit", "-qm", "stale")
    assert commit.returncode == 1 and "refusing 'git commit'" in commit.stderr
    # FY_GIT_SHIM_STALE_OK is the deliberate override…
    ok = rig.box("commit", "-qm", "deliberate", env={"FY_GIT_SHIM_STALE_OK": "1"})
    assert ok.returncode == 0
    # …and the documented fix (git reset) resyncs, after which everything is normal again.
    rig.box("reset", "-q")
    assert rig.box("status", "--porcelain").stderr == ""


def test_the_memo_is_keyed_on_index_CONTENT_not_just_size(rig):
    """The memo was keyed on the index's SIZE, which is blind to a content-only change: an entry
    is fixed-width plus the path, so only the FIRST `git add` after a write moves the byte count
    (by invalidating cache-tree) — restaging the same path again lands on the size the first one
    already settled. Measured on a 3.6k-file checkout: three different staged contents of one
    file, all 564418 bytes.

    So an index that became healable without changing size stayed memoized as unfixable: phantom
    staged diffs from `status` and `commit` blocked, until something unrelated moved the size.
    Two adds of the same path is the smallest reproduction of that same-size transition."""
    memo = rig.gitdir / "index-box.stale"
    rig.box("status", "--porcelain")  # sync point at C1
    rig.write("fileA", "junk1\n")
    rig.box("add", "fileA")  # the path the host's commit below changes: unfixable, not carried
    rig.host_commit("host", fileA="a2\n")
    assert "index-box is stale" in rig.box("status", "--porcelain").stderr
    rig.write("fileA", "junk2\n")
    rig.box("add", "fileA")  # settles the index at its post-add size, still unfixable → memoized
    assert memo.exists()
    size_before = (rig.gitdir / "index-box").stat().st_size
    # Back to the COMMITTED content: the index is now byte-exactly the initial commit's tree, so
    # it is pure staleness with nothing to lose — at a size identical to the memoized one.
    rig.write("fileA", "a1\n")
    rig.box("add", "fileA")
    assert (rig.gitdir / "index-box").stat().st_size == size_before, (
        "the reproduction needs a same-size transition — a size change would mask the bug"
    )
    out = rig.box("status", "--porcelain")
    assert "index-box is stale" not in out.stderr, "a healable index must not stay behind the memo"
    # No phantom STAGED diff; fileA's unstaged change is real — the shared worktree holds the
    # box's last write (a1) over the host's committed a2.
    assert out.stdout == " M fileA\n"
    assert not memo.exists()  # …and the memo is cleared, not left as a lie on disk
    assert "refusing 'git commit'" not in rig.box("commit", "-qm", "unblocked").stderr


def test_staging_that_matches_an_UNRELATED_BRANCHS_COMMIT_is_not_reset_away(rig):
    """The heal's "the index tree is a commit, so there is nothing to lose" rung used to scan ALL
    REFS, which makes it a claim about the tree's existence rather than its provenance. Staging
    content that reproduces some other commit's tree is ordinary work (re-applying a patch,
    `git checkout <branch> -- .`, backing a WIP change out) — and against an all-refs scan any of
    those looked disposable, so the next host-side HEAD move silently `git reset`-ed the staging
    away. The tree must come from a state HEAD has actually BEEN at (its reflog); an unrelated
    branch's commit is not that, so the staging is treated as real work — carried onto the new
    HEAD (the move didn't touch it), never dropped.

    `feature` is built with plumbing on purpose: committing it the normal way would put it in
    HEAD's reflog, which is exactly the provenance being withheld here."""
    rig.box("status", "--porcelain")  # sync point at C1
    rig.write("fileB", "b-box\n")  # the content the box will deliberately stage, below
    # Build `feature` — a commit carrying exactly that content — through a scratch index, so no
    # ref the box is on and no HEAD move ever went near it.
    plumb = {"GIT_INDEX_FILE": str(rig.gitdir / "index-feature")}
    for args in (("read-tree", "HEAD"), ("update-index", "--add", "fileB")):
        r = rig._run(rig.real, *args, env_extra=plumb)
        assert r.returncode == 0, r.stderr
    tree = rig._run(rig.real, "write-tree", env_extra=plumb).stdout.strip()
    feature = rig._run(rig.real, "commit-tree", tree, "-p", rig.head(), "-m", "feature")
    assert feature.returncode == 0, feature.stderr
    rig.host("update-ref", "refs/heads/feature", feature.stdout.strip())
    (rig.gitdir / "index-feature").unlink()

    # The box stages that same content deliberately; the host then moves HEAD underneath it.
    assert rig.box("add", "fileB").returncode == 0
    rig.host_commit("host", fileA="a2\n")
    out = rig.box("status", "--porcelain")
    assert "M  fileB" in out.stdout, "the box's deliberate staging must survive the heal"
    assert rig.box("commit", "-qm", "x").returncode == 0
    changes = rig.host("diff-tree", "--no-commit-id", "-r", "--name-status", "HEAD").stdout
    assert changes == "M\tfileB\n"  # the real change, on the new HEAD: the host's fileA intact


def test_heal_finds_a_sync_point_OLDER_THAN_THE_RECENT_HISTORY(rig):
    """The scan briefly carried `--max-count=200` as a speed guard, but --max-count keeps the 200
    most RECENT entries — so an index matching a state nobody had touched for a couple hundred
    commits fell off the end and was declared unfixable, blocking commits. Unbounded buys that
    back for nothing measurable: 0.29s on a checkout with 842 reflog entries, dwarfed by process
    start-up.

    The noise is 210 bare HEAD moves (update-ref, one process each, no worktree churn): the walk
    is over reflog ENTRIES, so what has to sit outside the bound is the entry that put `old` at
    HEAD, not a chain of commits."""
    rig.box("status", "--porcelain")  # sync point at C1
    init = rig.head()
    old = rig.host_commit("old work", fileC="c1\n")
    rig.host("branch", "old-work", old)  # off main's line: not an ancestor of the HEAD below
    rig.host("reset", "-q", "--hard", init)
    filler = rig.host_commit("filler", fileA="a-filler\n")
    for i in range(210):
        rig.host("update-ref", "HEAD", init if i % 2 else filler)
    rig.host("update-ref", "HEAD", filler)
    # The box index carries old-work's tree, with its marker back at init — so neither the
    # against-HEAD nor the against-marker rung can heal it; only the unbounded reflog walk can.
    rig.box("read-tree", old, env={"FY_GIT_SHIM_NO_HEAL": "1"})
    (rig.gitdir / "index-box.head").write_text(init + "\n")
    out = rig.box("status", "--porcelain")
    assert "index-box is stale" not in out.stderr, "an old commit is still a commit"
    assert (rig.gitdir / "index-box.head").read_text().strip() == rig.head()


def test_reset_then_commit_recovers_from_stale_staged(rig):
    rig.box("status", "--porcelain")
    rig.write("fileA", "a-box\n")
    rig.box("add", "fileA")  # overlaps the host's commit: unhealable
    rig.host_commit("host", fileA="a2\n")
    assert rig.box("commit", "-qm", "x").returncode == 1
    assert rig.box("reset", "-q").returncode == 0
    rig.write("fileA", "a-box2\n")  # the host's commit rewrote the shared file: re-apply, re-stage
    rig.box("add", "fileA")
    ok = rig.box("commit", "-qm", "retry")
    assert ok.returncode == 0, ok.stderr
    assert rig.box("status", "--porcelain").stdout == ""


def test_staged_box_work_is_carried_over_a_host_head_move(rig):
    """Agents stage, then commit — and the host pulls, rebases or commits meanwhile. The box's own
    heal refused ANY staged work once the host had moved HEAD (commits blocked until a manual
    reset), though the host side has carried disjoint staged work since #39. Same rule here: the
    new HEAD's tree plus the box's staged paths, when the move didn't touch them."""
    rig.box("status", "--porcelain")
    rig.write("fileB", "b-box\n")
    rig.write("fileC", "c-box\n")
    assert rig.box("add", "fileB", "fileC").returncode == 0  # a modification and a new file
    host = rig.host_commit("host", fileA="a2\n")
    assert rig.box("status", "--porcelain").stdout == "M  fileB\nA  fileC\n"
    # recorded as synced at the new HEAD: the next call doesn't pay for the carry again
    assert (rig.gitdir / "index-box.head").read_text().strip() == host
    r = rig.box("commit", "-qm", "box")
    assert r.returncode == 0, r.stderr
    assert rig.host("rev-parse", "HEAD~").stdout.strip() == host
    changes = rig.host("diff-tree", "--no-commit-id", "-r", "--name-status", "HEAD").stdout
    assert changes == "M\tfileB\nA\tfileC\n"  # the host's fileA is not reverted
    assert rig.box("status", "--porcelain").stdout == ""


def test_a_carry_git_cant_answer_keeps_the_staging(rig, tmp_path):
    # The carry asks git how the staged paths read in the new HEAD. An answer it didn't get is not
    # "none of them changed": that installed the new HEAD's bare tree over the box's staging.
    rig.box("status", "--porcelain")
    rig.write("fileB", "b-box\n")
    assert rig.box("add", "fileB").returncode == 0
    rig.host_commit("host", fileA="a2\n")
    spy = tmp_path / "failing-git"
    spy.mkdir()
    (spy / "git").write_text(
        "#!/bin/sh\n"
        'case " $* " in *" diff-index "*" -- "*) exit 128 ;; esac\n'
        f'exec "{rig.real}" "$@"\n'
    )
    (spy / "git").chmod(0o755)
    rig.box("status", "--porcelain", env={"PATH": f"{rig.shim.parent}:{spy}:{rig.env['PATH']}"})
    assert rig.box("diff", "--cached", "--name-only").stdout == "fileB\n"


def _spy_on_the_carry(rig, tmp_path, script: str) -> dict:
    """A "real git" that runs ``script`` (shell, $@ = git's args) on the box carry's own git
    calls — the ones on its work copy of index-box — then execs the real git."""
    spy = tmp_path / "carry-spy"
    spy.mkdir()
    (spy / "git").write_text(
        "#!/bin/sh\n"
        f'case "${{GIT_INDEX_FILE:-}}" in *index-box.carry.*) {script} ;; esac\n'
        f'exec "{rig.real}" "$@"\n'
    )
    (spy / "git").chmod(0o755)
    return {"PATH": f"{rig.shim.parent}:{spy}:{rig.env['PATH']}"}


def test_a_box_write_during_the_carry_is_never_silently_lost(rig, tmp_path):
    # The carry copied index-box, built from the copy, and only then took index-box.lock: another
    # box git (the agent's `git add` while the editor's server runs `git status`) staging in
    # between was overwritten by the older copy. Under the lock first, that writer is refused
    # instead — loudly, its own to retry.
    rig.box("status", "--porcelain")
    rig.write("fileB", "b-box\n")
    assert rig.box("add", "fileB").returncode == 0
    rig.host_commit("host", fileA="a2\n")
    rig.write("fileE", "e-box\n")
    ix, rc = rig.gitdir / "index-box", tmp_path / "concurrent-rc"
    env = _spy_on_the_carry(
        rig,
        tmp_path,
        f'case " $* " in *" read-tree "*) [ -e "{rc}" ] || {{ env -u GIT_INDEX_FILE '
        f'GIT_INDEX_FILE="{ix}" "{rig.real}" -C "{rig.repo}" add fileE; echo $? >"{rc}"; }} ;; esac',
    )
    rig.box("status", "--porcelain", env=env)
    if rc.read_text().strip() == "0":  # the other writer succeeded: its staging must survive
        assert "A  fileE" in rig.box("status", "--porcelain").stdout


def test_a_carry_that_fails_for_a_moment_is_tried_again(rig, tmp_path):
    # Only an overlap is a verdict worth remembering. A git call that fails for a moment was
    # memoized as "can't be carried forward" on inputs that don't change, so commits stayed
    # refused until the index happened to change.
    rig.box("status", "--porcelain")
    rig.write("fileB", "b-box\n")
    assert rig.box("add", "fileB").returncode == 0
    rig.host_commit("host", fileA="a2\n")
    once = tmp_path / "failed-once"
    env = _spy_on_the_carry(rig, tmp_path, f'[ -e "{once}" ] || {{ : >"{once}"; exit 128; }}')
    first = rig.box("status", "--porcelain", env=env)
    assert "can't be carried forward" not in first.stderr
    r = rig.box("commit", "-qm", "box")
    assert r.returncode == 0, r.stderr
    changes = rig.host("diff-tree", "--no-commit-id", "-r", "--name-status", "HEAD").stdout
    assert changes == "M\tfileB\n"


def test_moving_the_checked_out_branch_back_says_how(rig):
    # Git sends no expected value for `checkout -B <current> <older>`, so the shim can't tell a
    # deliberate rewind from one computed off a stale HEAD, and refuses both — with nothing on
    # your computer having moved, "run it again" can't help. `git reset` sends one and passes.
    rig.box("status", "--porcelain")
    first = rig.head()
    rig.host_commit("second", fileA="a2\n")
    r = rig.box("checkout", "-q", "-B", "main", first)
    assert r.returncode != 0 and "git reset --hard" in r.stderr
    assert rig.box("reset", "-q", "--hard", first).returncode == 0
    assert rig.head() == first


def test_a_staged_deletion_is_carried_too(rig):
    rig.box("status", "--porcelain")
    assert rig.box("rm", "-q", "fileB").returncode == 0
    rig.host_commit("host", fileA="a2\n")
    r = rig.box("commit", "-qm", "box")
    assert r.returncode == 0, r.stderr
    changes = rig.host("diff-tree", "--no-commit-id", "-r", "--name-status", "HEAD").stdout
    assert changes == "D\tfileB\n"


@pytest.mark.parametrize("staged", [False, True], ids=["stale", "carried"])
def test_the_heal_never_runs_a_ref_writing_git(rig, tmp_path, staged):
    """The staleness heal ran `git reset`, which rewrites the checked-out branch with the value it
    READ. Over the VM mount that read can be stale, and the view flips back to fresh when the
    cache entry expires — mid-command, the host's file reappears and git writes its stale value
    over it: the rewind the hook exists to refuse, run by the shim itself, without the hook (that
    flip can't be staged outside the VM). So the heal rebuilds indexes with read-tree/update-index,
    which touch no ref — pinned by the calls it makes."""
    rig.box("status", "--porcelain")
    if staged:
        rig.write("fileC", "c-box\n")
        rig.box("add", "fileC")
    rig.host_commit("host", fileA="a2\n")
    log, spy = tmp_path / "calls", tmp_path / "spy"
    spy.mkdir()
    (spy / "git").write_text(f'#!/bin/sh\necho "$*" >>"{log}"\nexec "{rig.real}" "$@"\n')
    (spy / "git").chmod(0o755)
    r = rig.box("status", "--porcelain", env={"PATH": f"{rig.shim.parent}:{spy}:{rig.env['PATH']}"})
    assert r.returncode == 0 and "index-box is stale" not in r.stderr, r.stderr
    writers = ("reset", "update-ref", "checkout", "switch", "symbolic-ref", "branch", "commit")
    calls = [c.split() for c in log.read_text().splitlines()]
    assert [c for c in calls if c and c[0] in writers] == []
    assert rig.box("status", "--porcelain").stdout == ("A  fileC\n" if staged else "")


def test_upgrade_path_stale_index_box_without_sync_point(rig):
    # A pre-heal index-box (no .head, no stamp) that is stale — the ancestor scan recognises
    # it as pure staleness and heals, fixing existing checkouts on shim upgrade.
    shutil.copyfile(rig.gitdir / "index", rig.gitdir / "index-box")  # old shim's seed
    rig.host_commit("host", fileA="a2\n", fileC="c1\n")
    assert not (rig.gitdir / "index-box.head").exists()
    out = rig.box("status", "--porcelain")
    assert out.returncode == 0 and out.stdout == ""


def test_first_sight_with_staged_work_is_treated_as_intentional(rig):
    # No sync point + staged work that matches no recent commit → assumed intentional at the
    # current HEAD: recorded, kept, no warning. (The conservative init branch.)
    shutil.copyfile(rig.gitdir / "index", rig.gitdir / "index-box")
    rig.write("fileB", "b-box\n")
    subprocess.run(  # stage into index-box directly, as an old shim would have
        [str(rig.real), "add", "fileB"],
        cwd=rig.repo,
        env={**rig.env, "GIT_INDEX_FILE": str(rig.gitdir / "index-box")},
        check=True,
    )
    out = rig.box("status", "--porcelain")
    assert out.stderr == "" and "M  fileB" in out.stdout
    assert (rig.gitdir / "index-box.head").read_text().strip() == rig.head()


def test_heal_skipped_mid_operation(rig):
    # Sequencer/merge state lives in the index — mid-operation the heal must not touch it,
    # even when HEAD looks stale.
    rig.box("status", "--porcelain")
    rig.host_commit("host", fileA="a2\n")
    (rig.gitdir / "MERGE_HEAD").write_text(rig.head() + "\n")
    out = rig.box("status", "--porcelain")
    assert "fileA" in out.stdout  # illusion deliberately left in place
    (rig.gitdir / "MERGE_HEAD").unlink()
    assert rig.box("status", "--porcelain").stdout == ""  # …and heals once the op is done


def test_no_heal_escape_hatch(rig):
    rig.box("status", "--porcelain")
    rig.host_commit("host", fileA="a2\n")
    out = rig.box("status", "--porcelain", env={"FY_GIT_SHIM_NO_HEAL": "1"})
    assert "fileA" in out.stdout  # untouched staleness
    assert rig.box("status", "--porcelain").stdout == ""  # next unflagged call heals


def test_shim_off_and_explicit_index_passthrough(rig):
    # FY_GIT_SHIM_OFF and a caller's own GIT_INDEX_FILE both bypass injection (and healing).
    r = rig.box("rev-parse", "--git-dir", env={"FY_GIT_SHIM_OFF": "1"})
    assert r.returncode == 0
    other = rig.gitdir / "index-other"
    r = rig.box("add", "fileA", env={"GIT_INDEX_FILE": str(other)})
    assert r.returncode == 0 and other.exists()


def test_unborn_repo_and_outside_repo(tmp_path):
    rig = Rig(tmp_path)  # no commit yet: unborn HEAD
    rig.write("fileA", "a1\n")
    assert rig.box("add", "fileA").returncode == 0
    assert rig.box("commit", "-qm", "first").returncode == 0  # guard must not fire on unborn
    outside = tmp_path / "not-a-repo"
    outside.mkdir()
    r = subprocess.run(
        [str(rig.shim), "status"], cwd=outside, env=rig.env, capture_output=True, text=True
    )
    assert r.returncode != 0 and "not a git repository" in r.stderr.lower()


def test_heal_finds_a_sync_point_on_ANOTHER_BRANCH(rig):
    """The heal used to walk only `rev-list -32 HEAD`, so a sync point on a branch that is not
    an ancestor of HEAD was invisible — and switching branches host-side on a shared checkout is
    the COMMON way this index goes stale. The probe then declared "genuinely staged work" over a
    byte-exact snapshot of a real commit, and (caching nothing) re-paid the full scan on every
    single git call: 3.1s each on a 3.6k-file checkout over virtiofs, which starved the TUI's 1s
    refresh timer. What fixes the diagnosis (not just the cost) is matching the index's tree hash
    against the states HEAD has ACTUALLY BEEN AT — `rev-list -g HEAD`, in fy_matches_a_past_head —
    and a host-side branch switch is itself a HEAD move, so the shared reflog carries it. NOT all
    refs: a tree that merely exists on some never-visited branch is no evidence of staleness, and
    treating it as such would reset deliberately staged work (pinned by
    test_staging_that_matches_an_UNRELATED_BRANCHS_COMMIT_is_not_reset_away)."""
    rig.box("status", "--porcelain")  # sync point at the initial commit
    init = rig.head()
    # A side branch the box's index will end up matching, then back to main and move it on.
    rig.host("checkout", "-q", "-b", "side")
    side = rig.host_commit("side work", fileC="c1\n")
    rig.host("checkout", "-q", "main")
    rig.host_commit("main work", fileA="a2\n")
    # The box index carries side's tree exactly (what a host-side branch switch leaves behind),
    # with its marker still back at init. NO_HEAL + a hand-written marker on purpose: letting the
    # shim run its heal during setup would resync the marker to HEAD and the assertion below
    # would pass vacuously against ANY shim (it did, first time round).
    rig.box("read-tree", side, env={"FY_GIT_SHIM_NO_HEAL": "1"})
    (rig.gitdir / "index-box.head").write_text(init + "\n")
    out = rig.box("status", "--porcelain")
    assert out.returncode == 0
    assert "index-box is stale" not in out.stderr, "a cross-branch sync point must heal, not warn"
    assert (rig.gitdir / "index-box.head").read_text().strip() == rig.head()


def test_index_irrelevant_subcommands_skip_the_heal(rig):
    """rev-parse and friends never READ the index, so a stale one cannot affect them and healing
    them is pure latency — which matters because they are foldyard's hot path (the TUI runs them
    on a 1s timer, on the event loop). They must pass through silently even while stale, WITHOUT
    healing it away behind the user's back."""
    rig.box("status", "--porcelain")
    rig.write("fileA", "a-box\n")
    rig.box("add", "fileA")  # overlaps the host's commit: unhealable
    rig.host_commit("host", fileA="a2\n")
    before = (rig.gitdir / "index-box").read_bytes()
    for sub in ("rev-parse", "rev-list", "for-each-ref", "config", "log"):
        args = {
            "rev-parse": ("HEAD",),
            "rev-list": ("-1", "HEAD"),
            "log": ("-1", "--oneline"),
            "config": ("--get", "user.name"),
        }
        r = rig.box(sub, *args.get(sub, ()))
        assert r.returncode == 0, f"{sub}: {r.stderr}"
        assert "index-box is stale" not in r.stderr, (
            f"{sub} healed/warned but never reads the index"
        )
    assert (rig.gitdir / "index-box").read_bytes() == before, (
        "an allowlisted verb mutated the index"
    )
    # …and a command that DOES read the index still gets the full treatment.
    assert "index-box is stale" in rig.box("status", "--porcelain").stderr


def test_unfixable_verdict_is_memoized_and_invalidated_by_staging(rig):
    """The unfixable verdict is the expensive one to reach and the only rung that records no sync
    point (there is no reconciliation to claim) — so uncached it was re-derived on EVERY call for
    as long as the state lasted. It is memoized on (rec, cur, fy_index_sig) — the index's identity
    is its SIGNATURE (size plus the trailing checksum), not its size alone, precisely so a
    content-only restage still invalidates. Staging must invalidate it, or a state that became
    healable would stay stuck behind a stale memo."""
    memo = rig.gitdir / "index-box.stale"
    rig.box("status", "--porcelain")
    rig.write("fileA", "a-box\n")
    rig.box("add", "fileA")  # overlaps the host's commit: unhealable
    rig.host_commit("host", fileA="a2\n")
    assert not memo.exists()
    for _ in range(3):  # the warning must survive memoization, not just the first time
        assert "index-box is stale" in rig.box("status", "--porcelain").stderr
    assert memo.exists()
    sig = memo.read_text()
    rig.write("fileC", "c-box\n")
    rig.box("add", "fileC")  # a new entry moves the index size
    rig.box("status", "--porcelain")
    assert memo.read_text() != sig, "staging must invalidate the memo"
    # Healing the state clears the memo rather than leaving a lie on disk.
    rig.box("reset", "-q")
    assert rig.box("status", "--porcelain").stderr == ""


def _record_offer(rig, plant):
    rig.box("status", "--porcelain")
    head = rig.host_commit("host", fileA="a2\n")
    plant(f"index.fy-record.{head}")
    return rig.box("status", "--porcelain")


def _sync_point(rig, plant):
    plant("index-box.head")
    return rig.box("status", "--porcelain")


def _box_stamp(rig, plant):
    plant("fy-box-head")
    rig.write("fileC", "c1\n")
    rig.box("add", "fileC")
    return rig.box("commit", "-qm", "box")


def _stale_memo(rig, plant):
    rig.box("status", "--porcelain")
    rig.write("fileA", "a-box\n")
    rig.box("add", "fileA")  # overlaps the host's commit: unhealable
    rig.host_commit("host", fileA="a2\n")
    plant("index-box.stale")
    return rig.box("status", "--porcelain")


@pytest.mark.parametrize(
    "scenario",
    [_record_offer, _sync_point, _box_stamp, _stale_memo],
    ids=["record-offer", "sync-point", "box-stamp", "stale-memo"],
)
def test_a_best_effort_write_that_fails_says_nothing(rig, tmp_path, scenario):
    """Every file the shim writes on the side is best effort — and over the VM mount a create can
    fail where the name reads absent: on podman machine's libkrun a name the HOST just unlinked
    (the githeal tick consumes index.fy-record.<head>) is a stale entry for up to ~5 s, so
    `[ -e ]` says no and `: >name` says ENOENT. Redirections apply left to right, so a trailing
    `2>/dev/null` never covered the failing one: the error reached the agent's terminal on a
    command that worked. A dangling symlink is that exact state."""

    def plant(name: str) -> None:
        (rig.gitdir / name).symlink_to(tmp_path / "gone" / name)

    r = scenario(rig, plant)
    assert r.returncode == 0, r.stderr
    assert "No such file or directory" not in r.stderr


def test_no_best_effort_write_redirects_its_own_stderr_after_the_fact():
    """The same rule for the writes no scenario above reaches (index.fy-refused.* needs the blob
    id of an index copy), and for reads: ``>file 2>/dev/null`` / ``<file 2>/dev/null`` leak the
    failed open, ``{ >file; } 2>/dev/null`` doesn't."""
    leaky = re.compile(r"""(?<![0-9&<])(>>?|<)\s*("[^"]*"|'[^']*'|[^\s;|&)}]+)\s+2>/dev/null""")
    hits = [
        f"{n}: {line.strip()}"
        for n, line in enumerate(SHIM.read_text().splitlines(), 1)
        if leaky.search(line) and not line.lstrip().startswith("#")
    ]
    assert hits == []


def _fake_ln(rig, tmp_path: Path, stale: Path | None = None, on: int = 1) -> tuple[dict, Path]:
    """An `ln` that logs every name the shim refreshes. With ``stale``, the name is moved aside
    first and put back on the ``on``-th refresh of it: this kernel's view right after a HOST
    replace over the VM mount — the name reads absent until a fresh lookup finds the host's file
    (``on`` > 1: the host replaced it again after the earlier refreshes)."""
    bin_, log, aside = tmp_path / "fake-ln", tmp_path / "ln.log", tmp_path / "aside"
    bin_.mkdir()
    target = f"{stale.parent.resolve()}/{stale.name}" if stale else ""
    if stale:
        stale.rename(aside)
    (bin_ / "ln").write_text(
        "#!/bin/sh\n"
        'for n; do :; done\necho "$n" >>"' + str(log) + '"\n'
        'if [ "$(cd "$(dirname "$n")" && pwd -P)/$(basename "$n")" = "' + target + '" ]; then\n'
        '  echo x >>"' + str(tmp_path / "ln.count") + '"\n'
        '  [ "$(wc -l <"'
        + str(tmp_path / "ln.count")
        + f'")" -ge {on} ] && [ -e "'
        + str(aside)
        + '" ] && mv "'
        + str(aside)
        + '" "'
        + target
        + '"\n'
        "fi\n"
        "exit 1\n"
    )
    (bin_ / "ln").chmod(0o755)
    return {"PATH": f"{bin_}:{rig.env['PATH']}"}, log


def _stale_during(
    rig, tmp_path: Path, sub: str, packed: bool, same: bool = False
) -> tuple[dict, str, str]:
    """The race the shim's own refreshes can't close: the host moves the branch to a new commit
    WHILE git runs, after every refresh, and this kernel then reads the loose ref as absent — git
    falls back to packed-refs (``packed``) or sees an unborn branch. Plays it with a "real git"
    that, on the user's ``sub`` (the call carrying our hooks, not the heal's own git), swaps the
    loose ref for its stale view (the host's new value waits aside),
    and an `ln` whose forced lookup — the hook's — finds the host's file. → (env, the branch as
    the box last saw it, the host's new value); `_settled` then shows what's really there."""
    rig.box("status", "--porcelain")
    if packed:
        rig.host("pack-refs", "--all")
    seen = rig.host_commit("host", fileA="a2\n")  # the branch as the box last saw it
    ref = rig.gitdir / "refs" / "heads" / "main"
    moved = rig.host("commit-tree", "-p", "HEAD", "-m", "host again", "HEAD^{tree}").stdout.strip()
    if same:  # the host rewrites the branch's file with the value it already had
        moved = seen
    env, _ = _fake_ln(rig, tmp_path)  # logging only: nothing is stale until git runs
    aside = tmp_path / "aside"
    stale = tmp_path / "stale-git"
    stale.mkdir()
    (stale / "git").write_text(
        "#!/bin/sh\n"
        f'case " $* " in *core.hooksPath=*" {sub} "*) [ -e "{tmp_path}/staled" ] || {{\n'
        f'  : >"{tmp_path}/staled"; rm -f "{ref}"; echo {moved} >"{aside}"; }} ;; esac\n'
        f'exec "{rig.real}" "$@"\n'
    )
    (stale / "git").chmod(0o755)
    ln = tmp_path / "fake-ln" / "ln"
    ln.write_text(
        "#!/bin/sh\n"
        "for n; do :; done\n"
        f'[ "$(cd "$(dirname "$n")" && pwd -P)/$(basename "$n")" = "{ref.parent.resolve()}/main" ] &&'
        f' [ -f "{aside}" ] && mv "{aside}" "{ref}"\n'
        "exit 1\n"
    )
    env["PATH"] = f"{env['PATH'].split(':')[0]}:{stale}:{rig.env['PATH']}"
    return env, seen, moved


def _settled(rig, tmp_path: Path) -> str:
    """The branch as it really is: the host's file, whether or not a lookup refreshed it."""
    if (tmp_path / "aside").exists():
        (tmp_path / "aside").rename(rig.gitdir / "refs" / "heads" / "main")
    return rig.host("rev-parse", "main").stdout.strip()


def test_a_box_commit_sees_the_branch_the_host_just_moved(rig, tmp_path):
    """Host git moves a branch by replacing its loose ref, and over the VM mount the box then read
    the name as absent for up to ~5 s — git falls back to packed-refs, so a box commit parented on
    the PACKED commit and moved the branch there, dropping the host's (measured on podman
    machine: with nothing packed it made a root commit). The shim refreshes the names a command
    resolves before running it."""
    rig.box("status", "--porcelain")
    rig.host("pack-refs", "--all")
    moved = rig.host_commit("host", fileA="a2\n")
    env, _ = _fake_ln(rig, tmp_path, stale=rig.gitdir / "refs" / "heads" / "main")
    r = rig.box("commit", "-q", "--allow-empty", "-m", "box", env=env)
    assert r.returncode == 0, r.stderr
    assert rig.host("rev-parse", "HEAD~").stdout.strip() == moved


def test_a_host_move_during_the_heal_is_healed_again_not_committed_as_a_revert(rig, tmp_path):
    """The heal takes long enough for the host to move HEAD meanwhile. Healed for the old HEAD, the
    index committed on the new one would silently revert the host's commit — so after the heal the
    shim looks again (a second refresh finds the move) and heals again."""
    rig.box("status", "--porcelain")
    rig.host("pack-refs", "--all")
    moved = rig.host_commit("host", fileA="a2\n")
    env, _ = _fake_ln(rig, tmp_path, stale=rig.gitdir / "refs" / "heads" / "main", on=2)
    r = rig.box("commit", "-q", "--allow-empty", "-m", "box", env=env)
    assert r.returncode == 0, r.stderr
    assert rig.host("rev-parse", "HEAD~").stdout.strip() == moved
    assert rig.host("show", "HEAD:fileA").stdout == "a2\n"  # the host's change, not reverted


@pytest.mark.parametrize("packed", [True, False], ids=["stale-parent", "root-commit"])
@pytest.mark.parametrize("verify", [[], ["--no-verify"]], ids=["hooks", "no-verify"])
def test_a_commit_git_prepared_on_a_stale_branch_moves_nothing(rig, tmp_path, packed, verify):
    """Measured on podman machine: a box commit that read the branch in its stale window parented
    on the packed commit or made a ROOT commit, and moved the branch there — git's under-lock
    re-read is as stale as its first. The shim's `reference-transaction` hook re-reads it after a
    forced lookup while git holds the lock, and aborts: the host's commit stays the tip. Hooks
    `--no-verify` skips don't include this one."""
    env, _, moved = _stale_during(rig, tmp_path, "commit", packed)
    r = rig.box("commit", "-q", *verify, "--allow-empty", "-m", "box", env=env)
    assert r.returncode != 0
    assert "foldyard git shim: not moving refs/heads/main" in r.stderr
    assert _settled(rig, tmp_path) == moved


def test_a_branch_with_history_that_reads_unborn_never_gets_a_root_commit(rig, tmp_path):
    """The host's `pull --rebase` finished (HEAD back on the branch, the branch file replaced) and
    the box read the branch as UNBORN — the shim and git alike, so the heal token (it saw unborn
    too) and the zero-old check (unborn = unborn) both passed, and a box commit made a ROOT commit
    that knocked the host's HEAD off its branch (libkrun workflow test 2). A branch whose own
    reflog has entries isn't unborn: that read is a stale view, refused before git runs."""
    rig.box("status", "--porcelain")
    head = rig.head()
    env, _ = _fake_ln(rig, tmp_path, stale=rig.gitdir / "refs" / "heads" / "main", on=99)
    rig.write("fileC", "c\n")
    r = rig.box("commit", "-q", "--allow-empty", "-m", "box", env=env)
    assert r.returncode != 0 and "foldyard git shim" in r.stderr
    (tmp_path / "aside").rename(rig.gitdir / "refs" / "heads" / "main")
    assert rig.head() == head
    assert rig.host("rev-list", "--max-parents=0", "--all").stdout.split() == [
        rig.host("rev-list", "--max-parents=0", "HEAD").stdout.strip()
    ]


def test_a_host_operation_that_starts_while_the_box_commits_stops_the_commit(rig):
    """The shim refuses a box commit while the host is mid-rebase — but it asks BEFORE git runs,
    and a host `pull --rebase` that started after that moved nothing the check could see: the box
    commit moved the branch under the rebase, and the host's rebase then failed to finish (its own
    compare-and-swap: "is at <box's> but expected <its start>"; libkrun workflow test 2, traced:
    the box's transaction itself saw `rebase-merge`). Asked again under git's ref lock."""
    rig.box("status", "--porcelain")
    head = rig.head()
    hooks = rig.gitdir / "hooks"
    hooks.mkdir(exist_ok=True)
    (hooks / "pre-commit").write_text(f'#!/bin/sh\nmkdir "{rig.gitdir / "rebase-merge"}"\n')
    (hooks / "pre-commit").chmod(0o755)
    rig.write("fileC", "c\n")
    rig.box("add", "fileC")
    r = rig.box("commit", "-qm", "box")
    (hooks / "pre-commit").unlink()
    (rig.gitdir / "rebase-merge").rmdir()
    assert r.returncode != 0 and "middle of a git operation" in r.stderr
    assert rig.head() == head


@pytest.mark.parametrize("where", ["same-repo", "via-C"])
def test_a_repo_hooks_tools_see_the_repos_own_hooks_not_ours(rig, where):
    """Git hands our `-c core.hooksPath` down to repo hooks (GIT_CONFIG_PARAMETERS), so lefthook's
    auto-sync (`git rev-parse --git-path hooks`) found OUR directory and wrote its hook scripts
    into it: with a writable install, through the symlinks OVER THE SHIM (lefthook 2.0.15 — the
    commit's own checks then silently skipped every file); root-owned, "could not replace the
    hook: permission denied" on every commit. A git call that never moves a ref runs without our
    override; a ref move keeps it (the check must still see it)."""
    rig.box("status", "--porcelain")
    out = rig.gitdir / "hook-saw"
    hooks = rig.gitdir / "hooks"
    hooks.mkdir(exist_ok=True)
    c = f'-C "{rig.repo}" ' if where == "via-C" else ""
    (hooks / "pre-commit").write_text(
        "#!/bin/sh\n"
        f'git {c}rev-parse --path-format=absolute --git-path hooks >"{out}"\n'
        f'git {c}config core.hooksPath >>"{out}"\n'
        "exit 0\n"
    )
    (hooks / "pre-commit").chmod(0o755)
    rig.write("fileC", "c\n")
    rig.box("add", "fileC")
    r = rig.box("commit", "-qm", "box")
    assert r.returncode == 0, r.stderr
    assert out.read_text() == f"{hooks.resolve()}\n"


def test_a_repo_hooks_ref_move_still_goes_through_our_hooks(rig):
    # The other half: a ref-moving git inside a repo hook keeps our override, so its transaction
    # still runs our under-lock check. Observable in the repo's own reference-transaction hook: run
    # through our directory, the `git` on its PATH is foldyard-git-bin's; run natively, git's own.
    rig.box("status", "--porcelain")
    rig.host("branch", "side")
    log = rig.gitdir / "rt-git"
    hooks = rig.gitdir / "hooks"
    hooks.mkdir(exist_ok=True)
    (hooks / "pre-commit").write_text("#!/bin/sh\ngit branch -f side HEAD\n")
    (hooks / "reference-transaction").write_text(
        f'#!/bin/sh\ncat >/dev/null\n[ "$1" = prepared ] && command -v git >>"{log}"\nexit 0\n'
    )
    for h in ("pre-commit", "reference-transaction"):
        (hooks / h).chmod(0o755)
    rig.write("fileC", "c\n")
    rig.box("add", "fileC")
    r = rig.box("commit", "-qm", "box")
    assert r.returncode == 0, r.stderr
    seen = log.read_text().splitlines()
    assert len(seen) >= 2  # the hook's `branch -f`, and the commit's own
    assert all(g.endswith("/foldyard-git-bin/git") for g in seen), seen


def test_the_boxs_own_pull_rebase_is_its_own_operation(rig):
    # `git pull` runs its rebase as git's own child: unless the box marks the pull as its own
    # operation, the rebase's last move of the branch finds `rebase-merge` and is refused.
    rig.box("status", "--porcelain")
    rig.host("branch", "upstream")
    rig.host("checkout", "-q", "upstream")
    rig.host_commit("theirs", fileU="u\n")
    rig.host("checkout", "-q", "main")
    rig.write("fileC", "c\n")
    rig.box("add", "fileC")
    assert rig.box("commit", "-qm", "ours").returncode == 0
    r = rig.box("pull", "-q", "--rebase", ".", "upstream")
    assert r.returncode == 0, r.stderr
    assert rig.host("log", "-2", "--format=%s", "main").stdout.split() == ["ours", "theirs"]


def test_an_orphan_branch_still_takes_its_first_commit(rig):
    rig.box("status", "--porcelain")
    assert rig.box("checkout", "-q", "--orphan", "fresh").returncode == 0
    r = rig.box("commit", "-q", "--allow-empty", "-m", "first")
    assert r.returncode == 0, r.stderr
    assert rig.host("rev-list", "--count", "fresh").stdout.strip() == "1"


def test_any_ref_move_on_a_stale_read_is_refused_not_only_a_commit(rig, tmp_path):
    env, seen, moved = _stale_during(rig, tmp_path, "reset", packed=True)
    r = rig.box("reset", "-q", "--soft", seen, env=env)  # from the stale view: packed → seen
    assert r.returncode != 0 and "not moving refs/heads/main" in r.stderr
    assert _settled(rig, tmp_path) == moved


@pytest.mark.parametrize("args", [[], ["--hard"], ["--soft"]], ids=["mixed", "hard", "soft"])
def test_a_reset_to_head_on_a_stale_read_rewinds_nothing(rig, tmp_path, args):
    """`git reset` (to HEAD) gives git no expected old value — its ref update is zero-old, which the
    hook can't judge in general (`branch -f`, `tag` send the same). But it moves the CHECKED-OUT
    branch to what git read as HEAD: through the stale view, the packed commit, rewinding every
    host commit since (measured with Tangible's lefthook: 5 host commits lost). The shim's recovery
    advice is `git reset`, so this is the common case. A zero-old move of the checked-out branch
    is held to the HEAD the shim resolved before git ran."""
    env, _, moved = _stale_during(rig, tmp_path, "reset", packed=True)
    r = rig.box("reset", "-q", *args, env=env)
    assert r.returncode != 0 and "not moving refs/heads/main" in r.stderr
    assert _settled(rig, tmp_path) == moved


@pytest.mark.parametrize("sub", ["update-ref", "checkout"])
def test_a_zero_old_move_of_the_checked_out_branch_is_held_to_the_head_the_shim_saw(
    rig, tmp_path, sub
):
    """Some git versions send no expected value for a move of the checked-out branch (box git 2.47
    for `reset`; every version for `update-ref <ref> <value>` and `checkout -B`). Such a move
    landing after the host moved the branch meanwhile is refused; a retry sees the new state."""
    env, seen, moved = _stale_during(rig, tmp_path, sub, packed=True)
    args = ["refs/heads/main", seen] if sub == "update-ref" else ["-q", "-B", "main", seen]
    r = rig.box(sub, *args, env=env)
    assert r.returncode != 0 and "not moving refs/heads/main" in r.stderr
    assert _settled(rig, tmp_path) == moved


def test_a_zero_old_move_after_the_host_rewrote_the_same_value_is_not_refused(rig, tmp_path):
    # The host replaces the branch's file with the SAME commit while the box's command runs (a
    # fresh file: `update-ref` to itself, a GUI's rewrite). Through the stale view it reads as the
    # older packed commit, so without the forced lookup the move would be refused for nothing.
    env, seen, _ = _stale_during(rig, tmp_path, "update-ref", packed=True, same=True)
    target = rig.host("commit-tree", "-p", seen, "-m", "box", f"{seen}^{{tree}}").stdout.strip()
    r = rig.box("update-ref", "refs/heads/main", target, env=env)
    assert r.returncode == 0, r.stderr
    assert _settled(rig, tmp_path) == target


@pytest.mark.parametrize(
    "args",
    [["update-ref", "refs/heads/main", "HEAD"], ["checkout", "-q", "-B", "main"]],
    ids=["update-ref", "checkout-B"],
)
def test_a_zero_old_move_git_computed_from_a_stale_head_is_refused(rig, tmp_path, args):
    """The shim read the branch fresh, but git's OWN read of HEAD came later and was stale (the
    host's file replaced in between, or a name the refresh didn't reach) — and a zero-old move
    takes its target from that read. The branch hadn't moved since the shim looked, so "is it
    still what the shim saw" passed and git wrote the old value: box git 2.47's `git reset -q`
    rewound 7 host commits this way under load (reflog: the tip → an older commit, while the
    shim's check held). A target that isn't the HEAD the shim saw, nor ahead of it, is refused."""
    env, seen, _ = _stale_during(rig, tmp_path, args[0], packed=True, same=True)
    r = rig.box(*args, env=env)
    assert r.returncode != 0 and "not moving refs/heads/main" in r.stderr
    assert _settled(rig, tmp_path) == seen


@pytest.mark.parametrize("where", ["top", "subdir", "worktree"])
def test_a_head_the_host_just_rewrote_does_not_hide_the_repository(rig, tmp_path, where):
    """A host checkout/rebase/stash rewrites HEAD, and over the VM mount the box can read the name
    as absent — git's discovery then says "not a git repository" (16 agent retries in one 30-s
    workflow run). Discovery runs before the shim's refresh, so on a failure the shim refreshes
    `.git` and `.git/HEAD` up the tree from where git starts (honouring -C, and a worktree's
    gitdir file) and asks once more."""
    rig.box("status", "--porcelain")
    args, head = [], rig.gitdir / "HEAD"
    if where == "subdir":
        (rig.repo / "sub").mkdir()
        args = ["-C", "sub"]
    if where == "worktree":
        wt = tmp_path / "wt"
        rig.host("worktree", "add", "-q", str(wt), "-b", "side")
        args, head = ["-C", str(wt)], rig.gitdir / "worktrees" / "wt" / "HEAD"
    env, _ = _fake_ln(rig, tmp_path, stale=head)
    r = rig.box(*args, "status", "--porcelain", env=env)
    assert r.returncode == 0, r.stderr
    assert head.exists()


@pytest.mark.parametrize("how", ["-C", "cwd"])
def test_a_nested_repo_that_reads_absent_is_not_mistaken_for_the_outer_one(rig, tmp_path, how):
    """A nested repo whose `.git` the VM reads as absent for a moment makes git's discovery walk
    UP — and a box commit landed in the OUTER repo (7 agent commits in the e2e fixture's own repo,
    session 7's Lima run; the probe repo's directory had been replaced). Discovery that succeeds
    above where the command started re-asks every `.git` on the way first."""
    rig.box("status", "--porcelain")
    outer = rig.head()
    inner = rig.repo / "inner"
    inner.mkdir()
    rig._run(rig.real, "-C", str(inner), "init", "-q")
    rig._run(rig.real, "-C", str(inner), "commit", "-q", "--allow-empty", "-m", "inner init")
    env, _ = _fake_ln(rig, tmp_path, stale=inner / ".git")
    if how == "-C":
        r = rig.box("-C", "inner", "commit", "-q", "--allow-empty", "-m", "box", env=env)
    else:
        r = subprocess.run(
            [str(rig.shim), "commit", "-q", "--allow-empty", "-m", "box"],
            cwd=inner,
            env={**rig.env, **env},
            capture_output=True,
            text=True,
        )
    assert r.returncode == 0, r.stderr
    assert rig.head() == outer  # nothing landed in the outer repo
    assert rig._run(rig.real, "-C", str(inner), "log", "-1", "--format=%s").stdout.strip() == "box"


def test_a_command_at_the_repos_top_asks_nothing_more(rig, tmp_path):
    # The nested-repo check re-asks `.git` names only when git's repo is ABOVE where the command
    # started: at a repo's top — nearly every call — it costs nothing.
    rig.box("status", "--porcelain")
    env, log = _fake_ln(rig, tmp_path)
    assert rig.box("commit", "-q", "--allow-empty", "-m", "box", env=env).returncode == 0
    assert f"{rig.gitdir}\n" not in log.read_text()


def test_a_zero_old_move_of_another_branch_is_still_not_second_guessed(rig):
    rig.box("status", "--porcelain")
    rig.host("branch", "side")
    first = rig.host("rev-list", "--max-parents=0", "HEAD").stdout.strip()
    r = rig.box("branch", "-f", "side", first)
    assert r.returncode == 0, r.stderr
    assert rig.host("rev-parse", "side").stdout.strip() == first


def _host_commit_script(rig, path: str) -> str:
    """Shell that makes a HOST commit adding ``path`` with the real git, off to the side (a
    scratch index, no worktree write) — the host committing while a box command runs."""
    scratch = rig.gitdir / "index-host-scratch"
    real = f'env -u GIT_INDEX_FILE -u GIT_CONFIG_PARAMETERS -u GIT_CONFIG_COUNT "{rig.real}" -C "{rig.repo}"'
    return (
        f'{real} read-tree --index-output="{scratch}" HEAD && '
        f"b=$(echo host | {real} hash-object -w --stdin) && "
        f'GIT_INDEX_FILE="{scratch}" {real} update-index --add --cacheinfo 100644,$b,{path} && '
        f't=$(GIT_INDEX_FILE="{scratch}" {real} write-tree) && '
        f'c=$({real} commit-tree -p HEAD -m host "$t") && '
        f'{real} update-ref refs/heads/main "$c" && rm -f "{scratch}"'
    )


def _index_box_lacks(rig, path: str) -> bool:
    return f"D  {path}" in rig.box("status", "--porcelain").stdout


def test_a_refused_box_commit_that_raced_a_host_commit_records_nothing(rig):
    """The post-command step took "HEAD changed" to mean "our command moved it": after a REFUSED
    box commit during which the host committed, it stamped the host's commit as the box's own and
    recorded index-box as synced there — though index-box still held the old tree. Every host
    file then read as a staged deletion, and the agent's next commit silently reverted them
    (lefthook run, libkrun). Only a move this command's own transaction committed counts."""
    rig.box("status", "--porcelain")
    hooks = rig.gitdir / "hooks"
    hooks.mkdir(exist_ok=True)
    (hooks / "pre-commit").write_text(
        f"#!/bin/sh\n{_host_commit_script(rig, 'hostfile')}\nexit 1\n"
    )
    (hooks / "pre-commit").chmod(0o755)
    rig.write("fileC", "c-box\n")
    rig.box("add", "fileC")
    assert rig.box("commit", "-qm", "box").returncode != 0
    (hooks / "pre-commit").unlink()
    assert not _index_box_lacks(rig, "hostfile")
    assert rig.box("commit", "-qm", "box again").returncode == 0
    changes = rig.host("diff-tree", "--no-commit-id", "-r", "--name-status", "HEAD").stdout
    assert changes == "A\tfileC\n"  # the host's file survives the agent's next commit


def test_a_reset_the_host_committed_under_rewinds_nothing(rig, tmp_path):
    """`git reset` reads its target (HEAD) first, rewrites the index, and only then reads the value
    it expects to replace — so a host commit in between is the expected value, verified under the
    lock by git and by the hook alike, and the branch moves back to the target: every host commit
    in that gap rewound (Tangible's lefthook, libkrun under load: 7 commits, the index work took
    seconds). No stale read needed — the same race on a single kernel. Played with the repo's own
    `post-index-change` hook, which runs in exactly that gap (the real command's only: the shim's
    own git calls carry no hooks override)."""
    rig.box("status", "--porcelain")
    hooks = rig.gitdir / "hooks"
    hooks.mkdir(exist_ok=True)
    once = tmp_path / "raced"
    (hooks / "post-index-change").write_text(
        "#!/bin/sh\n"
        f'case "$GIT_CONFIG_PARAMETERS" in *[hH]ooks[pP]ath*) [ -e "{once}" ] || {{ : >"{once}"; '
        f"{_host_commit_script(rig, 'hostfile')}; }} ;; esac\n"
    )
    (hooks / "post-index-change").chmod(0o755)
    r = rig.box("reset", "-q")
    (hooks / "post-index-change").unlink()
    assert once.exists()  # the host did commit in the gap
    assert rig.host("log", "-1", "--format=%s").stdout.strip() == "host"  # …and it is still the tip
    assert r.returncode != 0 and "not moving refs/heads/main" in r.stderr


@pytest.mark.parametrize("detached", [False, True], ids=["on-branch", "detached"])
def test_a_command_that_moves_the_branch_several_times_is_held_to_its_own_last_move(rig, detached):
    """Each move of the checked-out branch is held to the HEAD the command started from — and a
    cherry-pick of two commits (or a `rebase --continue` from a detached HEAD) makes its second
    move from its FIRST: the command's own committed moves advance what the next one is held to."""
    rig.box("status", "--porcelain")
    base = rig.head()
    rig.host("checkout", "-q", "-b", "side")
    picks = [rig.host_commit(f"side{i}", **{f"side{i}": f"{i}\n"}) for i in (1, 2)]
    rig.host("checkout", "-q", "main" if not detached else base)
    r = rig.box("cherry-pick", *picks)
    assert r.returncode == 0, r.stderr
    assert rig.host("log", "-2", "--format=%s", "HEAD").stdout.split() == ["side2", "side1"]


def test_a_host_commit_right_after_the_box_commit_is_not_claimed(rig):
    # Our commit lands, then the host commits before the post-command step reads HEAD: HEAD is
    # the host's commit, not the one our transaction committed — claiming it would record
    # index-box (our tree) as synced at the host's HEAD: the host's file a phantom deletion.
    rig.box("status", "--porcelain")
    hooks = rig.gitdir / "hooks"
    hooks.mkdir(exist_ok=True)
    (hooks / "post-commit").write_text(f"#!/bin/sh\n{_host_commit_script(rig, 'hostfile')}\n")
    (hooks / "post-commit").chmod(0o755)
    rig.write("fileC", "c-box\n")
    rig.box("add", "fileC")
    assert rig.box("commit", "-qm", "box").returncode == 0
    (hooks / "post-commit").unlink()
    assert rig.host("log", "-1", "--format=%s").stdout.strip() == "host"
    assert not _index_box_lacks(rig, "hostfile")
    rig.write("fileD", "d-box\n")
    rig.box("add", "fileD")
    assert rig.box("commit", "-qm", "box again").returncode == 0
    changes = rig.host("diff-tree", "--no-commit-id", "-r", "--name-status", "HEAD").stdout
    assert changes == "A\tfileD\n"


def test_a_box_read_that_raced_a_host_commit_records_nothing(rig, tmp_path):
    rig.box("status", "--porcelain")
    spy = tmp_path / "racing-git"
    spy.mkdir()
    (spy / "git").write_text(
        "#!/bin/sh\n"
        f'case " $* " in *" log "*) {_host_commit_script(rig, "hostfile")} ;; esac\n'
        f'exec "{rig.real}" "$@"\n'
    )
    (spy / "git").chmod(0o755)
    r = rig.box(
        "log", "-1", "--oneline", env={"PATH": f"{rig.shim.parent}:{spy}:{rig.env['PATH']}"}
    )
    assert r.returncode == 0, r.stderr
    assert not _index_box_lacks(rig, "hostfile")


@pytest.mark.parametrize("form", ["staged", "all"])  # `commit -a`: git's own temporary index
def test_a_repo_hooks_own_git_reads_through_the_refresh(rig, tmp_path, form):
    """git puts its exec-path first on a hook's PATH, so a repo hook's `git` (lefthook's) skipped
    the shim — and its refresh. Mid-commit, with the host moving the branch, lefthook's
    `git diff --cached --name-only` saw an UNBORN branch and listed every file in the repo:
    formatters ran on all of them, and `stage_fixed` would have re-staged everything. The hook's
    `git` is the shim again, which refreshes before handing a git-set GIT_INDEX_FILE through."""
    env, _, _ = _stale_during(rig, tmp_path, "commit", packed=False)
    log = tmp_path / "hook.log"
    hooks = rig.gitdir / "hooks"
    hooks.mkdir(exist_ok=True)
    (hooks / "pre-commit").write_text(f'#!/bin/sh\ngit diff --cached --name-only >"{log}"\n')
    (hooks / "pre-commit").chmod(0o755)
    if form == "staged":
        rig.write("fileC", "c-box\n")
        rig.box("add", "fileC")
    else:
        rig.write("fileB", "b-box\n")
    args = ["-a"] if form == "all" else []
    rig.box("commit", "-q", *args, "-m", "box", env=env)  # refused or not: what did its git see?
    assert log.read_text() == ("fileC\n" if form == "staged" else "fileB\n")


@pytest.mark.parametrize("sub", [["commit", "-q", "--allow-empty", "-m", "box"], ["reset", "-q"]])
def test_a_box_head_move_waits_out_an_operation_the_host_started(rig, sub):
    """During a host `pull --rebase`, HEAD is detached: a box commit landed on it and was dropped
    when the rebase finished (the workflow tests, both substrates) — and a box reset mid-rebase
    would wreck the host's. Not the VM's doing (one kernel has the same race), but the box can
    see the operation's state and wait: refused, with the reason, until it's done."""
    rig.box("status", "--porcelain")
    (rig.gitdir / "rebase-merge").mkdir()  # the host's rebase, in progress
    r = rig.box(*sub)
    assert r.returncode != 0 and "in the middle of" in r.stderr
    (rig.gitdir / "rebase-merge").rmdir()
    assert rig.box(*sub).returncode == 0


def test_an_operation_the_host_just_finished_does_not_hold_the_box_back(rig, tmp_path):
    # The host's rebase is over, but over the mount its state can read as present for a while:
    # re-asked (the fake `ln` plays the fresh lookup that finds it gone) before it counts.
    rig.box("status", "--porcelain")
    op = rig.gitdir / "rebase-merge"
    op.mkdir()
    fake = tmp_path / "fake-ln"
    fake.mkdir()
    (fake / "ln").write_text(
        f'#!/bin/sh\nfor n; do :; done\n[ "$n" = "{op}" ] && rmdir "{op}"\nexit 1\n'
    )
    (fake / "ln").chmod(0o755)
    r = rig.box(
        "commit", "-q", "--allow-empty", "-m", "box", env={"PATH": f"{fake}:{rig.env['PATH']}"}
    )
    assert r.returncode == 0, r.stderr


def test_an_operation_the_box_started_can_be_finished_in_the_box(rig):
    rig.box("status", "--porcelain")
    rig.host("switch", "-qc", "side")
    rig.host_commit("side", fileA="a-side\n")
    rig.host("switch", "-q", "main")
    rig.host_commit("main", fileA="a-main\n")
    rig.box("status", "--porcelain")
    assert rig.box("merge", "-q", "side").returncode != 0  # a conflict: MERGE_HEAD, the box's own
    rig.write("fileA", "a-both\n")
    assert rig.box("add", "fileA").returncode == 0
    r = rig.box("commit", "-q", "--no-edit")
    assert r.returncode == 0, r.stderr
    assert not (rig.gitdir / "MERGE_HEAD").exists()
    assert not (rig.gitdir / "fy-box-op").exists()  # finished: the marker goes with it


def test_a_repo_hooks_git_in_the_same_repo_only_refreshes(rig, tmp_path):
    """Since a repo hook's git goes through the shim, lefthook's ~25 git calls each ran the shim's
    whole path — discovery, heal, arming, post-command: 79 real git runs per commit instead of 23,
    +2 s a lefthook commit. Inside our own command's hooks, in the same repo, the outer command has
    healed and armed already (the ref check still reaches nested moves through the override git
    hands down): only the refresh is left to do. Counted as HEAD refreshes per in-hook call."""
    log = tmp_path / "ln.log"
    fake = tmp_path / "fake-ln"
    fake.mkdir()
    (fake / "ln").write_text(f'#!/bin/sh\nfor n; do :; done\necho "$n" >>"{log}"\nexit 1\n')
    (fake / "ln").chmod(0o755)
    hooks = rig.gitdir / "hooks"
    hooks.mkdir(exist_ok=True)
    (hooks / "pre-commit").write_text(
        f'#!/bin/sh\necho begin >>"{log}"\ngit status --porcelain >/dev/null\necho end >>"{log}"\n'
    )
    (hooks / "pre-commit").chmod(0o755)
    rig.write("fileC", "c1\n")
    rig.box("add", "fileC")
    r = rig.box("commit", "-qm", "box", env={"PATH": f"{fake}:{rig.env['PATH']}"})
    assert r.returncode == 0, r.stderr
    lines = log.read_text().splitlines()
    inside = lines[lines.index("begin") + 1 : lines.index("end")]
    assert sum(1 for n in inside if n.endswith("/HEAD")) == 1, inside


@pytest.mark.parametrize("how", ["cd", "-C"])
def test_a_repo_hooks_git_in_a_nested_repo_still_gets_its_own_index(rig, tmp_path, how):
    # The light path is for the SAME repo only: a git the hook runs in a repo nested inside the
    # checkout (a submodule, a scratch clone) must not be handed the outer command's index-box.
    nested = rig.repo / "nested"
    rig._run(rig.real, "init", "-q", str(nested))
    (nested / "n.txt").write_text("n\n")
    hooks = rig.gitdir / "hooks"
    hooks.mkdir(exist_ok=True)
    run = f'cd "{nested}" && git' if how == "cd" else f'git -C "{nested}"'
    (hooks / "pre-commit").write_text(
        f'#!/bin/sh\n{run} add n.txt && {run} ls-files >"{tmp_path}/nested.ls"\n'
    )
    (hooks / "pre-commit").chmod(0o755)
    rig.write("fileC", "c1\n")
    rig.box("add", "fileC")
    assert rig.box("commit", "-qm", "box").returncode == 0
    assert (tmp_path / "nested.ls").read_text() == "n.txt\n"


def _repo_hooks(where: Path, log: Path) -> None:
    where.mkdir(parents=True, exist_ok=True)
    for name in ("pre-commit", "reference-transaction"):
        stdin = "" if name == "pre-commit" else "; cat"
        (where / name).write_text(
            f'#!/bin/sh\n{{ echo "{name} $* in $(pwd -P)"{stdin}; }} >>"{log}"\n'
        )
        (where / name).chmod(0o755)


@pytest.mark.parametrize("hooks_path", [None, ".husky"], ids=["default", "core.hooksPath"])
def test_the_repos_own_hooks_still_run_with_their_args_and_input(rig, tmp_path, hooks_path):
    log = tmp_path / "hooks.log"
    if hooks_path:
        rig.host("config", "core.hooksPath", hooks_path)
    _repo_hooks(rig.repo / hooks_path if hooks_path else rig.gitdir / "hooks", log)
    r = rig.box("commit", "-q", "--allow-empty", "-m", "box")
    assert r.returncode == 0, r.stderr
    ran = log.read_text()
    assert "pre-commit  in" in ran
    assert "reference-transaction prepared" in ran and "reference-transaction committed" in ran
    assert f" {rig.head()} refs/heads/main" in ran  # the transaction's lines, on stdin


def test_a_hook_driving_git_in_another_repo_runs_that_repos_hooks(rig, tmp_path):
    """Our `core.hooksPath` reaches every git a hook starts (git passes `-c` down), so a hook
    committing in ANOTHER repo would otherwise get THIS repo's hooks."""
    other = tmp_path / "other"
    rig._run(rig.real, "init", "-q", str(other))
    log = tmp_path / "hooks.log"
    _repo_hooks(other / ".git" / "hooks", log)
    (rig.gitdir / "hooks").mkdir(exist_ok=True)
    (rig.gitdir / "hooks" / "pre-commit").write_text(
        f'#!/bin/sh\nenv -u GIT_INDEX_FILE git -C "{other}" commit -q --allow-empty -m nested\n'
    )
    (rig.gitdir / "hooks" / "pre-commit").chmod(0o755)
    r = rig.box("commit", "-q", "--allow-empty", "-m", "box")
    assert r.returncode == 0, r.stderr
    assert f"pre-commit  in {other.resolve()}" in log.read_text()


@pytest.mark.spawns("bash")
def test_the_hook_dispatch_never_hands_a_hook_to_itself(rig):
    """Structural guard: if "the repo's own hooks" ever resolves to OUR directory (it did, through
    the override git hands down to a hook's git — a nested repo re-armed from inside a hook), the
    wrapper would exec itself forever, growing PATH each time. That is "no repo hook": exit 0."""
    r = subprocess.run(
        ["bash", str(rig.hooks / "pre-commit")],  # $0 stays the hook's path: its dispatch
        cwd=rig.repo,
        env={**rig.env, "FY_ORIG_TOP": str(rig.repo.resolve()), "FY_ORIG_HOOKS": str(rig.hooks)},
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert r.returncode == 0, r.stderr


def test_a_callers_own_hooks_path_is_delegated_to_and_the_check_still_runs(rig, tmp_path):
    env, _, moved = _stale_during(rig, tmp_path, "commit", packed=False)
    log = tmp_path / "hooks.log"
    _repo_hooks(tmp_path / "mine", log)
    r = rig.box(
        "-c",
        f"core.hooksPath={tmp_path / 'mine'}",
        "commit",
        "-q",
        "--allow-empty",
        "-m",
        "box",
        env=env,
    )
    assert r.returncode != 0 and "not moving refs/heads/main" in r.stderr
    assert "pre-commit  in" in log.read_text()  # theirs ran — ours ran after it, and refused
    assert _settled(rig, tmp_path) == moved


def test_a_move_with_no_expected_value_is_not_second_guessed(rig):
    rig.host_commit("two", fileA="a2\n")
    rig.host("branch", "other", "HEAD~")
    for args in (("branch", "-f", "other", "HEAD"), ("tag", "t1"), ("branch", "-D", "other")):
        r = rig.box(*args)
        assert r.returncode == 0, (args, r.stderr)


def test_a_linked_worktree_refreshes_its_own_head_and_the_shared_refs(rig, tmp_path):
    wt = tmp_path / "wt"
    rig.host("worktree", "add", "-q", "-b", "wt", str(wt))
    env, log = _fake_ln(rig, tmp_path)
    assert rig.box("-C", str(wt), "status", "--porcelain", env=env).returncode == 0
    common = rig.gitdir.resolve()
    refreshed = {Path(n).parent.resolve() / Path(n).name for n in log.read_text().split()}
    assert {
        common / "worktrees" / "wt" / "HEAD",  # the worktree's own HEAD…
        common / "packed-refs",  # …and the refs it shares with the main checkout
        common / "config",
        common / "refs" / "heads" / "wt",
    } <= refreshed
    assert common / "HEAD" not in refreshed  # not the main checkout's


def test_the_refresh_never_creates_a_name_that_is_absent(rig):
    rig.host("pack-refs", "--all")  # the branch is packed-only: its loose name is absent
    head = (rig.gitdir / "HEAD").read_text()
    assert rig.box("status", "--porcelain").returncode == 0
    assert not (rig.gitdir / "refs" / "heads" / "main").exists()
    assert (rig.gitdir / "HEAD").read_text() == head
    rig.host("pack-refs", "--all")
    assert not any(p.name.endswith(".lock") for p in rig.gitdir.rglob("*"))


def _status(rig, *args: str, cwd: Path | None = None) -> str:
    """Box-side porcelain status (the shim), in ``cwd`` (default: the rig's repo)."""
    r = subprocess.run(
        [str(rig.shim), "status", "--porcelain", *args],
        cwd=cwd or rig.repo,
        env=rig.env,
        capture_output=True,
        text=True,
    )
    assert r.returncode == 0, r.stderr
    return r.stdout


def test_worktree_add_writes_the_NEW_checkout_not_this_ones_index(rig, tmp_path):
    """`git worktree add` checks the new tree out in a child git that INHERITS GIT_INDEX_FILE —
    like clone/init — so under the shim the new worktree's index went into THIS checkout's
    index-box. Both sides broke at once, silently: here, `MM` on every file the two commits
    differ in (a commit would revert them; a later `git switch` took them for local edits and
    carried them over without a word), and in the new worktree no index at all (staged `D` +
    `??` for every file, box-side and host-side)."""
    rig.host("switch", "-qc", "feat")
    rig.host_commit("feat", fileA="a2\n")
    assert _status(rig) == ""  # box-side, clean on `feat`
    wt = tmp_path / "wt"
    r = rig.box("worktree", "add", "-q", "--detach", str(wt), "main")
    assert r.returncode == 0, r.stderr
    assert _status(rig) == ""  # this checkout's index-box untouched
    assert _status(rig, cwd=wt) == ""  # the new worktree has a real index, box-side…
    host_wt = subprocess.run(
        [str(rig.real), "status", "--porcelain"],
        cwd=wt,
        env=rig.env,
        capture_output=True,
        text=True,
    )
    assert host_wt.returncode == 0 and host_wt.stdout == ""  # …and host-side
    # `worktree remove` runs its cleanliness check in a child git too: judged against this
    # checkout's index it refuses a clean tree (or passes a dirty one). Clean → removed.
    r = rig.box("worktree", "remove", str(wt))
    assert r.returncode == 0, r.stderr
    assert not wt.exists()
    assert _status(rig) == ""


# ── the first box call's seed: a host REPLACE reads as missing from the VM for up to ~1 s ─────
# Host git writes `.git/index` by lock → rename, and over virtiofs the VM then sees the name
# MISSING for 22 ms … ~1 s (measured: ADR-0021's visibility record). The seed used to copy only
# if the file was there that instant, so a box's first git call in that window ran on NO index:
# every tracked file a staged deletion — and an index-writing command made that permanent.


def _seeded_rig(tmp_path):
    rig = Rig(tmp_path)
    rig.host_commit("init", fileA="a1\n", fileB="b1\n")
    rig.write("fileE", "e1\n")
    rig.host("add", "--", "fileE")  # host staging the seed should carry into the box
    return rig


def test_the_seed_waits_out_a_shared_index_that_is_briefly_missing(tmp_path):
    import threading

    rig = _seeded_rig(tmp_path)
    shared, aside = rig.gitdir / "index", rig.gitdir / "index.aside"
    shared.rename(aside)  # mid-replace, as the VM sees it
    # 3 s: past Lima vz's ~1 s window, inside podman machine's libkrun ~5 s one (ADR-0021).
    threading.Timer(3.0, lambda: aside.rename(shared)).start()
    t0 = time.monotonic()
    r = rig.box("status", "--porcelain")
    assert r.returncode == 0, r.stderr
    assert r.stdout == "A  fileE\n"  # the shared index, staging and all — not an empty one
    assert time.monotonic() - t0 < 6.5


def test_with_no_shared_index_at_all_the_seed_is_heads_tree_never_empty(tmp_path):
    rig = _seeded_rig(tmp_path)
    (rig.gitdir / "index").unlink()
    r = rig.box("status", "--porcelain")
    assert r.returncode == 0, r.stderr
    assert "D " not in r.stdout and "fileA" not in r.stdout  # HEAD's tree: nothing phantom
    assert (rig.gitdir / "index-box").is_file()


def test_an_unborn_repo_seeds_nothing_and_does_not_wait(tmp_path):
    rig = Rig(tmp_path)  # no commit: no shared index, and empty IS correct
    t0 = time.monotonic()
    assert rig.box("status", "--porcelain").returncode == 0
    assert time.monotonic() - t0 < 1.5


def test_the_seed_waits_while_heads_ref_is_briefly_missing_too(tmp_path):
    # A host commit replaces the branch ref alongside the index, so both read as missing in the
    # same window: an unresolvable HEAD with a reflog behind it is that window, not an unborn repo.
    import threading

    rig = _seeded_rig(tmp_path)
    rig.write("fileF", "f1\n")
    moved = [(rig.gitdir / "index", rig.gitdir / "index.aside")]
    moved.append((rig.gitdir / "refs/heads/main", rig.gitdir / "main.aside"))
    for live, aside in moved:
        live.rename(aside)
    restore = threading.Timer(1.5, lambda: [aside.rename(live) for live, aside in moved])
    restore.start()
    r = rig.box("add", "--", "fileF")  # index-writing: an empty seed here would stick
    assert r.returncode == 0, r.stderr
    restore.join()
    assert _status(rig) == "A  fileE\nA  fileF\n"


@pytest.mark.parametrize("shared_returns", [True, False], ids=["copied", "heads-tree"])
def test_a_racing_first_call_never_replaces_an_index_box_seeded_meanwhile(tmp_path, shared_returns):
    # Two first calls both find no index-box; while one waits for the shared index, the other
    # seeds it and stages. The waiter must not publish its copy (or HEAD's tree) over that.
    rig = _seeded_rig(tmp_path)
    rig.write("fileF", "f1\n")
    shared, aside = rig.gitdir / "index", rig.gitdir / "index.aside"
    shared.rename(aside)
    # The waiter's first `sleep` says it is inside the retry loop, past its own absence check.
    probe, waiting = tmp_path / "probe", tmp_path / "waiting"
    probe.mkdir()
    (probe / "sleep").write_text(
        f'#!/bin/sh\n: >"{waiting}"\nfor d in /usr/bin /bin; do [ -x $d/sleep ] && exec $d/sleep "$@"; done\n'
    )
    (probe / "sleep").chmod(0o755)
    waiter = subprocess.Popen(
        [str(rig.shim), "status", "--porcelain"],
        cwd=rig.repo,
        env={**rig.env, "PATH": f"{probe}:{rig.env['PATH']}"},
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    deadline = time.monotonic() + 10
    while not waiting.exists():
        assert time.monotonic() < deadline and waiter.poll() is None, "the waiter never waited"
        time.sleep(0.02)
    other = {"GIT_INDEX_FILE": str(rig.gitdir / "index-box")}
    for args in (("read-tree", "HEAD"), ("add", "--", "fileF")):
        assert rig._run(rig.real, *args, env_extra=other).returncode == 0
    if shared_returns:
        aside.rename(shared)
    _, err = waiter.communicate(timeout=15)
    assert waiter.returncode == 0, err
    assert _status(rig) == "A  fileF\n?? fileE\n"  # the other call's staging, not the shared one
