"""Tests for the dev-posture TUI and its devmode backend.

Textual ships a headless test harness — `async with app.run_test() as pilot:` drives
the real app (key presses, focus, rendering) with no terminal, so these run anywhere
(CI, the box, the Mac). We stub the Mac-only writes (`devmode.set_mode`) and the live
probes (`devmode.doctor`, `devmode.branches`) so nothing here touches GCP, git, or the
mode files.

Run:  fy tui-test                 (uv resolves pytest + textual from foldyard)
  or  uv run --project foldyard pytest foldyard/tests
"""

from __future__ import annotations

import sys
from datetime import timedelta
from typing import cast

import pytest

from foldyard import config, configpin, devmode, plugins, tui

# The mode-grid/axis tests assume the Tangible-shaped axes; bind a full resolved config.
pytestmark = pytest.mark.usefixtures("full_config_bound")


def _panel_registry(plugin) -> plugins.Registry:
    """A synthetic single-panel registry with an explicit GENERIC config: this module binds
    FULL_TOML, whose ``[[require]]`` rows name the real llm/storage axes — a bare Registry
    resolving that ambiently would fail the unknown-owner validation (no such axes here)."""
    from conftest import GENERIC_TOML, make_config

    return plugins.Registry([plugin], config=make_config(GENERIC_TOML))


def _text(static) -> str:
    """The plain rendered text of a Static (no public `renderable` in textual 8)."""
    return str(static.render())


def _fid(app) -> str | None:
    """The focused widget's id (None if nothing focused) — narrows the Optional `focused`."""
    return app.focused.id if app.focused else None


# ── devmode: pure logic ─────────────────────────────────────────────────────────────
# (PAM-grant parsing + _human_left moved to the gcp plugin — see test_plugins.py.)


def test_strip_ansi_removes_colour_codes():
    assert devmode._strip_ansi("\x1b[31mERROR\x1b[0m: boom") == "ERROR: boom"
    assert devmode._strip_ansi("plain") == "plain"


def test_run_records_command_log():
    devmode.cmd_log_reset()
    rc, _ = devmode._run(["true"])
    log = devmode.cmd_log()
    assert rc == 0 and log and log[-1]["cmd"] == "true" and log[-1]["rc"] == 0


def test_secret_output_is_redacted_in_log_not_in_return():
    # successful credential-printing commands: token/PEM kept out of the log
    assert (
        devmode._redact_for_log("gh auth token", 0, "ghp_secret")
        == "<output redacted — credential>"
    )
    assert (
        devmode._redact_for_log(
            "gcloud auth print-access-token --impersonate-service-account=x", 0, "ya29.tok"
        )
        == "<output redacted — credential>"
    )
    assert (
        devmode._redact_for_log("gcloud secrets versions access latest", 0, "-----BEGIN-----")
        == "<output redacted — credential>"
    )
    # failures keep their (non-secret) error, and non-secret commands are untouched
    assert devmode._redact_for_log("gh auth token", 1, "not logged in") == "not logged in"
    assert devmode._redact_for_log("docker info", 0, "ok") == "ok"


def test_run_redacts_sensitive_command_output_end_to_end():
    devmode.cmd_log_reset()
    # `echo` succeeds; its cmd string contains a sensitive marker, so the log redacts
    # the output while the caller still receives the real text.
    rc, out = devmode._run(["echo", "auth", "token", "SECRET"])
    assert rc == 0 and "SECRET" in out  # caller sees real output
    assert "SECRET" not in devmode.cmd_log()[-1]["out"]  # log does not


def test_cmd_log_text_renders_command_and_output():
    text = str(tui._cmd_log_text([{"cmd": "gcloud whoami", "rc": 1, "out": "\x1b[31mboom\x1b[0m"}]))
    assert "gcloud whoami" in text and "rc=1" in text and "boom" in text
    assert "\x1b" not in text  # ANSI stripped
    assert "no commands yet" in str(tui._cmd_log_text([]))


def test_doctor_is_a_generator_with_running_placeholders():
    rows = list(devmode.doctor(deep=False))
    statuses = {status for status, _, _ in rows}
    assert statuses <= {"ok", "warn", "fail", "running"}
    # Every 'running' placeholder is resolved by a later real row of the same name.
    running = [name for status, name, _ in rows if status == "running"]
    resolved = [name for status, name, _ in rows if status != "running"]
    for name in running:
        assert name in resolved


def test_doctor_cli_skips_running(capsys):
    # The CLI prints one line per resolved check and drops the 'running' placeholders
    # (so the count matches even though some detail text legitimately says "running").
    n_results = sum(1 for status, _, _ in devmode.doctor(deep=False) if status != "running")
    devmode.doctor_cli(deep=False)
    marked = [ln for ln in capsys.readouterr().out.splitlines() if any(g in ln for g in "✓○✗")]
    assert len(marked) == n_results
    assert not any(frame in ln for ln in marked for frame in tui.SPINNER)


def test_branches_returns_list():
    assert isinstance(devmode.branches(), list)  # smoke: real git, parsing must not throw


# ── devmode: the "dev box env out of date" hint ───────────────────────────────────────


def _hint_for(monkeypatch, box_env, mode, *, claude_keyless=None, codex_keyless=None):
    """Drive _box_env_hint with a stubbed engine-inspect (box env) + keyless config."""
    import json as _json

    class _P:
        returncode = 0
        stdout = _json.dumps([f"{k}={v}" for k, v in box_env.items()])

    monkeypatch.setattr(devmode.subprocess, "run", lambda *a, **k: _P())
    monkeypatch.setattr(devmode.config, "engine", lambda: "docker")
    monkeypatch.setattr(devmode.config, "claude_keyless", lambda: claude_keyless)
    monkeypatch.setattr(devmode.config, "codex_keyless", lambda: codex_keyless)
    full = dict.fromkeys(devmode.axes(), "off")
    full.update(mode)
    return devmode._box_env_hint(full, project="tangible-podman")


def test_box_env_hint_ignores_always_baked_gcp_and_proxy(monkeypatch):
    # gcp=off (the default) but the box ALWAYS bakes GCE_METADATA_HOST + HTTPS_PROXY — this used to
    # mis-fire "gcp metadata out of date" forever. They no longer track the mode, so: no hint.
    env = {"GCE_METADATA_HOST": "metadata-emulator:80", "HTTPS_PROXY": "http://h:8088"}
    assert _hint_for(monkeypatch, env, {}) is None


def test_box_env_hint_flags_stale_keyless_then_clears(monkeypatch):
    # claude keyless on but the box hasn't baked its dummy token yet → stale until `fy box up`.
    base = {"GCE_METADATA_HOST": "metadata-emulator:80", "HTTPS_PROXY": "http://h:8088"}
    hint = _hint_for(monkeypatch, base, {"claude": "on"}, claude_keyless="oauth")
    assert hint and "claude keyless" in hint
    baked = {**base, "CLAUDE_CODE_OAUTH_TOKEN": "sk-ant-oat-dummy"}
    assert _hint_for(monkeypatch, baked, {"claude": "on"}, claude_keyless="oauth") is None


def test_box_env_hint_skips_file_based_codex_chatgpt(monkeypatch):
    # codex `chatgpt` keyless lives in ~/.codex/auth.json (a file), not Config.Env — so it can't be
    # detected here and must never be flagged (no false alarm), even with codex on and no env var.
    assert _hint_for(monkeypatch, {}, {"codex": "on"}, codex_keyless="chatgpt") is None


def _mac_doctor(monkeypatch, account_out, recorder=None):
    """Drive the Mac-side doctor with gcloud present and a stubbed _run."""
    monkeypatch.setattr(devmode, "in_box", lambda: False)
    monkeypatch.setattr(devmode, "_which", lambda c: True)
    created = devmode._iso(devmode.now() - timedelta(hours=1))

    def fake_run(cmd, timeout=8):
        s = " ".join(cmd)
        if recorder is not None:
            recorder.append(s)
        if "config get-value account" in s:
            return 0, account_out
        if "pam grants list" in s:
            return 0, f"ACTIVE\tme@x.io\t{created}\t43200s"
        return 0, "ok"

    monkeypatch.setattr(devmode, "_run", fake_run)
    return [(s, n, d) for s, n, d in devmode.doctor(deep=False)]


def test_pam_runs_in_fast_pass_when_account_present(monkeypatch):
    calls: list = []
    rows = _mac_doctor(monkeypatch, "me@x.io", calls)
    pam = [d for s, n, d in rows if n == "PAM elevation" and s != "running"]
    assert pam and "active grant (me@x.io)" in pam[0]
    # the list call is scoped to the account, so others' grants never come back
    assert any("pam grants list" in c and "requester:me@x.io" in c for c in calls)


def test_pam_skipped_entirely_without_account(monkeypatch):
    calls: list = []
    rows = _mac_doctor(monkeypatch, "(unset)", calls)
    assert not any(n == "PAM elevation" for _, n, _ in rows)
    assert not any("pam grants list" in c for c in calls)  # no call made at all


# ── doctor: core podman checks (Mac-only, not a plugin) ───────────────────────────────


def test_doctor_includes_engine_and_machine_checks_on_mac(monkeypatch):
    # The machine row is LABELLED by the active backend, so under the lima default it reads
    # "lima machine". The podman CLI row is unconditional: podman drives the socket whatever
    # backend produced it (lima needs limactl AND podman).
    monkeypatch.setattr(devmode, "machine_state", lambda: "running")
    # A non-podman backend also gets its own lifecycle-CLI row, and the machine row is gated on
    # it — so the box (no limactl) needs that stubbed to reach the machine row at all.
    monkeypatch.setattr(devmode._BACKEND, "available", lambda: True)
    rows = _mac_doctor(monkeypatch, "me@x.io")
    names = [n for _, n, _ in rows]
    assert "podman CLI" in names
    assert f"{devmode._BACKEND.name} machine" in names


def test_podman_checks_stop_at_cli_when_podman_missing(monkeypatch):
    # No podman CLI ⇒ the CLI row fails and there's nothing to inspect (no machine row).
    monkeypatch.setattr(devmode, "_which", lambda c: c != "podman")
    rows = [(s, n) for s, n, _ in devmode._podman_checks()]
    assert rows == [("fail", "podman CLI")]


def test_machine_state_maps_to_status(monkeypatch):
    # running ⇒ ok; stopped/uninitialised ⇒ warn (the expected, fixable pre-`fy up` state).
    monkeypatch.setattr(devmode, "_which", lambda c: True)
    monkeypatch.setattr(devmode._BACKEND, "available", lambda: True)
    label = f"{devmode._BACKEND.name} machine"
    for state, expected in [("running", "ok"), ("stopped", "warn"), ("unknown", "warn")]:
        monkeypatch.setattr(devmode, "machine_state", lambda s=state: s)
        by_name = {n: s for s, n, _ in devmode._podman_checks()}
        assert by_name["podman CLI"] == "ok"
        assert by_name[label] == expected


