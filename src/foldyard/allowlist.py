#!/usr/bin/env python3
"""Egress allow-store — the live, leveled allowlist behind the Network Log "allow this host" UX.

Companion to :mod:`foldyard.devmode`. When ``[proxy] default_deny`` is on, the egress proxy refuses
any host that isn't allowed. A host can be allowed at three levels:

  once         a short TTL (default 2 min), then auto-reverts — "let this one through, I'm watching"
  session      until the host supervisor restarts — "don't ask again this task"
  permanent    no expiry; survives a supervisor restart

Enforcement itself (``default_deny``) is host-owned too — see :func:`default_deny`.

EVERY level lives in one authoritative store in the Mac home (``allow-store.json``, OUTSIDE the repo
mount), so **nothing in the box can grant its own egress**. That placement is the whole
guarantee, so
permanent grants live there too rather than in the repo's ``foldyard.toml``: config travels with the
branch, and a store the box can edit is not a store — it read as "team-shared" but meant "the box
widens its own wall by writing a file it already owns". A grant is a property of an operator on a
host, like the posture itself and like the trust the mode system already keeps out of the mount.

The proxy daemon (a standalone mitmproxy addon that can't import foldyard) re-reads a resolved
*effective* file (``allow-effective.json``) per request, so grants take effect live with no daemon
restart. The host writes that file on every grant and on the supervisor's expiry sweep.

Stdlib only. Grants are Mac-only (the box must not escalate its own posture).
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

from . import config

LEVELS = ("once", "session", "permanent")
ONCE_TTL_SECONDS = 120  # "allow once" lifetime before it auto-reverts


def in_box() -> bool:
    return config.in_box()


def _now() -> datetime:
    return datetime.now(UTC)


def _iso(dt: datetime) -> str:
    return dt.astimezone(UTC).isoformat(timespec="seconds")


def _parse(iso: str | None) -> datetime | None:
    try:
        return datetime.fromisoformat(iso) if iso else None
    except (ValueError, TypeError):
        return None


def valid_host(host: str) -> bool:
    """A plausible host / ``*.suffix`` glob — no scheme, path, whitespace, and at least one dot.
    Keeps junk (and TOML-breaking text) out of the persisted allowlist."""
    host = host.strip()
    if not host or any(c.isspace() for c in host) or "/" in host or "://" in host:
        return False
    bare = host[2:] if host.startswith("*.") else host
    return "." in bare and all(part for part in bare.split("."))


# ── the live store (once / session) ──────────────────────────────────────────────────


class StoreUnreadable(Exception):
    """The allow-store exists but can't be read as our JSON object.

    Distinct from ABSENT, which is just first run. A control this file backs must never be weakened
    by its own damage, so readers fail CLOSED (enforce, grant nothing) and mutators refuse rather
    than rebuild — silently recreating it would drop every grant it held."""


def _load_raw() -> dict:
    """The store document. ``{}`` when the file doesn't exist yet (first run); raises
    :class:`StoreUnreadable` when it exists but is damaged."""
    path = config.allow_store_file()
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text())
    except (OSError, ValueError) as e:
        raise StoreUnreadable(f"{path}: {e}")
    if not isinstance(raw, dict):
        raise StoreUnreadable(f"{path}: expected a JSON object, got {type(raw).__name__}")
    return raw


def _write_json(path: Path, payload: dict) -> None:
    """Write ATOMICALLY — a temp file in the same dir, then ``os.replace``. Both files this module
    writes are read by someone else while we write: the proxy addon re-reads the effective file per
    request (a torn read would spuriously block egress — it fails closed), and a concurrent
    `fy allow` / supervisor sweep would otherwise observe a truncated store.

    The temp name is UNIQUE per writer (``mkstemp``). A fixed one only moves the tear: two
    concurrent writers — a `fy allow` per worktree, the supervisor's sweep — would write the same
    scratch file, and whoever replaced first would publish the other's half-written bytes."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    tmp = Path(name)
    try:
        with os.fdopen(fd, "w") as fh:
            fh.write(json.dumps(payload, indent=2) + "\n")
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)  # never leave scratch files beside the store
        raise


def _save_raw(doc: dict) -> None:
    doc["written"] = _iso(_now())
    _write_json(config.allow_store_file(), doc)


