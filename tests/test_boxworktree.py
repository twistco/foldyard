"""boxworktree.py — `fy worktree add` in a box, the worktree a box makes for itself. A REAL git repo;
the engine (the helper container that mounts the volumes) is faked, and `/proc/self/mountinfo` is a
file. The live proof is test_box_e2e's box-made worktree; the helper's own logic is
test_boxworktree_attach.py."""

from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path

import pytest

from foldyard import box, boxworktree, config, crossmask, devmode, sandbox, stack, worktree
from foldyard import worktree_registry as registry

REAL_RUN = subprocess.run
# `remove` runs foldyard's own check for uncommitted work (worktree._UNCOMMITTED_SH) for real.
pytestmark = pytest.mark.spawns("sh")
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
        (box, "engine_env", lambda: ("podman", {"CONTAINER_HOST": "unix:///nonexistent/fy"})),
        (boxworktree, "_OWN_MOUNTINFO", table),
        (boxworktree, "_LOCK", str(tmp_path / "lock")),
        (boxworktree.os.path, "ismount", lambda p: True),
    ):
        monkeypatch.setattr(mod, name, value)
    monkeypatch.delenv(sandbox.BAKED_ENV, raising=False)
    monkeypatch.setenv(crossmask.SNAPSHOT_ENV, json.dumps({"registered": 3}))
    state: dict = {"calls": [], "answer": None, "order": []}

    def run(cmd, **kw):
        if cmd[0] == "git":
            if "hook" in cmd:
                state["order"].append("hook")
            elif "worktree" in cmd and "add" in cmd:
                state["order"].append("worktree")
            return REAL_RUN(cmd, **kw)
        if cmd[0] == "sh":
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

    monkeypatch.setattr(boxworktree.subprocess, "run", run)
    state.update(main=main, root=root, table=table)
    yield state
    for d in root.rglob("*"):  # so pytest can clean the read-only dirs up
        if d.is_dir() and not d.is_symlink():
            d.chmod(0o755)


def _runs(world, mode):
    return [c for c in world["calls"] if c[1] == "run" and mode in c]


def test_it_makes_a_worktree_on_its_own_branch_in_the_worktrees_root(world, capsys):
    assert boxworktree.add("fix") == 0
    wt = world["root"] / "fix"
    assert _git(wt, "rev-parse", "--abbrev-ref", "HEAD").strip() == "wt/fix"
    assert (wt / "web" / "x").read_text() == "x\n"
    assert boxworktree.is_box_made(world["main"], wt)
    assert boxworktree.names(world["main"]) == ["fix"]
    assert "fy worktree remove fix" in capsys.readouterr().out


def test_the_volumes_are_named_as_its_own_box_would_name_them(world):
    boxworktree.add("fix")
    (helper,) = _runs(world, "attach")
    assert [a for a in helper if ":/fy/" in a] == [
        f"{box.shadow_volume('acme-fix-devbox', d)}:/fy/{n}" for n, d in enumerate(DIRS)
    ]
    assert helper[-2:] == [str(world["root"] / "fix" / d) for d in DIRS]


def test_the_helper_gets_three_capabilities_and_only_this_boxs_processes(world):
    # Anything holding the engine socket can start a container; what foldyard starts is the least
    # that works. A change here is a change to what every box routinely runs.
    boxworktree.add("fix")
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
    boxworktree.add("fix")
    order = [o for o in world["order"] if o in ("worktree", "attach", "hook")]
    assert order == ["worktree", "attach", "hook"]
    assert marker.read_text() == "0" * 40 + " 1\n"  # once, as `git worktree add` would call it


def test_the_dir_under_a_volume_is_read_only(world):
    # A safety net for where the volume isn't mounted (a restart): the share holds the box's root to
    # the mode too, so an install that writes into the dir fails instead of landing on your disk.
    boxworktree.add("fix")
    for d in DIRS:
        assert (world["root"] / "fix" / d).stat().st_mode & 0o777 == 0o555
    assert _git(world["root"] / "fix", "status", "--porcelain") == ""


def test_no_volume_means_no_hook_and_says_so(world, capsys):
    world["answer"] = {"refused": "setns: no", "results": {}}
    assert boxworktree.add("fix") == 0  # the checkout is still worth having
    assert "hook" not in world["order"]
    err = capsys.readouterr().err
    assert "no volume on web/node_modules, api/.venv: setns: no" in err
    assert "fy worktree add fix" in err


