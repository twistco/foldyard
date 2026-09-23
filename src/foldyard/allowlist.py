#!/usr/bin/env python3
"""Egress allow-store — the live, leveled allowlist behind the Network Log "allow this host" UX.

Companion to :mod:`foldyard.devmode`. When ``[proxy] default_deny`` is on, the egress proxy refuses
any host that isn't allowed. A host can be allowed at three levels:

  once         a short TTL (default 2 min), then auto-reverts — "let this one through, I'm watching"
  session      until the host supervisor restarts — "don't ask again this task"
  permanent    no expiry; survives a supervisor restart

Enforcement itself (``default_deny``) is host-owned too — see :func:`default_deny`. A LEARN
window (:func:`start_learning`) suspends enforcement until a deadline, while the proxy records
what it would have refused; enforcement resumes by itself when it lapses, and :func:`learned_hosts`
turns what was recorded into one reviewed batch of grants.

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
import re
import sys
import tempfile
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

from . import config

LEVELS = ("once", "session", "permanent")
ONCE_TTL_SECONDS = 120  # "allow once" lifetime before it auto-reverts
# `once` when it's offered ahead of work that takes a while — a recommendation at launch, a host
# the build gate asks about: the default 120 s can lapse before a build step reaches the network.
OFFER_ONCE_SECONDS = 900
LEARN_DEFAULT_SECONDS = 3600  # a learn window's length unless the operator says otherwise
LEARN_MAX_SECONDS = 8 * 3600  # an open wall is a lapse, not a posture — devmode.MAX_TTL's cap


def in_box() -> bool:
    return config.in_box()


def _now() -> datetime:
    return datetime.now(UTC)


def _iso(dt: datetime) -> str:
    return dt.astimezone(UTC).isoformat(timespec="seconds")


def _aware(iso: object) -> datetime | None:
    """``iso`` as a timezone-AWARE datetime, else None. The store's own timestamps are always
    written aware (:func:`_iso`); a naive one is damage — comparing it with :func:`_now` raises
    ``TypeError`` rather than failing closed."""
    dt = _parse(iso) if isinstance(iso, str) else None
    return dt if dt is not None and dt.tzinfo is not None else None


def _parse(iso: str | None) -> datetime | None:
    try:
        return datetime.fromisoformat(iso) if iso else None
    except (ValueError, TypeError):
        return None


def valid_host(host: str) -> bool:
    """A plausible host / ``*.suffix`` glob, optionally ``:port`` — no scheme, path, whitespace,
    and at least one dot. Keeps junk (and TOML-breaking text) out of the persisted allowlist.

    A bare host grants the HTTPS tunnel (``:443``) only; ``host:port`` grants a CONNECT tunnel to
    that one port and nothing else — the shape the proxy's blocked row carries when the port was
    the reason (``github.com:22``), so the TUI's allow action round-trips it unchanged."""
    host = host.strip()
    if not host or any(c.isspace() for c in host) or "/" in host or "://" in host:
        return False
    if ":" in host:
        host, _, port = host.rpartition(":")
        if ":" in host or not port.isdigit() or not 1 <= int(port) <= 65535:
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
        # A present `expires` must be an aware time: read as "no expiry" a once-grant would be
        # permanent, and compared as it is it crashes the prune instead of failing closed.
        bad = sorted(
            h
            for h, entry in hosts.items()
            if not isinstance(entry, dict)
            or (entry.get("expires") is not None and _aware(entry.get("expires")) is None)
        )
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
    for key in ("learn", "learned"):
        # A learn window SUSPENDS enforcement, so a malformed one is damage like a non-bool
        # default_deny: reading it as "no window" would be harmless, but reading a half-written one
        # as open could leave the wall down — refuse it and let the readers fail closed.
        if key in raw:
            w = raw[key]
            if not isinstance(w, dict) or any(_aware(w.get(f)) is None for f in ("since", "until")):
                raise StoreUnreadable(f"{path}: `{key}` is malformed")
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
        raw = _checked_raw()
    except StoreUnreadable as e:
        # Fail CLOSED: a damaged store must not hand enforcement back to `[proxy] default_deny`,
        # which is repo config the box can write — that would turn "my allow-store broke" into
        # "the yard switched its own wall off".
        _warn(f"egress allow-store unreadable ({e}) — ENFORCING until it's repaired or removed")
        return True
    if _open_window(raw) is not None:
        return False  # a learn window: observe until its deadline, then enforce again
    stored = raw.get("default_deny")
    # `_checked_raw` already rejected a present-but-non-bool value, so this is a bool or absent.
    # An unanswered "learn" seed ENFORCES: the window opens at a launch verb (seed_learning), and
    # until then nothing has said "open".
    return stored if stored is not None else config.proxy_default_deny()


