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

from foldyard import box, config, machine, stack, worktree, worktree_registry


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
            return _Proc(0, "".join(f"worktree {p}\n" for p in sorted(state["registered"])))
        if g and "show-ref" in cmd:
            return _Proc(0 if cmd[-1] in state["refs"] else 1)
        if g and "symbolic-ref" in cmd:  # origin/HEAD default-branch pointer
            head = state["origin_head"]
            return _Proc(0, head + "\n") if head else _Proc(1)
        if g and "rev-parse" in cmd and "--verify" in cmd:  # base-branch resolution
            target = cmd[-1].removesuffix("^{commit}")
            return _Proc(0 if target in state["resolves"] else 1)
        if g and "rev-parse" in cmd:  # --abbrev-ref HEAD (current branch)
            return _Proc(0, "main")
        return _Proc(0)

    monkeypatch.setattr(worktree.subprocess, "run", fake_run)
    yield {
        "main": main,
        "wt_root": wt_root,
        "state_dir": state_dir,
        "calls": calls,
        "state": state,
        "teardown": teardown,
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
    assert argv[:5] == ["worktree", "add", "--no-track", "-b", "wt/feat"]
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
    assert argv[:5] == ["worktree", "add", "--no-track", "-b", "wt/feat"]
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
    assert argv[:5] == ["worktree", "add", "--no-track", "-b", "wt/feat"]
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
        if cmd[0] != "podman":
            return inner(cmd, **kw)
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
    assert "WORKTREE=feat fy box up" in out and "fy box down && fy box up" in out
    assert not [c for c in calls if c[1] == "inspect"]
    assert not [c for c in calls if c[1] in ("rm", "run", "stop")]  # never recreates a box


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
    monkeypatch.setattr(config, "box_image", lambda: {"tag": "proj:devbox"})
    monkeypatch.setattr(
        box, "engine_env", lambda: ("podman", {"DOCKER_HOST": "unix:///nonexistent"})
    )
    monkeypatch.setattr(box, "image_exists", lambda *a: image)


def test_add_runs_the_init_hook_in_a_container_not_on_the_host(fake_main, monkeypatch):
    # The init script is a REPO file whose only job is copying gitignored config between two
    # checkouts — both inside the mount — so it needs no host privileges, and running it on the host
    # made `worktree add` a host-code-execution trigger for whatever the checkout contained
    # (ADR-0023).
    _init_hook(monkeypatch, fake_main["main"])
    assert worktree.add("feat") == 0
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


def test_add_skips_the_hook_when_the_box_image_is_missing(fake_main, monkeypatch, capsys):
    # No image to run it in ⇒ SKIP with the command to run later. Deliberately NOT a host fallback:
    # "the yard wasn't ready" must never silently become "so we ran it on the Mac".
    _init_hook(monkeypatch, fake_main["main"], image=False)
    assert worktree.add("feat") == 0
    assert not any(c[:2] == ["podman", "run"] for c in fake_main["calls"])
    assert "fy worktree init feat" in capsys.readouterr().err


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
        if cmd[1:] == ["-m", "foldyard", "box", "up"]:
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
    assert runs[0]["cmd"] == [sys.executable, "-m", "foldyard", "box", "up"]
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
    assert _wt_remove_argv(fake_main["calls"]) == ["worktree", "remove", str(wt)]
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
    assert _wt_remove_argv(fake_main["calls"]) == ["worktree", "remove", str(wt)]


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


def test_add_refuses_in_box(fake_main, monkeypatch, capsys):
    # The box shares the real .git but sees a container-local worktrees root — an in-box
    # `git worktree add` would register shared metadata for a path the Mac side can't manage.
    monkeypatch.setattr(config, "in_box", lambda: True)
    assert worktree.add("feat") == 1
    assert not fake_main["calls"]  # bailed before shelling git
    assert "run on your computer" in capsys.readouterr().err


def test_remove_refuses_in_box(fake_main, monkeypatch, capsys):
    # Same guard for remove: an in-box removal prunes SHARED worktree metadata against a
    # box-local dir, orphaning the Mac-side checkout (the observed failure this guards).
    _make_wt(fake_main)
    monkeypatch.setattr(config, "in_box", lambda: True)
    assert worktree.remove("feat", force=True) == 1
    assert fake_main["teardown"] == []  # nothing torn down
    assert "run on your computer" in capsys.readouterr().err


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


# ── the uncommitted-work snapshot taken before a worktree is destroyed ────────────────
#
# These drive REAL git (no mock): the whole point is the plumbing — a throwaway index, and a
# ref in the SHARED store that outlives the checkout. A mocked git would assert nothing.


@pytest.fixture
def real_worktree(tmp_path):
    """A real main repo with a real linked worktree, and a helper to run git in either."""
    import subprocess

    main, wt = tmp_path / "main", tmp_path / "wt"

    def git(repo, *args, **kw):
        return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, **kw)

    subprocess.run(["git", "init", "-q", str(main)], check=True)
    git(main, "config", "user.email", "t@example.com")
    git(main, "config", "user.name", "t")
    (main / "f.txt").write_text("base\n")
    git(main, "add", "-A")
    git(main, "commit", "-qm", "base")
    git(main, "worktree", "add", "-q", str(wt), "-b", "feat")
    return {"main": main, "wt": wt, "git": git}


