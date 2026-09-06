"""plugins/ — the plugin framework + the built-in gcp/github plugins.

Covers: axis validation, the registry's merge of axes/daemons/env/doctor across plugins,
duplicate-axis rejection, third-party discovery (a fake plugin via load_plugins(extra=…)
AND via the `foldyard.plugins` entry-point group), and the gcp/github plugin internals
(SA naming, minter paths, PAM-grant parsing relocated here from the devmode tests)."""

from __future__ import annotations

import json
import os
from datetime import timedelta

import pytest

from foldyard import config, devmode, keyless, plugins
from foldyard.plugins import (
    Axis,
    DoctorContext,
    InjectRule,
    PanelData,
    Plugin,
    Registry,
    Requires,
    TuiPanel,
    VerifyContext,
    auth0_sim,
    claude,
    codex,
    gcp,
    github,
    inject,
    proxy,
    static_token,
    vscode,
)

# ── Axis validation ───────────────────────────────────────────────────────────────────


def test_axis_default_is_rung_zero():
    # rungs[0] is the axis's zero-secret resting default — no longer necessarily "off" (storage
    # rests at "local", auth0 at "sim"); unset/invalid/expired values read as it.
    ax = Axis(name="x", rungs=("local", "staging"), blurb={"local": "", "staging": ""})
    assert ax.default == "local"


def test_axis_rejects_empty_rungs_and_emergency_default():
    with pytest.raises(ValueError):
        Axis(name="x", rungs=(), blurb={})
    with pytest.raises(ValueError):  # expiry reverts TO the default, so it can't BE emergency
        Axis(name="x", rungs=("off", "on"), blurb={"off": "", "on": ""}, emergency=("off",))


def test_axis_blurb_must_cover_rungs():
    with pytest.raises(ValueError):
        Axis(name="x", rungs=("off", "on"), blurb={"off": "zero"})  # missing "on"


def test_axis_emergency_must_be_a_rung():
    with pytest.raises(ValueError):
        Axis(name="x", rungs=("off", "on"), blurb={"off": "", "on": ""}, emergency=("boom",))


def test_axis_requires_validates_the_owning_side():
    # `when` must be non-empty rungs of the OWNING axis and severity error|warn; the required
    # axis side is deliberately NOT validated (it may be absent under this consumer's config —
    # that's the "absent satisfies nothing" semantics, not a declaration error).
    blurb = {"off": "", "on": ""}
    ok = Requires(when=("on",), axis="other", accepts=("x",))
    Axis(name="a", rungs=("off", "on"), blurb=blurb, requires=(ok,))  # fine, incl. unknown axis
    with pytest.raises(ValueError):  # when outside the owner's rungs
        Axis(
            name="a",
            rungs=("off", "on"),
            blurb=blurb,
            requires=(Requires(when=("boom",), axis="other", accepts=("x",)),),
        )
    with pytest.raises(ValueError):  # empty when — a requirement that never fires
        Axis(
            name="a",
            rungs=("off", "on"),
            blurb=blurb,
            requires=(Requires(when=(), axis="other", accepts=("x",)),),
        )
    with pytest.raises(ValueError):  # unknown severity
        Axis(
            name="a",
            rungs=("off", "on"),
            blurb=blurb,
            requires=(Requires(when=("on",), axis="other", accepts=("x",), severity="fatal"),),
        )


# ── a minimal third-party plugin, for the discovery + merge tests ─────────────────────


class DemoPlugin(Plugin):
    name = "demo"

    def axes(self):
        return [
            Axis(
                name="demo",
                rungs=("off", "on"),
                blurb={"off": "z", "on": "y"},
                daemon="demo-daemon",
                emergency=("on",),
            )
        ]

    def daemons(self, mode):
        if mode.get("demo", "off") == "off":
            return {}
        return {
            "demo-daemon": {"label": "demo", "port": 9, "cmd": ["x"], "env": {}, "requires": []}
        }

    def derive_env(self, mode):
        return {"DEMO": "1"} if mode.get("demo") == "on" else {}

    def doctor_checks(self, ctx):
        yield ctx.result(True, "demo check", "ok", "bad")


# ── Registry merging ──────────────────────────────────────────────────────────────────


# A synthetic "full" consumer config that declares the gcp-metadata + auth0-sim namespaces (so the
# DECLARED plugins load — registry plan Step C) and gcp's project (so the gcp axis self-gate passes
# — Step D). Passed to Registry/load_plugins to exercise the Tangible-shaped registry from a config,
# rather than depending on the ambient (generic, core-only) foldyard-dev config.
_FULL_TOML = {
    "proxy": {},
    "plugins": {
        "gcp-metadata": {"project": "acme-staging"},
        "github": {},
        "auth0-sim": {},
        "llm": {},
    },
    # Posture overlays are config-only now ([[overlay]] with a `when`); wiring one onto `storage`
    # is what makes the storage axis appear (config.overlay_when_axes — the gcp plugin's self-gate).
    "overlay": [
        {"file": "dev-stack/compose.identity.yml", "when": {"gcp": "sa"}},
        {"file": "dev-stack/compose.identity-data.yml", "when": {"gcp": "sa"}},
        {"file": "dev-stack/compose.storage-staging.yml", "when": {"storage": "staging"}},
        {"file": "dev-stack/compose.auth0-sim.yml", "when": {"auth0": "sim"}},
        {"file": "dev-stack/compose.auth0-real-gsm.yml", "when": {"auth0": "real", "gcp": "sa"}},
        {"file": "dev-stack/compose.llm.yml", "when": {"llm": ["record", "live"]}},
        {"file": "dev-stack/compose.llm-live.yml", "when": {"llm": "live"}},
    ],
}
_GENERIC_TOML: dict = {"project": {"name": "generic"}}  # declares none → core-only registry


def _cfg(toml: dict) -> config.Config:
    return config.Config(repo_root=config.repo_root(), worktree="", toml=toml)


def test_builtins_provide_gcp_and_github():
    reg = Registry([gcp.GcpPlugin(), github.GithubPlugin()], config=_cfg(_FULL_TOML))
    # gcp contributes BOTH its axes: gcp (identity-only — the llm rung is gone) and storage
    # (present because _FULL_TOML wires an [[overlay]] onto storage=staging, the axis's opt-in).
    assert set(reg.axes()) == {"gcp", "storage", "github"}
    assert reg.axis_rungs()["gcp"] == ("off", "logs", "sa", "user")
    assert reg.axis_rungs()["storage"] == ("local", "staging")
    assert reg.axis_defaults() == {"gcp": "off", "storage": "local", "github": "off"}
    assert reg.axis_daemon() == {"gcp": "gcp-minter", "storage": None, "github": "egress-proxy"}
    assert reg.emergency_rungs() == {"gcp": ("user",), "storage": (), "github": ("user",)}
    assert reg.blurbs()[("github", "app")].startswith("PR/issue")


def test_duplicate_axis_is_rejected():
    with pytest.raises(ValueError, match="duplicate mode axis 'gcp'"):
        Registry([gcp.GcpPlugin(), gcp.GcpPlugin()], config=_cfg(_FULL_TOML))


def test_registry_merges_daemons_env_doctor_across_plugins():
    reg = Registry([gcp.GcpPlugin(), DemoPlugin()])
    mode = {"gcp": "logs", "demo": "on"}
    daemons = reg.desired_daemons(mode)
    assert "gcp-minter" in daemons and "demo-daemon" in daemons
    assert reg.derive_env(mode)["DEMO"] == "1"
    names = [name for _, name, _ in reg.doctor_checks(_ctx())]
    assert "demo check" in names and "gcloud CLI" in names


def _ctx(deep=False, probe=lambda _port: False):
    return DoctorContext(
        deep=deep,
        run=devmode._run,
        which=devmode._which,
        result=devmode._result,
        probe=probe,
    )


# ── third-party discovery ─────────────────────────────────────────────────────────────


def test_load_plugins_appends_extra():
    loaded = plugins.load_plugins(config=_cfg(_FULL_TOML), extra=[DemoPlugin()], discover=False)
    assert any(isinstance(p, DemoPlugin) for p in loaded)
    # built-ins still come first (axis/doctor order is deliberate)
    assert [p.name for p in loaded[:2]] == ["gcp", "github"]


def test_entry_point_discovery(monkeypatch):
    """A plugin registered on the `foldyard.plugins` group is loaded + merged."""

    class _EP:
        def load(self):
            return DemoPlugin

    monkeypatch.setattr(plugins, "entry_points", lambda group: [_EP()])
    loaded = plugins.load_plugins(discover=True)
    assert any(isinstance(p, DemoPlugin) for p in loaded)


def test_broken_entry_point_is_ignored(monkeypatch):
    class _BadEP:
        def load(self):
            raise RuntimeError("boom")

    monkeypatch.setattr(plugins, "entry_points", lambda group: [_BadEP()])
    # A broken third-party plugin must never take down the hot path. Under a FULL config (gcp +
    # auth0-sim namespaces declared) all eight built-ins load, in their deliberate order.
    assert [p.name for p in plugins.load_plugins(config=_cfg(_FULL_TOML), discover=True)] == [
        "gcp",
        "github",
        "inject",
        "proxy",
        "auth0-sim",
        "llm",
        "claude",
        "vscode",
        "codex",
    ]


def test_load_plugins_core_only_for_a_generic_consumer():
    # The spinout boundary (registry plan Step C + Test strategy): a consumer that declares none of
    # the credential tables gets only the SMALL CORE — the declared gcp/auth0-sim plugins are absent.
    names = [p.name for p in plugins.load_plugins(config=_cfg(_GENERIC_TOML), discover=False)]
    assert names == ["github", "inject", "proxy", "claude", "vscode", "codex"]
    assert "gcp" not in names and "auth0-sim" not in names


# ── devmode reflects the registry ─────────────────────────────────────────────────────


def test_devmode_accessors_track_the_registry(full_config_bound):
    # devmode's axis accessors now read the LIVE registry for the active config (Step A) — no more
    # import-time snapshot. Under a full config the Tangible-shaped axes are present; the OPTIONAL
    # injectors (inject's penpot, claude/codex keyless) add more only when declared, so assert the
    # core set is a subset rather than an exact equality.
    assert {"gcp", "storage", "github", "capture", "auth0", "llm"} <= set(devmode.axes())
    assert devmode.axes()["capture"] == ("off", "on")
    assert devmode.axis_daemon()["github"] == "egress-proxy"
    assert devmode.axis_daemon()["capture"] == "egress-proxy"  # capture rides the proxy daemon
    assert devmode.emergency()["gcp"] == ("user",)
    assert devmode.mode_blurb()[("gcp", "off")] == "no GCP identity — zero secrets"


def test_devmode_accessors_are_core_only_for_a_generic_consumer():
    # The regression guard for the spinout boundary: under a generic config (no credential tables)
    # devmode sees NO axes at all — no gcp/storage/auth0/llm/capture, and no github either now
    # (its axis self-gates on [plugins.github], the last always-on exception; a consumer that
    # never declared github used to get a TUI mode row + gh doctor rows forever).
    with config.using(_cfg(_GENERIC_TOML)):
        plugins._clear_registry_cache()
        active = set(devmode.axes())
    plugins._clear_registry_cache()
    assert active == set()


# ── gcp plugin internals ──────────────────────────────────────────────────────────────


def test_gcp_sa_env_overrides_win(monkeypatch):
    monkeypatch.setenv("DEVBOX_LOG_SA", "box@p.iam.gserviceaccount.com")
    monkeypatch.setenv("APP_SERVICE_ACCOUNT", "app@p.iam.gserviceaccount.com")
    assert gcp.devbox_log_sa() == "box@p.iam.gserviceaccount.com"
    assert gcp.app_sa() == "app@p.iam.gserviceaccount.com"


def test_gcp_sa_email_built_from_project_and_labels(monkeypatch):
    monkeypatch.delenv("DEVBOX_LOG_SA", raising=False)
    monkeypatch.setattr(config, "gcp_project", lambda: "acme-staging")
    monkeypatch.setattr(config, "gcp_sa_labels", lambda: {"box": "log-reader"})
    assert gcp.devbox_log_sa() == "log-reader@acme-staging.iam.gserviceaccount.com"


def test_gcp_data_sas_are_the_non_app_box_labels(monkeypatch):
    # data_sas() = every sa_labels role beyond app/box, as <role>@<project>.iam… (the per-service
    # runtime SAs compose.identity-data.yml labels the data containers with).
    monkeypatch.setattr(config, "gcp_project", lambda: "acme-staging")
    monkeypatch.setattr(
        config,
        "gcp_sa_labels",
        lambda: {"app": "app-runtime", "box": "log-reader", "queue-worker": "queue-worker"},
    )
    assert gcp.data_sas() == {"queue-worker@acme-staging.iam.gserviceaccount.com"}


def test_gcp_minter_allowlist_grows_with_rung(monkeypatch):
    monkeypatch.setenv("DEVBOX_LOG_SA", "box@p.iam.gserviceaccount.com")
    monkeypatch.setenv("APP_SERVICE_ACCOUNT", "app@p.iam.gserviceaccount.com")
    logs = gcp.GcpPlugin().daemons({"gcp": "logs"})["gcp-minter"]["env"]["GCP_SA_ALLOWLIST"]
    sa = gcp.GcpPlugin().daemons({"gcp": "sa"})["gcp-minter"]["env"]["GCP_SA_ALLOWLIST"]
    assert "app@" not in logs and "app@" in sa


def test_gcp_derive_env_sa_identity_only_storage_adds_cloudsql():
    plugin = gcp.GcpPlugin()
    # gcp=sa is identity-only now — cloudsql (a data-plane concern) moved to storage=staging.
    assert plugin.derive_env({"gcp": "sa"})["COMPOSE_PROFILES"] == "metadata"
    env = plugin.derive_env({"gcp": "sa", "storage": "staging"})
    assert env["COMPOSE_PROFILES"] == "metadata,cloudsql"
    # The real-GCP compose overrides ride compose_overlays (not derive_env) so they STACK with the
    # auth0/data overlays instead of a single FOLDYARD_COMPOSE_EXTRA scalar clobbering them.
    assert "FOLDYARD_COMPOSE_EXTRA" not in env
    assert plugin.compose_overlays({"gcp": "off"}) == []


def test_gcp_grants_identity_only_and_storage_axis_gated_on_an_overlay(monkeypatch):
    # gcp grants IDENTITY only. The identity/storage compose overlays are config-only [[overlay]]
    # entries now (their layering is covered in test_posture_overlays_*), NOT plugin code. What
    # stays plugin-side: the minter's SA allowlist, and the `storage` axis appearing ONLY when the
    # consumer wires an overlay onto storage=staging (config.overlay_when_axes — the opt-in).
    monkeypatch.setenv("DEVBOX_LOG_SA", "box@p.iam.gserviceaccount.com")
    monkeypatch.setenv("APP_SERVICE_ACCOUNT", "app@p.iam.gserviceaccount.com")
    monkeypatch.setattr(config, "gcp_project", lambda: "p")
    monkeypatch.setattr(
        config,
        "gcp_sa_labels",
        lambda: {"app": "app-runtime", "box": "log-reader", "queue-worker": "queue-worker"},
    )
    plugin = gcp.GcpPlugin()
    # storage axis present iff some overlay's `when` references it; absent otherwise.
    monkeypatch.setattr(config, "overlay_when_axes", lambda: {"gcp", "storage"})
    assert any(ax.name == "storage" for ax in plugin.axes())
    monkeypatch.setattr(config, "overlay_when_axes", lambda: {"gcp"})
    assert not any(ax.name == "storage" for ax in plugin.axes())
    # The minter allows the app runtime SA AND each data-service SA on the sa rung only (identity
    # is the ONLY thing gcp grants).
    allow = plugin.daemons({"gcp": "sa"})["gcp-minter"]["env"]["GCP_SA_ALLOWLIST"]
    assert "app@" in allow
    assert "queue-worker@" in allow


