# Testing foldyard

What runs where, how to run it, and the rules for tests that reach an engine.

foldyard's surface splits by *where it can be validated*:

1. **Substrate + TUI** — `devmode`/`config`/`plugins` unit tests + headless Textual pilot
   tests. Run anywhere, no engine. Includes the **property-based tier** (hypothesis):
   `tests/test_properties.py` (pure-function properties + `settle_incoherent` over GENERATED
   registries — synthetic axes/constraints from `tests/pbt.py`),
   `tests/test_monitor_properties.py` (a hook's deliveries == the whole-store join, or a `gaps`
   entry says why, over generated streams, rotation and filters) and `tests/test_reconcile_model.py`
   (a stateful model driving the real set_mode/expiry/probe/publish pipeline — including the box
   mirror tier via box_up/box_down rules — through generated operation sequences). Convention: a new property must be validated **red first** — mutate the
   code under test, watch hypothesis shrink a counterexample, revert (each test section notes its
   mutations). **Commit everything BEFORE mutation-testing**: the natural revert is
   `git checkout -- <file>`, which wipes any uncommitted work in that file along with the
   mutation — a mutation round on a file carrying uncommitted changes has destroyed them once
   already. Git cannot help here: it cannot tell the mutation from your work (both are just
   unstaged changes to that file), so `stash` is no safer than `checkout` — stashing first
   mutation-tests code without the change you are validating, stashing after bundles the two
   back together. Revert with the exact INVERSE edit instead, leaving git out of it;
   committing first is then the fallback, not the mechanism. (Nor does `&&`-chaining the
   revert save you — it only guards the round where the mutation step itself failed.) There
   is no bare `python`/`python3` in a dev box — `uv run python`. Profiles live in
   `conftest.py`: CI (`CI` env) is derandomized so `check` never
   flakes; local runs stay randomized; `explore` (`FOLDYARD_HYPOTHESIS_PROFILE=explore`, which
   wins over `CI`) is the weekly randomised hunt in [CI](./ci.md). A test that sets its own
   `max_examples` wraps it in `pbt.scaled_examples(n)`, or `explore` leaves it at its everyday
   budget. Keep strategies as plain data (hypothesis shrinks data, not closures).
2. **Engine verbs (golden tests)** — mock the engine; assert the exact `docker`/`podman`
   command *sequences* foldyard emits. No real engine.
3. **Live smoke (e2e)** — drive the REAL machinery on a real engine, opt-in (`FOLDYARD_E2E=1`):
   - `tests/test_e2e.py`: the real CLI (`foldyard up`/`ps`/`down`) against the
     [example consumer](../../example/), asserting the api serves a DB-backed request
     (`just test -k e2e`). Isolated by the example's own `fyex` project.
   - `tests/test_proxy_e2e.py`: a real `mitmdump` + the `egress_proxy` addon + a fake minter + a
     CA-trusting `requests` client — host-side header rewrite, network log, 401 re-issue.
   - `tests/test_proxy_reload_e2e.py`: the same real `mitmdump` driven by its live file — a
     download survives a settings change on the same process, a tunnel whose host leaves
     `passthrough` is closed, an untouched one stays open.
   - `tests/test_proxy_box_e2e.py`: the **proxy plugin driving a REAL dev-box container** over
     the socket — runs in an adapted topology so it works inside a dev box; see
     [docs/nested-virt.md](../nested-virt.md). The proxy e2es:
     `just test-proxy-e2e` (pulls the `e2e` group), which also runs `test_proxy_wall_e2e.py`,
     `test_capture_box_e2e.py` and `test_gcp_metadata_box_e2e.py`.
