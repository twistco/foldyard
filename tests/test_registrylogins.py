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
OPT_IN = registrylogins.OPT_IN


@pytest.fixture
def host(monkeypatch):
    monkeypatch.setattr(config, "in_box", lambda: False)
    monkeypatch.delenv(AUTH, raising=False)
    monkeypatch.delenv(registrylogins.SAVED, raising=False)
    monkeypatch.delenv(OPT_IN, raising=False)
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


def test_the_no_logins_file_is_private_and_left_alone(host):
    path = registrylogins.no_logins_file()
    assert path.stat().st_mode & 0o077 == 0
    before = path.stat().st_ino
    assert registrylogins.no_logins_file() == path
    assert path.stat().st_ino == before  # no write on the hot path


def test_a_login_written_into_the_no_logins_file_doesnt_stay(host):
    """Anything inheriting the clamp that runs `podman login` writes HERE, and every later build
    would then send it."""
    path = registrylogins.no_logins_file()
    path.write_text('{"auths": {"ghcr.io": {"auth": "ZmFrZTpmYWtl"}}}')
    registrylogins.no_logins_file()
    assert json.loads(path.read_text()) == {"auths": {}}
    assert path.stat().st_mode & 0o077 == 0


def test_a_file_that_cant_be_written_still_carries_no_logins(host, monkeypatch):
    """Fails closed: a client pointed at a missing file carries no logins, and doesn't fall back
    to the default paths (tests/test_registrylogins_e2e.py shows that on a real client)."""
    monkeypatch.setattr(registrylogins, "_write", lambda *a: (_ for _ in ()).throw(OSError("ro")))
    monkeypatch.setattr(registrylogins, "_path", lambda: registrylogins.Path("/nonexistent/x.json"))
    env = _clamped(monkeypatch)
    assert env[AUTH] == "/nonexistent/x.json"


def test_a_login_that_cant_be_written_over_is_never_the_answer(host, monkeypatch):
    """A full disk, say. Stopping every `fy` verb over it would be out of proportion when a path
    with nothing there carries no logins either."""
    path = registrylogins.no_logins_file()
    path.write_text('{"auths": {"ghcr.io": {"auth": "ZmFrZTpmYWtl"}}}')
    monkeypatch.setattr(registrylogins, "_write", lambda *a: (_ for _ in ()).throw(OSError("full")))
    answer = registrylogins.no_logins_file()
    assert answer != path
    assert not answer.exists()


def test_a_login_written_into_the_fallback_is_never_handed_out(host, monkeypatch):
    """The fallback is what the client is pointed at, so whatever inherits it and runs `podman
    login` writes there. While the rewrite keeps failing, the next answer must not be that file."""
    path = registrylogins.no_logins_file()
    path.write_text('{"auths": {"ghcr.io": {"auth": "ZmFrZTpmYWtl"}}}')
    monkeypatch.setattr(registrylogins, "_write", lambda *a: (_ for _ in ()).throw(OSError("full")))
    first = registrylogins.no_logins_file()
    first.write_text('{"auths": {"ghcr.io": {"auth": "ZmFrZTpmYWtl"}}}')
    second = registrylogins.no_logins_file()
    assert second != first
    assert not second.exists()


def test_a_child_clamping_during_the_fallback_keeps_your_export(host, monkeypatch):
    """Its fallback isn't its parent's clamp value, and neither is a login of yours: taken for your
    export, it would overwrite the real one and go to your editor."""
    monkeypatch.setenv(AUTH, "/mine/auth.json")
    env = _clamped(monkeypatch)
    path = registrylogins.no_logins_file()
    path.write_text('{"auths": {"ghcr.io": {"auth": "ZmFrZTpmYWtl"}}}')
    monkeypatch.setattr(registrylogins, "_write", lambda *a: (_ for _ in ()).throw(OSError("full")))
    registrylogins.clamp(env)  # the parent's value was the main path; this one's a fallback
    registrylogins.clamp(env)  # and a grandchild's parent held a fallback
    assert env[registrylogins.SAVED] == "/mine/auth.json"
    assert registrylogins.operators_env(env)[AUTH] == "/mine/auth.json"


