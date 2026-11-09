---
section: Removed
bump: minor
---

- **foldyard drives only podman: the docker engine is gone, in CI too.** `[engine].cli` and
  `FOLDYARD_ENGINE` accept only `"podman"`, and the compose provider must be a podman-compose.
  docker was kept as a fallback for CI, because GitHub's runners had no podman and couldn't boot a
  VM. Neither is true now: `ubuntu-26.04` ships a rootless podman 5.7. Keeping the fallback meant
  CI tested a compose provider and a builder that no developer runs, and a docker client sends the
  VM your registry logins. **A CI job that sets `FOLDYARD_ENGINE: docker` must move to podman
  before taking this release.** `fy docs stack`, "In CI", has the recipe. A box image with no
  podman needs it added. foldyard still exports the VM's socket as `DOCKER_HOST` alongside
  `CONTAINER_HOST`, so a docker CLI of your own in a box still works.
