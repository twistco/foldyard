"""verify.py — the isolation battery. The engine + git are mocked (golden-ish): we assert
the right probes run and that PASS/FAIL + the exit code follow the probe results, without a
real engine. The injectors' box-dummy rows are unit-tested in test_inject_kinds.py."""

from __future__ import annotations

import errno
import os
import pathlib
import types
from typing import Any, ClassVar

import pytest

from foldyard import stack, verify
from foldyard.plugins import proxy


class _Proc:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


@pytest.fixture
def secure_engine(monkeypatch, tmp_path):
    """Fake engine + git whose probes report a SOUND boundary; stack.resolve mocked so no
    real git/machine runs. Returns (recorded calls, a mutable results dict to make a probe
    report insecurely)."""
    calls: list[list[str]] = []
    results: dict[str, Any] = {
        "rootless": True,
        "escape_rc": 1,  # --privileged --pid=host refused
        # The VM's mount table = PID 1's, read via --pid=host (the container's own `mount`
        # never shows VM-level mounts — see the namespace test below).
        "pid1_mounts": "proc /proc proc rw 0 0\ntmpfs /tmp tmpfs rw 0 0\n",
        "mount_rc": 0,
        "probe_rc": 0,  # the positive control: the probe image CAN run
        "has_commits": True,
        "git_rc": 1,  # origin unreachable…
        "git_stderr": "git@github.com: Permission denied (publickey).",  # …for CREDENTIAL reasons
        "ps": "",  # no stack containers ⇒ the advisory health section skips
        "ps_rc": 0,
        "inspect": "",
        "inspect_rc": 0,
        # ADR-0033's to-box write probe: what the --privileged container binding it rw reports
        "to_box_probe": "remount=refused\nwrite=refused\n",
        "to_box_rc": 0,
        # `git config --show-origin --get-regexp <credential helpers>`: 1 = none configured
        "config_rc": 1,
        "config_out": "",
        # Asked only when the proxy refused the dry-run push as a push: origin's push URL, and
        # what `git credential fill` finds for it (128 + "terminal prompts disabled" = nothing).
        "push_url": "https://github.com/acme/app.git\n",
        "push_url_rc": 0,
        "cred_rc": 128,
        "cred_out": "",
        "cred_err": "fatal: could not read Username for 'https://github.com': terminal prompts "
        "disabled\n",
        # `ssh -G <dest>`: the effective ssh config for an SSH origin's host (walled boxes only)
        "ssh_g": "user git\nhostname github.com\nport 22\nbatchmode no\n",
        "ssh_rc": 0,
    }

    def fake_run(cmd, **kw):
        calls.append(cmd)
        if cmd[0] == "ssh":
            results.setdefault("ssh_cmds", []).append(cmd)
            ssh_g = results["ssh_g"]
            return _Proc(results["ssh_rc"], str(ssh_g(cmd[-1]) if callable(ssh_g) else ssh_g))
        if cmd[0] == "git" and "rev-parse" in cmd:  # does HEAD name a commit?
            if results["has_commits"] is None:
                raise verify.subprocess.TimeoutExpired(cmd, kw.get("timeout") or 0)
            return _Proc(0 if results["has_commits"] else 1)
        if cmd[0] == "git" and "config" in cmd:  # the credential-helper read, not the push
            results["config_cmd"], results["config_env"] = cmd, kw.get("env")
            return _Proc(results["config_rc"], results["config_out"])
        if cmd[0] == "git" and "get-url" in cmd:
            results["url_cmd"] = cmd
            return _Proc(results["push_url_rc"], results["push_url"])
        if cmd[0] == "git" and "credential" in cmd:
            if results["cred_rc"] is None:
                raise verify.subprocess.TimeoutExpired(cmd, kw.get("timeout") or 0)
            results["cred_cmd"], results["cred_input"] = cmd, kw.get("input")
            results["cred_env"] = kw.get("env")
            return _Proc(results["cred_rc"], results["cred_out"], results["cred_err"])
        if cmd[0] == "git":
            results["git_env"] = kw.get("env")
            results["git_cmd"] = cmd
            return _Proc(results["git_rc"], stderr=results["git_stderr"])
        if cmd[1] == "ps":
            return _Proc(results["ps_rc"], results["ps"])
        if cmd[1] == "inspect":
            return _Proc(results["inspect_rc"], results["inspect"])
        if cmd[1] == "info":
            if "SecurityOptions" in cmd[-1]:
                return _Proc(
                    0, "[name=seccomp name=rootless]" if results["rootless"] else "[name=seccomp]"
                )
            return _Proc(0, "true" if results["rootless"] else "false")
        if cmd[1] == "run":
            tail = cmd[-1]
            if "cat /proc/1/ns/ipc" in tail:
                return _Proc(results["escape_rc"])
            if tail == "mount":  # the CONTAINER's namespace: always clean, never the evidence
                return _Proc(0, "proc /proc proc rw 0 0\n")
            if tail == "cat /proc/1/mounts" and "--pid=host" in cmd:
                return _Proc(results["mount_rc"], results["pid1_mounts"])
            if tail == "true":  # the positive control
                return _Proc(results["probe_rc"])
            if any(str(c).endswith(":/fy-to-box") for c in cmd):
                results["to_box_cmd"] = cmd
                return _Proc(results["to_box_rc"], results["to_box_probe"])
        return _Proc(0, "")

    monkeypatch.setattr(verify.subprocess, "run", fake_run)
    ctx = stack.Context(
        main=tmp_path,
        env={"FOLDYARD_CHECKOUT": str(tmp_path), "CONTAINER_HOST": "unix:///s"},
        compose=[],
        app="app",
        project="p",
        worktree="",
    )
    monkeypatch.setattr(verify.stack, "resolve", lambda *a, **k: ctx)
    # The engine is faked, so the machine gate passes (the stopped-VM refusal is tested below).
    monkeypatch.setattr(verify.stack, "engine_reachable", lambda *a, **k: True)
    monkeypatch.setattr(verify.config, "in_box", lambda: False)  # VM-boundary only by default
    # On your computer the firewall adds the loopback row, a subject with its own tests below.
    # Unpinned it reads the repo's own foldyard.toml (firewall on) — see _enter_box's note.
    monkeypatch.setattr(verify.config, "machine_wall", lambda: False)
    # A real dev box bakes the operator's home; the tests build their tables from Path.home().
    monkeypatch.delenv("FY_HOST_HOME", raising=False)
    # No to-box unless a test gives one (the repo's own foldyard.toml would otherwise decide).
    monkeypatch.setattr(verify.boxmounts, "vm_paths", lambda: ("", ""))
    monkeypatch.setattr(verify.machine, "to_box_mounted", lambda: False)
    monkeypatch.delenv("FY_TO_BOX", raising=False)
    return calls, results


