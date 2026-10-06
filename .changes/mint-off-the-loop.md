---
section: Fixed
bump: patch
---

- **A slow or failing credential mint no longer freezes the box's other traffic.** The egress
  proxy ran each token mint on the one event loop every connection shares, so a token service
  that hung held up all of the box's proxied requests (its agent's own API calls, package
  installs, git) for up to 30 seconds, and again on every retry of the failing request, since
  clients retry a 502. A mint now runs off that loop: only the requests that need that
  credential wait for it, and the ones arriving meanwhile share the one mint. A failed mint is
  remembered for 15 seconds, so retries get the same `mint failed` 502 at once instead of
  re-running the token service, and the host log gets one line per attempt rather than one per
  retried request. A secret pasted into `host.env` is picked up straight away, without waiting
  for the 15 seconds to pass.