def _checked_raw() -> dict:
    """:func:`_load_raw` plus FIELD validation. Absent fields are first run; PRESENT-but-wrong-
    shaped ones are damage and raise, because reading them as defaults is how a hand-edit or a
    partial restore silently drops every grant (an empty ``hosts`` we then rewrite) or hands
    enforcement back to the repo seed the box can write (a non-bool ``default_deny``)."""
    raw = _load_raw()
    path = config.allow_store_file()
    if "hosts" in raw:
        hosts = raw["hosts"]
        if not isinstance(hosts, dict):
            raise StoreUnreadable(f"{path}: `hosts` is not an object")
        bad = sorted(h for h, entry in hosts.items() if not isinstance(entry, dict))
        if bad:
            raise StoreUnreadable(f"{path}: malformed entries for {', '.join(bad)}")
    if "default_deny" in raw and not isinstance(raw["default_deny"], bool):
        raise StoreUnreadable(
            f"{path}: `default_deny` is {type(raw['default_deny']).__name__}, not a bool"
        )
    if "declined" in raw:
        declined = raw["declined"]
        if not isinstance(declined, dict) or any(
            not isinstance(v, dict) for v in declined.values()
        ):
            raise StoreUnreadable(f"{path}: `declined` is malformed")
    return raw


def _load_store() -> dict[str, dict]:
    """The grants, validated (see :func:`_checked_raw`)."""
    return _checked_raw().get("hosts", {})


def _warn(msg: str) -> None:
    print(f"⚠ {msg}", file=sys.stderr, flush=True)


def _save_store(hosts: dict[str, dict]) -> None:
    doc = _load_raw()
    doc["hosts"] = hosts
    _save_raw(doc)


def default_deny() -> bool:
    """Is the egress wall ENFORCING? Host-owned, like the grants.

    ``[proxy] default_deny`` is only a SEED: it says what the project wants the first time, and
    from then on the answer lives in the host store. It can't stay authoritative — repo config is
    writable from inside the box, so a committed enforcement switch is one the yard can flip OFF for
    itself, which is strictly worse than the per-host grants we already moved out (that widened the
    wall by one host; this drops it entirely). Change it with ``fy allow wall on|off``."""
    try:
        stored = _checked_raw().get("default_deny")
    except StoreUnreadable as e:
        # Fail CLOSED: a damaged store must not hand enforcement back to `[proxy] default_deny`,
        # which is repo config the box can write — that would turn "my allow-store broke" into
        # "the yard switched its own wall off".
        _warn(f"egress allow-store unreadable ({e}) — ENFORCING until it's repaired or removed")
        return True
    # `_checked_raw` already rejected a present-but-non-bool value, so this is a bool or absent.
    return stored if stored is not None else config.proxy_default_deny()


def set_wall(on: bool) -> dict:
    """Turn enforcement on/off in the host store (Mac only). Returns the new effective."""
    _require_host()
    _require_readable_store()
    doc = _load_raw()
    doc["default_deny"] = bool(on)
    _save_raw(doc)
    return write_effective()


def _prune(hosts: dict[str, dict]) -> tuple[dict[str, dict], bool]:
    """Drop expired ``once`` grants. Returns (hosts, changed)."""
    keep = {}
    for host, entry in hosts.items():
        exp = _parse(entry.get("expires"))
        if exp is not None and exp <= _now():
            continue
        keep[host] = entry
    return keep, len(keep) != len(hosts)


def live_hosts() -> list[str]:
    """Every non-expired grant (once / session / permanent) — the patterns the proxy allows. A
    damaged store grants NOTHING (fail closed, matching :func:`default_deny`)."""
    return [g["host"] for g in grants()]


def grants() -> list[dict]:
    """The non-expired grants WITH their metadata — ``{host, level, expires}`` sorted by host,
    for the surfaces that manage them (the TUI's wall pane). Damaged store ⇒ ``[]``, same
    fail-closed posture as :func:`live_hosts` (which is this, reduced to the patterns)."""
    try:
        hosts, _ = _prune(_load_store())
    except StoreUnreadable as e:
        _warn(f"egress allow-store unreadable ({e}) — granting nothing until it's repaired")
        return []
    return [
        {"host": h, "level": e.get("level", "?"), "expires": e.get("expires")}
        for h, e in sorted(hosts.items())
    ]


# ── effective allowlist (what the proxy reads) ───────────────────────────────────────


def effective() -> dict:
    """The resolved allowlist the egress proxy enforces — every grant in the host-side store. The
    injector host + in-stack ``NO_PROXY`` hosts are handled proxy-side, not here."""
    return {"default_deny": default_deny(), "allow": live_hosts()}


def write_effective() -> dict:
    """Write the effective file the proxy daemon re-reads per request. Host-side (Mac) only."""
    payload = effective()
    _write_json(config.allow_effective_file(), payload)
    return payload


# ── grants / revokes (Mac only) ──────────────────────────────────────────────────────


