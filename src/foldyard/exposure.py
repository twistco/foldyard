#!/usr/bin/env python3
"""What this checkout's config asks the HOST to allow — the widenings inventory (`fy config
widenings`) and its doctor row.

Everything else in foldyard answers "is the posture what it claims?". This answers the question
underneath it: **what did we agree to, and where is it written?** Three kinds of config statement
loosen a host-side control, and each is easy to lose track of:

  - **capture exemptions** (``[proxy] passthrough``) — hosts the proxy tunnels un-decrypted under
    ``capture=on``. ``@all`` is one token that resolves to ~200 hosts; nothing anywhere showed that
    number, so "capture is on" read as "everything is logged" when it never meant that.
  - **injection targets** — the host each mechanism delivers a real credential to. Some are fixed
    in package code (claude, codex, github), some are config (``[[inject]] host``). The difference
    matters, so the report states it per row rather than listing them all as equals.
  - **agent steering** (``[claude]/[codex] system_prompt``, and the ``[claude.settings]`` /
    ``[codex.config]`` overrides the launchers pass to the CLI) — repo-controlled input to a
    privileged actor. Not a credential, same shape (`docs/security.md`). The settings tables are
    reported by KEY rather than by value: they reach the agent as ``--settings``/``-c``, which is
    where a ``hooks`` or ``permissions`` entry would live.

And one thing that is not a widening but reads exactly like one: **keys that no longer do
anything**. ``[proxy] allow`` was deleted when grants moved host-side (ADR-0023); a config still
carrying it looks locked down and isn't. Silently ignoring a key that means
"security" is how a stale config outlives the change that broke it — so :data:`IGNORED_KEYS` names
them with the verb that replaces them, and that's what makes the doctor row WARN.

The report also names the blocks a ``disabled = true`` SUBTRACTS (``config.merge_config``). That's
a narrowing, not a widening — but it's the one config statement whose effect is an absence, so if
the inventory didn't say it, nothing would.

Deliberately NOT counted: ``[[box.tools]]``, ``[box].bootstrap``, ``[project].compose``,
``[ports]``. They run code, but in the yard — the blast radius the VM already owns. Mixing them in
would make the report unreadable and imply the two tiers are comparable.

Reads the ADOPTED config on the host (:mod:`foldyard.configpin`), so it describes what the host is
actually running, not what the working tree proposes. Stdlib only; plugin imports are lazy.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from . import config, configpin

# Config keys foldyard once honoured and now IGNORES, with the replacement to print. A stale
# config that still carries one reads as a control that isn't there — naming it is the whole point
# of this table, so an entry stays here permanently rather than being cleaned up a release later.
IGNORED_KEYS: dict[str, str] = {
    "proxy.allow": (
        "grants moved to the host-side allow-store (outside the repo mount, so the box can't "
        "widen its own wall) — re-add with `fy allow add <host> --level permanent`, or declare "
        "the hosts under `[proxy] recommend` (with a why) for the host to OFFER at adoption"
    ),
    "inject.minter": (
        "consumer minter COMMANDS were removed — a minter is a packaged kind or an installed "
        "plugin, never a string the host executes (ADR-0023)"
    ),
    "inject.token_env": (
        "the token var is DERIVED from the axis (FY_INJECT_<AXIS>) — a declared one is ignored, "
        "so a config rule can't point at another mechanism's secret"
    ),
}

# Config that runs code in the YARD, not on the host. Listed by name in the report's footer so its
# absence from the widenings above reads as a decision rather than an oversight.
BOX_SCOPED = ("[[box.tools]]", "[box].bootstrap", "[box].warmup", "[project].compose", "[ports]")


# Which FILE a widening came from. The distinction is load-bearing for the question every reader
# actually has — "does this apply to my colleagues too?" — because `foldyard.local.toml` is
# gitignored and per-developer, and it WINS the deep merge. A shared prompt steers everyone's
# agent; a local one steers yours.
SHARED, LOCAL, DEFAULT = "foldyard.toml", "foldyard.local.toml", "built-in default"


@dataclass(frozen=True)
class Target:
    """One host a real credential is delivered to, and where that host was decided."""

    host: str
    source: str  # what declares it, e.g. `[[inject]] penpot` / `[claude] keyless = "oauth"`
    active: bool  # is its axis on right now?
    from_config: bool  # True = the host is repo config; False = fixed in package code
    origin: str = DEFAULT  # which file declares it (SHARED / LOCAL / DEFAULT)


@dataclass(frozen=True)
class Exposure:
    label: str  # "main" / "worktree feat"
    adopted: str  # digest of the adopted config ("" when nothing is adopted yet)
    drifted: bool  # …and the checkout differs from it
    in_box: bool
    bundle_refs: list[str] = field(default_factory=list)  # @refs that resolved
    unknown_refs: list[str] = field(default_factory=list)  # @refs that matched no bundle
    literals: list[str] = field(default_factory=list)  # hosts/globs declared in this file
    declared: bool = True  # was `passthrough` declared at all (vs defaulted to @all)?
    hosts: int = 0  # resolved passthrough patterns
    wildcards: int = 0  # …of which `*.suffix` globs
    passthrough_origin: str = DEFAULT
    targets: list[Target] = field(default_factory=list)
    prompts: list[tuple[str, int, str]] = field(default_factory=list)  # (key, lines, origin)
    # (key, the setting keys it declares, origin) for the CLI-config tables the launchers pass on
    # — `[claude.settings]` → `--settings <json>`, `[codex.config]` → one `-c` per leaf.
    agent_config: list[tuple[str, list[str], str]] = field(default_factory=list)
    # (dotted path, origin) for blocks switched off with `disabled = true`. Subtraction leaves no
    # trace in the resolved config — the table is simply gone — so it is reported from the raw
    # files, or "why is there no codex row?" has no answer anywhere.
    disabled: list[tuple[str, str]] = field(default_factory=list)
    # (host, why, status) — status: granted / pending / declined / store unreadable. The repo can
    # only ASK; the status says what THIS host's operator answered (`fy allow sync` / the gate).
    recommended: list[tuple[str, str, str]] = field(default_factory=list)
    recommend_origin: str = DEFAULT
    # (key, values, fix, origin)
    ignored: list[tuple[str, list[str], str, str]] = field(default_factory=list)
    wall: bool = False
    backend: str = ""
    enforcing: bool = False  # the egress wall's allowlist (host-owned)

    @property
    def concerns(self) -> list[str]:
        """The findings that make the doctor row WARN: config that reads as a control but isn't."""
        out = [
            f"`{key}` is IGNORED ({_plural(len(values), 'entry', 'entries')})"
            for key, values, _fix, _origin in self.ignored
        ]
        out += [f"unknown passthrough bundle @{ref}" for ref in self.unknown_refs]
        return out


