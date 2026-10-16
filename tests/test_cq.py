"""cq (mozilla-ai/cq) for Claude and Codex in the box: the config surface, the plugin's box wiring
and keyless credential, the session hooks' handler, and the launchers' end-of-session reflect."""

from __future__ import annotations

import io
import json
import shutil
import subprocess
import tomllib
from pathlib import Path

import pytest
from typer.testing import CliRunner

from foldyard import box, cli, config, cqhook, keyless
from foldyard.plugins import Registry, cq

_ENV = {"FY_BOX_HOME": "/home/vscode"}


@pytest.fixture
def agents(monkeypatch):
    """Pin which agents cq is wired into."""

    def set_agents(*names: str) -> cq.CqPlugin:
        monkeypatch.setattr(config, "cq_agents", lambda: tuple(names))
        return cq.CqPlugin()

    return set_agents


# ── config ───────────────────────────────────────────────────────────────────────────────


def test_cq_is_a_boolean_opt_in_per_agent(fresh_config, tmp_path):
    cases = {
        "[claude]\ncq = true\n": ("claude",),
        "[codex]\ncq = true\n": ("codex",),
        "[claude]\ncq = true\n[codex]\ncq = true\n": ("claude", "codex"),
        '[claude]\ncq = "yes"\n': (),  # only a real boolean
        "[claude]\ncq = false\n": (),
        "[claude]\n": (),
        "": (),
    }
    for body, expected in cases.items():
        (tmp_path / "foldyard.toml").write_text(f'[project]\nname = "x"\n{body}')
        fresh_config(FOLDYARD_REPO=tmp_path)
        assert config.cq_agents() == expected, body


# ── the plugin: credential + box wiring ─────────────────────────────────────────────────


def test_inert_unless_an_agent_opts_in(agents):
    p = agents()
    assert p.switches() == [] and p.proxy_rules({"cq": "on"}) == []
    assert p.box_args(dict(_ENV)) == [] and p.box_bootstrap(dict(_ENV)) == []
    assert p.egress_recommend() == [] and p.secrets({"cq": "on"}) == []


def test_axis_on_injects_the_key_on_the_api_host_only(agents):
    p = agents("claude")
    (switch,) = p.switches()
    assert switch.name == "cq" and switch.levels == ("off", "on")
    assert switch.daemon == "egress-proxy"
    assert p.proxy_rules({"cq": "off"}) == []
    (rule,) = p.proxy_rules({"cq": "on"})
    # cq.exchange is split-origin: only the API host ever sees the key.
    assert rule.host == keyless.CQ_API_HOST == "api.cq.exchange"
    assert rule.header == "Authorization" and rule.value_prefix == "Bearer "
    assert rule.env == ("CQ_API_KEY",) and rule.requires == ()  # never gates the proxy's launch
    assert "foldyard.plugins.static_token" in rule.minter and "CQ_API_KEY" in rule.minter


def test_the_secret_is_asked_for_when_the_axis_goes_on(agents):
    p = agents("codex")
    assert p.secrets({"cq": "off"}) == []
    (secret,) = p.secrets({"cq": "on"})
    assert secret.var == "CQ_API_KEY" and secret.pattern == "cqa.*"
    assert "cq.exchange" in secret.how


def test_the_box_never_holds_a_cq_credential(agents):
    # No dummy either: the hosted API refuses a wrong key even on reads, while no key reads fine —
    # so at rest the box is anonymous and the proxy ADDS the header when the axis is on.
    p = agents("claude", "codex")
    args = p.box_args(dict(_ENV))
    assert not any("CQ_API_KEY" in a for a in args)
    assert p.held_credentials({"cq": "off"}) == []
    assert "FY_CQ_AGENTS=claude,codex" in args
    assert f"CQ_ADDR={keyless.CQ_ADDR}" in args and keyless.CQ_ADDR == "https://cq.exchange"
    assert "CQ_LOCAL_DB_PATH=/home/vscode/.local/share/cq/local.db" in args
    assert "devbox_cq_data:/home/vscode/.local/share/cq" in args  # queued KUs survive recreation


def test_cq_lights_up_proxy_routing_like_any_injector(agents):
    # Declaring cq is enough for the box to route through the proxy: some rung of the cq axis
    # carries a rule, and that is what the proxy plugin keys routing off.
    from foldyard.plugins import proxy

    reg = Registry([proxy.ProxyPlugin(), agents("claude")])
    assert "cq" in reg.switches()
    assert reg.proxy_rules({"cq": "on"})[0].host == keyless.CQ_API_HOST


