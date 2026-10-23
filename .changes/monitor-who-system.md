---
section: Changed
bump: minor
---

- **The guest monitor names what isn't a container: `podman`, `build` and the VM itself.** Most
  `unattributed` events were never containers: on a busy overnight run, 1.28 M of 1.49 M were
  podman's own processes (conmon, pasta, crun, netavark) and image builds. They are now `podman`
  and `build`, and `vm` also covers the VM's sandboxed systemd services. Each label comes from
  what the VM's kernel reports and nothing in a container can fake, never from a process's name.
  It applies to stored history: `fy monitor --since … --until …` re-joins it. The first read after
  upgrading indexes the new facts from the store (about 20 s on a 330 MB store).
- **A monitor hook without `who` no longer gets `vm` and `podman` events,** and every batch now
  counts what the hook's filters passed over, by label (`passed`), so nothing is left out
  silently. Set `skip_unless_asked` at the top of `monitor-hooks.toml` to change the default for
  every hook without `who`, or name a label in a hook's `who` to get it. A process that steps out
  of a container into the VM's or podman's namespace is marked `crossed`, labelled with the
  container it came from, and sent to every hook whatever its `who`.
- **The guest monitor no longer gives a container podman's tools that ran on its namespace number
  just before its start or just after its exit.** netavark, nft and crun run for an instant in a
  namespace of their own that takes the number a container is about to take or has just left (on
  one boot, 6,405 events labelled as containers' that weren't). A container's events now run from
  its init's start to its exit, plus its own processes killed after that.
