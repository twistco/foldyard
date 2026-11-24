---
section: Security
bump: patch
---

- **`fy monitor` no longer passes the box's text to your terminal as it is.** A program's name and
  arguments, and what the engine reports as a container's name, are chosen in the box: control
  sequences in them reached the terminal of whoever looked, and could redraw the listing or fake a
  line of it. They are now shown as escapes, in the crossing's log line and notification too.
