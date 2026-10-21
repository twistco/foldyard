"""monitorhooks.py — the guest monitor's hooks: the operator's program or endpoint, called with
batches of attributed events (docs/monitor-pipeline.md §6). How a scoring script, a decision model
or a notifier plugs in without being part of foldyard.

**Configured host-side only** — ``<state dir>/monitor-hooks.toml`` (``~/.foldyard/<project>/``),
never ``foldyard.toml``. Repo config is writable by anything in the box: a ``command`` there would
be host code execution for the box (ADR-0023), and a ``url`` would send every event wherever the
box chose. The file is re-read every round, so an edit applies within seconds; there is nothing
to adopt, because nothing in the mount can reach it.

Delivery: only SETTLED events (their attribution can't change any more: a later snapshot exists,
or :data:`SETTLE_SECONDS` passed), and never one past an event that hasn't settled yet;
at-least-once (a hook's cursor advances only on success — exit 0, or HTTP 2xx); retried with
backoff, and after :data:`MAX_ATTEMPTS` the batch is skipped and counted. Each hook reads the store
from its OWN position, so one that fell behind (a catch-up pull, a long backoff) resumes where it
stopped rather than at the store's tail; only the store rotating its position away loses events,
and that is logged.

Commands run with a minimal environment — never the supervisor's, which holds every
``host.env`` secret — on their own thread, so a slow hook never delays the pull. A ``file`` hook
is foldyard's own sink (:mod:`foldyard.monitorsink`): each batch appended as one signed, chained
line, never through a symlink — no shell append into a path the box can write.

Nothing else in foldyard writes the file but :func:`add`/:func:`remove` (``fy monitor hook``),
which append a block or remove one and keep every hand edit, refusing what they can't do exactly.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import shlex
import subprocess
import threading
import time
import tomllib
import urllib.request
from collections.abc import Callable
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path

from . import config, monitorlog, monitorsink

SCHEMA = "fy.monitor.events/1"
STAGES = ("events",)  # later: scored, decided, reviewed (docs/monitor-pipeline.md §5)
SETTLE_SECONDS = 120.0
BATCH_MAX = 500
MAX_ATTEMPTS = 5
ROUND_EVERY = 5.0
READ_BYTES = 4 << 20  # how far past its position a hook reads each round
LEAD_BYTES = 1 << 20  # and before it: the snapshot the first events join against (one a minute)


def hooks_file() -> Path:
    return config.state_dir() / "monitor-hooks.toml"


def _state_file() -> Path:
    return config.state_dir() / "monitor-hooks-state.json"


# ── configuration ─────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Hook:
    name: str
    command: tuple[str, ...] = ()
    url: str = ""
    file: str = ""  # the built-in sink: an absolute path each batch is appended to, signed
    headers: tuple[tuple[str, str], ...] = ()
    stage: str = "events"
    kinds: tuple[str, ...] = ()
    who: tuple[str, ...] = ()
    worktrees: tuple[str, ...] = ()
    timeout: float = 15.0

    def matches(self, e: monitorlog.Event) -> bool:
        return (
            (not self.kinds or e.kind in self.kinds)
            and (not self.who or e.who in self.who)
            and (not self.worktrees or e.worktree in self.worktrees)
        )


def _strings(raw: object, what: str) -> tuple[str, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise ValueError(f"{what} must be a list of strings")
    out = tuple(x for x in raw if isinstance(x, str))
    if len(out) != len(raw):
        raise ValueError(f"{what} must be a list of strings")
    return out


def parse(text: str) -> tuple[list[Hook], list[str]]:
    """The valid hooks in ``text`` and one message per invalid one (an invalid hook is skipped,
    never half-run). Never raises."""
    try:
        doc = tomllib.loads(text)
    except tomllib.TOMLDecodeError as e:
        return [], [f"monitor-hooks.toml is not valid TOML: {e}"]
    hooks: list[Hook] = []
    errors: list[str] = []
    seen: set[str] = set()
    for i, raw in enumerate(doc.get("hook") or []):
        label = f"hook #{i + 1}"
        try:
            if not isinstance(raw, dict):
                raise ValueError("must be a table")
            name = raw.get("name")
            if not isinstance(name, str) or not name.strip():
                raise ValueError("needs a `name`")
            label = f"hook '{name}'"
            if name in seen:
                raise ValueError("duplicate name (it keys the hook's cursor)")
            command = _strings(raw.get("command"), "`command`")
            url = raw.get("url") or ""
            file = raw.get("file") or ""
            if sum(map(bool, (command, url, file))) != 1:
                raise ValueError("needs exactly one of `command` (an argv list), `url` or `file`")
            if command and not command[0].startswith("/"):
                raise ValueError("`command` must start with an absolute path (no PATH lookup)")
            if url and not (isinstance(url, str) and url.startswith(("https://", "http://"))):
                raise ValueError("`url` must be http(s)")
            if file and not (isinstance(file, str) and file.startswith("/")):
                raise ValueError("`file` must be an absolute path")
            if file and os.path.normpath(file) != file:
                raise ValueError("`file` must be a normalised path (no `.`, `..` or trailing `/`)")
            headers = raw.get("headers") or {}
            if not isinstance(headers, dict) or not all(
                isinstance(k, str) and isinstance(v, str) for k, v in headers.items()
            ):
                raise ValueError("`headers` must be a table of strings")
            if headers and not url:
                raise ValueError("`headers` only applies to a `url` hook")
            stage = raw.get("stage", "events")
            if stage not in STAGES:
                raise ValueError(f"`stage` must be one of {', '.join(STAGES)} (for now)")
            timeout = float(raw.get("timeout", 15))
            if not 1 <= timeout <= 300:
                raise ValueError("`timeout` must be 1–300 seconds")
            hooks.append(
                Hook(
                    name=name,
                    command=command,
                    url=url,
                    file=file,
                    headers=tuple(sorted(headers.items())),
                    stage=stage,
                    kinds=_strings(raw.get("kinds"), "`kinds`"),
                    who=_strings(raw.get("who"), "`who`"),
                    worktrees=_strings(raw.get("worktrees"), "`worktrees`"),
                    timeout=timeout,
                )
            )
            seen.add(name)
        except (ValueError, TypeError) as e:
            errors.append(f"{label} skipped: {e}")
    return hooks, errors


def load() -> tuple[list[Hook], list[str]]:
    try:
        return parse(hooks_file().read_text())
    except FileNotFoundError:
        return [], []
    except OSError as e:
        return [], [f"can't read {hooks_file()}: {e}"]


# ── editing it: `fy monitor hook add|remove` ──────────────────────────────────────────
#
# There is no TOML writer in the standard library, and the file is the operator's: comments and
# hand edits must survive. So `add` APPENDS a rendered block and `remove` cuts one block out of
# the text — and each proves its edit by parsing the result before writing it: `add` that the new
# hook reads back exactly as meant and nothing else changed, `remove` that the document is the old
# one minus that hook. A file the text edit can't change exactly (an inline array of hooks, a
# multi-line string that looks like a header) is refused and left alone, never reformatted.

_FILE_HEAD = """\
# Monitor hooks: where the guest monitor's events go — a file, your own program or an endpoint.
# Host-only: never in foldyard.toml, which anything in the box can write. The supervisor re-reads
# this every few seconds. `fy monitor hook add|remove` edit it and keep your own edits.
"""
_HOOK_HEADER = re.compile(r"^\s*\[\[\s*hook\s*\]\]\s*(#.*)?$")
_TABLE = re.compile(r"^\s*\[")
_HOOK_SUBTABLE = re.compile(r"^\s*\[\s*hook\s*\.")


def _q(s: str) -> str:
    """A TOML basic string. JSON's escapes are TOML's, bar DEL, which TOML wants escaped."""
    return json.dumps(s, ensure_ascii=False).replace("\x7f", "\\u007f")


def _arr(xs: tuple[str, ...]) -> str:
    return "[" + ", ".join(_q(x) for x in xs) + "]"


def render(hook: Hook) -> str:
    """``hook`` as a ``[[hook]]`` block, defaults left out."""
    lines = ["[[hook]]", f"name = {_q(hook.name)}"]
    if hook.command:
        lines.append(f"command = {_arr(hook.command)}")
    if hook.url:
        lines.append(f"url = {_q(hook.url)}")
    if hook.headers:
        lines.append(
            "headers = { " + ", ".join(f"{_q(k)} = {_q(v)}" for k, v in hook.headers) + " }"
        )
    if hook.file:
        lines.append(f"file = {_q(hook.file)}")
    if hook.stage != "events":
        lines.append(f"stage = {_q(hook.stage)}")
    for key in ("kinds", "who", "worktrees"):
        if getattr(hook, key):
            lines.append(f"{key} = {_arr(getattr(hook, key))}")
    if hook.timeout != Hook.timeout:
        lines.append(f"timeout = {hook.timeout:g}")
    return "\n".join(lines) + "\n"


def _doc(text: str) -> dict:
    try:
        return tomllib.loads(text)
    except tomllib.TOMLDecodeError as e:
        raise ValueError(f"{hooks_file()} is not valid TOML ({e}) — fix it by hand first") from None


def added(text: str, hook: Hook) -> str:
    """``text`` with ``hook`` appended, or :class:`ValueError` saying why the supervisor would
    refuse it (nothing is written then)."""
    names = [h.get("name") for h in _doc(text).get("hook") or [] if isinstance(h, dict)]
    if hook.name in names:
        raise ValueError(f"a hook named '{hook.name}' is already in {hooks_file()}")
    sep = "" if not text or text.endswith("\n\n") else "\n" if text.endswith("\n") else "\n\n"
    new = text + sep + render(hook)
    (before, old_errors), (after, errors) = parse(text), parse(new)
    if after != [*before, hook] or errors != old_errors:
        fresh = [e for e in errors if e not in old_errors]
        raise ValueError(fresh[0] if fresh else f"hook '{hook.name}' wouldn't read back as written")
    return new


def removed(text: str, name: str) -> str:
    """``text`` without the hook called ``name`` — or :class:`ValueError` when there is none, or
    when cutting its block out of the text wouldn't leave exactly the rest of the document."""
    doc = _doc(text)
    entries = doc.get("hook")
    entries = entries if isinstance(entries, list) else []
    rest = [h for h in entries if not (isinstance(h, dict) and h.get("name") == name)]
    if len(rest) == len(entries):
        raise ValueError(f"no hook named '{name}' in {hooks_file()}")
    want = {k: v for k, v in doc.items() if k != "hook"} | ({"hook": rest} if rest else {})
    lines = text.splitlines(keepends=True)
    for start, line in enumerate(lines):
        if not _HOOK_HEADER.match(line):
            continue
        end = start + 1
        while end < len(lines) and not (
            _TABLE.match(lines[end]) and not _HOOK_SUBTABLE.match(lines[end])
        ):
            end += 1
        # comments and blank lines just above the next header introduce THAT one: keep them
        while end > start + 1 and (
            not lines[end - 1].strip() or lines[end - 1].lstrip()[:1] == "#"
        ):
            end -= 1
        candidate = "".join(lines[:start] + lines[end:])
        try:
            if tomllib.loads(candidate) == want:
                return candidate
        except tomllib.TOMLDecodeError:
            continue
    raise ValueError(
        f"can't remove '{name}' without disturbing the rest of {hooks_file()} — edit it by hand"
    )


