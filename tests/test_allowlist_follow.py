"""Follow grants in the host allow-store: `fy allow add <source> --redirects-to <target>` and a
`[proxy] recommend` entry with `redirects_to`.

A follow grant is kept apart from the host and path grants (`follows`, not `hosts`), so an older
foldyard reading the same store can't take one for a path grant on its source, and a proxy that
predates them never sees one in `allow` — it ignores `follow` and follows nothing. Its answers are
apart too: declining to follow a source's redirects never silences an offer to read under it.
"""

from __future__ import annotations

import json

import pytest

import test_allowlist as ta
from foldyard import allowlist, config

env = ta.env
_recommend = ta._recommend
_offer = ta._offer

SOURCE = "api.github.com/repos/twistco/foldyard/actions/"
BLOB = "*.blob.core.windows.net/actions-results/"
RECEIVER = "results-receiver.actions.githubusercontent.com/rest/runs/"


def _store() -> dict:
    return json.loads(config.allow_store_file().read_text())


def test_a_follow_grant_reaches_the_effective_file_and_grants_no_host(env):
    eff = allowlist.grant_follow(SOURCE, [BLOB, RECEIVER, BLOB], "permanent")
    assert eff["follow"] == [{"from": SOURCE, "to": [BLOB, RECEIVER]}]
    assert eff["allow"] == [] and eff["build_allow"] == []  # the source isn't granted by it
    assert json.loads(config.allow_effective_file().read_text())["follow"] == eff["follow"]
    assert _store().get("hosts", {}) == {}
    (g,) = allowlist.follow_grants()
    assert g["source"] == SOURCE and g["level"] == "permanent" and g["expires"] is None


def test_a_follow_grant_normalises_what_it_keeps(env):
    allowlist.grant_follow(
        " API.GitHub.com/repos/twistco/foldyard/actions/ ", ["*.Blob.Core.Windows.Net"], "session"
    )
    assert allowlist.live_follows() == {SOURCE: ["*.blob.core.windows.net"]}


@pytest.mark.parametrize(
    "source",
    [
        "api.github.com",  # a whole host: every redirect it can send
        "api.github.com/",
        "*.github.com/repos/",
        "api.github.com:443/repos/",
        "https://api.github.com/repos/",
        "api.github.com/repos/../x/",
    ],
)
def test_a_follow_source_must_be_one_path(env, source):
    with pytest.raises(SystemExit, match="not a valid redirect source"):
        allowlist.grant_follow(source, [BLOB], "session")
    assert allowlist.live_follows() == {}


@pytest.mark.parametrize(
    "target", ["*.com", "127.0.0.1", "10.0.0.5/x/", "localhost.", "blob.core.windows.net:443", "*"]
)
def test_a_follow_target_must_be_a_public_name_pattern(env, target):
    with pytest.raises(SystemExit, match="not a valid redirect target"):
        allowlist.grant_follow(SOURCE, [target], "session")
    assert allowlist.live_follows() == {}


def test_a_follow_needs_a_target(env):
    with pytest.raises(SystemExit, match="at least one target"):
        allowlist.grant_follow(SOURCE, [], "session")


def _levels() -> dict[str, str]:
    """Each followed target's level, for SOURCE."""
    return {t: g["level"] for g in allowlist.follow_grants() for t in g["to"]}


def test_each_target_keeps_the_level_it_was_granted_at(env):
    # A grant names the targets it is about; the ones it doesn't name keep their own lifetime —
    # a permanent answer about RECEIVER never makes a once BLOB permanent (it wasn't asked about).
    allowlist.grant_follow(SOURCE, [BLOB], "once", ttl=60)
    allowlist.grant_follow(SOURCE, [RECEIVER], "permanent")
    assert allowlist.live_follows() == {SOURCE: [BLOB, RECEIVER]}
    assert _levels() == {BLOB: "once", RECEIVER: "permanent"}
    (once,) = [g for g in allowlist.follow_grants() if g["level"] == "once"]
    assert once["expires"] is not None


