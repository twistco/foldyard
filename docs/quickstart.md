# Quickstart — from `init` to a working, isolated dev environment

`foldyard init` writes a `foldyard.toml` that doubles as the guide. It starts as a locked-down
dev box with no stack, safe to build and poke. Everything else — agents, your compose stack,
the extras — is already in the file as commented blocks you uncomment when you're ready. This
page follows the same three steps.

New words (box, mode, switch, allowlist, adopted config…) are defined in the
[glossary](./glossary.md).

## Before you start

**A clean repo.** Foldyard mounts only your repo into the VM, so the zero-credential guarantee
holds exactly when the repo holds config, not secrets. Scan your history with
[gitleaks](https://github.com/gitleaks/gitleaks) or
[TruffleHog](https://github.com/trufflesecurity/trufflehog) first, and move anything they find
out of the repo.

**Three tools: [uv](https://docs.astral.sh/uv/), [Lima](https://lima-vm.io/) and podman.** Lima
creates the VM; podman talks to the container engine inside it. `fy` refuses to start without
both.

macOS:

```bash
brew install uv lima podman
```

Linux (x86-64; Debian/Ubuntu package names shown — use your distribution's equivalents):

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh          # uv
sudo apt-get install qemu-system-x86 qemu-utils ovmf podman
curl -fsSL https://github.com/lima-vm/lima/releases/download/v2.2.0/lima-2.2.0-Linux-x86_64.tar.gz \
  | sudo tar Cxz /usr/local                               # Lima (2.2.0 is the version CI tests)
```

On Linux you also need:

- **KVM** — `/dev/kvm` must exist and be writable by you (usually: be in the `kvm` group).
- **`qemu-img`** — it comes in `qemu-utils` on Debian/Ubuntu; the QEMU system package alone
  doesn't include it.
- **A `systemd --user` session** — a normal desktop or SSH login has one. For a machine you
  reach some other way, run `loginctl enable-linger $USER` once.

What has been tested on Linux and inside WSL2, and what hasn't, is tracked in
[linux-support.md](https://github.com/twistco/foldyard/blob/main/docs/linux-support.md).

### Install the CLI

```bash
uv tool install "foldyard[host]"
```

This puts `foldyard` on your PATH, with `fy` as shorthand — `fy <verb>` and `foldyard <verb>`
are the same thing.

Don't leave out the `[host]` extra. It installs mitmproxy, which runs the egress proxy on your
computer, and the box's only way out is that proxy. `uv tool install` doesn't put a
dependency's commands on your PATH, so the copy inside foldyard's own install is the only
`mitmdump` you'll have. If you installed without the extra, `fy doctor` tells you how to fix it.

## Step 1 — a locked-down box you can safely poke

```bash
cd your-repo
foldyard init        # writes foldyard.toml and adds foldyard's generated files to .gitignore
fy box up            # creates the VM on first run, then builds and starts the dev box in it
fy box shell         # a root shell inside the box
```

What you now have:

- **A rootless Lima VM that mounts only this repo** — no home directory, no other checkouts.
- **A VM firewall.** Traffic from the box can only leave through the proxy on your computer.
  Anything that tries to go around the proxy is refused.
- **An allowlist, learning for its first hour.** `init` writes `[proxy] enforce = "learn"`. For
  the first hour after your first `fy box up`, nothing is refused: the proxy lets everything
  through and records every host it *would* have refused. Then it starts enforcing by itself,
  so it can't be left open by mistake. Grant what it saw in one reviewed batch with
  `fy allow learn`. To enforce straight away, run `fy allow enforce on`; to learn again (say,
  for a new dependency), `fy allow enforce learn --for 30m`.
- **Recommended hosts.** The hosts the box needs to build itself are listed under
  `[proxy] recommend` in the generated config. `fy box up` offers each one to you — one yes
  each. The repo can suggest a host; only you can grant it.
- **Zero credentials** — no SSH keys, no tokens, no push access. Committing works; pushing is
  refused.

Check the isolation rather than trusting it:

```bash
fy verify            # mount audit + escape attempts + credential checks — should be ALL PASS
```

Poke around from the box shell as much as you like. Nothing you run in there — installs, build
scripts, `curl` — can reach your computer, your keys or your other repos.

## Step 2 — put an agent in the box

Uncomment **one** agent table in `foldyard.toml` — `[claude]` for Claude Code or `[codex]` for
OpenAI Codex — including its `keyless` line. Then:

```bash
fy box up            # installs the agent; asks once (hidden input) for your real API key/token
fy mode claude=on    # switch on the proxy's key swap for the agent
fy claude            # Claude Code, running inside the box
```

**Keyless** means the real key never enters the box. The box holds a dummy value, and the proxy
on your computer swaps in the real one as each request goes out. You paste the real key once,
and it is stored only on your computer. The agent gets full autonomy — including the VM's
container socket, so it can drive your stack — but only over the repo and the VM.

A good first task for the agent: work out the rest of this config with you.

## Step 3 — bring your stack in

Uncomment the stack lines in `[project]` and add your ports:

```toml
[project]
app = "app"                  # the compose service `fy shell` opens
app_port = "WEB_PORT"        # which [ports] key `fy open` opens
compose = ["compose.yml"]    # your compose files, in -f order

[ports]
WEB_PORT = 3000              # published on your computer; worktrees get offset copies
```

```bash
fy up                # the whole compose stack, inside the same VM
fy ps                # what's running
fy logs [svc]        # follow a service's logs
fy open              # open the app in your browser
fy tui               # the dashboard: mode, network log, doctor, one-click fixes
```

Services reach each other by container name; your computer reaches them on the published ports.

From here, uncomment the rest as the project needs it: your own box image (`[box] image`),
monitored tool installs (`[[box.tools]]`), VS Code attach (`[vscode]` + `fy code`), compose
overlays that change with the mode (`[[overlay]]`). Every key is in
[configuration.md](./configuration.md).

For parallel work, `fy worktree add <name>` gives a branch its own checkout and its own stack,
with offset ports, sharing the one VM and its caches.

## Backends: lima · podman

`init` writes the default, `backend = "lima"` with `firewall = true`. Lima gives each project
its own VM, runs several side by side, and is the only backend that carries the VM firewall.
The one alternative, set in `[machine]`:

- `backend = "podman"` — one podman machine, shared by every project, and only one runs at a
  time. There is no VM firewall, so routing through the proxy is cooperative only. Pick it if
  podman machine is already your daily driver.

Either way there is always a VM, on Linux too. The old VM-less `native` backend is gone
([ADR-0027](./adrs/0027-always-a-vm-native-backend-retired.md)); a config that still names it
gets a warning and the podman backend.

Two more `[machine]` options add layers on top: `runtime = "gvisor"` runs the box under
gVisor's userspace kernel, and `host_firewall = true` (Linux only) adds a second firewall around
the VM on your computer, installed once with `fy machine host-firewall`. See
[configuration.md](./configuration.md#machine).

Sizing (`cpus`, `memory_mib`, `disk_gib`) applies when the VM is first created. To change it
later, run `foldyard machine recreate`.

## Upgrading

When a repo starts relying on a newer foldyard, it raises `min_foldyard_version` in the same
commit. After a `git pull`, an older `fy` then **refuses every verb** — `fy box down` included —
until you upgrade. Upgrade *before* you pull and nothing refuses:

```bash
uv tool upgrade foldyard      # 1. upgrade the tool — before pulling
fy --version
git pull                      # 2. pull the repo (and its foldyard.toml)
fy doctor                     # 3. expect ✓ foldyard version, ✓ mitmproxy, ○ adopted config
fy config diff                # 4. read what your computer would start running…
fy config adopt               #    …and adopt it
fy box down && fy box up      # 5. recreate the box on the new version
```

Why each step is the way it is:

- **Use `uv tool upgrade`, not `uv tool install --upgrade`.** The second form re-installs plain
  `foldyard`: it drops the `[host]` extra (so mitmproxy is removed, the next `fy box up` fails
  its checks, and the box loses its way out) and swaps an editable checkout for the PyPI
  release. `upgrade` keeps the install exactly as you made it.
- **If `upgrade` says "Nothing to upgrade"** (a pinned install), or `fy doctor` shows
  `mitmproxy` missing: the doctor row prints the reinstall command for *your* kind of install.
  Run it exactly as printed. For reference, it is `uv tool install --force 'foldyard[host]'`
  for a PyPI install, and the same with `--editable '<checkout>[host]'` for an install from a
  foldyard checkout.
- **Installed editable from a foldyard checkout?** `upgrade` rebuilds from whatever that
  checkout holds, so pull *the foldyard checkout* first — not the repo you work in.
- **Adopt on purpose, after reading the diff.** `fy box up` shows the same diff and asks, but
  its default answer is *ignore*. Press Enter and your computer keeps running the *previous*
  config, asks again next time, and the launch looks like it worked. `fy config diff` shows
  what would change — including where credentials are injected and which traffic is decrypted
  — and `fy config adopt` is the one step that applies it (see
  [configuration.md](./configuration.md#the-adopted-config-what-the-host-actually-runs)).
- **`fy box down` then `fy box up`, not `fy box up` alone.** On a running box, `fy box up` says
  "already up" and changes nothing. Recreating the box is what installs the new `fy` inside it
  and restarts the supervisor on the new code. Finish or park agent sessions in the box first:
  `fy box down` archives their transcripts, but an agent mid-task is interrupted.
- **Run `fy code` again** if the release changed anything under `[vscode]`: the VS Code attach
  config is written from the adopted config, so it's out of date until regenerated.

## When something's off

`fy doctor` diagnoses; `fy tui` shows the same checks, with one-click fixes where they're safe.
`fy verify` is the isolation check — run it whenever you want to re-check the boundary.

Where next: the [docs map](https://github.com/twistco/foldyard/blob/main/docs/README.md) —
modes and time limits, the egress proxy and its log, the security model — and, for the
reasoning behind the design, the
[ADRs](https://github.com/twistco/foldyard/tree/main/docs/adrs).

**From inside the box** (or anywhere without this page open): `fy docs` lists these same pages
and prints them. They're served from the installed package, so they always match the version
you're running, and they work offline. `fy init` also adds the bundled **`foldyard` skill** to
`.claude/skills/`. It orients an agent working in the box: what it can't do (push, give itself
credentials, widen its own allowlist), how to ask you for what it needs, and why a change
might not have taken effect. `fy skill list` shows the other bundled skills.