def set_wall(on: bool) -> dict:
    """Turn enforcement on/off in the host store (Mac only). Returns the new effective. Ends a
    learn window early (its record is kept, so `fy allow learn` can still review it)."""
    _require_host()
    _require_readable_store()
    doc = _load_raw()
    doc["default_deny"] = bool(on)
    _close_window(doc)
    _save_raw(doc)
    return write_effective()


# ── the learn window (observe, record, then enforce by itself) ───────────────────────


def _open_window(raw: dict) -> dict | None:
    """The store's learn window if it is still open, else None."""
    w = raw.get("learn")
    if not isinstance(w, dict):
        return None
    until = _parse(w.get("until"))
    return w if until is not None and until > _now() else None


def _close_window(doc: dict) -> bool:
    """Move an open-or-lapsed ``learn`` window to ``learned`` (ending it now if still open), so
    the record of what was observed outlives the window. Returns True when there was one."""
    w = doc.pop("learn", None)
    if not isinstance(w, dict):
        return False
    until = _parse(w.get("until"))
    end = min(until, _now()) if until is not None else _now()
    doc["learned"] = {"since": w.get("since"), "until": _iso(end)}
    return True


def learning() -> dict | None:
    """The open learn window ``{since, until}``, or None. A damaged store is None — no window —
    which with :func:`default_deny` failing closed means enforcing."""
    try:
        return _open_window(_checked_raw())
    except StoreUnreadable:
        return None


def last_window() -> dict | None:
    """The window a review should read: the open one, else the last one closed. None if there has
    never been one."""
    try:
        raw = _checked_raw()
    except StoreUnreadable:
        return None
    w = raw.get("learn") or raw.get("learned")
    return {"since": w["since"], "until": w["until"]} if isinstance(w, dict) else None


def start_learning(seconds: int = LEARN_DEFAULT_SECONDS) -> dict:
    """Open a learn window: enforcement is suspended until now + ``seconds`` (capped at
    :data:`LEARN_MAX_SECONDS`), the proxy records every host it WOULD have refused, and when the
    window lapses the wall ENFORCES — whatever it was before. That last part is the point: a
    window cannot be forgotten into an open wall, which ``fy allow wall off`` can. Mac only.
    Returns the window."""
    _require_host()
    _require_readable_store()
    seconds = max(1, min(int(seconds), LEARN_MAX_SECONDS))
    now = _now()
    doc = _load_raw()
    since = _unreviewed_since(doc) or now
    window = {"since": _iso(since), "until": _iso(now + timedelta(seconds=seconds))}
    doc["learn"] = window
    doc["default_deny"] = True  # what resumes when the window lapses
    _save_raw(doc)
    write_effective()
    return window


def _unreviewed_since(doc: dict) -> datetime | None:
    """Where the review of the previous window would start, if any of it is still unreviewed.

    `fy allow learn` reviews ONE window, so a window replaced before its review would take every
    host only it had seen out of the workflow. The replacement reaches back instead: to the
    previous window's start, or to the last review if that fell inside it. The stretch in between
    adds nothing — the wall was enforcing, and a review reads only would-block rows."""
    prior = doc.get("learn") or doc.get("learned")
    if not isinstance(prior, dict):
        return None
    since, until = _aware(prior.get("since")), _aware(prior.get("until"))
    if since is None or until is None:
        return None
    reviewed = _aware(doc.get("reviewed"))
    if reviewed is None or reviewed <= since:
        return since
    return reviewed if reviewed < min(until, _now()) else None


