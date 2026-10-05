"""The scope each credential was last OBSERVED to grant — reported, never enforced (ADR-0031).

foldyard neither narrows nor caps what a credential carries: a GitHub App installation's
permissions ARE the box's scope there, decided in the App's settings where an org owner reviews
them. What foldyard owes instead is that a widening never happens silently. A kind's capability
probe reads the scope (:class:`~foldyard.plugins.CredentialScope`); the supervisor records it here
and, when it differs from the last observation, logs one line and raises one notification.
`fy config widenings` and `fy mode` read the record OFFLINE — a report never calls the provider
(ADR-0023: doctor checks presence, offline).

The record, ``config.credential_scopes_file()``, is keyed by switch, then by the credential's
identity (an App + installation)::

    {"github": {"App 4008762, installation 139125083": {
        "permissions": {"issues": "write", "actions": "read"},
        "reach": "selected repositories",
        "checked": "2026-10-05T12:03:00Z"}}}

plus ``"unread": <when>`` while the probe can't read the scope: the reading shown is then the
last good one, and the reports say it may be stale (:func:`freshness`).

Keyed by identity too, because two worktrees may adopt different credentials under one switch
name: each is its own baseline, and alternating probes of the two must not erase each other's.
Host-side state, written only by the supervisor. Stdlib only.
"""

from __future__ import annotations

import json
import os
from datetime import datetime
from typing import Any, TypeGuard

from . import config
from .plugins import CredentialScope

# The levels a GitHub permission takes today. Any other is recorded as it reads and FLAGGED: a
# level added later must neither hide the rest of the scope nor read as harmless.
_LEVELS = ("read", "write", "admin")


def _read() -> dict[str, Any]:
    try:
        data = json.loads(config.credential_scopes_file().read_text())
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _valid(record: object) -> TypeGuard[dict]:
    return isinstance(record, dict) and isinstance(record.get("permissions"), dict)


def last(switch: str, identity: str) -> dict | None:
    """The last observation of ``identity`` on ``switch``, or None (never probed, or unreadable)."""
    by_identity = _read().get(switch)
    record = by_identity.get(identity) if isinstance(by_identity, dict) else None
    return record if _valid(record) else None


def changes(old: dict, new: dict) -> list[str]:
    """What differs between two records, in words: added, removed, level changed, then reach."""
    before, after = old.get("permissions", {}), new.get("permissions", {})
    out = [f"added {name}:{after[name]}" for name in sorted(after.keys() - before.keys())]
    out += [f"removed {name}:{before[name]}" for name in sorted(before.keys() - after.keys())]
    out += [
        f"{name} {before[name]} → {after[name]}"
        for name in sorted(before.keys() & after.keys())
        if before[name] != after[name]
    ]
    if old.get("reach", "") != new.get("reach", ""):
        out.append(f"reach {old.get('reach') or '?'} → {new.get('reach') or '?'}")
    return out


def observe(switch: str, scope: CredentialScope, checked: str) -> tuple[str, list[str]]:
    """Record ``scope`` as observed at ``checked``, returning ``(event, changes)``: ``"baseline"``
    (the first observation of this credential — nothing to compare), ``"same"``, or
    ``"changed"`` with :func:`changes`' list. Atomic replace, so a concurrent reader never sees a
    torn file. Raises ``OSError`` on a failed write (the caller logs it).

    The read-modify-write is unlocked because there is ONE writer: the file is per project
    (``state_dir``), the project's supervisor is a singleton (its lock), and its tick runs the
    probes one after another. Running probes concurrently would need a lock here first."""
    data = _read()
    by_identity = data.get(switch)
    if not isinstance(by_identity, dict):
        by_identity = data[switch] = {}
    previous = by_identity.get(scope.identity)
    record = {"permissions": dict(scope.permissions), "reach": scope.reach, "checked": checked}
    by_identity[scope.identity] = record
    _write(data)
    if not _valid(previous):
        return "baseline", []
    diff = changes(previous, record)
    return ("changed", diff) if diff else ("same", [])


def mark_unread(switch: str, identity: str, when: str) -> dict | None:
    """Note on ``identity``'s record that a probe at ``when`` couldn't read the scope, keeping the
    first such time, and return the record — or None, writing nothing, when there is no record
    (nothing is shown, so nothing is stale). The next :func:`observe` replaces the record, mark
    and all. Raises ``OSError`` on a failed write, like :func:`observe`."""
    data = _read()
    by_identity = data.get(switch)
    record = by_identity.get(identity) if isinstance(by_identity, dict) else None
    if not _valid(record):
        return None
    if not record.get("unread"):
        record["unread"] = when
        _write(data)
    return record


def _write(data: dict) -> None:
    path = config.credential_scopes_file()
    tmp = path.with_name(path.name + ".tmp")
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")
    os.replace(tmp, path)


def elevated(permissions: dict[str, str]) -> list[str]:
    """The permission names held at anything but ``read``, sorted: ``write``, ``admin``, and a
    level foldyard doesn't recognise, which may be either. What lets the box CHANGE things through
    the credential, which is what a report should make impossible to miss."""
    return sorted(name for name, level in permissions.items() if level != "read")


def unrecognised(permissions: dict[str, str]) -> list[str]:
    """The names at a level foldyard doesn't know, sorted (a subset of :func:`elevated`)."""
    return sorted(name for name, level in permissions.items() if level not in _LEVELS)


def ago(checked: str, now: datetime) -> str:
    """How long before ``now`` the ISO time ``checked`` was, coarsely (``43m ago``): enough to
    answer "is this reading stale?". ``""`` when it doesn't parse."""
    try:
        seconds = (now - datetime.fromisoformat(checked)).total_seconds()
    except (ValueError, TypeError):
        return ""
    for unit, size in (("d", 86400), ("h", 3600), ("m", 60)):
        if seconds >= size:
            return f"{int(seconds // size)}{unit} ago"
    return "just now"


def freshness(record: dict, now: datetime) -> str:
    """When ``record`` was read (``probed 2h ago``), and that it may be stale when the probe has
    failed to read the scope since (:func:`mark_unread`)."""
    checked = str(record.get("checked") or "?")
    out = f"probed {ago(checked, now) or checked}"
    if record.get("unread"):
        out += f"; unreadable since {ago(str(record['unread']), now) or '?'}, may be stale"
    return out


def summary(record: dict) -> str:
    """One line for a record: ``name:level`` sorted, elevated levels in capitals, then the reach."""
    permissions = record.get("permissions") or {}
    flagged = set(elevated(permissions))
    parts = [
        f"{name}:{level.upper() if name in flagged else level}"
        for name, level in sorted(permissions.items())
    ]
    text = ", ".join(parts) or "no permissions"
    return f"{text} — {record['reach']}" if record.get("reach") else text