def test_a_once_answer_to_a_recommendation_leaves_a_permanent_target_permanent(env):
    allowlist.grant_follow(SOURCE, [BLOB], "permanent")
    _recommend(env, f'[{{ host = "{SOURCE}", redirects_to = ["{BLOB}", "{RECEIVER}"] }}]')
    prompt, echo, lines = _offer(["o"])
    allowlist.offer_recommendations(interactive=True, prompt=prompt, echo=echo)
    assert _levels() == {BLOB: "permanent", RECEIVER: "once"}
    # The question names what the answer grants, and says what it leaves alone.
    asked = "\n".join(lines)
    assert f"allow redirects from {SOURCE} → {RECEIVER} (already" in asked
    assert f"already following {BLOB}" in asked


def test_granting_a_target_again_sets_its_level(env):
    # As `fy allow add host` does for a host: an explicit grant re-levels what it names.
    allowlist.grant_follow(SOURCE, [BLOB, RECEIVER], "permanent")
    allowlist.grant_follow(SOURCE, [BLOB], "session")
    assert _levels() == {BLOB: "session", RECEIVER: "permanent"}


def test_follow_grants_group_a_sources_targets_by_level(env):
    allowlist.grant_follow(SOURCE, [BLOB], "session")
    allowlist.grant_follow(SOURCE, [RECEIVER], "permanent")
    assert [(g["source"], g["level"], g["to"]) for g in allowlist.follow_grants()] == [
        (SOURCE, "permanent", [RECEIVER]),
        (SOURCE, "session", [BLOB]),
    ]


def test_a_once_follow_expires_and_the_sweep_tidies_it(env):
    allowlist.grant_follow(SOURCE, [BLOB], "once", ttl=60)
    store = _store()
    store["follows"][SOURCE][BLOB]["expires"] = "2000-01-01T00:00:00+00:00"
    config.allow_store_file().write_text(json.dumps(store))
    assert allowlist.live_follows() == {}
    assert allowlist.effective()["follow"] == []
    assert allowlist.sweep() is True
    assert _store()["follows"] == {}


def test_a_lapsed_once_target_leaves_the_sources_other_targets_followed(env):
    allowlist.grant_follow(SOURCE, [BLOB], "once", ttl=60)
    allowlist.grant_follow(SOURCE, [RECEIVER], "permanent")
    store = _store()
    store["follows"][SOURCE][BLOB]["expires"] = "2000-01-01T00:00:00+00:00"
    config.allow_store_file().write_text(json.dumps(store))
    assert allowlist.effective()["follow"] == [{"from": SOURCE, "to": [RECEIVER]}]
    assert allowlist.sweep() is True
    assert list(_store()["follows"][SOURCE]) == [RECEIVER]


def test_the_sweep_never_restores_a_follow_revoked_while_it_ran(env, monkeypatch):
    # Another writer lands between the sweep's read and its write: the sweep saves what the
    # store holds then, with only the lapsed grants taken out — never its earlier snapshot.
    allowlist.grant_follow(SOURCE, [BLOB], "permanent")
    allowlist.grant_follow("objects.example.com/dl/", ["cdn.example.net"], "once", ttl=60)
    store = _store()
    store["follows"]["objects.example.com/dl/"]["cdn.example.net"]["expires"] = (
        "2000-01-01T00:00:00+00:00"
    )
    config.allow_store_file().write_text(json.dumps(store))
    checked = allowlist._checked_raw

    def read_then_revoke() -> dict:
        snapshot = checked()
        monkeypatch.setattr(allowlist, "_checked_raw", checked)
        allowlist.revoke_follow(SOURCE)  # the concurrent `fy allow remove`
        return snapshot

    monkeypatch.setattr(allowlist, "_checked_raw", read_then_revoke)
    assert allowlist.sweep() is True
    assert _store()["follows"] == {}
    assert allowlist.live_follows() == {}


def test_a_supervisor_restart_ends_session_follows_and_keeps_permanent_ones(env):
    allowlist.grant_follow(SOURCE, [BLOB], "session")
    allowlist.grant_follow("objects.example.com/dl/", ["cdn.example.net"], "permanent")
    allowlist.clear_ephemeral()
    assert allowlist.live_follows() == {"objects.example.com/dl/": ["cdn.example.net"]}


