---
section: Fixed
bump: patch
---

- **`fy up` on a stopped stack after a config change no longer loses containers.** The bundled
  podman-compose removes every dependent of a service it recreates, but only re-creates the
  dependents that were running — so a stopped one vanished, and anything that depends on it
  failed with "is not a valid container, cannot be used as a dependency". foldyard now removes
  those containers itself before `up` (and before a mode reconcile), so compose creates them
  like any missing container. Named volumes are kept.