def test_egress_recommend_covers_the_download_and_both_hosts(agents):
    hosts = {r["host"] for r in agents("codex").egress_recommend()}
    assert {"github.com", "release-assets.githubusercontent.com"} <= hosts
    assert {keyless.CQ_DISCOVERY_HOST, keyless.CQ_API_HOST} <= hosts


# ── the plugin: bootstrap ────────────────────────────────────────────────────────────────


def _bash_n(script: str) -> None:
    res = subprocess.run(["bash", "-n"], input=script, text=True, capture_output=True)
    assert res.returncode == 0, res.stderr


def test_bootstrap_steps_per_agent_and_all_parse(agents):
    both = agents("claude", "codex").box_bootstrap(dict(_ENV))
    assert [s["label"] for s in both] == [
        f"cq CLI v{cq.CQ_VERSION}",
        "cq for Claude",
        "cq for Codex",
    ]
    for step in both:
        _bash_n(step["run"])
        if step["check"]:
            _bash_n(step["check"])
    assert [s["label"] for s in agents("codex").box_bootstrap(dict(_ENV))][1:] == ["cq for Codex"]
    assert [s["label"] for s in agents("claude").box_bootstrap(dict(_ENV))][1:] == ["cq for Claude"]


def test_the_cli_is_pinned_and_checksummed(agents):
    install = agents("claude").box_bootstrap(dict(_ENV))[0]
    run = install["run"]
    assert f"cli%2Fv{cq.CQ_VERSION}/cq_Linux_$arch.tar.gz" in run
    for digest in cq.CQ_SHA256.values():
        assert len(digest) == 64 and digest in run
    assert "sha256sum -c" in run and "releases/latest" not in run
    # cached in the data volume, so a recreated box relinks it without egress
    assert f"/home/vscode/.local/share/cq/fy-bin/v{cq.CQ_VERSION}" in run
    assert f"^cq v{cq.CQ_VERSION} " in install["check"]


# Every tool the install step names, as a function that records its call (builtins only: the
# suite's PATH carries none of them). `curl`/`tar` succeed without writing; `sha256sum` passes.
_INSTALL_STUBS = """
uname() { echo x86_64; }
mktemp() { echo /nonexistent/tmp; }
for t in curl sha256sum tar mkdir mv chmod rm ln; do eval "$t() { echo \\"$t \\$*\\"; }"; done
"""


@pytest.mark.spawns("bash")  # runs the REAL step under bash (stubs as functions, builtins only)
@pytest.mark.parametrize(
    ("cached", "fetched"),
    [
        (None, True),  # first box: nothing cached
        (b"\x7fELF\x02\x01\x01\x00\x00\x00", True),  # truncated binary: won't run at all
        (b'echo "cq v0.0.1 (old)"\n', True),  # runs, but isn't the pinned version
        (f'echo "cq v{cq.CQ_VERSION} (abc123)"\n'.encode(), False),  # the pinned one: relinked
    ],
)
def test_the_cache_is_reused_only_when_it_is_the_pinned_version(agents, tmp_path, cached, fetched):
    # The step's own check reads `cq --version` too, so a cached binary that fails it must be
    # replaced here — skipping on "it's executable" would relink the bad one on every box-up.
    home = tmp_path / "home"
    dest = home / ".local/share/cq/fy-bin" / f"v{cq.CQ_VERSION}" / "cq"
    if cached is not None:
        dest.parent.mkdir(parents=True)
        dest.write_bytes(cached)
        dest.chmod(0o755)
    run = agents("claude").box_bootstrap({"FY_BOX_HOME": str(home)})[0]["run"]
    bash = shutil.which("bash")  # resolved here: the child's PATH holds nothing
    assert bash
    out = subprocess.run(
        [bash, "-c", f"{_INSTALL_STUBS}\n{run}"],
        capture_output=True,
        text=True,
        env={"PATH": "/nonexistent", "HOME": str(home)},
    )
    assert out.returncode == 0, out.stderr
    calls = out.stdout.splitlines()
    assert any(c.startswith("curl ") for c in calls) is fetched
    assert any(c.startswith("mv /nonexistent/tmp/cq ") for c in calls) is fetched
    assert calls[-1] == f"ln -sf {dest} {home}/.local/bin/cq"


def test_codex_files_never_replace_the_images_own(agents):
    (_, step) = agents("codex").box_bootstrap(dict(_ENV))
    for name in ("config.toml", "hooks.json"):
        assert f"/etc/codex/{name} belongs to the box image" in step["run"]


