"""Pluggable backend for foldyard's rootless engine (docs/lima-backend-scope.md).

Backends expose ONE contract — a libpod socket for foldyard to target. VM-backed backends hand
out a per-project VM socket; the explicit native backend hands out the host's rootless podman
socket. Everything downstream (``stack.py``'s ``CONTAINER_HOST`` export, compose, box, worktrees)
is backend-blind: ``machine.socket()`` returns a podman URI either way.

``cli`` here is the VM-LIFECYCLE binary, and it is not the whole prerequisite. The engine CLI
(:func:`config.engine` — ``podman``, or ``docker`` where podman is absent) is a SEPARATE and
always-required host dependency: it is what drives the socket a backend hands out, for every
compose/box/engine verb. So Lima is limactl *plus* whichever engine CLI :func:`config.engine`
resolves to, not limactl *instead of* one.

* :class:`LimaBackend` (**the default**): ``limactl`` + Lima's **podman template**, which runs a
  real podman service inside each VM and forwards its libpod socket to the host
  (``<Dir>/sock/podman.sock``). Two things make it the default rather than an option: Lima runs
  VMs concurrently, so per-project machines coexist — no stop/swap dance, and a project's open
  ``box shell`` / ``code`` session survives while you work in another project — and it is the
  only backend that can be provisioned with the in-VM fail-closed egress wall
  (``[machine].wall``), which turns cooperative proxy routing into enforcement.

* :class:`PodmanBackend` (``[machine].backend = "podman"``): ``podman machine``. The
  zero-EXTRA-dependency floor: the engine CLI you already need is also the lifecycle CLI, so
  there is nothing further to install. macOS runs **one** VM at a time (the applehv/libkrun
  providers' ``RequireExclusiveActive`` gate; see ``docs/podman-multi-vm-issue-26281.md``), so it
  is NOT concurrent — :func:`machine.ensure` refuses to start beside another running machine and
  tells you to stop it (or switch to Lima) — and its CoreOS appliance can't be provisioned with
  the wall.

* :class:`NativeBackend` (explicit opt-in, ``[machine].backend = "native"``): no VM lifecycle;
  foldyard talks to the host's rootless podman socket directly. Useful for Linux/WSL2 dev and CI,
  but weaker isolation than a VM-backed backend because containers share the host kernel.

The Lima paths marked ``SPIKE`` below follow Lima's documented podman-template behaviour
but have not been exercised in CI (no ``limactl`` in the dev box). Verify on a Mac with
``brew install lima`` before relying on them — see the scope doc's "trickiest bits".
"""

from __future__ import annotations

import json
import os
import signal
import socket
import stat
import subprocess
import sys
import time
from abc import ABC, abstractmethod
from pathlib import Path
from shutil import which

VM_HELPERS = ("krunkit", "vfkit", "gvproxy", "qemu-system")
"""Basenames (prefixes) of the host processes that make up a running podman machine — the
hypervisor and the user-mode network/socket forwarder. :meth:`PodmanBackend.reap_orphans`
will only ever signal a process whose argv[0] is one of these."""


def _err(*a: object) -> None:
    print(*a, file=sys.stderr, flush=True)


def socket_alive(uri: str, timeout: float = 2.0) -> bool:
    """Does something actually ACCEPT on this ``unix://`` socket? The liveness primitive
    behind :meth:`Backend.responsive` — a plain ``exists()`` would pass on a stale socket
    file left by a killed process, and the lifecycle flag (``podman machine list``) can say
    ``running`` while nothing serves at all."""
    path = uri.removeprefix("unix://")
    if not path:
        return False
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    try:
        sock.connect(path)
        return True
    except OSError:
        return False
    finally:
        sock.close()


def _run(cmd: list[str]) -> subprocess.CompletedProcess:
    """Capture a backend CLI call. A missing binary (e.g. ``state()`` probed on a host with no
    ``podman``/``limactl``) degrades to a non-zero result rather than raising, so read-only
    callers like the doctor/TUI fast pass stay graceful."""
    try:
        return subprocess.run(cmd, capture_output=True, text=True)
    except OSError:
        return subprocess.CompletedProcess(cmd, 127, "", "")


