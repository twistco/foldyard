---
section: Added
bump: minor
experimental: true
---

- **Copilot keyless auth: `[copilot] keyless = "api-key"` or `"oauth"`.** The box bakes a dummy
  `COPILOT_GITHUB_TOKEN` and the egress proxy supplies the real token host-side. Keyless is
  required: a `[copilot]` table without it is refused, with no in-box login to fall back on.

  - **`api-key`** (or `true`, recommended) prompts once (hidden) on `fy box up` for a **Copilot-only
    fine-grained PAT**: resource owner your personal account, Account permissions → Copilot Requests
    only, no repository access. It is captured into `~/.foldyard/<project>/host.env` like the other
    agents' keys. That narrow scope is why its injector can rewrite `Authorization` on all of
    `api.github.com` without giving the box anything else on GitHub. Only `github_pat_` pastes are
    classified as the Copilot credential; classic `ghp_` PATs are refused by Copilot itself, and
    pasted `gho_`/`ghu_` values are refused too because they carry your whole GitHub identity.
  - **`oauth`** is for seats that can't use such a PAT. Nothing is pasted or stored: the packaged
    `gh-cli` minter runs `gh auth token` on your computer (short TTL; `gh auth logout` revokes it).
    Because that is your whole GitHub identity, `on` is an emergency level with a TTL, and its
    `api.github.com` rule injects only on `/copilot_internal/`, the calls the CLI needed in the
    observed trace. Copilot's own hosts still receive the token, including the GitHub MCP endpoint
    that acts with your full scope; the time limit bounds that reach.

  Copilot validates its token against `api.github.com` before its first Copilot call and then talks
  to a plan-specific host (individual/business/enterprise), so the injector covers `api.github.com`
  plus every `api.*.githubcopilot.com` host; the dummy names the rule where hosts are shared and
  `any_scheme` covers the `Bearer`/`token`/bare forms a GitHub client may send. The proxy overwrites
  `Authorization` on those hosts, so a credential left in the box is never forwarded.
  `[copilot] keyless` cannot coexist with a `github-app`/`gh-cli` `[[inject]]` rule: both pin
  `api.github.com` with no path, and the config points that out rather than letting one silently
  shadow the other. `fy verify` fails a real token left in the box — in `COPILOT_GITHUB_TOKEN` or in
  the CLI-managed `$COPILOT_HOME/config.json` (which `storeTokenPlaintext` can write) — and
  `fy doctor` names it, without ever editing that file or mistaking its `loggedInUsers` rows for a
  login.