def _plural(n: int, one: str, many: str) -> str:
    return f"{n} {one if n == 1 else many}"


def _files(cfg: config.Config) -> tuple[dict, dict]:
    """``(foldyard.toml, foldyard.local.toml)`` parsed SEPARATELY, so each widening can name the
    file it came from. Reads the ADOPTED copies on the host (what's running) and the working tree
    in the box (what a box session can see)."""
    drift = configpin.inspect(cfg)
    files = drift.tree if (config.in_box() or not drift.pinned_exists) else drift.pinned
    shared, local = (configpin._parse(files[name]) for name in configpin.PINNED_FILES)
    return shared, local


def _origin(shared: dict, local: dict, *path: str) -> str:
    """Which file supplies ``path`` (local wins the deep merge, as it does everywhere)."""

    def has(doc: dict) -> bool:
        for key in path:
            if not isinstance(doc, dict) or key not in doc:
                return False
            doc = doc[key]
        return True

    return LOCAL if has(local) else SHARED if has(shared) else DEFAULT


def _shared_note(origin: str) -> str:
    if origin == LOCAL:
        return "  (foldyard.local.toml — yours only, gitignored)"
    return "  (foldyard.toml — shared with the team)" if origin == SHARED else ""


def _passthrough_parts(entries: list[str]) -> tuple[list[str], list[str], list[str]]:
    """Split the declared ``passthrough`` entries into (@refs that resolve, @refs that don't,
    literal hosts/globs). An unknown ref expands to nothing — it trusts LESS, never more — which
    makes it a silent typo, hence its own bucket."""
    from .plugins._passthrough_bundles import BUNDLES

    refs, unknown, literals = [], [], []
    for entry in entries:
        if not entry.startswith("@"):
            literals.append(entry)
        elif entry == "@all" or entry[1:] in BUNDLES:
            refs.append(entry)
        else:
            unknown.append(entry[1:])
    return refs, unknown, literals