# ── TUI fixtures ────────────────────────────────────────────────────────────────────


class _CleanDrift:
    """An adopted, unchanged checkout — the normal state, and the one the devbox tests assume.
    They exercise the toggle, not the adopt gate; without it every `d` press would stop at
    AdoptConfigScreen because a tmp checkout has nothing pinned.

    Applied per-test, never autouse: `configpin.inspect` is shared with devmode's own status path,
    which reads fields this stand-in doesn't carry."""

    pinned_exists = True
    changed = False
    adopted = True
    cfg = None
    label = "main"
    # `configpin.effective()` reads these to build the adopted TOML, and it shares `inspect` with
    # us — so the stand-in carries the real field shape, not just what the TUI path touches.
    pinned = dict.fromkeys(configpin.PINNED_FILES)
    tree = dict.fromkeys(configpin.PINNED_FILES)

    def summary(self) -> str:
        return ""

    def tree_digest(self) -> str:
        return "0" * 16


@pytest.fixture(autouse=True)
def _stub_mac_only(monkeypatch):
    """The box can't write the mode file; stub the write so button presses don't raise."""
    _sig = {"env": {}, "overlays": []}
    monkeypatch.setattr(
        devmode,
        "set_mode",
        lambda *a, **k: {
            "mode": {},
            "expires": {},
            "prev_posture": _sig,
            "new_posture": _sig,
            "prev_profiles": "",
            "new_profiles": "",
        },
    )


def _fixed_doctor(deep=False):
    yield ("running", "engine socket", "")
    # The exact shapes that crashed the markup-string render in the wild: a raw ANSI
    # colour code (gcloud colours its output) and a stray closing tag.
    yield ("fail", "gcloud account", "\x1b[31mERROR\x1b[0m: (gcloud) [/red] denied")
    yield ("ok", "gcloud CLI", "installed")


# ── TUI: navigation ─────────────────────────────────────────────────────────────────


async def test_starts_on_tab_strip_no_focus():
    async with tui.DevModeTui().run_test() as pilot:
        await pilot.pause()
        assert pilot.app.focused is None


async def test_right_cycles_tabs_left_steps_out_to_list():
    async with tui.DevModeTui().run_test() as pilot:
        tabs = pilot.app.query_one(tui.TabbedContent)
        assert tabs.active == "tab-mode"
        await pilot.press("right")
        assert tabs.active == "tab-gcp"  # gcp's plugin-contributed GCP Tokens panel
        await pilot.press("right")
        assert tabs.active == "tab-network"  # proxy's plugin-contributed Network Log panel
        await pilot.press("right", "right")  # to doctor, then stays (no wrap)
        assert tabs.active == "tab-doctor"
        await pilot.press("left", "left", "left")  # doctor → network → gcp → mode
        assert tabs.active == "tab-mode"
        await pilot.press("left")  # ← off the first tab → workspace list
        assert isinstance(pilot.app.focused, tui.WorkspaceList)


def _two_workspaces():
    return [
        {
            "name": "main",
            "path": "/repo",
            "project": "proj",
            "branch": "main",
            "app_port": 3000,
            "containers": 0,
            "devbox": False,
        },
        {
            "name": "feat",
            "path": "/repo-wt/feat",
            "project": "proj-feat",
            "branch": "wt/feat",
            "app_port": 3007,
            "containers": 0,
            "devbox": False,
        },
    ]


async def test_remove_worktree_shortcut_hidden_on_main_and_new_row(monkeypatch):
    monkeypatch.setattr(devmode, "workspaces", _two_workspaces)
    async with tui.DevModeTui().run_test() as pilot:
        await pilot.pause()
        lv = cast(tui.DevModeTui, pilot.app).query_one("#workspaces", tui.WorkspaceList)
        lv.focus()
        lv.index = 0  # main → can't remove the main checkout
        await pilot.pause()
        assert lv.check_action("remove_worktree", ()) is False
        lv.index = 1  # a real worktree → remove is offered
        await pilot.pause()
        assert lv.check_action("remove_worktree", ()) is True
        lv.index = 2  # the ＋ new-worktree row → nothing to remove
        await pilot.pause()
        assert lv.check_action("remove_worktree", ()) is False


async def test_mode_button_targets_the_selected_workspace(monkeypatch):
    # Posture is per-worktree: highlighting a workspace and pressing a mode button must write THAT
    # worktree's posture (bound to its config), not a host-wide global — the whole point of the
    # per-worktree TUI (so a human acting on the 'feat' card never silently changes main).
    monkeypatch.setattr(devmode, "workspaces", _two_workspaces)
    full = {
        "proxy": {},
        "plugins": {"gcp-metadata": {"project": "a"}, "github": {}, "auth0-sim": {}},
    }
    monkeypatch.setattr(
        devmode,
        "worktree_config",
        lambda wt: config.Config(repo_root=config.repo_root(), worktree=wt, toml=full),
    )
    seen: dict = {}

    def fake_set_mode(updates, ttl=None, reconcile=True, reconcile_sink=None):
        seen["wt"] = config.active_worktree()  # the worktree bound at the moment of the write
        seen["updates"] = updates
        # Unchanged posture signature ⇒ the TUI skips the off-thread reconcile (equal prev/new).
        sig = {"env": {}, "overlays": []}
        return {
            "mode": {},
            "expires": {},
            "prev_posture": sig,
            "new_posture": sig,
            "prev_profiles": "",
            "new_profiles": "",
        }

    monkeypatch.setattr(devmode, "set_mode", fake_set_mode)
    async with tui.DevModeTui().run_test() as pilot:
        await pilot.pause()
        app = cast(tui.DevModeTui, pilot.app)
        app.query_one("#workspaces", tui.WorkspaceList).index = 1  # highlight the 'feat' worktree
        await pilot.pause()
        btn = app.query_one("#github-app", tui.Button)
        app.on_button_pressed(tui.Button.Pressed(btn))
    assert seen["updates"] == {"github": "app"}
    assert seen["wt"] == "feat"  # wrote feat's posture under feat's binding, not main's


async def test_highlight_dispatched_after_shutdown_does_not_crash_the_message_loop():
    """Textual keeps dispatching queued messages after the app stops and its screens are popped.
    `refresh_bindings()` goes through `self.screen`, so an unguarded Highlighted arriving then
    raised ScreenStackError out of the message loop — a crash traceback on quit, and a rare
    suite failure attributed to whichever test was mid-run. Deterministic here: run the app,
    let it shut down, then deliver the message it would have been holding."""
    async with tui.DevModeTui().run_test() as pilot:
        await pilot.pause()
        app = cast(tui.DevModeTui, pilot.app)

    assert not app.is_running and not app._screen_stacks["_default"]  # the state we captured
    app.on_list_view_highlighted(None)  # must be a no-op, not an exception


def test_fifo_gate_orders_by_ticket_not_by_which_thread_starts_first():
    """The guarantee a plain `threading.Lock` does NOT give. Mutual exclusion alone let the
    second toggle's worker win the lock and apply its reconcile first, leaving the stack on the
    posture the user toggled AWAY from — which surfaced as an ~8%-under-load flake in the TUI
    test below. Deterministic here: the second ticket's thread gets a head start and must STILL
    wait, so a non-FIFO implementation fails this every run rather than occasionally."""
    import threading
    import time

    gate = tui._FifoGate()
    first, second = gate.take(), gate.take()  # issued in press order, on the UI thread
    order: list[str] = []

    def run(ticket, label, entered=None):
        if entered is not None:
            entered.set()
        with ticket:
            order.append(label)
            time.sleep(0.05)

    running = threading.Event()
    t2 = threading.Thread(target=run, args=(second, "second", running))
    t2.start()
    running.wait(5)
    time.sleep(0.15)  # a clear head start for the LATER ticket
    assert order == []  # blocked on its predecessor — not merely losing a race

    t1 = threading.Thread(target=run, args=(first, "first"))
    t1.start()
    t1.join(5)
    t2.join(5)
    assert order == ["first", "second"]


def test_fifo_gate_releases_the_queue_when_a_ticket_holder_raises():
    # A reconcile that throws must not strand every later toggle behind it (the __exit__ runs on
    # the exception path too) — a stalled queue would be a hung TUI, worse than the bug it fixes.
    gate = tui._FifoGate()
    first, second = gate.take(), gate.take()
    with pytest.raises(RuntimeError), first:
        raise RuntimeError("compose blew up")
    with second:  # must not block
        pass


async def test_rapid_mode_toggles_serialize_the_stack_reconciles(monkeypatch):
    # Two quick posture toggles on the SAME workspace must never run two `docker compose`
    # reconciles concurrently against its stack (that's the corruption the per-worktree lock in
    # _reconcile_off_thread exists to prevent — run_worker alone doesn't queue) — and neither
    # toggle may be lost: both reconciles run, in order, each starting from the posture the
    # previous one ended on.
    import threading
    import time

    counter = {"i": 0}

    def fake_set_mode(updates, ttl=None, reconcile=True, reconcile_sink=None):
        i = counter["i"] = counter["i"] + 1
        # Each call moves the posture signature one step: prev N-1 → new N.
        return {
            "mode": {},
            "expires": {},
            "prev_posture": {"env": {"N": str(i - 1)}, "overlays": []},
            "new_posture": {"env": {"N": str(i)}, "overlays": []},
            "prev_profiles": "",
            "new_profiles": str(i),
        }

    counts = {"active": 0, "max_active": 0}
    calls: list[tuple[str, str]] = []
    guard = threading.Lock()

    def fake_reconcile(prev, new, sink=None):
        with guard:
            counts["active"] += 1
            counts["max_active"] = max(counts["max_active"], counts["active"])
        time.sleep(0.2)  # long enough that unserialized workers would overlap here
        with guard:
            counts["active"] -= 1
            calls.append((prev["env"]["N"], new["env"]["N"]))
        return True

    monkeypatch.setattr(devmode, "set_mode", fake_set_mode)
    monkeypatch.setattr(devmode, "reconcile_stack", fake_reconcile)
    async with tui.DevModeTui().run_test() as pilot:
        await pilot.pause()
        app = cast(tui.DevModeTui, pilot.app)
        # two back-to-back mode-button presses, no waiting in between (the rapid toggle)
        app.on_button_pressed(tui.Button.Pressed(app.query_one("#gcp-logs", tui.Button)))
        app.on_button_pressed(tui.Button.Pressed(app.query_one("#gcp-off", tui.Button)))
        await app.workers.wait_for_complete()
    assert counts["max_active"] == 1  # the reconciles never overlapped
    assert calls == [("0", "1"), ("1", "2")]  # both ran, in order, chained


