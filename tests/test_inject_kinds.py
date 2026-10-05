"""``[[inject]] kind`` — credentials as protocol kinds plus data (ADR-0031).

A row's ``kind`` names a token PROTOCOL shipped as package code (``static``, ``github-app``,
``gh-cli``), each with a FIXED field shape. These tests pin the shapes and their refusals, what
each kind turns into (the rule, the secret it declares, its capability probe and doctor rows), and
the per-row ``box_env`` dummies with the ``fy verify`` row that guards them. The minters themselves
are tested in test_github_minters.py; the generic static rows in test_plugins.py.
"""

from __future__ import annotations

import base64
import shlex

import pytest

from foldyard import config, devmode
from foldyard.plugins import (
    CredentialScope,
    DoctorContext,
    Registry,
    VerifyContext,
    inject,
    kinds,
    proxy,
)

_APP = {
    "switch": "github",
    "kind": "github-app",
    "app_id": "4008762",
    "installation_id": "139125083",
}
_USER = {"switch": "github-user", "kind": "gh-cli", "emergency": True}

_PEM_BODY = "-----BEGIN RSA PRIVATE KEY-----\nabc\n-----END RSA PRIVATE KEY-----\n"
_PEM_B64 = base64.b64encode(_PEM_BODY.encode()).decode()


def _plugin(monkeypatch, specs):
    """An InjectPlugin over stubbed config rows (stubs the read, not `_specs`, which validates)."""
    monkeypatch.setattr(inject.config, "inject_specs", lambda: specs)
    return inject.InjectPlugin()


def _argv(rule) -> list[str]:
    return shlex.split(rule.minter)


# ── the shapes, and what they refuse ──────────────────────────────────────────────────


def test_a_row_without_a_kind_is_static(monkeypatch):
    # Today's behaviour, unchanged: no `kind` is the static-token kind.
    spec = {"switch": "svc", "host": "api.svc.test"}
    (rule,) = _plugin(monkeypatch, [spec]).proxy_rules({"svc": "on"})
    assert "foldyard.plugins.static_token" in rule.minter
    assert _plugin(monkeypatch, [{**spec, "kind": "static"}]).proxy_rules({"svc": "on"}) == [rule]


def test_an_unknown_kind_is_refused_naming_the_kinds_that_exist(monkeypatch):
    plugin = _plugin(monkeypatch, [{"switch": "svc", "host": "a.test", "kind": "vault"}])
    with pytest.raises(ValueError, match="unknown kind 'vault'") as e:
        plugin.switches()
    assert "static" in str(e.value) and "github-app" in str(e.value) and "gh-cli" in str(e.value)


@pytest.mark.parametrize(
    ("spec", "field"),
    [
        # The ADR's first decision: `permissions` is gone — the App installation IS the scope.
        ({**_APP, "permissions": {"contents": "write"}}, "permissions"),
        ({"switch": "svc", "host": "a.test", "app_id": "1"}, "app_id"),  # not a static field
        ({**_USER, "header": "X-Token"}, "header"),  # gh-cli takes no kind-specific field
        ({**_APP, "repo": "Tangible"}, "repo"),  # the old [plugins.github] spelling
    ],
)
def test_a_field_the_kind_does_not_take_is_refused_naming_what_it_does(monkeypatch, spec, field):
    # A typo or a stale key must fail where it's written, not become a silently ignored control.
    with pytest.raises(ValueError, match=f"'{field}'") as e:
        _plugin(monkeypatch, [spec]).switches()
    assert "takes:" in str(e.value)


def test_the_retired_minter_and_token_env_keys_stay_ignored_not_refused(monkeypatch):
    # They are reported as IGNORED_KEYS by `fy config widenings` — refusing them would turn a
    # config the report already explains into a registry that won't load.
    spec = {"switch": "svc", "host": "a.test", "minter": "evil", "token_env": "ANTHROPIC_API_KEY"}
    (rule,) = _plugin(monkeypatch, [spec]).proxy_rules({"svc": "on"})
    assert rule.env == ("FY_INJECT_SVC",) and "evil" not in rule.minter


