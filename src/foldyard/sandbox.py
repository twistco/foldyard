"""sandbox.py — the gVisor MACHINE posture (``[machine].runtime = "gvisor"``).

Layer ③ of docs/isolation-layers.md: the dev box runs under gVisor's userspace kernel (runsc), so
a kernel exploit from inside the box has to get through the Sentry before it reaches the VM
kernel that holds the engine socket, the stack and the mounted checkout. It is a posture of the
MACHINE, not of one box: the VM's engine gets a SECOND API socket whose default runtime is runsc
(a user unit + a ``containers.conf`` override, no root, nothing in the boot script), ``fy box
up`` creates the box through that socket, and the box's OWN engine socket IS that socket — so
whatever the box creates (a sibling, the stack from in-box ``fy up``) runs under gVisor too, and
the box cannot opt itself or a sibling out by choosing a runtime. (On podman ≥ 6 the libpod
create endpoint honours a client-supplied runtime; the socket-narrowing filter that strips it is
the enforcement tier — until it lands, the socket default is the mechanism and the doc says so.)

Why a second socket and not ``podman run --runtime``: over the engine socket podman 5.8 has no
per-container runtime selection at all (``podman-remote`` has no ``--runtime``; the REST field
is ignored until 6.0) — measured on the rig, isolation-layers.md "The route". The runsc flags
the box needs live in a wrapper INSIDE the VM with no ``--allow-flag-override``, so a client
of the socket cannot reach runsc's flags (a widening annotation is refused, a narrowing one
honoured — both observed). Podman-remote reaches the second socket over the backend's own ssh
port (``CONTAINER_HOST=ssh://…``), so nothing in the VM's config changes and no restart is
needed: the posture is enabled live, and a box switches runtime by being recreated.

Everything here is host-side and provisioned by :func:`ensure` from ``machine ensure``. The
runtime binary is a pinned gVisor release downloaded by the HOST (the guest's egress is walled),
sha512-verified, cached under ``~/.foldyard/cache/`` and streamed into the guest over ssh — never
fetched by the guest and never from the repo mount (ADR-0023).
"""

from __future__ import annotations

import hashlib
import os
import shlex
import subprocess
import sys
import time
import urllib.request
from pathlib import Path
from typing import Protocol

from . import config
from .machine_backend import SshTarget


class SshBackend(Protocol):
    """What this module needs from a machine backend: a name for messages, and its ssh route
    into the guest (:meth:`machine_backend.Backend.ssh_target`)."""

    name: str

    def ssh_target(self, name: str) -> SshTarget | None: ...


GVISOR_RELEASE = "20260817.0"
RUNTIME = "runsc-fy"  # the runtime NAME the VM's engine resolves (the wrapper below)
SOCKET = "podman-runsc.sock"  # the runsc-DEFAULT service — the HOST creates the box through it
FILTER_SOCKET = "podman-runsc-filtered.sock"  # the narrowing filter — what the BOX mounts
FILTER_UNIT = "podman-runsc-filter.service"
BAKED_ENV = (
    "FY_MACHINE_RUNTIME"  # baked into the box at create: the already-up nag + verify read it
)
_BASE_URL = "https://storage.googleapis.com/gvisor/releases/release"


def _err(*a: object) -> None:
    print(*a, file=sys.stderr)


def wanted() -> bool:
    return config.machine_runtime() == "gvisor"


def _runtime_dir() -> str:
    """The VM user's ``XDG_RUNTIME_DIR``. The VM user is the host user's uid on both backends
    (Lima maps it; podman machine creates ``core`` with it), so it is ``/run/user/<host uid>``
    there — the same convention the Lima backend's guest socket uses."""
    return f"/run/user/{os.getuid()}"


def guest_socket() -> str:
    """The runsc-DEFAULT API socket inside the VM. The HOST creates the box through this (a
    trusted create with no runtime field); the box itself never touches it — it holds the
    filtered socket below, so it cannot opt a sibling out."""
    return f"{_runtime_dir()}/podman/{SOCKET}"


