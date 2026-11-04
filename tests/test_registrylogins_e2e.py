"""foldyard's clients send no registry logins: a real client, and a real `fy build` on a real VM.

The hermetic half is tests/test_registrylogins.py. What only the real thing shows: that
`REGISTRY_AUTH_FILE` beats podman's default paths, and that a login really does land where a box can
read it, without which the clean results here would mean nothing, so each has its positive control.
All the logins here are fake.

Host tier (tests/e2e_host.py).
"""

from __future__ import annotations

import base64
import json
import os
import shutil
import socket
import subprocess
import tempfile
import threading
import time
from pathlib import Path

import pytest

from e2e_host import (
    _adopt_on_host,
    _engine,
    engine,
    ensure_vm,
    example_copy,
    fy,
    fy_ok,
    heartbeat_file,
    host_tier,
    state_dir,
    wait_for,
)
from foldyard import registrylogins

pytestmark = host_tier

NEEDED = "fake-needed-not-a-secret"
UNRELATED = "fake-UNRELATED-not-a-secret"
WATCHER = "fy-loginwatch"


def _auth_file(path: Path) -> Path:
    def login(user: str, secret: str) -> dict[str, str]:
        return {"auth": base64.b64encode(f"{user}:{secret}".encode()).decode()}

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "auths": {
                    "registry.invalid": login("fakeuser", NEEDED),
                    "unrelated.invalid": login("fakeother", UNRELATED),
                }
            }
        )
    )
    return path


class _LoggingApi:
    """A listener where the API socket would be: keeps every X-Registry-* header, decoded."""

    def __init__(self, path: Path) -> None:
        self.headers: list[str] = []
        self.sock = socket.socket(socket.AF_UNIX)
        self.sock.bind(str(path))
        self.sock.listen(8)
        threading.Thread(target=self._serve, daemon=True).start()

    @staticmethod
    def _headers(conn: socket.socket) -> str:
        """Up to the blank line: one recv can stop short of it, and a header missed would read as
        a login not sent. The body (a build's context) isn't needed."""
        data = b""
        while b"\r\n\r\n" not in data:
            chunk = conn.recv(65536)
            if not chunk:
                break
            data += chunk
        return data.decode(errors="replace")

    def _serve(self) -> None:
        version = json.dumps(
            {
                "Version": "6.0.2",
                "ApiVersion": "1.41",
                "MinAPIVersion": "1.24",
                "Components": [
                    {
                        "Name": "Podman Engine",
                        "Version": "6.0.2",
                        "Details": {"APIVersion": "6.0.2", "MinAPIVersion": "4.0.0"},
                    }
                ],
            }
        )
        while True:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            data = self._headers(conn)
            for header in data.split("\r\n"):
                if header.lower().startswith("x-registry"):
                    value = header.split(":", 1)[1].strip()
                    self.headers.append(base64.urlsafe_b64decode(value + "==").decode())
            if "/_ping" in data.split("\r\n")[0] or "/version" in data.split("\r\n")[0]:
                reply = (
                    "HTTP/1.1 200 OK\r\nLibpod-API-Version: 6.0.2\r\nAPI-Version: 1.41\r\n"
                    f"Content-Type: application/json\r\nContent-Length: {len(version)}\r\n"
                    f"Connection: close\r\n\r\n{version}"
                )
            else:
                reply = "HTTP/1.1 500 Oops\r\nContent-Length: 2\r\nConnection: close\r\n\r\n{}"
            conn.sendall(reply.encode())
            conn.close()


@pytest.fixture
def api():
    # AF_UNIX paths are short, and a pytest tmp_path can be longer than that allows.
    d = Path(tempfile.mkdtemp(prefix="fyl", dir="/tmp"))
    yield d, _LoggingApi(d / "a.sock")
    shutil.rmtree(d, ignore_errors=True)


def _client(d: Path, env: dict[str, str], *args: str) -> None:
    (d / "ctx").mkdir(exist_ok=True)
    (d / "ctx" / "Dockerfile").write_text("FROM registry.invalid/fy/base:1\n")
    subprocess.run(
        ["podman", "--url", f"unix://{d}/a.sock", *args],
        env=env,
        cwd=str(d / "ctx"),
        capture_output=True,
        timeout=60,
    )


