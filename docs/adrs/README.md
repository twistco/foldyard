# Architecture Decision Records

One file per decision, `NNNN-slug.md`, each with **Status / Context / Decision / Consequences**
(and, where useful, **Rejected alternatives**). The spun-out repo started from a squash commit
([ADR-0013](./0013-in-repo-carve-out-until-extraction.md)), so these ADRs are the public record
of the "why" — distilled from the historical docs (PLAN.md, SPIKE.md, HANDOVER.md, the plans and
spikes) while both still existed side by side. The Tangible monorepo keeps the full archaeology.

A new decision gets the next number. Supersede, don't rewrite: flip the old ADR's Status to
`Superseded by ADR-NNNN` and say why in the new one.

**On the `Sources:` lines.** They cite the working documents a decision was distilled from, and
several of those (`PLAN.md`, `SPIKE.md`, `HANDOVER.md`, the run-logs) are pre-extraction internal
notes that stay in the origin monorepo — they are attribution, not links, and nothing in an ADR
should *depend* on reading them. Each ADR is meant to stand alone; if one leans on an unpublished
source to make its point, that's a bug in the ADR. Cite live docs (`../configuration.md`, another
ADR) as links, and internal working notes by name only.

## Index

| ADR | Decision |
| --- | --- |
| [0001](./0001-rootless-podman-vm-isolation-boundary.md) | Rootless Podman VM as the isolation boundary; repo-only mounts; escape-test-refused as the credibility gate |
| [0002](./0002-stack-colocation-project-as-isolation-unit.md) | Stack colocation: the project (whole compose stack), not the agent, is the unit of isolation |
| [0003](./0003-name-foldyard.md) | The name "foldyard" — metaphor, rejected alternatives, availability |
| [0004](./0004-one-machine-worktrees-as-compose-projects.md) | One machine + worktrees as namespaced compose projects, not VM-per-worktree |
| [0005](./0005-secretless-by-default-posture-axes.md) | Secretless by default: posture axes/rungs as data, TTL-bound emergency rungs, authoritative state host-side |
| [0006](./0006-host-side-enforcement-single-supervisor.md) | Host-side enforcement: ONE supervisor (singleton lock) reconciling a per-worktree mode-map |
| [0007](./0007-credential-injection-at-egress-proxy.md) | Credential injection at the egress proxy; 401 re-mint in-hook; multi-injector rule set |
| [0008](./0008-keyless-agent-auth.md) | Keyless agent auth (Claude api-key/OAuth, Codex api-key/ChatGPT): dummy in the box, rewrite in flight |
| [0009](./0009-monitoring-cooperative-enforcement-locked.md) | Monitoring is cooperative, enforcement is not: claims track the locked kind |
| [0010](./0010-podman-everywhere-container-host.md) | Engine = podman everywhere via `CONTAINER_HOST`; docker only as fallback |
| [0011](./0011-machine-backends-one-socket-contract.md) | Machine backends behind one libpod-socket contract: podman / lima / native |
| [0012](./0012-uv-tool-distribution-no-mutable-daemon-source.md) | Distribution via `uv tool install`; daemons must not run mutable working-tree source |
| [0013](./0013-in-repo-carve-out-until-extraction.md) | No submodule; in-repo carve-out until extraction; truth flips once; squash-start history |
| [0014](./0014-consumer-supplied-box-image.md) | Box image contract: consumer-supplied image + runtime injection; no foldyard base image |
| [0015](./0015-plugin-framework-per-consumer-registry.md) | Plugin framework: hook Registry; built-ins loaded directly; per-consumer registry; core vs declared |
| [0016](./0016-per-worktree-posture.md) | Per-worktree posture: the worktree is the consumer; per-worktree ports; shared identity |
| [0017](./0017-nested-virt-validation-strategy.md) | Nested-virt validation: one nesting level, host-role = container; CI uses containers not KVM |
| [0018](./0018-zed-editor-rejected.md) | Zed editor: rejected for now (SSH-only transport vs the no-inbound-creds box) |
| [0019](./0019-consumer-repo-plugins-trust-model.md) | ~~Consumer-repo plugins: path-loading gated by a hash-acknowledged trust store~~ — **superseded by [0024](./0024-declarative-consumer-axes-no-repo-path-plugins.md)**, never implemented |
| [0020](./0020-post-extraction-consumption-model.md) | Post-extraction consumption: PyPI-first frozen installs (amended 2026-08-29); editable for foldyard dev; opt-in co-dev mount; no vendoring |
| [0021](./0021-per-kernel-git-index-split.md) | Per-kernel git index split: runtime-installed box git shim writes `index-box`, ending the shared-checkout index race |
| [0022](./0022-host-runs-the-adopted-config.md) | The host reconciles from the `foldyard.toml` it ADOPTED (outside the mount), not the working tree; drift is reported, adoption is an explicit act |
| [0023](./0023-no-host-executed-code-from-the-repo-mount.md) | No host-executed code from the repo mount: minters are packaged KINDS, consumer scripts run in the yard, config declares data and never commands |
| [0024](./0024-declarative-consumer-axes-no-repo-path-plugins.md) | Consumer customisation is declarative data: no repo-path plugin loading, no trust store; `[[axis]]` + a conflict relation replace the two consumer-shaped built-ins |
