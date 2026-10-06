---
section: Fixed
bump: patch
---

- **With the VM firewall on, image pulls work from a registry the proxy decrypts.** A pull is the
  VM's own podman, not a container, so the CA that containers now get didn't reach it: a registry
  off `passthrough` failed with x509 "unknown authority". The VM's boot setup now also adds the
  proxy's CA to the VM's own trust store, replaces it when the CA changes, and takes it out when
  the firewall is turned off. It shares the one-time `fy machine stop && fy up` that giving
  containers the CA already asks for.