async def test_rapid_toggles_on_different_worktrees_both_reconcile(monkeypatch):
    # The serialization is PER worktree — quick toggles on two different workspace cards must
    # both reconcile (each under its own worktree's config binding), not deadlock on one lock.
    monkeypatch.setattr(devmode, "workspaces", _two_workspaces)
    full = {
        "proxy": {},
        "plugins": {"gcp-metadata": {"project": "a"}, "github": {}, "auth0-sim": {}},
    }
    monkeypatch.setattr(
        devmode,
        "worktree_config",
        lambda wt: config.Config(repo_root=config.repo_root(), worktree=wt, toml=full),
    )
    counter = {"i": 0}

    def fake_set_mode(updates, ttl=None, reconcile=True, reconcile_sink=None):
        i = counter["i"] = counter["i"] + 1
        return {
            "mode": {},
            "expires": {},
            "prev_posture": {"env": {}, "overlays": []},
            "new_posture": {"env": {"N": str(i)}, "overlays": []},
            "prev_profiles": "",
            "new_profiles": str(i),
        }

    reconciled: list[str] = []

    def fake_reconcile(prev, new, sink=None):
        reconciled.append(config.active_worktree())  # the binding the worker reconciled under
        return True

    monkeypatch.setattr(devmode, "set_mode", fake_set_mode)
    monkeypatch.setattr(devmode, "reconcile_stack", fake_reconcile)
    async with tui.DevModeTui().run_test() as pilot:
        await pilot.pause()
        app = cast(tui.DevModeTui, pilot.app)
        lv = app.query_one("#workspaces", tui.WorkspaceList)
        lv.index = 0  # main
        await pilot.pause()
        app.on_button_pressed(tui.Button.Pressed(app.query_one("#github-app", tui.Button)))
        lv.index = 1  # feat — immediately, while main's reconcile may still be running
        await pilot.pause()
        app.on_button_pressed(tui.Button.Pressed(app.query_one("#github-off", tui.Button)))
        await app.workers.wait_for_complete()
    assert sorted(reconciled) == ["", "feat"]  # each ran once, under its own worktree


async def test_mode_grid_rebuilds_for_selected_worktree_axes(monkeypatch):
    monkeypatch.setattr(devmode, "workspaces", _two_workspaces)
    monkeypatch.setattr(
        devmode,
        "worktree_config",
        lambda wt: config.Config(repo_root=config.repo_root(), worktree=wt, toml={}),
    )

    def axes():
        out = {"github": ("off", "app")}
        if config.active_worktree() == "feat":
            out["gcp"] = ("off", "logs")
        return out

    def read():
        return {
            "mode": dict.fromkeys(axes(), "off"),
            "expires": {},
            "written": None,
            "daemons": {},
        }

    def blurbs():
        return {(axis, rung): f"{axis} {rung}" for axis, rungs in axes().items() for rung in rungs}

    monkeypatch.setattr(devmode, "axes", axes)
    monkeypatch.setattr(devmode, "read", read)
    monkeypatch.setattr(devmode, "mode_blurb", blurbs)
    monkeypatch.setattr(devmode, "emergency", lambda: {})
    monkeypatch.setattr(devmode, "axis_daemon", lambda: {})
    monkeypatch.setattr(devmode, "daemon_status", lambda mode: {})
    monkeypatch.setattr(devmode, "doctor", lambda deep=False: [])

    async with tui.DevModeTui().run_test() as pilot:
        await pilot.pause()
        app = cast(tui.DevModeTui, pilot.app)
        assert [row.axis for row in app.query(tui.AxisRow)] == ["github"]
        app.query_one("#workspaces", tui.WorkspaceList).index = 1
        app.refresh_mode()
        await pilot.pause()
        assert [row.axis for row in app.query(tui.AxisRow)] == ["github", "gcp"]
        assert app.query_one("#gcp-logs", tui.Button)


async def test_remove_worktree_confirm_calls_removal(monkeypatch):
    monkeypatch.setattr(devmode, "workspaces", _two_workspaces)
    called: list[str] = []
    monkeypatch.setattr(
        devmode, "remove_worktree", lambda name: called.append(name) or (0, f"✓ removed {name}")
    )
    async with tui.DevModeTui().run_test() as pilot:
        await pilot.pause()
        app = cast(tui.DevModeTui, pilot.app)
        lv = app.query_one("#workspaces", tui.WorkspaceList)
        lv.focus()
        lv.index = 1  # the 'feat' worktree
        await pilot.pause()
        await pilot.press("x")  # opens the confirmation dialog
        assert isinstance(app.screen, tui.RemoveWorktreeScreen)
        await pilot.press("y")  # confirm → removal runs on a worker thread
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert called == ["feat"]


async def test_remove_worktree_button_press_is_not_a_mode_button(monkeypatch):
    """Clicking the dialog's Remove BUTTON (vs the y binding) must not bubble to the app's
    mode-button handler, which would parse 'rm-yes' as axis 'rm' and toast 'unknown axis'."""
    monkeypatch.setattr(devmode, "workspaces", _two_workspaces)
    called: list[str] = []
    monkeypatch.setattr(
        devmode, "remove_worktree", lambda name: called.append(name) or (0, f"✓ removed {name}")
    )
    mode_sets: list[dict] = []
    monkeypatch.setattr(devmode, "set_mode", lambda updates, **kw: mode_sets.append(updates) or {})
    async with tui.DevModeTui().run_test() as pilot:
        await pilot.pause()
        app = cast(tui.DevModeTui, pilot.app)
        lv = app.query_one("#workspaces", tui.WorkspaceList)
        lv.focus()
        lv.index = 1
        await pilot.pause()
        await pilot.press("x")
        assert isinstance(app.screen, tui.RemoveWorktreeScreen)
        await pilot.click("#rm-yes")  # the Button, not the y binding
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert called == ["feat"]
        assert mode_sets == []  # the press never reached the app's axis-button handler


async def test_remove_worktree_cancel_does_nothing(monkeypatch):
    monkeypatch.setattr(devmode, "workspaces", _two_workspaces)
    called: list[str] = []
    monkeypatch.setattr(devmode, "remove_worktree", lambda name: called.append(name) or (0, ""))
    async with tui.DevModeTui().run_test() as pilot:
        await pilot.pause()
        app = cast(tui.DevModeTui, pilot.app)
        lv = app.query_one("#workspaces", tui.WorkspaceList)
        lv.focus()
        lv.index = 1
        await pilot.pause()
        await pilot.press("x")
        assert isinstance(app.screen, tui.RemoveWorktreeScreen)
        await pilot.press("escape")  # decline → dialog closes, nothing torn down
        await pilot.pause()
        assert not isinstance(app.screen, tui.RemoveWorktreeScreen)
        assert called == []


# ── TUI: dev-box toggle (d) + stop confirmations ──────────────────────────────────────


def _two_workspaces_feat_box_up():
    ws = _two_workspaces()
    ws[1]["devbox"] = True
    return ws


async def test_devbox_toggle_starts_without_confirmation_when_down(monkeypatch):
    # feat's box is down → `d` is the non-destructive direction: `fy box up` runs straight away.
    ups, downs = [], []
    monkeypatch.setattr(devmode, "workspaces", _two_workspaces)
    monkeypatch.setattr(tui.configpin, "inspect", lambda cfg: _CleanDrift())
    monkeypatch.setattr(devmode, "box_up", lambda name: ups.append(name) or (0, "box up"))
    monkeypatch.setattr(devmode, "box_down", lambda name: downs.append(name) or (0, ""))
    async with tui.DevModeTui().run_test() as pilot:
        await pilot.pause()
        app = cast(tui.DevModeTui, pilot.app)
        lv = app.query_one("#workspaces", tui.WorkspaceList)
        lv.focus()
        lv.index = 1  # the 'feat' worktree
        await pilot.pause()
        await pilot.press("d")
        assert not isinstance(app.screen, tui.ConfirmScreen)  # no modal for starting
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert ups == ["feat"] and downs == []


async def test_devbox_rapid_presses_launch_only_one_operation(monkeypatch):
    # The card's devbox flag refreshes on a 5s timer, so a second `d` mid-`fy box up` sees the
    # box as still down — without the _devbox_busy guard it would launch a SECOND concurrent
    # `box up` against the same container. The guard must also clear when the run finishes
    # (worker finally), so a later press starts a fresh operation as before.
    import threading

    release = threading.Event()
    ups: list[str] = []

    def slow_box_up(name):
        ups.append(name)
        release.wait(5)  # in flight until the test releases it (timeout = failsafe only)
        return (0, "box up")

    monkeypatch.setattr(devmode, "workspaces", _two_workspaces)
    monkeypatch.setattr(tui.configpin, "inspect", lambda cfg: _CleanDrift())
    monkeypatch.setattr(devmode, "box_up", slow_box_up)
    async with tui.DevModeTui().run_test() as pilot:
        await pilot.pause()
        app = cast(tui.DevModeTui, pilot.app)
        lv = app.query_one("#workspaces", tui.WorkspaceList)
        lv.focus()
        lv.index = 1  # the 'feat' worktree, box down
        await pilot.pause()
        await pilot.press("d")
        await pilot.press("d")  # back-to-back, while the first `box up` is still running
        await pilot.pause()
        assert ups == ["feat"]  # the second press was ignored, not queued or run concurrently
        release.set()
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert ups == ["feat"]  # completion launched nothing extra
        await pilot.press("d")  # guard cleared → a fresh press starts a new operation
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert ups == ["feat", "feat"]


async def test_devbox_stop_asks_for_confirmation_then_runs(monkeypatch):
    # feat's box is UP → `d` is destructive (an agent session may run inside): confirm first.
    ups, downs = [], []
    monkeypatch.setattr(devmode, "workspaces", _two_workspaces_feat_box_up)
    monkeypatch.setattr(devmode, "box_up", lambda name: ups.append(name) or (0, ""))
    monkeypatch.setattr(devmode, "box_down", lambda name: downs.append(name) or (0, "stopped"))
    async with tui.DevModeTui().run_test() as pilot:
        await pilot.pause()
        app = cast(tui.DevModeTui, pilot.app)
        lv = app.query_one("#workspaces", tui.WorkspaceList)
        lv.focus()
        lv.index = 1
        await pilot.pause()
        await pilot.press("d")
        assert isinstance(app.screen, tui.ConfirmScreen)
        await pilot.press("y")  # confirm → `fy box down` runs on a worker thread
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert downs == ["feat"] and ups == []


async def test_devbox_stop_cancel_does_nothing_and_never_hits_the_mode_handler(monkeypatch):
    # Declining leaves the box alone — and clicking the dialog's BUTTON (vs the y/n bindings)
    # must not bubble to the app's mode-button handler ("cf-no" would parse as axis 'cf').
    downs: list[str] = []
    mode_sets: list[dict] = []
    monkeypatch.setattr(devmode, "workspaces", _two_workspaces_feat_box_up)
    monkeypatch.setattr(devmode, "box_down", lambda name: downs.append(name) or (0, ""))
    monkeypatch.setattr(devmode, "set_mode", lambda updates, **kw: mode_sets.append(updates) or {})
    async with tui.DevModeTui().run_test() as pilot:
        await pilot.pause()
        app = cast(tui.DevModeTui, pilot.app)
        lv = app.query_one("#workspaces", tui.WorkspaceList)
        lv.focus()
        lv.index = 1
        await pilot.pause()
        await pilot.press("d")
        assert isinstance(app.screen, tui.ConfirmScreen)
        await pilot.click("#cf-no")  # the Cancel Button, not the n/esc binding
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert not isinstance(app.screen, tui.ConfirmScreen)
        assert downs == [] and mode_sets == []


