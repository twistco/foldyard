"""monitorhooks.py — the guest monitor's hooks: the operator's program or endpoint, called with
batches of attributed events (docs/monitor-pipeline.md §6). How a scoring script, a decision model
or a notifier plugs in without being part of foldyard.

**Configured host-side only** — ``<state dir>/monitor-hooks.toml`` (``~/.foldyard/<project>/``),
never ``foldyard.toml``. Repo config is writable by anything in the box: a ``command`` there would
be host code execution for the box (ADR-0023), and a ``url`` would send every event wherever the
box chose. The file is re-read every round, so an edit applies within seconds; there is nothing
to adopt, because nothing in the mount can reach it.

Delivery: only SETTLED events (their attribution can't change any more: a later snapshot exists,
or :data:`SETTLE_SECONDS` passed); at-least-once (a hook's cursor advances only on success — exit
0, or HTTP 2xx); retried with backoff, and after :data:`MAX_ATTEMPTS` the batch is skipped and
counted. Commands run with a minimal environment — never the supervisor's, which holds every
``host.env`` secret — on their own thread, so a slow hook never delays the pull.
"""

from __future__ import annotations

import json
import subprocess
import threading
import time
import tomllib
import urllib.request
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path

from . import config, monitorlog

SCHEMA = "fy.monitor.events/1"
STAGES = ("events",)  # later: scored, decided, reviewed (docs/monitor-pipeline.md §5)
SETTLE_SECONDS = 120.0
BATCH_MAX = 500
MAX_ATTEMPTS = 5
ROUND_EVERY = 5.0
READ_BYTES = 4 << 20


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
            if bool(command) == bool(url):
                raise ValueError("needs exactly one of `command` (an argv list) or `url`")
            if command and not command[0].startswith("/"):
                raise ValueError("`command` must start with an absolute path (no PATH lookup)")
            if url and not (isinstance(url, str) and url.startswith(("https://", "http://"))):
                raise ValueError("`url` must be http(s)")
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


# ── state ─────────────────────────────────────────────────────────────────────────────


@dataclass
class HookState:
    last: list = field(default_factory=list)  # [boot, seq] of the last delivered event
    attempts: int = 0
    next_try: float = 0.0
    delivered: int = 0
    dropped: int = 0
    error: str = ""
    ok_at: float = 0.0


def _load_state() -> dict[str, HookState]:
    try:
        raw = json.loads(_state_file().read_text())
        return {k: HookState(**v) for k, v in raw.items()}
    except (OSError, ValueError, TypeError):
        return {}


def _save_state(states: dict[str, HookState]) -> None:
    path = _state_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps({k: asdict(v) for k, v in states.items()}))
    tmp.replace(path)


# ── what is ready to deliver ──────────────────────────────────────────────────────────


def settled(records: list[dict], now: float) -> list[monitorlog.Event]:
    """The attributed events whose attribution can't change any more: a snapshot after them
    exists in their boot, or they arrived more than :data:`SETTLE_SECONDS` ago. In store order."""
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
        if snapshot_after or now - recv.get((e.boot, e.seq), now) >= SETTLE_SECONDS:
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


def payload(hook: Hook, events: list[monitorlog.Event]) -> bytes:
    return json.dumps(
        {
            "schema": SCHEMA,
            "project": config.project(),
            "hook": hook.name,
            "first": [events[0].boot, events[0].seq],
            "last": [events[-1].boot, events[-1].seq],
            "events": [asdict(e) for e in events],
        }
    ).encode()


def deliver(hook: Hook, st: HookState, events: list[monitorlog.Event], now: float) -> str:
    """One batch for one hook: send the next up-to-:data:`BATCH_MAX` matching events after its
    cursor. Returns a log line when something worth saying happened, else ``""``."""
    if now < st.next_try:
        return ""
    pending = after(events, st.last)
    if not pending:
        return ""
    scan = pending[: BATCH_MAX * 4]  # bound the scan as well as the send
    wanted = [e for e in scan if hook.matches(e)][:BATCH_MAX]
    # A full batch ends at its last event (the rest of the scan comes next round); otherwise the
    # whole scan is covered — the events this hook doesn't want are passed over with it.
    batch_end = wanted[-1] if len(wanted) == BATCH_MAX else scan[-1]
    if not wanted:  # nothing this hook wants: just move past it
        st.last = [batch_end.boot, batch_end.seq]
        return ""
    body = payload(hook, wanted)
    why = _run_command(hook, body) if hook.command else _post(hook, body)
    if not why:
        st.last = [batch_end.boot, batch_end.seq]
        st.attempts, st.next_try, st.error, st.ok_at = 0, 0.0, "", now
        st.delivered += len(wanted)
        return ""
    st.attempts += 1
    st.error = why
    if st.attempts >= MAX_ATTEMPTS:
        st.last = [batch_end.boot, batch_end.seq]
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
    events = settled(monitorlog._tail(READ_BYTES), now)
    states = _load_state()
    for hook in hooks:
        st = states.setdefault(hook.name, HookState())
        msg = deliver(hook, st, events, now)
        if msg:
            log(msg)
    _save_state(states)


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
