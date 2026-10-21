# ADR-0033 — An inbox and an outbox beside the checkout: two foldyard-owned mounts, one per direction

- **Status:** Proposed (2026-10-21). Scope only — nothing is built. Open questions at the end.
- **Sources:** the current mount set (`machine.py`, `machine_backend.py`, `box.py`, `verify.py`
  as cited below); the transcript bind-out (`transcripts.py`, `plugins/claude.py`,
  `plugins/codex.py`); the guest monitor's hooks (`monitorhooks.py`) and the stopgap file sink
  being built on the `monitor-file-sink` branch; Lima 2.2.0 and QEMU sources as cited in
  [§ Read-only enforcement](#read-only-enforcement-by-backend). Related:
  [0001](./0001-rootless-podman-vm-isolation-boundary.md) (repo-only mounts),
  [0004](./0004-one-machine-worktrees-as-compose-projects.md) (worktrees at equal trust),
  [0022](./0022-host-runs-the-adopted-config.md) and
  [0023](./0023-no-host-executed-code-from-the-repo-mount.md) (what the host may take from the
  mount), [0025](./0025-gvisor-machine-posture-and-socket-narrowing.md) (gVisor),
  [0032](./0032-a-root-collector-in-the-guest.md) (the guest monitor).

## Context

The VM mounts exactly two host paths, both writable, both at their host path in the guest: the
main checkout and the worktrees root (`machine._volumes`, `src/foldyard/machine.py:45`; Lima
renders them `"writable": true` in `_set_expr`, `src/foldyard/machine_backend.py:667-689`; the
podman backend passes `--volume src:dst`, `machine_backend.py:353-354`). `verify`'s mount audit
exempts exactly those two mountpoints (`machine.guest_mounts`, `machine.py:51`, used at
`src/foldyard/verify.py:545`) and fails on any other line whose mountpoint matches the operator's
home or a foreign host root (`verify.py:41-56`, `:252-265`).

Every byte that crosses between the host and the box therefore goes through the checkout, in
both directions:

| direction | what | where today | problem |
| --- | --- | --- | --- |
| box → host | Claude transcripts | `<checkout>/<here>/.devbox-claude/projects`, bound to `~/.claude/projects` in the box (`box.py:1391-1399`, `plugins/claude.py:162-165`) | grep noise in the checkout; `git clean -fdx` and `worktree remove` take it; box-written content inside a tree the host walks |
| box → host | Codex sessions | `<checkout>/<here>/.devbox-codex/sessions` (`box.py:1400`, `plugins/codex.py:194-197`) | same |
| host → box | monitor events (planned file sink) | the checkout, written with no-follow opens + an HMAC (the stopgap) | the box can delete, truncate or replace the file; the HMAC detects tampering, cannot prevent it |
| host → box | posture mirror, proxy CA, staged wheel + `server.py` | `.dev-mode.json` (`devmode.py:227`), `.devbox-ca/` (`plugins/proxy.py:161-184`), `.devbox-foldyard/` | every host write into the mount needs `mountwrite`'s no-follow walk, and the box can still swap what it reads |

Unix permissions can't make one directory inside a writable mount read-only to the box: root in
the box is the VM user (rootless podman), the VM user owns every file on the share (Lima maps the
host uid), and an owner can `chmod`. Lima's `writable` flag is per mount. So a read-only area
needs a mount of its own.

## Decision (proposed)

**Two foldyard-owned host directories, each its own VM mount, one per direction.**

| | outbox | inbox |
| --- | --- | --- |
| host path | `<state dir>/outbox/` (`~/.foldyard/<project>/outbox/`) | `<state dir>/inbox/` |
| VM mount | writable, host path == guest path (as today) | **read-only** (`writable: false`), host path == guest path — Lima `vz`/virtiofs and `qemu`/9p only (below) |
| box mount | its own slot only, rw, at the agent's native path | whole inbox `:ro` at a fixed path (`/run/foldyard/inbox`) |
| written by | the box | the host only |
| read by | the host — as **untrusted** input | the box (and anything in the VM) |
| first tenant | Claude transcripts, Codex sessions | the monitor's event file sink |
| what it guards against | grep noise; transcripts lost to `git clean` / `worktree remove` | a box that tampers with evidence or plants symlinks to redirect host writes — refused by the hypervisor/host share server, not by politeness |

- **Layout mirrors the posture dirs** (`config.posture_dir`, `config.py:1905`):
  `outbox/main/{claude,codex}/…` and `outbox/worktrees/<wt>/{claude,codex}/…`, so a worktree
  literally named `main` can't collide. The inbox is project-level with per-worktree slots where
  a tenant needs one: `inbox/monitor/…`, `inbox/main/…`, `inbox/worktrees/<wt>/…`.
- **Per-worktree slots are organisation, not isolation.** Any box holds the engine socket and
  can start a sibling container mounting the whole outbox or inbox VM path. That is within
  [0004](./0004-one-machine-worktrees-as-compose-projects.md)'s equal-trust tier (a box can
  already edit every worktree), but it means **the inbox must hold only what every box of the
  project may see**, and nothing secret ever: no HMAC key, no `host.env` value, no token.