async def test_machine_stop_asks_for_confirmation(monkeypatch):
    # Stopping the machine takes every workspace down at once → confirm; cancel is a no-op.
    toggled: list[bool] = []
    monkeypatch.setattr(devmode, "workspaces", _two_workspaces)
    monkeypatch.setattr(devmode, "machine_state", lambda: "running")
    monkeypatch.setattr(devmode, "machine_toggle", lambda: toggled.append(True) or (0, "stopped"))
    async with tui.DevModeTui().run_test() as pilot:
        await pilot.pause()
        app = cast(tui.DevModeTui, pilot.app)
        lv = app.query_one("#workspaces", tui.WorkspaceList)
        lv.focus()
        lv.index = 0
        await pilot.pause()
        await pilot.press("s")
        assert isinstance(app.screen, tui.ConfirmScreen)
        await pilot.press("escape")  # decline → nothing stopped
        await pilot.pause()
        assert toggled == []
        await pilot.press("s")
        assert isinstance(app.screen, tui.ConfirmScreen)
        await pilot.press("y")  # confirm → the toggle runs
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert toggled == [True]


class _PanelPlugin(plugins.Plugin):
    """A third-party plugin contributing one data-only TUI panel — proves the TUI renders
    plugin panels generically (it never names github / the Network Log)."""

    name = "demo-panel"

    def tui_panels(self):
        return [
            plugins.TuiPanel(
                id="demo",
                title="Demo Panel",
                columns=("k", "v"),
                refresh=lambda: plugins.PanelData("the summary", [("a", "1"), ("b", "2")]),
            )
        ]


async def test_plugin_panel_is_rendered_navigated_and_focused(monkeypatch):
    monkeypatch.setattr(tui, "registry", lambda: _panel_registry(_PanelPlugin()))
    async with tui.DevModeTui().run_test() as pilot:
        await pilot.pause()
        app = cast(tui.DevModeTui, pilot.app)
        # the panel slots between Mode and Doctor — purely from the hook, no hardcoding
        assert app._pane_ids() == ["tab-mode", "tab-demo", "tab-doctor"]
        await pilot.press("right")  # mode → the plugin panel
        assert app.query_one(tui.TabbedContent).active == "tab-demo"
        # rendered from PanelData: the table rows + the summary line
        assert app.query_one("#demo-table", tui.DataTable).row_count == 2
        assert "the summary" in _text(app.query_one("#demo-summary", tui.Static))
        await pilot.press("down")  # ↓ enters the panel → its table takes focus
        assert _fid(app) == "demo-table"


class _TreePanelPlugin(plugins.Plugin):
    """A plugin contributing a TREE-kind panel — proves the TUI renders grouped, collapsible
    panels generically (the Network Log is one). The refresh is swappable so a test can change the
    data and assert the rebuild + expand-state preservation."""

    name = "tree-panel"

    def __init__(self, data_fn) -> None:
        self._data_fn = data_fn

    def tui_panels(self):
        return [
            plugins.TuiPanel(
                id="tree", title="Tree Panel", columns=(), refresh=self._data_fn, kind="tree"
            )
        ]


async def test_tree_panel_renders_groups_summary_and_focus(monkeypatch):
    data = plugins.PanelTree(
        summary="2 hosts",
        groups=[
            plugins.PanelGroup(key="a.com", label="[b]a.com[/b] 2", children=["r1", "r2"]),
            plugins.PanelGroup(key="b.com", label="[b]b.com[/b] 1", children=["r3"]),
        ],
    )
    monkeypatch.setattr(tui, "registry", lambda: _panel_registry(_TreePanelPlugin(lambda: data)))
    async with tui.DevModeTui().run_test() as pilot:
        await pilot.pause()
        app = cast(tui.DevModeTui, pilot.app)
        tree = app.query_one("#tree-tree", tui.Tree)
        # two top-level group nodes, keyed by host, each with its children
        assert [n.data for n in tree.root.children] == ["a.com", "b.com"]
        assert len(tree.root.children[0].children) == 2
        assert "2 hosts" in _text(app.query_one("#tree-summary", tui.Static))
        await pilot.press("right")  # mode → the tree panel
        await pilot.press("down")  # ↓ enters → the tree takes focus
        assert _fid(app) == "tree-tree"


async def test_tree_panel_preserves_expand_state_across_a_rebuild(monkeypatch):
    # The data grows (a.com gains a request) → the tree rebuilds; the group the user expanded must
    # stay expanded (keyed by host), while an unchanged tick must not touch the tree at all.
    state = {
        "data": plugins.PanelTree(
            summary="s", groups=[plugins.PanelGroup(key="a.com", label="a.com", children=["r1"])]
        )
    }
    monkeypatch.setattr(
        tui, "registry", lambda: _panel_registry(_TreePanelPlugin(lambda: state["data"]))
    )
    async with tui.DevModeTui().run_test() as pilot:
        await pilot.pause()
        app = cast(tui.DevModeTui, pilot.app)
        tree = app.query_one("#tree-tree", tui.Tree)
        tree.root.children[0].expand()  # user opens the a.com group
        assert tree.root.children[0].is_expanded
        # grow a.com's children → signature changes → rebuild
        state["data"] = plugins.PanelTree(
            summary="s",
            groups=[plugins.PanelGroup(key="a.com", label="a.com", children=["r1", "r2"])],
        )
        app.refresh_panels()
        assert len(tree.root.children[0].children) == 2
        assert tree.root.children[0].is_expanded  # expand state survived the rebuild


class _NetworkPanelPlugin(plugins.Plugin):
    """A tree panel with the Network Log's id, so the allow-host action (which keys off
    `#network-tree` / `tab-network`) has its real target to drive in the pilot."""

    name = "net-demo"

    def __init__(self, data_fn) -> None:
        self._data_fn = data_fn

    def tui_panels(self):
        return [
            plugins.TuiPanel(
                id="network", title="Network Log", columns=(), refresh=self._data_fn, kind="tree"
            )
        ]


def _blocked_tree():
    return plugins.PanelTree(
        summary="1 blocked",
        groups=[
            plugins.PanelGroup(
                key="evil.example.com",
                label="[b]evil.example.com[/b] · ⛔ 1 blocked",
                children=["⛔ blocked by the egress wall"],
            )
        ],
    )


async def test_allow_host_action_is_gated_to_the_network_tab(monkeypatch):
    # `a` (allow host) is meaningful only on the Network Log tab — check_action hides it elsewhere.
    monkeypatch.setattr(
        tui, "registry", lambda: _panel_registry(_NetworkPanelPlugin(_blocked_tree))
    )
    async with tui.DevModeTui().run_test() as pilot:
        await pilot.pause()
        app = cast(tui.DevModeTui, pilot.app)
        assert app.query_one(tui.TabbedContent).active == "tab-mode"
        assert app.check_action("allow_host", ()) is False  # hidden off the network tab
        app.query_one(tui.TabbedContent).active = "tab-network"
        await pilot.pause()
        assert app.check_action("allow_host", ()) is True  # shown on it


async def test_allow_host_grants_the_picked_level(monkeypatch):
    # Focus a blocked host in the Network Log, press `a`, pick `permanent` → allowlist.grant runs
    # in a worker with that host + level. The grant is stubbed (the real one writes Mac state).
    monkeypatch.setattr(
        tui, "registry", lambda: _panel_registry(_NetworkPanelPlugin(_blocked_tree))
    )
    grants: list[tuple[str, str]] = []
    monkeypatch.setattr(tui.allowlist, "grant", lambda host, level: grants.append((host, level)))
    async with tui.DevModeTui().run_test() as pilot:
        await pilot.pause()
        app = cast(tui.DevModeTui, pilot.app)
        app.query_one(tui.TabbedContent).active = "tab-network"
        await pilot.pause()
        tree = app.query_one("#network-tree", tui.Tree)
        tree.focus()
        tree.cursor_line = 0  # the first host group node
        await pilot.pause()
        assert tree.cursor_node is not None and tree.cursor_node.data == "evil.example.com"
        await pilot.press("a")  # open the level picker
        await pilot.pause()
        assert isinstance(app.screen, tui.AllowHostScreen)
        await pilot.press("p")  # permanent
        await pilot.pause()
        await app.workers.wait_for_complete()
        assert grants == [("evil.example.com", "permanent")]


async def test_allow_host_picker_cancels_without_granting(monkeypatch):
    monkeypatch.setattr(
        tui, "registry", lambda: _panel_registry(_NetworkPanelPlugin(_blocked_tree))
    )
    grants: list = []
    monkeypatch.setattr(tui.allowlist, "grant", lambda host, level: grants.append((host, level)))
    async with tui.DevModeTui().run_test() as pilot:
        await pilot.pause()
        app = cast(tui.DevModeTui, pilot.app)
        app.query_one(tui.TabbedContent).active = "tab-network"
        await pilot.pause()
        tree = app.query_one("#network-tree", tui.Tree)
        tree.focus()
        tree.cursor_line = 0
        await pilot.pause()
        await pilot.press("a")
        await pilot.pause()
        await pilot.press("escape")  # cancel → no grant
        await pilot.pause()
        await app.workers.wait_for_complete()
        assert grants == []


async def test_down_enters_grid_then_arrows_move_between_cells():
    async with tui.DevModeTui().run_test() as pilot:
        await pilot.press("down")  # tab strip → first cell
        assert _fid(pilot.app) == "gcp-off"
        await pilot.press("right")
        assert _fid(pilot.app) == "gcp-logs"
        await pilot.press("down")  # next row (the storage axis now sits under gcp), same column
        assert _fid(pilot.app) == "storage-staging"
        await pilot.press("up")
        assert _fid(pilot.app) == "gcp-logs"


async def test_left_off_left_column_focuses_workspaces():
    async with tui.DevModeTui().run_test() as pilot:
        await pilot.press("down")  # gcp-off
        await pilot.press("left")  # col 0 → workspace list
        assert isinstance(pilot.app.focused, tui.WorkspaceList)


