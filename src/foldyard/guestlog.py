"""guestlog.py — the machine VM's log budget, provisioned by ``machine ensure`` on every backend
with a VM.

Containers log to journald (podman's default driver), so the guest journal IS the container
logs — and Fedora's default cap is min(10% of the filesystem, 4G): measured 2026-09-15 on a
90G VM disk, 4.1G of journal, the largest single producer being the rootless API service's
own access log at ``--log-level=info`` (the TUI/doctor polling ``_ping`` and list-containers,
~48k lines per 2h), then conmon relaying container stdout.

Two knobs, two privilege levels:

* the **journald cap** is a root-owned ``/etc`` drop-in. On Lima root is boot-time only
  (machine.py), so it is rendered into the root boot script there (:func:`journal_snippet`
  with no sudo) and rides the provisioning id; podman machine's CoreOS appliance takes no
  boot script but keeps its user's passwordless sudo, so there it goes over the backend's ssh
  with ``sudo -n``.
* the **API log level** is a user-level ``podman.service`` drop-in — the stock unit runs
  ``podman $LOGGING system service`` with ``LOGGING=--log-level=info`` — applied over ssh on
  both backends, restarting the service only when the file changed. The gVisor posture's own
  second service (sandbox.py) passes no ``--log-level`` and so already sits at podman's default.

Best-effort throughout: this is housekeeping, not a posture, so a guest that refuses is a
warning, never the abort a failed wall or gVisor provisioning is.
"""

from __future__ import annotations

import sys

from .sandbox import SshBackend, _ssh

JOURNAL_MAX_USE = "1G"  # keeps a useful `podman logs` window for a chatty worker; 4G was the cap
API_LOG_LEVEL = "warn"  # podman's own default; the stock unit's LOGGING= raises it to info
_JOURNAL_DROPIN = "/etc/systemd/journald.conf.d/50-foldyard-cap.conf"
_SERVICE_DROPIN = "~/.config/systemd/user/podman.service.d/50-foldyard-loglevel.conf"


def _err(*a: object) -> None:
    print(*a, file=sys.stderr)


def journal_snippet(*, sudo: str) -> str:
    """The journald cap as idempotent bash: written, journald restarted and the backlog vacuumed
    ONLY when the drop-in's content differs (a steady-state boot or ``fy up`` touches nothing).
    ``sudo`` prefixes every root step — ``""`` where the caller already is root."""
    p = f"{sudo} " if sudo else ""
    content = f"[Journal]\\nSystemMaxUse={JOURNAL_MAX_USE}\\n"
    return f"""
# foldyard: cap the guest journal (= the container logs) at {JOURNAL_MAX_USE}.
{p}install -d -m 0755 /etc/systemd/journald.conf.d
if ! printf '{content}' | {p}cmp -s - {_JOURNAL_DROPIN} 2>/dev/null; then
  printf '{content}' | {p}tee {_JOURNAL_DROPIN} >/dev/null
  {p}systemctl restart systemd-journald
  {p}journalctl --vacuum-size={JOURNAL_MAX_USE} >/dev/null 2>&1 || true
  echo "journal: capped at {JOURNAL_MAX_USE}"
fi
"""


def user_script() -> str:
    """The user-level API log-level drop-in, same write-only-when-changed contract; the service
    is restarted (socket-activated, so the next request revives it) only on a change."""
    content = f"[Service]\\nEnvironment=LOGGING=--log-level={API_LOG_LEVEL}\\n"
    return f"""
set -euo pipefail
# foldyard: the rootless API service logs every request at info by default — quieten it.
mkdir -p ~/.config/systemd/user/podman.service.d
if ! printf '{content}' | cmp -s - {_SERVICE_DROPIN} 2>/dev/null; then
  printf '{content}' > {_SERVICE_DROPIN}
  systemctl --user daemon-reload
  systemctl --user try-restart podman.service
  echo "podman.service: log level {API_LOG_LEVEL}"
fi
"""


def ensure(backend: SshBackend, name: str) -> None:
    """Apply the log budget to the running VM ``name`` over the backend's ssh; a backend without
    a VM (native) has nothing to budget."""
    target = backend.ssh_target(name)
    if target is None:
        return
    script = user_script()
    if backend.name == "podman":
        script += journal_snippet(sudo="sudo -n")
    res = _ssh(target, script)
    if res.returncode != 0:
        _err(f"⚠ the log budget for '{name}' didn't apply (journal cap / API log level):")
        _err(f"  {res.stderr.strip() or res.stdout.strip()}")
        return
    for line in res.stdout.splitlines():
        if line.strip():
            _err(f"▶ {name}: {line.strip()}")
