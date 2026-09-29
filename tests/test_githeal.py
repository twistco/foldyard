"""githeal — the box heals the shared index itself under index.lock; when it can't (a host git
holds the lock) it offers the build, and the host only renames it into place.

Box-side commands run through the ACTUAL shim (test_git_shim.Rig), so the proposals the host
consumes are produced by the real mechanism — with a host git holding index.lock meanwhile
(`_host_git_busy`), which is what leaves the box an offer to make rather than an install. Every host-side heal here runs with process creation
FORBIDDEN (`heal`): the host runs no git — nor anything else — in the box-writable checkout.
"""

from __future__ import annotations

import contextlib
import os
import subprocess
from pathlib import Path

import pytest

from foldyard import githeal
from test_git_shim import Rig


@pytest.fixture
def rig(tmp_path):
    r = Rig(tmp_path)
    r.host_commit("init", fileA="a1\n", fileB="b1\n", fileD="d1\n")
    return r


def heal(repo, main=None):
    """One host-side heal pass with every way to start a process refused — a spawn fails the
    test by name, whatever the heal does with the exception."""

    def refuse(*args, **_kw):
        raise AssertionError(f"the host-side heal started a process: {args[1:2] or args[:1]}")

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(subprocess.Popen, "__init__", refuse)
        for name in ("system", "posix_spawn", "posix_spawnp", "fork", "execv", "execve"):
            if hasattr(os, name):
                mp.setattr(os, name, refuse)
        return githeal.heal_checkout(repo, main)


@contextlib.contextmanager
def _host_git_busy(rig):
    """A host git mid-write: it holds index.lock, so the box can't install and offers instead."""
    lock = Path(rig.host("rev-parse", "--absolute-git-dir").stdout.strip()) / "index.lock"
    lock.write_text("")
    try:
        yield
    finally:
        lock.unlink()


def _box_commit(rig, msg, installs=False, env=None, **files):
    """A box commit; unless ``installs``, while a host git holds index.lock (the host's path)."""
    for name, content in files.items():
        rig.write(name, content)
    assert rig.box("add", "--", *files).returncode == 0
    with contextlib.nullcontext() if installs else _host_git_busy(rig):
        r = rig.box("commit", "-qm", msg, env=env)
    assert r.returncode == 0, r.stderr
    return rig.head()


def _host_status(rig):
    return rig.host("status", "--porcelain").stdout


def _pending(rig) -> list[str]:
    return sorted(p.name for p in rig.gitdir.glob("index.fy-*") if p.name != githeal.SYNC_FILE)


def _synced_at(rig, head: str) -> None:
    """Bring the shared index's sync marker to ``head`` the way it happens for real: a box git
    call notices the host's own HEAD move and says so, and the host records it."""
    rig.box("status", "--porcelain")
    heal(rig.repo)
    assert (rig.gitdir / githeal.SYNC_FILE).read_text().strip() == head


def test_heals_shared_index_after_box_commit(rig):
    # A box commit moves shared HEAD, the host's index still describes the old one → phantom
    # staged-D/?? and MM pairs in every host-side git GUI. The box proposes; one pass installs.
    _box_commit(rig, "box", fileA="a2\n", fileC="c1\n")
    assert "fileA" in _host_status(rig)  # the illusion is really there before the heal
    assert [p for p in _pending(rig) if p.startswith("index.fy-proposed.")]
    msg = heal(rig.repo)
    assert msg and "fast-forwarded" in msg
    assert _host_status(rig) == ""
    assert (rig.gitdir / githeal.SYNC_FILE).read_text().strip() == rig.head()
    assert _pending(rig) == []  # consumed
    assert heal(rig.repo) is None  # steady state is a quiet no-op


def _host_commit_changes(rig, **files) -> str:
    """A host commit right after the box's, of new files only: what it records."""
    for name, content in files.items():
        rig.write(name, content)
    rig.host("add", "--", *files)
    rig.host("commit", "-qm", "host")
    return rig.host("diff-tree", "--no-commit-id", "-r", "--name-status", "HEAD").stdout


def test_a_box_commit_heals_the_shared_index_itself(rig):
    # Waiting for the host's tick was the silent revert: a host commit in between recorded the
    # box's files as deleted. The box installs its heal at once, under index.lock.
    _synced_at(rig, rig.head())
    _box_commit(rig, "box", installs=True, fileA="a2\n", fileC="c1\n")
    assert _host_status(rig) == ""
    assert _pending(rig) == []
    assert (rig.gitdir / githeal.SYNC_FILE).read_text().strip() == rig.head()
    assert _host_commit_changes(rig, fileH="h\n") == "A\tfileH\n"  # no D fileC, no M fileA