def _enter_box(monkeypatch, tmp_path):
    """Flip to in-box with a clean HOME + no gh + no token env, so only the probe results vary."""
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    monkeypatch.setattr(verify.config, "in_box", lambda: True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("SSH_AUTH_SOCK", raising=False)
    for v in verify._GIT_BRIDGE_VARS:
        monkeypatch.delenv(v, raising=False)
    monkeypatch.setattr(verify, "_BRIDGE_SOCKET_DIR", tmp_path / "tmp")
    (tmp_path / "tmp").mkdir(exist_ok=True)
    # The socket row also reads $TMPDIR and $XDG_RUNTIME_DIR — on a macOS host TMPDIR is the
    # operator's own temp dir, where their VS Code keeps real `vscode-ipc-*.sock` files.
    monkeypatch.setenv("TMPDIR", str(tmp_path / "tmp"))
    monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
    monkeypatch.delenv("GH_TOKEN", raising=False)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.setattr(verify, "which", lambda c: None)
    # No proxy CA mounted.
    monkeypatch.setattr(proxy, "BOX_CA", tmp_path / "no-ca.pem")
    # The wall section is a different subject with its own tests below, and it needs a live proxy
    # + network. Left unpinned it resolves the REPO's OWN foldyard.toml — so this repo declaring
    # `[machine] firewall = true` silently bolted a failing check onto every test here: green locally,
    # where a dev box exports HTTPS_PROXY ambiently, red in CI where nothing does. The `== 1` tests
    # kept passing throughout, on the wall's failure rather than their own subject's.
    monkeypatch.setattr(verify.config, "machine_wall", lambda: False)
    # The push-refusal row talks to the box's proxy over the network: stubbed to the proxy's
    # refusal (its own tests are below), and with no proxy in the env unless a test sets one.
    monkeypatch.delenv("HTTPS_PROXY", raising=False)
    monkeypatch.delenv("https_proxy", raising=False)
    monkeypatch.setattr(verify, "_probe_push", lambda host, port: _REFUSED_ANSWER)
    return home


_REFUSED_ANSWER = verify._PushAnswer(
    status=403,
    body=b"refused by the foldyard proxy - the box doesn't push; push from your computer\n",
    allowed=False,
    decrypted=True,
)


# ── VM boundary ────────────────────────────────────────────────────────────────────────


def test_a_stopped_machine_is_a_refusal_never_booted_or_passed(secure_engine, monkeypatch):
    # Verify CHECKS the boundary: booting the VM would make the check the thing that changed the
    # host, and a battery that never ran must not read as a pass.
    calls, _ = secure_engine
    resolved: list = []
    monkeypatch.setattr(verify.stack, "resolve", lambda *a, **k: resolved.append(1))
    monkeypatch.setattr(verify.stack, "engine_reachable", lambda *a, **k: False)
    assert verify.verify() == 1
    assert resolved == [] and calls == []


def test_all_pass_returns_0(secure_engine, capsys):
    assert verify.verify() == 0
    out = capsys.readouterr().out
    assert "ALL PASS" in out
    # The 3 VM-boundary checks; posture skipped. (Was 4: `ls /Users` inside the container read
    # the container's namespace and never saw a VM mount — folded into the PID-1 mount audit.)
    assert out.count("✓ PASS") == 3
    assert "skipped (not inside the box" in out


def test_escape_breakout_is_fail(secure_engine, capsys):
    _, results = secure_engine
    results["escape_rc"] = 0  # the --pid=host probe SUCCEEDS → breakout
    assert verify.verify() == 1
    out = capsys.readouterr().out
    assert "breakout" in out and "1 check(s) FAILED" in out


def test_not_rootless_is_fail(secure_engine):
    _, results = secure_engine
    results["rootless"] = False
    assert verify.verify() == 1


def test_host_mount_leak_is_fail(secure_engine, capsys):
    _, results = secure_engine
    results["pid1_mounts"] = "macfuse /Users/dain osxfuse rw 0 0\n"
    assert verify.verify() == 1
    assert "host paths" in capsys.readouterr().out


def test_escape_probe_command_shape(secure_engine):
    calls, _ = secure_engine
    verify.verify()
    assert any(c[1] == "run" and "--privileged" in c and "--pid=host" in c for c in calls)


def test_vm_level_mount_hidden_from_the_container_namespace_is_still_a_fail(secure_engine, capsys):
    # Found 2026-09-11 on the Linux rig: a Lima VM mounting ALL of the operator's home passed
    # "VM mount table free of host home/paths", because `mount` inside a --privileged container
    # prints the CONTAINER's mount namespace — VM-level mounts are not in it. PID 1's table is.
    # The fixture answers a bare `mount` with a clean table regardless, so reading the wrong
    # namespace again would turn this green.
    calls, results = secure_engine
    results["pid1_mounts"] = f"mount0 {pathlib.Path.home()} 9p rw,relatime 0 0\n"
    assert verify.verify() == 1
    assert "host paths" in capsys.readouterr().out
    assert not any(c[1] == "run" and c[-1] == "mount" for c in calls)


def test_the_isolation_mount_set_is_not_a_leak(secure_engine, monkeypatch, capsys):
    # Reading the REAL table means seeing the mounts foldyard itself makes: the repo and the
    # worktrees root, at their host paths (`machine._volumes`: host==guest). Those two are the
    # whole point, not a leak — exempt by exact mountpoint, so `$HOME` itself, or anything
    # else under it, still fails.
    _, results = secure_engine
    main = pathlib.Path.home() / "ws" / "repo"
    wt_root = pathlib.Path.home() / "ws" / "repo-worktrees"
    monkeypatch.setenv("FOLDYARD_WORKTREES_ROOT", str(wt_root))
    ctx = stack.Context(
        main=main,
        env={"CONTAINER_HOST": "unix:///s"},
        compose=[],
        app="app",
        project="p",
        worktree="",
    )
    monkeypatch.setattr(verify.stack, "resolve", lambda *a, **k: ctx)
    results["pid1_mounts"] = (
        "/dev/vda2 / btrfs rw 0 0\n"
        f"lima-1 {main} virtiofs rw,relatime 0 0\n"
        f"lima-2 {wt_root} virtiofs rw,relatime 0 0\n"
    )
    assert verify.verify() == 0
    assert "free of host home/paths" in capsys.readouterr().out
    # …but a sibling under the home, or the home itself, is not covered by the exemption.
    results["pid1_mounts"] += f"lima-3 {pathlib.Path.home() / 'ws' / 'other'} virtiofs rw 0 0\n"
    assert verify.verify() == 1


def test_a_host_path_string_in_the_mount_options_is_not_a_leak(secure_engine, monkeypatch, capsys):
    # Linux rig, 2026-09-13: run INSIDE the box, `Path.home()` is /root, and a Fedora guest's btrfs
    # root line carries `subvol=/root` in its OPTIONS field — the mountpoint is `/`, nothing of
    # the host is exposed, yet a whole-line search flagged it. Only the mountpoint decides.
    _, results = secure_engine
    monkeypatch.setattr(verify.Path, "home", staticmethod(lambda: pathlib.Path("/root")))
    results["pid1_mounts"] = (
        "/dev/vda3 / btrfs rw,seclabel,relatime,compress=zstd:1,subvolid=256,subvol=/root 0 0\n"
    )
    assert verify.verify() == 0
    assert "free of host home/paths" in capsys.readouterr().out
    # …while the same path AS the mountpoint is still the leak it always was.
    results["pid1_mounts"] = "lima-1 /root 9p rw,relatime 0 0\n"
    assert verify.verify() == 1


def test_in_box_the_audit_judges_by_the_operators_home_not_the_box_users(
    secure_engine, monkeypatch, capsys
):
    # In-box on a LINUX host, `Path.home()` is the box user's (/root) — so a Lima VM mounting the
    # operator's /home/<user> passed, there being no `/Users` to catch it and the pattern
    # looking for the wrong home. `fy box up` bakes the host's home as FY_HOST_HOME; the audit
    # judges by that when present.
    _, results = secure_engine
    monkeypatch.setattr(verify.Path, "home", staticmethod(lambda: pathlib.Path("/root")))
    monkeypatch.setenv("FY_HOST_HOME", "/home/operator")
    results["pid1_mounts"] = "lima-1 /home/operator 9p rw,relatime 0 0\n"
    assert verify.verify() == 1
    assert "host paths" in capsys.readouterr().out
    # The guest's own user shares the prefix and is still not a leak…
    results["pid1_mounts"] = "lima-1 /home/operator.linux 9p rw,relatime 0 0\n"
    assert verify.verify() == 0
    # …and a box from before the bake falls back to the process's own home, as before.
    monkeypatch.delenv("FY_HOST_HOME")
    results["pid1_mounts"] = "lima-1 /root 9p rw,relatime 0 0\n"
    assert verify.verify() == 1


# ── false passes: a negative check needs a positive control ──────────────────────────────
# Every check below asserts an ABSENCE, so it reports PASS when its probe command FAILS. A probe
# that could not run at all therefore reads as a clean bill of health. This bit in the wild:
# `verify` printed "ALL PASS — isolation intact" against a VM with no images, behind a wall with
# no proxy running — i.e. foldyard's own default posture with a cold cache.


def test_unrunnable_probe_image_cannot_report_a_sound_boundary(secure_engine, capsys):
    _, results = secure_engine
    results["probe_rc"] = 125  # image missing / cannot pull ⇒ NOTHING ran
    assert verify.verify() != 0
    out = capsys.readouterr().out
    assert "escape refused" not in out, "a container that never started is not a refused escape"
    assert "ALL PASS" not in out


def test_empty_mount_output_is_not_a_clean_mount_table(secure_engine, capsys):
    _, results = secure_engine
    results["pid1_mounts"] = ""  # command produced nothing — silence is not an all-clear
    results["mount_rc"] = 1
    assert verify.verify() != 0
    assert "free of host home/paths" not in capsys.readouterr().out


def test_host_paths_matches_the_real_host_home_not_just_macos(monkeypatch, tmp_path):
    # The regex was /Users|/private|/var/folders|/Volumes — every member macOS-only, so the
    # mount assertion passed vacuously on any Linux or WSL2 host.
    monkeypatch.delenv("FY_HOST_HOME", raising=False)  # set in a dev box; it wins over home()
    monkeypatch.setattr(verify.Path, "home", staticmethod(lambda: pathlib.Path("/home/dain")))
    assert verify._host_paths().search("host /home/dain/workspace/repo type virtiofs")


def test_host_paths_does_not_false_positive_on_a_guest_home_sharing_the_prefix(monkeypatch):
    # Lima's guest user is <user>.linux, so /home/dain must not match /home/dain.linux.
    monkeypatch.delenv("FY_HOST_HOME", raising=False)  # set in a dev box; it wins over home()
    monkeypatch.setattr(verify.Path, "home", staticmethod(lambda: pathlib.Path("/home/dain")))
    assert not verify._host_paths().search("x /home/dain.linux/.cache type ext4")


def test_wsl_windows_drive_mount_counts_as_a_host_leak(monkeypatch):
    monkeypatch.delenv("FY_HOST_HOME", raising=False)  # set in a dev box; it wins over home()
    monkeypatch.setattr(verify.Path, "home", staticmethod(lambda: pathlib.Path("/home/dain")))
    assert verify._host_paths().search("C:\\ /mnt/c type 9p")


def test_git_network_failure_does_not_prove_the_push_refusal(secure_engine, monkeypatch, tmp_path):
    # `git ls-remote` failing because the box has no egress says NOTHING about credentials.
    _enter_box(monkeypatch, tmp_path)
    _, results = secure_engine
    results["git_rc"] = 128
    results["git_stderr"] = (
        "fatal: unable to access 'https://github.com/x/y': Could not resolve host: github.com"
    )
    assert verify.verify() != 0


def test_git_missing_origin_is_named_not_blamed_on_the_network(
    secure_engine, monkeypatch, tmp_path, capsys
):
    # The shared .git/config lost its remotes (issue #6): git says 'origin' is not a repository.
    # Still UNPROVEN — but the message must point at the config, not at egress/credentials,
    # which is the box's own posture machinery and the wrong place to look.
    _enter_box(monkeypatch, tmp_path)
    _, results = secure_engine
    results["git_rc"] = 128
    results["git_stderr"] = (
        "fatal: 'origin' does not appear to be a git repository\n"
        "fatal: Could not read from remote repository.\n"
    )
    assert verify.verify() != 0
    out = capsys.readouterr().out
    assert "no `origin` remote" in out and "UNPROVEN" in out and "fy doctor" in out


def test_the_push_probe_runs_git_as_the_box_does(secure_engine, monkeypatch, tmp_path):
    # Git in the box reaches origin the way any box process does: through the proxy the box's
    # env names, trusting the box's CA bundle. The probe took the COMPOSE client's env instead,
    # which since 4925b8b is an allowlist with no proxy in it — so in a walled box git went
    # direct, the VM firewall refused it, and every walled box with an origin reported "git
    # remote UNREACHABLE … push refusal UNPROVEN" (found live 2026-10-12).
    _enter_box(monkeypatch, tmp_path)
    monkeypatch.setenv("HTTPS_PROXY", "http://192.168.5.2:41000")
    monkeypatch.setenv("NO_PROXY", "localhost")
    monkeypatch.setenv("GIT_SSL_CAINFO", "/etc/dev-proxy-ca-combined.pem")
    _, results = secure_engine
    verify.verify()
    env = results["git_env"]
    assert env["HTTPS_PROXY"] == "http://192.168.5.2:41000"
    assert env["NO_PROXY"] == "localhost"
    assert env["GIT_SSL_CAINFO"] == "/etc/dev-proxy-ca-combined.pem"
    assert "BatchMode=yes" in env["GIT_SSH_COMMAND"]  # never prompts…
    assert env["GIT_TERMINAL_PROMPT"] == "0"  # …over either transport


def test_the_push_probe_is_a_dry_run_push_not_a_read(secure_engine, monkeypatch, tmp_path):
    # `ls-remote` is a READ: a public origin answers it with no credential, so the probe read
    # "the box can push" on every public repo, and a read-only credential would too. The push
    # handshake needs WRITE access even on a public repo, and --dry-run sends nothing.
    _enter_box(monkeypatch, tmp_path)
    _, results = secure_engine
    verify.verify()
    cmd = results["git_cmd"]
    assert cmd[cmd.index("push") :] == ["push", "--dry-run", "origin", f"HEAD:{verify._PUSH_PROBE}"]
    assert "ls-remote" not in cmd


def test_the_push_probe_works_before_the_first_commit(secure_engine, monkeypatch, tmp_path):
    # No commit, no HEAD to push: git refuses locally without ever asking origin. Any object id
    # gets as far as the auth handshake, so offer the empty tree's.
    _enter_box(monkeypatch, tmp_path)
    _, results = secure_engine
    results["has_commits"] = False
    verify.verify()
    assert results["git_cmd"][-1] == f"{verify._EMPTY_TREE}:{verify._PUSH_PROBE}"


def test_a_stuck_head_probe_leaves_the_push_refusal_unproven(
    secure_engine, monkeypatch, tmp_path, capsys
):
    # Box-side git runs through the index shim, which can stall: a timed-out HEAD lookup says
    # nothing about the checkout, so it must not fall back to the empty tree — or pass.
    _enter_box(monkeypatch, tmp_path)
    _, results = secure_engine
    results["has_commits"] = None
    assert verify.verify() != 0
    out = capsys.readouterr().out
    assert "UNPROVEN" in out and "git push refused" not in out
    assert "git_cmd" not in results  # never pushed


def test_git_auth_failure_does_prove_the_push_refusal(secure_engine, monkeypatch, tmp_path, capsys):
    _enter_box(monkeypatch, tmp_path)
    _, results = secure_engine
    results["git_rc"] = 128
    results["git_stderr"] = (
        "git@github.com: Permission denied (publickey).\nfatal: Could not read from remote repository."
    )
    verify.verify()
    assert "git push refused" in capsys.readouterr().out


# ── dev-box posture ──────────────────────────────────────────────────────────────────────


def test_box_posture_clean_passes(secure_engine, monkeypatch, tmp_path, capsys):
    _enter_box(monkeypatch, tmp_path)
    assert verify.verify() == 0
    out = capsys.readouterr().out
    assert "no SSH agent" in out and "no ~/.netrc" in out and "git push refused" in out
    # Name the offender rather than reporting a bare 1 != 0: this test went red in CI once
    # because ambient config added a whole section nobody here asked for.
    assert "FAIL" not in out, out


def test_git_refused_by_a_missing_ssh_client_proves_the_push_refusal(
    secure_engine, monkeypatch, tmp_path, capsys
):
    # An SSH origin in a box that ships no ssh client: git cannot even start the transport. With
    # the rows above already asserting no keys and no agent, that absence IS the refusal — no
    # credential can reach origin over a transport that does not exist — and must not read as
    # "unreachable for network reasons" (which left every SSH-origin consumer short of ALL PASS).
    _, results = secure_engine
    results["git_rc"], results["git_stderr"] = (
        128,
        ("error: cannot run ssh: No such file or directory\nfatal: unable to fork\n"),
    )
    _enter_box(monkeypatch, tmp_path)
    assert verify.verify() == 0
    assert "git push refused" in capsys.readouterr().out


def test_git_timeout_is_still_not_a_refusal(secure_engine, monkeypatch, tmp_path, capsys):
    _, results = secure_engine
    results["git_rc"], results["git_stderr"] = (
        128,
        "ssh: connect to host github.com port 22: Connection timed out\n",
    )
    _enter_box(monkeypatch, tmp_path)
    assert verify.verify() == 1
    assert "UNPROVEN" in capsys.readouterr().out


# ── an SSH origin behind the VM firewall ─────────────────────────────────────────────────

_SSH_REFUSED_STDERR = (
    "ssh: connect to host github.com port 22: Connection refused\n"
    "fatal: Could not read from remote repository.\n\n"
    "Please make sure you have the correct access rights\nand the repository exists.\n"
)


def _ssh_origin(
    secure_engine,
    monkeypatch,
    tmp_path,
    *,
    wall=True,
    push_url="git@github.com:acme/app.git\n",
    connects=(),
    timeouts=(),
    unreachable=(),
    resolves=True,
    proxy_up=True,
):
    """An SSH origin whose dry-run push died at the connect, as in a walled box. `connects` are
    the (host, port)s a direct connect reaches, `timeouts` those it times out on, `unreachable`
    those an upstream ICMP reply turns away; everything else is refused, the wall's reset."""
    _enter_box(monkeypatch, tmp_path)
    monkeypatch.setattr(verify.config, "machine_backend", lambda: "lima")
    monkeypatch.setattr(verify.config, "machine_wall", lambda: wall)
    _, results = secure_engine
    results["git_rc"], results["git_stderr"] = 128, _SSH_REFUSED_STDERR
    results["push_url"] = push_url
    _wall_net(monkeypatch, proxy_up=proxy_up)
    wall_connect = verify.socket.create_connection
    dialled: list[tuple[str, int]] = []

    def connect(addr, *a, **kw):
        dialled.append(addr)
        if tuple(addr) in connects:
            return _Sock()
        if tuple(addr) in timeouts:
            raise TimeoutError("timed out")
        if tuple(addr) in unreachable:
            raise OSError(errno.EHOSTUNREACH, "No route to host")
        return wall_connect(addr, *a, **kw)

    def getaddrinfo(host, port, *a, **kw):
        if not resolves:
            raise verify.socket.gaierror(-2, "Name or service not known")
        return [(verify.socket.AF_INET, verify.socket.SOCK_STREAM, 6, "", ("140.82.121.4", port))]

    monkeypatch.setattr(verify.socket, "create_connection", connect)
    monkeypatch.setattr(verify.socket, "getaddrinfo", getaddrinfo)
    results["dialled"] = dialled
    return results


def test_an_ssh_origin_the_vm_firewall_refuses_proves_the_push_refusal(
    secure_engine, monkeypatch, tmp_path, capsys
):
    # The walled box can't open a TCP connection to the SSH host at all, so a push is impossible
    # whatever credential the box holds — the refusal, proven directly, not a network outage.
    results = _ssh_origin(secure_engine, monkeypatch, tmp_path)
    assert verify.verify() == 0
    out = capsys.readouterr().out
    assert "SSH push impossible: the VM firewall refuses github.com:22" in out
    assert "UNREACHABLE" not in out
    assert ("github.com", 22) in results["dialled"]
    assert results["url_cmd"][-4:] == ["get-url", "--push", "--all", "origin"]
    assert results["ssh_cmds"][0][:2] == ["ssh", "-G"]


def test_an_ssh_connect_that_times_out_warns_instead_of_proving_the_wall(
    secure_engine, monkeypatch, tmp_path, capsys
):
    # The wall rejects; a timeout looks the same as a network upstream dropping port 22, so it
    # can't be credited to the wall. Blocked all the same, so a warning, not a failure.
    _ssh_origin(secure_engine, monkeypatch, tmp_path, timeouts={("github.com", 22)})
    assert verify.verify() == 0
    out = capsys.readouterr().out
    assert "SSH push impossible" not in out
    assert "UNREACHABLE" not in out
    assert "SSH push blocked: github.com:22 (timed out)" in out
    assert "not proof the VM firewall" in out


def test_an_ssh_host_turned_away_upstream_warns_instead_of_proving_the_wall(
    secure_engine, monkeypatch, tmp_path, capsys
):
    # Only a reset is the wall's signature. "No route to host" is an ICMP reply from somewhere
    # upstream, which can't be credited to the wall either.
    _ssh_origin(secure_engine, monkeypatch, tmp_path, unreachable={("github.com", 22)})
    assert verify.verify() == 0
    out = capsys.readouterr().out
    assert "SSH push impossible" not in out
    assert "SSH push blocked: github.com:22 (No route to host)" in out


def test_one_timed_out_ssh_push_url_makes_the_whole_row_a_warning(
    secure_engine, monkeypatch, tmp_path, capsys
):
    results = _ssh_origin(
        secure_engine,
        monkeypatch,
        tmp_path,
        push_url="git@github.com:acme/app.git\ngit@gitlab.com:acme/app.git\n",
        timeouts={("gitlab.com", 22)},
    )
    results["ssh_g"] = lambda dest: f"hostname {dest.split('@')[-1]}\nport 22\n"
    assert verify.verify() == 0
    out = capsys.readouterr().out
    assert "SSH push impossible" not in out
    assert "gitlab.com:22 (timed out)" in out


def test_the_ssh_probe_dials_what_ssh_would_dial(secure_engine, monkeypatch, tmp_path, capsys):
    # ssh's own config decides the host and port (a `Host` alias, `Port 443`), so the probe asks
    # `ssh -G` for them, passing the URL's port, rather than assuming the URL's host on 22.
    results = _ssh_origin(
        secure_engine, monkeypatch, tmp_path, push_url="ssh://git@gh-alias:2222/acme/app.git\n"
    )
    results["ssh_g"] = "user git\nhostname ssh.github.com\nport 443\n"
    assert verify.verify() == 0
    out = capsys.readouterr().out
    assert "SSH push impossible: the VM firewall refuses ssh.github.com:443" in out
    assert results["ssh_cmds"][0][-4:] == ["-p", "2222", "--", "git@gh-alias"]
    assert ("ssh.github.com", 443) in results["dialled"]


def test_without_the_vm_firewall_an_unreachable_ssh_origin_stays_unproven(
    secure_engine, monkeypatch, tmp_path, capsys
):
    results = _ssh_origin(secure_engine, monkeypatch, tmp_path, wall=False)
    assert verify.verify() == 1
    out = capsys.readouterr().out
    assert "UNREACHABLE" in out and "VM firewall refuses" not in out
    assert results["dialled"] == []  # no wall, nothing to prove: not even probed
    # The row names ssh's own complaint, not git's closing boilerplate.
    assert "(ssh: connect to host github.com port 22: Connection refused)" in out


def test_an_ssh_host_the_box_can_connect_to_is_not_walled(
    secure_engine, monkeypatch, tmp_path, capsys
):
    # A grant, or a wall that isn't enforcing: the connect succeeds, so the push failed for some
    # other reason and nothing about the wall is proven.
    _ssh_origin(secure_engine, monkeypatch, tmp_path, connects={("github.com", 22)})
    assert verify.verify() == 1
    out = capsys.readouterr().out
    assert "UNREACHABLE" in out and "VM firewall refuses" not in out


def test_an_ssh_host_that_does_not_resolve_is_not_the_wall(
    secure_engine, monkeypatch, tmp_path, capsys
):
    _ssh_origin(secure_engine, monkeypatch, tmp_path, resolves=False)
    assert verify.verify() == 1
    out = capsys.readouterr().out
    assert "UNREACHABLE" in out and "VM firewall refuses" not in out


def test_an_ssh_host_reached_through_a_proxy_command_is_not_walled(
    secure_engine, monkeypatch, tmp_path, capsys
):
    # A ProxyCommand/ProxyJump carries ssh some other way (the egress proxy's CONNECT, say), so a
    # refused direct connect says nothing about whether ssh gets out.
    results = _ssh_origin(secure_engine, monkeypatch, tmp_path)
    results["ssh_g"] = "hostname ssh.github.com\nport 443\nproxycommand nc -X connect %h %p\n"
    assert verify.verify() == 1
    assert "VM firewall refuses" not in capsys.readouterr().out
    results["ssh_g"] = "hostname github.com\nport 22\nproxyjump bastion\n"
    assert verify.verify() == 1
    assert "VM firewall refuses" not in capsys.readouterr().out


def test_an_unreadable_ssh_config_is_not_walled(secure_engine, monkeypatch, tmp_path, capsys):
    results = _ssh_origin(secure_engine, monkeypatch, tmp_path)
    results["ssh_rc"], results["ssh_g"] = 255, ""
    assert verify.verify() == 1
    assert "VM firewall refuses" not in capsys.readouterr().out


def test_an_offline_walled_box_does_not_prove_an_ssh_refusal(
    secure_engine, monkeypatch, tmp_path, capsys
):
    # The proxy is the wall's permitted path: if that is unreachable too, the refused connect is
    # an outage, not the wall.
    _ssh_origin(secure_engine, monkeypatch, tmp_path, proxy_up=False)
    assert verify.verify() == 1
    assert "VM firewall refuses" not in capsys.readouterr().out


def test_every_push_url_must_be_a_walled_ssh_one(secure_engine, monkeypatch, tmp_path, capsys):
    # git pushes to each push URL; one over HTTPS goes through the proxy, which the SSH proof
    # says nothing about.
    _ssh_origin(
        secure_engine,
        monkeypatch,
        tmp_path,
        push_url="git@github.com:acme/app.git\nhttps://gitlab.com/acme/app.git\n",
    )
    assert verify.verify() == 1
    assert "VM firewall refuses" not in capsys.readouterr().out


def test_an_https_origin_behind_the_wall_is_not_an_ssh_refusal(
    secure_engine, monkeypatch, tmp_path, capsys
):
    results = _ssh_origin(
        secure_engine, monkeypatch, tmp_path, push_url="https://github.com/acme/app.git\n"
    )
    results["git_stderr"] = "fatal: unable to access 'https://github.com/acme/app.git/': 503\n"
    assert verify.verify() == 1
    assert "VM firewall refuses" not in capsys.readouterr().out


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("git@github.com:acme/app.git", ("git@github.com", None)),
        ("github.com:acme/app.git", ("github.com", None)),
        ("ssh://git@github.com/acme/app.git", ("git@github.com", None)),
        ("ssh://git@github.com:2222/acme/app.git", ("git@github.com", "2222")),
        ("git+ssh://github.com/acme/app.git", ("github.com", None)),
        ("ssh+git://git@github.com:22/acme/app", ("git@github.com", "22")),
        ("SSH://git@[2001:db8::1]:2222/acme/app", ("git@2001:db8::1", "2222")),
        ("https://github.com/acme/app.git", None),
        ("http://github.com/acme/app.git", None),
        ("file:///srv/app.git", None),
        ("/srv/app.git", None),
        ("./a:b", None),  # a slash before the colon: a local path, not scp-like
        ("git@[::1]:acme/app", None),  # bracketed scp-like: not parsed, so never passed
        ("", None),
    ],
)
def test_ssh_destination_reads_git_ssh_urls(url, expected):
    assert verify._ssh_destination(url) == expected


