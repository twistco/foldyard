# E2B self-hosting: what it actually costs, and could it run in foldyard

Research note, 2026-09-09. Prompted by the question "how hard is it to self-host
[E2B](https://github.com/e2b-dev/E2B) on GCP?" — the published guide provisions a fleet, and the
suspicion was that most of the fleet is E2B's production shape rather than a requirement.

**Nothing is decided here.** No ADR, no code. This note sits beside
[firecracker-and-microvm-backends.md](./firecracker-and-microvm-backends.md), which asks the
*inverted* question (could foldyard's machine layer *be* Firecracker — answered: no). This one asks
whether the E2B stack could run *as a workload* inside foldyard.

| read | version |
| --- | --- |
| `e2b-dev/infra` | `fc9bc5b`, main, 2026-09-09 |
| `e2b-dev/E2B` | main, 2026-09-09 (SDKs/CLI only — not the thing you self-host) |

## Verdict, in one table

| question | answer |
| --- | --- |
| Is it Kubernetes? | **No.** Nomad + Consul on raw GCE VMs. The orchestrator is a Nomad `raw_exec` job running as root, because Firecracker needs `/dev/kvm`, TAP devices, NBD mounts and cgroups on the bare host. |
| Minimum GCP footprint | **5–7 VMs across 5 node pools**, ~$1.2–1.3k/month all-in. |
| How much of that is capability? | **~30%.** By the template's own defaults, ~$735 of ~$1,076/month of compute is topology, not capacity. |
| Can it run on one machine? | **Yes, and it does — continuously.** E2B's own integration suite runs the whole stack on a single host on every PR. |
| Two-tier (control + one sandbox box), no scheduler? | **Yes, supported by config today.** See [the env trace](#the-env-trace-two-tier-static). |
| Two-tier with *several* sandbox boxes, no scheduler? | **No — needs a ~10-line patch.** The API's provider validator has no `static`. See [the correction](#the-limit-the-api-has-no-multi-node-static). |
| Could it run in foldyard? | **The substrate is already proven on our hardware; the posture is the blocker.** See [Could this run in foldyard?](#could-this-run-in-foldyard). |

## Maintenance: healthy repo, frozen IaC

Worth separating, because the two halves diverge sharply.

The repo is very much alive: 7,443 commits since 2019-11; **601 commits from 30 human authors in
the last 90 days**; monthly cadence roughly doubled over the past year (~90/month mid-2025 →
~200+/month mid-2026). Apache 2.0, no BSL.

But `iac/` — the Terraform you would actually deploy from — ran 26–39 commits/month from
2025-09 through 2026-06, then **1 in July, 4 in August, none in September**. The cause looks
clear from the sequence:

1. Late May / early June 2026: a burst of `refactor(iac): lift <service> env vars` commits
   (#2890, #2892, #2906, #2911, #2913, #2914, #2915) pulling config out of Nomad HCL.
2. 2026-06-03, #2910: `iac/provider-gcp/k8s-apps/` lands, generating ArgoCD `Application`
   manifests pointed at `ghcr.io/e2b-dev/charts`. `argocd_enabled = false` is hardcoded
   (`main.tf:370`) and the charts are **not in the repo**.
3. `packages/api` now depends on `k8s.io/client-go`; `servicediscovery` grew a `kubernetes`
   backend and a **composed `nomad+kubernetes` mode with Nomad primary and K8s as fallback** —
   refactored as recently as 2026-08-26.

That is a live migration. The practical consequence: the Nomad IaC is in maintenance mode and its
successor is not open yet. Anyone starting a self-host project builds on the frozen half.

## Why one machine works

Three findings, in ascending order of how much they matter.

**Storage is abstracted four ways.** `packages/shared/pkg/storage/storage_factory.go` dispatches on
`spec.Provider` across `LocalStorageProvider`, `AWSStorageProvider` (with `UsePathStyle` for
S3-compatible endpoints), `GCPStorageProvider` and `AzureStorageProvider`. Same for the artefact
registry (`artifacts-registry/registry.go`): `Local` is a first-class value. So the eleven GCS
buckets and the Artifact Registry repos are a deployment choice.

**The sandbox node and the build node are the same binary.** Production sets
`ORCHESTRATOR_SERVICES = "orchestrator"` on the client pool and `"template-manager"` on the build
pool (`iac/provider-gcp/main.tf:143`, `:171`). Locally it is
`ORCHESTRATOR_SERVICES=orchestrator,template-manager` — one process. The separate build pool
(an entire `n1-standard-8`, ~$341/month) is about tenancy, not capability.

**Service discovery is orchestrator-agnostic.** `packages/shared/pkg/servicediscovery` exposes
`nomad`, `kubernetes`, `local`, `remote`, `dns` and `static` as peers in one factory. Nothing
requires a scheduler.

And the decisive evidence: `.github/workflows/integration_tests.yml` runs on a self-hosted runner
and the composed action does *start databases (Compose) → init host (hugepages, nbd, KVM) → build
a real Firecracker template → start services → full integration suite*, all on `localhost`, eight
shards, three compression configs, on every PR and every push to main. The single-machine topology
is the **most continuously validated configuration in the repo** — more than the Terraform, which
has a lint (`validate-iac.yml`) and no deployment test at all.

### What the fleet is actually buying

| node | ~$/mo | what it is really for |
| --- | --- | --- |
| 3× `e2-standard-2` Nomad/Consul servers | 147 | Quorum HA only. Zero value single-tenant. |
| 1× `e2-standard-4` API | 115 | Control-plane isolation. |
| 1× `e2-standard-4` ClickHouse | 132 | Real component — but locally it is a Compose container. |
| 1× `n1-standard-8` build | 341 | Same binary as the client node. |
| 1× `n1-standard-8` client | 341 | **The only line that is actually capacity.** |

Plus a global external HTTPS LB (two forwarding rules), two Cloud Armor policies, Certificate
Manager, ~17 Secret Manager secrets, and a Packer image carrying the `enable-vmx` licence.
Postgres is BYO. Cloudflare is **not optional** — the `cloudflare` provider is wired into
`iac/provider-gcp/nomad-cluster/network/` for DNS records and cert DNS-auth challenges.

Framing that matters: the IaC is not padded. It is provisioned correctly for *one* profile —
multi-tenant SaaS with an abuse surface and an SLA. What is missing is a second profile. The code
beneath is markedly more flexible than the Terraform exposes.

## The env trace: two-tier static

Two machines, no Nomad, no Consul, no Kubernetes, no cloud load balancer.

- **Tier A (control):** Postgres, ClickHouse, Redis, `api`, `client-proxy`. No KVM needed. Small.
- **Tier B (sandbox):** `orchestrator` + `template-manager` (one process). Needs `/dev/kvm`,
  hugepages, `nbd`, root.

Provenance for every var below: **[L]** = in the committed `.env.local` files, so exercised by CI
on every PR. **[P]** = from the production Terraform locals (`iac/provider-gcp/main.tf:78–170`),
needed once you leave `ENVIRONMENT=local`. **[D]** = derived — the value differs in a two-tier
split and is the thing you actually have to set.

### Tier B — sandbox node

```dotenv
# identity & services
ENVIRONMENT=prod                                  # [P] not "local": gates dev shortcuts
ORCHESTRATOR_SERVICES=orchestrator,template-manager   # [L] both roles, one process
ORCHESTRATOR_PORT=5008                            # [P] consts.OrchestratorAPIPort default
GIN_MODE=release                                  # [P]

# storage — no object store
STORAGE_PROVIDER=Local                            # [L] storage_factory.go
ARTIFACTS_REGISTRY_PROVIDER=Local                 # [L] registry.go
LOCAL_TEMPLATE_STORAGE_BASE_PATH=/var/lib/e2b/templates    # [D] persist these two
LOCAL_BUILD_CACHE_STORAGE_BASE_PATH=/var/lib/e2b/build-cache  # [D]
ORCHESTRATOR_BASE_PATH=/var/lib/e2b/orchestrator  # [L]
ORCHESTRATOR_LOCK_PATH=/var/lib/e2b/.lock         # [L]
SANDBOX_CACHE_DIR=/var/lib/e2b/sandbox-cache      # [L]

# firecracker assets — build these for your arch
HOST_KERNELS_DIR=/opt/e2b/fc-kernels              # [L]
FIRECRACKER_VERSIONS_DIR=/opt/e2b/fc-versions      # [L]
HOST_ENVD_PATH=/opt/e2b/envd                      # [L]
HOST_BUSYBOX_DIR=/opt/e2b/busybox                 # [L]
NBD_POOL_SIZE=16                                  # [L]

# shared state — points at Tier A
REDIS_URL=<tierA>:6379                            # [D] was localhost:6379
CLICKHOUSE_CONNECTION_STRING=clickhouse://user:pass@<tierA>:9000/default  # [D]
LOGS_COLLECTOR_ADDRESS=http://<tierA>:30006       # [D]
OTEL_COLLECTOR_GRPC_ENDPOINT=<tierA>:4317         # [D]
REDIS_POOL_SIZE=10                                # [P]

# volumes & sandbox networking
DEFAULT_PERSISTENT_VOLUME_TYPE=default            # [L]
PERSISTENT_VOLUME_MOUNTS=default:/var/lib/e2b/volumes    # [L]
ALLOW_SANDBOX_INTERNAL_CIDRS=                     # [P] empty = private ranges denied
ENVD_TIMEOUT=                                     # [P]
DOMAIN_NAME=<your-domain>                         # [P]
```

Dropped from production and **not needed**: `CONSUL_TOKEN`, `TEMPLATE_BUCKET_NAME`,
`BUILD_CACHE_BUCKET_NAME`, `GOOGLE_SERVICE_ACCOUNT_BASE64`, `GCP_PROJECT_ID`, `GCP_REGION`,
`GCP_DOCKER_REPOSITORY_NAME`, `GCS_GRPC_CONNECTION_POOL_SIZE`, `PROVIDER`,
`SHARED_CHUNK_CACHE_PATH` (Filestore), `LAUNCH_DARKLY_API_KEY`,
`DOCKERHUB_REMOTE_REPOSITORY_URL`.

### Tier A — control node

```dotenv
# the whole point — no scheduler
SERVICE_DISCOVERY_PROVIDER=local                  # [D] cfg/model.go:64
LOCAL_ORCHESTRATOR_ADDRESS=<tierB>:5008           # [D] any host:port, NOT loopback-bound

ENVIRONMENT=prod                                  # [P]
GIN_MODE=release                                  # [P]
DOMAIN_NAME=<your-domain>                         # [P]
API_INTERNAL_GRPC_PORT=5009                       # [P]
ADMIN_TOKEN=<random>                              # [P]
SANDBOX_ACCESS_TOKEN_HASH_SEED=<random>           # [P]
AUTH_PROVIDER_CONFIG=<json>                       # [P] Ory or your OIDC issuer

# data
POSTGRES_CONNECTION_STRING=postgres://...         # [L]
AUTH_DB_CONNECTION_STRING=postgres://...          # [P] same DB is fine
CLICKHOUSE_CONNECTION_STRING=clickhouse://...     # [L]
REDIS_URL=localhost:6379                          # [L]
REDIS_POOL_SIZE=160                               # [P]

# volume tokens (api signs, orchestrator verifies)
VOLUME_TOKEN_ISSUER=<your-issuer>                 # [L]
VOLUME_TOKEN_SIGNING_METHOD=ES256                 # [L]
VOLUME_TOKEN_SIGNING_KEY=ECDSA:<base64>           # [L] generate your own
VOLUME_TOKEN_SIGNING_KEY_NAME=<name>              # [L]
DEFAULT_PERSISTENT_VOLUME_TYPE=default            # [L]

# observability
LOGS_COLLECTOR_ADDRESS=http://localhost:30006     # [L]
OTEL_COLLECTOR_GRPC_ENDPOINT=localhost:4317       # [L]
LOKI_URL=http://localhost:3100                    # [L] droppable once logs read from ClickHouse
TEMPLATE_BUCKET_NAME=skip                         # [P] literal "skip" — transitive import
```

And `client-proxy` on the same box:

```dotenv
SD_ORCHESTRATOR_PROVIDER=STATIC                   # [L] client-proxy DOES take a list
SD_ORCHESTRATOR_STATIC=<tierB>[,<tierB2>,...]     # [D]
SD_EDGE_PROVIDER=STATIC                           # [L]
SD_EDGE_STATIC=127.0.0.1                          # [L]
API_INTERNAL_GRPC_ADDRESS=localhost:5009          # [P] was api-internal-grpc.service.consul
EDGE_SECRET=<shared-with-api>                     # [L]
EDGE_URL=http://localhost:3000                    # [L]
NODE_IP=<tierA-ip>                                # [L]
REDIS_URL=localhost:6379                          # [L]
```

One trap: `SKIP_ORCHESTRATOR_READINESS_CHECK=true` appears in the local config. It is a dev
shortcut. Do not carry it.

Host prep on Tier B (from `DEV-LOCAL.md` and `.github/actions/host-init`):

```bash
sudo modprobe nbd nbds_max=64          # /etc/modules-load.d/nbd.conf
sudo sysctl -w vm.nr_hugepages=2048    # ~4 GB reserved; /etc/sysctl.d/99-hugepages.conf
ls -l /dev/kvm                         # must exist
```

### The limit: the API has no multi-node static

This corrects an earlier reading of mine. `STATIC` takes a list — but only for **client-proxy**
(`SD_ORCHESTRATOR_STATIC` → `StaticEndpoints []string`). The **API**, which is what actually places
sandboxes, validates `SERVICE_DISCOVERY_PROVIDER` against exactly `""`, `nomad`, `kubernetes`,
`nomad+kubernetes`, `local` (`packages/api/internal/cfg/model.go:291`) — **no `static`** — and its
`local` provider calls `servicediscovery.NewLocal(addr)`, which returns **one** instance.

So:

- **One sandbox node, split across two machines: works today.** `NewLocal` does no loopback
  check (`local.go:28–50`), so `LOCAL_ORCHESTRATOR_ADDRESS` can be any reachable host. The name is
  a misnomer: it means "single static address", not "same machine".
- **Several sandbox nodes without a scheduler: not a config option.** You would add `static` to the
  constant list and the validator, plus a `[]string` field calling `servicediscovery.NewStatic` —
  which already exists in the package the API imports. Perhaps ten lines. But it is a patch, which
  means a fork, which means maintenance. Plausibly a decent upstream contribution instead.

### Cost, roughly

Approximate, us-central1 on-demand, unverified against the calculator:

| shape | ~$/mo | sandbox capacity |
| --- | --- | --- |
| Published GCP guide, template defaults | 1,200–1,300 | 1 node, 8 vCPU / 30 GB (~24 GB after hugepages) |
| Two-tier on GCP (`e2-standard-2` + `n1-standard-8`) | 450–500 | same |
| Two-tier on bare metal (small VPS + Hetzner AX52) | 70–90 | 8c/16t, **64 GB** |

The delta is almost entirely the nested-virtualisation tax. Firecracker needs KVM; on a
hyperscaler you rent that through nested virt at a large premium, and on bare metal it is simply
present.

## Could this run in foldyard?

Reframing first: this is not the question
[firecracker-and-microvm-backends.md](./firecracker-and-microvm-backends.md) answers. That note
asks whether Firecracker could be foldyard's *machine backend* (verdict: no — no filesystem
sharing, TAP-only networking needing root per VM, and Linux-only, which inverts the ask). This
asks whether the E2B stack could run as a *consumer workload* on top of foldyard. Different layer,
different answer.

### The substrate: already measured, and it works

The M3+ instinct is right, and we have already proven it on our own hardware. From
[firecracker-and-microvm-backends.md](./firecracker-and-microvm-backends.md) (2026-09-07, M3 Max):

> `/dev/kvm` in the guest? **No** by default — but **yes** with `nestedVirtualization: true`
> (re-created and re-tested: `crw-rw-rw-. 1 root kvm 10, 232 /dev/kvm`).

And [nested-virt.md](./nested-virt.md) records the same for the podman backend: on Apple Silicon
**M3+ / macOS 15+**, `podman machine` under the libkrun provider turns nested virt on by default,
so `/dev/kvm` is live inside the L1 VM.

So the hard requirement chain is:

1. **Apple Silicon M3+ and macOS 15+.** Hypervisor.framework exposes nested virt only from M3.
   On M1/M2 there is no `/dev/kvm` in the L1 guest and therefore no Firecracker, full stop.
2. **A hypervisor that passes it through** — `podman machine` + libkrun (default on), or
   Lima + krunkit with `nestedVirtualization: true` (off by default; must be asked for).
   Note `MachineBackend.VMTYPE_PREFERENCE = ("vz", "qemu")` deliberately excludes krunkit from
   auto-selection, and **`vz` does not expose KVM** — so today's default posture cannot host this.
3. **arm64 Firecracker assets.** Good news: `firecracker/fc-versions/build.sh` takes `arm64`
   (`aarch64-unknown-linux-musl`), `fc-kernels/build.sh` cross-compiles with
   `ARCH=arm64 CROSS_COMPILE=aarch64-linux-gnu-`, there is a committed
   `fc-kernels/configs/arm64/6.1.102.config`, and CI cross-compiles every Go package for ARM64
   (`pr-tests-arm64.yml`). The *public prebuilt* bucket is likely amd64-only, so you would build
   your own — supported, not a fork.

Worth knowing: E2B's own macOS story is `packages/orchestrator/cmd/dummy-orchestrator` +
`pkg/dummyserver` — a stub that registers `IsBuilder=false` so it is never selected for builds.
They do not run real sandboxes on Darwin at all. That is fine for us, because we would run in the
Linux L1 guest rather than on Darwin — but it means zero upstream support for the Mac path.

### The actual blocker: posture, not hardware

The orchestrator needs root, `/dev/kvm`, TAP device creation, NBD mounts and cgroup control on the
machine it runs on. In foldyard terms that is **not the dev box**. `nested-virt.md` is explicit
that the box deliberately does not get the device:

> the project's dev box ← where you normally work / agents run
> (no `/dev/kvm` passed in → can't nest; not its job)

So E2B's Tier B would have to be either the L1 VM itself or a privileged sibling container over the
socket — architecturally the same shape as the rig's "host" container, and the same shape
`fy verify` exists to assert *against*. The battery checks engine rootless, escape refused,
no `/Users` in a privileged container, VM mount table free of host paths. A container holding
`/dev/kvm` + `CAP_NET_ADMIN` + `CAP_SYS_ADMIN` + root is a deliberate hole in exactly that wall.

That is the interesting tension, and it is not a hardware problem. It is: **foldyard's isolation
posture and E2B's orchestrator want the same privileges pointed in opposite directions.** Firecracker
is a strong boundary — that is the Trail of Bits finding the other note starts from — so the
sandboxes themselves would be well contained. The exposure is the *host* side: the thing that
launches them needs privileges the wall is built to deny.

There is also a plain resource question. Hugepages alone reserve ~4 GB, the guide wants 8 GB
minimum and 16 GB recommended, and that is on top of the L1 VM's own allocation and whatever the
dev box is doing. On a 36 GB M3 Max that is workable; on a 16 GB machine it is not.

### Shapes, if this were pursued

1. **A `[machine].vmtype = krunkit` + `nestedVirtualization: true` machine whose L1 guest runs the
   E2B stack directly** — no dev box in the path, closest to the CI-validated single-host
   topology, and it keeps the privileged surface inside the VM boundary rather than in a
   container beside the box. Cheapest to try.
2. **A privileged sibling container** (the rig's "host" shape) running Tier B, with Tier A in a
   normal box. Fits foldyard's existing patterns; punches the hole `verify` checks for.
3. **Don't.** Run Tier B on a Linux box with native KVM — a Hetzner AX52 at ~€60/month gives more
   sandbox capacity than the $1.2k GCP deployment — and point a local Tier A at it over
   `LOCAL_ORCHESTRATOR_ADDRESS`. No nested virt anywhere, no posture compromise, and the M3+
   constraint disappears.

Option 3 is the one I would cost first, precisely because it makes the interesting question
("can we nest this?") unnecessary.

## Open questions

1. **Will E2B open the Helm charts?** If yes, self-hosting gets much easier and k3s on two boxes
   becomes plausible. If no, the Nomad IaC is a frozen base to fork. Worth asking upstream directly
   — it is a cheap question with a large answer.
2. **Is multi-node sandbox capacity needed?** If one box suffices, nothing needs patching. If not,
   the API `static` provider is ten lines and arguably belongs upstream.
3. **Do templates get built while sandboxes serve?** The one piece of E2B's topology that is
   load-bearing rather than tenancy-driven is the build/sandbox split. Co-location means a heavy
   build competes with live sandboxes for CPU, disk and page cache.
4. **Does hosting this contradict foldyard's own thesis?** foldyard's boundary is host vs box.
   E2B's is host vs microVM, one level down and stronger. Running one inside the other means the
   *inner* boundary is better than the outer one, which is either a reason to be interested or a
   sign the layering is wrong. Not obvious which.

## Sources

- `e2b-dev/infra` at `fc9bc5b` (2026-09-09): `self-host.md`, `DEV-LOCAL.md`,
  `docs/ARCHITECTURE.md`, `.env.gcp.template`, `iac/provider-gcp/`,
  `packages/shared/pkg/servicediscovery/`, `packages/shared/pkg/storage/`,
  `packages/api/internal/cfg/model.go`, `packages/api/internal/handlers/store.go`,
  `.github/workflows/integration_tests.yml`, `.github/actions/integration-tests/`,
  `firecracker/fc-kernels/`, `firecracker/fc-versions/`, the three committed `.env.local` files.
- This repo: [nested-virt.md](./nested-virt.md),
  [firecracker-and-microvm-backends.md](./firecracker-and-microvm-backends.md),
  `src/foldyard/machine_backend.py`.
- Cost figures are order-of-magnitude from list prices, **not** verified against the GCP pricing
  calculator or current Hetzner pricing.
