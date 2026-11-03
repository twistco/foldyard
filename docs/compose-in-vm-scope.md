# Compose in the VM: scope

Status: **accepted, phase 1 built** (experimental), 2026-11-03. The four decisions below were taken as
recommended. Related:
[ADR-0022](./adrs/0022-host-runs-the-adopted-config.md) (its 0.3.2 amendment names this as the
structural fix), [ADR-0023](./adrs/0023-no-host-executed-code-from-the-repo-mount.md),
[ADR-0010](./adrs/0010-podman-everywhere-container-host.md) (why builds use native `podman build`),
[ADR-0033](./adrs/0033-a-to-box-and-a-from-box-beside-the-checkout.md), and
[security.md](./security.md)'s "The compose client reads your compose files on your computer".

## The problem

Your stack runs in the VM, but the compose client that starts it runs on your computer. It parses
compose files the box can edit, so the box chooses what the client reads. Two clamps stand in for
a boundary today:

- **The environment.** `stack._host_passthrough` gives the client an allowlisted environment, not
  the supervisor's (which holds every `host.env` secret). `[project] compose_env` widens it.
- **The paths.** `composeguard` refuses compose files that name paths outside the checkout. It is
  a list of keys that has to track compose's grammar (it once refused Tangible's `!override` tags),
  and the client re-opens every path after the guard checked it, so a box that swaps a checked file
  for a symlink in between wins the race.

Inside the box the client already runs in the VM, and neither clamp applies there. This scope moves
the host's client there too.

### What runs the client on your computer today

| caller | what it runs | who calls it |
| --- | --- | --- |
| `stack._compose` | `up -d --no-build`, `down`, `ps`, `logs`, `exec` (`fy shell`), `run` | the stack verbs |
| `stack._render_config` | `config`, parsed host-side | `up` (stranded dependents, the guard), `reclaim`, `_service_profiles` |
| `stack._podman_build` | `config`, then `podman build` and `podman tag` per service (the build context packed on your computer) | `fy up`, `fy build`, through `buildgate.run` |
| `stack.recreate_services` | `up` for named services | the supervisor's capability heal |
| `stack._compose_captured` | `ps`, parsed for running and crashed services | reconcile, `fy state`, doctor |
| `confighash.desired` | podman-compose's own parser, in a `python -P -m foldyard.confighash` child | `fy state` |
| `devmode._rendered_compose` | `config` | doctor's stack-env row |
| `box._build_img` | `podman build` of the box image, the repo as its context, packed on your computer | `fy box build`, `fy box up` (not compose, but the same shape) |

## Proposal: the guest runs the client, as the VM user, over ssh

- **Where.** In the guest, as the VM user, over the backend's ssh route (`Backend.ssh_target`,
  the one `crossinject` and `guestlog` already use, so both backends have it). Not in a container:
  that needs an image in the VM (built or pulled through the wall, and the consumer's box image may
  have no Python), and the dev box may not be running, or may not exist at all for a box-only
  consumer.
- **What.** foldyard's own pinned podman-compose (the version in foldyard's venv) with its two
  pure-Python dependencies (PyYAML, python-dotenv), delivered by foldyard, plus a small packaged
  entry point (`assets/compose/`) that reads one request from stdin (the compose argv, the env,
  the working directory) and runs podman-compose, the config hash or the builds, streaming output
  back. It is foldyard's code, run in the guest like the socket filter and the crossmask helper,
  so ADR-0023 is not in tension.
- **Which podman.** The guest's own `podman`, in remote mode against the same guest socket the
  host's client uses today (`podman.sock`, or `podman-runsc.sock` under the gVisor posture), so a
  posture's runtime selection doesn't change. The client is then always the server's own version:
  in the box today, a podman-remote 5.4.2 client against a 5.8.4 server fails
  `--build-context name=<dir>` with "invalid additional build context format", so the host build's
  `additional_contexts` currently depend on how new your computer's podman is.
- **The env.** The host still computes `ctx.env` and sends it in the request, with `DOCKER_HOST` /
  `CONTAINER_HOST` rewritten to the guest-side socket. Nothing comes from the guest's login
  environment except what podman itself needs (`XDG_RUNTIME_DIR` and the like).
- **The paths.** The checkout and its worktrees are mounted at the same path in the guest, and the
  posture overlays live in the checkout, so every `-f` resolves the same way. The build log stays
  host-side, written from the streamed output as today.
- **What the host keeps.** Engine selection, the adopt gate, the build gate (its per-build secret,
  the grant prompts, reading the proxy log), computing the env, and deciding from the answers.

### Why running there is safe: the premise the spike must prove

The VM user is the box's uid, and any container a box creates through the engine socket can
bind-mount whatever the VM user can read. So a client running as the VM user should reach nothing
a box can't already reach. **The spike checks this, rather than assuming it**, for each of: Lima's
`cidata` mount, the VM user's home in the guest, the to-box and the from-box, the guest monitor's
files (root-owned, so out of reach either way), and the gVisor posture's filtered socket (does it
allow the same bind mounts?). Anything the VM user can read that a box can't is a finding, fixed
before this lands.

