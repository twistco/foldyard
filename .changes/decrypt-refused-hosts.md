---
section: Changed
bump: patch
---

- **A refused HTTPS request now shows its method, path and User-Agent in the Network Log, and a
  refused host is never connected to.** The proxy used to refuse an ungranted host at `CONNECT`,
  before TLS, so the row could only say `CONNECT host:443`. It now answers the `CONNECT` itself,
  decrypts the tunnel and returns the `403` to the request inside. No connection to the refused
  host is ever opened: the proxy also refuses, at the point where every upstream connection is
  made, any connection to a host nothing grants. Previously a separate check on each path made
  that guarantee, so it now holds in one place. Unchanged: a port other than `:443` and an image
  build are still refused at `CONNECT`. One visible difference: a client in the box that keeps
  its own trust store (it doesn't trust the proxy's CA) now gets a certificate error for a
  refused host instead of the readable `403`. The log still records it as a blocked `CONNECT`.
