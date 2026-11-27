"""Which project a directory belongs to is decided by your computer's records, and only by them.

The records: the main checkouts you adopted a config for (`configpin`), each one's registered
worktrees (`worktree_registry`, written only by `fy worktree add`), and each one's worktrees root
as its ADOPTED config declares it, when that root is a directory for its worktrees alone. Walking up
from where `fy` runs, the nearest record names the project and the checkout. Everything the box can
write (a `.git` file or directory, a nested repository, a `foldyard.toml`, a link, a directory) can
only make fy refuse there, never steer it: so a box can't make a verb on your computer act on a
project of its choosing, or name its state dir. A worktree is always in its main's project.

These drive the CLI's own front door (its callback) with `fy mode` stubbed, and read what config
says while it runs. Each attack here is one a review of the earlier attempt found.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import unicodedata
from pathlib import Path

import pytest
from typer.testing import CliRunner

from foldyard import cli, config, configpin, devmode, stack, worktree_registry

runner = CliRunner()

# Captured at import, before conftest's autouse seams replace them.
_REAL = {
    (module, name): getattr(module, name)
    for module, name in (
        (config, "_host_toml"),
        (config, "_linked_admin"),
        (configpin, "pin_dir"),
    )
    if hasattr(module, name)
}

MAIN_TOML = '[project]\nname = "acme"\nprefix = "ac"\n\n[machine]\nname = "acme-vm"\n'


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-c", "user.email=t@example.com", "-c", "user.name=t", "-C", str(repo), *args],
        capture_output=True,
        text=True,
    )


def _repo(path: Path, toml: str | None = None) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", "-b", "main", str(path)], check=True)
    if toml is not None:
        (path / "foldyard.toml").write_text(toml)
    assert _git(path, "commit", "-q", "--allow-empty", "-m", "base").returncode == 0
    return path


def _adopt(root: Path) -> None:
    configpin.adopt(config.resolve(worktree="", repo=root))
    _clear()


def _adopt_before_the_rule(root: Path) -> None:
    """Adopt ``root`` as a main as fy did before a project could no longer lie inside another's
    (config.nesting_conflict): how a pre-existing layout is set up."""
    with pytest.MonkeyPatch.context() as before:
        before.setattr(config, "nesting_conflict", lambda *a, **k: None)
        _adopt(root)


def _clear() -> None:
    config.clear_caches()
    for cached in (stack.main_repo, stack.main_checkout_of, devmode._main_checkout):
        cached.cache_clear()


@pytest.fixture
def yard(tmp_path, monkeypatch):
    """acme: a main checkout, adopted, with `feat` registered in its worktrees root (the default,
    `acme-worktrees` beside it). Your computer's real records read (conftest points them at the
    tree, and reads no `.git` link, for the rest of the suite)."""
    for (module, name), real in _REAL.items():
        monkeypatch.setattr(module, name, real)
    monkeypatch.setattr(config, "in_box", lambda: False)
    for name in (
        "FOLDYARD_STATE_DIR",
        "FOLDYARD_REPO",
        "FOLDYARD_WORKTREES_ROOT",
        "FOLDYARD_PROJECT",
        "FOLDYARD_PROJECT_PREFIX",
        "PODMAN_MACHINE",
        "MACHINE_BACKEND",
        "WORKTREE",
        "PWD",
    ):
        # Set, then deleted: so whatever the CLI's callback exports while a test runs (WORKTREE,
        # and FOLDYARD_REPO for `fy box exec`) is undone after it, never left for the next test.
        monkeypatch.setenv(name, "")
        monkeypatch.delenv(name)
    base = Path(os.path.realpath(tmp_path))
    main = _repo(base / "acme", MAIN_TOML)
    _adopt(main)
    feat = base / "acme-worktrees" / "feat"
    assert _git(main, "worktree", "add", "-q", "-b", "feat", str(feat)).returncode == 0
    (feat / "foldyard.toml").write_text(MAIN_TOML)
    worktree_registry.register(main, "feat", feat)
    _clear()
    yield {"base": base, "main": main, "feat": feat, "root": base / "acme-worktrees"}
    _clear()


def _at(path: Path, monkeypatch, *, worktree: str | None = None, pwd: str | None = None):
    """``fy mode`` run in ``path``: ``(exit code, output, what config said while it ran)``."""
    monkeypatch.chdir(path)
    if worktree is not None:
        monkeypatch.setenv("WORKTREE", worktree)
    else:
        monkeypatch.delenv("WORKTREE", raising=False)
    if pwd is not None:
        monkeypatch.setenv("PWD", pwd)
    _clear()
    seen: dict = {}

    def spy(argv):
        seen["project"] = config.project()
        seen["root"] = config.repo_root()
        seen["worktree"] = os.environ.get("WORKTREE", "")
        try:
            seen["main"] = devmode.main_repo()
        except SystemExit:
            seen["main"] = None
        return 0

    monkeypatch.setattr(devmode, "main", spy)
    result = runner.invoke(cli.app, ["mode"])
    return result.exit_code, result.output, seen


def _refused(path: Path, monkeypatch, *needles: str, **kw) -> str:
    code, out, seen = _at(path, monkeypatch, **kw)
    assert code == 1 and not seen, (code, out, seen)
    for needle in needles:
        assert needle in " ".join(out.split()), out
    return out


# ── what keeps working ────────────────────────────────────────────────────────────────────────


def test_main_and_its_subdirectories_are_main(yard, monkeypatch):
    sub = yard["main"] / "src" / "deep"
    sub.mkdir(parents=True)
    for where in (yard["main"], sub):
        code, out, seen = _at(where, monkeypatch)
        assert code == 0, out
        assert seen == {
            "project": "acme",
            "root": yard["main"],
            "worktree": "",
            "main": yard["main"],
        }


def test_a_registered_worktree_is_in_mains_project(yard, monkeypatch):
    code, out, seen = _at(yard["feat"] / ".", monkeypatch)
    assert code == 0, out
    assert seen == {
        "project": "acme",
        "root": yard["feat"],
        "worktree": "feat",
        "main": yard["main"],
    }


def test_a_worktrees_own_identity_is_never_read(yard, monkeypatch, capsys):
    # A branch that renames the project: adopted on the worktree, still main's project, and said.
    (yard["feat"] / "foldyard.toml").write_text('[project]\nname = "evil"\n[machine]\nname = "x"\n')
    configpin.adopt(config.resolve(worktree="feat", repo=yard["feat"]))
    _clear()
    code, out, seen = _at(yard["feat"], monkeypatch)
    assert code == 0, out
    assert seen["project"] == "acme"
    cfg = devmode.worktree_config("feat")
    assert cfg.toml["project"]["name"] == "evil"  # adopted…
    assert (cfg.project(), cfg.machine_name()) == ("acme", "acme-vm")  # …and not read
    note = configpin.identity_note(cfg.toml, cfg.identity)
    assert "[project].name = 'evil'" in note and "(acme)" in note
    monkeypatch.setenv("WORKTREE", "feat")
    status, _, said = devmode._config_pin_check()
    assert status == "warn" and "takes effect once main adopts it" in said


def test_the_box_names_the_project_as_your_computer_baked_it(yard, monkeypatch):
    # An unadopted rename in main's tree: the box names its stacks as your computer does.
    monkeypatch.setattr(config, "in_box", lambda: True)
    (yard["main"] / "foldyard.toml").write_text('[project]\nname = "renamed"\n')
    monkeypatch.setenv("FOLDYARD_REPO", str(yard["main"]))
    monkeypatch.setenv(config.BAKED_IDENTITY_ENV, '{"project": {"name": "acme"}}')
    _clear()
    assert config.project() == "acme"
    assert devmode.worktree_config("").project() == "acme"
    monkeypatch.delenv(config.BAKED_IDENTITY_ENV)  # a box from before it: its tree, as ever
    _clear()
    assert config.project() == "renamed"


def test_a_worktree_registered_outside_the_root_is_found_by_the_registry(yard, monkeypatch):
    old = yard["base"] / "old-place" / "hotfix"
    assert _git(yard["main"], "worktree", "add", "-q", "-b", "hotfix", str(old)).returncode == 0
    worktree_registry.register(yard["main"], "hotfix", old)
    code, out, seen = _at(old, monkeypatch)
    assert code == 0, out
    assert (seen["project"], seen["worktree"], seen["main"]) == ("acme", "hotfix", yard["main"])


def test_an_unregistered_dir_in_the_root_is_named_for_main_and_reads_nothing(yard, monkeypatch):
    stray = yard["root"] / "stray"
    stray.mkdir()
    (stray / ".git").write_text(f"gitdir: {yard['main']}/.git/worktrees/stray\n")
    (stray / "foldyard.toml").write_text('[project]\nname = "evil"\n')
    code, out, seen = _at(stray, monkeypatch)
    assert code == 0, out
    assert (seen["project"], seen["worktree"], seen["main"]) == ("acme", "stray", yard["main"])
    assert config.current().toml == {}


@pytest.mark.parametrize("argv", [["--help"], ["--version"], ["mode", "--help"], ["ls"]])
def test_help_version_and_project_free_verbs_run_where_fy_refuses(yard, monkeypatch, argv):
    plain = yard["base"] / "plain"
    assert _git(yard["main"], "worktree", "add", "-q", "-b", "plain", str(plain)).returncode == 0
    monkeypatch.chdir(plain)
    _clear()
    result = runner.invoke(cli.app, argv)
    assert result.exit_code == 0, result.output
    assert "made outside fy" not in result.output


@pytest.mark.parametrize("checkout", ["main", "feat"])
def test_a_submodule_is_part_of_its_checkout(yard, monkeypatch, checkout):
    # Its `.git` is a file naming a git dir in main's `.git` (`modules/`, or a worktree's): its files
    # are the checkout's, and it names no other project. Refused, fy would refuse there for good.
    top = yard[checkout]
    admin = ".git/modules/lib" if checkout == "main" else ".git/worktrees/feat/modules/lib"
    lib = top / "vendor" / "lib"
    lib.mkdir(parents=True)
    (lib / ".git").write_text(f"gitdir: {yard['main']}/{admin}\n")
    code, out, seen = _at(lib, monkeypatch)
    assert code == 0, out
    assert (seen["project"], seen["root"], seen["main"]) == ("acme", top, yard["main"])
    # One naming a git dir anywhere else is a repository of its own there, as ever.
    (lib / ".git").write_text(f"gitdir: {yard['base']}/elsewhere/.git/modules/lib\n")
    _refused(lib, monkeypatch, f"{lib} is inside {top}")


def test_a_project_adopted_in_a_subdirectory_of_its_repository(yard, monkeypatch):
    mono = _repo(yard["base"] / "mono")
    svc = mono / "svc"
    svc.mkdir()
    (svc / "foldyard.toml").write_text('[project]\nname = "svc"\n')
    _adopt(svc)
    (svc / "src").mkdir()
    code, out, seen = _at(svc / "src", monkeypatch)
    assert code == 0, out
    assert (seen["project"], seen["root"], seen["worktree"]) == ("svc", svc, "")


# ── no fy project inside another's checkout ───────────────────────────────────────────────
# The outer box can write the inner project's files, and the inner box writes what the outer one
# reads: two walls bridged. A project in a subdirectory of a repository that is no fy project
# (`mono/svc`, above) is fine.

NESTED = "fy doesn't support a project inside another one's checkout"


@pytest.mark.parametrize("where", ["main", "feat"])
def test_a_project_adopted_inside_another_before_is_skipped_and_refused(yard, monkeypatch, where):
    tools = yard[where] / "tools" / "lint"
    tools.mkdir(parents=True)
    (tools / "foldyard.toml").write_text('[project]\nname = "lint"\n')
    with pytest.MonkeyPatch.context() as before:  # adopted before the rule
        before.setattr(config, "nesting_conflict", lambda *a, **k: None)
        _adopt(tools)
    assert any(str(tools) in p and NESTED in p for p in config.record_problems())
    (tools / "foldyard.toml").unlink()  # the box can delete it: still refused there
    _refused(tools, monkeypatch, NESTED, "Move it out")
    code, out, seen = _at(yard[where], monkeypatch)
    assert code == 0 and seen["project"] == "acme", out


@pytest.mark.parametrize("how", ["dotdot", "pruned", "moved"])
def test_a_fresh_project_beside_a_main_is_a_project_of_its_own(yard, monkeypatch, how):
    # Beside a main whose worktrees root holds it (`..`), or one whose adoption was pruned, or moved:
    # no record claims it, so it's a new project, adoptable as ever.
    if how == "dotdot":
        (yard["main"] / "foldyard.toml").write_text(MAIN_TOML + 'worktrees_root = ".."\n')
        _adopt(yard["main"])
    fresh = _repo(yard["base"] / "fresh", '[project]\nname = "fresh"\n')
    if how == "pruned":
        configpin.forget(config.resolve(worktree="", repo=fresh))
    if how == "moved":
        _adopt(fresh)
        (fresh / "foldyard.toml").write_text("")
        fresh = fresh.rename(yard["base"] / "moved")
    code, out, seen = _at(fresh, monkeypatch)
    assert code == 0, out
    # It reads nothing until adopted: named after its directory, as a fresh project always was.
    assert (seen["project"], seen["root"], seen["worktree"]) == (fresh.name, fresh, "")


def test_init_works_in_a_fresh_directory(yard, monkeypatch):
    fresh = yard["base"] / "brand-new"
    fresh.mkdir()
    monkeypatch.chdir(fresh)
    _clear()
    result = runner.invoke(cli.app, ["init", "--name", "brand-new"])
    assert result.exit_code == 0, result.output
    assert (fresh / "foldyard.toml").is_file()


# ── what the box can write refuses, and never steers ─────────────────────────────────────────


def test_a_worktree_made_outside_fy_is_refused(yard, monkeypatch):
    plain = yard["base"] / "plain"
    assert _git(yard["main"], "worktree", "add", "-q", "-b", "plain", str(plain)).returncode == 0
    (plain / "foldyard.toml").write_text(MAIN_TOML)
    _refused(
        plain,
        monkeypatch,
        f"{plain} is a worktree made outside fy (plain `git worktree add`, or Claude Code's own), "
        "which fy doesn't act in. Run fy from your main checkout:",
        str(yard["main"]),
    )


def test_claude_codes_worktree_inside_main_is_refused(yard, monkeypatch):
    wt = yard["main"] / ".claude" / "worktrees" / "agent"
    assert _git(yard["main"], "worktree", "add", "-q", "-b", "agent", str(wt)).returncode == 0
    (wt / "foldyard.toml").write_text(MAIN_TOML)
    _refused(wt, monkeypatch, f"{wt} is a worktree made outside fy", str(yard["main"]))


def test_worktree_env_only_picks_a_registered_worktree_and_never_lifts_a_refusal(yard, monkeypatch):
    # Attack 5: `WORKTREE=feat fy mode` from a plain worktree.
    plain = yard["base"] / "plain"
    assert _git(yard["main"], "worktree", "add", "-q", "-b", "plain", str(plain)).returncode == 0
    (plain / "foldyard.toml").write_text(MAIN_TOML)
    _refused(plain, monkeypatch, "made outside fy", worktree="feat")
    code, out, seen = _at(yard["main"], monkeypatch, worktree="feat")  # from main it picks feat
    assert code == 0 and seen["worktree"] == "feat", out


# ── `fy box exec`, a git hook's dispatch, from a worktree made inside main ───────────────────
# A commit in Claude Code's `.claude/worktrees/<x>` runs the project's hooks, which call
# `fy box exec --skip-if-down …` with `FOLDYARD_REPO` set to the worktree's top. Refused, the
# commit fails or silently skips its checks. Main's box already sees that path (it is inside main's
# checkout), and exec names nothing from the worktree's config: it runs there, as from main.


def _box_exec(path: Path, monkeypatch, *argv: str, **env: str):
    """``fy box exec <argv>`` run in ``path`` with ``env``: ``(exit code, output, what it saw)``."""
    from foldyard import box

    monkeypatch.chdir(path)
    for name in ("WORKTREE", "FOLDYARD_REPO"):  # undone after the test, whatever the CLI exports
        monkeypatch.setenv(name, "")
        monkeypatch.delenv(name)
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    _clear()
    seen: dict = {}

    def spy(cmd, args=None):
        seen.update(
            cmd=cmd,
            args=args,
            project=config.project(),
            root=config.repo_root(),
            worktree=config.active_worktree(),
            stack_main=stack.main_repo(),
            main=devmode.main_repo(),
            cwd=Path(os.getcwd()),
            toml=config.current().toml.get("project"),
        )
        return 0

    monkeypatch.setattr(box, "main", spy)
    result = runner.invoke(cli.app, ["box", "exec", *argv])
    return result.exit_code, result.output, seen


def _inside_main(yard) -> Path:
    wt = yard["main"] / ".claude" / "worktrees" / "agent"
    assert _git(yard["main"], "worktree", "add", "-q", "-b", "agent", str(wt)).returncode == 0
    (wt / "foldyard.toml").write_text('[project]\nname = "evil"\n[machine]\nname = "x"\n')
    return wt


@pytest.mark.parametrize("hook", [False, True])
@pytest.mark.parametrize("worktree", ["", "feat"])
def test_box_exec_from_a_worktree_made_inside_main_runs_in_mains_box(
    yard, monkeypatch, hook, worktree
):
    wt = _inside_main(yard)
    (wt / "src").mkdir()
    env = {"FOLDYARD_REPO": str(wt)} if hook else {}  # a hook sets it to the worktree's top
    if worktree:
        env["WORKTREE"] = worktree
    code, out, seen = _box_exec(wt / "src", monkeypatch, "--skip-if-down", "just lint", **env)
    assert code == 0, out
    assert seen == {
        "cmd": "exec",
        "args": ["--skip-if-down", "just lint"],
        "project": "acme",
        "root": yard["main"],
        "worktree": "",  # main's box: the one that sees this path
        "stack_main": yard["main"],
        "main": yard["main"],
        "cwd": wt / "src",  # the same path, in main's box
        "toml": {"name": "acme", "prefix": "ac"},  # main's adopted config, never the worktree's
    }


def test_every_other_verb_still_refuses_there_whatever_foldyard_repo_says(yard, monkeypatch):
    wt = _inside_main(yard)
    monkeypatch.setenv("FOLDYARD_REPO", str(wt))
    _refused(wt, monkeypatch, f"{wt} is a worktree made outside fy", str(yard["main"]))
    for argv in (["box", "shell"], ["config", "adopt"]):
        result = runner.invoke(cli.app, argv)
        assert result.exit_code == 1 and "made outside fy" in result.output, result.output


@pytest.mark.parametrize("where", ["beside-main", "in-a-registered-worktree", "deep-in-the-root"])
def test_box_exec_from_a_worktree_main_doesnt_hold_is_refused(yard, monkeypatch, where):
    # Beside main (`../x`), main's box doesn't see it; inside a registered worktree or deeper in
    # the root, no main checkout holds it by the records. Only main's tree counts.
    wt = {
        "beside-main": yard["base"] / "x",
        "in-a-registered-worktree": yard["feat"] / ".claude" / "worktrees" / "y",
        "deep-in-the-root": yard["root"] / "a" / "z",
    }[where]
    assert _git(yard["main"], "worktree", "add", "-q", "-b", where, str(wt)).returncode == 0
    for env in ({}, {"FOLDYARD_REPO": str(wt)}):
        code, out, seen = _box_exec(wt, monkeypatch, "--skip-if-down", "true", **env)
        assert code == 1 and not seen, (code, out, seen)
        assert "made outside fy" in out, out


def test_box_exec_never_follows_what_the_worktrees_git_says(yard, monkeypatch):
    # Inside main by the records, whatever its `.git` names: main's box, never the other project's.
    beta = _repo(yard["base"] / "beta", '[project]\nname = "beta"\n')
    _adopt(beta)
    wt = yard["main"] / ".claude" / "worktrees" / "b"
    assert _git(beta, "worktree", "add", "-q", "-b", "b", str(wt)).returncode == 0
    code, out, seen = _box_exec(wt, monkeypatch, "true")
    assert code == 0, out
    assert (seen["project"], seen["stack_main"]) == ("acme", yard["main"])


@pytest.mark.parametrize("how", ["commondir", "init"])
def test_a_registered_worktree_whose_git_became_a_directory_is_refused(yard, monkeypatch, how):
    # Attack 1: git would take it for main's worktree (commondir) or a repository of its own.
    git = yard["feat"] / ".git"
    git.unlink()
    if how == "commondir":
        git.mkdir()
        (git / "HEAD").write_text("ref: refs/heads/feat\n")
        (git / "commondir").write_text(f"{yard['main']}/.git\n")
    else:
        subprocess.run(["git", "init", "-q", str(yard["feat"])], check=True)
    _refused(
        yard["feat"],
        monkeypatch,
        f"{yard['feat']} is a worktree of {yard['main']} by your computer's records",
        "git worktree repair",
    )


@pytest.mark.parametrize("how", ["deleted", "empty-file", "empty-dir"])
def test_a_registered_worktree_whose_git_is_gone_or_empty_is_refused(yard, monkeypatch, how):
    # Attack 3.
    git = yard["feat"] / ".git"
    git.unlink()
    if how == "empty-file":
        git.write_text("")
    if how == "empty-dir":
        git.mkdir()
    _refused(yard["feat"], monkeypatch, "by your computer's records", "git worktree repair")


@pytest.mark.parametrize("where", ["root/a/victim", "feat/sub", "outside/sub", "main/sub"])
def test_a_git_init_inside_a_recorded_checkout_never_makes_a_project(yard, monkeypatch, where):
    # Attack 2 (the root itself is the next test): a repository the box made deeper in the root,
    # inside a registered worktree (in the root, or one registered outside it) or inside main.
    if where.startswith("outside"):
        old = yard["base"] / "outside"
        assert _git(yard["main"], "worktree", "add", "-q", "-b", "o", str(old)).returncode == 0
        worktree_registry.register(yard["main"], "outside", old)
    top, _, rest = where.partition("/")
    base = {"root": yard["root"], "feat": yard["feat"], "main": yard["main"]}.get(
        top, yard["base"] / top
    )
    nested = _repo(base / rest, '[project]\nname = "victim"\n')
    _refused(nested, monkeypatch, str(nested))
    assert config.project() != "victim"


def test_the_worktrees_root_itself_is_never_a_checkout(yard, monkeypatch):
    # Attack 8: the box `git init`s the root and writes a foldyard.toml naming the project.
    _repo(yard["root"], MAIN_TOML)
    _refused(yard["root"], monkeypatch, f"{yard['root']} is {yard['main']}'s worktrees root")
    monkeypatch.chdir(yard["root"])
    _clear()
    result = runner.invoke(cli.app, ["config", "adopt"])
    assert result.exit_code == 1, result.output
    assert yard["root"] not in configpin.adopted_mains()


@pytest.mark.parametrize("shared", ["same-root", "dotdot"])
def test_a_root_two_projects_share_never_follows_git(yard, monkeypatch, shared):
    # Attack 4: a `.git` line the box wrote names the other project.
    beta = _repo(yard["base"] / "beta", '[project]\nname = "beta"\n')
    if shared == "same-root":
        root = yard["base"] / "shared"
        for main in (yard["main"], beta):
            (main / "foldyard.toml").write_text(
                f'[project]\nname = "{main.name}"\n[machine]\nworktrees_root = "{root}"\n'
            )
        _adopt(yard["main"])
        with pytest.raises(configpin.NestedProject, match="the worktrees root"):
            _adopt(beta)  # a root two projects share is refused now…
        _adopt_before_the_rule(beta)  # …and, from before, neither claims it nor mounts it
        for main in (yard["main"], beta):
            assert config.mount_conflict(main, root) is not None
    else:
        (beta / "foldyard.toml").write_text(
            '[project]\nname = "beta"\n[machine]\nworktrees_root = ".."\n'
        )
        _adopt(beta)
        root = yard["base"]
    stray = root / "x"
    stray.mkdir(parents=True)
    (stray / ".git").write_text(f"gitdir: {beta}/.git/worktrees/x\n")
    (stray / "foldyard.toml").write_text('[project]\nname = "beta"\n')
    # No root claims it, so it's no one's worktree, whatever its `.git` names.
    _refused(stray, monkeypatch, str(stray), "outside fy")


@pytest.mark.parametrize("claims", ["main", "worktree"])
def test_a_root_declared_on_another_projects_checkout_claims_none_of_it(yard, monkeypatch, claims):
    # beta's adopted `worktrees_root` names acme's main (or its registered worktree): what acme's
    # own records say of that directory wins, so one project's config never stops fy in another.
    target = yard["main"] if claims == "main" else yard["feat"]
    beta = _repo(
        yard["base"] / "beta",
        f'[project]\nname = "beta"\n[machine]\nworktrees_root = "{target}"\n',
    )
    with pytest.raises(configpin.NestedProject, match="the worktrees root"):
        _adopt(beta)  # refused now…
    _adopt_before_the_rule(beta)  # …and one adopted before claims nothing, said in both doctors
    (target / "src").mkdir()
    for where in (target, target / "src"):
        code, out, seen = _at(where, monkeypatch)
        assert code == 0, out
        assert (seen["project"], seen["root"]) == ("acme", target)
    for main in (beta, yard["main"]):
        assert any(f"worktrees root {target}" in p for p in config.record_problems(main))


def test_a_root_declared_in_another_case_still_claims(yard, monkeypatch):
    # Attack 6: `worktrees_root = ".../CAPS-WT"` with `caps-wt` on disk.
    real = yard["base"] / "caps-wt"
    real.mkdir()
    if not (yard["base"] / "CAPS-WT").exists():
        pytest.skip("this filesystem is case-sensitive")
    (yard["main"] / "foldyard.toml").write_text(
        MAIN_TOML + f'worktrees_root = "{yard["base"]}/CAPS-WT"\n'
    )
    _adopt(yard["main"])
    stray = real / "stray"
    stray.mkdir()
    (stray / ".git").write_text(f"gitdir: {yard['main']}/.git/worktrees/stray\n")
    (stray / "foldyard.toml").write_text('[project]\nname = "evil"\n')
    code, out, seen = _at(stray, monkeypatch)
    assert code == 0, out
    assert (seen["project"], seen["worktree"]) == ("acme", "stray")


def test_a_foldyard_toml_planted_in_main_names_nothing(yard, monkeypatch, tmp_path):
    # Attack 7: `acme/other/foldyard.toml` made `fy allow add` write ~/.foldyard/other/.
    other = yard["main"] / "other"
    other.mkdir()
    (other / "foldyard.toml").write_text('[project]\nname = "other"\n')
    _refused(other, monkeypatch, f"{other} is inside {yard['main']}", NESTED)
    assert config.project() == "acme"
    result = runner.invoke(cli.app, ["allow", "add", "example.com"])
    assert result.exit_code == 1, result.output
    assert not (Path.home() / ".foldyard" / "other").exists()


@pytest.mark.parametrize("where", ["main", "feat"])
def test_a_project_inside_a_checkout_is_never_adopted(yard, monkeypatch, where):
    svc = _repo(yard[where] / "svc", '[project]\nname = "svc"\n')
    _refused(svc, monkeypatch, NESTED, "Move it out")
    for argv in (["config", "adopt"], ["config", "diff"]):
        result = runner.invoke(cli.app, argv)
        assert result.exit_code == 1 and NESTED in " ".join(result.output.split()), result.output
    with pytest.raises(configpin.Unparseable, match=NESTED):  # nor through any other door
        configpin.adopt(config.resolve(worktree="", repo=svc))
    assert svc not in configpin.adopted_mains()


def test_init_refuses_inside_a_checkout(yard, monkeypatch):
    fresh = yard["main"] / "tools" / "new"
    fresh.mkdir(parents=True)
    monkeypatch.chdir(fresh)
    _clear()
    result = runner.invoke(cli.app, ["init", "--name", "new"])
    assert result.exit_code == 1 and NESTED in " ".join(result.output.split()), result.output
    assert not (fresh / "foldyard.toml").exists()


def test_a_project_holding_an_adopted_one_is_never_adopted(yard, monkeypatch):
    # The outer one, adopted after: the same bridge from the other side.
    outer = yard["base"]
    (outer / "foldyard.toml").write_text('[project]\nname = "outer"\n')
    with pytest.raises(configpin.Unparseable, match="holds"):
        configpin.adopt(config.resolve(worktree="", repo=outer))
    monkeypatch.chdir(outer)
    _clear()
    result = runner.invoke(cli.app, ["init", "--name", "outer", "--force"])
    assert result.exit_code == 1 and "holds" in result.output, result.output
    _adopt(yard["main"])  # re-adopting the one that's there is no new nesting


def test_a_link_inside_main_as_the_root_claims_nothing_elsewhere(yard, monkeypatch):
    # Attack 9: a relative root inside main is the box's to swap for a link.
    (yard["main"] / "foldyard.toml").write_text(MAIN_TOML + 'worktrees_root = ".trees"\n')
    _adopt(yard["main"])
    elsewhere = yard["base"] / "elsewhere"
    other = _repo(elsewhere / "other", '[project]\nname = "other"\n')
    (yard["main"] / ".trees").symlink_to(elsewhere)
    code, out, seen = _at(other, monkeypatch)
    assert code == 0, out
    assert (seen["project"], seen["root"], seen["worktree"]) == ("other", other, "")


def test_a_path_through_a_link_in_the_root_is_refused(yard, monkeypatch):
    # Attack 10: you think you're in acme's worktree; fy would act on beta.
    beta = _repo(yard["base"] / "beta", '[project]\nname = "beta"\n')
    _adopt(beta)
    (yard["root"] / "link").symlink_to(beta)
    logical = f"{yard['root']}/link"
    _refused(beta, monkeypatch, "through a link", str(beta), pwd=logical)
    # From beta's real path, beta it is; and a link that leads where the path says is fine.
    code, out, seen = _at(beta, monkeypatch, pwd=str(beta))
    assert code == 0 and seen["project"] == "beta", out
    (yard["base"] / "to-acme").symlink_to(yard["main"])
    code, out, seen = _at(yard["main"], monkeypatch, pwd=f"{yard['base']}/to-acme")
    assert code == 0 and seen["project"] == "acme", out


def test_a_bad_record_in_another_project_is_skipped_and_reported(yard, monkeypatch):
    # Attack 11: `~nosuchuser` crashed fy in every other project.
    bad = _repo(
        yard["base"] / "bad",
        '[project]\nname = "bad"\n[machine]\nworktrees_root = "~nosuchuser-fy/x"\n',
    )
    _adopt(bad)
    code, out, seen = _at(yard["feat"], monkeypatch)
    assert code == 0 and seen["project"] == "acme", out
    assert any("~nosuchuser-fy/x" in p for p in config.record_problems(bad))  # its own doctor's
    assert config.record_problems(yard["main"]) == ()  # …not every project's


def test_the_box_is_unchanged(yard, monkeypatch):
    monkeypatch.setattr(config, "in_box", lambda: True)
    monkeypatch.chdir(yard["feat"])
    _clear()
    assert config.placement(yard["feat"]) is None


def test_lookups_are_cheap_with_many_projects(yard, monkeypatch):
    # Every fy start reads the records once (cold), and the TUI looks up again (warm): 45 projects
    # with 3 worktrees each, one with a root of its own declared. Counted in what the disk is
    # asked, not in time, which a loaded runner can't keep: about 11 ms and 0.7 ms alone.
    for i in range(45):
        main = yard["base"] / f"p{i}"
        main.mkdir()
        root = '[machine]\nworktrees_root = "../trees-p0"\n' if i == 0 else ""
        (main / "foldyard.toml").write_text(f'[project]\nname = "p{i}"\n{root}')
        _adopt(main)
        for j in range(3):
            wt = yard["base"] / (f"trees-p0/w{j}" if i == 0 else f"p{i}-worktrees/w{j}")
            wt.mkdir(parents=True)
            (wt / ".git").write_text("gitdir: x\n")
            worktree_registry.register(main, f"w{j}", wt)
    records = 45 * 5  # a main, three worktrees and a root each
    asked: list = []
    for name in ("stat", "lstat"):
        real = getattr(os, name)
        monkeypatch.setattr(
            os, name, lambda p, *a, _real=real, **k: asked.append(p) or _real(p, *a, **k)
        )
    config.clear_caches()
    config.placement(yard["base"] / "p0")
    cold = len(asked)
    stray = config.placement(yard["base"] / "trees-p0" / "stray")
    assert stray is not None and stray.kind == "stray"  # its root read
    paths = [yard["base"] / f"p{i}" for i in range(45)]
    asked.clear()
    for path in paths:
        placed = config.placement(path)
        assert placed is not None and placed.main == path
    warm = len(asked) / len(paths)
    assert cold < 8 * records and warm < 2 * records, (cold, warm)


@pytest.mark.parametrize(
    "argv", [["--help"], ["mode", "--help"], ["box", "--help"], ["docs", "--help"], ["docs"]]
)
def test_help_and_project_free_verbs_read_no_records(yard, monkeypatch, argv):
    # Help names nothing about a project, and a project-free verb acts on none: neither pays for
    # reading every adopted project's records.
    monkeypatch.chdir(yard["feat"])
    _clear()
    read: list = []
    real = config._records
    monkeypatch.setattr(config, "_records", lambda: read.append(1) or real())
    result = runner.invoke(cli.app, argv)
    assert result.exit_code == 0, result.output
    assert read == []


# ── what the box can swap, rename or plant around a record ──────────────────────────────────


def test_a_link_swapped_in_above_a_registered_worktree_claims_nothing(yard, monkeypatch):
    # A root inside main (`.trees`) is the box's to swap for a link: the worktree registered
    # there must not then match another project's directory of the same name.
    main = yard["main"]
    (main / "foldyard.toml").write_text(MAIN_TOML + 'worktrees_root = ".trees"\n')
    _adopt(main)
    inner = main / ".trees" / "web"
    assert _git(main, "worktree", "add", "-q", "-b", "web", str(inner)).returncode == 0
    worktree_registry.register(main, "web", inner)
    other = _repo(yard["base"] / "other", '[project]\nname = "other"\n')
    _adopt(other)
    (other / "web").mkdir()
    _clear()
    placed = config.placement(inner)  # read before the swap, as a long-lived TUI has them
    assert placed is not None and placed.kind == "worktree"
    (main / ".trees").rename(main / ".trees-old")
    (main / ".trees").symlink_to(other)
    placed = config.placement(other / "web")
    assert placed is not None and placed.main == other
    code, out, seen = _at(other / "web", monkeypatch)
    assert code == 0, out
    assert (seen["project"], seen["root"]) == ("other", other)
    assert any(str(inner) in problem for problem in config.record_problems())


@pytest.mark.parametrize("cached", [False, True])
def test_a_link_swapped_in_above_a_main_claims_nothing(yard, monkeypatch, cached):
    # A main adopted at apps/web, in a directory that is no project's: `apps` becomes a link to
    # another project, whose `web` must not then read as the first one. Read cold, or with the
    # records already held (a long-lived TUI's).
    web = _repo(yard["base"] / "apps" / "web", '[project]\nname = "web"\n')
    _adopt(web)
    other = _repo(yard["base"] / "other", '[project]\nname = "other"\n')
    _adopt(other)
    (other / "web").mkdir()
    _clear()
    placed = config.placement(web)
    assert placed is not None and placed.main == web
    (yard["base"] / "apps").rename(yard["base"] / "apps-old")
    (yard["base"] / "apps").symlink_to(other)
    if not cached:
        _clear()
    placed = config.placement(other / "web")
    assert placed is not None and (placed.main, placed.checkout) == (other, other)
    assert any(f"{web} (the main checkout)" in p for p in config.record_problems(web))


def test_a_checkout_spelled_in_another_case_reads_its_own_adoption(yard, monkeypatch):
    shouted = yard["base"] / "ACME"
    if not shouted.exists():
        pytest.skip("this filesystem is case-sensitive")
    monkeypatch.setenv("FOLDYARD_REPO", str(shouted))  # a cwd is spelled as the disk has it
    code, out, seen = _at(yard["main"], monkeypatch)
    assert code == 0, out
    assert seen["root"] == yard["main"]  # the record's path: its adoption is keyed by it
    assert config.machine_name() == "acme-vm"


def test_a_root_inside_main_is_walked_without_following_a_link(yard, monkeypatch):
    # Checked part by part, never "is it a link?" and then a lookup that follows one.
    (yard["main"] / "foldyard.toml").write_text(MAIN_TOML + 'worktrees_root = ".trees"\n')
    _adopt(yard["main"])
    elsewhere = yard["base"] / "elsewhere"
    (elsewhere / "proj").mkdir(parents=True)
    (yard["main"] / ".trees").symlink_to(elsewhere)
    monkeypatch.setattr(config.os.path, "islink", lambda path: False)
    _clear()
    assert config.placement(elsewhere / "proj") is None


@pytest.mark.parametrize("how", ["registered", "in-the-root", "beside-main"])
def test_a_worktree_once_adopted_as_a_project_of_its_own_is_mains_again(yard, monkeypatch, how):
    # Before this change a worktree could be adopted as a project of its own (a main's marker). It
    # is main's worktree: that old adoption is skipped and said, never a second claim.
    wt = yard["feat"]
    if how == "beside-main":
        wt = yard["base"] / "beside"
        assert _git(yard["main"], "worktree", "add", "-q", "-b", "b", str(wt)).returncode == 0
    if how != "registered":
        worktree_registry.unregister(yard["main"], "feat")
    _adopt_before_the_rule(wt)
    if how == "beside-main":  # still a main record, whose .git makes it one made outside fy
        _refused(wt, monkeypatch, "made outside fy")
        return
    if how == "in-the-root":  # unregistered, its main record stands, so acme's root loses…
        assert any("worktrees root" in p for p in config.record_problems(yard["main"]))
        _refused(wt, monkeypatch, "made outside fy")  # …and its .git says what it is
        return
    assert any("acts on it as that worktree" in p for p in config.record_problems(yard["main"]))
    code, out, seen = _at(wt, monkeypatch)
    assert code == 0, out
    assert (seen["project"], seen["worktree"], seen["main"]) == ("acme", "feat", yard["main"])
    assert stack.main_checkout_of(wt) == yard["main"]
    assert wt not in configpin.adopted_checkouts()  # nor is it listed as a project (`fy ls`)


def test_a_box_cant_make_its_own_main_another_projects_worktree(yard, monkeypatch):
    # Its .git names acme's admin dir: never a reason to read it as acme's, nor a doctor WARN.
    mike = _repo(yard["base"] / "mike", '[project]\nname = "mike"\n')
    _adopt(mike)
    shutil.rmtree(mike / ".git")
    (yard["main"] / ".git" / "worktrees").mkdir(exist_ok=True)
    (mike / ".git").write_text(f"gitdir: {yard['main']}/.git/worktrees/zz\n")
    _clear()
    assert config.record_problems(yard["main"]) == ()
    _refused(mike, monkeypatch, "made outside fy")
    code, out, seen = _at(yard["feat"], monkeypatch)
    assert code == 0 and seen["project"] == "acme", out


def test_a_project_adopted_in_another_projects_root_before_keeps_working(yard, monkeypatch):
    # The root loses, not the project in it: acme's root claims nothing and isn't mounted (its
    # doctor says what to change), and bravo is a project as it was.
    bravo = _repo(yard["root"] / "bravo", '[project]\nname = "bravo"\n')
    with pytest.raises(configpin.NestedProject):
        _adopt(bravo)
    _adopt_before_the_rule(bravo)
    for main in (bravo, yard["main"]):
        assert any("worktrees root" in p and NESTED in p for p in config.record_problems(main))
    code, out, seen = _at(bravo, monkeypatch)
    assert code == 0 and (seen["project"], seen["main"]) == ("bravo", bravo), out
    assert bravo in configpin.adopted_checkouts()
    assert config.mount_conflict(yard["main"], yard["root"]) is not None
    monkeypatch.chdir(yard["main"])
    _clear()
    status, _, said = devmode._worktree_registry_check()
    assert status == "fail" and "fy machine recreate" in said
    with pytest.raises(SystemExit):
        config.refuse_mount_conflict(yard["main"], yard["root"])


@pytest.mark.parametrize("where", ["inside", "at"])
def test_a_root_on_another_projects_checkout_is_refused_and_claims_nothing(
    yard, monkeypatch, where
):
    # alpha's root inside bravo's checkout (`../bravo/trees`), or at it (`../bravo`).
    bravo = _repo(yard["base"] / "bravo", '[project]\nname = "bravo"\n')
    _adopt(bravo)
    (bravo / "trees").mkdir()
    declared = "../bravo/trees" if where == "inside" else "../bravo"
    alpha = _repo(
        yard["base"] / "alpha",
        f'[project]\nname = "alpha"\n[machine]\nworktrees_root = "{declared}"\n',
    )
    with pytest.raises(configpin.NestedProject, match="the worktrees root"):
        _adopt(alpha)
    _adopt_before_the_rule(alpha)
    assert any("so it claims nothing" in p for p in config.record_problems(alpha))
    assert "inside" in (config.claimed_elsewhere(bravo / "trees" / "w", alpha) or "")
    # bravo's box swaps `trees` for a link to where a third project lives: nothing follows it.
    charlie = _repo(yard["base"] / "elsewhere" / "charlie", '[project]\nname = "charlie"\n')
    _adopt(charlie)
    (yard["base"] / "elsewhere" / "fresh").mkdir()
    shutil.rmtree(bravo / "trees")
    (bravo / "trees").symlink_to(yard["base"] / "elsewhere")
    for path, main in ((charlie, charlie), (yard["base"] / "elsewhere" / "fresh", None)):
        placed = config.placement(path)
        assert (placed.main if placed else None) == main
    code, out, seen = _at(charlie, monkeypatch)
    assert code == 0 and (seen["project"], seen["worktree"]) == ("charlie", ""), out


@pytest.mark.parametrize("cached", [False, True])
def test_a_root_outside_main_swapped_for_a_link_claims_nothing(yard, monkeypatch, cached):
    (yard["main"] / "foldyard.toml").write_text(MAIN_TOML + 'worktrees_root = "../trees"\n')
    _adopt(yard["main"])
    (yard["base"] / "trees").mkdir()
    fresh = yard["base"] / "elsewhere" / "fresh"
    fresh.mkdir(parents=True)
    _clear()
    placed = config.placement(yard["base"] / "trees" / "x")
    assert placed is not None and placed.kind == "stray"
    (yard["base"] / "trees").rmdir()
    (yard["base"] / "trees").symlink_to(yard["base"] / "elsewhere")
    if not cached:
        _clear()
    assert config.placement(fresh) is None


def test_a_project_at_another_projects_root(yard, monkeypatch):
    # bravo adopted where alpha's (empty) root is: refused now; before, alpha's root claims nothing
    # and a worktree alpha registered inside bravo neither.
    alpha = _repo(yard["base"] / "alpha", '[project]\nname = "alpha"\n')
    _adopt(alpha)
    root = yard["base"] / "alpha-worktrees"
    assert "worktrees root" in (config.nesting_conflict(root) or "")
    bravo = _repo(root, '[project]\nname = "bravo"\n')
    _adopt_before_the_rule(bravo)
    w1 = root / "w1"
    assert _git(alpha, "worktree", "add", "-q", "-b", "w1", str(w1)).returncode == 0
    worktree_registry.register(alpha, "w1", w1)
    _clear()
    problems = config.record_problems(alpha)
    assert any(f"worktrees root {root}" in p for p in problems)
    assert any(f"registered worktree {w1}" in p for p in problems)
    placed = config.placement(bravo)
    assert placed is not None and placed.main == bravo
    placed = config.placement(w1)
    assert placed is not None and placed.kind == "refused"  # inside bravo, with a .git of its own


def test_worktree_add_never_lands_in_another_projects_checkout(yard, monkeypatch):
    bravo = _repo(yard["base"] / "bravo", '[project]\nname = "bravo"\n')
    _adopt(bravo)
    assert NESTED in (config.claimed_elsewhere(bravo / "x", yard["main"]) or "")
    assert config.claimed_elsewhere(yard["root"] / "new", yard["main"]) is None


def test_a_stray_never_takes_a_registered_worktrees_name(yard, monkeypatch):
    # `hotfix` is registered elsewhere; a directory the box made as root/hotfix isn't it.
    old = yard["base"] / "old-place" / "hotfix"
    assert _git(yard["main"], "worktree", "add", "-q", "-b", "hotfix", str(old)).returncode == 0
    worktree_registry.register(yard["main"], "hotfix", old)
    stray = yard["root"] / "hotfix"
    stray.mkdir()
    _refused(stray, monkeypatch, f"{stray} is in {yard['main']}'s worktrees root", str(old))


def test_a_gitdir_spelled_in_another_case_is_still_a_worktree_made_outside_fy(yard, monkeypatch):
    wt = yard["main"] / ".claude" / "worktrees" / "agent"
    assert _git(yard["main"], "worktree", "add", "-q", "-b", "agent", str(wt)).returncode == 0
    if not (yard["main"] / ".GIT").exists():
        pytest.skip("this filesystem is case-sensitive")
    (wt / ".git").write_text(f"gitdir: {str(yard['main']).upper()}/.GIT/WORKTREES/agent\n")
    _refused(wt, monkeypatch, "made outside fy")
    code, out, seen = _box_exec(wt, monkeypatch, "true")
    assert code == 0 and seen["stack_main"] == yard["main"], out


def test_a_main_whose_git_dir_is_elsewhere_has_working_worktrees(yard, monkeypatch):
    # `git init --separate-git-dir`: main's .git is a file, and its worktrees' admin dirs are there.
    gitdir = yard["base"] / "gitdirs" / "sep.git"
    sep = yard["base"] / "sep"
    gitdir.parent.mkdir()
    subprocess.run(
        ["git", "init", "-q", "-b", "main", "--separate-git-dir", str(gitdir), str(sep)],
        check=True,
    )
    (sep / "foldyard.toml").write_text('[project]\nname = "sep"\n')
    assert _git(sep, "commit", "-q", "--allow-empty", "-m", "b").returncode == 0
    _adopt(sep)
    wt = yard["base"] / "sep-worktrees" / "feat"
    assert _git(sep, "worktree", "add", "-q", "-b", "feat", str(wt)).returncode == 0
    worktree_registry.register(sep, "feat", wt)
    lib = sep / "lib"
    lib.mkdir()
    (lib / ".git").write_text(f"gitdir: {gitdir}/modules/lib\n")  # a submodule's
    for where, worktree in ((wt, "feat"), (lib, "")):
        code, out, seen = _at(where, monkeypatch)
        assert code == 0, out
        assert (seen["project"], seen["worktree"], seen["main"]) == ("sep", worktree, sep)


def test_foldyard_repo_through_a_link_is_refused_too(yard, monkeypatch):
    # The recipes set FOLDYARD_REPO: a path through a link there is the same surprise as a cd.
    beta = _repo(yard["base"] / "beta", '[project]\nname = "beta"\n')
    _adopt(beta)
    (yard["root"] / "link").symlink_to(beta)
    monkeypatch.setenv("FOLDYARD_REPO", f"{yard['root']}/link")
    _refused(beta, monkeypatch, "through a link")


def test_a_stray_named_like_a_registered_worktree_in_another_case_is_refused(yard, monkeypatch):
    elsewhere = yard["base"] / "outside" / "feat2"
    assert _git(yard["main"], "worktree", "add", "-q", "-b", "f2", str(elsewhere)).returncode == 0
    worktree_registry.register(yard["main"], "feat2", elsewhere)
    stray = yard["root"] / "FEAT2"
    stray.mkdir()
    _refused(stray, monkeypatch, "is the worktree registered at", str(elsewhere))


def test_a_checkout_of_a_bare_repository_says_what_to_do(yard, monkeypatch):
    # `.bare/` plus `git worktree add main`: not a layout fy acts in, and it says so.
    layout = yard["base"] / "bare-layout"
    layout.mkdir()
    subprocess.run(
        ["git", "clone", "-q", "--bare", str(yard["main"]), str(layout / ".bare")], check=True
    )
    bare = [
        "git",
        "-C",
        str(layout / ".bare"),
        "worktree",
        "add",
        "-q",
        str(layout / "main"),
        "main",
    ]
    subprocess.run(bare, check=True)
    _refused(layout / "main", monkeypatch, "a checkout made from a bare repository", "git clone")
    _adopt_before_the_rule(layout / "main")
    _refused(layout / "main", monkeypatch, "a checkout made from a bare repository")


@pytest.mark.parametrize("argv", [["docs"], ["mode", "--help"]])
def test_help_and_project_free_verbs_ask_no_git_and_pin_no_worktree(yard, monkeypatch, argv):
    monkeypatch.chdir(yard["feat"])
    _clear()
    asked: list = []
    monkeypatch.setattr(stack, "find_main_repo", lambda: asked.append(1))
    result = runner.invoke(cli.app, argv)
    assert result.exit_code == 0, result.output
    assert asked == [] and "WORKTREE" not in os.environ


def test_config_adopt_in_a_stray_is_refused_and_not_offered(yard, monkeypatch):
    stray = _repo(yard["root"] / "bravo", '[project]\nname = "bravo"\n[machine]\nname = "x"\n')
    code, out, _ = _at(stray, monkeypatch)
    assert code == 0 and "isn't adopted" not in out, out  # no notice pointing at adopting it
    result = runner.invoke(cli.app, ["config", "adopt"])
    assert result.exit_code == 1, result.output
    assert "fy worktree add bravo" in " ".join(result.output.split()), result.output
    assert not configpin.pin_dir(config.resolve(worktree="bravo", repo=stray)).exists()


def test_a_dotdot_root_leaves_its_siblings_be(yard, monkeypatch):
    # `..` holds main: inert, so a project beside it is its own, and nobody's doctor says a thing.
    (yard["main"] / "foldyard.toml").write_text(MAIN_TOML + 'worktrees_root = ".."\n')
    _adopt(yard["main"])
    beta = _repo(yard["base"] / "beta", '[project]\nname = "beta"\n')
    _adopt(beta)
    code, out, seen = _at(beta, monkeypatch)
    assert code == 0 and (seen["project"], seen["main"]) == ("beta", beta), out
    for main, root in ((beta, yard["base"] / "beta-worktrees"), (yard["main"], yard["base"])):
        assert config.record_problems(main) == ()
        assert config.mount_conflict(main, root) is None


@pytest.mark.parametrize("nested", ["inside", "at"])
def test_a_root_around_or_at_another_projects_root_claims_nothing(yard, monkeypatch, nested):
    # The outer root mounts the inner one's worktrees: it loses, and the inner one stands; at one
    # directory, both lose.
    trees = yard["base"] / "trees"
    (trees / "b").mkdir(parents=True)
    declared = {"inside": trees / "b", "at": trees}[nested]
    alpha = _repo(
        yard["base"] / "alpha",
        f'[project]\nname = "alpha"\n[machine]\nworktrees_root = "{trees}"\n',
    )
    _adopt(alpha)
    beta = _repo(
        yard["base"] / "beta",
        f'[project]\nname = "beta"\n[machine]\nworktrees_root = "{declared}"\n',
    )
    with pytest.raises(configpin.NestedProject):
        _adopt(beta)  # an adoption makes no overlap at all
    _adopt_before_the_rule(beta)
    for path in (trees / "y", trees / "b" / "z"):
        path.mkdir(parents=True, exist_ok=True)
    assert config.placement(trees / "y") is None
    inner = config.placement(trees / "b" / "z")
    assert (inner.main if inner else None) == (beta if nested == "inside" else None)
    assert config.mount_conflict(alpha, trees) is not None
    assert (config.mount_conflict(beta, declared) is None) is (nested == "inside")


def test_a_root_spelled_another_way_is_judged_by_what_it_is(yard, monkeypatch):
    # APFS takes NFC and NFD for one name (and case, below): judged by identity, not spelling.
    nfc, nfd = (unicodedata.normalize(form, "café") for form in ("NFC", "NFD"))
    cafe = _repo(yard["base"] / nfc, '[project]\nname = "cafe"\n')
    _adopt(cafe)
    (cafe / "trees" / "x").mkdir(parents=True)
    if not os.path.samefile(yard["base"] / nfc, yard["base"] / nfd):
        pytest.skip("this filesystem tells the two spellings apart")
    declared = str(yard["base"] / nfd / "trees")
    bravo = _repo(
        yard["base"] / "bravo",
        f'[project]\nname = "bravo"\n[machine]\nworktrees_root = "{declared}"\n',
    )
    assert config.nesting_conflict(bravo, declared) is not None
    _adopt_before_the_rule(bravo)
    assert any("worktrees root" in p for p in config.record_problems(bravo))
    placed = config.placement(cafe / "trees" / "x")
    assert placed is not None and placed.main == cafe and placed.kind == "main"


@pytest.mark.parametrize("onto", ["another-checkout", "nowhere-else"])
def test_a_root_declared_through_your_own_link(yard, monkeypatch, onto):
    # `~/code` → `/Volumes/…`: the root is where the link leads. Onto another project's checkout
    # it's refused and never mounted; anywhere, it claims nothing and the doctor says so.
    alpha = _repo(yard["base"] / "alpha", '[project]\nname = "alpha"\n')
    _adopt(alpha)
    (alpha / "trees").mkdir()
    (yard["base"] / "code").symlink_to(yard["base"])
    target = "alpha/trees" if onto == "another-checkout" else "bravo-trees"
    declared = str(yard["base"] / "code" / target)
    bravo = _repo(
        yard["base"] / "bravo",
        f'[project]\nname = "bravo"\n[machine]\nworktrees_root = "{declared}"\n',
    )
    conflict = config.nesting_conflict(bravo, declared)
    assert (conflict is not None) is (onto == "another-checkout")
    _adopt_before_the_rule(bravo)
    assert any("reached through a link" in p for p in config.record_problems(bravo))
    assert (config.mount_conflict(bravo, Path(declared)) is not None) is (
        onto == "another-checkout"
    )


def test_a_root_holding_other_projects_loses_and_they_keep_working(yard, monkeypatch):
    code_dir = yard["base"] / "code"
    alpha = _repo(code_dir / "alpha", '[project]\nname = "alpha"\n')
    charlie = _repo(code_dir / "charlie", '[project]\nname = "charlie"\n')
    for main in (alpha, charlie):
        _adopt(main)
    zulu = _repo(
        yard["base"] / "elsewhere" / "zulu",
        f'[project]\nname = "zulu"\n[machine]\nworktrees_root = "{code_dir}"\n',
    )
    with pytest.raises(configpin.NestedProject, match="holds"):
        _adopt(zulu)
    _adopt_before_the_rule(zulu)
    for main in (alpha, charlie):
        code, out, seen = _at(main, monkeypatch)
        assert code == 0 and seen["main"] == main, out
        assert main in configpin.adopted_checkouts()
    assert any("worktrees root" in p for p in config.record_problems(zulu))
    assert config.mount_conflict(zulu, code_dir) is not None
    # alpha's own root, under zulu's, is alpha's to mount as ever: zulu's is the one that bridges.
    assert config.mount_conflict(alpha, code_dir / "alpha-worktrees") is None


def test_claimed_elsewhere_never_follows_a_link_the_box_put_in_main(yard, monkeypatch):
    main = yard["main"]
    (main / "foldyard.toml").write_text(MAIN_TOML + f'worktrees_root = "{main}/.trees"\n')
    _adopt(main)
    (main / ".trees").mkdir()
    bravo = _repo(yard["base"] / "bravo", '[project]\nname = "bravo"\n')
    _adopt(bravo)
    assert config.claimed_elsewhere(main / ".trees" / "x", main) is None
    (main / ".trees").rmdir()
    (main / ".trees").symlink_to(bravo)  # the box's swap
    assert "reached through a link" in (config.claimed_elsewhere(main / ".trees" / "x", main) or "")


def test_one_verb_sees_one_placement(yard, monkeypatch):
    # A `foldyard.toml` the box plants mid-verb changes nothing the verb reads after the front door.
    other = yard["main"] / "other"
    other.mkdir()
    calls: list = []
    real = config._placement_here

    def planting():
        calls.append(1)
        if len(calls) == 2:
            (other / "foldyard.toml").write_text('[project]\nname = "other"\n')
        return real()

    monkeypatch.setattr(config, "_placement_here", planting)
    code, out, seen = _at(other, monkeypatch)
    assert code == 0, out
    assert (seen["project"], seen["root"]) == ("acme", yard["main"])
    assert calls == [1]  # placed once, at the front door


def test_a_refusal_under_a_main_never_falls_back_to_what_the_box_planted(yard, monkeypatch):
    other = yard["main"] / "other"
    other.mkdir()
    (other / "foldyard.toml").write_text('[project]\nname = "other"\n')
    monkeypatch.chdir(other)
    _clear()
    assert config.repo_root() == yard["main"]


def test_an_unchanged_record_is_never_resolved_again(yard, monkeypatch):
    # What each record is was read with the records; a lookup only stats it, unless it changed.
    for i in range(5):
        _adopt(_repo(yard["base"] / f"p{i}", f'[project]\nname = "p{i}"\n'))
    config.placement(yard["main"])
    resolved: list = []
    real = os.path.realpath
    monkeypatch.setattr(
        config.os.path, "realpath", lambda p, **k: resolved.append(p) or real(p, **k)
    )
    config.placement(yard["main"])
    assert resolved == [yard["main"]]  # where it runs, alone


def test_a_git_dir_of_its_own_under_a_worktrees_directory_is_no_linked_worktree(yard, monkeypatch):
    # `git init --separate-git-dir …/worktrees/proj`: a whole git dir, not a worktree's admin one.
    gitdir = yard["base"] / "gitdirs" / "worktrees" / "proj"
    gitdir.parent.mkdir(parents=True)
    proj = yard["base"] / "proj"
    subprocess.run(["git", "init", "-q", "--separate-git-dir", str(gitdir), str(proj)], check=True)
    (proj / "foldyard.toml").write_text('[project]\nname = "proj"\n')
    _adopt(proj)
    code, out, seen = _at(proj, monkeypatch)
    assert code == 0 and seen["project"] == "proj", out


def test_where_fy_runs_is_placed_afresh_on_every_ask(yard, monkeypatch):
    # Nothing memoizes a placement: one made under other records (or, in the suite, by a thread a
    # test left running under other seams) never answers a later ask.
    stray = yard["root"] / "late"
    assert _git(yard["main"], "worktree", "add", "-q", "-b", "late", str(stray)).returncode == 0
    monkeypatch.chdir(stray)
    _clear()
    placed = config.placement_here()
    assert placed is not None and placed.kind == "stray"
    worktree_registry.register(yard["main"], "late", stray)
    placed = config.placement_here()
    assert placed is not None and (placed.kind, placed.name) == ("worktree", "late")


def test_a_record_reached_through_a_link_when_read_counts_again_once_undone(yard, monkeypatch):
    # Read while the box had a link above it, a worktree neither stands nor counts against anyone;
    # undone, it is read again — not left out of the records for the life of a TUI.
    main = yard["main"]
    (main / "foldyard.toml").write_text(MAIN_TOML + 'worktrees_root = ".trees"\n')
    _adopt(main)
    inner = main / ".trees" / "web"
    assert _git(main, "worktree", "add", "-q", "-b", "web", str(inner)).returncode == 0
    worktree_registry.register(main, "web", inner)
    (main / ".trees").rename(main / ".trees-real")
    (main / ".trees").symlink_to(main / ".trees-real")
    _clear()
    placed = config.placement(inner)
    assert placed is not None and placed.kind != "worktree"  # no worktree record stands there
    (main / ".trees").unlink()
    (main / ".trees-real").rename(main / ".trees")
    placed = config.placement(inner)
    assert placed is not None and (placed.kind, placed.name) == ("worktree", "web")


@pytest.mark.parametrize("site", ["ensure", "recreate", "stack"])
def test_every_place_that_mounts_the_root_refuses_one_on_another_project(
    yard, monkeypatch, capsys, site
):
    from foldyard import machine

    monkeypatch.setattr(config, "mount_conflict", lambda main, root: "it lies inside bravo")
    monkeypatch.chdir(yard["main"])
    _clear()
    capsys.readouterr()
    with pytest.raises(SystemExit):
        if site == "ensure":
            machine.ensure(yard["main"], yard["root"])
        elif site == "recreate":
            machine.recreate(yard["main"], yard["root"], assume_yes=True)
        else:
            stack.resolve(no_machine=True)
    assert "✗ it lies inside bravo" in capsys.readouterr().err