def test_the_hook_a_failed_mount_skipped_runs_once_the_volumes_are_on(world):
    world["answer"] = {"refused": "setns: no", "results": {}}
    boxworktree.add("fix")
    world["answer"] = None
    assert boxworktree.add("fix") == 0
    assert world["order"].count("hook") == 1
    world["table"].write_text("")
    assert boxworktree.add("fix") == 0  # a restart later: its setup is done
    assert world["order"].count("hook") == 1


def test_a_dir_inside_another_declared_one_is_on_that_ones_volume(world, monkeypatch):
    # A placeholder for either would be in the other's way, whichever came first.
    for dirs in (
        ["web/node_modules", "web/node_modules/.cache"],
        ["web/node_modules/.cache", "web/node_modules", "web/node_modules2"],
    ):
        monkeypatch.setattr(config, "box_shadow_volumes", lambda d=dirs: list(d))
        got = boxworktree.volumes(world["root"] / "fix", "fix")
        rels = [t.removeprefix(str(world["root"] / "fix") + "/") for t in got]
        assert rels == [d for d in dirs if not d.endswith(".cache")]


def test_gvisor_mounts_nothing(world, monkeypatch, capsys):
    monkeypatch.setenv(sandbox.BAKED_ENV, "gvisor")
    boxworktree.add("fix")
    assert _runs(world, "attach") == []
    assert "gVisor" in capsys.readouterr().err


def test_what_the_box_runs_under_is_the_boxs_own_word_not_the_configs(world, monkeypatch):
    # A posture turned on since this box was made: it still runs under the engine's own runtime.
    monkeypatch.setattr(sandbox, "wanted", lambda: True)
    boxworktree.add("fix")
    assert len(_runs(world, "attach")) == 1


def test_again_after_a_restart_mounts_the_volumes_again(world):
    boxworktree.add("fix")
    boxworktree.add("fix")
    assert len(_runs(world, "attach")) == 1  # they were still there
    world["table"].write_text("")
    assert boxworktree.add("fix") == 0
    assert len(_runs(world, "attach")) == 2
    world["table"].write_text("")
    assert boxworktree.mount_all() == 0
    assert len(_runs(world, "attach")) == 3


def test_a_restart_mounts_this_boxs_own_worktrees_and_no_other_boxs(world, monkeypatch):
    boxworktree.add("fix")
    monkeypatch.setattr(config, "active_worktree", lambda: "other")  # another box of the project
    world["table"].write_text("")
    assert boxworktree.mount_all() == 0
    assert len(_runs(world, "attach")) == 1
    monkeypatch.setattr(config, "active_worktree", lambda: "")
    boxworktree.mount_all()
    assert len(_runs(world, "attach")) == 2


def test_one_made_before_the_marker_named_its_box_is_mounted_by_any(world):
    boxworktree.add("fix")
    marker = boxworktree._marker(world["main"], world["root"] / "fix")
    assert marker is not None
    marker.write_text(boxworktree._MADE)
    world["table"].write_text("")
    boxworktree.mount_all()
    assert len(_runs(world, "attach")) == 2


def test_at_a_boxs_start_it_tries_again_until_the_engine_answers(world, monkeypatch):
    # The box starts it as it comes up, when the engine may not call the box running yet.
    boxworktree.add("fix")
    world["table"].write_text("")
    world["answer"] = {"refused": "container not running", "results": {}}
    waits = []

    def sleep(s):
        waits.append(s)
        if len(waits) == 2:
            world["answer"] = None

    monkeypatch.setattr(boxworktree.time, "sleep", sleep)
    assert boxworktree.mount_all(wait=True) == 0
    assert len(waits) == 2 and len(_runs(world, "attach")) == 4
    world["table"].write_text("")
    world["answer"] = {"refused": "no", "results": {}}
    waits.clear()
    monkeypatch.setattr(boxworktree.time, "sleep", waits.append)
    boxworktree.mount_all(wait=True)
    assert 2 < len(waits) < 20  # it gives up