def test_mount_audit_under_gvisor_is_not_applicable_not_a_failure(
    secure_engine, monkeypatch, tmp_path, capsys
):
    # `--pid=host` into the VM is exactly what gVisor blocks (the "escape refused" property), so
    # the in-box mount audit cannot run under the posture — that is not a leak and must not FAIL.
    # It is N/A here (fulfilled host-side / on a crun box), not an advisory: nothing to act on.
    _, results = secure_engine
    results["pid1_mounts"] = ""  # the probe reaches only the sandbox: nothing to read
    _enter_box(monkeypatch, tmp_path)
    monkeypatch.setenv("FY_MACHINE_RUNTIME", "gvisor")
    monkeypatch.setattr(verify, "_kernel_release", lambda: "4.19.0-gvisor")
    assert verify.verify() == 0  # N/A, not a fail
    out = capsys.readouterr().out
    assert "N/A" in out and "runs at another layer" in out and "escape refused" in out
    assert "WARN" not in out  # not softened to an advisory — it genuinely runs elsewhere


def test_mount_audit_empty_is_still_a_fail_under_crun(secure_engine, monkeypatch, tmp_path, capsys):
    _, results = secure_engine
    results["pid1_mounts"] = ""
    _enter_box(monkeypatch, tmp_path)
    monkeypatch.delenv("FY_MACHINE_RUNTIME", raising=False)
    assert verify.verify() == 1
    assert "UNPROVEN" in capsys.readouterr().out


