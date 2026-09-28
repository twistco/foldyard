---
section: Fixed
bump: minor
---

- **A checkout whose `foldyard.toml` you haven't adopted says so.** Your computer reads no config
  for it until you adopt, and commands used to report that empty config as if it were yours:
  "`[project].compose` is unset", or a project named after the directory. Now every command
  says the config isn't adopted, and `fy shellenv` and every command that acts on the stack or
  the box (`fy build`, `fy ps`, `fy down`, `fy reclaim`, `fy verify`, …) stop instead of acting on
  a guess — also with an inherited `DOCKER_HOST`. `fy up` still asks to adopt.
