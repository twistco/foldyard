---
section: Fixed
bump: patch
---

- **`fy code` attaches on a computer with podman and no Docker.** The Dev Containers extension
  runs `docker` for every container call unless `dev.containers.dockerPath` says otherwise, so a
  podman-only computer got "Docker version 17.12.0 or later required" and an offer to install
  Docker Desktop instead of the box. The `fy code` window now sets that setting to the podman
  foldyard itself uses, on every run, and `fy code` installs the Dev Containers extension when
  it's missing (into `~/.vscode/extensions`, so your everyday VS Code gets it too).