def test_posture_overlays_matched_by_when_in_declaration_order():
    # The generic [[overlay]] mechanism (the config-only replacement for the plugins' old
    # compose_overlays hooks): each entry layers its file when the mode matches `when` — axis→value,
    # or axis→[values] for OR within one axis; keys AND together. Declaration order = -f order.
    with config.using(_cfg(_FULL_TOML)):

        def names(mode):
            return [p.name for p in config.matching_overlays(mode)]

        # gcp=sa lays the two identity overlays; +storage=staging adds the storage one, IN ORDER.
        assert names({"gcp": "sa"}) == ["compose.identity.yml", "compose.identity-data.yml"]
        assert names({"gcp": "sa", "storage": "staging"}) == [
            "compose.identity.yml",
            "compose.identity-data.yml",
            "compose.storage-staging.yml",
        ]
        # AND across keys: auth0=real needs BOTH auth0=real AND gcp=sa — real alone lays nothing.
        assert names({"auth0": "real"}) == []
        assert names({"auth0": "real", "gcp": "sa"}) == [
            "compose.identity.yml",
            "compose.identity-data.yml",
            "compose.auth0-real-gsm.yml",
        ]
        # OR within one axis (list): record and live both match compose.llm.yml; live additionally
        # lays the thin live override AFTER it (later -f wins).
        assert names({"llm": "record"}) == ["compose.llm.yml"]
        assert names({"llm": "live"}) == ["compose.llm.yml", "compose.llm-live.yml"]
        # Axes any overlay's `when` references — what an OPTIONAL axis (storage) self-gates on.
        assert config.overlay_when_axes() == {"gcp", "storage", "auth0", "llm"}


def test_posture_overlay_env_override(monkeypatch, tmp_path):
    # An entry's optional `env` names a var that overrides its `file` (the test/CI escape hatch).
    toml = {
        "overlay": [
            {"file": "dev-stack/compose.identity.yml", "when": {"gcp": "sa"}, "env": "FY_OV_TEST"}
        ]
    }
    monkeypatch.setenv("FY_OV_TEST", str(tmp_path / "override.yml"))
    with config.using(_cfg(toml)):
        assert config.matching_overlays({"gcp": "sa"}) == [tmp_path / "override.yml"]
        assert config.matching_overlays({"gcp": "off"}) == []  # `when` unmet → not layered


def test_gcp_storage_staging_requires_sa_identity():
    # storage=staging without gcp=sa cannot function (the swapped endpoints need ADC) — a
    # CONFIG-declared `[[require]]` row now (FULL_TOML carries it, like the real foldyard.toml;
    # the plugin declares neither guard code nor a requires row), still an ERROR carrying the
    # atomic fix; with the identity the combination is clean. Needs the full config bound: the
    # storage axis self-gates on a consumer overlay being wired to it — and the llm plugin
    # loaded, since FULL_TOML's other [[require]] row names the llm axis as its owner.
    from conftest import FULL_TOML, make_config
    from foldyard.plugins import llm as llm_mod

    reg = Registry([gcp.GcpPlugin(), llm_mod.LlmPlugin()], config=make_config(FULL_TOML))
    issues = reg.mode_issues({"gcp": "off", "storage": "staging"})
    assert issues and issues[0][0] == "error" and "gcp=sa storage=staging" in issues[0][1]
    assert reg.mode_issues({"gcp": "sa", "storage": "staging"}) == []
    assert reg.mode_issues({"gcp": "user", "storage": "staging"}) == []  # user ⊇ sa
    assert reg.mode_issues({"gcp": "off", "storage": "local"}) == []


# ── [[require]] — the config tier of Axis.requires ────────────────────────────────────


def test_config_declared_require_rides_the_owning_axis():
    # A `[[require]]` row merges onto its owning axis at Registry construction and evaluates
    # through the SAME core path as an in-code row: synthesized atomic-fix message, satisfied
    # by any `accepts` rung, absent OWNER key reads as the default. `when`/`accepts` take a
    # scalar or a list (overlay-`when` ergonomics).
    toml = {
        "require": [
            {"axis": "demo", "when": "on", "needs": "gcp", "accepts": ["sa", "user"]},
        ]
    }
    reg = Registry([DemoPlugin()], config=_cfg(toml))
    assert reg.mode_issues({"demo": "on"}) == [
        ("error", "demo=on needs gcp=sa/user: `fy mode gcp=sa demo=on` (or drop back to demo=off)")
    ]
    assert reg.mode_issues({"demo": "on", "gcp": "sa"}) == []
    assert reg.mode_issues({"demo": "on", "gcp": "user"}) == []
    assert reg.mode_issues({}) == []


def test_config_require_appends_after_the_plugins_own_rows():
    # In-code (intrinsic) rows evaluate first, config (wiring) rows after — declaration tiers
    # stack rather than replace, so a plugin's own requirement can't be shadowed by config.
    class P(Plugin):
        name = "p"

        def axes(self):
            return [
                Axis(
                    name="a",
                    rungs=("off", "on"),
                    blurb={"off": "", "on": ""},
                    requires=(Requires(when=("on",), axis="x", accepts=("y",)),),
                )
            ]

    toml = {"require": [{"axis": "a", "when": "on", "needs": "z", "accepts": ["w"]}]}
    reg = Registry([P()], config=_cfg(toml))
    msgs = [msg for _, msg in reg.mode_issues({"a": "on"})]
    assert len(msgs) == 2
    assert "x=y" in msgs[0]  # the in-code row
    assert "z=w" in msgs[1]  # the config row, after it


def test_config_require_validation_is_loud():
    # A broken [[require]] declaration fails at Registry construction (the Axis-validation
    # philosophy: loud in development, never a guard that silently stops firing). Unlike an
    # overlay's `when`, an owner axis nobody loaded is a config bug, not an inert row.
    with pytest.raises(ValueError, match="unknown axis 'nope'"):
        Registry(
            [DemoPlugin()],
            config=_cfg({"require": [{"axis": "nope", "when": "on", "needs": "gcp"}]}),
        )
    with pytest.raises(ValueError, match="non-empty subset"):  # `when` outside the owner's rungs
        Registry(
            [DemoPlugin()],
            config=_cfg({"require": [{"axis": "demo", "when": "boom", "needs": "gcp"}]}),
        )
    with pytest.raises(ValueError, match="must name a string"):  # `needs` missing
        Registry([DemoPlugin()], config=_cfg({"require": [{"axis": "demo", "when": "on"}]}))
    with pytest.raises(ValueError, match="'error' or 'warn'"):  # bad severity, via __post_init__
        Registry(
            [DemoPlugin()],
            config=_cfg(
                {"require": [{"axis": "demo", "when": "on", "needs": "gcp", "severity": "fatal"}]}
            ),
        )
    # Non-dict rows are ignored (like [[overlay]]) — junk shapes don't crash the parse.
    Registry([DemoPlugin()], config=_cfg({"require": ["junk", 3]}))


def test_compose_overlays_stack_across_plugins_in_order():
    # The whole point of the overlay LIST: several plugins each contribute, and they STACK in load
    # order (an earlier plugin's overlay is a `-f` base a later one overrides) — not one clobbering
    # the rest the way a single FOLDYARD_COMPOSE_EXTRA scalar did.
    class A(Plugin):
        name = "a"

        def compose_overlays(self, mode):
            return ["/tmp/a.yml"] if mode.get("x") == "on" else []

    class B(Plugin):
        name = "b"

        def compose_overlays(self, mode):
            return ["/tmp/b.yml"] if mode.get("y") == "on" else []

    reg = Registry([A(), B()])
    assert reg.compose_overlays({"x": "on", "y": "on"}) == ["/tmp/a.yml", "/tmp/b.yml"]
    assert reg.compose_overlays({"x": "on", "y": "off"}) == ["/tmp/a.yml"]
    assert reg.compose_overlays({}) == []


def test_gcp_minter_daemon_points_at_the_mint_log(monkeypatch, tmp_path):
    # The daemon tells the minter where to write its per-request log; the panel reads the same path.
    monkeypatch.setattr(config, "log_dir", lambda: tmp_path)
    env = gcp.GcpPlugin().daemons({"gcp": "logs"})["gcp-minter"]["env"]
    assert env["GCP_MINTER_LOG_FILE"] == str(tmp_path / "gcp-minter.jsonl")
    assert str(gcp._minter_log()) == env["GCP_MINTER_LOG_FILE"]  # daemon + panel can't drift


# ── github plugin internals ───────────────────────────────────────────────────────────


def test_github_minter_path_app_vs_user():
    # Both rungs run a PACKAGE module under foldyard's own interpreter, like inject/codex already
    # do. They used to be repo scripts launched via `uv run --script`: the host executed a file
    # inside the mount on every mint (≈hourly), so anything that could write the checkout got code
    # execution as the operator, and PEP 723 resolved deps from the network at mint time. Both holes
    # close by the code being installed — ADR-0023.
    import shlex
    import sys

    app, user = shlex.split(github._minter("app")), shlex.split(github._minter("user"))
    assert app[:3] == [sys.executable, "-m", "foldyard.plugins.github_app_token"]
    assert app[3] == "--own-proxy-port" and app[4] == str(config.proxy_port())
    assert user == [sys.executable, "-m", "foldyard.plugins.gh_cli_token"]
    # The regression worth pinning: never go back to launching a consumer SCRIPT (a file in the
    # mount) — no `uv run --script`, no dev-stack minter path. (A path check against dev_vm_dir
    # can't express this: run-from-source puts the interpreter under the repo too.)
    both = github._minter("app") + github._minter("user")
    assert "run --script" not in both
    assert "gh-app-token" not in both and "gh-user-token" not in both


def test_github_axis_gated_on_the_declared_table(monkeypatch):
    # The registry contract (plugins.load_plugins): CORE plugins are inert until their own config
    # is declared — claude/codex gate on [claude]/[codex].keyless, inject on [[inject]]. github
    # was the one exception ("always-on axis"), so every consumer got a TUI github mode row and
    # gh doctor rows it never asked for. The opt-in is [plugins.github] — any table, even empty
    # (the user emergency needs no App fields).
    monkeypatch.setattr(config, "github_declared", lambda: False)
    assert github.GithubPlugin().axes() == []
    monkeypatch.setattr(config, "github_declared", lambda: True)
    (axis,) = github.GithubPlugin().axes()
    assert axis.name == "github" and axis.rungs == ("off", "app", "user")


def test_github_box_plumbing_absent_for_an_undeclared_consumer(monkeypatch):
    # The ambient in-box plumbing (dummy GH_TOKEN=x, the gh CLI bootstrap) exists so the github
    # axis can flip live — pointless for a consumer with no github axis, and the gh install was
    # a bootstrap step (and failure mode) every proxied box paid for.
    monkeypatch.setattr(config, "github_declared", lambda: False)
    p = github.GithubPlugin()
    assert p.box_args({"FY_PROXY": "h:8088"}) == []
    assert p.box_bootstrap({"FY_PROXY": "h:8088"}) == []


def test_github_box_bootstrap_installs_gh_with_the_proxy_substrate(monkeypatch):
    # gh rides the same ambient gate as the dummy token (FY_PROXY, plus the declared table); the
    # check skips when a consumer image bakes it. A proxy-less consumer's box gets nothing. The
    # install must be IMAGE-AGNOSTIC (a static release binary into /opt/fy-tools, the house
    # bootstrap pattern) — the packaged box is Fedora, so the old `apt-get` run failed on every
    # default box ("apt-get: command not found").
    monkeypatch.setattr(config, "github_declared", lambda: True)
    p = github.GithubPlugin()
    assert p.box_bootstrap({}) == []
    steps = p.box_bootstrap({"FY_PROXY": "h:8088"})
    assert steps[0]["check"] == "command -v gh"
    assert "apt-get" not in steps[0]["run"] and "dnf" not in steps[0]["run"]
    assert "github.com/cli/cli/releases" in steps[0]["run"]
    assert "/opt/fy-tools/bin/gh" in steps[0]["run"]


def test_github_env_defaults_only_for_app_rung(monkeypatch):
    monkeypatch.setattr(config, "github_app_id", lambda: "1234567")
    monkeypatch.setattr(config, "github_installation_id", lambda: "12345678")
    monkeypatch.setattr(config, "github_repo", lambda: "Tangible")
    monkeypatch.setattr(config, "github_permissions", lambda: "")
    # The GSM pair is NOT derived any more: the packaged minter reads the PEM from host.env and
    # never talks to a vault, so those names are only the capture HINT's text now.
    assert github.GithubPlugin().env_defaults({"github": "app"}) == {
        "GH_APP_ID": "1234567",
        "GH_INSTALLATION_ID": "12345678",
        "GH_REPO": "Tangible",
    }
    # A declared `[plugins.github].permissions` rides along so a consumer can NARROW the token.
    monkeypatch.setattr(config, "github_permissions", lambda: '{"issues": "read"}')
    assert github.GithubPlugin().env_defaults({"github": "app"})["GH_APP_PERMISSIONS"] == (
        '{"issues": "read"}'
    )
    # The gh-cli rung needs none of these; off contributes nothing either.
    assert github.GithubPlugin().env_defaults({"github": "user"}) == {}
    assert github.GithubPlugin().env_defaults({"github": "off"}) == {}


def test_github_env_defaults_omits_unresolved_keys(monkeypatch):
    # An unconfigured value (e.g. no [plugins.github].permissions) resolves to "" — never emitted,
    # so setdefault never overwrites an empty string over something meaningful.
    monkeypatch.setattr(config, "github_app_id", lambda: "1234567")
    monkeypatch.setattr(config, "github_installation_id", lambda: "12345678")
    monkeypatch.setattr(config, "github_repo", lambda: "Tangible")
    monkeypatch.setattr(config, "github_permissions", lambda: "")
    assert github.GithubPlugin().env_defaults({"github": "app"}) == {
        "GH_APP_ID": "1234567",
        "GH_INSTALLATION_ID": "12345678",
        "GH_REPO": "Tangible",
    }


def test_github_app_rule_never_requires_the_pem():
    # `requires` is the supervisor's spawn gate for the WHOLE proxy daemon, and under Phase A'
    # always-route a proxy that refuses to launch connection-refuses EVERY box request. So a
    # missing credential must degrade one host (the addon logs the mint failure), never all egress:
    # the PEM is deliberately absent from `requires`, and presence is surfaced by the doctor row +
    # the `secrets` capture prompt instead.
    (rule,) = github.GithubPlugin().proxy_rules({"github": "app"})
    assert rule.requires == ("GH_APP_ID", "GH_INSTALLATION_ID", "GH_REPO")
    assert not any("PEM" in r for r in rule.requires)
    # …while the minter's OWN env (what it may read) does carry the key.
    assert "GH_PEM_B64" in rule.env
    # The user emergency injects your own gh token — no minter env at all.
    (rule,) = github.GithubPlugin().proxy_rules({"github": "user"})
    assert rule.requires == ()


def _pem_rows(monkeypatch, tmp_path, *, host_env_body: str | None = None, deep: bool = False):
    """github's PEM doctor rows for a given host.env content. No gcloud anywhere: the check is
    PRESENCE (+ an offline shape check), so it works in the box, needs no --deep, and can't be
    broken by a lapsed PAM grant."""
    host_env = tmp_path / "host.env"
    if host_env_body is not None:
        host_env.write_text(host_env_body)
    monkeypatch.setattr(config, "host_env_file", lambda: host_env)
    monkeypatch.delenv("GH_PEM_B64", raising=False)
    # These tests model an App consumer: the table is declared (the whole-hook gate) and an
    # identity value resolves (the App-intent gate for the app rows — see
    # test_github_doctor_gh_rows_only_for_a_declared_table_without_app_config).
    monkeypatch.setattr(config, "github_declared", lambda: True)
    monkeypatch.setattr(config, "github_app_id", lambda: "1234567")
    ctx = DoctorContext(
        deep=deep,
        run=lambda cmd, timeout=None: pytest.fail(f"the PEM check must not shell out: {cmd}"),
        which=lambda tool: False,
        result=devmode._result,
        probe=lambda _port: False,
    )
    return [row for row in github.GithubPlugin().doctor_checks(ctx) if "PEM" in row[1]]


