"""`[machine] compose = "vm"` on this host's real VM — the spike of docs/compose-in-vm-scope.md.

The guest runs foldyard's own podman-compose, sent over the backend's ssh route on each call
(:mod:`foldyard.guestcompose`). This module drives that code in-process against the example's
VM and checks what the scope doc's phase 0 asks:

- the payload runs on the guest's ``/usr/bin/python3`` (PyYAML on its pure-Python parser);
- **what one call costs** — the ssh round trip plus the payload, measured, printed, and written to
  the run's step summary in CI, where the hosts differ (vz, qemu/9p, WSL2);
- the guest's config hash is the one the host's child computes (``fy state``'s verdict unchanged);
- **a compose file can't read your computer from there**: an ``env_file:`` (read at create) and an
  ``include:`` (read at render) naming files that exist only on your computer are read by the
  host's client (the red-first controls: today's channel, the one the path guard clamps) and not
  by the guest's;
- a build with ``additional_contexts`` works through the guest's own podman — a client of the
  server's own version (a podman-remote 5.4 client against a 5.8 server refused the flag);
- **the premise**: whatever the VM user can read among the guest's sensitive paths, a rootless
  container (anything a box can create through the engine socket) can read too. The table is
  printed either way.

The second half is the whole flow with ``[machine] compose = "vm"`` adopted: the module RECREATES
the VM from its own example copy, since the client now reads the checkout in the VM (the other
modules' stacks ship their build context over the socket from your computer). A logging ``podman``
first on the CLI's PATH makes "no compose client ran on your computer" an assertion over what ran.

Host tier (tests/e2e_host.py). The spike works in a scratch dir inside one of the VM's writable
mounts. The VM is left running and un-walled.
"""

from __future__ import annotations

import os
import shutil
import signal
import statistics
import subprocess
import sys
import time
from pathlib import Path

import pytest

