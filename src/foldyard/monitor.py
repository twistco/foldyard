"""monitor.py — the guest monitor (``[machine].monitor = "observe"``, ADR-0031).

A root eBPF collector — Tetragon, a pinned release — runs in the lima VM and records process,
connection and selected file activity for every container and the VM itself. It observes; it
blocks nothing. Three pieces, split by who holds root:

* **The boot script** (``assets/monitor/guest-boot.sh``, rendered by :func:`render`) is a SECOND
  ``provision: mode: system`` entry in lima.yaml, beside machine.py's ``fy-provision`` one, with
  its own marker and id: turning the monitor on never changes the wall script's id, so it never
  makes anyone else's VM stale. It runs as root at every boot, writes Tetragon's config and the
  packaged policy, foldyard's own units, and a root helper (``assets/monitor/fy-monitor.sh``).
  Once turned on and back off, an ``off`` rendering stays recorded and removes the install at boot.
* **The release** is downloaded by the HOST, checked against the sha256 pinned here, cached
  under ``~/.foldyard/cache/`` and streamed over ssh as the VM user into an inbox only that user
  can write — the same route as the gVisor runtime (sandbox.py). Root is boot-time only in the
  guest, so a root ``.path`` unit the boot script installed picks the delivery up, copies it into
  root-owned space and checks THAT copy against the hash the boot script carried. The transport
  is untrusted; the recorded hash is the anchor.
* **The report** — ``/run/fy-monitor/{applied,artifact,policy}``, world-readable — is what the host
  reads back over ssh (:func:`guest_report`), as the VM user, without root.

Attribution (which container did what) is NOT Tetragon's: its container id is a sub-cgroup name
the container's creator chooses (docs/ebpf-monitoring-spike.md, finding 5). The boot script turns
on ``enable-process-ns``; mapping each event's mount namespace to a container is the relay's job.

Failure here never aborts a launch verb: the monitor only observes, so a guest that did not
apply it is a visible warning (``fy up``, ``fy doctor``), never a reason to refuse the box.
"""

from __future__ import annotations

import hashlib
import secrets
import shlex
import subprocess
import sys
import time
import urllib.request
from dataclasses import dataclass, replace
from pathlib import Path

from . import config
from .sandbox import SshBackend, _ssh

TETRAGON_VERSION = "v1.7.1"
# The release tarballs' sha256, from the release's own .sha256sum files (checked 2026-10-03).
# Pinned HERE, not fetched beside the download: the hash is the trust anchor the guest re-checks.
TETRAGON_SHA256 = {
    "x86_64": "2f36a7bbb2b3d77a011383a01c8f804fe7761d934a76ff2fdf99ebad6f3e89ae",
    "aarch64": "aa71d12eddd962003e9fd78e3b3534185af5b2728c7d21ab3cb0f7fe2dc87702",
}
_RELEASE_ARCH = {"x86_64": "amd64", "aarch64": "arm64"}
_BASE_URL = "https://github.com/cilium/tetragon/releases/download"

# First line after the shebang of the rendered boot script; lima.yaml carries it verbatim.
MARKER = "# fy-monitor "
POLICY = "fy-observe"
_INBOX = "/var/lib/fy-monitor/inbox"


def _err(*a: object) -> None:
    print(*a, file=sys.stderr)


def wanted() -> bool:
    return config.machine_monitor() == "observe"


def _asset(name: str) -> Path:
    return Path(__file__).resolve().parent / "assets" / "monitor" / name


def relay_key() -> str:
    """The key the guest relay signs its spool with and the host verifies it with: 32 random
    bytes, hex, kept host-side (``<state dir>/monitor-relay.key``, 0600) and created on first use.
    One per project, like the VM. It travels to the guest inside the recorded boot script, which
    writes it root-only — so it is also in the provisioning id, and stays put across boots."""
    path = config.state_dir() / "monitor-relay.key"
    try:
        key = path.read_text().strip()
    except OSError:
        key = ""
    if len(key) != 64:
        path.parent.mkdir(parents=True, exist_ok=True)
        key = secrets.token_hex(32)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(key + "\n")
        tmp.chmod(0o600)
        tmp.replace(path)
    return key


def render(on: bool) -> tuple[str, str]:
    """The boot script for ``on`` (observe) or off, and its id — a hash of the rendered content,
    so a new release, policy or script reads as a different recording."""
    body = (
        _asset("guest-boot.sh")
        .read_text()
        .replace("@@MODE@@", "observe" if on else "off")
        .replace("@@HELPER@@", _asset("fy-monitor.sh").read_text().rstrip("\n"))
        .replace("@@RELAY@@", _asset("relay.py").read_text().rstrip("\n"))
        .replace("@@RELAY_KEY@@", relay_key() if on else "")
        .replace("@@POLICY@@", _asset("policy.yaml").read_text().rstrip("\n"))
        .replace("@@VERSION@@", TETRAGON_VERSION)
        .replace("@@SHA_X86_64@@", TETRAGON_SHA256["x86_64"])
        .replace("@@SHA_AARCH64@@", TETRAGON_SHA256["aarch64"])
    )
    ident = hashlib.sha256(body.encode()).hexdigest()[:16]
    return body.replace("@@ID@@", ident), ident


# ── the release: host-downloaded, pinned, verified, cached ────────────────────────────


def release_url(arch: str) -> str:
    name = f"tetragon-{TETRAGON_VERSION}-{_RELEASE_ARCH[arch]}.tar.gz"
    return f"{_BASE_URL}/{TETRAGON_VERSION}/{name}"