def box_socket() -> str:
    """The NARROWED API socket the box mounts as its own engine socket. The socket filter
    (``assets/sandbox/socket_filter.py``) forwards it to :func:`guest_socket` after stripping the
    runtime-selecting fields from container-create, so a sibling or an in-box ``fy up`` created
    through it cannot escape gVisor even on a podman that would honour a client-chosen runtime."""
    return f"{_runtime_dir()}/podman/{FILTER_SOCKET}"


def container_host(target: SshTarget, socket_path: str | None = None) -> str:
    """The ``CONTAINER_HOST`` podman-remote uses to reach a guest socket: ssh to the backend's
    loopback port, then the socket path in the guest. No forward to configure, nothing for the
    VM to restart. Defaults to the runsc socket (the host's create path)."""
    return f"ssh://{target.user}@127.0.0.1:{target.port}{socket_path or guest_socket()}"


def _target(backend: SshBackend, name: str) -> SshTarget:
    target = backend.ssh_target(name)
    if target is None:
        raise SystemExit(
            f'✗ [machine].runtime = "gvisor" needs ssh access to the VM, and the {backend.name} '
            f"backend offers none for machine '{name}' (native has no VM; is it created?)."
        )
    return target


def engine_env(env: dict, backend: SshBackend, name: str, *, filtered: bool = False) -> dict:
    """The stack env with the engine endpoint swapped for a guest socket — what ``box up`` hands
    the ONE ``podman run`` that creates the box. Everything else (probes, exec, build) stays on
    the default socket: both services share the VM's one libpod store, and the runtime is fixed
    at create. ``filtered`` selects the narrowed socket (the box's own, and what ``ensure``
    probes); the host's create uses the runsc socket directly."""
    target = _target(backend, name)
    uri = container_host(target, box_socket() if filtered else guest_socket())
    return {**env, "CONTAINER_HOST": uri, "DOCKER_HOST": uri, "CONTAINER_SSHKEY": target.identity}


# ── the guest side ────────────────────────────────────────────────────────────────────


def _ssh_argv(target: SshTarget) -> list[str]:
    # Loopback to a VM the backend created for us, with its own identity — the same trust the
    # backend's `shell` has. Batch: a prompt here would hang `fy up`.
    return [
        "ssh",
        "-p",
        str(target.port),
        "-i",
        target.identity,
        "-o",
        "BatchMode=yes",
        "-o",
        "StrictHostKeyChecking=no",
        "-o",
        "UserKnownHostsFile=/dev/null",
        "-o",
        "LogLevel=ERROR",
        "-o",
        "ConnectTimeout=15",
        f"{target.user}@127.0.0.1",
    ]


def _ssh(target: SshTarget, script: str, stdin: bytes | None = None) -> subprocess.CompletedProcess:
    """Run ``script`` in the guest as the VM user (``bash -c``, so stdin stays free for a
    payload). Output decoded; never raises on a non-zero exit."""
    out = subprocess.run(
        [*_ssh_argv(target), f"bash -c {shlex.quote(script)}"],
        input=stdin,
        capture_output=True,
        timeout=600,
    )
    return subprocess.CompletedProcess(
        out.args,
        out.returncode,
        out.stdout.decode(errors="replace"),
        out.stderr.decode(errors="replace"),
    )


def _filter_source() -> str:
    """The in-guest socket filter, embedded into the provisioning script (written by the guest's
    own ``write`` helper so it restarts the filter unit only when the source changed)."""
    path = Path(__file__).resolve().parent / "assets" / "sandbox" / "socket_filter.py"
    return path.read_text()


def guest_script() -> str:
    """The idempotent provisioning the VM user runs: the wrapper (flags fixed, override OFF), the
    runtime NAME registered engine-wide as a drop-in (so the DEFAULT service can exec/stop/rm a
    gVisor box — without it that service says the runtime is missing — and so an existing
    containers.conf is never clobbered), the override the second service defaults on, that
    service as an enabled user unit, and the narrowing filter (its source + a third unit serving
    the socket the box mounts). Services are restarted only when their inputs changed."""
    return _GUEST_SCRIPT.replace("@@FILTER_SOURCE@@", _filter_source())