def test_gvisor_posture_row_checks_the_kernel_the_box_actually_runs_on(
    secure_engine, monkeypatch, tmp_path, capsys
):
    _enter_box(monkeypatch, tmp_path)
    monkeypatch.setenv("FY_MACHINE_RUNTIME", "gvisor")
    monkeypatch.setattr(verify, "_kernel_release", lambda: "4.19.0-gvisor")
    assert verify.verify() == 0
    assert "gVisor" in capsys.readouterr().out
    monkeypatch.setattr(verify, "_kernel_release", lambda: "6.19.10-300.fc44.aarch64")
    assert verify.verify() == 1
    assert "NOT under gVisor" in capsys.readouterr().out


def test_no_gvisor_row_without_the_posture(secure_engine, monkeypatch, tmp_path, capsys):
    _enter_box(monkeypatch, tmp_path)
    monkeypatch.delenv("FY_MACHINE_RUNTIME", raising=False)
    monkeypatch.setattr(verify, "_kernel_release", lambda: "6.19.10")
    assert verify.verify() == 0
    assert "gVisor" not in capsys.readouterr().out


def test_git_remote_reachable_is_fail(secure_engine, monkeypatch, tmp_path, capsys):
    _, results = secure_engine
    results["git_rc"] = 0  # origin reachable → the box could push
    _enter_box(monkeypatch, tmp_path)
    assert verify.verify() == 1
    assert "push to origin was ACCEPTED" in capsys.readouterr().out  # ...and for THAT reason


def test_ssh_private_key_is_fail(secure_engine, monkeypatch, tmp_path, capsys):
    home = _enter_box(monkeypatch, tmp_path)
    ssh = home / ".ssh"
    ssh.mkdir()
    (ssh / "known_hosts").write_text("")  # harmless
    (ssh / "config").write_text("")  # harmless
    (ssh / "id_ed25519").write_text("KEY")  # a push path
    assert verify.verify() == 1
    assert "~/.ssh key material" in capsys.readouterr().out  # ...and for THAT reason


def test_a_forwarded_agent_is_fail_and_names_the_attach(
    secure_engine, monkeypatch, tmp_path, capsys
):
    # Seen live 2026-09-17: a VS Code attach forwarded the host's agent (1 key) into a box whose
    # posture read "never push". The row must name where it comes from, not just the var.
    _enter_box(monkeypatch, tmp_path)
    monkeypatch.setenv("SSH_AUTH_SOCK", "/tmp/vscode-ssh-auth-deadbeef.sock")
    assert verify.verify() == 1
    assert "SSH_AUTH_SOCK set" in capsys.readouterr().out


@pytest.mark.parametrize("var", ["GIT_ASKPASS", "VSCODE_GIT_IPC_HANDLE"])
def test_a_git_credential_bridge_is_fail(secure_engine, monkeypatch, tmp_path, capsys, var):
    # The other half of the same attach: git in the box asks the HOST's credential store — an
    # HTTPS push path with no key material anywhere in the box.
    _enter_box(monkeypatch, tmp_path)
    monkeypatch.setenv(var, "/root/.vscode-server/bin/x/extensions/git/dist/askpass.sh")
    assert verify.verify() == 1
    assert f"git-credential bridge to the host: {var}" in capsys.readouterr().out


def test_a_bridge_socket_on_disk_is_fail_even_with_the_vars_unset(
    secure_engine, monkeypatch, tmp_path, capsys
):
    # The socket is reachable by PATH: unsetting SSH_AUTH_SOCK in a shell is hygiene, the file
    # gone is the boundary. So `fy box shell` (vars scrubbed) must still fail while it exists.
    _enter_box(monkeypatch, tmp_path)
    (tmp_path / "tmp" / "vscode-ssh-auth-deadbeef.sock").write_text("")
    assert verify.verify() == 1
    out = capsys.readouterr().out
    assert "bridge sockets" in out and "vscode-ssh-auth-deadbeef.sock" in out


@pytest.mark.parametrize(
    "name",
    [
        "vscode-ssh-auth-a1.sock",
        "vscode-git-b2.sock",
        # Dev Containers' own IPC: its host side runs `git credential fill` for any caller
        "vscode-remote-containers-ipc-c3.sock",
        # the `code` CLI's IPC: drives the host's VS Code (URL opens too — around the proxy)
        "vscode-ipc-d4.sock",
    ],
)
@pytest.mark.parametrize("where", ["tmp", "tmp/user/1000", "xdg", "tmpdir"])
def test_every_attach_bridge_socket_is_fail_wherever_it_lands(
    secure_engine, monkeypatch, tmp_path, capsys, name, where
):
    # The same names and dirs the reaper covers (box._REAPER_PASS; the two globs tuples are
    # pinned equal in test_box): /tmp, the per-user /tmp/user/<uid>/, $XDG_RUNTIME_DIR, $TMPDIR.
    _enter_box(monkeypatch, tmp_path)
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "xdg"))
    monkeypatch.setenv("TMPDIR", str(tmp_path / "tmpdir"))
    for d in ("xdg", "tmpdir", "tmp/user/1000"):
        (tmp_path / d).mkdir(parents=True, exist_ok=True)
    assert verify.verify() == 0  # all four dirs read, all empty
    assert "no editor-attach bridge sockets" in capsys.readouterr().out
    (tmp_path / where / name).write_text("")
    assert verify.verify() == 1
    out = capsys.readouterr().out
    assert "bridge sockets" in out and str(tmp_path / where / name) in out


_DEVCONTAINERS_HELPER = (
    "!f() { /root/.vscode-server/bin/abc/node /tmp/vscode-remote-containers-1f2e.js "
    "git-credential-helper $*; }; f"
)


