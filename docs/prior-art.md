# Prior art & positioning

Where foldyard sits in the landscape, and why the niche was open. Condensed from a
deep-research sweep (2026-06-13, ~20 searches + page fetches) plus later per-tool analysis;
kept current-ish — dates note when each claim was checked.

## The verdict

- **The exact combination is unoccupied as a tool**: laptop-local, VM-bounded,
  secretless-by-default, whole-compose-stack + dev box + IDE backend, with host-side
  TTL-bound credential posture. Every component exists separately; nobody ships the
  combination. Notably *nobody* does stack colocation, and nobody does host-enforced
  credential posture for humans working locally.
- **Closest three:** (1) the hardened-devcontainer ecosystem — blogs and template repos,
  single container on rootful Docker Desktop, secrets explicitly unhandled, no stack, DIY;
  (2) Citrix Secure Developer Spaces / Gitpod-Ona / Coder — identical value-prop *language*
  ("the laptop holds nothing") but cloud-hosted, enterprise, closed; foldyard is the local,
  open, individual version of their pitch; (3) Coder Boundary + the agent-sandbox wave —
  process/agent-scoped and egress-focused, where foldyard's unit is the project stack + the
  human dev loop, covering filesystem/secrets, not just network.
- **Demand is recurring, not speculative:** worm waves keep landing (Shai-Hulud Sep + Nov
  2025, SANDWORM_MODE Feb 2026, CanisterWorm Mar 2026, TrapDoor + Mini Shai-Hulud May 2026),
  a CISA alert, pnpm v10/Bun changing lifecycle-script defaults, Docker bundling Socket
  Firewall into Hardened Images, Strong Network acquired by Citrix — and ≥5 independent
  2025–26 blog posts hand-rolling partial foldyards with no tool to point at. The vacuum is
  the *tool* layer for individuals and small teams on laptops.

## Positioning table (as of 2026-06)

| Neighbour | What it is | Daylight |
| --- | --- | --- |
| **Docker Sandboxes** | microVM per *agent*, own Docker engine, credential proxy; proprietary, Docker Desktop | Unit is the agent, not the stack; closed; no posture system; isolation by hiding (separate engine) vs our visible shared engine |
| **nono** (Landlock/Seatbelt) | kernel-capability sandbox for agent processes | Complementary, not competing — see the deep-dive below |
| **srt / ags / yolobox / agent-sandbox** | per-agent sandboxes | Same genre as nono; crowded, deliberately not entered |
| **VS Code devcontainers** | reproducible toolchain container | On Docker Desktop: rootful daemon + `/Users` mounted ⇒ not a security boundary; secrets via env; no posture, no verify |
| **Citrix Secure Developer Spaces** (ex-Strong Network) | "replace the secret-concentrating developer laptop" | The closest *positioning* match anywhere — but cloud, enterprise, closed |
| **Gitpod/Coder/Codespaces/Daytona** | cloud/self-hosted workspaces | Remote-first; laptop-local "your machine holds the secrets but the work can't reach them" is a different product |
| **Coder Boundary** | Linux process-level egress isolator (namespaces + transparent proxy, default-deny) | Network-only, agent-marketed; no filesystem/secret boundary, no stack |
| **Jetify Devbox / Flox / Nix shells** | reproducible environments | Reproducibility, not isolation; dev shells run on the host (they also own the word "devbox") |
| **Socket safe-npm / npq / ignore-scripts** | install-time package screening | Sandboxes the *install decision*, not the dev loop; complementary |
| **CyberArk Secretless Broker / Vault agent / 1Password `op run`** | credential brokering | Brokering without an environment boundary; foldyard combines both |

## Deep-dive: nono (and the agent-sandbox pattern)

