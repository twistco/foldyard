# Archive

Finished studies and incident records. Each page opens with what it was, its status and what
supersedes it; the current design lives in the docs one level up and in the [ADRs](../adrs/).

- [isolation-layers-sessions.md](./isolation-layers-sessions.md) — the dated macOS and GCP-rig
  measurements (libkrun, gVisor, the host firewall) behind [isolation-layers.md](../isolation-layers.md).
- [verify-false-pass.md](./verify-false-pass.md) — four ways `fy verify` went green (or red)
  without checking what it claimed, and the positive-control rule that fixed them.
- [mode-state-consolidation.md](./mode-state-consolidation.md) — the 2026-07 state-bug audit and
  its five proposals, all landed as `reconcile.py` and the capability probes.
- [firecracker-and-microvm-backends.md](./firecracker-and-microvm-backends.md) — why Firecracker
  can't be a foldyard backend (no filesystem sharing, by design).
- [lima-network-forcing-kit/](./lima-network-forcing-kit/) — the runnable proof that became the VM
  firewall, still usable as a red-team rig; [transparent/](./lima-network-forcing-kit/transparent/)
  is the rootful variant that wasn't adopted.
