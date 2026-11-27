"""`fy config features`: what foldyard can do and what this project's config uses of it — and the
keys in it foldyard no longer reads, or never did.

A project set up under an older foldyard keeps the config it was set up with: an upgrade adds
features, but nothing adds them to a ``foldyard.toml``. And ``config.py`` reads the TOML with
``.get()`` and no schema (``compat``'s docstring says why that is by construction), so a typo, a
key a release removed and a default someone wrote out before it changed all read as nothing at
all. :data:`KEYS` is therefore the one list of every key foldyard reads — the schema the readers
don't have — and each key's ``since`` is the release whose docs/configuration.md first described
it. A key with a ``pitch`` is a *feature*, listed whether it is used or not, newest release first,
so what arrived since the project was set up is at the top. The project's
``recommended_foldyard_version`` is where its review stops: the releases at or below it are shown
under a line (dimmed with colour), since whoever raised it had the changelog in front of them.

The problems it reports are each a key the readers would pass over in silence:

- **unknown** — not a key foldyard reads, with the nearest one that is (a typo: exit 1);
- **removed** — :data:`foldyard.exposure.IGNORED_KEYS`, with what replaced it (exit 1);
- **renamed** — :data:`foldyard.config.RENAMED_KEYS`; the old spelling still works;
- **old-default** — :data:`OLD_DEFAULTS`: a value that was the default before a release changed
  it, written out, so the project kept the old behaviour without choosing it — or, where
  :data:`foldyard.exposure.IGNORED_VALUES` names it (``[machine] compose = "host"``), didn't keep
  it at all.

Reads the files the way ``fy config widenings`` does (:func:`foldyard.exposure._files`): the
ADOPTED copies on the host, the working tree in a box or before anything is adopted — and each
file on its own, so a problem names the file it is in. A report, never a host decision.
``tests/test_features.py`` holds :data:`KEYS` to docs/configuration.md both ways, to the inject
kinds' fields and to the experimental register, and the configs foldyard ships to no problems.
Stdlib only.
"""

from __future__ import annotations

import difflib
from collections.abc import Iterable
from dataclasses import dataclass, field

from . import compat, config
from .term import printable as _printable  # key names and values are the box's to choose

SHARED = "foldyard.toml"
LOCAL = "foldyard.local.toml"

#: How each top-level table is shaped. ``array`` is an array of tables (``[[inject]]``), whose
#: entries are checked one by one; ``open`` is a table whose keys are the project's own (port
#: names, plugin names, switch names), so nothing in it is unknown.
TABLE, ARRAY, OPEN = "table", "array", "open"


@dataclass(frozen=True)
class Key:
    table: str
    field: str = ""  # "" is the table itself
    since: str = "0.0.1"  # the release whose docs first described it (0.0.1: before the split)
    pitch: str = ""  # set: a feature, listed whether or not the project uses it
    on: object = None  # the value that counts as using it; None: any value

    @property
    def label(self) -> str:
        table = f"[[{self.table}]]" if SHAPES.get(self.table) == ARRAY else f"[{self.table}]"
        return f"{table} {self.field}" if self.field else table

    @property
    def title(self) -> str:
        """How a feature is named in a list: its key, and the value that turns it on if any."""
        return self.label + (f" = {_toml(self.on)}" if self.on is not None else "")


SHAPES: dict[str, str] = {
    "project": TABLE,
    "machine": TABLE,
    "engine": TABLE,
    "ports": OPEN,
    "proxy": TABLE,
    "inject": ARRAY,
    "secret": ARRAY,
    "overlay": ARRAY,
    "require": ARRAY,
    "box": TABLE,
    "claude": TABLE,
    "codex": TABLE,
    "vscode": TABLE,
    "reclaim": TABLE,
    "plugins": OPEN,
    "host": TABLE,
    "resnapshot_on_capability": OPEN,
}


def _keys(table: str, *fields: str, since: str = "0.0.1") -> tuple[Key, ...]:
    return tuple(Key(table, f, since) for f in fields)