def test_github_doctor_silent_for_an_undeclared_consumer(monkeypatch, tmp_path):
    # A consumer with no [plugins.github] gets NO github doctor rows at all — not the app-key/PEM
    # nags (the old bug, two ✗ rows forever) and not the gh CLI/login rows either: with the axis
    # gated on the same declaration there is no rung the gh checks could ever serve.
    host_env = tmp_path / "host.env"
    monkeypatch.setattr(config, "host_env_file", lambda: host_env)
    monkeypatch.setattr(config, "github_declared", lambda: False)
    for var in ("GH_PEM_B64", "GH_APP_ID", "GH_INSTALLATION_ID", "GH_REPO"):
        monkeypatch.delenv(var, raising=False)
    ctx = DoctorContext(
        deep=False,
        run=lambda cmd, timeout=None: (1, ""),
        which=lambda tool: False,
        result=devmode._result,
        probe=lambda _port: False,
    )
    assert list(github.GithubPlugin().doctor_checks(ctx)) == []


def test_github_doctor_gh_rows_only_for_a_declared_table_without_app_config(monkeypatch, tmp_path):
    # A bare `[plugins.github]` table (a user-emergency-only consumer): the gh CLI/login rows
    # appear, but the App rows still need App INTENT — a resolved identity value or a captured
    # PEM — so declaring the table alone never buys an eternal "github=app keys missing" nag.
    host_env = tmp_path / "host.env"
    monkeypatch.setattr(config, "host_env_file", lambda: host_env)
    monkeypatch.setattr(config, "github_declared", lambda: True)
    for var in ("GH_PEM_B64", "GH_APP_ID", "GH_INSTALLATION_ID", "GH_REPO"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(config, "github_app_id", lambda: "")
    monkeypatch.setattr(config, "github_installation_id", lambda: "")
    monkeypatch.setattr(config, "github_repo", lambda: "")
    ctx = DoctorContext(
        deep=False,
        run=lambda cmd, timeout=None: (1, ""),
        which=lambda tool: False,
        result=devmode._result,
        probe=lambda _port: False,
    )
    rows = list(github.GithubPlugin().doctor_checks(ctx))
    assert [r for r in rows if r[1] == "gh CLI"]
    assert not [r for r in rows if "github=app" in r[1]]


_PEM_BODY = "-----BEGIN RSA PRIVATE KEY-----\nabc\n-----END RSA PRIVATE KEY-----\n"


def test_github_pem_doctor_reports_absence_with_the_hint(monkeypatch, tmp_path):
    rows = _pem_rows(monkeypatch, tmp_path, host_env_body="")
    assert [status for status, *_ in rows] == ["fail"]
    # The fix text is the consumer's `how` hint — printed for the operator to run, never executed.
    assert "fy box up" in rows[0][2]


def test_github_pem_doctor_accepts_a_captured_key_and_flags_a_broken_one(monkeypatch, tmp_path):
    import base64

    good = base64.b64encode(_PEM_BODY.encode()).decode()
    rows = _pem_rows(monkeypatch, tmp_path, host_env_body=f"GH_PEM_B64={good}\n")
    assert [status for status, *_ in rows] == ["ok", "ok"]  # present + shape
    # A truncated/half-pasted key is caught OFFLINE rather than as an opaque 401 at mint time.
    rows = _pem_rows(monkeypatch, tmp_path, host_env_body="GH_PEM_B64=-----BEGIN RSA PRIV\n")
    assert [status for status, *_ in rows] == ["ok", "fail"]


# ── declared secrets (plugins.Secret: presence, not provenance) ────────────────────────


def test_github_declares_its_pem_secret_only_on_the_app_rung(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "host_env_file", lambda: tmp_path / "host.env")
    monkeypatch.delenv("GH_PEM_B64", raising=False)
    p = github.GithubPlugin()
    assert p.secrets({"github": "off"}) == []  # an inactive mechanism prompts for nothing
    assert p.secrets({"github": "user"}) == []  # the gh-cli kind needs no stored secret
    (secret,) = p.secrets({"github": "app"})
    assert secret.var == "GH_PEM_B64" and secret.b64 is True
    assert "PRIVATE KEY" in secret.pattern


def test_github_pem_hint_defaults_generic_and_yields_to_the_consumer(
    fresh_config, monkeypatch, tmp_path
):
    # The hint is a STRING foldyard prints, never a command it runs — the plugin's default names the
    # App settings (every consumer has them), and a consumer whose key lives in a vault retargets it
    # with a `[[secret]]` row. Nothing here makes any vault CLI a dependency of github=app (that
    # coupling put a PAM-elevatable identity in front of posting a PR comment).
    fresh_config(FOLDYARD_REPO=tmp_path)
    (secret,) = github.GithubPlugin().secrets({"github": "app"})
    assert "App settings" in secret.how and "base64" in secret.how
    # The capture prompt gets the override from Registry.secrets; the doctor rows read plugins
    # directly, so they apply it themselves — one hint, wherever the operator looks.
    (tmp_path / "foldyard.toml").write_text(
        '[[secret]]\nvar = "GH_PEM_B64"\nhow = "vault read -field=pem secret/gh | base64"\n'
    )
    fresh_config(FOLDYARD_REPO=tmp_path)
    rows = _pem_rows(monkeypatch, tmp_path, host_env_body="")
    assert "vault read -field=pem secret/gh" in rows[0][2]


def test_registry_secrets_merges_plugins_with_declared_rows(fresh_config, tmp_path):
    (tmp_path / "foldyard.toml").write_text(
        "[[secret]]\n"
        'var = "PENPOT_TOKEN"\n'
        'label = "Penpot user token"\n'
        'how = "op read op://vault/penpot/token"\n'
        'when = { penpot = "on" }\n'
    )
    fresh_config(FOLDYARD_REPO=tmp_path)
    reg = Registry([])
    # `when` gates a declared row exactly like an [[overlay]] — one rule for the whole config.
    assert reg.secrets({"penpot": "off"}) == []
    (secret,) = reg.secrets({"penpot": "on"})
    assert secret.var == "PENPOT_TOKEN" and secret.how.startswith("op read")
    assert secret.b64 is False and secret.pattern == ""


def test_declared_secret_overrides_a_plugins_own(fresh_config, tmp_path):
    # A consumer retargets a plugin's hint (different vault, different wording) by declaring the
    # same var — never by editing plugin code.
    (tmp_path / "foldyard.toml").write_text(
        '[[secret]]\nvar = "GH_PEM_B64"\nlabel = "our PEM"\nhow = "ask ops"\nbase64 = true\n'
    )
    fresh_config(FOLDYARD_REPO=tmp_path)
    reg = Registry([github.GithubPlugin()])
    secrets = {s.var: s for s in reg.secrets({"github": "app"})}
    assert list(secrets) == ["GH_PEM_B64"]  # merged, not duplicated
    assert secrets["GH_PEM_B64"].how == "ask ops" and secrets["GH_PEM_B64"].label == "our PEM"
    # …and the fields it DIDN'T name are inherited, not blanked: retargeting a hint must not
    # silently drop the plugin's shape check (which is what re-declaring `pattern` by hand would
    # do the day the plugin tightens it).
    assert secrets["GH_PEM_B64"].pattern == github._PEM_PATTERN


@pytest.mark.parametrize(
    "row",
    [
        '[[secret]]\nlabel = "nameless"\n',  # no var at all
        '[[secret]]\nvar = ""\n',  # empty
        '[[secret]]\nvar = "NOT A NAME"\n',  # spaces
        '[[secret]]\nvar = "GOOD=EVIL\\nALSO_EVIL"\n',  # would append EXTRA host.env lines
    ],
)
def test_declared_secret_var_must_be_an_env_name(fresh_config, tmp_path, row):
    # `var` is written verbatim as a host.env KEY, one `KEY=value` per line — so anything outside an
    # env identifier is unreadable by the supervisor's parser or, with a newline, a way to append
    # entries. Loud at Registry construction, like a malformed [[require]].
    (tmp_path / "foldyard.toml").write_text(row)
    fresh_config(FOLDYARD_REPO=tmp_path)
    with pytest.raises(ValueError, match="environment-variable name"):
        Registry([]).secrets({})


# ── doctor checks + one-click fixes (proxy owns mitmproxy/CA; github owns gh) ──────────


def test_proxy_owns_mitmproxy_and_ca_doctor_checks_not_github(monkeypatch):
    monkeypatch.setattr(config, "github_declared", lambda: True)  # a github consumer
    ctx = _ctx()
    proxy_checks = [name for _, name, _ in proxy.ProxyPlugin().doctor_checks(ctx)]
    github_checks = [name for _, name, _ in github.GithubPlugin().doctor_checks(ctx)]
    # the proxy framework owns these now — github is just one injector that rides it
    assert "mitmproxy" in proxy_checks and "mitm CA" in proxy_checks
    assert "mitmproxy" not in github_checks and "mitm CA" not in github_checks
    assert "gh CLI" in github_checks  # github keeps its own credential-mechanism checks


def _injection_rows(monkeypatch, *, mode: str = "app", headers: str = "", rc: int = 0):
    """github's BOX-side injection rows against a canned `curl -D -` response."""
    monkeypatch.setattr(github, "_box_github_mode", lambda _env: mode)
    ctx = DoctorContext(
        deep=False,
        run=lambda cmd, timeout=None: (rc, headers),
        which=lambda _tool: True,
        result=devmode._result,
        probe=lambda _port: False,
    )
    return [row for row in github.GithubPlugin().box_doctor_checks(ctx) if row[0] != "running"]


def test_box_doctor_detects_that_injection_is_not_actually_happening(monkeypatch):
    """
    The gap this check exists to close.

    When the host-side mint broke, every state-reading signal stayed green — `fy mode` showed
    `github app`, the proxy daemon showed up, `fy doctor` was ALL PASS — because each is a true
    statement about host-side configuration. The wire told a different story: 401s from `gh`.
    GitHub's anonymous rate limit (60/h) vs an authenticated one is the cheapest witness.
    """
    (status, name, detail) = _injection_rows(monkeypatch, headers="x-ratelimit-limit: 60\r\n")[0]
    assert name == "github injection"
    assert status == "fail"
    assert "NOT injected" in detail
    assert "fy host" in detail, "a failing check has to say what to do about it"


def test_box_doctor_passes_when_the_app_token_is_reaching_requests(monkeypatch):
    rows = _injection_rows(monkeypatch, headers="HTTP/2 200\r\nx-ratelimit-limit: 5000\r\n")
    assert [(s, n) for s, n, _ in rows] == [("ok", "github injection")]


def test_box_doctor_skips_the_injection_probe_when_github_is_off(monkeypatch):
    """No network call in a mode that injects nothing — doctor stays fast and offline."""
    assert _injection_rows(monkeypatch, mode="off") == []
    assert _injection_rows(monkeypatch, mode="user") == []


def test_box_doctor_reports_unreachable_rather_than_guessing(monkeypatch):
    (status, _, detail) = _injection_rows(monkeypatch, rc=6, headers="")[0]
    assert status == "fail"
    assert "couldn't reach" in detail


def test_dump_doctor_checks_and_fixes(monkeypatch, tmp_path):
    # The dump axis serves the Auth0 sim over HTTPS; mkcert (+ nss's certutil) + a generated cert
    # make it locally trusted. All WARN, not fail: dump-browse still works (click through the cert),
    # so these must never block `foldyard doctor`/verify.
    monkeypatch.setattr(config, "repo_root", lambda: tmp_path)
    # The plugin reads the harness dir from config now (was a hardcoded constant) — pin it so the
    # unit test doesn't depend on the real foldyard.toml / lru_cache ordering.
    sim_rel = "apps/web/tests/auth0-simulator"
    monkeypatch.setattr(config, "auth0_sim_dir", lambda: sim_rel)
    cert = tmp_path / sim_rel / ".certs-local/localhost.pem"

    def rows(present):
        ctx = DoctorContext(
            deep=False,
            run=devmode._run,
            which=lambda c: c in present,
            result=devmode._result,
            probe=lambda _p: False,
        )
        return {n: s for s, n, _ in auth0_sim.Auth0SimPlugin().doctor_checks(ctx)}

    # nothing installed + no cert → all three warn
    assert rows(set()) == {"mkcert": "warn", "mkcert nss": "warn", "sim cert": "warn"}
    # libs present but cert still missing → "sim cert" stays warn (this is what bit the first run)
    assert rows({"mkcert", "certutil"})["sim cert"] == "warn"
    # generate the cert file → "sim cert" goes ok
    cert.parent.mkdir(parents=True)
    cert.write_text("x")
    assert rows({"mkcert", "certutil"}) == {"mkcert": "ok", "mkcert nss": "ok", "sim cert": "ok"}

    # one fix per check; all NON-INTERACTIVE (no sudo / no `mkcert -install` in a button)
    fixes = {f.check: f for f in auth0_sim.Auth0SimPlugin().doctor_fixes()}
    assert set(fixes) == {"mkcert", "mkcert nss", "sim cert"}
    assert fixes["mkcert"].cmd[:2] == ["brew", "install"]
    gen = " ".join(fixes["sim cert"].cmd)
    assert "mkcert -install" not in gen  # never the sudo trust step in a button
    assert "localhost" in gen  # generates the cert
    assert "restart" in gen  # …and bounces the sim so it actually serves it (the footgun)


def test_auth0_axis_pins_no_recipe_env():
    # The auth0 overlays are config-only [[overlay]] entries now (their layering + the auth0=real
    # ∧ gcp=sa gating is covered in test_posture_overlays_*). What stays plugin-side: the axis pins
    # NO recipe env — the sim's published port is [ports].SIM_PORT (per-worktree), templated INSIDE
    # the overlay, never a derive_env pin that would force every worktree onto one host port.
    plugin = auth0_sim.Auth0SimPlugin()
    assert plugin.derive_env({"auth0": "sim"}) == {}
    assert plugin.derive_env({"auth0": "real"}) == {}


def test_auth0_sim_with_staging_storage_is_a_warn_not_error():
    # The auth0=real ∧ gcp=sa overlay gating lives in [[overlay]].when now (test_posture_overlays_*).
    # What stays plugin-side: sim login (local dump DB) with storage=staging (REAL buckets) is a
    # mixed-data-planes hybrid — a WARN, never an error.
    plugin = auth0_sim.Auth0SimPlugin()
    issues = list(plugin.mode_issues({"auth0": "sim", "storage": "staging"}))
    assert issues and issues[0][0] == "warn"
    assert list(plugin.mode_issues({"auth0": "sim", "storage": "local"})) == []


# ── llm plugin (the llm axis: mock/cassettes vs real Vertex; Tangible-bound like auth0-sim) ──


def test_llm_axis_gated_on_declared_table(monkeypatch):
    from foldyard.plugins import llm as llm_mod

    monkeypatch.setattr(config, "llm_declared", lambda: False)
    assert llm_mod.LlmPlugin().axes() == []
    monkeypatch.setattr(config, "llm_declared", lambda: True)
    (axis,) = llm_mod.LlmPlugin().axes()
    assert axis.name == "llm" and axis.rungs == ("off", "record", "live")
    assert axis.default == "off"


def test_llm_requires_sa_identity():
    # Real LLM traffic consumes the ADC that only gcp=sa grants — refused otherwise via the
    # CONSUMER's `[[require]]` row (Tangible's wiring routes record/live through Vertex; the
    # plugin itself is provider-agnostic and declares no requirement at all): an ERROR carrying
    # the full fix, evaluated by the registry; clean under gcp=sa, and under gcp=user (a
    # superset of sa — escalation never strands llm).
    from conftest import make_config
    from foldyard.plugins import llm as llm_mod

    # The same row Tangible's foldyard.toml (and conftest.FULL_TOML) declares.
    toml = {
        "plugins": {"llm": {}},
        "require": [
            {
                "axis": "llm",
                "when": ["record", "live"],
                "needs": "gcp",
                "accepts": ["sa", "user"],
                "reason": "the runtime-SA identity",
            }
        ],
    }
    with config.using(make_config(toml)):
        (axis,) = llm_mod.LlmPlugin().axes()
    assert axis.requires == ()  # the PLUGIN declares nothing — the coupling is config
    reg = Registry([llm_mod.LlmPlugin()], config=make_config(toml))
    issues = reg.mode_issues({"llm": "record", "gcp": "off"})
    assert issues and issues[0][0] == "error" and "gcp=sa llm=record" in issues[0][1]
    assert reg.mode_issues({"llm": "live", "gcp": "sa"}) == []
    assert reg.mode_issues({"llm": "live", "gcp": "user"}) == []
    assert reg.mode_issues({"llm": "off", "gcp": "off"}) == []
    # An ABSENT identity axis (gcp not loaded at all) is still the error — the requirement
    # is unmet either way (the Requires absence semantics).
    assert [sev for sev, _ in reg.mode_issues({"llm": "record"})] == ["error"]
    # …while an absent OWNER key reads as the axis's default (llm=off), like everywhere else
    # in the posture substrate — no requirement fires.
    assert reg.mode_issues({}) == []


def test_registry_mode_issues_merges_across_plugins():
    # Config-declared `[[require]]` rows (FULL_TOML's llm→gcp + storage→gcp) AND the hook
    # issues (auth0's combination warn) all land in one merged list. Full config bound so the
    # gcp/storage/llm axes exist (they self-gate on the gcp project / a storage overlay / the
    # [plugins.llm] table).
    from conftest import FULL_TOML, make_config
    from foldyard.plugins import llm as llm_mod

    reg = Registry(
        [gcp.GcpPlugin(), auth0_sim.Auth0SimPlugin(), llm_mod.LlmPlugin()],
        config=make_config(FULL_TOML),
    )
    issues = reg.mode_issues({"gcp": "off", "storage": "staging", "llm": "record", "auth0": "sim"})
    severities = [sev for sev, _ in issues]
    assert severities.count("error") == 2  # storage needs sa + llm needs sa ([[require]] rows)
    assert severities.count("warn") == 1  # auth0=sim × storage=staging (the hook)


def test_proxy_running_doctor_check_uses_the_port_probe():
    # The runtime "egress proxy" check FAILs when the port probe says down (Phase A′ always-routes,
    # so a down proxy is why box requests hang) and OKs when it's up.
    down = {
        name: status
        for status, name, _ in proxy.ProxyPlugin().doctor_checks(_ctx(probe=lambda _p: False))
    }
    up = {
        name: status
        for status, name, _ in proxy.ProxyPlugin().doctor_checks(_ctx(probe=lambda _p: True))
    }
    assert down["egress proxy"] == "fail" and up["egress proxy"] == "ok"


def test_doctor_fixes_match_their_checks_and_are_runnable(monkeypatch):
    monkeypatch.setattr(config, "github_declared", lambda: True)  # a github consumer's fixes
    reg = Registry([gcp.GcpPlugin(), github.GithubPlugin(), proxy.ProxyPlugin()])
    fixes = {f.check: f for f in reg.doctor_fixes()}
    # each fix repairs a real check name, and the commands are the non-interactive ones
    assert set(fixes) == {"gh CLI", "mitmproxy", "mitm CA"}
    assert fixes["mitmproxy"].cmd[:3] == ["uv", "tool", "install"]
    assert "--editable" in fixes["mitmproxy"].cmd  # reinstall foldyard (mitmproxy is a core dep)
    assert fixes["gh CLI"].cmd == ["brew", "install", "gh"]
    # the CA fix shells the venv python at mitmproxy's CertStore — no server, no port
    assert fixes["mitm CA"].cmd[1] == "-c" and "CertStore.create_store" in fixes["mitm CA"].cmd[2]


# ── box_args (the box env/mounts hooks, ADR-0015) ────────────────────────────────────
# github contributes only the dummy GH_TOKEN; the CA mount + proxy env are the proxy plugin's.


def test_github_box_args_no_proxy_is_empty(monkeypatch):
    # A proxy-less consumer's box stays clean — no dummy to ride a proxy that isn't there.
    monkeypatch.setattr(config, "github_declared", lambda: True)
    assert github.GithubPlugin().box_args({"PODMAN_PROJECT": "p"}) == []


def test_github_box_args_proxy_adds_only_the_dummy_token(monkeypatch):
    # The dummy GH_TOKEN=x is AMBIENT with the proxy substrate (FY_PROXY), NOT gated on a github
    # rung — pre-positioned like the ambient CA so `fy mode github=app` flips live with no box
    # recreate (it does need the declared [plugins.github] table, like every github hook now).
    # It grants nothing: the host proxy's inject rule is the sole access gate, and the real
    # token never enters the box.
    monkeypatch.setattr(config, "github_declared", lambda: True)
    assert github.GithubPlugin().box_args({"FY_PROXY": "h:8088"}) == ["-e", "GH_TOKEN=x"]
    assert github.GithubPlugin().box_args({"FY_PROXY": "h:8088", "GH_INJECT": "app"}) == [
        "-e",
        "GH_TOKEN=x",
    ]


def test_proxy_box_args_mounts_ca_and_proxy_env(monkeypatch, tmp_path):
    ca = tmp_path / "ca.pem"
    ca.write_text("CERT")
    monkeypatch.setenv("MITMPROXY_CA", str(ca))
    args = proxy.ProxyPlugin().box_args({"FY_PROXY": "h:8088", "PODMAN_PROJECT": "tangible-podman"})
    # CA trust (ambient part) — mount + the ADDITIVE NODE_EXTRA_CA_CERTS
    assert f"{ca}:/etc/dev-proxy-ca.pem:ro" in args
    assert "NODE_EXTRA_CA_CERTS=/etc/dev-proxy-ca.pem" in args
    # routing (always-route part) — proxy env + the BUNDLE-REPLACING vars pointed at the COMBINED
    # bundle (system roots + mitm CA), so capture=off TLS-passthrough's real certs still verify.
    assert "HTTPS_PROXY=http://h:8088" in args
    # Loopback is unconditional; nothing else is hardcoded (see the no_proxy tests below).
    assert any("NO_PROXY=" in a and "localhost,127.0.0.1" in a for a in args)
    assert "REQUESTS_CA_BUNDLE=/etc/dev-proxy-ca-combined.pem" in args
    assert "GIT_SSL_CAINFO=/etc/dev-proxy-ca-combined.pem" in args
    assert (
        "SSL_CERT_FILE=/etc/dev-proxy-ca-combined.pem" in args
    )  # OpenSSL/uv/curl trust the proxy CA
    assert "GH_TOKEN=x" not in args  # the dummy token is github's box_args, not the proxy's


def _no_proxy_value(args: list[str]) -> str:
    return next(a.split("=", 1)[1] for a in args if a.startswith("NO_PROXY="))


def test_no_proxy_is_loopback_plus_plugins_plus_consumer(fresh_config, monkeypatch, tmp_path):
    # The three sources, and the point of the split: foldyard names NO consumer service. The
    # consumer's list carries {project}, which expands to the compose project name so ONE entry
    # covers every worktree's container-name prefix.
    ca = tmp_path / "ca.pem"
    ca.write_text("CERT")
    monkeypatch.setenv("MITMPROXY_CA", str(ca))
    (tmp_path / "foldyard.toml").write_text(
        '[proxy]\nno_proxy = ["{project}-postgres", "redis"]\n'
        '[plugins.gcp-metadata]\nproject = "acme-staging"\n'
    )
    fresh_config(FOLDYARD_REPO=tmp_path)
    reg = Registry([proxy.ProxyPlugin(), gcp.GcpPlugin()])
    args = reg.box_args({"FY_PROXY": "h:8088", "PODMAN_PROJECT": "acme-podman"})
    hosts = _no_proxy_value(args).split(",")
    assert hosts[:2] == ["localhost", "127.0.0.1"]  # core, unconditional
    assert "metadata-emulator" in hosts  # the gcp plugin's OWN service, via the hook
    assert "acme-podman-postgres" in hosts  # {project} expanded
    assert "redis" in hosts


def test_no_proxy_omits_an_unconfigured_plugins_service(fresh_config, monkeypatch, tmp_path):
    # The hook is CONFIG-gated, not mode-gated: no [plugins.gcp-metadata] ⇒ no emulator to bypass.
    # (Mode-gating would be worse than useless — NO_PROXY is baked at box create, so a bypass that
    # appeared only on an active rung would be missing from a box created while the rung was off.)
    ca = tmp_path / "ca.pem"
    ca.write_text("CERT")
    monkeypatch.setenv("MITMPROXY_CA", str(ca))
    (tmp_path / "foldyard.toml").write_text("[proxy]\n")
    fresh_config(FOLDYARD_REPO=tmp_path)
    reg = Registry([proxy.ProxyPlugin(), gcp.GcpPlugin()])
    args = reg.box_args({"FY_PROXY": "h:8088", "PODMAN_PROJECT": "p"})
    assert _no_proxy_value(args) == "localhost,127.0.0.1"


def test_no_proxy_refuses_a_dotted_entry(fresh_config, tmp_path):
    # Repo config, so the box can write it — and NO_PROXY is a STRONGER exemption than
    # [proxy] passthrough: a bypassed host never reaches the proxy, so it is neither captured nor
    # refused by default_deny. Stack names are single DNS labels, so refusing dots admits every
    # real use and structurally excludes every public host.
    (tmp_path / "foldyard.toml").write_text('[proxy]\nno_proxy = ["api.anthropic.com"]\n')
    fresh_config(FOLDYARD_REPO=tmp_path)
    with pytest.raises(SystemExit, match="may not contain dotted names"):
        config.proxy_no_proxy()


def test_proxy_box_args_ambient_ca_without_routing(monkeypatch, tmp_path):
    # AMBIENT CA: the CA exists but no proxy mode set FY_PROXY → mount + additively trust it, but
    # DON'T route and DON'T set the bundle-replacing vars (they'd break every non-proxied HTTPS).
    ca = tmp_path / "ca.pem"
    ca.write_text("CERT")
    monkeypatch.setenv("MITMPROXY_CA", str(ca))
    args = proxy.ProxyPlugin().box_args({"PODMAN_PROJECT": "p"})
    assert f"{ca}:/etc/dev-proxy-ca.pem:ro" in args
    assert "NODE_EXTRA_CA_CERTS=/etc/dev-proxy-ca.pem" in args
    assert not any("HTTPS_PROXY" in a for a in args)  # no routing
    assert not any(  # bundle-replacing vars are routing-only, never ambient
        "REQUESTS_CA_BUNDLE" in a or "GIT_SSL_CAINFO" in a or "SSL_CERT_FILE" in a for a in args
    )


def test_proxy_box_args_off_is_empty(monkeypatch, tmp_path):
    # No CA on the Mac AND no routing → nothing (point MITMPROXY_CA at a missing file: the default
    # ~/.mitmproxy CA may actually exist on a dev machine, which would otherwise add ambient args).
    monkeypatch.setenv("MITMPROXY_CA", str(tmp_path / "nope.pem"))
    assert proxy.ProxyPlugin().box_args({"PODMAN_PROJECT": "p"}) == []


def test_proxy_box_args_missing_ca_blocks_box_up(monkeypatch, tmp_path):
    monkeypatch.setenv("MITMPROXY_CA", str(tmp_path / "nope.pem"))
    with pytest.raises(SystemExit, match="no CA"):
        proxy.ProxyPlugin().box_args({"FY_PROXY": "h:8088", "PODMAN_PROJECT": "p"})


def test_stage_ca_copies_under_checkout_and_repoints_env(monkeypatch, tmp_path):
    # The machine mounts only repo+worktrees, so stage_ca_for_box copies the CA under the checkout
    # (VM-visible) and re-points MITMPROXY_CA there, so box_args mounts a bind-able source.
    src = tmp_path / "mitm" / "mitmproxy-ca-cert.pem"
    src.parent.mkdir()
    src.write_text("CERT")
    monkeypatch.setenv("MITMPROXY_CA", str(src))
    checkout = tmp_path / "repo"
    checkout.mkdir()
    staged = proxy.stage_ca_for_box(str(checkout), "dev-stack")
    assert staged is not None
    assert staged == checkout / "dev-stack/.devbox-ca/mitmproxy-ca-cert.pem"
    assert staged.read_text() == "CERT"
    assert os.environ["MITMPROXY_CA"] == str(staged)
    # idempotent: a re-run (MITMPROXY_CA now points at the staged copy) must not self-copy/throw
    assert proxy.stage_ca_for_box(str(checkout), "dev-stack") == staged


def test_stage_ca_noop_without_ca(monkeypatch, tmp_path):
    monkeypatch.setenv("MITMPROXY_CA", str(tmp_path / "nope.pem"))
    assert proxy.stage_ca_for_box(str(tmp_path / "repo"), "dev-stack") is None


def test_gcp_box_args_empty_without_project(monkeypatch):
    # Gated on `[plugins.gcp-metadata].project` — a generic consumer that hasn't configured gcp
    # gets NO box wiring (no malformed `…@.iam…` label, no emulator host its stack lacks).
    monkeypatch.setattr(config, "gcp_project", lambda: "")
    assert gcp.GcpPlugin().box_args({"PODMAN_PROJECT": "p"}) == []


def test_gcp_box_args_are_mode_independent(monkeypatch):
    # The box wiring no longer depends on the mode (no more `user` relabel) — so switching the gcp
    # rung never re-bakes the box. Always (when gcp is CONFIGURED): the read-only SA label, the
    # userEscalatable marker, and the emulator host. The rung is served by the live Mac minter.
    monkeypatch.setattr(config, "gcp_project", lambda: "p")  # gcp configured → wiring emitted
    monkeypatch.setenv("DEVBOX_LOG_SA", "box@p.iam.gserviceaccount.com")
    expected = [
        "--label",
        "gcp.serviceAccount=box@p.iam.gserviceaccount.com",
        "--label",
        "gcp.userEscalatable=1",
        # GCE_METADATA_HOST for the google-auth libraries, GCE_METADATA_ROOT for the gcloud CLI
        # (the box runs gcloud), GCE_METADATA_IP for python google-auth's ADC-detection ping
        # (which ignores HOST) — all three point at the emulator.
        "-e",
        "GCE_METADATA_HOST=metadata-emulator:80",
        "-e",
        "GCE_METADATA_ROOT=metadata-emulator:80",
        "-e",
        "GCE_METADATA_IP=metadata-emulator:80",
    ]
    # identical for off and user — the args carry no rung information
    assert gcp.GcpPlugin().box_args({}) == expected
    assert gcp.GcpPlugin().box_args({"GCP_METADATA_HOST": "x", "DEVBOX_SA": "user"}) == expected


def test_gcp_stage_assets_stages_server_py_when_active(tmp_path, monkeypatch):
    # The metadata emulator container bind-mounts server.py, but the machine mounts only the repo —
    # so a configured + active gcp rung stages the packaged server.py into the VM-visible
    # .devbox-foldyard dir for compose to mount.
    monkeypatch.setattr(config, "gcp_project", lambda: "p")  # configured
    gcp.GcpPlugin().stage_assets({"gcp": "sa"}, str(tmp_path), "dev-stack")
    staged = tmp_path / "dev-stack" / ".devbox-foldyard" / "server.py"
    assert staged.read_bytes() == (gcp.METADATA_DIR / "server.py").read_bytes()


def test_gcp_stage_assets_noop_when_off_or_unconfigured(tmp_path, monkeypatch):
    staged = tmp_path / "dev-stack" / ".devbox-foldyard" / "server.py"
    monkeypatch.setattr(config, "gcp_project", lambda: "p")
    gcp.GcpPlugin().stage_assets(
        {"gcp": "off"}, str(tmp_path), "dev-stack"
    )  # rung off ⇒ no emulator
    assert not staged.exists()
    monkeypatch.setattr(config, "gcp_project", lambda: "")  # unconfigured ⇒ no emulator either
    gcp.GcpPlugin().stage_assets({"gcp": "sa"}, str(tmp_path), "dev-stack")
    assert not staged.exists()


def test_gcp_stage_assets_stages_when_profile_forced_via_env(tmp_path, monkeypatch):
    # The documented manual path (`COMPOSE_PROFILES=metadata fy up`, README/justfile) starts the
    # emulator even with the persisted mode gcp=off — explicit env wins. Staging must follow the
    # profile, not just the mode, so the bind-mounted server.py is never missing.
    monkeypatch.setattr(config, "gcp_project", lambda: "p")  # configured
    monkeypatch.setenv("COMPOSE_PROFILES", "metadata")
    gcp.GcpPlugin().stage_assets({"gcp": "off"}, str(tmp_path), "dev-stack")
    staged = tmp_path / "dev-stack" / ".devbox-foldyard" / "server.py"
    assert staged.read_bytes() == (gcp.METADATA_DIR / "server.py").read_bytes()


def test_gcp_user_mode_is_minter_driven_not_a_box_relabel():
    # user mode used to set DEVBOX_SA="user" (relabel ⇒ box restart); now the escalation is the
    # live minter's job, so derive_env carries no box-relabel and the minter is told to allow it.
    env = gcp.GcpPlugin().derive_env({"gcp": "user"})
    assert "DEVBOX_SA" not in env and env["GCP_METADATA_HOST"] == "metadata-emulator:80"
    spec = gcp.GcpPlugin().daemons({"gcp": "user"})["gcp-minter"]
    assert spec["env"]["GCP_ALLOW_USER_TOKEN"] == "1"
    # short refresh window ⇒ a running box picks up the mode change (and off revokes) within it
    assert spec["env"]["GCP_TOKEN_REFRESH"] == str(gcp.TOKEN_REFRESH)


def test_registry_box_args_merges(monkeypatch, tmp_path):
    ca = tmp_path / "ca.pem"
    ca.write_text("x")
    monkeypatch.setenv("MITMPROXY_CA", str(ca))
    monkeypatch.setenv("DEVBOX_LOG_SA", "box@p.iam.gserviceaccount.com")
    monkeypatch.setattr(config, "gcp_project", lambda: "p")  # gcp configured → its label merges in
    monkeypatch.setattr(config, "github_declared", lambda: True)  # github configured → dummy token
    reg = Registry([gcp.GcpPlugin(), github.GithubPlugin(), proxy.ProxyPlugin()])
    args = reg.box_args({"FY_PROXY": "h:8088", "GH_INJECT": "app", "PODMAN_PROJECT": "p"})
    # gcp label (gcp) + dummy token (github, via GH_INJECT) + CA mount (proxy), all merged
    assert "--label" in args and "GH_TOKEN=x" in args
    assert f"{ca}:/etc/dev-proxy-ca.pem:ro" in args


# ── proxy_rules + the proxy plugin (the egress-proxy framework, ADR-0015) ──────


def test_github_proxy_rule_app_vs_user():
    app = github.GithubPlugin().proxy_rules({"github": "app"})
    assert len(app) == 1 and app[0].host == "api.github.com" and app[0].header == "Authorization"
    assert "-m foldyard.plugins.github_app_token" in app[0].minter and app[0].replay_on_401
    assert "GH_APP_ID" in app[0].requires
    user = github.GithubPlugin().proxy_rules({"github": "user"})
    assert user[0].minter.endswith("-m foldyard.plugins.gh_cli_token") and user[0].requires == ()
    assert github.GithubPlugin().proxy_rules({"github": "off"}) == []


def test_proxy_and_gcp_daemons_are_per_worktree(monkeypatch):
    # Per-worktree-proxy.md: the ONE supervisor runs N listeners, so a worktree's proxy + minter
    # daemons carry a "@<worktree>" name suffix and listen on the worktree's OFFSET port. The main
    # checkout keeps the bare name + base port (byte-identical). WT_OFFSET pins the offset.
    monkeypatch.setenv("WT_OFFSET", "5")
    cfg = config.Config(
        repo_root=config.repo_root(),
        worktree="feat",
        toml={"proxy": {}, "plugins": {"gcp-metadata": {"project": "acme"}}},
    )
    monkeypatch.setenv("DEVBOX_LOG_SA", "box@acme.iam.gserviceaccount.com")
    with config.using(cfg):
        config.clear_caches()  # drop any cached offset so WT_OFFSET applies
        reg = Registry([gcp.GcpPlugin(), proxy.ProxyPlugin()], config=cfg)
        prox = reg.desired_daemons({"capture": "off"})
        gcpd = reg.desired_daemons({"gcp": "logs"})
    # the project band's bases (proxy 41000, minter 41100) + this worktree's offset
    assert "egress-proxy@feat" in prox and prox["egress-proxy@feat"]["port"] == 41000 + 5
    assert "gcp-minter@feat" in gcpd and gcpd["gcp-minter@feat"]["port"] == 41100 + 5
    # the box + emulator are pointed at THIS worktree's ports (FY_PROXY + GCP_MINTER_URL)
    with config.using(cfg):
        prox_env = proxy.ProxyPlugin().derive_env({"github": "off", "capture": "off"})
        gcp_env = gcp.GcpPlugin().derive_env({"gcp": "logs"})
    # The alias half is backend-dependent (podman: host.containers.internal; lima: the guest→host
    # gateway IP) — what this test pins is the PORT half: each worktree gets its own offset.
    assert prox_env["FY_PROXY"] == f"{config.host_alias()}:{41000 + 5}"
    assert gcp_env["GCP_MINTER_URL"] == f"http://{config.host_alias()}:{41100 + 5}"


def test_derive_env_uses_the_lima_host_gateway_on_lima(monkeypatch):
    # Under [machine].backend = "lima", host.containers.internal resolves to the VM itself — the
    # box + emulator must be pointed at Lima's guest→host gateway instead (config.host_alias).
    monkeypatch.delenv("MACHINE_BACKEND", raising=False)
    monkeypatch.delenv("FY_HOST_ALIAS", raising=False)
    cfg = config.Config(
        repo_root=config.repo_root(),
        worktree="",
        toml={
            "proxy": {},
            "machine": {"backend": "lima"},
            "plugins": {"gcp-metadata": {"project": "acme"}},
        },
    )
    with config.using(cfg):
        config.clear_caches()
        prox_env = proxy.ProxyPlugin().derive_env({"github": "off", "capture": "off"})
        gcp_env = gcp.GcpPlugin().derive_env({"gcp": "logs"})
    assert prox_env["FY_PROXY"] == "192.168.5.2:41000"
    assert gcp_env["GCP_MINTER_URL"] == "http://192.168.5.2:41100"


def test_axis_daemon_names_are_per_worktree():
    cfg = config.Config(
        repo_root=config.repo_root(),
        worktree="feat",
        toml={"proxy": {}, "plugins": {"gcp-metadata": {"project": "acme"}}},
    )
    reg = Registry([gcp.GcpPlugin(), proxy.ProxyPlugin()], config=cfg)
    assert reg.axis_daemon()["gcp"] == "gcp-minter@feat"
    assert reg.axis_daemon()["capture"] == "egress-proxy@feat"


def test_main_worktree_daemons_keep_bare_names_and_base_ports():
    # The single-worktree world is unchanged: main ('' worktree) → bare daemon names + the
    # project band's base ports (no worktree offset).
    cfg = config.Config(repo_root=config.repo_root(), worktree="", toml=_FULL_TOML)
    with config.using(cfg):
        reg = Registry([gcp.GcpPlugin(), proxy.ProxyPlugin()], config=cfg)
        assert "egress-proxy" in reg.desired_daemons({"capture": "off"})
        assert reg.desired_daemons({"capture": "off"})["egress-proxy"]["port"] == 41000
        assert "gcp-minter" in reg.desired_daemons({"gcp": "logs"})


def test_registry_proxy_rules_merges_only_injectors():
    reg = Registry([gcp.GcpPlugin(), github.GithubPlugin(), proxy.ProxyPlugin()])
    rules = reg.proxy_rules({"github": "app"})
    assert len(rules) == 1 and rules[0].host == "api.github.com"  # gcp + proxy add none


def test_proxy_daemon_built_from_the_github_rule():
    reg = Registry([gcp.GcpPlugin(), github.GithubPlugin(), proxy.ProxyPlugin()])
    spec = reg.desired_daemons({"github": "app"})["egress-proxy"]  # proxy owns the daemon now
    assert "-m foldyard.plugins.github_app_token" in spec["env"]["INJECT_COMMAND"]
    # The daemon env is derived from the rule (not egress_proxy.py's defaults) — for github the
    # values equal those defaults, so the emitted command stays byte-identical.
    assert spec["env"]["INJECT_HOST"] == "api.github.com"
    assert spec["env"]["INJECT_HEADER"] == "Authorization"
    assert spec["env"]["INJECT_RETRY_401"] == "1"  # github sets replay_on_401=True
    # github=off + capture=off ⇒ still passthrough (the injector host is decrypted regardless).
    assert spec["env"]["CAPTURE_MODE"] == "passthrough"
    # The App identity trio only — the PEM is deliberately not gate-able (see
    # test_github_app_rule_never_requires_the_pem: a proxy that won't launch kills ALL box egress).
    assert spec["requires"] == ["GH_APP_ID", "GH_INSTALLATION_ID", "GH_REPO"]
    # cmd[0] is resolved (foldyard's venv copy or PATH) so it may be an absolute path — the
    # basename is what's invariant. See proxy.mitmdump_path / the github[extra] packaging.
    assert "App token" in spec["label"] and os.path.basename(spec["cmd"][0]) == "mitmdump"
    user = reg.desired_daemons({"github": "user"})["egress-proxy"]
    assert user["env"]["INJECT_COMMAND"].endswith("-m foldyard.plugins.gh_cli_token")
    assert user["requires"] == []


def test_proxy_routing_is_gated_on_opt_in_or_an_active_injector(monkeypatch):
    # Phase A′ always-route, but only when the consumer OPTED IN ([proxy] table) or an injector is
    # active (github != off). A bare project with no [proxy] and github off gets NO FY_PROXY, so
    # box_args adds no HTTPS_PROXY/NO_PROXY — a clean box. github still sets only its GH_INJECT.
    reg = Registry([github.GithubPlugin(), proxy.ProxyPlugin()])

    monkeypatch.setattr(config, "proxy_enabled", lambda: False)
    assert "FY_PROXY" in reg.derive_env({"github": "app"})  # injector active → routed
    assert reg.derive_env({"github": "off", "capture": "off"}) == {}  # bare + no opt-in → clean

    monkeypatch.setattr(config, "proxy_enabled", lambda: True)  # [proxy] declared → always routed
    assert "FY_PROXY" in reg.derive_env({"github": "off", "capture": "off"})

    assert github.GithubPlugin().derive_env({"github": "app"}) == {"GH_INJECT": "app"}
    assert github.GithubPlugin().derive_env({"github": "off"}) == {}


def test_proxy_daemon_gated_on_opt_in_or_injector(monkeypatch):
    # daemons() is gated the SAME as derive_env(): a consumer with no [proxy] and no active injector
    # gets NO egress-proxy listener — else desired_daemons() would expose :8088 and Doctor / the mode
    # dashboard would flag it perpetually DOWN for a project that never routes through it.
    reg = Registry([github.GithubPlugin(), proxy.ProxyPlugin()])

    monkeypatch.setattr(config, "proxy_enabled", lambda: False)
    assert reg.desired_daemons({"github": "off", "capture": "off"}) == {}  # bare + no opt-in
    assert "egress-proxy" in reg.desired_daemons({"github": "app"})  # injector active → listener

    monkeypatch.setattr(config, "proxy_enabled", lambda: True)  # [proxy] declared → always on
    assert "egress-proxy" in reg.desired_daemons({"github": "off", "capture": "off"})


def test_proxy_is_always_on_with_capture_mode_following_the_axis(monkeypatch):
    # Phase A′ — always-on FOR AN OPTED-IN CONSUMER ([proxy] declared): the egress-proxy daemon runs
    # for EVERY mode (was: only an injector or capture=on), even with no injector + capture=off.
    # CAPTURE_MODE follows the capture axis; with no injector INJECT_* is empty. (A consumer that
    # never opts in gets no daemon at all — test_proxy_daemon_gated_on_opt_in_or_injector covers it.)
    monkeypatch.setattr(config, "proxy_enabled", lambda: True)
    reg = Registry([gcp.GcpPlugin(), github.GithubPlugin(), proxy.ProxyPlugin()])
    off = reg.desired_daemons({"github": "off", "capture": "off"})["egress-proxy"]
    assert off["env"]["INJECT_HOST"] == "" and off["env"]["INJECT_COMMAND"] == ""
    assert off["env"]["CAPTURE_MODE"] == "passthrough"
    assert off["env"]["PROXY_LOG_FILE"].endswith("egress.jsonl") and off["requires"] == []
    assert "passthrough" in off["label"]

    on = reg.desired_daemons({"github": "off", "capture": "on"})["egress-proxy"]
    assert on["env"]["INJECT_HOST"] == "" and on["env"]["CAPTURE_MODE"] == "full"
    assert "capture" in on["label"]


def test_proxy_passthrough_resolves_bundles_globs_and_dedups():
    from foldyard.plugins._passthrough_bundles import BUNDLES

    out = proxy._resolve_passthrough(
        ["@anthropic", "api.github.com", "*.x.com", "@nope", "api.github.com"]
    )
    assert "api.anthropic.com" in out  # expanded from @anthropic
    assert out.count("api.github.com") == 1  # literal, deduped
    assert "*.x.com" in out  # glob passes through
    assert all(not p.startswith("@") for p in out)  # @nope (unknown) expands to nothing, fail-safe
    # @all = the union of every built-in bundle
    everything = proxy._resolve_passthrough(["@all"])
    assert set(everything) >= {h for hosts in BUNDLES.values() for h in hosts}


def test_proxy_daemon_carries_resolved_passthrough_hosts(monkeypatch):
    # The daemon spec hands egress_proxy.py the resolved trusted-host list as PASSTHROUGH_HOSTS.
    monkeypatch.setattr(proxy.config, "proxy_enabled", lambda: True)  # opted-in → daemon runs
    monkeypatch.setattr(proxy.config, "proxy_passthrough", lambda: ["@anthropic", "my.host"])
    reg = Registry([proxy.ProxyPlugin()])
    hosts = reg.desired_daemons({"capture": "on"})["egress-proxy"]["env"][
        "PASSTHROUGH_HOSTS"
    ].split(",")
    assert "api.anthropic.com" in hosts and "my.host" in hosts


def test_proxy_daemon_carries_the_allowlist_env(monkeypatch):
    # The daemon hands egress_proxy.py the egress-wall env: DEFAULT_DENY (static on/off, restart on
    # toggle) + ALLOW_FILE (the effective allowlist re-read per request, so grants need no restart).
    monkeypatch.setattr(proxy.config, "proxy_enabled", lambda: True)  # opted-in → daemon runs
    monkeypatch.setattr(proxy.config, "proxy_default_deny", lambda: True)
    reg = Registry([proxy.ProxyPlugin()])
    env = reg.desired_daemons({"capture": "off"})["egress-proxy"]["env"]
    assert env["DEFAULT_DENY"] == "1"
    assert env["ALLOW_FILE"].endswith("allow-effective.json")

    monkeypatch.setattr(proxy.config, "proxy_default_deny", lambda: False)
    off = reg.desired_daemons({"capture": "off"})["egress-proxy"]["env"]
    assert off["DEFAULT_DENY"] == ""  # only "1" arms the wall; off ⇒ empty (addon ignores it)


def test_proxy_capture_composes_with_a_github_injector():
    # capture=on + github=app: the injector wins the daemon env (it logs everything anyway), so the
    # api.github.com rewrite is unchanged — capture just flips CAPTURE_MODE to full (MITM all).
    reg = Registry([github.GithubPlugin(), proxy.ProxyPlugin()])
    spec = reg.desired_daemons({"github": "app", "capture": "on"})["egress-proxy"]
    assert spec["env"]["INJECT_HOST"] == "api.github.com"
    assert "-m foldyard.plugins.github_app_token" in spec["env"]["INJECT_COMMAND"]
    assert spec["env"]["CAPTURE_MODE"] == "full"


def test_proxy_serializes_multiple_injectors_into_a_rule_set():
    # The multi-injector rule-set contract: two live injectors no longer collide — both flow into
    # the one egress-proxy daemon as INJECT_RULES JSON, each with its own host/header/minter, and the
    # single INJECT_* keys are left empty (egress_proxy.py uses INJECT_RULES when present).
    class _SecondInjector(Plugin):
        name = "second"

        def proxy_rules(self, mode):
            return [
                InjectRule(
                    host="api.other.com",
                    header="authorization",
                    minter="/m",
                    requires=("OTHER_TOKEN",),
                    value_prefix="Bearer ",
                    replay_on_401=True,
                )
            ]

    reg = Registry([github.GithubPlugin(), _SecondInjector(), proxy.ProxyPlugin()])
    env = reg.desired_daemons({"github": "app"})["egress-proxy"]["env"]
    assert env["INJECT_HOST"] == "" and env["INJECT_COMMAND"] == ""  # single-rule keys cleared
    ruleset = json.loads(env["INJECT_RULES"])
    by_host = {r["host"]: r for r in ruleset}
    assert set(by_host) == {"api.github.com", "api.other.com"}  # both injectors present
    other = by_host["api.other.com"]
    assert other["command"] == "/m" and other["header"] == "authorization"
    assert other["value_prefix"] == "Bearer " and other["retry_401"] is True
    # github's app-token minter has no value_prefix → that key is omitted (compact serialization).
    assert "value_prefix" not in by_host["api.github.com"]
    # requires is the UNION across injectors (so the supervisor checks every minter's host.env keys).
    requires = reg.desired_daemons({"github": "app"})["egress-proxy"]["requires"]
    assert "OTHER_TOKEN" in requires and "GH_APP_ID" in requires


# ── tui_panels (the TUI-panel hook, ADR-0015) — GCP Tokens (gcp) + Network Log (proxy) ────


def test_github_contributes_no_tui_panel():
    # github rides the proxy's Network Log (it's one injector); it owns no panel of its own.
    assert github.GithubPlugin().tui_panels() == []


def test_gcp_contributes_the_gcp_tokens_panel():
    panels = gcp.GcpPlugin().tui_panels()
    assert [p.id for p in panels] == ["gcp"]
    assert panels[0].title == "GCP Tokens" and len(panels[0].columns) == 5


def test_proxy_contributes_the_network_log_panel():
    panels = proxy.ProxyPlugin().tui_panels()
    assert [p.id for p in panels] == ["network"]
    # The Network Log is now a host-grouped, collapsible TREE (no flat columns).
    assert panels[0].title == "Network Log" and panels[0].kind == "tree"
    assert panels[0].columns == ()


def test_registry_tui_panels_merges_across_plugins():
    class _PanelPlugin(Plugin):
        name = "demo-panel"

        def tui_panels(self):
            return [
                TuiPanel(id="demo", title="Demo", columns=("a",), refresh=lambda: PanelData("", []))
            ]

    reg = Registry([gcp.GcpPlugin(), proxy.ProxyPlugin(), _PanelPlugin()])
    # gcp's GCP Tokens panel + proxy's Network Log + the demo, in plugin order
    assert [p.id for p in reg.tui_panels()] == ["gcp", "network", "demo"]


def test_network_panel_data_rows_summary_and_escaping(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "log_dir", lambda: tmp_path)
    (tmp_path / "egress.jsonl").write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "ts": "2026-06-14T10:00:01Z",
                        "method": "GET",
                        "host": "api.github.com",
                        "path": "/p[q]",
                        "status": 200,
                    }
                ),
                json.dumps(
                    {
                        "ts": "2026-06-14T10:00:02Z",
                        "method": "POST",
                        "host": "api.github.com",
                        "path": "/issues",
                        "status": 502,
                        "injected": True,
                        "replayed": True,
                    }
                ),
            ]
        )
        + "\n"
    )
    data = proxy._network_panel_tree()
    # Both requests are to one host → one group, with both as children (newest first).
    assert [g.key for g in data.groups] == ["api.github.com"]
    group = data.groups[0]
    assert "api.github.com" in group.label and "2 req" in group.label
    assert "1 inj" in group.label and "1 err" in group.label  # injected + the 502
    assert len(group.children) == 2
    assert "POST" in group.children[0] and "GET" in group.children[1]  # newest first
    assert "[red]502[/red]" in group.children[0] and "inj+replay" in group.children[0]
    assert "[green]200[/green]" in group.children[1]
    assert "\\[q]" in group.children[1]  # untrusted path is markup-escaped
    assert "2 requests across 1 hosts · 1 with injected Authorization" in data.summary


