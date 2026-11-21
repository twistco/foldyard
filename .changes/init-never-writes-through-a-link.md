---
section: Fixed
bump: patch
---

- **`fy init` and `fy skill install` never write through a link.** A box can plant links in the
  checkout, and both commands followed them on your computer:
  - A `foldyard.toml` that was a link pointing at nothing didn't count as existing, so `fy init`
    wrote the new config wherever the link pointed. The link is now replaced instead, and counts as
    existing, so `--force` is needed as for a file.
  - A `.claude` or `.claude/skills` that was a link made the skill install create files wherever it
    pointed, and with `--force` delete a `skills/<name>` directory there. The install now refuses
    such a path, and `--force` overwrites the skill's files instead of deleting the directory first.
