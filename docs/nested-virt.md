# Nested virtualization — validating host-only paths headlessly

How to validate foldyard's tier-4 surface (`machine ensure|recreate`, `box up|build`,
`host`, `mode set` — the paths that refuse to run inside a dev box; see the test tiers in
[DEVELOPMENT.md](../DEVELOPMENT.md)) without a Mac round-trip: a throwaway sibling container
with `/dev/kvm` plays the "host" role and runs `podman machine` itself.

> **This page is a TEST RIG, not a posture recommendation.** Nesting here exists for exactly one
> reason: tier-4 verbs refuse to run in a box, so validating them headlessly needs a second
> host+engine, and that needs `/dev/kvm`. Every hypervisor choice below is chosen for *whether it
> exposes KVM to the guest*, nothing else. In particular, **libkrun appears here because it is
> the route to `/dev/kvm`, not because it is a microVM** — do not read the rig's provider as a
> statement about what a consumer's machine should run. (That inference has been made once
> already; see [firecracker-and-microvm-backends.md](./firecracker-and-microvm-backends.md),
> which discusses libkrun on its security merits *separately* and reaches its own conclusions.)
> The product's VMM is `[machine].vmtype`, and nothing on this page bears on it.

## Architecture

On Apple Silicon **M3+ / macOS 15+**, `podman machine` using the **libkrun** provider turns
on nested virtualization by default, so `/dev/kvm` is live *inside* the machine VM. A
throwaway sibling container launched over the socket with `--device /dev/kvm` therefore gets
real, hardware-accelerated nested KVM. That gives an isolated host+engine to run a project's
full lifecycle without touching the live box — or a Mac round-trip.

The crucial design point: **only one level of VM-nesting is needed**, and it's the level
that works reliably on ARM. The "host"/"Mac" role is played by a *container*, not a second
VM; that container runs `podman machine` itself to create the single nested VM.

```
┌─ Mac · Apple Silicon M3+ · macOS 15+ ─────────────────────────────────────────────┐
│  Hypervisor.framework with NESTED VIRT enabled                                    │
│                                                                                   │
│  ┌─ L1: podman machine VM (libkrun provider) ───────────────────────────────────┐ │
│  │   ├─ rootless podman engine  ·  /dev/kvm PRESENT (Mac exposes nested virt)   │ │
│  │   │                                                                          │ │
│  │   ├─ the project's dev box   ← where you normally work / agents run          │ │
│  │   │     (no /dev/kvm passed in → can't nest; not its job)                    │ │
│  │   │                                                                          │ │
│  │   └─ "foldyard host" container  (sibling over the socket, --device /dev/kvm, │ │
│  │        NOT given the dev-box signature → foldyard sees it as the HOST/"Mac") │ │
│  │        • foldyard + host daemons (supervisor · minters · egress proxy)       │ │
│  │        • `foldyard machine init`  ──────────────┐                            │ │
│  │                                                 ▼                            │ │
│  │            ┌─ L2: podman machine VM (KVM-accelerated) ────────────────────┐  │ │
│  │            │   foldyard's "machine" for the example consumer              │  │ │
│  │            │   ├─ example stack:  db (postgres) + api (fastapi)           │  │ │
│  │            │   └─ foldyard dev box  (example/box.Dockerfile)              │  │ │
│  │            └──────────────────────────────────────────────────────────────┘  │ │
│  └──────────────────────────────────────────────────────────────────────────────┘ │
└───────────────────────────────────────────────────────────────────────────────────┘
       nesting depth: Mac(L0) → VM(L1) → VM(L2).  ONE nested VM (L1→L2).
```

Why this shape:
- **`podman machine create` inside the L1 VM works** because the VM has `/dev/kvm`; anything
  with the device (a sibling container) can run `podman machine init` → an L2 VM. One nesting
  level — the kind ARM nested-virt supports dependably.
