"""The widenings inventory (`fy config widenings`) — what a config asks the HOST to allow.

Two jobs, and the tests split along them: report the standing decisions accurately (which hosts
escape capture, where each credential is delivered, who steers the agent), and WARN about config
that reads as a control but is no longer honoured. The second is the one that earns the doctor row:
a `[proxy] allow` list looks locked down and does nothing.
"""

from __future__ import annotations

import pytest

from foldyard import config, configpin, exposure

BASE = '[project]\nname = "acme"\n\n[proxy]\n'


@pytest.fixture
def checkout(tmp_path, monkeypatch):
    """A checkout + isolated host state; returns `write(toml, local=…) -> Config`."""
    repo, state = tmp_path / "repo", tmp_path / "state"
    repo.mkdir()
    monkeypatch.setenv("FOLDYARD_STATE_DIR", str(state))
    monkeypatch.setattr(config, "in_box", lambda: False)

    def write(toml: str, local: str = "") -> config.Config:
        (repo / "foldyard.toml").write_text(toml)
        if local:
            (repo / "foldyard.local.toml").write_text(local)
        return config.resolve(worktree="", repo=repo)

    return write


def collect(cfg, mode=None):
    with config.using(cfg):
        return exposure.collect(cfg, mode or {})


def rendered(cfg, mode=None) -> str:
    with config.using(cfg):
        return "\n".join(exposure.render(exposure.collect(cfg, mode or {})))


# ── capture exemptions ────────────────────────────────────────────────────────────────


def test_a_bundle_ref_is_counted_not_just_echoed(checkout):
    """The whole point: `@all` is one token that means ~200 hosts, and nothing else prints that."""
    exp = collect(checkout(BASE + 'passthrough = ["@all"]\n'))

    assert exp.hosts > 100 and exp.wildcards > 0
    assert exp.bundle_refs == ["@all"] and exp.literals == []
    assert f"{exp.hosts} hosts" in rendered(checkout(BASE + 'passthrough = ["@all"]\n'))


def test_an_undeclared_passthrough_reports_the_default_it_falls_back_to(checkout):
    body = rendered(checkout(BASE + "default_deny = true\n"))
    assert "not declared → defaults to @all" in body
    assert "194" in body or "hosts ·" in body  # the count, whatever the bundles currently hold


def test_an_empty_passthrough_says_everything_is_decrypted(checkout):
    exp = collect(checkout(BASE + "passthrough = []\n"))
    assert exp.hosts == 0
    assert "nothing exempt: capture=on decrypts everything" in rendered(
        checkout(BASE + "passthrough = []\n")
    )


def test_literal_hosts_are_named_because_they_are_the_repo_controlled_ones(checkout):
    cfg = checkout(BASE + 'passthrough = ["@python", "exfil.example", "*.corp.example"]\n')
    exp = collect(cfg)

    assert exp.literals == ["exfil.example", "*.corp.example"]
    assert "Declared literally here: exfil.example, *.corp.example" in rendered(cfg)


def test_an_unknown_bundle_ref_is_a_concern_because_it_silently_expands_to_nothing(checkout):
    cfg = checkout(BASE + 'passthrough = ["@pyton"]\n')
    exp = collect(cfg)

    assert exp.unknown_refs == ["pyton"] and exp.hosts == 0
    assert exp.concerns == ["unknown passthrough bundle @pyton"]
    assert exposure.doctor_row(exp)[0] is None  # WARN


# ── injection targets ─────────────────────────────────────────────────────────────────

INJECT = """
[[inject]]
axis = "penpot"
host = "penpot.example"
query_param = "userToken"
path_prefix = "/mcp"
"""


def test_a_declared_but_off_injector_is_still_reported(checkout):
    """A latent target is exactly what an operator needs to see BEFORE arming the axis."""
    cfg = checkout(BASE + "passthrough = []\n" + INJECT)
    target = next(t for t in collect(cfg).targets if "penpot" in t.source)

    assert target.host == "penpot.example/mcp"  # the path prefix bounds it — show it
    assert target.active is False
    assert target.from_config is True  # ← the bit that says "adopting a change re-points this"
    assert "$FY_INJECT_PENPOT" in target.source  # the DERIVED var, not any declared one

    armed = next(t for t in collect(cfg, {"penpot": "on"}).targets if "penpot" in t.source)
    assert armed.active is True


