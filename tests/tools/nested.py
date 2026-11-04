"""``just nested-up|test|down``: foldyard's host tier in a VM of its own, on an Apple M3+.

The tier drives one VM with a fixed name, so on your computer it collides with any
``foldyard-example`` already in use. A nested VM (the L1) has its own ``~/.lima``, so the tier's VM
(the L2) lives inside it. docs/nested-virt.md has the recipe's evidence;
docs/adrs/0035-the-box-asks-for-a-test-run.md says why this stays outside the box.

What runs in the L1 is treated as code you wouldn't run on your computer: a dependency's install
script, or a test an agent was steered into writing. So the L1 mounts and forwards nothing and holds
no credentials, the commit reaches it as a pack over stdin, a wall stops it reaching your computer's
localhost through the gateway, and the tier runs as a user with no ``sudo`` to remove that wall
(``--with-sudo`` grants it, for ``test_wall_e2e``, and since root can't be taken back that L1 is
stopped straight after and has to be replaced before another run). Every run checks all of that first and refuses
otherwise. It keeps your computer out of reach; it isn't a bound on egress, and the L1
has the public internet, since the tier needs Fedora's mirrors and the registries. A test's verdict
is also only what the code under test reports.

A foldyard-only tool, on purpose: it lives outside the wheel, so a consumer never gets it. It is
stdlib-only, so ``uv run --no-project`` runs it (like tests/tools/prune.py), and imports nothing
from foldyard, whose git flags it copies (tests/test_nested.py holds them equal).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import socket
import subprocess
import sys
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

ASSETS = Path(__file__).resolve().parent / "nested_l1"
REPO = Path(__file__).resolve().parents[2]

LIMA_VERSION = "2.2.0"  # CI's (tests/test_nested.py holds them equal)
TESTER = "fytest"  # its home isn't /home/<user>.guest, which Lima reserves in the L2
SUDO_GRANT = "90-fy-nested-test"
HAD_ROOT = "fy-nested-had-root"  # beside the VM's Lima files: out of its reach, deleted with it
UNIT = "fy-nested-run"
GATEWAY = "192.168.5.2"  # Lima's user-mode network: your computer's localhost, from a guest
TRAP_ARGS = (
    "kvm-arm.vgic_v3_group0_trap",
    "kvm-arm.vgic_v3_group1_trap",
    "kvm-arm.vgic_v3_common_trap",
)

# Off, spelled out. `.portForwards=[]` isn't: Lima appends a default rule to whatever's listed, and
# a listener in the guest appeared on your computer's localhost within seconds.
FORWARD_NOTHING = [{"guestIP": "0.0.0.0", "proto": "any", "ignore": True}]
FORWARD_WAIT_S = 6  # longer than Lima takes to notice a listener: it took under 4
WALL_TABLE = "fy_nested_wall"

_VM_NAME = re.compile(r"fy-nested(-[a-z0-9][a-z0-9-]*)?")
_MODULE = re.compile(r"tests/test_(?:[a-z0-9_]+_)?e2e\.py")
_ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)?")
_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")

# foldyard/worktree.py's reasoning, copied: the repo's config is box-writable, and git can run
# what it names, reading an object included (a partial clone fetches what's missing).
_PROTOCOLS = ("file", "git", "ssh", "http", "https", "ext")
_NO_FETCH_CONFIG = (
    "-c",
    "protocol.allow=never",
    *(arg for p in _PROTOCOLS for arg in ("-c", f"protocol.{p}.allow=never")),
)
_NO_FETCH_ENV = {"GIT_NO_LAZY_FETCH": "1", "GIT_PROTOCOL_FROM_USER": "0"}
NO_REPO_CODE = ("-c", "core.hooksPath=/dev/null", "-c", "core.fsmonitor=", *_NO_FETCH_CONFIG)


class Refused(Exception):
    """A reason not to go on, for the operator."""


@dataclass(frozen=True)
class Config:
    vm: str
    cpus: int
    memory_gib: int
    disk_gib: int


def config(env: Mapping[str, str]) -> Config:
    vm = env.get("FY_NESTED_VM", "fy-nested")
    if not _VM_NAME.fullmatch(vm):
        # `nested-down --delete` deletes it, so this can't be allowed to name a project's VM.
        raise Refused(f"FY_NESTED_VM must be fy-nested or fy-nested-<name>, not {vm!r}.")
    try:
        return Config(
            vm=vm,
            cpus=int(env.get("FY_NESTED_CPUS", "8")),
            memory_gib=int(env.get("FY_NESTED_MEMORY_GIB", "16")),
            disk_gib=int(env.get("FY_NESTED_DISK_GIB", "60")),
        )
    except ValueError as e:
        raise Refused(f"FY_NESTED_CPUS, _MEMORY_GIB and _DISK_GIB are whole numbers: {e}") from e


def clean(text: str) -> str:
    """What the L1 prints, safe for your terminal: no control sequence (a clipboard write, a
    title, a cursor move), only newlines and tabs."""
    return _CONTROL.sub("", _ANSI.sub("", text))


# ── running things ───────────────────────────────────────────────────────────────────────


def _run(argv: Sequence[str], *, stdin=None, input=None, capture=True):
    return subprocess.run(
        list(argv), stdin=stdin, input=input, capture_output=capture, text=True, check=False
    )


def _show(done: subprocess.CompletedProcess) -> None:
    """What a command in the L1 printed, through :func:`clean`."""
    print(clean(done.stdout + done.stderr), end="", flush=True)


def _stop(vm: str) -> None:
    """Stop the L1 from outside, by force if the guest won't: a VM left up is the risk."""
    if _run(["limactl", "stop", vm], capture=False).returncode == 0:
        return
    if _run(["limactl", "stop", "--force", vm], capture=False).returncode != 0:
        print(f"⚠ couldn't stop {vm}, even by force: it's still running. See `limactl list`.")