def test_a_credential_helper_pointing_at_the_host_is_fail_and_names_the_setting(
    secure_engine, monkeypatch, tmp_path, capsys
):
    # Dev Containers writes this into the box's ~/.gitconfig on every attach unless
    # dev.containers.gitCredentialHelperConfigLocation is "none": git then asks the HOST's
    # `git credential fill` over the IPC socket — an HTTPS push path the env scrub doesn't close
    # (any process can point REMOTE_CONTAINERS_IPC back at the socket while it exists).
    _, results = secure_engine
    _enter_box(monkeypatch, tmp_path)
    results["config_rc"] = 0
    results["config_out"] = (
        "file:/etc/gitconfig\tcredential.helper store\n"
        f"file:/root/.gitconfig\tcredential.helper {_DEVCONTAINERS_HELPER}\n"
    )
    assert verify.verify() == 1
    out = capsys.readouterr().out
    assert "credential helper that asks the host" in out
    assert "/root/.gitconfig" in out and "/etc/gitconfig" not in out  # names only the offender
    assert "dev.containers.gitCredentialHelperConfigLocation" in out
    # Every scope the box's git reads: run IN the checkout (local + worktree config), no scope
    # flag (system + global + XDG), with the box's own env (GIT_CONFIG_* / GIT_CONFIG_GLOBAL).
    cmd = results["config_cmd"]
    assert cmd[:3] == ["git", "-C", str(tmp_path)]
    assert not {"--global", "--system", "--local", "--file", "-f"} & set(cmd)
    assert results["config_env"]["HOME"] == os.environ["HOME"]


@pytest.mark.parametrize(
    "line",
    [
        # a URL-scoped helper reaches the host just the same
        f"file:/root/.gitconfig\tcredential.https://github.com.helper {_DEVCONTAINERS_HELPER}",
        # the script renamed, the subcommand still the host bridge's
        "command line:\tcredential.helper !node /opt/x.js git-credential-helper $*",
    ],
)
def test_every_shape_of_the_host_credential_helper_is_fail(
    secure_engine, monkeypatch, tmp_path, capsys, line
):
    _, results = secure_engine
    _enter_box(monkeypatch, tmp_path)
    results["config_rc"], results["config_out"] = 0, line + "\n"
    assert verify.verify() == 1
    assert "credential helper that asks the host" in capsys.readouterr().out


def test_an_ordinary_credential_helper_passes(secure_engine, monkeypatch, tmp_path, capsys):
    _, results = secure_engine
    _enter_box(monkeypatch, tmp_path)
    results["config_rc"] = 0
    results["config_out"] = "file:/etc/gitconfig\tcredential.helper store\n"
    assert verify.verify() == 0
    assert "no git credential helper asks the host" in capsys.readouterr().out


def test_an_unreadable_git_config_leaves_the_helper_row_unproven(
    secure_engine, monkeypatch, tmp_path, capsys
):
    # rc 1 is "no such key"; anything else (a bad config line is 128) proved nothing either way.
    _, results = secure_engine
    _enter_box(monkeypatch, tmp_path)
    results["config_rc"] = 128
    assert verify.verify() == 1
    assert "credential helpers UNPROVEN" in capsys.readouterr().out


def test_workspace_settings_re_enabling_the_git_bridge_is_fail(
    secure_engine, monkeypatch, tmp_path, capsys
):
    # `fy code` pins the bridge off at user + machine scope, but WORKSPACE settings win and
    # `.vscode/settings.json` is mount data the box can write — so a checkout flipping it back on
    # is the mount asking for a host credential, and verify says so. JSONC, matched textually.
    _enter_box(monkeypatch, tmp_path)
    checkout = tmp_path  # the fixture's Context carries FOLDYARD_CHECKOUT=tmp_path
    (checkout / ".vscode").mkdir(parents=True)
    (checkout / ".vscode" / "settings.json").write_text(
        '{\n  // re-arm\n  "git.terminalAuthentication": true,\n  "editor.fontSize": 12,\n}\n'
    )
    assert verify.verify() == 1
    out = capsys.readouterr().out
    assert "re-enable the git-credential bridge: git.terminalAuthentication" in out

    (checkout / ".vscode" / "settings.json").write_text('{"git.terminalAuthentication": false}')
    assert verify.verify() == 0
    assert "leave the git-credential bridge off" in capsys.readouterr().out


def test_workspace_settings_re_enabling_github_git_authentication_is_fail(
    secure_engine, monkeypatch, tmp_path, capsys
):
    # The GitHub extension's silent-token path: with it on, VS Code answers the box's git with
    # the host's GitHub session token — the same re-arm by mount data as the git.* keys.
    _enter_box(monkeypatch, tmp_path)
    (tmp_path / ".vscode").mkdir(parents=True)
    (tmp_path / ".vscode" / "settings.json").write_text('{ "github.gitAuthentication" : true }')
    assert verify.verify() == 1
    assert "re-enable the git-credential bridge: github.gitAuthentication" in (
        capsys.readouterr().out
    )


def test_ssh_only_known_hosts_passes(secure_engine, monkeypatch, tmp_path, capsys):
    home = _enter_box(monkeypatch, tmp_path)
    ssh = home / ".ssh"
    ssh.mkdir()
    (ssh / "known_hosts").write_text("")
    (ssh / "config").write_text("")
    assert verify.verify() == 0
    assert "no ~/.ssh private keys" in capsys.readouterr().out


# ── plugin-contributed posture flows through verify ──────────────────────────────────────
# The box-dummy row logic is unit-tested in test_inject_kinds.py; here we prove the registry's
# rows reach the report + the exit code.


def test_box_posture_real_token_fails(secure_engine, monkeypatch, tmp_path):
    """A real GH_TOKEN is caught in ANY box, declared or not — the inject plugin watches the vars
    every known kind's client reads (what the github plugin's verify row used to do)."""
    _enter_box(monkeypatch, tmp_path)
    monkeypatch.setenv("GH_TOKEN", "ghp_realtoken")  # no [[inject]] row declares a dummy for it
    assert verify.verify() == 1


def test_plugin_posture_info_row_prints_without_failing(monkeypatch, tmp_path, capsys):
    """An 'info' row (e.g. an emergency-mode banner) is printed, not counted as a fail."""

    class _InfoPlugin:
        def verify_checks(self, ctx):
            yield ("info", "⚠ heads-up banner")
            yield ("pass", "all good")

    monkeypatch.setattr(verify, "registry", lambda: _InfoPlugin())
    monkeypatch.setattr(verify, "which", lambda c: None)
    rep = verify._Report()
    verify._plugin_posture(rep, {})
    out = capsys.readouterr().out
    assert "⚠ heads-up banner" in out and rep.fails == 0


# ── stack health (advisory — WARN only, never the exit code) ─────────────────────────────


def test_stack_health_skipped_when_stack_is_down(secure_engine, capsys):
    """No containers for the project ⇒ one skip line, no PASS/WARN row."""
    assert verify.verify() == 0
    assert "stack not running — skipped" in capsys.readouterr().out


def test_stack_health_all_healthy_passes(secure_engine, capsys):
    _, results = secure_engine
    results["ps"] = "app worker\n"
    results["inspect"] = "/app healthy\n/worker -\n"  # worker has no healthcheck
    assert verify.verify() == 0
    assert (
        "2 stack container(s) healthy or unprobed (1 with healthchecks)" in capsys.readouterr().out
    )


def test_unhealthy_container_warns_without_failing(secure_engine, capsys):
    """The whole point: an unhealthy service is SAID OUT LOUD but doesn't fail the battery —
    the exit code stays the isolation verdict."""
    _, results = secure_engine
    results["ps"] = "app worker\n"
    results["inspect"] = "/app unhealthy\n/worker healthy\n"
    assert verify.verify() == 0
    out = capsys.readouterr().out
    assert "app is UNHEALTHY" in out
    assert "worker" not in out.split("UNHEALTHY")[1]  # only the sick one is named
    assert "1 health warning(s)" in out and "ALL PASS" in out


def test_health_warning_does_not_mask_an_isolation_failure(secure_engine, capsys):
    """A WARN alongside a real FAIL: exit 1 wins, and both are reported."""
    _, results = secure_engine
    results["escape_rc"] = 0  # breakout
    results["ps"] = "app\n"
    results["inspect"] = "/app unhealthy\n"
    assert verify.verify() == 1
    out = capsys.readouterr().out
    assert "1 check(s) FAILED" in out and "1 health warning(s)" in out


def test_failed_ps_reports_unknown_health_not_a_down_stack(secure_engine, capsys):
    """A non-zero `ps` produces no names — which must NOT be read as "stack not running",
    and must never reach the reassuring pass line."""
    _, results = secure_engine
    results["ps_rc"] = 1
    assert verify.verify() == 0  # advisory: still never the exit code
    out = capsys.readouterr().out
    assert "health UNKNOWN" in out
    assert "stack not running" not in out
    assert "healthy or unprobed" not in out


def test_failed_inspect_reports_unknown_health_not_an_all_clear(secure_engine, capsys):
    """Containers exist but `inspect` failed: empty output would otherwise print
    "N container(s) healthy or unprobed" — a false all-clear."""
    _, results = secure_engine
    results["ps"] = "app worker\n"
    results["inspect_rc"] = 1
    assert verify.verify() == 0
    out = capsys.readouterr().out
    assert "could not inspect 2 stack container(s) — health UNKNOWN" in out
    assert "healthy or unprobed" not in out


def test_stack_health_filters_on_the_portable_compose_label(secure_engine):
    """`com.docker.compose.project` is the one label BOTH providers set."""
    calls, _ = secure_engine
    verify.verify()
    ps = next(c for c in calls if c[1] == "ps")
    assert "label=com.docker.compose.project=p" in ps


class _Sock:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _wall_net(monkeypatch, *, proxy_up=True, direct_ports=()):
    """Fake the box's network: the PROXY (the permitted path) is the positive control, and
    `direct_ports` are the public ports that wrongly still connect."""
    monkeypatch.setenv("HTTPS_PROXY", "http://192.168.5.2:41800")

    def connect(addr, *a, **kw):
        host, port = addr
        if host == "192.168.5.2":
            if proxy_up:
                return _Sock()
            raise OSError("connection refused")
        if port in direct_ports:
            return _Sock()
        raise ConnectionRefusedError(errno.ECONNREFUSED, "Connection refused")

    monkeypatch.setattr(verify.socket, "create_connection", connect)


def test_wall_posture_direct_egress_refused_passes(monkeypatch, capsys):
    """Walled lima: a raw (proxy-ignoring) connect that gets REJECTED is the pass condition."""
    _wall_net(monkeypatch)
    rep = verify._Report()
    verify._wall_posture(rep)
    assert rep.fails == 0
    assert "rejected" in capsys.readouterr().out


def test_wall_posture_lists_each_opening_with_its_why(monkeypatch, capsys):
    # Informational: an opening is a decision someone made, not a failure — but it must be seen.
    _wall_net(monkeypatch)
    monkeypatch.setattr(
        verify.config,
        "machine_firewall_allow",
        lambda: [verify.config.FirewallOpening("34.147.131.70/32", 3307, "Cloud SQL instance")],
    )
    rep = verify._Report()
    verify._wall_posture(rep)
    out = capsys.readouterr().out
    assert rep.fails == 0
    assert "34.147.131.70/32 tcp/3307" in out
    assert "Cloud SQL instance" in out
    assert "checkout" not in out


