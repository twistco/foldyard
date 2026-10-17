# Lessons from building foldyard

> **What it is:** fourteen short stories from building foldyard, 2026-06 to 2026-09 — the
> incidents and near misses that shaped it, told for any developer rather than for foldyard's
> maintainers — and what's still open. Each says what happened, the lesson in one line, and where foldyard records the
> fix. **Status:** a record; the decisions themselves live in the [ADRs](../adrs/), which this
> page only points to. Details are generalised: no people, projects or internal names.

Most of these share one shape. A developer's laptop holds the keys to everything — SSH keys,
cloud logins, tokens, every other project — and the code that runs on it every day (package
installs, build scripts, dev servers, now AI agents) is mostly code nobody on the team wrote or
read. A dev sandbox puts that code somewhere the keys aren't. The stories are about how easy it is
to get that "somewhere" subtly wrong.

A few terms: the **VM** is a small Linux virtual machine on your computer; the **box** is the
container inside it where you and your agent work; the **proxy** is a program on your computer that
all of the box's internet traffic goes through. More in the [glossary](../glossary.md).

## 1. Your container VM may mount your whole home folder

**What happened.** On macOS, a container engine has to run in a Linux VM. Docker Desktop's VM
shares all of `/Users` into the VM, read-write, and runs its engine as root. So a
container started with `--privileged` can use `nsenter` (a tool that jumps into another process's
namespaces) to become root in the VM — and root in the VM can read every SSH key and browser
profile on the laptop. Switching to a rootless engine in a VM that mounts only the repo made the
same breakout fail with `cannot open /proc/1/ns/ipc: Permission denied`.

**The lesson:** "it's in a container" means nothing until you know what the VM underneath can see.

**Recorded in:** [ADR-0001](../adrs/0001-rootless-podman-vm-isolation-boundary.md).

## 2. `npm install` runs code — so give it nothing to steal

**What happened.** Supply-chain worms (Shai-Hulud and its successors, 2025–2026) spread through
package install scripts that run on developer laptops, steal tokens, and publish themselves
onward. The same trick works through anything in a repo that runs by itself: devcontainer
lifecycle hooks, editor tasks, git hooks.

**The lesson:** you can't review every dependency, so run installs where there are no credentials
to find.

**Recorded in:** the [README](../../README.md#who-its-for) and [prior-art.md](../prior-art.md).

## 3. The credential helper was a file in the repo, and it ran on a timer

**What happened.** To make a GitHub token, foldyard's supervisor on your computer ran a small
script — and that script was a file in the repo. Anything that could edit the repo (an agent in
the box, a malicious install script, a branch checked out for review) could change what ran on
your computer, next to every secret. It ran about every 55 minutes with nobody watching, and its
dependencies were declared inline and fetched at run time, so the payload didn't even have to
appear in the diff. Six other features had the same shape.

**The lesson:** nothing from the repo may execute on your computer — fixing only the one you found
is theatre.

**Recorded in:** [ADR-0023](../adrs/0023-no-host-executed-code-from-the-repo-mount.md).

## 4. It closed by deletion, not by a gate

**What happened.** The obvious fix for story 3 was a trust store: approve each repo script by
hash, then run only approved copies. It was designed in detail before anyone counted how many
consumers actually needed a custom script. There were two. foldyard shipped the token makers as
part of the package instead, and the codebase got smaller.

**The lesson:** count the customers of a dangerous feature before building a safe way to keep it.

**Recorded in:** [ADR-0023](../adrs/0023-no-host-executed-code-from-the-repo-mount.md),
Consequences.

## 5. The channel, not the field

**What happened.** After story 3, reviewers went through the config file field by field, moving
risky ones out of the repo. Four rounds later, a developer noticed that an agent's edit to the
proxy settings had reached the proxy on their computer within seconds. The supervisor still
re-read the config from the repo every few seconds, and two fields nobody had listed could exempt a host from
monitoring or send a live token to a host of the agent's choosing. The fix was to stop reading the
repo's file at all: your computer runs a copy you approved (**adopted**), and an edit waits for
you.

**The lesson:** while a file the sandbox can write is live input to your computer, the next field
added reopens the hole.

**Recorded in:** [ADR-0022](../adrs/0022-host-runs-the-adopted-config.md).

## 6. Repo config is untrusted input to parsers too

**What happened.** A config field let a repo declare a regular expression that a pasted secret had
to match. A crafted regex can take effectively forever to evaluate (ReDoS), and that would hang a
command on your computer, so it became a simple glob. Another field let a rule name *which*
environment variable held its token — so a rule could name another tool's API key and a host of
its choosing. That name is now derived by foldyard and checked for collisions.

**The lesson:** config from the repo is attacker input to every piece of code that reads it, not
just the parts that execute it.

**Recorded in:** [ADR-0023](../adrs/0023-no-host-executed-code-from-the-repo-mount.md) (the
derived name) and [configuration.md](../configuration.md) (`[[secret]]`'s `pattern`).

## 7. The token maker asked for its token through the proxy it was feeding

**What happened.** The proxy adds credentials to requests in flight. The program that makes the
GitHub token respected `HTTPS_PROXY` like a good citizen — and if that pointed at foldyard's own
proxy, the proxy would replace the program's login with the very token being made. The fix skips
exactly foldyard's own proxy port and honours any other proxy (a company proxy may be the only way
out).

**The lesson:** a credential helper must never send its request through the injector it serves.

**Recorded in:** the `github-app` token service's code (`_env_proxies` in
`src/foldyard/plugins/github_app_token.py`); no ADR yet.