def _stream(argv: Sequence[str]) -> int:
    """Run ``argv``, showing its output line by line through :func:`clean`; its exit code."""
    proc = subprocess.Popen(
        list(argv), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT
    )
    assert proc.stdout is not None
    for line in proc.stdout:
        print(clean(line.decode("utf-8", "replace")), end="", flush=True)
    return proc.wait()


def _shell_argv(vm: str, *cmd: str) -> list[str]:
    return ["limactl", "shell", "--workdir", "/", vm, *cmd]


def _tester_argv(vm: str, script: str) -> list[str]:
    env = (
        'export PATH="/usr/local/bin:/usr/bin:/bin" '
        'XDG_RUNTIME_DIR="/run/user/$(id -u)" '
        'DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/$(id -u)/bus"; '
    )
    return _shell_argv(vm, "sudo", "-n", "-u", TESTER, "--", "bash", "-c", env + script)


def _status(vm: str) -> str:
    """The VM's status, ``""`` when there's no such VM."""
    rows = _run(["limactl", "list", "--format", "{{.Name}} {{.Status}}"]).stdout.splitlines()
    return next((r.split()[1] for r in rows if r.split()[:1] == [vm] and len(r.split()) > 1), "")


def reaches_your_computer(vm: str) -> bool:
    """Can the L1 connect to a socket listening on this computer's loopback? Enforcement asked of
    the network itself: a rule that's loaded says nothing about one that works."""
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        port = listener.getsockname()[1]
        probe = f"</dev/tcp/{GATEWAY}/{port}"
        return _run(_shell_argv(vm, "timeout", "5", "bash", "-c", probe)).returncode == 0


def _port_answers(port: int) -> bool:
    with socket.socket() as client:
        client.settimeout(1)
        return client.connect_ex(("127.0.0.1", port)) == 0


def forwards_to_your_computer(vm: str) -> bool:
    """Does a listener in the L1 show up on this computer's localhost? Code there could otherwise
    squat a port a local tool or browser trusts. Asked of the real path, as with the wall."""
    with socket.socket() as free:
        free.bind(("127.0.0.1", 0))
        port = free.getsockname()[1]
    listen = (
        "import socket,time;s=socket.socket();"
        f"s.bind(('127.0.0.1',{port}));s.listen();time.sleep({FORWARD_WAIT_S + 8})"
    )
    _run(_tester_argv(vm, f"(nohup python3 -c {shlex.quote(listen)} >/dev/null 2>&1 &)"))
    time.sleep(FORWARD_WAIT_S)
    return _port_answers(port)


# ── what makes an L1 fit to run in ───────────────────────────────────────────────────────


def _traps_missing(vm: str) -> list[str]:
    words = _run(_shell_argv(vm, "cat", "/proc/cmdline")).stdout.split()
    return [arg for arg in TRAP_ARGS if f"{arg}=1" not in words]


