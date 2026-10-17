# The base VM — new projects in seconds, and the big downloads done ahead of time

Each foldyard project gets its own VM. Creating one from scratch means downloading a Fedora
cloud image, booting it, installing podman in it, then building the box image inside it. That's
a few minutes and several hundred megabytes, and none of it carries over: it lives inside the new
VM's disk, where the next project can't reach it.

The **base VM** does that work once per computer. `fy machine base bake` (or `fy prereq --fetch`)
creates a VM named `foldyard-base` from Lima's podman template (the recipe a project VM is
otherwise created from), with **nothing mounted**, boots it
so the template's setup runs, builds foldyard's generic box image in it, and stops it. From then
on, a new project's VM is a **clone** of it. On macOS the clone is copy-on-write, so it takes
seconds. The clone gets the project's own sizing and mounts, exactly as a fresh VM would. Then
foldyard's usual setup runs on it: the boot provisioning, the VM firewall, the checks. In `fy box
up`, the box image the clone carries in is re-tagged as the project's instead of being built.

Measured on Apple-silicon macOS (Lima 2.1.3, `vz`, Apple's hypervisor), from an empty directory to
a box behind the VM firewall:

| | first `fy box up`, no base | with a base |
| --- | --- | --- |
| VM image download | ~500 MB (unless Lima has it cached) | none |
| VM create + first boot | ~30 s, plus `dnf install podman` (~60 MB) | ~10 s, no install |
| box image | built: ~150 MB of downloads | already there |
| the whole `fy box up` | minutes | ~26 s |

Baking it once took 105 s with a good connection.

## Commands

```bash
fy prereq --fetch        # check this computer, then bake the base if it's missing or out of date
fy machine base          # is there a base, how old is it, is it still current?
fy machine base bake     # (re)bake it
fy machine base rm       # delete it — VMs already cloned from it are unaffected
```

`FOLDYARD_BASE_VM=0` turns cloning off: every VM is created from the template, as before.

## When a project's VM is a clone

`fy up`, `fy box up` and `fy machine recreate` clone when all of these hold. Otherwise they create
from the template, as before, and say why:

- a base has been baked, it is stopped, and foldyard baked it (an instance named
  `foldyard-base` that foldyard has no record of is never used, deleted or replaced);
- the project's hypervisor (`[machine] vmtype`, or the computer's default) is the base's;
- the project's disk is at least the base's 10 GiB (a clone can grow a disk, not shrink it);
- Lima's podman template still resolves to what the base was baked from (a Lima upgrade that
  moves to a new Fedora release changes it).

A box image that has changed since the bake doesn't stop the clone. The VM half still saves the
download and the install, and `fy box up` builds the box image as it would have anyway.

## What keeps it safe

- **A clone happens only when a VM is created.** An existing project VM is never re-cloned: its
  store holds that project's images and volumes, its agent logins and databases included.
  Rebaking the base changes what new VMs start from, nothing else.
- **The base is never made from a project's VM.** A project VM has run a box, and a box runs
  agents. Anything that escaped a container there would be copied into every later project. The
  base is touched only by Lima's own setup and foldyard's own box build, with nothing mounted.
- **The base has no firewall, by design.** It runs no repo code and mounts nothing; its only
  network traffic is the cloud image, Fedora's podman package, and the generic box image's
  downloads (Debian packages and uv). Every project VM gets its firewall and provisioning at its
  own boot, exactly as a template VM does.
- **Bake on the computer that uses it.** A VM disk from someone else's computer is a disk you
  haven't seen being made. For a room with a slow network, share what's public instead: Lima's
  download cache, and uv's.

## Staleness

The record at `~/.foldyard/base-vm.json` (on your computer, outside every repo) holds what the
base was made from. Three inputs decide whether it's current: the fully resolved template (with
its image digests), the hypervisor, and the generic box image's fingerprint. `fy machine base` and
`fy prereq` warn when one has changed. They also warn when the base is more than 30 days old,
because its packages then miss the security updates since. A VM created from the template doesn't
update its packages either, so the age warning is about when the bake happened, not about
cloning. The foldyard version that baked it is recorded for reference, but it doesn't make the
base stale: nothing in the base depends on it except the box image, and the fingerprint covers
that.

## Running a workshop

Attendees bake the base at home (`fy prereq --fetch`), so on the day `fy init` and `fy box up`
take seconds. [workshop.md](./workshop.md) has the per-platform setup to send them, the running
order for the day, and the failures to expect.

## Known Lima quirk

Lima 2.1.3's `limactl clone` creates the new instance's `sock/` directory with mode `0000`, so
the podman socket can't be forwarded and the VM boots with no reachable engine. foldyard resets
it to `0750` (what a template create makes) after every clone.
