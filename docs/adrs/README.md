# Architecture Decision Records

One file per decision, `NNNN-slug.md`, each with **Status / Context / Decision / Consequences**
(and, where useful, **Rejected alternatives**). The spun-out repo started from a squash commit
([ADR-0013](./0013-in-repo-carve-out-until-extraction.md)), so these ADRs are the public record
of the "why" — distilled from the historical docs (PLAN.md, SPIKE.md, HANDOVER.md, the plans and
spikes) while both still existed side by side. The Tangible monorepo keeps the full archaeology.

A new decision gets the next number. Supersede, don't rewrite: flip the old ADR's Status to
`Superseded by ADR-NNNN` and say why in the new one. Where only part of an ADR has been overtaken
(a renamed key, a retired backend, a verb that changed shape), add a dated `Amended YYYY-MM-DD:`
line to its Status instead and leave the body as the record. The ADRs keep the vocabulary of
their day (posture, axis, rung, wall); [../glossary.md](../glossary.md) maps it to today's.

**On the `Sources:` lines.** They cite the working documents a decision was distilled from, and
several of those (`PLAN.md`, `SPIKE.md`, `HANDOVER.md`, the run-logs) are pre-extraction internal
notes that stay in the origin monorepo — they are attribution, not links, and nothing in an ADR
should *depend* on reading them. Each ADR is meant to stand alone; if one leans on an unpublished
source to make its point, that's a bug in the ADR. Cite live docs (`../configuration.md`, another
ADR) as links, and internal working notes by name only.

## Index

