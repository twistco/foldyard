---
section: Fixed
bump: patch
---

- **When foldyard can't mint a credential, the box is told why instead of getting the
  upstream's 401.** A failed token mint used to forward the request with the box's placeholder
  credential, so the agent saw the provider's own "Bad credentials" and guessed at causes — while
  the real reason (e.g. a GitHub App permission the minter refused) sat in the host log. The
  egress proxy now answers such a request itself with a `502` whose JSON `message` names the host
  and the minter's reason and points at `fy host logs`, without sending the request; a 401 whose
  re-mint fails is replaced by the same kind of 502, worded for a request that did go out. The
  Network Log marks the row `mint failed`. The reason is the minter's diagnosis from its stderr,
  with token-shaped runs redacted; its command and its stdout (where a token comes back) never
  reach the box.
