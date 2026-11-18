# ADR-0008 — Keyless agent auth: dummy credential in the box, rewrite in flight

- **Status:** Accepted (design 2026-06-28; Claude api-key/OAuth + Codex api-key shipped late
  2026-06; Codex ChatGPT-subscription follow-up) — implemented. **Amended 2026-11-18:** in-box login
  removed: keyless is the only way an agent runs in the box (see
  [the amendment](#amendment-2026-11-18-no-in-box-login)).
- **Sources:** docs/history/agent-editor-plugins-plan.md Item 4b, src/foldyard/keyless.py,
  src/foldyard/plugins/claude.py, src/foldyard/plugins/codex.py

## Context

Coding agents are a first-class yard workload, and both major CLIs authenticate with exactly the
kind of secret the yard must not hold: Claude Code reads `ANTHROPIC_API_KEY` /
`CLAUDE_CODE_OAUTH_TOKEN` (or `~/.claude/.credentials.json`); Codex reads `OPENAI_API_KEY` or a
ChatGPT-session `~/.codex/auth.json`. Putting the real value in the box hands a long-lived,
broadly-scoped credential to the same untrusted code the agent is running — and the agent's own
transcript/telemetry surface makes accidental leakage easy.

The GitHub injector (ADR-0007) already proved the alternative: the box holds a dummy, the egress
proxy rewrites the auth in flight, the real value lives only in `~/.foldyard/<project>/host.env` on
the Mac. Empirical verification (plus the openai/codex source) showed both agents emit a rewritable
header as soon as *any* credential-shaped value is present — so keyless agents are mostly
configuration of existing machinery.

## Decision

**The agent CLIs run in the box with dummy credentials; the proxy injects the real ones host-side.**
`src/foldyard/keyless.py` owns the credential taxonomy and the host-side capture; the
`claude`/`codex` plugins derive their injector from it identically (shared
`inject_spec`/`dummy_box_args` helpers — neither imports the other).

- **Claude, two modes** (`[claude].keyless = "api-key" | "oauth"`):
  - `api-key` — box bakes dummy `ANTHROPIC_API_KEY=sk-ant-dummy`; the client sends
    `x-api-key: <dummy>`; the proxy overwrites it on `api.anthropic.com` with the real key.
  - `oauth` — box bakes dummy `CLAUDE_CODE_OAUTH_TOKEN=sk-ant-oat-dummy`; the client sends
    `authorization: Bearer <dummy>`. The proxy overwrites the header **verbatim**, so the rule's
    `value_prefix = "Bearer "` prepends the scheme in flight and host.env keeps the bare token.
    OAuth tokens (`sk-ant-oat…`) live ~1 year, so static injection suffices — no refresh minter,
    which killed the main objection to supporting OAuth at all.
- **Codex, two modes** (`[codex].keyless = "api-key" | "chatgpt"`):
  - `api-key` — dummy `OPENAI_API_KEY=sk-dummy`; rewrite `Authorization` on `api.openai.com` (Bearer
    prefix).
  - `chatgpt` (subscription) — there is no dummy-env path for ChatGPT mode, so the box gets a
    **dummy `~/.codex/auth.json`**: structurally-valid unsigned JWTs with a far-future `exp` (codex
    refreshes at `exp − 5 min`, so the box never tries its dummy refresh token) plus the **real
    `account_id`** (an identifier, not a secret) so codex emits `ChatGPT-Account-Id`. The proxy
    rewrites `Authorization` on `chatgpt.com/backend-api/codex` with the *current* access token from
    the `codex_chatgpt_token` **refresh-minter**: it reads the host's real `auth.json`, refreshes
    via the provider's token endpoint when near expiry, and writes the rotated tokens back
    atomically under an flock — one canonical file, so real codex on the host keeps working.
    `replay_on_401 = True` forces a refresh-check on rejection.
- **Keyless is a posture axis** (ADR-0005): each plugin contributes an off/on axis mapped to the
  shared `egress-proxy` daemon, absent entirely unless `keyless` is configured. `off` (the
  zero-secret default) means the box holds only the dummy and cannot reach the provider.
  `derive_env` sets a `CLAUDE_INJECT`/`CODEX_INJECT` marker only when the axis is on — the dummy
  bake and Claude's onboarding-flag seeding key on it, so a *non*-keyless `[claude]` box gets no
  dummy. (Such a box logged in for real inside the box, the user's choice until the 2026-11-18
  amendment below refused it.) Both rules ride the proxy's multi-injector rule set (ADR-0007), so
  claude + codex + github can all be on at once.
- **Capture is host-side and prompt-once:** on a TTY `fy box up` with keyless configured and no
  matching host.env entry, `keyless.ensure_cred` prompts (hidden input), classifies the paste by
  prefix (`sk-ant-api…` → Claude key, `sk-ant-oat…` → Claude OAuth, `sk-…` → OpenAI key, so it never
  asks which), and appends it to host.env (0600). A wrong-shaped paste stores nothing; a non-TTY run
  warns and boots the box anyway with just the dummy.

## Consequences

- A compromised agent session (prompt injection, malicious dependency) cannot exfiltrate the API key
  or OAuth/session token — the box never has it; flipping the axis off cuts access immediately
  without touching the agent's install or login state.
- The real credential is entered once on the host and survives box recreation and `fy nuke`;
  `verify`/doctor can assert no real key is present in the box.
- ChatGPT keyless carries one accepted risk: the host minter and a *manual* codex run on the host
  share the rotating refresh token, and a racing refresh can invalidate it — recovery is
  `codex login`.
- Coupling to provider auth shapes is real: header names, token prefixes, and codex's
  refresh-at-`exp` behaviour are verified facts that can drift with agent releases.

## Rejected alternatives

- **Mounting real credentials into the box** (`~/.claude/.credentials.json`, real `auth.json`) — a
  long-lived secret readable by every process in the yard, exactly what ADR-0007 exists to end.
- **Codex `CODEX_ACCESS_TOKEN` env for ChatGPT mode** — codex classifies it as a PAT/agent-JWT, not
  a ChatGPT session, so the dummy-env trick doesn't apply; hence the dummy-`auth.json` shape.
- **A host-side refresh minter for Claude OAuth** — unnecessary: the ~1-year token makes static
  injection correct, and skipping the refresh flow keeps the Claude path config-only.

## Amendment (2026-11-18): no in-box login

**Context.** A `[claude]` or `[codex]` table without `keyless` meant "log in inside the box"
(`claude`'s own login, `codex login --api-key`). That was the starting point from before the egress
proxy could inject credentials, and it outlived the reason for it. The login leaves a long-lived
credential in a volume (`~/.claude/.credentials.json`, `~/.codex/auth.json`) that every box of the
project mounts: readable by anything running in the box, and usable from anywhere once copied out.
The rejected alternatives below already reject mounting real credentials into the box; an in-box
login is the same outcome, reached by another route.

**Decision.** Keyless is the only supported way to run Claude Code or Codex in the box.

1. **A `[claude]`/`[codex]` table without `keyless` is refused** at `fy box up` and
   `fy box recreate` on your computer, and by `fy claude` / `fy codex` in the box, with a message
   naming the fix: set `keyless` under that table in `foldyard.local.toml` (personal: which
   subscription you pay for is personal, and a shared one prompts every colleague). Claude:
   `"oauth"` (`claude setup-token` on your computer) or `"api-key"`. Codex: `"chatgpt"`
   (`codex login` on your computer once) or `"api-key"`.
2. **`fy verify` in the box FAILs on a real login stored there:** Claude's `.credentials.json` under
   `$CLAUDE_CONFIG_DIR` or a `primaryApiKey` in its `.claude.json`; a Codex `auth.json` under
   `$CODEX_HOME` holding anything but foldyard's dummy (a real `OPENAI_API_KEY` or real ChatGPT
   tokens); and `ANTHROPIC_API_KEY`, `CLAUDE_CODE_OAUTH_TOKEN` or `OPENAI_API_KEY` holding anything
   but foldyard's dummy. `fy doctor` in the box names the file and how to remove it (`/logout` in
   `claude` or delete the file; `codex logout`, then under ChatGPT keyless `fy box recreate` on your
   computer to seed the dummy again). A stored login does not block launching: verify fails and
   doctor reports, so nobody is locked out mid-migration.
3. **The agents' `egress_recommend` no longer offers the login and API hosts** (`api.anthropic.com`,
   `console.anthropic.com`, `api.openai.com`, `auth.openai.com`). Under keyless the API host is an
   injector host, allowed anyway; the login hosts are never needed.
4. **"Not supported" is not "impossible".** The box is root, and nothing stops someone running
   `codex login` in it. Unsupported means: not offered, refused at launch when configured that way,
   and detected by verify and doctor.

**Consequences.**

- Breaking for a consumer whose committed `[claude]`/`[codex]` has no `keyless`: each developer adds
  one line to their `foldyard.local.toml`.
- An existing box holding a login fails `fy verify` until the login is removed.
- Features that need a fuller session than the injected credential carries stay unavailable in the
  box: Codex's apps MCP, for one, is already switched off under keyless.