[nono](https://github.com/always-further/nono) sandboxes a single process tree, and is the
best-engineered representative of the per-agent genre. How it works, and what does/doesn't
transfer to a VM-bounded tool:

- **Its forcing function is a kernel `connect()` lock** — Landlock ABI v4 per-port TCP rules
  on Linux (kernel ≥ 6.7), Seatbelt on macOS — restricting the wrapped process tree so it can
  only `connect()` to nono's own loopback proxy, inherited by every child, irreversibly. It
  also sets `HTTP(S)_PROXY` for cooperative apps, but the kernel rule is what makes it
  bypass-proof.
- **That half does not transfer to foldyard.** Landlock/Seatbelt is per-process and lives in
  the same kernel as the wrapped process; foldyard's containers run in a separate VM with
  their own kernel and many process trees — there is no single tree to wrap from the host.
  The VM-boundary equivalent of nono's kernel lock is **nftables default-deny inside the VM**
  — which is exactly what `[machine].wall` provisions.
- **The other half — proxy + ephemeral CA + allowlist — transfers, and foldyard has it**
  (the egress proxy, CA trust injection, `[proxy]` allowlist + live grants).
- **They compose:** if you care specifically about the coding agent's own traffic, a
  nono-wrapped agent can run *inside* the yard. Two caveats: it needs the box guest kernel
  ≥ 6.7 for per-port rules, and nono's hardcoded cloud-metadata denylist (169.254.169.254)
  collides with foldyard's GCP metadata emulator.

Nearest first-party analog: Anthropic's
[sandbox-runtime](https://github.com/anthropic-experimental/sandbox-runtime) (bubblewrap +
proxies on Linux, Seatbelt on macOS) — architecturally nono-like. The best public
*VM-wrapping* blueprint is INNOQ's
["I sandboxed my coding agents"](https://www.innoq.com/en/blog/2026/03/dev-sandbox-network/)
(Squid + Lima + nftables default-deny) — the same shape as foldyard's wall, host-side.

## The devcontainer gap (launch-post ammunition)

The hardened-devcontainer pattern is institutional advice (GitHub Well-Architected, Snyk
post-Shai-Hulud guidance) but never a product, and the substrate undercuts it:

- Docker Desktop **CVE-2025-9074** (CVSS 9.3): container → unauthenticated Engine API →
  mount the host drive.
- Devcontainer Features / lifecycle hooks execute arbitrary code (devcontainers/spec#60);
  VS Code task/extension auto-execution as a malware vector; Orca's Codespaces RCE via
  attacker-controlled devcontainer.json.
- "VS Code Remote is server-to-client RCE by design" — a caveat to any thin-client claim,
  mitigated when the server side *is* the sandbox.
- Grassroots analogs (Zwyx docker-dev, The Red Guild hardened devcontainer, Ken Muse,
  Kerkour) all explicitly punt on git/SSH/secrets, run rootful, and carry no stack.

Local VMs are convenience, not boundary, by default: Lima's default template mounts the host
home; OrbStack shares the host FS. **Foldyard's repo-only mount is the inversion of their
defaults.** Qubes OS is the philosophical ancestor (qube-per-project, disposables), but a
whole x86 OS with no macOS and no compose.

## Language worth claiming (verified unclaimed, 2026-06)

"Secretless by default" for a *local* environment · "`npm install` is RCE — give it nothing
to steal" (deny-the-loot vs everyone else's detect-the-malware) · "your whole compose stack
inside the boundary" · "git push is impossible from inside" · "isolation without hiding" ·
"the local, open-source answer to the secure-CDE pitch".

## Sources

- nono: <https://github.com/always-further/nono> · networking internals
  <https://nono.sh/docs/cli/features/networking.md> · TLS-intercept design
  <https://github.com/always-further/nono/discussions/650>
- Anthropic sandbox-runtime: <https://github.com/anthropic-experimental/sandbox-runtime>
- INNOQ VM-wrapping blueprint: <https://www.innoq.com/en/blog/2026/03/dev-sandbox-network/>
- Coder Boundary: <https://github.com/coder/boundary>
- Devcontainer-gap set: CVE-2025-9074 · devcontainers/spec#60 · lets.re/blog/vscode-remote-dev ·
  Orca Codespaces RCE writeup · opensourcemalware.com
- Grassroots analogs: zwyx.dev/blog/docker-dev · github.com/theredguild/devcontainer ·
  kenmuse.com "How I Avoided Shai-Hulud's Second Coming" · kerkour.com/rust-devcontainers
- Lima home-mount defaults: lima-vm/lima discussions #627 / #454