def test_network_panel_tree_groups_by_host(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "log_dir", lambda: tmp_path)
    (tmp_path / "egress.jsonl").write_text(
        "\n".join(
            [
                json.dumps({"ts": "2026-06-14T10:00:01Z", "host": "a.com", "status": 200}),
                json.dumps({"ts": "2026-06-14T10:00:02Z", "host": "b.com", "status": 200}),
                json.dumps({"ts": "2026-06-14T10:00:03Z", "host": "a.com", "status": 200}),
            ]
        )
        + "\n"
    )
    data = proxy._network_panel_tree()
    # Two host groups, ordered by most-recent activity (a.com's last hit is newest).
    assert [g.key for g in data.groups] == ["a.com", "b.com"]
    assert len(data.groups[0].children) == 2 and len(data.groups[1].children) == 1
    assert "3 requests across 2 hosts" in data.summary


def test_network_panel_tree_empty_summary(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "log_dir", lambda: tmp_path)  # no log file yet
    data = proxy._network_panel_tree()
    assert data.groups == [] and "no proxied traffic yet" in data.summary


def test_network_panel_marks_blocked_rows_and_tallies_them(monkeypatch, tmp_path):
    # A row refused by the default-deny wall renders as a red ⛔ leaf and bumps a `blocked` tally in
    # the host header — and its synthetic 403 is NOT also counted as a generic error (no double-count).
    monkeypatch.setattr(config, "log_dir", lambda: tmp_path)
    (tmp_path / "egress.jsonl").write_text(
        json.dumps(
            {
                "ts": "2026-06-25T10:00:01Z",
                "host": "evil.example.com",
                "status": 403,
                "blocked": True,
            }
        )
        + "\n"
    )
    data = proxy._network_panel_tree()
    group = data.groups[0]
    assert "⛔ 1 blocked" in group.label
    assert "err" not in group.label  # the 403 is the blocked tally, not also a generic error
    assert "⛔ blocked by the egress wall" in group.children[0] and "[red]" in group.children[0]


