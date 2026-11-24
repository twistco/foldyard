---
section: Added
bump: minor
---

- **The guest monitor can record containers' file changes.** `[machine] monitor = "observe-files"`
  (experimental) records what `"observe"` does, plus writes, renames, deletions, `chmod`, truncation
  and hard links by containers under `/etc/`, `/usr/local/`, `/opt/` and `/root/`: the path and the
  process, never the content. Changes other than writes are recorded as attempts, refused or not.
