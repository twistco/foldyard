# ADR-0028 — foldyard never elevates on the host: it renders, the operator applies, foldyard probes

- **Status:** Accepted (2026-09-18). Implemented on the same branch for the one host-side
  escalation the package had — the host wall (`[machine].host_wall`): `hostwall.install` /
  `remove` (a `sudo nft -f -` on every `fy up`) are gone; `fy machine host-wall` renders and
  prints, `fy up` probes.
- **Sources:** [configuration.md](../configuration.md) (`[machine].host_wall`),
  [lima-wall-machine-integration.md §3](../lima-wall-machine-integration.md#3-host-side-enforcement--machinehost_wall--true-linux),
  the 2026-09-18 runner experiment recorded there. Related:
  [0009](./0009-monitoring-cooperative-enforcement-locked.md) (the wall is enforcement, not
  cooperation), [0022](./0022-host-runs-the-adopted-config.md) and
  [0023](./0023-no-host-executed-code-from-the-repo-mount.md) (what the host may take from the
  checkout: nothing executable, nothing live), [0027](./0027-always-a-vm-native-backend-retired.md).

## Context

The host wall — nftables on the host matching the VM's own traffic by cgroup — is the one
foldyard feature that needs root on the host. Until this ADR it took it the usual way: `fy up`
ran `sudo nft -f -` with the ruleset on stdin, and the operator got sudo's password prompt.
Three things were wrong with that, in ascending order of importance.

**1. It prompted on every `fy up`.** The ruleset named Lima's forwarded SSH port and the
hostagent's loopback listener ports, all allocated per boot, and the host's resolvers, so it
was re-rendered and re-loaded every time. The documented remedy was a passwordless sudoers rule
for `nft` — "not yet written up as a recipe".

**2. The prompt did not say what it was for.** One line preceded it — `(root: sudo nft)` — not
the command, not that a password would be asked, and not the ruleset, which went over stdin and
could not be seen. `fy machine rm` prompted with no line at all. A missing `sudo` raised a
traceback out of machine start (a review found this the same day).

**3. It taught the wrong habit.** A tool that asks for elevation and shows nothing trains its
users to type their password into whatever asks. foldyard's whole premise is the opposite —
that the host keeps its posture because nothing in the checkout or the box can widen it
(ADR-0022, ADR-0023). An operator conditioned to approve blind is a wider channel than any the
config could open. As the maintainer put it: *"I don't like tools that just prompt for elevation
and I don't know what they are doing — it's teaching people the wrong habit of just accepting
whatever messing with their system."*

Two facts about the mechanism decide what the alternative can look like. They were established
on a GitHub `ubuntu-24.04` runner on 2026-09-18 (a throwaway workflow; the results are in
[lima-wall-machine-integration.md §3](../lima-wall-machine-integration.md#3-host-side-enforcement--machinehost_wall--true-linux)):

- **The ruleset can be boot-stable.** Loopback flows from the VM can be allowed OUT under a
  conntrack mark and judged on the INPUT hook by the *listening* socket's cgroup
  (`socket cgroupv2` resolves the listener for a SYN) — so the hostagent's and QEMU's per-boot
  ports never need naming — and DNS can be `dport 53` to any resolver. What is left depends only
  on the VM name, its cgroup and the project's daemon band: the same text every boot, so
  "apply once" is a coherent thing to ask of an operator.
- **The table cannot be read back, and a table that is there may be inert.** `nft list` needs
  root — there is no unprivileged read of nf_tables. Worse, `socket cgroupv2` compiles the path
  to a cgroup **ID** at load time; a cgroup destroyed and recreated (a host reboot, a stopped
  slice) gets a new ID that the loaded rule silently no longer matches. On the runner the
  recreated slice's probe reached the internet with the table still loaded: **fail-open**. So
  neither "foldyard loaded it" nor "the table exists" is knowledge; only enforcement observed
  from inside the matched cgroup is.

## Decision

**foldyard never elevates on the host.** Where a feature needs root, foldyard **renders** the
exact artefact and the exact commands, the **operator applies** them with the content in front of
them, and foldyard **probes** the resulting capability — it never reads privileged state it could
not have written, and never runs a command the operator has not seen. Concretely for the host
wall:

- **The VM runs under a persistent user slice foldyard owns** (`fy-machine-<vm>.slice`, a unit
  under `~/.config/systemd/user`, `WantedBy=default.target`, started before the VM). A slice
  survives being emptied, so its cgroup ID — the one the operator's table binds to — is the same
  across every VM restart. foldyard creates and enables it (no root); it never stops it.
- **`fy machine host-wall` renders the root-side files into the project's state dir and prints
  them in full** — the nftables table and a system unit `fy-host-wall-<vm>.service`
  (`After=` + `BindsTo=user@<uid>.service`, `WantedBy=user@<uid>.service`, `ExecStart=nft -f
  /etc/foldyard/host-wall-<vm>.nft`, `ExecStop=nft delete table …`, no shell, no template) — and
  then the four commands that install them: two `sudo install`, a `daemon-reload`, one
  `enable --now`. `--uninstall` prints the three that remove them. foldyard runs none of them.
  The installed copies are root-owned, so nothing running as the operator can change what root
  loads afterwards. The unit is bound to the user manager's lifetime, which is the slice's: the
  table loads once the manager is up (its default target wants the slice) and is dropped when it
  stops — so it comes back, bound to the right ID, on every login and boot.
- **Every `fy up` (and `fy machine host-wall`, and doctor's `host wall` row) PROBES.** A child
  is run under the slice and asked to connect to a listener foldyard opened *outside* the slice
  on loopback (must be **refused** — the input hook's judgement), to TEST-NET-1 off-host (must
  be **refused** by the output hook; without the wall the SYN leaves and times out; a host with
  no route reports "unreachable", which proves nothing and refuses nothing), and to a listener
  on the project's band (must **connect** — the staleness half). Refusals are TCP resets, so
  every verdict is immediate. Not enforcing ⇒ `fy up` refuses, prints which half failed and
  where the install steps are. This is what turns the cgroup-ID fail-open into fail-closed.
- **The install is an install, not part of the VM's lifecycle.** `fy machine stop` and `rm`
  leave it alone (`rm` says so and names `--uninstall`); the operator re-runs the steps after a
  change to the project's band, and once per host if the files were removed.

The principle generalises beyond the wall. It is the host-side counterpart of ADR-0023: that ADR
keeps the checkout from executing on the host; this one keeps *foldyard itself* from acting as
root there. Anything new that needs root on the host takes this shape — a rendered artefact, a
printed command, a probe — or does not ship.

## Alternatives considered

- **A passwordless sudoers rule for `nft`** (the previous documented answer). Silent, but it is
  the blind approval made permanent, for every invocation of `nft` by anything running as the
  operator — including the box's neighbours. Rejected.
- **Keep `--apply` beside the printed steps** for operators who would rather have foldyard run
  them. Two paths to keep fail-closed, and the convenient one is the blind one. Rejected: pure.
- **Detect the table with `sudo -n nft list`** (a read-only sudoers rule). Still a sudoers rule,
  and presence is not enforcement (the ID finding). The probe is stronger and needs nothing.
- **Persist the table via `/etc/nftables.conf`.** Loaded at boot, before any user manager exists,
  so the slice path fails to resolve and the load errors. The user-manager-bound unit is the
  ordering that works.
- **A hash of the VM name for the conntrack mark.** Two projects' tables both hook INPUT and
  each judges only flows carrying *its* mark; a collision would have project A rejecting B's
  own plumbing, and nothing could detect or explain it. The mark is the project's proxy band
  base under foldyard's byte — unique per project on the host by the allocator's construction.

## Consequences

- No `sudo` in the package's host-side code; the hermetic suite has no host-root path to guard,
  and the wall e2e's fixture *is* the operator — it runs the printed commands verbatim, which
  is the whole contract under test.
- The operator sees, once, exactly what root will do on their machine and where it lives.
- The probe runs on every `fy up` (a `systemd-run --scope` and three connects, immediate). A
  host without a `systemd --user` manager cannot host the wall (it never could — the scope
  needed one); `loginctl enable-linger` is the answer for a session without one.
- Residual: the probe checks what it probes. A table that still refuses the out-of-slice
  listener and still admits the band passes, whatever else it says; a band change is caught, a
  render change that touches neither half is not until the operator re-runs the steps.
  `fy machine host-wall` always prints the current render.
- WSL2 is unchanged: the stock kernel lacks `CONFIG_NFT_SOCKET`, preflight refuses `host_wall`
  there, and the operator's own `nft -f` would say why.