def test_gcp_panel_data_rows_summary_and_escaping(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "log_dir", lambda: tmp_path)
    (tmp_path / "gcp-minter.jsonl").write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "ts": "2026-06-14T10:00:01Z",
                        "sa": "devbox-log-reader@p.iam.gserviceaccount.com",
                        "user": False,
                        "status": 200,
                    }
                ),
                json.dumps(
                    {
                        "ts": "2026-06-14T10:00:02Z",
                        "sa": "evil@x[y]",
                        "user": False,
                        "status": 403,
                        "error": "SA not in allowlist: evil@x[y]",
                    }
                ),
                json.dumps(
                    {"ts": "2026-06-14T10:00:03Z", "sa": "user", "user": True, "status": 200}
                ),
            ]
        )
        + "\n"
    )
    data = gcp._gcp_panel_data()
    assert len(data.rows) == 3
    # newest first: the user-token mint, then the refused SA, then the granted SA
    assert data.rows[0][2] == "user" and "[green]200[/green]" in data.rows[0][3]
    assert data.rows[1][2] == "SA" and "[red]403[/red]" in data.rows[1][3]
    assert "not in allowlist" in data.rows[1][4]
    assert "\\[y]" in data.rows[1][1]  # untrusted SA text is markup-escaped
    assert data.rows[2][1].startswith("devbox-log-reader@")
    assert "3 token mints · 2 granted" in data.summary


