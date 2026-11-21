---
section: Added
bump: minor
---

- **Copilot keyless auth: `[copilot] keyless = "api-key"`.** The box bakes a dummy
  `COPILOT_GITHUB_TOKEN` and the egress proxy rewrites `Authorization` with `Bearer ` + the real
  token, which lives only in `~/.foldyard/<project>/host.env`. Keyless is required: a `[copilot]`
  table without it is refused, with no in-box login to fall back on. `fy box up` prompts once
  (hidden) and classifies a pasted `github_pat_`, `gho_` or `ghu_` value as the Copilot credential,
  so the same prompt that already services Claude and Codex accepts a GitHub token. Copilot
  validates its token against `api.github.com` before its first Copilot call and then talks to a
  plan-specific host (individual/business/enterprise), so the injector covers `api.github.com` plus
  every `api.*.githubcopilot.com` host; a rule fires only on a request carrying the dummy, and
  `any_scheme` covers the `Bearer`/`token`/bare forms a GitHub client may send. `[copilot] keyless`
  cannot coexist with a `github-app`/`gh-cli` `[[inject]]` rule: both pin `api.github.com` with no
  path, and the config points that out rather than letting one silently shadow the other.
  `fy verify` fails a real token left in the box — in `COPILOT_GITHUB_TOKEN` or in the CLI-managed
  `$COPILOT_HOME/config.json` (which `storeTokenPlaintext` can write) — and `fy doctor` names it,
  without ever editing that file or mistaking its `loggedInUsers` rows for a login.
