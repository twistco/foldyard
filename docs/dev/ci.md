# CI

`.github/workflows/foldyard.yml` + `foldyard-e2e.yml`.

Tiers 1–3 and the host tier all run on GitHub-hosted runners — Linux, and the host tier once more
inside WSL2 on a Windows runner. `foldyard.yml` is the fast gate on every push; `foldyard-e2e.yml`
holds the advisory live tiers, opt-in (`main`, a `[run-e2e]` commit message, or a dispatch). Every
job uses standard runners, which GitHub bills nothing for on a public repository. (Beside
them, `claude-pr-review.yml` posts an advisory Claude review on each same-repo PR, from the
base branch's `.claude/commands/pr-review.md`, the prompt `/pr-review` runs locally. A flaw it
finds already on main is filed as a private vulnerability report by
`.github/scripts/report-private-findings.sh`, never posted.) Five jobs, the two host tiers
sharded (below):

| job | what | engine |
| --- | --- | --- |
| `check` | `just check` — ruff + pyright + ty + the unit/golden/TUI suite. typecheck installs the `e2e` group so the opt-in proxy/box e2e files (which import `cryptography`/`requests`) resolve | none |
| `live-e2e` | the in-box topology: all the live e2es (`-k e2e`: the example stack up→serve→down, the in-process proxy e2e, AND the box e2e) — run **inside a docker-CLI container** that mirrors the dev box (see below) | runner Docker |
| `lima-host-e2e` | the HOST topology: `ubuntu-24.04` as a real Linux host running foldyard's default `lima` backend — `machine ensure` boots a QEMU/KVM VM on the runner, then `tests/test_e2e.py` drives the real `foldyard up` / worktree lifecycle through the config-adopt gate, the supervisor and compose, exactly as on an operator's machine. Two shards, `L1`/`L2` | Lima VM (podman in the guest, over the forwarded socket) |
| `wsl2-host-e2e` | the same host tier on `windows-2025`: an Ubuntu 24.04 distro under WSL2 ([Vampire/setup-wsl](https://github.com/Vampire/setup-wsl)) is the host, and the `lima` backend boots the QEMU/KVM VM INSIDE it — a second level of nesting the hosted Windows runners expose. Same modules, four shards `W1`–`W4`; `test_wall_e2e.py` skips itself there (below) | Lima VM inside the WSL2 distro |
| `e2e` | the one status for `foldyard-e2e.yml`: `needs:` the three live jobs above and fails unless each succeeded (a matrix job's result is its worst shard's). `check` gates separately, in `foldyard.yml` | none |

**The weekly randomised property run (`property-explore.yml`).** Every job above loads the `ci`
hypothesis profile, which replays the same examples every run and so never finds a new
counterexample; local randomised runs at about 3,000 examples found two real bugs that CI's replayed
examples had passed (PR #138, 2026-10). So once a week (Tuesdays 03:17 UTC, kept weekly to go easy
on GitHub's runners) one job runs the property tier — every test module that imports hypothesis,
which `tests/test_property_explore.py` holds the workflow's `PROPERTY_MODULES` list to — under the
`explore` profile: randomised, `max_examples` 5000 (50× the default; each test's own budget scales
with it), no deadline, `-n auto`. The tier took 7.7 min on a hosted runner (5.4 on 4 local workers),
under the step's 22-minute cap; the hook-delivery property is most of it. The example database lives
at `.hypothesis/examples`, which `actions/cache` carries from run to run (each run saves under
`hypothesis-explore-<run_id>-<attempt>`, the next restores the newest by the prefix), so a
counterexample found one week is replayed first every week until it is fixed. GitHub evicts an entry
nobody has read for 7 days, which a weekly run only just beats; a late start may find it gone and
begin a fresh database, which loses nothing the issue below doesn't keep. A manual run (Actions →
property explore → Run workflow) takes a `max_examples` and a `dry_run`.

**A failure opens a public issue, because GitHub's own failure email does not reach the
operator.** A failed scheduled run emails whoever last edited the workflow's cron line, so a
reformatting PR would silently move the alert to its author. Instead, on any failure,
`tests/tools/property_failures.py` turns the JUnit report (or, without one, the log's tail) into a
summary — the run's URL, the failing tests, each falsifying example and its `@reproduce_failure`
line — and `.github/scripts/notify-property-failure.sh` comments it on the open issue labelled
`property-tests`, or opens one assigned to and @-mentioning `NOTIFY_LOGIN` (the workflow's
top-level `env`; change it there to send it elsewhere). Assignment and mention both email under
GitHub's default notification settings. Public is deliberate: the run's log already shows the same
examples. The summary is untrusted text (whatever a strategy generated): it reaches `gh` only as
`--body-file`, inside a code fence it cannot close, and no `run:` in the workflow expands an
expression. A `dry_run` prints the issue instead of posting it.

**Why `live-e2e` runs inside a container.** The box e2e (`test_proxy_box_e2e.py`) spawns a
*sibling* box and must discover its own network — so the test process itself has to be in a
container, exactly as in the dev box. The job's `docker run` reproduces the dev-box environment
precisely: **docker CLI only** (no podman ⇒ foldyard's `_engine()` picks docker ⇒ `docker compose`,
not the `podman machine` VM path that a bare runner would take); the **host socket mounted** +
`IN_DEVBOX=1` + `DOCKER_HOST` set (⇒ `in_box()` true ⇒ the launch gates — preflight's backend
check, the config-adopt gate — and `machine.ensure` all short-circuit; no `/dev/kvm` needed); and
the **repo mounted at its same host path** (so the box's `-v <CA>` mount, which the daemon resolves
on the host, points at a path that exists there — the test copies the CA under the repo for this).
A `docker compose` v2 plugin binary is dropped in for the example stack.

**Why a VM job works on a shared runner (2026-09-17).** Until then this page said podman-machine
/ Lima VMs were "flaky" on GitHub-hosted runners; the only citation was a *libvirt* permission
failure, and foldyard's lima backend does not use libvirt. `/dev/kvm` is present on the x86 Linux
runners (under-documented, but Lima's own CI boots QEMU VMs on `ubuntu-24.04` on every PR with the
recipe `modprobe kvm; chown $USER /dev/kvm` — group membership does not take effect there). A
10-attempt spike of the real `lima` backend on `ubuntu-24.04` went 10/10 with no retry wrapper,
with and without the walls, QEMU start → READY in 29–41 s and the whole attempt under 2.5 min;
[ADR-0027](../adrs/0027-always-a-vm-native-backend-retired.md) records the outcome, and PR #17
the measurements (its `docs/linux-support.md`; #34 later removed them from that page). The job
creates the VM once from a throwaway example copy before pytest (the example stack has no bind
mounts, so every test copy can drive the one VM). It bakes a session base VM first (`fy-ci-base`,
named by `FY_BASE_VM_NAME`/`FY_BASE_VM_STAMP` job-wide, retried once), so that VM and every one a
module recreates are clones: a template create runs the template's `dnf install podman` over the
Fedora mirrors, and one stalled mirror once left a WSL2 run's every module after it on a VM whose
boot never finished (2026-10-16). The cost: a template-created PROJECT VM (foldyard's provisioning
on a fresh template boot) is no longer exercised by the host tier. `test_basevm_e2e` checks that
session base instead of baking a second one (sharding, below). It exports the VM's socket as
`CONTAINER_HOST` — for
the tests' OWN engine calls; a preset `DOCKER_HOST` would make the CLI under test skip
`machine.ensure` (`stack._docker_host`: dev-box semantics) and bypass the very lifecycle the tier
drives. The tests' `foldyard up` then runs the real host path: machine ensure → adopt gate →
supervisor → compose. First catch of the tier: podman 4.9.3's `ps --format` (Ubuntu 24.04's
package) has no `{{.Label "k"}}`, so every probe built on it read "engine unreachable" on a
Linux host — now `{{json .Labels}}` (`devmode.ps_labels`).
Things a Linux runner needs that a macOS host does not: `qemu-img` (from `qemu-utils`, not implied
by `qemu-system-x86-core`), a `systemd --user` manager for anything scoped (`loginctl
enable-linger`), and Lima from the release tarball into `/usr/local`. **arm64 runners have no
KVM** (`ubuntu-24.04-arm`: no `/dev/kvm` before or after `modprobe kvm`, probed 2026-09-17), so
the job is x86-only. The `podman` backend (podman-machine) has not been tried on a runner; the
`lima` backend is the product path and the one tested. The nested-KVM rig recipe in
[docs/nested-virt.md](../nested-virt.md) remains for what a VM job cannot reach (the gVisor
posture under nested virtualisation).

**Why the same tier runs inside WSL2 on a Windows runner (2026-09-17).** WSL2's distro is a
Hyper-V guest, so a Lima/QEMU VM inside it is nested twice (Azure → runner VM → WSL2 utility VM →
QEMU). The hosted `windows-2025` runners allow it: with `[wsl2] nestedVirtualization=true` in
`%USERPROFILE%\.wslconfig` (written BEFORE the distro first starts — the file is read at
utility-VM boot; Windows 11 defaults it on, Windows Server 2025 needs it said) the distro has
`/dev/kvm` (`root:kvm 0660`, WSL2 kernel 6.18), and foldyard's unmodified `machine ensure`
boots the Fedora 44 guest to READY in 68 s (47 s on a restart) — Lullabot/sandbar#149 measured
the same on 2026-08-27. WSL2 is only ever the HOST here: a VM-less engine in the distro would be
bare Linux without the VM ([ADR-0027](../adrs/0027-always-a-vm-native-backend-retired.md)).
The job differs from the Linux one in plumbing, not product: job `env:` is Windows process env,
so the variables the in-distro steps read are forwarded by `WSLENV` (`GITHUB_WORKSPACE/up`
arrives path-translated) and state between steps goes through an in-distro env file, never
`GITHUB_ENV`; the action's default distro user is root, so the root work (packages, Lima, uv,
the `runner` user with `NOPASSWD` sudo + the `kvm` group + linger) runs first and the wsl-bash
wrapper is then regenerated for `runner` (a second `setup-wsl` step with `wsl-shell-user`);
the checkout is CLONED from the Windows drive onto the distro's ext4 (objects carry the
committed modes and LF endings — the 9p automount shows 0777 and the runner's git has
`core.autocrlf`; a local clone needs a global `safe.directory`, the automount is root-owned and
`upload-pack` runs as a child). Automount stays on because the wrapper reads each step's script
through `/mnt/<drive>`. The action caches the distro installer (372 MB): the first run's 5.5 min
install is 40 s after. **What WSL2 cannot do: the host firewall.** `[machine] host_firewall`
matches the VM by `socket cgroupv2`, and the stock WSL2 kernel has `# CONFIG_NFT_SOCKET is not
set` (both the 6.6 and 6.18 branches), so `nft` refuses the rule with ENOENT — the wall fails
closed, as designed. `test_wall_e2e.py` now probes the kernel for the expression in its gate
(one rule into a throwaway table, `sudo -n`) and skips; before that its fixture had already
re-provisioned the VM walled, and the error left the worktree module refusing the stale
provisioning — 9 errors from one kernel option. The VM firewall (`[machine] firewall`) is
unaffected (it runs in the guest). Everything else passed unchanged — 30 passed, 6 skipped; the
tier is ~3.5× slower than on Linux (unsharded, the test step 43 min vs 12; `test_box_e2e` 8 min,
`test_machine_e2e` 9; the job 47–62 min — what the sharding below fixed). Facts about the
runner worth knowing: 16 GB / 4 vCPU, of which WSL2 takes half the memory; the distro's root
is a sparse 1 TB vhdx on `C:` with ~30 GB actually free; the `ubuntu-24.04-arm` finding carries
over — Windows-on-ARM boots the distro at EL1, no KVM, so this is x86-only too.

**Why the host tiers are sharded (2026-10-22).** Unsharded, `wsl2-host-e2e` took 47–62 min and
`lima-host-e2e` 16–18. Timing the WSL2 tier found no hotspot to fix: three levels of virtualisation
make everything 3–6× slower than on Linux (a process start ~4×, `podman run` ~6×, small files on
the shared folder ~12×), and the minutes go on the cold `fy up` after each VM recreate (65–154 s,
~24 s on Linux), with runners varying ±50%. Setup is only ~6 min of a job and the VM boots are
fine. Every module drives the one VM, so tests can't run in parallel INSIDE a job — the modules
are split across jobs instead: each host job is a matrix whose shards (`matrix.shard.modules`)
each pay the full setup on their own runner (distro, Lima, the session base bake, the first VM),
then run their modules in order — four on WSL2 (`W1`–`W4`), two on Linux (`L1`/`L2`, each the union
of two WSL2 shards, so a module's neighbours are the same on both hosts). The split is by module
times averaged over several runs, and each shard starts with a module that recreates the VM (`W3`
starts from the VM the setup just created). A module's time depends on its place: the first
`fy up` after a recreate is the cold one (image builds and pulls), so whichever module follows a
recreate carries it. Judge a rebalance on several runs' tables, not one: the apt step alone has
ranged from under a minute to 13 min on the same install (once 12 on Linux). (Pointing the
distro's apt at `azure.archive.ubuntu.com`, the Linux runners' mirror, made it slower, 2–16 min,
not faster. What the WSL2 job does instead is cache the debs together with the package lists they
were resolved against, keyed by month (`actions/cache`, restored to `RUNNER_TEMP`), and install
offline from them. On a miss it installs online as before and saves the result; if the offline
install fails it installs online too, but a cache key can't be overwritten, so that cache stays
until the month's key changes — bump the key's `v1` to replace it sooner, and when the package
list changes.)

**4 vCPUs for the CI VM was tried and reverted (2026-10-22).** `MACHINE_CPUS=4` (over the example's
`cpus = 2`) made no Linux module faster, and on WSL2 it made the VM-heavy ones slower and erratic.
Six unsharded 2-vCPU runs plus the 2-vCPU sharded spike had `test_machine_e2e` at 6.4–9.2 min
and `test_verify_e2e` at 3.6–4.2. Six sharded 4-vCPU runs had them at 7.5–14.7 and 4.2–10, the
box module at 6–11 against 4.3–6.5, and in-box `fy verify`'s 12 s dry-run push to the public
origin timing out in three of eight `test_box_e2e` attempts ("push refusal UNPROVEN (timed
out)"), which never happened at 2. The likely mechanism is a nested guest given every vCPU WSL2's
utility VM has, which leaves QEMU's own I/O thread (Lima's user-mode network included) and the
host processes to fight it. The jobs now set `MACHINE_CPUS=2` explicitly. The create step asserts
`limactl list`'s CPU count and prints the guest's `nproc`, and on WSL2 the variable is forwarded by
`WSLENV` like every job variable (the guard test holds `WSLENV` to the job's `env:` keys). Trying
another size is a one-line change; measure it over several runs. The verify bound stays where it
is: verify is never weakened to suit CI. The setup's base bake caps its first attempt at 6 min
(good bakes take 1–4.5): a stalled Fedora mirror otherwise holds Lima's boot wait for its full 10
min before the retry.

`tests/test_e2e_workflow.py`, in the fast suite, holds the matrix to the modules: every host-tier
module (test_e2e.py and each `test_*_e2e.py` that imports `e2e_host`) in exactly one shard per
job, so **a new host-tier module must be added to a shard in both jobs** or `check` goes red. Two
smaller savings ride along:
- the guest monitor's teardown skips its monitor-off reboot when nothing runs after it: the shard
  hands its module list to the substrate as `FY_E2E_MODULES` (the same string pytest gets), and
  `e2e_host.runs_last` is true only for its final entry. Unset (a local run), the VM is restored as
  before;
- `test_basevm_e2e` checks the job's session base (`fy-ci-base`) and clones from it instead of
  baking a second base. **The trade-off:** in CI the module no longer proves a bake from scratch on
  its own — the job's setup step, the same `fy machine base bake`, is that proof — and `rm` is
  proven on a never-booted stamped stand-in so the session's base survives for the modules after
  it. Without `FY_BASE_VM_NAME`/`FY_BASE_VM_STAMP` (a local run) it bakes, clones and removes its
  own, as before.

Each shard writes a per-module timing table to its run page (`--junitxml` →
`tests/tools/e2e_timings.py` → `$GITHUB_STEP_SUMMARY`; on WSL2 the distro appends to the runner's
file through `WSLENV`'s `GITHUB_STEP_SUMMARY/p`), the shards time out at 45 (WSL2) / 30 (Linux)
minutes, and the `e2e` job is the single status to check.