@pytest.mark.parametrize("missing", ["app_id", "installation_id"])
def test_github_app_requires_its_identity(monkeypatch, missing):
    spec = {k: v for k, v in _APP.items() if k != missing}
    with pytest.raises(ValueError, match=f"needs `{missing}`"):
        _plugin(monkeypatch, [spec]).switches()


@pytest.mark.parametrize("bad", ["12/../..", "abc", "", True, 1.5])
def test_github_app_ids_must_be_numbers(monkeypatch, bad):
    # They land in a URL path on api.github.com and on the minter's argv; a number is all they are.
    with pytest.raises(ValueError, match="app_id"):
        _plugin(monkeypatch, [{**_APP, "app_id": bad}]).switches()


def test_github_app_ids_may_be_written_as_toml_integers(monkeypatch):
    spec = {**_APP, "app_id": 4008762, "installation_id": 139125083}
    (rule,) = _plugin(monkeypatch, [spec]).proxy_rules({"github": "on"})
    argv = _argv(rule)
    assert argv[argv.index("--app-id") + 1] == "4008762"


@pytest.mark.parametrize(
    "repos", [["acme/Tangible"], "Tangible", [""], [7], ["has space"]], ids=repr
)
def test_github_app_repositories_are_a_list_of_bare_names(monkeypatch, repos):
    with pytest.raises(ValueError, match="repositories"):
        _plugin(monkeypatch, [{**_APP, "repositories": repos}]).switches()


@pytest.mark.parametrize("spec", [_APP, _USER], ids=["github-app", "gh-cli"])
def test_a_github_kind_is_pinned_to_api_github_com(monkeypatch, spec):
    # A minted GitHub token must never be aimed at another host by config — that's the whole
    # credential, delivered wherever the row says.
    (rule,) = _plugin(monkeypatch, [spec]).proxy_rules({spec["switch"]: "on"})
    assert rule.host == "api.github.com"
    assert _plugin(monkeypatch, [{**spec, "host": "api.github.com"}]).switches()
    with pytest.raises(ValueError, match=r"api\.github\.com"):
        _plugin(monkeypatch, [{**spec, "host": "collector.example"}]).switches()


@pytest.mark.parametrize("emergency", [None, False])
def test_gh_cli_must_be_an_emergency_switch(monkeypatch, emergency):
    # It injects the operator's OWN token (push included), so `on` has to expire — the property the
    # old `github=user` level carried by construction.
    spec = {k: v for k, v in _USER.items() if k != "emergency"}
    if emergency is not None:
        spec["emergency"] = emergency
    with pytest.raises(ValueError, match="emergency = true"):
        _plugin(monkeypatch, [spec]).switches()
    (switch,) = _plugin(monkeypatch, [_USER]).switches()
    assert switch.emergency == ("on",)


# ── what each kind turns into ─────────────────────────────────────────────────────────


def test_github_app_rule_mints_with_the_identity_on_argv_and_the_pem_by_name(monkeypatch):
    monkeypatch.setattr(config, "proxy_port", lambda: 41000)
    plugin = _plugin(monkeypatch, [{**_APP, "repositories": ["Tangible", "docs"]}])
    assert plugin.proxy_rules({"github": "off"}) == []
    (rule,) = plugin.proxy_rules({"github": "on"})
    argv = _argv(rule)
    assert argv[1:3] == ["-m", "foldyard.plugins.github_app_token"]
    assert argv[argv.index("--app-id") + 1] == "4008762"
    assert argv[argv.index("--installation-id") + 1] == "139125083"
    assert [argv[i + 1] for i, a in enumerate(argv) if a == "--repository"] == ["Tangible", "docs"]
    # The PEM arrives by NAME, the one var the rule lets the minter read (the addon resolves it
    # from host.env) — and that var is derived from the switch, so no row can aim at another's.
    assert argv[argv.index("--pem-env") + 1] == "FY_INJECT_GITHUB"
    assert rule.env == ("FY_INJECT_GITHUB",)
    # Never a spawn gate: one missing PEM must not connection-refuse every box request.
    assert rule.requires == ()
    assert argv[argv.index("--own-proxy-port") + 1] == "41000"
    assert rule.header == "Authorization" and rule.replay_on_401 is True


def test_github_app_without_repositories_sends_no_repository_flag(monkeypatch):
    (rule,) = _plugin(monkeypatch, [_APP]).proxy_rules({"github": "on"})
    assert "--repository" not in _argv(rule)