def _require_host() -> None:
    if in_box():
        raise SystemExit(
            "✗ egress allows are Mac-only: the box must not grant its own egress "
            "(the allow-store lives in the Mac home, outside the shared mount)."
        )


def _require_readable_store() -> None:
    """Mutators refuse on a damaged store instead of rebuilding it — a rebuild would silently drop
    every grant it held. The fix is a human one, so name the file. Validates the GRANTS too
    (``_checked_raw``), not just that the file parses."""
    try:
        _checked_raw()
    except StoreUnreadable as e:
        raise SystemExit(
            f"✗ {e}\n  Repair or delete that file, then re-run (deleting = no grants)."
        )


def grant(host: str, level: str, ttl: int | None = None) -> dict:
    """Allow ``host`` at ``level`` (once|session|permanent). Mac only. Returns the new effective."""
    _require_host()
    _require_readable_store()
    host = host.strip()
    if level not in LEVELS:
        raise SystemExit(f"✗ unknown level {level!r} (have: {', '.join(LEVELS)})")
    if not valid_host(host):
        raise SystemExit(f"✗ not a valid host/glob: {host!r}")

    hosts, _ = _prune(_load_store())
    expires = _iso(_now() + timedelta(seconds=ttl or ONCE_TTL_SECONDS)) if level == "once" else None
    hosts[host] = {"level": level, "expires": expires, "added": _iso(_now())}
    doc = _load_raw()
    doc["hosts"] = hosts
    # Granting supersedes a standing "never" on the same host: the operator changed their mind,
    # and a live grant shadowed by a decline record would make the recommendation surfaces lie.
    if isinstance(doc.get("declined"), dict):
        doc["declined"].pop(host, None)
    _save_raw(doc)
    return write_effective()


def revoke(host: str) -> dict:
    """Remove ``host`` from the store at any level. Mac only."""
    _require_host()
    _require_readable_store()
    host = host.strip()
    hosts, _ = _prune(_load_store())
    if host in hosts:
        del hosts[host]
        _save_store(hosts)
    return write_effective()


def clear_ephemeral() -> dict:
    """Drop all once/session grants, KEEPING permanent ones. For supervisor start — 'until restart'
    grants end here. Mac only."""
    _require_host()
    _require_readable_store()
    hosts, _ = _prune(_load_store())
    _save_store({h: e for h, e in hosts.items() if e.get("level") == "permanent"})
    return write_effective()


# ── repo-recommended grants (`[proxy] recommend`) ────────────────────────────────────
# The repo may RECOMMEND hosts (config.proxy_recommend — advisory, never enforced); the operator
# answers per host, and both answers land here in the host store: a yes is an ordinary grant, a
# "never" is a decline record so the offer stops re-asking. Enforcement never reads declines —
# they only silence offers, so damage to them can't widen or narrow the wall.


def declined() -> set[str]:
    """Hosts the operator answered "never" to. Unreadable store ⇒ EVERYTHING is declined —
    matching the fail-closed readers above: a broken store must not re-open offers (whose
    accept path would refuse anyway, see :func:`_require_readable_store`)."""
    try:
        raw = _checked_raw()
    except StoreUnreadable:
        return {"*"}
    return set(raw.get("declined", {}))


def decline(host: str) -> None:
    """Record "never offer ``host`` again" (Mac only). Undone by granting it (any level) —
    :func:`grant` drops the record — or by re-adding it by hand with `fy allow add`."""
    _require_host()
    _require_readable_store()
    host = host.strip()
    if not valid_host(host):
        raise SystemExit(f"✗ not a valid host/glob: {host!r}")
    doc = _load_raw()
    declined = doc.setdefault("declined", {})
    if not isinstance(declined, dict):  # pragma: no cover — _checked_raw already rejected this
        return
    declined[host] = {"declined": _iso(_now())}
    _save_raw(doc)


def recommendations() -> list[dict]:
    """Everything ASKING to be granted for the bound config, in offer order: the repo's
    ``[proxy] recommend`` first, then the active plugins' packaged asks
    (:meth:`foldyard.plugins.Plugin.egress_recommend` — Claude Code's installer, npm for Codex),
    de-duplicated by host with the repo's ``why`` winning.

    Two sources, one consent path, because they differ only in who WROTE the list: the repo's is
    box-writable and travels with the branch, a plugin's ships with the tool. Neither grants
    anything — both land in :func:`offer_recommendations`, which asks the operator host by host."""
    out = list(config.proxy_recommend())
    seen = {e["host"] for e in out}
    from .plugins import registry  # lazy: allowlist is on the import-light hot path

    for entry in registry().egress_recommend():
        if entry["host"] not in seen and valid_host(entry["host"]):
            seen.add(entry["host"])
            out.append(entry)
    return out


