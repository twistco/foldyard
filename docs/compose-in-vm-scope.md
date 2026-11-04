# Compose in the VM: scope

Status: **phases 0–3 built, behind an experimental switch**, 2026-11-04. With `[machine] compose =
"vm"`, no compose client runs on your computer. Phase 4 (on by default, the host path deleted) waits
on CI's 9p and WSL2 hosts and on real use. The four decisions below were taken as recommended.
Related:
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

### What ran the client on your computer

All of this moved under the switch, and each row still runs here without it.

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
| `stack._service_profiles`, `reconcile._overlay_services` | not a client at all: `yaml.safe_load(path.read_text())` of each compose file, through any link the box left there | `up`'s running profiles, `fy state` |

The last row wasn't in the first draft of this table, and the path guard never covered it. It turned
up while moving the others, which is the argument for a boundary over a list of keys.

## The design: the guest runs the client, as the VM user, over ssh

- **Where.** In the guest, as the VM user, over the backend's ssh route (`Backend.ssh_target`,
  the one `crossinject` and `guestlog` already use, so both backends have it). Not in a container:
  that needs an image in the VM (built or pulled through the wall, and the consumer's box image may
  have no Python), and the dev box may not be running, or may not exist at all for a box-only
  consumer.
- **What.** `foldyard.guestcompose` sends one payload per call on `python3 -I -`'s stdin: foldyard's
  own pinned podman-compose (the version in foldyard's venv) with its two pure-Python dependencies
  (PyYAML, python-dotenv) and a small runner (`assets/compose/runner.py`), as a 96 KB zip imported
  from memory, plus one request (the argv, the env, the working directory). Nothing is installed in
  the guest. It is foldyard's code, run in the guest like the socket filter and the crossmask
  helper, so ADR-0023 is not in tension.
- **Which podman.** The guest's own `podman`, in remote mode against the socket your computer's
  client uses (`podman.sock`), so what it starts belongs to the API service, not to the ssh session,
  and a posture's runtime selection doesn't change. The client is then always the server's own
  version: in the box, a podman-remote 5.4.2 client against a 5.8.4 server fails `--build-context
  name=<dir>` with "invalid additional build context format", so a build's `additional_contexts`
  otherwise depend on how new the client's podman is.
- **The env.** The host still computes `ctx.env` and sends it in the request, with `DOCKER_HOST` /
  `CONTAINER_HOST` rewritten to the guest-side socket. Nothing comes from the guest's login
  environment except what podman itself needs (`XDG_RUNTIME_DIR` and the like).
- **The paths.** The checkout and its worktrees are mounted at the same path in the guest, and the
  posture overlays live in the checkout, so every `-f` resolves the same way. The build log stays
  host-side, written from the streamed output as today.
- **What the host keeps.** Engine selection, the adopt gate, the build gate (its per-build secret,
  the grant prompts, reading the proxy log), computing the env, deciding from the answers, and
  engine calls that parse no compose file (`ps`, `inspect`, `image inspect`).
- **`fy shell`** is the one verb that doesn't go there: `exec` needs your terminal on the stdin the
  payload uses. It finds the container by its compose labels through the engine and runs `podman
  exec -it`, the way `fy box shell` enters a box.
- **Two builds stay here on purpose:** the packaged box image and the `[box].packages` layer. Their
  contexts are foldyard's own files, which the VM can't see and the box can't write.

### Why running there is safe: the premise, and what the spike found

The VM user is the box's uid, and any container a box creates through the engine socket can
bind-mount whatever the VM user can read. So a client running as the VM user should reach nothing a
box can't already reach. `test_the_vm_user_reads_nothing_a_box_cant` checks this through the engine
socket, as a box must, and a path the VM user reads and a box can't is a finding. On a vz guest with
the firewall, the monitor and the forwarder all on:

| path | VM user | box |
| --- | --- | --- |
| `/mnt/lima-cidata` (the boot scripts, keys included), `user-data.txt`, `cloud-init-output.log` | refused | — |
| `/etc/fy-monitor/relay.key`, `/etc/fy-fwd/forwarder.key`, `/etc/sudoers.d`, `/var/log/tetragon` | refused | — |
| `/run/fy-wall`, `/run/fy-monitor`, `/var/lib/fy-monitor` (the inbox) | readable | readable |
| `/var/log/journal`, `/run/log/journal` | readable (group `systemd-journal`) | readable, see below |

**The journal is the one that took looking at.** The VM user reads it through a supplementary group,
which a container doesn't keep by default, and a remote client refuses `--group-add keep-groups`.
But that refusal is the client's: `--annotation run.oci.keep_original_groups=1` reaches crun from any
client, and the API service holds the group. So a box can read the VM's whole journal, and the
premise holds (the test has a negative control, without the annotation). On those guests the relay
and forwarder keys are not in the journal, and nothing else secret was seen in it.

Not yet checked: the premise under the gVisor posture's filtered socket.

Consequences of running as the VM user:

- **The answers are box-influenced data.** Anything that can write the VM user's files (a box writing
  `~/.bashrc` through a bind mount, say, which the `bash` that sshd starts reads) changes what comes
  back. The host already treats the render and `ps` as claims, since the compose files are the box's
  anyway. So a failed call's stdout is never parsed, and the host acts on an answer only for the
  project it asked about, the existing rule for engine output.