def mark_reviewed() -> None:
    """Record that the operator was shown the current window's review (`fy allow learn`), so the
    next window starts fresh rather than carrying this one. Host-side, best-effort."""
    if in_box():
        return
    try:
        doc = _checked_raw()
    except StoreUnreadable:
        return
    doc["reviewed"] = _iso(_now())
    _save_raw(doc)


def seed_learning(echo: Callable[[str], None]) -> dict | None:
    """First launch on a checkout whose ADOPTED config seeds ``[proxy] default_deny = "learn"``:
    open the first learn window and say so, loudly. Only when the store has never answered
    (no ``default_deny``, no window past or present) — so it happens once, and an operator's own
    ``fy allow wall …`` always wins. Returns the window, or None when nothing was started.

    The seed is repo config, but it can only ever buy a BOUNDED open window before enforcing —
    strictly less than ``default_deny = false``, which the same repo could already write."""
    if in_box() or config.proxy_default_deny_seed() != "learn":
        return None
    try:
        raw = _checked_raw()
    except StoreUnreadable:
        return None
    if any(k in raw for k in ("default_deny", "learn", "learned")):
        return None
    window = start_learning(LEARN_DEFAULT_SECONDS)
    until = _parse(window["until"])
    at = until.astimezone().strftime("%H:%M") if until else window["until"]
    echo(
        f"▶ egress wall: LEARNING until {at} (first run) — the box's egress is allowed and every "
        "host the wall would refuse is recorded. Enforcement resumes by itself; review and grant "
        "what it saw with `fy allow learn` (`fy allow wall on` ends it now)."
    )
    return window


def _matches(key: str, patterns: list[str]) -> bool:
    """The proxy's grant semantics for a recorded host key — the addon's ``_host_matches`` over the
    whole key: an exact grant, or a ``*.suffix`` one (subdomains, not the bare domain). A
    ``host:port`` key is matched the same way, so ``*.example.com:22`` answers
    ``git.example.com:22`` as the proxy does, and a bare grant never answers a ``host:port``."""
    return any(key.endswith(p[1:]) if p.startswith("*.") else key == p for p in patterns)


def _printable(text: object, limit: int = 120) -> str:
    """``text`` cut to printable characters (no terminal escapes), capped."""
    if not isinstance(text, str):
        return ""
    return "".join(c for c in text if c.isprintable())[:limit]


# What a sampled path keeps: its first segments, enough to name the package or endpoint
# (`/react`, `/@types/node`, `/simple/requests`) and never the query string, where tokens live.
_PATH_SEGMENTS = 2
_PATH_SEGMENT_MAX = 40  # longer reads as an id or a token, not a name — shown as `…`
_PATH_EXAMPLES = 3
# The characters a learned `why` may carry: enough for tool tokens and package paths, and none
# that can end a TOML string or open markup. Box-originated text, so allowlisted, not escaped.
_WHY_SAFE = set("._/+-@~…")


def _sample_path(path: object) -> str:
    """A request path cut down to what explains a host: no query or fragment, the first
    :data:`_PATH_SEGMENTS` segments, an over-long segment replaced by ``…``. '' when nothing is
    left."""
    if not isinstance(path, str):
        return ""
    path = path.split("?", 1)[0].split("#", 1)[0]
    segments = [seg for seg in path.split("/") if seg][:_PATH_SEGMENTS]
    kept = [seg if len(seg) <= _PATH_SEGMENT_MAX else "…" for seg in segments]
    return "/" + "/".join(kept) if kept else ""


def _why_safe(text: str) -> str:
    return "".join(c for c in text if c.isalnum() or c in _WHY_SAFE)