def test_claude_plugin_is_named_cq_with_the_mcp_server_and_three_hooks():
    manifest = cq.claude_plugin_json()
    assert manifest["name"] == "cq"  # → /cq:reflect, /cq:status
    assert manifest["mcpServers"]["cq"]["args"] == ["mcp"]
    hooks = cq.claude_hooks()["hooks"]
    assert set(hooks) == {"SessionStart", "PostToolUseFailure", "SessionEnd"}
    commands = [h["command"] for groups in hooks.values() for g in groups for h in g["hooks"]]
    assert commands == [
        "@FY_BIN@ _cq-hook claude session-start",
        "@FY_BIN@ _cq-hook claude post-tool-use-failure",
        "@FY_BIN@ _cq-hook claude session-end",
    ]


# The event names Codex's hooks.json accepts (codex-rs config HookEventsToml, deny_unknown_fields).
_CODEX_EVENTS = {
    "PreToolUse",
    "PermissionRequest",
    "PostToolUse",
    "PreCompact",
    "PostCompact",
    "SessionStart",
    "SessionEnd",
    "UserPromptSubmit",
    "SubagentStart",
    "SubagentStop",
    "Stop",
    "Interrupt",
}


def test_codex_system_files_have_the_shape_codex_parses():
    hooks = cq.codex_hooks()
    assert set(hooks) == {"description", "hooks"}  # HooksFile: nothing else is allowed
    assert set(hooks["hooks"]) == {"SessionStart", "SessionEnd"} <= _CODEX_EVENTS
    for groups in hooks["hooks"].values():
        for handler in (h for g in groups for h in g["hooks"]):
            assert set(handler) == {"type", "command", "timeout"} and handler["type"] == "command"
    (end,) = hooks["hooks"]["SessionEnd"]
    assert end["hooks"][0]["timeout"] <= 3  # Codex's SessionEnd ceiling
    server = tomllib.loads(cq.codex_config_toml())["mcp_servers"]["cq"]
    assert server["args"] == ["mcp"]
    # Codex starts MCP servers from a scrubbed env: cq's address and the route out must be named.
    assert {"CQ_ADDR", "HTTPS_PROXY", "NO_PROXY", "SSL_CERT_FILE"} <= set(server["env_vars"])
    assert cq.codex_config_toml().startswith("# foldyard: cq for Codex")


# ── the hooks' handler ───────────────────────────────────────────────────────────────────


def _run(agent: str, event: str, payload: object, tmp_path: Path, env: dict | None = None) -> str:
    out = io.StringIO()
    raw = payload if isinstance(payload, str) else json.dumps(payload)
    rc = cqhook.run(agent, event, io.StringIO(raw), out, env=env or {}, directory=tmp_path)
    assert rc == 0  # a hook never fails the agent
    return out.getvalue()


@pytest.mark.parametrize("agent", ["claude", "codex"])
def test_session_start_puts_the_protocol_in_context(agent, tmp_path):
    out = json.loads(_run(agent, "session-start", {"session_id": "s"}, tmp_path))
    ctx = out["hookSpecificOutput"]
    assert ctx["hookEventName"] == "SessionStart"
    assert "query cq" in ctx["additionalContext"] and "fy mode cq=on" in ctx["additionalContext"]


def test_a_failure_nudges_once_per_distinct_call(tmp_path):
    fail = {"session_id": "s1", "tool_name": "Bash", "tool_input": {"command": "npm test"}}
    first = json.loads(_run("claude", "post-tool-use-failure", fail, tmp_path))
    assert first["hookSpecificOutput"]["hookEventName"] == "PostToolUseFailure"
    assert _run("claude", "post-tool-use-failure", fail, tmp_path) == ""  # same call again
    other = {**fail, "tool_input": {"command": "uv sync"}}
    assert _run("claude", "post-tool-use-failure", other, tmp_path) != ""
    # a different session has its own ledger
    assert _run("claude", "post-tool-use-failure", {**fail, "session_id": "s2"}, tmp_path) != ""


def test_failure_nudges_are_capped_and_skip_interrupts(tmp_path):
    base = {"session_id": "s", "tool_name": "Bash"}
    nudged = [
        _run(
            "claude",
            "post-tool-use-failure",
            {**base, "tool_input": {"command": f"c{i}"}},
            tmp_path,
        )
        for i in range(cqhook.MAX_NUDGES + 2)
    ]
    assert sum(bool(n) for n in nudged) == cqhook.MAX_NUDGES
    stop = {"session_id": "t", "tool_name": "Bash", "is_interrupt": True}
    assert _run("claude", "post-tool-use-failure", stop, tmp_path) == ""


