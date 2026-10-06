---
section: Fixed
bump: patch
---

- **Turning the VM firewall off now really takes podman off the proxy.** The firewall's proxy
  settings for the VM's user were removed only for users with an id of 1000 or more, and the VM's
  user takes your computer's (501 on macOS), so the first boot without the firewall still sent
  image pulls to a proxy that was no longer running.