def test_remove_takes_the_follow_and_the_path_grant_revoke_follow_only_the_follow(env):
    allowlist.grant(SOURCE, "permanent")
    allowlist.grant_follow(SOURCE, [BLOB], "permanent")
    allowlist.revoke_follow(SOURCE)
    assert allowlist.live_follows() == {} and allowlist.live_hosts() == [SOURCE]
    allowlist.grant_follow(SOURCE, [BLOB], "permanent")
    allowlist.revoke(SOURCE)
    assert allowlist.live_follows() == {} and allowlist.live_hosts() == []


def test_a_host_grant_leaves_follows_alone(env):
    allowlist.grant_follow(SOURCE, [BLOB], "permanent")
    allowlist.grant("pypi.org", "permanent")
    allowlist.revoke("pypi.org")
    assert allowlist.live_follows() == {SOURCE: [BLOB]}


@pytest.mark.parametrize(
    "follows",
    [
        [],
        {SOURCE: [BLOB]},
        {SOURCE: {}},
        {SOURCE: {BLOB: "permanent"}},
        {SOURCE: {"to": [BLOB], "level": "permanent"}},  # one level for a list: not this store
        {SOURCE: {"*.com": {"level": "session"}}},  # a target the CLI would refuse: damage
        {"api.github.com": {BLOB: {"level": "session"}}},  # a whole-host source
        {SOURCE: {BLOB: {"level": "forever"}}},
        {SOURCE: {BLOB: {"level": "once", "expires": "2000-01-01T00:00:00"}}},  # naive time
    ],
)
def test_a_damaged_follow_table_fails_closed(env, follows):
    allowlist.grant("pypi.org", "permanent")
    store = _store()
    store["follows"] = follows
    config.allow_store_file().write_text(json.dumps(store))
    assert allowlist.effective()["follow"] == []
    assert allowlist.live_hosts() == []  # the whole store reads as damaged, as for `hosts`
    assert allowlist.default_deny() is True
    with pytest.raises(SystemExit):
        allowlist.grant_follow(SOURCE, [BLOB], "session")


def test_the_box_cannot_make_a_follow_grant(env, monkeypatch):
    monkeypatch.setattr(config, "in_box", lambda: True)
    with pytest.raises(SystemExit, match="your computer only"):
        allowlist.grant_follow(SOURCE, [BLOB], "session")


# ── `[proxy] recommend` with `redirects_to` ───────────────────────────────────────────────────


def test_recommend_parses_a_follow_entry_and_drops_a_bad_one_whole(env):
    _recommend(
        env,
        "["
        f'{{ host = "{SOURCE}", redirects_to = ["{BLOB}", "{RECEIVER}"], why = "CI logs" }}, '
        f'{{ host = "{SOURCE}", why = "read it too" }}, '
        '{ host = "cdn.example.com/dl/", redirects_to = "objects.example.net" }, '
        '{ host = "api.github.com", redirects_to = ["x.example.net"] }, '
        f'{{ host = "{SOURCE}x/", redirects_to = ["{BLOB}", "*.com"] }}, '
        f'{{ host = "{SOURCE}y/", redirects_to = ["{BLOB}"], when = "build" }}, '
        f'{{ host = "{SOURCE}z/", redirects_to = 7 }}'
        "]",
    )
    assert config.proxy_recommend() == [
        {"host": SOURCE, "why": "CI logs", "when": "", "redirects_to": [BLOB, RECEIVER]},
        {"host": SOURCE, "why": "read it too", "when": ""},  # a path grant on the same key
        {
            "host": "cdn.example.com/dl/",
            "why": "",
            "when": "",
            "redirects_to": ["objects.example.net"],
        },
    ]
    dropped = config.proxy_recommend_dropped()
    assert len(dropped) == 4 and all("redirects_to" in d for d in dropped)