def test_the_box_carries_staged_host_work_itself(rig):
    _synced_at(rig, rig.head())
    rig.write("fileB", "b-host\n")
    rig.host("add", "--", "fileB")
    _box_commit(rig, "box", installs=True, fileA="a2\n")
    assert _host_status(rig) == "M  fileB\n"
    assert _pending(rig) == []


def _fallback_install(rig) -> None:
    """Send the next box commit's heal down the post-command path: a host git holds index.lock
    while the commit's transaction prepares (so it can't seal the heal to the ref move), and lets
    go before the shim's post-command step (a post-commit hook runs in between)."""
    lock, hooks = rig.gitdir / "index.lock", rig.gitdir / "hooks"
    lock.write_text("")
    hooks.mkdir(exist_ok=True)
    (hooks / "post-commit").write_text(f'#!/bin/sh\nrm -f "{lock}" "$0"\n')
    (hooks / "post-commit").chmod(0o755)


def _swap_in_under_the_lock(rig, tmp_path, variant: bytes, on: int = 1) -> dict:
    """An `ln` that, on the ``on``-th time the box refreshes .git/index while index.lock exists,
    puts ``variant`` there first — the host's rewrite that landed between the box's copy and its
    lock. (``on=2`` under `_fallback_install`: the first look is the transaction's, which finds
    the host's lock and skips; the second is the post-command install's, under its own.)"""
    fake, staged, count = tmp_path / "fake-ln", tmp_path / "variant", tmp_path / "looks"
    fake.mkdir()
    staged.write_bytes(variant)
    index = rig.gitdir / "index"
    (fake / "ln").write_text(
        "#!/bin/sh\nfor n; do :; done\n"
        f'if [ "$n" = "{index}" ] && [ -e "{index}.lock" ] && [ -e "{staged}" ]; then '
        f'echo x >>"{count}"; [ "$(wc -l <"{count}")" -ge {on} ] && '
        f'cp "{staged}" "{index}" && rm -f "{staged}"; fi\nexit 1\n'
    )
    (fake / "ln").chmod(0o755)
    return {"PATH": f"{fake}:{rig.env['PATH']}"}


@pytest.mark.parametrize("host_staged", [False, True], ids=["stale", "carried"])
def test_a_host_commit_right_after_the_box_commit_never_reverts_it(rig, host_staged):
    # The heal used to land AFTER the box's commit had moved the branch — a host commit in that
    # gap (libkrun, IDE poller: 3 in 46) recorded the box's files as deleted from the stale host
    # index. The box's own transaction installs it now: index.lock taken at `prepared`, the healed
    # index renamed in at `committed`, so a host commit (which needs that lock) can't land between.
    # A post-commit hook runs after the ref moved and before the shim's post-command step.
    _synced_at(rig, rig.head())
    if host_staged:  # an IDE-style `git add` on the host: the heal must carry it, not wait
        rig.write("fileB", "b-host\n")
        rig.host("add", "--", "fileB")
    hooks = rig.gitdir / "hooks"
    hooks.mkdir(exist_ok=True)
    real = f'env -u GIT_INDEX_FILE -u GIT_CONFIG_PARAMETERS -u GIT_CONFIG_COUNT "{rig.real}"'
    once = rig.gitdir / "host-committed"  # the host's own commit runs this hook too: only once
    (hooks / "post-commit").write_text(
        f'#!/bin/sh\n[ -e "{once}" ] && exit 0\n: >"{once}"\n'
        f'{real} -C "{rig.repo}" commit -q --allow-empty -m host\n'
    )
    (hooks / "post-commit").chmod(0o755)
    _box_commit(rig, "box", installs=True, fileC="c1\n")
    (hooks / "post-commit").unlink()
    assert rig.host("log", "-1", "--format=%s").stdout.strip() == "host"
    host_commit = rig.host("diff-tree", "--no-commit-id", "-r", "--name-status", "HEAD").stdout
    assert host_commit == ("M\tfileB\n" if host_staged else "")  # its own staging, and nothing else
    assert rig.host("show", "HEAD:fileC").stdout == "c1\n"  # never recorded as deleted


def _git_calls(rig, tmp_path, staged: int) -> int:
    """How many git processes a box commit starts while the host has ``staged`` paths staged —
    counted by git itself (GIT_TRACE: a hook's git is git's own binary, first on the hook's PATH,
    so no spy on PATH sees the sealed heal's calls)."""
    _synced_at(rig, rig.head())
    names = [f"host{staged}_{i}" for i in range(staged)]
    for n in names:
        rig.write(n, f"{n}\n")
    rig.host("add", "--", *names)
    trace = tmp_path / f"trace{staged}"
    _box_commit(rig, "box", installs=True, env={"GIT_TRACE": str(trace)}, fileC=f"c{staged}\n")
    assert _host_status(rig) == "".join(f"A  {n}\n" for n in sorted(names))  # all carried
    return trace.read_text().count("trace: built-in: git ")


