"""inject plugin — credentials as CONFIG: each ``[[inject]]`` row is one switch + one egress rule.

A credential is an ``[[inject]]`` rule with a ``kind`` (ADR-0031): the kind is a token PROTOCOL
shipped as package code (:mod:`~foldyard.plugins.kinds` — ``static``, ``github-app``, ``gh-cli``),
and the row is its data. Each row becomes one on/off mode switch + one egress-proxy
:class:`~foldyard.plugins.InjectRule`, so a consumer adds a host-side credential injector with
CONFIG ONLY — no Python, and no per-service plugin (GitHub had one; it was a kind plus data
wearing a class, and its permission ceiling was a second place that had to agree with the App).

Config (``foldyard.toml``)::

    [[inject]]
    switch      = "penpot"                              # → `fy mode penpot=on` (off by default)
    host        = "penpot.example.com"                  # the host to inject on
    query_param = "userToken"                           # XOR  header = "Authorization"
    label       = "Penpot MCP injection proxy"          # optional; shown in daemon status
    replay_on_401 = false                               # optional; re-mint + replay once on a 401
    path_prefix = "/mcp"                                # optional; only inject on these paths
    ttl         = 43200                                 # optional; static-token re-read cadence (s)
    emergency   = true                                  # optional; `on` expires (see below)
    box_env     = { PENPOT_TOKEN = "dummy" }            # optional; dummies baked into the box

    [[inject]]
    switch          = "github"                          # PEM in host.env as FY_INJECT_GITHUB
    kind            = "github-app"                      # host is pinned to api.github.com
    app_id          = "4008762"
    installation_id = "139125083"
    repositories    = ["Tangible"]                      # optional: narrow to these repos
    box_env         = { GH_TOKEN = "x" }

Each kind takes a FIXED set of fields (the generic ones above plus its own); a field it doesn't
take, or an unknown kind, is refused at load naming what IS allowed — a typo must fail where it's
written, not become a control that silently does nothing.

``emergency = true`` makes ``on`` an emergency level: it carries a TTL (``fy mode <switch>=on
ttl=30m``, else the default) and the supervisor switches it off when that lapses — for a token
whose access should never be left on by accident. ``gh-cli`` (the operator's own token) requires it.

``box_env`` is the box half of the keyless pattern (ADR-0008): the DUMMY a client needs before it
will send the header the proxy overwrites in flight. It is AMBIENT with the proxy substrate, not
keyed to the level — box env is create-time and the switch is host-side, so keying them together
would make turning a switch on need a box recreate. A dummy grants nothing: whether a request gains
a real credential is decided solely by the host proxy's rule. ``fy verify`` asserts each one still
holds its dummy (a real value there is the leak to catch), and a dummy may never overwrite a name
foldyard itself bakes into the box (the proxy, the engine socket, the CA bundles…).

Token handling: the secret lives ONLY in ``host.env`` on the host, under a var foldyard DERIVES from
the switch — ``FY_INJECT_<SWITCH>`` (see :func:`token_var`) — and the kind's minter reads it there
by NAME, host-side. It never enters the box or the repo (only the variable NAME reaches the daemon
command). The var is DERIVED, not declared, and that's the point: ``[[inject]]`` is repo config, so
a consumer-named ``token_env`` let anything that can write the checkout point a rule at any other
mechanism's host.env secret (``ANTHROPIC_API_KEY``, another App's PEM…) and at a ``host`` of its
choosing — the proxy would then hand that secret to that host, in flight, with the box none the
wiser. Under the derived name a config rule can only read the one var the operator created FOR that
injector. (Packaged plugins — claude, codex — build their spec in code via ``keyless.inject_spec``
and legitimately name their own var; the constraint is on the config tier, the untrusted one.)

A token source is a NAME, never a command: config cannot introduce host-side code. A command string
here would be executed by the supervisor's mint path, so anything able to write the checkout could
choose what the host runs (ADR-0023). Many injectors coexist: the proxy serializes every active rule
into the rule set of the addon's live file, so any number of switches (and ``claude``/``codex``) can
be "on" at once, each with its own host + minter.

Stdlib only — this loads on the registry hot path.
"""

