"""foldyard's clients on your computer carry no registry logins (`foldyard.registrylogins`).

A `podman` client sends the logins in its auth file to the API service, and that service is in the
VM: a build sends all of them, and the service writes them to a temp file a box can read through a
bind mount. So the client is pointed at an empty file, unless the project opted in. The live proof,
with a real client and a real VM, is tests/test_registrylogins_e2e.py.
"""

from __future__ import annotations

import json
import os
import subprocess

import pytest

from foldyard import config, registrylogins, stack

AUTH = "REGISTRY_AUTH_FILE"


@pytest.fixture
def host(monkeypatch):
    monkeypatch.setattr(config, "in_box", lambda: False)
    monkeypatch.delenv(AUTH, raising=False)
    monkeypatch.delenv(registrylogins.SAVED, raising=False)
    monkeypatch.setattr(config, "compose_env", lambda: [])


def _clamped(monkeypatch) -> dict[str, str]:
    """``clamp`` over a copy of the environment, so a test can't leak it into the next."""
    env = dict(os.environ)
    registrylogins.clamp(env)
    return env


def test_the_clamp_points_the_client_at_a_file_with_no_logins(host, monkeypatch):
    env = _clamped(monkeypatch)
    with open(env[AUTH]) as f:
        assert json.load(f) == {"auths": {}}


def test_the_clamp_wins_over_your_own_export_and_remembers_it(host, monkeypatch):
    monkeypatch.setenv(AUTH, "/mine/auth.json")
    env = _clamped(monkeypatch)
    assert env[AUTH] != "/mine/auth.json"
    assert env[registrylogins.SAVED] == "/mine/auth.json"


def test_a_child_clamping_again_keeps_your_export(host, monkeypatch):
    """The supervisor is a later `fy` under an already-clamped environment."""
    monkeypatch.setenv(AUTH, "/mine/auth.json")
    first = _clamped(monkeypatch)
    registrylogins.clamp(first)
    assert first[registrylogins.SAVED] == "/mine/auth.json"


def test_in_the_box_nothing_is_touched(monkeypatch):
    monkeypatch.setattr(config, "in_box", lambda: True)
    monkeypatch.setenv(AUTH, "/box/auth.json")
    assert _clamped(monkeypatch)[AUTH] == "/box/auth.json"


def test_the_no_logins_file_is_private_and_kept(host):
    path = registrylogins.no_logins_file()
    assert path.stat().st_mode & 0o077 == 0
    path.write_text('{"auths": {}, "kept": true}')
    assert registrylogins.no_logins_file() == path
    assert "kept" in path.read_text()


def test_a_file_that_cant_be_written_still_carries_no_logins(host, monkeypatch):
    """Fails closed: a client pointed at a missing file refuses, loudly, rather than falling back
    to the logins in the default paths."""
    monkeypatch.setattr(registrylogins, "_write", lambda path: (_ for _ in ()).throw(OSError("ro")))
    monkeypatch.setattr(registrylogins, "_path", lambda: registrylogins.Path("/nonexistent/x.json"))
    env = _clamped(monkeypatch)
    assert env[AUTH] == "/nonexistent/x.json"


# ── the compose client's environment ──────────────────────────────────────────────────────


def test_the_compose_client_gets_no_logins_by_default(host, monkeypatch):
    monkeypatch.setenv(AUTH, "/mine/auth.json")
    assert stack._host_passthrough()[AUTH] == str(registrylogins.no_logins_file())


def test_a_project_that_names_it_gets_the_file_you_exported(host, monkeypatch):
    """The opt-in: adopted, listed by `fy config widenings`, and only ever a file you named."""
    monkeypatch.setattr(config, "compose_env", lambda: [AUTH])
    monkeypatch.setenv(AUTH, "/mine/narrow.json")
    assert stack._host_passthrough()[AUTH] == "/mine/narrow.json"


def test_the_opt_in_survives_the_clamp_at_startup(host, monkeypatch):
    monkeypatch.setattr(config, "compose_env", lambda: [AUTH])
    monkeypatch.setenv(AUTH, "/mine/narrow.json")
    registrylogins.clamp(os.environ)  # monkeypatch.setenv restores both names at teardown
    monkeypatch.setenv(registrylogins.SAVED, os.environ[registrylogins.SAVED])
    assert stack._host_passthrough()[AUTH] == "/mine/narrow.json"


