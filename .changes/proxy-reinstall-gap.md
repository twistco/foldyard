---
section: Fixed
bump: patch
---

- **Reinstalling foldyard no longer restarts a running egress proxy.** `uv tool install --force`
  removes the environment the proxy's `mitmdump` is in and makes it again. A supervisor that looked
  in between took that for a changed command and stopped the proxy: every connection through it was
  cut, boxes had no egress for a couple of seconds, and a notification said it hadn't started. The
  running proxy is now kept. `fy host restart` is still what moves it to the new code.