class Backend(ABC):
    """The VM-lifecycle contract :mod:`foldyard.machine` orchestrates. ``name`` identifies
    the backend (``"podman"`` | ``"lima"``); ``cli`` is the host binary it drives."""

    name: str
    cli: str
    # How to get `cli`, quoted verbatim in the "not installed" error. Per-backend because the
    # binary and the package are not always the same word: lima ships `limactl`, so the obvious
    # `brew install limactl` fails — which is exactly the message a new user would hit first.
    install_hint: str = ""

    def available(self) -> bool:
        """Is the backend's host CLI installed?"""
        return which(self.cli) is not None

    @abstractmethod
    def supports_concurrent(self) -> bool:
        """Can several of these VMs run at once on this host? podman on macOS: no; Lima: yes."""

    @abstractmethod
    def exists(self, name: str) -> bool: ...

    @abstractmethod
    def state(self, name: str) -> str:
        """Normalised lifecycle: ``"running"`` | ``"stopped"`` | ``""`` (absent/unknown).

        A LIFECYCLE FLAG, NOT LIVENESS — it reports what the backend last recorded, which
        survives the VM dying underneath it. Pair with :meth:`responsive`."""

    def responsive(self, name: str) -> bool:
        """Is the VM's libpod socket actually being served? ``state() == "running"`` is not
        enough: a host crash (or any start that dies after the guest fails to signal ready)
        leaves the flag set while the socket is gone, so every engine call fails with a raw
        ``dial unix …: no such file or directory`` and ``ensure`` cheerfully no-ops."""
        return socket_alive(self.socket(name))

    def reap_orphans(self, name: str) -> list[int]:
        """Clean up what a failed/partial ``stop`` left behind — VM host processes still
        alive, and socket files nothing serves — returning the PIDs signalled. Backends that
        have no such failure mode keep the default no-op."""
        return []

    @abstractmethod
    def mounts(self, name: str) -> list[str]:
        """The VM's mount targets — for the worktrees-mount drift warning."""

    @abstractmethod
    def socket(self, name: str) -> str:
        """The VM's libpod socket as a ``unix://`` URI (assumes the VM exists)."""

    @abstractmethod
    def guest_socket(self) -> str:
        """The podman socket path as seen INSIDE the VM (the dev box bind-mounts this to its
        ``/var/run/docker.sock``). Differs by backend: podman machine exposes a docker-compat
        path; Lima's podman template forwards a rootless ``/run/user/<uid>/…`` socket."""

    @abstractmethod
    def list_running(self, name: str = "") -> list[str]:
        """Names of currently-running VMs of this backend."""

    @abstractmethod
    def create(self, name: str, resources: dict, volumes: list[tuple[str, str]]) -> bool:
        """Provision the VM (stopped) with the given sizing + ``(host, guest)`` mounts.
        Mounts REPLACE the backend's defaults so the VM sees ONLY what foldyard declares —
        the isolation property ``fy verify`` asserts."""

    @abstractmethod
    def start_argv(self, name: str) -> list[str]: ...

    @abstractmethod
    def stop_argv(self, name: str) -> list[str]: ...

    @abstractmethod
    def start(self, name: str) -> bool: ...

    def stop(self, name: str) -> bool:
        return _run(self.stop_argv(name)).returncode == 0

    @abstractmethod
    def remove(self, name: str) -> bool: ...