def recommend_why(entry: dict) -> str:
    """The ``why`` for a learned host's ``[proxy] recommend`` line — labelled as what it is: an
    OBSERVATION from box traffic (``observed: npm/10.8.2 GET /react, /lodash (+4) — edit me``),
    not a reason. The reason is the operator's to write before committing; the box chose every
    byte here, and teammates see this text at their own consent prompt. Restricted to characters
    that can't break out of a TOML string."""
    uas = entry.get("uas") or []
    tool = _why_safe(uas[0].split()[0])[:40] if uas and uas[0].split() else ""
    parts = [tool] if tool else []
    paths = [_why_safe(p)[: _PATH_SEGMENT_MAX * _PATH_SEGMENTS] for p in entry.get("paths") or []]
    paths = [p for p in paths if p]
    if paths:
        more = entry.get("more_paths", 0)
        parts.append("GET " + ", ".join(paths) + (f" (+{more})" if more else ""))
    return f"observed: {' '.join(parts) or 'no detail (tunnelled)'} — edit me"


def read_log_rows(paths: list[Path]) -> list[dict]:
    """Every JSON row in ``paths`` (oldest file first), skipping unreadable files and malformed
    lines. The review reads WHOLE files, unlike the TUI's bounded tail: a window can be hours old,
    and the would-block rows are rate-limited, so they are few among the request rows."""
    rows: list[dict] = []
    for path in paths:
        try:
            lines = path.read_text().splitlines()
        except OSError:
            continue
        for line in lines:
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if isinstance(row, dict):
                rows.append(row)
    return rows


def learned_hosts(rows: list[dict], window: dict) -> list[dict]:
    """The hosts the proxy recorded as WOULD-BLOCK inside ``window``, still not granted and not
    declined: ``{host, count, first, last, uas, paths, more_paths}`` in first-seen order. ``uas``
    is up to three distinct User-Agents — which TOOL reached the host. ``paths`` samples what it
    FETCHED there, from the decrypted request rows for the same host in the window (a tunnelled
    host has none): the attribution a reviewer needs without anything logging commands in the box.
    Pure over the log rows (tests pass them directly).

    Every field here was written from traffic the BOX originated, so it is untrusted: a host that
    isn't a valid grant is dropped (``grant`` would refuse it mid-batch), and a User-Agent is cut
    to printable characters before it reaches the operator's terminal."""
    since, until = _parse(window.get("since")), _parse(window.get("until"))
    granted = live_hosts()
    refused = declined()
    out: dict[str, dict] = {}
    fetched: dict[str, list[str]] = {}  # host → distinct sampled paths, first-seen order
    for row in rows:
        ts, key = _parse(row.get("ts")), row.get("host")
        if not key or ts is None or since is None or until is None or not since <= ts <= until:
            continue
        if not row.get("would_block"):
            # A decrypted request row (method + path): what the tool fetched, joined below by
            # host. Tunnel, blocked and would-block rows carry no path.
            if row.get("method") and isinstance(key, str):
                sample = _printable(_sample_path(row.get("path")))
                if sample and sample not in fetched.setdefault(key, []):
                    fetched[key].append(sample)
            continue
        if not isinstance(key, str) or not valid_host(key) or key.startswith("*."):
            continue
        if _matches(key, granted) or key in refused or "*" in refused:
            continue
        entry = out.setdefault(key, {"host": key, "count": 0, "first": row["ts"], "uas": []})
        entry["count"] += 1
        entry["last"] = row["ts"]
        ua = _printable(row.get("ua"))
        if ua and ua not in entry["uas"] and len(entry["uas"]) < 3:
            entry["uas"].append(ua)
    for key, entry in out.items():
        # A `host:port` key was a non-TLS-port tunnel — its requests (if any were decrypted) are
        # logged under the bare host, which may also be a different, granted :443 host; don't mix.
        seen = [] if ":" in key else fetched.get(key, [])
        entry["paths"] = seen[:_PATH_EXAMPLES]
        entry["more_paths"] = max(0, len(seen) - _PATH_EXAMPLES)
    return list(out.values())