Two consequences of running as the VM user:

- **The answers are box-influenced data.** Anything that can write the VM user's files (a box
  writing `~/.bashrc` through a bind mount, say, which the `bash -c` that sshd starts reads)
  changes what comes back. The host already treats the render and `ps` as claims, since the
  compose files are the box's anyway. The host must never act on an answer beyond the project
  it asked about. That's already the rule for engine output.
- **Terminal output.** podman-compose's output reaches your terminal today and keeps doing so.
  foldyard strips control sequences from what it relays from the guest, as it does for the box's
  tmux fields.

## What it removes

- **`composeguard` on the host.** Once no compose client runs on your computer, the paths it
  guards are read in the guest, where an `env_file: /etc/passwd` reads the guest's. The module, its
  tests and stack.md's "Which paths compose may read" section go. This needs the move to be total:
  no host fallback.
- **The compose half of `_host_passthrough`.** It becomes the explicit env the request carries.
  The fixed passthrough and `[project] compose_env` keep their meaning, and `fy config widenings`
  still lists `compose_env`, since that is how a `host.env` name reaches compose.
- **confighash's host child.** The config hash runs in the guest, through the same entry point.
- **ADR-0022's amendment and security.md's bullet** turn from "planned" into what it is.
- **A smaller supervisor split (plan 03).** The process holding `host.env` then parses no compose
  file at all: what remains is ssh and engine calls with the clamped env.

## Open questions for the spike

1. **Delivery.** Stream the payload on every call (the crossmask helper's pattern: no file in the
   guest the VM user can change), or cache it in the guest under its hash and check the hash on each
   run? Which Python each guest has (the Fedora 44 template, podman machine's CoreOS), and whether
   podman-compose runs on PyYAML's pure-Python path there.
2. **Latency.** One ssh per compose call. `fy up` makes about 6–10, `fy state` and doctor 1–3.
   Measure on vz, qemu/9p and WSL2, and use an ssh `ControlMaster` (its socket on your computer) if
   it's needed.
3. **Interactive verbs.** `fy shell` (`exec -it`) and `fy logs -f` over `ssh -t`, Ctrl-C included.
4. **The gVisor posture.** Confirm the client in the guest against `podman-runsc.sock` creates the
   same containers as today's host client.
5. **The docker-compose provider.** The guest has no `docker-compose` binary. Proposal: support only
   podman-compose here and refuse `PODMAN_COMPOSE_PROVIDER=docker-compose` on the host. (In the box,
   and in CI's in-box topology, nothing changes.)
6. **The podman-machine backend.** The ssh route exists. Check its user and socket paths.

Out of scope: **which compose client long-term** (plan 04 asked whether docker-compose in the VM
could retire podman-compose's quirks). Keep podman-compose for this move, so only one thing changes
at a time.

## Phases

0. **Spike** on `foldyard-example`: the open questions, vz locally, qemu/9p and WSL2 through a CI
   dispatch. It is a host-tier module, `tests/test_guestcompose_e2e.py` (shards L1 and W1), which
   writes the call cost and the premise table to the run's step summary. Already seen from a dev
   box: the payload (the runner, podman-compose 1.6.0, PyYAML 6.0.3, python-dotenv 1.2.2) is a
   96 KB zip, which loads from a memfd under `python3 -I` and renders on PyYAML's pure-Python
   parser.
1. **Read-only, behind an experimental switch** (`[machine] compose = "vm"`, in
   `experimental.REGISTER`): `ps`, `config --services` and the config hash run in the guest
   (`foldyard.guestcompose`). **Built.** The full `config` render does NOT move yet: while `up`
   and the builds run on your computer, the path guard must judge what THAT client will read, and
   a render from the guest is the box's to shape (the VM user's home is box-writable). A guard fed
   a clean render from the guest while the client on your computer reads the real files would be
   worse than today. So the render moves with `up`, in phase 2.
2. **Mutating verbs:** build, `up`, `down`, the supervisor's recreate, `logs`, `shell`, `run`.
3. **`fy box build`** in the guest too.
4. **Default on, then the host path deleted** with `composeguard`, the docs section, and a new ADR
   amending 0022. There is no permanent fallback: with ssh unavailable, the verb refuses and says so.

## Tests

- **Golden:** the argv becomes the ssh route plus the request. A guard test, like
  `tests/test_selfspawn.py`'s, fails on any compose client or `podman build` argv in host code paths
  outside the guest runner.
- **Live, red first:** a compose file whose `env_file:` names a file that exists only on your
  computer. Today, with the guard switched off, its contents reach the container. In the VM, the
  file isn't there. The existing host tiers (`test_e2e.py`, `test_worktree_e2e.py`,
  `test_probes_e2e.py`) must pass unchanged.

## Decisions for the maintainer

1. Where it runs: the guest, as the VM user, over ssh (recommended), rather than a container.
2. Rollout: an experimental switch, then default on, then the host path deleted, with no permanent
   fallback.
3. The docker-compose provider on your computer: refused.
4. `fy box build` included, as phase 3.
