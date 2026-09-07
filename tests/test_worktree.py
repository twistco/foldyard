"""worktree.py — `foldyard worktree add|list`. git + the machine are mocked, so nothing
touches a real repo; we assert the right git invocations and the config-driven init hook."""

from __future__ import annotations

import os
from typing import Any

import pytest

from foldyard import box, config, machine, stack, worktree


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


def test_add_seeds_keyless_posture_from_main(fake_main, monkeypatch):
    # A fresh worktree's posture defaults every axis to off, so its proxy wouldn't inject the
    # keyless Claude/Codex token and codex would 401→fail-to-refresh. `add` mirrors main's keyless
    # assistant posture into the new worktree so its box comes up injectable. Only the keyless
    # axes, only main's non-off values (codex=off here is NOT seeded).
    from foldyard import devmode

    monkeypatch.setattr(devmode, "worktree_config", lambda wt: {"wt": wt})
    monkeypatch.setattr(config, "claude_keyless", lambda: "oauth")
    monkeypatch.setattr(config, "codex_keyless", lambda: "chatgpt")
    monkeypatch.setattr(
        devmode, "read", lambda apply_expiry=True: {"mode": {"claude": "on", "codex": "off"}}
    )
    seeded: list[tuple] = []
    monkeypatch.setattr(
        devmode, "set_mode", lambda updates, **kw: seeded.append((updates, kw)) or {}
    )

    assert worktree.add("feat") == 0
    # codex=off in main is skipped; claude=on is seeded; never reconciles (nothing is up yet).
    assert seeded == [({"claude": "on"}, {"reconcile": False})]


def test_add_skips_seed_when_not_keyless(fake_main, monkeypatch):
    # No `[claude]`/`[codex]` keyless configured → nothing to inject, so no posture is written.
    from foldyard import devmode

    monkeypatch.setattr(config, "claude_keyless", lambda: "")
    monkeypatch.setattr(config, "codex_keyless", lambda: "")
    called: list = []
    monkeypatch.setattr(devmode, "set_mode", lambda *a, **k: called.append(1))

    assert worktree.add("feat") == 0
    assert not called


def test_list_prints_main_and_worktrees(fake_main, capsys):
    (fake_main["wt_root"] / "alpha").mkdir(parents=True)
    (fake_main["wt_root"] / "alpha" / ".git").write_text("")  # worktree marker
    assert worktree.list_() == 0
    out = capsys.readouterr().out
    assert "main" in out and "alpha" in out


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
            {"FOLDYARD_CHECKOUT": str(wt), "HERE": config.dev_vm_rel()},
            {"what": "worktree 'feat' transcripts"},
        )
    ]


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
    assert "run on the host" in capsys.readouterr().err


def test_remove_refuses_in_box(fake_main, monkeypatch, capsys):
    # Same guard for remove: an in-box removal prunes SHARED worktree metadata against a
    # box-local dir, orphaning the Mac-side checkout (the observed failure this guards).
    _make_wt(fake_main)
    monkeypatch.setattr(config, "in_box", lambda: True)
    assert worktree.remove("feat", force=True) == 1
    assert fake_main["teardown"] == []  # nothing torn down
    assert "run on the host" in capsys.readouterr().err


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
