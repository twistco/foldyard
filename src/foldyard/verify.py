"""Isolation battery — the product's credibility check (`foldyard verify`).

Faithful port of the `verify` recipe. Proves the VM boundary
over the engine socket (rootless, the `--privileged --pid=host` escape refused, no host
home/paths) and — INSIDE the box only — the credential-less dev-box posture (no SSH key
material, mode-aware GitHub posture, git push refused). Each check prints PASS/FAIL; returns
non-zero on any FAIL (CI-usable). One ADVISORY section rides along at the end — unhealthy
containers in the stack — printed as WARN so it can never move the isolation verdict.

Behaviour-preserving: same checks, same messages, same exit semantics as the recipe. The
engine probes run with the resolved stack env (so `CONTAINER_HOST`/`DOCKER_HOST` reach the
machine socket). MECHANISM-specific posture (e.g. github's mode-aware dummy-token/gh-CLI
checks) is contributed by the plugins via `registry().verify_checks` (ADR-0015); the
credential-AGNOSTIC backstops (no ssh keys, no netrc, git push refused) stay in core here so
the credibility check can never be weakened by a plugin being absent or broken.
"""

from __future__ import annotations

import os
import re
import socket
import subprocess
from pathlib import Path
from shutil import which

from . import config, stack
from .plugins import VerifyContext, registry

_PASS = "\033[32m✓ PASS\033[0m"
_FAIL = "\033[31m✗ FAIL\033[0m"
_WARN = "\033[33m⚠ WARN\033[0m"
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


