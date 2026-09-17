"""`fy worktree add` / `remove`, live on a Linux host: `add` registers a sibling checkout on its
own branch; its stack comes up beside main's in the one VM (`WORKTREE=<name> fy up`); `remove`
tears that stack down (containers AND the worktree's volumes), archives the worktree's bound-out
agent transcripts to the durable store BEFORE git deletes the tree, removes the checkout, drops
its local state, and leaves main's stack and the branch alone.

Host tier (tests/e2e_host.py). The verbs are host-only (`worktree._host_only`: in-box they would
edit the shared .git against a box-local worktrees dir). The example stack has no bind mounts, so
the worktree's stack ships its build context over the socket and the VM need not mount the
worktrees root (`machine.ensure` says so, as a warning). Lima 2.2.0, Fedora 44 guest.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

import pytest

from e2e_host import (
    PROJECT,
    _adopt_on_host,
    _probe_db,
    _service_is_listed,
    engine,
    ensure_vm,
    example_copy,
    fy,
    fy_ok,
    host_tier,
    state_dir,
)

pytestmark = host_tier

NAME = "feat"
WT_PROJECT = f"{PROJECT}-{NAME}"


@dataclass
class Rig:
    main: Path
    wt_root: Path
    archive: Path
    env: dict[str, str]

    @property
    def feat(self) -> Path:
        return self.wt_root / NAME

    @property
    def feat_env(self) -> dict[str, str]:
        return {**self.env, "WORKTREE": NAME}


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=True
    ).stdout


def _worktree_paths(main: Path) -> list[str]:
    porcelain = _git(main, "worktree", "list", "--porcelain")
    return [
        line.split(" ", 1)[1] for line in porcelain.splitlines() if line.startswith("worktree ")
    ]


@pytest.fixture(scope="module")
def rig(tmp_path_factory):
    base = tmp_path_factory.mktemp("worktree")
    main = example_copy(base)
    # The bound-out transcript dir is a consumer's to gitignore (`fy init` does); the example
    # commits none, and `git worktree remove` refuses a tree with untracked files.
    with (main / ".gitignore").open("a") as fh:
        fh.write(".devbox-claude/\n")
    _git(main, "add", ".gitignore")
    _git(main, "-c", "user.email=e2e@foldyard", "-c", "user.name=e2e", "commit", "-qm", "ignore")
    # A NAMED volume for the db (the example ships none: postgres's data dir is an anonymous
    # volume there), so `remove`'s volume sweep — `{project}_*` — has something real to drop.
    compose = main / "compose.yml"
    text = compose.read_text()
    marker = "    image: docker.io/library/postgres:16-alpine\n"
    assert marker in text
    text = text.replace(marker, marker + "    volumes:\n      - pgdata:/var/lib/postgresql/data\n")
    compose.write_text(text + "\nvolumes:\n  pgdata: {}\n")
    _git(main, "add", "compose.yml")
    _git(main, "-c", "user.email=e2e@foldyard", "-c", "user.name=e2e", "commit", "-qm", "volume")
    wt_root = base / "example-worktrees"
    archive = base / "transcripts-archive"
    env = {"FOLDYARD_WORKTREES_ROOT": str(wt_root), "FOLDYARD_TRANSCRIPTS_ARCHIVE": str(archive)}
    _adopt_on_host(main, env)
    ensure_vm(main, env_extra=env)
    fy_ok(["up"], main, env_extra=env)
    r = Rig(main=main, wt_root=wt_root, archive=archive, env=env)
    yield r
    if r.feat.exists():  # whatever a failing test left
        fy(["worktree", "remove", NAME, "--yes", "--force"], main, timeout=600, env_extra=env)
    fy(["down"], main, timeout=180, env_extra=env)


def test_add_registers_a_sibling_checkout_on_its_own_branch(rig):
    added = fy_ok(["worktree", "add", NAME], rig.main, timeout=300, env_extra=rig.env)
    assert "worktree ready" in added.out, added.out
    assert (rig.feat / ".git").exists(), added.out
    assert str(rig.feat) in _worktree_paths(rig.main), added.out
    assert _git(rig.feat, "rev-parse", "--abbrev-ref", "HEAD").strip() == f"wt/{NAME}"
    assert _git(rig.feat, "status", "--porcelain") == ""  # clean: nothing leaked into the tree


def test_the_worktree_stack_comes_up_beside_mains(rig):
    _adopt_on_host(rig.feat, rig.feat_env)
    fy_ok(["up"], rig.feat, env_extra=rig.feat_env)
    feat_ps = fy_ok(["ps"], rig.feat, timeout=60, env_extra=rig.feat_env)
    assert _service_is_listed(feat_ps.out, WT_PROJECT, "api"), feat_ps.out
    main_ps = fy_ok(["ps"], rig.main, timeout=60, env_extra=rig.env)
    assert _service_is_listed(main_ps.out, PROJECT, "api"), main_ps.out
    assert "reachable" in _probe_db(f"{WT_PROJECT}_default")


def test_remove_tears_down_archives_and_drops_the_checkout_but_not_main_or_the_branch(rig):
    # A transcript the box would have bound out (transcripts.bound_out_dir, `here` = "."), and a
    # posture/state dir for the worktree — both must be gone from the tree, the transcript
    # ARCHIVED first.
    session = rig.feat / ".devbox-claude" / "projects" / "-work-feat" / "session.jsonl"
    session.parent.mkdir(parents=True)
    session.write_text('{"type":"user","text":"hello from feat"}\n')
    marker = state_dir() / "worktrees" / NAME / "marker"
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text("x")
    volumes_before = engine("volume", "ls", "-q", "--filter", f"name={WT_PROJECT}_").stdout.split()
    assert volumes_before, "the worktree stack should own the named db volume (fixture)"

    removed = fy_ok(["worktree", "remove", NAME, "--yes"], rig.main, timeout=600, env_extra=rig.env)
    assert f"worktree '{NAME}' removed" in removed.out, removed.out

    # 1+2. its box (none here) and its stack: containers AND its prefixed volumes gone …
    leftover = engine(
        "ps", "-a", "-q", "--filter", f"label=com.docker.compose.project={WT_PROJECT}"
    )
    assert leftover.stdout.strip() == "", f"{leftover.stdout}\n{removed.out}"
    volumes_after = engine("volume", "ls", "-q", "--filter", f"name={WT_PROJECT}_").stdout.split()
    assert volumes_after == [], volumes_after
    # … while main's stack is untouched.
    main_ps = fy_ok(["ps"], rig.main, timeout=60, env_extra=rig.env)
    assert _service_is_listed(main_ps.out, PROJECT, "api"), main_ps.out
    assert "reachable" in _probe_db(f"{PROJECT}_default")
    # 3. the transcript reached the durable archive before the tree went.
    assert f"archiving worktree '{NAME}' transcripts" in removed.out, removed.out
    archived = list(rig.archive.rglob("session.jsonl"))
    assert archived and "hello from feat" in archived[0].read_text(), removed.out
    # 4. the checkout is gone and git no longer tracks it; the branch is KEPT.
    assert not rig.feat.exists()
    assert str(rig.feat) not in _worktree_paths(rig.main)
    assert f"wt/{NAME}" in _git(rig.main, "branch", "--list", f"wt/{NAME}")
    # 5. its local state is gone.
    assert not marker.parent.exists(), removed.out