- **The box's `:ro` is defence in depth, not the boundary.** The boundary is the VM mount being
  read-only on the host side (next section). The box isn't `--privileged`, so it can't remount
  its `:ro` bind, but over the socket it can create a container with the same VM path bound rw —
  which then meets the VM mount's own read-only flag, and below that the host's.
- **The state dir stays private.** Only the two subdirectories are mounted, never
  `~/.foldyard/<project>/` (which holds `host.env`, `monitor-relay.key`, the allow-store). The
  audit's exact-mountpoint exemption (below) is what catches a regression that mounts the parent.

### Read-only enforcement by backend

The question that decides whether the inbox is worth a mount: can **root in the guest** (a
container escape that reaches VM root — the VM user itself has no root after boot, `guest-boot.sh`
narrows its sudo to `shutdown`) write through the read-only mount? Only enforcement outside the
guest kernel counts; a guest `ro` mount option is one `mount -o remount,rw` away for guest root.

Read from source (Lima v2.2.0, the version CI pins in `foldyard-e2e.yml`; sshocker v0.3.11;
QEMU master; vfkit, krunkit; podman v5.0–v6.1), not yet run live:

| backend / path | how `writable: false` reaches the hypervisor | enforced outside the guest? | confidence |
| --- | --- | --- | --- |
| **Lima `vz` + virtiofs** (macOS default) | `vz.NewSharedDirectory(location, !writable)` — [`pkg/driver/vz/vm_darwin.go#L674`](https://github.com/lima-vm/lima/blob/v2.2.0/pkg/driver/vz/vm_darwin.go#L674), `#L704`; Apple: `readOnly` — "exposed as read-only to the guest" ([`VZSharedDirectory`](https://developer.apple.com/documentation/virtualization/vzshareddirectory/init(url:readonly:))) | **yes** — the flag is in the host's device config; the virtiofs server is in the host's Virtualization process | medium-high (a documented contract; no published guest-root test) |
| **Lima `qemu` + 9p** (Linux and WSL2; 9p is the 2.2.0 default for qemu, [`qemu_driver.go#L239-L253`](https://github.com/lima-vm/lima/blob/v2.2.0/pkg/driver/qemu/qemu_driver.go#L239-L253)) | `-virtfs …,readonly=on` — [`pkg/driver/qemu/qemu.go#L990-L998`](https://github.com/lima-vm/lima/blob/v2.2.0/pkg/driver/qemu/qemu.go#L990-L998) | **yes** — QEMU's 9p server returns `EROFS` for every non-read op and for write/append/trunc opens ([`hw/9pfs/9p.c`](https://github.com/qemu/qemu/blob/8967b2fe1b2aaae3c29e207d85fab901d580a2dc/hw/9pfs/9p.c#L4360) `is_read_only_op`, `#L2186-L2191`) | high |
| Lima `qemu` + virtiofs (Linux, opt-in) | none: virtiofsd started without `--readonly` ("not supported … yet", [`qemu.go#L1000-L1001`](https://github.com/lima-vm/lima/blob/v2.2.0/pkg/driver/qemu/qemu.go#L1000-L1001)) | **no** — guest `ro` only | high |
| Lima `krunkit` | none ([`krunkit_darwin_arm64.go#L98`](https://github.com/lima-vm/lima/blob/v2.2.0/pkg/driver/krunkit/krunkit_darwin_arm64.go#L98)) | **no** | high |
| Lima reverse-sshfs (old instances / explicit) | host `sftp-server -R` ([sshocker `reversesshfs.go#L213-L216`](https://github.com/lima-vm/sshocker/blob/v0.3.11/pkg/reversesshfs/reversesshfs.go#L213-L216)) | writes yes, but `-d` is a chdir not a chroot: guest root can **read** the rest of the home ([Lima's own warning](https://github.com/lima-vm/lima/blob/v2.2.0/website/content/en/docs/config/mount.md#L62-L64)) | high — and disqualifying for every mount, not just this one |
| podman `applehv` / `libkrun` (macOS) | `:ro` parsed, then dropped: vfkit hard-codes `NewSharedDirectory(dir, false)` ([vfkit `virtio.go#L194`](https://github.com/crc-org/vfkit/blob/22b7610767167e5060d1c90a477f106ef6234a20/pkg/vf/virtio.go#L194)); the guest unit doesn't even mount `ro` | **no** — not even guest-side | high |
| podman `qemu` (Linux, podman ≥ 5.2) | virtiofsd without `--readonly` ("TODO: Honor hostmnt.readonly", [`virtiofsd.go#L81`](https://github.com/containers/podman/blob/v5.8.9/pkg/machine/qemu/virtiofsd.go#L81)) | **no** — guest `ro` only (5.0–5.1 used 9p `readonly`: yes) | high |
| podman WSL | no volume mounts at all | n/a | high |

Lima also writes `ro` into the guest fstab via cloud-init for 9p and virtiofs
([`cidata.go#L221-L226`](https://github.com/lima-vm/lima/blob/v2.2.0/pkg/cidata/cidata.go#L221-L226)),
which is the guest-only layer the VM user meets first.

So:

- **The inbox exists only on Lima `vz`+virtiofs and `qemu`+9p.** foldyard doesn't set
  `mountType` today (`_set_expr` writes mounts, sizing and `vmType`), so a Lima default change or
  an operator's `[machine].vmtype = "krunkit"` would silently downgrade it. Pin `.mountType`
  with the vmType (vz → virtiofs, qemu → 9p), and on any other pair, or on the podman backend,
  mount **no inbox**: tenants fall back to their signed-sink-in-the-checkout form, and `fy
  doctor` says why. An inbox the guest can write is worse than none, because it reads as
  tamper-proof.
- **Never nested in a writable mount.** A read-only mount inside a writable one stays writable
  through the parent (Lima's `--mount` help: "do not specify directories that overlap"). With
  `FOLDYARD_STATE_DIR` pointed inside the checkout or the worktrees root that is exactly what
  happens, so `ensure` refuses it.
- **Freshness:** a read-only 9p mount defaults to `cache=fscache`
  ([`defaults.go#L803-L811`](https://github.com/lima-vm/lima/blob/v2.2.0/pkg/limayaml/defaults.go#L803-L811)),
  so a reader in the guest may see host appends late. Fine for evidence, wrong for anything the
  box polls for liveness; pin `9p.cache` if a tenant needs it.
- **Only the operator can widen it:** Lima merges mounts across default, instance and
  `~/.lima/_config/override.yaml`, and the most permissive `writable` wins
  ([`defaults.go#L139-L141`](https://github.com/lima-vm/lima/blob/v2.2.0/pkg/limayaml/defaults.go#L139-L141)).
  The verify row below catches it.

### `verify`: two exemptions, one new assertion

- **Exemptions:** `machine.guest_mounts` returns four mountpoints instead of two. Still exact
  mountpoint only, so the parent state dir, a sibling under it or a nested bind are still FAILs.
  The in-box run can't resolve the host's state dir (`FOLDYARD_STATE_DIR` may move it), so
  `fy box up` bakes the two VM paths into the box env, as it bakes `FY_HOST_HOME` for the audit
  today (`box.py`, `env_args`).
- **New assertion (the inbox is read-only), from inside the box and from the host-side run:**
  1. PID 1's mount table shows the inbox mountpoint with `ro` in its options (`verify.py:221`
     already reads it). Necessary, not sufficient — it's the guest's own flag.
  2. A `--privileged` probe container that binds the inbox VM path **rw** cannot create a file
     there, and `mount -o remount,rw` on it fails. The VM-level mount is in the VM's initial user
     namespace, so in the container's namespace its read-only flag should be locked (the row is
     what checks that expectation); a successful write is a FAIL that names the file it left.
  3. In the box: `/run/foldyard/inbox` refuses a write (the `:ro` bind).
  Under gVisor the probe runs in the sandbox and (2)'s remount says nothing about the VM: report
  it as N/A like the PID-1 audit (`verify.py:234-247`); (1) is read host-side, (3) still holds.
- **What verify can't prove:** host-side enforcement against guest ROOT. The box never has it,
  so that proof is a host-tier e2e (next item), not a verify row. `fy docs security` says so in
  "What verify doesn't prove".
- **The negative stays a negative.** `test_verify_e2e`'s home-mounting VM mounts the home at
  `/mnt/c`, not at an exempted path, so it still FAILs. Add a second negative: the same VM with
  the inbox flipped `writable: true` (`lima_edit` on the stopped instance, restored in the
  fixture) must FAIL the new row.

### Host-tier proof (new e2e, Lima on QEMU/9p in CI, vz/virtiofs on a macOS host)

Root in the guest is boot-time only, and the harness must not break that rule by adding a sudo
path. So the test records a **test-only** `mode: system` provision script under its own marker
(the [0032](./0032-a-root-collector-in-the-guest.md) precedent: never mentions the other
markers, always exits 0), which at boot, as root: `mount -o remount,rw` the inbox; tries to
create, append to, truncate, `chmod`, `chown`, rename and delete a host-seeded file; creates a
symlink and a hardlink; writes each result to `/run/fy-test/inbox-ro`. The host then asserts
every attempt was refused **and** that the seeded file's bytes, mode and inode are unchanged on
the host, then removes the script and restarts. Same harness, `writable: true` flipped, must show
the writes landing (the red-first control). Lives beside `test_verify_e2e.py` as
`test_mounts_e2e.py`; skips on a backend without a recorded-script route (the podman backend).

## Migration

One VM restart on Lima, nothing else.

- **Mount set changes apply on a restart, not a recreate (Lima).** There is no mount-set
  staleness check today: `ensure` only warns when the worktrees root is missing
  (`machine.py:725-728`), and `recreate`'s docstring calls mount sets "init-only"
  (`machine.py:928-931`). But `test_verify_e2e`'s fixture adds a mount with `limactl edit --set`
  on the STOPPED instance and the next start honours it (`tests/test_verify_e2e.py:76-98`).
  So: a new `_record_mounts()` in `ensure`, beside `_record_provisioning()` (`machine.py:548`):
  stopped → `limactl edit` the mount set; running with a different set → refuse with
  `fy machine stop && fy up`, as stale provisioning is refused. Named volumes survive. Needs a
  mount reader that sees the `writable` flag (`LimaBackend.mounts`, `machine_backend.py:531`,
  reads only paths). Why a restart suffices: `limactl edit` refuses a running instance and rewrites `lima.yaml` otherwise; vz and qemu both
  build their shares from it at every start
  ([`cmd/limactl/edit.go#L68-L69`](https://github.com/lima-vm/lima/blob/v2.2.0/cmd/limactl/edit.go#L68-L69),
  [`vm_darwin.go#L660-L720`](https://github.com/lima-vm/lima/blob/v2.2.0/pkg/driver/vz/vm_darwin.go#L660-L720),
  [`qemu.go#L982-L1003`](https://github.com/lima-vm/lima/blob/v2.2.0/pkg/driver/qemu/qemu.go#L982-L1003)).
  `recreate`'s "init-only" docstring is then true only of the podman backend.
- **podman backend:** mounts are fixed at `podman machine init`; `podman machine set` has no
  volume flag
  ([`set.go#L48-L90`](https://github.com/containers/podman/blob/v5.8.9/cmd/podman/machine/set.go#L48-L90)).
  It gets the outbox at its next `fy machine recreate` (which loses named volumes, so a warning
  until then, plus the legacy layout) and never an inbox (above).
- **No boot-script change**, so the `# fy-provision` id is untouched. If the deferred rewording
  of `guest-boot.sh` / `machine-wall.sh` ("wall", "Mac"; CLAUDE.md) is to ride a restart, this is
  the restart — see the open questions.
- **Transcripts:** the box binds `outbox/<slot>/claude/projects` instead of
  `<checkout>/<here>/.devbox-claude/projects` (likewise Codex). For one release the host also
  reads the old bound-out dirs: `sync_current`, the supervisor sweep, `fy transcripts` and
  `worktree remove` archive both, the new one first. The first `fy box up` on the new layout
  archives the old dir once and prints that it can be deleted; it never moves it into the outbox.
  `DEVBOX_TRANSCRIPTS` / `DEVBOX_CODEX_TRANSCRIPTS` keep working. The `.devbox-claude/` /
  `.devbox-codex/` ignore lines (`init.py:47-49`) stay for that release, then go.
- **`worktree remove`** archives the slot, then deletes it (the checkout deletion no longer takes
  it). The delete must not follow symlinks the box planted: `shutil.rmtree` only where
  `shutil.rmtree.avoids_symlink_attacks` is true, else refuse.
- **Monitor file sink:** defaults to `inbox/monitor/`. It keeps its HMAC: the inbox protects the
  file at rest on this host; the signature is what a reader re-verifies once a copy has left it.
  The no-follow writes stay (cheap, and on a backend with no inbox the sink is back in the
  checkout, where they matter). A sink path the operator points at a box-writable tree (the checkout, the
  worktrees root, the outbox) gets a warning.
- **Later inbox tenants, each its own change:** the posture mirror, the staged proxy CA, the
  staged wheel and `server.py`. Each removes a `mountwrite` call site and a file the box can swap.

## Outbox content is box-written: how the host reads it

Treat everything in the outbox like a request body from the box. Today's reader is
`rsync -rtu --no-perms` (`transcripts.py:79`, `:82-104`):

| hazard | today (`rsync -rtu` of the bound-out dir) | required of an outbox reader |
| --- | --- | --- |
| symlinked file | skipped (no `-l`: "non-regular file") | skip — keep |
| symlinked **directory on the path** (`…/claude` → `~/.ssh`) | **followed**: rsync opens `src/` through it and copies the target into `~/.claude/projects` | walk with `O_NOFOLLOW \| O_DIRECTORY` from the outbox root (a reading twin of `mountwrite`) |
| FIFO / device | skipped (no `-D`) | `open(O_NONBLOCK)` + `fstat` `S_ISREG`, else skip (a FIFO blocks a plain open) |
| hardlink | n/a | can't cross the mount; harmless |
| size | unbounded | per-file and per-pass caps; a file over the cap is skipped and reported, never truncated silently |
| content | copied into `~/.claude/projects`, which a HOST `claude --resume` loads | unchanged by this ADR, but say it: resuming a box session on the host feeds box-written text to a host-privileged agent |

Disk exhaustion is unchanged: the box can already fill the host disk through the checkout.

## Alternatives considered

| option | box → host | host → box tamper-proof | cost | verdict |
| --- | --- | --- | --- | --- |
| **bind-out in the checkout** (today) | live, durable | no | grep noise; lost to `git clean`; host walks a box-writable tree | the status quo being replaced |
| **bind-out under the worktrees root** | live | no | a reserved dir name inside a box-writable tree; the same tamper surface | rejected — a hack, and no inbox |
| **dedicated mounts** (this) | live | **yes, where the backend enforces it** | one Lima restart; two audit exemptions; a new e2e | proposed |
| **named volume + tar pulled over ssh** | seconds of loss window | n/a (one way) | the host parses a box-produced tar stream; a pull loop per worktree | rejected — loses the tail on a crash, adds a parser |
| **read the VM's disk image on the host** | only when stopped | n/a | no btrfs tooling on macOS; host parses an attacker-shaped filesystem | rejected |
| **signed sink in the checkout, no mounts** | — | detects, can't prevent | none now | the stopgap; kept as the signature, not as the place |

## Consequences

- `fy docs security`'s boundary section ("mounts only your repo") gains two foldyard-owned
  directories, one of them read-only, and their verify rows.
- The checkout stops carrying box-written content; grep and `git status` see only the code.
- Evidence the host publishes can no longer be altered by the box on a host-enforcing backend;
  on a backend with no inbox it is no worse than today, and the HMAC still detects.
- One more thing the host must read defensively (the outbox) — but it is one place, not
  "anywhere in the checkout".
- The podman backend gets the outbox only after a recreate and never an inbox: no current podman
  provider enforces read-only outside the guest, and applehv/libkrun don't apply it at all.

## Settled with the operator (2026-10-21)

1. **The box sees the monitor's events.** The reader is the in-box harness: control-arena's
   flip experiment runs in the box, builds each step's kernel digest from the events file and
   sends it to the scoring model. What is observed is the BashArena task containers, which are the
   box's SIBLINGS on the engine and have no mounts. They can't read the inbox; only the box can,
   and it is the experimenter. A box observing ITSELF (a monitor over the box's own agent) is a
   different deployment. There the inbox would tell the watched what was caught, so a hook only
   ever delivers into the inbox because the operator configured it to, never by default.
2. **Your computer verifies; the box doesn't.** No Ed25519. Tampering is unlikely here, so the
   response is loud and manual rather than automatic: `fy monitor check FILE` on your computer
   (#107) reports the first bad line, and the operator investigates, then sets the file aside and
   starts a fresh one. Two follow-ups belong to the sink, not to this ADR:
   - The supervisor could compare the file's last line with the recorded MAC before each append,
     so a cut or edit shows in the supervisor log, a notification and doctor, not only at check
     time. It would keep appending, so evidence isn't lost.
   - A host-side `rotate` that records where a fresh file starts, so a legitimately rotated file
     passes `check` and a deleted one still fails it.
   On the inbox the signature stays for portability. A reader of the results can re-verify, which
   "it was mounted read-only" doesn't let them do.
3. **The deferred `guest-boot.sh` / `machine-wall.sh` rewording rides this restart.** The
   operator runs `fy machine stop && fy up` once for both.
4. **A live vz test runs before building:** guest root remounts read-write, remounts the
   virtiofs tag fresh, then writes. The result is recorded below.

## Open questions for the operator

1. **Refuse or warn on a running VM with the old mount set?** Proposed: refuse, like stale
   provisioning (fail closed; the restart keeps volumes). The alternative is a release of the
   legacy layout behind a warning.
2. **Names.** `/var/lib/fy-monitor/inbox` already exists in the guest (the Tetragon delivery
   inbox, `monitor.py:60`, ADR-0032). Keep inbox/outbox (the box's point of view) and rename
   that one, or pick `to-box/` / `from-box/`?
3. **How long do the legacy transcript paths live?** Proposed: one release of dual reading.
   (The symlink-following transcript sync this ADR flagged is being fixed on its own, ahead of
   the outbox.)

## The vz test (2026-10-21)

Setup: Lima 2.1.3, macOS 27.0.1, a Fedora 44 guest (kernel 6.19.10) cloned from the base VM, with
one scratch directory mounted `writable: false` and `mountType: virtiofs`. As root in the guest,
17 kinds of write were tried, among them create, append, overwrite, `mmap`, `truncate`, `chmod`,
`chown`, mtime, xattr, symlink, hard link, FIFO, `mkdir`, rename and `rm`. They ran three ways: on
the mount as Lima made it; after `mount -o remount,rw`; and on the virtiofs tag freshly mounted
`-o rw`. Every write failed every time, and the directory on the host was unchanged by `stat`,
sha256 and xattrs after each run. The same writes against a directory inside the guest all
succeeded, which shows the test could see a write.

**Verdict:** on vz, `writable: false` is enforced on the host side, against root in the guest.
Three things follow for `verify` and the e2e:

- **The guest's mount table can say the inbox is writable when it isn't.** Both the remount and
  the fresh mount return 0 and show `rw` in `/proc/mounts`. Only an actual write shows the truth,
  so the assertion is a write attempt, not the `ro` flag.
- **The refusal isn't always `EROFS`.** It is `EROFS` while the guest's `ro` flag is set. Once the
  guest drops it, the host refuses with `EPERM` (create, `chmod`, links, rename, `rm`) or `EACCES`
  (opening for write). A probe that reads "refused" as `EROFS` alone would misjudge the case that
  matters.
- **Ownership seen in the guest says nothing.** It reports the caller as owner (`root:root` to
  root, the VM user to the VM user).

QEMU with 9p still rests on reading the source. CI's host tier is where that gets proved.