def test_gcp_panel_data_empty_summary(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "log_dir", lambda: tmp_path)  # no mint log yet
    data = gcp._gcp_panel_data()
    assert data.rows == [] and "no token mints yet" in data.summary


# ── verify_checks (the verify posture hook, ADR-0015) ────────────────────────────────


def _vctx(env=None, in_box=True, gh=False):
    return VerifyContext(in_box=in_box, env=env or {}, which=lambda c: gh)


def _statuses(rows):
    return [status for status, _ in rows]


def test_github_verify_off_clean_passes():
    rows = list(github._verify_rows("off", gh_present=False, gh_token="", github_token=""))
    assert "fail" not in _statuses(rows)


def test_github_verify_off_token_fails():
    rows = list(github._verify_rows("off", False, "ghp_real", ""))
    assert "fail" in _statuses(rows)


def test_github_verify_off_ambient_plumbing_passes():
    # The dummy 'x' + the gh CLI are ambient box plumbing (baked with the proxy substrate so the
    # axis flips live) and grant no access while the host injects nothing — NOT a posture fail.
    # The tested invariant is no-real-token (+ the core's push-refused backstop).
    rows = list(github._verify_rows("off", True, "x", ""))
    assert "fail" not in _statuses(rows)


def test_github_verify_off_real_looking_token_still_fails_with_plumbing():
    rows = list(github._verify_rows("off", True, "ghp_real", ""))
    assert "fail" in _statuses(rows)


def test_github_verify_proxy_dummy_token_passes():
    rows = list(github._verify_rows("app", True, "x", ""))
    assert "fail" not in _statuses(rows)


def test_github_verify_proxy_real_token_fails():
    rows = list(github._verify_rows("user", True, "ghp_real", ""))
    assert "fail" in _statuses(rows)


def test_github_verify_user_mode_emits_emergency_info():
    assert "info" in _statuses(github._verify_rows("user", True, "x", ""))  # EMERGENCY banner


def test_box_github_mode_no_ca_is_off(monkeypatch, tmp_path):
    monkeypatch.setattr(proxy, "BOX_CA", tmp_path / "nope.pem")
    assert github._box_github_mode({}) == "off"


def test_box_github_mode_ca_present_reads_mirror(monkeypatch, tmp_path):
    ca = tmp_path / "ca.pem"
    ca.write_text("c")
    monkeypatch.setattr(proxy, "BOX_CA", ca)
    monkeypatch.setattr(config, "github_declared", lambda: True)
    checkout = tmp_path / "co"
    mirror_dir = checkout / config.dev_vm_rel()
    mirror_dir.mkdir(parents=True)
    (mirror_dir / ".dev-mode.json").write_text('{"github": "user"}')
    assert github._box_github_mode({"FOLDYARD_CHECKOUT": str(checkout)}) == "user"


def test_box_github_mode_ca_present_no_mirror_defaults_app(monkeypatch, tmp_path):
    ca = tmp_path / "ca.pem"
    ca.write_text("c")
    monkeypatch.setattr(proxy, "BOX_CA", ca)
    monkeypatch.setattr(config, "github_declared", lambda: True)
    assert github._box_github_mode({"FOLDYARD_CHECKOUT": str(tmp_path / "empty")}) == "app"


def test_box_github_mode_undeclared_is_off_whatever_mounted_the_ca(monkeypatch, tmp_path):
    # The CA is ambient proxy substrate (capture, claude keyless, …), so its presence must not
    # read as github intent for a consumer with no [plugins.github] — the old "CA present, mode
    # unknown → assume app" fallback made verify assert app-mode invariants (dummy-token rows)
    # in boxes that never had github at all.
    ca = tmp_path / "ca.pem"
    ca.write_text("c")
    monkeypatch.setattr(proxy, "BOX_CA", ca)
    monkeypatch.setattr(config, "github_declared", lambda: False)
    assert github._box_github_mode({"FOLDYARD_CHECKOUT": str(tmp_path / "empty")}) == "off"


def test_github_verify_skipped_outside_box(monkeypatch, tmp_path):
    monkeypatch.setattr(proxy, "BOX_CA", tmp_path / "nope.pem")
    assert list(github.GithubPlugin().verify_checks(_vctx(in_box=False))) == []


def test_registry_verify_checks_merges_in_box(monkeypatch, tmp_path):
    monkeypatch.setattr(proxy, "BOX_CA", tmp_path / "nope.pem")  # github=off
    monkeypatch.delenv("GH_TOKEN", raising=False)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    reg = Registry([gcp.GcpPlugin(), github.GithubPlugin()])
    rows = list(reg.verify_checks(_vctx(in_box=True, gh=False)))
    assert rows  # github contributes; gcp is a no-op
    assert "fail" not in _statuses(rows)
    assert all(status in ("pass", "fail", "info") for status in _statuses(rows))


# ── PAM-grant parsing (relocated from the devmode tests) ──────────────────────────────


def test_human_left_formats_hours_then_minutes():
    assert gcp._human_left(11 * 3600 + 59 * 60) == "11h59m"
    assert gcp._human_left(59 * 60 + 30) == "59m30s"
    assert gcp._human_left(0) == "EXPIRED"


def test_pam_left_parses_create_plus_duration():
    created = devmode._iso(devmode.now() - timedelta(hours=2))
    assert gcp._pam_left(f"{created} 43200s") == "9h59m"  # 12h grant, 2h elapsed


def test_pam_left_handles_z_suffix_and_garbage():
    assert gcp._pam_left("2099-01-01T00:00:00Z 3600s") is not None  # 'Z' tz
    assert gcp._pam_left("nonsense") is None
    assert gcp._pam_left("") is None
    assert gcp._pam_left("only-one-field") is None


def test_pam_left_expired():
    created = devmode._iso(devmode.now() - timedelta(hours=13))
    assert gcp._pam_left(f"{created} 43200s") == "EXPIRED"


def test_pam_summary_picks_your_active_unexpired_grant():
    created = devmode._iso(devmode.now() - timedelta(hours=1))
    me, them = "me@x.io", "them@x.io"
    out = "\n".join(
        [
            f"INACTIVE\t{me}\t-\t-",
            f"ACTIVE\t{them}\t{created}\t43200s",  # teammate's grant — not mine
            f"ACTIVE\t{me}\t{created}\t43200s",  # mine, 11h left
        ]
    )
    assert gcp._pam_summary(out, me) == f"active grant ({me}) — expires in 10h59m"


def test_pam_summary_ignores_other_peoples_grants():
    created = devmode._iso(devmode.now() - timedelta(hours=1))
    out = f"ACTIVE\tsomeone-else@x.io\t{created}\t43200s"
    assert gcp._pam_summary(out, "me@x.io") is None


def test_pam_summary_ignores_inactive_expired_and_empty():
    expired = devmode._iso(devmode.now() - timedelta(hours=13))
    assert gcp._pam_summary("INACTIVE\tme@x.io\t-\t-", "me@x.io") is None
    assert gcp._pam_summary(f"ACTIVE\tme@x.io\t{expired}\t43200s", "me@x.io") is None
    assert gcp._pam_summary("", "me@x.io") is None


# ── inject plugin (the generic config-driven injector) ────────────────────────────────

_PENPOT_SPEC = {
    "axis": "penpot",
    "host": "truenas.example.ts.net",
    "query_param": "userToken",
    "label": "Penpot MCP injection proxy",
    "path_prefix": "/mcp",
}


def _inject_plugin(monkeypatch, specs):
    """An InjectPlugin whose config rows are stubbed (no foldyard.toml needed). Stubs the CONFIG
    read, not `_specs` — `_specs` carries the cross-row validation, so stubbing it would test a
    plugin with the check removed."""
    monkeypatch.setattr(inject.config, "inject_specs", lambda: specs)
    return inject.InjectPlugin()


def test_inject_axis_from_config(monkeypatch):
    axes = _inject_plugin(monkeypatch, [_PENPOT_SPEC]).axes()
    assert len(axes) == 1
    ax = axes[0]
    assert ax.name == "penpot" and ax.rungs == ("off", "on") and ax.daemon == "egress-proxy"
    assert ax.emergency == ()  # a plain injector axis carries no TTL'd emergency rung


def test_inject_rule_built_for_query_param_token_env(monkeypatch):
    plugin = _inject_plugin(monkeypatch, [_PENPOT_SPEC])
    assert plugin.proxy_rules({"penpot": "off"}) == []  # off → no rule
    rules = plugin.proxy_rules({"penpot": "on"})
    assert len(rules) == 1
    rule = rules[0]
    assert rule.host == "truenas.example.ts.net"
    assert rule.query_param == "userToken" and rule.header == ""  # query-param mode clears header
    assert rule.path_prefix == "/mcp"
    # `requires` gates the WHOLE proxy daemon's spawn, so a token source never goes there — one
    # injector's missing token must not connection-refuse every box request under always-route. It's
    # `env` (what the minter may read) instead; a missing token degrades just this host.
    assert rule.requires == () and rule.env == ("FY_INJECT_PENPOT",)
    # The minter is the shipped static-token module fed the env var NAME — never the secret itself.
    assert "foldyard.plugins.static_token" in rule.minter and "FY_INJECT_PENPOT" in rule.minter