def build_refusals(rows: list[dict], since: datetime) -> list[dict]:
    """The hosts the wall REFUSED an image build since ``since``, still not granted:
    ``{host, count, uas, paths, more_paths}`` in first-seen order (the ``learned_hosts`` shape, so
    :func:`recommend_why` reads it). Only rows the proxy attributed to a build (its ``build`` flag,
    from the build marker) count — a box session refused in the same minute is not the build's.
    A build is tunnelled, so there are never paths. Box-originated text, handled as in
    :func:`learned_hosts`."""
    granted = live_hosts()
    out: dict[str, dict] = {}
    for row in rows:
        ts, key = _parse(row.get("ts")), row.get("host")
        if not (row.get("blocked") and row.get("build")) or ts is None or ts < since:
            continue
        if not isinstance(key, str) or not valid_host(key) or _matches(key, granted):
            continue
        entry = out.setdefault(
            key, {"host": key, "count": 0, "uas": [], "paths": [], "more_paths": 0}
        )
        entry["count"] += 1
        ua = _printable(row.get("ua"))
        if ua and ua not in entry["uas"] and len(entry["uas"]) < 3:
            entry["uas"].append(ua)
    return list(out.values())


def recommend_block(entries: list[dict]) -> list[str]:
    """The ``[proxy] recommend`` TOML for ``entries`` (``learned_hosts``/``build_refusals``
    shape), each ``why`` the labelled observation of :func:`recommend_why` — ready to paste, and
    to reword before committing."""
    lines = ["  [proxy]", "  recommend = ["]
    lines += [f'    {{ host = "{e["host"]}", why = "{recommend_why(e)}" }},' for e in entries]
    return [*lines, "  ]"]


_TABLE_HEADER = re.compile(r"^\s*\[\[?[^\]]*\]\]?\s*(#.*)?$")
_PROXY_HEADER = re.compile(r"^\s*\[proxy\]\s*(#.*)?$")
_RECOMMEND_KEY = re.compile(r"^\s*recommend\s*=\s*")


def _array_bounds(text: str, start: int) -> tuple[int, int] | None:
    """For the TOML array whose ``[`` is at ``start``: ``(close, last)`` — the index of its
    matching ``]`` and of the last significant character inside it (``start`` itself when empty).
    Strings and comments are skipped, so a bracket in either never counts. ``None`` if it never
    closes."""
    depth, last, i = 0, start, start
    while i < len(text):
        ch = text[i]
        if ch == "#":
            i = text.find("\n", i)
            if i < 0:
                return None
            continue
        if ch in "\"'":
            triple = text.startswith(ch * 3, i)
            quote = ch * 3 if triple else ch
            j = i + len(quote)
            while j < len(text) and not text.startswith(quote, j):
                j += 2 if ch == '"' and text[j] == "\\" else 1
            if j >= len(text):
                return None
            i = j + len(quote)
            last = i - 1
            continue
        if ch == "[":
            depth += 1
        elif ch == "]":
            depth -= 1
            if depth == 0:
                return i, last
        if not ch.isspace():
            last = i
        i += 1
    return None