def _host_paths() -> re.Pattern[str]:
    """The leak pattern for THIS host. The host's own home is matched exactly — followed by a
    separator, whitespace or end — so a guest home that merely shares its prefix does not trip
    it: Lima names the guest user ``<user>.linux``, so ``/home/dain`` must not match
    ``/home/dain.linux``."""
    home = re.escape(str(Path.home()))
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
    edge case: the probe is pulled from a registry, and ``[machine].wall = true`` (what
    ``fy init`` scaffolds) denies egress unless the host supervisor is up, so a cold image cache
    makes the pull fail exactly when the posture is most locked down. Run something trivial
    first; if that cannot run, no absence below is evidence of anything."""
    return _engine_run_succeeds(engine, env, [probe, "true"])


# `git ls-remote` failing proves "no credential reaches origin" ONLY if it failed for credential
# reasons. A box with no egress fails it too, which would certify the product's headline claim
# ("git push is impossible from inside") from a network outage.
_GIT_AUTH_DENIED = re.compile(
    r"permission denied|authentication failed|could not read username|could not read password|"
    r"terminal prompts disabled|invalid username or password|access denied|403|401",
    re.I,
)


def _rootless(engine: str, env: dict) -> bool:
    """docker reports rootless in `.SecurityOptions`; podman in `.Host.Security.Rootless`."""
    sec = _run([engine, "info", "--format", "{{.SecurityOptions}}"], env, capture=True)
    if sec.returncode == 0 and "rootless" in sec.stdout:
        return True
    rl = _run([engine, "info", "--format", "{{.Host.Security.Rootless}}"], env, capture=True)
    return rl.returncode == 0 and "true" in rl.stdout.lower()


def _vm_boundary(rep: _Report, engine: str, env: dict, probe: str) -> None:
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
            "nothing below is proven. (Behind the wall? Start the host supervisor, or pre-pull "
            f"the image / set VERIFY_IMG to one already in the VM.)"
        )
        return

    if _engine_run_succeeds(
        engine, env, ["--privileged", "--pid=host", probe, "sh", "-c", "cat /proc/1/ns/ipc"]
    ):
        rep.bad("host PID1 namespace READABLE from --privileged --pid=host (breakout!)")
    else:
        rep.ok("escape refused (--privileged --pid=host can't read host PID1 ns)")

    if _engine_run_succeeds(engine, env, ["--privileged", probe, "sh", "-c", "ls /Users"]):
        rep.bad("/Users visible inside a --privileged container — VM mounts host home")
    else:
        rep.ok("no /Users inside a --privileged container")

    # An EMPTY mount table is not a clean one: `mount` printing nothing means the probe did not
    # run, and grepping no lines for host paths finds none of them.
    mp = _run(
        [engine, "run", "--rm", "--privileged", probe, "sh", "-c", "mount"], env, capture=True
    )
    if mp.returncode != 0 or not mp.stdout.strip():
        rep.bad("could not read the VM mount table (probe produced nothing) — leak check UNPROVEN")
        return
    host = [ln for ln in mp.stdout.splitlines() if _host_paths().search(ln)]
    if not host:
        rep.ok("VM mount table free of host home/paths")
    else:
        rep.bad(f"VM exposes host paths: {host[0]}")


def _plugin_posture(rep: _Report, env: dict) -> None:
    """Mechanism-specific posture from the plugins (ADR-0015): each contributes mode-aware
    assertions for its own credential mechanism (e.g. github's dummy-token / gh-CLI posture).
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


def _box_posture(rep: _Report, env: dict) -> None:
    print("▶ dev-box posture (credential-less: read+commit, never push)")
    home = Path(os.environ.get("HOME") or str(Path.home()))

    if not os.environ.get("SSH_AUTH_SOCK"):
        rep.ok("no SSH agent forwarded (SSH_AUTH_SOCK unset)")
    else:
        rep.bad("SSH_AUTH_SOCK set")

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

    if not (home / ".netrc").exists():
        rep.ok("no ~/.netrc")
    else:
        rep.bad("~/.netrc present")

    checkout = env.get("FOLDYARD_CHECKOUT") or str(stack.main_repo())
    git_env = {
        **env,
        "GIT_SSH_COMMAND": "ssh -o BatchMode=yes -o StrictHostKeyChecking=no -o ConnectTimeout=6",
    }
    try:
        got = subprocess.run(
            ["git", "-C", checkout, "ls-remote", "origin"],
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
        rep.bad("git remote REACHABLE — the box can push (credential leaked?)")
    elif _GIT_AUTH_DENIED.search(why):
        rep.ok("git push refused (no credential reaches origin)")
    else:
        # It failed, but not because a credential was refused — origin was never reached. That
        # is a network fact, not a posture one, and must not certify the credential-less claim.
        rep.bad(
            "git remote UNREACHABLE for non-credential reasons — push refusal UNPROVEN "
            f"({why.strip().splitlines()[-1] if why.strip() else 'no error output'})"
        )


def _wall_posture(rep: _Report) -> None:
    """[machine].wall on lima: the box's DIRECT (proxy-ignoring) egress must be REJECTED — the
    fail-closed property the wall exists for. Two probes, both bypassing HTTPS_PROXY: a raw connect
    to a public IP on 443 (the baseline), AND one on port 53 — the wall now allows :53 only to
    LOCAL resolvers, so a public-IP :53 connect (the exfil-tunnel class) must also fail. Only the
    Mac proxy path may be open.

    The former caveat here — "an offline Mac also fails these, a false PASS" — is now CHECKED
    rather than noted. Both probes assert a refusal, so a box with no egress at all passes them
    both and the wall reads as enforcing when nothing was tested. The PERMITTED path (the proxy
    the wall exists to funnel traffic into) is the positive control: if that is unreachable too,
    the refusals are evidence of an outage, not of enforcement. (The wall REJECTs with tcp-reset,
    so a fast refusal remains the expected signature and a long timeout is still suspicious. The
    fuller red-team battery — rootful socket masked, nft-flush denied, host-network egress caught
    — is the host-side `_wall_vm_state` probe on every `fy up` + example-lima-wall/
    test_network.sh; the box can't inspect VM-root state from an unprivileged container.)"""
    print("▶ egress wall ([machine].wall — direct egress from the box must be refused)")

    proxy = os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy") or ""
    m = re.search(r"//(?:[^@/]*@)?([^:/]+):(\d+)", proxy)
    if not m:
        rep.bad("no HTTPS_PROXY in the box — the wall's permitted path is unknown, wall UNPROVEN")
        return
    try:
        with socket.create_connection((m.group(1), int(m.group(2))), timeout=5):
            pass
    except OSError:
        # No egress AT ALL. The refusals below would pass for the wrong reason.
        rep.bad(
            f"the permitted path ({m.group(1)}:{m.group(2)}) is unreachable too — this box has "
            "no egress at all, so a refused direct connect proves nothing. Wall UNPROVEN "
            "(is `fy host` running?)"
        )
        return

    for host, port, what in (("1.1.1.1", 443, "443"), ("1.1.1.1", 53, "53 (exfil-tunnel port)")):
        try:
            with socket.create_connection((host, port), timeout=5):
                rep.bad(f"direct egress to {host}:{what} CONNECTED — the wall is NOT enforcing")
        except OSError:
            rep.ok(f"direct egress to a public IP:{what} rejected")


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
    ctx = stack.resolve()
    engine = config.engine()
    probe = os.environ.get("VERIFY_IMG", "alpine")
    rep = _Report()

    _vm_boundary(rep, engine, ctx.env, probe)

    if config.in_box():
        _box_posture(rep, ctx.env)
        if config.machine_backend() == "lima" and config.machine_wall():
            _wall_posture(rep)
    else:
        print(
            "▶ dev-box posture — skipped (not inside the box; run via 'fy box shell' "
            "then 'fy verify')"
        )

    _stack_health(rep, engine, ctx.env, ctx.project)

    print()
    tail = f" ({rep.warns} health warning(s))" if rep.warns else ""
    if rep.fails == 0:
        print(f"✓ verify: ALL PASS — isolation intact.{tail}")
        return 0
    print(f"✗ verify: {rep.fails} check(s) FAILED.{tail}")
    return 1
