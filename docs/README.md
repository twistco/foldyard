# foldyard docs — the map

Start with the [README](../README.md): what foldyard is and why. Then pick by what you need.

![foldyard's quick start, recorded against a real VM: foldyard init, review and adopt the config, grant the recommended egress, fy box up, fy verify ALL PASS in the box, then the box edits foldyard.toml and your computer shows the diff and asks before it runs it](https://github.com/twistco/foldyard/releases/latest/download/foldyard-demo.gif)

## The manual

For using foldyard. These pages also ship with the install — `fy docs` prints them offline.

- [glossary.md](./glossary.md) — every term these docs use, in plain words. Read this first if a
  word is unfamiliar (box, mode, switch, allowlist, adopted config…).
- [quickstart.md](./quickstart.md) — install, `fy init`, the box, an agent, your stack, and how
  to upgrade.
- [workshop.md](./workshop.md) — running a foldyard workshop: what attendees set up before the
  day, per platform, the running order on the day, and the failures to expect.
- [base-vm.md](./base-vm.md) — `fy prereq --fetch` and the base VM: new projects' VMs
  as clones (when they fit the base; otherwise from the template), and the big downloads done
  once per computer.
- [configuration.md](./configuration.md) — every `foldyard.toml` key, `foldyard.local.toml`
  overrides, environment variables, the adopted config, and what lives in `~/.foldyard/`.
- [modes.md](./modes.md) — switching credentials on and off: switches and levels, time limits,
  keyless agents, and what enforces them on your computer.
- [agents.md](./agents.md) — `fy agent`: agents in tmux sessions that outlive their terminal,
  listed across your projects and reattached by session id.
- [networking.md](./networking.md) — the egress proxy, the traffic log, the allowlist, and the
  VM and host firewalls.
- [github-app.md](./github-app.md) — give the box scoped GitHub access with a GitHub App:
  register, install, the `[[inject]]` row, checking it from the box, and rotating the key.
- [gcp.md](./gcp.md) — Google Cloud access: the metadata emulator, the `gcp` switch's levels,
  and wiring your services to it.
- [security.md](./security.md) — the threat model, what the boundary does and doesn't defend,
  and what `fy verify` checks.
- [git.md](./git.md) — git when the box and your computer share one checkout: the index shim
  and its refusals, recovering a wrong-looking `git status`, the shared stash, the executable
  bit, and why pushing happens on your computer.
- [stack.md](./stack.md) — bringing your compose stack in: the variables compose can use,
  worktree stacks and shared volumes, which paths compose may read, and podman-compose's
  differences from docker compose.
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
  - [egress-forcing-study.md](./archive/egress-forcing-study.md) — the 2026-06 study of forcing
    all egress through the proxy, and why the rootless firewall won.
  - [lessons.md](./archive/lessons.md) — fourteen incidents from building foldyard, each with its
    lesson and the ADR that records the fix, and what's still open: why a dev sandbox is needed.

## Contributing to foldyard

Start at [DEVELOPMENT.md](../DEVELOPMENT.md): the module map, test tiers, CI and conventions.

- [releasing.md](./releasing.md) — how a release is cut (tag-driven).
- [linux-support.md](./linux-support.md) — what has been run on a Linux host and inside WSL2,
  and what hasn't yet.
- [isolation-layers.md](./isolation-layers.md) — which layer carries the boundary on each host
  (macOS, Linux, WSL2), with measurements.
- [fleet-scope.md](./fleet-scope.md) — the agent control plane: what `fy agent` is as a first
  version, and the next phase (a hosted control plane, interaction, sizing).
- [lima-backend-scope.md](./lima-backend-scope.md) — the VM-backend contract (lima and podman)
  and the tricky parts of the Lima backend.
- [lima-wall-machine-integration.md](./lima-wall-machine-integration.md) — how the box reaches
  the proxy on the host, and the design of the VM and host firewalls.
- [nested-virt.md](./nested-virt.md) — a nested-KVM rig for testing the paths that only run on
  the host.
- [monitor-pipeline.md](./monitor-pipeline.md) — the guest monitor's host side: the storage
  measurements (chDB, DuckDB, SQLite, compressed JSONL), retention tiers, filtering at source,
  the score → decide → review pipeline, and hooks.
- [ebpf-monitoring-spike.md](./ebpf-monitoring-spike.md) — whether Tetragon runs in the Lima
  guest and can attribute events to containers (2026-10-03), with a kit to re-run it.
- [forwarder-spike.md](./forwarder-spike.md) — whether an in-VM forwarder can stamp each proxy
  connection with the container it came from, and what it costs (2026-10-22, for ADR-0034).

**History:** foldyard was extracted from Twist's monorepo as a fresh start
([ADR-0013](./adrs/0013-in-repo-carve-out-until-extraction.md)). The pre-extraction run-logs,
plans and design boards stay there; the ADRs are the public record of the reasoning.
