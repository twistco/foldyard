"""The ``claude`` plugin — Claude Code in the dev box, gated on a ``[claude]`` table.

When the consumer declares ``[claude]`` (even empty), this plugin:
  - INSTALLS Claude Code on box-up (a monitored ``box_bootstrap`` step), and
  - mounts Claude's persisted state — its config home (``~/.claude``), the native-installer
    version store (``~/.local/share/claude``), and the transcripts bind out to the host — so
    login + history survive box recreation and ``fy nuke``.

Without ``[claude]`` the box is shell-only: no install, no Claude volumes. The launcher itself
(``fy claude``) lives in ``box.py``/``cli.py`` and reads ``[claude].system_prompt``.

A BARE ``[claude]`` (no ``keyless``) is the MANUAL-LOGIN box, and every keyless artifact is gated
so it stays that way: no dummy credential (it would take precedence over the token you obtain), no
onboarding seed (that prompt is how you log in), no mode axis (there is no host-side rung to flip —
:meth:`ClaudePlugin.axes` returns nothing), and the login + API hosts join ``egress_recommend``
because no injector exists to exempt them. You run ``fy claude``, log in once, and the token
persists in the mounted ``~/.claude`` across box recreation and ``fy nuke``.

KEYLESS auth (``[claude].keyless``) — no real key/token in the box. When set, this plugin mirrors
the ``github`` injector: it contributes a ``claude`` mode axis (off/on), an egress-proxy
:class:`~foldyard.plugins.InjectRule` rewriting Claude's auth header on ``api.anthropic.com`` from a
``host.env`` var on the host (via the shipped ``static_token`` minter — reused through
``inject._spec_to_rule``), and bakes a DUMMY credential into the box so the client emits the header
the proxy then overwrites. The real key/token never enters the box. Two modes: ``api-key``
(``x-api-key`` ← ``ANTHROPIC_API_KEY``) and ``oauth`` (``authorization: Bearer`` ←
``CLAUDE_CODE_OAUTH_TOKEN``, via the rule's ``value_prefix``). It rides the shared proxy's
multi-injector rule set, so it can be ``on`` alongside github / other injectors.

AMAZON BEDROCK (``[claude.bedrock]``, experimental) — the PROVIDER, separate from the credential.
The table points Claude Code at Bedrock (``CLAUDE_CODE_USE_BEDROCK`` + ``AWS_REGION`` in the box).
With ``keyless = "bedrock"`` the credential is a Bedrock API key the proxy writes as the bearer on
the region's two Bedrock hosts: a long-term key from host.env, or — with ``profile`` — short-term
keys minted from your AWS login (:mod:`~foldyard.plugins.bedrock_token`). Without ``keyless`` the
box resolves AWS credentials itself, which is where an AWS metadata emulator would plug in
(docs/adrs/0035-claude-on-bedrock-provider-apart-from-credential.md).

The volume TARGETS are resolved from the image's HOME (``FY_BOX_HOME``/``FY_CLAUDE_HOME`` that
``box._up`` injects into the box env before calling ``box_args`` — Claude resolves ``~`` against
that HOME, ``/home/vscode`` even as root). Stdlib only; loads on the recipe hot path.
"""

from __future__ import annotations

import base64
import json
import os
import shlex
import sys
from collections.abc import Iterator
from dataclasses import replace
from datetime import UTC, datetime

from .. import boxmounts, config, keyless
from ..keyless import CLAUDE_KEYLESS as _KEYLESS  # the shared keyless taxonomy (stdlib-only)
from ..keyless import CLAUDE_KEYLESS_HOST as _KEYLESS_HOST
from . import CapabilityProbe, HeldCredential, InjectRule, Plugin, Secret, Switch, VerifyContext
from .inject import _spec_to_rule  # reuse the static-token minter wiring (same package, no cycle)

# What the short-term minter's `aws` may read beyond the base env: where the operator keeps ~/.aws.
_AWS_FILES_ENV = ("AWS_CONFIG_FILE", "AWS_SHARED_CREDENTIALS_FILE")
# AWS's own credential variables: under keyless Bedrock the box needs none of them, so any value is
# a credential that reached the box some other way.
_AWS_KEY_ENV = ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN")


def _bedrock() -> tuple[str, str]:
    """``(region, profile)`` from ``[claude.bedrock]``; ``("", "")`` without the table."""
    bedrock = config.claude_bedrock()
    return bedrock.get("region", ""), bedrock.get("profile", "")