4. **The host tier** — `machine ensure|stop|recreate`, `box up|build`, `host`, `mode set`,
   `verify`'s real VM boundary, the walls. These deliberately **refuse to run inside a dev box**
   (`config.in_box()` guards — the box must not manage its own VM or escalate its posture), and
   creating a machine/box from inside the live box would collide with it. They have a REAL VM
   in CI — the `lima-host-e2e` job ([CI](./ci.md)), and the same VM inside WSL2 on a Windows runner
   (`wsl2-host-e2e`) — and live as `tests/test_*_e2e.py` modules over the
   shared substrate `tests/e2e_host.py` (gated: `FOLDYARD_E2E=1`, not in a box, `limactl` on
   PATH; each module takes a throwaway example copy, leaves the VM running and un-walled):
   - `test_verify_e2e.py` — ALL PASS on the boundary foldyard builds, **FAIL against a VM
     mounting the operator's whole home**, PASS again once the mount is gone (the negative
     [docs/archive/verify-false-pass.md](../archive/verify-false-pass.md) owed). Never weaken this one.
   - `test_probes_e2e.py` — the read-only engine probes in-process (`devmode.workspaces` /
     `up_worktrees` / `_stack_mounts` / `_stack_shadow_check`, `stack.disk_headroom`,
     `machine.state/socket/responsive` with the moved-socket invariant, `reconcile.scopes()`),
     plus `fy state` and `fy doctor` — the output that drifts between podman versions, which the
     hermetic unit suite cannot see.
   - `test_machine_e2e.py` — ensure idempotent; stop stops the supervisor (heartbeat stale) and
     keeps the VM; a SIGKILLed hypervisor recovered by ensure (the flag-is-not-liveness item in [gotchas](./gotchas.md)); recreate. Its copy lives UNDER THE HOST HOME (`~/fy-e2e/machine/`, left in place)
     and the VM is recreated from it first, so every restart runs with a repo mounted at
     `/home/<user>/…` — the realistic Linux layout the open home-mount finding in
     [docs/linux-support.md](../linux-support.md) needed exercised.
   - `test_mount_visibility_e2e.py` — the git heal's premise on THIS host's mount (virtiofs on
     macOS, 9p on the Linux/WSL2 runners): a file the VM renames into place reaches the host
     whole, never briefly missing, never older than one already seen (ADR-0021, 2026-09-27).
   - `test_registrylogins_e2e.py` — a real client's headers on a logging socket (an auth file sends
     every login; clamped, none, default paths included), then a real `fy build` watched by a
     rootless container bind-mounting the guest's `/var/tmp`, as a box could, with a positive control.
   - `test_mounts_e2e.py` — ADR-0033's to-box against ROOT in the guest: a test-only boot script
     (own marker, always exits 0) remounts it rw and tries every kind of write; all refused and the
     host's seeded file unchanged. The red-first control records it `writable: true` (started with
     `limactl start`, as `fy up` would re-record it read-only): the writes land and `fy verify`
     FAILs its to-box row. First, its FRESHNESS: after the guest has read a to-box file, an append
     and a rename-over from your computer each reach it within 5 s, and on 9p the to-box carries
     foldyard's pin (`9p.cache = mmap`; Lima's default `fscache` never revalidates, and left every
     round stale on CI's 9p hosts before the pin).
   - `test_reclaim_e2e.py` — `fy reclaim` on a real store: a removed worktree's tagged images
     (both provider spellings) swept, the main image + base images kept, the next `up` still
     `Using cache` (the three reclaim properties in [gotchas](./gotchas.md), live).
   - `test_worktree_e2e.py` — `fy worktree add` (registered, own branch, clean tree), its stack
     up beside main's, `remove`: containers + volumes gone, the bound-out transcript ARCHIVED
     before the tree is deleted, main untouched, the branch kept, local state dropped.
   - `test_monitor_e2e.py` — `[machine] monitor = "observe"` via `fy up`: the release delivered
     and installed, the packaged policy LOADED on the shipped kernel (an invalid one stops
     Tetragon outright), and the box's uid unable to stop it, reach its socket, read its log,
     write its policy dir or reach its health port; a junk inbox delivery rejected beside the
     running install; a container's file read reaching `fy monitor` attributed to that
     container's id through the real relay → pull → join; `off` removes it at the next boot (the
     teardown, unasserted — and skipped in CI, where the module runs last in its shard).
   - `test_wall_e2e.py` — `[machine] firewall` + `host_firewall` via `fy up`: the fixture IS the
     operator — the first `fy up` refused (nothing installed), then the `sudo` lines `fy machine
     host-firewall` printed run verbatim, then `fy up` passes; the host table on the VM's own slice,
     the unit active, direct guest egress refused, DNS resolving, the proxy the way out, the api
     still served, and the stale-provisioning refusal.
   - `test_host_daemons_e2e.py` — the supervisor with the zero-secret rig
     ([docs/testing-modes.md](../testing-modes.md)): mode on → fake minter up + the overlay
     re-rendered, blocked-daemons empty; mode off.
   - `test_box_e2e.py` — `fy box up` on the VM (recreated to mount the module's copy), in-box
     `fy ps` over `CONTAINER_HOST`, in-box `fy verify` ALL PASS, `box down`.
   - `test_forwarder_e2e.py` — `[machine] attribution = "record"` on a real walled VM: the forwarder's
     boot script recorded under its own marker, `fy up` refusing unless the guest reports it serving,
     then through the real supervisor-run mitmdump (ADR-0034).
   - `test_basevm_e2e.py` — `fy machine base bake`, then a project VM created as a clone of it that
     boots, provisions and runs containers (which base it drives: the sharding notes in [CI](./ci.md)).
   On a Lima host these run against the example's own VM (creating, restarting, recreating it),
   never a consumer's. What the runner cannot reach — nested virtualisation for the gVisor
   posture — stays the recipe in [docs/nested-virt.md](../nested-virt.md). That page's M3+ rig
   (`just nested-up`, `just nested-test`, tests/tools/nested.py) also runs this tier on arm64 Linux
   in a VM of its own, so it can't collide with the `foldyard-example` on your computer. **On the M3
   Max and macOS 27.0.1 where this was measured, the L1 kernel needs the GIC traps the page names, or
   no L2 boots**, while `/dev/kvm` and `KVM_CREATE_VM` still look fine. Why a box doesn't get `/dev/kvm` itself:
   [ADR-0035](../adrs/0035-the-box-asks-for-a-test-run.md).
   From a Claude Code cloud session (root, no `/dev/kvm`, an egress proxy that re-signs TLS) the
   tier runs under QEMU's TCG, slowly: [`.claude/skills/cloud-host/`](../../.claude/skills/cloud-host/SKILL.md)
   sets that host up in one script and lists what breaks there and why.

**In-box validation you CAN do:** `fy verify`, `fy ps/down/up/logs`, the e2e tiers above.
**CANNOT from inside the box:** `fy host restart` (real daemons), `fy mode <set>` (authoritative
state), `fy tui`, real credentialed modes, and box creation (`fy box up`/`build` would
recreate the running box; `fy box ps`/`shell`/`down` are safe).

## Golden tests must never see a live engine socket

Tier-2 fixtures use a REAL consumer's project prefix, and inside the dev box
`unix:///var/run/docker.sock` is the machine's LIVE engine — reachable by the box's installed
podman-remote via the stack-exported `CONTAINER_HOST`. In 2026-08 a single engine probe that
escaped the reconcile tests' mocks `podman rm -f`'d the box's entire running dev stack while
every test passed green. Invariants: fixture `DOCKER_HOST` values must be dead paths
(`unix:///nonexistent/…`, never a plausible socket), and a fixture that lets a verb execute
(`up`/`down`/`reconcile_posture`) must stub `stack.subprocess.run` itself — stubbing only a
higher layer (e.g. `devmode.run_stream`) leaves the direct-engine helpers live. When touching
stack verbs, canary-check from the box with the active provider's signature. For Docker Compose:
`docker run -d --name fy-canary --label com.docker.compose.project=<project> --label
com.docker.compose.config-hash=fy-canary <any-image> …`. For podman-compose: `podman run -d
--name fy-canary --label com.docker.compose.project=<project> --label
io.podman.compose.project=<project> <any-image> …`. Run the suite once in each provider direction,
confirm the matching canary survives each run, then remove it.

**The suite is hermetic by construction, not by convention** (`tests/conftest.py`, the "hermetic
subprocess" section; pinned by `tests/test_hermetic.py`). Every non-e2e test runs on a PATH
scrubbed to `git`/`cksum`/the shell/the interpreter — a host tool named bare is *not found*, as on
a CI runner, so the code under test takes its "not installed" branch rather than the live
machine's — and `subprocess.run`/`Popen` refuse to execute anything else reached by absolute path
or a caller's own `env["PATH"]`, failing the test by name with an exception no `except Exception`
can swallow. The shell is allowlisted for `bash -n`, not for a program (`bash -c …`/`sh script`
carry one past argv[0] — refused like `shell=True`). A test that means to run a host binary
marks it (`@pytest.mark.spawns("/abs/path")`, or `spawns("bash")` to run a program under that
shell); one that needs a real tool is an e2e test (`tests/*_e2e.py`, exempt). Before this, `set_mode`
round trips were running `limactl list` + `podman ps` against the live machine, and the
supervisor's blocked-daemon push reached a real Notification Center — green in CI only because
those binaries are absent there.

**`just census` is the report over that gate, not a gate itself** (`tests/tools/census.py`, a
`-p` plugin the recipe loads): every process the suite spawns, binary × test, aggregated across
the xdist workers — what the allowlist still lets through (`git` from the git-shim tests, `cksum`
from the port-offset parity test, the shells only from the guard's own tests) and, with
`FOLDYARD_E2E=1 just census tests/test_*_e2e.py` on a Lima host, what the live tiers reach.
Arguments pass to pytest (paths, not a quoted `-k` — `just` splits on whitespace);
`CENSUS_TESTS=1` lists the tests under each binary.
