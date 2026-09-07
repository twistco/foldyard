"""inject plugin — the generic, CONFIG-DRIVEN egress injector (header-auth, generalized to a
header OR a URL query-param).

Each ``[[inject]]`` entry in ``foldyard.toml`` becomes one on/off mode axis + one egress-proxy
:class:`~foldyard.plugins.InjectRule`, so a consumer adds a host-side credential injector with
CONFIG ONLY — no Python. This covers the plain "rewrite ONE thing on ONE host with a host-side
token" case. ``github`` deliberately stays its OWN plugin: it carries non-generic, security-
load-bearing posture (the off/app/user ladder with a TTL'd emergency rung, the dummy ``GH_TOKEN``
baked into the box, its mode-aware verify + doctor checks) that shouldn't be flattened into config.

Config (``foldyard.toml``)::

    [[inject]]
    axis        = "penpot"                              # → `fy mode penpot=on` (off by default)
    host        = "penpot.example.com"                  # the host to inject on
    query_param = "userToken"                           # XOR  header = "Authorization"
    label       = "Penpot MCP injection proxy"          # optional; shown in daemon status
    replay_on_401 = false                               # optional; re-mint + replay once on a 401
    path_prefix = "/mcp"                                # optional; only inject on these paths
    ttl         = 43200                                 # optional; static-token re-read cadence (s)

Token handling: the secret lives ONLY in ``host.env`` on the Mac, under a var foldyard DERIVES from
the axis — ``FY_INJECT_<AXIS>`` (see :func:`token_var`) — and the shipped
:mod:`~foldyard.plugins.static_token` minter echoes it as ``{"value","ttl"}`` host-side. It never
enters the box or the repo (only the variable NAME reaches the daemon command).

The var is DERIVED, not declared, and that's the point: ``[[inject]]`` is repo config, so a
consumer-named ``token_env`` let anything that can write the checkout point a rule at any other
mechanism's host.env secret (``ANTHROPIC_API_KEY``, ``GH_PEM_B64``…) and at a ``host`` of its
choosing — the proxy would then hand that secret to that host, in flight, with the box none the
wiser. Under the derived name a config rule can only read the one var the operator created FOR that
injector; every other credential on the Mac is out of its reach. (Packaged plugins — claude, codex —
build their spec in code via ``keyless.inject_spec`` and legitimately name their own var; the
constraint is on the config tier, which is the untrusted one.)

A token source is a NAME, never a command: config cannot introduce host-side code. A command
string here would be executed by the supervisor's mint path, so anything able to write the
checkout could choose what the host runs (ADR-0023). A mechanism
needing more than a static token is a packaged minter KIND (``github_app_token``,
``gh_cli_token``, ``codex_chatgpt_token``) or an entry-point plugin — both installed, host-side
acts. Many
injectors coexist: the proxy serializes every active rule into the addon's ``INJECT_RULES``
set, so any number of ``[[inject]]`` axes (and ``github``/``claude``) can be "on" at once, each with
its own host + minter.

Stdlib only — this loads on the registry hot path.
"""

from __future__ import annotations

import re
import shlex
import sys

from .. import config
from . import Axis, InjectRule, Plugin


class InjectPlugin(Plugin):
    name = "inject"

    def _specs(self) -> list[dict]:
        """The consumer's ``[[inject]]`` rows, with the one cross-row invariant checked: no two
        axes may share a derived token var.

        Distinct axis names can normalize to the SAME ``FY_INJECT_<AXIS>`` (``pen-pot`` /
        ``pen_pot``; ``penpot`` / ``PenPot``), and a shared var is the derivation's own version of
        the hole it closed: a second row could point the first axis's token at a host of its
        choosing, needing only that the operator turn the newcomer on. Raises loudly, matching the
        ``[[require]]`` philosophy — a broken declaration fails in development, not as a guard that
        silently never fires."""
        specs = config.inject_specs()
        owner: dict[str, str] = {}
        for spec in specs:
            axis = spec.get("axis")
            if not axis:
                continue
            first = owner.setdefault(token_var(str(axis)), str(axis))
            if first != str(axis):
                raise ValueError(
                    f"[[inject]] axes {first!r} and {axis!r} both derive the token var "
                    f"{token_var(str(axis))} — rename one so each injector reads its own secret"
                )
        return specs

    def axes(self) -> list[Axis]:
        axes: list[Axis] = []
        for spec in self._specs():
            axis = spec.get("axis")
            if not axis:
                continue
            on_blurb = spec.get("label") or f"inject on {spec.get('host', '?')}"
            axes.append(
                Axis(
                    name=str(axis),
                    rungs=("off", "on"),
                    blurb={"off": "no injection", "on": str(on_blurb)},
                    daemon="egress-proxy",
                )
            )
        return axes

    def proxy_rules(self, mode: dict) -> list[InjectRule]:
        rules: list[InjectRule] = []
        for spec in self._specs():
            axis = spec.get("axis")
            if not axis or mode.get(str(axis), "off") == "off":
                continue
            # The token var is DERIVED from the axis, never read from the row — see the module
            # docstring: a config-named one reaches every other mechanism's secret.
            rule = _spec_to_rule({**spec, "token_env": token_var(str(axis))})
            if rule is not None:
                rules.append(rule)
        return rules


def token_var(axis: str) -> str:
    """The host.env var an ``[[inject]]`` axis's static token lives in: ``FY_INJECT_<AXIS>``,
    uppercased with anything outside ``[A-Za-z0-9]`` folded to ``_`` (host.env keys are env
    identifiers). The mapping is many-to-one, so :meth:`InjectPlugin._specs` rejects a config where
    two axes land on the same var."""
    return "FY_INJECT_" + re.sub(r"[^A-Za-z0-9]", "_", axis).upper()


def _spec_to_rule(spec: dict) -> InjectRule | None:
    """Turn one ``[[inject]]`` entry into an :class:`InjectRule`, or ``None`` if it's unusable
    (no host, or no ``token_env``). The token source is always the shipped :mod:`static_token`
    minter fed the VAR NAME — there is no consumer-command path (see the module docstring)."""
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
