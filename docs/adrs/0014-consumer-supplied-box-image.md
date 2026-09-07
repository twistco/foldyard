# ADR-0014 — Box image contract: consumer-supplied image + runtime injection at box-up; no foldyard base image

- **Status:** Accepted (2026-06-14; generic default image + bootstrap skill added 2026-06-21;
  generic base moved to Debian 2026-08-27, see Amendment) — implemented (`src/foldyard/box.py`,
  `src/foldyard/config.py::box_image`, `src/foldyard/assets/box/Dockerfile`)
- **Sources:** PLAN.md §7.4 (box image contract), §8.1.1 (generic default box), §12 (bundled
  bootstrapping skill), src/foldyard/box.py

## Context

The dev-box image is mostly the **consumer's project toolchain** (node/python/build deps) —
foldyard never touches that, and "mostly project-dependent" is correct, not a problem to fix. But
foldyard's box features (drive the stack over the socket, self-install the CLI, mount the proxy
CA and mode mirror, plugin tooling) still need a few things present in whatever image the
consumer brings. The design problem: satisfy those needs *without* foldyard owning the image.
The box splits into three concerns — project toolchain (consumer-owned), foldyard's own
needs (deliberately tiny), and plugin needs (plugin-declared, posture-dependent) — and only the
middle one is foldyard's to solve.

## Decision

**Lean on runtime injection at box-up, not a foldyard base image.** The image contract is
minimal and mostly *not* a build-time concern:

- **Required in the image:** an engine client that speaks the mounted socket (podman **or**
  docker — near-universal in dev images) plus `git`. A compose provider is required only for
  in-box stack control (the agent-autonomy persona); host-driven stacks don't need it in the box.
- **Everything else is injected at box-up** (`box.py::_up`), the pattern the original `devbox`
  recipe already proved: the machine socket bind-mounted to `/var/run/docker.sock` with
  `CONTAINER_HOST`/`DOCKER_HOST` pointed at it; the staged proxy CA and posture mirror; plugin
  env/mounts via `registry().box_args`; a shared on-PATH tools prefix (`devbox_tools` volume at
  `/opt/fy-tools`); and a monitored bootstrap (`run_step`, ✓/⏭/✗ per step + a failure summary)
  that installs the foldyard CLI itself plus each enabled plugin's `box_bootstrap` steps and the
  consumer's `[[box.tools]]` / `[box].bootstrap`.
- The **foldyard self-install never assumes an editable checkout on the mount**
  (`box.py::_foldyard_install_subst` / `_foldyard_run`): a repo that vendors foldyard installs
  editable from the mount; an editable host install stages a wheel into a VM-visible
  `.devbox-foldyard/` dir (`stage_foldyard_for_box`) so the box pins to the exact host version;
  a published host install pins `foldyard==X`; else — no wheel, no detectable host version, no
  vendored source — the install **fails closed** rather than pulling an unpinned `foldyard` that
  could drift from the tested version.
- The contract is **declared and fails loudly** rather than mysteriously: the packaged
  Dockerfile documents it, the bootstrap reports every failed step by name, and `fy doctor`
  probes the in-box engine socket — a non-conforming image says "install X", not
  "connection refused".

**A packaged generic box image covers repos with no custom image** (added 2026-06-21, PLAN
§8.1.1). `box.py` only knows how to *build* from a Dockerfile, so foldyard ships
`src/foldyard/assets/box/Dockerfile` — a base (`quay.io/podman/stable` at decision time,
`debian:trixie-slim` since the 2026-08-27 amendment below) + git + uv, exactly the minimal
contract (uv is there so box-up can `uv tool install foldyard`; uv brings its own managed
Python). When a consumer declares no `[box].image`, `config.box_image()` falls back to that
packaged Dockerfile **with the packaged dir as build context** — the generic box must not depend
on repo contents. Consequence: any repo can `foldyard init --box-only && foldyard box up` with
zero Dockerfile or compose authoring. Consumers that declare `[box].image` are unaffected
(context defaults to the repo root; `target`/`build_args`/`context` are supported).