def pending_recommendations() -> list[dict]:
    """The BOUND config's recommendations (:func:`recommendations`) still awaiting an answer: not
    granted at any live level, not declined. Callers bind the ADOPTED config first (``devmode.
    worktree_config`` does) so a recommendation only reaches an operator after the file carrying
    it survived the adoption gate — an in-box edit can at most queue an ask for NEXT adoption
    (and a plugin's ask only surfaces once the adopted copy declares that plugin's table)."""
    refused = declined()
    if "*" in refused:
        return []
    granted = set(live_hosts())
    return [e for e in recommendations() if e["host"] not in granted and e["host"] not in refused]


def offer_recommendations(
    *,
    interactive: bool,
    prompt: Callable[[str], str],
    echo: Callable[[str], None],
    accept_all: bool = False,
) -> dict[str, int]:
    """Offer the pending recommendations, one host at a time — the consent moment that makes a
    shared allowlist safe. Returns ``{granted, declined, deferred}`` counts.

    Per host: ``[y]es`` grants PERMANENT (the team-baseline intent), ``[s]ession`` until the
    supervisor restarts, ``[n]ot now`` (the default — asked again next launch), ``ne[v]er``
    records a decline. In the box it is a no-op (grants are host-side only). Injected
    ``prompt``/``echo``
    like ``configpin.resolve`` — tests drive it with no real stdin.

    ``accept_all`` grants every pending host permanently WITHOUT asking — the unattended path
    (`fy allow sync --yes`), for a first box-up with no terminal to answer on. It is a separate,
    explicitly-typed decision rather than a fallback: a non-interactive caller that didn't ask for
    it gets the list and no state change, because "nobody was there to say no" must never read as
    yes. Only ever reachable from a host CLI invocation an operator typed."""
    if in_box():
        return {"granted": 0, "declined": 0, "deferred": 0}
    pending = pending_recommendations()
    counts = {"granted": 0, "declined": 0, "deferred": len(pending)}
    if not pending:
        return counts
    n = len(pending)
    echo(f"▶ {n} recommended egress host{'s' if n != 1 else ''} not yet granted:")
    if accept_all:
        for e in pending:
            grant(e["host"], "permanent")
            echo(f"  ✓ {e['host']} allowed (permanent)" + (f" — {e['why']}" if e["why"] else ""))
        return {"granted": n, "declined": 0, "deferred": 0}
    if not interactive:
        for e in pending:
            echo(f"    {e['host']}" + (f" — {e['why']}" if e["why"] else ""))
        echo(
            "  No terminal here — review with `fy allow sync` (or the TUI's Network Log), "
            "or grant them all unattended with `fy allow sync --yes`."
        )
        return counts
    for e in pending:
        why = f" — {e['why']}" if e["why"] else ""
        answer = (
            prompt(
                f"  allow {e['host']}{why}?  [y]es permanent · [s]ession · "
                f"[n]ot now (default) · ne[v]er: "
            )
            .strip()
            .lower()
        )
        # EXACT matches, like the adoption gate: anything unrecognised defers, which changes
        # nothing and asks again next time.
        if answer in ("y", "yes"):
            grant(e["host"], "permanent")
            echo(f"  ✓ {e['host']} allowed (permanent)")
            counts["granted"] += 1
            counts["deferred"] -= 1
        elif answer in ("s", "session"):
            grant(e["host"], "session")
            echo(f"  ✓ {e['host']} allowed (session — until the supervisor restarts)")
            counts["granted"] += 1
            counts["deferred"] -= 1
        elif answer in ("v", "never"):
            decline(e["host"])
            echo(f"  ✗ {e['host']} declined — not offered again (`fy allow add` re-allows)")
            counts["declined"] += 1
            counts["deferred"] -= 1
        else:
            echo(f"    ({e['host']} deferred — you'll be offered it again)")
    return counts


def sweep() -> bool:
    """Expire lapsed ``once`` grants; rewrite the store + effective file if anything changed.
    Returns True when it changed. Called from the supervisor tick. Mac only."""
    if in_box():
        return False
    try:
        hosts = _load_store()
    except StoreUnreadable as e:
        # The supervisor tick must not die on it; the fail-closed readers above already cover the
        # posture, and the operator sees the reason.
        _warn(f"egress allow-store unreadable ({e}) — skipping the expiry sweep")
        return False
    pruned, changed = _prune(hosts)
    if changed:
        _save_store(pruned)
        write_effective()
    return changed