def test_a_follow_recommendation_is_offered_granted_as_a_follow_and_declined_apart(env):
    _recommend(
        env,
        f'[{{ host = "{SOURCE}", redirects_to = ["{BLOB}"], why = "CI logs" }}, "{SOURCE}"]',
    )
    prompt, echo, lines = _offer(["y", "v"])
    counts = allowlist.offer_recommendations(interactive=True, prompt=prompt, echo=echo)
    assert counts == {"granted": 1, "declined": 1, "deferred": 0}
    assert allowlist.live_follows() == {SOURCE: [BLOB]}
    assert allowlist.live_hosts() == []  # the follow granted no path; the path grant declined
    asked = "\n".join(lines)
    assert f"redirects from {SOURCE} → {BLOB}" in asked and "the source chooses it" in asked
    assert allowlist.pending_recommendations() == []

    # Declining the follow leaves the path grant's own offer alone, and the other way round.
    allowlist.revoke_follow(SOURCE)
    store = _store()
    store["declined"] = {}
    config.allow_store_file().write_text(json.dumps(store))
    allowlist.decline_follow(SOURCE)
    assert [allowlist.describe(e) for e in allowlist.pending_recommendations()] == [SOURCE]


def test_a_recommendation_that_adds_a_target_is_asked_again(env):
    allowlist.grant_follow(SOURCE, [BLOB], "permanent")
    _recommend(env, f'[{{ host = "{SOURCE}", redirects_to = ["{BLOB}"] }}]')
    assert allowlist.pending_recommendations() == []
    _recommend(env, f'[{{ host = "{SOURCE}", redirects_to = ["{BLOB}", "{RECEIVER}"] }}]')
    (e,) = allowlist.pending_recommendations()
    assert e["redirects_to"] == [BLOB, RECEIVER]
    allowlist.accept(e, "permanent")
    assert allowlist.live_follows() == {SOURCE: [BLOB, RECEIVER]}


def test_sync_yes_grants_a_follow_recommendation(env):
    _recommend(env, f'[{{ host = "{SOURCE}", redirects_to = ["{BLOB}"] }}, "pypi.org"]')
    prompt, echo, _lines = _offer([])
    allowlist.offer_recommendations(interactive=False, prompt=prompt, echo=echo, accept_all=True)
    assert allowlist.live_follows() == {SOURCE: [BLOB]}
    assert allowlist.live_hosts() == ["pypi.org"]


def test_widenings_report_a_follow_recommendation_by_its_own_answer(env):
    from foldyard import exposure

    _recommend(env, f'[{{ host = "{SOURCE}", redirects_to = ["{BLOB}"] }}, "{SOURCE}"]')
    allowlist.grant(SOURCE, "permanent")  # the path grant answers the path ask only
    exp = exposure.collect(config.resolve(), {})
    assert [(h, s) for h, _w, s in exp.recommended] == [
        (f"redirects from {SOURCE} → {BLOB}", "pending"),
        (SOURCE, "granted"),
    ]
    allowlist.grant_follow(SOURCE, [BLOB], "session")
    exp = exposure.collect(config.resolve(), {})
    assert [s for _h, _w, s in exp.recommended] == ["granted", "granted"]


def test_never_to_a_follow_leaves_the_same_keys_path_offer_alone(env):
    _recommend(env, f'[{{ host = "{SOURCE}", redirects_to = ["{BLOB}"] }}, "{SOURCE}"]')
    prompt, echo, _lines = _offer(["v", ""])  # never follow; the path grant: not now
    allowlist.offer_recommendations(interactive=True, prompt=prompt, echo=echo)
    assert [allowlist.describe(e) for e in allowlist.pending_recommendations()] == [SOURCE]
    assert SOURCE in _store()["declined_follows"] and SOURCE not in _store().get("declined", {})


def test_allow_add_names_the_level_of_the_targets_it_granted_alone(env):
    from typer.testing import CliRunner

    from foldyard import cli

    allowlist.grant_follow(SOURCE, [BLOB], "permanent")
    out = CliRunner().invoke(
        cli.app, ["allow", "add", SOURCE, "--redirects-to", RECEIVER, "--level", "session"]
    )
    assert out.exit_code == 0, out.output
    assert f"followed to {RECEIVER} (session)" in out.output
    assert f"also following {BLOB}" in out.output
