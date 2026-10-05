---
section: Changed
bump: minor
---

- **A declared credential routes the box through the proxy, even while its switch is off.** An
  `[[inject]]` row, or keyless Claude or Codex, without a `[proxy]` table used to leave a box
  created with that switch off with no proxy route and no placeholder, so switching it on later
  couldn't reach the box until `fy box down && fy box up`. The box now gets the route and its
  `box_env` placeholders whenever a credential is declared, and the proxy listener runs for it.
