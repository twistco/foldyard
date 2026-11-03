"""`[machine] compose = "vm"` on this host's real VM — the spike of docs/compose-in-vm-scope.md.

The guest runs foldyard's own podman-compose, sent over the backend's ssh route on each call
(:mod:`foldyard.guestcompose`). This module drives that code in-process against the example's
VM and checks what the scope doc's phase 0 asks:

- the payload runs on the guest's ``/usr/bin/python3`` (PyYAML on its pure-Python parser);
- **what one call costs** — the ssh round trip plus the payload, measured, printed, and written to
  the run's step summary in CI, where the hosts differ (vz, qemu/9p, WSL2);
- the guest's config hash is the one the host's child computes (``fy state``'s verdict unchanged);
- **a compose file can't read your computer from there**: an ``env_file:`` naming a file that exists
  only on your computer is read by the host's client (the red-first control: today's channel, the
  one the path guard clamps) and not by the guest's;
- a build with ``additional_contexts`` works through the guest's own podman — a client of the
  server's own version (a podman-remote 5.4 client against a 5.8 server refused the flag);
- **the premise**: whatever the VM user can read among the guest's sensitive paths, a rootless
  container (anything a box can create through the engine socket) can read too. The table is
  printed either way.

Host tier (tests/e2e_host.py). Works in a scratch dir inside one of the VM's writable mounts and
leaves the VM running and un-walled.
"""

from __future__ import annotations

import json
import os
import shutil
import statistics
import subprocess
import time
from pathlib import Path

import pytest

from e2e_host import (
    LIMA_SOCK,
    VM,
    ensure_vm,
    example_copy,
    host_tier,
    lima_list,
    lima_shell,
    lima_status,
)

pytestmark = host_tier

CALLS = 10

COMPOSE = """\
services:
  web:
    image: docker.io/library/alpine
    environment:
      A: ${A_VAR:-unset}
"""


@pytest.fixture(scope="module")
def shared(tmp_path_factory):
    """A writable VM mount at the same path on both sides, and a fresh scratch dir in it."""
    if "Running" not in lima_status():
        ensure_vm(example_copy(tmp_path_factory.mktemp("guestcompose")))
    mounts = lima_list(VM)[0].get("config", {}).get("mounts") or []
    roots = [
        Path(m["location"]).expanduser()
        for m in mounts
        if m.get("writable") and m.get("mountPoint") in (None, m["location"])
    ]
    if not roots:
        pytest.skip(f"{VM} has no writable mount at the same path on both sides")
    scratch = roots[0] / f".fy-guestcompose-{int(time.time())}"
    scratch.mkdir()
    (scratch / "compose.yml").write_text(COMPOSE)
    yield scratch, [Path(m["location"]).expanduser() for m in mounts]
    shutil.rmtree(scratch, ignore_errors=True)


@pytest.fixture
def bound(monkeypatch):
    """The in-process package pointed at the example's VM (its import-time constants re-pointed,
    as test_probes_e2e does)."""
    from foldyard import config, machine, machine_backend

    monkeypatch.setattr(config, "in_box", lambda: False)
    monkeypatch.setenv("MACHINE_COMPOSE", "vm")
    monkeypatch.setattr(machine, "MACHINE", VM)
    monkeypatch.setattr(machine, "BACKEND", machine_backend.get_backend("lima"))


def _ctx(scratch: Path, files: tuple[str, ...] = ("compose.yml",), **env: str):
    from foldyard import stack

    compose = ["podman", "compose"]
    for name in files:
        compose += ["-f", str(scratch / name)]
    full = {
        "PODMAN_COMPOSE_PROVIDER": stack.compose_provider_path(),
        "PODMAN_PROJECT": "fygc",
        "COMPOSE_PROJECT_NAME": "fygc",
        "FOLDYARD_CHECKOUT": str(scratch),
        **env,
    }
    return stack.Context(scratch, full, compose, "web", "fygc", "")


