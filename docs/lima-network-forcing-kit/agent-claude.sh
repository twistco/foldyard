#!/bin/bash
# Red-team the egress wall from the INSIDE with a real agent (Claude Code).
#
# The thesis, demonstrated by the agent against its own cage: install Claude as ROOT (unwalled),
# then run it as the WALLED `agent` user and have it try to escape. A correctly-walled agent reaches
# ONLY its allowlisted API, can't pick another route, can't escalate, and can't break out via the
# container engine. Runs INSIDE the Lima guest (`limactl shell wall`), as root.
#
#   sudo bash /tmp/agent-claude.sh setup                       # one-time: install node + claude
#   sudo CLAUDE_CODE_OAUTH_TOKEN=… bash /tmp/agent-claude.sh run [claude args…]   # launch AS agent
#
# AUTH — Max/Pro subscription, headless (no browser in the VM). Mint a long-lived (1-year) token ON
# THE MAC, which has a browser, then pass it in:
#   # on the Mac:
#   claude setup-token            # prints the token to stdout — copy it (it is NOT saved)
#   # inside the VM (as your Lima admin user):
#   sudo CLAUDE_CODE_OAUTH_TOKEN=<paste> bash /tmp/agent-claude.sh run
# Do NOT also set ANTHROPIC_API_KEY — it outranks the OAuth token (the token is inference-only).
#
# The wall is the real backstop: even if Claude ignored the proxy env, its direct egress is dropped
# (fail-closed), so a misrouted request just fails — it can't sneak out. We set the proxy env anyway
# so Claude FUNCTIONS (reaches api.anthropic.com THROUGH the only open door).
set -euo pipefail

AGENT=agent
PROXY=http://127.0.0.1:8080

usage() {
  echo "usage: sudo bash agent-claude.sh setup" >&2
  echo "       sudo CLAUDE_CODE_OAUTH_TOKEN=… bash agent-claude.sh run [claude args…]" >&2
  exit 2
}

[ "$(id -u)" = 0 ] || { echo "run as root (sudo) — setup installs globally; run drops to the agent" >&2; exit 1; }

case "${1:-}" in
setup)
  # root is unwalled, so this reaches NodeSource + npm directly.
  if ! command -v node >/dev/null 2>&1; then
    curl -fsSL https://deb.nodesource.com/setup_20.x | bash -
    DEBIAN_FRONTEND=noninteractive apt-get install -y nodejs
  fi
  npm i -g @anthropic-ai/claude-code
  # api.anthropic.com is the ONE host required for model calls under a pre-minted OAuth token (the
  # subscription LOGIN hosts — claude.ai / platform.claude.com — aren't needed once setup-token has
  # run on the Mac). Seed it so `filtered` mode lets Claude work. Want the exact set? Run a
  # `sudo wall-mode passthrough` session first and watch /var/log/transproxy.log, then add what shows.
  grep -qxF api.anthropic.com /etc/wall/allowlist || echo api.anthropic.com >> /etc/wall/allowlist
  echo "✓ $(claude --version 2>/dev/null || echo 'claude installed'); api.anthropic.com allowlisted."
  echo "  next:  sudo CLAUDE_CODE_OAUTH_TOKEN=… bash $0 run"
  ;;
run)
  : "${CLAUDE_CODE_OAUTH_TOKEN:?mint on the Mac with 'claude setup-token', then: sudo CLAUDE_CODE_OAUTH_TOKEN=… bash $0 run}"
  shift
  # Drop to the WALLED agent (login env → HOME/USER/PATH/TERM sane), forcing egress through the
  # proxy explicitly and silencing Claude's non-essential traffic so the allowlist stays minimal and
  # the test is about MODEL calls, not telemetry/updaters.
  exec sudo -iu "$AGENT" env \
    HTTP_PROXY="$PROXY" HTTPS_PROXY="$PROXY" http_proxy="$PROXY" https_proxy="$PROXY" \
    NO_PROXY=localhost,127.0.0.1 no_proxy=localhost,127.0.0.1 \
    CLAUDE_CODE_OAUTH_TOKEN="$CLAUDE_CODE_OAUTH_TOKEN" \
    CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1 \
    DISABLE_AUTOUPDATER=1 \
    claude "$@"
  ;;
*)
  usage
  ;;
esac
