"""``just nested-up|test|down`` (tests/tools/nested.py): the host tier in a VM of its own.

The orchestration runs against a fake L1, which answers the commands the tool sends; the commit's
trip uses real git. The live proof is running the recipes on an M3+ (docs/nested-virt.md).
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

import pytest

from foldyard import worktree
from tools import nested

ROOT = Path(__file__).resolve().parent.parent
PROVISION = (ROOT / "tests/tools/nested_l1/provision.sh").read_text()
RUN_TIER = (ROOT / "tests/tools/nested_l1/run-tier.sh").read_text()
WORKFLOW = (ROOT / ".github/workflows/foldyard-e2e.yml").read_text()

CFG = nested.Config(vm="fy-nested", cpus=8, memory_gib=16, disk_gib=60)
TRAPS = " ".join(f"{arg}=1" for arg in nested.TRAP_ARGS)


class FakeL1:
    """What the tool can observe of an L1, and every command it was sent."""

    def __init__(self, status: str = "Running"):
        self.status = status
        self.cmdline = f"BOOT_IMAGE=x ro {TRAPS}"
        self.traps_after_restart = True
        self.reaches = [False]  # successive answers of the probe; the last repeats
        self.tester_has_sudo = False
        self.mounts = "null"
        self.run_active = False
        self.tier_exit = 0
        self.calls: list[list[str]] = []
        self.streamed: list[list[str]] = []
        self.sent_pack = False

    def run(self, argv, *, stdin=None, input=None, capture=True):
        self.calls.append(list(argv))
        joined = " ".join(argv)
        out, rc = "", 0
        if argv[:2] == ["limactl", "list"] and ".Config.Mounts" in joined:
            out = self.mounts
        elif argv[:2] == ["limactl", "list"]:
            out = f"fy-nested {self.status}\nother Running\n" if self.status else "other Running\n"
        elif argv[:2] == ["limactl", "info"]:
            out = '{"vmTypes":["qemu","vz"]}'
        elif argv[:2] == ["limactl", "stop"]:
            self.status = "Stopped"
        elif argv[:2] == ["limactl", "start"]:
            self.status = "Running"
            if self.traps_after_restart:
                self.cmdline = f"BOOT_IMAGE=x ro {TRAPS}"
        elif "/proc/cmdline" in joined:
            out = self.cmdline
        elif "dmesg" in joined:
            out = "kvm [1]: GICv3 sysreg trapping enabled" if TRAPS in self.cmdline else ""
        elif "/dev/tcp/" in joined:
            answer = self.reaches.pop(0) if len(self.reaches) > 1 else self.reaches[0]
            rc = 0 if answer else 1
        elif "sudo -n true" in joined:
            rc = 0 if self.tester_has_sudo else 1
        elif "is-active" in joined:
            rc = 0 if self.run_active else 3
        return subprocess.CompletedProcess(argv, rc, out, "")

    def stream(self, argv):
        self.streamed.append(list(argv))
        return self.tier_exit

    def sent(self, fragment: str) -> list[list[str]]:
        return [c for c in self.calls if fragment in " ".join(c)]


@pytest.fixture
def l1(monkeypatch):
    fake = FakeL1()
    monkeypatch.setattr(nested, "_run", fake.run)
    monkeypatch.setattr(nested, "_stream", fake.stream)

    def _send(cfg, repo, sha):
        fake.sent_pack = True

    monkeypatch.setattr(nested, "_send_pack", _send)
    monkeypatch.setattr(nested, "_rev_parse", lambda repo, ref: "a" * 40)
    monkeypatch.delenv("IN_DEVBOX", raising=False)
    return fake


# ── up ───────────────────────────────────────────────────────────────────────────────────


def test_a_fresh_l1_has_nesting_and_nothing_shared(l1):
    l1.status = ""
    l1.cmdline = "BOOT_IMAGE=x ro"
    l1.reaches = [True, False]  # the control, then the wall
    nested.up(CFG)
    create = l1.sent("--name=fy-nested")[0]
    assert create[:2] == ["limactl", "start"]
    assert "--vm-type=vz" in create
    assert create[-1] == "template:podman"
    expr = create[create.index("--set") + 1]
    assert ".nestedVirtualization=true" in expr
    assert ".mounts=[]" in expr
    assert ".portForwards=[]" in expr


def test_the_probe_is_proven_against_a_fresh_l1_before_the_wall(l1, capsys):
    l1.status = ""
    l1.cmdline = "BOOT_IMAGE=x ro"
    l1.reaches = [False, False]  # unreachable even BEFORE the wall: the probe proves nothing
    nested.up(CFG)
    assert "probe" in capsys.readouterr().out


def test_up_restarts_only_when_the_traps_are_not_in_the_running_kernel(l1):
    nested.up(CFG)
    assert not l1.sent("limactl stop")
    l1.cmdline = "BOOT_IMAGE=x ro"
    nested.up(CFG)
    assert l1.sent("limactl stop") and l1.sent("limactl start")


def test_up_fails_closed_when_the_traps_never_reach_the_kernel(l1):
    # Seen live on krunkit: the boot entries had them and /proc/cmdline didn't.
    l1.cmdline = "BOOT_IMAGE=x ro"
    l1.traps_after_restart = False
    with pytest.raises(nested.Refused, match="trap"):
        nested.up(CFG)


def test_up_refuses_a_host_without_vz(l1, monkeypatch):
    real = l1.run

    def no_vz(argv, **kw):
        if argv[:2] == ["limactl", "info"]:
            return subprocess.CompletedProcess(argv, 0, '{"vmTypes":["qemu"]}', "")
        return real(argv, **kw)

    monkeypatch.setattr(nested, "_run", no_vz)
    with pytest.raises(nested.Refused, match="vz"):
        nested.up(CFG)


def test_up_sends_the_provisioning_script_to_root(l1):
    nested.up(CFG)
    call = l1.sent("bash -s")[0]
    assert "sudo" in call
    assert f"LIMA_VERSION={nested.LIMA_VERSION}" in call


# ── what a run requires ──────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("spoil", "why"),
    [
        (lambda f: setattr(f, "cmdline", "BOOT_IMAGE=x ro"), "trap"),
        (lambda f: setattr(f, "reaches", [True]), "your computer"),
        (lambda f: setattr(f, "tester_has_sudo", True), "sudo"),
        (lambda f: setattr(f, "mounts", '[{"location":"~"}]'), "mount"),
        (lambda f: setattr(f, "status", "Stopped"), "nested-up"),
        (lambda f: setattr(f, "status", ""), "nested-up"),
        (lambda f: setattr(f, "run_active", True), "running"),
    ],
)
def test_a_run_refuses_an_l1_that_is_not_contained(l1, spoil, why):
    spoil(l1)
    with pytest.raises(nested.Refused, match=why):
        nested.run_tests(CFG, ROOT, "HEAD", [], with_sudo=False)
    assert not l1.sent_pack
    assert not l1.streamed


def test_a_run_sends_the_commit_then_runs_the_tier_as_the_tester(l1):
    assert nested.run_tests(CFG, ROOT, "HEAD", ["tests/test_box_e2e.py"], with_sudo=False) == 0
    assert l1.sent_pack
    cmd = " ".join(l1.streamed[0])
    assert f"-u {nested.TESTER}" in cmd
    assert "systemd-run --user" in cmd
    assert "a" * 40 in cmd
    assert "tests/test_box_e2e.py" in cmd
    assert not l1.sent("sudoers.d")


def test_a_run_returns_the_tiers_exit_code(l1):
    l1.tier_exit = 3
    assert nested.run_tests(CFG, ROOT, "HEAD", [], with_sudo=False) == 3


def test_sudo_is_granted_for_one_run_and_taken_back_even_when_it_fails(l1, monkeypatch):
    def boom(argv):
        raise KeyboardInterrupt

    monkeypatch.setattr(nested, "_stream", boom)
    with pytest.raises(KeyboardInterrupt):
        nested.run_tests(CFG, ROOT, "HEAD", [], with_sudo=True)
    grant, revoke = l1.sent("sudoers.d")
    assert "NOPASSWD" in " ".join(grant)
    assert "rm -f" in " ".join(revoke)


@pytest.mark.parametrize(
    "module",
    ["tests/test_box.py", "tests/test_box_e2e.py; rm -rf /", "../x_e2e.py", "--co", ""],
)
def test_a_run_takes_only_host_tier_module_paths(l1, module):
    with pytest.raises(nested.Refused, match="module"):
        nested.run_tests(CFG, ROOT, "HEAD", [module], with_sudo=False)


@pytest.mark.parametrize("module", ["tests/test_e2e.py", "tests/test_wall_e2e.py"])
def test_the_host_tier_modules_are_accepted(l1, module):
    assert nested.run_tests(CFG, ROOT, "HEAD", [module], with_sudo=False) == 0


# ── down ─────────────────────────────────────────────────────────────────────────────────


def test_down_stops_inside_out_and_keeps_the_vm(l1):
    nested.down(CFG, delete=False)
    order = [" ".join(c) for c in l1.calls]
    inner = next(i for i, c in enumerate(order) if "limactl list -q" in c)
    outer = next(i for i, c in enumerate(order) if c.startswith("limactl stop"))
    assert inner < outer
    assert not l1.sent("limactl delete")


def test_down_deletes_only_when_asked(l1):
    nested.down(CFG, delete=True)
    assert ["limactl", "delete", "fy-nested"] in l1.calls


def test_down_on_no_vm_is_a_no_op(l1):
    l1.status = ""
    nested.down(CFG, delete=True)
    assert not l1.sent("limactl stop")
    assert not l1.sent("limactl delete")


# ── config and refusals ──────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "name", ["foldyard", "foldyard-example", "cabash", "fy-nested;x", "fy nested"]
)
def test_the_vm_name_cant_point_at_another_vm(name):
    # `nested-down --delete` deletes it, so a mistyped variable mustn't reach a project's VM.
    with pytest.raises(nested.Refused, match="fy-nested"):
        nested.config({"FY_NESTED_VM": name})


def test_config_defaults_and_overrides():
    assert nested.config({}) == CFG
    got = nested.config({"FY_NESTED_VM": "fy-nested-2", "FY_NESTED_CPUS": "4"})
    assert (got.vm, got.cpus) == ("fy-nested-2", 4)


def test_every_verb_refuses_inside_a_box(monkeypatch, capsys):
    monkeypatch.setenv("IN_DEVBOX", "1")
    for verb in (["up"], ["test"], ["down"]):
        assert nested.main(verb) == 1
    assert "box" in capsys.readouterr().err


def test_main_says_why_it_refused(l1, capsys):
    l1.status = "Stopped"
    assert nested.main(["test"]) == 1
    assert "nested-up" in capsys.readouterr().err


# ── what comes back from the L1 ──────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("raw", "shown"),
    [
        ("plain line\n", "plain line\n"),
        ("\x1b[31mred\x1b[0m\n", "red\n"),
        ("\x1b]52;c;ZXZpbA==\x07after\n", "after\n"),  # OSC 52: write to the clipboard
        ("\x1b]0;title\x1b\\after\n", "after\n"),
        ("a\rb\x08c\x00d\n", "abcd\n"),
        ("tab\tkept\n", "tab\tkept\n"),
        ("\x9b31mcsi\n", "31mcsi\n"),  # the 8-bit introducer goes too
        ("✓ passed — ok\n", "✓ passed — ok\n"),
    ],
)
def test_output_from_the_l1_reaches_the_terminal_without_control_sequences(raw, shown):
    assert nested.clean(raw) == shown


# ── the commit's trip: real git ──────────────────────────────────────────────────────────


def _git(repo: Path, *args: str) -> str:
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@t",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@t",
    }
    return subprocess.run(
        ["git", "-C", str(repo), *args], env=env, capture_output=True, text=True, check=True
    ).stdout.strip()


@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "repo"
    r.mkdir()
    _git(r, "init", "-q", "-b", "main")
    (r / "f.txt").write_text("hello\n")
    _git(r, "add", "-A")
    _git(r, "commit", "-qm", "one")
    return r


def test_a_commit_arrives_whole_with_no_ref_written_on_this_side(repo, tmp_path):
    sha = nested._rev_parse(repo, "HEAD")
    before = _git(repo, "for-each-ref")
    pack = subprocess.run(
        nested.pack_argv(repo), input=f"{sha}\n".encode(), capture_output=True, env=nested.git_env()
    )
    assert pack.returncode == 0 and pack.stdout
    assert _git(repo, "for-each-ref") == before


def test_the_pack_rebuilds_the_commit_elsewhere(repo, tmp_path):
    sha = nested._rev_parse(repo, "HEAD")
    pack = subprocess.run(
        nested.pack_argv(repo), input=f"{sha}\n".encode(), capture_output=True, env=nested.git_env()
    )
    other = tmp_path / "other"
    other.mkdir()
    _git(other, "init", "-q")
    subprocess.run(
        ["git", "-C", str(other), "index-pack", "--stdin"], input=pack.stdout, check=True,
        capture_output=True,
    )  # fmt: skip
    _git(other, "update-ref", "refs/heads/nested", sha)
    assert _git(other, "show", "nested:f.txt") == "hello"


def test_a_pack_runs_no_hook_the_repo_names(repo, tmp_path):
    canary = tmp_path / "ran"
    hook = repo / ".git/hooks/reference-transaction"
    hook.write_text(f"#!/bin/sh\ntouch {canary}\n")
    hook.chmod(0o755)
    sha = nested._rev_parse(repo, "HEAD")
    subprocess.run(
        nested.pack_argv(repo), input=f"{sha}\n".encode(), capture_output=True,
        env=nested.git_env(), check=True,
    )  # fmt: skip
    assert not canary.exists()


def test_git_here_uses_foldyards_no_repo_code_flags():
    # A repo's config is box-writable, and reading an object can run a command it names. These
    # are worktree.py's reasoned-out flags; the tool is stdlib-only, so a copy held equal here.
    assert nested.NO_REPO_CODE == worktree._NO_REPO_CODE
    env = nested.git_env()
    for key, value in worktree._NO_FETCH_ENV.items():
        assert env[key] == value
    assert nested.pack_argv(Path("/r"))[1 : 1 + len(nested.NO_REPO_CODE)] == list(
        nested.NO_REPO_CODE
    )


@pytest.mark.parametrize("ref", ["no-such-ref", "--upload-pack=x"])
def test_a_ref_that_doesnt_name_a_commit_is_refused(repo, ref):
    with pytest.raises(nested.Refused, match="commit"):
        nested._rev_parse(repo, ref)


# ── the scripts that run in the L1 ───────────────────────────────────────────────────────


@pytest.mark.parametrize("script", ["provision.sh", "run-tier.sh"])
def test_the_l1_scripts_parse(script):
    path = ROOT / "tests/tools/nested_l1" / script
    subprocess.run(["bash", "-n", str(path)], check=True)


def test_provisioning_sets_the_traps_nested_checks_for():
    for arg in nested.TRAP_ARGS:
        assert f"{arg}=1" in PROVISION


def test_the_wall_lets_dns_out_before_it_drops_private_addresses():
    # Dropping the gateway first took DNS, and so all public egress, with it (live, 2026-11-04).
    rules = [ln.strip() for ln in PROVISION.splitlines() if "ip daddr" in ln]
    assert [("dport 53" in r) for r in rules] == [True, True, False]
    assert rules[-1].endswith("drop")
    assert "192.168.0.0/16" in rules[-1]


def test_the_wall_comes_up_at_boot_and_exempts_nobody():
    assert "systemctl enable" in PROVISION
    assert "Before=network-pre.target" in PROVISION
    assert "skuid" not in PROVISION


def test_the_tester_is_not_given_sudo_and_a_stale_grant_goes():
    # Lima's user holds the whole default subordinate range; a plain useradd fails (live).
    assert "useradd -m -K SUB_UID_COUNT=0 -K SUB_GID_COUNT=0" in PROVISION
    assert nested.SUDO_GRANT in PROVISION
    assert re.search(rf"rm -f \S*{re.escape(nested.SUDO_GRANT)}", PROVISION)
    assert "NOPASSWD" not in PROVISION


def test_the_lima_in_the_l1_is_cis():
    assert f'LIMA_VERSION: "{nested.LIMA_VERSION}"' in WORKFLOW


def test_the_tier_runs_with_cis_settings():
    job = WORKFLOW[WORKFLOW.index("lima-host-e2e:") : WORKFLOW.index("wsl2-host-e2e:")]
    for name in ("FOLDYARD_E2E", "FOLDYARD_ENGINE", "MACHINE_BACKEND", "MACHINE_CPUS"):
        ci = re.search(rf'^\s+{name}: "?([^"\n]+)"?', job, re.M)
        assert ci, name
        assert f"{name}={ci[1]}" in RUN_TIER
    assert "FY_BASE_VM_NAME" in RUN_TIER
    assert "FY_E2E_MODULES" in RUN_TIER
    assert "machine base bake" in RUN_TIER