def _targets(
    cfg: config.Config, mode: dict, origin: str, shared: dict, local: dict
) -> list[Target]:
    """Every injection target this config can produce:

    1. the ``[[inject]]`` rows — config-declared hosts, on or off (a declared-but-off injector is a
       latent widening the operator should see BEFORE arming it),
    2. the rules the CURRENT mode activates (the packaged plugins: claude, codex, github),
    3. the rules another rung WOULD activate — asked of the REGISTRY rather than derived from host
       constants here, so a mechanism with an unusual shape reports as it actually behaves (Codex's
       ChatGPT rung injects on ``chatgpt.com/backend-api/codex``, not on the API host, and a report
       that guessed would quietly name the wrong destination).
    """
    from .plugins import registry
    from .plugins.inject import token_var

    reg = registry(cfg)
    out: list[Target] = []
    seen: set[str] = set()

    def add(where: str, source: str, active: bool, cfg_host: bool, org: str = DEFAULT) -> None:
        if where in seen:
            return
        seen.add(where)
        out.append(
            Target(host=where, source=source, active=active, from_config=cfg_host, origin=org)
        )

    for spec in config.inject_specs():
        axis, host = str(spec.get("axis") or ""), str(spec.get("host") or "")
        if axis and host:
            add(
                host + str(spec.get("path_prefix") or ""),
                f"[[inject]] {axis}  (token: ${token_var(axis)})",
                mode.get(axis, "off") != "off",
                True,
                origin,
            )
    for rule in reg.proxy_rules(mode):  # what the CURRENT posture activates
        add(rule.host + rule.path_prefix, rule.label or "packaged injector", True, False)
    for axis, rungs in reg.axis_rungs().items():  # …and what another rung would
        if not rungs or mode.get(axis, rungs[0]) != rungs[0]:
            continue  # already armed — its rules came from the pass above
        for rung in rungs[1:]:
            for rule in reg.proxy_rules({**mode, axis: rung}):
                add(
                    rule.host + rule.path_prefix,
                    f"{rule.label or 'packaged injector'} — arms with `fy mode {axis}={rung}`",
                    False,
                    False,
                    _origin(shared, local, axis, "keyless"),
                )
    return out


def _prompts(shared: dict, local: dict) -> list[tuple[str, int, str]]:
    """``(key, line count, origin)`` for every repo-controlled prompt prepended to an agent."""
    prompts = []
    for key, text in (
        ("[claude].system_prompt", config.claude_system_prompt()),
        ("[codex].system_prompt", config.codex_system_prompt()),
    ):
        if text.strip():
            table = key.strip("[]").split("].")[0]
            prompts.append(
                (
                    key,
                    len(text.strip().splitlines()),
                    _origin(shared, local, table, "system_prompt"),
                )
            )
    return prompts


def _agent_config(shared: dict, local: dict) -> list[tuple[str, list[str], str]]:
    """``(key, declared setting keys, origin)`` for the agent-CLI config tables.

    Reported by KEY, not by value: the point is which knobs the repo turns on someone's agent
    (``hooks``, ``permissions``, ``model``), and the values are one `fy config diff` away. Codex's
    are the flattened dotted paths the launcher actually emits, so what's listed here is what
    reaches the CLI."""
    from .box import _codex_overrides

    tables = (
        ("claude", "settings", sorted(config.claude_settings())),
        # …the flattened dotted paths, so what's listed is exactly what the `-c` flags carry.
        ("codex", "config", [o.partition("=")[0] for o in _codex_overrides(config.codex_config())]),
    )
    return [
        (f"[{table}.{sub}]", keys, _origin(shared, local, table, sub))
        for table, sub, keys in tables
        if keys
    ]