def test_in_the_box_openings_are_named_as_the_checkouts_not_the_vms(monkeypatch, capsys):
    # The box reads the working tree; the VM applied what the host adopted, which may differ.
    _wall_net(monkeypatch)
    monkeypatch.setattr(verify.config, "in_box", lambda: True)
    monkeypatch.setattr(
        verify.config,
        "machine_firewall_allow",
        lambda: [verify.config.FirewallOpening("34.147.131.70/32", 3307, "Cloud SQL instance")],
    )
    verify._wall_posture(verify._Report())
    out = capsys.readouterr().out
    assert "34.147.131.70/32 tcp/3307" in out
    assert "this checkout declares" in out


def _deny(dst: str, port: int | None, proto: str = "tcp", seq: int = 1, ts: str | None = None):
    row: dict = {"boot": "b1", "seq": seq, "chain": "out", "dst": dst, "proto": proto}
    row["port"] = port
    if ts:
        row["ts"] = ts
    return row


def _refusals(monkeypatch, rows, openings=()):
    monkeypatch.setattr(verify.machine, "wall_denies", lambda: rows)
    monkeypatch.setattr(verify.config, "machine_firewall_allow", lambda: list(openings))
    rep = verify._Report()
    verify._wall_refusals(rep)
    return rep


def test_wall_refusals_are_grouped_newest_first_with_chatter_folded(monkeypatch, capsys):
    rows = [
        _deny("104.16.8.34", 443, seq=1, ts="2026-11-10T11:30:08Z"),
        _deny("224.0.0.22", None, proto="2", seq=2),  # IGMP
        _deny("ff02:0000:0000:0000:0000:0000:0000:0016", None, proto="icmpv6", seq=3),
        _deny("104.16.8.34", 443, seq=4, ts="2026-11-10T13:33:05Z"),
        _deny("20.26.156.215", 22, seq=5),  # a row from a reporter without ts
    ]
    rep = _refusals(monkeypatch, rows)
    out = capsys.readouterr().out
    assert rep.fails == rep.warns == 0
    assert out.index("20.26.156.215 tcp/22") < out.index("104.16.8.34 tcp/443")
    assert "104.16.8.34 tcp/443 ×2, last 2026-11-10T13:33:05Z" in out
    assert "2 more: local-network chatter" in out
    assert "224.0.0.22" not in out


def test_a_refused_address_on_an_opened_port_warns_that_the_address_may_have_changed(
    monkeypatch, capsys
):
    opening = verify.config.FirewallOpening("34.147.131.70/32", 3307, "Cloud SQL instance")
    rep = _refusals(monkeypatch, [_deny("35.1.2.3", 3307)], [opening])
    out = capsys.readouterr().out
    assert rep.warns == 1 and rep.fails == 0
    assert "firewall_allow opens tcp/3307 only to 34.147.131.70 (Cloud SQL instance)" in out
    assert "something tried 35.1.2.3: has the address changed?" in out


def test_wall_refusals_are_capped(monkeypatch, capsys):
    rows = [_deny(f"1.1.1.{i}", 443, seq=i) for i in range(1, 13)]
    _refusals(monkeypatch, rows)
    out = capsys.readouterr().out
    assert out.count("tcp/443 ×1") == 10 and "2 more destinations" in out


def test_unreadable_wall_refusals_say_so(monkeypatch, capsys):
    rep = _refusals(monkeypatch, None)
    assert rep.warns == rep.fails == 0
    assert "couldn't read the firewall's refusals" in capsys.readouterr().out


def test_no_wall_refusals_says_so(monkeypatch, capsys):
    _refusals(monkeypatch, [])
    assert "nothing refused since the VM booted" in capsys.readouterr().out


def test_the_host_lists_wall_refusals_and_the_box_points_there(secure_engine, monkeypatch, capsys):
    monkeypatch.setattr(verify, "_walled", lambda: True)
    monkeypatch.setattr(verify, "_host_loopback", lambda *a: None)
    listed: list[int] = []
    monkeypatch.setattr(verify, "_wall_refusals", lambda rep: listed.append(1))
    verify.verify()
    assert listed == [1]

    # In the box the guest's log isn't visible: say where to look instead.
    _wall_net(monkeypatch)
    monkeypatch.setattr(verify.config, "in_box", lambda: True)
    monkeypatch.setattr(verify.config, "machine_firewall_allow", lambda: [])
    verify._wall_posture(verify._Report())
    assert "run `fy verify` on your computer" in capsys.readouterr().out


def test_wall_posture_offline_box_does_not_certify_the_wall(monkeypatch, capsys):
    """The docstring already admitted this: with NO egress at all, both refusal probes fail and
    the wall reads as enforcing. An offline box must not certify a fail-closed claim."""
    _wall_net(monkeypatch, proxy_up=False)
    rep = verify._Report()
    verify._wall_posture(rep)
    assert rep.fails > 0
    assert "UNPROVEN" in capsys.readouterr().out


def test_wall_posture_direct_egress_connecting_fails(monkeypatch, capsys):
    """If the raw connect SUCCEEDS the wall isn't enforcing — both probes (443 + 53) fail."""
    _wall_net(monkeypatch, direct_ports=(443, 53))
    rep = verify._Report()
    verify._wall_posture(rep)
    assert rep.fails == 2  # both the 443 baseline and the port-53 exfil probe connected
    assert "NOT enforcing" in capsys.readouterr().out


@pytest.mark.parametrize(
    ("proxy", "expected"),
    [
        ("http://[fd00::2]:41800", ("fd00::2", 41800)),  # a bracketed IPv6 literal
        ("http://192.168.5.2", ("192.168.5.2", 80)),  # no port: the scheme's default
        ("http://u:secret@192.168.5.2:41800", ("192.168.5.2", 41800)),  # userinfo
    ],
)
def test_wall_posture_reads_the_proxy_the_way_the_push_row_does(monkeypatch, proxy, expected):
    # The permitted path is the positive control, so the wall row parses HTTPS_PROXY with the
    # same `_proxy_address` as the push row: the old regex missed IPv6 and port-less values and
    # called the firewall UNPROVEN for a proxy that was there. (Review on #160.)
    monkeypatch.setenv("HTTPS_PROXY", proxy)
    dialled = []

    def connect(addr, *a, **kw):
        dialled.append(addr)
        if addr == expected:
            return _Sock()
        raise OSError("connection refused")

    monkeypatch.setattr(verify.socket, "create_connection", connect)
    rep = verify._Report()
    verify._wall_posture(rep)
    assert dialled[0] == expected
    assert rep.fails == 0


def test_wall_posture_an_unreadable_proxy_is_unproven_without_leaking_it(monkeypatch, capsys):
    monkeypatch.setenv("HTTPS_PROXY", "http://u:secret@[broken:41800")
    rep = verify._Report()
    verify._wall_posture(rep)
    out = capsys.readouterr().out
    assert rep.fails == 1
    assert "UNPROVEN" in out
    assert "secret" not in out


def test_wall_posture_flags_port_53_exfil_hole(monkeypatch, capsys):
    """The #2 regression, in isolation: 443 correctly refused but a public-IP :53 connect SUCCEEDS
    (the wall allows :53 to any destination) — the port-53 probe must catch it as one fail."""
    _wall_net(monkeypatch, direct_ports=(53,))
    rep = verify._Report()
    verify._wall_posture(rep)
    assert rep.fails == 1
    out = capsys.readouterr().out
    assert "53" in out and "NOT enforcing" in out


# ── your computer's loopback (from your computer, with the VM firewall) ─────────────────────


def _loopback_probe(
    monkeypatch, *, control: bool = True, reaches: bool = False, direct_unknown: bool = False
) -> list:
    """Stand in for the probe container: answer the liveness control (or not), say its wget is
    one the direct leg can't trust (or not), and — when the path is open — connect to the listener
    the check opened, as a container reaching it would. Returns the commands run."""
    import re
    import socket
    import types

    monkeypatch.setattr(verify.config, "host_alias", lambda: "192.168.5.2")
    monkeypatch.setattr(verify.config, "proxy_port", lambda: 41800)
    ran: list = []

    def run(cmd, env, *, capture=False):
        ran.append(cmd)
        script = cmd[-1]
        if reaches:
            found = re.search(r"127\.0\.0\.1:(\d+)/", script)
            assert found is not None
            socket.create_connection(("127.0.0.1", int(found.group(1))), timeout=2).close()
        out = ("control=ok\n" if control else "control=failed\n") + (
            "direct=unknown\n" if direct_unknown else ""
        )
        return types.SimpleNamespace(returncode=0, stdout=out, stderr="")

    monkeypatch.setattr(verify, "_run", run)
    return ran


def test_host_loopback_out_of_reach_passes(monkeypatch, capsys):
    ran = _loopback_probe(monkeypatch)
    rep = verify._Report()
    verify._host_loopback(rep, "podman", {}, "alpine")
    assert rep.fails == 0 and "out of reach" in capsys.readouterr().out
    (cmd,) = ran
    # Both paths are tried from one probe: direct at the gateway, and through the proxy (whose
    # URL is set explicitly, with NO_PROXY emptied so loopback isn't kept local in the container).
    assert "http_proxy=http://192.168.5.2:41800" in cmd and "no_proxy=" in cmd
    # The direct leg must not use the proxy, which would refuse the gateway as a local address and
    # read as the wall's refusal. The env is unset AND each wget's own switch is used — busybox's
    # `-Y off`, GNU Wget 1.x's `--no-config --no-proxy` (a wgetrc can set a proxy too). Any other
    # wget says so, and the row can't pass (below).
    # A wgetrc's proxy also beats the env for the control and the proxy leg, so GNU Wget runs every
    # leg with --no-config: each then reaches THIS checkout's proxy (the -e above), or nothing.
    script = cmd[-1]
    assert script.index("case ") < script.index("wget -q")  # the switches are known before any leg
    assert "*BusyBox*) cfg=; off='-Y off'" in script
    assert "*'GNU Wget 1.'*) cfg=--no-config; off=--no-proxy" in script
    assert "echo direct=unknown" in script
    assert "wget -q -T 10 -O /dev/null $cfg http://foldyard-liveness" in script
    assert (
        "(unset http_proxy HTTP_PROXY; wget -q -T 5 -O /dev/null $cfg $off http://192.168.5.2:"
        in script
    )
    assert "wget -q -T 10 -O /dev/null $cfg http://127.0.0.1:" in script