def test_replay_on_401_keeps_its_per_kind_default_and_can_be_turned_off(monkeypatch):
    (rule,) = _plugin(monkeypatch, [{**_APP, "replay_on_401": False}]).proxy_rules({"github": "on"})
    assert rule.replay_on_401 is False
    (static,) = _plugin(monkeypatch, [{"switch": "s", "host": "a.test"}]).proxy_rules({"s": "on"})
    assert static.replay_on_401 is False


def test_gh_cli_rule_reads_nothing_from_host_env(monkeypatch):
    (rule,) = _plugin(monkeypatch, [_USER]).proxy_rules({"github-user": "on"})
    assert _argv(rule)[1:3] == ["-m", "foldyard.plugins.gh_cli_token"]
    assert rule.env == () and rule.requires == ()  # `gh` holds its own credential
    assert rule.replay_on_401 is True


def test_github_app_declares_its_pem_as_a_base64_secret_only_when_on(monkeypatch):
    plugin = _plugin(monkeypatch, [_APP, _USER])
    assert plugin.secrets({"github": "off", "github-user": "on"}) == []  # gh-cli has no secret
    (secret,) = plugin.secrets({"github": "on"})
    assert secret.var == "FY_INJECT_GITHUB" and secret.b64 is True
    assert secret.pattern == kinds.PEM_PATTERN
    assert "private key" in secret.label and "github" in secret.label
    assert "App settings" in secret.how  # printed at the prompt, never run


def test_a_secret_row_can_retarget_the_pem_hint(monkeypatch):
    # The `[[secret]]` override works on the derived var like on any other.
    monkeypatch.setattr(
        config, "secret_specs", lambda: [{"var": "FY_INJECT_GITHUB", "how": "op read op://x"}]
    )
    reg = Registry([_plugin(monkeypatch, [_APP])])
    (secret,) = reg.secrets({"github": "on"})
    assert secret.how == "op read op://x" and secret.b64 is True


def test_kind_rules_flow_into_the_proxy_daemon(monkeypatch):
    reg = Registry([_plugin(monkeypatch, [_APP, _USER]), proxy.ProxyPlugin()])
    spec = reg.desired_daemons({"github": "on"})["egress-proxy"]
    (rule,) = spec["live"]["data"]["rules"]
    assert rule["host"] == "api.github.com" and "github_app_token" in rule["command"]
    assert rule["env"] == ["FY_INJECT_GITHUB"]


# ── box_env: the dummies the box holds ────────────────────────────────────────────────


def test_box_env_is_baked_with_the_proxy_substrate_whatever_the_level(monkeypatch):
    # AMBIENT, like the old dummy GH_TOKEN: box env is create-time and the switch is host-side, so
    # keying them together would make `fy mode github=on` need a box recreate to take.
    specs = [{**_APP, "box_env": {"GH_TOKEN": "x"}}, {**_USER, "box_env": {"GH_TOKEN": "x"}}]
    plugin = _plugin(monkeypatch, specs)
    assert plugin.box_args({"FY_PROXY": "h:1"}) == ["-e", "GH_TOKEN=x"]  # once, not per row
    assert plugin.box_args({}) == []  # no proxy routing ⇒ nothing would ever replace it


@pytest.mark.parametrize(
    ("box_env", "match"),
    [
        ({"GH-TOKEN": "x"}, "environment-variable name"),
        ({"GH_TOKEN": 1}, "string"),
        ("GH_TOKEN=x", "table"),
        ({"FY_PROXY": "x"}, "foldyard sets"),
        ({"FOLDYARD_CHECKOUT": "/"}, "foldyard sets"),
        ({"HTTPS_PROXY": "http://evil:1"}, "foldyard sets"),
        ({"https_proxy": "http://evil:1"}, "foldyard sets"),
        ({"CONTAINER_HOST": "tcp://x"}, "foldyard sets"),
        ({"SSL_CERT_FILE": "/x"}, "foldyard sets"),
        ({"IN_DEVBOX": "0"}, "foldyard sets"),
    ],
)
def test_box_env_is_refused_unless_it_is_plain_dummies(monkeypatch, box_env, match):
    # A dummy may not overwrite what foldyard itself bakes: rerouting the proxy, the engine socket
    # or the CA bundle from a credential row would be a box-escape knob in a field meant for "x".
    with pytest.raises(ValueError, match=match):
        _plugin(monkeypatch, [{**_APP, "box_env": box_env}]).switches()


