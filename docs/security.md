# Security model — the boundary, the threat model, and `fy verify`

The [README's third idea](https://github.com/twistco/foldyard/blob/main/README.md#why) is
*isolation without hiding*: safety comes from a real boundary, not from concealment, and you can
test the boundary yourself. This page covers what the boundary is, what it defends against, what
it doesn't, and what `fy verify` proves. The reasoning behind each design call lives in the
[ADRs](./adrs/README.md). Terms are defined in the
[glossary](./glossary.md).

## The boundary: a disposable VM that mounts only your repo

Everything that runs untrusted code — installs, build scripts, your compose stack, the agent —
runs inside **the VM**: a throwaway Linux virtual machine running rootless Podman that mounts
**only your repo** (plus its worktrees folder). No home directory, no other checkouts, no keys.
Two properties combine ([ADR-0001](./adrs/0001-rootless-podman-vm-isolation-boundary.md)):

- **Rootless engine.** A container's "root" maps to an unprivileged user *inside the VM*, so the
  classic `--privileged --pid=host` breakout to VM root is refused at the source.
- **Repo-only mounts.** The VM's mount table holds your repo and nothing else from your computer.
  Even with full control of the VM, there is no other file of yours to reach.

So a container escape — a kernel bug, a runtime bug — lands the attacker in a disposable VM
whose only contents are a repo they could already read. Not on your computer, not near your keys,
browser profile or other projects. That is also why handing the agent the VM's container socket
is safe: what the socket can reach is bounded to the repo and the VM's containers.

**The VM is required on Linux too.** Rootless Podman run directly on Linux sees your whole
filesystem, so anything holding its socket could mount `~/.ssh` into a new container. The VM is
what limits the socket to "repo + containers", so there is no VM-less backend
([ADR-0027](./adrs/0027-always-a-vm-native-backend-retired.md)).

## Threat model

What foldyard is built against — not the agent as such, but what comes in with development:

- **Malicious packages and install scripts** (`npm install`, PyPI, postinstall hooks). They run
  freely *inside the VM* and find: the repo (which the [clean-repo precondition](#the-precondition-a-clean-repo)
  keeps free of secrets), no SSH keys, no tokens, no credential helpers, no push access. Your
  home directory, other repos and long-lived secrets aren't inside, so they can't be reached.
- **Prompt-injected agents.** The agent has real autonomy — shells, the stack, the container
  socket — but everything it holds is bounded to the VM. A hijacked agent can damage the local
  checkout (recoverable); it can't push, delete the remote, or take credentials that were never
  there.
- **Build hooks and dev servers.** Anything a `just build` or `next dev` pulls in runs in the
  same VM under the same rules.

**The one overlap window.** If you switch on access (say `fy mode github=app`) and *then* run an
untrusted install step, install-time code and a live credential path overlap. The token still
never enters the box (see below), and it is short-lived and scoped — but while the switch is on,
code in the box can *use* that access. Keep switches off while installing things you don't trust
yet.

### What code in the box can still reach on your computer

foldyard runs nothing from the repo on your computer: consumer scripts such as
`[project].worktree_init` run in a container, and where a script's output affects your computer
(the VS Code attach config, say), foldyard writes it itself from validated data. And
**`foldyard.toml` is not live input to your computer**: the supervisor runs from a copy you
*adopted*, stored outside the repo, so an edit to `[proxy]` or `[[inject]]` does nothing until you
accept it at the next `fy up` or `fy config adopt`
([ADR-0022](./adrs/0022-host-runs-the-adopted-config.md)). Two channels remain
([ADR-0023](./adrs/0023-no-host-executed-code-from-the-repo-mount.md)):

- **Git's own hooks run on your computer, and foldyard doesn't manage them.** `lefthook.yml`,
  `.git/hooks/*` and `.git/config` aliases can be written from the box and run on your computer
  the next time you use git in that checkout. foldyard can't close this without owning your
  `.git`, so treat a shared checkout's hooks as code you are choosing to run.
- **Repo text that steers the agent.** `[claude].system_prompt` is prepended to every teammate's
  agent, and `[claude.settings]` / `[codex.config]` are passed to the agent CLI (`--settings` /
  `-c`), where a `hooks` or `permissions` entry steers it harder than any prompt. This runs in the
  box, not on your computer, but it is still repo content steering a trusted actor.
  `fy config widenings` lists all three. They are adopted like the rest of the file — but
  adopting means a person reading a diff, which is weaker than "can't happen".

foldyard itself is installed on your computer as a uv tool (`uv tool install foldyard`), a
frozen copy the repo mount can't change
([ADR-0020](./adrs/0020-post-extraction-consumption-model.md)). The exception is developing
foldyard itself with an editable install: if that source checkout is also mounted into a VM, the
box can edit the code the supervisor runs.

## The credential story: nothing at rest, injected in flight

The resting state is **zero secrets in the VM**: no key files, no token environment variables, no
`~/.netrc`, no credential helpers — nothing to steal. When you need real access, you switch it on
(`fy mode gcp=logs github=app`) and three mechanisms keep the grant contained:

- **Tokens are made on your computer and injected at the proxy.** The box holds only a dummy
  value; the proxy on your computer replaces it with a real, short-lived, narrowly scoped token
  as the request passes through. The real token never enters the box — not as an environment
  variable, not as a file ([ADR-0007](./adrs/0007-credential-injection-at-egress-proxy.md)). A
  fully compromised box can use the access only while the switch is on, and only against the
  injected host; it never *holds* the credential.
- **Emergency levels expire.** Levels that act as *you* have a mandatory TTL (one hour by
  default) and switch themselves back off
  ([ADR-0005](./adrs/0005-secretless-by-default-posture-axes.md)).
- **Control lives outside the VM.** The current mode is stored in your home directory, where
  nothing in the box can reach it, so the box can't raise its own access. The same goes for the
  adopted config ([ADR-0022](./adrs/0022-host-runs-the-adopted-config.md)). The supervisor's
  daemons are the only path a credential takes: **no daemon running, no credential flows**
  ([ADR-0006](./adrs/0006-host-side-enforcement-single-supervisor.md)).
- **Token services are built in, never repo code.** The kinds foldyard ships (`github-app`,
  `gh-cli`, the Codex refresh flow, `static_token`) or an installed plugin are the only ones that
  run. There is no config key naming a command to run and no token-service path inside the repo:
  otherwise any write to the checkout — an agent, a postinstall script, a branch you checked out
  to review — would run as you, next to your credentials. Secrets are declared with `[[secret]]`,
  whose `how` hint foldyard **prints for you to run** and never runs itself
  ([ADR-0023](./adrs/0023-no-host-executed-code-from-the-repo-mount.md)).

## Network: what's cooperative, what's enforced

The box's traffic goes through the proxy on your computer because of proxy environment
variables. That gives full visibility — every destination logged, requests decrypted, and an
optional [allowlist](./networking.md). But routing by environment variable is **cooperative**:
tools that honour it are logged; malicious code can unset `HTTPS_PROXY` and connect directly.
foldyard doesn't call that filtering, because it isn't
([ADR-0009](./adrs/0009-monitoring-cooperative-enforcement-locked.md)).

**Enforcement is the VM firewall, `[machine] firewall = true`** (Lima backend): nftables rules
inside the VM that *refuse* traffic not going through the proxy, so the proxy becomes the only way
out. They are installed as root when the VM boots, from your adopted config, and the same boot
script removes the VM user's passwordless sudo. Root in the VM exists only at boot, so a container
escape that lands as the VM user can't remove the firewall — only a VM-kernel exploit can, and
even that reaches no credential. On Linux, `[machine] host_firewall` adds the same rules on your
computer around the VM process, which survives even that. Without the firewall, QUIC (UDP 443)
can bypass the proxy. Details are in [networking.md](./networking.md).

## `fy verify`: prove it, don't trust it

`fy verify` is the isolation self-test: run it any time; it exits non-zero on any failure, so CI
can use it. Run it *inside* the box (`fy box shell`, then `fy verify`) for the full set; outside
the box only the VM-boundary checks run. What it checks:

- **VM boundary** (over the container socket): the engine reports **rootless**; a
  `--privileged --pid=host` container **can't read the VM's PID-1 namespace** (the known breakout,
  attempted and refused); and a **mount audit** — the VM's own mount table carries no path from
  your computer beyond the repo and worktrees mounts, matched exactly.
- **Credential checks** (inside the box): no SSH agent forwarded, no git-credential bridge to your
  computer (`GIT_ASKPASS`), no editor-attach bridge sockets in `/tmp`, no `~/.ssh` private keys,
  no `~/.netrc`, and `git ls-remote origin` **fails** — the box can't reach the remote to push.
  (This needs a *private* origin: a public one answers `ls-remote` without credentials.) These
  are built into foldyard's core, so a missing or broken plugin can't weaken them.
- **Per-switch checks** (from the credential plugins): each checks its own mode. For `github`,
  the token in the box is never more than the dummy `x` in *any* mode; the real one stays on your
  computer. Active **emergency levels print a banner**, so an escalation is never invisible.
- **Firewall probes** (Lima with `[machine] firewall = true`): direct connections from the box
  that ignore the proxy — to a public IP on port 443 *and* on 53, the DNS-tunnel case — must be
  refused. A reachable proxy is checked first, so a box that is simply offline doesn't pass.

### When a check fails

Each row names *what* failed. What it usually means, and what to do:

- **`probe image … could not run — the boundary battery DID NOT EXECUTE`** — the first check
  failed, so none of the boundary checks ran. The probe image (`alpine`, or `VERIFY_IMG`) must
  reach the VM: with the firewall on, the proxy must be up (`fy host restart`, or `fy up`), or
  pre-pull the image, or point `VERIFY_IMG` at one already in the VM. Not an isolation failure —
  an unproven result.
- **`engine is NOT reported rootless`** — the VM was created rootful, or verify reached a socket
  that isn't foldyard's. Check which socket the section header names, then `fy doctor`; rebuild a
  rootful VM with `fy machine recreate`.
- **`host PID1 namespace READABLE … (breakout!)`** — the known escape worked. Treat the VM as
  compromised and stop using it (`fy machine stop`) until you know why. The usual cause is the
  socket belonging to an engine on your computer rather than the VM's, not a kernel bug.
- **`VM exposes host paths: …`** — the VM mounts more than the repo and the worktrees folder (a
  backend default that shares your whole home, or a mount added by hand). Mounts are set when the
  VM is created: fix the `[machine]` config, then `fy machine recreate`. **`could not read the VM
  mount table … UNPROVEN`** means the probe failed, not that anything leaked. Under gVisor the row
  reads `N/A`; run `fy verify` on your computer for the audit.
- **`SSH_AUTH_SOCK set` / `git-credential bridge to the host` / `editor-attach bridge sockets`**
  — a VS Code attach forwarded your computer's credentials into the box. See
  [below](#editor-attach-credential-bridges).
- **`~/.ssh key material` / `~/.netrc present`** — a credential is in the box. foldyard doesn't
  put it there: find the mount or `[box]`/bootstrap step that copies it in, remove it, then
  `fy box down` and `fy box up`.
- **`git remote REACHABLE — the box can push`** — the box authenticated to `origin`, or `origin`
  is public (then the row can't tell). For a private origin, `fy state` shows what the mode
  grants and `fy config widenings` where credentials are delivered. A network timeout does *not*
  pass this row; an unreachable remote proves nothing.
- **Plugin rows** (e.g. the `github` token in the box is more than the dummy) — the box's
  environment carries a real credential. `fy mode` shows the mode, `fy state` desired against
  observed; a box created under older config is recreated with `fy box down` and `fy box up`.
- **`box is NOT under gVisor`** — the box was created before `[machine] runtime = "gvisor"` was
  adopted, or through the wrong socket. `fy box down` and `fy box up` recreate it.
- **VM firewall rows:**
  - `no HTTPS_PROXY in the box` — the box predates the firewall config: `fy box down`, `fy box up`.
  - `the permitted path … is unreachable too` — the box has no egress at all, so the refusals
    prove nothing. `fy host` shows whether the supervisor runs; `fy host restart` starts it.
  - `direct egress … CONNECTED` — the firewall isn't enforcing in the VM. It is installed at
    boot, so run `fy machine stop`, then `fy up`, which also checks the VM's own firewall report
    and refuses on a mismatch, printing where the boot log is.
- **Health `WARN`s** are advisory (`fy logs <service>`) and never change the result.

#### Editor-attach credential bridges

VS Code's Dev Containers attach (`fy code`, or a manual "Attach to Running Container") runs its
server in the box and, by default, forwards your computer's SSH agent
(`/tmp/vscode-ssh-auth-*.sock`) and git credentials (`GIT_ASKPASS` over `/tmp/vscode-git-*.sock`)
into every terminal it opens. foldyard blocks both:

- **SSH:** `fy code` starts VS Code with foldyard's own *empty* ssh-agent, so there is nothing to
  forward.
- **Git:** `fy code` turns `git.terminalAuthentication` and `git.useIntegratedAskPass` off, but
  `.vscode/settings.json` in the repo can turn them back on, and `fy verify` fails a checkout that
  does (`workspace settings re-enable the git-credential bridge`).
- **In the box:** `~/.config/foldyard/harden.sh`, sourced on the first line of `~/.bashrc`,
  unsets the bridge variables in every bash shell and starts a reaper that deletes the sockets as
  they appear. This also covers a manual attach that didn't go through `fy code`.

If a row still fails:

1. Attach with `fy code` rather than attaching manually.
2. Run `fy box shell` — it restarts the reaper (a socket can survive up to one second before it
   is deleted).
3. If the box predates this protection, `fy box up` applies it to the running box.
4. Use bash: other shells don't source `harden.sh`.
5. Not from an attach at all? Look for a mount or `[box]`/bootstrap step that copies a credential
   in (`fy config widenings` lists what the adopted config allows), remove it, then `fy box down`
   and `fy box up`.

### What verify doesn't prove

**`fy config widenings`** answers the question verify doesn't: not "is the mode what it claims?"
but "what did we agree to, and where is it written?" — the `passthrough` hosts left undecrypted
(with `@all` resolved to its real count), where each credential is injected, which agent prompt
is shared and which is yours, and any key that reads as security config but is no longer honoured.

verify exercises the escapes we know about. A pass means the known breakouts are refused and the
mode is what it claims, not that no unknown escape exists. It also doesn't scan your repo for
committed secrets — that is your precondition, below.

## The precondition: a clean repo

foldyard mounts your repo into the VM, so "zero credentials in the box" holds only when the repo
carries **config, not credentials**. foldyard can mount a clean repo; it can't clean a dirty one.
Before you start, scan your history with [gitleaks](https://github.com/gitleaks/gitleaks) or
[TruffleHog](https://github.com/trufflesecurity/trufflehog), move anything they find into
`host.env` behind a switch, and re-scan regularly — see
[the README's TL;DR](https://github.com/twistco/foldyard/blob/main/README.md#tldr).

## Reporting a vulnerability

Report anything you believe is exploitable privately, as described in
[SECURITY.md](https://github.com/twistco/foldyard/blob/main/SECURITY.md) — not as a public
issue. Non-sensitive bugs go to the
[issue tracker](https://github.com/twistco/foldyard/issues).
