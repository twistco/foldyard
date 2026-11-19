"""Adopting a ``min_foldyard_version`` this fy doesn't meet locked every verb out (2026-11-19).

On the host the floor is read from the ADOPTED config, and it fronts every verb. An operator on
0.11.0 adopted a branch's config asking for 0.12.0 (not yet released) at `fy code`'s launch gate:
from then on every `fy` refused, `fy config adopt` included, so checking main out again changed
nothing, and the only way out was editing ``~/.foldyard/adopted`` by hand. So adoption refuses a
floor this fy can't meet, and the `fy config` verbs are never behind the floor: they are how an
adopted config is put right."""

from __future__ import annotations

import pytest
from typer.testing import CliRunner

import foldyard
from foldyard import config, configpin

# Captured at import, before conftest's autouse seam swaps it for the tree reader.
_REAL_HOST_TOML = config._host_toml

LOW = '[project]\nname = "acme"\nmin_foldyard_version = "0.1.0"\n'
HIGH = '[project]\nname = "acme"\nmin_foldyard_version = "99.0.0"\n'


@pytest.fixture
def host_checkout(tmp_path, fresh_config, monkeypatch):
    root = tmp_path / "repo"
    root.mkdir()
    monkeypatch.setattr(config, "_host_toml", _REAL_HOST_TOML)
    monkeypatch.setattr(config, "in_box", lambda: False)
    monkeypatch.setattr(foldyard, "__version__", "1.0.0")
    fresh_config(FOLDYARD_REPO=root)
    monkeypatch.chdir(root)
    return root


def _cfg(root):
    return config.resolve(worktree="", repo=root)


def _write(root, text):
    (root / "foldyard.toml").write_text(text)
    config.clear_caches()


def _adopted_floor(root):
    toml = configpin.adopted_toml(root)
    return None if toml is None else toml["project"].get("min_foldyard_version")


# ── adoption refuses a floor this fy can't meet ───────────────────────────────────────────────


def test_adopting_a_floor_this_fy_cannot_meet_changes_nothing(host_checkout):
    _write(host_checkout, LOW)
    configpin.adopt(_cfg(host_checkout))
    _write(host_checkout, HIGH)
    with pytest.raises(configpin.NeedsNewerFoldyard) as e:
        configpin.adopt(_cfg(host_checkout))
    assert "99.0.0" in str(e.value) and "1.0.0" in str(e.value)
    assert _adopted_floor(host_checkout) == "0.1.0"  # still the one it had


def test_a_first_adoption_is_refused_too(host_checkout):
    _write(host_checkout, HIGH)
    with pytest.raises(configpin.NeedsNewerFoldyard):
        configpin.adopt(_cfg(host_checkout))
    assert configpin.adopted_toml(host_checkout) is None


def test_a_floor_this_fy_meets_is_adopted(host_checkout, monkeypatch):
    monkeypatch.setattr(foldyard, "__version__", "99.0.0")
    _write(host_checkout, HIGH)
    configpin.adopt(_cfg(host_checkout))
    assert _adopted_floor(host_checkout) == "99.0.0"


def test_an_unknown_installed_version_is_not_refused(host_checkout, monkeypatch):
    # Like the floor itself: a version that can't be ordered can't be PROVEN too old.
    monkeypatch.setattr(foldyard, "__version__", "dev")
    _write(host_checkout, HIGH)
    configpin.adopt(_cfg(host_checkout))
    assert _adopted_floor(host_checkout) == "99.0.0"


def test_the_launch_gate_says_why_and_adopts_nothing(host_checkout):
    _write(host_checkout, LOW)
    configpin.adopt(_cfg(host_checkout))
    _write(host_checkout, HIGH)
    said: list[str] = []
    status = configpin.resolve(
        _cfg(host_checkout), interactive=True, prompt=lambda _: "a", echo=said.append
    )
    assert status == "unresolved"
    assert any("99.0.0" in line and "upgrade" in line for line in said)
    assert _adopted_floor(host_checkout) == "0.1.0"


def test_the_launch_gate_refuses_a_first_adoption_the_same_way(host_checkout):
    _write(host_checkout, HIGH)
    said: list[str] = []
    status = configpin.resolve(
        _cfg(host_checkout), interactive=True, prompt=lambda _: "", echo=said.append
    )
    assert status == "unadopted"
    assert any("99.0.0" in line for line in said)
    assert configpin.adopted_toml(host_checkout) is None


# ── through the CLI: the reported lock-out ────────────────────────────────────────────────────


def _invoke(argv):
    from foldyard import cli

    return CliRunner().invoke(cli.app, argv)


def test_fy_config_adopt_refuses_a_floor_it_cannot_meet(host_checkout):
    _write(host_checkout, LOW)
    configpin.adopt(_cfg(host_checkout))
    _write(host_checkout, HIGH)
    result = _invoke(["config", "adopt"])
    assert result.exit_code == 1
    assert "99.0.0" in result.output
    assert _adopted_floor(host_checkout) == "0.1.0"


def test_an_adopted_floor_this_fy_cannot_meet_can_still_be_put_right(host_checkout, monkeypatch):
    # The report: a config demanding a newer fy got adopted (here by a newer fy, as an older one
    # without the refusal above did for real), and the checkout went back to one this fy meets.
    monkeypatch.setattr(foldyard, "__version__", "99.0.0")
    _write(host_checkout, HIGH)
    configpin.adopt(_cfg(host_checkout))
    monkeypatch.setattr(foldyard, "__version__", "1.0.0")
    _write(host_checkout, LOW)

    assert _invoke(["ps", "--help"]).exit_code == 1  # every other verb: behind the floor
    # …but not the way out (status exits 1 on drift, which there is: assert it ran)
    status = _invoke(["config", "status"])
    assert "needs foldyard" not in status.output and "differs" in status.output
    result = _invoke(["config", "adopt"])
    assert result.exit_code == 0, result.output
    config.clear_caches()
    assert _adopted_floor(host_checkout) == "0.1.0"
    assert _invoke(["ps", "--help"]).exit_code == 0  # and fy works again
