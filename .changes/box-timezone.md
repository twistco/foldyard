---
section: Added
bump: patch
---

- **The dev box shows times in your computer's timezone and time format.** Claude Code in the box
  showed UTC timestamps, even though the box's clock was right. `fy box up` now passes your
  timezone as `TZ` (read from `/etc/localtime`) and your time locale as `LC_TIME`, which decides
  a 12- or 24-hour clock. `LC_TIME` is passed only when the box image has that locale and no
  `LC_ALL` would override it. A `TZ` or
  `LC_TIME` in `[box].env` still wins, so `TZ = "UTC"` keeps the old behaviour. Stack containers
  stay on UTC. An existing box picks this up when it's recreated (`fy box down && fy box up`).
