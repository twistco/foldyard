"""verify.py — the isolation battery. The engine + git are mocked (golden-ish): we assert
the right probes run and that PASS/FAIL + the exit code follow the probe results, without a
real engine. Mode-aware GitHub posture is unit-tested directly."""

from __future__ import annotations

import pytest

from foldyard import stack, verify
from foldyard.plugins import proxy


class _Proc:
    def __init__(self, returncode=0, stdout=""):
        self.returncode = returncode
        self.stdout = stdout


@pytest.fixture
def secure_engine(monkeypatch, tmp_path):
    """Fake engine + git whose probes report a SOUND boundary; stack.resolve mocked so no
    real git/machine runs. Returns (recorded calls, a mutable results dict to make a probe
    report insecurely)."""
    calls: list[list[str]] = []
    results = {
        "rootless": True,
        "escape_rc": 1,  # --privileged --pid=host refused
        "ls_users_rc": 1,  # /Users not visible
        "mount": "proc /proc proc rw 0 0\ntmpfs /tmp tmpfs rw 0 0\n",
        "git_rc": 1,  # origin unreachable (push refused)
        "ps": "",  # no stack containers ⇒ the advisory health section skips
        "ps_rc": 0,
        "inspect": "",
        "inspect_rc": 0,
    }

    def fake_run(cmd, **kw):
        calls.append(cmd)
        if cmd[0] == "git":
            return _Proc(results["git_rc"])
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
            if "ls /Users" in tail:
                return _Proc(results["ls_users_rc"])
            if tail == "mount":
                return _Proc(0, results["mount"])
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
    monkeypatch.setattr(verify.config, "in_box", lambda: False)  # VM-boundary only by default
    return calls, results


def _enter_box(monkeypatch, tmp_path):
    """Flip to in-box with a clean HOME + no gh + github=off, so only the probe results vary."""
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    monkeypatch.setattr(verify.config, "in_box", lambda: True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("SSH_AUTH_SOCK", raising=False)
    monkeypatch.delenv("GH_TOKEN", raising=False)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.setattr(verify, "which", lambda c: None)
    # No proxy CA mounted ⇒ the github plugin's verify hook reports github=off.
    monkeypatch.setattr(proxy, "BOX_CA", tmp_path / "no-ca.pem")
    return home


# ── VM boundary ────────────────────────────────────────────────────────────────────────


def test_all_pass_returns_0(secure_engine, capsys):
    assert verify.verify() == 0
    out = capsys.readouterr().out
    assert "ALL PASS" in out
    assert out.count("✓ PASS") == 4  # the 4 VM-boundary checks; posture skipped
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
    results["mount"] = "macfuse /Users/dain osxfuse rw 0 0\n"
    assert verify.verify() == 1
    assert "host paths" in capsys.readouterr().out


def test_escape_probe_command_shape(secure_engine):
    calls, _ = secure_engine
    verify.verify()
    assert any(c[1] == "run" and "--privileged" in c and "--pid=host" in c for c in calls)


# ── dev-box posture ──────────────────────────────────────────────────────────────────────


def test_box_posture_clean_passes(secure_engine, monkeypatch, tmp_path, capsys):
    _enter_box(monkeypatch, tmp_path)
    assert verify.verify() == 0
    out = capsys.readouterr().out
    assert "no SSH agent" in out and "no ~/.netrc" in out and "git push refused" in out


def test_git_remote_reachable_is_fail(secure_engine, monkeypatch, tmp_path):
    _, results = secure_engine
    results["git_rc"] = 0  # origin reachable → the box could push
    _enter_box(monkeypatch, tmp_path)
    assert verify.verify() == 1


def test_ssh_private_key_is_fail(secure_engine, monkeypatch, tmp_path):
    home = _enter_box(monkeypatch, tmp_path)
    ssh = home / ".ssh"
    ssh.mkdir()
    (ssh / "known_hosts").write_text("")  # harmless
    (ssh / "config").write_text("")  # harmless
    (ssh / "id_ed25519").write_text("KEY")  # a push path
    assert verify.verify() == 1


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


def test_wall_posture_direct_egress_refused_passes(monkeypatch, capsys):
    """Walled lima: a raw (proxy-ignoring) connect that gets REJECTED is the pass condition."""

    def refuse(*a, **kw):
        raise OSError("connection refused")

    monkeypatch.setattr(verify.socket, "create_connection", refuse)
    rep = verify._Report()
    verify._wall_posture(rep)
    assert rep.fails == 0
    assert "rejected" in capsys.readouterr().out


class _Sock:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_wall_posture_direct_egress_connecting_fails(monkeypatch, capsys):
    """If the raw connect SUCCEEDS the wall isn't enforcing — both probes (443 + 53) fail."""
    monkeypatch.setattr(verify.socket, "create_connection", lambda *a, **kw: _Sock())
    rep = verify._Report()
    verify._wall_posture(rep)
    assert rep.fails == 2  # both the 443 baseline and the port-53 exfil probe connected
    assert "NOT enforcing" in capsys.readouterr().out


def test_wall_posture_flags_port_53_exfil_hole(monkeypatch, capsys):
    """The #2 regression, in isolation: 443 correctly refused but a public-IP :53 connect SUCCEEDS
    (the wall allows :53 to any destination) — the port-53 probe must catch it as one fail."""

    def by_port(addr, *a, **kw):
        if addr[1] == 53:
            return _Sock()  # the tunnel is open
        raise OSError("connection refused")

    monkeypatch.setattr(verify.socket, "create_connection", by_port)
    rep = verify._Report()
    verify._wall_posture(rep)
    assert rep.fails == 1
    out = capsys.readouterr().out
    assert "53" in out and "NOT enforcing" in out