async def test_workspace_refreshes_tolerate_a_detached_list():
    # The per-worktree binding (refresh_mode/refresh_panels/refresh_ws_hint) reads the SELECTED
    # workspace via query_one("#workspaces"). Those run on the async Highlighted handler and the
    # 1s/3s/5s timers, which can fire during startup (before compose mounts the list) or teardown
    # (after it's detached) — on a slow CI runner a stray tick then crashed the app with a
    # NoMatches. Simulate the detached window and assert every path is a no-op, not a crash.
    async with tui.DevModeTui().run_test() as pilot:
        await pilot.pause()
        app = cast(tui.DevModeTui, pilot.app)
        app.query_one("#workspaces", tui.WorkspaceList).remove()
        await pilot.pause()
        assert app.selected_workspace is None
        app.refresh_workspaces()  # each of these queried #workspaces unguarded before the fix
        app.refresh_ws_hint()
        app.refresh_mode()
        app.refresh_panels()


async def test_up_off_top_row_returns_to_tab_strip():
    async with tui.DevModeTui().run_test() as pilot:
        await pilot.press("down")  # gcp-off (top row)
        await pilot.press("up")  # off the top → tab strip
        assert pilot.app.focused is None


async def test_right_edge_stays_put():
    async with tui.DevModeTui().run_test() as pilot:
        await pilot.press("down", "left")  # into grid then to workspaces
        await pilot.press("right")  # workspaces → grid first cell
        await pilot.press("right", "right", "right", "right")  # past the last gcp cell
        assert _fid(pilot.app) == "gcp-user"  # clamped, not wrapped/crashed


async def test_selected_status_describes_focused_mode():
    async with tui.DevModeTui().run_test() as pilot:
        await pilot.press("down", "right")  # focus gcp-logs (not the active mode)
        await pilot.pause()
        row = next(r for r in pilot.app.query(tui.AxisRow) if r.axis == "gcp")
        status = _text(row.query_one(".axis-status", tui.Static))
        assert "logs" in status and ("enter to switch" in status or "active" in status)


async def test_shared_daemon_status_shown_only_when_active_and_down(monkeypatch):
    # Many axes ride the one egress-proxy; repeating its "● up" line under each was noise. An OFF
    # axis shows no daemon line; an ON axis whose daemon is up shows none either (the host-status
    # footer carries the healthy state); only an ON axis whose daemon is DOWN gets a warning.
    def mode_with(**over):
        m = dict(devmode.axis_defaults())  # each axis's own resting rung, not a blanket "off"
        m.update(over)
        return {"mode": m, "expires": {}}

    proxy_up = {"egress-proxy": {"up": True, "label": "x", "port": 8088}}
    proxy_down = {"egress-proxy": {"up": False, "label": "x", "port": 8088}}
    async with tui.DevModeTui().run_test() as pilot:
        app = cast(tui.DevModeTui, pilot.app)
        cap = next(r for r in app.query(tui.AxisRow) if r.axis == "capture")  # → egress-proxy

        def line() -> str:
            return _text(cap.query_one(".axis-status", tui.Static))

        monkeypatch.setattr(devmode, "read", lambda: mode_with())  # capture off
        monkeypatch.setattr(devmode, "daemon_status", lambda mode: proxy_up)
        app.refresh_mode()
        assert "8088" not in line() and "up" not in line()  # off → no daemon line

        monkeypatch.setattr(devmode, "read", lambda: mode_with(capture="on"))
        app.refresh_mode()
        assert "8088" not in line()  # on + up → still no per-axis line (footer covers it)

        monkeypatch.setattr(devmode, "daemon_status", lambda mode: proxy_down)
        app.refresh_mode()
        assert "DOWN" in line() and "8088" in line()  # on + down → actionable warning


async def test_banner_raises_degraded_capability_alongside_emergency(monkeypatch):
    # A probed-and-failing capability raises a banner line — same published claim `fy mode`
    # renders as ⚠ DEGRADED — and STACKS with the emergency-rung banner rather than replacing it.
    monkeypatch.setattr(
        devmode,
        "degraded_capabilities",
        lambda mode=None: [("gcp", "PAM lapsed — just gcp-elevate")],
    )
    async with tui.DevModeTui().run_test() as pilot:
        app = cast(tui.DevModeTui, pilot.app)
        await pilot.pause()
        banner = _text(app.query_one("#banner", tui.Static))
        assert "gcp capability DEGRADED" in banner and "just gcp-elevate" in banner
        assert "EMERGENCY" not in banner

        # gcp=user is the emergency rung — both alarms must show at once.
        monkeypatch.setattr(
            devmode,
            "read",
            lambda: {"mode": {**devmode.axis_defaults(), "gcp": "user"}, "expires": {}},
        )
        app.refresh_mode()
        await pilot.pause()
        banner = _text(app.query_one("#banner", tui.Static))
        assert "EMERGENCY" in banner and "gcp capability DEGRADED" in banner


async def test_banner_clears_when_nothing_is_alarming():
    async with tui.DevModeTui().run_test() as pilot:
        await pilot.pause()
        # Default test world: offline mode, no published capability claims (tmp state file).
        assert _text(pilot.app.query_one("#banner", tui.Static)) == ""


# ── TUI: Mode tab axis grid (scroll region + responsive columns) ──────────────────────


async def test_mode_axes_live_in_their_own_scroll_region():
    # The axes (+ banner/hint/host-status) sit inside #mode-top, a VerticalScroll, so a tall axis
    # list scrolls instead of clipping the last axes or pushing the host-log off the bottom.
    async with tui.DevModeTui().run_test() as pilot:
        await pilot.pause()
        app = pilot.app
        grid = app.query_one("#mode-grid", tui.ModeGrid)
        assert app.query_one("#mode-top #mode-grid", tui.ModeGrid) is grid  # nested in the scroller
        assert isinstance(app.query_one("#mode-top"), tui.VerticalScroll)


@pytest.mark.parametrize("size,expected_cols", [((80, 24), 1), ((200, 40), 2)])
async def test_mode_grid_uses_two_columns_only_when_wide(size, expected_cols):
    # Narrow terminal → one column (so the existing single-column nav holds); wide → two columns to
    # use the horizontal space. The count is pushed to the CSS grid as well as tracked for nav.
    async with tui.DevModeTui().run_test(size=size) as pilot:
        await pilot.pause()
        grid = pilot.app.query_one("#mode-grid", tui.ModeGrid)
        assert grid.cols == expected_cols
        assert grid.styles.grid_size_columns == expected_cols


async def test_mode_grid_arrows_cross_columns_when_two_wide():
    # With two columns, ↓ steps a whole visual row (two axes down in DOM order), and →/← cross
    # between the side-by-side axes (off the left column's left edge still exits to the workspaces).
    axes = list(devmode.axes())
    assert len(axes) >= 3  # the Tangible config has 7 axes; this test needs at least two columns
    async with tui.DevModeTui().run_test(size=(200, 40)) as pilot:
        await pilot.pause()
        app = pilot.app
        assert app.query_one("#mode-grid", tui.ModeGrid).cols == 2
        await pilot.press("down")  # tab strip → first cell
        assert _fid(app) == f"{axes[0]}-off"
        await pilot.press("down")  # one visual row down = two axes on in DOM order
        assert _fid(app) == f"{axes[2]}-{devmode.axes()[axes[2]][0]}"
        # → off the right edge of a left-column axis crosses into its right-column neighbour…
        last0 = devmode.axes()[axes[0]][-1]
        app.query_one(f"#{axes[0]}-{last0}", tui.Button).focus()
        await pilot.press("right")
        assert _fid(app) == f"{axes[1]}-{devmode.axes()[axes[1]][0]}"
        # …and ← off its left edge lands back on the left-column axis's last button (not workspaces)
        await pilot.press("left")
        assert _fid(app) == f"{axes[0]}-{last0}"


# ── TUI: responsive layout (detail pane fills, never overflows) ───────────────────────


@pytest.mark.parametrize("size", [(70, 24), (99, 26), (140, 30), (200, 40)])
async def test_detail_pane_fills_width_without_overflow(size):
    # Regression: TabbedContent had no explicit width, so it resolved to `auto` (its widest
    # content) and overran the right edge — its descendants then wrapped to an off-screen
    # width and looked truncated. It must fill exactly the columns left of the fixed-width
    # workspace list, at every terminal size, so wrapped text stays on screen.
    w, _ = size
    async with tui.DevModeTui().run_test(size=size) as pilot:
        await pilot.pause()
        ws = pilot.app.query_one("#workspaces", tui.WorkspaceList).region
        tabs = pilot.app.query_one(tui.TabbedContent).region
        # the pane starts where the list ends and stops exactly at the right edge — no overrun
        assert tabs.x == ws.right
        assert tabs.right == w
        # the long host-log placeholder (the text that overflowed in the wild) stays in bounds
        log = pilot.app.query_one("#host-log", tui.Static).region
        assert log.right <= w


# ── TUI: doctor rendering ───────────────────────────────────────────────────────────


async def test_doctor_render_survives_ansi_and_markup_in_detail(monkeypatch):
    monkeypatch.setattr(devmode, "doctor", _fixed_doctor)
    async with tui.DevModeTui().run_test() as pilot:
        # the startup deep run drives the (stubbed) doctor — there's no d/D shortcut anymore
        await pilot.pause(0.1)
        # No MarkupError raised, and the adversarial detail reached the row data and the
        # rendered Static (which the old markup-string render choked on).
        rows = cast(tui.DevModeTui, pilot.app)._doctor_rows
        assert any("[/red]" in detail for _, _, detail in rows)
        assert any(status == "ok" for status, _, _ in rows)
        out = _text(pilot.app.query_one("#doctor-out", tui.Static))
        assert "gcloud CLI" in out and "denied" in out


async def test_doctor_log_pane_shows_background_commands(monkeypatch):
    # The box doctor runs `<engine> info`; it should land in the Doctor log pane. Pin in_box
    # so we exercise that branch regardless of where the test runs (on the box DOCKER_HOST
    # makes it true; a plain CI runner would otherwise take the Mac-side gcloud/gh path and
    # never log "info"). Poll instead of a fixed pause — the background command can take a
    # beat on a slow CI runner. The startup deep run is the trigger (no d/D shortcut anymore;
    # the in-box doctor runs `<engine> info` in both fast and deep passes).
    monkeypatch.setattr(devmode, "in_box", lambda: True)
    async with tui.DevModeTui().run_test() as pilot:
        log = ""
        for _ in range(60):  # up to ~6s
            await pilot.pause(0.1)
            log = _text(pilot.app.query_one("#doctor-log", tui.Static))
            if "$" in log and "info" in log:
                break
        assert "$" in log and "info" in log


def test_render_doctor_never_raises_on_adversarial_details():
    # Direct unit check of the renderer against strings that defeat markup escaping.
    evil = ["\x1b[31m red \x1b[0m", "[/cyan] stray", "a \\[b] c", "[123] [foo=bar]", ""]
    app = tui.DevModeTui()
    app._spin = 0
    app._doctor_header = "deep"
    glyphs = ("ok", "warn", "fail", "running")
    app._doctor_rows = [(glyphs[i % 4], f"check {i}", d) for i, d in enumerate(evil)]
    text = tui._doctor_text(app._doctor_header, app._doctor_rows, app._spin)
    rendered = str(text)
    assert "check 0" in rendered  # built a Text without raising
    assert "\x1b" not in rendered  # ANSI stripped even if it reaches the renderer


