"""The locked-down example (example-lima-wall/) stays a valid foldyard config.

Guards it from bitrot the same way test_example does for the minimal fixture — but this one
asserts the LOCKED-DOWN posture wiring specifically: lima backend, the in-VM wall, an enforced
allowlist, and keyless Claude all resolve. It never boots anything (that needs a real Mac + Lima);
it only checks the config the docs promise resolves through foldyard's accessors."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from foldyard import config

EXAMPLE = Path(__file__).resolve().parent.parent / "example-lima-wall"


@pytest.fixture
def wall_repo(fresh_config):
    fresh_config(FOLDYARD_REPO=str(EXAMPLE), MACHINE_BACKEND=None, MACHINE_WALL=None)
    return EXAMPLE


def test_files_present():
    for rel in ("foldyard.toml", "compose.yml", "box.Dockerfile", "test_network.sh", "README.md"):
        assert (EXAMPLE / rel).is_file(), rel


def test_locked_down_posture_resolves(wall_repo):
    assert config.project() == "fy-wall-example"
    assert config.machine_backend() == "lima"
    assert config.machine_wall() is True
    # host_alias follows the backend: lima → the Lima host gateway (so the box reaches the Mac).
    assert config.host_alias() == config.LIMA_HOST_GATEWAY
    # enforced allowlist, carried by RECOMMENDATIONS (offers) — never by a grant list in the repo.
    assert config.proxy_default_deny() is True
    assert config.claude_keyless() == "oauth"
    assert "allow" not in config._table("proxy")  # the dead key: grants are host-side, always
    hosts = {e["host"] for e in config.proxy_recommend()}
    assert {"github.com", "pypi.org", "deb.debian.org", "registry-1.docker.io"} <= hosts
    assert all(e["why"] for e in config.proxy_recommend())  # each carries its why
    # Two sets the example must NOT list: the injector host (implicitly allowed — foldyard has to
    # reach it to mint) and the agent's installer, which the [claude] plugin contributes itself.
    assert "api.anthropic.com" not in hosts
    assert "claude.ai" not in hosts


def test_the_claude_plugin_supplies_its_own_installer_hosts(wall_repo):
    """Why the config above can stay short: a declared `[claude]` recommends its installer hosts
    from the plugin, into the same per-host offer. Pinned so a rename in the plugin can't quietly
    leave this walled example unable to install the agent it declares."""
    from foldyard.plugins import registry

    hosts = {e["host"] for e in registry().egress_recommend()}
    assert {"claude.ai", "downloads.claude.ai"} <= hosts
    assert "api.anthropic.com" not in hosts  # injector host: exempt at the proxy, never offered


def test_test_network_script_is_executable_and_host_only():
    # It shells `limactl` (host-only) and is committed +x so `bash test_network.sh` / `./` both work.
    text = (EXAMPLE / "test_network.sh").read_text()
    assert "limactl shell" in text
    assert "192.168.5.2" in text  # probes the Lima host gateway routing hop
    # The exec bit must be COMMITTED (index mode 100755). Probe git, not the filesystem: the
    # repo runs with core.fileMode false because shared-mount writers squash working-tree modes,
    # so an on-disk stat can read 644 while the commit is correct (and vice versa).
    proc = subprocess.run(
        ["git", "ls-files", "-s", "test_network.sh"],
        cwd=EXAMPLE,
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        pytest.skip("not a git checkout — can't probe the committed mode")
    mode = proc.stdout.split()[0] if proc.stdout.split() else ""
    assert mode == "100755", (
        f"test_network.sh committed with mode {mode or '<untracked>'} — restore the exec bit:"
        " git update-index --chmod=+x test_network.sh"
    )


def test_compose_worker_documents_the_no_proxy_caveat():
    # The teaching payload: the worker service must carry the no_proxy bypass for the in-stack name.
    text = (EXAMPLE / "compose.yml").read_text()
    assert "no_proxy" in text and "api" in text
