# foldyard

> Bring your whole dev stack into the fold. **Secretless by default.**

Foldyard is a laptop-local, isolated development environment for your project's
**entire** dev stack — not just an agent, not just a shell. Your docker-compose
services (databases, emulators, app servers), your dependency installs, your IDE
backend, and optionally your coding agent all run together inside a throwaway
Linux VM running **rootless Podman** — one that mounts only the repo, and nothing
else.

## TL;DR

```bash
brew install uv lima podman        # lima creates the VM; podman drives the socket it hands out
uv tool install "foldyard[host]"   # puts `foldyard` on your PATH, with `fy` as shorthand

cd your-repo
foldyard init                      # writes foldyard.toml: a locked-down, stack-less box, with
                                   # agents / stack / editor attach as commented opt-ins
fy box up                          # VM + dev box; egress fail-closed through a host-side proxy
fy box shell                       # you're in the yard
```

No custom image required — foldyard ships a Debian box (engine client, git, uv). To grow it
into one tuned for your repo: `foldyard skill install bootstrap-devbox`, then run the skill.
An agent inside the box (`fy claude/fy codex`) is told what `fy` is and how to edit the
config, so you can let them help you. The config changes won't apply automatically, but
instead you'll see the changes and have to approve on the next `fy box up`. This way the
agent inside the box can't sneak changes in, but you can still share the config with your team.

**One prerequisite: a clean repo.** The yard mounts only your repo, so the zero-credential
guarantee holds exactly when that repo carries *config, not credentials*. Scan your history
with [gitleaks](https://github.com/gitleaks/gitleaks) or
[TruffleHog](https://github.com/trufflesecurity/trufflehog) first and move anything that
surfaces into a posture. Foldyard can mount a clean repo; it can't clean a dirty one.

The walkthrough is [docs/quickstart.md](./docs/quickstart.md) — and the whole manual ships
with the install, offline and version-matched: `fy docs`.

## Architecture

Everything that holds a credential stays on your machine; the yard gets the repo and nothing
else. One picture of the whole split:

![Foldyard architecture: the host runs the CLI, supervisor, credential minters and egress
proxy; the yard is a throwaway Linux VM holding the compose stack, dev box, coding agent and
per-worktree boxes, with `fy verify` auditing the boundary from
inside.](./docs/assets/foldyard-architecture.svg)

## Why

A *fold-yard* is the enclosed farmyard where animals are folded (penned) for the night. Same
idea here: everything that runs untrusted code lives in the yard, and your machine — keys,
browser profile, other repos — stays outside the fence. Three ideas hold it up.

**The unit of isolation is the project, not the agent.** Agent sandboxes are plentiful, and
isolated dev environments exist (Devcontainers, Codespaces, Gitpod, Coder). What's genuinely
missing is one boundary that holds the real compose stack, the real e2e loop and real services
by container name *and* zero credentials. → [prior-art.md](./docs/prior-art.md)

**Secretless by default, posture on demand.** The yard starts with no `~/.ssh`, no tokens, no
credential helpers — nothing to lift, and no keys to push with. When you need real access you
declare a posture (`fy mode gcp=logs github=app`), and host-side daemons *outside the blast
radius* mint short-lived scoped tokens and inject them at the egress proxy — so the token
reaches the upstream API but never the yard. Emergency rungs are TTL-bound and auto-revert. No
daemon running ⇒ no credential flows, whatever a file inside the yard says.
→ [modes.md](./docs/modes.md)

**Isolation without hiding.** A container escape lands the attacker inside the disposable VM,
not on your machine — there is no host filesystem mounted to reach even at VM level. Because
the boundary doesn't depend on concealment, nothing needs to be nested or hidden: Podman
Desktop shows every container, and `fy verify` audits the mounts and exercises a battery of
known escapes on demand. (It tests the escapes we know about — it raises assurance, it doesn't
prove a negative.) → [security.md](./docs/security.md)

## Who it's for

**Agent users** — hand Claude Code (or any agent) the in-VM machine socket: full stack
autonomy, bounded to the repo and the containers *inside* the VM. The socket controls the
machine from within the moat, not your host. Safer autonomy, not less of it.

**Everyone else** — supply-chain worms (Shai-Hulud and friends) ride `npm install` and
lifecycle scripts on developer laptops. Run installs, dev servers and your IDE backend in the
yard, and the worm wakes up with no ambient credentials and no push access, its reach bounded
to a repo the clean-repo prerequisite keeps boring. Egress routes through the host-side proxy:
every destination logged (SNI-only by default, full decrypt-and-log under `capture=on`), with
an opt-in default-deny wall and live host grants. → [networking.md](./docs/networking.md)

Two honesty notes. Proxy routing rides proxy env vars, which cooperative software honours and
malware may not — `[machine].wall = true` (what `foldyard init` writes) closes that by
provisioning a fail-closed firewall into the VM itself, making the proxy the only way out.
And declaring a posture while running an untrusted install step is the one window where
install-time code and a live token coincide.

## Shape

One Python CLI, one `foldyard.toml` in your repo, host state in `~/.foldyard/`. Your own task
runners (`just`, npm scripts) stay yours — foldyard runs *under* them, not instead of them.

- **Core** — machine lifecycle · compose-stack runner (worktree-namespaced projects, shared
  caches) with declarative posture [overlays](./docs/compose-overlays.md) · long-lived dev box
  (multi-session, shadow volumes, IDE-attachable) · `verify` isolation battery · mode/posture
  substrate with TTLs and a host-side supervisor · credential-injecting egress proxy with
  capture logging, a default-deny wall and VM-level fail-closed forcing · doctor · TUI.
- **Plugins** — credential minters (GCP metadata emulator, GitHub App header injection,
  generic header/query-param-auth APIs) · editor attach (VS Code) · agents (Claude Code and
  Codex, keyless: the proxy injects your key host-side, a dummy lives in the box).

Each project gets its own rootless VM, scoped by project name. The default backend is **Lima**
because it runs the per-project VMs side by side — so one project's open `box shell` / `code`
session survives while you work in another — and because it's the backend that can be
provisioned with the fail-closed wall. Other backends, and when to pick them, are in
[configuration.md](./docs/configuration.md#machine).

## Status

The engine runs daily on Macs with a production project, but has only been lightly tested on
other repos, treat it as alpha quality for now. Other platforms (Linux and WSL2) are planned,
but currently not supported. Brave testers and issues very welcome!

## More info

Start at the [docs map](./docs/README.md); the "why" behind each design call is in
[docs/adrs/](./docs/adrs/).
