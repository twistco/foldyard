"""The ``codex`` plugin — OpenAI Codex CLI in the dev box, gated on a ``[codex]`` table.

Mirrors the ``claude`` plugin: when ``[codex]`` is declared, install the Codex CLI on box-up (a
monitored ``box_bootstrap`` step) and mount its persisted ``~/.codex`` (auth.json + config) so
login survives box recreation / ``fy nuke``. The launcher is ``fy codex`` (in ``box.py``).

A BARE ``[codex]`` (no ``keyless``) is the MANUAL-LOGIN box: no dummy ``auth.json``, no apps-MCP
override, no mode axis, and the login + API hosts join ``egress_recommend`` (no injector exists to
exempt them). Headless caveat, and why the recommend says `codex login` rather than promising the
browser flow: `codex login` with a ChatGPT account redirects to ``127.0.0.1:1455`` — a loopback
INSIDE the box, which the Mac's browser can't reach — so the workable in-box path is
``codex login --api-key``. A ChatGPT subscription wants ``keyless = "chatgpt"`` instead, which is
the whole reason that rung exists.

KEYLESS auth — no real credential in the box; the egress proxy injects it in flight (composes in the
shared proxy's multi-injector rule set with claude / github). Two modes (``config.codex_keyless``):

  - ``"api-key"`` — rewrite ``Authorization`` on ``api.openai.com`` from ``OPENAI_API_KEY`` in
    ``host.env`` (``Bearer `` prefix), and bake a DUMMY ``OPENAI_API_KEY`` in the box.
  - ``"chatgpt"`` (your ChatGPT subscription) — the box holds a DUMMY ``~/.codex/auth.json`` (a
    far-future-exp JWT so codex there never refreshes); the proxy rewrites ``Authorization`` on
    ``chatgpt.com/backend-api/codex`` with the *current* access token, which the
    :mod:`~foldyard.plugins.codex_chatgpt_token` minter refreshes host-side from the Mac's real
    ``~/.codex/auth.json``. The real access/refresh tokens never enter the box; the ``account_id``
    (an identifier, not a secret) IS baked into the dummy so codex emits ``ChatGPT-Account-Id``.

Facts verified against the Codex source (openai/codex): the CLI installs from OpenAI's NATIVE
installer (``chatgpt.com/codex/install.sh`` → a static musl binary), never npm — the house
pattern every bootstrap step follows (see :mod:`~foldyard.plugins.github`), because the box
contract promises git + uv + an engine client and nothing else, so an ``npm i -g`` step ✗'d on
the generic uv-only image and on any consumer image whose node came without npm (Debian's
``nodejs`` package does). Creds live in ``$CODEX_HOME`` (default ``~/.codex``) as ``auth.json``
(its ``OPENAI_API_KEY`` field) or the ``OPENAI_API_KEY`` env; the auto-approve flag is
``--dangerously-bypass-approvals-and-sandbox``. Stdlib only.
"""

from __future__ import annotations

import base64
import shlex
import sys
from pathlib import Path

from .. import config, keyless
from ..keyless import CODEX_KEYLESS as _KEYLESS  # the shared keyless taxonomy (stdlib-only)
from ..keyless import CODEX_KEYLESS_HOST as _KEYLESS_HOST
from . import Axis, InjectRule, Plugin
from .inject import _spec_to_rule  # reuse the static-token minter wiring (same package, no cycle)

# Remove a stale npm-managed Codex so the native installer's ~/.local/bin copy wins on PATH (the
# installer itself only WARNS about a conflicting manager and leaves the choice to PATH order).
# Only reached when the step RUNS, i.e. `command -v codex` found nothing — a consumer image that
# bakes its own codex is skipped whole and never has it removed.
# Then link the release the persisted CODEX_HOME volume already holds, or install one. The payload
# lands under $CODEX_HOME/packages/standalone (the mounted `devbox_codex_home`), so only the
# ~/.local/bin symlink dies with the container: a recreated box relinks with ZERO egress, the same
# warm-cache fast path the Claude step has.
_INSTALL = (
    "rm -f /opt/fy-tools/bin/codex /usr/local/bin/codex 2>/dev/null || true\n"
    "rm -rf /usr/local/lib/node_modules/@openai 2>/dev/null || true\n"
    'cached="${CODEX_HOME:-$HOME/.codex}/packages/standalone/current/bin/codex"\n'
    'if [ -x "$cached" ]; then\n'
    '  mkdir -p "$HOME/.local/bin" && ln -sf "$cached" "$HOME/.local/bin/codex"\n'
    "else\n"
    # CODEX_NON_INTERACTIVE: the installer prompts (on /dev/tty, which the bootstrap's exec may
    # still have) when it finds a conflicting install; a bootstrap step must never block.
    "  (cd /tmp && curl -fsSL https://chatgpt.com/codex/install.sh "
    "| CODEX_NON_INTERACTIVE=1 sh)\n"
    "fi"
)