def _had_root_mark(vm: str) -> Path | None:
    got = _run(["limactl", "list", vm, "--format", "{{.Dir}}"]).stdout.strip()
    return Path(got) / HAD_ROOT if got else None


def _tainted(vm: str) -> bool:
    """Did code in this L1 ever have root? A run with it can leave a narrow sudo rule, or a timer
    that drops the wall later, and no check from outside would find those. Unknown counts as yes."""
    mark = _had_root_mark(vm)
    return mark is None or mark.exists()


def _refuse_tainted(vm: str) -> None:
    if _tainted(vm):
        if _status(vm) == "Running":  # a killed run never got to stop it
            _stop(vm)
        raise Refused(
            f"{vm} had root in a `--with-sudo` run, and that can't be taken back: anything could "
            "be left in it. `just nested-down --delete && just nested-up` replaces it."
        )


def _check_contained(vm: str) -> None:
    missing = _traps_missing(vm)
    if missing:
        raise Refused(
            f"{vm}'s kernel lacks the GIC trap arguments ({', '.join(missing)}), and without them "
            "no nested VM boots. Run `just nested-up`."
        )
    # The journal, not dmesg: this is printed once at boot, and a run's containers can push it
    # out of the ring buffer. Then every run would refuse, with nothing able to repair it.
    log = _shell_argv(vm, "sudo", "journalctl", "-k", "-b", "-g", "sysreg trapping", "--no-pager")
    if "sysreg trapping" not in _run(log).stdout:
        raise Refused(f"{vm}'s KVM doesn't report GICv3 sysreg trapping: no nested VM would boot.")
    mounts = _run(["limactl", "list", vm, "--format", "{{json .Config.Mounts}}"]).stdout.strip()
    if json.loads(mounts or "null"):
        raise Refused(f"{vm} has mounts, so code in it could reach your files. Recreate it.")
    forwards = _run(["limactl", "list", vm, "--format", "{{json .Config.PortForwards}}"]).stdout
    if (json.loads(forwards.strip() or "null") or [{}])[0] != FORWARD_NOTHING[0]:
        raise Refused(
            f"{vm} isn't set to forward nothing, so a listener in it could appear on your "
            "computer. `just nested-down --delete && just nested-up` replaces it."
        )
    wall = _run(_shell_argv(vm, "sudo", "nft", "list", "table", "inet", WALL_TABLE))
    if wall.returncode != 0 or "100.64.0.0/10" not in wall.stdout:
        raise Refused(f"{vm}'s wall isn't loaded as it should be. Run `just nested-up`.")
    if reaches_your_computer(vm):
        raise Refused(f"{vm} can reach your computer's localhost: its wall is down.")
    if forwards_to_your_computer(vm):
        raise Refused(
            f"a listener in {vm} appears on your computer's localhost, whatever its config says."
        )
    # `-l`, not `true`: a rule allowing only `nft` refuses `true` and can still remove the wall.
    if _run(_tester_argv(vm, "sudo -n -l")).returncode == 0:
        raise Refused(
            f"{TESTER} in {vm} has sudo, so code it runs could remove the wall. Nothing here should "
            "have given it that. `just nested-down --delete && just nested-up` replaces the VM."
        )


# ── the verbs ────────────────────────────────────────────────────────────────────────────


def up(cfg: Config) -> None:
    """Make the L1 ready, from whatever state it's in. Safe to repeat."""
    drivers = json.loads(_run(["limactl", "info"]).stdout or "{}").get("vmTypes", [])
    if "vz" not in drivers:
        raise Refused(
            "this needs Lima's vz driver on an Apple M3+. Elsewhere, see Rig 1 in "
            "docs/nested-virt.md."
        )
    status = _status(cfg.vm)
    fresh = not status
    if not fresh:
        _refuse_tainted(cfg.vm)
    if fresh:
        _create(cfg)
        if not reaches_your_computer(cfg.vm):
            _stop(cfg.vm)
            _run(["limactl", "delete", cfg.vm])
            raise Refused(
                f"{cfg.vm} couldn't reach your computer even before its wall, so the wall's probe "
                "would prove nothing. Has Lima's gateway moved? The new VM has been deleted."
            )
    elif status != "Running":
        _start(cfg.vm)
    script = (ASSETS / "provision.sh").read_text()
    print(f"▶ provisioning {cfg.vm} (about a minute the first time)")
    provisioned = _run(
        _shell_argv(cfg.vm, "sudo", "env", f"LIMA_VERSION={LIMA_VERSION}", "bash", "-s"),
        input=script,
    )
    _show(provisioned)
    if provisioned.returncode != 0:
        raise Refused(f"provisioning {cfg.vm} failed (exit {provisioned.returncode}).")
    if _traps_missing(cfg.vm):
        print(f"▶ restarting {cfg.vm}: its kernel needs the GIC trap arguments")
        _stop(cfg.vm)
        _start(cfg.vm)
    _check_contained(cfg.vm)
    print(f"✓ {cfg.vm} is ready: `just nested-test`")


