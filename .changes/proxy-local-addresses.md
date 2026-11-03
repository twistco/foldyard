---
section: Security
bump: minor
---

- **The egress proxy no longer reaches services on your computer or its network for the box.**
  The proxy runs on your computer, so whatever it dials is dialled from there. While enforcement
  was off or a learn window was open, a box could reach any service listening on your loopback
  (a dev server, a database, a local model server) by sending the request through the proxy, and
  with enforcement on, a granted name that resolved to `127.0.0.1` did the same. Now a dial to
  loopback, a private range, CGNAT (a tailnet) or link-local (a cloud's metadata service) is
  refused in every mode, whether the box named an address or a name that resolves there. Names are
  judged by the answer the connection actually uses, so DNS rebinding can't get past the check.
  **Only a grant naming the exact address lets one through** (`fy allow add 127.0.0.1:8123`), and
  only that command makes one: the Network Log's allow key, `fy allow learn` and `[proxy]
  recommend` never offer it. **If the box reached a LAN or VPN host by a name that resolves to a
  private address** (an internal registry, say), the refusal names the address to grant.
- **`fy verify` on your computer checks your loopback is out of reach** when `[machine] firewall`
  is on: a probe container tries a throwaway service on your loopback directly and through the
  proxy, and the service must see no connection.