def test_the_sealed_heal_does_not_grow_with_the_hosts_staging(rig, tmp_path):
    """The sealed heal runs under git's ref lock, so every host commit waits it out ("HEAD.lock:
    File exists"). Its carry asked git about each staged path on its own — 2-3 processes a path:
    19 staged paths held the lock ~28 s on a loaded VM. The carry asks twice, whatever the count."""
    few = _git_calls(rig, tmp_path, 1)
    rig.host("commit", "-qm", "host")
    assert _git_calls(rig, tmp_path, 12) == few


def test_a_carried_path_keeps_its_mode(rig):
    _synced_at(rig, rig.head())
    rig.write("tool", "#!/bin/sh\n")
    (rig.repo / "tool").chmod(0o755)
    rig.host("add", "--", "tool")
    _box_commit(rig, "box", installs=True, fileC="c\n")
    assert rig.host("ls-files", "-s", "tool").stdout.startswith("100755 ")


def test_a_sync_point_git_cant_read_refuses_the_carry_never_drops_the_staging(rig):
    # The sync marker names a commit this repo doesn't have (gc'd after a rewrite): git can't say
    # what is staged against it. Carrying "nothing" installed the new HEAD's bare tree over the
    # host's staged work.
    _synced_at(rig, rig.head())
    rig.write("fileB", "b-host\n")
    rig.host("add", "--", "fileB")
    (rig.gitdir / githeal.SYNC_FILE).write_text("1" * len(rig.head()) + "\n")
    _box_commit(rig, "box", installs=True, fileC="c\n")
    assert rig.host("show", ":fileB").stdout == "b-host\n"


def test_a_host_file_where_the_box_made_a_directory_is_not_carried_over_it(rig):
    # The host staged a new file `p`, the box committed `p/x`: they can't both be in the index.
    # Carried, `p` replaced the box's `p/x` in the healed index — the host's next commit deleted
    # it. The heal is refused instead (the index stays at its old sync point, and says so).
    _synced_at(rig, rig.head())
    before = rig.head()
    rig.write("p", "host\n")
    rig.host("add", "--", "p")
    (rig.repo / "p").rename(rig.repo / "p.host")
    (rig.repo / "p").mkdir()
    _box_commit(rig, "box", installs=True, **{"p/x": "box\n"})
    assert (rig.gitdir / githeal.SYNC_FILE).read_text().strip() == before
    assert rig.host("show", ":p").stdout == "host\n"


def test_a_host_conflict_is_never_carried_as_a_deletion(rig):
    # A conflicted `git stash pop` leaves unmerged entries and no operation state, so the box can
    # still commit — and the carry, finding no stage-0 entry, staged the file's DELETION.
    _synced_at(rig, rig.head())
    rig.write("fileA", "a-stash\n")
    rig.host("stash", "-q")
    rig.host_commit("host", fileA="a-host\n")
    _synced_at(rig, rig.head())
    assert rig._run(rig.real, "stash", "pop", "-q").returncode != 0  # the conflict
    _box_commit(rig, "box", installs=True, fileC="c\n")
    assert "fileA" in rig.host("ls-files", "-u").stdout


def test_host_staging_before_the_transactions_lock_is_carried_not_dropped(rig, tmp_path):
    # The transaction builds the healed index from a copy, then takes the lock: host staging that
    # landed in between must stop it (it would install N's bare tree over that staging) — the
    # post-command path then carries the staging forward.
    _synced_at(rig, rig.head())
    before = (rig.gitdir / "index").read_bytes()
    rig.write("fileB", "b-host\n")
    rig.host("add", "--", "fileB")
    staged = (rig.gitdir / "index").read_bytes()
    (rig.gitdir / "index").write_bytes(before)
    env = _swap_in_under_the_lock(rig, tmp_path, staged)  # the transaction's own look
    _box_commit(rig, "box", installs=True, env=env, fileA="a2\n")
    assert _host_status(rig) == "M  fileB\n"


def _host_commits_in_the_gap(rig) -> None:
    """A post-commit hook — after the box's ref moved, before the shim's post-command step — makes
    one host commit from the SHARED index: whatever that index says then is what it records."""
    hooks = rig.gitdir / "hooks"
    hooks.mkdir(exist_ok=True)
    once = rig.gitdir / "host-committed"
    real = f'env -u GIT_INDEX_FILE -u GIT_CONFIG_PARAMETERS -u GIT_CONFIG_COUNT "{rig.real}"'
    (hooks / "post-commit").write_text(
        f'#!/bin/sh\n[ -e "{once}" ] && exit 0\n: >"{once}"\n'
        f'{real} -C "{rig.repo}" commit -q --allow-empty -m host\n'
    )
    (hooks / "post-commit").chmod(0o755)


