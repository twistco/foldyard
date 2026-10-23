"""monitorhooks.py — the guest monitor's hooks: the operator's program or endpoint, called with
batches of attributed events (docs/monitor-pipeline.md §6). How a scoring script, a decision model
or a notifier plugs in without being part of foldyard.

**Configured host-side only** — ``<state dir>/monitor-hooks.toml`` (``~/.foldyard/<project>/``),
never ``foldyard.toml``. Repo config is writable by anything in the box: a ``command`` there would
be host code execution for the box (ADR-0023), and a ``url`` would send every event wherever the
box chose. The file is re-read every round, so an edit applies within seconds; there is nothing
to adopt, because nothing in the mount can reach it.

Delivery: only SETTLED events (their attribution can't change any more: a later snapshot exists
anywhere in the store, or — only when none does — :data:`SETTLE_SECONDS` passed), and never one
past an event that hasn't settled yet; at-least-once (a hook's cursor advances only on success —
exit 0, or HTTP 2xx); retried with backoff, and after :data:`MAX_ATTEMPTS` the batch is skipped
and counted. Each hook reads the store from its OWN position, so one that fell behind (a catch-up
pull, a long backoff, a storm) resumes where it stopped rather than at the store's tail, and the
events it reads are joined with the catalogue's snapshots around them however far apart those are
(:func:`monitorlog.join`) — a byte window around the position once lost every event between two
snapshots further apart than it (2026-10-21).

A hook's ``who`` picks the attributions it gets; one without gets every event but the VM's and
podman's own (:data:`SKIP_UNLESS_ASKED`, the file's ``skip_unless_asked``) — hundreds of thousands
an hour on a busy VM, none of them the box's. What a hook's filters pass over is never silent:
each batch counts it by ``who`` (``passed``), and an event that crossed out of a container into
the VM's or podman's namespace reaches every hook whatever its ``who``.

Whatever a hook didn't get, or got without the full evidence, is SAID in the batch: ``gaps``
(:func:`_record_gaps`, :func:`_unsettled`, :func:`_ambiguous`) — the store rotated past its
position, events lost between the VM and here, the relay missing Tetragon's, events released
without a snapshot after them, events its ``who`` filter passed over because the join couldn't
place them between two containers, a batch skipped. A consumer marks those times "no evidence",
never "nothing happened".

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

from . import boxmounts, config, monitorlog, monitorsink, monitorstore

SCHEMA = "fy.monitor.events/1"
STAGES = ("events",)  # later: scored, decided, reviewed (docs/monitor-pipeline.md §5)
SETTLE_SECONDS = 120.0
BATCH_MAX = 500
MAX_ATTEMPTS = 5
ROUND_EVERY = 5.0
READ_BYTES = 4 << 20  # how far past its position a hook reads at a time
ROUND_BATCHES = 20  # a hook behind (a storm, a backoff) sends up to this many batches a round…
ROUND_SECONDS = 2.0  # …for about this long
STALL_SECONDS = 600.0  # a cursor still this long while the store grows is stuck (`problems`)


WHO = ("container", "build", "unattributed", "ambiguous", "podman", "vm")
# What a hook without `who` passes over (counted in each batch's `passed`): the VM's and podman's
# own events — none of them the box's, unless one crossed out of a container (always sent). Image
# builds are NOT here: a build step runs what the box wrote. `skip_unless_asked` in the file.
SKIP_UNLESS_ASKED = ("vm", "podman")


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
    # what it gets only when its `who` names it — the file's `skip_unless_asked`, not the hook's
    unless_asked: tuple[str, ...] = field(default=SKIP_UNLESS_ASKED, compare=False)

    def matches(self, e: monitorlog.Event) -> bool:
        if self.who:
            who = e.who in self.who
        else:
            who = e.who not in self.unless_asked
        return (
            (not self.kinds or e.kind in self.kinds)
            and (who or e.crossed)  # crossed out of a container: never left out by `who`
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
    files: dict[str, str] = {}  # file → the hook writing it
    unless_asked = SKIP_UNLESS_ASKED
    if "skip_unless_asked" in doc:
        try:
            got = _strings(doc["skip_unless_asked"], "`skip_unless_asked`")
            bad = [w for w in got if w not in WHO]
            if bad:
                raise ValueError(
                    f"no event has {', '.join(map(repr, bad))} (who: {', '.join(WHO)})"
                )
            unless_asked = got
        except ValueError as e:
            errors.append(f"`skip_unless_asked` ignored, {', '.join(SKIP_UNLESS_ASKED)} kept: {e}")
    for i, raw in enumerate(doc.get("hook") or []):
        label = f"hook #{i + 1}"
        try:
            if not isinstance(raw, dict):
                raise ValueError("must be a table")
            name = raw.get("name")
            if not isinstance(name, str) or not name.strip():
                raise ValueError("needs a `name`")
            label = f"hook '{name}'"
            if "skip_unless_asked" in raw:  # TOML gives a key after `[[hook]]` to that hook
                errors.append(
                    f"{label}: `skip_unless_asked` ignored here, {', '.join(unless_asked)} kept: "
                    "it belongs at the top of the file, before any [[hook]]"
                )
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
            if file in files:
                raise ValueError(
                    f"`file` is already hook '{files[file]}'s (a file holds one hook's chain)"
                )
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
                    unless_asked=unless_asked,
                )
            )
            seen.add(name)
            if file:
                files[file] = name
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
# A hook without `who` gets every event but these (each batch counts what it passed over):
# skip_unless_asked = ["vm", "podman"]
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


KINDS = ("exec", "exit", "connect", "file", "other")


def _values(values: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(x.strip() for v in values for x in v.split(",") if x.strip())


_TO_BOX_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")


def to_box_file(name: str) -> Path:
    """Where ``fy monitor hook add NAME --to-box`` puts the sink: the to-box's ``monitor/``."""
    return boxmounts.to_box_dir() / "monitor" / f"{name}.jsonl"


