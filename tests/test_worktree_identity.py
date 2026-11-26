"""A registered worktree with nothing adopted carries its PROJECT's identity, not its directory's.

On your computer an unadopted checkout reads an empty config (ADR-0022), so its project, prefix,
machine and backend fell back to defaults — the project named after the worktree's directory, the
machine after that, the backend `lima`. A consumer's post-checkout hook running `fy box exec` in a
fresh worktree was told "lima machine 'sec-dead-routes' does not exist" on a podman host whose VM
is `tangible`, and every state path for that worktree pointed at a project that doesn't exist.

The identity now comes from MAIN's adopted snapshot, and only when the host vouches for the link:
the worktree is in its own registry under that main. Only identity: the worktree's config stays
empty (``configpin.effective``), so the supervisor still runs nothing for it and every launch verb
still asks for the adoption. These run the real host read (conftest's seams point it at the tree).
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from foldyard import config, configpin, devmode, stack, worktree_registry

# Captured at import, before conftest's autouse seams replace them.
_REAL_HOST_TOML = config._host_toml
_REAL_PIN_DIR = configpin.pin_dir

MAIN_TOML = (
    '[project]\nname = "acme"\nprefix = "ac"\n\n'
    '[machine]\nname = "acme-vm"\nbackend = "podman"\nworktrees_root = "../trees"\n'
    "firewall = true\n"
)


def _git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True)


def _adopt(root):
    configpin.adopt(config.resolve(worktree="", repo=root))
    config.clear_caches()


@pytest.fixture
def project(tmp_path, fresh_config, monkeypatch):
    """A real main checkout, adopted, with a real git worktree beside it that is registered but
    not adopted. The worktree's tree claims a different identity — which must never be read."""
    monkeypatch.setattr(config, "_host_toml", _REAL_HOST_TOML)
    monkeypatch.setattr(configpin, "pin_dir", _REAL_PIN_DIR)
    monkeypatch.setattr(config, "in_box", lambda: False)
    monkeypatch.setenv("FOLDYARD_STATE_DIR", str(tmp_path / "state"))
    for name in ("FOLDYARD_PROJECT", "FOLDYARD_PROJECT_PREFIX", "PODMAN_MACHINE"):
        monkeypatch.delenv(name, raising=False)
    for name in ("MACHINE_BACKEND", "FOLDYARD_WORKTREES_ROOT", "MACHINE_FIREWALL", "MACHINE_WALL"):
        monkeypatch.delenv(name, raising=False)
    main = tmp_path / "main"
    subprocess.run(["git", "init", "-q", "-b", "main", str(main)], check=True)
    _git(main, "config", "user.email", "t@example.com")
    _git(main, "config", "user.name", "t")
    (main / "foldyard.toml").write_text(MAIN_TOML)
    _git(main, "add", "-A")
    _git(main, "commit", "-qm", "base")
    wt = tmp_path / "trees" / "feat"
    assert _git(main, "worktree", "add", "-q", "-b", "feat", str(wt)).returncode == 0
    (wt / "foldyard.toml").write_text('[project]\nname = "evil"\n\n[machine]\nname = "evil"\n')
    _adopt(main)
    worktree_registry.register(main, "feat", wt)
    monkeypatch.setattr(devmode, "main_repo", lambda: main)
    fresh_config(FOLDYARD_REPO=wt)
    return {"main": main, "wt": wt}


def test_an_unadopted_registered_worktree_takes_mains_identity(project):
    assert config.project() == "acme"
    assert config.project_prefix() == "ac"
    assert config.machine_name() == "acme-vm"
    assert config.retired_backend() == "podman"
    # …and where the project's worktrees live, or the CLI's worktree inference (which reads it)
    # would read this checkout as MAIN and key its posture on main's.
    assert config.worktrees_root(project["main"]) == project["main"].parent / "trees"


def test_only_identity_comes_with_it(project):
    # Everything with a host consequence still reads the worktree's (empty) adoption: the launch
    # gate asks, `machine.ensure` refuses, the stack refuses — and says why.
    assert config.machine_wall() is False  # main's `firewall = true` did NOT come along
    assert config.ambient_adopted() is False
    assert config.unadopted_notice() is not None


def test_the_bound_worktree_config_stays_empty_but_named(project):
    cfg = devmode.worktree_config("feat")
    assert cfg.toml == {}  # configpin.effective: the supervisor runs nothing for it
    assert cfg.project() == "acme"
    assert cfg.machine_name() == "acme-vm"
    assert cfg.machine_wall() is False
    # Exactly the naming keys: anything else main declares is config, and stays main's.
    assert cfg.identity == {
        "project": {"name": "acme", "prefix": "ac"},
        "machine": {"name": "acme-vm", "backend": "podman", "worktrees_root": "../trees"},
    }


def test_an_unregistered_worktree_inherits_nothing(project):
    # The registry is the host's own word that this checkout belongs to that main — git's link
    # (`.git` in the worktree) is box-writable, so it alone vouches for nothing.
    worktree_registry.unregister(project["main"], "feat")
    config.clear_caches()
    assert config.project() == "feat"


# ── a directory the box made under the worktrees root ─────────────────────────────────────
# The host lists it (`fy tui`'s card, the doctor row) and reads nothing from it. But the config the
# card binds named the project after the DIRECTORY, so the box chose the host state dir its card
# reads and writes (`~/.foldyard/<name>/`, another project's if it took that name), and a port band
# was allocated for it — while a name that isn't a plain one took the TUI down on highlight. It is
# listed under main, by main's anchor (never git's link), so it is named for main: nothing of its
# own, and nothing named after it.

