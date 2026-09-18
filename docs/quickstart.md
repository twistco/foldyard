# Quickstart — from `init` to a caged, working dev environment

Foldyard grows with you in three steps, and the `foldyard.toml` that `foldyard init` writes
is the guide: it starts as a locked-down, stack-less dev box that is safe to build and poke,
and everything else — agents, your compose stack, the extras — is already in the file as
commented blocks you uncomment when you're ready. This page walks the same arc.

## Before you start

- **A clean repo.** Foldyard mounts only your repo into the VM, so the zero-credential
  guarantee holds exactly when the repo carries config, not secrets. Scan first — see
  [the README's TL;DR](../README.md#tldr).
- **macOS with [uv](https://docs.astral.sh/uv/), [Lima](https://lima-vm.io/) and podman**
  (`brew install uv lima podman`). Lima creates the default machine and podman drives the
  socket it hands out — `fy` preflight refuses without both. See
  [Backends](#backends-lima--podman) for the alternatives.

Install the CLI:

```bash
uv tool install "foldyard[host]"
```

The `[host]` extra pulls mitmproxy, which the host-side egress proxy needs. It is not
optional in practice: `foldyard init` seeds `default_deny = true`, so the box's only route
out is that proxy. `uv tool install` does not link a dependency's console scripts onto PATH,
so for a tool install the copy in foldyard's own venv is the only `mitmdump` that exists —
install foldyard bare and `fy doctor` will tell you to redo it with the extra.

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

## Backends: lima · podman

`init` writes the default — `backend = "lima"` with `wall = true` — which gives each project
its own VM, lets several projects run side by side, and makes egress fail-closed. The
alternatives, in `[machine]`:

- `backend = "podman"` — one shared podman machine for everything. No concurrent
  per-project VMs (macOS runs one podman machine at a time) and no in-VM wall: egress
  control is the cooperative proxy only. Pick it if podman is already your daily driver.

Both are VMs: foldyard always has one, on Linux too
([ADR-0027](./adrs/0027-always-a-vm-native-backend-retired.md)).

Sizing (`cpus`/`memory_mib`/`disk_gib`) applies when the VM is first created; change it
later with `foldyard machine recreate`.

## Upgrading

A repo raises `min_foldyard_version` in the same commit as the setting an older `fy` would
ignore, so after a `git pull` an old `fy` **refuses every verb** — `fy box down` included — until
you upgrade. Do the upgrade first and nothing ever refuses:

```bash
uv tool upgrade foldyard      # 1. the host tool — BEFORE pulling
fy --version
git pull                      # 2. the repo (and its foldyard.toml)
fy doctor                     # 3. ✓ foldyard version · ✓ mitmproxy · ○ adopted config (expected)
fy config diff                # 4. read what the host would start honouring…
fy config adopt               #    …and adopt it explicitly
fy box down && fy box up      # 5. recreate the box on the new version
```

Why each line is the way it is:

- **`uv tool upgrade`, never `uv tool install --upgrade`.** The latter re-specifies the
  requirement as bare `foldyard`: the `[host]` extra is dropped (mitmproxy uninstalled — the next
  `fy box up` fails preflight, and the box's egress would connection-refuse) and an editable
  checkout is swapped for PyPI's. `upgrade` re-resolves the install exactly as it was made. If it
  answers "Nothing to upgrade" (a pinned install), or `fy doctor` shows `mitmproxy` missing,
  the row prints the reinstall shaped to *your* install: `uv tool install --force
  'foldyard[host]'` for a PyPI install, the same with `--editable '<checkout>[host]'` for one
  made from a foldyard checkout — run the line as printed, not the other one. (Editable from a
  checkout? `upgrade` rebuilds from whatever that checkout holds, so pull *it* — not the repo
  you are in — first.)
- **Adopt explicitly, having read the diff.** `fy box up` would show the same diff and ask, but
  its default answer is *ignore*: press Enter and the host keeps running the *previous*
  config, asks again next time, and the launch looks like it worked. `fy config diff` shows what
  the host would start honouring — credential injection and egress capture live in this file —
  and `fy config adopt` is the one act that changes it ([configuration](./configuration.md)).
- **`down` then `up`, not `up` alone.** `fy box up` on a running box says "already up" and
  reinstalls nothing; the recreate is what installs the box's own `fy` to match the host's, and
  what replaces a host supervisor still running the old code. Finish or park in-box agent
  sessions first — `box down` archives their transcripts, but a mid-task agent is interrupted.
- **`fy code` afterwards** if the release moved anything under `[vscode]`: the attach config is
  authored from the adopted table, so it is stale until re-authored.

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
