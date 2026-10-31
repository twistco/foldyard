"""`fy agent` — agents in tmux sessions inside the dev box: start, list, attach, stop.

`fy claude` / `fy codex` run the agent in the terminal that started them, so closing that terminal
ends the agent. `fy agent start` runs the same `fy claude` / `fy codex` in a detached tmux session
in the box instead, so the agent outlives any terminal, and any terminal on your computer can
reattach to it by its session id (`fy agent attach <id>`). It is a separate verb, never folded into
`fy claude`: under tmux, scrollback, selection and some keys behave differently (docs/agents.md).

**One id per agent.** foldyard generates a UUID per session. For Claude it is passed as
`--session-id`, so it IS the Claude session id — the one the transcript is named after, and the one
a transcript browser such as claude-code-log shows — which makes `fy agent attach <that id>` work
from anything that names the session. Codex takes no such flag, so its id is foldyard's own.

**The transport is the engine socket, nothing else.** The host reaches each box with `podman exec`,
as every other host verb does; no listener is opened anywhere. tmux runs on foldyard's own socket
(`tmux -L fy`, with `-f /dev/null`), so it never meets a tmux the box's user runs for themselves,
and foldyard sets its options on every start (:data:`_SERVER_OPTIONS`) rather than reading a
config file the box could edit. Two of those options are posture, not taste: tmux re-draws the
screen itself, so with `allow-passthrough off` and `set-clipboard external` an agent can't write
raw escape sequences through to your terminal, nor set your clipboard (it can today, without
tmux). tmux older than 3.3 has no `allow-passthrough` — it passes them always — so start refuses
it.

**Everything a box reports is box-written.** Session names, ids and timestamps come from a tmux
server the box controls, and any box can reach a sibling's: they are shown, never acted on beyond
attaching to or stopping what they name, and every field is cut to printable characters before it
reaches a terminal (:func:`_printable`).

**Every local project** (`fy agent ls`, and `attach`/`stop` from outside a project): the host's
record of the checkouts it adopted (:func:`configpin.adopted_checkouts`) — never a scan of the
disk. Each is listed by running this same CLI in that checkout (`fy agent ls --here --json`), so
each project resolves its own VM, prefix and worktrees exactly as a command run there would; a
project whose VM isn't running is reported, never booted.

Stdlib-only; imported lazily by the CLI.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import sys
import uuid
from collections.abc import Iterable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from . import config

AGENTS = ("claude", "codex")
SOCKET = "fy"  # `tmux -L fy`: foldyard's own server, apart from any the box's user runs
MIN_TMUX = (3, 3)  # `allow-passthrough` (off by default) arrived in 3.3; before it, always on

# Applied on every start, server-wide, before the session is made. A tmux command list, so one
# argv carries them all (`;` is tmux's separator, passed as its own argument — no shell).
_SERVER_OPTIONS: tuple[tuple[str, ...], ...] = (
    # Posture: no raw escape sequences through to your terminal, no clipboard writes from the box.
    ("set-option", "-g", "allow-passthrough", "off"),
    ("set-option", "-s", "set-clipboard", "external"),
    # Papercuts: Esc interrupts Claude, so no half-second wait on it (tmux < 3.5 defaults to
    # 500 ms); Shift-Enter reaches the agent as its own key; the newest client sets the size, so a
    # second attach doesn't shrink the first; scrollback worth scrolling.
    ("set-option", "-s", "escape-time", "10"),
    ("set-option", "-s", "extended-keys", "on"),
    ("set-option", "-g", "window-size", "latest"),
    ("set-option", "-g", "history-limit", "50000"),
    # An agent that exits leaves its pane, so `fy agent attach` can show why and `ls` says so.
    ("set-option", "-g", "remain-on-exit", "on"),
)

# What `ls` reads per pane: one line each (our sessions have one pane), fields joined by _SEP.
# Printable on purpose: tmux writes a control character in `-F` output as `_`, so a tab joined
# every field into one (seen live, tmux 3.5a). A name that contains _SEP gives the line too many
# fields, and it is skipped like any other malformed line.
_FIELDS = (
    "session_name",
    "@fy_session",
    "@fy_agent",
    "session_created",
    "session_activity",
    "session_attached",
    "pane_dead",
    "pane_dead_status",
    "pane_dead_time",  # tmux ≥ 3.3; `clean` falls back to the last activity without it
)
_SEP = "|:fy:|"
_FORMAT = _SEP.join(f"#{{{f}}}" for f in _FIELDS)

_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}")
_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
_MIN_PREFIX = 8  # an id prefix shorter than this matches too much to be a handle
_LIST_TIMEOUT = 10  # one box's `tmux list-panes`
_PROJECT_TIMEOUT = 60  # one project's `fy agent ls --here --json`

# Claude flags that name (or fork) the session themselves, so foldyard mustn't add --session-id.
_RESUME_FLAGS = ("--resume", "-r", "--continue", "-c", "--session-id", "--fork-session")


def _err(*a: object) -> None:
    print(*a, file=sys.stderr, flush=True)


def _printable(text: object, limit: int = 80) -> str:
    """Box-written ``text`` cut to printable characters (no terminal escapes), capped."""
    if not isinstance(text, str):
        return ""
    return "".join(c for c in text if c.isprintable())[:limit]


@dataclass(frozen=True)
class Session:
    """One agent session, as a box's tmux reports it (every field box-written, sanitised)."""

    name: str
    id: str  # "" when the session carries no valid foldyard id (not made by `fy agent start`)
    agent: str
    created: str  # ISO 8601, "" when unreadable
    activity: str
    attached: int
    alive: bool
    exit_status: int | None
    project: str = ""
    worktree: str = ""  # "" = the main checkout
    box: str = ""
    checkout: str = ""  # the project's MAIN checkout, where `fy agent … --here` runs
    exited: str = ""  # when the agent exited (ISO 8601), "" while it runs or when unreadable

    @property
    def state(self) -> str:
        if not self.alive:
            return "exited" if self.exit_status is None else f"exited ({self.exit_status})"
        return f"attached ×{self.attached}" if self.attached else "running"

    @property
    def handle(self) -> str:
        """What `fy agent attach` takes for this session: its id, else its name."""
        return self.id or self.name


