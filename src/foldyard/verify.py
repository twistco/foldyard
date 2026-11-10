"""Isolation battery — the product's credibility check (`foldyard verify`).

Faithful port of the `verify` recipe. Proves the VM boundary over the engine socket
(rootless, the `--privileged --pid=host` escape refused, the VM's own mount table — PID 1's —
free of host home/paths) and — INSIDE the box only — the credential-less dev-box posture (no
SSH key material, the injectors' box dummies, git push refused — by origin for want of a
credential, and by the egress proxy for being a push). Each check prints PASS/FAIL;
returns non-zero on any FAIL (CI-usable). One ADVISORY section rides along at the end —
unhealthy containers in the stack — printed as WARN so it can never move the isolation verdict.

Behaviour-preserving: same checks, same messages, same exit semantics as the recipe. The
engine probes run with the resolved stack env (so `CONTAINER_HOST`/`DOCKER_HOST` reach the
machine socket). MECHANISM-specific posture (e.g. each ``[[inject]]`` row's box dummies — no
real credential where a dummy belongs) is contributed by the plugins via
`registry().verify_checks` (ADR-0015); the credential-AGNOSTIC backstops (no ssh keys, no
netrc, git push refused) stay in core here so the credibility check can never be weakened by a
plugin being absent or broken.
"""

from __future__ import annotations

import contextlib
import http.client
import os
import re
import secrets
import socket
import ssl
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from shutil import which
from urllib.parse import urlsplit

from . import boxmounts, config, machine, stack
from .plugins import VerifyContext, registry

_PASS = "\033[32m✓ PASS\033[0m"
_FAIL = "\033[31m✗ FAIL\033[0m"
_WARN = "\033[33m⚠ WARN\033[0m"
_NA = "\033[36m⊘ N/A \033[0m"
# Where each check is explained — what it proves, why it fails, what to do. A row names WHAT
# failed and stops there (the messages must stay terse enough to scan), so the manual pointer is
# printed up front and again beside a FAIL verdict; the topic ships in the wheel (pyproject's
# force-include), so it resolves in every install, offline.
_MANUAL = "fy docs security"
_MANUAL_SECTION = "§ fy verify"
# Paths that must NEVER appear in a privileged container's mount table, built from the ACTUAL
# host rather than a hardcoded macOS list. The old regex was `/Users|/private|/var/folders|
# /Volumes` — every member macOS-only, so on a Linux or WSL2 host the leak check passed
# vacuously: green while asserting nothing. Derived, not branched: no `sys.platform` here, just
# "where does this host keep its home", plus the fixed mount points other platforms expose a
# host filesystem at.
_FOREIGN_MOUNTS = (
    "/Users",  # macOS home root
    "/Volumes",  # macOS external volumes
    "/private",  # macOS real path behind /tmp, /var
    "/var/folders",  # macOS per-user temp
    "/mnt/c",  # WSL2 Windows drive (automount)
    "/mnt/wsl",  # WSL2 cross-distro share
    "/run/host",  # systemd/flatpak-style host passthrough
    "/media",  # Linux removable media
)


# The VM's own mount table: PID 1's, in PID 1's mount namespace. Read with `--pid=host` so the
# probe container's /proc is the VM's — the container's `mount` shows only its own namespace.
_PID1_MOUNTS = "cat /proc/1/mounts"
_MOUNT_ESCAPE = re.compile(r"\\([0-7]{3})")  # /proc/mounts octal-escapes space, tab, newline, \


def _mountpoint(line: str) -> str:
    """Field 2 of a `/proc/<pid>/mounts` line, unescaped."""
    fields = line.split()
    if len(fields) < 2:
        return ""
    return _MOUNT_ESCAPE.sub(lambda m: chr(int(m.group(1), 8)), fields[1])


def _host_home() -> str:
    """The OPERATOR's home — the identity the leak pattern looks for. In-box ``Path.home()`` is
    the box user's (``/root``, ``/home/vscode``), which on a Linux host is not the home a leaked
    mount would carry (``/home/<user>``; a macOS home is caught by ``/Users`` regardless), so
    ``fy box up`` bakes the host's as ``FY_HOST_HOME`` and an in-box audit judges by that. A box
    from before the bake falls back to the process's own."""
    return os.environ.get("FY_HOST_HOME") or str(Path.home())


def _host_paths() -> re.Pattern[str]:
    """The leak pattern for THIS host. The host's own home is matched exactly — followed by a
    separator, whitespace or end — so a guest home that merely shares its prefix does not trip
    it: Lima names the guest user ``<user>.linux``, so ``/home/dain`` must not match
    ``/home/dain.linux``."""
    home = re.escape(_host_home())
    return re.compile("|".join((rf"{home}(?=/|\s|$)", *(re.escape(m) for m in _FOREIGN_MOUNTS))))


class _Report:
    def __init__(self) -> None:
        self.fails = 0
        self.warns = 0

    def ok(self, msg: str) -> None:
        print(f"  {_PASS}  {msg}")

    def bad(self, msg: str) -> None:
        print(f"  {_FAIL}  {msg}")
        self.fails += 1

    def warn(self, msg: str) -> None:
        """Advisory only — never touches the exit code. `verify`'s verdict is about ISOLATION;
        a consumer's service being sick is worth saying out loud but isn't a breach."""
        print(f"  {_WARN}  {msg}")
        self.warns += 1

    def info(self, msg: str) -> None:
        """Worth seeing, nothing to judge: no exit effect and not a warning."""
        print(f"  ℹ  {msg}")

    def na(self, msg: str) -> None:
        """Not applicable HERE — the check is fulfilled at another layer and there is nothing to
        act on in this context. Neutral: no exit effect and not a warning (unlike `warn`, which
        flags something worth a human's attention). Use only where another layer genuinely
        performs the check, never to soften a gap nothing covers."""
        print(f"  {_NA}  {msg}")