def test_a_first_box_commit_before_any_sync_point_is_still_sealed(rig):
    # A fresh worktree's index has never been synced, so it has no sync marker — and the sealed
    # heal needs one to tell staged work from staleness. The box's first git call only OFFERED a
    # record, which the host's tick turns into the marker up to ~2 s later: the agent's first
    # commit fell back to the post-command install, and a host commit in that gap recorded its
    # file as deleted (worktree workflow test, libkrun: `host 3` → `D b1.txt`; traced: exactly the
    # first box commit of every run went unsealed). The box's first call records it itself.
    assert not (rig.gitdir / githeal.SYNC_FILE).exists()
    rig.box("status", "--porcelain")  # the agent's first git call — no host tick runs here
    _host_commits_in_the_gap(rig)
    _box_commit(rig, "box", installs=True, fileC="c1\n")
    assert rig.host("log", "-1", "--format=%s").stdout.strip() == "host"
    assert rig.host("show", "HEAD:fileC").stdout == "c1\n"


def test_a_briefly_busy_lock_is_waited_for_not_fallen_back_from(rig, tmp_path):
    # A host git holds index.lock for moments; falling back to the post-command install left the
    # gap a host commit could still land in (the worktree test on libkrun). The transaction waits.
    _synced_at(rig, rig.head())
    lock, fake = rig.gitdir / "index.lock", tmp_path / "fake-ln"
    fake.mkdir()
    (fake / "ln").write_text(
        "#!/bin/sh\nfor n; do :; done\n"
        f'if [ "$n" = "{rig.gitdir}/index" ] && [ ! -e "{tmp_path}/held" ]; then '
        f': >"{tmp_path}/held"; : >"{lock}"; (sleep 0.3; rm -f "{lock}") & fi\nexit 1\n'
    )
    (fake / "ln").chmod(0o755)
    _host_commits_in_the_gap(rig)
    _box_commit(rig, "box", installs=True, env={"PATH": f"{fake}:{rig.env['PATH']}"}, fileC="c1\n")
    assert rig.host("log", "-1", "--format=%s").stdout.strip() == "host"
    assert rig.host("show", "HEAD:fileC").stdout == "c1\n"


def test_a_host_change_before_the_lock_is_rebuilt_under_it_not_fallen_back_from(rig, tmp_path):
    _synced_at(rig, rig.head())
    before = (rig.gitdir / "index").read_bytes()
    rig.write("fileB", "b-host\n")
    rig.host("add", "--", "fileB")
    staged = (rig.gitdir / "index").read_bytes()
    (rig.gitdir / "index").write_bytes(before)
    env = _swap_in_under_the_lock(rig, tmp_path, staged)  # lands between the copy and the lock
    _host_commits_in_the_gap(rig)
    _box_commit(rig, "box", installs=True, env=env, fileC="c1\n")
    host = rig.host("diff-tree", "--no-commit-id", "-r", "--name-status", "HEAD").stdout
    assert host == "M\tfileB\n"  # the host's staging, carried under the lock — fileC untouched
    assert rig.host("show", "HEAD:fileC").stdout == "c1\n"


def test_a_sync_marker_the_host_just_rewrote_still_seals_the_heal(rig, tmp_path):
    # The host's heal tick rewrites index.fy-head (a record the box offered after a host HEAD
    # move), and over the VM mount that name then reads as MISSING for up to ~5 s: the sealed heal
    # skipped ("no sync point") and fell back — the worktree test's reverts on libkrun, traced.
    from test_git_shim import _fake_ln

    _synced_at(rig, rig.head())
    env, _ = _fake_ln(rig, tmp_path, stale=rig.gitdir / githeal.SYNC_FILE)
    _host_commits_in_the_gap(rig)
    _box_commit(rig, "box", installs=True, env=env, fileC="c1\n")
    assert rig.host("log", "-1", "--format=%s").stdout.strip() == "host"
    assert rig.host("show", "HEAD:fileC").stdout == "c1\n"


def test_a_refused_box_commit_leaves_no_index_lock(rig, tmp_path):
    # The lock the transaction took at `prepared` is released at `aborted` — a leftover would make
    # every host git say "Another git process seems to be running".
    from test_git_shim import _settled, _stale_during

    env, _, moved = _stale_during(rig, tmp_path, "commit", packed=True)
    r = rig.box("commit", "-q", "--allow-empty", "-m", "box", env=env)
    assert r.returncode != 0
    assert not (rig.gitdir / "index.lock").exists()
    assert _settled(rig, tmp_path) == moved