# ── TUI: new-worktree picker ────────────────────────────────────────────────────────


async def test_workspace_actions_fire_on_highlighted_card(monkeypatch):
    browsed, coded, toggled = [], [], []
    monkeypatch.setattr(devmode, "workspaces", _two_workspaces)
    monkeypatch.setattr(devmode, "open_browser", lambda name: browsed.append(name) or (0, ""))
    monkeypatch.setattr(devmode, "open_code", lambda name: coded.append(name) or (0, ""))
    monkeypatch.setattr(devmode, "machine_toggle", lambda: toggled.append(True) or (0, "started"))
    # stopped → `s` starts the machine directly (only the STOP direction confirms first)
    monkeypatch.setattr(devmode, "machine_state", lambda: "stopped")
    async with tui.DevModeTui().run_test() as pilot:
        await pilot.pause()
        lst = pilot.app.query_one("#workspaces", tui.WorkspaceList)
        lst.focus()
        lst.index = 0  # the 'main' workspace card
        await pilot.pause()
        await pilot.press("b")
        await pilot.press("c")
        await pilot.press("s")
        await pilot.pause(0.2)
        assert browsed == ["main"] and coded == ["main"] and toggled == [True]


async def test_open_code_hidden_on_new_worktree_row():
    async with tui.DevModeTui().run_test() as pilot:
        await pilot.pause()
        lst = pilot.app.query_one("#workspaces", tui.WorkspaceList)
        lst.focus()
        lst.index = len(list(lst.children)) - 1  # the ＋ new worktree row
        await pilot.pause()
        # check_action hides the per-workspace actions there, but the machine toggle
        # (global) stays available
        assert lst.check_action("open_browser", ()) is False
        assert lst.check_action("open_code", ()) is False
        assert lst.check_action("toggle_devbox", ()) is False
        assert lst.check_action("toggle_machine", ()) is True


def test_workspace_card_shows_app_port():
    rendered = tui._render_ws(_two_workspaces()[1])
    assert "http://localhost:3007" in rendered


async def test_new_worktree_card_is_last_and_opens_modal():
    async with tui.DevModeTui().run_test() as pilot:
        await pilot.pause()
        lv = pilot.app.query_one("#workspaces", tui.WorkspaceList)
        assert isinstance(list(lv.children)[-1], tui.NewWorktreeCard)


async def test_worktree_picker_filters_and_flags_new(monkeypatch):
    monkeypatch.setattr(devmode, "branches", lambda: ["main", "cross-service-e2e", "feature/x"])
    chosen: list = []
    async with tui.DevModeTui().run_test() as pilot:
        screen = tui.NewWorktreeScreen()
        pilot.app.push_screen(screen, lambda r: chosen.append(r))
        await pilot.pause()

        # type a substring → only matching branches listed
        for ch in "cross":
            await pilot.press(ch)
        await pilot.pause()
        items = list(screen.query_one("#wt-list", tui.ListView).children)
        assert len(items) == 1 and getattr(items[0], "branch", None) == "cross-service-e2e"
        assert "enter checks out" in _text(screen.query_one("#wt-hint", tui.Static))

        # enter checks out the highlighted existing branch (name = sanitised branch)
        await pilot.press("enter")
        await pilot.pause()
        assert chosen[-1] == ("cross-service-e2e", "cross-service-e2e")


async def test_worktree_picker_creates_new_branch_when_no_match(monkeypatch):
    monkeypatch.setattr(devmode, "branches", lambda: ["main", "feature/x"])
    chosen: list = []
    async with tui.DevModeTui().run_test() as pilot:
        screen = tui.NewWorktreeScreen()
        pilot.app.push_screen(screen, lambda r: chosen.append(r))
        await pilot.pause()
        for ch in "shiny/thing":
            await pilot.press(ch)
        await pilot.pause()
        assert "creates new branch" in _text(screen.query_one("#wt-hint", tui.Static))
        await pilot.press("enter")
        await pilot.pause()
        # slashes in the branch become dashes in the worktree dir name
        assert chosen[-1] == ("shiny-thing", "shiny/thing")


async def test_worktree_picker_cancel_returns_none(monkeypatch):
    monkeypatch.setattr(devmode, "branches", lambda: ["main"])
    chosen: list = []
    async with tui.DevModeTui().run_test() as pilot:
        pilot.app.push_screen(tui.NewWorktreeScreen(), lambda r: chosen.append(r))
        await pilot.pause()
        await pilot.press("escape")
        await pilot.pause()
        assert chosen[-1] is None


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v", "-o", "asyncio_mode=auto", *sys.argv[1:]]))


# ── TUI: follow the terminal's system light/dark theme (OSC 11) ────────────────────────


@pytest.mark.parametrize(
    "reply,expected",
    [
        ("\x1b]11;rgb:0000/0000/0000\x1b\\", "textual-dark"),  # black bg → dark
        ("\x1b]11;rgb:ffff/ffff/ffff\x1b\\", "textual-light"),  # white bg → light
        ("\x1b]11;rgb:1e/1e/2e\x07", "textual-dark"),  # short hex, dim bg (BEL-terminated) → dark
        ("no osc reply here", None),  # unparseable → keep the default
    ],
)
def test_theme_for_osc11_maps_background_luminance(reply, expected):
    assert tui._theme_for_osc11(reply) == expected


def test_start_theme_is_adopted_on_mount():
    # The detected terminal theme is applied on mount (None ⇒ Textual's default is kept).
    assert tui.DevModeTui(start_theme=None)._start_theme is None
    assert tui.DevModeTui(start_theme="textual-light")._start_theme == "textual-light"


# ── TUI: doctor triggers (startup deep run + the tab's refresh button; no shortcuts) ────


async def test_doctor_shortcuts_are_gone_and_refresh_button_reruns_deep(monkeypatch):
    # The d/D app shortcuts are removed (d now belongs to the workspace list's dev-box toggle);
    # the Doctor tab's own ↻ deep refresh button is the manual trigger, always visible, running
    # the DEEP pass. The startup run still happens (that's runs[0]).
    runs: list[bool] = []

    def counting_doctor(deep=False):
        runs.append(deep)
        yield ("ok", "a check", "fine")

    monkeypatch.setattr(devmode, "doctor", counting_doctor)
    async with tui.DevModeTui().run_test() as pilot:
        await pilot.pause()
        app = cast(tui.DevModeTui, pilot.app)
        keys = {b.key if isinstance(b, tui.Binding) else b[0] for b in app.BINDINGS}
        assert "d" not in keys and "D" not in keys and "o" not in keys
        for _ in range(40):  # let the startup run land
            await pilot.pause(0.05)
            if runs:
                break
        assert runs == [True]
        btn = app.query_one("#doctor-refresh", tui.Button)
        assert btn.display  # unlike the fix buttons, never hidden
        app.on_button_pressed(tui.Button.Pressed(btn))
        for _ in range(40):
            await pilot.pause(0.05)
            if len(runs) == 2:
                break
        assert runs == [True, True]  # the button re-ran the deep pass


async def test_all_off_shortcut_is_gone(monkeypatch):
    # `o` used to reset every axis to its default — which would drop claude/codex keyless auth
    # out from under a live agent session. The binding AND the action are gone: pressing o on
    # the tab strip must write no mode at all.
    mode_sets: list[dict] = []
    monkeypatch.setattr(devmode, "set_mode", lambda updates, **kw: mode_sets.append(updates) or {})
    async with tui.DevModeTui().run_test() as pilot:
        await pilot.pause()
        app = cast(tui.DevModeTui, pilot.app)
        assert not hasattr(app, "action_all_off")
        await pilot.press("o")
        await app.workers.wait_for_complete()
        assert mode_sets == []


# ── TUI: Doctor-tab one-click fixes ────────────────────────────────────────────────────


async def test_doctor_fix_button_shows_for_failing_check_and_runs(monkeypatch):
    # A doctor that fails the `mitmproxy` check (the proxy plugin contributes a fix for it).
    def failing_doctor(deep=False):
        yield ("fail", "mitmproxy", "missing — reinstall foldyard")

    ran: dict[str, list[str]] = {}

    def fake_run_stream(cmd, on_line, timeout=600.0):
        ran["cmd"] = cmd
        on_line("reinstalled foldyard")
        return 0

    monkeypatch.setattr(devmode, "doctor", failing_doctor)
    monkeypatch.setattr(devmode, "run_stream", fake_run_stream)
    async with tui.DevModeTui().run_test() as pilot:
        app = cast(tui.DevModeTui, pilot.app)
        bid = next(b for b, f in app._fix_buttons.items() if f.check == "mitmproxy")
        # the startup (deep) doctor failed `mitmproxy` → its fix button becomes visible
        for _ in range(60):
            await pilot.pause(0.05)
            if app.query_one(f"#{bid}", tui.Button).display:
                break
        assert app.query_one(f"#{bid}", tui.Button).display is True
        # run the fix → run_stream is invoked with the install cmd; its output streams to the log
        app._run_fix(app._fix_buttons[bid])
        log = ""
        for _ in range(60):
            await pilot.pause(0.05)
            log = _text(app.query_one("#doctor-log", tui.Static))
            if "reinstalled foldyard" in log:
                break
        assert ran["cmd"][:3] == ["uv", "tool", "install"]
        assert "reinstall foldyard" in log and "reinstalled foldyard" in log


async def test_doctor_fix_button_hidden_when_check_passes(monkeypatch):
    # No failing check that a fix targets ⇒ every fix button stays hidden.
    def passing_doctor(deep=False):
        yield ("ok", "mitmproxy", "installed")

    monkeypatch.setattr(devmode, "doctor", passing_doctor)
    async with tui.DevModeTui().run_test() as pilot:
        app = cast(tui.DevModeTui, pilot.app)
        for _ in range(40):
            await pilot.pause(0.05)
        assert all(not app.query_one(f"#{bid}", tui.Button).display for bid in app._fix_buttons)


async def test_doctor_fix_button_shows_for_warn_not_just_fail(monkeypatch):
    # A missing mitm CA is a WARN (○), not a fail — its 'generate CA' button must still appear.
    def warn_doctor(deep=False):
        yield ("warn", "mitm CA", "not generated yet — first `fy host` run creates it")

    monkeypatch.setattr(devmode, "doctor", warn_doctor)
    async with tui.DevModeTui().run_test() as pilot:
        app = cast(tui.DevModeTui, pilot.app)
        bid = next(b for b, f in app._fix_buttons.items() if f.check == "mitm CA")
        shown = False
        for _ in range(60):
            await pilot.pause(0.05)
            if app.query_one(f"#{bid}", tui.Button).display:
                shown = True
                break
        assert shown, "the 'generate CA' button should show for a warn-status mitm CA check"


# ── TUI: host-daemon supervisor (status + log tail; NOT owned by the TUI) ──────────────


