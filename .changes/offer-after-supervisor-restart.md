---
section: Fixed
bump: patch
---

- **A host you grant for the session when `fy up` or `fy box up` asks now keeps that grant.**
  Session and once grants end when the host supervisor restarts, and the launch verbs asked about
  recommended hosts *before* starting a new supervisor (after a foldyard update, say). An answer
  of "session" or "once" was then wiped a moment later by the supervisor that started, and a host
  whose session grant was about to end wasn't asked about at all, so the box hit refusals for a
  host you had just allowed. The launch now ends the old grants first, then asks.