def test_a_git_that_dies_mid_transaction_leaves_no_index_lock(rig):
    # The transaction holds index.lock from `prepared` to `committed`/`aborted`; a git killed in
    # between runs neither, and a leftover lock makes every host git refuse ("Another git process
    # seems to be running"). The shim's post-command step releases a lock its transaction held.
    _synced_at(rig, rig.head())
    hooks = rig.gitdir / "hooks"
    hooks.mkdir(exist_ok=True)
    (hooks / "reference-transaction").write_text(
        # this hook's parent is the shim's wrapper (it pipes the transaction in); git is ITS parent
        "#!/bin/sh\ncat >/dev/null\n"
        '[ "$1" = prepared ] && kill -9 "$(ps -o ppid= -p $PPID | tr -d " ")"\nexit 0\n'
    )
    (hooks / "reference-transaction").chmod(0o755)
    rig.write("fileC", "c1\n")
    assert rig.box("add", "fileC").returncode == 0
    rig.box("commit", "-qm", "box")  # git dies
    (hooks / "reference-transaction").unlink()
    assert not (rig.gitdir / "index.lock").exists()


def test_a_stat_only_rewrite_before_the_lock_still_installs(rig, tmp_path):
    # An IDE's status poller rewrites the index to refresh its stat cache — same entries, other
    # bytes. That must not send the heal to the host's tick (where it used to be dropped).
    _synced_at(rig, rig.head())
    before = (rig.gitdir / "index").read_bytes()
    rig.write("fileB", "b1\n")  # same content, a new mtime: status refreshes the stat cache
    rig.host("status", "--porcelain")
    polled = (rig.gitdir / "index").read_bytes()
    assert polled != before
    (rig.gitdir / "index").write_bytes(before)
    env = _swap_in_under_the_lock(rig, tmp_path, polled, on=2)
    _fallback_install(rig)
    _box_commit(rig, "box", installs=True, env=env, fileA="a2\n")
    assert _host_status(rig) == ""
    assert _pending(rig) == []


def test_host_staging_before_the_lock_is_never_overwritten(rig, tmp_path):
    # A real host `git add` between the box's copy and its lock: installing the build would drop
    # that staging, so the box declines and leaves its offer (which the host then drops too).
    _synced_at(rig, rig.head())
    before = (rig.gitdir / "index").read_bytes()
    rig.write("fileB", "b-host\n")
    rig.host("add", "--", "fileB")
    staged = (rig.gitdir / "index").read_bytes()
    (rig.gitdir / "index").write_bytes(before)
    env = _swap_in_under_the_lock(rig, tmp_path, staged, on=2)
    _fallback_install(rig)
    _box_commit(rig, "box", installs=True, env=env, fileA="a2\n")
    assert (rig.gitdir / "index").read_bytes() == staged  # the host's staging, untouched
    assert heal(rig.repo) is None  # the offer was built on the old copy: dropped
    rig.box("status", "--porcelain")  # the box's next call carries it
    assert _host_status(rig) == "M  fileB\n"


def test_a_head_moved_during_the_box_install_puts_the_host_index_back(rig, tmp_path):
    # The lock guards the index, not refs: the host can move HEAD while the box installs. HEAD is
    # read again after the rename, and the host's index put back — never left healed for a HEAD
    # that is gone (it would describe the wrong tree: the next host commit reverts).
    _synced_at(rig, rig.head())
    fake, index = tmp_path / "fake-ln", rig.gitdir / "index"
    fake.mkdir()
    snapshot = tmp_path / "before-install"
    (fake / "ln").write_text(
        "#!/bin/sh\nfor n; do :; done\n"
        f'if [ -e "{index}.lock" ] && [ "$n" = "{index}" ]; then cp "{index}" "{snapshot}"; fi\n'
        f'if [ -e "{index}.lock" ] && [ "$n" = "{rig.gitdir}/HEAD" ] && [ -e "{snapshot}" ] && '
        f'! cmp -s "{index}" "{snapshot}"; then '
        f'c=$("{rig.real}" -C "{rig.repo}" commit-tree -m host -p HEAD "HEAD^{{tree}}") && '
        f'"{rig.real}" -C "{rig.repo}" update-ref refs/heads/main "$c" && rm -f "{snapshot}"; fi\n'
        "exit 1\n"
    )
    (fake / "ln").chmod(0o755)
    rig.write("fileA", "a2\n")
    assert rig.box("add", "--", "fileA").returncode == 0
    _fallback_install(rig)
    host_index_before = index.read_bytes()
    r = rig.box("commit", "-qm", "box", env={"PATH": f"{fake}:{rig.env['PATH']}"})
    assert r.returncode == 0, r.stderr
    assert rig.host("log", "-1", "--format=%s").stdout.strip() == "host"  # HEAD did move
    assert index.read_bytes() == host_index_before  # the host's index, put back