def test_two_rows_may_share_a_dummy_but_not_disagree_on_it(monkeypatch):
    specs = [{**_APP, "box_env": {"GH_TOKEN": "x"}}, {**_USER, "box_env": {"GH_TOKEN": "y"}}]
    with pytest.raises(ValueError, match="GH_TOKEN"):
        _plugin(monkeypatch, specs).switches()


def _verify(monkeypatch, specs, env: dict[str, str], in_box: bool = True):
    for var in ("GH_TOKEN", "GITHUB_TOKEN", "SVC_TOKEN"):
        monkeypatch.delenv(var, raising=False)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    ctx = VerifyContext(in_box=in_box, env={}, which=lambda _c: False)
    return list(_plugin(monkeypatch, specs).verify_checks(ctx))


def test_verify_passes_a_box_holding_only_its_dummies(monkeypatch):
    rows = _verify(monkeypatch, [{**_APP, "box_env": {"GH_TOKEN": "x"}}], {"GH_TOKEN": "x"})
    assert [s for s, _ in rows] == ["pass", "pass"]  # GH_TOKEN the dummy, GITHUB_TOKEN unset
    assert "GH_TOKEN" in rows[0][1] and "dummy" in rows[0][1]
    # Unset is not a leak either (a box created without proxy routing has no dummy).
    rows = _verify(monkeypatch, [{**_APP, "box_env": {"GH_TOKEN": "x"}}], {})
    assert [s for s, _ in rows] == ["pass", "pass"]


def test_verify_fails_a_real_looking_credential_where_a_dummy_belongs(monkeypatch):
    specs = [{"switch": "svc", "host": "a.test", "box_env": {"SVC_TOKEN": "dummy"}}]
    rows = _verify(monkeypatch, specs, {"SVC_TOKEN": "sk-live-123"})
    (status, message) = next(r for r in rows if "SVC_TOKEN" in r[1])
    assert status == "fail" and message.startswith("svc: SVC_TOKEN isn't its dummy 'dummy'")
    assert "possible real credential in the box" in message
    assert "sk-live-123" not in message  # verify output is not where a secret gets printed


def test_verify_watches_the_github_token_vars_a_dummy_does_not_name(monkeypatch):
    # gh reads GITHUB_TOKEN too; the old github verify row checked both, and a kind that knows its
    # client's credential vars keeps doing so — never weaker than what it replaced.
    rows = _verify(monkeypatch, [{**_APP, "box_env": {"GH_TOKEN": "x"}}], {"GITHUB_TOKEN": "ghp"})
    assert ("fail" in [s for s, _ in rows]) and any("GITHUB_TOKEN" in m for _, m in rows)
    # …and without any box_env the github kinds still watch both.
    rows = _verify(monkeypatch, [_USER], {"GH_TOKEN": "gho_real"})
    assert [s for s, m in rows if "GH_TOKEN" in m] == ["fail"]


def test_verify_says_nothing_outside_the_box(monkeypatch):
    assert _verify(monkeypatch, [{**_APP, "box_env": {"GH_TOKEN": "x"}}], {}, in_box=False) == []


def test_every_box_is_watched_for_the_credential_vars_a_known_kind_reads(monkeypatch):
    # The github plugin's verify row ran in EVERY box, declared or not: a real GH_TOKEN failed
    # verify whatever the config said. Removing the plugin must not weaken that — so the vars every
    # known kind's client reads are watched with no row declaring them, and only a declared dummy
    # passes.
    rows = _verify(monkeypatch, [{"switch": "svc", "host": "a.test"}], {"GH_TOKEN": "ghp_real"})
    assert ("fail", "GH_TOKEN isn't unset — possible real credential in the box") in rows
    assert ("pass", "GITHUB_TOKEN unset (no credential in the box)") in rows
    assert [s for s, _ in _verify(monkeypatch, [], {})] == ["pass", "pass"]


# ── the github-app capability probe + doctor rows ─────────────────────────────────────