def _bedrock_source(profile: str) -> str:
    if profile:
        return f"short-term keys from AWS profile {profile!r}"
    return f"long-term key from host.env (${_KEYLESS['bedrock']['env']})"


def _bedrock_rules() -> list[InjectRule]:
    """One rule per Bedrock host of the region, all writing the same bearer."""
    region, profile = _bedrock()
    if not region:
        return []
    label = f"Claude on Bedrock, keyless ({region}: {_bedrock_source(profile)})"
    rules = []
    for host in keyless.bedrock_hosts(region):
        spec = keyless.inject_spec(_KEYLESS, host, "bedrock", label)
        rule = _spec_to_rule(spec) if spec else None
        if rule is None:
            continue
        if profile:
            minter = [sys.executable, "-m", "foldyard.plugins.bedrock_token"]
            minter += ["--region", region, "--profile", profile]
            rule = replace(rule, minter=shlex.join(minter), env=_AWS_FILES_ENV)
        rules.append(rule)
    return rules


def _profile_check(profile: str) -> tuple[bool, str]:
    """The short-term minter's precondition: the profile still gives credentials worth a key."""
    from .. import awsauth  # lazy: off the hot path
    from . import bedrock_token

    now = datetime.now(UTC)
    try:
        creds = awsauth.profile_credentials(profile)
        bedrock_token.ttl(creds, now)
    except awsauth.CredentialError as e:
        return False, str(e)
    left = creds.remaining(now)
    lasts = "long-lived keys" if left is None else f"its session has {int(left // 60)} min left"
    return True, f"AWS profile {profile!r} gives credentials ({lasts})"


# Remove a stale npm/global Claude so the native installer's ~/.local/bin copy wins on PATH (an
# npm install pins the stale prefix). Then link the newest cached native version, or install it.
_INSTALL = (
    "rm -rf /opt/fy-tools/bin/claude /opt/fy-tools/lib/node_modules /usr/local/bin/claude "
    "/usr/local/lib/node_modules/@anthropic-ai 2>/dev/null || true\n"
    'latest=$(ls -1 "$HOME/.local/share/claude/versions" 2>/dev/null | sort -V | tail -1)\n'
    'if [ -n "$latest" ]; then\n'
    '  mkdir -p "$HOME/.local/bin" && '
    'ln -sf "$HOME/.local/share/claude/versions/$latest" "$HOME/.local/bin/claude"\n'
    "else\n"
    "  (cd /tmp && curl -fsSL https://claude.ai/install.sh | bash)\n"
    "fi"
)

# Mark Claude Code's first-run onboarding done so a keyless box doesn't prompt to authenticate
# (auth comes from the injected header, not an in-box login). Just the one non-secret flag, MERGED
# into any existing `.claude.json` so Claude's own state survives. Runs in the box via the
# bootstrap's `fy_python` (box._BASE_SCRIPT) — NOT a bare `python3`, which the packaged uv-first
# image doesn't have; it only ever looked fine because the check skips on a warm ~/.claude volume.
_ONBOARD_PY = (
    "import json,os;"
    "p=os.environ['CLAUDE_CONFIG_DIR']+'/.claude.json';"
    "d=json.load(open(p)) if os.path.exists(p) else {};"
    "d['hasCompletedOnboarding']=True;"
    "open(p,'w').write(json.dumps(d,indent=2))"
)


