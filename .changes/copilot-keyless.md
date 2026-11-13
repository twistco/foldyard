---
section: Added
bump: minor
---

- **Copilot keyless auth: `[copilot] keyless = "api-key"`.** The box bakes a dummy
  `COPILOT_GITHUB_TOKEN` and the egress proxy rewrites `Authorization` with `Bearer ` + the real
  token, which lives only in `~/.foldyard/<project>/host.env`. `fy box up` prompts once (hidden)
  and classifies a pasted `github_pat_`, `gho_` or `ghu_` value as the Copilot credential, so the
  same prompt that already services Claude and Codex accepts a GitHub token. Copilot validates its
  token against `api.github.com` before its first Copilot call and then talks to a plan-specific
  host (individual/business/enterprise), so the injector covers `api.github.com` plus every
  `api.*.githubcopilot.com` host; a rule fires only on a request carrying the dummy, and
  `any_scheme` covers the `Bearer`/`token`/bare forms a GitHub client may send. `[copilot] keyless`
  cannot coexist with a `github-app`/`gh-cli` `[[inject]]` rule: both pin `api.github.com` with no
  path, and the config points that out rather than letting one silently shadow the other.
