# foldyard

> Bring your whole dev stack into the fold. **Secretless by default.**

Foldyard is an isolated development environment that runs on your own computer, for your
project's **entire** dev stack — not just an agent, not just a shell. Your docker-compose
services (databases, emulators, app servers), your dependency installs, your IDE backend and,
if you want, your coding agent all run together inside a throwaway Linux VM running **rootless
Podman**. The VM mounts your repo and nothing else.

New to the vocabulary? The [glossary](./docs/glossary.md) defines every term these docs use.

## TL;DR

Install the prerequisites. Lima creates the VM; podman talks to the container engine inside it.

```bash
# macOS
brew install uv lima podman

# Linux (Debian/Ubuntu shown; x86-64 with KVM)
curl -LsSf https://astral.sh/uv/install.sh | sh
sudo apt-get install qemu-system-x86 qemu-utils ovmf podman
curl -fsSL https://github.com/lima-vm/lima/releases/download/v2.2.0/lima-2.2.0-Linux-x86_64.tar.gz \
  | sudo tar Cxz /usr/local
```

Then:

```bash
uv tool install "foldyard[host]"   # puts `foldyard` on your PATH, with `fy` as shorthand

cd your-repo
foldyard init                      # writes foldyard.toml: a locked-down box with no stack yet;
                                   # agents, stack and editor attach are commented-out opt-ins
fy box up                          # creates the VM and the dev box; all traffic goes out
                                   # through a proxy on your computer
fy box shell                       # you're in the box
```

No custom image is needed: foldyard ships a Debian box with the engine client, git and uv. To
grow it into one tuned for your repo, run `foldyard skill install bootstrap-devbox` and then
the skill.

An agent inside the box (`fy claude`, `fy codex`) is told what `fy` is and how to edit the
config, so it can help you set foldyard up. Its edits don't take effect on their own: the next
`fy box up` shows you the change and asks you to approve it. The agent can't sneak a change
in, and you can still share the config with your team.

