# ADR-0035 — The host tier gets a VM of its own; a box asks for a run, it never holds KVM

- **Status:** Accepted (2026-11-04), in three parts. **Built:** the test VM, as `just nested-up`,
  `nested-test` and `nested-down`. **Decided:** a box does not get `/dev/kvm`. **Designed, not
  built:** a box asking your computer for a run, to build when the round trip through the operator
  gets in the way.
- **Sources:** the 2026-11-04 spike and the recipe (docs/nested-virt.md, Rig 2); `tests/tools/nested.py`;
  [ADR-0017](./0017-nested-virt-validation-strategy.md) (the nested rig);
  [ADR-0023](./0023-no-host-executed-code-from-the-repo-mount.md) (no host code from the mount);
  [ADR-0032](./0032-a-root-collector-in-the-guest.md) (the guest monitor);
  [ADR-0033](./0033-a-to-box-and-a-from-box-beside-the-checkout.md) (the to-box and from-box).

## Context

The host tier (`machine ensure`, `box up`, `fy host`, the walls) refuses to run in a box, so an
agent working in a box can't test what it changes there. The workaround so far has been to resume
the agent outside the box. That runs box-written tests directly on the operator's computer, with
everything it holds in reach, and on a computer where the tier's fixed `foldyard-example` VM may
already be in use.

On an Apple M3+, a nested Lima VM now runs the whole tier once its kernel traps GIC registers. That
makes three shapes possible: a test VM driven from the computer, `/dev/kvm` in the box, or a box
asking the computer for a run.

**What this guards against**, as the operator set it: supply-chain attacks and prompt injection.
That means an automated payload in a dependency, or an agent steered by something it read. foldyard
isn't meant to be a frontier-lab-grade sandbox, so a crafted hypervisor escape is out of scope. But
both threats land in the box, and both run code there without anyone asking.

The need is foldyard's own: a consumer has no reason to run foldyard's host tier.

## Decision

### 1. A test VM of its own, driven from your computer (built)

`just nested-up` makes an L1 with nesting and the GIC traps, `just nested-test` sends it a commit
and runs the tier, and `just nested-down` stops it inside-out. What runs in the L1 is treated as
hostile:

- **Nothing shared:** no mounts, no forwards, no credentials. A commit arrives as a pack over stdin.
- **A wall at boot** keeps the L1 off your computer's localhost and off private addresses, DNS
  aside. Without it, code in a Lima guest reaches the computer's loopback through the gateway.
- **An unprivileged user** runs the tier, so what it runs can't remove the wall. `test_wall_e2e`
  needs `sudo` on its "host" and skips without it; `--with-sudo` grants it for one run.
- **Every run checks first:** the traps in the running kernel, no mounts, no `sudo`, and a real
  connection attempt to a socket on your computer. It refuses otherwise.
- **Your computer's git reads the box-writable repo** under the same no-repo-code flags as
  `worktree.py`. It writes no ref and checks nothing out, so no hook or filter driver has a turn.
- **What comes back** reaches the terminal with its control sequences stripped.

These are recipes, not `fy` verbs: consumers never get foldyard's justfile, and the code stays out
of the wheel, so there's no config key, no documented surface and no promise to keep. A verb gated on
"is this foldyard's own repo?" would ship the code anyway, behind a check a project could satisfy
by naming itself.

### 2. A box does not get `/dev/kvm` (decided)

It works: a rootless container with box-style flags plus `--device /dev/kvm` booted a Lima L2 in
46 s. The objection isn't feasibility.

- **KVM would sit where the payload lands.** The box is where a dependency's install script or a
  steered agent runs. `/dev/kvm` hands that code the VM kernel's KVM ioctls and, behind them, Apple's
  nested layer, a large surface exposed for one test tier's benefit. A bug there turns "code in a
  box" into "code in the VM". Tokens stay on your computer, but the VM holds every box of the
  project, each checkout mounted writable, and the route to the proxy that injects those tokens.
- **It is a VM-wide switch.** Nesting is set on the VM, and a box holds the engine socket, so any
  box on that VM should be able to start a sibling with `--device /dev/kvm` whatever its own flags
  say. Inferred from how the socket works; not tested.
- **The monitor would lose sight of it.** The guest monitor watches the VM's kernel, so what runs
  inside an L2 appears as one QEMU process. Expected from how it attributes; not tested.
- **The wall would have to widen.** The L2's downloads leave through the box, so under
  `[proxy] enforce` Fedora's mirrors and the registries join the project's allowlist for every box,
  and the L2 has to trust the proxy's CA. Not tried.
- **It can't stay a recipe.** The traps need root in a VM where root is boot-time only: the
  firewall's boot script narrows the VM user's `sudo`, so a later `limactl shell … sudo` is refused.
  So this means a recorded boot script and a `[machine]` key: shipped code for one project. `fy
  verify` has no `/dev/kvm` check today, and would need one.
- **The tests expect a host.** They'd need the `in_box()` guards and the tier's gate on `IN_DEVBOX`
  bypassed, a non-root user (`limactl` refuses root; the box is `--user 0`), and for some modules
  `systemd --user` and `nft` in a container.

### 3. A box asks, your computer runs it (designed, not built)

When an agent needs its own test results, keep the VM of part 1 and add a narrow request path:

1. The agent commits, and writes a request to the from-box: a commit id and a list of modules.
2. `just nested-serve`, in a terminal the operator opened, reads it through the no-follow walk the
   transcript sync uses. It accepts a 40-hex commit and names matching `tests/test_*_e2e.py`, nothing
   else: no shell string, no flags, no path, and `--with-sudo` is never on offer. One run at a time.
3. It runs them as part 1 does, and writes the log and JUnit to the to-box, which the box can read
   and not write.

The box gets a test result without KVM, a host shell or a credential. Closing the terminal ends it,
and that terminal is the consent: it isn't a daemon, and it isn't in the supervisor.

**What this still grants, and it is a real widening:** box-written code runs with the public
internet in a VM outside `[proxy] enforce`. A steered agent could put whatever the box can read in a
commit, and a test could send it out. There's no credential at rest in a box to take, so what's
exposed is the checkout and the transcripts, not the keys. Routing the L1 through an allowlist would
close it, and it wants deciding when this is built. Also:

- The result is text an agent will read, written by code it asked to run, so it stays untrusted data.
- A verdict from the L1 is what the code under test reports. It's a test result, not an attestation.

Rejected for the path itself: giving the box SSH into the L1. It needs no server, but it needs a
hole in the VM firewall to your computer's forwarded port and a key in the box, and it lets the box
run anything rather than the tier.

## Consequences

- Testing fy's host tier no longer needs an agent outside the box, or the `foldyard-example` VM on
  your computer. It needs an Apple M3+: elsewhere, the x86 nested rig or CI.
- An agent in a box still can't run the tier itself until part 3 is built. Someone runs it for them.
- The arm64 Linux host tier gets a way to run, by hand; CI's arm runners have no KVM.
- It isn't a boundary against a hypervisor escape from the L1, and doesn't try to be. Nor does it
  bound what leaves the L1 over the public internet.
- **The recipes read from the box-writable checkout.** With an editable `fy` that is true of `fy`
  itself on foldyard's own project ([security.md](../security.md) names the exception), so these add
  no new exposure, and don't remove it. A steered agent could edit `tests/tools/nested.py` before
  you next run it. To tighten this, run the recipes and `fy` from a clean clone or a frozen install.
- The wall's rules live in `tests/tools/nested_l1/provision.sh`. If Lima's gateway moves, the probe
  could pass for the wrong reason: `nested-up` tries it before the wall on a fresh L1 and warns.