def _summary(line: str) -> None:
    print(line)
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if path:
        with open(path, "a") as fh:
            fh.write(line + "\n")


def test_the_payload_runs_on_the_guests_python(shared, bound):
    from foldyard import guestcompose

    scratch, _ = shared
    py = lima_shell(guestcompose.PYTHON, "--version")
    assert py.returncode == 0, f"no {guestcompose.PYTHON} in the guest: {py.stderr}"
    proc = guestcompose.compose(_ctx(scratch), ["version"], timeout=120)
    assert proc.returncode == 0, proc.stderr
    assert "podman-compose version" in proc.stdout + proc.stderr
    _summary(
        f"compose-in-vm: guest {py.stdout.strip()}, payload {len(guestcompose.payload())} B "
        f"({guestcompose.fingerprint()})"
    )


def test_what_one_call_costs(shared, bound):
    from foldyard import guestcompose, machine
    from foldyard.sandbox import _ssh_argv

    scratch, _ = shared
    target = machine.BACKEND.ssh_target(VM)
    assert target is not None
    bare, full = [], []
    for _ in range(CALLS):
        t = time.monotonic()
        subprocess.run([*_ssh_argv(target), "true"], check=True, timeout=60)
        bare.append(time.monotonic() - t)
        t = time.monotonic()
        proc = guestcompose.compose(_ctx(scratch), ["config", "--services"], timeout=120)
        full.append(time.monotonic() - t)
        assert proc.returncode == 0 and proc.stdout.split() == ["web"], proc.stderr

    def fmt(xs: list[float]) -> str:
        return f"p50 {statistics.median(xs) * 1000:.0f} ms, max {max(xs) * 1000:.0f} ms"

    _summary(f"compose-in-vm: bare ssh {fmt(bare)}; `config --services` {fmt(full)} ({CALLS})")
    assert statistics.median(full) < 10, "a compose call in the guest took over 10 s (median)"


def test_ps_answers_json(shared, bound):
    from foldyard import stack

    scratch, _ = shared
    proc = stack._compose_read(_ctx(scratch), ["ps", "--format", "json"], timeout=120)
    assert proc.returncode == 0, proc.stderr
    assert stack._compose_ps_rows(proc.stdout) == []  # nothing of this project runs


def test_the_guests_config_hash_is_the_hosts(shared, bound, monkeypatch):
    from foldyard import confighash

    scratch, _ = shared
    ctx = _ctx(scratch, A_VAR="v1")
    guest, why = confighash.desired(ctx)
    assert guest, why
    monkeypatch.setenv("MACHINE_COMPOSE", "host")
    here, why = confighash.desired(ctx)
    assert here == guest, why
    monkeypatch.setenv("MACHINE_COMPOSE", "vm")
    changed, _ = confighash.desired(_ctx(scratch, A_VAR="v2"))
    assert changed and changed["web"] != guest["web"]


def test_an_env_file_on_your_computer_is_read_here_and_not_in_the_vm(
    shared, bound, tmp_path_factory, monkeypatch
):
    from foldyard import guestcompose, stack

    scratch, mounts = shared
    secret_dir = tmp_path_factory.mktemp("host-only")
    assert not any(secret_dir.resolve().is_relative_to(m.resolve()) for m in mounts), (
        "the host-only dir is inside a VM mount; the test needs one the VM can't see"
    )
    secret = secret_dir / "secret.env"
    secret.write_text("FY_HOST_ONLY_SECRET=only-on-your-computer\n")
    (scratch / "leak.yml").write_text(
        f"services:\n  web:\n    image: docker.io/library/alpine\n    env_file: [{secret}]\n"
    )
    ctx = _ctx(scratch, files=("leak.yml",))
    # The control: the host's client reads it (today's channel — `fy up` refuses it with the
    # guard; here the client is asked directly, as the guard's render is).
    monkeypatch.setenv("MACHINE_COMPOSE", "host")
    host_env = {**stack._host_passthrough(), **ctx.env, "CONTAINER_HOST": f"unix://{LIMA_SOCK}"}
    here = stack._run([*ctx.compose, "config"], env=host_env, cwd=str(scratch), timeout=120)
    assert "only-on-your-computer" in here.stdout, (
        f"red-first control: the host's client didn't read {secret}:\n{here.stdout}{here.stderr}"
    )
    monkeypatch.setenv("MACHINE_COMPOSE", "vm")
    there = guestcompose.compose(ctx, ["config"], timeout=120)
    assert "only-on-your-computer" not in there.stdout + there.stderr


