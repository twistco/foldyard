---
section: Changed
bump: patch
---

- **A refused request in the Network Log now shows what was asked.** A blocked row used to read
  only "⛔ refused by the allowlist". Now it shows the method and target: `CONNECT host:port` for
  HTTPS (refused before TLS, so the path is never seen), and the method and path for plain HTTP.
  The text reads "blocked — not on the allowlist". A request refused because its Host header
  named a different host than the one it was sent to now says so, instead of blaming the
  allowlist. It is also no longer offered as a host to grant, since a grant would not let it
  through. The egress log's blocked rows carry `method`, `path` and `mismatch` to match.