def test_inject_header_mode_defaults_authorization(monkeypatch):
    spec = {"axis": "svc", "host": "api.svc.test"}
    rule = _inject_plugin(monkeypatch, [spec]).proxy_rules({"svc": "on"})[0]
    assert rule.header == "Authorization" and rule.query_param == ""  # header is the default mode
    assert rule.value_prefix == ""  # no scheme prefix by default


def test_inject_value_prefix_flows_through(monkeypatch):
    # A generic [[inject]] can carry value_prefix too (e.g. "Bearer " / "token ") — the proxy
    # prepends it, so the host.env token stays bare. Reaches the daemon env as INJECT_VALUE_PREFIX.
    spec = {"axis": "svc", "host": "api.svc.test", "value_prefix": "token "}
    rule = _inject_plugin(monkeypatch, [spec]).proxy_rules({"svc": "on"})[0]
    assert rule.value_prefix == "token "
    reg = Registry([_inject_plugin(monkeypatch, [spec]), proxy.ProxyPlugin()])
    env = reg.desired_daemons({"svc": "on"})["egress-proxy"]["env"]
    assert env["INJECT_VALUE_PREFIX"] == "token "


def test_inject_spec_without_a_host_is_dropped(monkeypatch):
    spec = {"axis": "svc"}  # nothing to inject on
    assert _inject_plugin(monkeypatch, [spec]).proxy_rules({"svc": "on"}) == []
    # The packaged path (claude/codex, via keyless.inject_spec) still declares its own var, and a
    # spec without one is unusable — that's the tier where naming a var is legitimate.
    assert inject._spec_to_rule({"host": "api.svc.test"}) is None


def test_inject_token_var_is_derived_from_the_axis_not_the_row(monkeypatch):
    # `[[inject]]` is REPO config. A row-named token var would let anything that can write the
    # checkout point a rule at another mechanism's host.env secret AND at a host of its choosing —
    # the proxy would hand it over in flight. So the var is derived from the axis and a declared
    # one is ignored: a config rule reaches only the var the operator made for that injector.
    spec = {"axis": "pen-pot", "host": "api.svc.test", "token_env": "ANTHROPIC_API_KEY"}
    rule = _inject_plugin(monkeypatch, [spec]).proxy_rules({"pen-pot": "on"})[0]
    assert rule.env == ("FY_INJECT_PEN_POT",)  # punctuation folds to _; host.env keys are env names
    assert "ANTHROPIC_API_KEY" not in rule.minter


@pytest.mark.parametrize(
    ("first", "second"),
    [
        ("pen-pot", "pen_pot"),  # punctuation: both fold to FY_INJECT_PEN_POT
        ("penpot", "PenPot"),  # case: both upper to FY_INJECT_PENPOT
    ],
)
def test_inject_axes_that_collide_on_the_token_var_are_refused(monkeypatch, first, second):
    # Deriving the var from the axis is only a boundary if the mapping is injective HERE: two axes
    # sharing FY_INJECT_<X> means the second row can aim the first's token at a host of its
    # choosing, which is the hole the derivation closed, one rename away. Distinct axis names, so
    # the registry's own duplicate check never sees it — this is the check that does.
    specs = [{"axis": first, "host": "a.test"}, {"axis": second, "host": "collector.test"}]
    plugin = _inject_plugin(monkeypatch, specs)
    with pytest.raises(ValueError, match="derive the token var"):
        plugin.axes()
    with pytest.raises(ValueError, match="derive the token var"):
        plugin.proxy_rules({first: "on"})


def test_inject_rule_flows_into_the_proxy_daemon_env(monkeypatch):
    # End-to-end through the registry: penpot=on → the egress-proxy daemon gets the query-param env.
    reg = Registry([_inject_plugin(monkeypatch, [_PENPOT_SPEC]), proxy.ProxyPlugin()])
    env = reg.desired_daemons({"penpot": "on"})["egress-proxy"]["env"]
    assert env["INJECT_HOST"] == "truenas.example.ts.net"
    assert env["INJECT_QUERY_PARAM"] == "userToken" and env["INJECT_HEADER"] == ""
    assert env["INJECT_PATH_PREFIX"] == "/mcp"


def test_static_token_minter_emits_value_ttl_json(monkeypatch, capsys):
    monkeypatch.setenv("PENPOT_USER_TOKEN", "secret-xyz")
    assert static_token.main(["PENPOT_USER_TOKEN", "60"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out == {"value": "secret-xyz", "ttl": 60}


def test_static_token_minter_fails_on_unset_var(monkeypatch, capsys):
    monkeypatch.delenv("PENPOT_USER_TOKEN", raising=False)
    assert static_token.main(["PENPOT_USER_TOKEN"]) == 1  # non-zero → proxy logs a clear failure
    assert "host.env" in capsys.readouterr().err


# ── claude / vscode plugins (config-gated agent/editor surface) ───────────────────────────

_BOX_ENV = {
    "FY_BOX_HOME": "/home/vscode",
    "FY_CLAUDE_HOME": "/home/vscode/.claude",
    "FY_TRANSCRIPTS": "",
}


# A bootstrap step may use ONLY what the box contract (ADR-0014) promises — git, uv, an engine
# client, and the coreutils/curl any base carries. Every spelling below ties a step to one image
# family, and each has already broken a real box: `apt-get` ✗'d every step back when the packaged
# base was Fedora; `npm i -g @openai/codex` ✗'d on the uv-only base AND on a Debian box whose
# `nodejs` package shipped without npm; a bare `python3` silently needed a warm ~/.claude volume to
# look fine (use the bootstrap's `fy_python` instead). The rule is cheap to state and cheap to
# check, so check it rather than trusting the next author to remember.
_IMAGE_TIED = (
    "apt-get",
    "apt ",
    "dnf ",
    "yum ",
    "apk ",
    "npm ",
    "npm i",
    "pip ",
    "pip3 ",
    "python3",
)


def test_box_bootstrap_steps_stay_image_agnostic(monkeypatch):
    monkeypatch.setattr(config, "claude_enabled", lambda: True)
    monkeypatch.setattr(config, "codex_enabled", lambda: True)
    monkeypatch.setattr(config, "github_declared", lambda: True)
    env = {**_BOX_ENV, "FY_PROXY": "1", "CLAUDE_INJECT": "on"}
    steps = [
        s
        for p in (claude.ClaudePlugin(), codex.CodexPlugin(), github.GithubPlugin())
        for s in p.box_bootstrap(dict(env))
    ]
    assert len(steps) >= 3  # the guard is worthless if the plugins went inert on us
    for step in steps:
        blob = f"{step.get('check', '')} {step['run']}"
        offenders = [bad for bad in _IMAGE_TIED if bad in blob]
        assert not offenders, f"{step['label']} depends on {offenders} — not in the box contract"


def test_claude_plugin_inert_without_table(monkeypatch):
    monkeypatch.setattr(config, "claude_enabled", lambda: False)
    p = claude.ClaudePlugin()
    assert p.box_args(dict(_BOX_ENV)) == [] and p.box_bootstrap(dict(_BOX_ENV)) == []


def test_claude_plugin_mounts_and_installs_when_enabled(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "claude_enabled", lambda: True)
    env = {**_BOX_ENV, "FY_TRANSCRIPTS": str(tmp_path / "tx")}
    args = claude.ClaudePlugin().box_args(env)
    assert "devbox_claude_home:/home/vscode/.claude" in args  # config home volume
    assert "devbox_claude_native:/home/vscode/.local/share/claude" in args  # native version store
    assert f"{tmp_path / 'tx'}:/home/vscode/.claude/projects" in args  # transcripts bind
    assert (tmp_path / "tx").is_dir()  # box_args created the host-side transcripts dir
    boot = claude.ClaudePlugin().box_bootstrap(env)
    assert boot[0]["check"] == "command -v claude" and "claude.ai/install.sh" in boot[0]["run"]


def test_claude_keyless_seeds_onboarding_flag(monkeypatch):
    # Keyless box: box_bootstrap adds a step marking Claude onboarding done, so the box doesn't
    # prompt to authenticate (auth is the injected header). Ambient with the proxy substrate when
    # keyless is CONFIGURED, like the dummy — never keyed to the rung.
    monkeypatch.setattr(config, "claude_enabled", lambda: True)
    monkeypatch.setattr(config, "claude_keyless", lambda: "api-key")
    p = claude.ClaudePlugin()
    # BARE [claude] (no keyless) → install step only, NO onboarding seed: that prompt is the login.
    monkeypatch.setattr(config, "claude_keyless", lambda: "")
    assert all(
        s["label"] != "Claude onboarding flag"
        for s in p.box_bootstrap({**_BOX_ENV, "FY_PROXY": "1"})
    )
    monkeypatch.setattr(config, "claude_keyless", lambda: "api-key")
    # keyless configured → the onboarding step sets hasCompletedOnboarding (merged via `fy_python`)
    steps = p.box_bootstrap({**_BOX_ENV, "FY_PROXY": "1"})
    step = next(s for s in steps if s["label"] == "Claude onboarding flag")
    assert "hasCompletedOnboarding" in step["check"]
    payload = step["run"].split("printf %s ")[1].split(" |")[0]
    decoded = __import__("base64").b64decode(payload).decode()
    assert "hasCompletedOnboarding" in decoded and "json" in decoded


@pytest.mark.parametrize(
    ("plugin", "enabled", "keyless", "cred", "seed", "api_host"),
    [
        (
            claude.ClaudePlugin,
            "claude_enabled",
            "claude_keyless",
            "ANTHROPIC_API_KEY",
            "Claude onboarding flag",
            "api.anthropic.com",
        ),
        (
            codex.CodexPlugin,
            "codex_enabled",
            "codex_keyless",
            "OPENAI_API_KEY",
            "Codex keyless auth.json",
            "api.openai.com",
        ),
    ],
)
def test_a_bare_agent_block_is_the_manual_login_box(
    monkeypatch, plugin, enabled, keyless, cred, seed, api_host
):
    # `[claude]` / `[codex]` with NO keyless = "install it, mount its state, I'll log in myself".
    # Every keyless artifact has to stay out of that box or the login can't happen: a dummy
    # credential takes precedence over the token you obtain, and a pre-seeded onboarding/auth file
    # skips or satisfies the very prompt you need. There's also no rung to flip — the axis only
    # exists under keyless — and no injector, so nothing exempts the API host from a default_deny
    # wall; it has to be OFFERED instead. Run with the proxy substrate present, since that's what
    # makes the keyless artifacts ambient: only `keyless` being unset may hold them back.
    monkeypatch.setattr(config, enabled, lambda: True)
    monkeypatch.setattr(config, keyless, lambda: "")
    p = plugin()
    env = {**_BOX_ENV, "FY_PROXY": "1"}
    assert p.axes() == []  # no mode switch: there is no host-side decision to make
    assert p.proxy_rules({}) == [] and p.derive_env({}) == {}
    assert not any(cred in a for a in p.box_args(dict(env)))  # no dummy to shadow a real token
    assert all(s["label"] != seed for s in p.box_bootstrap(dict(env)))
    assert api_host in [h["host"] for h in p.egress_recommend()]  # reachable behind the wall
    # …and under keyless the API host drops off the offer: it's the injector host, exempt already.
    monkeypatch.setattr(config, keyless, lambda: "api-key")
    assert api_host not in [h["host"] for h in p.egress_recommend()]


def test_claude_keyless_inert_when_off(monkeypatch):
    # keyless off (a normal-login [claude] box): no axis, no proxy rule, no derive_env marker, and
    # NO dummy key baked even with [claude] enabled — the box logs in for real.
    monkeypatch.setattr(config, "claude_enabled", lambda: True)
    monkeypatch.setattr(config, "claude_keyless", lambda: "")
    p = claude.ClaudePlugin()
    assert p.axes() == []
    assert p.proxy_rules({"claude": "on"}) == []  # no mode configured → nothing, even if axis "on"
    assert p.derive_env({"claude": "on"}) == {}
    assert not any("ANTHROPIC_API_KEY" in a for a in p.box_args(dict(_BOX_ENV)))  # no dummy baked


def test_claude_keyless_api_key_axis_rule_and_dummy(monkeypatch):
    monkeypatch.setattr(config, "claude_enabled", lambda: True)
    monkeypatch.setattr(config, "claude_keyless", lambda: "api-key")
    p = claude.ClaudePlugin()

    # The on/off injector axis, mapping to the shared egress-proxy daemon (like github/inject).
    axes = p.axes()
    assert len(axes) == 1 and axes[0].name == "claude"
    assert axes[0].rungs == ("off", "on") and axes[0].daemon == "egress-proxy"

    # off → no rule / no marker; on → the InjectRule (x-api-key on api.anthropic.com via static_token
    # reading ANTHROPIC_API_KEY from host.env) + the CLAUDE_INJECT marker derive_env sets.
    assert p.proxy_rules({"claude": "off"}) == [] and p.derive_env({"claude": "off"}) == {}
    rule = p.proxy_rules({"claude": "on"})[0]
    assert rule.host == "api.anthropic.com" and rule.header == "x-api-key"
    assert rule.replay_on_401 is False and rule.env == ("ANTHROPIC_API_KEY",)
    assert "foldyard.plugins.static_token" in rule.minter and "ANTHROPIC_API_KEY" in rule.minter
    assert p.derive_env({"claude": "on"}) == {"CLAUDE_INJECT": "on"}

    # The dummy is AMBIENT with the proxy substrate, like github's GH_TOKEN — not gated on the rung,
    # which is host-side while box env is create-time (that pairing made the flip need a recreate).
    assert not any("ANTHROPIC_API_KEY" in a for a in p.box_args(dict(_BOX_ENV)))  # no substrate
    args = p.box_args({**_BOX_ENV, "FY_PROXY": "1"})  # rung OFF
    assert "ANTHROPIC_API_KEY=sk-ant-dummy" in args


def test_claude_keyless_oauth_rule_carries_bearer_prefix(monkeypatch):
    # oauth: rewrite `authorization` on api.anthropic.com from CLAUDE_CODE_OAUTH_TOKEN, with the
    # rule carrying value_prefix "Bearer " (the proxy prepends it; the bare token stays in host.env).
    monkeypatch.setattr(config, "claude_enabled", lambda: True)
    monkeypatch.setattr(config, "claude_keyless", lambda: "oauth")
    p = claude.ClaudePlugin()
    assert p.axes()[0].name == "claude"
    rule = p.proxy_rules({"claude": "on"})[0]
    assert rule.host == "api.anthropic.com" and rule.header == "authorization"
    assert rule.value_prefix == "Bearer " and rule.env == ("CLAUDE_CODE_OAUTH_TOKEN",)
    # The dummy is the OAuth-token env (not the api-key one), so the client emits a Bearer header.
    args = p.box_args({**_BOX_ENV, "FY_PROXY": "1"})
    assert "CLAUDE_CODE_OAUTH_TOKEN=sk-ant-oat-dummy" in args
    assert not any("ANTHROPIC_API_KEY" in a for a in args)  # not the api-key var


def test_claude_keyless_oauth_value_prefix_reaches_the_daemon_env(monkeypatch):
    # End-to-end through the registry: the oauth rule's value_prefix lands as INJECT_VALUE_PREFIX in
    # the single egress-proxy daemon env, so egress_proxy.py prepends "Bearer " to the minted token.
    monkeypatch.setattr(config, "claude_enabled", lambda: True)
    monkeypatch.setattr(config, "claude_keyless", lambda: "oauth")
    monkeypatch.setattr(config, "proxy_enabled", lambda: False)
    monkeypatch.setattr(config, "proxy_passthrough", lambda: [])
    monkeypatch.setattr(config, "proxy_default_deny", lambda: False)
    reg = Registry([claude.ClaudePlugin(), proxy.ProxyPlugin()])
    env = reg.desired_daemons({"claude": "on"})["egress-proxy"]["env"]
    assert env["INJECT_HEADER"] == "authorization" and env["INJECT_VALUE_PREFIX"] == "Bearer "


def test_claude_keyless_api_key_emits_no_value_prefix(monkeypatch):
    # api-key needs no scheme prefix, so INJECT_VALUE_PREFIX is absent (github stays byte-identical).
    monkeypatch.setattr(config, "claude_enabled", lambda: True)
    monkeypatch.setattr(config, "claude_keyless", lambda: "api-key")
    monkeypatch.setattr(config, "proxy_enabled", lambda: False)
    monkeypatch.setattr(config, "proxy_passthrough", lambda: [])
    monkeypatch.setattr(config, "proxy_default_deny", lambda: False)
    reg = Registry([claude.ClaudePlugin(), proxy.ProxyPlugin()])
    env = reg.desired_daemons({"claude": "on"})["egress-proxy"]["env"]
    assert "INJECT_VALUE_PREFIX" not in env


def test_secret_value_change_flips_the_proxy_daemon_stamp(monkeypatch):
    # A minter reads its secret from the DAEMON's environment, which is frozen at spawn — so a
    # keyless token captured (or rotated) in host.env AFTER the proxy launched never reaches the
    # minter, and the box's dummy credential goes upstream verbatim (the observed failure:
    # "OAuth access token is invalid" for days, with a perfectly good token in host.env). The spec
    # env therefore carries a fingerprint of every rule-declared secret VALUE: the supervisor's
    # per-tick signature comparison sees the flip and restarts the daemon with the fresh value.
    monkeypatch.setattr(config, "claude_enabled", lambda: True)
    monkeypatch.setattr(config, "claude_keyless", lambda: "oauth")
    monkeypatch.setattr(config, "proxy_enabled", lambda: False)
    monkeypatch.setattr(config, "proxy_passthrough", lambda: [])
    monkeypatch.setattr(config, "proxy_default_deny", lambda: False)
    reg = Registry([claude.ClaudePlugin(), proxy.ProxyPlugin()])

    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    absent = reg.desired_daemons({"claude": "on"})["egress-proxy"]["env"]
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "sk-ant-oat01-first")
    first = reg.desired_daemons({"claude": "on"})["egress-proxy"]["env"]
    again = reg.desired_daemons({"claude": "on"})["egress-proxy"]["env"]
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "sk-ant-oat01-second")
    second = reg.desired_daemons({"claude": "on"})["egress-proxy"]["env"]

    # Late capture AND rotation each flip the stamp → restart; unchanged → no restart churn.
    assert len({e["INJECT_ENV_STAMP"] for e in (absent, first, second)}) == 3
    assert first == again
    # The stamp is a fingerprint — the secret itself must never appear in the spec env (it would
    # land in `ps eww` on the child and in the supervisor's Child.signature repr).
    for env in (absent, first, second):
        assert "sk-ant-oat01" not in json.dumps(env)


