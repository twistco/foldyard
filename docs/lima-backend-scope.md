# Machine backends — the contract, and why Lima is the default

The design note behind `machine_backend.py` and `config.machine_backend()`. The decision is
[ADR-0011](./adrs/0011-machine-backends-one-socket-contract.md).

## The contract (backend-blind downstream)

A backend provides one thing: a **libpod socket** from a per-project VM. foldyard always has a VM
— there is no VM-less backend ([ADR-0027](./adrs/0027-always-a-vm-native-backend-retired.md)).
Everything downstream (`stack.py`'s `CONTAINER_HOST` export, compose, the box, worktrees) is
backend-blind: `machine.socket()` returns a podman URI either way, with no `--connection`
juggling. Selection: `MACHINE_BACKEND` env → `[machine] backend` → `"lima"`.

- **`LimaBackend`** (the default): `limactl` + Lima's **podman template** — rootless podman in
  each VM (guest socket `/run/user/<uid>/podman/podman.sock`), forwarded to
  `<instanceDir>/sock/podman.sock` on the host. `supports_concurrent()` → `True`. `create()`
  pins CPU, memory, disk and `vmType` with a `--set` expression that also **replaces** the
  template's mounts with only foldyard's (repo + worktrees root, host path = guest path).
  `vmType` resolves from `limactl info` rather than the OS (`[machine] vmtype` overrides it).
  `mountType` is not pinned, so it is Lima's per-driver default: virtiofs under `vz`, 9p under
  QEMU ([linux-support.md](./linux-support.md#1-lima-on-linux)).
- **`PodmanBackend`**: wraps `podman machine`. No extra dependency (the engine CLI is also the
  lifecycle CLI), but `supports_concurrent()` → `False` (below), and its CoreOS appliance can't
  be provisioned with the VM firewall.

Lima is the default for two reasons: its VMs run concurrently, so each project keeps its own VM
(and an open `fy box shell` or `fy code` session in one project survives while you work in
another); and it lets foldyard own VM provisioning (a root `provision:` script at boot), which is
how the VM firewall (`[machine] firewall`) is installed
([lima-wall-machine-integration.md](./lima-wall-machine-integration.md),
[ADR-0009](./adrs/0009-monitoring-cooperative-enforcement-locked.md)).

All of this is validated on real `limactl` — on macOS and in CI on Linux and WSL2 hosts (the
`lima-host-e2e` / `wsl2-host-e2e` jobs). `tests/test_machine_backend.py` covers both backends
with mocked CLIs.

Known loose end: `create()` still passes `template://podman`, which Lima deprecated in v2.0 in
favour of `template:podman`, so every creation prints a deprecation warning.

## podman machine: one VM at a time (upstream podman#26281)

On macOS, `podman machine` with the **applehv** and **libkrun** providers allows only **one
active machine at a time**: the providers gate on `RequireExclusiveActive`, and starting a second
fails with an "only one VM can be active at a time"-class error. It is a macOS-provider limit,
not a podman-wide one. Upstream tracking:
[podman#26281](https://github.com/containers/podman/issues/26281).

So under the podman backend, two projects can't both have a running VM. foldyard handles it in
`machine._start()`, gated on `BACKEND.supports_concurrent()`:

- **podman** → `False`: before starting, foldyard looks for another running machine and, instead
  of podman's error, prints guidance and fails — stop the other machine, or set
  `[machine] backend = "lima"`. `PodmanBackend` returns `False` on every host, not just macOS.
- **lima** → `True`: the guard is skipped.

If upstream lifts the limit, `PodmanBackend.supports_concurrent()` is the one switch to flip; the
guard in `machine._start()` then turns itself off. Re-check the upstream issue before changing
it.
