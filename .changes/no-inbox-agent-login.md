---
section: Changed
bump: minor
---

- **Claude Code and Codex run keyless only: a `[claude]` or `[codex]` table without `keyless` is
  refused, and a login stored in the box fails `fy verify`.** Without `keyless`, the agent used to
  log in inside the box (`claude`'s login, `codex login --api-key`), which left a long-lived
  credential in a volume every box of the project mounts, readable by anything in the box and usable
  from anywhere once copied. That predates the proxy injecting credentials; keyless has replaced it.
  `fy box up` and `fy box recreate` on your computer, and `fy claude` / `fy codex` in the box, now
  refuse such a table and name the fix. In the box, `fy verify` fails on Claude's
  `.credentials.json` or a `primaryApiKey` in its `.claude.json`, a Codex `auth.json` holding
  anything but foldyard's dummy, and a real `ANTHROPIC_API_KEY`, `CLAUDE_CODE_OAUTH_TOKEN` or
  `OPENAI_API_KEY`; `fy doctor` there names the file and how to remove it. A stored login doesn't
  stop the agent launching. The agents no longer offer their login and API hosts
  (`api.anthropic.com`, `console.anthropic.com`, `api.openai.com`, `auth.openai.com`) for the
  allowlist. **What to do:** each developer adds one line to their `foldyard.local.toml` under the
  agent's table: `keyless = "oauth"` (`claude setup-token` on your computer) or `"api-key"` for
  Claude, `keyless = "chatgpt"` (`codex login` on your computer once) or `"api-key"` for Codex.
  Remove any login already in a box (`/logout` in `claude`; `codex logout`, then under ChatGPT
  keyless `fy box recreate` on your computer to put foldyard's dummy `auth.json` back); `fy verify`
  says where one is.
