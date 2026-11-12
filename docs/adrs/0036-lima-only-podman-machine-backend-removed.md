# ADR-0036 — Lima is foldyard's only backend: `podman machine` is removed

- **Status:** Accepted (2026-11-12) — **amends
  [ADR-0011](./0011-machine-backends-one-socket-contract.md)** (its second implementation is
  removed; the socket contract stands, with one implementation) and follows
  [ADR-0027](./0027-always-a-vm-native-backend-retired.md), which retired the third. Deprecated in
  0.7.0 (2026-10-27).
- **Sources:** `src/foldyard/machine_backend.py`, `config.retired_backend`, CHANGELOG 0.7.0
  (Deprecated). Related: [0009](./0009-monitoring-cooperative-enforcement-locked.md) (the wall),
  [0033](./0033-a-to-box-and-a-from-box-beside-the-checkout.md) (to-box and from-box).

## Context

Lima has been the default since 2026-09-04, before foldyard's first PyPI release, so every published
version gave a config with no `[machine].backend` a Lima VM; reaching `podman machine` took writing
`backend = "podman"`. The 0.7.0 deprecation said why staying there was a worse product, and nothing
built since has changed that:

- **One VM at a time.** On macOS podman machine runs one VM (podman#26281), so a second project
  can't come up without stopping the first.
- **No guest foldyard can provision.** Its CoreOS appliance takes no boot script, so it gets no VM
  firewall (the proxy is then only cooperative, ADR-0009), no guest monitor, no attributing
  forwarder, and its mounts are fixed at `podman machine init`, so no to-box or from-box.

Every one of those is a `BACKEND.name != "lima"` branch, and every new Lima feature had to add one,
with tests for a backend nobody was meant to choose.

## Decision

`machine_backend.LimaBackend` is the only backend. `PodmanBackend`, `get_backend` and the
one-VM-at-a-time guard are gone, and so is every branch on the backend's name. `Backend` stays as
the seam tests fake.

A config still naming another backend (`[machine].backend` or `MACHINE_BACKEND`, `podman`, `native`
or anything else but `lima`) is **refused before anything touches a VM**: `fy up`, `fy box up`,
`fy machine ensure` and `fy machine recreate` stop, preflight lists it, `fy doctor` shows it red and
`fy ls` shows the project's VM as `retired`. Going ahead on Lima instead would make an empty Lima VM
beside the old one's data, which is the trap a project's migration has to work around (its import
merges into whatever volumes the new VM already has).

Everything that doesn't touch the VM keeps working with such a config, so a project's migration can
use it: `fy worktree list`, `fy config diff`, `fy config adopt`. The key itself stays valid with the
value `lima`, so a config that names it needs no edit.

## Consequences

- **A podman machine user has to move before upgrading their VM.** foldyard can't do the move: which
  volumes and images matter is the project's business. The refusal says so and leaves the old VM and
  its volumes untouched. Starting over is setting `backend = "lima"` and adopting it.
- **`limactl` is required on every host.** It already was for anyone on the default; the error for a
  missing one now has one wording, not two.
- **The `[machine].backend` key has no choice left in it.** Keeping it valid costs one function;
  dropping it would break every config `fy init` wrote.
- **The doc trail for podman machine is history.** ADR-0001, 0010, 0011 and 0017 describe a world
  with two backends; their text stands as written, and this ADR is the pointer.

## Rejected alternatives

- **Treat a retired name as `lima`, with a warning.** The next `fy up` would create an empty Lima
  VM, and the warning would scroll past. A project's migration would then refuse to import into it.
- **Keep `PodmanBackend` for the migration.** A migration needs the `podman` CLI against the old
  machine (`podman machine start`, `podman volume export`), which the host has anyway; it doesn't
  need foldyard to drive it.