def _ignored(
    cfg: config.Config, shared: dict, local: dict
) -> list[tuple[str, list[str], str, str]]:
    """Which :data:`IGNORED_KEYS` this config actually carries, with the values it declares — so the
    report names them (`fy allow add github.com …`) instead of saying "something here is stale"."""
    found: list[tuple[str, list[str], str, str]] = []
    proxy = cfg.toml.get("proxy")
    if isinstance(proxy, dict) and isinstance(proxy.get("allow"), list):
        found.append(
            (
                "[proxy] allow",
                [str(h) for h in proxy["allow"]],
                IGNORED_KEYS["proxy.allow"],
                _origin(shared, local, "proxy", "allow"),
            )
        )
    inject_origin = _origin(shared, local, "inject")
    for key in ("minter", "token_env"):
        rows = [e for e in config.inject_specs() if e.get(key)]
        if rows:
            found.append(
                (
                    f"[[inject]] {key}",
                    [f"{e.get('axis', '?')} = {e[key]}" for e in rows],
                    IGNORED_KEYS[f"inject.{key}"],
                    inject_origin,
                )
            )
    return found


def collect(cfg: config.Config, mode: dict) -> Exposure:
    """Gather ``cfg``'s widenings. Bind ``cfg`` first (``with config.using(cfg)``) — every helper
    reads through the module config functions, so the caller's binding is what selects the
    checkout."""
    from .allowlist import declined, default_deny, live_hosts
    from .plugins.proxy import _resolve_passthrough

    drift = configpin.inspect(cfg)
    shared, local = _files(cfg)
    entries = config.proxy_passthrough()
    refs, unknown, literals = _passthrough_parts(entries)
    resolved = _resolve_passthrough(entries)
    proxy = cfg.toml.get("proxy")
    granted, refused = set(live_hosts()), declined()
    recommended = [
        (
            e["host"],
            e["why"],
            "store unreadable"
            if "*" in refused
            else "granted"
            if e["host"] in granted
            else "declined"
            if e["host"] in refused
            else "pending",
        )
        for e in config.proxy_recommend()
    ]
    return Exposure(
        label=drift.label,
        adopted=drift.pinned_digest(),
        drifted=drift.changed,
        in_box=config.in_box(),
        bundle_refs=refs,
        unknown_refs=unknown,
        literals=literals,
        declared=isinstance(proxy, dict) and proxy.get("passthrough") is not None,
        hosts=len(resolved),
        wildcards=sum(1 for h in resolved if h.startswith("*.")),
        passthrough_origin=_origin(shared, local, "proxy", "passthrough"),
        targets=_targets(cfg, mode, _origin(shared, local, "inject"), shared, local),
        prompts=_prompts(shared, local),
        agent_config=_agent_config(shared, local),
        disabled=[
            (f"[{path}]", LOCAL if in_local else SHARED)
            for path, in_local in config.disabled_blocks(shared, local)
        ],
        recommended=recommended,
        recommend_origin=_origin(shared, local, "proxy", "recommend"),
        ignored=_ignored(cfg, shared, local),
        wall=config.machine_wall(),
        backend=config.machine_backend(),
        enforcing=default_deny(),
    )


