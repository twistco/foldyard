"""The ``copilot`` plugin — GitHub Copilot CLI in the dev box, gated on a ``[copilot]`` table.

Mirrors the ``codex`` plugin: when ``[copilot]`` is declared, install the Copilot CLI on box-up (a
monitored ``box_bootstrap`` step) and mount its persisted ``~/.copilot`` — settings, permissions and
``session-state`` — so the session survives box recreation / ``fy nuke``. A ``keyless`` key is
required (ADR-0008, amended 2026-11-18): there is no in-box login. The launcher is ``fy copilot``
(in ``box.py``).

KEYLESS auth (``[copilot].keyless = "api-key"``) — no real token in the box; the egress proxy
injects it in flight (composes in the shared proxy's multi-injector rule set with claude / codex /
``[[inject]]``). The box bakes a dummy ``COPILOT_GITHUB_TOKEN``; the proxy rewrites the
``Authorization`` header with ``Bearer `` + the real token from ``host.env`` on every host in
:data:`~foldyard.keyless.COPILOT_KEYLESS_HOSTS`. That list is not one host because Copilot talks to
a PLAN-SPECIFIC API host (individual/business/enterprise) and ALWAYS validates the token against
``api.github.com`` before its first call — so the rule set covers each host that can carry it. A
rule names the dummy (``any_scheme``: ``Bearer``/``token``/bare) and the proxy overwrites
``Authorization`` on its hosts — so a credential obtained in the box is never forwarded. If a
future CLI exchanges the injected token, that belongs in a new host-side keyless kind
(ADR-0031), not in proxy routing.

Facts verified against the installed CLI (1.0.109): state lives in ``$COPILOT_HOME`` (default
``~/.copilot``: ``settings.json``, the CLI-managed ``config.json``, ``permissions-config.json``,
``session-state/<uuid>/``, ``session-store.db``). That config.json can hold a real token when the
CLI's ``storeTokenPlaintext`` setting is on with no keychain, so verify/doctor scan its VALUES for
a token shape (never its user rows, which a keyless session legitimately fills, and never editing
it); ``--session-id <uuid>`` names a new session and
``--resume <id>`` resumes one; ``--allow-all`` = tools + paths + urls; ``COPILOT_ALLOW_ALL=true``
also trusts the working directory; there is no ``--append-system-prompt`` and no ad-hoc config
override flag — orientation arrives as ``$COPILOT_HOME/instructions/foldyard.instructions.md``, one
of the personal instruction sources the CLI loads (``box._seed_copilot_instructions``). The CLI
self-updates in the background; ``--no-auto-update`` / ``COPILOT_AUTO_UPDATE=false`` turn it off.
Stdlib only.
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path

from .. import boxmounts, config, keyless
from ..keyless import COPILOT_KEYLESS as _KEYLESS  # the shared keyless taxonomy (stdlib-only)
from ..keyless import COPILOT_KEYLESS_HOSTS as _KEYLESS_HOSTS
from . import (
    DoctorContext,
    HeldCredential,
    InjectRule,
    Plugin,
    Secret,
    Switch,
    VerifyContext,
)
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
# `pipefail` makes a failed `curl` fail the step: without it the pipeline's status is bash's, and
# an empty download would "install" nothing yet report success.
_INSTALL = (
    "(set -o pipefail && cd /tmp && curl -fsSL --retry 3 "
    "https://raw.githubusercontent.com/github/copilot-cli/refs/heads/main/install.sh "
    '| PREFIX="$HOME/.local" PATH="$HOME/.local/bin:$PATH" bash)'
)


def _stored_token() -> list[str]:
    """Where this box's Copilot has a real token in its CLI-managed config.json (see
    :func:`foldyard.keyless.copilot_stored_token`), under the config dir `box_args` points it at."""
    home = Path(os.environ.get("COPILOT_HOME") or Path.home() / ".copilot")
    return keyless.copilot_stored_token(home)


# What to do with a real token that reached the box — either in the CLI-managed config.json (via
# `storeTokenPlaintext`) or in the credential env var. The user ROWS in config.json are not the
# finding: a keyless session legitimately names its user there. Never edit that CLI-owned file.
_REMOVE_LOGIN = (
    "use `/logout` in `copilot` to clear a stored one (or delete $COPILOT_HOME/config.json), or "
    "unset COPILOT_GITHUB_TOKEN; keyless injects the real token from your computer, never stored "
    "here"
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
        # experiments host. `any_scheme` because a GitHub client may send the bare token as
        # `Bearer x`, `token x` or bare — name the rule by the BARE taxonomy dummy (what the box
        # bakes), and let the proxy's `any_scheme` match whichever form the client puts on the
        # header. There is no exchange special case: the proxy overwrites `Authorization` on these
        # hosts, so a stray box-held token is never forwarded. A future CLI that exchanges the
        # token should surface as a 401 and be handled by a new host-side keyless kind (ADR-0031).
        kind = config.copilot_keyless()
        if not kind or mode.get("copilot", "off") == "off":
            return []
        spec = _KEYLESS[kind]
        out: list[InjectRule] = []
        for host in _KEYLESS_HOSTS:
            inject_spec = keyless.inject_spec(
                _KEYLESS, host, kind, f"Copilot keyless proxy ({kind}, {host})"
            )
            rule = _spec_to_rule(inject_spec) if inject_spec else None
            if rule:
                out.append(
                    replace(
                        rule,
                        dummies=(spec["dummy"],),
                        any_scheme=True,
                    )
                )
        return out

    def held_credentials(self, mode: dict) -> list[HeldCredential]:
        # As claude's/codex's: at rest the box's dummy is answered by the proxy, in a JSON error
        # shape the CLI prints. The box bakes the BARE taxonomy dummy; `any_scheme` lets the held
        # entry match it however the client prefixes it (`Bearer x`, `token x` or bare).
        kind = config.copilot_keyless()
        if not kind or mode.get("copilot", "off") != "off":
            return []
        spec = _KEYLESS[kind]
        message = keyless.at_rest_message("Copilot", "copilot")
        body = json.dumps({"error": {"message": message, "type": "invalid_request_error"}})
        return [
            HeldCredential(host, spec["header"], spec["dummy"], "copilot", body, any_scheme=True)
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
        # its state under COPILOT_HOME — settings, permissions, the session index and
        # `session-state/`. One volume therefore persists everything; we set the var explicitly so
        # it never depends on HOME resolving to the same dir (root here has HOME=/home/vscode).
        # Keyless is the only supported posture (ADR-0008, amended 2026-11-18), so this volume
        # only ever holds a keyless session — never a real token.
        args = [
            "-e",
            f"COPILOT_HOME={copilot_home}",
            "-v",
            f"devbox_copilot_home:{copilot_home}",
        ]
        transcripts = env.get("FY_COPILOT_TRANSCRIPTS") or ""
        if transcripts:  # never through a link the box planted in its from-box slot (ADR-0033)
            boxmounts.ensure_bind_source(transcripts)
            args += ["-v", f"{transcripts}:{copilot_home}/session-state"]
        # Keyless: bake the DUMMY token so the client emits the Authorization header the proxy
        # rewrites in flight. AMBIENT with the proxy substrate (FY_PROXY) whenever keyless is
        # CONFIGURED — never gated on the rung, which is a host-side decision (does the proxy
        # inject?), while box env is create-time; gating them together made `fy mode copilot=on`
        # land with nothing changed in the box. A declared [copilot] without `keyless` is refused
        # at launch, so a real token never reaches this volume.
        if env.get("FY_PROXY") and config.copilot_keyless():
            args += keyless.dummy_box_args(_KEYLESS, config.copilot_keyless())
        return args

    def egress_recommend(self) -> list[dict]:
        """What `_INSTALL` reaches for, so a walled box can install the CLI after one consented yes
        instead of a blocked-host hunt: only the installer's hosts. Never the API or auth hosts —
        there is no in-box login (ADR-0008, amended 2026-11-18), and every host in
        :data:`~foldyard.keyless.COPILOT_KEYLESS_HOSTS` is the injector's, exempt from the wall
        structurally. Telemetry (``telemetry.*.githubcopilot.com``) is deliberately absent: the CLI
        degrades without it, and a dev box gains nothing from shipping usage data."""
        if not config.copilot_enabled():
            return []
        return [
            {"host": "github.com", "why": "Copilot CLI installer + release page"},
            {
                "host": "raw.githubusercontent.com",
                "why": "Copilot CLI installer script (install.sh)",
            },
            {"host": "release-assets.githubusercontent.com", "why": "Copilot CLI release tarball"},
            {"host": "objects.githubusercontent.com", "why": "Copilot CLI release downloads"},
        ]

    def verify_checks(self, ctx: VerifyContext) -> Iterator[tuple[str, str]]:
        # In the box only (the volume isn't on your computer): no real Copilot token in it —
        # neither a plaintext one the CLI stored in its own config.json nor a real value where the
        # dummy env var belongs. The config.json USER rows are not inspected: a keyless session
        # legitimately names its user there. Watched in EVERY box, declared or not, like the others.
        if not ctx.in_box:
            return
        leaks = keyless.credential_env_leaks(_KEYLESS, os.environ)
        yield from keyless.stored_login_rows("Copilot", _stored_token(), leaks, _REMOVE_LOGIN)

    def box_doctor_checks(self, ctx: DoctorContext) -> Iterator[tuple[str, str, str]]:
        # As claude's/codex's: a row for a declared agent, and for an undeclared one only when
        # there's a finding — a stored token in config.json or a real env var.
        found = _stored_token()
        leaks = keyless.credential_env_leaks(_KEYLESS, os.environ)
        if not found and not leaks and not config.copilot_enabled():
            return
        problems = [keyless.stored_login_detail(found, _REMOVE_LOGIN)] if found else []
        problems += [f"{name} holds a real token — {_REMOVE_LOGIN}" for name in leaks]
        yield ctx.result(
            not problems,
            "copilot credential",
            "none in the box (keyless)",
            "; ".join(problems),
        )

    def box_bootstrap(self, env: dict) -> list[dict]:
        if not config.copilot_enabled():
            return []
        return [{"label": "Copilot CLI", "check": "command -v copilot", "run": _INSTALL}]