def test_host_moved_head_only_resyncs_the_marker(rig):
    # The host's own git already manages its index — an unattributed HEAD move must never touch
    # it. In particular `git reset --soft` (index ≠ HEAD BY DESIGN) survives untouched.
    _synced_at(rig, rig.head())
    rig.host_commit("host C2", fileA="a2\n")
    _synced_at(rig, rig.head())
    rig.host("reset", "-q", "--soft", "HEAD~")
    _synced_at(rig, rig.head())
    assert "M  fileA" in _host_status(rig)  # the soft reset's staged state is intact


def test_staged_host_work_is_carried_forward(rig):
    # Host has a staged modification, a staged deletion, and a staged add; the box commits a
    # DISJOINT change. The proposal replays the host's staged entries on the new HEAD's tree.
    _synced_at(rig, rig.head())  # also seeds index-box at C1 BEFORE the host stages anything
    rig.write("fileB", "b-host\n")
    rig.write("fileE", "e1\n")
    rig.host("add", "--", "fileB", "fileE")
    rig.host("rm", "-q", "--cached", "fileD")
    _box_commit(rig, "box", fileA="a2\n")
    msg = heal(rig.repo)
    assert msg and "carried forward" in msg
    status = _host_status(rig)
    assert "M  fileB" in status and "A  fileE" in status and "D  fileD" in status
    assert "fileA" not in status  # the box's commit is absorbed, not phantom-staged


@pytest.mark.parametrize("var", [None, "GIT_GLOB_PATHSPECS", "GIT_ICASE_PATHSPECS"])
def test_a_carried_path_is_matched_literally_never_as_a_glob(rig, var):
    # The carry re-stages the host's staged paths by name. Read as a pathspec, `file*` also
    # matches `fileA` — whose old entry would then land on the new HEAD's tree: a proposal that
    # silently stages a revert of the box's commit. The shim inherits the box user's environment,
    # so a global pathspec setting there must neither re-widen the match nor clash with the
    # literal matching (git refuses `literal` beside any other global setting: a false refusal).
    _synced_at(rig, rig.head())
    rig.write("file*", "star\n")
    rig.host("add", "--", ":(literal)file*")
    rig.write("fileA", "a2\n")
    assert rig.box("add", "--", "fileA").returncode == 0
    with _host_git_busy(rig):
        r = rig.box("commit", "-qm", "box", env={var: "1"} if var else None)
    assert r.returncode == 0, r.stderr
    msg = heal(rig.repo)
    assert msg and "carried forward" in msg
    assert _host_status(rig) == "A  file*\n"


def test_overlapping_staged_work_refuses_once(rig):
    # Both sides touched fileA — never merge silently: say so once, leave the index alone, and
    # stay quiet until the state changes.
    _synced_at(rig, rig.head())
    rig.write("fileA", "a-host\n")
    rig.host("add", "--", "fileA")
    _box_commit(rig, "box", fileA="a-box\n")
    msg = heal(rig.repo)
    assert msg and "NOT healing" in msg
    assert heal(rig.repo) is None  # throttled repeat
    assert "fileA" in _host_status(rig)  # untouched, as promised
    # The documented manual fix clears it: the refusal goes stale with the index it judged.
    rig.host("reset", "-q")
    assert heal(rig.repo) is None
    assert _pending(rig) == []
    assert _host_status(rig) == ""


def test_a_proposal_built_on_an_index_the_host_since_changed_is_dropped(rig):
    # The host stages between the box's proposal and the tick: installing it would lose that
    # staging. The proposal names the index it was built from; a mismatch drops it, and the box
    # builds again (from the index as it now is) on its next git call — and installs it itself.
    _synced_at(rig, rig.head())
    _box_commit(rig, "box", fileA="a2\n")
    rig.write("fileB", "b-host\n")
    rig.host("add", "--", "fileB")
    assert heal(rig.repo) is None
    assert _pending(rig) == []  # dropped, not installed
    assert "M  fileB" in _host_status(rig)
    rig.box("status", "--porcelain")  # the box's next git call: the carried heal, installed
    assert _host_status(rig) == "M  fileB\n"
    assert heal(rig.repo) is None  # nothing left for the host


