---
section: Added
bump: minor
---

- **The guest monitor can record containers' file writes.** `[machine] monitor = "observe-files"`
  (experimental) records what `"observe"` does, plus writes by containers under `/etc/`,
  `/usr/local/`, `/opt/` and `/root/`: the path and the process, never the content.