class CodexPlugin(Plugin):
    name = "codex"

    def axes(self) -> list[Axis]:
        # Only when keyless is configured: a github-shaped on/off injector axis (like claude's).
        # Off (the zero-secret default) → the box holds only a dummy key; on → the proxy injects the
        # real key host-side. Maps to the shared "egress-proxy" daemon (one proxy, many injectors).
        if not config.codex_keyless():
            return []
        return [
            Axis(
                name="codex",
                rungs=("off", "on"),
                blurb={
                    "off": "no Codex credential reaches OpenAI (box holds only a dummy)",
                    "on": "keyless Codex: proxy injects your credential host-side (none in box)",
                },
                daemon="egress-proxy",
            )
        ]

    def proxy_rules(self, mode: dict) -> list[InjectRule]:
        # Keyless injection (only when configured AND the axis is on; composes in the rule set):
        #   api-key — rewrite Authorization on api.openai.com from OPENAI_API_KEY in host.env, via
        #             the SAME static-token wiring [[inject]]/claude use.
        #   chatgpt — rewrite Authorization on chatgpt.com/backend-api/codex with the access token
        #             the codex_chatgpt_token minter refreshes host-side from the Mac's auth.json
        #             (Bearer prefix added in flight; re-mint on a 401 to force a refresh-check).
        kind = config.codex_keyless()
        if not kind or mode.get("codex", "off") == "off":
            return []
        if kind == "chatgpt":
            auth = str(keyless.codex_auth_json_path())
            minter = shlex.join(
                [sys.executable, "-m", "foldyard.plugins.codex_chatgpt_token", auth]
            )
            return [
                InjectRule(
                    host=keyless.CODEX_CHATGPT_HOST,
                    header="Authorization",
                    minter=minter,
                    value_prefix="Bearer ",
                    path_prefix=keyless.CODEX_CHATGPT_PATH_PREFIX,
                    replay_on_401=True,
                    label="Codex keyless proxy (ChatGPT subscription)",
                )
            ]
        spec = keyless.inject_spec(_KEYLESS, _KEYLESS_HOST, kind, f"Codex keyless proxy ({kind})")
        rule = _spec_to_rule(spec) if spec else None
        return [rule] if rule else []

    def derive_env(self, mode: dict) -> dict[str, str]:
        # A codex-owned marker recording the rung in the box env, like github's GH_INJECT. Nothing
        # box-side READS it any more (the dummies are ambient — see box_args/box_bootstrap), so a
        # codex flip changes no box-side state and needs no recreate; it stays as the visible
        # answer to "which rung was this box created under?".
        if config.codex_keyless() and mode.get("codex", "off") != "off":
            return {"CODEX_INJECT": str(mode["codex"])}
        return {}

    def box_args(self, env: dict) -> list[str]:
        if not config.codex_enabled():
            return []
        box_home = env.get("FY_BOX_HOME") or "/home/vscode"
        codex_home = f"{box_home}/.codex"
        # Unlike Claude (whose `.claude.json` sits OUTSIDE `~/.claude`), Codex keeps ALL of its
        # state — auth.json, config.toml, the SQLite state DB, trust/onboarding — under CODEX_HOME
        # (default `~/.codex`; see codex_utils_home_dir::find_codex_home in openai/codex), which is
        # exactly the volume we mount. So the single mount already persists everything; we set
        # CODEX_HOME explicitly anyway so persistence never silently depends on HOME resolving to
        # the same dir (we run as root with HOME=/home/vscode). The mount creates the dir, so
        # codex's "CODEX_HOME must exist" check is satisfied.
        args = ["-e", f"CODEX_HOME={codex_home}", "-v", f"devbox_codex_home:{codex_home}"]
        transcripts = env.get("FY_CODEX_TRANSCRIPTS") or ""
        if transcripts:
            Path(transcripts).mkdir(parents=True, exist_ok=True)
            args += ["-v", f"{transcripts}:{codex_home}/sessions"]
        # Keyless: bake the DUMMY OPENAI_API_KEY so the client emits the Authorization header the
        # proxy overwrites in flight. AMBIENT with the proxy substrate (FY_PROXY) whenever keyless
        # is CONFIGURED — not gated on the rung, exactly like github's dummy GH_TOKEN. The rung is a
        # host-side decision (does the proxy inject?), and gating box-side state on it made
        # `fy mode codex=on` a lie until you recreated the box: the flip landed, nothing in the box
        # changed, and codex asked you to log in. A non-keyless [codex] box (a real login in
        # ~/.codex) still gets no dummy, because `codex_keyless()` is empty there.
        if env.get("FY_PROXY") and config.codex_keyless():
            args += keyless.dummy_box_args(_KEYLESS, config.codex_keyless())
        return args

    def egress_recommend(self) -> list[dict]:
        """What `_INSTALL` below reaches for, so a walled box can install Codex after one consented
        yes instead of a blocked-host hunt. `chatgpt.com` is listed because the installer SCRIPT
        lives there and is fetched in every keyless mode — under `keyless = "chatgpt"` the same host
        is also the injector host (exempt without a grant), but under `api-key`/no keyless nothing
        exempts it. `api.openai.com` is deliberately absent: injector host, exempt structurally.
        The installer's GitHub fallback (`CODEX_INSTALLER_USE_RELEASES_OPENAI_COM=0`) isn't
        recommended — `releases.openai.com` is its default source.

        Under a BARE ``[codex]`` (no ``keyless``) the API + login hosts join the offer: that box
        logs in for real, and with no injector rule nothing exempts them structurally."""
        if not config.codex_enabled():
            return []
        hosts = [
            {"host": "chatgpt.com", "why": "Codex CLI installer (install.sh)"},
            {"host": "releases.openai.com", "why": "Codex CLI release binaries + updates"},
        ]
        if not config.codex_keyless():
            hosts += [
                {"host": "api.openai.com", "why": "Codex API (manual in-box login)"},
                {"host": "auth.openai.com", "why": "Codex login (`codex login`)"},
            ]
        return hosts

    def box_bootstrap(self, env: dict) -> list[dict]:
        if not config.codex_enabled():
            return []
        steps = [{"label": "Codex CLI", "check": "command -v codex", "run": _INSTALL}]
        # Both keyless steps below are AMBIENT with the proxy substrate whenever keyless is
        # CONFIGURED (mirroring box_args' dummy) — never gated on the rung. Box-side state that
        # tracked the rung could only be built at CREATE time, so `fy mode codex=on` couldn't take
        # effect without `fy box down && fy box up`; ambient means the flip is purely host-side
        # (does the proxy inject?) and the box is already shaped for it either way.
        keyless_on = bool(env.get("FY_PROXY") and config.codex_keyless())
        # Keyless: the `apps`/codex_apps MCP (chatgpt.com/backend-api/ps/mcp) needs full ChatGPT-
        # session auth we don't inject, so it 401s on every startup ("Could not parse your
        # authentication token"). Turn it off via codex's own merge-safe config writer.
        if keyless_on:
            steps.append(
                {
                    "label": "Codex disable apps MCP (keyless)",
                    "check": "codex features list 2>/dev/null | grep -qE '^apps[[:space:]].*false'",
                    "run": "codex features disable apps",
                }
            )
        # ChatGPT keyless: codex needs a structurally-valid ~/.codex/auth.json to operate in chatgpt
        # mode + emit headers. Seed a DUMMY one (far-future-exp JWT so codex never refreshes it;
        # real account_id read host-side from the Mac's auth.json). Base64 so the JSON survives the
        # monitored-step shell intact. The file is inert without the host proxy: a fake JWT plus an
        # account_id, which is an identifier and not a secret — so seeding it ambiently grants the
        # box nothing that the rung being off should have withheld.
        if keyless_on and config.codex_keyless() == "chatgpt":
            account_id = keyless.codex_account_id()
            if account_id:
                b64 = base64.b64encode(keyless.dummy_codex_auth_json(account_id).encode()).decode()
                run = (
                    'mkdir -p "$HOME/.codex" && '
                    f'printf %s {b64} | base64 -d > "$HOME/.codex/auth.json" && '
                    'chmod 600 "$HOME/.codex/auth.json"'
                )
                steps.append(
                    {
                        "label": "Codex keyless auth.json",
                        # Re-bake when the on-disk file isn't byte-identical to the desired dummy
                        # (format change, different account_id, or a stale/broken one) — not just
                        # when absent, so a box with an old dummy self-heals on `fy box up`.
                        # Keyless mode always owns this file (the real login lives host-side), so
                        # overwriting a non-matching one is safe.
                        "check": f'printf %s {b64} | base64 -d | cmp -s - "$HOME/.codex/auth.json"',
                        "run": run,
                    }
                )
            else:  # no usable auth.json on the Mac → the box can't do chatgpt keyless; fail loudly
                steps.append(
                    {
                        "label": "Codex keyless auth.json",
                        "check": "false",
                        "run": 'echo "✗ no ChatGPT ~/.codex/auth.json on the Mac '
                        "(run 'codex login' there first)\"; false",
                    }
                )
        return steps
