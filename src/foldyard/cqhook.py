"""The in-box handler behind cq's session hooks: ``fy _cq-hook <agent> <event>``.

cq (mozilla-ai/cq) works in two phases, and foldyard drives each from the agent's own hooks
(wired by :mod:`foldyard.plugins.cq` when ``[claude]``/``[codex]`` say ``cq = true``):

  - **during the session** — the skill-guided query/propose loop. ``session-start`` puts the
    protocol in front of the agent (the job ``cq install``'s always-loaded instruction does for
    other hosts), and ``post-tool-use-failure`` (Claude only — Codex has no failure event and its
    PostToolUse carries no exit code) turns a failed tool call into the moment to query cq before
    retrying, the "post-error auto-query" hook cq's own design started from.
  - **at the end of the session** — ``/cq:reflect``. ``session-end`` records which session just
    ended in the file the launcher named (``FY_CQ_SESSION_FILE``), so ``fy claude``/``fy codex``
    can offer to resume exactly that session with the reflect prompt once the agent exits. A
    SessionEnd hook can't talk to the agent itself (the session is over), and reflect asks the
    human to approve each candidate, so the launcher owns the conversation and the hook only says
    which session to resume.

Contract with both agents: read the event JSON on stdin, print at most one JSON object, ALWAYS
exit 0 — a hook must never block or fail the agent over cq. Stdlib-only and import-light: Claude
gives a plugin's SessionEnd hooks 1.5 s in total, Codex 1–3 s.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import tempfile
import time
from pathlib import Path
from typing import IO

AGENTS = ("claude", "codex")
EVENTS = ("session-start", "post-tool-use-failure", "session-end")

# The launcher sets this for the agent it runs; session-end writes the ended session's id there.
SESSION_FILE_ENV = "FY_CQ_SESSION_FILE"

# A nudge per DISTINCT failing call, at most this many per session: the point is the first query,
# not a reminder under every red test run of a long debugging session.
MAX_NUDGES = 5
_STATE_TTL_S = 24 * 3600
_MAX_PAYLOAD = 1 << 20
_SESSION_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}")

SESSION_START_CONTEXT = (
    "cq — the shared agent-knowledge commons — is connected in this dev box (the `cq` MCP "
    "server, hosted store at cq.exchange). Follow the cq skill's Core Protocol: query cq before "
    "starting a task and before retrying after an error; confirm or flag knowledge units that "
    "proved right or wrong; when you resolve something non-obvious, draft a knowledge unit and "
    "propose it once the user approves. Writes reach cq.exchange only while the operator's cq "
    "credential is on (`fy mode cq=on`, on their computer); otherwise cq keeps them in its local "
    "store and uploads them at a later session start — say so if a propose reports a fallback."
)

FAILURE_CONTEXT = (
    "cq: that tool call failed. Before retrying, query cq with domains for the tool, library and "
    "error — another agent may already have solved it. If you then fix it in a way the error "
    "didn't make obvious, propose a knowledge unit (with the user's approval)."
)


def _payload(stdin: IO[str]) -> dict:
    """The event JSON, or ``{}`` — malformed or oversized input degrades, never raises."""
    try:
        raw = stdin.read(_MAX_PAYLOAD + 1)
    except (OSError, ValueError):
        return {}
    if len(raw) > _MAX_PAYLOAD:
        return {}
    try:
        data = json.loads(raw) if raw.strip() else {}
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def _context(event_name: str, text: str) -> str:
    return json.dumps(
        {"hookSpecificOutput": {"hookEventName": event_name, "additionalContext": text}}
    )


def session_id(payload: dict) -> str:
    """The payload's ``session_id`` when it is a plain id, else ``""`` — it ends up in a resume
    argv and a file name, so anything else is refused rather than escaped."""
    sid = payload.get("session_id")
    return sid if isinstance(sid, str) and _SESSION_ID.fullmatch(sid) else ""


def state_dir() -> Path:
    """Per-box scratch for the nudge ledger (per session; swept after a day)."""
    base = os.environ.get("XDG_RUNTIME_DIR") or tempfile.gettempdir()
    return Path(base) / "fy-cq-hook"


def _sweep(directory: Path, now: float) -> None:
    try:
        entries = list(directory.iterdir())
    except OSError:
        return
    for entry in entries:
        try:
            if entry.is_file() and now - entry.stat().st_mtime > _STATE_TTL_S:
                entry.unlink()
        except OSError:
            continue


def _signature(payload: dict) -> str:
    """What makes two failures "the same": the tool and what it was asked to do — so rerunning one
    failing command nudges once, while a different failure still gets its own."""
    tool = payload.get("tool_name")
    tool_input = payload.get("tool_input")
    if isinstance(tool_input, dict) and isinstance(tool_input.get("command"), str):
        what = tool_input["command"]
    else:
        what = json.dumps(tool_input, sort_keys=True, default=str)
    return hashlib.sha256(f"{tool}\0{what}".encode()).hexdigest()[:16]


def _should_nudge(payload: dict, directory: Path) -> bool:
    if payload.get("is_interrupt") is True:
        return False  # the user stopped it; nothing to look up
    sid = session_id(payload) or "session"
    ledger = directory / f"{sid}.nudged"
    try:
        seen = ledger.read_text().split() if ledger.exists() else []
    except OSError:
        seen = []
    sig = _signature(payload)
    if sig in seen or len(seen) >= MAX_NUDGES:
        return False
    try:
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        with ledger.open("a") as fh:
            fh.write(sig + "\n")
    except OSError:
        pass  # can't remember it: nudge anyway, a repeat beats silence
    return True


def record_session(payload: dict, env: dict) -> None:
    """Write the ended session's id where the launcher asked (``FY_CQ_SESSION_FILE``). Last write
    wins: after a ``/clear`` the session that ends at exit is the one the operator was in."""
    target, sid = env.get(SESSION_FILE_ENV, ""), session_id(payload)
    if not target or not sid:
        return
    path = Path(target)
    tmp = path.with_name(path.name + ".tmp")
    try:
        tmp.write_text(sid + "\n")
        tmp.replace(path)
    except OSError:
        pass


def read_session(path: Path) -> str:
    """The id :func:`record_session` left in ``path``, or ``""``."""
    try:
        sid = path.read_text().strip()
    except OSError:
        return ""
    return sid if _SESSION_ID.fullmatch(sid) else ""


def run(
    agent: str,
    event: str,
    stdin: IO[str],
    stdout: IO[str],
    env: dict | None = None,
    directory: Path | None = None,
) -> int:
    """Handle one hook event; always returns 0."""
    env = dict(os.environ) if env is None else env
    directory = state_dir() if directory is None else directory
    payload = _payload(stdin)
    try:
        if event == "session-start":
            _sweep(directory, time.time())
            stdout.write(_context("SessionStart", SESSION_START_CONTEXT) + "\n")
        elif event == "post-tool-use-failure":
            if agent == "claude" and _should_nudge(payload, directory):
                stdout.write(_context("PostToolUseFailure", FAILURE_CONTEXT) + "\n")
        elif event == "session-end":
            record_session(payload, env)
    except Exception as exc:
        print(f"fy _cq-hook: {exc}", file=sys.stderr)
    return 0
