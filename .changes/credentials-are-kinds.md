---
section: Removed
bump: minor
---

- **`[plugins.github]` is gone: GitHub is `[[inject]]` rows now, and `fy up` refuses the old
  table until it's replaced.** The refusal prints the rows to paste, computed from your table: a
  `kind = "github-app"` row (your `app_id`, `installation_id`, `repo` as `repositories`, and
  `box_env = { GH_TOKEN = "x" }`) and, commented out, a `kind = "gh-cli"` emergency row for the old
  `user` level. Three things move with it. The App's private key is read from the switch's
  `FY_INJECT_<SWITCH>` in `host.env` (`FY_INJECT_GITHUB` for `switch = "github"`) — rename
  `GH_PEM_B64`, same value, and any `[[secret]]` row naming it. `fy mode github=app` is
  `fy mode github=on`, and the old `github=user` is the gh-cli row's own switch. And
  `permissions` has no successor: the token now carries **the App installation's own
  permissions** — foldyard no longer narrows it to `pull_requests`/`issues` or refuses a map that
  widens it, which is what turned a consumer's granted `actions: read` into a morning of 401s
  (ADR-0031).
  To change what the box can do on GitHub, change the App; for a second scope, a second App with
  its own row. foldyard no longer installs `gh` in the box: put it in your box image.
