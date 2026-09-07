"""fakecred — the zero-secret mode-machinery testing rig (docs/testing-modes.md): a DECLARED
plugin whose axis pair + fake minter + toggleable capability exercise TTL expiry, the settle
cascade, daemon lifecycle, and capability probes with no real credential anywhere."""

from __future__ import annotations

from typing import Any, cast

import pytest

from conftest import FULL_TOML, make_config
from foldyard import config, devmode, plugins
from foldyard.plugins import fakecred

# FULL_TOML + the fakecred table: the Tangible-shaped axes PLUS the testing pair.
_full_plugins = cast("dict[str, Any]", FULL_TOML["plugins"])
FAKECRED_TOML = {**FULL_TOML, "plugins": {**_full_plugins, "fakecred": {}}}


@pytest.fixture
def fakecred_bound(tmp_path, monkeypatch):
    plugins._clear_registry_cache()
    monkeypatch.setattr(config, "mode_file", lambda: tmp_path / "dev-mode.json")
    monkeypatch.setattr(config, "mirror_file", lambda: tmp_path / "mirror.json")
    monkeypatch.setattr(devmode, "in_box", lambda: False)
    monkeypatch.setenv("FOLDYARD_STATE_DIR", str(tmp_path))
    with config.using(make_config(FAKECRED_TOML)):
        yield tmp_path
    plugins._clear_registry_cache()


def test_fakecred_loads_only_when_declared():
    plugin_names = [p.name for p in plugins.load_plugins(make_config(FULL_TOML), discover=False)]
    assert "fakecred" not in plugin_names  # undeclared → absent (a generic repo never sees it)
    plugin_names = [
        p.name for p in plugins.load_plugins(make_config(FAKECRED_TOML), discover=False)
    ]
    assert "fakecred" in plugin_names


def test_fakecred_axes_and_daemon(fakecred_bound):
    rungs = devmode.axes()
    assert rungs["fakecred"] == ("off", "on", "user")
    assert rungs["fakedep"] == ("off", "on")
    assert devmode.emergency()["fakecred"] == ("user",)
    # NB the always-on egress proxy is desired in every mode — assert on the fake-minter only.
    assert "fake-minter" not in devmode.desired_daemons({"fakecred": "off"})
    spec = devmode.desired_daemons({"fakecred": "on"})["fake-minter"]
    # Staged per-project launch path — the marker that makes orphan reaping work on it too.
    assert spec["cmd"][1].endswith("fakecred_minter.py")
    assert spec["cmd"][1].startswith(str(config.state_dir()))
    assert spec["env"]["LISTEN_PORT"] == str(spec["port"])


def test_fakedep_requires_fakecred(fakecred_bound):
    with pytest.raises(SystemExit, match="fakecred=on fakedep=on"):
        devmode.set_mode({"fakedep": "on"})
    assert devmode.set_mode({"fakecred": "on", "fakedep": "on"})["mode"]["fakedep"] == "on"


def test_fakecred_probe_toggles_with_the_capability_file(fakecred_bound):
    (probe,) = devmode.capability_probes({"fakecred": "on"})
    assert probe.axis == "fakecred" and devmode.capability_probes({"fakecred": "off"}) == []
    ok, detail = probe.check()
    assert ok  # no lapse file = capable (the resting state)
    fakecred.capability_file().write_text("lapsed\n")
    ok, detail = probe.check()
    assert not ok and "lapsed" in detail and "echo ok" in detail
    fakecred.capability_file().write_text("ok\n")
    assert probe.check()[0]


def test_fakecred_ttl_expiry_settles_fakedep(fakecred_bound, monkeypatch):
    # The full E loop on the rig: emergency rung + dependent axis + a clock fast-forward →
    # the supervisor's expiry flips fakecred off AND cascades fakedep with it, durably.
    import json
    from datetime import timedelta

    from foldyard import supervisor

    devmode.set_mode({"fakecred": "user", "fakedep": "on"}, ttl=3600)
    auth = config.mode_file()
    raw = json.loads(auth.read_text())
    raw["expires"]["fakecred"] = devmode._iso(devmode.now() - timedelta(hours=1))
    auth.write_text(json.dumps(raw))

    mode = supervisor.expire_user_modes()

    assert mode["fakecred"] == "off" and mode["fakedep"] == "off"
    durable = devmode.read(apply_expiry=False)["mode"]
    assert durable["fakecred"] == "off" and durable["fakedep"] == "off"