BOX_MADE = ["stray", "acme-other", "bad name", "-flag", ".dot", "UPPER"]


def _box_made(project, name, *, toml='[project]\nname = "evil"\n'):
    path = project["main"].parent / "trees" / name
    path.mkdir(parents=True)
    (path / ".git").write_text("gitdir: /nowhere\n")
    (path / "foldyard.toml").write_text(toml)
    assert name in worktree_registry.unregistered(project["main"], path.parent)
    return path


@pytest.mark.parametrize("name", BOX_MADE)
def test_a_box_made_worktree_dir_is_named_for_main(project, monkeypatch, tmp_path, name):
    _box_made(project, name)
    # The state dir from the project's name, as on your computer (the adoptions stay where they are).
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    (tmp_path / "home").mkdir()
    (tmp_path / "home" / ".foldyard").symlink_to(tmp_path / "state")
    monkeypatch.delenv("FOLDYARD_STATE_DIR")
    cfg = devmode.worktree_config(name)
    assert cfg.toml == {}  # nothing of its own: it still runs nothing
    assert cfg.worktree == name
    assert (cfg.project(), cfg.project_prefix(), cfg.machine_name()) == ("acme", "ac", "acme-vm")
    home = Path.home() / ".foldyard" / "acme"
    assert cfg.state_dir() == home
    assert cfg.mode_file() == home / "worktrees" / name / "dev-mode.json"


@pytest.mark.parametrize("name", BOX_MADE)
def test_a_box_made_worktree_dir_gets_no_port_band(project, name):
    from foldyard import ports

    _box_made(project, name)
    cfg = devmode.worktree_config(name)
    with config.using(cfg):
        config.proxy_port()
        config.gcp_minter_port()
    assert set(json.loads(ports.registry_file().read_text())) == {"acme"}


@pytest.mark.parametrize("named", ["", '[project]\nname = ""\n'])
def test_a_box_made_worktree_dir_under_an_unnamed_main_takes_mains_directory_name(project, named):
    (project["main"] / "foldyard.toml").write_text(
        named + '[machine]\nworktrees_root = "../trees"\n'
    )
    _adopt(project["main"])
    _box_made(project, "stray")
    cfg = devmode.worktree_config("stray")
    assert cfg.project() == project["main"].name == "main"  # what main itself resolves to
    assert cfg.machine_name() == "main"


def test_nothing_is_read_from_a_box_made_worktree_dir(project, monkeypatch, tmp_path):
    # A symlink to another checkout the host has adopted: following it would hand the stray dir
    # that checkout's name (and its adoption). Nothing is resolved, parsed or asked of git there.
    other = tmp_path / "other"
    other.mkdir()
    (other / ".git").mkdir()
    (other / "foldyard.toml").write_text('[project]\nname = "other"\n[[inject]]\n')
    _adopt(other)
    link = project["main"].parent / "trees" / "stray"
    link.parent.mkdir(exist_ok=True)
    link.symlink_to(other)
    asked: list = []
    real = configpin.adopted_toml
    monkeypatch.setattr(configpin, "adopted_toml", lambda root: asked.append(root) or real(root))
    real_git = config._git_main

    def git_main(root):
        assert Path(root) not in (link, other), f"git asked of {root}"
        return real_git(root)

    monkeypatch.setattr(config, "_git_main", git_main)
    cfg = devmode.worktree_config("stray")
    assert cfg.project() == "acme" and cfg.toml == {}
    assert project["main"] in asked  # main's adoption names it…
    assert not {link, other} & set(asked)  # …and the dir, and where it points, are never looked up


def test_nothing_is_inherited_when_main_is_not_adopted(project, tmp_path):
    for child in (tmp_path / "state" / "adopted").iterdir():
        if child.name.startswith("main-"):
            import shutil

            shutil.rmtree(child)
    config.clear_caches()
    assert config.project() == "feat"


def test_a_worktree_with_its_own_adoption_reads_only_that(project):
    (project["wt"] / "foldyard.toml").write_text('[project]\nname = "acme-feat"\n')
    _adopt(project["wt"])
    assert config.project() == "acme-feat"
    assert config.machine_name() == "acme-feat"  # its own adoption is all it reads


def test_main_reads_its_own_adoption(project, fresh_config):
    fresh_config(FOLDYARD_REPO=project["main"])
    assert config.project() == "acme"
    assert config.machine_wall() is True


def test_the_box_reads_its_own_tree(project, monkeypatch):
    monkeypatch.setattr(config, "in_box", lambda: True)
    config.clear_caches()
    assert config.project() == "evil"  # the box's own checkout, as before: no host state there


def test_the_worktree_inference_finds_it_under_the_projects_root(project, monkeypatch):
    # The CLI's up-front pin (cli.py) infers WORKTREE from the cwd under the worktrees root.
    monkeypatch.chdir(project["wt"])
    assert stack._active_worktree(stack.worktrees_root(project["main"])) == "feat"


def test_a_worktree_adopted_empty_still_reads_only_its_own(project):
    # Adopting a checkout with no foldyard.toml is an adoption (an empty one): from then on the
    # worktree is configured by it, and inherits nothing — on both read paths.
    (project["wt"] / "foldyard.toml").unlink()
    _adopt(project["wt"])
    assert config.project() == "feat"
    assert devmode.worktree_config("feat").project() == "feat"