- **A separate "host" container (not the dev box)** because foldyard's host-only commands
  guard on `in_box()`. A fresh sibling without the dev-box signature reads as the host, so
  foldyard behaves exactly as it would on a developer's Mac.
- **Not a third VM as "Mac"** — that would be L1→L2→L3 (triple nesting); ARM KVM gives one
  nesting level reliably, not two. Keeping the host role in a *container* stays at one VM
  level while still faithfully exercising `podman machine`.

**Fallback** if `podman machine`-in-a-container proves too fiddly (gvproxy networking,
systemd-in-container): the sibling container boots a single plain qemu/KVM Linux VM as the
"machine" and foldyard points its engine there — validates `up`/`box`/`verify`/`host`; the
thin `machine` wrapper (`podman machine init/start`) is then validated in isolation.

## Prerequisites & known-good setup (researched 2026-06-14)

Nested KVM on Apple Silicon needs **all** of: an **M3 or newer** CPU, **macOS 15+**
(Apple added EL2 nested virt there), and `podman machine` on the **libkrun** provider.
No other engine is required — podman+libkrun is the whole stack (podman → krunkit →
libkrun-efi → Apple Hypervisor.framework). UTM/Lima/Parallels are *peers* that also get
M3 nested virt, not layers you add under podman; you don't need them here.

