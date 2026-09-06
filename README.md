# foldyard

> Bring your whole dev stack into the fold. **Secretless by default.**

Foldyard is a laptop-local, isolated development environment for your project's
**entire** dev stack — not just an agent, not just a shell. Your docker-compose
services (databases, emulators, app servers), your dependency installs, your IDE
backend, and optionally your coding agent all run together inside a throwaway
Linux VM running **rootless Podman** — one that mounts only the repo, and nothing
else.

The yard starts with **zero credentials**: no `~/.ssh`, no tokens, no credential
helpers. With no posture declared there are no keys to push with and nothing to
lift — and access is never baked in: it's opt-in, scoped, and TTL-bound (thesis
2). A malicious npm/PyPI package wakes up with no ambient credentials to lift and
no push access; any access it *is* granted is injected at a host-side proxy, so
the token itself never enters the yard.

A *fold-yard* is the enclosed farmyard where animals are folded (penned) for the
night. Same idea here: everything that runs untrusted code lives in the yard;
your machine — keys, browser profile, other repos — stays outside the fence.

## Three theses

1. **Stack colocation.** The unit of isolation is the *project*, not the agent.
   Agent sandboxes are plentiful; isolated dev environments exist (Devcontainers,
   Codespaces, Gitpod, Coder). What's genuinely missing is an environment that
   gives the agent — *or just you* — the real compose stack, real e2e loop, and
   real services by container name **inside the same boundary that holds zero
   credentials**.

2. **Secretless by default, posture on demand.** The yard starts with zero
   credentials. When real access is needed, you *declare a posture*
   (`foldyard mode gcp=logs github=app`) and host-side daemons — running outside
   the blast radius — mint short-lived, scoped tokens and inject them at the
   egress proxy, so the token reaches the upstream API but never the yard itself.
   Emergency rungs are TTL-bound and auto-revert. No daemon running ⇒ no
   credential flows, whatever any file inside the yard says.

3. **Isolation without hiding.** Safety comes from the VM moat + rootless Podman
   + restricted mounts: a container escape lands the attacker inside the
   disposable Linux VM, not on your machine — there is no host filesystem mounted
   to reach even at VM level, and the `nsenter` escape test is *refused*. Because
   the boundary doesn't depend on concealment, nothing needs to be nested or
   hidden: Podman Desktop shows every container, and `foldyard verify` asserts the
   machine exposes *only* the repo (no stray `$HOME` bind-mount — the one default
   that would quietly break everything) and exercises a battery of known escapes
   on demand. (It tests the escapes we know about — it raises assurance, it
   doesn't prove a negative.)

## Before you adopt: a clean repo

Secretless is a precondition foldyard *relies on*, not a transformation it
performs. The yard mounts only your repo, so the guarantee holds precisely when
that repo carries **config, not credentials** — env vars that point at services,
not the keys to them. foldyard can mount a clean repo; it can't clean a dirty one.

So step one is a scan, not an install. Run
[gitleaks](https://github.com/gitleaks/gitleaks) or
[TruffleHog](https://github.com/trufflesecurity/trufflehog) across your history
first, and move anything they surface into a posture. Re-run the scan as part of
your routine (folding it into `foldyard verify` is on the roadmap) so the
precondition can't silently rot back into the repo.

## Who it's for

- **Agent users** — hand Claude Code (or any agent) the **in-VM machine socket**:
  full stack autonomy, bounded to the repo and containers *inside the VM*. The
  socket controls the machine from within the moat, not your host. Safer
  autonomy, not less of it.
- **Everyone else** — supply-chain worms (Shai-Hulud and friends) ride
  `npm install` and lifecycle scripts on developer laptops. Run installs, dev
  servers, and your IDE backend in the yard and the worm wakes up with no ambient
  credentials and no push access — its reach bounded to the repo it can already
  see (which the clean-repo prerequisite keeps boring). The box's egress routes
  through the host-side proxy: every destination is logged (SNI-only by default,
  full decrypt-and-log with `capture=on`), and an opt-in **default-deny egress
  wall** (`[proxy] default_deny` + live host grants) refuses any host you haven't
  allowed. One honesty note: that routing rides proxy env vars, which cooperative
  software honors and malware may not — `[machine].wall = true` (what `foldyard
  init` writes) closes it by provisioning a fail-closed firewall into the VM
  itself, making the proxy the only way out. Mind the one overlap: declaring a
  posture while running an untrusted `install` step is the window where
  install-time code and a live token coincide.

## Shape

- **Core**: machine lifecycle · compose-stack runner (worktree-namespaced
  projects, shared caches) with declarative posture overlays
  ([`[[overlay]]`](docs/compose-overlays.md): layer a `-f` file when the mode matches, config
  only) · long-lived dev box (multi-session, shadow volumes,
  IDE-attachable) · `verify` isolation battery (mount audit + escape tests +
  posture checks) · mode/posture substrate with TTLs and host-side supervisor ·
  credential-injecting egress proxy with capture logging, an opt-in default-deny
  egress wall, and VM-level fail-closed forcing (`[machine].wall`) ·
  doctor · TUI.
- **Plugins**: credential minters (GCP metadata emulator, GitHub App
  header-injection, generic header/query-param-auth APIs) · editor attach
  (VS Code) · agents (Claude Code and Codex — keyless: the proxy injects your
  key host-side, a dummy lives in the box).

One Python CLI (`uv tool install foldyard`; also aliased as `fy` — `fy <verb>` == `foldyard <verb>`), one `foldyard.toml` in your repo,
host state in `~/.foldyard/`. Project-specific task runners (`just`, npm
scripts) stay yours — foldyard runs *under* them, not instead of them.

Getting started in any repo ([docs/quickstart.md](./docs/quickstart.md) is the walkthrough):
`foldyard init` scaffolds a `foldyard.toml` that is
live for an isolated box-only VM (no compose stack, no agents) with everything
else — agents, editor attach, stack wiring — present as commented blocks you
uncomment to opt in; it also fences foldyard's box-generated dirs into your
`.gitignore`. Then `foldyard box up`. Projects with no custom image use
foldyard's packaged generic box (engine client + git + uv); to grow it into a
tuned image, install `foldyard skill install bootstrap-devbox` and run the skill.

Each project gets its own rootless VM, scoped by project name. The default backend
is **Lima** (`brew install lima`), because it runs the per-project VMs side by side
— so a project's open `box shell` / `code` session survives while you work in
another — and because it's the backend that can be provisioned with the
fail-closed wall above. You'll also need `podman` on the host: the backend creates
the VM, podman drives the socket it hands out. Other backends, and when to pick
them, are in [docs/configuration.md](./docs/configuration.md#machine).

## Status

The engine runs daily in production. The user guides live under [docs/](./docs/)
(start at the [docs map](./docs/README.md)); the "why" behind each design call in
[docs/adrs/](./docs/adrs/).

**Host platform: macOS is what's validated.** There is no platform branching in the package — no
`sys.platform`, no `Darwin` test — and `[machine].backend = "native"` exists for Linux and WSL2
hosts, but that path is unexercised and much of the prose still says "Mac" where it means "the
machine running `fy host`". Treat Linux and WSL2 as *untested*, not unsupported. The box is Linux
either way.
