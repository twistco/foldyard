---
section: Security
bump: patch
---

- **`fy init` no longer writes through a link at `.gitignore` or `foldyard.local.toml.example`.**
  The checkout may be one a box can write, and a link it planted there to a file on your computer
  had `fy init` add foldyard's ignore block to that file, write the template into it, or create it.
  Both are now written by replacing the name, and a rewritten `.gitignore` keeps its permissions. A
  link at either is left as it is, and `fy init` says so: `fy init` and `fy init --private` alike
  (which failed on one) list what to ignore instead, since git doesn't read a `.gitignore` that's a
  link, and doctor no longer warns about one. A `.gitignore` that isn't UTF-8 no longer stops
  `fy init` with a traceback, and one it can't read is named as the file it couldn't write, not
  `foldyard.toml`.
- **`fy up` accepts a project `.env`, an `env_file:` or a `label_file:` that is `/dev/null`.** They
  were refused as paths outside the checkout, though people link a `.env` there so compose loads
  nothing, and there's nothing of yours to read. Anything else outside the checkout is still
  refused, another device included, and so is a path through a process's own descriptors
  (`/dev/stdout`, `/proc/self/fd/1`), which can read as `/dev/null` to foldyard but not to compose.
