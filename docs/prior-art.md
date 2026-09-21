# Prior art & positioning

Where foldyard sits in the landscape, and why the niche was open. Condensed from a
deep-research sweep (2026-06-13, ~20 searches + page fetches) plus later per-tool analysis;
kept current-ish — dates note when each claim was checked. **Upgraded 2026-07-20** after the
June sweep was found to have missed [gondolin](./prior-art/gondolin.md) (public since
2026-02) entirely — that pass added the gondolin deep-dive and re-swept the neighbourhood.
**Upgraded 2026-09-21** with the [vhrn](./prior-art/vhrn.md) deep-dive, and the per-tool
deep-dives moved into [prior-art/](./prior-art/) — the competitor library, which carries the
axis-by-axis comparison table, the entry template, and the maintenance rules. This file stays
the landscape: the whole neighbourhood, one row per tool, and the positioning argument.

## The verdict

- **The exact combination is unoccupied as a tool**: laptop-local, VM-bounded,
  secretless-by-default, whole-compose-stack + dev box + IDE backend, with host-side
  TTL-bound credential posture. Every component exists separately; nobody ships the
  combination. Notably *nobody* does stack colocation, and nobody does host-enforced
  credential posture for humans working locally.
- **Closest five:** (1) the hardened-devcontainer ecosystem — blogs and template repos,
  single container on rootful Docker Desktop, secrets explicitly unhandled, no stack, DIY;
  (2) Citrix Secure Developer Spaces / Gitpod-Ona / Coder — identical value-prop *language*
  ("the laptop holds nothing") but cloud-hosted, enterprise, closed; foldyard is the local,
  open, individual version of their pitch; (3) Coder Boundary + the agent-sandbox wave —
  process/agent-scoped and egress-focused, where foldyard's unit is the project stack + the
  human dev loop, covering filesystem/secrets, not just network; (4) **gondolin** — the
  closest *mechanism* match anywhere (local VM, host-side egress mediation, secrets that
  never enter the guest — ADR-0007 independently reinvented), but a per-task agent-sandbox
  SDK, not a dev environment: no stack, no posture, no human loop
  ([deep-dive](./prior-art/gondolin.md)); (5) **vhrn** — the closest *ergonomics* match: the
  same user (a developer on their own machine, daily loop, persistent login) and the same
  operator verbs foldyard grew (scoped persistent egress grants, a status view with
  provenance, a denial log), but the unit is one agent run in one directory — no stack, no
  posture ladder, no worktrees, and no credential injection at all
  ([deep-dive](./prior-art/vhrn.md)).