KEYS: tuple[Key, ...] = (
    # [project]
    *_keys("project", "name", "prefix", "app", "app_port", "ensure_dirs", "external_network"),
    *_keys("project", "dev_vm_dir"),
    *_keys("project", "compose_env", since="0.3.2"),
    Key("project", "compose", pitch="a stack of your own beside the box (`fy up`)"),
    Key(
        "project", "worktree_init", pitch="a script that sets up each new worktree, in a container"
    ),
    Key("project", "min_foldyard_version", "0.2.0", "refuse to run under a foldyard too old"),
    Key(
        "project",
        "recommended_foldyard_version",
        "0.2.0",
        "nudge to upgrade; also where this list's review stops",
    ),
    Key("project", "foldyard_version_reasons", "0.2.0", "a ledger of why each version was adopted"),
    Key(
        "project",
        "private",
        "0.13.0",
        "foldyard for a project that doesn't use it, kept out of git",
    ),
    # [machine]
    *_keys("machine", "backend", "name", "cpus", "memory_mib", "disk_gib", "worktrees_root"),
    *_keys("machine", "worktree_base"),
    *_keys("machine", "vmtype", since="0.1.0"),
    *_keys("machine", "release_files", since="0.13.0"),
    Key("machine", "firewall", pitch="the VM firewall: the proxy is the box's only way out"),
    Key("machine", "host_firewall", "0.3.0", "the VM's firewall enforced again on your computer"),
    Key("machine", "runtime", "0.3.0", "the box and its containers under gVisor", on="gvisor"),
    Key("machine", "vmtype", "0.1.0", "the krunkit hypervisor (macOS, arm64)", on="krunkit"),
    Key(
        "machine",
        "monitor",
        "0.6.0",
        "a guest monitor recording what crosses out of a container",
        on="observe",
    ),
    Key(
        "machine",
        "monitor",
        "0.15.0",
        "the guest monitor also records containers' file changes",
        on="observe-files",
    ),
    Key(
        "machine",
        "attribution",
        "0.7.0",
        "the proxy's log names the container behind each request",
        on="record",
    ),
    *_keys("machine", "compose", since="0.9.0"),  # always "vm" since 0.16.0: not a feature now
    Key(
        "machine",
        "firewall_allow",
        "0.10.0",
        "openings in the VM firewall for a client that can't use a proxy",
    ),
    # [engine]
    *_keys("engine", "cli"),
    # [proxy]
    Key("proxy", pitch="the allowlisting egress proxy every request leaves through"),
    *_keys("proxy", "enforce", "passthrough", "no_proxy"),
    Key("proxy", "recommend", pitch="hosts the repo asks you to grant, each with a why"),
    # [[inject]]
    Key("inject", pitch="a credential the proxy adds on your computer, never held in the box"),
    *_keys("inject", "switch", "host", "label", "replay_on_401", "header", "query_param"),
    *_keys("inject", "path_prefix", "value_prefix", "ttl"),
    *_keys("inject", "kind", "app_id", "installation_id", "repositories", since="0.4.0"),
    Key(
        "inject",
        "kind",
        "0.4.0",
        "a GitHub App's installation token, minted per request",
        on="github-app",
    ),
    Key("inject", "box_env", "0.4.0", "placeholders in the box for a client that wants a token"),
    Key("inject", "emergency", "0.3.2", "a credential switch that expires by itself"),
    # [[secret]] · [[overlay]] · [[require]]
    Key("secret", pitch="secrets the host asks you for, checked against a pattern"),
    *_keys("secret", "var", "label", "how", "pattern", "base64", "when"),
    Key("overlay", pitch="compose files added while a switch is at a level"),
    *_keys("overlay", "file", "when", "env"),
    Key("require", pitch="one switch needs another, from your wiring"),
    *_keys("require", "switch", "when", "needs", "accepts", "reason", "severity", "message"),
    # [box]
    *_keys("box", "image", "sock_in_vm", "env", "bootstrap", "clean_docker_config"),
    *_keys("box", "git_index_split"),
    Key(
        "box", "shadow_volumes", pitch="in-tree dirs (node_modules, .venv) on volumes of their own"
    ),
    Key("box", "caches", pitch="package caches kept across box rebuilds"),
    Key("box", "warmup", pitch="background steps after box up (an install, a build)"),
    Key("box", "tools", pitch="one-time tool installs, reported ✓/✗ at box up"),
    Key("box", "packages", "0.5.0", "system packages baked into a cached layer of the box image"),
    Key("box", "git_hooks", "0.12.0", "the checkout's own git hooks run in the box"),
    # [claude] · [codex] · [vscode]
    Key("claude", pitch="Claude Code in the box (`fy claude`)"),
    *_keys("claude", "keyless", "system_prompt", "settings"),
    Key(
        "claude",
        "transcript_sync_seconds",
        "0.3.2",
        "Claude's transcripts copied out while the box runs",
    ),
    Key("codex", pitch="the Codex CLI in the box (`fy codex`)"),
    *_keys("codex", "keyless", "system_prompt", "config"),
    Key(
        "codex",
        "transcript_sync_seconds",
        "0.3.2",
        "Codex's transcripts copied out while the box runs",
    ),
    Key("vscode", pitch="VS Code attached to the box, its server kept across rebuilds (`fy code`)"),
    Key("vscode", "extensions", "0.3.0", "VS Code extensions installed in the box on attach"),
    *_keys("vscode", "settings", since="0.3.0"),
    # [reclaim] · [plugins] · supervisor settings
    Key("reclaim", "script", "0.3.2", "your own clean-up when `fy up` is short of disk"),
    *_keys("plugins"),
    *_keys("host", "notifications", since="0.3.2"),
    Key(
        "resnapshot_on_capability",
        since="0.3.2",
        pitch="services recreated when a credential comes back",
    ),
)

