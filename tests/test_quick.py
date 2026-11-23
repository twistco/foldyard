"""quick.py — `fy worktree quick`, the worktree a box makes for itself. A REAL git repo; the engine
(the helper container that mounts the volumes) is faked, and `/proc/self/mountinfo` is a file. The
live proof is test_box_e2e's quick worktree; the helper's own logic is test_quick_attach.py."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from foldyard import box, config, crossmask, devmode, quick, sandbox, stack, worktree
from foldyard import worktree_registry as registry

REAL_RUN = subprocess.run
DIRS = ["web/node_modules", "api/.venv"]


def _git(where: Path, *args: str) -> str:
    cmd = ["git", "-C", str(where), "-c", "user.name=t", "-c", "user.email=t@t", *args]
    return REAL_RUN(cmd, check=True, capture_output=True, text=True).stdout


@pytest.fixture
def world(tmp_path, monkeypatch):
    main, root = tmp_path / "repo", tmp_path / "repo-worktrees"
    main.mkdir()
    root.mkdir()
    _git(main, "init", "-q", "-b", "main")
    for d in ("web", "api"):
        (main / d).mkdir()
        (main / d / "x").write_text("x\n")
    (main / ".gitignore").write_text("**/node_modules/\n**/.venv/\n")
    _git(main, "add", "-A")
    _git(main, "commit", "-qm", "x")
    table = tmp_path / "mountinfo"
    table.write_text("")
    for mod, name, value in (
        (stack, "main_repo", lambda: main),
        (devmode, "main_repo", lambda: main),
        (config, "worktrees_root", lambda m: root),
        (config, "project_prefix", lambda: "acme"),
        (config, "box_shadow_volumes", lambda: list(DIRS)),
        (config, "worktree_base", lambda: None),
        (config, "in_box", lambda: True),
        (config, "active_worktree", lambda: ""),
        (sandbox, "wanted", lambda: False),
        (box, "engine_env", lambda: ("podman", {"CONTAINER_HOST": "unix:///nonexistent/fy"})),
        (quick, "_OWN_MOUNTINFO", table),
        (quick.os.path, "ismount", lambda p: True),
    ):
        monkeypatch.setattr(mod, name, value)
    monkeypatch.setenv(crossmask.SNAPSHOT_ENV, json.dumps({"registered": 3}))
    state: dict = {"calls": [], "answer": None, "order": []}

    def run(cmd, **kw):
        if cmd[0] == "git":
            if "hook" in cmd:
                state["order"].append("hook")
            elif "worktree" in cmd and "add" in cmd:
                state["order"].append("worktree")
            return REAL_RUN(cmd, **kw)
        state["calls"].append(cmd)
        if cmd[1] == "inspect":
            return subprocess.CompletedProcess(cmd, 0, "sha256:abc\n", "")
        if cmd[1] == "run":
            mode, *targets = cmd[cmd.index("-") + 1 :]
            vols = [a.split(":")[0] for a in cmd if ":/fy/" in a]
            state["order"].append(mode)
            if state["answer"] is not None:
                return subprocess.CompletedProcess(
                    cmd, 0, json.dumps(state["answer"]).encode(), b""
                )
            lines = [
                f"1 1 0:1 /s/volumes/{v}/_data {t} rw - xfs d rw"
                for v, t in zip(vols, targets, strict=True)
            ]
            table.write_text("\n".join(lines) + "\n" if mode == "attach" else "")
            got = "attached" if mode == "attach" else "detached"
            answer = {"refused": None, "results": dict.fromkeys(targets, got)}
            return subprocess.CompletedProcess(cmd, 0, json.dumps(answer).encode(), b"")
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(quick.subprocess, "run", run)
    state.update(main=main, root=root, table=table)
    yield state
    for d in root.rglob("*"):  # so pytest can clean the read-only dirs up
        if d.is_dir() and not d.is_symlink():
            d.chmod(0o755)


def _runs(world, mode):
    return [c for c in world["calls"] if c[1] == "run" and mode in c]


def test_it_makes_a_worktree_on_its_own_branch_in_the_worktrees_root(world, capsys):
    assert quick.add("fix") == 0
    wt = world["root"] / "fix"
    assert _git(wt, "rev-parse", "--abbrev-ref", "HEAD").strip() == "wt/fix"
    assert (wt / "web" / "x").read_text() == "x\n"
    assert quick.is_quick(world["main"], wt)
    assert quick.names(world["main"]) == ["fix"]
    assert "fy worktree remove fix" in capsys.readouterr().out


def test_the_volumes_are_named_as_its_own_box_would_name_them(world):
    quick.add("fix")
    (helper,) = _runs(world, "attach")
    assert [a for a in helper if ":/fy/" in a] == [
        f"{box.shadow_volume('acme-fix-devbox', d)}:/fy/{n}" for n, d in enumerate(DIRS)
    ]
    assert helper[-2:] == [str(world["root"] / "fix" / d) for d in DIRS]


def test_the_helper_gets_three_capabilities_and_only_this_boxs_processes(world):
    # Anything holding the engine socket can start a container; what foldyard starts is the least
    # that works. A change here is a change to what every box routinely runs.
    quick.add("fix")
    (helper,) = _runs(world, "attach")
    assert "--privileged" not in helper and "host" not in helper
    assert helper[helper.index("--pid") + 1] == "container:acme-devbox"
    assert helper[helper.index("--network") + 1] == "none"
    assert helper[helper.index("--cap-drop") + 1] == "all"
    caps = [helper[i + 1] for i, a in enumerate(helper) if a == "--cap-add"]
    assert caps == ["sys_admin", "sys_chroot", "sys_ptrace"]
    assert helper[helper.index("--pull") + 1] == "never"
    assert [a for a in helper if a == "-v"] == ["-v"] * len(DIRS)  # the volumes and nothing else


def test_the_hook_runs_once_the_volumes_are_on_and_not_at_the_checkout(world):
    marker = world["main"] / "hook-ran"
    hook = world["main"] / ".git" / "hooks" / "post-checkout"
    hook.parent.mkdir(exist_ok=True)
    hook.write_text(f'#!/bin/sh\necho "$1 $3" >> "{marker}"\n')
    hook.chmod(0o755)
    quick.add("fix")
    order = [o for o in world["order"] if o in ("worktree", "attach", "hook")]
    assert order == ["worktree", "attach", "hook"]
    assert marker.read_text() == "0" * 40 + " 1\n"  # once, as `git worktree add` would call it


def test_the_dir_under_a_volume_is_read_only(world):
    # Your computer's share enforces the mode on the box's root too: where the volume isn't mounted
    # (a restart), an install fails instead of landing on your disk.
    quick.add("fix")
    for d in DIRS:
        assert (world["root"] / "fix" / d).stat().st_mode & 0o777 == 0o555
    assert _git(world["root"] / "fix", "status", "--porcelain") == ""


def test_no_volume_means_no_hook_and_says_so(world, capsys):
    world["answer"] = {"refused": "setns: no", "results": {}}
    assert quick.add("fix") == 0  # the checkout is still worth having
    assert "hook" not in world["order"]
    err = capsys.readouterr().err
    assert "no volume on web/node_modules, api/.venv: setns: no" in err
    assert "fy worktree add fix" in err


def test_gvisor_mounts_nothing(world, monkeypatch, capsys):
    monkeypatch.setattr(sandbox, "wanted", lambda: True)
    quick.add("fix")
    assert _runs(world, "attach") == []
    assert "gVisor" in capsys.readouterr().err


def test_again_after_a_restart_mounts_the_volumes_again(world):
    quick.add("fix")
    quick.add("fix")
    assert len(_runs(world, "attach")) == 1  # they were still there
    world["table"].write_text("")
    assert quick.add("fix") == 0
    assert len(_runs(world, "attach")) == 2
    world["table"].write_text("")
    assert quick.mount_all() == 0
    assert len(_runs(world, "attach")) == 3


def test_a_planted_link_is_never_followed(world, tmp_path, capsys):
    quick.add("fix")
    world["table"].write_text("")
    d = world["root"] / "fix" / "web" / "node_modules"
    d.rmdir()
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    d.symlink_to(elsewhere)
    assert quick.add("fix") == 1
    assert "symlink" in capsys.readouterr().err
    assert elsewhere.stat().st_mode & 0o777 != 0o555
    assert str(d) not in _runs(world, "attach")[-1]


@pytest.mark.parametrize("name", ["main", "worktrees", "a/b", "..", "/etc"])
def test_the_names_fy_worktree_add_refuses(world, name):
    assert quick.add(name) == 1
    assert world["calls"] == []


def test_a_registered_worktree_has_its_own_box(world, capsys):
    assert quick.add("registered") == 1
    assert "WORKTREE=registered fy box exec" in capsys.readouterr().err


def test_another_checkout_there_is_left_alone(world, capsys):
    (world["root"] / "mine").mkdir()
    assert quick.add("mine") == 1
    assert "isn't a quick worktree" in capsys.readouterr().err
    assert world["calls"] == []


def test_a_worktrees_root_the_box_cannot_see_is_refused(world, monkeypatch, capsys):
    monkeypatch.setattr(quick.os.path, "ismount", lambda p: False)
    assert quick.add("fix") == 1
    assert "isn't mounted in this box" in capsys.readouterr().err
    assert not (world["root"] / "fix").exists()


def test_on_your_computer_it_names_the_other_verb(world, monkeypatch, capsys):
    monkeypatch.setattr(config, "in_box", lambda: False)
    assert quick.add("fix") == 1
    assert "fy worktree add fix" in capsys.readouterr().err


def test_remove_takes_the_volumes_too_and_keeps_the_branch(world):
    quick.add("fix")
    assert worktree.remove("fix") == 0  # `fy worktree remove`, in the box
    assert not (world["root"] / "fix").exists()
    assert len(_runs(world, "detach")) == 1
    vols = [box.shadow_volume("acme-fix-devbox", d) for d in DIRS]
    assert ["podman", "volume", "rm", *vols] in world["calls"]
    assert "wt/fix" in _git(world["main"], "branch", "--list", "wt/fix")


def test_remove_refuses_uncommitted_work_and_unmounts_nothing(world, capsys):
    quick.add("fix")
    (world["root"] / "fix" / "new").write_text("n")
    assert worktree.remove("fix") == 1
    assert "uncommitted work" in capsys.readouterr().err
    assert _runs(world, "detach") == []
    assert worktree.remove("fix", force=True) == 0


def test_remove_stops_where_a_volume_cannot_be_unmounted(world, capsys):
    quick.add("fix")
    world["answer"] = {"refused": None, "results": {}}
    assert worktree.remove("fix") == 1
    assert "a volume is in use" in capsys.readouterr().err
    assert (world["root"] / "fix" / "web" / "x").exists()


def test_remove_of_any_other_worktree_is_still_your_computers(world, capsys):
    _git(world["main"], "worktree", "add", "-q", "-b", "o", str(world["root"] / "other"))
    assert worktree.remove("other") == 1
    assert "run on your computer" in capsys.readouterr().err


def test_fy_up_is_refused_there_and_nowhere_else(world, capsys):
    quick.add("fix")
    assert quick.refuse_stack(world["main"], "fix", "fy up")
    assert "fy worktree add fix --box" in capsys.readouterr().err
    assert not quick.refuse_stack(world["main"], "", "fy up")
    assert not quick.refuse_stack(world["main"], "other", "fy up")


def test_on_your_computer_the_boxs_marker_refuses_nothing(world, monkeypatch):
    # The shared `.git` is box-written: a marker planted in a registered worktree must not stop it.
    quick.add("fix")
    monkeypatch.setattr(config, "in_box", lambda: False)
    assert not quick.refuse_stack(world["main"], "fix", "fy up")


def test_promoting_it_makes_it_an_ordinary_worktree(world, monkeypatch, tmp_path):
    quick.add("fix")
    wt = world["root"] / "fix"
    monkeypatch.setattr(config, "in_box", lambda: False)
    monkeypatch.setattr(config, "state_dir", lambda: tmp_path / "state")
    monkeypatch.setattr(worktree, "_no_pooled_port", lambda n: False)
    monkeypatch.setattr(worktree, "_claim_ports", lambda *a, **k: True)
    monkeypatch.setattr(worktree, "_say_unmasked", lambda *a, **k: None)
    monkeypatch.setattr(registry, "store_file", lambda m: tmp_path / "reg.json")
    assert worktree._register_existing(world["main"], "fix", wt, assume_yes=True) == 0
    assert not quick.is_quick(world["main"], wt)
    for d in DIRS:
        assert (wt / d).stat().st_mode & 0o777 == 0o755
    assert "fix" in registry.checkouts(world["main"])


def test_promoting_follows_no_link_the_box_planted(world, tmp_path):
    quick.add("fix")
    wt = world["root"] / "fix"
    mine = tmp_path / "mine"
    mine.mkdir(mode=0o700)
    (wt / "web" / "node_modules").rmdir()
    (wt / "web" / "node_modules").symlink_to(mine)
    quick.promoted(world["main"], wt)
    assert mine.stat().st_mode & 0o777 == 0o700


def test_a_git_file_pointing_elsewhere_is_no_quick_worktree(world, tmp_path):
    evil = tmp_path / "evil"
    evil.mkdir()
    (evil / quick.MARKER).write_text("x")
    d = world["root"] / "planted"
    d.mkdir()
    (d / ".git").write_text(f"gitdir: {evil}\n")
    assert not quick.is_quick(world["main"], d)
    (d / ".git").write_text(f"gitdir: {world['main']}/.git/worktrees/../../../evil\n")
    assert not quick.is_quick(world["main"], d)
    quick.promoted(world["main"], d)
    assert (evil / quick.MARKER).exists()


def test_another_dirs_git_file_cannot_borrow_a_quick_worktrees_marker(world, tmp_path):
    quick.add("fix")
    (tmp_path / "evil" / "fix").mkdir(parents=True)
    d = world["root"] / "planted"
    d.mkdir()
    (d / ".git").write_text(f"gitdir: {tmp_path}/evil/fix\n")
    assert not quick.is_quick(world["main"], d)


def test_your_computer_lists_it_as_quick_and_the_doctor_does_not_warn(
    world, monkeypatch, capsys, tmp_path
):
    quick.add("fix")
    _git(world["main"], "worktree", "add", "-q", "-b", "o", str(world["root"] / "stray"))
    monkeypatch.setattr(config, "in_box", lambda: False)
    monkeypatch.setattr(registry, "store_file", lambda m: tmp_path / "reg.json")
    capsys.readouterr()
    worktree.list_()
    out = capsys.readouterr().out
    assert "(quick)" in next(ln for ln in out.splitlines() if " fix " in ln)
    assert "runs nothing for: stray." in out
    row = devmode._worktree_registry_check()
    assert "stray" in row[2] and "`fy worktree add fix`" not in row[2]
    _git(world["main"], "worktree", "remove", "--force", str(world["root"] / "stray"))
    row = devmode._worktree_registry_check()
    # Named, never just counted: a box decides what is marked, and the operator should see it.
    assert "quick (marked so by a box): fix" in row[2]