def test_claude_keyless_routes_through_proxy(monkeypatch):
    # End-to-end: claude=on is an active injector, so the proxy routes the box through it (FY_PROXY)
    # AND its rule flows into the single egress-proxy daemon env — without any [proxy] table declared.
    monkeypatch.setattr(config, "claude_enabled", lambda: True)
    monkeypatch.setattr(config, "claude_keyless", lambda: "api-key")
    monkeypatch.setattr(config, "proxy_enabled", lambda: False)
    monkeypatch.setattr(config, "proxy_passthrough", lambda: [])
    monkeypatch.setattr(config, "proxy_default_deny", lambda: False)
    reg = Registry([claude.ClaudePlugin(), proxy.ProxyPlugin()])
    assert "FY_PROXY" in reg.derive_env({"claude": "on"})  # injector active → routed (no [proxy])
    assert reg.derive_env({"claude": "off"}) == {}  # off + no opt-in → clean box
    env = reg.desired_daemons({"claude": "on"})["egress-proxy"]["env"]
    assert env["INJECT_HOST"] == "api.anthropic.com" and env["INJECT_HEADER"] == "x-api-key"


def test_vscode_plugin_gated(monkeypatch):
    p = vscode.VscodePlugin()
    monkeypatch.setattr(config, "vscode_enabled", lambda: False)
    assert p.box_args(dict(_BOX_ENV)) == []
    monkeypatch.setattr(config, "vscode_enabled", lambda: True)
    assert "devbox_vscode_server:/home/vscode/.vscode-server" in p.box_args(dict(_BOX_ENV))


def test_codex_plugin_gated(tmp_path, monkeypatch):
    p = codex.CodexPlugin()
    monkeypatch.setattr(config, "codex_enabled", lambda: False)
    assert p.box_args(dict(_BOX_ENV)) == [] and p.box_bootstrap(dict(_BOX_ENV)) == []
    monkeypatch.setattr(config, "codex_enabled", lambda: True)
    assert "devbox_codex_home:/home/vscode/.codex" in p.box_args(dict(_BOX_ENV))  # ~/.codex auth
    sessions = tmp_path / "codex-sessions"
    args = p.box_args({**_BOX_ENV, "FY_CODEX_TRANSCRIPTS": str(sessions)})
    assert f"{sessions}:/home/vscode/.codex/sessions" in args
    assert sessions.is_dir()
    boot = p.box_bootstrap(dict(_BOX_ENV))
    # Native installer, never npm: the box contract has no node, and Debian's `nodejs` has no npm.
    # Warm-volume fast path first, so a recreated box relinks the cached release with no egress.
    assert boot[0]["check"] == "command -v codex"
    assert "chatgpt.com/codex/install.sh" in boot[0]["run"]
    assert "packages/standalone/current/bin/codex" in boot[0]["run"]
    assert "npm" not in boot[0]["run"]


def test_codex_keyless_inert_when_off(monkeypatch):
    monkeypatch.setattr(config, "codex_enabled", lambda: True)
    monkeypatch.setattr(config, "codex_keyless", lambda: "")
    p = codex.CodexPlugin()
    assert (
        p.axes() == []
        and p.proxy_rules({"codex": "on"}) == []
        and p.derive_env({"codex": "on"}) == {}
    )
    assert not any("OPENAI_API_KEY" in a for a in p.box_args(dict(_BOX_ENV)))  # no dummy baked


def test_codex_keyless_api_key_axis_rule_and_dummy(monkeypatch):
    monkeypatch.setattr(config, "codex_enabled", lambda: True)
    monkeypatch.setattr(config, "codex_keyless", lambda: "api-key")
    p = codex.CodexPlugin()
    axes = p.axes()
    assert len(axes) == 1 and axes[0].name == "codex" and axes[0].daemon == "egress-proxy"
    # off → nothing; on → the InjectRule (Authorization ← OPENAI_API_KEY, Bearer-prefixed) + marker.
    assert p.proxy_rules({"codex": "off"}) == [] and p.derive_env({"codex": "off"}) == {}
    rule = p.proxy_rules({"codex": "on"})[0]
    assert rule.host == "api.openai.com" and rule.header == "Authorization"
    assert rule.value_prefix == "Bearer " and rule.env == ("OPENAI_API_KEY",)
    assert "foldyard.plugins.static_token" in rule.minter and "OPENAI_API_KEY" in rule.minter
    assert p.derive_env({"codex": "on"}) == {"CODEX_INJECT": "on"}
    # The dummy is AMBIENT with the proxy substrate, NOT gated on the rung: `fy mode codex=on` is a
    # host-side flip, and a box-side artifact keyed to the rung could only be built at create time —
    # which is what made the flip silently require `fy box down && fy box up`.
    assert not any("OPENAI_API_KEY" in a for a in p.box_args(dict(_BOX_ENV)))  # no proxy substrate
    assert "OPENAI_API_KEY=sk-dummy" in p.box_args({**_BOX_ENV, "FY_PROXY": "1"})  # rung OFF
    assert "OPENAI_API_KEY=sk-dummy" in p.box_args(
        {**_BOX_ENV, "FY_PROXY": "1", "CODEX_INJECT": "on"}
    )


def test_codex_keyless_chatgpt_rule_and_dummy_auth_json(monkeypatch, tmp_path):
    # ChatGPT-subscription mode: the rule injects Authorization on chatgpt.com/backend-api/codex via
    # the codex_chatgpt_token minter (Bearer-prefixed), and box_bootstrap seeds a dummy auth.json
    # carrying the REAL account_id (read host-side from the Mac's ~/.codex/auth.json) — no env dummy.
    mac_auth = tmp_path / "auth.json"
    mac_auth.write_text(json.dumps({"auth_mode": "chatgpt", "tokens": {"account_id": "acc-42"}}))
    monkeypatch.setattr(config, "codex_enabled", lambda: True)
    monkeypatch.setattr(config, "codex_keyless", lambda: "chatgpt")
    monkeypatch.setattr(keyless, "codex_auth_json_path", lambda: mac_auth)
    p = codex.CodexPlugin()

    assert p.axes()[0].name == "codex"
    rule = p.proxy_rules({"codex": "on"})[0]
    assert rule.host == "chatgpt.com" and rule.path_prefix == "/backend-api/codex"
    assert rule.header == "Authorization" and rule.value_prefix == "Bearer "
    assert rule.replay_on_401 is True and rule.requires == ()
    assert "foldyard.plugins.codex_chatgpt_token" in rule.minter and str(mac_auth) in rule.minter

    # chatgpt mode bakes NO env dummy (the dummy is the auth.json, not OPENAI_API_KEY).
    assert not any("OPENAI_API_KEY" in a for a in p.box_args({**_BOX_ENV, "FY_PROXY": "1"}))
    # box_bootstrap seeds the dummy auth.json with the real account_id, base64-wrapped — ambient
    # with the proxy substrate, so the rung ("codex": off here) never gates it.
    steps = p.box_bootstrap({**_BOX_ENV, "FY_PROXY": "1"})
    seed = next(s for s in steps if s["label"] == "Codex keyless auth.json")
    payload = seed["run"].split("printf %s ")[1].split(" |")[0]
    decoded = json.loads(__import__("base64").b64decode(payload))
    assert decoded["auth_mode"] == "chatgpt" and decoded["tokens"]["account_id"] == "acc-42"
    assert decoded["tokens"]["refresh_token"].startswith("fy-dummy")  # never a real token
    # Keyless also disables the codex_apps MCP (it 401s — can't auth with the codex-scoped token).
    apps = next(s for s in steps if s["label"] == "Codex disable apps MCP (keyless)")
    assert apps["run"] == "codex features disable apps"
    # NOT present on a box with no proxy substrate (nothing could ever inject there)
    assert all(
        s["label"] != "Codex disable apps MCP (keyless)" for s in p.box_bootstrap(dict(_BOX_ENV))
    )


def test_claude_box_side_state_is_identical_across_the_rung(monkeypatch):
    # Claude's half of the same invariant codex is pinned on below: the rung decides only whether
    # the HOST proxy injects, so it must leave the box byte-identical — otherwise `fy mode
    # claude=on` lands and the box keeps whatever it was created with, silently.
    monkeypatch.setattr(config, "claude_enabled", lambda: True)
    monkeypatch.setattr(config, "claude_keyless", lambda: "oauth")
    p = claude.ClaudePlugin()
    off, on = {**_BOX_ENV, "FY_PROXY": "1"}, {**_BOX_ENV, "FY_PROXY": "1", "CLAUDE_INJECT": "on"}
    assert p.box_args(dict(off)) == p.box_args(dict(on))
    assert p.box_bootstrap(dict(off)) == p.box_bootstrap(dict(on))
    assert any(s["label"] == "Claude onboarding flag" for s in p.box_bootstrap(dict(off)))


def test_codex_box_side_state_is_identical_across_the_rung(monkeypatch, tmp_path):
    # THE regression: `fy mode codex=on` is a host-side decision (does the proxy inject?), so it
    # must change NOTHING about the box — otherwise the flip lands, the box keeps whatever it was
    # created with, and codex prompts you to sign in with no hint that a recreate is what's missing.
    # That cost a real debugging session, so pin the invariant rather than the two call sites.
    mac_auth = tmp_path / "auth.json"
    mac_auth.write_text(json.dumps({"auth_mode": "chatgpt", "tokens": {"account_id": "acc-42"}}))
    monkeypatch.setattr(config, "codex_enabled", lambda: True)
    monkeypatch.setattr(config, "codex_keyless", lambda: "chatgpt")
    monkeypatch.setattr(keyless, "codex_auth_json_path", lambda: mac_auth)
    p = codex.CodexPlugin()
    off, on = {**_BOX_ENV, "FY_PROXY": "1"}, {**_BOX_ENV, "FY_PROXY": "1", "CODEX_INJECT": "on"}
    assert p.box_args(dict(off)) == p.box_args(dict(on))
    assert p.box_bootstrap(dict(off)) == p.box_bootstrap(dict(on))
    # …and the seed IS there in the rung-off box, which is the whole point.
    assert any(s["label"] == "Codex keyless auth.json" for s in p.box_bootstrap(dict(off)))


def test_codex_keyless_chatgpt_without_mac_auth_fails_loudly(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "codex_enabled", lambda: True)
    monkeypatch.setattr(config, "codex_keyless", lambda: "chatgpt")
    monkeypatch.setattr(keyless, "codex_auth_json_path", lambda: tmp_path / "absent.json")
    steps = codex.CodexPlugin().box_bootstrap({**_BOX_ENV, "FY_PROXY": "1"})
    seed = next(s for s in steps if s["label"] == "Codex keyless auth.json")
    assert seed["check"] == "false" and "codex login" in seed["run"]  # can't seed → fail the step


def test_claude_and_codex_keyless_coexist_in_one_rule_set(monkeypatch):
    # The whole point of lifting the single-minter limit: claude=on AND codex=on at once produce
    # TWO rules in one egress-proxy daemon (INJECT_RULES), each its own host + minter — no collision.
    monkeypatch.setattr(config, "claude_enabled", lambda: True)
    monkeypatch.setattr(config, "claude_keyless", lambda: "api-key")
    monkeypatch.setattr(config, "codex_enabled", lambda: True)
    monkeypatch.setattr(config, "codex_keyless", lambda: "api-key")
    monkeypatch.setattr(config, "proxy_enabled", lambda: False)
    monkeypatch.setattr(config, "proxy_passthrough", lambda: [])
    monkeypatch.setattr(config, "proxy_default_deny", lambda: False)
    reg = Registry([claude.ClaudePlugin(), codex.CodexPlugin(), proxy.ProxyPlugin()])
    daemon = reg.desired_daemons({"claude": "on", "codex": "on"})["egress-proxy"]
    ruleset = {r["host"]: r for r in json.loads(daemon["env"]["INJECT_RULES"])}
    assert set(ruleset) == {"api.anthropic.com", "api.openai.com"}  # both injectors, one proxy
    assert ruleset["api.openai.com"]["header"] == "Authorization"
    assert ruleset["api.anthropic.com"]["header"] == "x-api-key"
    # Each rule carries its own minter env allowlist; neither gates the shared daemon's spawn.
    assert daemon["requires"] == []
    assert ruleset["api.openai.com"]["env"] == ["OPENAI_API_KEY"]
    assert ruleset["api.anthropic.com"]["env"] == ["ANTHROPIC_API_KEY"]


def test_capability_probe_unknown_axis_is_rejected():
    from foldyard.plugins import Axis, CapabilityProbe, Plugin, Registry

    class Bad(Plugin):
        name = "bad"

        def axes(self):
            return [Axis(name="real", rungs=("off", "on"), blurb={"off": "-", "on": "-"})]

        def capability_probes(self, mode):
            return [CapabilityProbe(axis="ghost", name="p", check=lambda: (True, "ok"))]

    with pytest.raises(ValueError, match="unknown axis 'ghost'"):
        Registry([Bad()]).capability_probes({"real": "on"})


def test_capability_probe_duplicate_names_are_rejected():
    from foldyard.plugins import Axis, CapabilityProbe, Plugin, Registry

    def _mk(name):
        class P(Plugin):
            def axes(self):
                return (
                    [Axis(name="ax", rungs=("off", "on"), blurb={"off": "-", "on": "-"})]
                    if name == "one"
                    else []
                )

            def capability_probes(self, mode):
                return [CapabilityProbe(axis="ax", name="dup", check=lambda: (True, "ok"))]

        P.name = name
        return P()

    with pytest.raises(ValueError, match="duplicate capability probe name 'dup'"):
        Registry([_mk("one"), _mk("two")]).capability_probes({"ax": "on"})