**A bundled skill grows the generic box into a tuned image by observation.** The gap
from generic box to a real `[box].image` used to be "go write a Dockerfile from scratch". The
packaged `bootstrap-devbox` skill (`src/foldyard/assets/skills/bootstrap-devbox/`, installed into
a consumer repo via `foldyard skill install bootstrap-devbox`) drives the loop instead: use the
running box as a REPL for the image — install interactively, test against the real repo, record
what worked — then codify the transcript into a local `box.Dockerfile` layered on the generic
base, point `[box].image` at it, rebuild, and re-run `foldyard verify`. It ships in-package so it
versions with foldyard and needs no separate registry.

## Consequences

- Projects with an existing dev image keep it untouched; foldyard's layer arrives at box-up as
  mounts + env + installs, so upgrading foldyard never means rebuilding the consumer's image
  (only re-running `fy box up`).
- Tangible (consumer #1) keeps its own `dev-stack/box.Dockerfile` with `docker-cli` as its engine
  client — the contract says "an engine client", not "podman specifically" (the engine-selection
  decision made podman the default *foldyard* drives, with docker as fallback).
- The box stays light: every self-install branch installs foldyard **bare** (never the `[host]`
  extra) because the box only routes egress through the Mac's proxy, it never runs mitmproxy —
  which also shrinks bootstrap egress.
- Zero-authoring onboarding works end to end: `foldyard init --box-only && foldyard box up` on a
  stack-less repo, then the skill when the generic box stops being enough.
- Open at decision time, resolved pragmatically since: whether the foldyard CLI
  in the box is required or opt-in (the agent persona needs it; the bootstrap installs it
  unconditionally today), and contract strictness (current behaviour: degrade with loud
  per-step failure reports rather than hard-fail).

## Rejected alternatives

- **A foldyard-owned base image consumers must inherit from** — couples every consumer to
  foldyard's distro/toolchain choices and breaks projects that already have an image. Rejected
  outright (2026-06-14).
- **A devcontainer Feature (`ghcr.io/foldyard/foldyard`) or install-script `RUN` snippet** — the
  same layer bound earlier (baked at build time, cached) for consumers who prefer it. Not
  rejected on principle, but explicitly deferred: optional, may ship later, revisit on demand
  (reaffirmed out-of-scope in the spin-out plan).

## Amendment (2026-08-27) — generic base: `quay.io/podman/stable` → `debian:trixie-slim`

The original base was picked for the contract's engine client — the podman project's own image
ships a current podman for free — and Fedora was only what that image happened to be built on.
The distro turned out to be the part consumers actually live with, and it charged rent:

- **dnf's metalink mirror system resolves to arbitrary hosts** no egress allowlist can name, so
  every locked-down consumer needed a `dnf-direct-mirror` workaround step pinning
  `dl.fedoraproject.org` (plus `dnf-keepcache` to make a cache volume work). apt pulls from one
  stable host (`deb.debian.org`) an allowlist names in one line.
- **Playwright does not support Fedora**: browsers run as an ubuntu fallback build with a BEWARE
  warning, and `playwright install-deps` (apt-only) can't install the system libraries — so
  consumers hand-maintained a dnf library list, which is exactly what silently broke when an
  allowlist regression suppressed it (the incident that prompted this change).
- Most `[[box.tools]]` recipes and tool docs assume apt.

The contract itself is unchanged — the box needs an engine CLIENT for the mounted socket, never a
nested engine — and Debian satisfies it with one `apt-get install podman` in the generic
Dockerfile. The nested-virt spikes and docs keep `quay.io/podman/stable`: they genuinely run
podman *inside* the container, which is the job that image is actually for. Consumers with their
own `[box].image` are unaffected, as ever; generic-box consumers pick the new base up on their
next image rebuild and swap their dnf tool steps for apt (or for `playwright install-deps`,
which now works).
