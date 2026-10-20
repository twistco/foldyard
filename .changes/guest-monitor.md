---
section: Added
bump: minor
---

- **Experimental: `[machine] monitor = "observe"` runs a guest monitor — Tetragon, a root eBPF
  collector, in the lima VM.** Experimental means off unless you turn it on, and its event format,
  `fy monitor --json` output and hook payload may change in a minor release while it settles. It
  records process starts, outbound TCP connections and access to selected credential
  files for every container and the VM itself, and blocks nothing. A pinned, checksummed release
  is downloaded on your computer, copied into the VM, and checked again as root before it is
  installed. The box's user can't stop it, change its policy or read its log. `fy doctor` shows
  whether it is observing. Off by default; changing it needs `fy machine stop && fy up`
  ([ADR-0032](docs/adrs/0032-a-root-collector-in-the-guest.md)).
