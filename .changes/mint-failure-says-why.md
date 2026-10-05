---
section: Fixed
bump: patch
---

- **When foldyard can't mint a credential, the box is told why instead of getting the
  upstream's 401.** A failed token mint used to forward the request with the box's placeholder
  credential, so the agent saw the provider's own "Bad credentials" and guessed at causes — while
  the real reason (e.g. a GitHub App permission the minter refused) sat in the host log. The
  egress proxy now answers such a request itself with a `502` whose JSON `message` names the host
  and the minter's reason and points at `fy host logs`; the same answer replaces a 401 whose
  re-mint fails. The request is not sent, the Network Log marks the row `mint failed`, and the
  minter's command and output never reach the box.