def test_host_loopback_with_an_unknown_wget_proves_nothing(monkeypatch, capsys):
    # A wget the row can't tell to skip every proxy: its direct leg might have gone through one.
    _loopback_probe(monkeypatch, direct_unknown=True)
    rep = verify._Report()
    verify._host_loopback(rep, "podman", {}, "alpine")
    out = capsys.readouterr().out
    assert rep.fails == 1 and "UNPROVEN" in out and "wget" in out


def test_host_loopback_reached_fails(monkeypatch, capsys):
    _loopback_probe(monkeypatch, reaches=True)
    rep = verify._Report()
    verify._host_loopback(rep, "podman", {}, "alpine")
    assert rep.fails == 1 and "REACHED a service on your computer" in capsys.readouterr().out


def test_host_loopback_without_the_control_proves_nothing(monkeypatch, capsys):
    # Nothing reached the listener — but nothing reached the proxy either: no egress at all.
    _loopback_probe(monkeypatch, control=False)
    rep = verify._Report()
    verify._host_loopback(rep, "podman", {}, "alpine")
    assert rep.fails == 1 and "UNPROVEN" in capsys.readouterr().out


# ── the manual pointer ───────────────────────────────────────────────────────────────────
# A row that fails names WHAT failed, not what to do about it; the "why might it fail, what do
# I do" lives in the manual. The pointer is the exact `fy docs` call, printed up front (so the
# reader knows before the first row where the explanations are) and again beside a FAIL verdict.


def test_verify_points_at_the_manual_section(secure_engine, capsys):
    verify.verify()
    out = capsys.readouterr().out
    assert "fy docs security" in out.splitlines()[0]


def test_a_failed_verdict_repeats_the_manual_pointer(secure_engine, capsys):
    _, results = secure_engine
    results["escape_rc"] = 0  # any FAIL will do
    assert verify.verify() == 1
    out = capsys.readouterr().out
    verdict = [ln for ln in out.splitlines() if "check(s) FAILED" in ln]
    assert verdict and "fy docs security" in out.split(verdict[0], 1)[1]


# ── ADR-0033: the to-box is read-only to everything in the VM ─────────────────────────────
#
# Verified by a real WRITE, never by the `ro` flag: on vz, root in the guest can remount the
# to-box rw (the table then says `rw`) while the host still refuses every write — and the refusal
# is EROFS, EPERM or EACCES depending on which layer said no. A write that lands is the FAIL.


def _to_box() -> str:
    # At TEST time, like the tables above: conftest gives each test its own HOME, and the audit
    # judges a leak by the home it runs with — a path built at import time is the operator's.
    return str(pathlib.Path.home() / ".foldyard" / "p" / "to-box")


def _from_box() -> str:
    return str(pathlib.Path.home() / ".foldyard" / "p" / "from-box")


@pytest.fixture
def with_to_box(secure_engine, monkeypatch):
    calls, results = secure_engine
    monkeypatch.setattr(verify.boxmounts, "vm_paths", lambda: (_from_box(), _to_box()))
    monkeypatch.setattr(verify.machine, "to_box_mounted", lambda: True)
    results["pid1_mounts"] = (
        "/dev/vda2 / btrfs rw 0 0\n"
        f"mount2 {_from_box()} virtiofs rw,relatime 0 0\n"
        f"mount3 {_to_box()} virtiofs ro,relatime 0 0\n"
    )
    return calls, results


def test_a_read_only_to_box_that_refuses_the_write_passes(with_to_box, capsys):
    _, results = with_to_box
    assert verify.verify() == 0
    out = capsys.readouterr().out
    assert "to-box mounted read-only in the VM" in out
    assert "to-box refuses a write from a --privileged container binding it read-write" in out
    # the probe binds the to-box READ-WRITE into a privileged container: the VM mount is the claim
    cmd = results["to_box_cmd"]
    assert "--privileged" in cmd and f"{_to_box()}:/fy-to-box" in cmd
    assert "remount,rw" in cmd[-1]


def test_foldyards_two_directories_are_not_a_leak_but_their_parent_is(with_to_box, capsys):
    _, results = with_to_box
    assert verify.verify() == 0
    assert "free of host home/paths" in capsys.readouterr().out
    # the state dir itself (host.env, the keys) or a sibling under it: still a leak
    state = str(pathlib.Path(_to_box()).parent)
    for leak in (state, f"{state}/monitor-sink.key.d"):
        results["pid1_mounts"] += f"mount9 {leak} virtiofs rw 0 0\n"
        assert verify.verify() == 1
        assert "VM exposes host paths" in capsys.readouterr().out
        results["pid1_mounts"] = results["pid1_mounts"].rsplit("mount9", 1)[0]


def test_a_write_that_lands_on_the_to_box_fails(with_to_box, capsys):
    _, results = with_to_box
    results["to_box_probe"] = "remount=refused\nwrite=LANDED\nremoved=yes\n"
    assert verify.verify() == 1
    out = capsys.readouterr().out
    assert "CREATED" in out and f"{_to_box()}/.fy-verify-" in out and "writable from the VM" in out


def test_a_guest_showing_rw_while_the_host_refuses_is_a_warning_not_a_fail(with_to_box, capsys):
    # The vz test: guest root remounted it rw, the table says rw, every write still refused.
    _, results = with_to_box
    results["pid1_mounts"] = results["pid1_mounts"].replace("ro,relatime", "rw,relatime")
    results["to_box_probe"] = "remount=done\nwrite=refused\n"
    assert verify.verify() == 0
    out = capsys.readouterr().out
    assert "WARN" in out and "something in the guest remounted it" in out
    assert "its remount to rw returned 0; the write was still refused" in out


def test_a_probe_that_did_not_run_proves_nothing(with_to_box, capsys):
    _, results = with_to_box
    results["to_box_rc"] = 125
    results["to_box_probe"] = ""
    assert verify.verify() == 1
    assert "read-only UNPROVEN" in capsys.readouterr().out


def test_no_to_box_in_the_table(with_to_box, monkeypatch, capsys):
    _, results = with_to_box
    results["pid1_mounts"] = "/dev/vda2 / btrfs rw 0 0\n"
    assert verify.verify() == 0  # expected but absent: a warning — nothing writable to protect
    assert "not in the VM's mount table" in capsys.readouterr().out
    monkeypatch.setattr(verify.machine, "to_box_mounted", lambda: False)  # krunkit, say
    assert verify.verify() == 0
    out = capsys.readouterr().out
    assert "no to-box in this VM" in out and "WARN" not in out


def test_under_gvisor_the_write_probe_is_not_applicable_in_the_box(
    with_to_box, monkeypatch, capsys
):
    _, results = with_to_box
    monkeypatch.setenv("FY_MACHINE_RUNTIME", "gvisor")
    results["mount_rc"] = 1
    results["pid1_mounts"] = ""
    verify._vm_boundary(verify._Report(), "podman", {}, "alpine", [], _to_box(), True)
    out = capsys.readouterr().out
    assert "to-box write probe runs at another layer" in out
    assert "to_box_cmd" not in results


def test_in_the_box_the_to_box_bind_refuses_a_write(tmp_path, monkeypatch, capsys):
    here = tmp_path / "to-box"
    here.mkdir()
    monkeypatch.setattr(verify.boxmounts, "TO_BOX_IN_BOX", str(here))
    monkeypatch.setenv("FY_TO_BOX", _to_box())
    rep = verify._Report()
    verify._to_box_in_box(rep)  # a writable dir: the bind is not read-only
    assert rep.fails == 1
    assert "took a write" in capsys.readouterr().out
    assert list(here.iterdir()) == []  # and the probe cleaned up after itself
    if os.geteuid() == 0:
        return  # root ignores the mode below (a dev box runs the suite as root)
    here.chmod(0o555)
    try:
        rep = verify._Report()
        verify._to_box_in_box(rep)
        assert rep.fails == 0
        assert "refuses a write" in capsys.readouterr().out
    finally:
        here.chmod(0o755)