def test_a_proposal_for_a_head_the_host_moved_away_from_is_dropped(rig):
    # A soft reset moves HEAD without changing the index — the base still matches, but installing
    # the box's tree now would stage the undone commit. HEAD must still be the proposal's.
    _box_commit(rig, "box", fileA="a2\n")
    rig.host("reset", "-q", "--soft", "HEAD~")
    before = (rig.gitdir / "index").read_bytes()
    assert heal(rig.repo) is None
    assert (rig.gitdir / "index").read_bytes() == before
    assert not [p for p in _pending(rig) if p.startswith("index.fy-proposed.")]


@pytest.mark.parametrize("when", ["before the lock", "during the write"])
def test_a_head_moved_while_installing_is_never_left_installed(rig, monkeypatch, when):
    # index.lock guards the index, not refs: a soft reset can land between the pass reading HEAD
    # and the proposal being written. Checked again under the lock, and once more after the write
    # (then the index the host had is put back) — never a recorded heal to a HEAD that has gone.
    before = rig.head()
    ref = rig.repo / ".git" / rig.host("symbolic-ref", "HEAD").stdout.strip()
    new = _box_commit(rig, "box", fileA="a2\n")
    shared = (rig.gitdir / "index").read_bytes()
    moved, written = [], []

    def soft_reset():  # as `git reset --soft HEAD~`: the ref moves, the index is untouched
        if not moved:
            moved.append(True)
            ref.write_text(before + "\n")

    def write(m, rel, data, _real=githeal.mountwrite.write):
        _real(m, rel, data)
        if rel.endswith("/index"):
            written.append(data)
            if when == "during the write":
                soft_reset()

    monkeypatch.setattr(githeal.mountwrite, "write", write)
    if when == "before the lock":
        resolve = githeal._resolve_head
        monkeypatch.setattr(githeal, "_resolve_head", lambda *a: (resolve(*a), soft_reset())[0])
    assert heal(rig.repo) is None
    assert moved and rig.head() == before != new
    assert (rig.gitdir / "index").read_bytes() == shared
    # Caught under the lock → nothing written at all; caught after → the proposal, then put back.
    assert len(written) == (0 if when == "before the lock" else 2)
    sync = rig.gitdir / githeal.SYNC_FILE
    assert not sync.exists() or sync.read_text().strip() != new


def test_skips_in_progress_operations_and_lock(rig):
    _box_commit(rig, "box", fileA="a2\n")
    (rig.gitdir / "MERGE_HEAD").write_text(rig.head() + "\n")
    assert heal(rig.repo) is None
    (rig.gitdir / "MERGE_HEAD").unlink()
    (rig.gitdir / "index.lock").write_text("")
    assert heal(rig.repo) is None
    (rig.gitdir / "index.lock").unlink()
    assert heal(rig.repo) is not None  # heals once the coast is clear


