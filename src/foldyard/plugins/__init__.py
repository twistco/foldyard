"""foldyard plugin framework — the spine that turns the hardcoded gcp/github credential
machinery into pluggable axes (ADR-0015).

A plugin contributes, for its credential mechanism:
  - one or more mode **axes** (name, rungs, per-rung blurb, the daemon its status maps to,
    which rungs are emergency/TTL-bound),
  - the host **daemons** a given posture demands (the supervisor reconciles to them),
  - the recipe **env** the posture derives (emitted as ``${K:-v}`` defaults),
  - **doctor** checks ("what can this machine grant?", shallow + deep IAM probes),
  - **verify** assertions (mode-aware posture for its mechanism),
  - **box** env+mounts (``box_args`` — what the posture bakes into the dev box),
  - **TUI** panels (``tui_panels`` — a data-only tab the TUI renders, e.g. the Network Log),
  - egress-proxy **injection rules** (``proxy_rules`` — header rewrites the shared mitm proxy
    applies; the built-in ``proxy`` plugin aggregates them and owns the proxy/log/panel).

The substrate (devmode/supervisor/tui) calls the merged ``Registry``, never a specific
plugin — so a consumer adds a credential mechanism by *shipping a plugin*, not by editing
the core. Built-ins (gcp, github) are loaded directly (always available, no dist metadata
needed, work when run from source); third-party plugins register on the
``foldyard.plugins`` entry-point group and are discovered here. Stdlib only — this loads
on the recipe hot path (``foldyard env``/``shellenv`` derive env via the registry).
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable, Iterator
from contextlib import nullcontext
from dataclasses import dataclass, replace
from importlib.metadata import entry_points
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..config import Config

ENTRY_POINT_GROUP = "foldyard.plugins"
# A POSIX environment-variable name — what a `[[secret]]` `var` must be, since it lands verbatim as
# a host.env key.
_ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


@dataclass(frozen=True)
class Requires:
    """One DECLARATIVE cross-axis requirement: while the owning axis sits at a rung in
    ``when``, ``axis`` must sit at a rung in ``accepts`` — else ``Registry.mode_issues``
    emits a ``severity`` row whose synthesized message carries the full atomic fix
    (``fy mode <axis>=<accepts[0]> <owner>=<value>``).

    This is the data form of the hand-coded cross-plugin ``mode_issues`` guards (llm→gcp,
    storage→gcp, fakedep→fakecred all had this exact shape): the dependency lives on the
    axis that HAS it, referencing the other axis by NAME only — no plugin's code reads
    another plugin's posture — and one evaluator (property-tested over generated constraint
    graphs in tests/test_properties.py) replaces N hand-rolled guards. Uniform semantics the
    guards used to re-derive individually: an ABSENT required axis (not loaded under this
    consumer's config) satisfies nothing, so the requirement still fires; and ``accepts``
    lists every satisfying rung, so a superset rung (gcp=user ⊇ sa) is declared once instead
    of remembered per guard. Combination WARNINGS with the opposite absence semantics (warn
    only when the other axis IS at a specific rung — auth0×storage) stay on the
    ``mode_issues`` hook.

    Rows come from TWO tiers, merged at Registry construction: the owning plugin's
    ``Axis.requires`` for INTRINSIC couplings (fakedep→fakecred — true wherever the plugin
    runs), and the consumer's ``[[require]]`` table in ``foldyard.toml`` for WIRING-dependent
    ones (llm→gcp holds only because Tangible's overlays route LLM traffic through Vertex/ADC;
    another consumer's llm rungs might need an ``[[inject]]`` credential axis instead — an axis
    no plugin file could name). See ``config.requires_declared``."""

    when: tuple[str, ...]  # owning-axis rungs that activate the requirement
    axis: str  # the required axis, by name (may be another plugin's, or absent entirely)
    accepts: tuple[str, ...]  # rungs of ``axis`` that satisfy it; FIRST is the suggested fix
    severity: str = "error"  # "error" (set_mode refuses) | "warn" (printed, still applied)
    reason: str = ""  # human name for what's needed, e.g. "the runtime-SA identity"
    message: str = ""  # full custom message — overrides synthesis entirely

    def render(self, owner: str, value: str, owner_default: str) -> str:
        """The issue message for ``owner=value`` violating this requirement."""
        if self.message:
            return self.message
        need = self.reason or f"{self.axis}={'/'.join(self.accepts) or '<no rung>'}"
        fix = f": `fy mode {self.axis}={self.accepts[0]} {owner}={value}`" if self.accepts else ""
        return f"{owner}={value} needs {need}{fix} (or drop back to {owner}={owner_default})"


@dataclass(frozen=True)
class Axis:
    """A posture dimension from its zero-secret DEFAULT (rung 0) to emergency. Rung 0 is the
    axis's resting state — what an unset/expired/unknown value reads as. Credential ladders
    name it ``off`` by convention; a swap-style axis whose resting state isn't an on/off
    toggle names it for what it IS (``storage``'s ``local``, ``auth0``'s ``real``). ``blurb``
    must cover every rung; ``emergency`` rungs carry a TTL and auto-revert to the default
    (the supervisor's structural guarantee); ``requires`` declares the rungs' cross-axis
    coherence requirements as data (see :class:`Requires`)."""

    name: str  # e.g. "gcp"
    rungs: tuple[str, ...]  # ("off", "logs", "sa", "user") — rungs[0] is the default
    blurb: dict[str, str]  # rung -> one-line human description
    daemon: str | None = None  # the daemon name this axis's status maps to (show/TUI)
    emergency: tuple[str, ...] = ()  # rungs that carry a TTL + auto-revert (e.g. ("user",))
    requires: tuple[Requires, ...] = ()  # declarative cross-axis requirements (see Requires)

    def __post_init__(self) -> None:
        if not self.rungs:
            raise ValueError(f"axis {self.name!r}: rungs must be non-empty")
        if set(self.blurb) != set(self.rungs):
            raise ValueError(f"axis {self.name!r}: blurb must cover exactly its rungs {self.rungs}")
        if not set(self.emergency) <= set(self.rungs):
            raise ValueError(
                f"axis {self.name!r}: emergency rungs {self.emergency} not all in rungs"
            )
        if self.default in self.emergency:
            raise ValueError(
                f"axis {self.name!r}: the default rung {self.default!r} cannot be emergency "
                "(expiry reverts TO the default)"
            )
        for req in self.requires:
            # Only the OWNING side is validated here (the required axis may be absent under
            # this consumer's config — that's the "absent satisfies nothing" semantics, not
            # an error). Loud ValueError like the axis checks: a broken declaration fails in
            # development, not as a requirement that silently never fires.
            if req.severity not in ("error", "warn"):
                raise ValueError(
                    f"axis {self.name!r}: requires severity {req.severity!r} "
                    "must be 'error' or 'warn'"
                )
            if not req.when or not set(req.when) <= set(self.rungs):
                raise ValueError(
                    f"axis {self.name!r}: requires.when {req.when} must be a non-empty "
                    f"subset of its rungs {self.rungs}"
                )

    @property
    def default(self) -> str:
        """The zero-secret resting rung — what unset/invalid/expired values read as."""
        return self.rungs[0]


@dataclass
class DoctorContext:
    """What a plugin's doctor checks need from the substrate, passed in (rather than
    imported) so the plugin never imports devmode — devmode imports the registry, so the
    reverse would cycle. ``run``/``which``/``result`` are devmode's own helpers, so plugin
    subprocess calls still hit the redacting command log and render consistently."""

    deep: bool
    run: Callable[..., tuple[int, str]]  # devmode._run(cmd, timeout=…) -> (rc, output)
    which: Callable[[str], bool]  # devmode._which
    result: Callable[..., tuple[str, str, str]]  # devmode._result(ok, name, good, bad)
    probe: Callable[[int], bool]  # devmode.probe(port) — TCP liveness (box→host.containers vs Mac)


@dataclass
class VerifyContext:
    """What a plugin's verify assertions need, passed in (not imported) so the plugin never
    imports verify. ``env`` is the resolved stack env (carries ``FOLDYARD_CHECKOUT`` etc.);
    ``which`` returns True when a command is on PATH. Plugins contribute MECHANISM-specific
    posture checks only — the credential-AGNOSTIC backstops (no ssh keys, no netrc, git push
    refused) stay in core's verify and must never depend on a plugin being present."""

    in_box: bool
    env: dict
    which: Callable[[str], bool]


@dataclass(frozen=True)
class PanelData:
    """What a :class:`TuiPanel`'s ``refresh`` returns each tick: the summary line shown above
    the table + the table rows, ordered exactly as the plugin wants them displayed. ``rows``
    are cell-string tuples (one string per column) that MAY carry Rich console markup for
    styling — the plugin is responsible for escaping any untrusted text (lazily importing
    ``rich.markup.escape`` inside ``refresh``, never at module load)."""

    summary: str
    rows: list[tuple[str, ...]]


@dataclass(frozen=True)
class PanelGroup:
    """One collapsible group of a :class:`PanelTree` (e.g. every request to one host). ``key`` is
    the STABLE identity used to preserve the user's expand/collapse choice across refreshes — the
    display ``label`` carries live counts so it can't be the key. ``label`` and each ``children``
    line MAY carry Rich markup; escape untrusted text plugin-side (lazy ``rich.markup.escape``)."""

    key: str  # stable group identity (e.g. the host) — survives label changes across refreshes
    label: str  # the group header row (markup ok), e.g. "api.github.com  12 req · 1 inj"
    children: list[str]  # the leaf rows under the group (markup ok), display-ordered


@dataclass(frozen=True)
class PanelTree:
    """What a tree-style :class:`TuiPanel` (``kind="tree"``) returns each tick: the summary line +
    grouped, collapsible rows. The core TUI renders it as a Textual ``Tree`` (groups expand to
    their rows) and preserves which groups the user expanded across refreshes, keyed by
    :attr:`PanelGroup.key`. The DATA-ONLY contract is unchanged — the plugin still imports no
    Textual; it just hands back groups instead of flat rows."""

    summary: str
    groups: list[PanelGroup]


@dataclass(frozen=True)
class TuiPanel:
    """A plugin-contributed TUI tab (ADR-0015). Deliberately
    DATA-ONLY: the plugin names the tab + its columns and supplies a ``refresh`` callable
    returning :class:`PanelData` (``kind="table"``) or :class:`PanelTree` (``kind="tree"``); the
    core TUI builds the Textual widgets and paints it. So a plugin never imports Textual (it loads
    on the registry hot path). ``refresh`` is invoked on a timer by the TUI — already inside a
    Textual context — so it may lazily import rich/etc. there. The pane id is ``f"tab-{id}"``; a
    table's widget is ``f"#{id}-table"``, a tree's is ``f"#{id}-tree"``."""

    id: str  # pane-id SUFFIX → the TabPane id is f"tab-{id}" (e.g. "network" → "tab-network")
    title: str  # the tab label (e.g. "Network Log")
    columns: tuple[str, ...]  # the DataTable column headers (kind="table"; () for a tree)
    refresh: Callable[[], PanelData | PanelTree]  # () -> data; the core TUI calls it on a timer
    kind: str = "table"  # "table" → DataTable(columns, rows); "tree" → Tree(grouped PanelTree)


@dataclass(frozen=True)
class InjectRule:
    """One egress-proxy header-injection rule an injector plugin contributes — ADR-0007's
    ``(host-pattern, header, minter, on-401-replay?)``. The built-in ``proxy`` plugin aggregates
    these from ``registry().proxy_rules(mode)`` and drives its single mitmdump from them — the
    real credential is minted HOST-side by ``minter`` and injected in flight, never entering the
    box. ``requires`` is host env that must be PRESENT or the supervisor refuses to launch the
    proxy,
    so it belongs only to vars whose absence makes the whole listener pointless — never to one
    mechanism's credential (under always-route a proxy that won't start connection-refuses every box
    request). ``env`` is the separate, usually larger set of names this rule's minter may READ: the
    addon runs each minter with a small base env plus these, rather than inheriting the supervisor's
    environment, which carries every axis's secret from ``host.env``.
    ``label`` names the proxy in daemon status. Many rules coexist: the proxy serializes them into
    ``egress_proxy.py``'s ``INJECT_RULES`` set (one proxy, N hosts, each its own minter + cache —
    so github + claude + codex + any ``[[inject]]`` can all be live at once)."""

    host: str  # the host pattern to inject on (e.g. "api.github.com")
    header: str  # the header to inject (e.g. "Authorization"); ignored when query_param is set
    minter: str  # host-side command that mints the value (the proxy runs it)
    replay_on_401: bool = False  # re-mint + replay once on a 401 (token rotation)
    requires: tuple[str, ...] = ()  # host env that must EXIST or the proxy daemon won't launch
    env: tuple[str, ...] = ()  # host env names the minter subprocess may READ (see the docstring)
    label: str = ""  # human label for the proxy daemon's status line
    query_param: str = ""  # inject as this URL query param instead of a header (XOR header)
    path_prefix: str = ""  # only inject on requests whose path starts with this ("" → whole host)
    value_prefix: str = ""  # string the proxy PREPENDS to the minted value (e.g. "Bearer " for an
    # OAuth `authorization` header); the proxy overwrites verbatim, so the bare token stays in
    # host.env and the prefix is added in flight. "" → inject the minted value unchanged.


@dataclass(frozen=True)
class Secret:
    """One host-side secret a posture needs present before its minter can work — the DECLARATIVE
    replacement for "the consumer ships a script that fetches it from somewhere".

    Foldyard's business is PRESENCE, not provenance: it checks whether ``var`` is in
    ``~/.foldyard/<project>/host.env`` (a doctor row) and, on a Mac TTY, prompts once for a paste
    (:func:`foldyard.keyless.ensure_secret`). ``how`` is the one-line "where do I get this?" hint
    echoed at that prompt — e.g. a ``gcloud secrets versions access …`` command, a 1Password item,
    a URL. It is **PRINTED, NEVER EXECUTED**: running a consumer-declared command string host-side
    would rebuild exactly the repo-code-runs-on-the-Mac hole that moving the minters in-package
    closed (ADR-0023). A vault-fetching ``source`` may arrive later
    as a fixed set of package-implemented kinds — never as a command.

    Contributed by a plugin for its ACTIVE rungs (``Plugin.secrets``) and/or declared by the
    consumer as ``[[secret]]`` (``config.secret_specs``); the config tier wins on ``var`` so a
    consumer can retarget the hint without touching plugin code."""

    var: str  # the host.env key the minter reads (e.g. "GH_PEM_B64")
    label: str  # human name shown at the prompt (e.g. "GitHub App private key (PEM)")
    how: str = ""  # "where do I get this?" — printed at the prompt, never run
    pattern: str = ""  # GLOB (fnmatch) the (decoded) value must match, else nothing is stored
    b64: bool = False  # value is stored base64-encoded (host.env is line-based — see below)
    # b64 exists because supervisor.load_host_env parses strict single-line KEY=VALUE, so a PEM
    # can only live there encoded. It also makes a truncated paste fail loudly: a multi-line PEM
    # pasted into a single-line prompt isn't valid base64, so it's refused rather than stored.


@dataclass(frozen=True)
class CapabilityProbe:
    """A liveness check for the EXTERNAL capability a posture rung promises (mode-state
    consolidation proposal B). A mode is a desired posture, not a capability — the PAM grant
    behind ``gcp=sa``, the operator's ADC, a keyless token can all lapse while every dashboard
    shows green. A plugin that knows its rung's capability chain contributes a probe; the
    supervisor runs due probes each tick and publishes results to ``state_dir/capabilities.json``
    + each up worktree's mirror, so ``fy mode``/``fy state`` (host and box) render the axis
    DEGRADED with the fix instead of silently 401ing. Observation only — a probe never grants,
    blocks, or writes mode state, so the security model is untouched.

    ``check`` returns ``(ok, detail)`` — detail is shown to humans, so on failure it should carry
    the fix (e.g. "PAM grant lapsed — just gcp-elevate") and must NEVER contain a credential.
    It may be slow (a gcloud call); it runs on the supervisor tick, which re-stamps its liveness
    heartbeat before each due probe — so the budget is PER PROBE, not per tick: implement your
    own timeout well under ``HEARTBEAT_STALE_SECONDS`` (30s) and rely on ``interval`` to keep it
    cheap. The supervisor also reacts to the axis's MERGED verdict flipping: a lapse posts a
    macOS notification, a heal notifies and restarts the consumer's
    ``[resnapshot_on_capability]`` services (boot-snapshotted credentials re-fetch only by
    rebooting)."""

    axis: str  # the axis whose active rung this capability backs
    name: str  # unique probe name (also the log/reap key), e.g. "gcp-impersonation"
    check: Callable[[], tuple[bool, str]]  # () -> (ok, human detail); NEVER returns a secret
    interval: float = 120.0  # seconds between runs while the axis is active


@dataclass(frozen=True)
class DoctorFix:
    """A one-click repair for a not-ok doctor check (``fail`` OR ``warn`` — e.g. the missing mitm
    CA is a warn), contributed by the plugin that owns the check (so the fix lives next to the
    check it repairs). The TUI shows a button per fix whose ``check`` is not-ok, runs ``cmd``
    (streaming into the Doctor log pane), then re-runs
    doctor. ``cmd`` MUST be NON-INTERACTIVE — it runs in a background worker with no TTY, so
    anything that prompts (``gh auth login``, ``gcloud auth login``) does NOT belong here; leave
    those to the check's detail text. ``check`` matches the doctor row's ``name`` exactly."""

    check: str  # the doctor check NAME this repairs (matches a (status, name, detail) row's name)
    label: str  # the button text (e.g. "reinstall foldyard")
    cmd: list[str]  # the non-interactive command to run


class Plugin:
    """Base class — override the hooks a plugin needs; the rest stay no-ops. A plugin
    sees the FULL mode dict and contributes only what its own axes imply."""

    name: str = ""
    # Back-reference to the owning Registry, set in Registry.__init__. Lets a plugin that
    # AGGREGATES across plugins (the proxy framework reads every plugin's proxy_rules) reach
    # the right registry — the one it's in (test-local or the global), not always the global.
    _registry: Registry | None = None

    def axes(self) -> list[Axis]:
        return []

    def daemons(self, mode: dict) -> dict[str, dict]:
        """name -> daemon spec ``{label, port, cmd, env, requires}`` the posture demands."""
        return {}

    def derive_env(self, mode: dict) -> dict[str, str]:
        """Recipe env the posture implies (merged as ``${K:-v}`` defaults; explicit env wins)."""
        return {}

    def env_defaults(self, mode: dict) -> dict[str, str]:
        """HOST-process env this posture can derive from committed, non-secret config (e.g. a
        Pulumi App id, a deterministic SA email) instead of requiring a human to hand-populate
        ``host.env``. The supervisor applies these via ``os.environ.setdefault`` each reconcile
        tick — BEFORE a daemon's ``requires`` gate and its launch env are read — so an ambient
        export or a real ``host.env`` secret always wins; this only fills gaps a plugin can
        compute for itself. Keep genuine secrets (a PEM, a token) OUT of this — only values
        safe to derive from checked-in config belong here."""
        return {}

    def compose_overlays(self, mode: dict) -> list[str]:
        """Extra compose ``-f`` overlay files this posture appends to the stack, in order. Unlike a
        scalar env var, overlays from every plugin STACK (all are appended), so ``gcp=sa`` +
        ``auth0=sim`` layer both their overrides instead of one clobbering the other. Paths may be
        absolute or checkout-relative; a non-existent file is skipped. Later entries win on
        conflicting keys (compose ``-f`` precedence), so plugin load order sets base→override."""
        return []

    def mode_issues(self, mode: dict) -> Iterable[tuple[str, str]]:
        """Coherence problems in the FULL prospective mode, as ``("error"|"warn", message)``.

        Axes are orthogonal by design, but not every combination functions. PREFER declaring
        a rung's cross-axis requirement as DATA — :attr:`Axis.requires` for an intrinsic
        coupling, the consumer's ``[[require]]`` table for a wiring-dependent one — which the
        registry evaluates uniformly (synthesized fix message, absent-axis-is-unmet semantics,
        one property-tested evaluator); this hook remains for coherence logic the data can't
        express, e.g. a combination WARNING that must stay quiet when the other axis is
        absent (auth0×storage). ``"error"`` = the combination cannot work (``fy mode``
        refuses to set it — include the fix in the message, e.g. the full ``fy mode a=x b=y``
        to run); ``"warn"`` = it functions but is probably not what you want (printed, still
        applied)."""
        return ()

    def doctor_checks(self, ctx: DoctorContext) -> Iterable[tuple[str, str, str]]:
        """Yield ``(status, name, detail)`` rows. Emit a ``("running", name, "")``
        placeholder immediately before a networked check so a live UI can spin on it."""
        return ()

    def box_doctor_checks(self, ctx: DoctorContext) -> Iterable[tuple[str, str, str]]:
        """Yield ``(status, name, detail)`` rows for `fy doctor` run INSIDE the box.

        Distinct from :meth:`doctor_checks`, which answers the Mac-side question "what can
        this machine grant?" — this answers the box-side one, "is what the mode claims
        actually reaching me?". They are not the same question, and the difference is a real
        failure mode: an injection whose host-side mint is failing leaves `fy mode` showing
        the axis on and the proxy daemon up (both true), while every request from the box
        goes out unauthenticated. Prefer an END-TO-END probe of the injected path over
        re-reading state the box was handed.
        """
        return ()

    def verify_checks(self, ctx: VerifyContext) -> Iterable[tuple[str, str]]:
        """Yield ``(status, message)`` posture assertions for this plugin's credential
        mechanism: status is ``"pass"`` | ``"fail"`` | ``"info"`` (info is a non-pass/fail
        annotation, e.g. an emergency-mode banner). A ``"fail"`` makes ``foldyard verify``
        exit non-zero. Keep these mechanism-specific — the agnostic backstops live in core."""
        return ()

    def box_args(self, env: dict) -> list[str]:
        """Extra ``<engine> run`` args (``-e``/``-v``/``--label``) this posture bakes into
        the dev box — e.g. the proxy CA mount + env, the GCE metadata host + SA label. Keys
        off the RESOLVED box env (so an explicit env var wins, like the recipe did). May
        raise ``SystemExit(msg)`` to block box-up on a misconfiguration (e.g. CA missing)."""
        return []

    def no_proxy_hosts(self) -> list[str]:
        """In-stack hostnames this plugin's own containers must reach DIRECTLY, bypassing the
        egress proxy (they end up in the box's ``NO_PROXY``). The proxy runs on the Mac and can't
        resolve a stack-network hostname, so a proxied call to one 502s — or hangs first.

        Only for services the PLUGIN owns (the gcp metadata emulator). Consumer stack services go
        in ``[proxy].no_proxy`` instead; foldyard has no business knowing their names.

        Gate on the plugin's CONFIG, not on the mode: ``NO_PROXY`` is baked into the box at create
        time, so a bypass that appeared only on an active rung would be missing from a box created
        while that rung was off — and re-baking the box on a mode change is exactly what the
        mode-independent box wiring exists to avoid. Default: none."""
        return []

    def egress_recommend(self) -> list[dict]:
        """Hosts this plugin's own box-up work needs through the egress wall, each
        ``{host, why}`` — the PACKAGED twin of ``[proxy] recommend``. Offered per host at the
        launch verbs / ``fy allow sync``, never granted automatically: a plugin may ASK, only an
        operator on the host answers (:mod:`foldyard.allowlist`). Packaged rather than scaffolded
        into
        ``foldyard.toml`` because foldyard, not the consumer, knows where its own installers
        fetch from — and a list in the repo would rot the moment either moves.

        Only for egress the plugin ITSELF causes (Claude Code's installer, the registry Codex
        is fetched from). NOT for injector hosts: the proxy exempts those structurally, since
        it has to reach them to mint (``egress_proxy._allowed``).

        Gate on the plugin's CONFIG, like :meth:`no_proxy_hosts` — uncommenting ``[claude]``
        should surface its ask at the next ``fy up``, and a project with no agent must never be
        asked about one. Default: none."""
        return []

    def stage_assets(self, mode: dict, checkout: str, here: str) -> None:
        """Copy any VM-visible assets this posture's STACK containers need into the checkout, before
        ``fy up`` runs ``compose up``. The machine mounts ONLY repo + worktrees root, so a file a
        compose service bind-mounts (e.g. the gcp metadata emulator's ``server.py``, shipped inside
        the package and off the mount) must be staged under the repo first — into the gitignored
        ``<here>/.devbox-foldyard/`` the box wheel-staging already uses. Gate on config/mode so a
        disabled feature stages nothing. Default: none."""
        return None

    def posture_services(self, mode: dict) -> dict[str, bool]:
        """Posture-critical compose services: service name → should it be RUNNING under ``mode``.
        Declare ONLY tiny, stateless, image-only containers a rung is ENFORCED by (the gcp
        metadata emulator) — never app/stack services. The stack posture reconcile converges
        exactly these when the stack is otherwise down (start the wanted, reap the dropped), so
        a mode change works in a checkout whose stack was never brought up — without this, a
        declared gcp rung in a fresh worktree read as granted while the box couldn't mint a
        single token. Gate on the plugin's config so an unconfigured consumer claims nothing.
        Default: none."""
        return {}

    def box_bootstrap(self, env: dict) -> list[dict]:
        """One-time, MONITORED install steps this plugin runs on a fresh box:
        ``[{label, check?, run}]`` — ``check`` is a shell guard (skip the step when it succeeds),
        ``run`` is the install. box._up runs each with visible ✓/✗ reporting. Gate on the plugin's
        config so a disabled feature contributes nothing (like ``box_args``). Default: none."""
        return []

    def tui_panels(self) -> list[TuiPanel]:
        """Optional TUI tabs this plugin contributes. DATA-ONLY (see :class:`TuiPanel`) —
        return ``[]`` (the default) to add none. The Network Log lives on the ``proxy`` plugin."""
        return []

    def proxy_rules(self, mode: dict) -> list[InjectRule]:
        """Egress-proxy header-injection rules this plugin's posture implies (ADR-0015).
        The built-in ``proxy`` plugin aggregates these across all plugins and runs ONE mitmdump
        from them; an injector plugin contributes rules here instead of owning a proxy daemon."""
        return []

    def doctor_fixes(self) -> Iterable[DoctorFix]:
        """One-click repairs for this plugin's failing doctor checks (see :class:`DoctorFix`).
        Return only NON-INTERACTIVE fixes; the TUI surfaces a button per fix whose check fails."""
        return ()

    def secrets(self, mode: dict) -> list[Secret]:
        """Host-side secrets THIS posture needs present in host.env (see :class:`Secret`).
        Contribute only for ACTIVE rungs (return ``[]`` at your axis's default), like
        ``proxy_rules``/``capability_probes`` — an inactive mechanism must not prompt for a
        credential nobody asked for."""
        return []

    def capability_probes(self, mode: dict) -> list[CapabilityProbe]:
        """Continuous "does the capability this posture promises actually work right now?"
        checks (see :class:`CapabilityProbe`). Contribute probes only for ACTIVE rungs (return
        ``[]`` when your axis is at its default) — an empty list clears the axis's published
        capability state. Doctor is the on-demand version of this; a probe is the same check
        run continuously by the supervisor so a lapse (an expired PAM grant, a revoked token)
        surfaces on the posture dashboards instead of as silent request-time failures."""
        return []


def _declared_requires() -> dict[str, list[Requires]]:
    """The consumer's ``[[require]]`` rows as :class:`Requires`, keyed by OWNING axis — the
    config tier of ``Axis.requires`` (see the class docstring). Reads the AMBIENT config, so
    the caller (``Registry.__init__``) binds the registry's config around it, like the axis
    snapshot. Malformed entries raise loudly (matching the Axis validation philosophy: a broken
    declaration fails in development, not as a guard that silently never fires); ``when`` /
    ``accepts`` take a scalar or a list, like an ``[[overlay]]`` entry's ``when`` values."""
    from .. import config as config_mod

    def rungs(raw: object) -> tuple[str, ...]:
        vals = raw if isinstance(raw, list) else [] if raw in (None, "") else [raw]
        return tuple(str(v) for v in vals)

    out: dict[str, list[Requires]] = {}
    for entry in config_mod.requires_declared():
        owner, needs = entry.get("axis"), entry.get("needs")
        if not (isinstance(owner, str) and owner and isinstance(needs, str) and needs):
            raise ValueError(
                f"[[require]] entry must name a string `axis` (the owning axis) and `needs` "
                f"(the required axis): {entry!r}"
            )
        out.setdefault(owner, []).append(
            Requires(
                when=rungs(entry.get("when")),
                axis=needs,
                accepts=rungs(entry.get("accepts")),
                severity=str(entry.get("severity", "error")),
                reason=str(entry.get("reason", "")),
                message=str(entry.get("message", "")),
            )
        )
    return out


class Registry:
    """The merged view of all loaded plugins. The substrate talks only to this.

    Built FROM a resolved :class:`~foldyard.config.Config` (registry plan Step B): ``config`` is
    the consumer/worktree this registry is for. Axes are snapshotted at construction UNDER that
    config's binding, so each plugin's ``axes()`` self-gating (Step D) sees the right config even
    in a long-lived process holding several registries. Live hooks (``desired_daemons``,
    ``derive_env``, ``box_args``) read the ambient config; the supervisor binds the worktree's
    config around them (per-worktree posture). ``config`` is None only for bare test registries,
    which resolve ambiently."""

    def __init__(self, plugins: list[Plugin], config: Config | None = None):
        from .. import config as config_mod

        self.config = config
        self.plugins = list(plugins)
        self._axes: dict[str, Axis] = {}
        owner: dict[str, str] = {}
        # Snapshot axes under this registry's config so each plugin's axes() self-gating resolves
        # against it (no-op binding when config is None — bare test registries stay ambient).
        ctx = config_mod.using(config) if config is not None else nullcontext()
        with ctx:
            for plugin in self.plugins:
                plugin._registry = self  # so aggregating plugins (proxy) read THIS registry's rules
                for ax in plugin.axes():
                    if ax.name in self._axes:
                        raise ValueError(
                            f"duplicate mode axis {ax.name!r} "
                            f"(plugins {owner[ax.name]!r} and {plugin.name!r})"
                        )
                    self._axes[ax.name] = ax
                    owner[ax.name] = plugin.name
            # Fold the consumer's [[require]] rows onto their owning axes (after plugin rows, so
            # intrinsic requirements evaluate first). `replace` re-runs Axis.__post_init__, so a
            # config row with a rung outside the owner's or a bad severity fails as loudly as an
            # in-code one. An owner axis nobody loaded is a config bug (a stale row after its
            # plugin table was removed, or a typo) — unlike an overlay `when`, there is no
            # legitimate reason to constrain an axis you haven't summoned, so it errors rather
            # than becoming a guard that silently never fires.
            for owning, rows in _declared_requires().items():
                ax = self._axes.get(owning)
                if ax is None:
                    raise ValueError(
                        f"[[require]] names unknown axis {owning!r} "
                        f"(loaded: {', '.join(self._axes) or 'none'}) — declare the plugin/axis "
                        "that owns it or remove the stale entry"
                    )
                self._axes[owning] = replace(ax, requires=ax.requires + tuple(rows))

    # ── axis metadata ─────────────────────────────────────────────────────────────────
    def axes(self) -> dict[str, Axis]:
        return dict(self._axes)

    def axis_rungs(self) -> dict[str, tuple[str, ...]]:
        return {name: ax.rungs for name, ax in self._axes.items()}

    def axis_defaults(self) -> dict[str, str]:
        """axis -> its zero-secret resting rung (rungs[0]) — what unset/expired reads as."""
        return {name: ax.default for name, ax in self._axes.items()}

    def blurbs(self) -> dict[tuple[str, str], str]:
        return {(n, r): t for n, ax in self._axes.items() for r, t in ax.blurb.items()}

    def axis_daemon(self) -> dict[str, str | None]:
        from .. import config as config_mod

        ctx = config_mod.using(self.config) if self.config is not None else nullcontext()
        with ctx:
            suffix = config_mod.worktree_suffix()
        return {
            name: (f"{ax.daemon}{suffix}" if ax.daemon else None) for name, ax in self._axes.items()
        }

    def emergency_rungs(self) -> dict[str, tuple[str, ...]]:
        return {name: ax.emergency for name, ax in self._axes.items()}

    # ── posture → effects ────────────────────────────────────────────────────────────
    def desired_daemons(self, mode: dict) -> dict[str, dict]:
        out: dict[str, dict] = {}
        for plugin in self.plugins:
            out.update(plugin.daemons(mode))
        return out

    def derive_env(self, mode: dict) -> dict[str, str]:
        out: dict[str, str] = {}
        for plugin in self.plugins:
            out.update(plugin.derive_env(mode))
        return out

    def env_defaults(self, mode: dict) -> dict[str, str]:
        out: dict[str, str] = {}
        for plugin in self.plugins:
            out.update(plugin.env_defaults(mode))
        return out

    def compose_overlays(self, mode: dict) -> list[str]:
        """The posture overlays for a mode: FIRST the declarative ``[[overlay]]`` table (config
        only — each entry layered when its ``when`` matches, in declaration = ``-f`` order), THEN
        any overlay a plugin still adds PROGRAMMATICALLY (so a plugin overlay stacks after and can
        ``-f``-override the table). Nearly every posture overlay is a plain "layer file X under
        mode Y" and lives in the table; the plugin hook remains for logic ``when`` can't express.
        The stack appends each existing file after the configured compose files."""
        from .. import config as config_mod

        ctx = config_mod.using(self.config) if self.config is not None else nullcontext()
        with ctx:
            out: list[str] = [str(p) for p in config_mod.matching_overlays(mode)]
        for plugin in self.plugins:
            out += plugin.compose_overlays(mode)
        return out

    def mode_issues(self, mode: dict) -> list[tuple[str, str]]:
        """Every coherence issue for a prospective mode: first the axes' declarative
        :class:`Requires` rows (axis declaration order), then every plugin's ``mode_issues``
        hook (load order). ``set_mode`` refuses on any ``"error"``; ``"warn"`` rows are
        printed and applied."""
        out: list[tuple[str, str]] = []
        for name, ax in self._axes.items():
            value = mode.get(name, ax.default)
            for req in ax.requires:
                # `mode.get(req.axis)` deliberately without a default: a required axis that
                # is absent (not loaded, or missing from a partial mode dict) satisfies
                # nothing — its requirement is unmet either way (see Requires).
                if value in req.when and mode.get(req.axis) not in req.accepts:
                    out.append((req.severity, req.render(name, value, ax.default)))
        for plugin in self.plugins:
            out += list(plugin.mode_issues(mode))
        return out

    def doctor_checks(self, ctx: DoctorContext) -> Iterator[tuple[str, str, str]]:
        for plugin in self.plugins:
            yield from plugin.doctor_checks(ctx)

    def box_doctor_checks(self, ctx: DoctorContext) -> Iterator[tuple[str, str, str]]:
        for plugin in self.plugins:
            yield from plugin.box_doctor_checks(ctx)

    def verify_checks(self, ctx: VerifyContext) -> Iterator[tuple[str, str]]:
        for plugin in self.plugins:
            yield from plugin.verify_checks(ctx)

    def box_args(self, env: dict) -> list[str]:
        out: list[str] = []
        for plugin in self.plugins:
            out += plugin.box_args(env)
        return out

    def box_bootstrap(self, env: dict) -> list[dict]:
        out: list[dict] = []
        for plugin in self.plugins:
            out += plugin.box_bootstrap(env)
        return out

    def no_proxy_hosts(self) -> list[str]:
        """Every plugin's direct-reach hostnames, de-duplicated, order preserved."""
        out: list[str] = []
        for plugin in self.plugins:
            for host in plugin.no_proxy_hosts():
                if host and host not in out:
                    out.append(host)
        return out

    def egress_recommend(self) -> list[dict]:
        """Every active plugin's recommended hosts, normalised to ``{host, why}`` and
        de-duplicated by host (first plugin to name it wins its ``why``)."""
        out: list[dict] = []
        seen: set[str] = set()
        for plugin in self.plugins:
            for entry in plugin.egress_recommend():
                host = str(entry.get("host") or "").strip()
                if host and host not in seen:
                    seen.add(host)
                    out.append({"host": host, "why": str(entry.get("why") or "").strip()})
        return out

    def stage_assets(self, mode: dict, checkout: str, here: str) -> None:
        for plugin in self.plugins:
            plugin.stage_assets(mode, checkout, here)

    def posture_services(self, mode: dict) -> dict[str, bool]:
        out: dict[str, bool] = {}
        for plugin in self.plugins:
            out.update(plugin.posture_services(mode))
        return out

    def tui_panels(self) -> list[TuiPanel]:
        out: list[TuiPanel] = []
        for plugin in self.plugins:
            out += plugin.tui_panels()
        return out

    def proxy_rules(self, mode: dict) -> list[InjectRule]:
        out: list[InjectRule] = []
        for plugin in self.plugins:
            out += plugin.proxy_rules(mode)
        return out

    def doctor_fixes(self) -> list[DoctorFix]:
        out: list[DoctorFix] = []
        for plugin in self.plugins:
            out += list(plugin.doctor_fixes())
        return out

    def secrets(self, mode: dict) -> list[Secret]:
        """Every host-side secret this mode needs: the plugins' (load order) then the consumer's
        ``[[secret]]`` rows whose ``when`` matches. Keyed by ``var``, with a config row overriding
        the FIELDS IT NAMES on a plugin's declaration and inheriting the rest — so retargeting a
        hint at your own vault is one ``how = …`` line, never a copy of the plugin's ``pattern``
        (which would then rot the day the plugin tightens it). Malformed rows raise loudly, matching
        the ``[[require]]`` philosophy: a broken declaration fails in development, not as a prompt
        that silently never fires."""
        from .. import config as config_mod

        out: list[Secret] = []
        for plugin in self.plugins:
            out += plugin.secrets(mode)
        by_var = {s.var: s for s in out}

        ctx = config_mod.using(self.config) if self.config is not None else nullcontext()
        with ctx:
            rows = config_mod.secret_specs()
            for entry in rows:
                if not config_mod.when_matches(entry.get("when"), mode):
                    continue
                var = entry.get("var")
                if not (isinstance(var, str) and _ENV_NAME.match(var)):
                    # `var` is written verbatim as a host.env KEY (`KEY=value` per line), so
                    # anything outside an env identifier is either unreadable by the supervisor's
                    # parser or, with a newline in it, a way to append extra host.env entries.
                    raise ValueError(
                        f"[[secret]] `var` must be an environment-variable name "
                        f"([A-Za-z_][A-Za-z0-9_]*): {entry!r}"
                    )
                base = by_var.get(var)
                by_var[var] = Secret(
                    var=var,
                    label=str(entry.get("label") or (base.label if base else var)),
                    how=str(entry.get("how", base.how if base else "")),
                    pattern=str(entry.get("pattern", base.pattern if base else "")),
                    b64=bool(entry.get("base64", base.b64 if base else False)),
                )
        return list(by_var.values())

    def capability_probes(self, mode: dict) -> list[CapabilityProbe]:
        """Merged probes, validated like axes at construction: a probe must target a REGISTERED
        axis (results are keyed/rendered per axis — an unknown one would publish claims nothing
        displays) and names must be unique across plugins (the name keys the supervisor's result
        cache — a collision would silently interleave two mechanisms' verdicts). Loud ValueError,
        matching the duplicate-axis error, so a broken plugin fails in development, not as a
        quietly-wrong dashboard."""
        out: list[CapabilityProbe] = []
        seen: dict[str, str] = {}
        for plugin in self.plugins:
            for probe in plugin.capability_probes(mode):
                if probe.axis not in self._axes:
                    raise ValueError(
                        f"plugin {plugin.name!r}: capability probe {probe.name!r} targets "
                        f"unknown axis {probe.axis!r} (have: {', '.join(self._axes)})"
                    )
                if probe.name in seen:
                    raise ValueError(
                        f"duplicate capability probe name {probe.name!r} "
                        f"(plugins {seen[probe.name]!r} and {plugin.name!r})"
                    )
                seen[probe.name] = plugin.name
                out.append(probe)
        return out


def _entry_point_plugins() -> list[Plugin]:
    """Third-party plugins registered on the ``foldyard.plugins`` group. A broken one
    must never take down the hot path, so each load is isolated."""
    try:
        eps = list(entry_points(group=ENTRY_POINT_GROUP))
    except Exception:  # pragma: no cover — defensive (e.g. corrupt dist metadata)
        return []
    found: list[Plugin] = []
    for ep in eps:
        try:
            found.append(ep.load()())
        except Exception:  # pragma: no cover — ignore a broken third-party plugin
            continue
    return found


def load_plugins(
    config: Config | None = None,
    extra: list[Plugin] | None = None,
    discover: bool = True,
) -> list[Plugin]:
    """The built-ins as a function of the resolved ``config`` (registry plan Step C): a SMALL CORE
    that's always loaded (batteries — inert until its own config is declared) plus a DECLARED set
    loaded only when the consumer opts in by declaring its config namespace. Then entry-point
    plugins, then any ``extra`` (tests inject here).

    The CORE is the project-agnostic spine + the batteries-included agent/editor/injector surface
    (github, inject, proxy, claude, vscode, codex) — each already contributes nothing until its own
    config is declared, so a plain ``foldyard init`` repo gets them inert. The DECLARED plugins
    (gcp, auth0-sim, llm) carry axes/wiring that are MEANINGLESS without their config (a ``gcp``
    axis centred on a real GCP project, an ``auth0`` axis backed by a simulator harness, an ``llm``
    axis backed by consumer LLM-mode overlays), so they load ONLY when their ``[plugins.*]`` table
    is present — the actual "small core + plugins" boundary the spinout review asks for.

    Order is preserved from before the split (gcp, github, inject, proxy, auth0-sim, llm, claude,
    vscode, codex when all are present) because it sets axis + doctor-row order (``fy mode``
    output) AND overlay stacking order (identity/storage → auth0 → llm)."""
    from .. import config as config_mod
    from . import auth0_sim, claude, codex, fakecred, gcp, github, inject, llm, proxy, vscode

    cfg = config if config is not None else config_mod.current()

    plugins: list[Plugin] = []
    # gcp (DECLARED, gated on [plugins.gcp-metadata]) sorts first when present, and loads before
    # auth0-sim/llm so its identity/storage overlays are the -f BASE the later ones override.
    if cfg.gcp_metadata_declared():
        plugins.append(gcp.GcpPlugin())
    plugins.append(github.GithubPlugin())  # CORE: axis (off/app/user) only when [plugins.github]
    plugins.append(inject.InjectPlugin())  # CORE: contributes axes only per [[inject]] config
    plugins.append(proxy.ProxyPlugin())  # CORE: capture axis self-gates on [proxy] (Step D)
    if cfg.auth0_sim_declared():  # DECLARED: gated on [plugins.auth0-sim]
        plugins.append(auth0_sim.Auth0SimPlugin())
    if cfg.llm_declared():  # DECLARED: gated on [plugins.llm] (Tangible-bound, spinout D4-style)
        plugins.append(llm.LlmPlugin())
    if cfg.fakecred_declared():  # DECLARED: the zero-secret mode-machinery TESTING rig
        plugins.append(fakecred.FakecredPlugin())
    plugins.append(claude.ClaudePlugin())  # CORE: axis only when [claude].keyless
    plugins.append(vscode.VscodePlugin())  # CORE: no axis, [vscode]-gated box volume only
    plugins.append(codex.CodexPlugin())  # CORE: axis only when [codex].keyless

    if discover:
        plugins += _entry_point_plugins()
    if extra:
        plugins += extra
    return plugins


# Registry cache keyed on the resolved-config CONTENT (registry plan Step B): a consumer/worktree
# with a different config gets a different registry; the same content reuses it, even across the
# `config.clear_caches()` tests + worktree switches trigger (re-parsing identical toml is a cache
# hit). Content- not id-keyed so a freed toml dict's reused address can't return a stale registry.
# Bounded by the handful of distinct configs a single process ever resolves.
_REGISTRY_CACHE: dict[str, Registry] = {}


def _config_key(cfg: Config) -> str:
    import json

    return json.dumps([str(cfg.repo_root), cfg.worktree, cfg.toml], sort_keys=True, default=str)


def registry(config: Config | None = None) -> Registry:
    """The merged registry for a resolved ``config`` (default: the active/ambient one). A pure
    function of that config — its plugin set + axes derive from it (registry plan Steps B–D) — so
    a long-lived ``fy host``/TUI can hold one registry per worktree with no import-time global.
    Cached per config content; the registry carries ``config`` so plugins can self-gate on it."""
    from .. import config as config_mod

    cfg = config if config is not None else config_mod.current()
    key = _config_key(cfg)
    cached = _REGISTRY_CACHE.get(key)
    if cached is None:
        cached = Registry(load_plugins(cfg), config=cfg)
        _REGISTRY_CACHE[key] = cached
    return cached


def _clear_registry_cache() -> None:
    """Drop the per-config registry cache (tests, after mutating the resolved config)."""
    _REGISTRY_CACHE.clear()
