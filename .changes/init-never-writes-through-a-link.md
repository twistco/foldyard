---
section: Fixed
bump: patch
---

- **`fy init` never writes through a link.** A `foldyard.toml` that was a link pointing at nothing
  didn't count as existing, so `fy init` wrote the new config to wherever the link pointed. A box
  can plant such a link, so this could create or overwrite a file of its choosing on your computer.
  The link is now replaced instead, and counts as existing, so `--force` is needed as for a file.
