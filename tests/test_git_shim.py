"""The box git shim (assets/box/git-index-shim.sh), driven against REAL git in scratch repos.

Exercises the ADR-0021 behaviours end-to-end: the index split itself, and the staleness
self-heal — "host" commands run the real git binary (shared index), "box" commands run the
shim, exactly the two-writer topology of the shared checkout. Runs anywhere git exists
(including inside the dev box: the fixture resolves the REAL binary past any installed shim).
"""

from __future__ import annotations

import itertools
import os
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
    # A box commit lands in shared refs, writes index-box (not the shared index), and stamps
    # both sync files — the raw split + the attribution the heals depend on.
    shared_before = (rig.gitdir / "index").read_bytes()
    rig.write("fileC", "c1\n")
    assert rig.box("add", "fileC").returncode == 0
    assert rig.box("commit", "-qm", "box").returncode == 0
    head = rig.head()
    assert (rig.gitdir / "index-box").exists()
    assert (rig.gitdir / "index").read_bytes() == shared_before  # shared index untouched
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
    # The one unhealable state: host moved HEAD AND the box has genuinely staged work. Status
    # still runs (warn on stderr), but commit is refused — it would revert the host's commit.
    rig.box("status", "--porcelain")  # sync point at C1
    rig.write("fileB", "b-box\n")
    rig.box("add", "fileB")  # genuine box-side staged work
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
    rig.write("fileB", "junk1\n")
    rig.box("add", "fileB")
    rig.host_commit("host", fileA="a2\n")
    assert "index-box is stale" in rig.box("status", "--porcelain").stderr
    rig.write("fileB", "junk2\n")
    rig.box("add", "fileB")  # settles the index at its post-add size, still unfixable → memoized
    assert memo.exists()
    size_before = (rig.gitdir / "index-box").stat().st_size
    # Back to the COMMITTED content: the index is now byte-exactly the initial commit's tree, so
    # it is pure staleness with nothing to lose — at a size identical to the memoized one.
    rig.write("fileB", "b1\n")
    rig.box("add", "fileB")
    assert (rig.gitdir / "index-box").stat().st_size == size_before, (
        "the reproduction needs a same-size transition — a size change would mask the bug"
    )
    out = rig.box("status", "--porcelain")
    assert "index-box is stale" not in out.stderr, "a healable index must not stay behind the memo"
    assert out.stdout == ""  # no phantom staged diff
    assert not memo.exists()  # …and the memo is cleared, not left as a lie on disk
    assert "refusing 'git commit'" not in rig.box("commit", "-qm", "unblocked").stderr


def test_staging_that_matches_an_UNRELATED_BRANCHS_COMMIT_is_not_reset_away(rig):
    """The heal's "the index tree is a commit, so there is nothing to lose" rung used to scan ALL
    REFS, which makes it a claim about the tree's existence rather than its provenance. Staging
    content that reproduces some other commit's tree is ordinary work (re-applying a patch,
    `git checkout <branch> -- .`, backing a WIP change out) — and against an all-refs scan any of
    those looked disposable, so the next host-side HEAD move silently `git reset`-ed the staging
    away. The tree must come from a state HEAD has actually BEEN at (its reflog); an unrelated
    branch's commit is not that, and the stale-index refusal (recoverable) must stand instead.

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
    assert "index-box is stale" in out.stderr, (
        "an unvisited commit's tree is not proof of staleness"
    )
    assert "M  fileB" in out.stdout, "the box's deliberate staging must survive the heal"
    assert rig.box("commit", "-qm", "x").returncode == 1  # …and the guard still blocks the revert
    # The documented recovery still works, and re-staging on the new HEAD commits the real change.
    assert rig.box("reset", "-q").returncode == 0
    rig.box("add", "fileB")
    assert rig.box("commit", "-qm", "retry").returncode == 0
    assert "fileB" in rig.host("show", "--stat", "--oneline", "HEAD").stdout


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
    rig.write("fileB", "b-box\n")
    rig.box("add", "fileB")
    rig.host_commit("host", fileA="a2\n")
    assert rig.box("commit", "-qm", "x").returncode == 1
    assert rig.box("reset", "-q").returncode == 0
    rig.box("add", "fileB")  # re-stage (the file itself was never touched)
    ok = rig.box("commit", "-qm", "retry")
    assert ok.returncode == 0, ok.stderr
    assert rig.box("status", "--porcelain").stdout == ""


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
    rig.write("fileB", "b-box\n")
    rig.box("add", "fileB")
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
    rig.write("fileB", "b-box\n")
    rig.box("add", "fileB")
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