- **2026-07 re-sweep: the placeholder-proxy idea has commoditized.** "Secrets never enter
  the sandbox — a host-side TLS proxy swaps placeholders for real credentials on
  allowlisted egress" went from rare to near-standard in the per-task genre inside a year:
  gondolin, matchlock, microsandbox, Buildkite Cleanroom, and a wave of smaller tools
  (agent-vm, nilbox, bromure, shuru, …) plus standalone broker proxies all ship it. ADR-0007
  is thoroughly vindicated and **no longer differentiating on its own**. What stays
  unoccupied is the narrower, sharper claim: the *persistent, project-colocated dev
  environment* — whole compose stack + human dev loop + host-enforced TTL posture — inside
  the boundary. Every placeholder-wave tool is an ephemeral per-task/per-agent sandbox. The
  one shape-match found (vmoat: compose stack in a per-worktree Colima VM, 2026-06) is
  embryonic and has no secrets or egress story at all. Details in the
  [re-sweep section](#the-2026-07-re-sweep--the-placeholder-wave-and-whats-still-open).
- **2026-09: the frontier moved from *whether* to mediate egress to *how rigorously*.** The
  vhrn review found a competitor whose proxy is specified as a normative contract (RFC-
  referenced MUST/SHOULD, exact error bodies, fail-closed policy reads) and whose address
  handling — an IANA-registry unicast boundary applied in every mode, resolve-once-and-pin
  against rebinding, host-loopback as a separate narrow capability — is simply better than
  ours, on axes where foldyard has no answer written down at all. The placeholder mechanism
  commoditized in 2026-07; the *quality* of the mediation is the 2026-09 frontier, and it is
  the one axis where a neighbour is ahead of foldyard rather than beside it. The four cheap
  closes (address discipline, grant provenance, proxy containment, a written proxy contract)
  are listed in [prior-art/README.md](./prior-art/README.md#how-foldyard-compares).
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
| **Gondolin** (earendil-works, added 2026-07) | per-task Alpine micro-VM (QEMU/libkrun) with the network stack and filesystem implemented as programmable host-side JS: TLS-MITM egress mediation, placeholder secrets substituted in-flight per destination host, FUSE-backed policy filesystem | Closest mechanism neighbour — validates the host-side-injection and host-as-enforcement bets independently. Unit is the agent *task* (one command at a time, VM per turn); no compose stack, no posture ladder, no human operator surface; HTTP/1.x-only egress. Full analysis: [prior-art/gondolin.md](./prior-art/gondolin.md) |
| **vhrn** (aravind-n, added 2026-09) | agent harness (Claude Code / Codex / Pi) in a container jailed to the current project dir on Apple `container` or Docker; in-container nftables installed before a privilege drop, default-deny egress through a capability-dropped Rust proxy sidecar; five additive host-owned policy layers with per-project scope; no TLS termination | Closest ergonomics neighbour — same user and daily loop as foldyard, and the same operator verbs (`vhrn net allow --project .`, status with provenance, denial log). Unit is one agent *run* in one directory: no stack, no posture ladder, no worktrees, no credential minting or injection (hostname-only policy makes it impossible). **Ahead of foldyard** on address discipline (IANA unicast boundary, DNS-answer pinning, brokered loopback as a separate capability), proxy containment, grant provenance, and a written proxy contract. Full analysis: [prior-art/vhrn.md](./prior-art/vhrn.md) |
| **matchlock** (added 2026-07; missed by the June sweep) | ephemeral microVMs (Firecracker / Virtualization.framework) for agent runs; nftables/gVisor egress allowlist; transparent proxy injects credentials, guest sees `SANDBOX_SECRET_…` placeholders | MIT, ~600★, active. The placeholder pattern, per-invocation: VM and overlay volumes discarded after each run; no stack, no dev loop |
| **microsandbox** (added 2026-07; missed — existed since mid-2025) | self-hosted libkrun microVM runtime for agent code execution (~320 ms boot, OCI images, MCP server, SDKs); deny-all egress + domain allowlists; now also ships placeholder substitution | Highest-profile in the genre (6.5k★, YC-backed, cloud beta). Unit is the execution, framing is an SDK/server for agent builders — not a dev environment |
| **Buildkite Cleanroom** (added 2026-07) | repo-declared policy → warm Firecracker snapshot with deps installed; CoW fork fan-out; deny-by-default egress re-enforced on resume; "secrets never enter the sandbox or its captured artifact" | Closest *policy-philosophy* cousin (repo-declared, deny-by-default, secretless) — but CI/task-shaped ephemeral snapshots, corporate-backed, not a live laptop stack |
| **agent-vm · nilbox · bromure · shuru** (the placeholder wave, added 2026-07; catalog-sourced — see re-sweep caveats) | per-project or per-task local VMs (Lima / VZ.framework / QEMU), each with a host-side TLS-intercept proxy swapping dummy tokens for real | agent-vm is the closest single match to per-project + proxy-substitution; all are agent-run wrappers — no compose stack, no posture ladder, no human operator surface |
| **vmoat** (added 2026-07; created 2026-06) | one Colima VM per git worktree, own Docker daemon, **whole compose stack inside** | The only stack-colocation shape-match found anywhere — and it's v0.1.0/0★, isolation-for-parallelism, with *no* secrets or egress story. Watch it; today it proves the niche, not fills it |
| **Microsoft MXC** (added 2026-07; announced 2026-06-02) | unified policy/SDK abstraction over sandbox backends (bubblewrap, seatbelt, microVM, Windows Sandbox, …); adopted at launch by Copilot CLI, Codex, NVIDIA OpenShell | An isolation *abstraction* for agent vendors — Microsoft says profiles are "not yet security boundaries"; no secretless/egress posture, not an environment |
| **Apple `container`** + ecosystem (CodeRunner, drydock, sand; added 2026-07) | Apache-2.0 per-container lightweight VMs on Apple Silicon (macOS 26); spawned a macOS agent-sandbox wave | Substrate, not product — per-container VM primitive vs one VM around a project; no secrets/egress opinions. A future foldyard backend candidate at most |
| **AgentFS** (Turso, added 2026-07) | CoW filesystem for agent writes: read-only base + SQLite-backed delta, FUSE/NFS, fork-by-copying-the-delta, file-op audit queryable in SQL | The strongest programmable-FS-view answer in the field (cf. gondolin's VFS); write-isolation and audit, not secret-hiding — complement, not competitor |
| **container-use** (Dagger, added 2026-07; missed — since 2025-06) | container + git branch per agent task, via MCP | Container-per-task for parallel agents; no VM boundary, no secrets/egress story |
| **Claw Patrol** (Deno, added 2026-07) | protocol-aware egress gateway: parses PostgreSQL/ClickHouse/kubectl/HTTP at the wire, HCL/CEL rules, can pause a destructive query for human approval | No isolation of its own — but a big-name proof that egress gating is going *protocol-aware*, one level deeper than foldyard's hostname allowlist + wall |
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

## Deep-dive: gondolin (separate doc)

[Gondolin](https://github.com/earendil-works/gondolin) (Armin Ronacher / Earendil Labs,
Apache-2.0, public 2026-02) earns the other deep-dive: of all the neighbours that arrived at
foldyard's two core bets — host-side enforcement outside the untrusted environment, and
secrets that never enter it (placeholder in the guest, in-flight substitution at the
mediation point, per-destination allowlists) — it is by far the most substantial, and it got
there from the opposite end of the design space: a from-scratch userspace network stack plus
a programmable FUSE filesystem, ~40k lines of focused implementation, per-task micro-VMs
driven by a TypeScript SDK. Reading it against foldyard exposed three concrete foldyard gaps (proxy dials internal
upstreams from outside the wall; `query_param` injection leaks tokens into the egress log;
the DNS-recursion tunneling residual is undocumented) and a shortlist of cheap adoptions
(dummy-token tripwire, egress capability matrix, `.env`-hiding overmounts). It does not
compete on the dev-environment unit: no stack colocation, no posture, no human loop. The
full analysis — VFS mechanics, egress comparison table, fix shapes, and what deliberately
does *not* transfer — is in [prior-art/gondolin.md](./prior-art/gondolin.md).

## The 2026-07 re-sweep — the placeholder wave, and what's still open

A bounded follow-up sweep (2026-07-20, ~9 searches + 8 repo fetches), triggered by the
gondolin miss. Tier-1 entries above (matchlock, Cleanroom, microsandbox, MXC, Apple
`container`, AgentFS, vmoat, container-use, Claw Patrol, brood-box) were verified by repo
fetch; the smaller placeholder-wave names (agent-vm, nilbox, bromure, shuru, strangeClaw,
airut, iron-proxy, onecli, clawshell, agent-creds, wardgate, warden) are sourced from the
two catalogs below and **not individually verified** — treat names as real, details as
to-confirm.

What the re-sweep changes strategically:

- **The placeholder-proxy pattern is now table stakes in the per-task genre.** At least
  eight shipping tools substitute dummy credentials at a host-side proxy. Launch-post
  framing should not lead with the mechanism; it should lead with the *unit* — the
  persistent project stack and the human dev loop, which nobody else covers.
- **First-party sandboxes are absorbing the wrapper niche.** Anthropic's sandbox-runtime,
  Codex's Landlock/Seatbelt defaults, Docker Sandboxes' GA push (microVM + private Docker
  daemon per sandbox, "strongest agent isolation on the market" marketing, 2026-07), and
  MXC's vendor adoption all point the same way: standalone per-agent wrappers are
  consolidating into the platforms. Several early wrappers are already dormant. foldyard's
  defensible ground is the environment layer, not the wrapper layer.
- **Filesystem policy views are emerging as their own category** — gondolin's programmable
  VFS, Turso's AgentFS (CoW SQLite delta + SQL-queryable audit), ai-jail's glob-masking,
  brood-box's non-overridable exclusion of `.env*`/`.ssh`/`.aws`. Notably, *nobody* ships
  FUSE-based secret-hiding inside a shared checkout — the field solved secret exposure at
  the proxy instead. foldyard's cheap move here is the `[box] hide` overmount sketch in
  [prior-art/gondolin.md](./prior-art/gondolin.md).
- **Nulls, for the record:** Kata Containers has no local/laptop agent positioning (cloud
  substrate only); krunvm is subsumed by the libkrun ecosystem (microsandbox, brood-box,
  krunai); the hyperscalers (AWS Lambda MicroVMs 2026-06, GKE Agent Sandbox, Azure dynamic
  sessions, Cloudflare) all added agent-sandbox SKUs but none moved local.

Meta-sources worth keeping: the wincent catalog gist (2026-05), fishman/awesome-agent-sandbox,
bureado/awesome-agent-runtime-security, and arXiv 2606.08433 ("AI Code Sandboxes: A
Comparative Security Study", 2026-06) — the latter is useful benchmark framing if foldyard
ever wants third-party validation of the `verify` battery's claims.

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

**2026-07 erosion check:** "secrets never enter the sandbox/VM" phrasing is now common
across the microVM wave (matchlock, microsandbox, Cleanroom, nilbox's "Zero Token
Architecture") — stop treating that *sentence* as ours. Still unclaimed: "secretless by
default" applied to a persistent dev *environment*, "your whole compose stack inside the
boundary", and "isolation without hiding".

## Sources

- gondolin: <https://github.com/earendil-works/gondolin> — reviewed at `29fa74d` /
  v0.12.0 (2026-07-20); see [prior-art/gondolin.md](./prior-art/gondolin.md)
- vhrn: <https://github.com/aravind-n/vhrn> — reviewed at `0e3926d` / v0.5.1 (2026-09-21);
  see [prior-art/vhrn.md](./prior-art/vhrn.md). Its
  `docs/proxy/consumer-contract.md` is the normative proxy spec foldyard should answer with
  one of its own.
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
- 2026-07 re-sweep (all accessed 2026-07-20): matchlock
  <https://github.com/jingkaihe/matchlock> · Buildkite Cleanroom
  <https://github.com/buildkite/cleanroom> · microsandbox <https://microsandbox.dev> ·
  MXC <https://github.com/microsoft/mxc> + Windows Dev Blog 2026-06-02 · Apple container
  <https://github.com/apple/container> · AgentFS <https://turso.tech/blog/agentfs> ·
  vmoat <https://github.com/voycey/vmoat> · container-use
  <https://github.com/dagger/container-use> · Claw Patrol
  <https://github.com/denoland/clawpatrol> · brood-box
  <https://github.com/stacklok/brood-box> · catalogs:
  <https://gist.github.com/wincent/2752d8d97727577050c043e4ff9e386e> ·
  <https://github.com/fishman/awesome-agent-sandbox> ·
  <https://github.com/bureado/awesome-agent-runtime-security> · study: arXiv 2606.08433