#: A value that WAS the default until ``changed_in``, and isn't now: written out, it keeps the old
#: behaviour without anyone having chosen it. From the changelog's "now defaults to" entries.
#: ``(table, field, old value, changed in, today's default)``.
OLD_DEFAULTS: tuple[tuple[str, str, object, str, object], ...] = (
    ("proxy", "passthrough", ["@all"], "0.4.0", ["@jvm", "@linux"]),
    ("machine", "compose", "host", "0.16.0", "vm"),
)


@dataclass(frozen=True)
class Use:
    key: Key
    used: bool
    value: object = None  # what the project sets it to, when it does
    origin: str = ""  # the file that sets it: SHARED · LOCAL · ""


@dataclass(frozen=True)
class Problem:
    kind: str  # unknown · removed · renamed · old-default
    label: str
    detail: str
    origin: str


@dataclass(frozen=True)
class Report:
    features: list[Use]
    problems: list[Problem] = field(default_factory=list)
    reviewed: str | None = None  # [project] recommended_foldyard_version

    @property
    def failed(self) -> bool:
        """An unknown or removed key: something the project wrote that does nothing."""
        return any(p.kind in ("unknown", "removed") for p in self.problems)


# ── the check ──────────────────────────────────────────────────────────────────────────────────


def _fields(table: str) -> set[str]:
    renamed = {old for t, old, _ in config.RENAMED_KEYS if t == table}
    removed = {k.split(".", 1)[1] for k in _ignored() if k.split(".", 1)[0] == table}
    own = {k.field for k in KEYS if k.table == table and k.field}
    return own | renamed | removed | {config.DISABLE_KEY}


def _ignored() -> dict[str, str]:
    from .exposure import IGNORED_KEYS

    return IGNORED_KEYS


def _ignored_values() -> dict[str, str]:
    from .exposure import IGNORED_VALUES

    return IGNORED_VALUES


def _entries(value: object) -> list[dict]:
    if isinstance(value, dict):
        return [value]
    if isinstance(value, list):
        return [e for e in value if isinstance(e, dict)]
    return []


def _suggest(name: str, candidates: Iterable[str], fmt: str) -> str:
    near = difflib.get_close_matches(name, sorted(candidates), n=1, cutoff=0.6)
    return f"; did you mean {fmt.format(near[0])}?" if near else ""


