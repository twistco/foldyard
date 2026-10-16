---
section: Added
bump: minor
---

- **`[box].packages` installs system packages once, not on every new box.** List them
  (`packages = ["nodejs", "just"]`) and foldyard bakes them into a cached layer on top of the box
  image, rebuilt only when the list or the base image changes (or by `fy box build`). Until now
  the only way to add a package to the packaged box was a `[[box.tools]]` step, which reinstalled
  it, through the egress wall, every time a box was created. The layer uses whatever package
  manager the image has (apt, apk, dnf, microdnf, yum, zypper, pacman), so it works on your own
  `[box].image` too. A box that is already running keeps its old image until
  `fy box down && fy box up`.
