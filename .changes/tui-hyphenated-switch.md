---
section: Fixed
bump: patch
---

- **The TUI's mode buttons work for a switch with a hyphen in its name.** Pressing a level button
  for a switch such as `sanity-editor` was refused as "sanity mode 'editor-off'": the button's id
  was split at its first hyphen to find the switch and level. Each button now carries both itself.
  `fy mode sanity-editor=off` was never affected.
