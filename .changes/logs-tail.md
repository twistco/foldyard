---
section: Fixed
bump: patch
---

- **`fy logs` works on the podman engine.** It asked compose for the last 50 lines with `-n 50`,
  which docker compose reads as `--tail 50` but the bundled podman-compose reads as its
  value-less `--names` switch followed by a service called `50`, so every `fy logs [svc]` on
  podman failed with "missing services [50]". It now sends `--tail 50`, which both read the same.
  A new test hands every compose command foldyard sends to podman-compose's own argument parser,
  so a flag the two providers read differently fails the suite instead of a user's command.