class PodmanBackend(Backend):
    """``podman machine`` — a straight extraction of foldyard's original logic."""

    name = "podman"
    cli = "podman"
    install_hint = "brew install podman"

    def supports_concurrent(self) -> bool:
        return False  # macOS applehv/libkrun: RequireExclusiveActive — one VM at a time

    def exists(self, name: str) -> bool:
        return _run(["podman", "machine", "inspect", name]).returncode == 0

    def state(self, name: str) -> str:
        out = _run(["podman", "machine", "inspect", name, "--format", "{{.State}}"])
        if out.returncode != 0:
            return ""
        s = out.stdout.strip().lower()
        return "running" if s == "running" else ("stopped" if s else "")

    def _config(self, name: str) -> dict:
        """The machine's on-disk config json (`<ConfigDir>/<name>.json`) — podman's own record
        of the VM's mounts and file paths. ``{}`` when unreadable."""
        out = _run(["podman", "machine", "inspect", name, "--format", "{{.ConfigDir.Path}}"])
        if out.returncode != 0:
            return {}
        cfg = Path(out.stdout.strip()) / f"{name}.json"
        try:
            data = json.loads(cfg.read_text())
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def mounts(self, name: str) -> list[str]:
        """podman 5.x `machine inspect` does NOT expose `.Mounts` for the libkrun/applehv
        providers — the field is absent from `InspectInfo`, so a `{{range .Mounts}}` template
        ERRORS (exit 125) and we'd cry wolf about a missing worktrees mount on every start.
        Read them from the on-disk machine config json instead."""
        data = self._config(name)
        return [m["Target"] for m in data.get("Mounts") or [] if m.get("Target")]

    def socket(self, name: str) -> str:
        out = _run(
            [
                "podman",
                "machine",
                "inspect",
                name,
                "--format",
                "{{.ConnectionInfo.PodmanSocket.Path}}",
            ]
        )
        return "unix://" + out.stdout.strip()

    def guest_socket(self) -> str:
        return "/run/docker.sock"  # podman machine exposes the docker-compat socket here

    def list_running(self, name: str = "") -> list[str]:
        out = _run(["podman", "machine", "list", "--format", "{{.Name}}|{{.Running}}"])
        if out.returncode != 0:
            return []
        names = []
        for line in out.stdout.splitlines():
            n, _, running = line.partition("|")
            if running.strip().lower() == "true" and n.strip():
                names.append(n.strip())
        return names

    def create(self, name: str, resources: dict, volumes: list[tuple[str, str]]) -> bool:
        flags = [
            "--cpus",
            resources["cpus"],
            "--memory",
            resources["memory"],
            "--disk-size",
            resources["disk"],
        ]
        vols: list[str] = []
        for src, dst in volumes:
            vols += ["--volume", f"{src}:{dst}"]  # --volume REPLACES podman's default mounts
        return subprocess.run(["podman", "machine", "init", name, *flags, *vols]).returncode == 0

    def start_argv(self, name: str) -> list[str]:
        return ["podman", "machine", "start", name]

    def stop_argv(self, name: str) -> list[str]:
        return ["podman", "machine", "stop", name]

    def start(self, name: str) -> bool:
        return subprocess.run(self.start_argv(name)).returncode == 0

    # ── orphan reaping (the half-torn-down machine) ────────────────────────────────────
    #
    # When a start dies after the hypervisor is up (host crash, guest failing to signal ready),
    # podman tears down ITS half — the api socket, the network forwarder — and gives up, but the
    # hypervisor process survives, unreachable and holding the machine's ports. `podman machine
    # stop` then reports success WITHOUT reaping it (it no longer tracks the process), so the
    # next start races the orphan. Reaping is by podman's OWN recorded paths, never by name
    # matching: a sibling machine sharing a name prefix (`acme` vs `acme-two`) must
    # never be killed by the wrong project's `fy up`.

    @staticmethod
    def _efi_stores(node: object) -> list[str]:
        """Every ``efiVariableStorePath`` in the config, at whatever depth the provider nests
        it (``LibKrunHypervisor.KRun.…`` vs ``AppleHypervisor.…``)."""
        found: list[str] = []
        if isinstance(node, dict):
            for key, value in node.items():
                if key == "efiVariableStorePath" and isinstance(value, str) and value:
                    found.append(value)
                else:
                    found += PodmanBackend._efi_stores(value)
        elif isinstance(node, list):
            for value in node:
                found += PodmanBackend._efi_stores(value)
        return found

    def _machine_paths(self, name: str) -> set[str]:
        """Absolute paths that belong to THIS machine and nothing else: its disk image, its EFI
        variable store, its api socket. Deliberately NOT the machine's mounts — the repo path
        appears in half the argvs on the host and would match anything."""
        data = self._config(name)
        paths = {p for p in self._efi_stores(data) if p}
        image = data.get("ImagePath")
        if isinstance(image, dict) and image.get("Path"):
            paths.add(str(image["Path"]))
        sock = self.socket(name).removeprefix("unix://")
        if sock:
            paths.add(sock)
        return paths

    def _orphan_pids(self, name: str) -> list[int]:
        """Live VM host processes belonging to ``name``: OUR uid, a known hypervisor/forwarder
        binary, and an argv naming one of the machine's own paths. All three must hold."""
        paths = self._machine_paths(name)
        if not paths:
            return []  # can't identify the machine's files → never guess at what to kill
        out = _run(["ps", "-Ao", "pid=,uid=,command="])
        if out.returncode != 0:
            return []
        pids = []
        for line in out.stdout.splitlines():
            fields = line.split(maxsplit=2)
            if len(fields) < 3 or not fields[0].isdigit() or fields[1] != str(os.getuid()):
                continue
            cmd = fields[2]
            argv0 = Path(cmd.split()[0]).name
            if not any(argv0.startswith(h) for h in VM_HELPERS):
                continue
            if any(p in cmd for p in paths):
                pids.append(int(fields[0]))
        return pids

    @staticmethod
    def _alive(pid: int) -> bool:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except OSError:
            return True  # EPERM — someone else's process, so very much alive
        return True

    def reap_orphans(self, name: str) -> list[int]:
        """SIGTERM (then SIGKILL) the machine's surviving host processes and unlink its dead
        socket files. Returns the PIDs signalled."""
        pids = self._orphan_pids(name)
        for pid in pids:
            try:
                os.kill(pid, signal.SIGTERM)
            except OSError:
                pass
        for _ in range(20):
            if not any(self._alive(p) for p in pids):
                break
            time.sleep(0.25)
        for pid in pids:
            if self._alive(pid):
                try:
                    os.kill(pid, signal.SIGKILL)
                except OSError:
                    pass
        self._unlink_dead_sockets(name)
        return pids

    def _unlink_dead_sockets(self, name: str) -> None:
        """Drop the machine's leftover socket files — a killed hypervisor leaves them behind and
        the next start's bind fails with EADDRINUSE.

        The candidates are derived from podman's OWN api-socket path (``<dir>/<stem>-api.sock``)
        rather than globbed off the machine name, so `acme` can't sweep `acme-two.sock`
        out from under a sibling project. Each is unlinked only if it is a socket (never the
        machine log) that fails a connect — a live machine's sockets are untouchable."""
        api = self.socket(name).removeprefix("unix://")
        if not api.endswith("-api.sock"):
            return
        stem = Path(api).name.removesuffix("-api.sock")
        directory = Path(api).parent
        siblings = (f"{stem}.sock", f"{stem}-gvproxy.sock", f"{stem}-gvproxy.sock-krun.sock")
        for path in [Path(api), *(directory / s for s in siblings)]:
            try:
                if not stat.S_ISSOCK(path.stat().st_mode):
                    continue
                if socket_alive(f"unix://{path}"):
                    continue
                path.unlink()
            except OSError:
                continue

    def remove(self, name: str) -> bool:
        return _run(["podman", "machine", "rm", "-f", name]).returncode == 0