def test_at_a_start_under_gvisor_there_is_nothing_to_wait_for(world, monkeypatch):
    boxworktree.add("fix")
    world["table"].write_text("")
    monkeypatch.setenv(sandbox.BAKED_ENV, "gvisor")
    monkeypatch.setattr(boxworktree.time, "sleep", lambda s: pytest.fail("waited"))
    assert boxworktree.mount_all(wait=True) == 0


@pytest.mark.spawns("sh")
def test_the_boxs_start_command_never_holds_the_box_up(tmp_path):
    # No foldyard yet (the first start), and one that hangs or fails: PID 1's sleep runs at once.
    home = tmp_path / "home"
    for body in (None, "sleep 30", "exit 1"):
        if body:
            fy = home / ".local" / "bin" / "foldyard"
            fy.parent.mkdir(parents=True, exist_ok=True)
            fy.write_text(f'#!/bin/sh\necho "$@" > {tmp_path}/ran\n{body}\n')
            fy.chmod(0o755)
        env = {"HOME": str(home), "PATH": "/usr/bin:/bin"}
        out = REAL_RUN(
            boxworktree.start_argv("echo", "up", "again"),
            env=env,
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert out.stdout.split() == ["up", "again"]  # every word of it


@pytest.mark.spawns("sh")
@pytest.mark.parametrize("where", ["home/.local/bin", "image/bin"])
def test_the_start_finds_foldyard_where_the_bootstrap_does(tmp_path, where):
    # The bootstrap installs none where the image's own, anywhere on PATH, is the right version.
    fy = tmp_path / where / "foldyard"
    fy.parent.mkdir(parents=True)
    fy.write_text(f'#!/bin/sh\necho "$@" > {tmp_path}/ran\n')
    fy.chmod(0o755)
    env = {"HOME": f"{tmp_path}/home", "PATH": f"/usr/bin:/bin:{tmp_path}/image/bin"}
    REAL_RUN(boxworktree.start_argv("true"), env=env, timeout=10)
    deadline = time.monotonic() + 10
    while not (ran := tmp_path / "ran").exists() or not ran.read_text():
        assert time.monotonic() < deadline, "never run"
        time.sleep(0.02)
    assert ran.read_text() == "worktree remount --wait\n"


def test_a_planted_link_is_never_followed(world, tmp_path, capsys):
    boxworktree.add("fix")
    world["table"].write_text("")
    d = world["root"] / "fix" / "web" / "node_modules"
    d.rmdir()
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    d.symlink_to(elsewhere)
    assert boxworktree.add("fix") == 1
    assert "symlink" in capsys.readouterr().err
    assert elsewhere.stat().st_mode & 0o777 != 0o555
    assert str(d) not in _runs(world, "attach")[-1]


@pytest.mark.parametrize("name", ["main", "worktrees", "a/b", "..", "/etc"])
def test_the_names_fy_worktree_add_refuses(world, name):
    assert boxworktree.add(name) == 1
    assert world["calls"] == []


def test_a_registered_worktree_has_its_own_box(world, capsys):
    assert boxworktree.add("registered") == 1
    assert "WORKTREE=registered fy box exec" in capsys.readouterr().err


def test_another_checkout_there_is_left_alone(world, capsys):
    (world["root"] / "mine").mkdir()
    assert boxworktree.add("mine") == 1
    assert "no box made it" in capsys.readouterr().err
    assert world["calls"] == []


def test_a_worktrees_root_the_box_cannot_see_is_refused(world, monkeypatch, capsys):
    monkeypatch.setattr(boxworktree.os.path, "ismount", lambda p: False)
    assert boxworktree.add("fix") == 1
    assert "isn't mounted in this box" in capsys.readouterr().err
    assert not (world["root"] / "fix").exists()


def test_fy_worktree_add_in_a_box_is_this(world):
    assert worktree.add("fix", "fix/it", "main") == 0
    wt = world["root"] / "fix"
    assert _git(wt, "rev-parse", "--abbrev-ref", "HEAD").strip() == "fix/it"
    assert boxworktree.is_box_made(world["main"], wt)


def test_a_box_of_its_own_is_your_computers_to_start(world, capsys):
    assert worktree.add("fix", box_up=True) == 1
    assert "fy worktree add fix --box" in capsys.readouterr().err
    assert not (world["root"] / "fix").exists()


def test_remove_takes_the_volumes_too_and_keeps_the_branch(world):
    boxworktree.add("fix")
    assert worktree.remove("fix") == 0  # `fy worktree remove`, in the box
    assert not (world["root"] / "fix").exists()
    assert len(_runs(world, "detach")) == 1
    vols = [box.shadow_volume("acme-fix-devbox", d) for d in DIRS]
    assert ["podman", "volume", "rm", *vols] in world["calls"]
    assert "wt/fix" in _git(world["main"], "branch", "--list", "wt/fix")


def _parked(world):
    return _git(world["main"], "for-each-ref", "--format=%(refname)", "refs/fy/removed/").split()


def test_remove_refuses_uncommitted_work_and_unmounts_nothing(world, capsys):
    boxworktree.add("fix")
    (world["root"] / "fix" / "new").write_text("n")
    assert worktree.remove("fix") == 1
    err = capsys.readouterr().err
    assert "uncommitted work" in err and "--force" in err
    assert _runs(world, "detach") == [] and _parked(world) == []


def test_remove_force_saves_uncommitted_work_first(world, capsys):
    # What the option's help says, and what it does on your computer.
    boxworktree.add("fix")
    wt = world["root"] / "fix"
    (wt / "new").write_text("n\n")
    (wt / "web" / "x").write_text("edited\n")
    assert worktree.remove("fix", force=True) == 0
    (ref,) = _parked(world)
    assert ref in capsys.readouterr().out
    assert _git(world["main"], "show", f"{ref}:new") == "n\n"
    assert _git(world["main"], "show", f"{ref}:web/x") == "edited\n"
    assert not wt.exists()


def test_remove_force_with_nothing_to_save_makes_no_ref(world):
    boxworktree.add("fix")
    assert worktree.remove("fix", force=True) == 0
    assert _parked(world) == []


def test_remove_discard_saves_nothing(world):
    boxworktree.add("fix")
    (world["root"] / "fix" / "new").write_text("n")
    assert worktree.remove("fix", discard=True) == 0
    assert _parked(world) == [] and not (world["root"] / "fix").exists()


@pytest.mark.parametrize("force", [False, True])
def test_remove_stops_where_the_work_cannot_be_checked_or_saved(world, monkeypatch, capsys, force):
    boxworktree.add("fix")
    (world["root"] / "fix" / "new").write_text("n")
    monkeypatch.setattr(worktree, "_UNCOMMITTED_SH", "exit 3")
    assert worktree.remove("fix", force=force) == 1
    assert "--discard" in capsys.readouterr().err
    assert (world["root"] / "fix" / "new").exists() and _runs(world, "detach") == []


def test_remove_stops_where_the_ref_cannot_be_written(world, capsys):
    boxworktree.add("fix")
    (world["root"] / "fix" / "new").write_text("n")
    (world["main"] / ".git" / "refs" / "fy").write_text("in the way")
    assert worktree.remove("fix", force=True) == 1
    assert (world["root"] / "fix" / "new").exists()


def test_remove_stops_where_a_volume_cannot_be_unmounted(world, capsys):
    boxworktree.add("fix")
    world["answer"] = {"refused": None, "results": {}}
    assert worktree.remove("fix") == 1
    assert "a volume is in use" in capsys.readouterr().err
    assert (world["root"] / "fix" / "web" / "x").exists()


def test_remove_of_any_other_worktree_is_still_your_computers(world, capsys):
    _git(world["main"], "worktree", "add", "-q", "-b", "o", str(world["root"] / "other"))
    assert worktree.remove("other") == 1
    assert "run on your computer" in capsys.readouterr().err


def test_fy_up_is_refused_there_and_nowhere_else(world, capsys):
    boxworktree.add("fix")
    assert boxworktree.refuse_stack(world["main"], "fix", "fy up")
    assert "fy worktree add fix --box" in capsys.readouterr().err
    assert not boxworktree.refuse_stack(world["main"], "", "fy up")
    assert not boxworktree.refuse_stack(world["main"], "other", "fy up")


def test_fy_up_says_so_before_it_resolves_the_stack(world, monkeypatch, capsys):
    # A pooled port is unknown to the box for a worktree made in it: resolving it would stop the
    # verb first, with the wrong advice.
    from foldyard import preflight

    boxworktree.add("fix")
    monkeypatch.setattr(preflight, "check_or_abort", lambda v: None)
    monkeypatch.setattr(stack, "_active_worktree", lambda root: "fix")
    monkeypatch.setattr(stack, "resolve", lambda *a, **k: pytest.fail("resolved"))
    assert stack.up() == 1
    assert "fy worktree add fix --box" in capsys.readouterr().err


def test_on_your_computer_the_boxs_marker_refuses_nothing(world, monkeypatch):
    # The shared `.git` is box-written: a marker planted in a registered worktree must not stop it.
    boxworktree.add("fix")
    monkeypatch.setattr(config, "in_box", lambda: False)
    assert not boxworktree.refuse_stack(world["main"], "fix", "fy up")


def test_promoting_it_makes_it_an_ordinary_worktree(world, monkeypatch, tmp_path):
    boxworktree.add("fix")
    wt = world["root"] / "fix"
    monkeypatch.setattr(config, "in_box", lambda: False)
    monkeypatch.setattr(config, "state_dir", lambda: tmp_path / "state")
    monkeypatch.setattr(worktree, "_no_pooled_port", lambda n: False)
    monkeypatch.setattr(worktree, "_claim_ports", lambda *a, **k: True)
    monkeypatch.setattr(worktree, "_say_unmasked", lambda *a, **k: None)
    monkeypatch.setattr(registry, "store_file", lambda m: tmp_path / "reg.json")
    assert worktree._register_existing(world["main"], "fix", wt, assume_yes=True) == 0
    assert not boxworktree.is_box_made(world["main"], wt)
    for d in DIRS:
        assert (wt / d).stat().st_mode & 0o777 == 0o755
    assert "fix" in registry.checkouts(world["main"])


def test_promoting_follows_no_link_the_box_planted(world, tmp_path):
    boxworktree.add("fix")
    wt = world["root"] / "fix"
    mine = tmp_path / "mine"
    mine.mkdir(mode=0o700)
    (wt / "web" / "node_modules").rmdir()
    (wt / "web" / "node_modules").symlink_to(mine)
    boxworktree.promoted(world["main"], wt)
    assert mine.stat().st_mode & 0o777 == 0o700


def test_a_git_file_pointing_elsewhere_is_no_box_made_worktree(world, tmp_path):
    evil = tmp_path / "evil"
    evil.mkdir()
    (evil / boxworktree.MARKER).write_text("x")
    d = world["root"] / "planted"
    d.mkdir()
    (d / ".git").write_text(f"gitdir: {evil}\n")
    assert not boxworktree.is_box_made(world["main"], d)
    (d / ".git").write_text(f"gitdir: {world['main']}/.git/worktrees/../../../evil\n")
    assert not boxworktree.is_box_made(world["main"], d)
    boxworktree.promoted(world["main"], d)
    assert (evil / boxworktree.MARKER).exists()


def test_another_dirs_git_file_cannot_borrow_a_box_made_worktrees_marker(world, tmp_path):
    boxworktree.add("fix")
    (tmp_path / "evil" / "fix").mkdir(parents=True)
    d = world["root"] / "planted"
    d.mkdir()
    (d / ".git").write_text(f"gitdir: {tmp_path}/evil/fix\n")
    assert not boxworktree.is_box_made(world["main"], d)


def test_your_computer_says_a_box_made_it_and_the_doctor_does_not_warn(
    world, monkeypatch, capsys, tmp_path
):
    boxworktree.add("fix")
    _git(world["main"], "worktree", "add", "-q", "-b", "o", str(world["root"] / "stray"))
    monkeypatch.setattr(config, "in_box", lambda: False)
    monkeypatch.setattr(registry, "store_file", lambda m: tmp_path / "reg.json")
    capsys.readouterr()
    worktree.list_()
    out = capsys.readouterr().out
    assert "(made in a box)" in next(ln for ln in out.splitlines() if " fix " in ln)
    assert "runs nothing for: stray." in out
    row = devmode._worktree_registry_check()
    assert "stray" in row[2] and "`fy worktree add fix`" not in row[2]
    _git(world["main"], "worktree", "remove", "--force", str(world["root"] / "stray"))
    row = devmode._worktree_registry_check()
    # Named, never just counted: a box decides what is marked, and the operator should see it.
    assert "made in a box (by its own word): fix" in row[2]
