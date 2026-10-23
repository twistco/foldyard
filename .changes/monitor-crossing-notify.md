---
section: Added
bump: minor
---

- **A crossing out of a container is told on your computer.** The first time a process crosses
  out of a container in a monitored VM boot, the supervisor logs it and posts a desktop
  notification naming the container, once per crossing (a restart doesn't repeat it). Off with
  `[host] notifications = false`.
