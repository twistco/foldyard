"""`confighash` — "would `up` recreate it?", asked of the compose provider itself.

The podman path drives the bundled podman-compose's own parser in a subprocess, so these tests
render REAL compose files through the installed podman-compose (no engine: the hash is a pure
function of the rendered config). What they pin is the contract `fy state` leans on: stable for an
unchanged config, moved by an env-only change for exactly the services that interpolate it, blind
to services outside the rendered profiles unless asked. The live e2e tiers close the loop against
labels a real `up` wrote (tests/test_probes_e2e.py on podman, tests/test_e2e.py on docker).
"""

from __future__ import annotations

import os
import types
from pathlib import Path

from foldyard import confighash, stack

_COMPOSE = """\
services:
  api:
    image: example/api
    environment:
      PROJECT: ${GCP_PROJECT:-local}
  db:
    image: example/db
  worker:
    image: example/worker
    profiles: [data]
"""


def _ctx(tmp_path: Path, engine: str = "podman", **env: str) -> stack.Context:
    (tmp_path / "compose.yml").write_text(_COMPOSE)
    return stack.Context(
        main=tmp_path,
        env={"PATH": os.environ.get("PATH", ""), "HOME": str(tmp_path), **env},
        compose=[engine, "compose", "-f", str(tmp_path / "compose.yml")],
        app="api",
        project="proj",
        worktree="",
    )


def test_podman_hashes_are_stable_and_move_only_with_what_a_service_renders(tmp_path):
    base, why = confighash.desired(_ctx(tmp_path))
    assert base is not None, why
    assert set(base) == {"api", "db"}  # `worker` is outside the rendered profiles
    again, _ = confighash.desired(_ctx(tmp_path))
    assert again == base
    moved, _ = confighash.desired(_ctx(tmp_path, GCP_PROJECT="other"))
    assert moved is not None
    assert moved["api"] != base["api"]  # an env-only change, seen
    assert moved["db"] == base["db"]  # …and only where it is interpolated


def test_podman_renders_running_profiles_when_asked(tmp_path):
    hashes, why = confighash.desired(_ctx(tmp_path), extra_profiles=["data"])
    assert hashes is not None, why
    assert set(hashes) == {"api", "db", "worker"}


def test_podman_render_failure_is_unavailable_never_empty(tmp_path):
    ctx = _ctx(tmp_path)
    (tmp_path / "compose.yml").write_text("services: [not, a, mapping")
    hashes, why = confighash.desired(ctx)
    assert hashes is None and why


def test_docker_uses_the_native_hash_command(tmp_path, monkeypatch):
    calls: list[list[str]] = []

    def fake_run(cmd, **kw):
        calls.append(cmd)
        return types.SimpleNamespace(returncode=0, stdout="api 111\ndb 222\n", stderr="")

    monkeypatch.setattr(confighash.subprocess, "run", fake_run)
    hashes, _ = confighash.desired(_ctx(tmp_path, engine="docker"), extra_profiles=["data"])
    assert hashes == {"api": "111", "db": "222"}
    assert calls[-1][-3:] == ["config", "--hash", "*"]
    assert calls[-1][calls[-1].index("--profile") + 1] == "data"


def test_docker_failure_or_garbage_is_unavailable(tmp_path, monkeypatch):
    for proc in (
        types.SimpleNamespace(returncode=1, stdout="", stderr="unknown flag: --hash"),
        types.SimpleNamespace(returncode=0, stdout="", stderr=""),
        types.SimpleNamespace(returncode=0, stdout="no-hash-here\n", stderr=""),
    ):
        monkeypatch.setattr(confighash.subprocess, "run", lambda cmd, _p=proc, **kw: _p)
        hashes, why = confighash.desired(_ctx(tmp_path, engine="docker"))
        assert hashes is None and why, proc


def test_label_names_the_providers_own_hash_label():
    assert confighash.label("podman") == "io.podman.compose.config-hash"
    assert confighash.label("docker") == "com.docker.compose.config-hash"