_GUEST_SCRIPT = r"""
set -euo pipefail
PODMAN=$(command -v podman)
PY=$(command -v python3)
mkdir -p ~/.local/bin ~/.config/containers/containers.conf.d ~/.config/systemd/user
cat > ~/.local/bin/runsc-fy <<EOF
#!/bin/sh
# foldyard: gVisor with the flags the dev box needs, fixed HERE. No --allow-flag-override: a
# client of the engine socket must not be able to reach runsc's flags (widening annotations
# are refused; runsc still honours ones that narrow). Managed by \`fy up\` (machine ensure).
# Absolute path: podman's conmon runs the runtime with no HOME in its environment.
exec "$HOME/.local/bin/runsc" --ignore-cgroups --host-uds=all "\$@"
EOF
chmod 0755 ~/.local/bin/runsc-fy
write() {  # write <path> (content on stdin): replace only when different; print "changed"
  local tmp="$1.fy-new"
  cat > "$tmp"
  if [ "$(cat "$tmp")" = "$(cat "$1" 2>/dev/null)" ]; then
    rm -f "$tmp"
  else
    mv -f "$tmp" "$1"; echo changed
  fi
}
runtimes=$(write ~/.config/containers/containers.conf.d/50-foldyard-runsc.conf <<EOF
# foldyard: gVisor registered by NAME engine-wide, so every API service (the default crun one
# and the runsc-default one) can drive a gVisor container. Only runsc.conf DEFAULTS to it.
[engine.runtimes]
runsc-fy = ["$HOME/.local/bin/runsc-fy"]
EOF
)
override=$(write ~/.config/containers/runsc.conf <<'EOF'
# foldyard: the gVisor box socket — every container created through THIS service runs under
# runsc (CONTAINERS_CONF_OVERRIDE of podman-runsc.service). SELinux labelling is off for them:
# runsc refuses a labelled spec ("SELinux is not supported"), and gVisor's own kernel is the
# separation — the box's create passes label=disable itself, in-box `podman run` cannot.
[containers]
label = false

[engine]
runtime = "runsc-fy"
EOF
)
unit=$(write ~/.config/systemd/user/podman-runsc.service <<EOF
[Unit]
Description=foldyard: podman API service defaulting to gVisor (runsc)
After=podman.socket

[Service]
Environment=CONTAINERS_CONF_OVERRIDE=%h/.config/containers/runsc.conf
ExecStart=$PODMAN system service --time=0 unix://%t/podman/podman-runsc.sock
Restart=on-failure

[Install]
WantedBy=default.target
EOF
)
filter=$(write ~/.local/bin/fy-socket-filter <<'PYEOF'
@@FILTER_SOURCE@@
PYEOF
)
chmod 0755 ~/.local/bin/fy-socket-filter
filterunit=$(write ~/.config/systemd/user/podman-runsc-filter.service <<EOF
[Unit]
# foldyard: the narrowing filter in front of the runsc socket. The box mounts THIS socket, so a
# container the box creates cannot pick a non-gVisor runtime — the filter strips oci_runtime and
# dev.gvisor.* from container-create (docs/isolation-layers.md, ADR ③).
Description=foldyard: gVisor socket filter (box-facing, strips the runtime opt-out from creates)
After=podman-runsc.service
Requires=podman-runsc.service

[Service]
ExecStart=$PY %h/.local/bin/fy-socket-filter %t/podman/podman-runsc.sock %t/podman/podman-runsc-filtered.sock
Restart=on-failure

[Install]
WantedBy=default.target
EOF
)
systemctl --user daemon-reload
if [ -n "$runtimes" ]; then systemctl --user try-restart podman.service; fi
if [ -n "$unit$override" ]; then systemctl --user try-restart podman-runsc.service; fi
systemctl --user enable --now podman-runsc.service
if [ -n "$filter$filterunit" ]; then systemctl --user try-restart podman-runsc-filter.service; fi
systemctl --user enable --now podman-runsc-filter.service
"""


# ── the runtime binary: host-downloaded, pinned, verified, cached ─────────────────────


def release_url(arch: str) -> str:
    return f"{_BASE_URL}/{GVISOR_RELEASE}/{arch}/runsc"


def _cache_dir() -> Path:
    return Path.home() / ".foldyard" / "cache"


def _fetch(url: str) -> bytes:
    with urllib.request.urlopen(url, timeout=120) as resp:
        return resp.read()