class ClaudePlugin(Plugin):
    name = "claude"

    def switches(self) -> list[Switch]:
        # Only when keyless is configured: a github-shaped on/off injector axis. Off (the
        # zero-secret default) → the box holds only a dummy key and can't reach Claude; on → the
        # proxy injects the real key/token host-side. Maps to the shared "egress-proxy" daemon.
        kind = config.claude_keyless()
        if not kind:
            return []
        where = "Bedrock" if kind == "bedrock" else "Anthropic"
        return [
            Switch(
                name="claude",
                levels=("off", "on"),
                blurb={
                    "off": f"no Claude credential reaches {where} (box holds only a dummy)",
                    "on": "keyless Claude: proxy injects your key/token host-side (none in box)",
                },
                daemon="egress-proxy",
            )
        ]

    def proxy_rules(self, mode: dict) -> list[InjectRule]:
        # Keyless injection: rewrite Claude's auth header on api.anthropic.com from a host.env var,
        # via the SAME static-token wiring [[inject]] uses (no duplication). Only when keyless is
        # configured AND the axis is on. The proxy plugin aggregates this into its single mitmdump's
        # rule set, so it composes with github / other injectors (no mutual exclusivity).
        kind = config.claude_keyless()
        if not kind or mode.get("claude", "off") == "off":
            return []
        if kind == "bedrock":
            return _bedrock_rules()
        spec = keyless.inject_spec(_KEYLESS, _KEYLESS_HOST, kind, f"Claude keyless proxy ({kind})")
        rule = _spec_to_rule(spec) if spec else None
        return [rule] if rule else []

    def held_credentials(self, mode: dict) -> list[HeldCredential]:
        # At rest the box still sends its dummy (box_args bakes it whenever keyless is configured);
        # the proxy answers it in the provider's error shape, which Claude Code prints as the cause.
        kind = config.claude_keyless() or ""
        held = keyless.held_dummy(_KEYLESS, kind)
        if held is None or mode.get("claude", "off") != "off":
            return []
        message = keyless.at_rest_message("Claude", "claude")
        header, dummy = held
        if kind == "bedrock":
            region, _ = _bedrock()
            body = json.dumps({"message": message})  # AWS's JSON error shape
            hosts = keyless.bedrock_hosts(region) if region else ()
            return [HeldCredential(host, header, dummy, "claude", body) for host in hosts]
        body = {"type": "error", "error": {"type": "authentication_error", "message": message}}
        return [HeldCredential(_KEYLESS_HOST, header, dummy, "claude", json.dumps(body))]

    def secrets(self, mode: dict) -> list[Secret]:
        # The moment the axis goes on is when the proxy needs the real credential, so it's asked
        # for there (the TUI modal / `fy mode`'s prompt); box-up's capture stays the backstop.
        kind = config.claude_keyless()
        spec = _KEYLESS.get(kind)
        if not spec or mode.get("claude", "off") == "off":
            return []
        if kind == "bedrock" and _bedrock()[1]:
            return []  # minted from the AWS profile: nothing to keep in host.env
        return [
            Secret(
                var=spec["env"],
                label=f"Claude keyless credential ({kind})",
                how=spec["how"],
                pattern=spec["prefix"] + "*",
            )
        ]

    def capability_probes(self, mode: dict) -> list[CapabilityProbe]:
        # A lapsed SSO session otherwise shows only as Claude's own errors in the box, while
        # `fy mode` says claude=on: the probe puts the fix (log in again) on the switch.
        _, profile = _bedrock()
        if config.claude_keyless() != "bedrock" or not profile or mode.get("claude") == "off":
            return []
        return [
            CapabilityProbe(
                switch="claude",
                name="claude-bedrock-profile",
                check=lambda: _profile_check(profile),
                interval=300.0,
            )
        ]

    def verify_checks(self, ctx: VerifyContext) -> Iterator[tuple[str, str]]:
        # Keyless Bedrock only: the box holds the dummy key and no AWS keys of any kind.
        if not ctx.in_box or config.claude_keyless() != "bedrock":
            return
        spec = _KEYLESS["bedrock"]
        value = os.environ.get(spec["env"], "")
        if value in ("", spec["dummy"]):
            state = "the dummy" if value else "unset"
            yield (
                "pass",
                f"claude: {spec['env']} is {state} (the real key stays on your computer)",
            )
        else:
            yield ("fail", f"claude: {spec['env']} isn't the dummy — possible real key in the box")
        for name in _AWS_KEY_ENV:
            if os.environ.get(name):
                yield ("fail", f"claude: {name} is set — AWS credentials in a keyless Bedrock box")
            else:
                yield ("pass", f"claude: {name} unset (no AWS credentials in the box)")

    def derive_env(self, mode: dict) -> dict[str, str]:
        # A claude-owned marker so the box's DUMMY key is baked ONLY when the injector is active —
        # NOT merely because [claude] is declared (a non-keyless agent box logs in for real).
        # Mirrors codex's CODEX_INJECT. Flows into the resolved box env where box_args reads it.
        if config.claude_keyless() and mode.get("claude", "off") != "off":
            return {"CLAUDE_INJECT": str(mode["claude"])}
        return {}

    def box_args(self, env: dict) -> list[str]:
        if not config.claude_enabled():
            return []
        box_home = env.get("FY_BOX_HOME") or "/home/vscode"
        home = env.get("FY_CLAUDE_HOME") or f"{box_home}/.claude"
        args = [
            # Point Claude's global config INTO the mounted dir. Without this, Claude writes
            # `.claude.json` (login/onboarding/trust state) to `$HOME/.claude.json` — a SIBLING of
            # the mounted `.claude` dir, on the ephemeral container layer — so it's lost on every
            # box recreation and you re-onboard each fresh box even though the token
            # (`.credentials.json`, which IS inside `.claude/`) persisted. Setting
            # CLAUDE_CONFIG_DIR=<the mount> lands `.claude.json` inside the volume too, so
            # onboarding survives a recreate along with the token.
            "-e",
            f"CLAUDE_CONFIG_DIR={home}",
            "-v",
            f"devbox_claude_home:{home}",
            "-v",
            f"devbox_claude_native:{box_home}/.local/share/claude",
        ]
        transcripts = env.get("FY_TRANSCRIPTS") or ""
        if transcripts:  # bound out to the host (survives nuke); create the host dir for the mount
            # never through a link the box planted in its from-box slot (ADR-0033)
            boxmounts.ensure_bind_source(transcripts)
            args += ["-v", f"{transcripts}:{home}/projects"]
        # Keyless: bake the DUMMY credential so the client emits the header the proxy overwrites in
        # flight. AMBIENT with the proxy substrate (FY_PROXY) whenever keyless is CONFIGURED, like
        # an [[inject]] row's box_env — NOT keyed to the rung, which is a host-side decision (does
        # the proxy inject?) while box env is create-time; keying them together made `fy mode
        # claude=on` land with nothing changed in the box until `fy box down && fy box up`. A BARE
        # [claude] box still gets no dummy, because `claude_keyless()` is empty there — that box
        # logs in for real and a dummy would take precedence over the token it obtains.
        if env.get("FY_PROXY") and config.claude_keyless():
            args += keyless.dummy_box_args(_KEYLESS, config.claude_keyless())
        # Bedrock is the provider whatever the credential, so it rides the table, not keyless.
        region, _ = _bedrock()
        if region:
            args += ["-e", "CLAUDE_CODE_USE_BEDROCK=1", "-e", f"AWS_REGION={region}"]
        return args

    def egress_recommend(self) -> list[dict]:
        """What a walled box needs to get Claude Code working, after one consented yes instead of a
        blocked-host hunt. Always the installer's hosts; under a BARE ``[claude]`` (no ``keyless``)
        also the ones a manual in-box login and its API traffic use, because nothing exempts them
        there — the structural exemption belongs to an INJECTOR host, and a bare block has no
        injector. So `api.anthropic.com` is offered exactly when it isn't already free."""
        if not config.claude_enabled():
            return []
        hosts = [
            {"host": "claude.ai", "why": "Claude Code installer (install.sh)"},
            {"host": "downloads.claude.ai", "why": "Claude Code release binaries + updates"},
        ]
        if config.claude_keyless():
            return hosts
        region, _ = _bedrock()
        if region:
            runtime, control = keyless.bedrock_hosts(region)
            hosts += [
                {"host": runtime, "why": "Claude Code on Bedrock (model calls)"},
                {"host": control, "why": "Claude Code on Bedrock (inference-profile lookups)"},
            ]
        else:
            hosts += [
                {"host": "api.anthropic.com", "why": "Claude Code API (manual in-box login)"},
                {"host": "console.anthropic.com", "why": "Claude Code login (Console account)"},
            ]
        return hosts

    def box_bootstrap(self, env: dict) -> list[dict]:
        if not config.claude_enabled():
            return []
        steps = [{"label": "Claude Code", "check": "command -v claude", "run": _INSTALL}]
        # Seed the onboarding flag so a keyless box skips Claude Code's first-run auth prompt (auth
        # is the injected header, and there's nothing here to log in WITH). A BARE [claude] box must
        # KEEP onboarding — that's how you log in for real — so this is gated on keyless being
        # CONFIGURED, exactly like the dummy in box_args, and on nothing else: keying it to the RUNG
        # made it create-time state, so `fy mode claude=on` couldn't take effect without a recreate.
        if env.get("FY_PROXY") and config.claude_keyless():
            b64 = base64.b64encode(_ONBOARD_PY.encode()).decode()
            steps.append(
                {
                    "label": "Claude onboarding flag",
                    "check": "grep -qE '\"hasCompletedOnboarding\": *true' "
                    '"$CLAUDE_CONFIG_DIR/.claude.json" 2>/dev/null',
                    "run": f"printf %s {b64} | base64 -d | fy_python -",
                }
            )
        return steps