def _iso(epoch: str) -> str:
    try:
        return datetime.fromtimestamp(int(epoch), UTC).isoformat(timespec="seconds")
    except (ValueError, OverflowError, OSError):
        return ""


def parse_panes(stdout: str) -> list[Session]:
    """`tmux list-panes -a -F` output → sessions, one per session name. A line that doesn't have
    exactly the fields asked for (a name with the separator in it) is skipped, never guessed at."""
    out: dict[str, Session] = {}
    for line in stdout.splitlines():
        parts = line.split(_SEP)
        if len(parts) != len(_FIELDS):
            continue
        name, sid, agent, created, activity, attached, dead, status, dead_time = parts
        name = _printable(name, 64)
        if not name or name in out:
            continue
        out[name] = Session(
            name=name,
            id=sid if _UUID.fullmatch(sid) else "",
            agent=agent if agent in AGENTS else "",
            created=_iso(created),
            activity=_iso(activity),
            attached=int(attached) if attached.isdigit() else 0,
            alive=dead != "1",
            exit_status=int(status) if dead == "1" and status.isdigit() else None,
            exited=_iso(dead_time) if dead == "1" else "",
        )
    return list(out.values())


# ── tmux argv (shared by the box, which runs them, and the host, which wraps them in an exec) ─────


def _tmux(*args: str, start: bool = False) -> list[str]:
    """``tmux -L fy …``. ``start``: also ``-f /dev/null``, for a command that may start the server
    — foldyard's options are set explicitly, never read from a file in the box."""
    return ["tmux", "-L", SOCKET, *(["-f", "/dev/null"] if start else []), *args]


