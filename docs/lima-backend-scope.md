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

## Status: SPIKE — unit-tested, not yet exercised on a real `limactl`

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
