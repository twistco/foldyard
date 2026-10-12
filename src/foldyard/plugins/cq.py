"""The ``cq`` plugin — mozilla-ai/cq, the shared agent-knowledge commons, for Claude and Codex in
the dev box, on the HOSTED service (cq.exchange). Opted into per agent: ``[claude] cq = true``
and/or ``[codex] cq = true`` (see :func:`foldyard.config.cq_agents`).

What a box gets:

  - **the cq CLI**, a pinned release verified against its published SHA-256 (:data:`CQ_VERSION`),
    cached in the ``devbox_cq_data`` volume beside cq's own local store, so a recreated box relinks
    it with no egress and keeps any knowledge units that are still waiting to be uploaded.
  - **for Claude**, a plugin foldyard renders at box-up from that binary (its skill and its
    ``/cq:reflect`` / ``/cq:status`` prompts come from ``cq prompt …``, so they always match the
    CLI) and ``fy claude`` loads with ``--plugin-dir`` — nothing is installed into the persisted
    ``~/.claude``, so dropping ``cq = true`` leaves no trace. It carries the MCP server and the
    session hooks.
  - **for Codex**, the same pieces as box-owned SYSTEM config under ``/etc/codex`` (the MCP
    server in ``config.toml``, the skill under ``skills/``, the hooks in ``hooks.json``): hooks in
    the system layer are managed, so Codex runs them without its per-hook trust prompt, and the
    user's persisted ``~/.codex`` is never written. Codex hands an MCP server a scrubbed
    environment, so the entry forwards cq's settings and the proxy routing by name (``env_vars``).
  - **the hooks** (:mod:`foldyard.cqhook`): during the session — the protocol at session start,
    and on Claude a nudge to query cq after a failed tool call; at the end — ``session-end`` names
    the session that ended, and the launcher offers ``/cq:reflect`` on it (``box._with_reflect``).

The credential follows foldyard's keyless rule — the box never holds it. cq sends ``Authorization:
Bearer <key>`` to the API host only when it has a key, so the box has NONE (not even a dummy: the
hosted API refuses a wrong key even on reads), and the ``cq`` axis decides whether the egress proxy
adds the operator's key (``CQ_API_KEY`` in host.env) on :data:`~foldyard.keyless.CQ_API_HOST`. At
rest cq is anonymous: queries of the public commons work, and a refused write lands in the local
store for cq's MCP server to drain once the key flows (see :data:`~foldyard.keyless.CQ_KEYLESS`).
Stdlib only.
"""

from __future__ import annotations

import base64
import json
import shlex

from .. import config, keyless
from ..keyless import CQ_KEYLESS as _KEYLESS
from . import InjectRule, Plugin, Secret, Switch
from .inject import _spec_to_rule

# The cq CLI release the box runs, and the SHA-256 of each Linux archive as published in that
# release's checksums.txt (the repo tags every component, so `releases/latest` is not reliably the
# CLI — the version is pinned, and a download that doesn't match is refused, not run).
CQ_VERSION = "0.18.0"
CQ_SHA256 = {
    "x86_64": "27787adbe5ac2ab8daab833033f1c37ea3b0e8fa3b8a8b39db15e9a2aba3dc2c",
    "arm64": "bcff3c53a0307adf402a3526658fbdd300c4277f6f98fe10753fe169db60c85f",
}
_RELEASE = f"https://github.com/mozilla-ai/cq/releases/download/cli%2Fv{CQ_VERSION}"

# Where foldyard renders the Claude plugin (`fy claude --plugin-dir`) — on the container layer, so
# a recreated box renders it afresh from its own binary.
CLAUDE_PLUGIN_DIR = "/opt/fy-cq/claude/cq"
CODEX_SYSTEM_DIR = "/etc/codex"
# The first line of every file foldyard owns under /etc/codex: one without it is the image's own
# and is never overwritten.
_CODEX_MARKER = "foldyard: cq for Codex"

# Env Codex's MCP launcher must forward to `cq mcp`: it starts servers from a short default list
# (HOME, PATH, …), and without these cq would find no address and no route out of the box.
CODEX_MCP_ENV_VARS = (
    "CQ_ADDR",
    "CQ_LOCAL_DB_PATH",
    "HTTPS_PROXY",
    "HTTP_PROXY",
    "NO_PROXY",
    "https_proxy",
    "http_proxy",
    "no_proxy",
    "SSL_CERT_FILE",
)

# Placeholders the bootstrap fills with the box's own paths before writing a file.
_FY, _CQ = "@FY_BIN@", "@CQ_BIN@"


def data_dir(env: dict) -> str:
    """cq's data home in the box — the ``devbox_cq_data`` volume (local store + cached CLI)."""
    return f"{env.get('FY_BOX_HOME') or '/home/vscode'}/.local/share/cq"


def _hook(agent: str, event: str, timeout: int) -> dict:
    return {
        "hooks": [
            {"type": "command", "command": f"{_FY} _cq-hook {agent} {event}", "timeout": timeout}
        ]
    }


