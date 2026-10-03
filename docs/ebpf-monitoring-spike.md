# eBPF monitoring — slice 0 spike (2026-10-03)

The question before any monitoring code: on the guest foldyard actually runs, does a standalone
[Tetragon](https://tetragon.io/) load, and can it say **which container** did something? This
is the record of one run, with the kit to repeat it ([ebpf-spike-kit/](./ebpf-spike-kit/)).
Nothing here is product code; foldyard is unchanged.

## Where it ran

| | |
| --- | --- |
| Host | a cloud dev container, x86_64, **no `/dev/kvm`** — so QEMU ran with TCG (software emulation) |
| VM | Lima 2.2.0 (the release CI pins), `template:podman`, `vmType: qemu`, 4 vCPU / 6 GiB |
| Guest | Fedora Linux 44 Cloud, kernel `6.19.10-300.fc44.x86_64`, podman 5.8.7, SELinux enforcing |
| Collector | Tetragon v1.7.1 amd64 release tarball, standalone systemd service, one tracing policy |

What this run can't speak to: **timings and overhead** (TCG is one to two orders of magnitude
slower than KVM — every duration below is an upper bound, not a measurement), **arm64 / `vz`**
(an Apple-silicon host), and foldyard's own provisioning — the VM firewall and the narrowed sudo
grant were NOT in place; the spike VM kept cloud-init's `NOPASSWD:ALL`, used only from the host.