def test_codex_gets_no_failure_nudge(tmp_path):
    fail = {"session_id": "s", "tool_name": "Bash", "tool_input": {"command": "x"}}
    assert _run("codex", "post-tool-use-failure", fail, tmp_path) == ""


def test_session_end_names_the_session_for_the_launcher(tmp_path):
    target = tmp_path / "session"
    env = {cqhook.SESSION_FILE_ENV: str(target)}
    _run("codex", "session-end", {"session_id": "019a-uuid", "reason": "other"}, tmp_path, env)
    assert cqhook.read_session(target) == "019a-uuid"
    # Not an id → ignored (it lands in a resume argv); the earlier one stands.
    _run("claude", "session-end", {"session_id": "../../etc/passwd"}, tmp_path, env)
    _run("claude", "session-end", {"session_id": "--help"}, tmp_path, env)
    assert cqhook.read_session(target) == "019a-uuid"
    # no file asked for → nothing written anywhere
    _run("claude", "session-end", {"session_id": "abc"}, tmp_path, {})
    assert cqhook.read_session(target) == "019a-uuid"


@pytest.mark.parametrize("raw", ["", "not json", "[1, 2]", "x" * (2 << 20)])
def test_garbage_in_never_fails_the_agent(raw, tmp_path):
    for event in cqhook.EVENTS:
        _run("claude", event, raw, tmp_path)


def test_the_hook_command_skips_the_cli_gates(monkeypatch, tmp_path):
    # Wired through the real CLI: the root callback's version floor / adoption notice must not
    # turn into an error under every hook event.
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    monkeypatch.setattr(
        cli,
        "_say_if_unadopted",
        lambda verb: pytest.fail("the hook must not run the adoption notice"),
    )
    result = CliRunner().invoke(
        cli.app, ["_cq-hook", "claude", "session-start"], input='{"session_id": "s"}'
    )
    assert result.exit_code == 0
    assert json.loads(result.stdout)["hookSpecificOutput"]["hookEventName"] == "SessionStart"


# ── the launchers ────────────────────────────────────────────────────────────────────────


def test_claude_argv_loads_the_plugin_before_the_callers_args():
    argv = box._claude_argv("", {}, ["--resume", "abc"], (cq.CLAUDE_PLUGIN_DIR,))
    assert argv[-4:] == ["--plugin-dir", cq.CLAUDE_PLUGIN_DIR, "--resume", "abc"]
    assert "--plugin-dir" not in box._claude_argv("", {}, [])


def test_cq_wired_reads_the_boxs_own_record(monkeypatch):
    monkeypatch.setenv("FY_CQ_AGENTS", "claude,codex")
    assert box._cq_wired("claude") and box._cq_wired("codex")
    monkeypatch.setenv("FY_CQ_AGENTS", "codex")
    assert not box._cq_wired("claude")
    monkeypatch.delenv("FY_CQ_AGENTS")
    assert not box._cq_wired("codex")


def test_reflect_is_offered_only_at_a_terminal_and_not_headless(monkeypatch):
    tty = type("T", (), {"isatty": lambda self: True})()
    monkeypatch.setattr(box.sys, "stdin", tty)
    monkeypatch.setattr(box.sys, "stdout", tty)
    assert box._reflect_offerable([], box._CLAUDE_HEADLESS)
    assert not box._reflect_offerable(["-p", "hi"], box._CLAUDE_HEADLESS)
    assert not box._reflect_offerable(["exec", "do it"], box._CODEX_HEADLESS)
    assert box._reflect_offerable(["resume"], box._CODEX_HEADLESS)
    monkeypatch.setattr(box.sys, "stdin", io.StringIO())
    assert not box._reflect_offerable([], box._CLAUDE_HEADLESS)


@pytest.fixture
def agent_runs(monkeypatch):
    """Fake the child agent: each run records its argv; the first one plays the SessionEnd hook
    (writes the session id into the file the launcher named)."""
    runs: list[list[str]] = []

    def fake_run(argv, env):
        runs.append(argv)
        target = env.get(cqhook.SESSION_FILE_ENV)
        if len(runs) == 1 and target:
            cqhook.record_session({"session_id": "sess-1"}, env)
        return 3 if len(runs) == 1 else 0

    monkeypatch.setattr(box, "_run_foreground", fake_run)
    return runs


