# foldyard docs — the map

Start with the [package README](../README.md) (what foldyard is and why). Then, by audience:

**Using foldyard:**

- [quickstart.md](./quickstart.md) — install → `init` → box → agent → stack, following the
  config the `init` template writes
- [configuration.md](./configuration.md) — every `foldyard.toml` key, `foldyard.local.toml`
  overrides, env precedence, the `~/.foldyard/` state layout
- [modes.md](./modes.md) — posture axes and rungs, TTLs, keyless agents, host-side enforcement
- [networking.md](./networking.md) — the egress proxy, capture, allowlisting, and the wall
- [security.md](./security.md) — the threat model and what `fy verify` proves
- [compose-overlays.md](./compose-overlays.md) — the `[[overlay]]` posture-overlay config,
  in depth
- plugins.md — writing your own plugin (coming with the consumer-repo plugin work)

**Why it's built this way:**

- [adrs/](./adrs/) — one file per decision, with status and consequences
- [prior-art.md](./prior-art.md) — the landscape, positioning, and the nono/agent-sandbox
  analysis

**Developing foldyard** (library internals and test rigs — start at
[DEVELOPMENT.md](../DEVELOPMENT.md)):

- [nested-virt.md](./nested-virt.md) — validating host-only paths headlessly (the nested-KVM
  rig)
- [lima-backend-scope.md](./lima-backend-scope.md) — the machine-backend contract
  (podman | lima | native)
- [lima-wall-machine-integration.md](./lima-wall-machine-integration.md) — the wall's design
  and per-project port bands
- [lima-network-forcing-kit/](./lima-network-forcing-kit/) — the runnable wall proof kit
- [podman-multi-vm-issue-26281.md](./podman-multi-vm-issue-26281.md) — why the podman backend
  runs one VM at a time
- [firecracker-and-microvm-backends.md](./firecracker-and-microvm-backends.md) — research note:
  where Firecracker could (and can't) fit, and the libkrun/krunkit alternative
- [e2b-self-host-research.md](./e2b-self-host-research.md) — research note: what self-hosting
  E2B actually costs, its two-tier static env trace, and whether the stack could run in foldyard
- [isolation-layers.md](./isolation-layers.md) — which layer carries the boundary on each host
  (macOS · Linux · WSL2), with the hypervisor stacks drawn out
- [verify-false-pass.md](./verify-false-pass.md) — how `fy verify` could print ALL PASS while
  asserting nothing, and the control that fixed it
- [testing-modes.md](./testing-modes.md) — the zero-secret `fakecred` rig for exercising the
  posture machinery live (ships in the wheel)
- [mode-state-consolidation.md](./mode-state-consolidation.md) — the state-tier inventory and the
  reconcile SCOPE design, distilled from every mode/lifecycle bug in the history

**Archaeology:** foldyard was extracted from Twist's monorepo as a squash-start
([ADR-0013](./adrs/0013-in-repo-carve-out-until-extraction.md)). The pre-extraction run-logs,
plans, design boards and deck stay there; the ADRs are the public record of the "why".
