"""The shipped example consumer (example/) stays a valid foldyard config.

Guards example/foldyard.toml from bitrot: if a config accessor's contract drifts, or the
referenced files (compose, Dockerfiles) go missing, this fails. The example is both the
docs fixture and the payload we dogfood `foldyard up|verify|box` against (incl. nested)."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from foldyard import config, devmode

EXAMPLE = Path(__file__).resolve().parent.parent / "example"


@pytest.fixture
def example_repo(fresh_config):
    """Resolve foldyard config against the example dir (as if it were the repo root)."""
    fresh_config(FOLDYARD_REPO=str(EXAMPLE))
    return EXAMPLE


def test_example_files_present():
    for rel in ("foldyard.toml", "compose.yml", "box.Dockerfile", "api/Dockerfile", "api/main.py"):
        assert (EXAMPLE / rel).is_file(), rel


def test_example_config_parses(example_repo):
    assert config.project() == "foldyard-example"
    assert config.project_prefix() == "fyex"
    assert config.app_service() == "api"
    assert config.compose_files() == ["compose.yml"]
    assert config.port_bases() == {"API_PORT": 8080, "PG_PORT": 5544}
    assert config.machine_name() == "foldyard-example"


def test_example_compose_file_resolves(example_repo):
    # compose paths are relative to the checkout; the one we declare must really exist.
    assert (EXAMPLE / config.compose_files()[0]).is_file()


def test_example_box_image_dockerfile_resolves(example_repo):
    img = config.box_image()
    assert img["tag"] == "foldyard-example-box:latest"
    assert (EXAMPLE / img["dockerfile"]).is_file()


def test_example_posture_wiring(example_repo):
    # The README's posture walkthrough: the zero-secret fakecred rig is declared, and its
    # feature overlay layers on fakedep=on ONLY — a resting mode stays a bare -f chain.
    assert config.fakecred_declared()
    assert (EXAMPLE / "compose.feature.yml").is_file()
    assert config.matching_overlays({"fakedep": "on"}) == [EXAMPLE / "compose.feature.yml"]
    assert config.matching_overlays({}) == []


def test_example_omits_the_proxy_tier_and_says_why(example_repo):
    """This fixture is the STACK tier: no `[proxy]`, no wall, so box egress is direct — one notch
    below what `fy init` writes. The omission is structural (declaring `[proxy]` makes `fy box up`
    demand a CA only the host-side `fy host` can mint, which would lock this fixture out of the CI
    container / in-box / nested-KVM runs it exists for), but a deliberate gap rots into a silent
    one unless something pins it — so both files must SAY so and point at the locked-down sibling.
    """
    assert config.proxy_enabled() is False
    assert config.machine_wall() is False
    for rel in ("foldyard.toml", "README.md"):
        text = (EXAMPLE / rel).read_text()
        assert "example-lima-wall" in text, f"{rel} must point at the locked-down example"


def test_example_registry_refuses_a_strandable_fakedep(example_repo):
    # The walkthrough's refusal, pinned end-to-end through the example's own resolved config:
    # fakedep=on alone is an error whose message carries the atomic fix; set together, clean.
    from foldyard import plugins

    reg = plugins.registry()
    assert {"fakecred", "fakedep"} <= set(reg.axes())
    ((_sev, msg),) = [i for i in reg.mode_issues({"fakedep": "on"}) if i[0] == "error"]
    assert "`fy mode fakecred=on fakedep=on`" in msg
    assert reg.mode_issues({"fakecred": "on", "fakedep": "on"}) == []


def test_example_git_worktree_state_is_per_checkout(fresh_config, tmp_path, monkeypatch):
    repo = tmp_path / "example"
    shutil.copytree(EXAMPLE, repo)
    subprocess.run(["git", "-C", str(repo), "init"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo), "checkout", "-b", "trunk"], check=True)
    subprocess.run(
        ["git", "-C", str(repo), "config", "user.email", "test@example.test"], check=True
    )
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "Test"], check=True)
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(
        ["git", "-C", str(repo), "commit", "-m", "init"], check=True, capture_output=True
    )
    wt_root = tmp_path / "example-worktrees"
    feat = wt_root / "feat"
    subprocess.run(["git", "-C", str(repo), "worktree", "add", "-b", "feat", str(feat)], check=True)

    fresh_config(
        FOLDYARD_REPO=repo,
        FOLDYARD_STATE_DIR=tmp_path / "state",
        FOLDYARD_WORKTREES_ROOT=wt_root,
        WORKTREE=None,
    )
    assert (
        subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "--abbrev-ref", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        == "trunk"
    )
    assert config.posture_dir() == (tmp_path / "state").resolve() / "main"

    monkeypatch.setenv("WORKTREE", "feat")
    config.clear_caches()
    assert config.posture_dir() == (tmp_path / "state").resolve() / "worktrees" / "feat"
    main_cfg = devmode.worktree_config("")
    feat_cfg = devmode.worktree_config("feat")
    assert main_cfg.worktree == "" and main_cfg.repo_root == repo.resolve()
    assert feat_cfg.worktree == "feat" and feat_cfg.repo_root == feat.resolve()
