---
section: Changed
bump: minor
---

- **Two switches can no longer both inject on one host and path.** `fy mode` (and the TUI's mode
  buttons) refuse a level whose injection rules would overlap another switch's active ones (the
  same host, with path prefixes where one is under the other or either covers the whole host). The
  message names the other switch and the exact `fy mode` command to run first. Before, a consumer's
  `[[inject]]` row on `api.anthropic.com` beside keyless `claude=on` both applied, and the proxy
  sent whichever credential's rule came first in plugin load order. One switch may still split a
  host by path, as Codex on a ChatGPT subscription does. The check runs on the rules the proxy
  would actually get, so a rule that only appears with several switches on together is caught too. If your saved mode already has such a
  pair on (or you adopt an `[[inject]]` row that creates one), the proxy injects **neither** until
  one is off, and says so: a supervisor log line and notification, an error row in `fy mode` and
  `fy state`, a `credential overlap` row in `fy doctor`, and both rows marked `HELD BACK` in
  `fy config widenings`, with the commands that end it.