def _unknown(doc: dict, origin: str) -> list[Problem]:
    found: list[Problem] = []
    for table, value in doc.items():
        shape = SHAPES.get(table)
        if shape is None:
            # A scalar at the top level is no table at all; a table is named as one.
            label = f"[{table}]" if isinstance(value, (dict, list)) else table
            hint = _suggest(table, SHAPES, "`[{}]`")
            detail = f"not a table foldyard reads{hint}"
            found.append(Problem("unknown", _printable(label), _printable(detail), origin))
            continue
        if shape == OPEN:
            continue
        known = _fields(table)
        for i, entry in enumerate(_entries(value)):
            for name in entry:
                if name in known:
                    continue
                key = Key(table, name)
                where = ""
                if shape == ARRAY:
                    switch = entry.get("switch") or entry.get("var") or entry.get("file")
                    where = f" (entry {i + 1}" + (f", {switch}" if switch else "") + ")"
                hint = _suggest(name, known - {config.DISABLE_KEY}, "`{}`")
                detail = f"not a key foldyard reads{where}{hint}"
                found.append(Problem("unknown", _printable(key.label), _printable(detail), origin))
    return found


def _removed(doc: dict, origin: str) -> list[Problem]:
    found: list[Problem] = []
    for dotted, replacement in _ignored().items():
        table, name = dotted.split(".", 1)
        if any(name in e for e in _entries(doc.get(table))):
            label = f"[{dotted}]" if SHAPES.get(table) == OPEN else Key(table, name).label
            found.append(Problem("removed", label, replacement, origin))
    return found


def _renamed(shared: dict, local: dict) -> list[Problem]:
    return [
        Problem(
            "renamed", old, f"renamed to `{new}`; the old spelling still works", _file(in_local)
        )
        for old, new, in_local in config.renamed_keys(shared, local)
    ]


def _old_defaults(doc: dict, origin: str) -> list[Problem]:
    found: list[Problem] = []
    for table, name, old, changed_in, now in OLD_DEFAULTS:
        if any(_same(e.get(name), old) for e in _entries(doc.get(table))):
            label = f"{Key(table, name).label} = {_toml(old)}"
            detail = (
                f"the default before {changed_in}; today's is {_toml(now)}, so keep it only if "
                "you mean it"
            )
            if ignored := _ignored_values().get(f"{table}.{name}"):  # nothing to mean any more
                detail = f"the default before {changed_in}, and no longer honoured: {ignored}"
            found.append(Problem("old-default", label, detail, origin))
    return found


def _same(value: object, old: object) -> bool:
    """Whether ``value`` is ``old`` as foldyard read it: a word in any case and spacing."""
    if isinstance(value, str) and isinstance(old, str):
        return value.strip().lower() == old.lower()
    return value == old


def _file(in_local: bool) -> str:
    return LOCAL if in_local else SHARED


def _toml(value: object) -> str:
    """A value as it reads in TOML (lists and strings; enough for what is shown here)."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str):
        return f'"{value}"'
    if isinstance(value, list):
        return "[" + ", ".join(_toml(v) for v in value) + "]"
    return str(value)


def _use(key: Key, merged: dict, shared: dict, local: dict) -> Use:
    entries = _entries(merged.get(key.table))
    if not key.field:
        # `bool(entries)`: a table counts declared, an array only with an entry left after the
        # `disabled = true` ones are dropped (the merge leaves an empty list behind).
        return Use(key, bool(entries), origin=_origin(key, shared, local))
    values = [e[key.field] for e in entries if key.field in e]
    if not values:
        return Use(key, False)
    if key.on is None:
        value = values[0] if SHAPES.get(key.table) != ARRAY else None
        return Use(key, True, value, _origin(key, shared, local))
    used = key.on in values
    value = key.on if used else values[0]
    return Use(key, used, value, _origin(key, shared, local))


def _origin(key: Key, shared: dict, local: dict) -> str:
    def has(doc: dict) -> bool:
        if key.table not in doc:
            return False
        return not key.field or any(key.field in e for e in _entries(doc[key.table]))

    return LOCAL if has(local) else SHARED if has(shared) else ""


def check(shared: dict, local: dict) -> Report:
    """``shared`` (foldyard.toml) and ``local`` (foldyard.local.toml), each as parsed. Pure."""
    merged = config.merge_config(shared, local)
    # Each file under today's names, as the merge reads it: a local `wall` is a local `firewall`.
    shared_now, local_now = config._apply_renames(shared), config._apply_renames(local)
    uses = [_use(k, merged, shared_now, local_now) for k in KEYS if k.pitch]
    problems: list[Problem] = []
    for raw, now, origin in ((shared, shared_now, SHARED), (local, local_now, LOCAL)):
        problems += _unknown(now, origin)
        problems += _removed(raw, origin)
        problems += _old_defaults(now, origin)
    problems += _renamed(shared, local)
    project = merged.get("project")
    reviewed = project.get("recommended_foldyard_version") if isinstance(project, dict) else None
    return Report(uses, problems, _printable(reviewed) if isinstance(reviewed, str) else None)


def _files(cfg: config.Config) -> tuple[dict, dict]:
    from . import exposure

    return exposure._files(cfg)


def collect(cfg: config.Config) -> Report:
    """The report for ``cfg``'s checkout — its adopted files on the host, its tree in a box."""
    return check(*_files(cfg))