def _write(text: str) -> None:
    """Atomically, 0600 from the create: a url hook's headers may hold a token."""
    path = hooks_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{secrets.token_hex(4)}")
    with os.fdopen(os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w") as f:
        f.write(text)
    tmp.replace(path)


def _text() -> str:
    try:
        return hooks_file().read_text()
    except FileNotFoundError:
        return ""


def add(hook: Hook) -> None:
    text = _text()
    _write(added(text or _FILE_HEAD, hook))


def remove(name: str) -> None:
    """Remove the hook ``name``. Its delivery state (cursor, sink sequence) is kept: a file it
    wrote stays checkable, and re-adding the name resumes where it stopped."""
    _write(removed(_text(), name))


WHO = ("container", "vm", "unattributed", "ambiguous")
KINDS = ("exec", "exit", "connect", "file", "other")


def _values(values: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(x.strip() for v in values for x in v.split(",") if x.strip())


def cli_add(
    name: str,
    *,
    command: tuple[str, ...] = (),
    file: str = "",
    url: str = "",
    who: tuple[str, ...] = (),
    kinds: tuple[str, ...] = (),
    worktrees: tuple[str, ...] = (),
) -> int:
    """`fy monitor hook add`. Refuses — writing nothing — what the supervisor would skip, a filter
    value that can never match (a typo would silently send nothing), and a file the sink would
    refuse to write."""
    if sum(map(bool, (command, file, url))) != 1:
        print("✗ give exactly one of `--file PATH`, `--url URL`, or a program after `--`")
        return 1
    who, kinds, worktrees = _values(who), _values(kinds), _values(worktrees)
    bad = [w for w in who if w not in WHO] + [k for k in kinds if k not in KINDS]
    if bad:
        print(
            f"✗ no event has {', '.join(map(repr, bad))} — who: {', '.join(WHO)}; "
            f"kinds: {', '.join(KINDS)}"
        )
        return 1
    if file:
        file = os.path.abspath(os.path.expanduser(file))
        try:
            monitorsink.check_path(file)
        except monitorsink.Refused as e:
            print(f"✗ {e}")
            return 1
    hook = Hook(
        name=name, command=command, url=url, file=file, who=who, kinds=kinds, worktrees=worktrees
    )
    try:
        add(hook)
    except ValueError as e:
        print(f"✗ {e}")
        return 1
    kind, where = target(hook)
    print(f"✓ hook '{name}' added: {kind} → {where}")
    print(
        "  The supervisor picks it up within seconds and sends settled events from the recent "
        "ones on; `fy monitor hook` shows how its deliveries are going."
    )
    if file:
        print(
            "  Each batch is one signed JSON line. Anything that can write that directory (the "
            "box, if it is in the checkout) can still edit the file — "
            f"`fy monitor check {shlex.quote(file)}` says whether it has."
        )
    if config.machine_monitor() == "off":
        print(
            '  ℹ the monitor is off: nothing is recorded until `[machine] monitor = "observe"` '
            "is adopted and the VM re-provisioned (`fy machine stop && fy up`)."
        )
    return 0


def cli_remove(name: str) -> int:
    try:
        remove(name)
    except ValueError as e:
        print(f"✗ {e}")
        return 1
    print(f"✓ hook '{name}' removed (its place in the event stream is kept if you re-add it)")
    return 0


def _first_hook(data: bytes) -> str:
    for raw in data.split(b"\n"):
        try:
            doc = json.loads(raw)
        except ValueError:
            continue
        if isinstance(doc, dict) and isinstance(doc.get("hook"), str):
            return doc["hook"]
    return ""


def check(path: str, name: str = "") -> int:
    """`fy monitor check`: :func:`monitorsink.verify` over the file, against the hook's recorded
    state. The hook is ``name``, else the configured hook writing to ``path``, else the one the
    file's first line names (its state still has to agree)."""
    path = os.path.abspath(os.path.expanduser(path))
    key = monitorsink.load_key()
    if key is None:
        print(
            f"✗ no sink key ({monitorsink.key_file()}) — no file hook has written on this computer"
        )
        return 1
    try:
        data = monitorsink.read(path)
    except (OSError, monitorsink.Refused) as e:
        print(f"✗ {e}")
        return 1
    name = name or next((h.name for h in load()[0] if h.file == path), "") or _first_hook(data)
    if not name:
        print(f"✗ {path}: no hook names it — say which with `--hook NAME`")
        return 1
    st = _load_state().get(name)
    got = monitorsink.verify(data, key, name, (st.sink_seq, st.sink_mac) if st else None)
    for note in got.notes:
        print(f"ℹ {note}")
    if got.problem:
        print(f"✗ {path}:{got.line}: {got.problem}")
        return 1
    print(
        f"✓ {path}: {got.lines} lines, seq 1–{got.last_seq}, {got.events} events — each signed by "
        f"this computer's sink for hook '{name}', none missing, moved or added, and the file "
        "ends where the sink last wrote"
    )
    return 0


# ── reading it back: `fy monitor hook`, `fy doctor` ───────────────────────────────────


def target(hook: Hook) -> tuple[str, str]:
    """``(kind, where)`` — header names only, never their values (they hold tokens)."""
    if hook.file:
        return "file", hook.file
    if hook.url:
        names = ", ".join(k for k, _ in hook.headers)
        return "url", hook.url + (f" (headers: {names})" if names else "")
    return "command", shlex.join(hook.command)


def problems(st: HookState | None, now: float) -> list[str]:
    """What is wrong with a hook's delivery: failing now, and events it has ever skipped."""
    out: list[str] = []
    if st is None:
        return out
    if st.attempts:
        wait = max(0.0, st.next_try - now)
        out.append(f"failing ({st.attempts} attempts): {st.error} — retrying in {wait:.0f}s")
    if st.dropped:
        last = f" (last error: {st.error})" if st.error and not st.attempts else ""
        out.append(f"{st.dropped} events skipped after {MAX_ATTEMPTS} failed attempts{last}")
    return out


def progress(hook: Hook, st: HookState | None, now: float) -> str:
    if st is None or not (st.delivered or st.ok_at):
        return "nothing delivered yet"
    ago = f", last {now - st.ok_at:.0f}s ago" if st.ok_at else ""
    line = f", line {st.sink_seq} of its file" if hook.file else ""
    return f"{st.delivered} events delivered{ago}{line}"


def listing(now: float | None = None) -> list[str]:
    """`fy monitor hook`: each hook — its kind, target, filters and delivery health — and every
    hook the supervisor is skipping, with why."""
    now = time.time() if now is None else now
    hooks, errors = load()
    if not hooks and not errors:
        return [
            "no monitor hooks — the events stay in `fy monitor`. `fy monitor hook add` sends them "
            f"to a file, a program or a URL (host-only config: {hooks_file()}).",
        ]
    states = _load_state()
    out: list[str] = []
    for h in hooks:
        kind, where = target(h)
        picks = [(k, getattr(h, k)) for k in ("kinds", "who", "worktrees")]
        filters = " ".join(f"{k}={','.join(v)}" for k, v in picks if v) or "all events"
        out.append(f"{h.name}  {kind}  {where}  [{filters}]")
        st = states.get(h.name)
        out.append(f"  ✓ {progress(h, st, now)}")
        out.extend(f"  ⚠ {why}" for why in problems(st, now))
    out.extend(f"✗ {e}" for e in errors)
    out.append(f"— {hooks_file()}")
    return out


# ── state ─────────────────────────────────────────────────────────────────────────────


@dataclass
class HookState:
    last: list = field(default_factory=list)  # [boot, seq] of the last delivered event
    pos: list = field(default_factory=list)  # [inode, offset] in the store where that event ends
    attempts: int = 0
    next_try: float = 0.0
    delivered: int = 0
    dropped: int = 0
    error: str = ""
    ok_at: float = 0.0
    sink_seq: int = 0  # a file hook: the last line written, which `fy monitor check` ends on
    sink_mac: str = ""  # …and its mac, the next line's `prev`


_STATE_FIELDS = frozenset(f.name for f in fields(HookState))


def _load_state() -> dict[str, HookState]:
    try:
        raw = json.loads(_state_file().read_text())
        # a field this version doesn't know (a newer foldyard wrote it) is dropped, not fatal:
        # failing here would hand every hook a fresh cursor
        return {
            k: HookState(**{f: x for f, x in v.items() if f in _STATE_FIELDS})
            for k, v in raw.items()
        }
    except (OSError, ValueError, TypeError, AttributeError):
        return {}


def load_state() -> dict[str, HookState]:
    """Each hook's delivery state, as the supervisor last saved it (read-only callers)."""
    return _load_state()


def _save_state(states: dict[str, HookState]) -> None:
    path = _state_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps({k: asdict(v) for k, v in states.items()}))
    tmp.replace(path)


# ── what is ready to deliver ──────────────────────────────────────────────────────────


def settled(records: list[dict], now: float) -> list[monitorlog.Event]:
    """The attributed events whose attribution can't change any more: a snapshot after them
    exists in their boot, or they arrived more than :data:`SETTLE_SECONDS` ago. In store order,
    and only up to the first one that hasn't settled: a VM restarted within the settle window
    leaves the old boot's tail unsettled while the new boot's first snapshot settles its events,
    and a cursor moved onto the new boot would rank the old tail behind it for good."""
    last_snap: dict[str, int] = {}
    recv: dict[tuple[str, int], float] = {}
    for r in records:
        if r.get("k") == "scopes" and "boot" in r:
            last_snap[r["boot"]] = max(last_snap.get(r["boot"], -1), int(r["seq"]))
        elif r.get("k") == "ev" and "boot" in r:
            recv[(r["boot"], int(r["seq"]))] = float(r.get("recv", 0.0))
    out = []
    for e in monitorlog.attribute(records, config.project_prefix()):
        snapshot_after = e.seq < last_snap.get(e.boot, -1)
        if not (snapshot_after or now - recv.get((e.boot, e.seq), now) >= SETTLE_SECONDS):
            break
        out.append(e)
    return out


def after(events: list[monitorlog.Event], last: list) -> list[monitorlog.Event]:
    """The events after a hook's cursor ``[boot, seq]``. Boots are ordered as the store holds
    them; a cursor whose boot has left the read window is older than all of it."""
    if not last:
        return events
    boot, seq = last[0], int(last[1])
    order = list(dict.fromkeys(e.boot for e in events))
    if boot not in order:
        return events
    rank = order.index(boot)
    return [e for e in events if order.index(e.boot) > rank or (e.boot == boot and e.seq > seq)]


# ── delivery ──────────────────────────────────────────────────────────────────────────


def _env() -> dict[str, str]:
    """A hook command's whole environment: never the supervisor's (every host.env secret)."""
    import os

    env = {k: os.environ[k] for k in ("PATH", "HOME", "LANG", "TZ") if k in os.environ}
    env.setdefault("PATH", "/usr/local/bin:/usr/bin:/bin")
    return env


def _run_command(hook: Hook, body: bytes) -> str:
    """``""`` on success, else why not."""
    env = {**_env(), "FY_PROJECT": config.project(), "FY_HOOK": hook.name}
    try:
        res = subprocess.run(
            list(hook.command), input=body, capture_output=True, timeout=hook.timeout, env=env
        )
    except subprocess.TimeoutExpired:
        return f"timed out after {hook.timeout:.0f}s"
    except OSError as e:
        return f"couldn't run: {e}"
    if res.returncode != 0:
        tail = res.stderr.decode(errors="replace").strip().splitlines()[-1:] or [""]
        return f"exit {res.returncode}: {tail[0][:200]}"
    return ""


def _post(hook: Hook, body: bytes) -> str:
    req = urllib.request.Request(
        hook.url,
        data=body,
        method="POST",
        headers={"Content-Type": "application/json", **dict(hook.headers)},
    )
    try:
        with urllib.request.urlopen(req, timeout=hook.timeout) as resp:
            code = resp.status
    except Exception as e:  # any transport failure is the same retryable failure
        return f"{type(e).__name__}: {e}"[:300]
    return "" if 200 <= code < 300 else f"HTTP {code}"


def batch(hook: Hook, events: list[monitorlog.Event]) -> dict:
    return {
        "schema": SCHEMA,
        "project": config.project(),
        "hook": hook.name,
        "first": [events[0].boot, events[0].seq],
        "last": [events[-1].boot, events[-1].seq],
        "events": [asdict(e) for e in events],
    }


def payload(hook: Hook, events: list[monitorlog.Event]) -> bytes:
    return json.dumps(batch(hook, events)).encode()


def _to_file(hook: Hook, st: HookState, events: list[monitorlog.Event]) -> str:
    """Append the batch as the next signed line of the hook's chain; ``""`` on success. The
    sequence moves only on success, so a retry re-writes the same seq (see monitorsink.verify)."""
    try:
        key = monitorsink.ensure_key()
        line, mac = monitorsink.signed_line(key, batch(hook, events), st.sink_seq + 1, st.sink_mac)
        monitorsink.append(hook.file, line)
    except (OSError, monitorsink.Refused) as e:
        return str(e)[:300]
    st.sink_seq, st.sink_mac = st.sink_seq + 1, mac
    return ""


def deliver(
    hook: Hook,
    st: HookState,
    events: list[monitorlog.Event],
    now: float,
    where: dict[tuple[str, int], list[int]] | None = None,
) -> str:
    """One batch for one hook: send the next up-to-:data:`BATCH_MAX` matching events after its
    cursor. ``where`` maps an event to its store position, kept beside the cursor so the next
    round reads from there. Returns a log line when something worth saying happened, else ``""``."""
    where = where or {}

    def move(e: monitorlog.Event) -> None:
        st.last, st.pos = [e.boot, e.seq], where.get((e.boot, e.seq), [])

    if now < st.next_try:
        return ""
    pending = after(events, st.last)
    if not pending:
        return ""
    wanted: list[monitorlog.Event] = []
    batch_end = pending[0]
    # Pass over what this hook doesn't want in the same round, so a narrow filter never takes
    # minutes of rounds to skip a backlog (seen on a 72k-event store); send at most one batch.
    while pending and not wanted:
        scan = pending[: BATCH_MAX * 4]
        wanted = [e for e in scan if hook.matches(e)][:BATCH_MAX]
        # A full batch ends at its last event (the rest comes next round); otherwise the whole
        # scan is covered — the events this hook doesn't want are passed over with it.
        batch_end = wanted[-1] if len(wanted) == BATCH_MAX else scan[-1]
        if not wanted:
            move(batch_end)
            pending = pending[len(scan) :]
    if not wanted:
        return ""
    if hook.file:
        why = _to_file(hook, st, wanted)
    else:
        body = payload(hook, wanted)
        why = _run_command(hook, body) if hook.command else _post(hook, body)
    if not why:
        move(batch_end)
        st.attempts, st.next_try, st.error, st.ok_at = 0, 0.0, "", now
        st.delivered += len(wanted)
        return ""
    st.attempts += 1
    st.error = why
    if st.attempts >= MAX_ATTEMPTS:
        move(batch_end)
        st.dropped += len(wanted)
        st.attempts, st.next_try = 0, 0.0
        return (
            f"monitor hook '{hook.name}': skipped {len(wanted)} events after "
            f"{MAX_ATTEMPTS} failures ({why})"
        )
    st.next_try = now + min(300.0, 10.0 * 2 ** (st.attempts - 1))
    return f"monitor hook '{hook.name}': delivery failed, retrying ({why})"


def round_once(now: float | None = None, log: Callable[[str], None] = lambda m: None) -> None:
    """Every configured hook, one batch each. Never raises for a hook's failure."""
    hooks, errors = load()
    for msg in errors:
        log(msg)
    if not hooks:
        return
    now = time.time() if now is None else now
    states = _load_state()
    for hook in hooks:
        st = states.setdefault(hook.name, HookState())
        records, where, lost = _window(st)
        if lost:
            log(
                f"monitor hook '{hook.name}': the store rotated past its position — the events "
                f"after {st.last[0]}:{st.last[1]} that were rotated out were never delivered to it"
            )
        msg = deliver(hook, st, settled(records, now), now, where)
        if msg:
            log(msg)
    _save_state(states)


def _window(st: HookState) -> tuple[list[dict], dict[tuple[str, int], list[int]], bool]:
    """A hook's records for this round — read on from its position, or the store's tail for a hook
    with none yet — with each event's position, and whether the position was lost: rotated out,
    or its inode now names another file (the record ending there isn't the cursor's)."""
    if not st.pos:
        records, where = _split(monitorlog.span(None, READ_BYTES, 0)[0])
        return records, where, False
    read, found = monitorlog.span(st.pos, LEAD_BYTES, READ_BYTES)
    lost = not found or not any(p == st.pos and _key(r) == tuple(st.last) for r, p in read)
    if found and lost:
        read, _ = monitorlog.span([-1, 0], 0, READ_BYTES)  # no inode -1: the oldest byte on
    records, where = _split(read)
    return records, where, lost


def _key(r: dict) -> tuple[str, int] | None:
    return (r["boot"], int(r["seq"])) if r.get("k") == "ev" and "boot" in r else None


def _split(
    read: list[tuple[dict, list[int]]],
) -> tuple[list[dict], dict[tuple[str, int], list[int]]]:
    where = {k: p for r, p in read if (k := _key(r)) is not None}
    return [r for r, _ in read], where


def run(log: Callable[[str], None], stopping: Callable[[], bool]) -> None:
    said: set[str] = set()

    def once(msg: str) -> None:  # a repeating failure is logged once, not every round
        if msg not in said:
            said.add(msg)
            log(msg)

    while not stopping():
        try:
            round_once(log=once)
        except Exception as e:
            once(f"monitor hooks: round failed: {e}")
        for _ in range(int(ROUND_EVERY / 0.2)):
            if stopping():
                return
            time.sleep(0.2)


def start(log: Callable[[str], None], stopping: Callable[[], bool]) -> threading.Thread:
    t = threading.Thread(target=run, args=(log, stopping), name="monitor-hooks", daemon=True)
    t.start()
    return t
