# ADR-0017 — Nested-virt validation: one nesting level, host-role = container; CI uses containers, not KVM

- **Status:** Accepted (2026-06-14; scope sharpened 2026-06-24) — implemented as the tier-4
  validation rig; the full machine-wrapper nested e2e remains deferred
- **Sources:** DEVELOPMENT.md (test tiers, nested-virt architecture, confirmations),
  docs/history/nested-podman-in-devbox.md, docs/history/lima-network-forcing-spike.md

## Context

foldyard's surface splits into four test tiers by *where* each can be validated:

1. **Substrate + TUI** — unit tests + headless Textual pilot tests; run anywhere, no engine.
2. **Engine verbs (golden tests)** — mock the engine, assert the exact `docker`/`podman` command
   sequences emitted; no real engine.
3. **Live smoke (e2e, opt-in `FOLDYARD_E2E=1`)** — the real CLI against the example consumer,
   the in-process proxy e2e, and the proxy plugin driving a real dev-box container over the
   socket.
4. **Host-only paths** — `machine ensure|recreate`, `box up|build`, `host` (supervisor +
   credential daemons), `mode set`. These deliberately **refuse to run inside a dev box**
   (`config.in_box()` guards: the box must not manage its own VM or escalate its posture), so
   historically they could only be validated by hand on a real Mac.

Tier 4 is the gap. Nested containers inside the box are blocked by the box's own hardening
(read-only `/proc/sys`, masked cgroups, no `/dev/fuse|kvm|net/tun`), and that's desirable: it's the same property
`fy verify` asserts. But on Apple
Silicon **M3+ / macOS 15+**, `podman machine` on the **libkrun** provider enables nested
virtualization by default, so `/dev/kvm` is live *inside* the machine VM — a sibling container
launched over the socket with `--device /dev/kvm` gets real hardware-accelerated nested KVM.

## Decision

**Close the tier-4 gap with exactly one level of VM nesting, where the "Mac"/host role is played
by a *container*, not a second VM — and keep CI on containers, with no KVM job.**

- **Topology: Mac(L0) → machine VM(L1) → nested VM(L2).** A throwaway sibling container with
  `--device /dev/kvm` (and *without* the dev-box signature, so `config.in_box()` reads it as the
  host) plays the Mac: it runs foldyard + the host daemons and drives `foldyard machine init` to
  create the single L2 VM. ARM nested KVM supports **one** nesting level reliably; a third VM as
  the "Mac" would be L1→L2→L3 triple nesting. Keeping the host role in a container stays at one
  VM level while still faithfully exercising `podman machine`.
- **Prerequisites are pinned:** M3+ CPU, macOS 15+ (EL2 nested virt), podman ≥ 5.6.0 on the
  libkrun provider selected persistently in `containers.conf` (the default applehv provider does
  **not** expose `/dev/kvm`), machine sized ≤ physical cores.

  *(Correction, 2026-09-07: the parenthetical was true when researched but is no longer a safe
  assumption — upstream has been moving the macOS/arm64 default toward libkrun, and a machine's
  provider is fixed at init regardless of the current default. The libkrun REQUIREMENT stands;
  only the claim about what you get by default is stale. Lima's `vz` driver has since turned out
  to support `nestedVirtualization` too, so the L1 need not be a podman machine at all — see
  docs/nested-virt.md. Neither changes this ADR's decision.)*
- **Scope the rig to what it's good for.** Confirmed: `/dev/kvm` passthrough + `KVM_CREATE_VM`
  (2026-06-14); the full example dogfood — `foldyard up` building and serving the example stack —
  via rootful podman *containers* in the sibling (2026-06-14/18, codified as the tier-3 e2e); a
  real **L2 VM boots** via limactl + qemu/KVM (2026-06-24). But that 2026-06-24 run also showed
  the L2 guest is **operationally fragile**: a 2-vCPU guest churned ~4 host cores and starved
  L1's control plane until `docker exec` timed out, needing a Mac-side machine restart. So the
  rig is for `KVM_CREATE_VM` smoke and thin `machine`-wrapper validation — not for running a
  full guest workload. The complete `machine ensure/recreate`/`box up`/`host` nested e2e stays
  deferred.
- **Graceful teardown is a hard rule.** SIGKILL of the inner VMM (or `docker rm -f` of a
  container running a nested guest) can wedge the L1 machine. Tear down inside-out: ACPI
  `poweroff` the guest → stop the container → `podman machine stop`/`rm` the nested machine.
  Treat nested machines as disposable; if L1 goes sluggish, recreate it from the Mac.
- **CI runs tiers 1–3 on containers, none on KVM.** The `live-e2e` job runs inside a docker-CLI
  container that mirrors the dev box: docker CLI only (so `engine()` picks docker, ADR-0010),
  the runner's socket mounted + `DOCKER_HOST` set (so `in_box()` is true and `machine.ensure`
  short-circuits — no `/dev/kvm` needed), and the repo mounted at its same host path (so the
  box's CA `-v` mount, resolved daemon-side, exists). The box e2e spawns a *sibling* box and
  must discover its own network, which is exactly why the test process must itself be in a
  container. There is deliberately no KVM job: raw `/dev/kvm` exists on GitHub runners, but full
  podman-machine/libvirt VMs are flaky there, so tier 4 stays the Mac / nested-rig recipe.
- **Don't use the nested rig for network-forcing tests.** The in-VM nftables wall (ADR-0009) is
  identical regardless of how the VM is created; nesting adds an L2 that only burns CPU and
  reproduces no macOS network stack. Those tests run in a **native Lima VM on the Mac**
  (the runnable kit, docs/lima-network-forcing-kit/) — the 2026-06-24 spike's explicit
  recommendation after the L1-wedge incident.

## Consequences

- Tier-4 code paths get a headless validation story that behaves exactly like a developer's Mac
  (a fresh sibling without the box signature *is* the host, as far as the guards can tell), with
  no Mac round-trip — at the cost of an operationally delicate rig that must be torn down
  gracefully.
- The proxy/box e2e runs both in CI and inside the dev box via the "adapted topology": proxy +
  upstream in-process, `FY_PROXY` overridden to the test container's own IP. The only divergence
  from production is that proxy address; CA mount, proxy env, in-flight injection, and a real
  box over the real socket are genuine.
- CI stays fast, unprivileged, and portable to the extracted repo (the workflows carry no
  monorepo coupling); the price is that the `podman machine` lifecycle itself has no CI gate and
  relies on the Mac/nested recipe plus the native-backend e2e tier (ADR-0011) for real-engine
  coverage.
- Each validation concern has one designated rig: golden tests for command shape, CI containers
  for live e2e, the nested rig for machine-wrapper smoke, a native Lima VM for the wall. Using
  the wrong rig (the temptation this ADR forecloses) produces fragile, low-fidelity runs.