- **podman ≥ 5.6.0** (5.6.0 turns nested virt *on by default* for libkrun on M3/macOS15;
  5.6.2 known-good). **Avoid 5.7.0-rc2** — krunkit abort-trap regression (podman #27427).
- **Select libkrun persistently** in `~/.config/containers/containers.conf`:

  ```toml
  [machine]
  provider = "libkrun"
  ```

  Do *not* rely on `CONTAINERS_MACHINE_PROVIDER` — it's
  only read at init and not propagated to later `podman machine` commands (podman-desktop
  #9860 makes them appear broken).

  **On the default (rechecked 2026-09-07):** at the time of the original research the default
  was `applehv`, which does **not** expose `/dev/kvm`, so opting in was required. Upstream has
  since been moving the macOS/arm64 default to libkrun (podman-desktop's docs now describe it
  as the default; the podman-side change is tied to 6.0 dropping Intel Macs), so the default is
  **version-dependent — don't assume either way**. It matters less than it looks: a machine's
  provider is fixed **at init**, so an existing machine keeps whatever it was created with
  regardless of the current default or config. Check what you actually have rather than
  inferring it:

  ```bash
  podman machine info --format '{{.Host.VMType}}'   # the provider in force
  pgrep -lf 'krunkit|vfkit'                         # krunkit ⇒ libkrun, vfkit ⇒ applehv
  ```
- **Size the machine sanely:** `--cpus` ≤ physical cores — oversizing *hangs or aborts* the
  machine on start (podman #23194 / #28322). Keep memory conservative too.

### The L1 need not be a podman machine any more (2026-09-07)

This recipe pins the L1 to `podman machine` + libkrun because, when it was written, that was the
only configuration known to expose `/dev/kvm`. Two things have changed since, and both point the
same way:

- **Lima's `vz` driver supports nested virtualization** — `nestedVirtualization: true` in the
  instance YAML, gated at runtime on `vz.IsNestedVirtualizationSupported()` (Lima
  `pkg/driver/vz/vm_darwin.go`). Default off. Same M3+/macOS 15 hardware requirement, since it is
  the same Apple EL2 support underneath.
- **Lima's `krunkit` driver does too, and it is MEASURED** (2026-09-07, M3 Max / macOS 26.6.1 /
  Lima 2.1.3): a throwaway instance created from `template:podman` with `.vmType = "krunkit"` and
  `.nestedVirtualization = true` boots with `crw-rw-rw-. 1 root kvm 10, 232 /dev/kvm` present in
  the guest. Without the flag it is absent — unlike podman's libkrun, Lima's krunkit does not turn
  it on for you. The same probe confirmed the forwarded libpod socket, a working virtio-fs mount,
  and **two libkrun VMs running concurrently** (the probe beside a live podman machine).
- **Lima is the default backend** (ADR-0011, 2026-08-29), so the podman-machine L1 is now the
  *odd* one out: the rig depends on a backend most consumers no longer run, which is a poor place
  to validate host-only verbs from.

A Lima L1 would also settle the deferred fragility question directly, since that is precisely the
comparison it asks for. The `/dev/kvm` prerequisite is no longer the obstacle — that part is
measured above; what is untried is a full L2 guest under a Lima L1. Untried as of this note — the version pins above (podman ≥ 5.6.0, avoid
5.7.0-rc2) are historical from the 2026-06 research and predate podman 6.x; re-derive them rather
than trusting them if you pick the podman route again.

## Validated so far

- `/dev/kvm` passes through to a `--device /dev/kvm` sibling container and `KVM_CREATE_VM`
  works; the full CLI dogfood (`foldyard up` → example api serves `/db` from Postgres) runs
  in such a sibling — codified as the tier-3 e2e, which also passes against the dev box's
  own machine socket.
- A real **L2 VM** boots (limactl, qemu `-accel kvm`, Ubuntu aarch64) — but nested EL2 on
  Apple HVF is **operationally fragile under a real guest workload**: a 2-vCPU guest starved
  the L1 machine's control plane (`docker exec` timeouts; Mac-side `podman machine stop/start`
  to recover). Good for `KVM_CREATE_VM` smoke + thin `machine`-wrapper validation, NOT a
  comfortable place to run a full guest + workload.

## `tests/test_proxy_box_e2e.py` — box-side proxy validation (adapted topology)

Drives the REAL `ProxyPlugin` (daemon spec, CA mount + proxy env from `box_args`) to bring up
a real dev-box container over the socket, with a fake minter + fake TLS upstream: egress is
rewritten in flight, the CA mount is load-bearing, and a 401 re-mint reaches the box as a 200.
**"Adapted"** because the faithful topology (proxy at `host.containers.internal:8088`) needs
foldyard to run as the *host*, which the `in_box()` guards forbid inside a dev box — so the
test runs proxy + upstream in-process and points `FY_PROXY` at this container's own IP; the
only divergence from production is that proxy address. Two constraints any sibling-container
work must respect: `-v` mount sources resolve on the **VM** (mount repo-relative paths, not
`tmp_path`), and `box_args` reads the CA path from the ambient `MITMPROXY_CA`, not its `env`
arg. The faithful `foldyard box up` + `host` path stays the nested-KVM/Mac recipe below.

### Recipe — dogfood the CLI against a nested engine

`<repo>` = the checkout path on the machine VM; run from the Mac or from inside the dev box.

```bash
# 1. long-lived "host": engine + the kvm device, repo bind-mounted. NB *no* proxy env — see
#    the egress note below; a TLS-intercepting dev proxy breaks the image build's pip.
docker run -d --name fy-nested --privileged --device /dev/kvm \
  -v <repo>:/repo quay.io/podman/stable sleep infinity
# 2. tools: uv (foldyard installs AS a uv tool — NOT pip) + a compose provider. Both ship in
#    podman/stable's dnf (Fedora); podman+git are already there.
docker exec fy-nested dnf -q install -y uv podman-compose
# 3. install the foldyard CLI the way it is really installed — `uv tool install` (→ ~/.local/bin)
docker exec fy-nested bash -lc 'uv tool install /repo/foldyard'
# 4. the example as a git repo (foldyard's main_repo() shells git) + the podman API socket
docker exec fy-nested bash -lc 'rm -rf /example && cp -r /repo/foldyard/example /example &&
  cd /example && git init -q && git add -A && git -c user.email=a@b -c user.name=x commit -qm init'
docker exec fy-nested bash -lc 'mkdir -p /run/podman &&
  (podman system service --time=0 unix:///run/podman/podman.sock &) && sleep 3'
# 5. drive foldyard in box-mode (IN_DEVBOX=1 ⇒ no machine mgmt; DOCKER_HOST ⇒ the local socket)
docker exec fy-nested bash -lc 'export PATH=$HOME/.local/bin:$PATH; cd /example &&
  IN_DEVBOX=1 FOLDYARD_REPO=/example DOCKER_HOST=unix:///run/podman/podman.sock foldyard up'
# 5b. the db has `depends_on: service_healthy`; a bare host has no systemd to run the
#     healthcheck timer, so trigger it once to unblock `up` (the api then starts):
docker exec fy-nested bash -lc 'podman healthcheck run fyex_db_1'
# 6. teardown — GRACEFUL only:  docker stop fy-nested && docker rm fy-nested
```

**⚠ Egress / build:** image *builds* that fetch from the network (the example's
`pip install fastapi …`) go DIRECT to PyPI here and verify with the base image's system
roots — so do **not** set `HTTPS_PROXY` on the host sibling. Routing a build through a
TLS-intercepting dev egress proxy (e.g. a consumer dev box's always-on proxy) fails with
`CERTIFICATE_VERIFY_FAILED` unless the *build* trusts the proxy CA — a property of your
test env, not foldyard or the example. (Installing foldyard itself with `uv` works either
way — direct, or through the proxy with the combined CA bundle on `SSL_CERT_FILE`.)

**⚠ Healthcheck caveat:** podman runs compose healthchecks via **systemd timers**. A bare
`sleep infinity` host has no systemd, so a service with `depends_on: condition:
service_healthy` (the example's db) stays `starting` and `up` blocks. The real machine
(Fedora CoreOS) has systemd, so the e2e is clean there; in a bare nested host either run
the host with systemd as init, or trigger the check manually (`podman healthcheck run
<ctr>`, step 5b).

## Still to validate (deferred)

- The full **`foldyard machine ensure/recreate` / `box up` / `host`** wrappers driven over a
  nested VM end-to-end (the L2 VM *boots*; the thin lifecycle wrappers on top are the untested
  part). Needs qemu+gvproxy in the host container and the careful graceful teardown below.
- **Whether nested VMs behave better under a Lima machine** (QEMU/VZ) than under
  podman/libkrun — the L2 fragility above was observed on libkrun; a Lima-backed L1 might
  starve less under a real guest workload. ~~Needs a Lima-backend host to try~~ — **no longer
  blocked (2026-09-07):** lima has been the default backend since ADR-0011's 2026-08-29
  amendment, so the host to try it on is the ordinary one. See the L1 note below.
- **Don't use the nested rig for *network*-forcing tests** — it adds an L2 that only burns CPU
  and doesn't reproduce macOS net stacks. The in-VM nftables forcing wall is identical regardless
  of how the VM is made; test it in a **native Lima VM on the Mac** instead. See
  [lima-network-forcing-kit/](./lima-network-forcing-kit/) (the runnable kit).

## ⚠ Caution: tear nested guests down gracefully (this bites hard)

An ungraceful teardown of a nested KVM guest can wedge the **L1** podman machine — observed
firsthand (the machine went sluggish and needed recreating, which is slow). This matches the
general KVM "host freeze on ungraceful guest teardown" failure class (there's no single filed
libkrun bug for it, but nested EL2 on Apple HVF is young and lightly battle-tested). Rules:
- **Never `pkill -9` the inner qemu/VMM**, and never `docker rm -f` a container that's running
  a nested guest. SIGKILL of the VMM is the thing that wedges L1.
- **Tear down inside-out, gracefully:** `poweroff` the nested guest from *inside* it (ACPI) →
  stop the container → `podman machine stop`/`rm` the *nested* machine from its host container.
- **Treat the nested machine as disposable:** if it wedges, `podman machine rm <name>` + re-init.
- **If L1 itself goes sluggish, recreate the L1 podman machine from the Mac** — never an
  in-box force-kill (that's what dragged it down).