# ── rendering ──────────────────────────────────────────────────────────────────────────────────

_DIM, _RESET = "\x1b[2m", "\x1b[0m"
GLYPH = {"unknown": "✗", "removed": "✗", "renamed": "⚠", "old-default": "ℹ"}


def _experimental(key: Key) -> bool:
    from . import experimental

    return any(
        f.key == f"{key.table}.{key.field}" and f.value == key.on for f in experimental.REGISTER
    )


def _ordered(report: Report) -> list[tuple[str, list[Use]]]:
    by: dict[str, list[Use]] = {}
    for use in report.features:
        by.setdefault(use.key.since, []).append(use)
    return sorted(by.items(), key=lambda kv: compat._parse(kv[0]) or (0,), reverse=True)


def behind(since: str, reviewed: str | None) -> bool:
    """Is a feature from release ``since`` at or below the ``reviewed`` version?"""
    mark, at = compat._parse(reviewed), compat._parse(since)
    return mark is not None and at is not None and at <= mark


def _feature_line(use: Use, width: int) -> str:
    tags = []
    if _experimental(use.key):
        tags.append("experimental")
    if use.origin == LOCAL:
        tags.append(LOCAL)
    if not use.used and use.value is not None:
        tags.append(f"set to {_printable(_toml(use.value))}")
    tail = f"  ({', '.join(tags)})" if tags else ""
    return f"    {'●' if use.used else '·'} {use.key.title.ljust(width)}  {use.key.pitch}{tail}"


def render(report: Report, *, color: bool = False) -> list[str]:
    width = max(len(u.key.title) for u in report.features)
    lines = ["Features by release, newest first (● in use · not set):"]
    marked = False
    for since, uses in _ordered(report):
        reviewed = behind(since, report.reviewed)
        if reviewed and not marked:
            lines.append(
                f"  ── reviewed up to {report.reviewed} ([project] recommended_foldyard_version) ──"
            )
            marked = True
        group = [f"  {since}", *(_feature_line(u, width) for u in uses)]
        lines += [f"{_DIM}{line}{_RESET}" for line in group] if reviewed and color else group
    if report.problems:
        lines += ["", "Keys foldyard won't act on as written:"]
        for p in report.problems:
            lines.append(f"  {GLYPH[p.kind]} {p.label} — {p.detail}  ({p.origin})")
    return lines


def as_dict(report: Report) -> dict:
    return {
        "reviewed": report.reviewed,
        "features": [
            {
                "key": u.key.label,
                "on": u.key.on,
                "title": u.key.title,
                "since": u.key.since,
                "pitch": u.key.pitch,
                "experimental": _experimental(u.key),
                "used": u.used,
                "value": u.value,
                "origin": u.origin,
            }
            for u in report.features
        ],
        "problems": [
            {"kind": p.kind, "label": p.label, "detail": p.detail, "origin": p.origin}
            for p in report.problems
        ],
    }