- **Names in `[project] compose_env`** reach the guest on every compose call, where a box could read
  them from the client's `/proc`. That widens nothing: a box can already edit a compose file to
  interpolate any of them into a container it can inspect.
- **Terminal output.** What's relayed from the guest has its control sequences stripped
  (`guestcompose.printable`). The client on your computer doesn't do that, so `fy logs` is cleaner
  here than it is without the switch.

### What changes for a project

- **The checkout must be mounted in the VM**, because the client reads it there. A real project's
  always is, since `fy machine ensure` creates the VM from it. A VM that doesn't mount the checkout
  used to work for image-only and context-shipped stacks, and now refuses, naming `fy machine
  recreate`.
- **Registry logins on your computer don't reach the VM.** A client on your computer read
  `auth.json` and sent the credentials with each pull and build, which is itself a host secret the
  compose files could spend. In the VM there is no such file, so a private base image needs another
  route. **Open:** what that route should be before phase 4.
- **`${HOME}`, `~` and `$USER` in a compose file are the guest's.** Your computer's home was never in
  the VM, so a bind mount from it found nothing there anyway.
- **Your own recipes aren't moved.** A recipe that runs `"$ENGINE" compose` after `eval "$(fy
  shellenv)"` is still a compose client on your computer, and foldyard can't see it.
- **Only podman-compose runs there:** `PODMAN_COMPOSE_PROVIDER` naming docker-compose is refused, and
  preflight refuses `vm` on the docker engine.

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

## What the spike answered

1. **Delivery:** the payload on stdin on every call, imported from memory. There is nothing to cache
   or invalidate, and no version to drift. An earlier version loaded it from a memfd, but
   `memfd_create` doesn't exist on macOS, where the hermetic tests run the payload.
2. **Latency:** on vz, a bare ssh is about 125 ms and a whole `config --services` about 240 ms, payload
   included. `fy up` makes about 10 calls, so connection reuse isn't needed there. 9p and WSL2 are
   measured by the same test in CI: it writes the numbers to the step summary.
3. **Interactive verbs:** `fy logs -f` runs without a terminal, so nothing would tell the guest when
   ssh goes; the runner watches its parent and ends its process group. `fy shell` doesn't go there
   (above). Both are tested live, the shell through a real pty.
4. **A file your computer just replaced** can read as missing from the VM for up to about 1 s on
   virtiofs, 5 s on libkrun (ADR-0021), and compose would call that a missing file. So the runner
   waits up to 6 s for a `-f` file before compose runs.

**Still open:** the gVisor posture, the podman-machine backend (its ssh route exists, untested), and
9p and WSL2 until CI runs them.

Out of scope: **which compose client long-term** (plan 04 asked whether docker-compose in the VM
could retire podman-compose's quirks). Keep podman-compose for this move, so only one thing changes
at a time.

## Phases

0. **Spike.** Done on vz: `tests/test_guestcompose_e2e.py` (shards L1 and W1).
1. **Read-only calls, behind the switch** (`[machine] compose = "vm"`, in `experimental.REGISTER`).
   Done. The full render stayed on your computer until `up` moved: a guard fed a render from the guest
   while the client here reads the real files would be worse than either.
2. **Every other call:** the render, the builds, `up`, `down`, `logs`, the supervisor's recreate, and
   the two reads that parsed compose files here. Done. The guard stands down under the switch.
3. **`fy box build`**, for a project's own image. Done.
4. **Default on, then the host path deleted** with `composeguard`, the docs section, and a new ADR
   amending 0022. There is no permanent fallback: with ssh unavailable, the verb refuses and says so.

## Tests

- **Hermetic** (`tests/test_guestcompose.py`): the real payload run by the test's interpreter. An AST
  guard fails on any new use of `ctx.compose` outside the helpers that ask `guestcompose` first.
- **Live** (`tests/test_guestcompose_e2e.py`): the red-first controls prove the channel with the client
  on your computer (an `env_file:` reaching a container, an `include:` reaching a render) and then that
  it's closed from the guest, including a planted symlink, the race the guard can't close. A logging
  `podman` first on the CLI's PATH makes "no compose client or build ran on your computer" an assertion
  over what ran. `fy verify` passes.
- **The existing tiers, unchanged, with `MACHINE_COMPOSE=vm`:** their copies aren't in the VM's mounts,
  so give the VM one for a directory of its own and run under it:

  ```bash
  limactl stop foldyard-example
  limactl edit --tty=false foldyard-example \
    --set ".mounts += [{\"location\":\"$HOME/fy-e2e/vmtier\",\"mountPoint\":\"$HOME/fy-e2e/vmtier\",\"writable\":true}]"
  limactl start foldyard-example
  FOLDYARD_E2E=1 MACHINE_COMPOSE=vm CONTAINER_HOST=unix://$HOME/.lima/foldyard-example/sock/podman.sock \
    uv run --group e2e pytest --basetemp=$HOME/fy-e2e/vmtier/run tests/test_e2e.py tests/test_probes_e2e.py …
  ```

  `fy verify` fails on that VM, correctly, for that extra mount. `test_reclaim_e2e` must not directly
  follow `test_worktree_e2e` there: the removed worktree's image is one more orphan than it expects.

## Decisions for the maintainer

1. Where it runs: the guest, as the VM user, over ssh (recommended), rather than a container.
2. Rollout: an experimental switch, then default on, then the host path deleted, with no permanent
   fallback.
3. The docker-compose provider on your computer: refused.
4. `fy box build` included, as phase 3.