def test_github_app_probe_is_contributed_per_active_rule_named_after_its_switch(
    monkeypatch, tmp_path
):
    from foldyard.plugins import github_app_token

    (tmp_path / "host.env").write_text(f"FY_INJECT_GITHUB={_PEM_B64}\n")
    monkeypatch.setattr(config, "host_env_file", lambda: tmp_path / "host.env")
    monkeypatch.delenv("FY_INJECT_GITHUB", raising=False)
    plugin = _plugin(monkeypatch, [_APP, _USER])
    assert plugin.capability_probes({"github": "off", "github-user": "on"}) == []
    (probe,) = plugin.capability_probes({"github": "on"})
    assert probe.switch == "github" and probe.name == "github-github-app"
    seen = []
    scope = {"permissions": {"issues": "write"}, "repository_selection": "selected"}
    monkeypatch.setattr(
        github_app_token,
        "installation_probe",
        lambda app_id, installation_id, pem_b64, skip_port=None, var="": (
            seen.append((app_id, installation_id, pem_b64, var)) or (True, "ok", scope)
        ),
    )
    assert probe.scope is not None and probe.scope() is None  # nothing observed before a check
    assert probe.check() == (True, "ok")
    # The PEM exactly as the minter would read it, and the var a missing one belongs in.
    assert seen == [("4008762", "139125083", _PEM_B64, "FY_INJECT_GITHUB")]
    # The scope that check read, as the core's CredentialScope: this App + installation's.
    assert probe.scope() == CredentialScope(
        identity="App 4008762, installation 139125083",
        permissions={"issues": "write"},
        reach="selected repositories",
    )
    # A probe error must degrade the switch, never take the supervisor tick down — and leaves no
    # scope behind: the next report must not repeat an earlier read as if it were fresh.
    monkeypatch.setattr(
        github_app_token,
        "installation_probe",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("x")),
    )
    ok, detail = probe.check()
    assert not ok and "RuntimeError" in detail
    assert probe.scope() is None


def test_only_a_kind_that_reads_its_scope_names_a_scope_identity():
    # The identity keys the scope record: `fy config widenings` shows a record only for the
    # credential the ADOPTED row names, never one an earlier installation_id left behind.
    assert kinds.scope_identity(_APP) == "App 4008762, installation 139125083"
    assert kinds.scope_identity(_USER) == ""
    assert kinds.scope_identity({"switch": "svc", "host": "a.test"}) == ""
    assert kinds.scope_identity({"switch": "svc", "kind": "nonsense"}) == ""


def test_the_probe_validates_against_the_registry(monkeypatch):
    # Registry.capability_probes refuses a probe for a switch it doesn't have: the kind's probe
    # must name the row's own switch, which the same plugin registered.
    reg = Registry([_plugin(monkeypatch, [_APP])])
    monkeypatch.setattr(config, "host_env_file", lambda: __import__("pathlib").Path("/nonexistent"))
    assert [p.switch for p in reg.capability_probes({"github": "on"})] == ["github"]


def _doctor(monkeypatch, tmp_path, specs, *, host_env="", mode=None, which=False):
    (tmp_path / "host.env").write_text(host_env)
    monkeypatch.setattr(config, "host_env_file", lambda: tmp_path / "host.env")
    monkeypatch.setattr(config, "secret_specs", lambda: [])
    monkeypatch.delenv("FY_INJECT_GITHUB", raising=False)
    ctx = DoctorContext(
        deep=False,
        run=lambda cmd, timeout=None: pytest.fail(f"doctor must not shell out here: {cmd}"),
        which=lambda _tool: which,
        result=devmode._result,
        probe=lambda _port: False,
        mode=mode or {},
    )
    return list(_plugin(monkeypatch, specs).doctor_checks(ctx))


def test_pem_doctor_fails_an_armed_switch_and_only_warns_a_resting_one(monkeypatch, tmp_path):
    # PRESENCE, offline (ADR-0023). A second App kept off for emergencies shouldn't fail doctor
    # forever for a key nobody needs until it's turned on.
    rows = _doctor(monkeypatch, tmp_path, [_APP], mode={"github": "on"})
    assert [(s, n) for s, n, _ in rows] == [("fail", "github PEM")]
    assert "fy box up" in rows[0][2] or "fy mode" in rows[0][2]
    rows = _doctor(monkeypatch, tmp_path, [_APP], mode={"github": "off"})
    assert [(s, n) for s, n, _ in rows] == [("warn", "github PEM")]


