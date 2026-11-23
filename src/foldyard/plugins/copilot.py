"""The ``copilot`` plugin — GitHub Copilot CLI in the dev box, gated on a ``[copilot]`` table.

Mirrors the ``codex`` plugin: when ``[copilot]`` is declared, install the Copilot CLI on box-up (a
monitored ``box_bootstrap`` step) and mount its persisted ``~/.copilot`` — settings, permissions and
``session-state`` — so the session survives box recreation / ``fy nuke``. A ``keyless`` key is
required (ADR-0008, amended 2026-11-18): there is no in-box login. The launcher is ``fy copilot``
(in ``box.py``).

KEYLESS auth (``[copilot].keyless``) — no real token in the box; the egress proxy supplies it in
flight (composes in the shared proxy's multi-injector rule set with claude / codex /
``[[inject]]``). The box bakes a dummy ``COPILOT_GITHUB_TOKEN``; a rule names it (``any_scheme``:
``Bearer``/``token``/bare) and the proxy overwrites ``Authorization`` on its hosts — so a credential
obtained in the box is never forwarded. Two rungs, because the token's SCOPE decides whether an
unpath-limited rule is safe (ADR-0031):

* ``"api-key"`` (recommended) captures a Copilot-ONLY fine-grained PAT into ``host.env`` and injects
  it on every host in :data:`~foldyard.keyless.COPILOT_KEYLESS_HOSTS` — no path limit needed,
  because a PAT scoped to Account → Copilot Requests with no repository access can do nothing else
  on GitHub.
* ``"oauth"`` mints `gh auth token` host-side with the packaged ``gh-cli`` minter (nothing pasted or
  stored). That is the operator's WHOLE GitHub identity, so its ``on`` is an emergency level with a
  TTL, and its ``api.github.com`` rule is limited to ``/copilot_internal/`` — the only calls the CLI
  made there in the observed trace. The token still reaches Copilot's own hosts (including the MCP
  endpoint, which acts with the token's full scope); the time limit bounds that reach without having
  to track every Copilot endpoint.

The host list is not one host because Copilot talks to a PLAN-SPECIFIC API host
(individual/business/enterprise) and ALWAYS validates the token against ``api.github.com`` before
its first call. If a future CLI exchanges the injected token, that belongs in a new host-side
keyless kind (ADR-0031), not in proxy routing.

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
import shlex
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path

from .. import boxmounts, config, keyless, selfspawn
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
# are in `egress_recommend` below. Like the Codex/Claude steps, the payload lands in the AGENT'S
# PERSISTED volume — here ${COPILOT_HOME:-$HOME/.copilot}/bin (the mounted `devbox_copilot_home`) —
# so only the ~/.local/bin symlink dies with the container: a recreated box relinks from the cache
# with ZERO egress, instead of re-downloading through the broad github.com grant. `PREFIX` points
# the installer at that volume, and PATH puts its bin dir first for the installer's "is the install
# dir on PATH?" probe: the binary it just extracted is then found, so it skips the "add this to
# your shell rc?" prompt, which otherwise reads /dev/tty — a bootstrap step must never block.
# `cd /tmp` keeps a download from landing in the repo if curl is interrupted.
# Run it under `bash`, not `sh`: the script is `#!/usr/bin/env bash` and uses bash arrays
# (`CURL_AUTH=()`), which the box's dash rejects with `Syntax error: "(" unexpected`.
# `pipefail` makes a failed `curl` fail the step: without it the pipeline's status is bash's, and
# an empty download would "install" nothing yet report success. The `&&` chain after the installer
# propagates that failure to `run_step` (which judges the whole `eval`'d script by its exit status).
_INSTALL = (
    'home="${COPILOT_HOME:-$HOME/.copilot}"\n'
    'cached="$home/bin/copilot"\n'
    'if [ -x "$cached" ]; then\n'
    '  mkdir -p "$HOME/.local/bin" && ln -sf "$cached" "$HOME/.local/bin/copilot"\n'
    "else\n"
    "  (set -o pipefail && cd /tmp && curl -fsSL --retry 3 "
    "https://raw.githubusercontent.com/github/copilot-cli/refs/heads/main/install.sh "
    '| PREFIX="$home" PATH="$home/bin:$HOME/.local/bin:$PATH" bash) '
    '&& mkdir -p "$HOME/.local/bin" && ln -sf "$cached" "$HOME/.local/bin/copilot"\n'
    "fi"
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


# The `oauth` rung reuses the gh-cli kind's packaged minter. It returns `Bearer <token>` itself
# (with its own short TTL), so this rule must NOT add a second `value_prefix`, and it reads no
# host.env var — `gh` holds its own credential on your computer. Not `GhCliKind().rule()` directly:
# that rule sets `grants_push`, which would lift the proxy's push refusal even though this rule
# never injects on a repository-write path.
def _oauth_minter() -> str:
    # github.com is PINNED here, not configurable: Copilot's injection hosts are github.com's, so a
    # token from another gh host would only produce 401s on api.github.com. `oauth_user`, when set,
    # names which github.com account supplies it; no user means that host's active account.
    args = ["--hostname", "github.com"]
    if user := config.copilot_oauth_user():
        args += ["--user", user]
    return shlex.join(selfspawn.python_m("foldyard.plugins.gh_cli_token", *args))


# The only api.github.com calls the Copilot CLI needed in the observed trace — token validation and
# `copilot_internal/*`. Everything else on that host (repository reads/writes through the REST API,
# including push) must NOT receive the operator's full-identity gh token. Copilot's own hosts keep
# the whole host: narrowing those by path would mean chasing the CLI's endpoints as they are added;
# the emergency TTL is what bounds the token there.
_OAUTH_GITHUB_PATH = "/copilot_internal/"


class CopilotPlugin(Plugin):
    name = "copilot"

    def switches(self) -> list[Switch]:
        # Only when keyless is configured: an on/off injector axis, like claude's/codex's. Off (the
        # zero-secret default) → the box holds only a dummy token; on → the proxy injects the real
        # one host-side. Maps to the shared "egress-proxy" daemon (one proxy, many injectors).
        # `oauth` carries your whole GitHub identity, so its `on` is an emergency level:
        # `fy mode copilot=on [ttl=30m]` expires and auto-reverts (default TTL 1h). The PAT rung
        # needs no TTL.
        kind = config.copilot_keyless()
        if not kind:
            return []
        return [
            Switch(
                name="copilot",
                levels=("off", "on"),
                blurb={
                    "off": "no Copilot credential reaches GitHub (box holds only a dummy)",
                    "on": (
                        "oauth (emergency, TTL): your host's gh token, path-limited on "
                        "api.github.com"
                        if kind == "oauth"
                        else "keyless api-key: proxy injects your Copilot-only PAT host-side"
                    ),
                },
                daemon="egress-proxy",
                emergency=("on",) if kind == "oauth" else (),
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
        #
        # The rung decides the MINTER and the api.github.com PATH: a Copilot-only PAT is safe on the
        # whole host, while the gh token is not — see the module docstring.
        kind = config.copilot_keyless()
        if not kind or mode.get("copilot", "off") == "off":
            return []
        spec = _KEYLESS[kind]
        out: list[InjectRule] = []
        minter = _oauth_minter() if kind == "oauth" else ""
        for host in _KEYLESS_HOSTS:
            if kind == "oauth":
                rule: InjectRule | None = InjectRule(
                    host=host,
                    header=spec["header"],
                    minter=minter,
                    replay_on_401=False,  # the minter re-runs on its short TTL anyway
                    env=(),  # `gh` holds its own credential; nothing from host.env
                    label=f"Copilot keyless proxy (oauth, {host})",
                    path_prefix=_OAUTH_GITHUB_PATH if host == "api.github.com" else "",
                    dummies=(spec["dummy"],),
                    any_scheme=True,
                )
            else:
                inject_spec = keyless.inject_spec(
                    _KEYLESS, host, kind, f"Copilot keyless proxy ({kind}, {host})"
                )
                base = _spec_to_rule(inject_spec) if inject_spec else None
                rule = replace(base, dummies=(spec["dummy"],), any_scheme=True) if base else None
            if rule is not None:
                out.append(rule)
        return out

    def held_credentials(self, mode: dict) -> list[HeldCredential]:
        # As claude's/codex's: at rest the box's dummy is answered by the proxy, in a JSON error
        # shape the CLI prints. The box bakes the BARE taxonomy dummy; `any_scheme` lets the held
        # entry match it however the client prefixes it (`Bearer x`, `token x` or bare). The oauth
        # rung's api.github.com answer is path-limited like its injector, so a box request to a
        # repository path is not answered as if the gh token were injectable there.
        kind = config.copilot_keyless()
        if not kind or mode.get("copilot", "off") != "off":
            return []
        spec = _KEYLESS[kind]
        message = keyless.at_rest_message("Copilot", "copilot")
        body = json.dumps({"error": {"message": message, "type": "invalid_request_error"}})
        return [
            HeldCredential(
                host,
                spec["header"],
                spec["dummy"],
                "copilot",
                body,
                path_prefix=_OAUTH_GITHUB_PATH
                if kind == "oauth" and host == "api.github.com"
                else "",
                any_scheme=True,
            )
            for host in _KEYLESS_HOSTS
        ]

    def secrets(self, mode: dict) -> list[Secret]:
        # Asked for when the axis goes on — but ONLY for the PAT rung. `oauth` mints its token
        # host-side from `gh`, so there is nothing to paste or store. The PAT pattern is narrow on
        # purpose: a `gho_`/`ghu_` whole-identity paste would be injected on api.github.com with no
        # path limit, which is exactly what the `oauth` rung's TTL + path limit exist to contain.
        kind = config.copilot_keyless()
        spec = _KEYLESS.get(kind)
        if not spec or kind == "oauth" or mode.get("copilot", "off") == "off":
            return []
        return [
            Secret(
                var=spec["env"],
                label=f"GitHub Copilot token ({kind})",
                how=spec["how"],
                pattern=f"{spec['prefix']}*",
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
        instead of a blocked-host hunt: only the installer's hosts. The binary is cached in the
        COPILOT_HOME volume, so these are needed for the FIRST install only — a recreated box
        relinks from the cache with no egress. Never the API or auth hosts — there is no in-box
        login (ADR-0008, amended 2026-11-18), and every host in
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
