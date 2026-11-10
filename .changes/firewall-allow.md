---
section: Added
bump: minor
---

- **`[machine] firewall_allow` opens one TCP port to one address through the VM firewall, for a
  client that can't use an HTTP proxy.** cloud-sql-proxy, for one, authenticates over HTTPS
  through the proxy and then dials its database instance on TCP 3307 directly, which the firewall
  refused, so staging-database access couldn't work with `firewall = true`. Each entry is
  `{ to, port, why }`: an IP or CIDR (never a host name), one port, and the reason, which
  `fy verify` lists. An opening can't reach your computer or network: private, loopback,
  link-local, CGNAT, multicast and reserved ranges are refused, and so is anything wider than a
  `/24` (`/64` for IPv6), and port 53, since DNS stays with local resolvers. It is read from the adopted config and applied at boot, so a change
  takes `fy machine stop && fy up`; with `host_firewall`, install the host rules again too.
  Traffic through an opening bypasses the proxy, its log and the allowlist.