def with_recommends(text: str, entries: list[dict]) -> str | None:
    """``text`` (a ``foldyard.toml``) with ``entries`` appended to ``[proxy] recommend`` — hosts
    already recommended skipped, their ``why`` the observation of :func:`recommend_why`. Edits the
    TEXT, so comments and layout survive, then proves it: the result must parse to exactly the
    original plus the new entries, or this returns ``None`` and the caller prints the block
    instead. A file it can't parse, or a ``recommend`` that isn't a list, is ``None`` too."""
    import tomllib

    try:
        before = tomllib.loads(text)
    except tomllib.TOMLDecodeError:
        return None
    proxy = before.get("proxy", {})
    existing = proxy.get("recommend", []) if isinstance(proxy, dict) else None
    if not isinstance(existing, list):
        return None
    have = {
        e if isinstance(e, str) else e.get("host") for e in existing if isinstance(e, str | dict)
    }
    new = [e for e in entries if e["host"] not in have]
    if not new:
        return text
    items = [f'{{ host = "{e["host"]}", why = "{recommend_why(e)}" }}' for e in new]
    block = "".join(f"  {item},\n" for item in items)

    lines = text.splitlines(keepends=True)
    header = next((n for n, line in enumerate(lines) if _PROXY_HEADER.match(line)), None)
    if header is None:
        sep = "" if not text or text.endswith("\n\n") else ("\n" if text.endswith("\n") else "\n\n")
        out = f"{text}{sep}[proxy]\nrecommend = [\n{block}]\n"
    else:
        end = next(
            (n for n in range(header + 1, len(lines)) if _TABLE_HEADER.match(lines[n])),
            len(lines),
        )
        key = next((n for n in range(header + 1, end) if _RECOMMEND_KEY.match(lines[n])), None)
        if key is None:
            at = sum(len(line) for line in lines[: header + 1])
            out = f"{text[:at]}recommend = [\n{block}]\n{text[at:]}"
        else:
            offset = sum(len(line) for line in lines[:key])
            matched = _RECOMMEND_KEY.match(lines[key])
            assert matched  # `key` was chosen by this same match
            opening = text.find("[", offset + matched.end() - 1)
            bounds = _array_bounds(text, opening) if opening >= 0 else None
            if bounds is None:
                return None
            close, last = bounds
            comma = "" if text[last] in "[," else ","
            line_start = text.rfind("\n", 0, close) + 1
            if text[line_start:close].strip():  # `]` shares its line: an inline list
                out = f"{text[: last + 1]}{comma} {', '.join(items)}{text[last + 1 :]}"
            else:
                head = f"{text[: last + 1]}{comma}{text[last + 1 : line_start]}"
                out = f"{head}{block}{text[line_start:]}"
    try:
        after = tomllib.loads(out)
    except tomllib.TOMLDecodeError:
        return None
    added = [{"host": e["host"], "why": recommend_why(e)} for e in new]
    expected = {**before, "proxy": {**proxy, "recommend": [*existing, *added]}}
    return out if after == expected else None


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
    supervisor restarts, ``[o]nce`` for :data:`OFFER_ONCE_SECONDS` (a broad host needed for one
    build — it is offered again once it lapses), ``[n]ot now`` (the default — asked again next
    launch), ``ne[v]er`` records a decline. In the box it is a no-op (grants are host-side only).
    Injected ``prompt``/``echo`` like ``configpin.resolve`` — tests drive it with no real stdin.

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
                f"[o]nce ({OFFER_ONCE_SECONDS // 60} min) · [n]ot now (default) · ne[v]er: "
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
        elif answer in ("o", "once"):
            grant(e["host"], "once", OFFER_ONCE_SECONDS)
            echo(
                f"  ✓ {e['host']} allowed (once — {OFFER_ONCE_SECONDS // 60} min, then asked again)"
            )
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
        raw = _checked_raw()
    except StoreUnreadable as e:
        # The supervisor tick must not die on it; the fail-closed readers above already cover the
        # posture, and the operator sees the reason.
        _warn(f"egress allow-store unreadable ({e}) — skipping the expiry sweep")
        return False
    pruned, changed = _prune(raw.get("hosts", {}))
    # A lapsed learn window is closed into its `learned` record — enforcement is ALREADY back
    # (default_deny() stops honouring a window at its deadline); this just tidies the store.
    lapsed = "learn" in raw and _open_window(raw) is None
    if changed or lapsed:
        doc = _load_raw()
        doc["hosts"] = pruned
        if lapsed:
            _close_window(doc)
        _save_raw(doc)
        write_effective()
    return changed or lapsed