def _chain(*commands: Iterable[str]) -> list[str]:
    """One tmux command list. tmux splits its argv at any argument ENDING in ``;``, so one that
    does (a working directory, say) would end its command early and run the rest as another —
    refused rather than escaped."""
    out: list[str] = []
    for cmd in commands:
        cmd = list(cmd)
        if any(arg.endswith(";") for arg in cmd):
            raise ValueError("a tmux argument may not end in ';'")
        if out:
            out.append(";")
        out.extend(cmd)
    return out


def start_argv(name: str, sid: str, agent: str, cwd: str, command: list[str]) -> list[str]:
    """Start the server if needed, set foldyard's options, make the detached session running
    ``command`` (an argv — tmux runs it directly, no shell) and tag it with its id and agent. The
    tag's ``-t`` is a PANE target, where an exact session is ``=name:`` — a bare ``=name`` there
    names no session (unlike `attach-session`/`kill-session`, whose ``-t`` is a session)."""
    target = f"={name}:"
    return _tmux(
        *_chain(
            ["start-server"],
            *_SERVER_OPTIONS,
            ["new-session", "-d", "-s", name, "-c", cwd, *command],
            ["set-option", "-t", target, "@fy_session", sid],
            ["set-option", "-t", target, "@fy_agent", agent],
        ),
        start=True,
    )


def list_argv() -> list[str]:
    return _tmux("list-panes", "-a", "-F", _FORMAT)


def attach_argv(name: str) -> list[str]:
    return _tmux("attach-session", "-t", f"={name}")


def stop_argv(name: str) -> list[str]:
    return _tmux("kill-session", "-t", f"={name}")


def pane_command(agent: str, args: list[str]) -> list[str]:
    """What the session's pane runs: a login bash (so the box's PATH and `harden.sh` apply, as in
    `fy box shell`), the TERM fallback, then `exec fy <agent> <args>`."""
    from . import box

    script = (
        f"{box._PATH_PREPEND_SNIPPET}{box._TERM_FALLBACK_SNIPPET}\n"
        f"exec {shlex.join(['fy', agent, *args])}"
    )
    return ["bash", "-lc", script]


def session_args(agent: str, args: list[str]) -> tuple[str, list[str]]:
    """``(id, the agent's args)``. Claude gets ``--session-id <id>`` so the two ids are one —
    unless the caller already names the session: ``--resume <uuid>`` (or ``--session-id <uuid>``)
    makes that uuid the id; ``--continue``, a bare ``--resume`` or ``--fork-session`` leave
    foldyard's id its own, since Claude picks the session then."""
    own = str(uuid.uuid4())
    if agent != "claude":
        return own, args
    if any(a.partition("=")[0] == "--fork-session" for a in args):
        return own, args
    for i, arg in enumerate(args):
        flag, eq, value = arg.partition("=")
        if flag not in _RESUME_FLAGS:
            continue
        if not eq and flag in ("--resume", "-r", "--session-id") and i + 1 < len(args):
            value = args[i + 1]
        value = value.lower()
        return (value if _UUID.fullmatch(value) else own), args
    return own, ["--session-id", own, *args]


def tmux_version(stdout: str) -> tuple[int, int] | None:
    """``tmux -V`` → (major, minor); None for a build that doesn't say (`next-3.6`, `master`)."""
    m = re.fullmatch(r"tmux (\d+)\.(\d+)[a-z]?\s*", stdout)
    return (int(m.group(1)), int(m.group(2))) if m else None


# ── where the commands run: in the box directly, or on the host through `<engine> exec` ───────────


