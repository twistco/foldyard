# Quickstart — from `init` to a caged, working dev environment

Foldyard grows with you in three steps, and the `foldyard.toml` that `foldyard init` writes
is the guide: it starts as a locked-down, stack-less dev box that is safe to build and poke,
and everything else — agents, your compose stack, the extras — is already in the file as
commented blocks you uncomment when you're ready. This page walks the same arc.

## Before you start

- **A clean repo.** Foldyard mounts only your repo into the VM, so the zero-credential
  guarantee holds exactly when the repo carries config, not secrets. Scan first — see
  ["Before you adopt"](../README.md#before-you-adopt-a-clean-repo) in the README.
- **macOS with [uv](https://docs.astral.sh/uv/) and [Lima](https://lima-vm.io/)**
  (`brew install uv lima`). Lima backs the default machine; see
  [Backends](#backends-lima--podman--native) for the alternatives.

Install the CLI:

```bash
uv tool install foldyard
```

That puts `foldyard` on your PATH, with `fy` as a shorthand — `fy <verb>` and
`foldyard <verb>` are the same thing.

## Step 1 — a locked-down box you can safely poke

```bash
cd your-repo
foldyard init        # writes foldyard.toml + fences foldyard's generated dirs into .gitignore
fy box up            # creates the VM on first run, builds/starts the dev box inside it
fy box shell         # a root shell inside the box
```

What you now have:

- a **rootless Lima VM** that mounts only this repo — no `$HOME`, no other checkouts;
- an **egress wall** inside the VM: fail-closed, so the box's only way out is the
  allowlisting proxy running on your host — and that proxy ENFORCES from the first run
  (`fy init` seeds `default_deny = true`). The hosts the box needs to build itself are
  `[proxy] recommend`ed in the generated config and offered to you, one yes each, before the
  first build; `fy allow wall off` drops back to observe-only if you'd rather watch first;
- **zero credentials**: no ssh keys, no tokens, no push access. Committing works; pushing
  is refused.

Prove the cage rather than trusting it:

```bash
fy verify            # mount audit + escape attempts + posture checks — should be ALL PASS
```

Poke around from the box shell as much as you like. Nothing you run in there — installs,
build scripts, curl — can reach your Mac, your keys, or your other repos.

## Step 2 — put an agent in the yard

Uncomment ONE agent table in `foldyard.toml` — `[claude]` for Claude Code or `[codex]` for
OpenAI Codex — including its `keyless` line, then:

```bash
fy box up            # re-runs the bootstrap: installs the agent, mounts its state volumes
fy mode claude=on    # declare the posture; you'll be prompted ONCE for the real token
fy claude            # Claude Code, running inside the cage
```

**Keyless** means the real API token never enters the box: a dummy value lives inside, and
the Mac-side proxy swaps it for the real one in flight. The token is captured once on the
Mac (hidden prompt) and stored only there. The agent gets full autonomy — including the
machine's own container socket, so it can drive your stack — bounded to the repo and the VM.

A useful first task for the in-box agent: hand it the rest of this config to work out with
you.

## Step 3 — bring your stack in

Uncomment the stack lines in `[project]` and add your host ports:

```toml
[project]
app = "app"                  # the compose service `fy shell` targets
app_port = "WEB_PORT"        # which [ports] key `fy open` opens
compose = ["compose.yml"]    # your compose files, in -f order

[ports]
WEB_PORT = 3000              # host-published; worktrees get offset copies automatically
```

```bash
fy up                # the whole compose stack, inside the same VM
fy ps                # what's running
fy logs [svc]        # tail a service's logs
fy open              # open the app in your Mac browser
fy tui               # the dashboard: posture, network log, doctor, one-click fixes
```

Services reach each other by container name; your Mac reaches them on the published ports.
From here, grow into the rest of the commented blocks as the project needs them: your own
box image (`[box] image`), monitored tool installs (`[[box.tools]]`), VS Code attach
(`[vscode]` + `fy code`), posture-conditional compose overlays (`[[overlay]]`).

Parallel work happens in worktrees: `fy worktree add <name>` gives each branch its own
namespaced stack with offset ports, sharing the one VM and its caches.

## Backends: lima · podman · native

`init` writes the default — `backend = "lima"` with `wall = true` — which gives each project
its own VM, lets several projects run side by side, and makes egress fail-closed. The
alternatives, in `[machine]`:

- `backend = "podman"` — one shared podman machine for everything. No concurrent
  per-project VMs (macOS runs one podman machine at a time) and no in-VM wall: egress
  control is the cooperative proxy only. Pick it if podman is already your daily driver.
- `backend = "native"` — the host's rootless podman socket directly, no VM. Linux/CI
  convenience; the weakest profile (shared kernel), so it's an explicit opt-in.

Sizing (`cpus`/`memory_mib`/`disk_gib`) applies when the VM is first created; change it
later with `foldyard machine recreate`.

## When something's off

`fy doctor` diagnoses; `fy tui` shows the same checks with one-click fixes where they're
safe. `fy verify` is the trust check — run it whenever you want to re-prove the boundary.

Where next: the [docs map](./README.md) — posture modes and TTLs, the egress proxy and
capture log, the security model, and (for the "why") the [ADRs](./adrs/).

**From inside the box** (or anywhere without this page open): `fy docs` lists these same topics
and prints them, served from the installed package — so they always describe the version you're
running, and they work with no network. `fy init` also drops the bundled **`foldyard` skill** into
`.claude/skills/`, which is what orients an agent working in the box: what it can't do (push,
grant itself credentials, widen its own egress), how to ask for what it needs, and why a change
might not have taken effect. `fy skill list` shows what else is bundled.