class NativeBackend(Backend):
    """Native rootless podman on Linux/WSL2 — no VM exists, so lifecycle verbs are no-ops."""

    name = "native"
    cli = "podman"
    install_hint = "your distro's podman package"

    def supports_concurrent(self) -> bool:
        return True

    def exists(self, name: str) -> bool:
        return True

    def state(self, name: str) -> str:
        return "running"

    def responsive(self, name: str) -> bool:
        """Pairs with the unconditional ``state()``: there is no VM here, so there is no
        half-started VM to detect and nothing ``ensure`` could restart if we said otherwise."""
        return True

    def mounts(self, name: str) -> list[str]:
        return []

    def _socket_path(self) -> str:
        for env in ("CONTAINER_HOST", "DOCKER_HOST"):
            value = os.environ.get(env, "")
            if value.startswith("unix://"):
                return value.removeprefix("unix://")
        runtime = os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}"
        return str(Path(runtime) / "podman" / "podman.sock")

    def socket(self, name: str) -> str:
        return "unix://" + self._socket_path()

    def guest_socket(self) -> str:
        return self._socket_path()

    def list_running(self, name: str = "") -> list[str]:
        return []

    def create(self, name: str, resources: dict, volumes: list[tuple[str, str]]) -> bool:
        return True

    def start_argv(self, name: str) -> list[str]:
        return ["true"]

    def stop_argv(self, name: str) -> list[str]:
        return ["true"]

    def start(self, name: str) -> bool:
        return True

    def stop(self, name: str) -> bool:
        return True

    def remove(self, name: str) -> bool:
        return True