def _box_writable_roots() -> list[tuple[Path, str]]:
    """Trees a box can write, for the warning on a ``--file`` placed in one."""
    from . import stack

    main = stack.find_main_repo()
    if main is None:  # not in a checkout: nothing to compare against
        return []
    return [
        (main, "the checkout"),
        (config.worktrees_root(main), "the worktrees root"),
        (boxmounts.from_box_dir(), "the from-box"),
    ]


def cli_add(
    name: str,
    *,
    command: tuple[str, ...] = (),
    file: str = "",
    to_box: bool = False,
    url: str = "",
    who: tuple[str, ...] = (),
    kinds: tuple[str, ...] = (),
    worktrees: tuple[str, ...] = (),
) -> int:
    """`fy monitor hook add`. Refuses — writing nothing — what the supervisor would skip, a filter
    value that can never match (a typo would silently send nothing), and a file the sink would
    refuse to write. ``to_box``: the sink in the to-box (ADR-0033), which every box reads and none
    can write — on a VM that has one; elsewhere it says why and points at ``--file``."""
    if sum(map(bool, (command, file or to_box, url))) != 1 or (file and to_box):
        print(
            "✗ give exactly one of `--to-box`, `--file PATH`, `--url URL`, or a program after `--`"
        )
        return 1
    if to_box:
        from . import machine  # lazy: binds the machine name from the adopted config

        why = machine.to_box_unavailable()
        if why:
            print(f"✗ no to-box for this project: {why}.")
            print(
                "  Use `--file PATH` instead: still signed (`fy monitor check PATH` finds an "
                "edit), but a box that can write that directory can change the file."
            )
            return 1
        if not _TO_BOX_NAME.fullmatch(name):
            print(f"✗ '{name}' can't name a file in the to-box: letters, digits, `.`, `_`, `-`")
            return 1
        file = str(to_box_file(name))
        Path(file).parent.mkdir(mode=0o700, parents=True, exist_ok=True)
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
    if to_box:
        in_box = f"{boxmounts.TO_BOX_IN_BOX}/monitor/{name}.jsonl"
        print(f"  In every box it is {in_box} — read-only there and in the VM.")
        print(
            "  Each batch is one signed JSON line; `fy monitor check "
            f"{shlex.quote(file)}` verifies it, and a copy that leaves your computer keeps the "
            "signature."
        )
        print(
            "  A VM or box from before the to-box shares none: `fy machine stop && fy up`, then "
            "`fy box down && fy box up`."
        )
    elif file:
        print(
            "  Each batch is one signed JSON line. Anything that can write that directory (the "
            "box, if it is in the checkout) can still edit the file — "
            f"`fy monitor check {shlex.quote(file)}` says whether it has."
        )
        where = next(
            (what for root, what in _box_writable_roots() if Path(file).is_relative_to(root)), ""
        )
        if where:
            print(
                f"  ⚠ it is in {where}, which the box can write: it can delete or replace the "
                "file (the signature finds an edit, but can't stop one). `--to-box` puts it where "
                "the box can read it and not change it."
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
        except (ValueError, RecursionError):
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
    name = name or next((h.name for h in load()[0] if h.file == path), "")
    if not name and (name := _first_hook(data)):
        print(
            f"ℹ hook '{name}' is taken from the file's first line, which whatever can write the "
            "file controls — no configured hook writes this path; `--hook NAME` names it yourself"
        )
    if not name:
        print(f"✗ {path}: no hook names it — say which with `--hook NAME`")
        return 1
    st = _load_state().get(name)
    chain = (st.sink_seq, st.sink_mac) if st and st.sink_file == path else None
    got = monitorsink.verify(data, key, name, chain)
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


def problems(st: HookState | None, now: float, grew: float | None = None) -> list[str]:
    """What is wrong with a hook's delivery: failing now, events it has ever skipped, and a
    cursor that stood still for :data:`STALL_SECONDS` while events kept arriving (``grew``: when
    the store's newest one did). A cursor moves over the events a filter doesn't want too, so only
    a hook that is stuck stays still that long — 0.6.1's upgrade left hooks so, silently."""
    out: list[str] = []
    if st is None:
        return out
    if st.attempts:
        wait = max(0.0, st.next_try - now)
        out.append(f"failing ({st.attempts} attempts): {st.error} — retrying in {wait:.0f}s")
    else:
        since = st.moved_at or st.ok_at
        grew = monitorstore.last_event_recv() if grew is None else grew
        if since and grew - since > STALL_SECONDS:
            at = f"{st.last[0][:8]}:{st.last[1]}" if st.last else "nothing yet"
            out.append(
                f"no progress for {(now - since) / 60:.0f} min while events kept arriving — "
                f"its cursor is at {at}; `fy host logs` says why"
            )
    if st.dropped:
        last = f" (last error: {st.error})" if st.error and not st.attempts else ""
        out.append(f"{st.dropped} events skipped after {MAX_ATTEMPTS} failed attempts{last}")
    return out


def progress(hook: Hook, st: HookState | None, now: float) -> str:
    if st is None or not (st.delivered or st.ok_at):
        return "nothing delivered yet"
    ago = f", last {now - st.ok_at:.0f}s ago" if st.ok_at else ""
    line = f", line {st.sink_seq} of its file" if hook.file and st.sink_file == hook.file else ""
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
    grew = monitorstore.last_event_recv()
    out: list[str] = []
    for h in hooks:
        kind, where = target(h)
        picks = [(k, getattr(h, k)) for k in ("kinds", "who", "worktrees")]
        filters = " ".join(f"{k}={','.join(v)}" for k, v in picks if v)
        if not h.who:
            but = f" but {', '.join(h.unless_asked)}" if h.unless_asked else ""
            filters = " ".join(x for x in (filters, f"all events{but}") if x)
        out.append(f"{h.name}  {kind}  {where}  [{filters}]")
        st = states.get(h.name)
        out.append(f"  ✓ {progress(h, st, now)}")
        if st is not None and st.passed:
            counts = ", ".join(
                f"{w} {n}" for w, n in sorted(st.passed.items(), key=lambda x: -x[1])
            )
            out.append(f"  · passed over since its last batch: {counts}")
        out.extend(f"  ⚠ {why}" for why in problems(st, now, grew))
    out.extend(f"✗ {e}" for e in errors)
    out.append(f"— {hooks_file()}")
    return out


# ── state ─────────────────────────────────────────────────────────────────────────────


@dataclass
class HookState:
    last: list = field(default_factory=list)  # [boot, seq] of the last delivered event
    pos: list = field(default_factory=list)  # [segment id, offset] in the store after that event
    attempts: int = 0
    next_try: float = 0.0
    delivered: int = 0
    dropped: int = 0
    error: str = ""
    ok_at: float = 0.0
    sink_seq: int = 0  # a file hook: the last line written, which `fy monitor check` ends on
    sink_mac: str = ""  # …and its mac, the next line's `prev`
    sink_file: str = ""  # …in this file: a hook given another `file` starts a new chain there
    gaps: list = field(default_factory=list)  # gaps owed to the hook's next batch (see `gaps`)
    passed: dict = field(default_factory=dict)  # who -> events passed over, owed to the next batch
    last_time: str = ""  # the time of the event at `last`
    moved_at: float = 0.0  # when `last` last moved — sent or passed over, so progress either way


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


def last_snapshots(records: list[dict], cat=None) -> dict[str, int]:
    """Each boot's last snapshot seq — anywhere in the store when the catalogue is given, else
    among ``records``."""
    last: dict[str, int] = {}
    for r in records:
        if r.get("k") == "scopes" and "boot" in r:
            last[r["boot"]] = max(last.get(r["boot"], -1), int(r["seq"]))
    if cat is not None:
        for boot in {r["boot"] for r in records if "boot" in r}:
            last[boot] = max(last.get(boot, -1), cat.last_snapshot(boot))
    return last


def settled(records: list[dict], now: float, cat=None) -> list[monitorlog.Event]:
    """The attributed events whose attribution can't change any more: a snapshot after them
    exists in their boot — anywhere in the store, which the catalogue ``cat`` knows; a later
    snapshot merely outside ``records`` must not leave them waiting, nor release them half-joined
    — or none does and they arrived more than :data:`SETTLE_SECONDS` ago (the relay stalled, or
    the boot ended). Joined as over the whole store (:func:`monitorlog.join`). In store order,
    and only up to the first one that hasn't settled: a VM restarted within the settle window
    leaves the old boot's tail unsettled while the new boot's first snapshot settles its events,
    and a cursor moved onto the new boot would rank the old tail behind it for good."""
    last_snap = last_snapshots(records, cat)
    recv: dict[tuple[str, int], float] = {}
    for r in records:
        if r.get("k") == "ev" and "boot" in r:
            recv[(r["boot"], int(r["seq"]))] = float(r.get("recv", 0.0))
    out = []
    for e in monitorlog.join(records, cat, config.project_prefix()):
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


def batch(
    hook: Hook,
    events: list[monitorlog.Event],
    gaps: list[dict] | None = None,
    passed: dict[str, int] | None = None,
) -> dict:
    """One batch. ``gaps`` is always there, usually empty: the stretches of the stream this batch
    accounts for that it can't vouch for (see :func:`deliver`). ``passed`` too: how many events
    the hook's filters passed over since its last batch, by ``who`` — so the VM's and podman's own,
    which a hook gets only when it asks, are counted, never silently gone. A batch can carry only
    gaps — ``events`` empty, ``first``/``last`` null — when nothing this hook wants came with
    them."""
    return {
        "schema": SCHEMA,
        "project": config.project(),
        "hook": hook.name,
        "first": [events[0].boot, events[0].seq] if events else None,
        "last": [events[-1].boot, events[-1].seq] if events else None,
        "events": [asdict(e) for e in events],
        "gaps": list(gaps or []),
        "passed": dict(passed or {}),
    }


def payload(
    hook: Hook,
    events: list[monitorlog.Event],
    gaps: list[dict] | None = None,
    passed: dict[str, int] | None = None,
) -> bytes:
    return json.dumps(batch(hook, events, gaps, passed)).encode()


def _to_file(
    hook: Hook,
    st: HookState,
    events: list[monitorlog.Event],
    gaps: list[dict] | None = None,
    passed: dict[str, int] | None = None,
) -> str:
    """Append the batch as the next signed line of the hook's chain; ``""`` on success. The
    sequence moves only on success, so a retry re-writes the same seq (see monitorsink.verify).
    The chain belongs to the file: a hook pointed at another one starts at seq 1 there (a file
    that merely went missing at the same path does NOT — its next line must still fail the
    check)."""
    if st.sink_file != hook.file:
        st.sink_seq, st.sink_mac, st.sink_file = 0, "", hook.file
    try:
        key = monitorsink.ensure_key()
        body = batch(hook, events, gaps, passed)
        line, mac = monitorsink.signed_line(key, body, st.sink_seq + 1, st.sink_mac)
        monitorsink.append(hook.file, line)
    except (OSError, monitorsink.Refused) as e:
        return str(e)[:300]
    st.sink_seq, st.sink_mac = st.sink_seq + 1, mac
    return ""


# ── gaps: what a batch can't vouch for ────────────────────────────────────────────────
#
# A gap is {"why", "after", "before", "since", "until", "detail"}: evidence is missing, or
# incomplete, for the records strictly between `after` and `before` ([boot, seq] in store order;
# null = open), which fall between the event times `since` and `until` ("" = unknown). An
# `ambiguous` gap also names the mount namespace (`mnt`) it is about.


def _gap(why: str, after: list | None, before: list | None, since: str, until: str, detail: str):
    return {"why": why, "after": after, "before": before, "since": since, "until": until,
            "detail": detail}  # fmt: skip


def _ev_time(r: dict) -> str:
    return str((r.get("e") or {}).get("time") or "")


def _record_gaps(records: list[dict]) -> list[tuple[tuple[str, int] | None, dict]]:
    """The gaps the store itself records in ``records``, each with the event that follows it
    (``None`` when none has yet) — a gap is owed once the cursor moves past that event:
    sequence numbers that never reached your computer (``transport``), and the relay's own
    "Tetragon's export rotated while I was down" (``relay``)."""
    out: list[tuple[tuple[str, int] | None, dict]] = []
    waiting: list[dict] = []
    last_time = ""
    for r in records:
        if r.get("k") == "ev" and "boot" in r:
            t = _ev_time(r)
            for g in waiting:
                g["until"] = t
                out.append(((r["boot"], int(r["seq"])), g))
            waiting, last_time = [], t
        elif r.get("k") == "gap" and "boot" in r:
            boot = r["boot"]
            if "from" in r:
                lo, hi = int(r["from"]), int(r["to"])
                why, detail = "transport", f"{hi - lo + 1} records never reached your computer"
            else:
                lo = hi = int(r["seq"])
                why, detail = "relay", f"the relay restarted and missed events: {r.get('why', '')}"
            waiting.append(_gap(why, [boot, lo - 1], [boot, hi + 1], last_time, "", detail))
    return out + [(None, g) for g in waiting]


def _unsettled(passed: list[monitorlog.Event], aged: set[tuple[str, int]]) -> list[dict]:
    """A gap per run of ``passed`` events released on age with no snapshot after them in the
    store: attributed from the snapshot before alone — a container that started after it, or
    whose namespace number was reused, reads as ``unattributed``."""
    out: list[dict] = []
    run: list[monitorlog.Event] = []
    for e in [*passed, None]:
        if e is not None and (e.boot, e.seq) in aged and (not run or run[-1].boot == e.boot):
            run.append(e)
            continue
        if run:
            b = run[0].boot
            out.append(_gap("unsettled", [b, run[0].seq - 1], [b, run[-1].seq + 1], run[0].time,
                            run[-1].time, f"{len(run)} events attributed without a snapshot "
                            "after them (none came within the settle window)"))  # fmt: skip
        run = [e] if e is not None and (e.boot, e.seq) in aged else []
    return out


def _ambiguous(hook: Hook, passed: list[monitorlog.Event]) -> list[dict]:
    """A gap per mount namespace for the ``ambiguous`` events in ``passed`` that ``hook`` is passing
    over (a worktree filter passes over every one: it has no worktree) but would want were they
    placed: two snapshots name different containers for the
    namespace and the containers' inits don't say where it changed hands — either container's,
    or podman's own short-lived namespace in between (``mnt`` says which; the events around it name
    the two). Without this a ``who = ["container"]`` hook can't tell lost evidence from a container
    that did nothing (cabash, 2026-10-23). Rare: on cabash's busiest night, 31 namespaces over the
    whole boot once the inits are read, none outside a container being replaced. Not for
    ``unattributed``: podman's own namespace and image builds keep tens of thousands of those an
    hour coming, a gap for them would say nothing."""
    runs: dict[tuple[str, int | None], list[monitorlog.Event]] = {}
    for e in passed:
        if (
            e.who == "ambiguous"
            and not hook.matches(e)
            and (not hook.kinds or e.kind in hook.kinds)
            and (not hook.who or "container" in hook.who)
        ):
            runs.setdefault((e.boot, e.mnt), []).append(e)
    out = []
    for (boot, mnt), run in runs.items():
        gap = _gap("ambiguous", [boot, run[0].seq - 1], [boot, run[-1].seq + 1], run[0].time,
                   run[-1].time, f"{len(run)} events in mount namespace {mnt}, which "
                   "changed hands between two snapshots: the join can't place them")  # fmt: skip
        out.append({**gap, "mnt": mnt})
    return out


def deliver(
    hook: Hook,
    st: HookState,
    events: list[monitorlog.Event],
    now: float,
    where: dict[tuple[str, int], list] | None = None,
    gaps: list[tuple[tuple[str, int] | None, dict]] | None = None,
    aged: set[tuple[str, int]] | None = None,
) -> str:
    """One batch for one hook: the next up-to-:data:`BATCH_MAX` matching events after its cursor,
    with every gap the cursor moves past (``st.gaps`` owed from before, the store's own gap
    records ``gaps``, and runs of ``aged`` events — released without a later snapshot). Nothing
    wanted but a gap owed → a batch of gaps alone. ``where`` maps an event to its store position,
    kept beside the cursor so the next round reads from there. Returns a log line when something
    worth saying happened, else ``""``."""
    where = where or {}

    def move(e: monitorlog.Event) -> None:
        st.last, st.pos = [e.boot, e.seq], where.get((e.boot, e.seq), [])
        st.last_time, st.moved_at = e.time, now

    if now < st.next_try:
        return ""
    pending = after(events, st.last)
    picked: list[int] = []
    end = len(pending) - 1  # the last event the cursor moves over
    # Pass over what this hook doesn't want in the same round, so a narrow filter never takes
    # minutes of rounds to skip a backlog (seen on a 72k-event store); send at most one batch.
    for i in range(0, len(pending), BATCH_MAX * 4):
        scan = range(i, min(i + BATCH_MAX * 4, len(pending)))
        picked = [j for j in scan if hook.matches(pending[j])][:BATCH_MAX]
        if picked:
            # A full batch ends at its last event (the rest comes next round); otherwise the
            # whole scan is covered — the events this hook doesn't want are passed over with it.
            end = picked[-1] if len(picked) == BATCH_MAX else scan[-1]
            break
    wanted = [pending[j] for j in picked]
    passed = pending[: end + 1]
    keys = {(e.boot, e.seq) for e in passed}
    owed = [*st.gaps, *(g for k, g in gaps or [] if k in keys), *_unsettled(passed, aged or set()),
            *_ambiguous(hook, passed)]  # fmt: skip
    sent = set(picked)
    over = dict(st.passed)  # everything the cursor moves over unsent, by who: never silently
    for j, e in enumerate(passed):
        if j not in sent:
            over[e.who] = over.get(e.who, 0) + 1
    if not wanted and not owed:
        if passed:
            move(passed[-1])
            st.passed = over  # owed to the next batch
        return ""
    if hook.file:
        why = _to_file(hook, st, wanted, owed, over)
    else:
        body = payload(hook, wanted, owed, over)
        why = _run_command(hook, body) if hook.command else _post(hook, body)
    if not why:
        if passed:
            move(passed[-1])
        st.gaps, st.passed = [], {}
        st.attempts, st.next_try, st.error, st.ok_at = 0, 0.0, "", now
        st.delivered += len(wanted)
        return ""
    st.attempts += 1
    st.error = why
    if st.attempts >= MAX_ATTEMPTS:
        if wanted:
            first, last = wanted[0], wanted[-1]
            owed.append(_gap("skipped", st.last or None, [last.boot, last.seq + 1], first.time,
                             last.time, f"{len(wanted)} events this hook failed to take "
                             f"{MAX_ATTEMPTS} times ({why})"))  # fmt: skip
        if passed:
            move(passed[-1])
        st.gaps = owed  # the skipped batch's gaps are still owed — to the next one
        st.passed = over  # …and so is what it passed over
        st.dropped += len(wanted)
        st.attempts, st.next_try = 0, 0.0
        return (
            f"monitor hook '{hook.name}': skipped {len(wanted)} events after "
            f"{MAX_ATTEMPTS} failures ({why})"
        )
    st.next_try = now + min(300.0, 10.0 * 2 ** (st.attempts - 1))
    return f"monitor hook '{hook.name}': delivery failed, retrying ({why})"


def round_once(now: float | None = None, log: Callable[[str], None] = lambda m: None) -> None:
    """Every configured hook: batches until it is caught up (at most :data:`ROUND_BATCHES`, for
    about :data:`ROUND_SECONDS`) or a delivery fails. Never raises for a hook's failure."""
    hooks, errors = load()
    for msg in errors:
        log(msg)
    if not hooks:
        return
    now = time.time() if now is None else now
    states = _load_state()
    cat = monitorlog.catalogue()
    try:
        for hook in hooks:
            st = states.setdefault(hook.name, HookState())
            started = time.monotonic()
            for _ in range(ROUND_BATCHES):
                mark = (list(st.last), list(st.pos), len(st.gaps), st.attempts, st.dropped)
                records, where, gaps, lost = _window(st, cat)
                if lost:
                    since = f"after {st.last[0]}:{st.last[1]}" if st.last else "it was added"
                    log(
                        f"monitor hook '{hook.name}': the store rotated past its position — events "
                        f"since {since} were removed before it read them (its next batch says so)"
                    )
                events = settled(records, now, cat)
                last_snap = last_snapshots(records, cat)
                aged = {(e.boot, e.seq) for e in events if e.seq > last_snap.get(e.boot, -1)}
                msg = deliver(hook, st, events, now, where, gaps, aged)
                if msg:
                    log(msg)
                moved = (list(st.last), list(st.pos), len(st.gaps), st.attempts, st.dropped)
                if st.attempts or moved == mark or time.monotonic() - started > ROUND_SECONDS:
                    break
    finally:
        if cat is not None:
            cat.close()
    _save_state(states)


def _window(st: HookState, cat=None) -> tuple[list[dict], dict, list, bool]:
    """A hook's records for this read — on from its position; for a hook with none yet, from the
    store's last :data:`READ_BYTES`, where it is anchored at once (so a hook with nothing settled
    to send yet doesn't slide along with the tail) — with each event's position, the gaps the
    store records among them, and whether the position was lost. A position this store can't
    resolve (an older foldyard's inode) is found again by the cursor's ``(boot, seq)``; one whose
    segment retention removed is lost — only once the store PROVES it (:func:`_gone`), never
    because a search came up empty while the store moved under it: reading resumes at the
    oldest record, and the hook owes a ``rotated`` gap from its cursor to there. A position
    behind its own cursor (0.6.1 put hooks there, at the oldest record, on a false "lost") is
    moved up to the cursor, or past what it read — never left where every read is events the
    hook already had, with nothing to send and nothing to move it on."""
    segs = monitorstore.segments()
    if cat is not None:
        # caught up with THIS listing, not the round's: a segment sealed since (the pull's first
        # append after an upgrade seals the older foldyard's live file) would otherwise be read
        # with no snapshots, claims or spans of its own — joined wrong, or its events not found
        try:
            cat.catch_up(segs, whole=True)
        except Exception:
            pass
    if not st.pos and not st.last:
        start = monitorstore.tail(READ_BYTES, segs)
        if start is None:
            return [], {}, [], False
        st.pos = start
    read, found = monitorstore.read(st.pos, READ_BYTES, segs) if st.pos else ([], False)
    if found and _behind(read, st.last):
        at = monitorstore.find(st.last, segs, cat)
        again, there = monitorstore.read(at, READ_BYTES, segs) if at else ([], False)
        if at is None or not there or _behind(again, st.last):
            st.pos = read[-1][1]  # walk on: the cursor's event is further on, or gone
            return [], {}, [], False
        st.pos, read = at, again
    lost = False
    if not found:
        at = monitorstore.find(st.last, segs, cat) if st.last else None
        if at is None:
            if not segs:
                return [], {}, [], False
            gone = _gone(st.last, segs, cat) if st.last else _removed(st.pos, segs)
            if gone is None:
                return [], {}, [], False  # nothing proves it gone: look again next round
            lost, at = gone, [segs[0].id, 0]
        read, _ = monitorstore.read(at, READ_BYTES, segs)
        st.pos = at
        if lost:
            first = next((r for r, _ in read if "boot" in r and "seq" in r), None)
            st.gaps.append(
                _gap(
                    "rotated",
                    st.last or None,
                    [first["boot"], int(first["seq"])] if first else None,
                    st.last_time,
                    next((_ev_time(r) for r, _ in read if r.get("k") == "ev"), ""),
                    "the store removed these events before this hook read them",
                )
            )
    records = [r for r, _ in read]
    where = {(r["boot"], int(r["seq"])): p for r, p in read if r.get("k") == "ev" and "boot" in r}
    return records, where, _record_gaps(records), lost


def _behind(read: list[tuple[dict, list]], last: list) -> bool:
    """Every event in ``read`` (one at least) is the cursor's or one before it in its boot: the
    position is behind the cursor."""
    if not last:
        return False
    seqs = [(r["boot"], int(r["seq"])) for r, _ in read if r.get("k") == "ev" and "boot" in r]
    return bool(seqs) and all(b == last[0] and s <= int(last[1]) for b, s in seqs)


def _gone(last: list, segs: list[monitorstore.Segment], cat=None) -> bool | None:
    """``True`` when the store PROVES the event ``last`` gone, after :func:`monitorstore.find`
    came up empty over ``segs``: the store didn't move under that search (a segment sealed,
    compressed or removed since ``segs`` was listed — the search may have missed it), and the
    catalogue doesn't say ``segs`` hold that boot's events on both sides of it (then it is the
    catalogue that is behind, not the store). Without a catalogue, the search read every segment.
    ``None``: not proven — look again."""
    if monitorstore.segments() != segs:
        return None
    if cat is None:
        return True
    try:
        span = cat.span(str(last[0]), segs)
    except Exception:
        return None
    return True if span is None or not span[0] <= int(last[1]) <= span[1] else None


def _removed(pos: list, segs: list[monitorstore.Segment]) -> bool | None:
    """For a hook with no cursor yet (anchored, nothing past it): whether the segment its
    position names was removed — ``True`` when older than every one the store has (and the store
    didn't move under the listing), or when no segment can have it (an older foldyard's inode, or
    its live file since sealed under an id of its own: with no cursor to find, nothing can say
    whether retention took what was after it, so the gap is claimed), or when its segment is
    there but the offset is past its end in a store that didn't move (a seal or a compression
    under the listing makes a listed segment read short for a moment); ``None``: look again."""
    sid = pos[0] if pos else None
    if not isinstance(sid, str) or sid.startswith(monitorstore.LEGACY):
        return True
    if monitorstore.segments() != segs:
        return None
    return True if any(s.id == sid for s in segs) or sid < segs[0].id else None


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
