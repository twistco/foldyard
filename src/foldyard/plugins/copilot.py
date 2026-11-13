"""The ``copilot`` plugin — GitHub Copilot CLI in the dev box, gated on a ``[copilot]`` table.

Mirrors the ``codex`` plugin: when ``[copilot]`` is declared, install the Copilot CLI on box-up (a
monitored ``box_bootstrap`` step) and mount its persisted ``~/.copilot`` — login, settings,
``session-state`` — so the session survives box recreation / ``fy nuke``. The launcher is
``fy copilot`` (in ``box.py``).

A BARE ``[copilot]`` (no ``keyless``) is the MANUAL-LOGIN box: no dummy token, no mode axis, and the
Copilot API + auth hosts join ``egress_recommend`` (no injector exists to exempt them). You run
``fy copilot``, log in once, and the token persists in the mounted volume across recreation. The
browser flow's loopback callback (``127.0.0.1``) is a loopback INSIDE the box, which your computer's
browser can't reach, so a headless box wants the device-code flow the CLI falls back to, or
``copilot login --with-token`` for a PAT.

KEYLESS auth (``[copilot].keyless = "api-key"``) — no real token in the box; the egress proxy
injects it in flight (composes in the shared proxy's multi-injector rule set with claude / codex /
``[[inject]]``). The box bakes a dummy ``COPILOT_GITHUB_TOKEN``; the proxy rewrites the
``Authorization`` header with ``Bearer `` + the real token from ``host.env`` on every host in
:data:`~foldyard.keyless.COPILOT_KEYLESS_HOSTS`. That list is not one host because Copilot talks to
a PLAN-SPECIFIC API host (individual/business/enterprise) and ALWAYS validates the token against
``api.github.com`` before its first call — so the rule set covers each host that can carry it. A
rule fires only on a request carrying the dummy (``any_scheme``: ``Bearer``/``token``/bare), so
requests already carrying the real, exchanged Copilot token are untouched.

Facts verified against the installed CLI (1.0.109): state lives in ``$COPILOT_HOME`` (default
``~/.copilot``: ``settings.json``, the CLI-managed ``config.json``, ``permissions-config.json``,
``session-state/<uuid>/``, ``session-store.db``); ``--session-id <uuid>`` names a new session and
``--resume <id>`` resumes one; ``--allow-all`` = tools + paths + urls; ``COPILOT_ALLOW_ALL=true``
also trusts the working directory; there is no ``--append-system-prompt`` and no ad-hoc config
override flag — orientation arrives as ``$COPILOT_HOME/instructions/foldyard.instructions.md``, one
of the personal instruction sources the CLI loads (``box._seed_copilot_instructions``). The CLI
self-updates in the background; ``--no-auto-update`` / ``COPILOT_AUTO_UPDATE=false`` turn it off.
Stdlib only.
"""

from __future__ import annotations

import json
from dataclasses import replace

from .. import boxmounts, config, keyless
from ..keyless import COPILOT_KEYLESS as _KEYLESS  # the shared keyless taxonomy (stdlib-only)
from ..keyless import COPILOT_KEYLESS_HOSTS as _KEYLESS_HOSTS
from . import HeldCredential, InjectRule, Plugin, Secret, Switch
from .inject import _spec_to_rule  # reuse the static-token minter wiring (same package, no cycle)

# The upstream installer: a static release tarball fetched from github.com (redirecting to
# release-assets.githubusercontent.com) and verified against its SHA256SUMS, which is why both hosts
# are in `egress_recommend` below. PREFIX puts the binary at $HOME/.local/bin/copilot — a dir the
# box already has FIRST on PATH (the same one the Claude/Codex steps install into). PATH is set for
# the installer's own "is the install dir on PATH?" probe: with it present the script skips its
# "add this to your shell rc?" prompt, which otherwise reads /dev/tty — a bootstrap step must never
# block. `cd /tmp` keeps a download from landing in the repo if curl is interrupted.
# Run it under `bash`, not `sh`: the script is `#!/usr/bin/env bash` and uses bash arrays
# (`CURL_AUTH=()`), which the box's dash rejects with `Syntax error: "(" unexpected`.
_INSTALL = (
    "(cd /tmp && curl -fsSL --retry 3 "
    "https://raw.githubusercontent.com/github/copilot-cli/refs/heads/main/install.sh "
    '| PREFIX="$HOME/.local" PATH="$HOME/.local/bin:$PATH" bash)'
)