def test_reflect_resumes_the_ended_session_when_accepted(agent_runs, monkeypatch):
    monkeypatch.setattr(box, "_confirm", lambda q: True)
    rc = box._with_reflect(
        "Claude", ["claude"], lambda sid: ["claude", "--resume", sid, "/cq:reflect"]
    )
    assert rc == 3  # the session's own status, not the epilogue's
    assert agent_runs == [["claude"], ["claude", "--resume", "sess-1", "/cq:reflect"]]


def test_declining_or_no_session_runs_nothing_more(agent_runs, monkeypatch):
    monkeypatch.setattr(box, "_confirm", lambda q: False)
    assert box._with_reflect("Codex", ["codex"], lambda sid: ["never"]) == 3
    assert agent_runs == [["codex"]]
    # No SessionEnd recorded (the agent crashed, or a hook didn't fire): no question at all.
    agent_runs.clear()
    asked = []
    monkeypatch.setattr(box, "_confirm", lambda q: asked.append(q) or True)
    monkeypatch.setattr(box, "_run_foreground", lambda argv, env: agent_runs.append(argv) or 0)
    assert box._with_reflect("Codex", ["codex"], lambda sid: ["never"]) == 0
    assert agent_runs == [["codex"]] and asked == []


def test_claude_launcher_wires_the_plugin_and_the_reflect_offer(monkeypatch, tmp_path):
    plugin = tmp_path / "cq"
    plugin.mkdir()
    monkeypatch.setattr(cq, "CLAUDE_PLUGIN_DIR", str(plugin))
    monkeypatch.setenv("IN_DEVBOX", "1")
    monkeypatch.setenv("FY_CQ_AGENTS", "claude")
    monkeypatch.setattr(box.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(box.config, "claude_system_prompt", lambda: "")
    monkeypatch.setattr(box.config, "claude_settings", lambda: {})
    monkeypatch.setattr(box, "_reflect_offerable", lambda args, headless: True)
    seen = {}

    def fake_with_reflect(agent, argv, resume):
        seen["argv"], seen["resume"] = argv, resume("sid-9")
        return 0

    monkeypatch.setattr(box, "_with_reflect", fake_with_reflect)
    assert box.claude(["hello"]) == 0
    assert seen["argv"][-3:] == ["--plugin-dir", str(plugin), "hello"]
    assert seen["resume"][-5:] == ["--plugin-dir", str(plugin), "--resume", "sid-9", "/cq:reflect"]


def test_claude_launcher_execs_as_before_without_cq(monkeypatch):
    monkeypatch.setenv("IN_DEVBOX", "1")
    monkeypatch.delenv("FY_CQ_AGENTS", raising=False)
    monkeypatch.setattr(box.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(box.config, "claude_system_prompt", lambda: "")
    monkeypatch.setattr(box.config, "claude_settings", lambda: {})
    monkeypatch.setattr(box, "_with_reflect", lambda *a: pytest.fail("no cq, no epilogue"))

    def fake_exec(file, argv, env):
        assert "--plugin-dir" not in argv
        raise SystemExit(0)

    monkeypatch.setattr(box.os, "execvpe", fake_exec)
    with pytest.raises(SystemExit):
        box.claude([])


def test_codex_launcher_resumes_with_cqs_reflect_prompt(monkeypatch):
    monkeypatch.setenv("IN_DEVBOX", "1")
    monkeypatch.setenv("FY_CQ_AGENTS", "codex")
    monkeypatch.setattr(box.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(box.config, "codex_system_prompt", lambda: "")
    monkeypatch.setattr(box.config, "codex_config", lambda: {})
    monkeypatch.setattr(box, "_reflect_offerable", lambda args, headless: True)
    monkeypatch.setattr(box, "_cq_reflect_prompt", lambda: "REFLECT")
    seen = {}

    def fake_with_reflect(agent, argv, resume):
        seen["agent"], seen["resume"] = agent, resume("019a")
        return 0

    monkeypatch.setattr(box, "_with_reflect", fake_with_reflect)
    assert box.codex([]) == 0
    assert seen["agent"] == "Codex" and seen["resume"][-3:] == ["resume", "019a", "REFLECT"]
    # cq's prompt unavailable → no resume argv (the launcher reports it and skips)
    monkeypatch.setattr(box, "_cq_reflect_prompt", lambda: None)
    assert box.codex([]) == 0 and seen["resume"] is None