def _cache_dir() -> Path:
    return Path.home() / ".foldyard" / "cache"


def _fetch(url: str) -> bytes:
    with urllib.request.urlopen(url, timeout=300) as resp:
        return resp.read()


def download(arch: str) -> Path:
    """The pinned release for the GUEST's ``uname -m``, from the host's cache or GitHub, checked
    against :data:`TETRAGON_SHA256` before anything is written (and again on every cache hit:
    the cache is the operator's, but a truncated file must never reach the guest)."""
    if arch not in TETRAGON_SHA256:
        raise ValueError(f"no pinned Tetragon release for architecture {arch!r}")
    want = TETRAGON_SHA256[arch]
    dest = _cache_dir() / f"tetragon-{TETRAGON_VERSION}-{_RELEASE_ARCH[arch]}.tar.gz"
    if dest.exists() and hashlib.sha256(dest.read_bytes()).hexdigest() == want:
        return dest
    url = release_url(arch)
    _err(f"▶ downloading Tetragon {TETRAGON_VERSION} ({arch}) for the guest monitor…")
    body = _fetch(url)
    if hashlib.sha256(body).hexdigest() != want:
        raise ValueError(f"the download from {url} failed its sha256 check")
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_suffix(".part")
    part.write_bytes(body)
    part.replace(dest)
    return dest


# ── the guest's report ────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Report:
    """What the guest says it applied (``/run/fy-monitor/*``) plus Tetragon's unit state —
    empty strings where a file is missing. ``arch`` is the guest's ``uname -m``."""

    arch: str = ""
    applied: str = ""
    artifact: str = ""
    policy: str = ""
    service: str = ""
    relay: str = ""

    def problem(self, want_id: str) -> str:
        """``""`` when the monitor is observing as recorded, else why not, in one line."""
        if not self.applied:
            return "the guest wrote no monitor report — its boot script did not run"
        if self.applied != f"applied {want_id}":
            return f"the guest applied a different recording ({self.applied!r})"
        if not self.artifact.startswith("installed"):
            return f"Tetragon is not installed ({self.artifact or 'no status'})"
        if self.service != "active":
            return f"tetragon.service is {self.service or 'unknown'}"
        if self.policy != f"loaded {POLICY}":
            return f"the {POLICY} policy is {self.policy or 'unknown'}"
        if self.relay != "active":
            return f"the relay (fy-monitor-relay.service) is {self.relay or 'unknown'}"
        return ""


_REPORT = (
    "uname -m; for f in applied artifact policy; do "
    'echo "$(cat /run/fy-monitor/$f 2>/dev/null)"; done; '
    "systemctl is-active tetragon.service 2>/dev/null || true; "
    "systemctl is-active fy-monitor-relay.service 2>/dev/null || true"
)


def guest_report(backend: SshBackend, name: str) -> Report | None:
    """The guest's report over the backend's ssh, as the VM user — ``None`` when unreachable."""
    target = backend.ssh_target(name)
    if target is None:
        return None
    try:
        res = _ssh(target, _REPORT)
    except subprocess.TimeoutExpired:
        return None
    if res.returncode != 0:
        return None
    lines = [*res.stdout.splitlines(), *[""] * 6][:6]
    return Report(*(line.strip() for line in lines))


def _deliver(backend: SshBackend, name: str, blob: Path) -> str:
    """Stream the release into the inbox (``.part`` then a rename, so the root .path unit only
    ever sees a whole file). ``""`` on success, else the reason."""
    target = backend.ssh_target(name)
    if target is None:
        return "no ssh route into the VM"
    dest = f"{_INBOX}/tetragon.tar.gz"
    res = _ssh(
        target,
        f"cat > {shlex.quote(dest)}.part && mv -f {shlex.quote(dest)}.part {shlex.quote(dest)}",
        stdin=blob.read_bytes(),
    )
    return "" if res.returncode == 0 else (res.stderr.strip() or f"exit {res.returncode}")


def ensure(backend: SshBackend, name: str, want_id: str, *, wait: float = 600.0) -> str:
    """Bring the running VM's monitor to the recording ``want_id``: deliver the release if the
    guest is waiting for one, then wait for Tetragon to load the policy. Returns ``""`` when
    observing, else the problem — the caller warns, never aborts (the monitor only observes)."""
    report = guest_report(backend, name)
    if report is None:
        return "can't reach the VM over ssh to read its monitor report"
    if report.applied != f"applied {want_id}":
        return report.problem(want_id)
    if report.artifact.startswith(("awaiting", "rejected")):
        if report.arch not in TETRAGON_SHA256:
            return f"no pinned Tetragon release for the guest's architecture {report.arch!r}"
        try:
            blob = download(report.arch)
        except Exception as e:  # any download failure is the same warning
            return f"downloading Tetragon failed: {e}"
        _err(f"▶ installing the guest monitor (Tetragon {TETRAGON_VERSION}) into '{name}'…")
        why = _deliver(backend, name, blob)
        if why:
            return f"copying Tetragon into the VM failed: {why}"
        report = replace(report, artifact="installing")  # the guest's .path unit has it now
    deadline = time.monotonic() + wait
    while True:
        problem = report.problem(want_id)
        settling = (
            report.artifact.startswith(("awaiting", "installing"))
            or report.policy in ("loading", "not installed")
            # the relay starts with Tetragon, and restarts every 5 s until its export exists
            or report.relay in ("activating", "inactive", "")
        )
        if not problem or not settling or time.monotonic() >= deadline:
            return problem
        time.sleep(5)
        report = guest_report(backend, name) or report
