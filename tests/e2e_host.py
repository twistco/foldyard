"""Shared substrate for the HOST-tier live e2es (`tests/test_*_e2e.py` that need a real VM).

The host tier is foldyard's product path: an operator's machine running the default `lima`
backend, where `fy up` provisions/boots a VM, the config-adopt gate and the supervisor run for
real, and `verify` audits a real VM boundary. CI runs it on an `ubuntu-24.04` runner
(`lima-host-e2e` in .github/workflows/foldyard-e2e.yml — `/dev/kvm` + QEMU + Lima; the record is
in docs/linux-support.md). The gate is deliberately strict — `FOLDYARD_E2E=1`, NOT in a box,
`limactl` on PATH, the lima backend — so these are skipped (not failed) by the container mirror
(`live-e2e`, `IN_DEVBOX=1`), the dev box, and any host without Lima. On a Lima host they drive the
example's own VM (`[machine].name = "foldyard-example"`): they create it if absent, restart it,
recreate it, and mount things into it — never a consumer's VM.

Every module here reuses `tests/test_e2e.py`'s runner (`_foldyard`: the real CLI as a subprocess
with the example copy as the repo root) and its engine helpers, and follows its rules: a
throwaway git copy of `example/` per module, adopted; `fy down` in teardown; the VM left RUNNING
and un-walled so the next module (alphabetical, in one pytest process) starts from the same state.
Versions asserted against are named in each module's docstring.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

import pytest

from test_e2e import (  # noqa: F401  (re-exported for the host-tier modules)
    EXAMPLE,
    NETWORK,
    PROJECT,
    _adopt_on_host,
    _engine,
    _engine_env,
    _foldyard,
    _probe_db,
    _service_is_listed,
)

VM = "foldyard-example"  # example/foldyard.toml [machine].name
LIMA_DIR = Path.home() / ".lima" / VM
LIMA_SOCK = LIMA_DIR / "sock" / "podman.sock"


def host_tier_available() -> bool:
    """See the module docstring: opt-in, host-side, lima. IN_DEVBOX is read here at import,
    before conftest's autouse scrub strips it (same trick as test_e2e's `_IN_BOX`)."""
    return (
        os.environ.get("FOLDYARD_E2E") == "1"
        and os.environ.get("IN_DEVBOX") != "1"
        and shutil.which("limactl") is not None
        and (os.environ.get("MACHINE_BACKEND") or "lima") == "lima"
    )


host_tier = pytest.mark.skipif(
    not host_tier_available(),
    reason="host tier: FOLDYARD_E2E=1 on a host (not a box) with limactl — the lima backend",
)


def state_dir() -> Path:
    """The example project's host state dir, as `config.state_dir()` resolves it (FOLDYARD_STATE_DIR
    wins — CI sets it — else `~/.foldyard/<project name>`)."""
    env = os.environ.get("FOLDYARD_STATE_DIR")
    return Path(env).expanduser().resolve() if env else Path.home() / ".foldyard" / VM


def heartbeat_file() -> Path:
    return state_dir() / "host-supervisor.heartbeat"


def heartbeat_age() -> float | None:
    try:
        return time.time() - heartbeat_file().stat().st_mtime
    except OSError:
        return None


def wait_for(predicate, timeout: float = 60, every: float = 1.0, what: str = "condition"):
    """Poll `predicate()` until truthy; return its value. AssertionError with `what` on timeout."""
    deadline = time.time() + timeout
    while True:
        value = predicate()
        if value:
            return value
        if time.time() > deadline:
            raise AssertionError(f"timed out after {timeout}s waiting for {what}")
        time.sleep(every)


def example_copy(base: Path, extra_toml: str = "") -> Path:
    """A throwaway git copy of example/ (preflight refuses the nested project inside the foldyard
    checkout; `main_repo()` shells git), optionally with `extra_toml` appended to foldyard.toml,
    then ADOPTED — the launch gate refuses an unadopted checkout with no terminal (ADR-0022)."""
    dst = base / "example"
    shutil.copytree(EXAMPLE, dst)
    if extra_toml:
        with (dst / "foldyard.toml").open("a") as fh:
            fh.write(extra_toml)
    subprocess.run(["git", "init", "-q"], cwd=str(dst), check=True)
    subprocess.run(["git", "add", "-A"], cwd=str(dst), check=True)
    subprocess.run(
        ["git", "-c", "user.email=e2e@foldyard", "-c", "user.name=e2e", "commit", "-qm", "init"],
        cwd=str(dst),
        check=True,
    )
    return dst


@dataclass
class Run:
    """One CLI invocation: exit code + stdout AND stderr together (foldyard reports on both)."""

    rc: int
    out: str


def fy(
    args: list[str], repo: Path, timeout: int = 900, env_extra: dict[str, str] | None = None
) -> Run:
    """The real CLI against `repo` (test_e2e's runner)."""
    result = _foldyard(args, repo, timeout=timeout, env_extra=env_extra)
    return Run(result.returncode, result.stdout + result.stderr)


def fy_ok(
    args: list[str], repo: Path, timeout: int = 900, env_extra: dict[str, str] | None = None
) -> Run:
    run = fy(args, repo, timeout=timeout, env_extra=env_extra)
    assert run.rc == 0, f"fy {' '.join(args)} failed (rc={run.rc}):\n{run.out}"
    return run


def export_vm_socket() -> None:
    """Point the tests' OWN engine calls at the VM's forwarded socket for the rest of the
    process — the lima backend registers no podman connection (unlike `podman machine init`), so
    a bare `podman` would talk to the host's own store. CONTAINER_HOST only, never DOCKER_HOST:
    the CLI under test treats a preset DOCKER_HOST as "the socket is someone else's business"
    (`stack._docker_host`: dev box / pre-exported) and SKIPS `machine.ensure` — the start-after-
    stop, the provisioning record, the host wall — which is exactly the path these tests exist
    to drive. `test_e2e._engine_env` mirrors DOCKER_HOST→CONTAINER_HOST, not the reverse, so
    the subprocess CLI sees no DOCKER_HOST and resolves the socket itself, and podman (which
    reads CONTAINER_HOST) reaches the VM for the tests' probes. Idempotent; a preset wins."""
    os.environ.setdefault("CONTAINER_HOST", f"unix://{LIMA_SOCK}")


def ensure_vm(repo: Path, env_extra: dict[str, str] | None = None, warm: float = 90) -> None:
    """`fy machine ensure` from `repo` (creates the VM on first use, starts it if stopped, no-op
    when running), export its socket, then wait until the engine can actually RUN a container.

    `ensure` returns when the forwarded socket accepts, which after a (re)boot is earlier than
    the guest's rootless podman can start a container (the verify battery's positive control —
    `podman run --rm alpine true` — failed for a moment after every restart, 2026-09-17). A real
    operator never notices: `fy up` builds for 30 s first. The tests do, so warm here, and say
    how long it took when it was not immediate."""
    fy_ok(["machine", "ensure"], repo, timeout=900, env_extra=env_extra)
    export_vm_socket()
    start = time.time()
    last = None
    while time.time() - start < warm:
        last = engine("run", "--rm", "docker.io/library/alpine", "true", timeout=60)
        if last.returncode == 0:
            waited = time.time() - start
            if waited > 3:
                print(f"(engine could run a container {waited:.0f}s after ensure returned)")
            return
        time.sleep(2)
    raise AssertionError(
        f"engine accepted connections but could not run a container within {warm}s of ensure:\n"
        f"{last.stderr if last else ''}\n--- inside the guest ---\n{guest_diagnostics()}"
    )


def guest_diagnostics() -> str:
    """What the guest itself says when the engine misbehaves — run natively over `limactl
    shell`, not through the forwarded socket, so a remote-only symptom is told apart from a
    broken engine."""
    probes = (
        "id; echo HOME=$HOME; uptime",
        "podman info --format '{{.Store.GraphDriverName}} graph={{.Store.GraphRoot}} "
        "run={{.Store.RunRoot}} status={{json .Store.GraphStatus}}'",
        "podman images --format '{{.Repository}}:{{.Tag}} {{.Id}}'",
        "podman run --rm docker.io/library/alpine true && echo NATIVE-RUN-OK",
        "mount | grep -E ' / | /home|overlay|9p|virtiofs' | head -20",
        "ls -la ~/.local/share/containers/storage/overlay/ | head -12",
        "journalctl --user -u podman --no-pager -n 12",
        "journalctl -b -p warning --no-pager -n 12",
        "cat /run/fy-wall/boot.log 2>/dev/null | tail -20",
        # The rootless ID mapping: a store populated under one subuid range and a user namespace
        # built from another reads as EPERM/ENOENT on files the image plainly has.
        "cat /etc/subuid /etc/subgid; ls -la /usr/bin/newuidmap; cat /proc/sys/user/max_user_namespaces",
        "podman unshare cat /proc/self/uid_map /proc/self/gid_map",
        "podman info --format '{{json .Host.IDMappings}}'",
        "d=$(ls -d ~/.local/share/containers/storage/overlay/*/diff | head -1); ls -lan $d | head -6; ls -lan $d/etc | head -4",
        "stat -c '%u %g %n' /home/runner* ~/.local/share/containers/storage 2>/dev/null; ls -lan /home",
        "podman system migrate 2>&1; podman run --rm docker.io/library/alpine true && echo MIGRATE-FIXED",
        "podman run --rm --userns=keep-id docker.io/library/alpine true && echo KEEP-ID-OK",
    )
    out = []
    for cmd in probes:
        r = lima_shell("sh", "-c", cmd, timeout=90)
        out.append(f"$ {cmd}\n{r.stdout}{r.stderr}".rstrip())
    return "\n".join(out)


def lima_status() -> str:
    """The instance's lifecycle flag as Lima reports it (`Running` / `Stopped` / absent → "")."""
    out = subprocess.run(
        ["limactl", "list", "--json"], capture_output=True, text=True, timeout=30
    ).stdout
    for line in out.splitlines():
        if not line.strip():
            continue
        inst = json.loads(line)
        if inst.get("name") == VM:
            return str(inst.get("status", ""))
    return ""


def lima_shell(*cmd: str, timeout: int = 60) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["limactl", "shell", VM, "--", *cmd], capture_output=True, text=True, timeout=timeout
    )


def lima_edit(expr: str) -> None:
    """`limactl edit --set` (yq) on the STOPPED instance — the only way a mount set changes."""
    subprocess.run(["limactl", "edit", "--tty=false", VM, "--set", expr], check=True, timeout=60)


def socket_alive(path: Path = LIMA_SOCK) -> bool:
    import socket as _socket

    if not path.exists():
        return False
    sock = _socket.socket(_socket.AF_UNIX, _socket.SOCK_STREAM)
    try:
        sock.settimeout(2)
        sock.connect(str(path))
        return True
    except OSError:
        return False
    finally:
        sock.close()


def engine(*args: str, timeout: int = 60) -> subprocess.CompletedProcess:
    """The engine CLI against the VM socket (test_e2e's `_engine_env` mirrors DOCKER_HOST into
    CONTAINER_HOST for podman)."""
    return subprocess.run(
        [_engine(), *args], env=_engine_env(), capture_output=True, text=True, timeout=timeout
    )