async def test_host_status_reflects_daemon_liveness(monkeypatch):
    # The TUI doesn't own the supervisor (it's launched by `fy up`) — it just reports whether
    # the daemons are up. No start/stop button exists anymore.
    monkeypatch.setattr(devmode, "daemon_status", lambda mode: {})
    async with tui.DevModeTui().run_test() as pilot:
        app = cast(tui.DevModeTui, pilot.app)
        assert not app.query("#host-toggle")  # the start/stop button is gone
        app._render_host_status()
        # Per-workspace wording now: "none up" when this worktree's daemons are down.
        assert "none up" in _text(app.query_one("#host-status", tui.Static))

        monkeypatch.setattr(
            devmode,
            "daemon_status",
            lambda mode: {"egress-proxy": {"up": True, "label": "egress proxy", "port": 8088}},
        )
        app._render_host_status()
        status = _text(app.query_one("#host-status", tui.Static))
        assert "● up" in status
        # the daemon detail (port + which injectors ride it) lives here ONCE, not under every axis
        assert "egress proxy :8088" in status and "● up" in status


async def test_host_log_pane_tails_the_supervisor_logfile(monkeypatch, tmp_path):
    # The pane tails host-supervisor.log (which the supervisor always writes) — so mitmproxy's
    # output is visible however the supervisor was started, not just when the TUI spawned it. It's
    # the PROJECT-level supervisor log (state_dir), not the per-worktree log_dir.
    monkeypatch.setattr(devmode, "daemon_status", lambda mode: {})
    monkeypatch.setattr(tui.config, "supervisor_log_file", lambda: tmp_path / "host-supervisor.log")
    (tmp_path / "host-supervisor.log").write_text("[host] started egress-proxy (pid 5): capture\n")
    async with tui.DevModeTui().run_test() as pilot:
        app = cast(tui.DevModeTui, pilot.app)
        app._render_host_log()
        assert "started egress-proxy" in _text(app.query_one("#host-log", tui.Static))


async def test_host_log_pane_follows_tail_only_when_at_bottom(monkeypatch, tmp_path):
    # The pane should pin to the newest line on refresh ONLY while the user is already at the
    # bottom. If they've scrolled up to read earlier lines, a refresh must leave them put — an
    # unconditional scroll_end on every tick made scrollback impossible.
    monkeypatch.setattr(devmode, "daemon_status", lambda mode: {})
    monkeypatch.setattr(tui.config, "supervisor_log_file", lambda: tmp_path / "host-supervisor.log")
    log = tmp_path / "host-supervisor.log"
    log.write_text("".join(f"line {i}\n" for i in range(200)))
    async with tui.DevModeTui().run_test() as pilot:
        app = cast(tui.DevModeTui, pilot.app)
        app._render_host_log()
        await pilot.pause()
        scroll = app.query_one("#host-log-wrap", tui.VerticalScroll)
        assert scroll.scroll_offset.y >= scroll.max_scroll_y - 1  # starts pinned to the bottom

        # User scrolls to the top, then more lines arrive and the pane refreshes.
        scroll.scroll_to(y=0, animate=False)
        await pilot.pause()
        log.write_text("".join(f"line {i}\n" for i in range(260)))
        app._render_host_log()
        await pilot.pause()
        assert scroll.scroll_offset.y == 0  # left where the user parked it — no jump to the end


async def test_host_log_pane_handles_a_missing_logfile(monkeypatch, tmp_path):
    monkeypatch.setattr(devmode, "daemon_status", lambda mode: {})
    # no host-supervisor.log present at the project-level path
    monkeypatch.setattr(tui.config, "supervisor_log_file", lambda: tmp_path / "host-supervisor.log")
    async with tui.DevModeTui().run_test() as pilot:
        app = cast(tui.DevModeTui, pilot.app)
        app._render_host_log()
        assert "no supervisor log yet" in _text(app.query_one("#host-log", tui.Static))


# ── the Network Log's wall pane (grants + pending `[proxy] recommend` offers) ─────────


def _stub_wall(monkeypatch, pending=(), granted=(), enforcing=True):
    monkeypatch.setattr(tui.allowlist, "pending_recommendations", lambda: list(pending))
    monkeypatch.setattr(tui.allowlist, "grants", lambda: list(granted))
    monkeypatch.setattr(tui.allowlist, "default_deny", lambda: enforcing)


async def test_wall_pane_lists_pending_offers_before_grants(monkeypatch):
    monkeypatch.setattr(
        tui, "registry", lambda: _panel_registry(_NetworkPanelPlugin(_blocked_tree))
    )
    _stub_wall(
        monkeypatch,
        pending=[{"host": "unpkg.com", "why": "vis-timeline for browser tests"}],
        granted=[{"host": "pypi.org", "level": "permanent", "expires": None}],
    )
    async with tui.DevModeTui().run_test() as pilot:
        await pilot.pause()
        app = cast(tui.DevModeTui, pilot.app)
        app.query_one(tui.TabbedContent).active = "tab-network"
        await pilot.pause()
        app.refresh_panels()
        table = app.query_one("#network-manage", tui.DataTable)
        assert table.row_count == 2
        assert app._manage_keys == [("rec", "unpkg.com"), ("grant", "pypi.org")]
        summary = _text(app.query_one("#network-manage-summary", tui.Static))
        assert "1 recommended pending" in summary and "1 granted" in summary


async def test_wall_pane_accepts_a_recommendation_via_the_level_picker(monkeypatch):
    monkeypatch.setattr(
        tui, "registry", lambda: _panel_registry(_NetworkPanelPlugin(_blocked_tree))
    )
    _stub_wall(monkeypatch, pending=[{"host": "unpkg.com", "why": "vis-timeline"}])
    grants: list[tuple[str, str]] = []
    monkeypatch.setattr(tui.allowlist, "grant", lambda host, level: grants.append((host, level)))
    async with tui.DevModeTui().run_test() as pilot:
        await pilot.pause()
        app = cast(tui.DevModeTui, pilot.app)
        app.query_one(tui.TabbedContent).active = "tab-network"
        await pilot.pause()
        app.refresh_panels()
        app.query_one("#network-manage", tui.DataTable).focus()
        await pilot.pause()
        await pilot.press("a")  # on the wall pane: acts on the SELECTED row, not the tree
        await pilot.pause()
        assert isinstance(app.screen, tui.AllowHostScreen)
        await pilot.press("p")
        await pilot.pause()
        await app.workers.wait_for_complete()
        assert grants == [("unpkg.com", "permanent")]


async def test_wall_pane_x_declines_a_rec_and_revokes_a_grant(monkeypatch):
    monkeypatch.setattr(
        tui, "registry", lambda: _panel_registry(_NetworkPanelPlugin(_blocked_tree))
    )
    _stub_wall(
        monkeypatch,
        pending=[{"host": "unpkg.com", "why": ""}],
        granted=[{"host": "pypi.org", "level": "permanent", "expires": None}],
    )
    declined: list[str] = []
    revoked: list[str] = []
    monkeypatch.setattr(tui.allowlist, "decline", declined.append)
    monkeypatch.setattr(tui.allowlist, "revoke", revoked.append)
    async with tui.DevModeTui().run_test() as pilot:
        await pilot.pause()
        app = cast(tui.DevModeTui, pilot.app)
        app.query_one(tui.TabbedContent).active = "tab-network"
        await pilot.pause()
        app.refresh_panels()
        table = app.query_one("#network-manage", tui.DataTable)
        table.focus()
        await pilot.pause()
        await pilot.press("x")  # row 0: the pending recommendation → decline
        await app.workers.wait_for_complete()
        table.move_cursor(row=1)
        await pilot.press("x")  # row 1: the grant → revoke
        await app.workers.wait_for_complete()
        assert declined == ["unpkg.com"] and revoked == ["pypi.org"]


async def test_x_is_gated_to_the_network_tab(monkeypatch):
    monkeypatch.setattr(
        tui, "registry", lambda: _panel_registry(_NetworkPanelPlugin(_blocked_tree))
    )
    _stub_wall(monkeypatch)
    async with tui.DevModeTui().run_test() as pilot:
        await pilot.pause()
        app = cast(tui.DevModeTui, pilot.app)
        assert app.check_action("wall_answer", ()) is False
        app.query_one(tui.TabbedContent).active = "tab-network"
        await pilot.pause()
        assert app.check_action("wall_answer", ()) is True


class _UnadoptedDrift(_CleanDrift):
    pinned_exists = False
    adopted = False
    label = "worktree feat"
    worktree = "feat"

    def diff(self) -> str:
        return ""


async def test_unadopted_checkout_asks_in_a_modal_instead_of_hanging(monkeypatch):
    """`fy box up` inherits configpin's adopt gate, which prompts on a terminal. Under Textual
    stdin is still a TTY but nobody can answer it, so the prompt used to wedge the box start on
    any fresh worktree. Ask it here, where it CAN be answered — and don't start the box until it
    is, since the gate would only refuse."""
    ups, adopted = [], []
    monkeypatch.setattr(devmode, "workspaces", _two_workspaces)
    monkeypatch.setattr(devmode, "box_up", lambda name: ups.append(name) or (0, "box up"))
    monkeypatch.setattr(tui.configpin, "inspect", lambda cfg: _UnadoptedDrift())
    monkeypatch.setattr(
        tui.configpin, "adopt", lambda cfg, **_kw: adopted.append(cfg) or _CleanDrift()
    )

    async with tui.DevModeTui().run_test() as pilot:
        await pilot.pause()
        app = cast(tui.DevModeTui, pilot.app)
        lv = app.query_one("#workspaces", tui.WorkspaceList)
        lv.focus()
        lv.index = 1
        await pilot.pause()
        await pilot.press("d")
        await pilot.pause()

        assert isinstance(app.screen, tui.AdoptConfigScreen)
        assert ups == []  # nothing started while the question is open

        await pilot.press("a")
        await pilot.pause()
        assert len(adopted) == 1
        assert ups == ["feat"]


async def test_the_adopt_modal_reopens_when_the_tree_changed_while_it_was_open(monkeypatch):
    """Pressing `a` adopts what the modal SHOWED, not whatever the checkout holds by then. When
    `configpin.adopt` refuses because the tree moved, the box must not start and the question is
    asked again over the new tree."""
    ups: list[str] = []
    monkeypatch.setattr(devmode, "workspaces", _two_workspaces)
    monkeypatch.setattr(devmode, "box_up", lambda name: ups.append(name) or (0, "box up"))
    monkeypatch.setattr(tui.configpin, "inspect", lambda cfg: _UnadoptedDrift())

    def _stale(cfg, **_kw):
        raise configpin.ReviewStale("foldyard.toml changed since it was reviewed")

    monkeypatch.setattr(tui.configpin, "adopt", _stale)

    async with tui.DevModeTui().run_test() as pilot:
        await pilot.pause()
        app = cast(tui.DevModeTui, pilot.app)
        lv = app.query_one("#workspaces", tui.WorkspaceList)
        lv.focus()
        lv.index = 1
        await pilot.pause()
        await pilot.press("d")
        await pilot.pause()
        assert isinstance(app.screen, tui.AdoptConfigScreen)

        await pilot.press("a")
        await pilot.pause()
        assert isinstance(app.screen, tui.AdoptConfigScreen)  # asked again, over the new tree
        assert ups == []