def test_a_build_with_an_additional_context_runs_in_the_guest(shared, bound):
    from foldyard import guestcompose

    scratch, _ = shared
    (scratch / "ctx").mkdir()
    (scratch / "extra").mkdir()
    (scratch / "extra" / "f").write_text("from the extra context\n")
    (scratch / "ctx" / "Dockerfile").write_text("FROM scratch\nCOPY --from=extra f /f\n")
    tag = f"localhost/fygc-probe:{int(time.time())}"
    argv = [
        "build",
        "-t",
        tag,
        "--build-context",
        f"extra={scratch / 'extra'}",
        str(scratch / "ctx"),
    ]
    try:
        proc = guestcompose.call("podman", argv, {}, str(scratch), timeout=300)
        assert proc.returncode == 0, proc.stdout + proc.stderr
    finally:
        guestcompose.call("podman", ["rmi", "-f", tag], {}, str(scratch), timeout=60)


# The guest's paths worth knowing about: what Lima's boot data and foldyard's root-only files
# would give away (boot scripts carry the monitor's and the forwarder's keys when those are on).
_SENSITIVE = (
    "/mnt/lima-cidata",
    "/mnt/lima-cidata/user-data",
    "/mnt/lima-cidata/provision.system",
    "/var/lib/cloud/instance/user-data.txt",
    "/var/lib/cloud/instance/scripts",
    "/etc/fy-monitor/relay.key",
    "/etc/fy-forwarder",
    "/run/fy-wall",
    "/etc/sudoers.d",
)

_PROBE = r"""
import json, os, sys
out = {}
for p in sys.argv[1:]:
    try:
        if os.path.isdir(p):
            os.listdir(p); out[p] = "readable"
        else:
            open(p, "rb").read(1); out[p] = "readable"
    except FileNotFoundError:
        out[p] = "absent"
    except OSError as e:
        out[p] = type(e).__name__
print(json.dumps(out))
"""


def test_the_vm_user_reads_nothing_a_box_cant(shared, bound):
    """The premise of running compose as the VM user: a box can create a rootless container that
    bind-mounts any path the VM user can read, so the VM user's reads add nothing. A path the VM
    user can read and a container can't is a finding (docs/compose-in-vm-scope.md)."""
    user = lima_shell("python3", "-I", "-c", _PROBE, *_SENSITIVE, timeout=60)
    assert user.returncode == 0, user.stderr
    as_user = json.loads(user.stdout)
    as_box = {}
    for path, seen in as_user.items():
        if seen != "readable":
            continue
        r = lima_shell(
            "podman", "run", "--rm", "--security-opt", "label=disable",
            "-v", f"{path}:/probe:ro", "docker.io/library/alpine",
            "sh", "-c", "if [ -d /probe ]; then ls /probe; else head -c1 /probe; fi >/dev/null",
            timeout=120,
        )  # fmt: skip
        as_box[path] = "readable" if r.returncode == 0 else f"refused ({r.stderr.strip()[-80:]})"
    for path in _SENSITIVE:
        _summary(
            f"compose-in-vm premise: {path}: VM user {as_user[path]}"
            + (f", box {as_box[path]}" if path in as_box else "")
        )
    widened = [p for p, v in as_box.items() if v != "readable"]
    assert not widened, f"the VM user reads what a box can't: {widened}"