class CopilotPlugin(Plugin):
    name = "copilot"

    def switches(self) -> list[Switch]:
        # Only when keyless is configured: an on/off injector axis, like claude's/codex's. Off (the
        # zero-secret default) → the box holds only a dummy token; on → the proxy injects the real
        # one host-side. Maps to the shared "egress-proxy" daemon (one proxy, many injectors).
        if not config.copilot_keyless():
            return []
        return [
            Switch(
                name="copilot",
                levels=("off", "on"),
                blurb={
                    "off": "no Copilot credential reaches GitHub (box holds only a dummy)",
                    "on": "keyless Copilot: proxy injects your token host-side (none in box)",
                },
                daemon="egress-proxy",
            )
        ]

    def proxy_rules(self, mode: dict) -> list[InjectRule]:
        # One rule per host that can carry the token: `keyless.inject_spec` is single-host, and
        # Copilot spreads the credential over a plan-specific API host plus api.github.com (token
        # validation + `copilot_internal/*`), cafe.github.com (entitlement) and the business-plan
        # experiments host. `any_scheme` because a GitHub client may send the token as
        # `Bearer x`, `token x` or bare — the dummy must be recognised in each, since it is a client
        # foldyard doesn't control.
        kind = config.copilot_keyless()
        if not kind or mode.get("copilot", "off") == "off":
            return []
        out: list[InjectRule] = []
        for host in _KEYLESS_HOSTS:
            spec = keyless.inject_spec(
                _KEYLESS, host, kind, f"Copilot keyless proxy ({kind}, {host})"
            )
            rule = _spec_to_rule(spec) if spec else None
            if rule:
                out.append(replace(rule, any_scheme=True))
        return out

    def held_credentials(self, mode: dict) -> list[HeldCredential]:
        # As claude's/codex's: at rest the box's dummy is answered by the proxy, in a JSON error
        # shape the CLI prints. `any_scheme` for the same reason as the rules.
        kind = config.copilot_keyless()
        if not kind or mode.get("copilot", "off") != "off":
            return []
        held = keyless.held_dummy(_KEYLESS, kind)
        if not held:
            return []
        header, dummy = held
        message = keyless.at_rest_message("Copilot", "copilot")
        body = json.dumps({"error": {"message": message, "type": "invalid_request_error"}})
        return [
            HeldCredential(host, header, dummy, "copilot", body, any_scheme=True)
            for host in _KEYLESS_HOSTS
        ]

    def secrets(self, mode: dict) -> list[Secret]:
        # Asked for when the axis goes on. The pattern accepts every token shape Copilot accepts
        # (fine-grained PAT, gh OAuth token, user token) — classic ghp_ PATs are not among them,
        # which is why they are absent here rather than silently stored and refused by GitHub.
        kind = config.copilot_keyless()
        spec = _KEYLESS.get(kind)
        if not spec or mode.get("copilot", "off") == "off":
            return []
        return [
            Secret(
                var=spec["env"],
                label=f"GitHub Copilot token ({kind})",
                how=spec["how"],
                pattern="github_pat_*|gho_*|ghu_*",
            )
        ]

    def derive_env(self, mode: dict) -> dict[str, str]:
        # A copilot-owned marker recording the rung, like claude's/codex's: the dummies are ambient
        # (see box_args), so a flip changes no box-side state and needs no recreate — this stays as
        # the visible answer to "which rung was this box created under?".
        if config.copilot_keyless() and mode.get("copilot", "off") != "off":
            return {"COPILOT_INJECT": str(mode["copilot"])}
        return {}

    def box_args(self, env: dict) -> list[str]:
        if not config.copilot_enabled():
            return []
        box_home = env.get("FY_BOX_HOME") or "/home/vscode"
        copilot_home = f"{box_home}/.copilot"
        # Unlike Claude (whose `.claude.json` sits outside its mounted dir), Copilot keeps ALL of
        # its state under COPILOT_HOME — the login token (or the plaintext fallback when no system
        # credential store is available), settings, permissions, the session index and
        # `session-state/`. One volume therefore persists everything; we set the var explicitly so
        # it never depends on HOME resolving to the same dir (root here has HOME=/home/vscode).
        args = ["-e", f"COPILOT_HOME={copilot_home}", "-v", f"devbox_copilot_home:{copilot_home}"]
        transcripts = env.get("FY_COPILOT_TRANSCRIPTS") or ""
        if transcripts:  # never through a link the box planted in its from-box slot (ADR-0033)
            boxmounts.ensure_bind_source(transcripts)
            args += ["-v", f"{transcripts}:{copilot_home}/session-state"]
        # Keyless: bake the DUMMY token so the client emits the Authorization header the proxy
        # rewrites in flight. AMBIENT with the proxy substrate (FY_PROXY) whenever keyless is
        # CONFIGURED — never gated on the rung, which is a host-side decision (does the proxy
        # inject?), while box env is create-time; gating them together made `fy mode copilot=on`
        # land with nothing changed in the box. A BARE [copilot] box gets no dummy, because
        # `copilot_keyless()` is empty there — that box logs in for real.
        if env.get("FY_PROXY") and config.copilot_keyless():
            args += keyless.dummy_box_args(_KEYLESS, config.copilot_keyless())
        return args

    def egress_recommend(self) -> list[dict]:
        """What `_INSTALL` and a bare (manual-login) box reach for, so a walled box can install and
        sign in after one consented yes instead of a blocked-host hunt. Under ``keyless`` the API
        hosts are exempt structurally (the proxy has a rule for each), so only the installer's hosts
        are offered. Telemetry (``telemetry.*.githubcopilot.com``) is deliberately absent: the CLI
        degrades without it, and a dev box gains nothing from shipping usage data."""
        if not config.copilot_enabled():
            return []
        hosts = [
            {"host": "github.com", "why": "Copilot CLI installer + release page"},
            {
                "host": "raw.githubusercontent.com",
                "why": "Copilot CLI installer script (install.sh)",
            },
            {"host": "release-assets.githubusercontent.com", "why": "Copilot CLI release tarball"},
            {"host": "objects.githubusercontent.com", "why": "Copilot CLI release downloads"},
        ]
        if not config.copilot_keyless():
            hosts += [
                {"host": h, "why": "GitHub Copilot API (manual in-box login)"}
                for h in _KEYLESS_HOSTS
            ]
        return hosts

    def box_bootstrap(self, env: dict) -> list[dict]:
        if not config.copilot_enabled():
            return []
        return [{"label": "Copilot CLI", "check": "command -v copilot", "run": _INSTALL}]
