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
SOCKET = "podman-runsc.sock"
BAKED_ENV = (
    "FY_MACHINE_RUNTIME"  # baked into the box at create: the already-up nag + verify read it
)
_BASE_URL = "https://storage.googleapis.com/gvisor/releases/release"


def _err(*a: object) -> None:
    print(*a, file=sys.stderr)


def wanted() -> bool:
    return config.machine_runtime() == "gvisor"


def guest_socket() -> str:
    """The runsc-default API socket INSIDE the VM. The VM user is the host user's uid on both
    backends (Lima maps it; podman machine creates ``core`` with it), so ``XDG_RUNTIME_DIR`` is
    ``/run/user/<host uid>`` there — the same convention the Lima backend's guest socket uses."""
    return f"/run/user/{os.getuid()}/podman/{SOCKET}"


def container_host(target: SshTarget) -> str:
    """The ``CONTAINER_HOST`` podman-remote uses to reach the runsc socket: ssh to the backend's
    loopback port, then the socket path in the guest. No forward to configure, nothing for the
    VM to restart."""
    return f"ssh://{target.user}@127.0.0.1:{target.port}{guest_socket()}"


def _target(backend: SshBackend, name: str) -> SshTarget:
    target = backend.ssh_target(name)
    if target is None:
        raise SystemExit(
            f'✗ [machine].runtime = "gvisor" needs ssh access to the VM, and the {backend.name} '
            f"backend offers none for machine '{name}' (native has no VM; is it created?)."
        )
    return target


def engine_env(env: dict, backend: SshBackend, name: str) -> dict:
    """The stack env with the engine endpoint swapped for the runsc socket — what ``box up``
    hands the ONE ``podman run`` that creates the box. Everything else (probes, exec, build)
    stays on the default socket: both services share the VM's one libpod store, and the runtime
    is fixed at create."""
    target = _target(backend, name)
    uri = container_host(target)
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


def guest_script() -> str:
    """The idempotent provisioning the VM user runs: the wrapper (flags fixed, override OFF), the
    runtime NAME registered engine-wide as a drop-in (so the DEFAULT service can exec/stop/rm a
    gVisor box — without it that service says the runtime is missing — and so an existing
    containers.conf is never clobbered), the override the second service defaults on, and that
    service as an enabled user unit. Services are restarted only when their inputs changed."""
    return r"""
set -euo pipefail
PODMAN=$(command -v podman)
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
systemctl --user daemon-reload
if [ -n "$runtimes" ]; then systemctl --user try-restart podman.service; fi
if [ -n "$unit$override" ]; then systemctl --user try-restart podman-runsc.service; fi
systemctl --user enable --now podman-runsc.service
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


def _probe_runtime(env: dict) -> str:
    """What the runsc socket's service reports as its default runtime — asked THROUGH the same
    ssh:// endpoint ``box up`` will use, so the probe covers the whole path."""
    out = subprocess.run(
        ["podman", "info", "--format", "{{.Host.OCIRuntime.Name}}"],
        env=env,
        capture_output=True,
        text=True,
        timeout=90,
    )
    return out.stdout.strip() or f"(podman info failed: {out.stderr.strip()[-300:]})"


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
    got = _probe_runtime(engine_env(dict(os.environ), backend, name))
    if got != RUNTIME:
        raise SystemExit(
            f"✗ the gVisor socket in '{name}' answers with runtime {got!r}, not {RUNTIME!r} — "
            "refusing to create boxes outside the posture [machine].runtime asks for."
        )
    _err(f"✓ gVisor posture: {RUNTIME} is the default on {guest_socket()}")