from __future__ import annotations

import os
import re
import shlex
import sys
from collections.abc import Iterator

from .. import config
from . import (
    CapabilityProbe,
    DoctorContext,
    HeldCredential,
    InjectRule,
    Plugin,
    Secret,
    Switch,
    VerifyContext,
)
from .kinds import DEFAULT, KINDS, RETIRED_FIELDS, Kind

_ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

# What foldyard itself bakes into the box (box._up, the proxy/gcp/agent plugins' box_args). A
# `box_env` dummy may not overwrite one: rerouting the proxy, the engine socket or the CA bundle
# from a credential row would be a box-wiring knob hiding in a field meant for "x". Compared
# upper-cased, so `https_proxy` is as refused as `HTTPS_PROXY`.
_FOLDYARD_BOX_ENV = frozenset(
    {
        # the engine + the box's own identity
        "CONTAINER_HOST",
        "DOCKER_HOST",
        "DOCKER_CONFIG",
        "IN_DEVBOX",
        "IS_SANDBOX",
        "WORKTREE",
        "PODMAN_PROJECT",
        "COMPOSE_PROJECT_NAME",
        # proxy routing + the CA trust that makes it work
        "HTTPS_PROXY",
        "HTTP_PROXY",
        "ALL_PROXY",
        "NO_PROXY",
        "REQUESTS_CA_BUNDLE",
        "GIT_SSL_CAINFO",
        "SSL_CERT_FILE",
        "CLOUDSDK_CORE_CUSTOM_CA_CERTS_FILE",
        "NODE_EXTRA_CA_CERTS",
        # the gcp metadata emulator
        "GCE_METADATA_HOST",
        "GCE_METADATA_IP",
        "GCE_METADATA_ROOT",
        "GCP_MINTER_PORT",
        # the agents' homes, and the keyless dummies foldyard bakes for them
        "CLAUDE_CONFIG_DIR",
        "CODEX_HOME",
        "ANTHROPIC_API_KEY",
        "CLAUDE_CODE_OAUTH_TOKEN",
        "OPENAI_API_KEY",
        # the shell basics
        "PATH",
        "HOME",
        "SHELL",
        "USER",
    }
)
_FOLDYARD_BOX_PREFIXES = ("FY_", "FOLDYARD_")


def _foldyard_owned(name: str) -> bool:
    upper = name.upper()
    return upper in _FOLDYARD_BOX_ENV or upper.startswith(_FOLDYARD_BOX_PREFIXES)


def _kind(spec: dict) -> Kind:
    return KINDS[str(spec.get("kind", DEFAULT))]


