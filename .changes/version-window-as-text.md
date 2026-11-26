---
section: Security
bump: patch
---

- **The version window in `foldyard.toml` is printed as text.** `min_foldyard_version`,
  `recommended_foldyard_version` and the `foldyard_version_reasons` ledger reach your terminal
  through the version gate. Any terminal control characters in them are now shown as visible escapes
  instead of being sent to the terminal. The file can be written from the box, so a value could
  otherwise write to your clipboard or erase the gate's own message. A version keeps ordering as it
  did, so a floor still holds.
