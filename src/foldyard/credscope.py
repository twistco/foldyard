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

Keyed by identity too, because two worktrees may adopt different credentials under one switch
name: each is its own baseline, and alternating probes of the two must not erase each other's.
Host-side state, written only by the supervisor. Stdlib only.
"""

from __future__ import annotations

import json
import os
from typing import Any

from . import config
from .plugins import CredentialScope

_ELEVATED = ("write", "admin")


def _read() -> dict[str, Any]:
    try:
        data = json.loads(config.credential_scopes_file().read_text())
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _valid(record: object) -> bool:
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
    torn file. Raises ``OSError`` on a failed write (the caller logs it)."""
    data = _read()
    by_identity = data.get(switch)
    if not isinstance(by_identity, dict):
        by_identity = data[switch] = {}
    previous = by_identity.get(scope.identity)
    record = {"permissions": dict(scope.permissions), "reach": scope.reach, "checked": checked}
    by_identity[scope.identity] = record
    path = config.credential_scopes_file()
    tmp = path.with_name(path.name + ".tmp")
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")
    os.replace(tmp, path)
    if not _valid(previous):
        return "baseline", []
    diff = changes(previous, record)
    return ("changed", diff) if diff else ("same", [])


def elevated(permissions: dict[str, str]) -> list[str]:
    """The permission names held at ``write`` or ``admin``, sorted — what lets the box CHANGE
    things through the credential, which is what a report should make impossible to miss."""
    return sorted(name for name, level in permissions.items() if level in _ELEVATED)


def summary(record: dict) -> str:
    """One line for a record: ``name:level`` sorted, elevated levels in capitals, then the reach."""
    permissions = record.get("permissions") or {}
    parts = [
        f"{name}:{level.upper() if level in _ELEVATED else level}"
        for name, level in sorted(permissions.items())
    ]
    text = ", ".join(parts) or "no permissions"
    return f"{text} — {record['reach']}" if record.get("reach") else text