@dataclass(frozen=True)
class Box:
    """A box to run tmux commands in. ``engine`` None = THIS box (we're inside it)."""

    name: str
    worktree: str
    engine: str | None = None
    env: dict | None = None

    def argv(self, cmd: list[str], *, tty: bool = False) -> list[str]:
        if self.engine is None:
            return cmd
        if not tty:
            return [self.engine, "exec", self.name, *cmd]
        from . import box

        script = f"{box._TERM_FALLBACK_SNIPPET}\nexec {shlex.join(cmd)}"
        return [
            self.engine,
            "exec",
            "-it",
            *box._terminal_env(),
            self.name,
            "bash",
            "-lc",
            script,
        ]

    def run(self, cmd: list[str], timeout: float = _LIST_TIMEOUT) -> subprocess.CompletedProcess:
        # stdin=DEVNULL: a listing never reads your terminal. Inherited, a child took the answer
        # typed at `fy box down`'s question (seen live).
        return subprocess.run(
            self.argv(cmd),
            env=self.env,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=timeout,
        )


def sessions_in(b: Box) -> list[Session]:
    """``b``'s sessions; none when its tmux has no server, isn't installed, or doesn't answer."""
    try:
        out = b.run(list_argv())
    except (OSError, subprocess.TimeoutExpired):
        return []
    if out.returncode != 0:
        return []
    return [_place(s, b) for s in parse_panes(out.stdout)]


def _place(s: Session, b: Box) -> Session:
    from dataclasses import replace

    return replace(s, worktree=b.worktree, box=b.name)


def this_box() -> Box:
    """The box we're in (only meaningful when ``config.in_box()``)."""
    wt = config.active_worktree()
    prefix = config.project_prefix()
    return Box(name=f"{prefix}-{wt}-devbox" if wt else f"{prefix}-devbox", worktree=wt)


def project_boxes() -> tuple[list[Box], str | None]:
    """``(the running boxes of this project's workspaces, why none can be asked)`` — on the host,
    through the read-only probes: a VM that isn't running is a reason, never a boot."""
    from . import box, devmode, stack

    if (reason := stack.machine_down()) is not None:
        return [], reason
    engine, env = box.engine_env()
    found = []
    for ws in devmode.workspaces():
        if ws.get("devbox"):
            wt = "" if ws["name"] == "main" else ws["name"]
            found.append(Box(f"{ws['project']}-devbox", wt, engine, env))
    return found, None


def live_in(engine: str, box_name: str, env: dict | None) -> list[Session]:
    """The sessions still running in one box — what `fy box down` and the TUI warn about."""
    return [s for s in sessions_in(Box(box_name, "", engine, env)) if s.alive]


def live_in_workspace(project: str) -> list[Session]:
    """:func:`live_in` for a workspace's box by its compose project (`<prefix>[-<worktree>]`), for
    a caller with no engine env of its own (the TUI's stop-box confirm). Best-effort: anything
    that keeps the box from answering reads as no sessions, never an error in that dialog."""
    try:
        from . import box

        engine, env = box.engine_env()
        return live_in(engine, f"{project}-devbox", env)
    except (Exception, SystemExit):
        return []


# ── listing ───────────────────────────────────────────────────────────────────────────────────────


@dataclass
class ProjectListing:
    project: str
    checkout: str
    sessions: list[Session]
    unavailable: str = ""  # why this project's boxes couldn't be asked ("" = they were)


def list_here() -> ProjectListing:
    """This project's sessions (in a box: this box's own)."""
    from . import stack

    project = config.project()
    if config.in_box():
        return ProjectListing(project, "", sessions_in(this_box()))
    checkout = str(stack.main_repo())
    boxes, reason = project_boxes()
    if reason is not None:
        return ProjectListing(project, checkout, [], reason)
    with ThreadPoolExecutor(max_workers=8) as pool:
        found = [s for got in pool.map(sessions_in, boxes) for s in got]
    sessions = [_with(s, project, checkout) for s in found]
    return ProjectListing(project, checkout, sessions)