class LimaBackend(Backend):
    """``limactl`` + Lima's podman template — concurrent, project-scoped VMs on macOS."""

    name = "lima"
    cli = "limactl"
    install_hint = "brew install lima"

    def supports_concurrent(self) -> bool:
        return True  # Lima runs N VMs natively — no exclusive-active gate

    def _instances(self, name: str = "") -> list[dict]:
        """Parse `limactl list --json` (JSON-lines, one object per instance). With ``name``,
        filters to that instance."""
        cmd = ["limactl", "list", "--json"]
        if name:
            cmd.append(name)
        out = _run(cmd)
        if out.returncode != 0:
            return []
        rows = []
        for line in out.stdout.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except ValueError:
                continue
        return rows

    def _instance(self, name: str) -> dict | None:
        for r in self._instances(name):
            if r.get("name") == name:
                return r
        return None

    def exists(self, name: str) -> bool:
        return self._instance(name) is not None

    def state(self, name: str) -> str:
        inst = self._instance(name)
        if inst is None:
            return ""
        return "running" if str(inst.get("status", "")).lower() == "running" else "stopped"

    def mounts(self, name: str) -> list[str]:
        """Lima records mounts in `~/.lima/<name>/lima.yaml`. `limactl list --json` doesn't
        surface them, so scan the config for `location:`/`mountPoint:` lines — a YAML parser
        would be overkill for a single drift warning (keeps foldyard stdlib-only)."""
        cfg = Path.home() / ".lima" / name / "lima.yaml"
        try:
            text = cfg.read_text()
        except OSError:
            return []
        targets = []
        for line in text.splitlines():
            s = line.strip()
            for key in ("mountPoint:", "location:"):
                if s.startswith(key):
                    val = s[len(key) :].strip().strip("\"'")
                    if val:
                        targets.append(val)
        return targets

    def socket(self, name: str) -> str:
        """SPIKE: Lima's podman template forwards the guest libpod socket to
        ``<instance dir>/sock/podman.sock`` on the host. Confirm the path/forward survives
        across Lima versions before trusting it (scope doc risk #3)."""
        inst = self._instance(name)
        if inst and inst.get("dir"):
            return "unix://" + str(Path(inst["dir"]) / "sock" / "podman.sock")
        return ""

    def guest_socket(self) -> str:
        """Lima's podman template runs ROOTLESS podman, forwarding the guest socket at
        ``/run/user/<uid>/podman/podman.sock``. Lima maps the guest user's UID to the host's
        (so mount ownership lines up), so the host UID is the right ``<uid>``. Override with
        ``[box].sock_in_vm`` if you've customised the Lima ``user.uid``."""
        return f"/run/user/{os.getuid()}/podman/podman.sock"

    def list_running(self, name: str = "") -> list[str]:
        return [
            r["name"]
            for r in self._instances()
            if r.get("name") and str(r.get("status", "")).lower() == "running"
        ]

    @staticmethod
    def _memory_mib(mib: str) -> int:
        try:
            return int(mib)
        except (TypeError, ValueError):
            return 8192  # safe default, same as machine_resources()

    # Preference order when the consumer hasn't named a vmType, best-first. Both entries are
    # Lima BUILT-IN drivers, so this never silently selects something that needs a separate
    # install: `vz` (Apple Virtualization.framework) exposes a small fixed virtio set and, on a
    # Mac, means the stack runs neither QEMU nor KVM — the two components with the published
    # guest→host escape history. `qemu` is the fallback because on a Linux host Lima registers
    # nothing else. `krunkit` is deliberately ABSENT: it is upstream-experimental and needs
    # `brew install krunkit`, so it must be asked for by name, never auto-selected.
    VMTYPE_PREFERENCE = ("vz", "qemu")

    def available_vmtypes(self) -> list[str]:
        """The Lima drivers REGISTERED on this host (`limactl info`'s ``vmTypes``), including
        external plugins. Capability detection, not platform detection — foldyard carries no
        ``sys.platform`` branch, and "which hypervisors does this machine have?" is exactly the
        question, not "is this a Mac?". ``[]`` when limactl is missing or the JSON is unreadable
        (an older limactl predating the field), which callers treat as "don't pin anything"."""
        res = _run(["limactl", "info"])
        if res.returncode != 0:
            return []
        try:
            return [str(v) for v in json.loads(res.stdout).get("vmTypes", [])]
        except (ValueError, AttributeError):
            return []

    def resolve_vmtype(self) -> str:
        """The vmType to pin at create: the consumer's ``[machine].vmtype`` verbatim if they
        named one, else the best of :data:`VMTYPE_PREFERENCE` this host actually registers.

        ``""`` means "emit nothing and let Lima choose" — the pre-pinning behaviour, kept as the
        graceful degradation for a host offering neither (Windows registers only wsl2/hcs) and
        for a limactl too old to report. An explicit value is passed through UNVALIDATED on
        purpose: limactl's own error names the drivers it has, which beats foldyard second-
        guessing a list that grows with every external plugin."""
        from . import config

        return config.machine_vmtype() or next(
            (v for v in self.VMTYPE_PREFERENCE if v in self.available_vmtypes()), ""
        )

    def _set_expr(self, resources: dict, volumes: list[tuple[str, str]], vmtype: str = "") -> str:
        """A yq-style override expression for ``limactl create --set`` that pins sizing and
        REPLACES the template's mounts with ONLY foldyard's (host==guest path, writable) — so
        the VM sees nothing but the repo + worktrees root, matching podman's isolation.

        Memory is emitted in MiB VERBATIM (never converted to a rounded GiB string): a
        two-decimal GiB like ``"3.91GiB"`` is not a whole number of MiB, and macOS
        Virtualization.framework hard-rejects it at every boot with ``“memorySize” is not a
        multiple of 1 megabyte`` — bricking the created VM until it's deleted."""
        mounts = ", ".join(
            f'{{"location": {json.dumps(str(src))}, '
            f'"mountPoint": {json.dumps(str(dst))}, "writable": true}}'
            for src, dst in volumes
        )
        expr = (
            f".cpus = {int(resources['cpus'])} "
            f'| .memory = "{self._memory_mib(resources["memory"])}MiB" '
            f'| .disk = "{int(resources["disk"])}GiB" '
            f"| .mounts = [{mounts}]"
        )
        # Only when resolved: an empty `.vmType = ""` would OVERRIDE Lima's own default with a
        # value it can't parse, which is worse than not pinning.
        return f'{expr} | .vmType = "{vmtype}"' if vmtype else expr

    def create(self, name: str, resources: dict, volumes: list[tuple[str, str]]) -> bool:
        """SPIKE: create (stopped) from the podman template, overriding sizing + mounts + the
        vmType via ``--set``. Keeps the template's podman provisioning + socket forward;
        ``--tty=false`` skips the interactive review.

        The vmType is PINNED here rather than inherited from Lima's `runtime.GOOS` default,
        and printed, because it is the hypervisor the whole boundary rests on and it cannot be
        changed afterwards without recreating the VM."""
        vmtype = self.resolve_vmtype()
        if vmtype:
            _err(f"▶ hypervisor (vmType): {vmtype}")
        cmd = [
            "limactl",
            "create",
            "--tty=false",
            "--name",
            name,
            "template://podman",
            "--set",
            self._set_expr(resources, volumes, vmtype),
        ]
        return subprocess.run(cmd).returncode == 0

    def start_argv(self, name: str) -> list[str]:
        return ["limactl", "start", name]

    def stop_argv(self, name: str) -> list[str]:
        return ["limactl", "stop", name]

    def start(self, name: str) -> bool:
        if subprocess.run(self.start_argv(name)).returncode != 0:
            return False
        return self._wait_for_socket(name)

    def _wait_for_socket(self, name: str, tries: int = 30, delay: float = 1.0) -> bool:
        """SPIKE: the forwarded podman socket appears a moment after `start` returns — poll
        for it so callers can use ``socket()`` immediately (scope doc risk #3)."""
        uri = self.socket(name)
        if not uri:
            return False
        sock = Path(uri.removeprefix("unix://"))
        for _ in range(tries):
            if sock.exists():
                return True
            time.sleep(delay)
        _err(f"⚠ Lima podman socket {sock} not present after {tries}s — VM may still be booting.")
        return sock.exists()

    def remove(self, name: str) -> bool:
        return _run(["limactl", "delete", "-f", name]).returncode == 0


