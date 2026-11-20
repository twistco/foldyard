---
section: Fixed
bump: patch
---

- **The guest monitor runs on Fedora 45.** Lima 2.3.0's podman template is Fedora 45, whose 7.2
  kernel Tetragon v1.7.1 couldn't read (`parsing .BTF header: header contains non-zero trailer`), so
  it exited at every start and the monitor never observed. foldyard now installs Tetragon v1.8.0,
  and reads its policy table's new layout (a `DOMAIN` column), so `fy up` no longer waits ten
  minutes for a policy that had loaded.
- **With `[machine] attribution = "record"`, builds reach the proxy again on podman 6.** Podman 6
  gives each build step a network of its own, served by a pasta helper, and the forwarder refused
  every pasta helper's connections, so a build step's downloads failed. A helper whose namespace
  holds only build steps is now recorded as the build's; any other is still refused.