| ADR | Decision |
| --- | --- |
| [0001](./0001-rootless-podman-vm-isolation-boundary.md) | Rootless Podman VM as the isolation boundary; repo-only mounts; escape-test-refused as the credibility gate (its opt-in `native` backend retired by [0027](./0027-always-a-vm-native-backend-retired.md)) |
| [0002](./0002-stack-colocation-project-as-isolation-unit.md) | Stack colocation: the project (whole compose stack), not the agent, is the unit of isolation |
| [0003](./0003-name-foldyard.md) | The name "foldyard" — metaphor, rejected alternatives, availability |
| [0004](./0004-one-machine-worktrees-as-compose-projects.md) | One machine + worktrees as namespaced compose projects, not VM-per-worktree |
| [0005](./0005-secretless-by-default-posture-axes.md) | Secretless by default: posture axes/rungs as data, TTL-bound emergency rungs, authoritative state host-side (now *modes*, *switches* and *levels*; the `capture` axis removed by [0029](./0029-the-proxy-always-decrypts.md)) |
| [0006](./0006-host-side-enforcement-single-supervisor.md) | Host-side enforcement: ONE supervisor (singleton lock) reconciling a per-worktree mode-map (amended 2026-09-24: always detached, no foreground `fy host`; `fy host status\|restart\|logs`) |
| [0007](./0007-credential-injection-at-egress-proxy.md) | Credential injection at the egress proxy; 401 re-mint in-hook; multi-injector rule set |
| [0008](./0008-keyless-agent-auth.md) | Keyless agent auth (Claude api-key/OAuth, Codex api-key/ChatGPT): dummy in the box, rewrite in flight |
| [0009](./0009-monitoring-cooperative-enforcement-locked.md) | Monitoring is cooperative, enforcement is not: claims track the locked kind (the VM-level wall — now `[machine] firewall` — validated on real Lima VMs, amended 2026-09-24) |
| [0010](./0010-podman-everywhere-container-host.md) | Engine = podman everywhere via `CONTAINER_HOST`; docker only as fallback |
| [0011](./0011-machine-backends-one-socket-contract.md) | Machine backends behind one libpod-socket contract: podman / lima (the third, native, retired by [0027](./0027-always-a-vm-native-backend-retired.md)) |
| [0012](./0012-uv-tool-distribution-no-mutable-daemon-source.md) | Distribution via `uv tool install`; daemons must not run mutable working-tree source (the forced bounce is now `fy host restart`) |
| [0013](./0013-in-repo-carve-out-until-extraction.md) | No submodule; in-repo carve-out until extraction; truth flips once; squash-start history |
| [0014](./0014-consumer-supplied-box-image.md) | Box image contract: consumer-supplied image + runtime injection; no foldyard base image |
| [0015](./0015-plugin-framework-per-consumer-registry.md) | Plugin framework: hook Registry; built-ins loaded directly; per-consumer registry; core vs declared |
| [0016](./0016-per-worktree-posture.md) | Per-worktree posture: the worktree is the consumer; per-worktree ports; shared identity |
| [0017](./0017-nested-virt-validation-strategy.md) | Nested-virt validation: one nesting level, host-role = container; CI uses containers not KVM (that clause superseded by [0027](./0027-always-a-vm-native-backend-retired.md): a Lima/QEMU VM runs in CI) |
| [0018](./0018-zed-editor-rejected.md) | Zed editor: rejected for now (SSH-only transport vs the no-inbound-creds box) |
| [0019](./0019-consumer-repo-plugins-trust-model.md) | ~~Consumer-repo plugins: path-loading gated by a hash-acknowledged trust store~~ — **superseded by [0024](./0024-declarative-consumer-axes-no-repo-path-plugins.md)**, never implemented |
| [0020](./0020-post-extraction-consumption-model.md) | Post-extraction consumption: PyPI-first frozen installs (amended 2026-08-29); editable for foldyard dev; opt-in co-dev mount; no vendoring |
| [0021](./0021-per-kernel-git-index-split.md) | Per-kernel git index split: runtime-installed box git shim writes `index-box`, ending the shared-checkout index race (every host since [0027](./0027-always-a-vm-native-backend-retired.md): the checkout is always shared between two kernels) |
| [0022](./0022-host-runs-the-adopted-config.md) | The host reconciles from the `foldyard.toml` it ADOPTED (outside the mount), not the working tree; drift is reported, adoption is an explicit act |
| [0023](./0023-no-host-executed-code-from-the-repo-mount.md) | No host-executed code from the repo mount: minters are packaged KINDS, consumer scripts run in the yard, config declares data and never commands |
| [0024](./0024-declarative-consumer-axes-no-repo-path-plugins.md) | Consumer customisation is declarative data: no repo-path plugin loading, no trust store; `[[axis]]` + a conflict relation replace the two consumer-shaped built-ins |
| [0025](./0025-gvisor-machine-posture-and-socket-narrowing.md) | gVisor as a machine posture (`[machine].runtime = "gvisor"`): second runsc-default socket + a box-facing filter that strips the runtime opt-out; runtime narrowing only (strip shown live on podman 6.1.1); the broader mount/endpoint allowlist deferred |
| [0026](./0026-vscode-attach-config-is-declarative.md) | The VS Code attached-container config is declarative `[vscode]` data (`extensions`, `settings`) read from the ADOPTED copy behind the gate; `remoteUser` + the port pin as facts; settings land in the box's Remote [Machine] layer so the checkout's `.vscode/settings.json` stays personal; one attach shape (the folder); the in-box generator script, the mount read of `.vscode/extensions.json` and `workspace_file` are removed — amends [0023](./0023-no-host-executed-code-from-the-repo-mount.md) §2 |
| [0027](./0027-always-a-vm-native-backend-retired.md) | foldyard always has a VM: the `native` (no-VM) backend retired — its CI rationale replaced by the Lima/QEMU runner job, WSL2 does not rescue it, and every product claim needs the VM; a config naming it gets a loud warning + the podman backend |
| [0028](./0028-no-elevation-on-the-host-operator-applies.md) | foldyard never elevates on the host: it renders the artefact and the commands, the operator applies them with the content in front of them, foldyard probes the capability. The host wall is instance one — a boot-stable table bound to a persistent user slice, installed once via a printed system unit, probed on every `fy up` (the cgroup-ID fail-open is why). Since renamed: `[machine] host_firewall`, `fy machine host-firewall` |
| [0029](./0029-the-proxy-always-decrypts.md) | The proxy always decrypts: the `capture` axis is removed, `[proxy] passthrough` is the one decryption control (the trusted fast lane + the escape hatch for undecryptable hosts), and large bodies stream — decided on a loopback measurement (~3 ms per new connection, ~590 MB/s decrypted) |
| [0030](./0030-the-proxy-reloads-instead-of-restarting.md) | The proxy reloads instead of restarting: posture (rules, wall, passthrough) travels in a supervisor-written live file the addon re-reads, secrets are read by name from `host.env` (and stripped from the proxy's environment), and a narrowing closes only the connections it no longer allows — through mitmproxy internals, pinned by a real-mitmdump e2e |