def test_naming_it_without_exporting_one_is_still_no_logins(host, monkeypatch):
    """Never the default paths: that would be your whole auth.json again, by one config line."""
    monkeypatch.setattr(config, "compose_env", lambda: [AUTH])
    assert stack._host_passthrough()[AUTH] == str(registrylogins.no_logins_file())


def test_the_remembered_path_never_reaches_the_compose_client(host, monkeypatch):
    monkeypatch.setenv(registrylogins.SAVED, "/mine/auth.json")
    assert registrylogins.SAVED not in stack._host_passthrough()


def test_in_the_box_the_compose_client_keeps_the_boxs_own(monkeypatch):
    monkeypatch.setattr(config, "in_box", lambda: True)
    monkeypatch.setattr(config, "compose_env", lambda: [])
    monkeypatch.setenv(AUTH, "/box/auth.json")
    assert stack._host_passthrough()[AUTH] == "/box/auth.json"


# ── saying so ─────────────────────────────────────────────────────────────────────────────


def test_a_failure_names_the_logins_only_when_you_have_some(host, tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
    assert registrylogins.failure_hint() == []
    (tmp_path / ".docker").mkdir()
    (tmp_path / ".docker" / "config.json").write_text("{}")
    hint = "\n".join(registrylogins.failure_hint())
    assert "compose_env" in hint and AUTH in hint


def test_a_project_already_opted_in_isnt_told_to(host, tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / ".docker").mkdir()
    (tmp_path / ".docker" / "config.json").write_text("{}")
    monkeypatch.setattr(config, "compose_env", lambda: [AUTH])
    assert registrylogins.failure_hint() == []


def test_a_failed_stack_build_says_so(host, tmp_path, monkeypatch, capfd):
    monkeypatch.setattr(registrylogins, "failure_hint", lambda: ["the logins hint"])
    monkeypatch.setattr(
        stack.subprocess, "run", lambda cmd, **kw: subprocess.CompletedProcess(cmd, 1)
    )
    rendered = {"services": {"api": {"build": {"context": str(tmp_path)}}}}
    monkeypatch.setattr(config, "state_dir", lambda: tmp_path / "state")
    monkeypatch.setattr(stack, "_image_id", lambda *a: None)
    monkeypatch.setattr(stack, "_escapes", lambda *a, **k: [])
    import yaml

    monkeypatch.setattr(
        stack,
        "_run",
        lambda cmd, **kw: subprocess.CompletedProcess(cmd, 0, yaml.safe_dump(rendered), ""),
    )
    ctx = stack.Context(
        tmp_path, {"PODMAN_PROJECT": "acme"}, ["podman", "compose"], "app", "acme", ""
    )
    assert stack._podman_build(ctx, None) == 1
    assert "the logins hint" in capfd.readouterr().err


# ── what isn't foldyard's to clamp ────────────────────────────────────────────────────────


def test_the_operators_view_gives_your_editor_your_own_setting_back(host, monkeypatch):
    monkeypatch.setenv(AUTH, "/mine/auth.json")
    env = _clamped(monkeypatch)
    assert registrylogins.operators_env(env)[AUTH] == "/mine/auth.json"
    assert registrylogins.SAVED not in registrylogins.operators_env(env)


def test_the_operators_view_drops_the_clamp_when_you_set_nothing(host, monkeypatch):
    env = _clamped(monkeypatch)
    assert AUTH not in registrylogins.operators_env(env)


def test_the_entry_point_clamps_before_any_verb(host, monkeypatch):
    from foldyard import cli

    order = []
    monkeypatch.setattr(cli, "app", lambda: order.append("verb"))
    monkeypatch.setattr(registrylogins, "clamp", lambda env=None: order.append("clamp"))
    cli.main()
    assert order == ["clamp", "verb"]


def test_a_failed_box_image_build_says_so(host, tmp_path, monkeypatch, capfd):
    from foldyard import box

    monkeypatch.setattr(registrylogins, "failure_hint", lambda: ["the logins hint"])
    monkeypatch.setattr(box, "_gated_build", lambda *a, **k: 1)
    monkeypatch.setattr(box, "_box_image_fingerprint", lambda main: "fp")
    assert box._build_img("podman", tmp_path, {}) == 1
    assert "the logins hint" in capfd.readouterr().err
