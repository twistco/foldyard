---
section: Fixed
bump: patch
---

- **With the VM firewall on, your stack's containers can reach HTTPS hosts outside `passthrough`.**
  The firewalled VM sends every container's traffic through the proxy, which decrypts every host
  not on `passthrough`, but only the dev box trusted the proxy's CA — so a service calling, say, a
  third-party API failed with a certificate error. The VM's boot setup now installs the CA and a
  podman default that mounts it into every container and sets `NODE_EXTRA_CA_CERTS`,
  `SSL_CERT_FILE`, `REQUESTS_CA_BUNDLE` and `GIT_SSL_CAINFO` (a value your image already sets
  wins). It changes the VM's boot setup, so an existing firewalled VM asks for
  `fy machine stop && fy up` once.
- **Turning the VM firewall off no longer leaves podman pointed at the proxy until the next
  restart.** The first boot without it kept the old proxy settings, so image pulls failed with
  "connection refused" until the VM was restarted again.