def _create_set_expr() -> str:
    forwards = json.dumps(FORWARD_NOTHING)
    return f".nestedVirtualization=true | .mounts=[] | .portForwards={forwards}"


def _create(cfg: Config) -> None:
    print(f"▶ creating {cfg.vm}")
    created = _run(
        [
            "limactl",
            "start",
            "--tty=false",
            f"--name={cfg.vm}",
            "--vm-type=vz",
            f"--cpus={cfg.cpus}",
            f"--memory={cfg.memory_gib}",
            f"--disk={cfg.disk_gib}",
            "--set",
            _create_set_expr(),
            "template:podman",
        ],
        capture=False,
    )
    if created.returncode != 0:
        raise Refused(
            f"couldn't create {cfg.vm}. Nesting needs an Apple M3+ on macOS 15+; `limactl` said "
            "why above."
        )


def _start(vm: str) -> None:
    if _run(["limactl", "start", "--tty=false", vm], capture=False).returncode != 0:
        raise Refused(f"couldn't start {vm}.")


def run_tests(cfg: Config, repo: Path, ref: str, modules: Sequence[str], *, with_sudo: bool) -> int:
    """Run the tier on a commit, ``modules`` or CI's shards; the tier's exit code."""
    for module in modules:
        if not _MODULE.fullmatch(module):
            raise Refused(f"not a host-tier module: {module!r} (tests/test_*_e2e.py).")
    status = _status(cfg.vm)
    if status:
        _refuse_tainted(cfg.vm)
    if status != "Running":
        raise Refused(f"{cfg.vm} isn't running. Run `just nested-up`.")
    _check_contained(cfg.vm)
    if _run(_tester_argv(cfg.vm, f"systemctl --user is-active -q {UNIT}")).returncode == 0:
        raise Refused(f"a run is still running in {cfg.vm}. `just nested-down` stops it.")
    if _run(["git", "--no-lazy-fetch", "version"]).returncode != 0:
        raise Refused(
            "reading this repo needs git 2.44+ on your computer: the repo's config can make it a "
            "partial clone, and an older git could then run what that config names."
        )
    sha = _rev_parse(repo, ref)
    print(f"▶ sending {sha[:12]} to {cfg.vm} (commits only: uncommitted work stays here)")
    script = (ASSETS / "run-tier.sh").read_text()
    install = "mkdir -p ~/nested && cat > ~/nested/run-tier.sh"
    if _run(_tester_argv(cfg.vm, install), input=script).returncode != 0:
        raise Refused(f"couldn't reach {TESTER} in {cfg.vm}. Run `just nested-up`.")
    _send_pack(cfg, repo, sha)
    command = shlex.join(
        [
            "systemd-run",
            "--user",
            "--wait",
            "--pipe",
            "--collect",
            "--quiet",
            f"--unit={UNIT}",
            "--property=RuntimeMaxSec=7200",
            "bash",
            f"/home/{TESTER}/nested/run-tier.sh",
            sha,
            *modules,
        ]
    )
    grant = f"/etc/sudoers.d/{SUDO_GRANT}"
    if with_sudo:
        mark = _had_root_mark(cfg.vm)
        if mark is None:
            raise Refused(f"couldn't find {cfg.vm}'s Lima directory, to mark that it had root.")
        mark.touch()  # first: an interrupted run has had root as well
        print(
            f"⚠ {TESTER} has root for this run, so this code could remove the wall. {cfg.vm} is "
            "stopped afterwards: `just nested-down --delete && just nested-up` replaces it."
        )
        line = shlex.quote(f"{TESTER} ALL=(ALL) NOPASSWD: ALL")
        _run(_shell_argv(cfg.vm, "sudo", "bash", "-c", f"echo {line} > {grant}; chmod 440 {grant}"))
    try:
        return _stream(_tester_argv(cfg.vm, f"{command} </dev/null"))
    finally:
        if with_sudo:
            # Not a repair: what root left behind stays. Stopped, it can't act on your computer.
            _run(_shell_argv(cfg.vm, "sudo", "rm", "-f", grant))
            down(cfg, delete=False)