def _download(arch: str) -> Path:
    """The pinned runsc for ``arch`` (the GUEST's ``uname -m``), from the host's cache or the
    release bucket, sha512-checked against the published sum before anything is written."""
    dest = _cache_dir() / f"runsc-{GVISOR_RELEASE}-{arch}"
    if dest.exists():
        return dest
    url = release_url(arch)
    _err(f"▶ downloading gVisor runsc release-{GVISOR_RELEASE} ({arch})…")
    try:
        body = _fetch(url)
        want = _fetch(url + ".sha512").decode(errors="replace").split()[0]
    except Exception as e:
        raise SystemExit(f"✗ downloading runsc failed ({url}): {e}") from None
    if hashlib.sha512(body).hexdigest() != want:
        raise SystemExit("✗ the runsc download failed its sha512 check — refusing to install it")
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_suffix(".part")
    part.write_bytes(body)
    part.replace(dest)
    return dest


def _probe_runtime(env: dict, *, attempts: int = 10, delay: float = 1.0) -> str:
    """What the runsc socket's service reports as its default runtime — asked THROUGH the same
    ssh:// endpoint ``box up`` will use, so the probe covers the whole path.

    The exit code decides, never stdout: podman-remote prints its OWN client version block to
    stdout when it cannot reach the server, which read as "answers with runtime 'OS: linux…'"
    until this checked. And a service started moments ago by ``systemctl --user enable --now`` is
    'active' before podman listens, so a failed connection is retried briefly — the first probe
    after a fresh provisioning raced it live."""
    out = None
    for i in range(attempts):
        out = subprocess.run(
            ["podman", "info", "--format", "{{.Host.OCIRuntime.Name}}"],
            env=env,
            capture_output=True,
            text=True,
            timeout=90,
        )
        if out.returncode == 0:
            return out.stdout.strip() or "(podman info printed nothing)"
        if i + 1 < attempts:
            time.sleep(delay)
    assert out is not None
    return f"(podman info failed: {out.stderr.strip()[-300:]})"


def ensure(backend: SshBackend, name: str) -> None:
    """Provision the posture in the running VM ``name`` (idempotent, cheap when already there)
    and FAIL CLOSED: a socket that does not answer with the gVisor runtime aborts the verb, so a
    box is never created outside the posture the config asks for."""
    if not wanted():
        return
    target = _target(backend, name)
    probe = _ssh(target, "uname -m; ~/.local/bin/runsc --version 2>/dev/null | head -1")
    if probe.returncode != 0:
        raise SystemExit(f"✗ can't reach machine '{name}' over ssh: {probe.stderr.strip()}")
    lines = probe.stdout.splitlines()
    arch = lines[0].strip() if lines else ""
    have = lines[1].strip() if len(lines) > 1 else ""
    if f"release-{GVISOR_RELEASE}" not in have:
        blob = _download(arch)
        _err(f"▶ installing runsc release-{GVISOR_RELEASE} into '{name}' (user-level, no root)…")
        push = _ssh(
            target,
            "mkdir -p ~/.local/bin && cat > ~/.local/bin/runsc.new && "
            "chmod 0755 ~/.local/bin/runsc.new && mv -f ~/.local/bin/runsc.new ~/.local/bin/runsc",
            stdin=blob.read_bytes(),
        )
        if push.returncode != 0:
            raise SystemExit(f"✗ installing runsc into '{name}' failed: {push.stderr.strip()}")
    prov = _ssh(target, guest_script())
    if prov.returncode != 0:
        raise SystemExit(
            f"✗ provisioning the gVisor socket in '{name}' failed:\n{prov.stderr.strip()}"
        )
    # Probe through the FILTERED socket — the box's own path. It forwards to the runsc socket, so
    # a wrong runtime default upstream (or a filter that isn't up) both surface here. Fail-closed:
    # the box is never created against a socket that doesn't answer as gVisor.
    got = _probe_runtime(engine_env(dict(os.environ), backend, name, filtered=True))
    if got != RUNTIME:
        raise SystemExit(
            f"✗ the gVisor socket in '{name}' answers with runtime {got!r}, not {RUNTIME!r} — "
            "refusing to create boxes outside the posture [machine].runtime asks for."
        )
    _err(f"✓ gVisor posture: {RUNTIME} via the box-facing filter on {box_socket()}")
