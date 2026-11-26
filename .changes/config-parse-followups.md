---
section: Security
bump: patch
---

- **The adoption diff colours and counts each line as the change it is.** `fy config diff`, the
  launch prompt and the adopt dialog in `fy tui` styled a line by what its text said. A changed line
  whose text starts with `--` or `++` (a key like `--x`, or a line in a multi-line string, in a
  `foldyard.toml` the box can write) reads like a file header once its `-` or `+` is in front. It
  was then shown dimmed as a header and left out of the count, so a real change could be summed up
  as "only blank/comment lines changed". The text of the diff is unchanged.
- **A `foldyard.toml` nested too deep to read is treated like any other that doesn't parse, and the
  same everywhere.** About 500 nested `[` made Python's TOML reader fail in a way foldyard didn't
  expect, so reading the checkout's config stopped with a traceback instead of reading the file as
  empty, and so did adding a package or a recommended host to it and `fy init --private`. Where the
  limit fell also depended on what was reading the file: a `foldyard.local.toml` could be adopted at
  the launch prompt, then read as empty by what runs on your computer, dropping the narrowings it
  holds. Every reader now gives one answer for the same file. `fy --version` on an editable install
  no longer fails on a `pyproject.toml` nested that deep either.
