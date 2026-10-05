---
section: Fixed
bump: patch
---

- **A request two switches both claim is answered with the overlap, not the provider's 401.**
  When two switches that inject on the same host and path are on together, the proxy injects
  neither, which is the safe choice, but the box's request still went out with its placeholder
  credential and came back as the provider's "Bad credentials", which looks like a broken
  token. The proxy now answers such a request itself, without sending it, with a `502` whose
  JSON `message` is the same text `fy mode` shows: both switches, and the `fy mode` command to
  run on your computer to turn one off. It matches exactly where the held-back rules would have
  injected (host and path prefix, decrypted HTTPS only), and the Network Log marks the row
  `credential overlap`.