def claude_hooks() -> dict:
    """The plugin's ``hooks/hooks.json``. Claude caps a plugin's SessionEnd hooks at 1.5 s overall
    whatever a hook asks for, which the handler fits in (no network, one small write)."""
    return {
        "hooks": {
            "SessionStart": [_hook("claude", "session-start", 10)],
            "PostToolUseFailure": [_hook("claude", "post-tool-use-failure", 10)],
            "SessionEnd": [_hook("claude", "session-end", 5)],
        }
    }


def codex_hooks() -> dict:
    """``/etc/codex/hooks.json``. No failure hook: Codex has no PostToolUseFailure, and its
    PostToolUse hands a shell call's output over as plain text with no exit code. SessionEnd
    allows at most 3 s."""
    return {
        "description": _CODEX_MARKER,
        "hooks": {
            "SessionStart": [_hook("codex", "session-start", 10)],
            "SessionEnd": [_hook("codex", "session-end", 3)],
        },
    }


def claude_plugin_json() -> dict:
    """The rendered plugin's manifest: named ``cq`` so its commands are ``/cq:reflect`` and
    ``/cq:status``, as in cq's own Claude plugin, with the MCP server at the box's binary."""
    return {
        "name": "cq",
        "version": CQ_VERSION,
        "description": "cq, the shared agent-knowledge commons (hosted) — set up by foldyard",
        "author": {"name": "Mozilla AI", "url": "https://github.com/mozilla-ai/"},
        "repository": "https://github.com/mozilla-ai/cq",
        "license": "Apache-2.0",
        "mcpServers": {"cq": {"command": _CQ, "args": ["mcp"]}},
    }


def codex_config_toml() -> str:
    """``/etc/codex/config.toml`` — the system layer, lowest precedence, so a user's own
    ``[mcp_servers.cq]`` still wins."""
    names = ", ".join(json.dumps(n) for n in CODEX_MCP_ENV_VARS)
    return (
        f"# {_CODEX_MARKER} — written by `fy box up`; edits are replaced on the next box.\n"
        "[mcp_servers.cq]\n"
        f"command = {json.dumps(_CQ)}\n"
        'args = ["mcp"]\n'
        f"env_vars = [{names}]\n"
    )


def _write(path: str, content: str) -> str:
    """Shell that writes ``content`` to ``path`` with the placeholders filled in. Base64 so any
    byte survives the step's quoting; ``sed`` with ``|`` since the paths are absolute."""
    b64 = base64.b64encode(content.encode()).decode()
    return (
        f"printf %s {b64} | base64 -d | "
        f'sed -e "s|{_FY}|$fy_bin|g" -e "s|{_CQ}|$cq_bin|g" > {shlex.quote(path)}'
    )


_PATHS = (
    'fy_bin=$(command -v fy) || { echo "✗ fy is not on PATH"; exit 1; }\n'
    'cq_bin=$(command -v cq) || { echo "✗ cq is not on PATH"; exit 1; }\n'
)


def _install(env: dict) -> str:
    cache = f"{data_dir(env)}/fy-bin/v{CQ_VERSION}"
    sums = " ".join(f"{arch}) sum={digest};;" for arch, digest in CQ_SHA256.items())
    return (
        "(set -e\n"
        f'case "$(uname -m)" in x86_64|amd64) arch=x86_64;; aarch64|arm64) arch=arm64;; '
        '*) echo "✗ cq: no release for $(uname -m)"; exit 1;; esac\n'
        f'case "$arch" in {sums} esac\n'
        f"dir={shlex.quote(cache)}\n"
        'if [ ! -x "$dir/cq" ]; then\n'
        "  tmp=$(mktemp -d)\n"
        f'  curl -fsSL -o "$tmp/cq.tgz" "{_RELEASE}/cq_Linux_$arch.tar.gz"\n'
        '  echo "$sum  $tmp/cq.tgz" | sha256sum -c - >/dev/null '
        '|| { echo "✗ cq: checksum mismatch — not installing"; rm -rf "$tmp"; exit 1; }\n'
        '  tar -xzf "$tmp/cq.tgz" -C "$tmp" cq\n'
        '  mkdir -p "$dir" && mv "$tmp/cq" "$dir/cq" && chmod 755 "$dir/cq" && rm -rf "$tmp"\n'
        "fi\n"
        'mkdir -p "$HOME/.local/bin" && ln -sf "$dir/cq" "$HOME/.local/bin/cq")'
    )


def _render_claude() -> str:
    d = shlex.quote(CLAUDE_PLUGIN_DIR)
    return (
        "(set -e\n"
        + _PATHS
        + f"rm -rf {d} && mkdir -p {d}/.claude-plugin {d}/skills/cq {d}/commands {d}/hooks\n"
        + _write(
            f"{CLAUDE_PLUGIN_DIR}/.claude-plugin/plugin.json",
            json.dumps(claude_plugin_json(), indent=2),
        )
        + "\n"
        + _write(f"{CLAUDE_PLUGIN_DIR}/hooks/hooks.json", json.dumps(claude_hooks(), indent=2))
        + "\n"
        + f'"$cq_bin" prompt skill > {d}/skills/cq/SKILL.md\n'
        + f'"$cq_bin" prompt reflect > {d}/commands/reflect.md\n'
        + f'"$cq_bin" prompt status > {d}/commands/status.md)'
    )