def down(cfg: Config, *, delete: bool) -> None:
    """Stop inside-out: the run, the VMs in the L1, then the L1. ``delete`` removes it too. A VM
    that had root is only stopped from outside: a binary planted in it would answer anything run
    there, and the VMs inside it stop along with it."""
    status = _status(cfg.vm)
    if not status:
        print(f"{cfg.vm} doesn't exist.")
        return
    if status == "Running" and not _tainted(cfg.vm):
        inner = (
            f"systemctl --user stop {UNIT} 2>/dev/null; "
            'for v in $(limactl list -q 2>/dev/null); do limactl stop "$v" 2>/dev/null; done; true'
        )
        _show(_run(_tester_argv(cfg.vm, inner)))
    if status == "Running":
        _stop(cfg.vm)
    if delete:
        _run(["limactl", "delete", cfg.vm], capture=False)


# ── the commit's trip ────────────────────────────────────────────────────────────────────


def git_env() -> dict[str, str]:
    return {**os.environ, **_NO_FETCH_ENV}


def _rev_parse(repo: Path, ref: str) -> str:
    got = subprocess.run(
        [
            "git",
            *NO_REPO_CODE,
            "-C",
            str(repo),
            "rev-parse",
            "--verify",
            "--quiet",
            "--end-of-options",
            f"{ref}^{{commit}}",
        ],
        env=git_env(),
        capture_output=True,
        text=True,
        check=False,
    )
    if got.returncode != 0:
        raise Refused(f"{ref!r} doesn't name a commit here.")
    return got.stdout.strip()


def pack_argv(repo: Path) -> list[str]:
    """A pack of a commit named on stdin, all it reaches. Not a bundle, whose commit needs a ref:
    writing one here would run the repo's reference-transaction hook. Not `git archive`, which
    runs the filter drivers the repo's config names."""
    return ["git", *NO_REPO_CODE, "-C", str(repo), "pack-objects", "--revs", "--stdout", "--quiet"]


def _send_pack(cfg: Config, repo: Path, sha: str) -> None:
    pack = subprocess.Popen(
        pack_argv(repo),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        env=git_env(),
    )
    assert pack.stdin is not None and pack.stdout is not None
    pack.stdin.write(f"{sha}\n".encode())
    pack.stdin.close()
    received = _run(_tester_argv(cfg.vm, "cat > ~/nested/foldyard.pack"), stdin=pack.stdout)
    pack.stdout.close()
    if pack.wait() != 0 or received.returncode != 0:
        raise Refused(f"couldn't send {sha[:12]} to {cfg.vm}.")


# ── the command line ─────────────────────────────────────────────────────────────────────


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="just nested", description="foldyard's host tier in a VM of its own, on an Apple M3+."
    )
    verbs = ap.add_subparsers(dest="verb", required=True)
    verbs.add_parser("up", help="create, provision and check the L1")
    test = verbs.add_parser("test", help="run the host tier in the L1 on a commit")
    test.add_argument("--ref", default="HEAD", help="the commit to run (default: HEAD)")
    test.add_argument(
        "--with-sudo",
        action="store_true",
        help="give the tester root, for test_wall_e2e: the VM is stopped after, and must be replaced",
    )
    test.add_argument("modules", nargs="*", help="tests/test_*_e2e.py (default: CI's shards)")
    stop = verbs.add_parser("down", help="stop everything, inside-out")
    stop.add_argument("--delete", action="store_true", help="delete the L1 as well")
    args = ap.parse_args(argv)

    try:
        if os.environ.get("IN_DEVBOX") == "1":
            raise Refused("this runs on your computer, not in a dev box.")
        cfg = config(os.environ)
        if args.verb == "up":
            up(cfg)
        elif args.verb == "down":
            down(cfg, delete=args.delete)
        else:
            return run_tests(cfg, REPO, args.ref, args.modules, with_sudo=args.with_sudo)
    except Refused as e:
        print(f"✗ {e}", file=sys.stderr)
        return 1
    except FileNotFoundError as e:
        print(f"✗ {e.filename} isn't installed.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