An aside: the dev container's own kernel can't run Tetragon at all (`# CONFIG_KPROBES is not
set`; the base sensor's exit hook fails to attach). Irrelevant to the product — the guest kernel is
what matters — but it is why everything here goes through a VM.

## Findings

### 1. The guest kernel has everything observe-mode needs, and BPF-LSM is active

`/boot/config` and `tetra probe` agree: BTF, kprobes, `kprobe_multi`, fentry, BPF trampolines,
and **`bpf` is in the active LSM list** (`lockdown,capability,yama,selinux,bpf,landlock,ipe,ima,evm`)
— Fedora enables it by default, no boot argument needed. One gap for later enforcement:
`override_return: false` (Fedora doesn't build `CONFIG_FUNCTION_ERROR_INJECTION`) and
`fmodret_syscall: false`, so Tetragon's `Override` action on syscalls is unavailable; denial
would have to go through LSM hooks. cgroup v2 only.

### 2. Delivery: host fetches, ssh carries, root re-verifies — works

The 81 MB tarball, checksum-verified on the host against the release's `.sha256sum`, was copied
in over ssh (`limactl copy`, 10 s) to `/tmp` as the Lima user, then copied by root into a
`0700` root directory and **verified there** (3.5 s) before unpacking. The hash is the trust
anchor; the transport can be anything the Lima user controls (rsync, scp, a read-only mount).
Verifying the root-owned copy, not the `/tmp` one, is what closes the swap-after-check window.

### 3. One bad policy file takes the whole collector down

The first policy used `returnArgAction: Post` (not valid in 1.7). Tetragon refused to start at
all — `Failed to execute tetragon … policy handler 'tracing' failed loading policy` — and
systemd's `Restart=on-failure` looped. No policy, no monitoring: foldyard must validate a
rendered policy before recording it, and the guest report must say "collector down", not
"no events". Startup to `Listening for events` took ~60 s under TCG (40 s of it loading the
policy's two kprobes + two return probes).

### 4. The admin surface is closed to the box uid — except the health port

As the Lima user (= the box's uid), without sudo:

- the gRPC admin socket `/var/run/tetragon/tetragon.sock` is `root:root 0660` → `permission denied`;
- the export log `/var/log/tetragon/tetragon.log` is `0600 root` → `Permission denied`;
- `systemctl stop tetragon` → `Access denied`.

But the gRPC **health server listens on `*:6789`**, and from inside the box
`http://host.containers.internal:6789/` answered (HTTP 415 — a gRPC server). `host.containers.internal`
is `169.254.1.2`, which pasta maps to the VM's own address. A listener bound to `127.0.0.1` was
**not** reachable that way. So: set `health-server-address` to `127.0.0.1:6789` (or empty).
The same applies to `metrics-server` and `gops-address` if ever enabled.

### 5. Attribution: Tetragon's container id is missing for podman 5, and forgeable

This is the finding that shapes slice 1.

**Missing.** Rootless podman 5.8.7 with the systemd cgroup manager puts a container's payload in a
sub-cgroup: `…/user@UID.service/user.slice/libpod-<id>.scope/container`. Standalone Tetragon
fills `process.docker` from the **leaf** cgroup's name (`bpf/process/bpf_process_event.h`,
`__event_get_current_cgroup_name`; parsed in `pkg/sensors/exec/exec_linux.go`), and `container`
isn't an id — so every process inside the box and inside the sibling had **no container id**. Only
crun's setup step, which runs in `libpod-<id>.scope` before moving the payload down, carried one.

**Ancestry doesn't recover it.** A `podman exec` process chains to `conmon ← podman ← the Lima
user's shell`; an API-created sibling to `conmon ← the podman service`. Neither passes through a
process that carried the id. And `uid` is the Lima user's (30033) for the box's root and for the
user's own shell alike.

**Forgeable.** crun takes the sub-cgroup's name from the `run.oci.systemd.subgroup` annotation,
which any engine client sets. With it empty, the payload stays in `libpod-<id>.scope` and every
event was attributed correctly. With it set to **the box's id**, a different container's
`cat /etc/passwd` was reported as the box's:

```
flat-…  (subgroup="")          /bin/cat /etc/passwd   docker=fb23d27ab5ee…   ← its own id ✓
forge-… (subgroup=<box id>)    /bin/cat /etc/passwd   docker=4c6ef73934f2…   ← the BOX's id ✗
```

A second, scripted run (the kit, fresh containers) reproduced it exactly: the box's 500 `/bin/true`
execs, its `cat` and the sibling's `cat` all reported no container; the flat container's were its
own; and **the only events reported as the box's were the forging container's**.

The agent in the box holds the engine socket, so it can do exactly this. Tetragon's container id
is therefore a **claim made by whoever created the container**, not evidence — the plan's rule
"treat agent-provided labels as claims" applies to the cgroup name too.

What can be trusted is the `libpod-<id>.scope` component: podman names it from the id it
generated, which an API client can't choose. Attribution should key on that ancestor, never on the leaf name.

**Decided (same day): mount-namespace inode, resolved against the outermost podman scope.**
Tetragon's events carry no cgroup id or path (`api/v1/tetragon/tetragon.proto`, `Process`), so
the first option as first written isn't available. What they can carry, with `enable-process-ns`,
is the process's namespace inodes, and every podman container gets its own mount namespace. A
root process in the guest maps inode → container from kernel state
([ebpf-spike-kit/resolve.py](./ebpf-spike-kit/resolve.py)): walk the Lima user's delegated subtree
(`user@<uid>.service`) top-down, let the OUTERMOST `libpod-<64 hex>.scope` on each path own
everything below it, and read each member's `/proc/<pid>/ns/mnt`. Checked live:

| container | how it was made | resolved to |
| --- | --- | --- |
| box | `podman exec` payload (Tetragon: no id) | the box ✓ — event inode `4026532977` = the box's |
| forge | sub-cgroup named `<box id>` | itself ✓ |
| nest | sub-cgroup named `libpod-<box id>.scope` | itself ✓ — outermost wins |
| sharepid | `--pid=container:<box>` | itself ✓ — own mount namespace |
| parent | `--cgroup-parent=fy-forge.slice` (lands under `fy.slice/…`) | itself ✓ |
| nocg | `--cgroups=disabled` (stays in the caller's cgroup) | **unmapped** — honest, not wrong |

`--cgroups=disabled` is the remaining gap: such a container is visible as an unknown mount
namespace, never misattributed. Refusing it at the engine socket is enforcement work for later.
Not taken: Tetragon's `enable-cgidmap` + its OCI hook (unverified on rootless podman, and it
would make Tetragon's mapping the authority again); a guest default of
`run.oci.systemd.subgroup=""` (helps well-behaved clients only).

An escape to the Lima user could also create and enter its own `libpod-<anything>.scope` in its
delegated subtree; that is the same "escape to the VM user" boundary the threat model already
draws, not a new hole.

### 6. A box connection is two events

`curl` in the box connecting to `1.1.1.1:443` produced `tcp_connect` from `/usr/bin/curl`
(saddr = the VM's own address — pasta mirrors it into the namespace) and, 8 ms later, from
`/usr/bin/pasta.avx2` — the real egress. Nothing ties the two together but time and destination.
pasta also produced ~250 exit events in 45 s of activity: noise to filter.

### 7. Volume

`process_exec` ≈ 780 B and `process_exit` ≈ 820 B of JSON; 500 execs = 1,000 events ≈ 0.8 MB.
Tetragon's default export rotation (10 MB × 5 backups) holds ~70,000 events — a loop of ~35,000
execs rotates earlier evidence out of the guest. Tetragon's RSS was ~84 MB.

## What it changes in the plan

- **Attribution is foldyard's, Tetragon is the sensor** (finding 5): `enable-process-ns` on, and
  the relay resolves each event's mount-namespace inode against the outermost
  `libpod-<id>.scope`. Tetragon's `docker` field is never read.
- **The relay is root, started at boot.** The log is `0600 root`, and foldyard's host reaches the
  guest over ssh as the Lima user — the box's uid — so a host-side pull can't read it without
  handing the log to the box too. A root relay can push to the host: the VM firewall exempts
  system uids (`meta skuid != { WALL_UID, subuids } accept`) and opens for the box only this
  project's port band, so a relay port **outside** the band is unreachable from the box when the
  firewall is on. It is opt-in, so the channel also needs its own authentication — a secret the
  boot script writes root-only.
- **Collector config is part of the recording:** `health-server-address` bound to loopback,
  export rotation sized for the expected rate, policy validated before it is recorded.
- **Enforcement, later, is LSM-hook only** on this kernel (finding 1).

## Not done

arm64 + `vz` (run the kit on an Apple-silicon host); overhead on real hardware; the same run
with foldyard's provisioning in place; the proxy join; gVisor.

## Re-running

```bash
limactl create --tty=false --name fyspike template:podman --set '.mounts = []'
limactl start fyspike
docs/ebpf-spike-kit/run.sh fyspike          # results in ./ebpf-spike-fyspike-<stamp>/
limactl delete -f fyspike
```

The kit installs Tetragon with the guest's sudo, so it needs a throwaway instance — on a foldyard
VM the boot provisioning has narrowed sudo to `shutdown`, and it fails there by design.