def test_snapshot_saves_tracked_and_untracked_work(real_worktree):
    wt, main, git = real_worktree["wt"], real_worktree["main"], real_worktree["git"]
    (wt / "f.txt").write_text("EDITED\n")
    (wt / "new.txt").write_text("BRAND NEW\n")

    ref = worktree._snapshot_uncommitted(wt, "feat")
    assert ref and ref.startswith("refs/fy/removed/feat-")
    assert git(main, "show", f"{ref}:f.txt").stdout == "EDITED\n"
    # The untracked file is the one `git stash create` would silently drop — and the one that
    # is unrecoverable, since nothing ever wrote it to the object database.
    assert git(main, "show", f"{ref}:new.txt").stdout == "BRAND NEW\n"


def test_snapshot_outlives_the_worktree_it_describes(real_worktree):
    wt, main, git = real_worktree["wt"], real_worktree["main"], real_worktree["git"]
    (wt / "new.txt").write_text("BRAND NEW\n")
    ref = worktree._snapshot_uncommitted(wt, "feat")

    # The destruction it exists for, plus the two sweeps that would collect an unreferenced
    # object. Linked worktrees share objects AND refs with main, so the ref keeps it reachable.
    git(main, "worktree", "remove", "--force", str(wt))
    git(main, "worktree", "prune")
    git(main, "gc", "-q", "--prune=now")
    assert not wt.exists()
    assert git(main, "show", f"{ref}:new.txt").stdout == "BRAND NEW\n"


def test_snapshot_does_not_disturb_what_it_is_snapshotting(real_worktree):
    # It runs on the way OUT of a checkout, so it must not be able to change what is removed:
    # the tree is built through a throwaway index, never the worktree's own.
    wt, git = real_worktree["wt"], real_worktree["git"]
    (wt / "f.txt").write_text("EDITED\n")
    (wt / "new.txt").write_text("BRAND NEW\n")
    before = (git(wt, "status", "--porcelain").stdout, git(wt, "rev-parse", "HEAD").stdout)

    worktree._snapshot_uncommitted(wt, "feat")

    assert (git(wt, "status", "--porcelain").stdout, git(wt, "rev-parse", "HEAD").stdout) == before
    assert (wt / "f.txt").read_text() == "EDITED\n"  # and the files themselves are untouched


def test_snapshot_is_silent_when_there_is_nothing_to_save(real_worktree):
    # A clean tree has nothing to lose — no ref, no noise on the way out.
    assert worktree._snapshot_uncommitted(real_worktree["wt"], "feat") is None


def test_snapshot_ignores_what_gitignore_ignores(real_worktree):
    # What keeps it cheap (no node_modules) is also its blind spot — pin the behaviour so the
    # tradeoff is a decision rather than a surprise.
    wt, main, git = real_worktree["wt"], real_worktree["main"], real_worktree["git"]
    (wt / ".gitignore").write_text("junk/\n")
    (wt / "junk").mkdir()
    (wt / "junk" / "big.bin").write_text("x" * 1000)
    ref = worktree._snapshot_uncommitted(wt, "feat")
    assert ref
    assert "junk/big.bin" not in git(main, "ls-tree", "-r", "--name-only", ref).stdout


def test_snapshot_returns_none_when_git_cannot_be_driven(tmp_path):
    # A pruned orphan's `.git` file dangles. That case genuinely cannot be rescued, so it must
    # report nothing rather than raise on the way out of a removal already under way.
    orphan = tmp_path / "not-a-repo"
    orphan.mkdir()
    assert worktree._snapshot_uncommitted(orphan, "feat") is None


def test_remove_snapshots_before_git_takes_the_checkout(fake_main, monkeypatch, capsys):
    # Ordering is the whole point: after `git worktree remove` there is nothing left to read.
    order: list[str] = []
    monkeypatch.setattr(
        worktree,
        "_snapshot_uncommitted",
        lambda wt_dir, name: order.append("snapshot") or "refs/fy/removed/feat-x",
    )
    real_run = worktree.subprocess.run

    def spy(cmd, **kw):
        if cmd[:1] == ["git"] and "worktree" in cmd and "remove" in cmd:
            order.append("git-remove")
        return real_run(cmd, **kw)

    monkeypatch.setattr(worktree.subprocess, "run", spy)
    _make_wt(fake_main)
    assert worktree.remove("feat", assume_yes=True) == 0
    assert order == ["snapshot", "git-remove"]
    assert "refs/fy/removed/feat-x" in capsys.readouterr().out  # and it says where it went


def test_remove_is_not_blocked_by_a_failed_snapshot(fake_main, monkeypatch):
    # Unlike the transcript archive above it, this must never refuse: both paths that still
    # carry work at this point are explicit --force, so the loss was already accepted. A net
    # under the operator, not a promise made to them.
    monkeypatch.setattr(worktree, "_snapshot_uncommitted", lambda wt_dir, name: None)
    _make_wt(fake_main)
    assert worktree.remove("feat", assume_yes=True) == 0