def _render_codex() -> str:
    d = shlex.quote(CODEX_SYSTEM_DIR)
    marker = shlex.quote(_CODEX_MARKER)
    # Refuse to replace a config.toml / hooks.json the IMAGE ships: foldyard only ever rewrites
    # files that carry its own marker.
    guard = "".join(
        f"if [ -e {d}/{name} ] && ! grep -q {marker} {d}/{name}; then "
        f'echo "✗ {CODEX_SYSTEM_DIR}/{name} belongs to the box image — not replacing it; '
        f'add cq to it by hand or drop it from the image"; exit 1; fi\n'
        for name in ("config.toml", "hooks.json")
    )
    return (
        "(set -e\n"
        + _PATHS
        + guard
        + f"mkdir -p {d}/skills/cq\n"
        + _write(f"{CODEX_SYSTEM_DIR}/config.toml", codex_config_toml())
        + "\n"
        + _write(f"{CODEX_SYSTEM_DIR}/hooks.json", json.dumps(codex_hooks(), indent=2))
        + "\n"
        + f'"$cq_bin" prompt skill > {d}/skills/cq/SKILL.md)'
    )


class CqPlugin(Plugin):
    name = "cq"

    def switches(self) -> list[Switch]:
        # Whether the operator's cq key rides along. Off is anonymous cq, not no cq: reads work and
        # writes queue locally (see the module docstring), so off is a safe default to stay at.
        if not config.cq_agents():
            return []
        return [
            Switch(
                name="cq",
                levels=("off", "on"),
                blurb={
                    "off": "cq is anonymous: reads the public commons, queues its writes locally",
                    "on": "proxy adds your cq.exchange API key; queued + new knowledge uploads",
                },
                daemon="egress-proxy",
            )
        ]

    def proxy_rules(self, mode: dict) -> list[InjectRule]:
        if not config.cq_agents() or mode.get("cq", "off") == "off":
            return []
        spec = keyless.inject_spec(_KEYLESS, keyless.CQ_API_HOST, "api-key", "cq (cq.exchange)")
        rule = _spec_to_rule(spec) if spec else None
        return [rule] if rule else []

    def secrets(self, mode: dict) -> list[Secret]:
        if not config.cq_agents() or mode.get("cq", "off") == "off":
            return []
        spec = _KEYLESS["api-key"]
        return [
            Secret(
                var=spec["env"],
                label="cq API key (cq.exchange)",
                how=spec["how"],
                pattern=spec["prefix"] + "*",
            )
        ]

    def box_args(self, env: dict) -> list[str]:
        agents = config.cq_agents()
        if not agents:
            return []
        data = data_dir(env)
        return [
            # The create-time record of which agents were wired — the launcher reads it, and its
            # absence or mismatch is how `fy box up` spots a box that predates the config.
            "-e",
            f"FY_CQ_AGENTS={','.join(agents)}",
            "-e",
            f"CQ_ADDR={keyless.CQ_ADDR}",
            "-e",
            f"CQ_LOCAL_DB_PATH={data}/local.db",
            "-v",
            f"devbox_cq_data:{data}",
        ]

    def egress_recommend(self) -> list[dict]:
        """The CLI download and cq's discovery document always. The API host is offered too: it is
        an injector host only while the axis is ON, and at rest (the default) anonymous reads of
        the public commons go there through the wall like any other egress."""
        if not config.cq_agents():
            return []
        return [
            {"host": "github.com", "why": f"cq CLI v{CQ_VERSION} release download"},
            {
                "host": "release-assets.githubusercontent.com",
                "why": "cq CLI release archive (GitHub's download redirect)",
            },
            {"host": keyless.CQ_DISCOVERY_HOST, "why": "cq node discovery (hosted cq)"},
            {
                "host": keyless.CQ_API_HOST,
                "why": "cq API — anonymous reads while `fy mode cq` is off",
            },
        ]

    def box_bootstrap(self, env: dict) -> list[dict]:
        agents = config.cq_agents()
        if not agents:
            return []
        steps = [
            {
                "label": f"cq CLI v{CQ_VERSION}",
                "check": f"cq --version 2>/dev/null | grep -q '^cq v{CQ_VERSION} '",
                "run": _install(env),
            }
        ]
        # No check: rendering is a few small writes from the binary just verified, and doing it on
        # every box keeps the plugin and the skill in step with that binary.
        if "claude" in agents:
            steps.append({"label": "cq for Claude", "check": "", "run": _render_claude()})
        if "codex" in agents:
            steps.append({"label": "cq for Codex", "check": "", "run": _render_codex()})
        return steps