def _with(s: Session, project: str, checkout: str) -> Session:
    from dataclasses import replace

    return replace(s, project=project, checkout=checkout)


def to_json(listing: ProjectListing) -> str:
    doc = {
        "project": listing.project,
        "checkout": listing.checkout,
        "unavailable": listing.unavailable,
        "sessions": [asdict(s) for s in listing.sessions],
    }
    return json.dumps(doc)


def from_json(raw: str) -> ProjectListing | None:
    """A child's `ls --here --json` back into a listing; None when it isn't one. Its strings
    passed through this host's own sanitiser again: the child is foldyard, but the data is a
    box's."""
    try:
        doc = json.loads(raw)
        sessions = []
        for row in doc.get("sessions") or []:
            exit_status = row.get("exit_status")
            sessions.append(
                Session(
                    name=_printable(row.get("name"), 64),
                    id=row["id"] if _UUID.fullmatch(str(row.get("id", ""))) else "",
                    agent=row.get("agent") if row.get("agent") in AGENTS else "",
                    created=_printable(row.get("created"), 32),
                    activity=_printable(row.get("activity"), 32),
                    attached=int(row.get("attached") or 0),
                    alive=bool(row.get("alive")),
                    exit_status=exit_status if isinstance(exit_status, int) else None,
                    project=_printable(row.get("project"), 64),
                    worktree=_printable(row.get("worktree"), 64),
                    box=_printable(row.get("box"), 128),
                    checkout=str(row.get("checkout") or ""),
                    exited=_printable(row.get("exited"), 32),
                )
            )
        return ProjectListing(
            _printable(doc.get("project"), 64),
            str(doc.get("checkout") or ""),
            [s for s in sessions if s.name],
            _printable(doc.get("unavailable"), 200),
        )
    except (ValueError, TypeError, AttributeError, KeyError):
        return None


def _child_env() -> dict[str, str]:
    """This process's env for a child run in another checkout: without the worktree this one
    pinned, nor a ``FOLDYARD_REPO`` that would point every child at the same project."""
    return {k: v for k, v in os.environ.items() if k not in ("WORKTREE", "FOLDYARD_REPO")}