from e2e_host import (
    LIMA_SOCK,
    PROJECT,
    VM,
    _adopt_on_host,
    _engine,
    _engine_env,
    _probe_db,
    _service_is_listed,
    engine,
    ensure_vm,
    example_copy,
    export_vm_socket,
    fy,
    fy_ok,
    host_tier,
    lima_list,
    lima_shell,
    lima_status,
    wait_for,
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
    export_vm_socket()  # the tests' own engine calls: ensure_vm only does this when it ran
    mounts = lima_list(VM)[0].get("config", {}).get("mounts") or []
    roots = [
        Path(m["location"]).expanduser()
        for m in mounts
        if m.get("writable") and m.get("mountPoint") in (None, m["location"])
    ]
    # A mount can outlive what it shares here: pytest deletes the copies of its earlier sessions.
    roots = [r for r in roots if r.is_dir()]
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
    monkeypatch.setattr(machine, "BACKEND", machine_backend.LimaBackend())


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


def _foldyard_env(repo: Path) -> dict[str, str]:
    """The environment `_foldyard` gives the CLI, for the verbs driven by hand here."""
    env = _engine_env()
    env.pop("CONTAINER_HOST", None)
    return {**env, "FOLDYARD_REPO": str(repo), "FOLDYARD_ENGINE": _engine()}


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
    proc = stack._compose_output(_ctx(scratch), ["ps", "--format", "json"], timeout=120)
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


SECRET = "only-on-your-computer"


@pytest.fixture
def host_only(shared, tmp_path_factory):
    """A directory on your computer that the VM can't see, holding a secret."""
    _, mounts = shared
    d = tmp_path_factory.mktemp("host-only")
    assert not any(d.resolve().is_relative_to(m.resolve()) for m in mounts), (
        "the host-only dir is inside a VM mount; the test needs one the VM can't see"
    )
    return d


def _host_client(ctx, args: list[str]) -> subprocess.CompletedProcess:
    """The compose client on your computer, asked directly: `fy up` would refuse these files with
    the path guard, and this is what the guard stands in front of."""
    from foldyard import stack

    env = {**stack._host_passthrough(), **ctx.env, "CONTAINER_HOST": f"unix://{LIMA_SOCK}"}
    return stack._run([*ctx.compose, *args], env=env, cwd=str(ctx.main), timeout=300)


def test_an_env_file_on_your_computer_reaches_a_container_only_from_the_hosts_client(
    shared, bound, host_only
):
    """The channel itself: the client reads `env_file` at create (podman-compose turns it into
    `-e` flags), and the box can `inspect` the container it lands in."""
    from foldyard import guestcompose

    scratch, _ = shared
    secret = host_only / "secret.env"
    secret.write_text(f"FY_HOST_ONLY={SECRET}\n")
    (scratch / "leak.yml").write_text(
        f"services:\n  web:\n    image: docker.io/library/alpine\n    env_file: [{secret}]\n"
    )
    ctx = _ctx(scratch, files=("leak.yml",))
    try:
        made = _host_client(ctx, ["up", "--no-start"])
        assert made.returncode == 0, made.stdout + made.stderr
        seen = engine("inspect", "--format", "{{json .Config.Env}}", "fygc_web_1", timeout=60)
        assert SECRET in seen.stdout, f"red-first control: it never reached the container: {seen}"
    finally:
        _host_client(ctx, ["down"])
    there = guestcompose.compose(ctx, ["up", "--no-start"], timeout=300)
    try:
        assert there.returncode != 0
        assert "does not exist" in there.stdout + there.stderr
        assert engine("inspect", "fygc_web_1", timeout=60).returncode != 0
    finally:
        guestcompose.compose(ctx, ["down"], timeout=120)


def test_a_compose_file_on_your_computer_is_rendered_only_by_the_hosts_client(
    shared, bound, host_only
):
    """The same channel at render time: `include` pulls another file's contents into `config`."""
    from foldyard import guestcompose

    scratch, _ = shared
    other = host_only / "other.yml"
    other.write_text(f"services:\n  other:\n    environment:\n      FY_HOST_ONLY: {SECRET}\n")
    (scratch / "inc.yml").write_text(
        f"include:\n  - {other}\nservices:\n  web:\n    image: docker.io/library/alpine\n"
    )
    ctx = _ctx(scratch, files=("inc.yml",))
    here = _host_client(ctx, ["config"])
    assert SECRET in here.stdout, f"red-first control: {here.stdout}{here.stderr}"
    there = guestcompose.compose(ctx, ["config"], timeout=120)
    assert SECRET not in there.stdout + there.stderr


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


# The guest's paths worth knowing about: what Lima's boot data and foldyard's root-only files would
# give away (the boot scripts carry the monitor's and the forwarder's keys when those are on), and
# the journal, which the VM user reads through its `systemd-journal` group.
_SENSITIVE = (
    "/mnt/lima-cidata",
    "/mnt/lima-cidata/user-data",
    "/var/lib/cloud/instance/user-data.txt",
    "/var/log/cloud-init-output.log",
    "/etc/fy-monitor/relay.key",
    "/etc/fy-fwd/forwarder.key",
    "/etc/sudoers.d",
    "/run/fy-wall",
    "/var/log/journal",
    "/run/log/journal",
)

# One probe, the same on both sides: a file's first byte, else the first file under a directory
# (its listing alone proves little: the journal's directories are world-readable, its files aren't).
_READ = (
    'f="$(find "$1" -type f 2>/dev/null | head -1)"; '
    'if [ -n "$f" ]; then head -c1 "$f" >/dev/null 2>&1; else ls "$1" >/dev/null 2>&1; fi'
)


def _vm_user_reads(path: str) -> str:
    if lima_shell("test", "-e", path).returncode != 0:
        # `test -e` can't see under a directory it can't enter, which is also "can't read".
        missing = lima_shell("sh", "-c", f'ls -d "{path}" 2>&1').stdout
        return "absent" if "No such file" in missing else "refused"
    return "readable" if lima_shell("sh", "-c", _READ, "-", path).returncode == 0 else "refused"


def _box_reads(path: str, *, keep_groups: bool = True) -> bool:
    """Through the engine socket, as a box asks: a rootless container bind-mounting ``path``. A
    remote client refuses `--group-add keep-groups`, but that is the client's check: the annotation
    it sets reaches crun from any client, and the API service holds the VM user's groups."""
    groups = ["--annotation", "run.oci.keep_original_groups=1"] if keep_groups else []
    r = engine(
        "run", "--rm", *groups, "--security-opt", "label=disable",
        "-v", f"{path}:/probe:ro", "docker.io/library/alpine", "sh", "-c", _READ, "-", "/probe",
        timeout=120,
    )  # fmt: skip
    return r.returncode == 0


def test_the_box_probe_can_tell_a_refused_read_from_a_granted_one(shared, bound):
    """The control: without the VM user's groups the journal's files stay shut, so a "readable"
    below means the box really read one, not that the probe can't fail."""
    if not any(_box_reads(p) for p in ("/var/log/journal", "/run/log/journal")):
        pytest.skip("no journal file readable by the VM user on this guest")
    assert not _box_reads("/var/log/journal", keep_groups=False)


def test_the_vm_user_reads_nothing_a_box_cant(shared, bound):
    """The premise of running compose as the VM user: a box can create a rootless container that
    bind-mounts anything the VM user can read, so running there adds nothing. A path the VM user
    reads and a box can't is a finding (docs/compose-in-vm-scope.md)."""
    widened = []
    for path in _SENSITIVE:
        user = _vm_user_reads(path)
        box = _box_reads(path) if user == "readable" else None
        if user == "readable" and not box:
            widened.append(path)
        _summary(
            f"compose-in-vm premise: {path}: VM user {user}"
            + ("" if box is None else f", box {'readable' if box else 'REFUSED'}")
        )
    assert not widened, f"the VM user reads what a box can't: {widened}"


# ── the whole flow, with `[machine] compose = "vm"` adopted ───────────────────────────────


@pytest.fixture(scope="module")
def flow(tmp_path_factory):
    """``(repo, env)``: an example copy that turned it on, the VM recreated to mount it, and a
    ``podman`` on the CLI's PATH that logs every argv before running the real one."""
    base = tmp_path_factory.mktemp("flow")
    repo = example_copy(base)
    toml = repo / "foldyard.toml"
    text = toml.read_text()
    assert text.count("\n[machine]\n") == 1
    toml.write_text(text.replace("\n[machine]\n", '\n[machine]\ncompose = "vm"\n'))
    subprocess.run(
        ["git", "-c", "user.email=e2e@foldyard", "-c", "user.name=e2e", "commit", "-qam", "vm"],
        cwd=str(repo),
        check=True,
    )
    shim = base / "bin"
    shim.mkdir()
    log = base / "podman.log"
    real = shutil.which("podman")
    assert real
    (shim / "podman").write_text(f'#!/bin/sh\nprintf "%s\\n" "$*" >> "{log}"\nexec "{real}" "$@"\n')
    (shim / "podman").chmod(0o755)
    env = {"PATH": f"{shim}{os.pathsep}{os.environ['PATH']}"}
    _adopt_on_host(repo, env)
    fy_ok(["machine", "recreate", "--yes"], repo, timeout=900, env_extra=env)
    export_vm_socket()
    fy_ok(["up"], repo, env_extra=env)
    yield repo, env, log
    fy(["down"], repo, timeout=180, env_extra=env)


def test_flow_up_serves_from_the_guests_client(flow):
    repo, env, _ = flow
    assert _probe_db() and "reachable" in _probe_db()
    ps = fy_ok(["ps"], repo, timeout=120, env_extra=env)
    assert "(in the VM)" in ps.out
    assert _service_is_listed(ps.out, PROJECT, "api"), ps.out


def test_flow_state_sees_no_drift_through_the_guests_hash(flow):
    repo, env, _ = flow
    state = fy(["state"], repo, timeout=120, env_extra=env)
    assert "match the rendered config" in state.out, state.out


def test_flow_doctor_has_no_failing_row(flow):
    repo, env, _ = flow
    doctor = fy(["doctor"], repo, timeout=300, env_extra=env)
    assert doctor.rc == 0, doctor.out
    assert '[machine] compose = "vm"' in doctor.out  # the experimental row: never forgotten


def test_flow_fy_shell_enters_the_running_container(flow):
    """`exec` needs a terminal: a real pty, the engine's exec, a real shell at the other end."""
    import pty
    import select

    repo, env, _ = flow
    leader, follower = pty.openpty()
    full = {**_foldyard_env(repo), **env}
    proc = subprocess.Popen(
        [sys.executable, "-m", "foldyard", "shell"],
        cwd=str(repo),
        env=full,
        stdin=follower,
        stdout=follower,
        stderr=follower,
    )
    os.close(follower)
    seen = b""

    def read_until(marker: bytes) -> None:
        nonlocal seen
        deadline = time.monotonic() + 60
        while marker not in seen and time.monotonic() < deadline:
            if select.select([leader], [], [], 1)[0]:
                try:
                    seen += os.read(leader, 4096)
                except OSError:
                    break
        assert marker in seen, seen.decode(errors="replace")

    try:
        read_until(b"# ")  # the container's prompt: typed any sooner, the input is lost
        os.write(leader, b"echo marker-$((40+2)); exit\n")
        read_until(b"marker-42")
    finally:
        proc.terminate()
        proc.wait(timeout=30)
        os.close(leader)


def _clients_in_the_guest() -> str:
    """The compose runner's processes in the guest, and what they started."""
    found = lima_shell("pgrep", "-af", "python3 -I -|podman logs|podman-compose")
    return "\n".join(
        line for line in found.stdout.splitlines() if "pgrep" not in line and "sh -c" not in line
    )


def test_flow_an_interrupted_fy_logs_leaves_nothing_in_the_guest(flow):
    repo, env, _ = flow
    proc = subprocess.Popen(
        [sys.executable, "-m", "foldyard", "logs"],
        cwd=str(repo),
        env={**_foldyard_env(repo), **env},
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        wait_for(lambda: bool(_clients_in_the_guest()), timeout=30, what="the client in the guest")
    finally:
        proc.send_signal(signal.SIGINT)
        proc.wait(timeout=30)
    wait_for(lambda: not _clients_in_the_guest(), timeout=20, what="the guest's client to end")


def _with_env_file(repo: Path, env_file: str):
    """``repo``'s compose.yml with the api taking ``env_file``, committed nowhere; undone by the
    caller's ``restore()``."""
    compose = repo / "compose.yml"
    before = compose.read_text()
    assert "\n  api:\n" in before
    compose.write_text(before.replace("\n  api:\n", f"\n  api:\n    env_file: [{env_file}]\n"))
    return lambda: compose.write_text(before)


@pytest.mark.parametrize("kind", ["absolute", "planted link"])
def test_flow_a_host_file_never_reaches_a_container(flow, host_only, kind):
    """The channel the path guard narrows, against the real verb. The planted link is what the
    guard can't close (the client re-opens each path after the check): in the VM it dangles."""
    repo, env, _ = flow
    secret = host_only / "secret.env"
    secret.write_text(f"FY_HOST_ONLY={SECRET}\n")
    link = repo / "linked.env"
    if kind == "planted link":
        link.symlink_to(secret)
    restore = _with_env_file(repo, str(secret) if kind == "absolute" else "linked.env")
    try:
        up = fy(["up"], repo, env_extra=env)
        assert up.rc != 0, f"it was meant to fail, in the VM:\n{up.out}"
        assert SECRET not in up.out
        assert "does not exist" in up.out, up.out
        assert "outside the checkout" not in up.out  # the VM said so, not the guard
    finally:
        restore()
        link.unlink(missing_ok=True)
    seen = engine("inspect", "--format", "{{json .Config.Env}}", f"{PROJECT}_api_1", timeout=60)
    assert SECRET not in seen.stdout
    fy_ok(["up"], repo, env_extra=env)  # and the stack comes back


def test_flow_a_projects_box_image_builds_in_the_guest(flow):
    repo, env, _ = flow
    built = fy(["box", "build"], repo, timeout=1500, env_extra=env)
    assert built.rc == 0, built.out
    # Quick when the base VM's clone carried the layers, so prove it built: the fingerprint is a
    # layer of its own, made by this build.
    label = engine(
        "image", "inspect", "--format", '{{index .Labels "io.foldyard.box-fingerprint"}}',
        "foldyard-example-box:latest",
    )  # fmt: skip
    assert label.returncode == 0 and label.stdout.strip(), label.stderr


def test_flow_verify_passes_with_the_client_in_the_vm(flow):
    """The credibility check, on the VM this flow made: moving the client widens nothing it sees."""
    repo, env, _ = flow
    verify = fy(["verify"], repo, timeout=600, env_extra=env)
    assert verify.rc == 0, verify.out


def test_flow_no_compose_client_or_build_ran_on_your_computer(flow):
    """Last, over everything above: what really ran here. The engine is asked plenty (`ps`,
    `inspect`, `exec`), and none of that parses a compose file or packs a context."""
    _, _, log = flow
    argvs = log.read_text().splitlines()
    assert any(a.startswith("ps") or a.startswith("inspect") for a in argvs), "the shim saw nothing"
    ran_here = [a for a in argvs if a.split()[:1] in (["compose"], ["build"])]
    assert not ran_here, f"on your computer:\n{chr(10).join(ran_here)}"
