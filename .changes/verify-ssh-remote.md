---
section: Fixed
bump: patch
---

- **`fy verify` passes an SSH origin behind the VM firewall instead of failing it as
  unreachable.** With `[machine] firewall = true`, a push to a `git@host:path` or `ssh://` origin
  dies at the connection, since ssh doesn't use the proxy. Verify could only say "git remote
  UNREACHABLE … push refusal UNPROVEN", so every firewall user with an SSH origin got a failing
  verify. It now proves the refusal directly: for every push URL, the host and port ssh would
  dial (`ssh -G`) resolve, the proxy answers, and a direct connection to that port is refused. The
  row reads `SSH push impossible: the VM firewall refuses github.com:22 (Connection refused)`. A
  connection that fails any other way (a timeout, "No route to host") is a warning instead: the
  wall answers with a reset, so anything else could just as well be a network turning port 22
  away, and isn't credited to the wall. A
  `ProxyCommand` or `ProxyJump`, a name that doesn't resolve, a connection that succeeds, or no
  firewall still fails the row. The unreachable row now also names ssh's own error rather than
  git's closing "…and the repository exists."