def _list_checkout(checkout: Path) -> ProjectListing:
    """One project's listing, from this same CLI run in its main checkout."""
    name = checkout.name
    try:
        out = subprocess.run(
            [sys.executable, "-m", "foldyard", "agent", "ls", "--here", "--json"],
            cwd=checkout,
            env=_child_env(),
            capture_output=True,
            text=True,
            timeout=_PROJECT_TIMEOUT,
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        return ProjectListing(name, str(checkout), [], f"couldn't ask ({type(e).__name__})")
    got = from_json(out.stdout) if out.returncode == 0 else None
    if got is None:
        tail = _printable((out.stderr.strip().splitlines() or [""])[-1], 160)
        return ProjectListing(
            name, str(checkout), [], tail or f"couldn't ask (exit {out.returncode})"
        )
    return got


def list_all() -> list[ProjectListing]:
    """Every local project's listing: the adopted main checkouts, each asked in its own process."""
    from . import configpin

    checkouts = configpin.adopted_checkouts()
    if not checkouts:
        return []
    with ThreadPoolExecutor(max_workers=8) as pool:
        return list(pool.map(_list_checkout, checkouts))


def _table(listings: list[ProjectListing]) -> str:
    rows = [("PROJECT", "WORKTREE", "AGENT", "NAME", "STATE", "ACTIVITY", "ID")]
    notes = []
    for listing in listings:
        if listing.unavailable:
            notes.append(f"  {listing.project}: {listing.unavailable}")
        for s in listing.sessions:
            rows.append(
                (
                    s.project or listing.project,
                    s.worktree or "main",
                    s.agent or "?",
                    s.name,
                    s.state,
                    s.activity.replace("+00:00", "Z"),
                    s.id or "-",
                )
            )
    lines = []
    if len(rows) > 1:
        widths = [max(len(r[i]) for r in rows) for i in range(len(rows[0]))]
        lines = [
            "  ".join(c.ljust(w) for c, w in zip(r, widths, strict=True)).rstrip() for r in rows
        ]
    else:
        lines = ["(no agent sessions)"]
    if notes:
        lines += ["", "Not listed:", *notes]
    return "\n".join(lines)


# ── the verbs ─────────────────────────────────────────────────────────────────────────────────────


def ls(*, here: bool, as_json: bool) -> int:
    """`fy agent ls`: on your computer every local project (``here``: this one); in a box, its own
    sessions. ``as_json``: the same as JSON — one project's document with ``here`` (what the
    fan-out reads from each project), else a list of them."""
    if config.in_box() or here:
        if not (config.in_box() or config.in_project()):
            _err("✗ --here needs a foldyard project — run it in one, or drop --here.")
            return 1
        listing = list_here()
        print(to_json(listing) if as_json else _table([listing]))
        return 0
    listings = list_all()
    if config.in_project() and not any(_same(listing.checkout) for listing in listings):
        listings.insert(0, list_here())  # a checkout not adopted yet still lists itself
    if as_json:
        print("[" + ",".join(to_json(listing) for listing in listings) + "]")
        return 0
    print(_table(listings))
    if any(s.id for listing in listings for s in listing.sessions):
        print("\nAttach with `fy agent attach <id>` (Ctrl-B d detaches; the agent keeps running).")
    return 0


def _same(checkout: str) -> bool:
    """Is ``checkout`` this project's main checkout? (Never true outside a project.)

    ``stack`` is imported only past the project check: it imports ``devmode``, which binds the
    machine name from the working directory at import, and outside a project (``/``, whose name
    is empty) that refuses the whole command."""
    if not config.in_project():
        return False
    from . import stack

    try:
        return bool(checkout) and Path(checkout).resolve() == stack.main_repo().resolve()
    except (OSError, SystemExit):
        return False


def _matches(s: Session, target: str) -> bool:
    t = target.lower()
    return s.name == target or (
        bool(s.id) and (s.id == t or (len(t) >= _MIN_PREFIX and s.id.startswith(t)))
    )


def _one(candidates: list[Session], target: str) -> Session | None:
    hits = [s for s in candidates if _matches(s, target)]
    exact = [s for s in hits if s.id == target.lower() or s.name == target]
    hits = exact or hits
    if len(hits) == 1:
        return hits[0]
    if hits:
        _err(f"✗ {target!r} matches {len(hits)} sessions — name one by its full id:")
        for s in hits:
            _err(f"    {s.id or s.name}  ({s.project or '?'} / {s.worktree or 'main'} / {s.name})")
    return None


def _act(verb: str, target: str, *, here: bool) -> int:
    """`attach` / `stop` ``target`` (an id, a unique id prefix, or a name). In a box: this box. On
    your computer: this project's boxes, then — unless ``here`` — every local project's, handing
    over to the CLI in the project that holds it."""
    if config.in_box():
        b = this_box()
        return _do(verb, b, _one(sessions_in(b), target), target)
    if config.in_project():
        boxes, reason = project_boxes()
        candidates = [s for b in boxes for s in sessions_in(b)]
        found = _one(candidates, target)
        if found is not None:
            box = next(b for b in boxes if b.name == found.box)
            return _do(verb, box, found, target)
        if here:
            if reason:
                _err(f"({reason})")
            return _not_found(target)
    for listing in list_all():
        if _same(listing.checkout):
            continue  # asked above
        found = _one(listing.sessions, target)
        if found is not None:
            return _hand_over(verb, found, listing.checkout)
    return _not_found(target)


def _not_found(target: str) -> int:
    _err(f"✗ no agent session {target!r} — `fy agent ls` lists them.")
    return 1


def _do(verb: str, b: Box, found: Session | None, target: str) -> int:
    if found is None:
        return _not_found(target)
    if verb == "stop":
        out = b.run(stop_argv(found.name))
        if out.returncode != 0:
            _err(f"✗ couldn't stop {found.name}: {_printable(out.stderr.strip(), 200)}")
            return 1
        print(f"✓ stopped {found.name}")
        return 0
    if not found.alive:
        _err(f"(the agent in {found.name} has exited — its last screen follows; `fy agent stop "
             f"{found.handle}` removes it)")  # fmt: skip
    argv = b.argv(attach_argv(found.name), tty=True)
    if b.engine is None:
        os.execvp(argv[0], argv)
    return subprocess.run(argv, env=b.env).returncode


def _hand_over(verb: str, found: Session, checkout: str) -> int:
    """Run `fy agent <verb> --here <exact handle>` in the project that holds the session."""
    if not checkout or not Path(checkout).is_dir():
        _err(f"✗ {found.handle} is in {found.project}, whose checkout can't be found.")
        return 1
    argv = [sys.executable, "-m", "foldyard", "agent", verb, "--here", found.handle]
    return subprocess.run(argv, cwd=checkout, env=_child_env()).returncode


_AGE_UNITS = {"m": 60, "h": 3600, "d": 86400, "w": 7 * 86400}


def parse_age(text: str) -> int:
    """'30m' / '12h' / '7d' / '2w' → seconds. A unit is required: `clean --older-than 7` would
    otherwise read as seven seconds and remove every exited session."""
    m = re.fullmatch(r"\s*(\d+)\s*([mhdw])\s*", text.lower())
    if not m or int(m.group(1)) <= 0:
        raise ValueError(f"{text!r} isn't an age — a number and m, h, d or w (`7d`)")
    return int(m.group(1)) * _AGE_UNITS[m.group(2)]


def _stale(s: Session, cutoff: datetime) -> bool:
    """An exited session whose exit (else, from a tmux older than `pane_dead_time`, its last
    activity) is before ``cutoff``. A running one never is, nor one whose time can't be read."""
    if s.alive:
        return False
    try:
        when = datetime.fromisoformat(s.exited or s.activity)
    except ValueError:
        return False
    return when < cutoff


def _clean_boxes(boxes: list[Box], cutoff: datetime, *, dry_run: bool, label: str) -> int:
    rc, found = 0, False
    for b in boxes:
        for s in sessions_in(b):
            if not _stale(s, cutoff):
                continue
            found = True
            where = f"{label} / {b.worktree or 'main'} / {s.name}"
            if dry_run:
                print(f"  would remove {where}  ({s.state}, {s.exited or s.activity})")
                continue
            out = b.run(stop_argv(s.name))
            if out.returncode == 0:
                print(f"  ✓ removed {where}")
            else:
                _err(f"  ✗ couldn't remove {where}: {_printable(out.stderr.strip(), 200)}")
                rc = 1
    if not found:
        print(f"  {label}: no session exited before {cutoff.isoformat(timespec='seconds')}")
    return rc


def clean(older_than: str, *, here: bool, dry_run: bool) -> int:
    """`fy agent clean`: remove EXITED sessions that ended longer than ``older_than`` ago — every
    local project's on your computer (``here``: this one), this box's in a box. A running agent is
    never touched, whatever its age."""
    try:
        age = parse_age(older_than)
    except ValueError as e:
        _err(f"✗ {e}")
        return 2
    cutoff = datetime.now(UTC) - timedelta(seconds=age)
    if config.in_box():
        return _clean_boxes([this_box()], cutoff, dry_run=dry_run, label=config.project())
    if here:
        if not config.in_project():
            _err("✗ --here needs a foldyard project — run it in one, or drop --here.")
            return 1
        boxes, reason = project_boxes()
        if reason is not None:
            print(f"  {config.project()}: {reason}")
            return 0
        return _clean_boxes(boxes, cutoff, dry_run=dry_run, label=config.project())
    rc = 0
    flags = ["--older-than", older_than, *(["--dry-run"] if dry_run else [])]
    for checkout in _adopted_with_here():
        argv = [sys.executable, "-m", "foldyard", "agent", "clean", "--here", *flags]
        rc |= subprocess.run(argv, cwd=checkout, env=_child_env()).returncode
    return rc


def _adopted_with_here() -> list[Path]:
    """The adopted main checkouts, plus this project's when it hasn't been adopted yet."""
    from . import configpin

    checkouts = configpin.adopted_checkouts()
    if config.in_project() and not any(_same(str(c)) for c in checkouts):
        from . import stack  # in a project only — see _same

        checkouts.insert(0, stack.main_repo())
    return checkouts


def attach(target: str, *, here: bool) -> int:
    return _act("attach", target, here=here)


def stop(target: str, *, here: bool) -> int:
    return _act("stop", target, here=here)


def start(agent: str, name: str, args: list[str], *, attach_after: bool) -> int:
    """`fy agent start`: `fy <agent> <args>` in a new detached session in this checkout's box —
    run here when we're in the box, through `<engine> exec` from your computer."""
    if agent not in AGENTS:
        _err(f"✗ unknown agent {agent!r} — one of: {', '.join(AGENTS)}.")
        return 2
    if name and not _NAME.fullmatch(name):
        _err("✗ a session name is letters, digits, '-' and '_' (at most 64, starting with a letter "
             "or digit).")  # fmt: skip
        return 2
    b, cwd = _start_target()
    if b is None:
        return 1
    if not _tmux_ok(b):
        return 1
    sid, agent_args = session_args(agent, args)
    name = name or f"{agent}-{sid[:8]}"
    out = b.run(start_argv(name, sid, agent, cwd, pane_command(agent, agent_args)))
    if out.returncode != 0:
        _err(f"✗ couldn't start {name}: {_printable(out.stderr.strip(), 200)}")
        return 1
    print(f"✓ started {agent} in session {name} (id {sid})")
    if attach_after:
        return _do("attach", b, Session(name, sid, agent, "", "", 0, True, None), name)
    print(f"  attach: `fy agent attach {sid}` · list: `fy agent ls`")
    return 0


def _start_target() -> tuple[Box | None, str]:
    """``(the box to start in, the session's working directory)``: this box and the current
    directory inside one; on your computer the active worktree's running box, at its checkout."""
    if config.in_box():
        return this_box(), os.getcwd()
    from . import box, stack

    wt = config.active_worktree()
    if not stack.engine_reachable("start an agent in", box._start_hint(wt)):
        return None, ""
    ctx = box._ctx()
    name, _ = box._names(ctx)
    engine = config.engine()
    if not box._running(engine, name, ctx.env):
        _err(f"✗ dev box {name} not running — {box._start_it(ctx.worktree)}")
        return None, ""
    checkout = ctx.env.get("FOLDYARD_CHECKOUT") or str(config.repo_root())
    return Box(name, wt, engine, ctx.env), checkout


def _tmux_ok(b: Box) -> bool:
    try:
        out = b.run(["tmux", "-V"])
    except (OSError, subprocess.TimeoutExpired):
        out = None
    if out is None or out.returncode != 0:
        _err("✗ tmux isn't installed in the box. foldyard's packaged box image has it; on your own")
        _err('  image add `packages = ["tmux"]` under [box] in foldyard.toml, then `fy box up`.')
        return False
    version = tmux_version(out.stdout)
    if version is not None and version < MIN_TMUX:
        _err(f"✗ the box's tmux is {out.stdout.strip()}; `fy agent` needs 3.3 or newer (older tmux")
        _err("  passes an agent's raw escape sequences through to your terminal).")
        return False
    return True
