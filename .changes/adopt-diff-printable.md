---
section: Security
bump: patch
---

- **The `foldyard.toml` diff you review before adopting no longer passes the file's text to your
  terminal as it is.** The box can write that file, and control sequences in it reached your
  terminal at the very prompt that decides the adoption, on `fy up`, `fy box up` and `fy host`, in
  `fy config diff` and in `fy tui`: they could erase or fake a line of the diff, or reorder one with
  a bidi override, which valid TOML allows. They are now shown as escapes (`\x1b`, `\x9b`), so you
  can see that something odd is there.
- **A `foldyard.toml` or `foldyard.local.toml` that doesn't parse is no longer adopted, or reviewed
  as a comment edit.** TOML refuses a control character even in a comment, and foldyard reads a file
  that doesn't parse as empty. So one byte in a `# note` could drop your whole
  `foldyard.local.toml`, the narrowing in it too, while the review said "only blank/comment lines
  changed". The review now says the file doesn't parse, and adopting it is refused until it does. If
  a copy adopted before this release doesn't parse, a fix that makes it parse is shown as the change
  it is, not as a comment edit. The worktree name in these messages is shown as escapes too.