## 8. The verb isn't the boundary; the config the verb consumes is

**What happened.** An early idea was to hide the container engine behind a "broker" that allowed
only safe commands, such as `compose up`. But `compose up` reads the compose file, and the box can
edit that file: add `privileged: true` or mount `/` and the "safe" command does everything the raw
engine could.

**The lesson:** allowing a command is meaningless if the sandbox controls what the command reads.

**Recorded in:** only the pre-extraction run log; no foldyard ADR states it. The closest current
record is [isolation-layers.md](../isolation-layers.md#the-engine-socket-is-a-hole-by-design).

## 9. The agent's most useful tool is also its escape hatch

**What happened.** An agent that drives a whole stack needs the container engine. With a rootful
engine, that access *is* root in the VM, and root in the VM can switch off any firewall inside
it. foldyard's firewall therefore matches traffic by the VM user that sends it, on a rootless
engine the agent can't use to become root; and on Linux a second firewall on your computer
matches the VM itself, out of reach even of VM-root.

**The lesson:** put the guard where the sandbox's own tools can't reach it.

**Recorded in:** [egress-forcing-study.md](./egress-forcing-study.md),
[lima-wall-machine-integration.md](../lima-wall-machine-integration.md) and
[ADR-0028](../adrs/0028-no-elevation-on-the-host-operator-applies.md).

## 10. Cooperative capture is not enforcement

**What happened.** Pointing `HTTPS_PROXY` at a logging proxy shows you every request from
well-behaved software. It is only a suggestion: Go binaries, gRPC, QUIC and malicious code ignore
it and go straight out. A proxy, a log and an allowlist made "egress filtering" one README sentence
away, while it stopped no attacker. foldyard didn't claim filtering until a firewall made the
proxy the only way out.

**The lesson:** don't advertise a security control that the attacker can opt out of.

**Recorded in:** [ADR-0009](../adrs/0009-monitoring-cooperative-enforcement-locked.md) and
[egress-forcing-study.md](./egress-forcing-study.md).

## 11. A git checkout silently switched off credential injection

**What happened.** The proxy's plugin file lived in the repo, and the proxy reloads a plugin when
the file changes. A branch switch rewrote it under the running proxy, the reload dropped the
plugin, and the proxy carried on as a plain pass-through: no tokens added, no logging, just 401
errors from upstream — while the proxy stayed up and listening, so nothing looked down.

**The lesson:** long-running daemons must not run code from a working tree that changes under
them.

**Recorded in:** [ADR-0012](../adrs/0012-uv-tool-distribution-no-mutable-daemon-source.md).

## 12. Two lifecycles disagreeing about one thing

**What happened.** An audit of fourteen state bugs found nine with the same cause: two parts of the
system each managing the same file, port or process on their own schedule. In one, two projects'
supervisors used the same proxy port and each killed the other's proxy as a stray, forever. In
another, an idle supervisor kept rewriting a status file and made clean checkouts look dirty.

**The lesson:** give every shared resource exactly one owner.

**Recorded in:** [mode-state-consolidation.md](./mode-state-consolidation.md) and
[ADR-0016](../adrs/0016-per-worktree-posture.md) (per-project port ranges).

## 13. A green test suite deleted the live dev stack

**What happened.** foldyard's tests fake the container engine. One test's engine call slipped past
the fakes — and inside the box, the real engine was reachable. It force-removed every container in
the developer's running stack while the whole suite passed green.

**The lesson:** make the test suite unable to reach real tools, rather than trusting each test to
mock them.

**Recorded in:** [DEVELOPMENT.md](../../DEVELOPMENT.md) ("Golden tests must never see a live engine
socket" and the hermetic suite).

## 14. The self-test went green without checking

**What happened.** `fy verify` tries known container escapes and expects them to fail. Several
checks passed because the probe container never started at all — a failed image pull looked
exactly like a refused escape.

**The lesson:** a negative check needs a positive control: a probe that didn't run found nothing.

**Recorded in:** [verify-false-pass.md](./verify-false-pass.md).

## 15. What's still not closed

**What happened.** Not every hole has a fix. Git hooks and git aliases in a checkout the box can
write still run on your computer, and foldyard doesn't manage `.git`. A prompt in the shared config
is fed to every teammate's agent. Adopting config (story 5) is only as good as the person reading
the diff.

**The lesson:** say plainly what the sandbox doesn't cover; a known gap is safer than a hidden one.

**Recorded in:** [security.md](../security.md).
