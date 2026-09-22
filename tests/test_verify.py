"""verify.py — the isolation battery. The engine + git are mocked (golden-ish): we assert
the right probes run and that PASS/FAIL + the exit code follow the probe results, without a
real engine. Mode-aware GitHub posture is unit-tested directly."""

from __future__ import annotations

import pathlib

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
    results = {
        "rootless": True,
        "escape_rc": 1,  # --privileged --pid=host refused
        # The VM's mount table = PID 1's, read via --pid=host (the container's own `mount`
        # never shows VM-level mounts — see the namespace test below).
        "pid1_mounts": "proc /proc proc rw 0 0\ntmpfs /tmp tmpfs rw 0 0\n",
        "mount_rc": 0,
        "probe_rc": 0,  # the positive control: the probe image CAN run
        "git_rc": 1,  # origin unreachable…
        "git_stderr": "git@github.com: Permission denied (publickey).",  # …for CREDENTIAL reasons
        "ps": "",  # no stack containers ⇒ the advisory health section skips
        "ps_rc": 0,
        "inspect": "",
        "inspect_rc": 0,
    }

    def fake_run(cmd, **kw):
        calls.append(cmd)
        if cmd[0] == "git":
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
    # A real dev box bakes the operator's home; the tests build their tables from Path.home().
    monkeypatch.delenv("FY_HOST_HOME", raising=False)
    return calls, results


def _enter_box(monkeypatch, tmp_path):
    """Flip to in-box with a clean HOME + no gh + github=off, so only the probe results vary."""
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    monkeypatch.setattr(verify.config, "in_box", lambda: True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("SSH_AUTH_SOCK", raising=False)
    for v in verify._GIT_BRIDGE_VARS:
        monkeypatch.delenv(v, raising=False)
    monkeypatch.setattr(verify, "_BRIDGE_SOCKET_DIR", tmp_path / "tmp")
    (tmp_path / "tmp").mkdir(exist_ok=True)
    monkeypatch.delenv("GH_TOKEN", raising=False)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.setattr(verify, "which", lambda c: None)
    # No proxy CA mounted ⇒ the github plugin's verify hook reports github=off.
    monkeypatch.setattr(proxy, "BOX_CA", tmp_path / "no-ca.pem")
    # The wall section is a different subject with its own tests below, and it needs a live proxy
    # + network. Left unpinned it resolves the REPO's OWN foldyard.toml — so this repo declaring
    # `[machine] wall = true` silently bolted a failing check onto every test here: green locally,
    # where a dev box exports HTTPS_PROXY ambiently, red in CI where nothing does. The `== 1` tests
    # kept passing throughout, on the wall's failure rather than their own subject's.
    monkeypatch.setattr(verify.config, "machine_wall", lambda: False)
    return home


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
    assert "git remote REACHABLE" in capsys.readouterr().out  # ...and for THAT reason


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


def test_ssh_only_known_hosts_passes(secure_engine, monkeypatch, tmp_path, capsys):
    home = _enter_box(monkeypatch, tmp_path)
    ssh = home / ".ssh"
    ssh.mkdir()
    (ssh / "known_hosts").write_text("")
    (ssh / "config").write_text("")
    assert verify.verify() == 0
    assert "no ~/.ssh private keys" in capsys.readouterr().out


# ── plugin-contributed posture flows through verify ──────────────────────────────────────
# The github mode-aware row logic is unit-tested in test_plugins.py (github._verify_rows /
# _box_github_mode); here we prove the registry's rows reach the report + the exit code.


def test_box_posture_real_token_fails(secure_engine, monkeypatch, tmp_path):
    """A real GH_TOKEN in an off-mode box is caught by the github plugin's verify hook."""
    _enter_box(monkeypatch, tmp_path)
    monkeypatch.setenv("GH_TOKEN", "ghp_realtoken")  # not the dummy 'x', github=off
    assert verify.verify() == 1


def test_plugin_posture_info_row_prints_without_failing(monkeypatch, tmp_path, capsys):
    """An 'info' row (e.g. the github=user banner) is printed, not counted as a fail."""

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
        raise OSError("connection refused")

    monkeypatch.setattr(verify.socket, "create_connection", connect)


def test_wall_posture_direct_egress_refused_passes(monkeypatch, capsys):
    """Walled lima: a raw (proxy-ignoring) connect that gets REJECTED is the pass condition."""
    _wall_net(monkeypatch)
    rep = verify._Report()
    verify._wall_posture(rep)
    assert rep.fails == 0
    assert "rejected" in capsys.readouterr().out


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


def test_wall_posture_flags_port_53_exfil_hole(monkeypatch, capsys):
    """The #2 regression, in isolation: 443 correctly refused but a public-IP :53 connect SUCCEEDS
    (the wall allows :53 to any destination) — the port-53 probe must catch it as one fail."""
    _wall_net(monkeypatch, direct_ports=(53,))
    rep = verify._Report()
    verify._wall_posture(rep)
    assert rep.fails == 1
    out = capsys.readouterr().out
    assert "53" in out and "NOT enforcing" in out


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