def render(exp: Exposure) -> list[str]:
    """The full inventory, as lines. Ordered by how much a reader should care: what isn't
    decrypted, where credentials go, what steers the agent, what's stale."""
    if exp.in_box:
        where = f"[{exp.label} · box view: this checkout, not the copy the host adopted]"
    elif not exp.adopted:
        where = f"[{exp.label} · nothing adopted yet — this is the working tree]"
    elif exp.drifted:
        where = f"[{exp.label} · adopted {exp.adopted} — the checkout DIFFERS (`fy config diff`)]"
    else:
        where = f"[{exp.label} · adopted {exp.adopted}]"
    out = [f"Repo-declared widenings — what foldyard.toml asks the HOST to allow   {where}", ""]

    out.append(f"  capture exemptions   [proxy] passthrough{_shared_note(exp.passthrough_origin)}")
    source = " ".join(exp.bundle_refs) if exp.declared else "not declared → defaults to @all"
    out.append(
        f"    {source} → {_plural(exp.hosts, 'host', 'hosts')} · "
        f"{_plural(exp.wildcards, 'wildcard suffix', 'wildcard suffixes')}"
        if exp.hosts
        else f"    {source or '(empty list)'} → nothing exempt: capture=on decrypts everything"
    )
    if exp.hosts:
        out.append(
            "    Under capture=on these are TLS-tunnelled: SNI only, no method/path/body logged."
        )
    out.append(
        f"    Declared literally here: {', '.join(exp.literals)}"
        if exp.literals
        else "    Declared literally here: none — every entry resolves to package data."
    )
    for ref in exp.unknown_refs:
        out.append(f"    ⚠ @{ref} matches no bundle — it expands to NOTHING (a typo trusts LESS)")
    out.append("")

    out.append("  injection targets   where a minted credential is delivered")
    if not exp.targets:
        out.append("    none — no injector is declared or active.")
    for t in exp.targets:
        state = "ON" if t.active else "off"
        out.append(f"    {t.host}   ← {t.source}  [{state}]{_shared_note(t.origin)}")
        out.append(
            "      Host is REPO CONFIG: adopting a change here re-points the credential."
            if t.from_config
            else "      Host fixed in package code, not config."
        )
    out.append("")

    if exp.prompts or exp.agent_config:
        out.append("  agent steering")
        for key, lines, origin in exp.prompts:
            who = "every colleague's agent" if origin == SHARED else "your agent"
            out.append(f"    {key} — {lines} lines prepended to {who}.{_shared_note(origin)}")
        for key, keys, origin in exp.agent_config:
            who = "every colleague's agent" if origin == SHARED else "your agent"
            out.append(
                f"    {key} — {_plural(len(keys), 'setting', 'settings')} passed to {who} on the "
                f"command line: {', '.join(keys)}.{_shared_note(origin)}"
            )
        out.append("")

    if exp.recommended:
        out.append(
            "  recommended egress   [proxy] recommend — the repo ASKS, this host answers"
            f"{_shared_note(exp.recommend_origin)}"
        )
        for host, why, status in exp.recommended:
            mark = {"granted": "✓", "declined": "✗"}.get(status, "·")
            out.append(f"    {mark} {host}{' — ' + why if why else ''}  [{status}]")
        if any(status == "pending" for _h, _w, status in exp.recommended):
            out.append("    Answer the pending ones with `fy allow sync` (or the TUI).")
        out.append("")

    if exp.disabled:
        out.append("  disabled blocks   declared, then switched off with `disabled = true`")
        for path, origin in exp.disabled:
            who = "for everyone" if origin == SHARED else "on this machine only"
            out.append(
                f"    {path} — removed from the resolved config, {who}.{_shared_note(origin)}"
            )
        out.append("")

    if exp.ignored:
        out.append("  ⚠ ignored keys — these read as security config, but nothing consumes them")
        for key, values, fix, origin in exp.ignored:
            out.append(
                f"    {key} — {_plural(len(values), 'entry', 'entries')}: "
                f"{', '.join(values)}{_shared_note(origin)}"
            )
            out.append(f"      {fix}")
        out.append("")

    out.append(f"  Not counted — runs in the yard, not on the host: {' · '.join(BOX_SCOPED)}")
    wall = (
        f"wall ON ({exp.backend})"
        if exp.wall
        else f"wall OFF ({exp.backend}) — proxy routing is cooperative"
    )
    allowlist = "allowlist ENFORCING" if exp.enforcing else "allowlist observing only"
    out.append(f"  Context: egress {wall} · {allowlist}.")
    if not exp.wall:
        out.append("    So the exemptions above bound what is LOGGED, not what can leave.")
    return out


def doctor_row(exp: Exposure) -> tuple[bool | None, str, str]:
    """``(ok, good, bad)`` for :func:`foldyard.devmode._config_pin_check`'s sibling row. WARN only
    for :attr:`Exposure.concerns` — config that reads as a control and isn't. Declaring a literal
    passthrough host or an injector is legitimate; it belongs in the report, not in a nag that
    never goes away."""
    detail = f"{exp.hosts} hosts exempt from capture"
    targets = [t for t in exp.targets if t.active]
    detail += f", {len(targets)} injector target{'' if len(targets) == 1 else 's'} live"
    pending = sum(1 for _h, _w, status in exp.recommended if status == "pending")
    if pending:
        # A pending recommendation is an outstanding OFFER, not a misconfiguration — it rides the
        # green row's detail (the gate and `fy allow sync` already ask) rather than WARNing.
        detail += f", {pending} recommended host{'' if pending == 1 else 's'} unanswered"
    if exp.concerns:
        # None ⇒ WARN, like the `adopted config` row: the posture is safe, the CONFIG is
        # misleading. A fail would say the yard is unsafe, which it isn't.
        return (None, "config widenings", f"{'; '.join(exp.concerns)} — `fy config widenings`")
    return (True, "config widenings", f"{detail} — `fy config widenings`")
