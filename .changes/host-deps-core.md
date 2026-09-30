---
section: Changed
bump: patch
---

- **`uv tool install foldyard` is now the complete install on your computer — no `[host]` extra
  needed.** mitmproxy (the egress proxy) and PyJWT (the GitHub App minter) are ordinary
  dependencies now, so the install you get by default is the one that works, and an upgrade
  can no longer quietly leave the proxy out. The box, which only routes through the proxy, is
  installed by `fy box up` with both removed, as before. `"foldyard[host]"` still installs (the
  extra is kept, empty), so existing instructions and installs keep working.
