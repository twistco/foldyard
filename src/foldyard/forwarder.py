"""forwarder.py — the attributing forwarder (``[machine].attribution = "record"``, ADR-0034).

The egress proxy treats every client as one principal, *something in the VM*: by the time a
container's connection reaches it, pasta and Lima's user-mode network have each re-made it, and
nothing on the host can say which container sent it. A root forwarder in the lima guest sits where
each container still has its own address, is made the only way to the host-side daemons, and
stamps each proxy request with where it came from under an HMAC the proxy verifies
(``assets/forwarder/fwd.py``). This is step 1 of the ADR — attribute and record: rows in the
egress log carry the verified stamp, the host resolves it to a container at read time
(:mod:`foldyard.attribution`), and nothing is decided on it yet.

Three pieces, split by who holds root, as for the guest monitor (:mod:`foldyard.monitor`):

* **The boot script** (``assets/forwarder/guest-boot.sh``, rendered by :func:`render`) is its own
  ``provision: mode: system`` entry in lima.yaml under its own marker, so turning the forwarder
  on never changes the wall script's id or the monitor's. It installs the forwarder, the key, an
  OCI hook and a ``containers.conf`` drop-in, and starts the unit, at every boot. Once turned on
  and back off, an ``off`` rendering stays recorded and removes the install at boot. Nothing in
  the VM firewall's own script changes: the forwarder's table and policy routes do their work
  beside it (see fwd.py's module docstring).
* **The key** (``<state dir>/forwarder.key``) is kept host-side, travels inside the recorded boot
  script, and is written root-only in the guest. The proxy reads it by path from its live
  settings — never from its launch environment (ADR-0030).
* **The report** — ``/run/fy-fwd/{applied,state,netns,stats}``, world-readable — is what the host
  reads back over ssh (:func:`guest_report`), as the VM user, without root.

A forwarder that isn't serving takes containers' egress away (it fails closed), so ``fy up``
says so loudly and ``fy doctor`` keeps the row red.
"""

from __future__ import annotations

import hashlib
import os
import secrets
import subprocess
from dataclasses import dataclass
from pathlib import Path

from . import config
from .sandbox import SshBackend, _ssh

# First line after the shebang of the rendered boot script; lima.yaml carries it verbatim. A
# regex-literal string (set_provision matches entries by it), mentioning no other script's marker.
MARKER = "# fy-forwarder "


def wanted() -> bool:
    return config.machine_attribution() == "record"


def _asset(name: str) -> Path:
    return Path(__file__).resolve().parent / "assets" / "forwarder" / name


def key_path() -> Path:
    return config.forwarder_key_file()


def key() -> str:
    """The key the forwarder stamps with and the proxy verifies with: 32 random bytes, hex, kept
    host-side (``<state dir>/forwarder.key``, 0600) and created on first use. One per project,
    like the VM; it stays put across boots (it is in the provisioning id, as the monitor's is)."""
    path = key_path()
    try:
        text = path.read_text().strip()
    except OSError:
        text = ""
    if len(text) != 64:
        path.parent.mkdir(parents=True, exist_ok=True)
        text = secrets.token_hex(32)
        tmp = path.with_suffix(".tmp")
        tmp.unlink(missing_ok=True)
        # 0600 from the create: a write-then-chmod leaves the key readable in between
        with os.fdopen(os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w") as f:
            f.write(text + "\n")
        tmp.replace(path)
    return text


def ports() -> tuple[str, str]:
    """``(proxy ports, other daemon ports)`` in the VM firewall's nft spelling: this project's
    proxy listeners' span (stamped) and its token services' (forwarded untouched). The same spans
    the firewall opens (``machine._wall_ports``)."""
    proxy, minter = config.proxy_port_base(), config.gcp_minter_port_base()
    plain = f"{minter}-{minter + 89}" if minter != proxy else ""
    return f"{proxy}-{proxy + 89}", plain


def render(on: bool) -> tuple[str, str]:
    """The boot script for ``on`` (record) or off, and its id — a hash of the rendered content,
    so a new forwarder, a moved port band or a new key reads as a different recording."""
    proxy, plain = ports()
    body = (
        _asset("guest-boot.sh")
        .read_text()
        .replace("@@MODE@@", "record" if on else "off")
        .replace("@@FWD@@", _asset("fwd.py").read_text().rstrip("\n") if on else "")
        .replace("@@WAIT@@", _asset("wait.sh").read_text().rstrip("\n") if on else "")
        .replace("@@KEY@@", key() if on else "")
        .replace("@@GW@@", config.LIMA_HOST_GATEWAY)
        .replace("@@PROXY_PORTS@@", proxy)
        .replace("@@PLAIN_PORTS@@", plain)
    )
    ident = hashlib.sha256(body.encode()).hexdigest()[:16]
    return body.replace("@@ID@@", ident), ident


# ── the guest's report ────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Report:
    """What the guest says (``/run/fy-fwd/*``) plus the unit's state — empty strings where a file
    is missing."""

    applied: str = ""
    state: str = ""
    netns: str = ""
    stats: str = ""
    service: str = ""

    def problem(self, want_id: str) -> str:
        """``""`` when the forwarder is serving as recorded, else why not, in one line."""
        if not self.applied:
            return "the guest wrote no forwarder report — its boot script did not run"
        if self.applied != f"applied {want_id}":
            return f"the guest applied a different recording ({self.applied!r})"
        if self.service != "active":
            return f"fy-fwd.service is {self.service or 'unknown'} ({self.state or 'no state'})"
        if self.state != f"serving {want_id}":
            return f"the forwarder reports {self.state or 'nothing'!r}"
        return ""


_REPORT = (
    "for f in applied state netns stats; do "
    'echo "$(cat /run/fy-fwd/$f 2>/dev/null)"; done; '
    "systemctl is-active fy-fwd.service 2>/dev/null || true"
)


def guest_report(backend: SshBackend, name: str) -> Report | None:
    """The guest's report over the backend's ssh, as the VM user — ``None`` when unreachable."""
    target = backend.ssh_target(name)
    if target is None:
        return None
    try:
        res = _ssh(target, _REPORT)
    except (subprocess.TimeoutExpired, OSError):
        return None
    if res.returncode != 0:
        return None
    lines = [*res.stdout.splitlines(), *[""] * 5][:5]
    return Report(*(line.strip() for line in lines))