def test_in_the_box_with_no_to_box_baked_there_is_no_row(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("FY_TO_BOX", raising=False)
    rep = verify._Report()
    verify._to_box_in_box(rep)
    assert rep.fails == rep.warns == 0
    assert capsys.readouterr().out == ""


def test_a_box_from_before_the_two_directories_fails_with_the_recreate_hint(
    secure_engine, monkeypatch, capsys
):
    # Review on #115: a box created before ADR-0033 has no FY_FROM_BOX / FY_TO_BOX, so once its VM
    # restarts with them, foldyard's own mounts aren't exempt there. Never exempted by guess — a
    # FAIL still — but one that says what it is and how to clear it.
    _, results = secure_engine
    monkeypatch.setattr(verify.config, "in_box", lambda: True)
    monkeypatch.setattr(verify, "_box_posture", lambda rep, env: None)
    results["pid1_mounts"] = f"mount3 {_to_box()} virtiofs ro,relatime 0 0\n"
    assert verify.verify() == 1
    out = capsys.readouterr().out
    assert "VM exposes host paths" in out and "fy box down && fy box up" in out
    # …while a real leak gets no such hint
    results["pid1_mounts"] = f"mount3 {pathlib.Path.home()} virtiofs rw 0 0\n"
    assert verify.verify() == 1
    assert "fy box down && fy box up" not in capsys.readouterr().out


def test_a_write_that_lands_and_cant_be_removed_still_names_the_file(with_to_box, capsys):
    _, results = with_to_box
    results["to_box_rc"] = 1  # `rm` failed: the shell's last status
    results["to_box_probe"] = "remount=refused\nwrite=LANDED\n"
    assert verify.verify() == 1
    out = capsys.readouterr().out
    assert "CREATED" in out and "LEFT there" in out and "UNPROVEN" not in out


# ── the push refusal: the proxy refuses a git push by its URL ────────────────────────────────

_PROXY = "http://192.168.5.2:41000"


def _push_row(monkeypatch, answer, proxy: str | None = _PROXY) -> tuple[verify._Report, list]:
    asked: list = []

    def probe(host, port):
        asked.append((host, port))
        if isinstance(answer, BaseException):
            raise answer
        return answer

    monkeypatch.setattr(verify, "_probe_push", probe)
    for var in ("HTTPS_PROXY", "https_proxy"):
        monkeypatch.delenv(var, raising=False)
    if proxy is not None:
        monkeypatch.setenv("HTTPS_PROXY", proxy)
    rep = verify._Report()
    verify._push_refusal(rep)
    return rep, asked


def test_a_push_the_proxy_refuses_passes(monkeypatch, capsys):
    rep, asked = _push_row(monkeypatch, _REFUSED_ANSWER)
    assert asked == [("192.168.5.2", 41000)]  # through the box's own proxy
    assert rep.fails == rep.warns == 0
    assert "git push over HTTPS refused by the foldyard proxy" in capsys.readouterr().out


def test_a_push_the_active_mode_allows_is_reported_not_failed(monkeypatch, capsys):
    allowed = verify._PushAnswer(status=401, body=b"", allowed=True, decrypted=True)
    rep, _ = _push_row(monkeypatch, allowed)
    assert rep.fails == rep.warns == 0
    out = capsys.readouterr().out
    assert "allowed by the active mode" in out and "PASS" not in out
    # The lift is per host — where a push-capable switch injects — so the row names the probe's.
    assert "to github.com" in out and "injects on github.com" in out


def test_a_push_the_proxy_let_through_unrefused_fails(monkeypatch, capsys):
    # Decrypted (the proxy's certificate), no refusal and no "allowed" word: GitHub answered.
    forwarded = verify._PushAnswer(status=401, body=b"Unauthorized", allowed=False, decrypted=True)
    rep, _ = _push_row(monkeypatch, forwarded)
    assert rep.fails == 1
    out = capsys.readouterr().out
    assert "NOT refused" in out and "401" in out


def test_another_refusal_is_not_the_push_refusal(monkeypatch, capsys):
    # The allowlist's 403, with no word that a mode let the push past: a proxy that predates the
    # push refusal (a running addon not yet restarted). Unproven, so a FAIL.
    wall = verify._PushAnswer(
        status=403,
        body=b"refused by the foldyard allowlist - grant it on your computer",
        allowed=False,
        decrypted=True,
    )
    rep, _ = _push_row(monkeypatch, wall)
    assert rep.fails == 1
    assert "fy host restart" in capsys.readouterr().out


def test_a_blind_tunnel_is_a_warning_naming_passthrough(monkeypatch, capsys):
    # GitHub's own certificate: github.com is tunnelled (`[proxy] passthrough`), so the proxy
    # can't see the push. A declared decision (`fy config widenings` says so), not a breach.
    blind = verify._PushAnswer(status=401, body=b"", allowed=False, decrypted=False)
    rep, _ = _push_row(monkeypatch, blind)
    assert rep.fails == 0 and rep.warns == 1
    out = capsys.readouterr().out
    assert "passthrough" in out and "fy config widenings" in out


@pytest.mark.parametrize(
    "error",
    [TimeoutError("timed out"), OSError("connection refused"), verify.ssl.SSLError("bad cert")],
    ids=["timeout", "refused", "tls"],
)
def test_a_probe_that_got_no_answer_proves_nothing(monkeypatch, capsys, error):
    rep, _ = _push_row(monkeypatch, error)
    assert rep.fails == 1
    assert "UNPROVEN" in capsys.readouterr().out


def test_no_proxy_in_the_box_skips_the_row(monkeypatch, capsys):
    rep, asked = _push_row(monkeypatch, _REFUSED_ANSWER, proxy=None)
    assert asked == [] and rep.fails == rep.warns == 0
    assert "skipped" in capsys.readouterr().out


@pytest.mark.parametrize(
    ("proxy", "address"),
    [
        ("http://[::1]:41000", ("::1", 41000)),  # a bracketed IPv6 literal
        ("http://[fd00::2]:41000/", ("fd00::2", 41000)),
        ("http://192.168.5.2", ("192.168.5.2", 80)),  # no port: the scheme's default
        ("https://proxy.example", ("proxy.example", 443)),
        ("http://user:s3cret@192.168.5.2:41000", ("192.168.5.2", 41000)),
        ("192.168.5.2:41000", ("192.168.5.2", 41000)),  # no scheme: http, as curl reads it
    ],
)
def test_the_probe_goes_through_any_spelling_of_the_proxy(monkeypatch, capsys, proxy, address):
    rep, asked = _push_row(monkeypatch, _REFUSED_ANSWER, proxy=proxy)
    assert asked == [address]
    assert rep.fails == rep.warns == 0
    assert "s3cret" not in capsys.readouterr().out


@pytest.mark.parametrize(
    "proxy",
    [
        "http://user:s3cret@[::1:41000",  # an unclosed bracket
        "http://user:s3cret@192.168.5.2:port",  # a port that isn't a number
        "http://user:s3cret@192.168.5.2:99999",  # …or isn't a port
        "http://user:s3cret@:41000",  # no host
        "socks5://user:s3cret@192.168.5.2:1080",  # a scheme the probe can't speak
    ],
)
def test_a_proxy_that_cant_be_read_is_unproven_not_skipped(monkeypatch, capsys, proxy):
    # Set, so the box routes somewhere: a row that read it as "no proxy" would skip the check
    # exactly where it can't tell what the box does.
    rep, asked = _push_row(monkeypatch, _REFUSED_ANSWER, proxy=proxy)
    assert asked == [] and rep.fails == 1
    out = capsys.readouterr().out
    assert "UNPROVEN" in out and "HTTPS_PROXY" in out and "skipped" not in out
    # Its shape is named, so the operator can see what's wrong — never the credential in it.
    assert "s3cret" not in out and "user" not in out
    assert proxy.rpartition("@")[2] in out


def test_the_push_refusal_row_runs_in_the_box(secure_engine, monkeypatch, tmp_path, capsys):
    _enter_box(monkeypatch, tmp_path)
    monkeypatch.setenv("HTTPS_PROXY", _PROXY)
    assert verify.verify() == 0
    assert "git push over HTTPS refused by the foldyard proxy" in capsys.readouterr().out


class _FakeConn:
    made: ClassVar[list] = []

    def __init__(self, host, port, *, timeout, context):
        self.args = {"host": host, "port": port, "timeout": timeout, "context": context}
        self.tunnel = None
        self.sent = None
        _FakeConn.made.append(self)
        self.sock: Any = types.SimpleNamespace(
            getpeercert=lambda: {
                "issuer": ((("commonName", "mitmproxy"),), (("organizationName", "mitmproxy"),))
            }
        )

    def set_tunnel(self, host, port):
        self.tunnel = (host, port)

    def request(self, method, path, headers):
        self.sent = (method, path, headers)

    def getresponse(self):
        # A response that closes the connection: http.client drops the socket once it's read, so
        # the certificate must already have been taken (found by the proxy e2e).
        self.sock = None
        return types.SimpleNamespace(
            status=403,
            read=lambda n: b"refused by the foldyard proxy - the box doesn't push",
            getheader=lambda name, default=None: default,
        )

    def close(self):
        pass


def test_the_probe_sends_a_push_discovery_through_the_proxy(monkeypatch, tmp_path):
    bundle = tmp_path / "bundle.pem"
    loaded: list = []
    monkeypatch.setenv("SSL_CERT_FILE", str(bundle))
    monkeypatch.setattr(
        verify.ssl, "create_default_context", lambda cafile=None: loaded.append(cafile) or "ctx"
    )
    _FakeConn.made = []
    monkeypatch.setattr(verify.http.client, "HTTPSConnection", _FakeConn)
    answer = verify._probe_push("192.168.5.2", 41000)
    [conn] = _FakeConn.made
    assert (conn.args["host"], conn.args["port"], conn.args["context"]) == (
        "192.168.5.2",
        41000,
        "ctx",
    )
    assert loaded == [str(bundle)]  # the box's combined bundle: the proxy CA + system roots
    assert conn.args["timeout"] == 12
    assert conn.tunnel == ("github.com", 443)
    method, path, _headers = conn.sent
    assert method == "GET" and path.endswith("/info/refs?service=git-receive-pack")
    assert answer == verify._PushAnswer(
        status=403,
        body=b"refused by the foldyard proxy - the box doesn't push",
        allowed=False,
        decrypted=True,
    )


# ── the dry-run push, when the proxy refused it as a push ────────────────────────────────────
# The proxy answers git's first request (the discovery GET) with its 403 before origin is asked,
# so git never offers a credential and the probe no longer says whether one would reach origin.
# git is asked directly instead: `git credential fill` for origin's push URL.

_PROXY_REFUSED_STDERR = (
    "remote: refused by the foldyard proxy - the box doesn't push; push from your computer\n"
    "fatal: unable to access 'https://github.com/acme/app.git/': The requested URL returned"
    " error: 403\n"
)


def _proxy_refused(secure_engine, monkeypatch, tmp_path):
    _enter_box(monkeypatch, tmp_path)
    _, results = secure_engine
    results["git_rc"] = 128
    results["git_stderr"] = _PROXY_REFUSED_STDERR
    return results


def test_a_push_the_proxy_refused_asks_git_for_a_credential_and_finds_none(
    secure_engine, monkeypatch, tmp_path, capsys
):
    results = _proxy_refused(secure_engine, monkeypatch, tmp_path)
    assert verify.verify() == 0
    out = capsys.readouterr().out
    assert "git push refused by the foldyard proxy, and git has no credential for origin" in out
    assert results["url_cmd"][-3:] == ["get-url", "--push", "origin"]
    assert results["cred_cmd"][-2:] == ["credential", "fill"]
    assert results["cred_input"] == "url=https://github.com/acme/app.git\n\n"
    assert results["cred_env"]["GIT_TERMINAL_PROMPT"] == "0"  # never prompts


def test_a_credential_git_would_send_fails_even_though_the_proxy_refused(
    secure_engine, monkeypatch, tmp_path, capsys
):
    results = _proxy_refused(secure_engine, monkeypatch, tmp_path)
    results["cred_rc"], results["cred_err"] = 0, ""
    results["cred_out"] = "protocol=https\nhost=github.com\nusername=x\npassword=ghp_REALTOKEN\n"
    assert verify.verify() == 1
    out = capsys.readouterr().out
    assert "git HOLDS a credential for origin" in out
    assert "ghp_REALTOKEN" not in out  # never echoed


def test_a_credential_lookup_that_hangs_leaves_it_unproven(
    secure_engine, monkeypatch, tmp_path, capsys
):
    results = _proxy_refused(secure_engine, monkeypatch, tmp_path)
    results["cred_rc"] = None  # times out
    assert verify.verify() == 1
    assert "UNPROVEN" in capsys.readouterr().out


def test_a_credential_lookup_failing_otherwise_leaves_it_unproven(
    secure_engine, monkeypatch, tmp_path, capsys
):
    results = _proxy_refused(secure_engine, monkeypatch, tmp_path)
    results["cred_err"] = "fatal: bad config line 3 in file .git/config\n"
    assert verify.verify() == 1
    assert "UNPROVEN" in capsys.readouterr().out


def test_no_push_url_leaves_it_unproven(secure_engine, monkeypatch, tmp_path, capsys):
    results = _proxy_refused(secure_engine, monkeypatch, tmp_path)
    results["push_url_rc"], results["push_url"] = 2, ""
    assert verify.verify() == 1
    assert "UNPROVEN" in capsys.readouterr().out