class InjectPlugin(Plugin):
    name = "inject"

    def _specs(self) -> list[dict]:
        """The consumer's ``[[inject]]`` rows, validated and normalised (a pinned host filled in).

        Per row: the kind exists, the row carries only fields that kind takes (the retired
        ``minter``/``token_env`` excepted — ignored and reported, see ``exposure.IGNORED_KEYS``),
        the kind's required fields and value checks, a pinned host is the only host, and
        ``box_env`` is plain dummies. Across rows: no two switches derive the same token var, and
        no two rows disagree about a dummy.

        Distinct switch names can normalize to the SAME ``FY_INJECT_<SWITCH>`` (``pen-pot`` /
        ``pen_pot``; ``penpot`` / ``PenPot``), and a shared var is the derivation's own version of
        the hole it closed: a second row could point the first switch's token at a host of its
        choosing, needing only that the operator turn the newcomer on. Raises loudly, matching the
        ``[[require]]`` philosophy — a broken declaration fails in development, not as a guard that
        silently never fires."""
        specs = config.inject_specs()
        owner: dict[str, str] = {}
        dummies: dict[str, tuple[str, str]] = {}
        out: list[dict] = []
        for spec in specs:
            switch = spec.get("switch")
            if not switch:
                continue
            spec = _validated(spec, f"[[inject]] {switch!r}")
            first = owner.setdefault(token_var(str(switch)), str(switch))
            if first != str(switch):
                raise ValueError(
                    f"[[inject]] switches {first!r} and {switch!r} both derive the token var "
                    f"{token_var(str(switch))} — rename one so each injector reads its own secret"
                )
            for name, value in spec.get("box_env", {}).items():
                seen = dummies.setdefault(name, (value, str(switch)))
                if seen[0] != value:
                    raise ValueError(
                        f"[[inject]] {seen[1]!r} and {switch!r} disagree about the box's {name} "
                        f"({seen[0]!r} vs {value!r}) — the box holds one value; give both the same "
                        "dummy"
                    )
            out.append(spec)
        return out

    def switches(self) -> list[Switch]:
        axes: list[Switch] = []
        for spec in self._specs():
            on_blurb = (
                spec.get("label")
                or _kind(spec).default_label
                or f"inject on {spec.get('host', '?')}"
            )
            axes.append(
                Switch(
                    name=str(spec["switch"]),
                    levels=("off", "on"),
                    blurb={"off": "no injection", "on": str(on_blurb)},
                    daemon="egress-proxy",
                    # an emergency switch: `on` carries a TTL and the supervisor reverts it
                    emergency=("on",) if spec.get("emergency") else (),
                )
            )
        return axes

    def _active(self, mode: dict) -> Iterator[dict]:
        for spec in self._specs():
            if mode.get(str(spec["switch"]), "off") != "off":
                yield spec

    def proxy_rules(self, mode: dict) -> list[InjectRule]:
        rules: list[InjectRule] = []
        for spec in self._active(mode):
            # The token var is DERIVED from the switch, never read from the row — see the module
            # docstring: a config-named one reaches every other mechanism's secret.
            rule = _kind(spec).rule(spec, token_var(str(spec["switch"])))
            if rule is not None:
                rules.append(rule)
        return rules

    def held_credentials(self, mode: dict) -> list[HeldCredential]:
        # A resting row's dummies are still in the box (box_env is ambient), so its client still
        # sends them. Forwarded, they'd be refused upstream in words that name neither the switch
        # nor the fix; the proxy answers them instead (ADR-0031 decision 5), wherever the row's rule
        # would inject. Everyday switches first: where two resting rows share a host and a dummy
        # the first match answers, and it should name the switch to turn on, not the emergency one.
        resting = [
            s for s in self._specs() if s["box_env"] and mode.get(str(s["switch"]), "off") == "off"
        ]
        out: list[HeldCredential] = []
        for spec in sorted(resting, key=lambda s: bool(s.get("emergency"))):
            switch = str(spec["switch"])
            rule = _kind(spec).rule(spec, token_var(switch))
            if rule is None:
                continue
            body = _kind(spec).held_body(_at_rest_message(switch, bool(spec.get("emergency"))))
            for dummy in dict.fromkeys(spec["box_env"].values()):
                out.append(
                    HeldCredential(
                        host=rule.host,
                        header="" if rule.query_param else rule.header,
                        dummy=dummy,
                        axis=switch,
                        body=body,
                        path_prefix=rule.path_prefix,
                        query_param=rule.query_param,
                        any_scheme=True,  # the box's client, not the row, decides the scheme
                    )
                )
        return out

    def secrets(self, mode: dict) -> list[Secret]:
        # The var is derived, so nothing else would ever ask for it: declared here, a switch going
        # on prompts for its secret (a `[[secret]]` row naming the var can still retarget the hint).
        out: list[Secret] = []
        for spec in self._active(mode):
            secret = _kind(spec).secret(spec, token_var(str(spec["switch"])))
            if secret is not None:
                out.append(secret)
        return out

    def capability_probes(self, mode: dict) -> list[CapabilityProbe]:
        out: list[CapabilityProbe] = []
        for spec in self._active(mode):
            out += _kind(spec).probes(spec, token_var(str(spec["switch"])))
        return out

    def doctor_checks(self, ctx: DoctorContext) -> Iterator[tuple[str, str, str]]:
        # Every DECLARED row, armed or not — the operator should see a missing key BEFORE turning
        # its switch on. The kind decides how loud a resting switch's finding is.
        for spec in self._specs():
            switch = str(spec["switch"])
            active = ctx.mode.get(switch, "off") != "off"
            yield from _kind(spec).doctor_checks(spec, token_var(switch), active, ctx)

    def box_doctor_checks(self, ctx: DoctorContext) -> Iterator[tuple[str, str, str]]:
        # In the box `ctx.mode` is the mirror the host keeps for this worktree: only an armed switch
        # has a wire worth probing.
        for spec in self._active(ctx.mode):
            yield from _kind(spec).box_doctor_checks(spec, ctx)

    def box_args(self, env: dict) -> list[str]:
        # The dummies, AMBIENT with the proxy substrate (FY_PROXY — always set under Phase A′
        # always-route), NOT gated on the level. Mirrors the proxy plugin's ambient CA trust:
        # pre-position the inert box-side plumbing so a switch flips LIVE, host-side only (`fy mode
        # github=on` → the supervisor starts injecting), with no box recreate — exactly how the
        # gcp switch already works (metadata env always baked, the host-side minter is the gate).
        # Without proxy routing nothing could ever replace a dummy, so none is baked.
        if not env.get("FY_PROXY"):
            return []
        baked: dict[str, str] = {}
        for spec in self._specs():
            for name, value in spec.get("box_env", {}).items():
                baked.setdefault(name, value)  # `_specs` already refused a disagreement
        return [arg for name, value in baked.items() for arg in ("-e", f"{name}={value}")]

    def verify_checks(self, ctx: VerifyContext) -> Iterator[tuple[str, str]]:
        # In-box only: what the box's env holds where a credential would go. Each declared dummy
        # must still BE the dummy, and each var a known kind's client reads a credential from must
        # hold nothing else — a real-looking value is the leak. Those are watched in EVERY box,
        # declared or not: the github plugin's row did that (a real GH_TOKEN failed verify whatever
        # the config said), and removing the plugin must not make verify claim less. Unset is fine
        # (a box created without proxy routing gets no dummy, and an absent token grants nothing).
        # Ambient, like the dummies: the level doesn't change what the box may hold.
        if not ctx.in_box:
            return
        # var → (the dummy it may hold, the switch that declared it)
        watched = {name: ("", "") for kind in KINDS.values() for name in kind.credential_env}
        for spec in self._specs():
            for name, value in spec.get("box_env", {}).items():
                watched[name] = (value, str(spec["switch"]))
        for name, (dummy, switch) in watched.items():
            value = os.environ.get(name, "")
            where = f"{switch}: {name}" if switch else name
            if not value:
                yield ("pass", f"{where} unset (no credential in the box)")
            elif dummy and value == dummy:
                yield (
                    "pass",
                    f"{where} is the dummy {dummy!r} (the real credential stays on your computer)",
                )
            else:
                expected = f"its dummy {dummy!r}" if dummy else "unset"
                yield ("fail", f"{where} isn't {expected} — possible real credential in the box")


