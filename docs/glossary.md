# Glossary

The words foldyard's docs, messages and config use, in plain terms. Where an older word is still
around (in the code, in ADRs, in an older foldyard's output), it's listed so you can map it across.

## The pieces

**Your computer** (in code and design docs: *the host*) — the machine you run `fy` on. It holds
your credentials, the proxy, and foldyard's state in `~/.foldyard/`. Nothing in the VM can reach
any of it.

**The VM** (config: `[machine]`; also *the machine*) — a throwaway Linux virtual machine, one per
project, running rootless Podman. It mounts your repo (and its worktrees) and nothing else from
your computer. Everything that runs untrusted code runs in here. The README calls it *the yard*,
after the farmyard where animals are penned for the night.

**The box** (*the dev box*) — the long-lived container inside the VM where you and your agent
work: `fy box shell`, `fy claude`, `fy codex`. It has the repo, your toolchain and the VM's
container socket, and no credentials.

**The stack** — your compose services (databases, API servers, emulators), run in the same VM by
`fy up`. Services reach each other by container name; your computer reaches them on the ports in
`[ports]`.

**Worktree** — a git worktree made with `fy worktree add`. Each gets its own stack, its own box,
its own mode and offset ports, all sharing the one VM.

**The supervisor** — foldyard's one background process on your computer per project. It runs
the proxy and the token services, turns them on and off as the mode changes, and watches that the
credentials behind them still work. Started with the VM; `fy host` shows its status.

## Network

**The proxy** (*the egress proxy*) — a `mitmdump` on your computer that all of the box's
internet traffic goes through. It logs every request, decrypts it (except for
`passthrough` hosts), adds credentials where the mode says to, and enforces the allowlist.

**Passthrough** (`[proxy] passthrough`) — hosts the proxy forwards *without* decrypting (it
still logs the hostname). For trusted toolchain hosts, and hosts that break under decryption
(pinned certificates, client certificates). Entries can be `@bundle` names — ready-made lists
like `@node` or `@vcs`; `@all` is every bundle.

**The allowlist** — the list of hosts the box may reach. Grants are stored on your computer, never
in the repo (`fy allow add`, `fy allow sync`, one keypress in `fy tui`). **Enforcing** (`fy allow
enforce on`) means the proxy refuses any host not on the list; off means it only watches.
*Older name:* "the wall", `fy allow wall`, `[proxy] default_deny`.

**Learn** (`fy allow enforce learn`, `[proxy] enforce = "learn"`) — a time-boxed window where
nothing is refused but every host that *would* have been is recorded, then enforcement turns back
on by itself. `fy allow learn` reviews and grants what it recorded.

**Recommend** (`[proxy] recommend`) — hosts the repo *suggests* granting, each with a reason.
Everyone is asked about each one on their own computer; the repo can ask, never grant.

**VM firewall** (`[machine] firewall`) — nftables rules inside the VM, set up as root when it
boots, that refuse any traffic not going through the proxy. Without it, routing through the proxy
is **cooperative**: well-behaved tools honour the proxy settings, but a program that ignores them
can go around. *Older name:* `[machine] wall`, "the in-VM wall".

**Host firewall** (`[machine] host_firewall`, Linux only) — the same rules again on your computer,
around the VM process itself, so even someone who took over the VM's kernel still can't get out.
You install it once with `fy machine host-firewall`. *Older name:* `host_wall`, "the host wall".

**Port range** (*band* in the code) — each project gets its own block of ports on your computer
for the proxy and token services, so two projects never share a listener.

## Credentials

**Clean repo** — the one thing foldyard needs from you: a repo with config in it, not secrets.
The VM mounts the repo, so whatever is committed is inside. Scan it with gitleaks or TruffleHog
before you start.

**Mode** — what access is switched on right now, as a set of switches: `fy mode github=app
gcp=logs`. The resting mode is every switch at its default: zero credentials. *Older name:* posture.

**Switch** — one credential mechanism in the mode: `github`, `gcp`, `claude`, or one you
declare with `[[inject]]`. *Older name:* axis (and `Axis` in the plugin API).

**Level** — one setting of a switch. A switch has two or more, from its default (almost always
`off`) up to the most privileged, e.g. `github=off|app|user`. *Older name:* rung.

**Emergency level** — a level that acts as *you* (your own GitHub or GCP identity). It always
has a TTL — one hour by default, eight at most — and switches itself back off when that runs out.

**Token service** (*minter* in the code) — a small program on your computer that makes a
short-lived token for a switch that's on (a GitHub App installation token, say). Only the kinds
built into foldyard or an installed plugin can run; your repo can't add one.

**Injection** (`[[inject]]`, *injector*) — the proxy adding a credential to requests for one host
as they pass through. The box never holds the real value.

**Keyless** (`[claude] keyless`, `[codex] keyless`) — how an AI agent runs in the box without its
key: the box has a dummy, and the proxy swaps in the real key for the provider's API host. You
paste the real key once, on your computer.

**`host.env`** — `~/.foldyard/<project>/host.env`, the file on your computer holding the secrets
token services need. Readable only by you; never inside the repo or the VM.

**DEGRADED** — a switch is on, but the credential behind it has stopped working (an expired grant,
a revoked token). Shown on `fy mode`, `fy state` and the TUI, with the fix.

**BLOCKED** — a switch is on, but its token service couldn't start (a missing secret, a port
already taken). Shown in the same places, with the reason.

## Config and state

**`foldyard.toml`** — the project's config, committed and shared. **`foldyard.local.toml`** —
your personal overrides beside it, gitignored.

**Adopted config** — the copy of `foldyard.toml` your computer actually runs from, stored outside
the repo. Because the box can write the repo, an edit does nothing on your computer until you read
it (`fy config diff`) and accept it (`fy config adopt`, or the prompt at `fy up`).

**Widenings** (`fy config widenings`) — the list of everything the adopted config lets through:
hosts left undecrypted, where credentials get injected, what text steers the agent.

**Mode mirror** — `.dev-mode.json` in the checkout: a read-only copy of the mode so tools in the
box can see it. Nothing grants access based on it.

**Overlay** (`[[overlay]]`) — an extra compose file layered onto the stack while a switch is at
a given level.

**`fy verify`** — the isolation self-test. It checks the VM's mounts, tries known container
escapes, and checks the box holds no credentials. Run it any time; it exits non-zero on failure.

**Operator** — used in design docs and ADRs for the person at the computer: you.