def _host_env(**extra: str) -> dict[str, str]:
    keep = ("PATH", "HOME", "USER", "TMPDIR")
    return {**{k: os.environ[k] for k in keep if k in os.environ}, **extra}


@pytest.mark.parametrize(
    "args", [("pull", "registry.invalid/fy/base:1"), ("build", ".")], ids=["pull", "build"]
)
def test_an_auth_file_sends_its_logins_to_the_api_service(api, args):
    """The positive control, and what a build costs: all of them, not only the one it needs."""
    d, listener = api
    _client(d, _host_env(REGISTRY_AUTH_FILE=str(_auth_file(d / "auth.json"))), *args)
    sent = "".join(listener.headers)
    assert NEEDED in sent, listener.headers
    if args[0] == "build":
        assert UNRELATED in sent


@pytest.mark.parametrize(
    "args", [("pull", "registry.invalid/fy/base:1"), ("build", ".")], ids=["pull", "build"]
)
def test_the_clamped_client_sends_none_even_with_logins_in_a_default_path(api, args):
    d, listener = api
    # The default path both macOS and Linux read (macOS has no XDG_RUNTIME_DIR one).
    _auth_file(d / "home" / ".config" / "containers" / "auth.json")
    env = {**_host_env(), "HOME": str(d / "home")}
    _client(d, env, *args)
    assert NEEDED in "".join(listener.headers), "the default path wasn't read: no control"
    listener.headers.clear()
    registrylogins.clamp(env)
    _client(d, env, *args)
    assert NEEDED not in "".join(listener.headers), listener.headers
    assert UNRELATED not in "".join(listener.headers), listener.headers


@pytest.mark.parametrize(
    "args", [("pull", "registry.invalid/fy/base:1"), ("build", ".")], ids=["pull", "build"]
)
def test_a_client_pointed_at_a_missing_file_sends_none(api, args):
    """What `no_logins_file` relies on when it can't write: the variable alone decides, so a missing
    file means no logins, never a fall back to the default paths. (The client carries on without
    them: it doesn't refuse.)"""
    d, listener = api
    _auth_file(d / "home" / ".config" / "containers" / "auth.json")
    env = {**_host_env(), "HOME": str(d / "home")}
    _client(d, env, *args)
    assert NEEDED in "".join(listener.headers), "the default path wasn't read: no control"
    listener.headers.clear()
    _client(d, {**env, "REGISTRY_AUTH_FILE": str(d / "missing.json")}, *args)
    assert NEEDED not in "".join(listener.headers), listener.headers
    assert UNRELATED not in "".join(listener.headers), listener.headers


# ── the real thing, seen from where a box stands ──────────────────────────────────────────

_WATCH = (
    'end=$(($(date +%s)+{secs})); while [ "$(date +%s)" -lt "$end" ]; do '
    "grep -rlE 'ZmFrZXVzZXI6|ZmFrZW90aGVy|fake-needed' /v 2>/dev/null; sleep 0.2; done; echo done"
)


def _watch(secs: int) -> None:
    engine("rm", "-f", WATCHER)
    started = engine(
        "run", "-d", "--name", WATCHER, "--security-opt", "label=disable",
        "-v", "/var/tmp:/v:ro", "docker.io/library/alpine", "sh", "-c", _WATCH.format(secs=secs),
    )  # fmt: skip
    assert started.returncode == 0, started.stderr
    time.sleep(1)


def _seen() -> list[str]:
    engine("wait", WATCHER, timeout=120)
    out = engine("logs", WATCHER).stdout.split()
    engine("rm", "-f", WATCHER)
    assert out and out[-1] == "done", out
    return sorted({line for line in out[:-1] if "auth.json" in line})


@pytest.fixture(scope="module")
def repo(tmp_path_factory):
    r = example_copy(tmp_path_factory.mktemp("logins"))
    _adopt_on_host(r)
    ensure_vm(r)
    yield r
    fy(["down"], r, timeout=180)


def _slow_build(repo: Path, nonce: str) -> None:
    """A build that really runs for a while: a new last layer, whatever the cache holds."""
    with (repo / "api" / "Dockerfile").open("a") as f:
        f.write(f"\nRUN echo {nonce} && sleep 12\n")