def _validated(spec: dict, where: str) -> dict:
    """One row checked against its kind's shape, with a pinned host filled in (see ``_specs``)."""
    name = spec.get("kind", DEFAULT)
    kind = KINDS.get(name) if isinstance(name, str) else None
    if kind is None:
        raise ValueError(f"{where}: unknown kind {name!r} (have: {', '.join(KINDS)})")
    where = f"{where} (kind {kind.name!r})"
    allowed = kind.allowed()
    unknown = [k for k in spec if k not in allowed and k not in RETIRED_FIELDS]
    if unknown:
        raise ValueError(
            f"{where}: unknown field(s) {', '.join(repr(k) for k in unknown)} — this kind "
            f"takes: {', '.join(allowed)}"
        )
    for key in kind.required:
        if spec.get(key) in (None, ""):
            raise ValueError(f"{where}: needs `{key}`")
    for key in ("emergency", "replay_on_401"):
        if not isinstance(spec.get(key, False), bool):
            # a quoted "false" is truthy: the switch would expire when the operator said not to
            raise ValueError(f"{where}: {key} must be true or false, got {spec[key]!r}")
    if kind.emergency_only and spec.get("emergency") is not True:
        raise ValueError(
            f"{where}: needs `emergency = true` — it injects a credential that must never be left "
            "on by accident, so its `on` has to expire"
        )
    if kind.host:
        if spec.get("host", kind.host) != kind.host:
            raise ValueError(
                f"{where}: `host` is always {kind.host} for this kind, got {spec['host']!r} — a "
                "minted token is never aimed at another host by config"
            )
        spec = {**spec, "host": kind.host}
    spec = {**spec, "box_env": _box_env(spec.get("box_env", {}), where)}
    kind.check(spec, where)
    return spec