def _run(cmd: list[str], env: dict, *, capture: bool = False) -> subprocess.CompletedProcess:
    """Run a probe. Inherit nothing to stdout unless capturing — the recipe swallows probe
    output and only prints PASS/FAIL."""
    if capture:
        return subprocess.run(cmd, env=env, capture_output=True, text=True)
    return subprocess.run(cmd, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def _engine_run_succeeds(engine: str, env: dict, args: list[str]) -> bool:
    """`<engine> run --rm <args>` exited 0? (A *successful* escape probe is a FAIL.)"""
    return _run([engine, "run", "--rm", *args], env).returncode == 0


def _probe_runs(engine: str, env: dict, probe: str) -> bool:
    """POSITIVE CONTROL for every absence-check below.

    Those checks report PASS when their probe command FAILS, and :func:`_engine_run_succeeds`
    cannot tell "the escape was refused" from "the container never started". So a probe image
    that cannot run turns the whole battery green while proving nothing — and that is not an
    edge case: the probe is pulled from a registry, and ``[machine] firewall = true`` (what
    ``fy init`` scaffolds) denies egress unless the host supervisor is up, so a cold image cache
    makes the pull fail exactly when the posture is most locked down. Run something trivial
    first; if that cannot run, no absence below is evidence of anything."""
    return _engine_run_succeeds(engine, env, [probe, "true"])


# The push probe: a DRY-RUN push to origin. A read (`ls-remote`) proves nothing — a public origin
# answers it with no credential — but the push handshake needs write access on any repo, and
# --dry-run sends nothing. Its failure proves "no credential reaches origin" ONLY if it failed for
# credential reasons: a box with no egress fails it too, which would certify the product's
# headline claim ("git push is impossible from inside") from a network outage.
_PUSH_PROBE = "refs/heads/fy-verify-push-probe"
# Before the first commit there is no HEAD to push, and git refuses that locally without asking
# origin; any object id reaches the auth handshake, so the empty tree's stands in.
_EMPTY_TREE = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"
# git could not even START the SSH transport: no client in the box. With no keys and no agent
# (asserted beside it) that absence is the refusal — no credential can travel a transport that
# does not exist — not an "unreachable for network reasons" that leaves the claim unproven.
# The env an editor attach sets so the box's git asks the HOST for credentials — a push path over
# HTTPS the same way a forwarded agent is over SSH. And the sockets it drops, which are reachable
# by path regardless of the env (below). The workspace settings that turn VS Code's credential
# paths back on: the git extension's askpass, and the GitHub extension's silent session token —
# `fy code` pins them off at user + machine scope (vscode._GIT_BRIDGE_PINS — keep the two in step),
# but `.vscode/settings.json` outranks both.
_GIT_BRIDGE_VARS = ("GIT_ASKPASS", "VSCODE_GIT_IPC_HANDLE", "VSCODE_GIT_ASKPASS_MAIN")
_GIT_BRIDGE_SETTINGS = (
    "git.terminalAuthentication",
    "git.useIntegratedAskPass",
    "github.gitAuthentication",
)


# The egress proxy refuses a git push by its URL (egress_proxy._is_git_push), on any host it
# decrypts, except a host a push-capable credential is injected on now (the emergency gh-cli
# switch: api.github.com, so a push to github.com stays refused even then). The probe sends what
# git sends first — the push's discovery request — through the box's proxy to a repo that needn't
# exist: refused, nothing leaves.
_PUSH_PROBE_HOST = "github.com"
_PUSH_PROBE_PORT = 443
_PUSH_PROBE_PATH = "/foldyard/fy-verify-push-probe.git/info/refs?service=git-receive-pack"
# The start of the proxy's refusal body (egress_proxy._PUSH_REFUSED_BODY), which git prints as a
# `remote:` line, and the header the proxy sets on a push a mode let through
# (egress_proxy._PUSH_HEADER). A test pins both to the addon's.
_PUSH_REFUSED = "refused by the foldyard proxy - the box doesn't push"
_PUSH_ALLOWED_HEADER = "x-foldyard-push"
# The issuer of every certificate the proxy makes: present ⇒ the proxy decrypted the connection
# (and so saw the request); absent ⇒ a blind tunnel (`[proxy] passthrough`) with the real one.
_PROXY_ISSUER = "mitmproxy"


@dataclass(frozen=True)
class _PushAnswer:
    status: int
    body: bytes  # its first bytes
    allowed: bool  # the proxy's word that a push-capable mode let it through
    decrypted: bool  # the proxy's certificate: it saw the request


def _workspace_rearms_git_bridge(settings: Path) -> list[str]:
    """The git-bridge settings a workspace ``settings.json`` sets to ``true``. A textual match, not
    a parse: the file is JSONC (comments, trailing commas) and an unparseable one must not read
    as "off"."""
    try:
        text = settings.read_text()
    except OSError:
        return []
    return [k for k in _GIT_BRIDGE_SETTINGS if re.search(rf'"{re.escape(k)}"\s*:\s*true\b', text)]


# The attach's sockets: the SSH agent, the git askpass IPC, Dev Containers' own IPC (its host side
# runs `git credential fill` and the docker credential helpers for any caller) and the `code` CLI's
# IPC (drives the host's VS Code — URL opens too, around the proxy). The same names box.py's reaper
# unlinks (box._BRIDGE_SOCKET_GLOBS; a test pins the two equal), in the same places: /tmp, the
# per-user /tmp/user/<uid>/, $XDG_RUNTIME_DIR, $TMPDIR.
_BRIDGE_SOCKET_DIR = Path("/tmp")
_BRIDGE_SOCKET_GLOBS = (
    "vscode-ssh-auth-*.sock",
    "vscode-git-*.sock",
    "vscode-remote-containers-ipc-*.sock",
    "vscode-ipc-*.sock",
)


def _bridge_socket_dirs() -> list[Path]:
    """Every directory an attach's bridge socket can land in, as the box's own env names them."""
    dirs = [_BRIDGE_SOCKET_DIR, *sorted(_BRIDGE_SOCKET_DIR.glob("user/*"))]
    dirs += [Path(v) for k in ("XDG_RUNTIME_DIR", "TMPDIR") if (v := os.environ.get(k))]
    return list(dict.fromkeys(dirs))  # $TMPDIR is often /tmp itself: read it once


# A git credential helper that asks the HOST: what Dev Containers writes into the box's git config
# (`!f() { <node> /tmp/vscode-remote-containers-<id>.js git-credential-helper $*; }; f`) unless
# dev.containers.gitCredentialHelperConfigLocation is "none". Any key shape (`credential.helper`,
# `credential.<url>.helper`), matched on the script's name or on its subcommand.
_HOST_CRED_HELPER_KEYS = r"^credential\..*helper$"
_HOST_CRED_HELPER = re.compile(r"vscode-remote-containers|git-credential-helper")
_GIT_NO_SSH = re.compile(r"cannot run ssh|ssh: (command )?not found", re.I)
_GIT_NO_ORIGIN = re.compile(r"'origin' does not appear to be a git repository", re.I)
_GIT_AUTH_DENIED = re.compile(
    r"permission denied|authentication failed|could not read username|could not read password|"
    r"terminal prompts disabled|invalid username or password|access denied|403|401",
    re.I,
)


def _rootless(engine: str, env: dict) -> bool:
    rl = _run([engine, "info", "--format", "{{.Host.Security.Rootless}}"], env, capture=True)
    return rl.returncode == 0 and "true" in rl.stdout.lower()


def _vm_boundary(
    rep: _Report,
    engine: str,
    env: dict,
    probe: str,
    allowed: list[str] | None = None,
    to_box: str = "",
    to_box_expected: bool = False,
) -> None:
    socket = env.get("CONTAINER_HOST") or env.get("DOCKER_HOST", "")
    print(f"▶ VM boundary (engine: {engine}, over the socket: {socket})")

    if _rootless(engine, env):
        rep.ok("engine is rootless")
    else:
        rep.bad("engine is NOT reported rootless — container-root could be VM-root")

    # Everything below asserts an ABSENCE and so passes when its probe fails. Without this
    # control a probe image that cannot run reports a perfect score (see :func:`_probe_runs`).
    if not _probe_runs(engine, env, probe):
        rep.bad(
            f"probe image '{probe}' could not run — the boundary battery DID NOT EXECUTE, so "
            "nothing below is proven. (Behind the VM firewall? Start the host supervisor, or "
            f"pre-pull the image / set VERIFY_IMG to one already in the VM.)"
        )
        return

    if _engine_run_succeeds(
        engine, env, ["--privileged", "--pid=host", probe, "sh", "-c", "cat /proc/1/ns/ipc"]
    ):
        rep.bad("host PID1 namespace READABLE from --privileged --pid=host (breakout!)")
    else:
        rep.ok("escape refused (--privileged --pid=host can't read host PID1 ns)")

    # The VM's mount table is PID 1's, read through the host PID namespace: `mount` (or
    # `ls /Users`) inside a --privileged container shows the CONTAINER's mount namespace, in
    # which a VM-level mount of the operator's whole home never appears — a Lima VM mounting all
    # of `$HOME` passed the old probe (2026-09-11, docs/archive/verify-false-pass.md).
    # `/proc/1/mounts` is world-readable, so this coexists with the escape probe above, which
    # relies on `/proc/1/ns/*` being unreadable. An EMPTY table is not a clean one: nothing
    # printed means the probe did not run, and grepping no lines for host paths finds none of them.
    mp = _run(
        [engine, "run", "--rm", "--privileged", "--pid=host", probe, "sh", "-c", _PID1_MOUNTS],
        env,
        capture=True,
    )
    if mp.returncode != 0 or not mp.stdout.strip():
        # Under the gVisor posture the box CANNOT read the VM's PID-1 mounts: `--pid=host` reaches
        # only the sandbox, not the VM kernel — the SAME reach "escape refused" above proves is
        # blocked. This is NOT APPLICABLE here rather than advisory: there is nothing to act on
        # in-box, and the audit is genuinely fulfilled at another layer — host-side `fy verify`
        # runs its probe over the DEFAULT (crun) socket, whose --pid=host container CAN read the
        # VM's PID-1 mounts, as can a crun box. So it is neither a FAIL nor a silent pass: the
        # check runs, just not from inside the sandbox.
        if os.environ.get("FY_MACHINE_RUNTIME") == "gvisor":
            rep.na(
                "VM mount audit runs at another layer, not inside a gVisor box (the sandbox "
                "blocks --pid=host into the VM — see 'escape refused'); run `fy verify` on the "
                "host, or from a crun box, to audit the VM mount boundary"
            )
            if to_box:  # the probe container would run in the sandbox, not on the VM's mount
                rep.na(
                    "to-box write probe runs at another layer, not inside a gVisor box (its "
                    "container runs in the sandbox); `fy verify` on the host probes the VM's mount"
                )
        else:
            rep.bad(
                "could not read the VM mount table (probe produced nothing) — leak check UNPROVEN"
            )
        return
    # The real table carries the mounts foldyard itself makes — the repo and the worktrees root,
    # at their host paths (`machine.guest_mounts`). Exempt by EXACT mountpoint only: the home
    # itself, a sibling under it, or a nested bind at a sub-path are all still leaks. Judge the
    # MOUNTPOINT field, not the whole line: the options field can carry a path-shaped string
    # that exposes nothing — a Fedora guest's btrfs root is `/ … subvol=/root`, and in the box
    # (uid 0) `/root` IS the home this pattern looks for (Linux rig, 2026-09-13).
    exempt = set(allowed or ())
    host = [
        ln
        for ln in mp.stdout.splitlines()
        if (mnt := _mountpoint(ln)) not in exempt and _host_paths().search(mnt)
    ]
    if not host:
        rep.ok("VM mount table (PID 1's namespace) free of host home/paths beyond the repo mounts")
    else:
        rep.bad(f"VM exposes host paths: {host[0]}{_predates_box_dirs_hint(host)}")
    if to_box:
        _to_box_rows(rep, engine, env, probe, mp.stdout, to_box, to_box_expected)


def _predates_box_dirs_hint(leaks: list[str]) -> str:
    """A box created before ADR-0033's to-box and from-box has neither path baked
    (:func:`boxmounts.vm_paths`), so once its VM shares them they aren't exempt here. They stay a
    FAIL — nothing is exempted by guess — but one that names the likely cause and the fix."""
    if not config.in_box() or any(boxmounts.vm_paths()):
        return ""
    if all(_mountpoint(ln).endswith(("/to-box", "/from-box")) for ln in leaks):
        return (
            " — if that is foldyard's to-box/from-box, this box was created before them and "
            "doesn't know their paths: `fy box down && fy box up`"
        )
    return ""


# Where the write probe binds the to-box. A fixed path in the probe container's own namespace.
_TO_BOX_AT = "/fy-to-box"


def _to_box_rows(
    rep: _Report, engine: str, env: dict, probe: str, table: str, to_box: str, expected: bool
) -> None:
    """ADR-0033: the to-box is READ-ONLY to everything in the VM. Two rows, read and probed from the
    VM's side (the host-side enforcement against guest ROOT is a host-tier e2e, not a verify row —
    the box never has root in the guest):

    1. PID 1's mount table carries the to-box with ``ro``. The guest's own flag — necessary, not
       sufficient: root in the guest can drop it while the host still refuses every write (the vz
       test), so ``rw`` here is a WARN that something remounted it, never the verdict.
    2. A ``--privileged`` container that binds the to-box READ-WRITE can't create a file in it,
       even after trying ``mount -o remount,rw``. The verdict is an actual WRITE, never the flag:
       a guest can show ``rw`` and still be refused, and the refusal's errno varies (EROFS while
       the guest flag is set, EPERM/EACCES from the host once it isn't) — any refusal counts. A
       write that lands is a FAIL naming the file it made."""
    line = next((ln for ln in table.splitlines() if _mountpoint(ln) == to_box), "")
    if not line:
        if expected:
            rep.warn(
                f"the to-box ({to_box}) is not in the VM's mount table — expected there; "
                "`fy machine stop && fy up` re-records the VM's mount set"
            )
        else:
            print(
                f"  ℹ no to-box in this VM ({to_box}) — nothing to check (Lima vz or qemu only; "
                "the podman backend has none)"
            )
        return
    fields = line.split()
    options = fields[3].split(",") if len(fields) > 3 else []
    if "ro" in options:
        rep.ok("to-box mounted read-only in the VM (the guest's own flag)")
    else:
        rep.warn(
            f"to-box shows {'rw' if 'rw' in options else 'no ro'} in the VM's mount table — "
            "something in the guest remounted it; the write probe below is the verdict"
        )
    name = f".fy-verify-{secrets.token_hex(4)}"
    target = f"{_TO_BOX_AT}/{name}"
    script = (
        f"if mount -o remount,rw {_TO_BOX_AT} 2>/dev/null; then echo remount=done; "
        "else echo remount=refused; fi; "
        f'if (: >"{target}") 2>/dev/null; then echo write=LANDED; '
        f'rm -f "{target}" 2>/dev/null && echo removed=yes; else echo write=refused; fi; true'
    )
    got = _run(
        [
            engine, "run", "--rm", "--privileged", "-v", f"{to_box}:{_TO_BOX_AT}",
            probe, "sh", "-c", script,
        ],
        env,
        capture=True,
    )  # fmt: skip
    out = got.stdout or ""  # judged by what it printed: a failed cleanup must not hide a write
    if "write=" not in out:
        rep.bad("the to-box write probe did not run — read-only UNPROVEN")
    elif "write=LANDED" in out:
        left = "removed again" if "removed=yes" in out else "LEFT there"
        rep.bad(
            f"a --privileged container binding the to-box read-write CREATED {to_box}/{name} "
            f"({left}) — the to-box is writable from the VM"
        )
    else:
        tried = " (its remount to rw returned 0; the write was still refused)"
        rep.ok(
            "to-box refuses a write from a --privileged container binding it read-write"
            + (tried if "remount=done" in out else "")
        )


def _to_box_in_box(rep: _Report) -> None:
    """In the box: the to-box bind (``:ro``) refuses a write. Defence in depth — the boundary is
    the VM mount below it, which the rows above probe."""
    if not os.environ.get(boxmounts.ENV_TO_BOX):
        return
    here = Path(boxmounts.TO_BOX_IN_BOX)
    if not here.is_dir():
        rep.warn(f"{here} is missing — this box was created with a to-box; recreate it")
        return
    probe = here / f".fy-verify-{secrets.token_hex(4)}"
    try:
        fd = os.open(probe, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except OSError:  # EROFS, EPERM or EACCES: any refusal is the claim
        rep.ok(f"{here} refuses a write (the box's read-only bind)")
        return
    os.close(fd)
    try:
        probe.unlink()
        left = "removed again"
    except OSError:
        left = "LEFT there"
    rep.bad(f"{here} took a write ({probe}, {left}) — the box's to-box bind is writable")


def _plugin_posture(rep: _Report, env: dict) -> None:
    """Mechanism-specific posture from the plugins (ADR-0015): each contributes mode-aware
    assertions for its own credential mechanism (e.g. the ``[[inject]]`` dummies' rows).
    The credential-AGNOSTIC backstops around this call — no ssh keys, no netrc, git push
    refused — stay in core and must never depend on a plugin being present."""
    ctx = VerifyContext(in_box=True, env=env, which=lambda c: which(c) is not None)
    for status, msg in registry().verify_checks(ctx):
        if status == "pass":
            rep.ok(msg)
        elif status == "fail":
            rep.bad(msg)
        else:  # "info" — a non-pass/fail annotation (e.g. the emergency-mode banner)
            print(f"  {msg}")


def _kernel_release() -> str:
    return os.uname().release


def _host_credential_helper(rep: _Report, checkout: Path) -> None:
    """No git credential helper the box's git would run asks the HOST. Read the way that git
    reads it — in the checkout (local + worktree config), no scope flag (system, global, XDG), the
    box's own env (``GIT_CONFIG_*``) — so every scope it would consult is covered. Not covered by
    the push probe: that runs with ``REMOTE_CONTAINERS_IPC`` unset (harden.sh scrubs it), so the
    helper fails there and the push reads as refused — yet any process can point the variable
    back at Dev Containers' socket while one exists. The entry is the arming; this row finds it."""
    try:
        got = subprocess.run(
            [
                "git", "-C", str(checkout), "config", "--show-origin",
                "--get-regexp", _HOST_CRED_HELPER_KEYS,
            ],
            env=dict(os.environ),
            capture_output=True,
            text=True,
            timeout=12,
        )  # fmt: skip
    except subprocess.TimeoutExpired:
        rep.bad("git credential helpers UNPROVEN — reading the box's git config timed out")
        return
    if got.returncode not in (0, 1):  # 1 = no such key; anything else read nothing
        why = (got.stderr or "").strip().splitlines()
        rep.bad(
            "git credential helpers UNPROVEN — `git config` could not read the box's config "
            f"({why[-1] if why else f'exit {got.returncode}'})"
        )
        return
    hits = [ln for ln in (got.stdout or "").splitlines() if _HOST_CRED_HELPER.search(ln)]
    if not hits:
        rep.ok("no git credential helper asks the host (credential.*helper)")
        return
    origins = sorted({ln.split("\t", 1)[0].removeprefix("file:") for ln in hits})
    rep.bad(
        f"git has a credential helper that asks the host, in {' '.join(origins)} — Dev Containers "
        'writes it on attach unless dev.containers.gitCredentialHelperConfigLocation is "none" '
        "(`fy code` pins it); remove it: git config --file <that file> --unset-all "
        "credential.helper vscode-remote-containers"
    )


def _box_posture(rep: _Report, env: dict) -> None:
    print("▶ dev-box checks (credential-less: read+commit, never push)")
    home = Path(os.environ.get("HOME") or str(Path.home()))

    # [machine].runtime = "gvisor" bakes FY_MACHINE_RUNTIME into the box at create; the claim is
    # checked against the kernel the box ACTUALLY runs on (gVisor's Sentry reports its own).
    if os.environ.get("FY_MACHINE_RUNTIME") == "gvisor":
        kernel = _kernel_release()
        if "gvisor" in kernel.lower():
            rep.ok(f"box runs under gVisor (kernel {kernel})")
        else:
            rep.bad(
                f"box is NOT under gVisor — kernel {kernel} is the VM's "
                "([machine].runtime not applied)"
            )

    # The editor attach's host bridges (docs/security.md § fy verify). VS Code's Dev Containers
    # attach forwards the host's SSH agent and its git-credential store into the box and sets the
    # vars in every terminal it opens; foldyard's bootstrap unsets them in every box shell and
    # reaps the sockets (box._HARDEN_SNIPPET), so a hit here is that hygiene NOT running — a
    # shell that isn't bash, a box created before it shipped — or an agent from somewhere else.
    if not os.environ.get("SSH_AUTH_SOCK"):
        rep.ok("no SSH agent forwarded (SSH_AUTH_SOCK unset)")
    else:
        rep.bad("SSH_AUTH_SOCK set — an SSH agent is reachable (an editor attach forwards one)")
    bridged = [v for v in _GIT_BRIDGE_VARS if os.environ.get(v)]
    if not bridged:
        rep.ok("no git-credential bridge to the host (GIT_ASKPASS unset)")
    else:
        rep.bad(f"git-credential bridge to the host: {' '.join(bridged)} set (an editor attach's)")
    checkout_dir = Path(env.get("FOLDYARD_CHECKOUT") or str(stack.main_repo()))
    _host_credential_helper(rep, checkout_dir)
    # The sockets themselves, not just the vars: a socket left behind is usable by PATH, so an
    # unset var is hygiene and a missing socket is the boundary.
    dirs = _bridge_socket_dirs()
    socks = sorted(str(p) for d in dirs for g in _BRIDGE_SOCKET_GLOBS for p in d.glob(g))
    where = ", ".join(str(d) for d in dirs)
    if not socks:
        rep.ok(f"no editor-attach bridge sockets ({where})")
    else:
        rep.bad(
            f"editor-attach bridge sockets: {' '.join(socks)} — usable by path even with the "
            "vars unset (foldyard's reaper removes these; a box shell restarts it)"
        )

    # `fy code` pins VS Code's credential paths OFF (git.terminalAuthentication /
    # useIntegratedAskPass, github.gitAuthentication) at user + machine scope — but WORKSPACE
    # settings win, and `.vscode/settings.json` is mount data the box can write. A checkout
    # flipping one back on is asking VS Code to hand its git a host credential: the in-box unset +
    # reaper still neutralise it (with their window), so this is the row that says the mount is
    # trying.
    rearmed = _workspace_rearms_git_bridge(checkout_dir / ".vscode" / "settings.json")
    if not rearmed:
        rep.ok("workspace settings leave the git-credential bridge off")
    else:
        rep.bad(
            f"workspace settings re-enable the git-credential bridge: {' '.join(rearmed)} = true "
            f"in {checkout_dir / '.vscode' / 'settings.json'} — mount data overriding `fy code`'s "
            "pin; remove it (the in-box unset + reaper are all that stand between git and a host "
            "credential)"
        )

    # Only PRIVATE KEY material is a push path — known_hosts*/config are harmless.
    ssh = home / ".ssh"
    keys = (
        sorted(p.name for p in ssh.iterdir() if not re.fullmatch(r"known_hosts.*|config", p.name))
        if ssh.is_dir()
        else []
    )
    if not keys:
        rep.ok("no ~/.ssh private keys (known_hosts/config only)")
    else:
        rep.bad(f"~/.ssh key material: {' '.join(keys)}")

    _plugin_posture(rep, env)
    _to_box_in_box(rep)

    if not (home / ".netrc").exists():
        rep.ok("no ~/.netrc")
    else:
        rep.bad("~/.netrc present")

    checkout = env.get("FOLDYARD_CHECKOUT") or str(stack.main_repo())
    # The box's OWN env, as every other check here reads it: the claim is about what a process in
    # the box can do, and that process has the box's proxy + CA bundle. `env` is the compose
    # client's (stack._host_passthrough — no proxy since 4925b8b), so git with it went direct and
    # a walled box reported every origin UNREACHABLE instead of proving the refusal.
    git_env = {
        **os.environ,
        "GIT_SSH_COMMAND": "ssh -o BatchMode=yes -o StrictHostKeyChecking=no -o ConnectTimeout=6",
        "GIT_TERMINAL_PROMPT": "0",
        # GIT_ASKPASS deliberately NOT blanked: an empty one disables every askpass helper
        # (core.askPass included), which is a route a credential reaches the box's git by — the
        # probe must see it. A helper that hangs hits the timeout: UNREACHABLE, a FAIL.
    }
    try:
        has_head = (
            subprocess.run(
                ["git", "-C", checkout, "rev-parse", "--verify", "-q", "HEAD^{commit}"],
                env=git_env,
                capture_output=True,
                timeout=12,  # box-side git goes through the index shim, which can stall
            ).returncode
            == 0
        )
    except subprocess.TimeoutExpired:
        rep.bad("push refusal UNPROVEN — reading the checkout's HEAD timed out, nothing was probed")
        return
    source = "HEAD" if has_head else _EMPTY_TREE
    try:
        got = subprocess.run(
            ["git", "-C", checkout, "push", "--dry-run", "origin", f"{source}:{_PUSH_PROBE}"],
            env=git_env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
            timeout=12,
        )
        reachable, why = got.returncode == 0, got.stderr or ""
    except subprocess.TimeoutExpired:
        reachable, why = False, "timed out"
    if reachable:
        rep.bad("a dry-run push to origin was ACCEPTED — the box can push (credential leaked?)")
    elif _PUSH_REFUSED in why:
        # Refused by the egress proxy before origin was asked, so git never offered a credential:
        # whether one would reach origin is asked of git directly instead.
        _origin_credential(rep, checkout, git_env)
    elif _GIT_AUTH_DENIED.search(why):
        rep.ok("git push refused (no credential reaches origin)")
    elif _GIT_NO_SSH.search(why):
        rep.ok("git push refused (SSH origin, and the box has no ssh client — no transport)")
    elif _GIT_NO_ORIGIN.search(why):
        # Not a network fact either: the checkout has no `origin` to refuse. The shared
        # .git/config losing its remotes (issue #6) surfaced here first, read as an egress problem.
        rep.bad(
            "no `origin` remote in the checkout — push refusal UNPROVEN "
            "(the shared .git/config lost its remotes? `fy doctor` → shared git config)"
        )
    elif _walled() and (walled := _ssh_push_walled(checkout, git_env)):
        proven, msg = walled
        (rep.ok if proven else rep.warn)(msg)
    else:
        # It failed, but not because a credential was refused — origin was never reached. That
        # is a network fact, not a posture one, and must not certify the credential-less claim.
        rep.bad(
            "git remote UNREACHABLE for non-credential reasons — push refusal UNPROVEN "
            f"({_git_reason(why)})"
        )


def _git_reason(stderr: str) -> str:
    """The line of a failed push's stderr that says why: ssh's own complaint when there is one,
    since git closes an SSH failure with boilerplate ("…and the repository exists.")."""
    lines = [line for line in stderr.strip().splitlines() if line.strip()]
    return next(
        (line for line in lines if line.startswith("ssh:")),
        lines[-1] if lines else "no error output",
    )


def _walled() -> bool:
    return config.machine_backend() == "lima" and config.machine_wall()


_SSH_URL = re.compile(
    r"(?:ssh|git\+ssh|ssh\+git)://(?:(?P<user>[^@/]*)@)?(?P<host>\[[^\]/]+\]|[^:/\[\]]+)"
    r"(?::(?P<port>\d*))?(?:/.*)?",
    re.I,
)
# git's scp-like form: no slash before the first colon. Bracketed hosts are left unparsed.
_SCP_LIKE = re.compile(r"(?:(?P<user>[^@/:\[\]]+)@)?(?P<host>[^@/:\[\]]+):.*")


def _ssh_destination(url: str) -> tuple[str, str | None] | None:
    """The ssh destination (``[user@]host``) and port a git URL names, or None for any other
    transport (or an SSH form this doesn't read, which therefore never proves anything)."""
    if m := _SSH_URL.fullmatch(url):
        host = m["host"].strip("[]")
    elif "://" not in url and (m := _SCP_LIKE.fullmatch(url)):
        host = m["host"]
    else:
        return None
    dest = f"{m['user']}@{host}" if m["user"] else host
    return dest, (m.groupdict().get("port") or None)


def _ssh_target(dest: str, port: str | None, git_env: dict) -> tuple[str, int] | None:
    """The host and port ssh itself would dial for ``dest`` (its config applied: ``Host``
    aliases, ``HostName``, ``Port``), or None when ssh won't say or would go through a
    ProxyCommand/ProxyJump — a refused direct connect proves nothing about those."""
    try:
        got = subprocess.run(
            ["ssh", "-G", *(["-p", port] if port else []), "--", dest],
            env=git_env,
            capture_output=True,
            text=True,
            timeout=12,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if got.returncode != 0:
        return None
    cfg = {k: v for k, _, v in (line.partition(" ") for line in (got.stdout or "").splitlines())}
    if any(cfg.get(k, "none").strip().lower() != "none" for k in ("proxycommand", "proxyjump")):
        return None
    host, ssh_port = cfg.get("hostname", "").strip(), cfg.get("port", "").strip()
    return (host, int(ssh_port)) if host and ssh_port.isdigit() else None


def _ssh_push_walled(checkout: str, git_env: dict) -> tuple[bool, str] | None:
    """With the VM firewall on, an SSH origin's push dies at the TCP connect: ssh doesn't use the
    proxy, and direct egress is refused. When every push URL's connect fails (its host resolves,
    the proxy answers): (True, PASS row) if all were refused, a reset being the wall's signature;
    (False, WARN row) if any failed otherwise (a timeout, an upstream ICMP "no route"), which a
    network turning ssh's port away looks like too. None otherwise."""
    try:
        got = subprocess.run(
            ["git", "-C", checkout, "remote", "get-url", "--push", "--all", "origin"],
            env=git_env,
            capture_output=True,
            text=True,
            timeout=12,
        )
    except subprocess.TimeoutExpired:
        return None
    urls = (got.stdout or "").split() if got.returncode == 0 else []
    dests = [d for u in urls if (d := _ssh_destination(u))]
    if not dests or len(dests) != len(urls) or not _permitted_path_open():
        return None
    refused: list[str] = []
    unproven: list[str] = []
    for dest, port in dests:
        target = _ssh_target(dest, port, git_env)
        if target is None:
            return None
        host, ssh_port = target
        try:
            socket.getaddrinfo(host, ssh_port, type=socket.SOCK_STREAM)
        except OSError:  # a DNS failure is not the wall
            return None
        where = f"[{host}]:{ssh_port}" if ":" in host else f"{host}:{ssh_port}"
        try:
            with socket.create_connection((host, ssh_port), timeout=5):
                return None
        except OSError as e:
            reason = "timed out" if isinstance(e, TimeoutError) else None
            reason = reason or e.strerror or str(e) or type(e).__name__
            (refused if isinstance(e, ConnectionRefusedError) else unproven).append(
                f"{where} ({reason})"
            )
    if unproven:
        return False, (
            f"SSH push blocked: {', '.join(unproven)}, not a refusal, so this is not proof the "
            "VM firewall did it (a network turning that port away upstream looks the same)"
        )
    return True, f"SSH push impossible: the VM firewall refuses {', '.join(refused)}"


def _permitted_path_open() -> bool:
    """The box's proxy accepts a connection: the wall's one way out, so a refusal elsewhere is
    the wall's and not an outage's."""
    try:
        host, port = _proxy_address(
            os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy") or ""
        )
        with socket.create_connection((host, port), timeout=5):
            return True
    except (OSError, ValueError):
        return False


def _origin_credential(rep: _Report, checkout: str, git_env: dict) -> None:
    """The proxy refused the dry-run push for being a push — its first request, before origin
    could answer 401 and git could look for a credential to send — so the push probe no longer
    says whether one would have reached origin. Ask git what it would have sent: ``git credential
    fill`` for origin's push URL, with the probe's env (prompts off, any askpass still consulted).
    That finds a helper's or stored credential, one embedded in the URL, and an askpass bridge.
    A credential found FAILS: the proxy's refusal is defence in depth, not a reason for git to
    hold one. Never printed."""
    try:
        got = subprocess.run(
            ["git", "-C", checkout, "remote", "get-url", "--push", "origin"],
            env=git_env,
            capture_output=True,
            text=True,
            timeout=12,
        )
        url = (got.stdout or "").strip() if got.returncode == 0 else ""
    except subprocess.TimeoutExpired:
        url = ""
    if not url:
        rep.bad(
            "git push refused by the foldyard proxy — but origin's push URL couldn't be read, so "
            "whether git holds a credential for it is UNPROVEN"
        )
        return
    try:
        got = subprocess.run(
            ["git", "-C", checkout, "credential", "fill"],
            input=f"url={url}\n\n",
            env=git_env,
            capture_output=True,
            text=True,
            timeout=12,
        )
    except subprocess.TimeoutExpired:
        rep.bad(
            "git push refused by the foldyard proxy — `git credential fill` for origin timed out "
            "(a helper that hangs?), so a credential for origin is UNPROVEN"
        )
        return
    if got.returncode == 0 and re.search(r"^password=.", got.stdout or "", re.M):
        rep.bad(
            "git push refused by the foldyard proxy, but git HOLDS a credential for origin "
            "(`git credential fill` returned one: a helper, a stored credential or one in the "
            "remote's URL) — remove it; the refusal is a backstop, not the boundary"
        )
    elif _GIT_AUTH_DENIED.search(got.stderr or ""):
        rep.ok("git push refused by the foldyard proxy, and git has no credential for origin")
    else:
        why = (got.stderr or "").strip().splitlines()
        rep.bad(
            "git push refused by the foldyard proxy — `git credential fill` failed for another "
            f"reason, so a credential for origin is UNPROVEN ({why[-1] if why else 'no output'})"
        )


def _probe_push(proxy_host: str, proxy_port: int) -> _PushAnswer:
    """Send a git push's discovery request to ``_PUSH_PROBE_HOST`` through the box's proxy, as
    git would: CONNECT, then TLS trusting the box's bundle (``SSL_CERT_FILE``: the proxy CA plus
    the system roots, so a passthrough host's real certificate verifies too). Raises on no
    answer."""
    context = ssl.create_default_context(cafile=os.environ.get("SSL_CERT_FILE") or None)
    conn = http.client.HTTPSConnection(proxy_host, proxy_port, timeout=12, context=context)
    try:
        conn.set_tunnel(_PUSH_PROBE_HOST, _PUSH_PROBE_PORT)
        conn.request(
            "GET", _PUSH_PROBE_PATH, headers={"User-Agent": "git/fy-verify", "Accept": "*/*"}
        )
        # The certificate now, while the socket is open: http.client drops it once a response
        # that closes the connection has been read (an upstream's, typically).
        cert = conn.sock.getpeercert() if conn.sock is not None else None
        issuer = {v for rdn in (cert or {}).get("issuer", ()) for _k, v in rdn}
        resp = conn.getresponse()
        body = resp.read(512)
        return _PushAnswer(
            status=resp.status,
            body=body,
            allowed=resp.getheader(_PUSH_ALLOWED_HEADER) is not None,
            decrypted=_PROXY_ISSUER in issuer,
        )
    finally:
        conn.close()


# The schemes an HTTPS_PROXY may name, and the port each implies when the URL names none. The probe
# speaks plain CONNECT, as to the box's own proxy; an https:// one that wants TLS first gets no
# answer, which the row reports as UNPROVEN rather than passing.
_PROXY_DEFAULT_PORTS = {"http": 80, "https": 443}


def _redacted(value: str) -> str:
    """``value`` with any userinfo cut: an HTTPS_PROXY can carry ``user:password@``. Everything up
    to the LAST ``@`` goes (a password may hold one), but the scheme stays: it's the shape."""
    if "@" not in value:
        return value
    scheme, sep, _rest = value.partition("://")
    return f"{scheme if sep else ''}{sep}…@{value.rpartition('@')[2]}"


def _proxy_address(value: str) -> tuple[str, int]:
    """The (host, port) an ``HTTPS_PROXY`` value names: a bracketed IPv6 literal unwrapped, a
    missing port the scheme's default, a missing scheme ``http`` (as curl reads one). Raises
    ``ValueError`` naming the value's shape — userinfo cut — for anything the probe can't use."""
    url = value.strip()
    try:
        parts = urlsplit(url if "://" in url else f"http://{url}")  # raises on a broken bracket
        port = parts.port  # …and here on a port that isn't a number, or isn't a port
        host = parts.hostname
    except ValueError:
        raise ValueError(_redacted(url)) from None
    scheme = parts.scheme.lower()
    if not host or scheme not in _PROXY_DEFAULT_PORTS:
        raise ValueError(_redacted(url))
    return host, _PROXY_DEFAULT_PORTS[scheme] if port is None else port


def _push_refusal(rep: _Report) -> None:
    """The egress proxy refuses a git push over HTTPS — the box's "never push" posture without
    relying on no credential ever reaching it. An ACTIVE probe through the box's own proxy: the
    proxy's refusal passes; a push-capable mode letting it through is reported, not failed; a
    blind tunnel (`[proxy] passthrough`) WARNs, since the proxy can't see into it by design;
    GitHub answering a request the proxy decrypted FAILS, and so does no answer — unproven is not
    a pass."""
    print("▶ git push over HTTPS (the egress proxy refuses it, whatever credential the box holds)")
    proxy = os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy") or ""
    if not proxy.strip():
        print("  no egress proxy in this box (HTTPS_PROXY unset) — skipped")
        return
    try:
        host, port = _proxy_address(proxy)
    except ValueError as e:
        # Set, so the box routes SOMEWHERE: reading it as "no proxy" would skip the row exactly
        # where verify can't tell what the box's traffic does.
        rep.bad(
            f"git push refusal by the proxy UNPROVEN — HTTPS_PROXY is set but can't be read as "
            f"an http(s)://host[:port] proxy ({e})"
        )
        return
    where = f"[{host}]:{port}" if ":" in host else f"{host}:{port}"
    try:
        answer = _probe_push(host, port)
    except (OSError, http.client.HTTPException) as e:  # ssl.SSLError and timeouts are OSErrors
        rep.bad(
            f"git push refusal by the proxy UNPROVEN — no answer through {where} "
            f"({type(e).__name__}: {e})"
        )
        return
    if answer.status == 403 and answer.body.decode("utf-8", "replace").startswith(_PUSH_REFUSED):
        rep.ok(f"git push over HTTPS refused by the foldyard proxy ({_PUSH_PROBE_HOST})")
    elif answer.allowed:
        print(
            f"  ⚠ git push over HTTPS to {_PUSH_PROBE_HOST} allowed by the active mode — a "
            f"push-capable switch injects on {_PUSH_PROBE_HOST} (`fy mode` on your computer); "
            "refused again once it is off"
        )
    elif not answer.decrypted:
        rep.warn(
            f"{_PUSH_PROBE_HOST} is tunnelled blind ([proxy] passthrough), so the proxy can't see "
            "a push to it, nor refuse one — `fy config widenings`"
        )
    elif answer.status == 403:
        rep.bad(
            "a git push was refused, but not as a push — the proxy predates the push refusal "
            "(`fy host restart` on your computer)"
        )
    else:
        rep.bad(
            f"a git push through the proxy was NOT refused — {_PUSH_PROBE_HOST} answered HTTP "
            f"{answer.status}"
        )


def _wall_posture(rep: _Report) -> None:
    """[machine] firewall on lima: the box's DIRECT (proxy-ignoring) egress must be REJECTED — the
    fail-closed property the wall exists for. Two probes, both bypassing HTTPS_PROXY: a raw connect
    to a public IP on 443 (the baseline), AND one on port 53 — the wall now allows :53 only to
    LOCAL resolvers, so a public-IP :53 connect (the exfil-tunnel class) must also fail. Only the
    host proxy path may be open.

    The former caveat here — "an offline host also fails these, a false PASS" — is now CHECKED
    rather than noted. Both probes assert a refusal, so a box with no egress at all passes them
    both and the wall reads as enforcing when nothing was tested. The PERMITTED path (the proxy
    the wall exists to funnel traffic into) is the positive control: if that is unreachable too,
    the refusals are evidence of an outage, not of enforcement. (The wall REJECTs with tcp-reset,
    so a fast refusal remains the expected signature and a long timeout is still suspicious. The
    fuller red-team battery — rootful socket masked, nft-flush denied, host-network egress caught
    — is the host-side `_guest_state` probe on every `fy up` + example-lima-wall/
    test_network.sh; the box can't inspect VM-root state from an unprivileged container.)"""
    print("▶ VM firewall ([machine] firewall — direct egress from the box must be refused)")

    proxy = os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy") or ""
    if not proxy.strip():
        rep.bad(
            "no HTTPS_PROXY in the box — the VM firewall's permitted path is unknown, "
            "VM firewall UNPROVEN"
        )
        return
    try:
        # The same reading as the push row: IPv6 literals, a port-less URL, userinfo cut.
        proxy_host, proxy_port = _proxy_address(proxy)
    except ValueError as e:
        rep.bad(
            f"HTTPS_PROXY is set but can't be read as an http(s)://host[:port] proxy ({e}) — the "
            "VM firewall's permitted path is unknown, VM firewall UNPROVEN"
        )
        return
    where = f"[{proxy_host}]:{proxy_port}" if ":" in proxy_host else f"{proxy_host}:{proxy_port}"
    try:
        with socket.create_connection((proxy_host, proxy_port), timeout=5):
            pass
    except OSError:
        # No egress AT ALL. The refusals below would pass for the wrong reason.
        rep.bad(
            f"the permitted path ({where}) is unreachable too — this box has "
            "no egress at all, so a refused direct connect proves nothing. VM firewall UNPROVEN "
            "(is the host supervisor running? `fy host` on the host)"
        )
        return

    for host, port, what in (("1.1.1.1", 443, "443"), ("1.1.1.1", 53, "53 (exfil-tunnel port)")):
        try:
            with socket.create_connection((host, port), timeout=5):
                rep.bad(
                    f"direct egress to {host}:{what} CONNECTED — the VM firewall is NOT enforcing"
                )
        except OSError:
            rep.ok(f"direct egress to a public IP:{what} rejected")
    try:
        openings = config.machine_firewall_allow()
    except SystemExit as e:
        rep.warn(f"{e} (the VM applies only what was adopted)")
        return
    # In the box this is the working tree; the VM applied what the host adopted.
    source = (
        "this checkout declares [machine] firewall_allow"
        if config.in_box()
        else "opened by [machine] firewall_allow"
    )
    for o in openings:
        rep.info(f"{source}: {o.to} tcp/{o.port} — {o.why}")


class _Listener:
    """A service on your computer's loopback that nothing offered the box: it counts the
    connections it accepts and answers none."""

    def __init__(self) -> None:
        import threading

        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen()
        self.port = self.sock.getsockname()[1]
        self.accepted = 0
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self) -> None:
        while True:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            self.accepted += 1
            conn.close()

    def close(self) -> None:
        with contextlib.suppress(OSError):
            self.sock.shutdown(socket.SHUT_RDWR)  # wakes the accept() blocked in the thread
        self.sock.close()


def _host_loopback(rep: _Report, engine: str, env: dict, probe: str) -> None:
    """On your computer, with the VM firewall: a container can't reach a service on your
    computer's loopback — directly at the gateway Lima hands to it, or through the proxy, which
    runs there and so would dial it from there. Proven by a listener of our own on a port nothing
    granted: a probe container tries both, and the listener must accept NOTHING (a refusal and an
    empty port look alike from inside; the count doesn't). The positive control: the same
    container gets the proxy's liveness answer through the same gateway, so the two attempts
    really left it. Run from your computer because only it can open the listener."""
    from .plugins.proxy import LIVENESS_URL

    print("▶ your computer's loopback ([machine] firewall — a container must not reach it)")
    gateway, proxy = config.host_alias(), f"{config.host_alias()}:{config.proxy_port()}"
    listener = _Listener()
    try:
        script = (
            # A wgetrc can name a proxy, and it beats the env: GNU Wget runs every leg without one,
            # so the control and the proxy leg reach THIS checkout's proxy (the env) or nothing.
            # The direct leg must skip every proxy: one would refuse the gateway as a local
            # address, which reads as the wall refusing it. A wget we don't know is reported.
            'case "$(wget --help 2>&1)" in '
            "*BusyBox*) cfg=; off='-Y off' ;; "
            "*'GNU Wget 1.'*) cfg=--no-config; off=--no-proxy ;; "
            "*) cfg=; off=; echo direct=unknown ;; esac; "
            f"if wget -q -T 10 -O /dev/null $cfg {LIVENESS_URL}; "
            "then echo control=ok; else echo control=failed; fi; "
            '[ -z "$off" ] || (unset http_proxy HTTP_PROXY; '
            f"wget -q -T 5 -O /dev/null $cfg $off http://{gateway}:{listener.port}/); "
            f"wget -q -T 10 -O /dev/null $cfg http://127.0.0.1:{listener.port}/; true"
        )
        out = _run(
            [engine, "run", "--rm",
             "-e", f"http_proxy=http://{proxy}", "-e", f"HTTP_PROXY=http://{proxy}",
             "-e", "no_proxy=", "-e", "NO_PROXY=",
             probe, "sh", "-c", script],
            env,
            capture=True,
        )  # fmt: skip
        time.sleep(0.5)  # a connection the probe opened last is accepted by now
    finally:
        listener.close()
    if listener.accepted:
        rep.bad(
            f"a container REACHED a service on your computer's loopback (127.0.0.1:"
            f"{listener.port}, {listener.accepted} connection(s)) — directly or through the proxy"
        )
    elif "control=ok" not in out.stdout:
        rep.bad(
            "the probe couldn't reach the proxy through the gateway either, so a refused "
            "connection proves nothing. Your computer's loopback UNPROVEN (is the host "
            "supervisor running? `fy host`)"
        )
    elif "direct=unknown" in out.stdout:
        rep.bad(
            f"the probe image's wget ({probe}) is neither busybox's nor GNU Wget 1.x, so its "
            "direct attempt may have gone through a proxy. Your computer's loopback UNPROVEN (set "
            "VERIFY_IMG to an alpine or busybox image)"
        )
    else:
        rep.ok("a service on your computer's loopback is out of reach (directly and by the proxy)")


def _stack_health(rep: _Report, engine: str, env: dict, project: str) -> None:
    """Advisory: any container in this project sitting at `unhealthy`.

    A container whose app is dead does NOT stop looking alive on its own — under a restart
    policy the engine re-runs the command forever while reporting `Up 43 hours` with
    RestartCount 0, which is exactly how a detached bind mount once ate two days unnoticed.
    A healthcheck turns that into `unhealthy`, and this prints it so nobody has to go looking.
    WARN, never FAIL: the exit code stays the isolation verdict (CI-usable), and the engine
    takes no action of its own either (podman's `--health-on-failure` defaults to `none`).

    Consumers without healthchecks see the same "healthy or unprobed" pass — the check
    reports what is there rather than demanding anyone adopt healthchecks. A probe that
    could not RUN (either engine call non-zero) says so instead: silence from a failed
    command must never read as an all-clear.
    """
    print(f"▶ stack health (advisory — containers in project '{project}')")
    # `com.docker.compose.project` is set by BOTH docker compose and podman-compose, so one
    # filter covers either provider.
    ps = _run(
        [
            engine,
            "ps",
            "--filter",
            f"label=com.docker.compose.project={project}",
            "--format",
            "{{.Names}}",
        ],
        env,
        capture=True,
    )
    # A probe that FAILED is not a probe that found nothing: report it as unprobed and stop,
    # never fall through to the reassuring "healthy or unprobed" line on empty output.
    if ps.returncode != 0:
        rep.warn("could not list stack containers (engine `ps` failed) — health UNKNOWN")
        return
    names = [n for n in ps.stdout.split() if n]
    if not names:
        print("  stack not running — skipped")
        return

    fmt = "{{.Name}} {{if .State.Health}}{{.State.Health.Status}}{{else}}-{{end}}"
    out = _run([engine, "inspect", "--format", fmt, *names], env, capture=True)
    if out.returncode != 0:
        rep.warn(f"could not inspect {len(names)} stack container(s) — health UNKNOWN")
        return
    probed, sick = 0, []
    for line in out.stdout.splitlines():
        name, _, status = line.strip().partition(" ")
        if status and status != "-":
            probed += 1
            if status == "unhealthy":
                sick.append(name.lstrip("/"))
    for name in sick:
        rep.warn(f"{name} is UNHEALTHY — `fy logs` it; a lost mount needs a recreate, not a bounce")
    if not sick:
        rep.ok(f"{len(names)} stack container(s) healthy or unprobed ({probed} with healthchecks)")


def verify() -> int:
    # Verify CHECKS the boundary; it doesn't build one. A stopped VM is a refusal (never a pass),
    # and booting it here would make the credibility check the thing that changed the host.
    if not stack.engine_reachable("verify"):
        return 1
    ctx = stack.resolve()
    engine = config.engine()
    probe = os.environ.get("VERIFY_IMG", "alpine")
    rep = _Report()
    print(f"ℹ what each check proves, why it fails, what to do: `{_MANUAL}` ({_MANUAL_SECTION})")

    allowed = machine.guest_mounts(ctx.main, stack.worktrees_root(ctx.main))
    to_box = boxmounts.vm_paths()[1]
    expected = bool(to_box) and (config.in_box() or machine.to_box_mounted())
    _vm_boundary(rep, engine, ctx.env, probe, allowed, to_box, expected)

    if config.in_box():
        _box_posture(rep, ctx.env)
        _push_refusal(rep)
        if _walled():
            _wall_posture(rep)
    else:
        if _walled():
            _host_loopback(rep, engine, ctx.env, probe)
        print(
            "▶ dev-box checks — skipped (not inside the box; run via 'fy box shell' "
            "then 'fy verify')"
        )

    _stack_health(rep, engine, ctx.env, ctx.project)

    print()
    tail = f" ({rep.warns} health warning(s))" if rep.warns else ""
    if rep.fails == 0:
        print(f"✓ verify: ALL PASS — isolation intact.{tail}")
        return 0
    print(f"✗ verify: {rep.fails} check(s) FAILED.{tail}")
    print(f"  → `{_MANUAL}` ({_MANUAL_SECTION}: each check, why it fails, what to do)")
    return 1
