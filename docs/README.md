# foldyard docs — the map

Start with the [README](../README.md): what foldyard is and why. Then pick by what you need.

## The manual

For using foldyard. These pages also ship with the install — `fy docs` prints them offline.

- [glossary.md](./glossary.md) — every term these docs use, in plain words. Read this first if a
  word is unfamiliar (box, mode, switch, allowlist, adopted config…).
- [quickstart.md](./quickstart.md) — install, `fy init`, the box, an agent, your stack, and how
  to upgrade.
- [configuration.md](./configuration.md) — every `foldyard.toml` key, `foldyard.local.toml`
  overrides, environment variables, the adopted config, and what lives in `~/.foldyard/`.
- [modes.md](./modes.md) — switching credentials on and off: switches and levels, time limits,
  keyless agents, and what enforces them on your computer.
- [networking.md](./networking.md) — the egress proxy, the traffic log, the allowlist, and the
  VM and host firewalls.
- [security.md](./security.md) — the threat model, what the boundary does and doesn't defend,
  and what `fy verify` checks.
- [compose-overlays.md](./compose-overlays.md) — `[[overlay]]` in depth: extra compose files
  that apply while a switch is at a given level.
- [testing-modes.md](./testing-modes.md) — a zero-secret test rig (`fakecred` and a skewable
  clock) for trying modes, time limits and failure handling live.

## The design record

Why foldyard is built the way it is.

- [adrs/](./adrs/) — one file per decision, with its status and consequences.
- [prior-art.md](./prior-art.md) — the landscape of similar tools and where foldyard fits.
- [prior-art/](./prior-art/) — deep-dives on the closest tools (gondolin, vhrn), read against
  foldyard, and a side-by-side comparison.
- [archive/](./archive/) — finished studies and incident records, kept for reference:
  - [verify-false-pass.md](./archive/verify-false-pass.md) — how `fy verify` once printed ALL
    PASS while checking nothing, and the control that fixed it.
  - [mode-state-consolidation.md](./archive/mode-state-consolidation.md) — the inventory of
    state tiers behind the reconciler (`fy state`).
  - [firecracker-and-microvm-backends.md](./archive/firecracker-and-microvm-backends.md) — why
    Firecracker doesn't fit (no filesystem sharing, by design).
  - [lima-network-forcing-kit/](./archive/lima-network-forcing-kit/) — the runnable proof kit
    that showed a VM firewall can force all egress through a proxy.

## Contributing to foldyard

Start at [DEVELOPMENT.md](../DEVELOPMENT.md): the module map, test tiers, CI and conventions.

- [releasing.md](./releasing.md) — how a release is cut (tag-driven).
- [linux-support.md](./linux-support.md) — what has been run on a Linux host and inside WSL2,
  and what hasn't yet.
- [isolation-layers.md](./isolation-layers.md) — which layer carries the boundary on each host
  (macOS, Linux, WSL2), with measurements.
- [lima-backend-scope.md](./lima-backend-scope.md) — the VM-backend contract (lima and podman)
  and the tricky parts of the Lima backend.
- [lima-wall-machine-integration.md](./lima-wall-machine-integration.md) — how the box reaches
  the proxy on the host, and the design of the VM and host firewalls.
- [nested-virt.md](./nested-virt.md) — a nested-KVM rig for testing the paths that only run on
  the host.

**History:** foldyard was extracted from Twist's monorepo as a fresh start
([ADR-0013](./adrs/0013-in-repo-carve-out-until-extraction.md)). The pre-extraction run-logs,
plans and design boards stay there; the ADRs are the public record of the reasoning.
