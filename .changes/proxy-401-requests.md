---
section: Fixed
bump: patch
---

- **The egress proxy re-issues a request whose token was refused, as it always meant to.** On a 401
  from a host it injects into, it mints a fresh token and sends the request again, through
  `requests`, which no release installed: every re-issue failed with `ModuleNotFoundError`, the box
  got the 401, and the request was missing from the network log. `requests` is now a dependency (on
  your computer only, the box's install leaves it out), and a re-issue that can't run still hands
  the 401 back and logs it.
