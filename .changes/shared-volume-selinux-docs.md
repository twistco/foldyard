---
section: Added
bump: patch
---

- **The docs and both bundled skills explain why a cache prune can fail with `Permission
  denied` in a shared volume.** On the VM's SELinux guest, a file copied into a volume can keep
  the private label of the container that copied it, and once that container is recreated no
  other container can delete it. `fy docs configuration` (under `[reclaim]`) gives the fix. To
  refresh a copy installed in your repo, run `fy skill install --force foldyard` and
  `fy skill install --force bootstrap-devbox`.