def test_non_repo_and_unborn_are_quiet(tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()
    assert heal(empty) is None
    subprocess.run(["git", "init", "-q", str(tmp_path / "unborn")], check=True)
    assert heal(tmp_path / "unborn") is None


def test_the_shim_proposes_nothing_with_healing_off(rig):
    rig.write("fileA", "a2\n")
    rig.box("add", "--", "fileA", env={"FY_GIT_SHIM_NO_HEAL": "1"})
    assert rig.box("commit", "-qm", "box", env={"FY_GIT_SHIM_NO_HEAL": "1"}).returncode == 0
    assert _pending(rig) == []


def test_sweep_gates_on_index_split_and_never_raises(monkeypatch):
    logged: list[str] = []
    monkeypatch.setattr(githeal.config, "box_git_index_split", lambda: False)
    monkeypatch.setattr(
        githeal, "_checkouts", lambda: pytest.fail("split off → must not even resolve checkouts")
    )
    githeal.sweep(logged.append)
    assert logged == []

    monkeypatch.setattr(githeal.config, "box_git_index_split", lambda: True)
    monkeypatch.setattr(githeal, "_checkouts", lambda: (githeal.Path("/m"), [githeal.Path("/m")]))
    monkeypatch.setattr(
        githeal, "heal_checkout", lambda co, main: (_ for _ in ()).throw(RuntimeError("boom"))
    )
    githeal.sweep(logged.append)  # must not raise — the reconcile loop depends on it
    assert logged and "sweep failed" in logged[0]


# ── the host runs nothing, and writes only where it means to ─────────────────────────────


def test_a_heal_runs_nothing_the_mount_plants(rig, tmp_path):
    # `.git/config` and the attributes are box-writable: git would run `core.fsmonitor` or a
    # clean filter as a command. The heal must not merely neutralise them — it runs no git at all
    # (`heal` refuses any process), and the planted canaries stay unwritten.
    _box_commit(rig, "box", fileA="a2\n", fileC="c1\n")
    canary = tmp_path / "PWNED"
    (rig.repo / ".gitattributes").write_text("* filter=evil\n")
    subprocess.run(
        ["git", "-C", str(rig.repo), "config", "filter.evil.clean", f"sh -c ': > {canary}; cat'"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(rig.repo), "config", "core.fsmonitor", f"sh -c ': > {canary}'"],
        check=True,
    )
    msg = heal(rig.repo)
    assert msg and "fast-forwarded" in msg
    assert not canary.exists()


def test_a_symlinked_proposal_is_never_installed(rig, tmp_path):
    # A proposal is a box-written file in the git dir; one that is a symlink would have the host
    # install (and later rewrite) a file of the box's choosing outside it.
    _synced_at(rig, rig.head())
    _box_commit(rig, "box", fileA="a2\n")
    prop = next(p for p in rig.gitdir.glob("index.fy-proposed.*"))
    outside = tmp_path / "outside-index"
    outside.write_bytes(prop.read_bytes())
    prop.unlink()
    prop.symlink_to(outside)
    before = (rig.gitdir / "index").read_bytes()
    assert heal(rig.repo) is None
    assert (rig.gitdir / "index").read_bytes() == before
    assert not (rig.gitdir / "index").is_symlink()


def test_a_symlinked_git_dir_is_not_followed(rig, tmp_path):
    # `.git` itself is box-writable: pointed at a directory of the operator's, a heal would read
    # and write there.
    elsewhere = tmp_path / "elsewhere"
    head = rig.head()
    (rig.repo / ".git").rename(elsewhere)
    (rig.repo / ".git").symlink_to(elsewhere)
    # A record for the REAL HEAD there — one a followed symlink would act on.
    (elsewhere / f"index.fy-record.{head}").write_text("")
    assert heal(rig.repo) is None
    assert not (elsewhere / githeal.SYNC_FILE).exists()  # (mountwrite would refuse this anyway)
    assert (elsewhere / f"index.fy-record.{head}").exists()  # …but nothing there was consumed


def test_a_worktree_git_file_pointing_outside_mains_worktrees_is_refused(rig, tmp_path):
    # A worktree's `.git` is a box-writable FILE naming its git dir. Only `<main>/.git/worktrees/*`
    # is ever this project's — anything else (another checkout's git dir) is not healed.
    wt = tmp_path / "wt"
    rig.host("worktree", "add", "-q", str(wt), "-b", "side")
    assert heal(wt, rig.repo) is None  # a real, confined worktree: quiet, no error
    (tmp_path / "o").mkdir()
    other = Rig(tmp_path / "o")  # a real repo with a HEAD, and a record the heal would act on
    other_head = other.host_commit("other", x="x\n")
    (other.gitdir / f"index.fy-record.{other_head}").write_text("")
    (wt / ".git").write_text(f"gitdir: {other.gitdir}\n")
    assert heal(wt, rig.repo) is None
    assert not (other.gitdir / githeal.SYNC_FILE).exists()


def test_a_worktree_heals_in_its_own_git_dir(rig, tmp_path):
    # The confined path works: a box commit in a worktree is healed in `<main>/.git/worktrees/<n>`.
    wt = tmp_path / "wt"
    rig.host("worktree", "add", "-q", str(wt), "-b", "side")
    wt_rig = Rig.__new__(Rig)
    wt_rig.__dict__.update(rig.__dict__, repo=wt)
    _box_commit(wt_rig, "box in wt", fileA="a-wt\n")
    msg = heal(wt, rig.repo)
    assert msg and "fast-forwarded" in msg
    assert wt_rig.host("status", "--porcelain").stdout == ""


def test_malformed_proposal_names_are_ignored(rig):
    (rig.gitdir / "index.fy-proposed.not-hex.x.ff").write_bytes(b"DIRC")
    assert heal(rig.repo) is None
    assert (rig.gitdir / "index.fy-proposed.not-hex.x.ff").exists()  # not ours — left alone


def test_the_sweep_visits_only_registered_worktrees(monkeypatch, tmp_path, register_worktree):
    # The worktrees root is box-writable: a dir (or a symlink to another checkout) planted there
    # must not become somewhere the supervisor heals.
    from foldyard import config, devmode

    main = tmp_path / "repo"
    (main / ".git").mkdir(parents=True)
    root = tmp_path / "repo-worktrees"
    register_worktree(main, "known", root / "known")
    (root / "planted" / ".git").mkdir(parents=True)
    monkeypatch.setattr(devmode, "main_repo", lambda: main)
    monkeypatch.setattr(config, "worktrees_root", lambda _base: root)
    assert githeal._checkouts() == (main, [main, (root / "known").resolve()])