def get_backend(name: str) -> Backend:
    """The backend for ``[machine].backend`` (see :func:`config.machine_backend`).

    An unknown name falls back to **podman** — deliberately not to the default (lima), even
    though lima is what an absent key resolves to. A typo shouldn't turn a warning into a hard
    failure on a host that has no ``limactl``, and podman needs nothing the engine didn't already
    need. It is still a VM backend, so the isolation boundary holds; what's lost is concurrency
    and the wall, which is why the warning is loud rather than silent."""
    if name == "lima":
        return LimaBackend()
    if name == "native":
        return NativeBackend()
    if name == "podman":
        return PodmanBackend()
    _err(
        f"⚠ unknown [machine].backend '{name}' — falling back to podman (one shared VM, no "
        "in-VM wall). Fix the name to get the backend you asked for."
    )
    return PodmanBackend()


def default_unavailable_block(backend: Backend) -> str:
    """Why an INHERITED (unnamed) default backend whose CLI is missing is a hard stop, not a skip.

    It used to skip quietly — "nobody chose this backend, and this host has no VM tooling". That
    was safe only while the default was ``podman``, whose CLI is also the ENGINE CLI: without it
    nothing could reach an engine at all. With ``lima`` as the default the two binaries came
    apart, and the quiet skip became the ADR-0011 alternative that was explicitly REJECTED
    ("auto-selecting native when no VM tech is available"): ``machine.socket()`` returns ``""``,
    so ``stack`` exports neither ``DOCKER_HOST`` nor ``CONTAINER_HOST``, and every engine verb
    lands on the host's own rootless podman socket — the native profile, containers sharing the
    host kernel, with nobody having chosen it and nothing saying so.

    So: name the three ways out and let the operator pick. One block, shared by
    :func:`machine.ensure` and :mod:`preflight`, so the two can't drift."""
    return (
        f"✗ `{backend.cli}` isn't installed, and '{backend.name}' is foldyard's DEFAULT\n"
        "    [machine].backend — so there is no VM to target. Refusing to fall back to this\n"
        "    host's own podman socket: that would drop the VM boundary silently. Pick one:\n"
        f"      • install it ({backend.install_hint}) — per-project VMs + the in-VM egress wall\n"
        '      • [machine] backend = "podman"  — one shared VM, no extra CLI to install\n'
        '      • [machine] backend = "native"  — NO VM: containers share this host\'s kernel'
    )