def test_pem_doctor_accepts_a_captured_key_and_flags_a_broken_one(monkeypatch, tmp_path):
    rows = _doctor(monkeypatch, tmp_path, [_APP], host_env=f"FY_INJECT_GITHUB={_PEM_B64}\n")
    assert [(s, n) for s, n, _ in rows] == [("ok", "github PEM"), ("ok", "github PEM shape")]
    # A truncated/half-pasted key is caught OFFLINE rather than as an opaque 401 at mint time.
    rows = _doctor(monkeypatch, tmp_path, [_APP], host_env="FY_INJECT_GITHUB=-----BEGIN RSA PRIV\n")
    assert [s for s, *_ in rows] == ["ok", "fail"]


def test_pem_doctor_names_the_consumers_hint_where_there_is_one(monkeypatch, tmp_path):
    # The capture prompt gets a `[[secret]]` override from Registry.secrets; doctor reads the kind
    # directly, so it applies the same override — one hint, wherever the operator looks. It's a
    # string foldyard prints, never a command it runs.
    _doctor(monkeypatch, tmp_path, [])  # pins host.env + the env for the call below
    monkeypatch.setattr(
        config,
        "secret_specs",
        lambda: [{"var": "FY_INJECT_GITHUB", "how": "vault read -field=pem secret/gh | base64"}],
    )
    ctx = DoctorContext(
        deep=False,
        run=lambda cmd, timeout=None: (1, ""),
        which=lambda _tool: False,
        result=devmode._result,
        probe=lambda _port: False,
    )
    (row,) = _plugin(monkeypatch, [_APP]).doctor_checks(ctx)
    assert "vault read -field=pem secret/gh" in row[2]


def test_gh_cli_doctor_names_the_cli_with_no_install_button(monkeypatch, tmp_path):
    rows = _doctor(monkeypatch, tmp_path, [_USER])
    assert [(s, n) for s, n, _ in rows] == [("fail", "github-user gh CLI")]
    assert "cli.github.com" in rows[0][2]
    assert list(_plugin(monkeypatch, [_USER]).doctor_fixes()) == []


def test_a_static_row_contributes_no_doctor_rows(monkeypatch, tmp_path):
    assert _doctor(monkeypatch, tmp_path, [{"switch": "svc", "host": "a.test"}]) == []


# ── the box-side end-to-end check: is the token actually reaching requests? ──────────


def _box_rows(monkeypatch, specs, *, mode, headers="", rc=0):
    ctx = DoctorContext(
        deep=False,
        run=lambda cmd, timeout=None: (rc, headers),
        which=lambda _tool: True,
        result=devmode._result,
        probe=lambda _port: False,
        mode=mode,
    )
    return [r for r in _plugin(monkeypatch, specs).box_doctor_checks(ctx) if r[0] != "running"]


def test_box_doctor_detects_that_injection_is_not_actually_happening(monkeypatch):
    # When the host-side mint broke, every state-reading signal stayed green — `fy mode` showed the
    # switch on and the proxy up — while `gh` 401'd. GitHub's anonymous rate limit (60/h) vs an
    # authenticated one is the cheapest witness on the wire, and never reveals the token.
    (row,) = _box_rows(monkeypatch, [_APP], mode={"github": "on"}, headers="x-ratelimit-limit: 60")
    assert row[:2] == ("fail", "github injection") and "NOT injected" in row[2]
    assert "fy host" in row[2]
    rows = _box_rows(
        monkeypatch, [_APP], mode={"github": "on"}, headers="HTTP/2 200\r\nx-ratelimit-limit: 5000"
    )
    assert [(s, n) for s, n, _ in rows] == [("ok", "github injection")]


def test_box_doctor_skips_a_resting_switch_and_reports_an_unreachable_api(monkeypatch):
    assert _box_rows(monkeypatch, [_APP], mode={"github": "off"}) == []
    (row,) = _box_rows(monkeypatch, [_USER], mode={"github-user": "on"}, rc=6)
    assert row[:2] == ("fail", "github-user injection") and "couldn't reach" in row[2]