**One prerequisite: a clean repo.** The VM mounts only your repo, so the zero-credential
guarantee holds exactly when that repo holds *config, not credentials*. Scan your history with
[gitleaks](https://github.com/gitleaks/gitleaks) or
[TruffleHog](https://github.com/trufflesecurity/trufflehog) first, and move anything they find
out of the repo. Credentials the box really needs are supplied by [modes](./docs/modes.md)
without ever entering the VM.
Foldyard can mount a clean repo; it can't clean a dirty one.

The full walkthrough is [docs/quickstart.md](./docs/quickstart.md). The whole manual also ships
with the install, offline and matched to your version: `fy docs`.

## Architecture

Everything that holds a credential stays on your computer. The VM gets the repo and nothing else.

![Foldyard architecture: your machine runs the foldyard CLI, the supervisor, the credential
token services and the egress proxy, and holds the config you approve, the allowlist and the
access modes; the VM ("the yard") is a throwaway Linux VM running rootless Podman that mounts
only the repo and holds the compose stack, the dev box, the coding agent and worktrees side by
side, with `fy verify` auditing the boundary from inside; traffic from the VM reaches upstream
APIs only through the proxy.](./docs/assets/foldyard-architecture.svg)

Isolation comes in layers, not as one switch. You can harden the same setup one step at a
time — from a rootless Podman VM, to Lima with a firewall inside the VM, to gVisor under the dev
box, to a second firewall on a Linux host — with egress control as a separate dial alongside.
Each step is one line in `foldyard.toml`, and the credentials never move:

![Foldyard isolation layers: four cumulative steps — a rootless Podman VM; Lima with the
firewall inside the VM; gVisor under the dev box, reached through a narrowed engine socket;
and the host-side firewall on Linux — then the egress dial from open through observe and
enforce to fail-closed, and what never moves: the credentials stay on your computer, the
engine socket is the one deliberate opening, and `fy verify` checks the layer you are
in.](./docs/assets/foldyard-isolation-layers.svg)

Which layer carries the weight depends on your operating system —
[isolation-layers.md](./docs/isolation-layers.md).

## Why

A *fold-yard* is the enclosed farmyard where animals are folded (penned) for the night. Same
idea here: everything that runs untrusted code lives in the yard, and your computer — keys,
browser profile, other repos — stays outside the fence. Three ideas hold it up.

**The unit of isolation is the project, not the agent.** Agent sandboxes are plentiful, and
isolated dev environments exist (Devcontainers, Codespaces, Gitpod, Coder). What's missing is
one boundary that holds the real compose stack, the real end-to-end test loop and real services
reachable by container name — *and* zero credentials. → [prior-art.md](./docs/prior-art.md)

**Secretless by default, credentials on demand.** The yard starts with no `~/.ssh`, no tokens,
no credential helpers: nothing to steal and no keys to push with. When you need real access you
switch it on (`fy mode gcp=logs github=app`). Token services on your computer — outside anything
the yard can reach — make short-lived, scoped tokens, and the egress proxy adds them to requests
on the way out. The upstream API gets the token; the yard never does. Emergency levels that act
as *you* expire on a timer and switch themselves off. If no token service is running, no
credential flows, whatever a file inside the yard says. → [modes.md](./docs/modes.md)

**Isolation without hiding.** A container escape lands the attacker inside the throwaway VM,
not on your computer — no host filesystem is mounted for it to reach, even at VM level. Because
the boundary doesn't depend on concealment, nothing needs to be nested or hidden: Podman Desktop
shows every container, and `fy verify` audits the mounts and tries a set of known escapes on
demand. (It tests the escapes we know about: it raises confidence, it can't prove there are
none.) → [security.md](./docs/security.md)

## Who it's for

**Agent users.** Give Claude Code (or any agent) the container socket inside the VM: full
control of the stack, bounded to the repo and the containers in that VM. The socket controls
the VM from the inside, not your computer. Safer autonomy, not less of it.

**Everyone else.** Supply-chain worms (Shai-Hulud and friends) ride `npm install` and lifecycle
scripts on developer laptops. Run installs, dev servers and your IDE backend in the VM, and a
worm wakes up with no credentials and no push access, able to reach only a repo the clean-repo
rule keeps boring. All traffic out goes through the proxy on your computer: every request is
decrypted and logged (except trusted toolchain hosts you mark as passthrough), and an allowlist
decides what may go out, with grants you add live. → [networking.md](./docs/networking.md)

Two honest caveats. Routing through the proxy relies on proxy environment variables, which
well-behaved software honours and malware may ignore. `[machine] firewall = true` (which
`foldyard init` writes) closes that gap: a firewall inside the VM refuses any traffic that
doesn't go through the proxy. And switching a credential on while running an untrusted install
step is the one window where install-time code and a live token meet.

## Shape

One Python CLI, one `foldyard.toml` in your repo, and state on your computer in `~/.foldyard/`.
Your own task runners (`just`, npm scripts) stay yours — foldyard runs *under* them, not
instead of them.

- **Core** — VM lifecycle · a compose-stack runner (one namespaced stack per worktree, shared
  caches) with [overlays](./docs/compose-overlays.md) that change with the mode · a long-lived
  dev box (several sessions at once, IDE-attachable) · the `fy verify` isolation self-test ·
  modes with time limits, run by a supervisor on your computer · an egress proxy that adds
  credentials, logs traffic and enforces the allowlist, backed by the VM firewall · `fy doctor`
  · a TUI.
- **Plugins** — token services (a GCP metadata emulator, GitHub App tokens, generic header- or
  query-parameter-authenticated APIs) · editor attach (VS Code) · agents (Claude Code and Codex,
  keyless: the box holds a dummy key and the proxy swaps in your real one).

Each project gets its own rootless VM, named after the project. The default backend is **Lima**:
it runs one VM per project side by side, so a `fy box shell` or `fy code` session in one project
survives while you work in another, and it is the backend that can carry the VM firewall. The
alternative, and when to pick it, is in [configuration.md](./docs/configuration.md#machine).

## Status

Alpha. Treat it that way, and expect rough edges.

- **macOS** — used daily with a production project. Other repos have only been lightly tested.
- **Linux (x86-64)** — CI boots a real Lima VM on an Ubuntu 24.04 runner and drives the real
  loop on it: `fy up`, the dev box, worktrees, VM lifecycle, `fy verify`, and both firewalls.
  `fy tui`, `fy code` and `fy open` haven't been tried on Linux yet, and some messages still
  give macOS-only hints (`brew install …`).
- **WSL2** — the same CI tests pass inside WSL2 on a Windows runner, except the host firewall,
  which the stock WSL2 kernel can't support. Not yet tried on a real Windows 11 machine.

What has and hasn't been run on Linux is tracked in
[docs/linux-support.md](./docs/linux-support.md). Brave testers and issues very welcome!

## More info

Start at the [docs map](./docs/README.md). The reasoning behind each design decision is in
[docs/adrs/](./docs/adrs/).
