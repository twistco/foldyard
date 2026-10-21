---
section: Fixed
bump: patch
---

- **On a host that exports `HTTPS_PROXY`, a walled Lima VM's image pulls no longer fail.** Lima
  copies the host's proxy variables into the guest at each start (`propagateProxyEnv`), where
  they overrode the VM firewall's own proxy: the guest's podman pulled through the host's proxy,
  and the firewall refused every pull (`proxyconnect tcp: dial tcp 192.168.5.2:…: connection
  refused`). With `[machine] firewall` on, foldyard now records `propagateProxyEnv: false`
  before the VM boots; without the firewall the host's proxy stays the guest's. A VM already
  running with the copied proxy is warned about once, and needs `fy machine stop && fy up`.