def test_a_box_can_read_a_login_the_client_sends(repo, tmp_path):
    """The positive control, from a box's position: the same client, not clamped."""
    auth = _auth_file(tmp_path / "auth.json")
    (tmp_path / "ctx").mkdir()
    (tmp_path / "ctx" / "Dockerfile").write_text(
        f"FROM docker.io/library/alpine\nRUN echo {time.time()} && sleep 12\n"
    )
    _watch(20)
    env = {**os.environ, "REGISTRY_AUTH_FILE": str(auth)}
    subprocess.run(
        [_engine(), "build", "-q", "--no-cache", "-t", "fy-loginprobe", "."],
        cwd=str(tmp_path / "ctx"),
        env=env,
        capture_output=True,
        timeout=120,
    )
    seen = _seen()
    engine("rmi", "-f", "fy-loginprobe")
    assert seen, "no login reached /var/tmp: this module's clean result below proves nothing"


def test_fy_build_leaves_none_there_with_your_export_set(repo, tmp_path):
    _slow_build(repo, f"fy-{time.time()}")
    auth = _auth_file(tmp_path / "auth.json")
    _watch(25)
    built = fy(["build"], repo, timeout=600, env_extra={"REGISTRY_AUTH_FILE": str(auth)})
    assert built.rc == 0, built.out
    assert _seen() == []


def test_a_docker_engine_is_refused_and_can_be_undone(tmp_path_factory):
    """One adopted line a box can propose. The refusal must leave the way back open: `fy config`
    reads no engine, or a bad adoption could never be reverted."""
    r = example_copy(tmp_path_factory.mktemp("docker"), extra_toml='\n[engine]\ncli = "docker"\n')
    _adopt_on_host(r)
    from_config = {"FOLDYARD_ENGINE": ""}  # the test runner names podman, and the variable wins
    built = fy(["build"], r, timeout=120, env_extra=from_config)
    assert built.rc != 0, built.out
    assert "docker" in built.out and "podman" in built.out
    assert "Traceback" not in built.out, built.out
    assert fy(["config", "diff"], r, timeout=60, env_extra=from_config).rc == 0
    toml = r / "foldyard.toml"
    toml.write_text(toml.read_text().replace('\n[engine]\ncli = "docker"\n', "\n"))
    _adopt_on_host(r)
    assert fy(["build"], r, timeout=600, env_extra=from_config).rc == 0


def _still_ticking() -> None:
    beat = heartbeat_file().stat().st_mtime
    wait_for(
        lambda: heartbeat_file().stat().st_mtime > beat + 4,  # some ticks on
        timeout=30,
        what="the supervisor still ticking",
    )


def test_a_supervisor_outlives_the_engine_being_refused_under_it(tmp_path_factory):
    """The same line, adopted while a supervisor is running. It is all that expires a mode or stops
    a daemon, so it has to stay, say why, and let `fy doctor` finish."""
    r = example_copy(tmp_path_factory.mktemp("supervised"))
    _adopt_on_host(r)
    from_config = {"FOLDYARD_ENGINE": ""}
    fy_ok(["host", "restart"], r, timeout=300, env_extra=from_config)
    log = state_dir() / "host-supervisor.log"
    toml = r / "foldyard.toml"
    original = toml.read_text()
    toml.write_text(original + '\n[engine]\ncli = "docker"\n')
    try:
        _adopt_on_host(r)
        said = "the engine is refused"
        wait_for(
            lambda: log.exists() and said in log.read_text(errors="replace"),
            timeout=30,
            what="the supervisor saying the engine is refused",
        )
        _still_ticking()
        # One started now reads the refused engine outside any binding, on its first tick: the
        # way in a running one is spared (its ambient config is cached), so what proves it stays.
        restarted = fy(["host", "restart"], r, timeout=120, env_extra=from_config)
        assert "Traceback" not in restarted.out, restarted.out
        _still_ticking()
        doctor = fy(["doctor"], r, timeout=120, env_extra=from_config)
        assert "Traceback" not in doctor.out, doctor.out
        assert "podman" in doctor.out and "adopted config" in doctor.out, doctor.out
    finally:
        toml.write_text(original)
        _adopt_on_host(r)
    fy_ok(["host", "restart"], r, timeout=300, env_extra=from_config)