def test_packaged_targets_come_from_the_registry_not_from_guessed_constants(checkout):
    """Codex's ChatGPT rung injects on chatgpt.com/backend-api/codex, not the API host. A report
    that hardcoded hosts would name the wrong destination — so it asks the plugins."""
    cfg = checkout(BASE + 'passthrough = []\n\n[codex]\nkeyless = "chatgpt"\n')
    hosts = {t.host: t for t in collect(cfg).targets}

    target = hosts["chatgpt.com/backend-api/codex"]
    assert target.from_config is False  # fixed in package code
    assert "fy mode codex=on" in target.source  # …and how to arm it


def test_targets_name_the_file_that_declares_them(checkout):
    """ "Does this apply to my colleagues?" — foldyard.local.toml is gitignored and wins the merge."""
    cfg = checkout(BASE + "passthrough = []\n", local='[claude]\nkeyless = "oauth"\n')
    target = next(t for t in collect(cfg).targets if t.host == "api.anthropic.com")

    assert target.origin == exposure.LOCAL
    assert "yours only" in rendered(cfg)


def test_a_shared_agent_prompt_says_it_steers_everyone(checkout):
    cfg = checkout(BASE + 'passthrough = []\n\n[claude]\nsystem_prompt = "one\\ntwo\\n"\n')
    assert collect(cfg).prompts == [("[claude].system_prompt", 2, exposure.SHARED)]
    assert "prepended to every colleague's agent" in rendered(cfg)


def test_the_codex_prompt_is_inventoried_like_claudes(checkout):
    """Both agents take a repo-controlled prompt now (Codex's arrives as a developer instruction),
    and steering a privileged actor is the same exposure whichever CLI reads it."""
    cfg = checkout(
        BASE + 'passthrough = []\n\n[codex]\nsystem_prompt = "one\\ntwo\\n"\n',
        local='[codex]\nsystem_prompt = "mine\\n"\n',
    )
    assert collect(cfg).prompts == [("[codex].system_prompt", 1, exposure.LOCAL)]
    assert "prepended to your agent" in rendered(cfg)


def test_agent_cli_config_is_inventoried_beside_the_prompts(checkout):
    """`[claude.settings]` / `[codex.config]` reach the agent on the command line, and a `hooks` or
    `permissions` entry there steers it harder than any prompt — so they belong in the same
    section. Listed by KEY: Codex's as the flattened dotted paths the launcher really emits."""
    cfg = checkout(
        BASE + 'passthrough = []\n\n[claude.settings]\nmodel = "claude-opus-4-8"\n'
        "\n[codex.config.tui]\nraw_output_mode = false\n",
        local='[claude.settings]\nmodel = "mine"\n',
    )
    exp = collect(cfg)

    assert exp.agent_config == [
        ("[claude.settings]", ["model"], exposure.LOCAL),  # local wins the merge → yours only
        ("[codex.config]", ["tui.raw_output_mode"], exposure.SHARED),
    ]
    body = rendered(cfg)
    assert "1 setting passed to your agent on the command line: model." in body
    assert "tui.raw_output_mode" in body and "every colleague's agent" in body


# ── disabled blocks: the one statement whose effect is an absence ─────────────────────


def test_a_locally_disabled_block_is_reported_as_gone_and_as_yours_only(checkout):
    """A `disabled = true` deletes the block from the resolved config, so nothing downstream can
    show it — including the widening it used to declare. Unreported, "why is there no codex row?"
    would have no answer anywhere."""
    cfg = checkout(
        BASE + 'passthrough = []\n\n[codex]\nkeyless = "chatgpt"\n',
        local="[codex]\ndisabled = true\n",
    )
    exp = collect(cfg, {"codex": "on"})

    assert exp.disabled == [("[codex]", exposure.LOCAL)]
    assert not [t for t in exp.targets if "chatgpt.com" in t.host]  # the widening went with it
    body = rendered(cfg, {"codex": "on"})
    assert "removed from the resolved config, on this machine only" in body


def test_a_shared_disabled_block_says_it_is_off_for_everyone(checkout):
    cfg = checkout(BASE + 'passthrough = []\n\n[codex]\nkeyless = "chatgpt"\ndisabled = true\n')
    assert collect(cfg).disabled == [("[codex]", exposure.SHARED)]
    assert "for everyone" in rendered(cfg)


def test_nothing_disabled_means_no_section_at_all(checkout):
    cfg = checkout(BASE + "passthrough = []\n")
    assert collect(cfg).disabled == []
    assert "disabled blocks" not in rendered(cfg)


# ── ignored keys: the reason the row warns ────────────────────────────────────────────


