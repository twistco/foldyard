---
section: Security
bump: patch
---

- **A mode change removes only the stack containers it meant to.** After `fy mode`, a switch in
  `fy tui`, or an emergency level expiring, foldyard removes the project's containers whose service
  has left your compose config, and the mode services whose level dropped. It read each container's
  service label as a line of text. Anyone on the engine can set that label, the box included, and a
  label holding a line break could add other container names, or `--all`, to the removal. The labels
  are now read as structured data, and only the engine's own container ids are removed.
- **`fy tui` no longer exits over a switch's name.** An `[[inject]]` switch named like `api.v2`, or
  with a `[` or a space in it, is accepted config, but `fy tui` built its buttons' ids from the name
  and exited on start. A switch named `fix` took it down when its button was pressed. The name is
  now kept as the button's data, never in its id.
