"""Show every project's Lima VM in Podman Desktop at once.

Podman Desktop's Lima extension shows ONE instance, named by ``lima.name`` and read once when
the extension starts — with several foldyard projects it is always the wrong one. Its podman
extension has a better door: with "Load remote system connections (ssh)" on
(``podman.system.connections.remote``), it polls ``podman system connection list`` every 5s and
shows each ``ssh://`` connection as its own entry, live. So foldyard registers one connection per
VM, ``fy-<machine>``, pointing at the VM's rootless podman socket over Lima's own ssh forward and
key, and says where the setting is when it's off (the setting is only ever read — see
:func:`remote_state`).

Podman Desktop tracks those connections by NAME: a connection whose port moved keeps its old
tunnel until Podman Desktop restarts. Lima picks a fresh ssh port at every boot, so
:mod:`foldyard.machine` pins it to the project's band first — the entry then survives reboots.

Followed where Podman Desktop is installed (:func:`following`): ``machine.ensure`` keeps the
connection current when its settings file exists, and leaves the VM's port and podman's connection
list alone where it doesn't — they belong to another tool, and nobody there would look.
``FOLDYARD_PODMAN_DESKTOP`` overrides the detection either way (``0`` opts out, ``1`` forces it,
e.g. for a settings file somewhere we don't look); `fy machine desktop` does it on demand. Never
the DEFAULT connection — bare ``podman`` on the host keeps talking to what it did. Best-effort
throughout: never a reason to fail a verb.

Stdlib only.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .machine_backend import SshTarget

REMOTE_KEY = "podman.system.connections.remote"


def settings_path() -> Path:
    return Path.home() / ".local/share/containers/podman-desktop/configuration/settings.json"


_ON = ("1", "true", "yes", "on")
_OFF = ("0", "false", "no", "off")


def choice() -> bool | None:
    """The operator's explicit choice, from their shell — a preference about THIS machine across
    every project, so it is not a foldyard.toml key. ``None`` when unset (or unrecognised)."""
    value = os.environ.get("FOLDYARD_PODMAN_DESKTOP", "").strip().lower()
    return True if value in _ON else False if value in _OFF else None


def following() -> bool:
    """Keep this machine's VMs listed in Podman Desktop: the operator's choice if they made one,
    else whether Podman Desktop is here — its settings file exists once it has run as this user."""
    chosen = choice()
    return chosen if chosen is not None else remote_state() != "absent"


def connection_name(machine: str) -> str:
    return f"fy-{machine}"


def uri(target: SshTarget, guest_socket: str) -> str:
    return f"ssh://{target.user}@127.0.0.1:{target.port}{guest_socket}"


def _podman() -> str | None:
    return shutil.which("podman")


def _run(argv: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(argv, capture_output=True, text=True)


def _connections(podman: str) -> list[dict] | None:
    res = _run([podman, "system", "connection", "list", "--format", "json"])
    if res.returncode != 0:
        return None
    try:
        rows = json.loads(res.stdout or "[]")
    except ValueError:
        return None
    return [r for r in rows if isinstance(r, dict)] if isinstance(rows, list) else None


def register(name: str, uri: str, identity: str) -> str:
    """Make ``name`` point at ``uri``: ``added`` · ``updated`` · ``unchanged`` · ``no-podman`` ·
    ``would-default`` · ``failed``. Restores the previous default if podman promoted the new
    connection; with NO default to restore (an empty list) it adds nothing — podman makes a first
    connection the default and can't be told otherwise, which would repoint bare ``podman`` on
    the host at the VM."""
    podman = _podman()
    if podman is None:
        return "no-podman"
    rows = _connections(podman)
    if rows is None:
        return "failed"
    mine = next((r for r in rows if r.get("Name") == name), None)
    if mine and mine.get("URI") == uri and mine.get("Identity") == identity:
        return "unchanged"
    default = next(
        (r.get("Name") for r in rows if r.get("Default") and r.get("Name") != name), None
    )
    if default is None and mine is None:
        return "would-default"
    if mine and _run([podman, "system", "connection", "remove", name]).returncode != 0:
        return "failed"
    add = [podman, "system", "connection", "add", name, uri, "--identity", identity]
    if _run(add).returncode != 0:
        return "failed"
    now = _connections(podman) or []
    if default and any(r.get("Name") == name and r.get("Default") for r in now):
        _run([podman, "system", "connection", "default", default])
    return "updated" if mine else "added"


def unregister(name: str) -> str:
    """``removed`` · ``absent`` · ``no-podman`` · ``failed``."""
    podman = _podman()
    if podman is None:
        return "no-podman"
    rows = _connections(podman)
    if rows is None:
        return "failed"
    if not any(r.get("Name") == name for r in rows):
        return "absent"
    ok = _run([podman, "system", "connection", "remove", name]).returncode == 0
    return "removed" if ok else "failed"


def remote_state() -> str:
    """Is "Load remote system connections (ssh)" on: ``on`` · ``off`` · ``absent`` (no settings
    file — not installed or never started) · ``unreadable``. READ ONLY: Podman Desktop writes its
    in-memory settings back on quit, so an edit made while it runs is silently undone — the
    operator flips the switch in its Preferences, which it applies live."""
    try:
        doc = json.loads(settings_path().read_text())
    except FileNotFoundError:
        return "absent"
    except (OSError, ValueError):
        return "unreadable"
    if not isinstance(doc, dict):
        return "unreadable"
    return "on" if doc.get(REMOTE_KEY) is True else "off"


def messages(name: str, registered: str, remote: str, *, verbose: bool = False) -> list[str]:
    """What the operator should know: the registration if it changed, and — with it, or when
    asked (``verbose``) — where the switch is if it's off. The steady state says nothing."""
    out = {
        "added": [f"▶ Podman Desktop: added connection '{name}' (`podman system connection`)"],
        "updated": [
            f"▶ Podman Desktop: '{name}' moved to a new ssh port — restart Podman Desktop if it "
            "still shows the old one"
        ],
        "no-podman": [
            f"⚠ no podman CLI on PATH — Podman Desktop lists '{name}' through it; skipped"
        ],
        "would-default": [
            f"⚠ '{name}' not registered for Podman Desktop: podman has no connection yet, so it "
            "would become the default one bare `podman` uses — add yours first, or export "
            "FOLDYARD_PODMAN_DESKTOP=0 to stop trying"
        ],
        "failed": [f"⚠ couldn't register '{name}' with `podman system connection` — skipped"],
    }.get(registered, [])
    if remote in ("off", "unreadable") and (verbose or registered in ("added", "updated")):
        out.append(
            '  To see it: Podman Desktop → Settings → Preferences, search "remote", turn on '
            '"Load remote system connections (ssh)" (applies live)'
        )
    return out