def test_a_stale_proxy_allow_list_is_named_with_the_verb_that_replaced_it(checkout):
    """The finding this was built for: grants moved host-side, so a committed allow list looks
    locked down and does nothing."""
    cfg = checkout(BASE + 'allow = ["github.com", "pypi.org"]\npassthrough = []\n')
    exp = collect(cfg)

    key, values, fix, origin = exp.ignored[0]
    assert key == "[proxy] allow" and values == ["github.com", "pypi.org"]
    assert "fy allow add" in fix and origin == exposure.SHARED
    assert exp.concerns == ["`[proxy] allow` is IGNORED (2 entries)"]

    body = rendered(cfg)
    assert "nothing consumes them" in body and "github.com, pypi.org" in body


def test_a_declared_inject_token_env_is_named_as_ignored(checkout):
    """Derived from the axis since the audit's fourth round — a config that still declares one is
    pointing at a host.env var nothing reads."""
    cfg = checkout(BASE + "passthrough = []\n" + INJECT + 'token_env = "PENPOT_USER_TOKEN"\n')
    exp = collect(cfg)

    assert exp.ignored[0][0] == "[[inject]] token_env"
    assert exp.ignored[0][1] == ["penpot = PENPOT_USER_TOKEN"]


# ── the doctor row ────────────────────────────────────────────────────────────────────


def test_the_row_is_green_and_informative_when_nothing_is_stale(checkout):
    """A legitimate widening belongs in the report, not in a nag that never goes away."""
    cfg = checkout(BASE + 'passthrough = ["@all", "corp.example"]\n' + INJECT)
    ok, name, detail = exposure.doctor_row(collect(cfg, {"penpot": "on"}))

    assert ok is True and name == "config widenings"
    assert "hosts exempt from capture" in detail and "1 injector target live" in detail
    assert "fy config widenings" in detail


def test_the_row_warns_only_for_config_that_reads_as_a_control_and_is_not(checkout):
    cfg = checkout(BASE + 'allow = ["github.com"]\npassthrough = []\n')
    ok, _name, detail = exposure.doctor_row(collect(cfg))

    assert ok is None  # WARN, not fail: the posture is safe, the CONFIG is misleading
    assert "IGNORED" in detail


# ── which config it describes ─────────────────────────────────────────────────────────


def test_the_report_describes_the_adopted_config_not_the_working_tree(checkout):
    cfg = checkout(BASE + 'passthrough = ["@python"]\n')
    configpin.adopt(cfg)
    (cfg.repo_root / "foldyard.toml").write_text(BASE + 'passthrough = ["@all"]\n')

    adopted = configpin.effective(cfg)
    exp = collect(adopted)
    assert exp.bundle_refs == ["@python"]  # not @all — the checkout drifted, the host didn't
    assert exp.drifted is True
    assert "the checkout DIFFERS" in "\n".join(exposure.render(exp))


def test_in_the_box_it_reports_the_checkout_and_says_so(checkout, monkeypatch):
    cfg = checkout(BASE + 'passthrough = ["@python"]\n')
    configpin.adopt(cfg)
    monkeypatch.setattr(config, "in_box", lambda: True)

    body = rendered(config.resolve(worktree="", repo=cfg.repo_root))
    assert "box view: this checkout, not the copy the host adopted" in body


# ── recommended egress ([proxy] recommend — the repo asks, the host answers) ──────────


def test_recommended_hosts_report_their_answer_status(checkout):
    from foldyard import allowlist

    cfg = checkout(
        BASE + 'recommend = [{ host = "pypi.org", why = "box bootstrap" }, '
        '"unpkg.com", "never.example.com"]\n'
    )
    with config.using(cfg):
        allowlist.grant("pypi.org", "permanent")
        allowlist.decline("never.example.com")
    exp = collect(cfg)

    assert exp.recommended == [
        ("pypi.org", "box bootstrap", "granted"),
        ("unpkg.com", "", "pending"),
        ("never.example.com", "", "declined"),
    ]
    body = rendered(cfg)
    assert "recommended egress" in body and "fy allow sync" in body
    assert "box bootstrap" in body  # the why travels into the report


def test_a_pending_recommendation_rides_the_doctor_detail_not_a_warning(checkout):
    cfg = checkout(BASE + 'recommend = ["unpkg.com"]\n')
    ok, _name, detail = exposure.doctor_row(collect(cfg))
    assert ok is True  # an outstanding OFFER is not a misconfiguration
    assert "1 recommended host unanswered" in detail


def test_recommend_is_absent_from_the_report_when_not_declared(checkout):
    assert "recommended egress" not in rendered(checkout(BASE + "default_deny = true\n"))
