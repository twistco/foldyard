# Lima backend — scope, contract, and the trickiest bits

Referenced by `machine_backend.py` and `config.machine_backend()`. This is the
scope/risk write-up those point at.

## Why more backends at all

`podman machine` on macOS runs **one VM at a time** — the applehv/libkrun providers gate on
`RequireExclusiveActive` (see [podman-multi-vm-issue-26281.md](./podman-multi-vm-issue-26281.md)).
So two foldyard projects can't have live machines simultaneously: `machine._start()` detects a
running machine and refuses, telling you to stop it or switch backends. **Lima runs VMs
concurrently**, so per-project machines coexist — no stop/swap dance, and an open
`box shell`/`code` session in one project survives while you work in another.

## The contract (backend-blind downstream)

Backends expose ONE thing: a **libpod socket**. VM-backed backends provide that socket from a
per-project VM; the explicit native backend uses the host's rootless podman socket directly.
Everything downstream (`stack.py`'s `CONTAINER_HOST` export, compose, box, worktrees) is
backend-blind — `machine.socket()` returns a podman URI either way, no `--connection` juggling.
Selection: `MACHINE_BACKEND` env → `[machine].backend` toml → `"lima"` default (ADR-0011's
2026-08-29 amendment; it was `"podman"` originally). Native is never
auto-selected; Linux/WSL2 users opt in with `backend = "native"` when they want convenience/CI over
the VM boundary.

- **`PodmanBackend`** (no extra deps): wraps `podman machine`; `supports_concurrent()` →
  `False` (the one-VM guard in `machine._start()` is active). The portable floor.
- **`LimaBackend`** (the default; `[machine].backend = "lima"`): wraps `limactl` + Lima's **podman
  template** (a real podman service in each VM, libpod socket forwarded to
  `<instanceDir>/sock/podman.sock`); `supports_concurrent()` → `True` (guard skipped). The VM is
  generated from the template with a `--set` expression that pins CPU/memory/disk and **replaces**
  the template mounts with only foldyard's isolation mounts (repo + worktrees root).
- **`NativeBackend`** (opt-in, `[machine].backend = "native"`): no VM lifecycle; foldyard uses the
  host's rootless podman socket. Useful for Linux/WSL2 and cheap CI coverage, but not equivalent to
  the VM-backed sandbox because containers share the host kernel.

## Status update (2026-09-07): most of the SPIKE is now settled on real hardware

Measured on an M3 Max / macOS 26.6.1 / Lima 2.1.3, with four projects live on this backend
(`claude-code-log`, `danieldemmel.me-next`, `garmin`, `homelab` — all with `wall = on`). Against
the "trickiest bits" checklist below:

1. **Forwarded socket path — CONFIRMED.** `<instanceDir>/sock/podman.sock` is documented by
   upstream's own podman template (`limactl template copy template:podman` prints it as the
   `CONTAINER_HOST` recipe). No longer an assumption.
2. **Guest socket** — unchanged assumption, still rootless `/run/user/<uid>/podman/podman.sock`.
3. **Mount replacement — CONFIRMED.** Every live instance carries exactly foldyard's isolation
   set (one repo mount, `writable: true`, host path == guest path). Lima did not re-add the
   template defaults.
4. **Concurrency — CONFIRMED.** Three instances coexist.

Two things the same pass found that are *not* on the checklist:

- **`vmType` was never pinned** — Lima's `DefaultDriver()` was silently choosing it. Closed by
  `[machine].vmtype` (config.machine_vmtype → `LimaBackend.resolve_vmtype`), which resolves from
  `limactl info` rather than the OS. `mountType` is still unpinned and inherits Lima's per-driver
  default: virtiofs under vz, **9p under qemu** — which matters the moment a Linux host uses this
  backend. See [firecracker-and-microvm-backends.md](./firecracker-and-microvm-backends.md).
- **`template://podman` is deprecated** since Lima v2.0 (`template:podman` is the spelling now).
  `LimaBackend.create()` still uses the old form and emits a warning on every creation.

The Lima paths are therefore no longer a spike in the "never run for real" sense. What remains
untested is `krunkit` as an alternative `vmType` — see the microVM note linked above.

## Historical: SPIKE — unit-tested, not yet exercised on a real `limactl`

`test_machine_backend.py` covers the backends with **mocked** CLIs (selection, concurrency
flags, guest sockets, native socket resolution, Lima JSON-line parsing, the `--set` override). The
Lima paths follow Lima's documented podman-template behaviour but have **not** run against a real
`limactl` (none in CI / the dev box). Verify on a Mac with `brew install lima` before relying on
them.

## Trickiest bits to confirm on a real Mac

1. **Forwarded socket path/stability** — `LimaBackend.socket()` assumes Lima forwards the guest
   libpod socket to `<instanceDir>/sock/podman.sock`. Confirm the path and that the forward
   survives across Lima versions + VM restarts.
2. **Guest socket** — rootless libpod at `/run/user/<uid>/podman/podman.sock` inside the VM.
3. **Mount replacement** — the `--set` mutates the template's `mounts` to foldyard's isolation
   set; verify Lima doesn't silently re-add template defaults (drift would break the isolation
   property `machine.mounts()` asserts).
4. **Concurrency in practice** — two live per-project Lima VMs + their forwarded sockets at once.

## Bonus: Lima also unlocks bypass-proof egress capture

Because Lima lets you **own VM provisioning** (root `provision:` scripts, declarative), it makes
the in-VM nftables forcing wall ([ADR-0009](./adrs/0009-monitoring-cooperative-enforcement-locked.md)) easy —
the thing podman's immutable CoreOS appliance makes hard. See
[lima-wall-machine-integration.md](./lima-wall-machine-integration.md) and the runnable
[lima-network-forcing-kit/](./lima-network-forcing-kit/). This is a *consequence* of adopting
Lima for concurrency, not a separate reason — but it's a real bonus for the capture roadmap.
