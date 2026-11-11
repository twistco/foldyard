---
section: Security
bump: patch
---

- **Removing a grant for a local address now closes the connections it let through, with the
  allowlist observing too.** An SSH session to a machine on your network, through
  `fy allow add <address>:22`, used to outlive `fy allow remove` unless the allowlist was enforcing.
  Such a tunnel, neither TLS nor HTTP, also gets a Network Log row now (`tcp tunnel`); a granted one
  left no trace before.
