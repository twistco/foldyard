---
section: Added
bump: minor
---

- **`[machine] firewall_allow` opens one TCP port to one address through the VM firewall, for a
  client that can't use an HTTP proxy.** cloud-sql-proxy, for one, authenticates over HTTPS
  through the proxy and then dials its database instance on TCP 3307 directly, which the firewall
  refused, so staging-database access couldn't work with `firewall = true`. Each entry is
  `{ to, port, why }`: one IP address (never a range or a host name), one port, and the reason,
  which `fy verify` lists. An opening can't name an address the proxy treats as local (private,
  loopback, link-local, CGNAT, multicast, reserved, or a v4 one wrapped in v6), or port 53, since
  DNS stays with local resolvers. Your computer's own public address is the one case foldyard
  can't detect. It is read from the adopted config and applied at boot, so a change takes
  `fy machine stop && fy up`; with `host_firewall`, install the host rules again too. Traffic
  through an opening bypasses the proxy, its log and the allowlist. The firewall's refusals now
  carry a UTC time, and `fy verify` lists them, warning when a port an opening names was refused
  at another address.