def _box_env(raw: object, where: str) -> dict[str, str]:
    if not isinstance(raw, dict):
        raise ValueError(f'{where}: `box_env` must be a table of NAME = "dummy", got {raw!r}')
    for name, value in raw.items():
        if not _ENV_NAME.match(str(name)):
            raise ValueError(
                f"{where}: box_env {name!r} isn't an environment-variable name "
                "([A-Za-z_][A-Za-z0-9_]*)"
            )
        if _foldyard_owned(str(name)):
            raise ValueError(
                f"{where}: box_env {name!r} is one foldyard sets in the box itself — a dummy may "
                "not overwrite it"
            )
        if not isinstance(value, str):
            raise ValueError(f"{where}: box_env {name} must be a string, got {value!r}")
    return {str(name): str(value) for name, value in raw.items()}


def _at_rest_message(switch: str, emergency: bool) -> str:
    """What the proxy tells the box's client when it sends a resting row's dummy. The client prints
    it as the API's error, so it carries the fix — and where to run it, since the box can't."""
    lapsed = " (or its time limit ran out)" if emergency else ""
    return (
        f"foldyard: `{switch}` is off{lapsed}, so this box only holds a placeholder for this "
        "credential and the proxy did not send the request. Switch it on from your computer, not "
        f"in the box: `fy mode {switch}=on` (or `fy tui`)."
    )


def token_var(axis: str) -> str:
    """The host.env var an ``[[inject]]`` switch's secret lives in: ``FY_INJECT_<SWITCH>``,
    uppercased with anything outside ``[A-Za-z0-9]`` folded to ``_`` (host.env keys are env
    identifiers). The mapping is many-to-one, so :meth:`InjectPlugin._specs` rejects a config where
    two switches land on the same var."""
    return "FY_INJECT_" + re.sub(r"[^A-Za-z0-9]", "_", axis).upper()


def _spec_to_rule(spec: dict) -> InjectRule | None:
    """Turn one static-token spec into an :class:`InjectRule`, or ``None`` if it's unusable (no
    host, or no ``token_env``). The token source is always the shipped :mod:`static_token` minter
    fed the VAR NAME — there is no consumer-command path (see the module docstring). Shared by the
    ``static`` kind and the keyless agents (``keyless.inject_spec``)."""
    host = spec.get("host")
    if not host:
        return None
    query_param = str(spec.get("query_param", "") or "")
    header = str(spec.get("header", "") or "")
    if not query_param and not header:
        header = "Authorization"  # sensible default for the header case

    token_env = spec.get("token_env")
    if not token_env:
        return None  # no token source declared — nothing to inject
    # Build the static-token minter command from foldyard's OWN interpreter (the supervisor
    # runs the daemon under it), passing only the env var NAME — never the secret.
    cmd = [sys.executable, "-m", "foldyard.plugins.static_token", str(token_env)]
    ttl = spec.get("ttl")
    if ttl:
        cmd.append(str(int(ttl)))
    minter = shlex.join(cmd)

    return InjectRule(
        host=str(host),
        header=header,
        minter=str(minter),
        replay_on_401=bool(spec.get("replay_on_401", False)),
        # NOT `requires`: that gates the WHOLE proxy daemon's spawn, and under always-route a proxy
        # that refuses to launch connection-refuses every box request — so one injector's missing
        # token would take out all egress. A missing token degrades THIS host (the addon logs the
        # mint failure); `env` is just what the minter may read.
        env=(str(token_env),),
        label=str(spec.get("label", "") or f"inject proxy ({host})"),
        query_param=query_param,
        path_prefix=str(spec.get("path_prefix", "") or ""),
        value_prefix=str(spec.get("value_prefix", "") or ""),
    )