async def test_the_adopt_modal_shows_what_the_branch_changes_about_mains_config(monkeypatch):
    """The modal asks for a security decision, and on a first adoption it had nothing to show but
    a digest — while pointing at `fy config diff`, which refuses on an unadopted checkout. Show
    the diff against the config the host already runs for main, right where the answer is given."""
    base = configpin.Baseline(
        digest="deadbeefdeadbeef",
        body='--- main-adopted/foldyard.toml\n@@ [proxy] @@\n+passthrough = ["evil.example"]',
        identical=False,
    )
    ups, adopted = [], []
    monkeypatch.setattr(devmode, "workspaces", _two_workspaces)
    monkeypatch.setattr(devmode, "box_up", lambda name: ups.append(name) or (0, "box up"))
    monkeypatch.setattr(tui.configpin, "inspect", lambda cfg: _UnadoptedDrift())
    monkeypatch.setattr(
        tui.configpin, "adopt", lambda cfg, **_kw: adopted.append(cfg) or _CleanDrift()
    )
    monkeypatch.setattr(tui.configpin, "main_baseline", lambda drift: base)

    async with tui.DevModeTui().run_test() as pilot:
        await pilot.pause()
        app = cast(tui.DevModeTui, pilot.app)
        lv = app.query_one("#workspaces", tui.WorkspaceList)
        lv.focus()
        lv.index = 1
        await pilot.pause()
        await pilot.press("d")
        await pilot.pause()

        assert isinstance(app.screen, tui.AdoptConfigScreen)
        shown = "\n".join(_text(w) for w in app.screen.query(tui.Static))
        assert "the config the host runs for main" in shown
        assert 'passthrough = ["evil.example"]' in shown

        # The diff sits in a scroll pane, which takes focus (so ↑/↓ review a long branch) — the
        # answer keys still have to reach the screen's bindings past it.
        assert app.focused is app.screen.query_one("#adopt-diff")
        await pilot.press("a")
        await pilot.pause()
        assert len(adopted) == 1 and ups == ["feat"]


async def test_the_adopt_modals_diff_is_focused_so_a_long_one_can_be_scrolled(monkeypatch):
    """`DevModeTui` sets AUTO_FOCUS = None so the arrows drive its tab strip, which means a modal
    focuses something explicitly or nothing is focused at all. This one showed the config decision
    in a scroll pane and focused nothing, so ↑/↓/PageDown went nowhere and only the first
    screenful was reachable — on the one screen whose content has to be read in full before it's
    answered, and where a first adoption with no baseline renders the WHOLE config."""
    long_diff = "--- adopted/foldyard.toml\n+++ tree/foldyard.toml\n" + "\n".join(
        f'+host = "h{i}.example"' for i in range(200)
    )

    class _LongDrift(_UnadoptedDrift):
        label = "main"
        worktree = ""

        def diff(self) -> str:
            return long_diff

    monkeypatch.setattr(devmode, "workspaces", _two_workspaces)
    monkeypatch.setattr(devmode, "box_up", lambda name: (0, "box up"))
    monkeypatch.setattr(tui.configpin, "inspect", lambda cfg: _LongDrift())
    monkeypatch.setattr(tui.configpin, "main_baseline", lambda drift: None)

    async with tui.DevModeTui().run_test() as pilot:
        await pilot.pause()
        app = cast(tui.DevModeTui, pilot.app)
        lv = app.query_one("#workspaces", tui.WorkspaceList)
        lv.focus()
        lv.index = 1
        await pilot.pause()
        await pilot.press("d")
        await pilot.pause()

        assert isinstance(app.screen, tui.AdoptConfigScreen)
        pane = app.screen.query_one("#adopt-diff")
        assert app.focused is pane, "the diff pane must hold focus or the diff can't be scrolled"

        # And focus is worth something: the pane really does scroll off its first screenful.
        assert pane.max_scroll_y > 0, "expected the 200-line diff to overflow the modal"
        await pilot.press("pagedown")
        await pilot.pause()
        assert pane.scroll_target_y > 0


async def test_the_adopt_modal_still_answers_when_there_is_no_diff_to_focus(monkeypatch):
    """The no-diff path is unchanged: a worktree byte-identical to main's adopted copy renders no
    scroll pane at all, so there is nothing to focus — and a/i/esc are screen-level bindings that
    must keep working with focus nowhere."""
    ups, adopted = [], []
    monkeypatch.setattr(devmode, "workspaces", _two_workspaces)
    monkeypatch.setattr(devmode, "box_up", lambda name: ups.append(name) or (0, "box up"))
    monkeypatch.setattr(tui.configpin, "inspect", lambda cfg: _UnadoptedDrift())  # diff() == ""
    monkeypatch.setattr(
        tui.configpin, "adopt", lambda cfg, **_kw: adopted.append(cfg) or _CleanDrift()
    )
    monkeypatch.setattr(tui.configpin, "main_baseline", lambda drift: None)

    async with tui.DevModeTui().run_test() as pilot:
        await pilot.pause()
        app = cast(tui.DevModeTui, pilot.app)
        lv = app.query_one("#workspaces", tui.WorkspaceList)
        lv.focus()
        lv.index = 1
        await pilot.pause()
        await pilot.press("d")
        await pilot.pause()

        assert isinstance(app.screen, tui.AdoptConfigScreen)
        assert not app.screen.query("#adopt-diff")
        await pilot.press("a")
        await pilot.pause()
        assert len(adopted) == 1 and ups == ["feat"]


async def test_the_adopt_modal_lists_the_config_when_there_is_no_baseline(monkeypatch):
    """A brand-new project has nothing adopted anywhere, so the modal falls back to the same
    commentless listing the terminal gate prints — never to a digest on its own."""

    class _NoBaseline(_UnadoptedDrift):
        label = "main"
        worktree = ""

        def diff(self) -> str:
            return "--- adopted/foldyard.toml  (absent)\n+++ tree/foldyard.toml\n+[proxy]"

    monkeypatch.setattr(devmode, "workspaces", _two_workspaces)
    monkeypatch.setattr(devmode, "box_up", lambda name: (0, "box up"))
    monkeypatch.setattr(tui.configpin, "inspect", lambda cfg: _NoBaseline())
    monkeypatch.setattr(tui.configpin, "main_baseline", lambda drift: None)

    async with tui.DevModeTui().run_test() as pilot:
        await pilot.pause()
        app = cast(tui.DevModeTui, pilot.app)
        lv = app.query_one("#workspaces", tui.WorkspaceList)
        lv.focus()
        lv.index = 1
        await pilot.pause()
        await pilot.press("d")
        await pilot.pause()

        shown = "\n".join(_text(w) for w in app.screen.query(tui.Static))
        assert "Everything it declares is below" in shown
        assert "+[proxy]" in shown


async def test_the_adopt_modal_survives_a_baseline_it_cannot_compute(monkeypatch):
    """Same rule as the gate's: the comparison decorates the decision, so its failure must leave
    the question askable rather than take the modal down with it."""
    monkeypatch.setattr(devmode, "workspaces", _two_workspaces)
    monkeypatch.setattr(devmode, "box_up", lambda name: (0, "box up"))
    monkeypatch.setattr(tui.configpin, "inspect", lambda cfg: _UnadoptedDrift())
    monkeypatch.setattr(
        tui.configpin,
        "main_baseline",
        lambda drift: (_ for _ in ()).throw(RuntimeError("no main here")),
    )

    async with tui.DevModeTui().run_test() as pilot:
        await pilot.pause()
        app = cast(tui.DevModeTui, pilot.app)
        lv = app.query_one("#workspaces", tui.WorkspaceList)
        lv.focus()
        lv.index = 1
        await pilot.pause()
        await pilot.press("d")
        await pilot.pause()

        assert isinstance(app.screen, tui.AdoptConfigScreen)


async def test_declining_adoption_does_not_start_the_box(monkeypatch):
    """Ignoring is a real answer: the host still has nothing adopted, so a box started now would
    hit the gate's refusal anyway. Better to leave it down than to fail halfway up."""
    ups, adopted = [], []
    monkeypatch.setattr(devmode, "workspaces", _two_workspaces)
    monkeypatch.setattr(devmode, "box_up", lambda name: ups.append(name) or (0, "box up"))
    monkeypatch.setattr(tui.configpin, "inspect", lambda cfg: _UnadoptedDrift())
    monkeypatch.setattr(
        tui.configpin, "adopt", lambda cfg, **_kw: adopted.append(cfg) or _CleanDrift()
    )

    async with tui.DevModeTui().run_test() as pilot:
        await pilot.pause()
        app = cast(tui.DevModeTui, pilot.app)
        lv = app.query_one("#workspaces", tui.WorkspaceList)
        lv.focus()
        lv.index = 1
        await pilot.pause()
        await pilot.press("d")
        await pilot.pause()
        await pilot.press("i")
        await pilot.pause()

    assert adopted == [] and ups == []


# ── the 1s refresh timer vs a half-mounted / half-detached wall pane ──────────


async def test_refresh_panels_survives_a_partly_detached_wall_pane(monkeypatch):
    """`refresh_panels` runs off a 1s timer, so it can fire while the wall pane is only PARTLY in
    the DOM — teardown detaches widgets one at a time, and compose mounts them one at a time.

    `_refresh_manage_panel` guarded on `#network-manage` and then queried a DIFFERENT widget,
    `#network-manage-summary`, unguarded. A tick landing in that window raised NoMatches out of
    the timer callback, which Textual re-raises from `run_test` — so it killed whichever test's
    app happened to be alive at that moment, NOT a specific test. Observed in the wild as
    `test_right_cycles_tabs_left_steps_out_to_list` and `test_allow_host_grants_the_picked_level`
    failing on different runs of the same suite, roughly 1 run in 20 under `-n 16`.

    The call sits OUTSIDE the `except NoMatches: continue` that guards the panel loop just below
    it, which is why the loop's own protection didn't cover it."""
    monkeypatch.setattr(
        tui, "registry", lambda: _panel_registry(_NetworkPanelPlugin(_blocked_tree))
    )
    async with tui.DevModeTui().run_test() as pilot:
        await pilot.pause()
        app = cast(tui.DevModeTui, pilot.app)
        await app.query_one("#network-manage-summary", tui.Static).remove()
        await pilot.pause()
        # The guard's OWN widget is still mounted — that is the whole point: the pane looks
        # present, so the early-out doesn't fire, and the next line used to explode.
        assert app.query("#network-manage")
        app.refresh_panels()