def test_the_fallback_doesnt_read_as_you_having_logins(host, tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
    path = registrylogins.no_logins_file()
    fallback = path.with_name(f"{path.name}.unwritable.abc")
    fallback.write_text("{}")
    monkeypatch.setenv(AUTH, str(fallback))
    assert registrylogins.failure_hint() == []


# ── the compose client's environment ──────────────────────────────────────────────────────


def test_the_compose_client_gets_no_logins_by_default(host, monkeypatch):
    monkeypatch.setenv(AUTH, "/mine/auth.json")
    assert stack._host_passthrough()[AUTH] == str(registrylogins.no_logins_file())


def test_a_project_that_names_it_gets_the_file_you_exported_for_it(host, monkeypatch):
    """The opt-in: adopted, listed by `fy config widenings`, and only ever a file you named."""
    monkeypatch.setattr(config, "compose_env", lambda: [OPT_IN])
    monkeypatch.setenv(OPT_IN, "/mine/narrow.json")
    assert stack._host_passthrough()[AUTH] == "/mine/narrow.json"


def test_the_opt_in_survives_the_clamp_at_startup(host, monkeypatch):
    monkeypatch.setattr(config, "compose_env", lambda: [OPT_IN])
    monkeypatch.setenv(OPT_IN, "/mine/narrow.json")
    monkeypatch.setenv(AUTH, "/mine/everything.json")
    registrylogins.clamp(os.environ)  # monkeypatch.setenv restores it at teardown
    assert stack._host_passthrough()[AUTH] == "/mine/narrow.json"


def test_a_general_export_is_never_consent(host, monkeypatch):
    """Plenty of people export REGISTRY_AUTH_FILE at their whole auth.json so podman, skopeo and
    buildah share it, and a box can commit one line to `compose_env`, which reads as harmless when
    it is adopted. So naming THAT variable gives nothing: only one that exists for this purpose."""
    monkeypatch.setattr(config, "compose_env", lambda: [AUTH])
    monkeypatch.setenv(AUTH, "/mine/everything.json")
    assert stack._host_passthrough()[AUTH] == str(registrylogins.no_logins_file())


def test_a_project_naming_the_opt_in_without_your_export_gets_no_logins(host, monkeypatch):
    """Never the default paths: that would be your whole auth.json again, by one config line."""
    monkeypatch.setattr(config, "compose_env", lambda: [OPT_IN])
    monkeypatch.setenv(AUTH, "/mine/everything.json")
    assert stack._host_passthrough()[AUTH] == str(registrylogins.no_logins_file())


def test_an_export_a_project_didnt_name_gets_no_logins(host, monkeypatch):
    """Yours to offer and the adopted config's to take: neither alone is enough."""
    monkeypatch.setenv(OPT_IN, "/mine/narrow.json")
    assert stack._host_passthrough()[AUTH] == str(registrylogins.no_logins_file())


def test_the_remembered_path_never_reaches_the_compose_client(host, monkeypatch):
    monkeypatch.setenv(registrylogins.SAVED, "/mine/auth.json")
    assert registrylogins.SAVED not in stack._host_passthrough()


def test_in_the_box_the_compose_client_keeps_the_boxs_own(monkeypatch):
    monkeypatch.setattr(config, "in_box", lambda: True)
    monkeypatch.setattr(config, "compose_env", lambda: [])
    monkeypatch.setenv(AUTH, "/box/auth.json")
    assert stack._host_passthrough()[AUTH] == "/box/auth.json"


# ── what can't be undone by a later merge ─────────────────────────────────────────────────


def _resolved(monkeypatch, tmp_path, **ports) -> dict[str, str]:
    """``stack.resolve``'s env with ``[ports]`` naming ``ports``."""
    monkeypatch.setattr(
        stack, "_context", lambda *a, **k: (tmp_path, {}, _EXPORTED(tmp_path), {**ports})
    )
    monkeypatch.setattr(stack.devmode, "read", lambda: {"mode": {}})
    monkeypatch.setattr(stack.devmode, "derive_env", lambda mode: {})
    monkeypatch.setattr(stack, "_compose_overlays", lambda mode, base: [])
    monkeypatch.setattr(config, "compose_files", lambda: [])
    monkeypatch.setattr(config, "app_service", lambda: "app")
    return stack.resolve().env


def _EXPORTED(tmp_path) -> dict[str, str]:
    return {"FOLDYARD_CHECKOUT": str(tmp_path), "PODMAN_PROJECT": "acme", "WORKTREE": ""}


def test_a_port_named_like_the_auth_file_cant_replace_it(host, tmp_path, monkeypatch):
    """`[ports] REGISTRY_AUTH_FILE = 123` would point the client at a relative `123`, which the box
    can make a link to your auth.json: one adopted line that reads like a port."""
    env = _resolved(monkeypatch, tmp_path, **{AUTH: "123"})
    assert env[AUTH] == str(registrylogins.no_logins_file())


def test_a_port_named_like_the_docker_config_cant_replace_it(host, tmp_path, monkeypatch):
    env = _resolved(monkeypatch, tmp_path, **{registrylogins.DOCKER: "123"})
    assert env[registrylogins.DOCKER] == str(registrylogins.no_logins_docker_config())


def test_the_opt_in_survives_the_merge_too(host, tmp_path, monkeypatch):
    monkeypatch.setattr(config, "compose_env", lambda: [OPT_IN])
    monkeypatch.setenv(OPT_IN, "/mine/narrow.json")
    assert _resolved(monkeypatch, tmp_path, **{AUTH: "123"})[AUTH] == "/mine/narrow.json"


def test_in_the_box_the_merge_is_left_alone(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "in_box", lambda: True)
    monkeypatch.setattr(config, "compose_env", lambda: [])
    monkeypatch.delenv(AUTH, raising=False)
    assert _resolved(monkeypatch, tmp_path, **{AUTH: "123"})[AUTH] == "123"


# ── the docker CLI: its logins are in its own config, which REGISTRY_AUTH_FILE doesn't touch ──


DOCKER = registrylogins.DOCKER


@pytest.fixture
def docker_home(host, tmp_path, monkeypatch):
    """Your ~/.docker with logins, a current context, contexts and plugins."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv(DOCKER, raising=False)
    monkeypatch.delenv(registrylogins.SAVED_DOCKER, raising=False)
    own = tmp_path / ".docker"
    (own / "contexts").mkdir(parents=True)
    (own / "config.json").write_text(
        json.dumps(
            {
                "auths": {"ghcr.io": {"auth": "ZmFrZTpmYWtl"}},
                "credsStore": "desktop",
                "currentContext": "desktop-linux",
            }
        )
    )
    return own


def _docker_config(env_dir) -> dict:
    return json.loads((registrylogins.Path(env_dir) / "config.json").read_text())


def test_the_clamp_points_docker_at_a_config_with_no_logins(docker_home, monkeypatch):
    env = _clamped(monkeypatch)
    cfg = _docker_config(env[DOCKER])
    assert "credsStore" not in cfg and "credHelpers" not in cfg
    assert not any(entry for entry in cfg["auths"].values())  # names, and nothing to send


def test_the_clean_docker_config_doesnt_read_as_empty(docker_home, monkeypatch):
    """With no entry at all the docker CLI goes looking for the platform's credential store, and a
    build sends every login in it: tests/test_registrylogins_e2e.py shows it on a real client."""
    assert _docker_config(_clamped(monkeypatch)[DOCKER])["auths"]


def test_the_clean_config_keeps_your_plugins_contexts_and_current_context(docker_home, monkeypatch):
    """Without these `docker compose` is "unknown command": the plugin lives in the config dir."""
    env = _clamped(monkeypatch)
    cfg = _docker_config(env[DOCKER])
    assert cfg["cliPluginsExtraDirs"] == [str(docker_home / "cli-plugins")]
    assert cfg["currentContext"] == "desktop-linux"
    assert "credsStore" not in cfg
    assert (registrylogins.Path(env[DOCKER]) / "contexts").resolve() == docker_home / "contexts"


def test_your_own_docker_config_is_remembered_and_read(docker_home, tmp_path, monkeypatch):
    mine = tmp_path / "elsewhere"
    mine.mkdir()
    monkeypatch.setenv(DOCKER, str(mine))
    env = _clamped(monkeypatch)
    assert env[registrylogins.SAVED_DOCKER] == str(mine)
    registrylogins.clamp(env)  # a child, under the clamped value
    assert env[registrylogins.SAVED_DOCKER] == str(mine)
    assert _docker_config(env[DOCKER])["cliPluginsExtraDirs"] == [str(mine / "cli-plugins")]


def test_a_docker_login_under_the_clamp_doesnt_stay(docker_home, monkeypatch):
    env = _clamped(monkeypatch)
    cfg = registrylogins.Path(env[DOCKER]) / "config.json"
    cfg.write_text('{"auths": {"ghcr.io": {"auth": "ZmFrZTpmYWtl"}}}')
    registrylogins.clamp(env)
    assert "ghcr.io" not in _docker_config(env[DOCKER])["auths"]


def test_a_docker_config_that_cant_be_put_right_is_never_the_answer(docker_home, monkeypatch):
    env = _clamped(monkeypatch)
    cfg = registrylogins.Path(env[DOCKER]) / "config.json"
    cfg.write_text('{"auths": {"ghcr.io": {"auth": "ZmFrZTpmYWtl"}}}')
    monkeypatch.setattr(registrylogins, "_write", lambda *a: (_ for _ in ()).throw(OSError("full")))
    first = registrylogins.no_logins_docker_config()
    second = registrylogins.no_logins_docker_config()
    assert first != registrylogins.Path(env[DOCKER])
    assert first != second
    assert not first.exists()


def test_your_own_extra_plugin_dirs_come_too(docker_home, monkeypatch):
    """A compose plugin can live only in one of them, and then it would be an unknown command."""
    own = docker_home / "config.json"
    data = json.loads(own.read_text())
    data["cliPluginsExtraDirs"] = ["/opt/docker-plugins", str(docker_home / "cli-plugins"), 7]
    own.write_text(json.dumps(data))
    cfg = _docker_config(_clamped(monkeypatch)[DOCKER])
    assert cfg["cliPluginsExtraDirs"] == [str(docker_home / "cli-plugins"), "/opt/docker-plugins"]


def test_an_unreadable_docker_config_of_yours_is_only_no_current_context(docker_home, monkeypatch):
    (docker_home / "config.json").write_text("not json")
    env = _clamped(monkeypatch)
    assert "currentContext" not in _docker_config(env[DOCKER])


def test_the_compose_client_gets_the_clean_docker_config(docker_home, monkeypatch):
    monkeypatch.setenv(DOCKER, str(docker_home))
    assert stack._host_passthrough()[DOCKER] == str(registrylogins.no_logins_docker_config())


def test_naming_the_docker_config_in_compose_env_gives_nothing(docker_home, monkeypatch):
    """The opt-in is `podman`'s: a box can propose this line as easily as any."""
    monkeypatch.setattr(config, "compose_env", lambda: [DOCKER])
    monkeypatch.setenv(DOCKER, str(docker_home))
    assert stack._host_passthrough()[DOCKER] == str(registrylogins.no_logins_docker_config())


def test_in_the_box_docker_is_left_alone(monkeypatch):
    monkeypatch.setattr(config, "in_box", lambda: True)
    monkeypatch.setattr(config, "compose_env", lambda: [])
    monkeypatch.setenv(DOCKER, "/box/.docker-fy")
    assert stack._host_passthrough()[DOCKER] == "/box/.docker-fy"
    assert _clamped(monkeypatch)[DOCKER] == "/box/.docker-fy"


def test_the_editor_gets_your_docker_config_back_too(docker_home, tmp_path, monkeypatch):
    mine = tmp_path / "elsewhere"
    mine.mkdir()
    monkeypatch.setenv(DOCKER, str(mine))
    env = _clamped(monkeypatch)
    assert registrylogins.operators_env(env)[DOCKER] == str(mine)
    assert registrylogins.SAVED_DOCKER not in registrylogins.operators_env(env)


def test_the_editor_gets_no_docker_config_if_you_set_none(docker_home, monkeypatch):
    assert DOCKER not in registrylogins.operators_env(_clamped(monkeypatch))


# ── which files are ours ──────────────────────────────────────────────────────────────────


def test_a_file_of_yours_with_a_like_name_elsewhere_is_yours(host, tmp_path, monkeypatch):
    """Matching the name alone would lose your export, and hide it from the hint."""
    theirs = tmp_path / "no-registry-logins.json.backup"
    monkeypatch.setenv(AUTH, str(theirs))
    env = _clamped(monkeypatch)
    assert env[registrylogins.SAVED] == str(theirs)
    assert registrylogins.operators_env(env)[AUTH] == str(theirs)


# ── saying so ─────────────────────────────────────────────────────────────────────────────


def test_a_failure_names_the_logins_only_when_you_have_some(host, tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
    assert registrylogins.failure_hint() == []
    (tmp_path / ".docker").mkdir()
    (tmp_path / ".docker" / "config.json").write_text("{}")
    hint = "\n".join(registrylogins.failure_hint())
    assert "compose_env" in hint and OPT_IN in hint


def test_an_export_this_project_doesnt_name_still_gets_the_hint(host, tmp_path, monkeypatch):
    """Half an opt-in gives no logins, so it's the case most in need of being told."""
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / ".docker").mkdir()
    (tmp_path / ".docker" / "config.json").write_text("{}")
    monkeypatch.setenv(OPT_IN, "/mine/narrow.json")
    assert OPT_IN in "\n".join(registrylogins.failure_hint())


def test_the_clamp_itself_doesnt_read_as_you_having_logins(host, tmp_path, monkeypatch):
    """After the clamp REGISTRY_AUTH_FILE names the empty file, which is no login of yours, or every
    failed build would tell people with no logins about them."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
    monkeypatch.setenv(AUTH, str(registrylogins.no_logins_file()))
    assert registrylogins.failure_hint() == []


def test_a_project_already_opted_in_isnt_told_to(host, tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / ".docker").mkdir()
    (tmp_path / ".docker" / "config.json").write_text("{}")
    monkeypatch.setattr(config, "compose_env", lambda: [OPT_IN])
    monkeypatch.setenv(OPT_IN, "/mine/narrow.json")
    assert registrylogins.failure_hint() == []


def test_a_project_that_names_it_tells_whoever_hasnt_exported_it(host, tmp_path, monkeypatch):
    """A teammate on the same adopted config: no export means no logins, so the failure is theirs
    to fix, and this is how they find out."""
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / ".docker").mkdir()
    (tmp_path / ".docker" / "config.json").write_text("{}")
    monkeypatch.setattr(config, "compose_env", lambda: [OPT_IN])
    assert OPT_IN in "\n".join(registrylogins.failure_hint())


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
