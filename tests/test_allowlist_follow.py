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


def test_granting_again_keeps_the_targets_already_held(env):
    allowlist.grant_follow(SOURCE, [BLOB], "session")
    allowlist.grant_follow(SOURCE, [RECEIVER], "permanent")
    assert allowlist.live_follows() == {SOURCE: [BLOB, RECEIVER]}
    assert allowlist.follow_grants()[0]["level"] == "permanent"


def test_a_once_follow_expires_and_the_sweep_tidies_it(env):
    allowlist.grant_follow(SOURCE, [BLOB], "once", ttl=60)
    store = _store()
    store["follows"][SOURCE]["expires"] = "2000-01-01T00:00:00+00:00"
    config.allow_store_file().write_text(json.dumps(store))
    assert allowlist.live_follows() == {}
    assert allowlist.effective()["follow"] == []
    assert allowlist.sweep() is True
    assert _store()["follows"] == {}


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
        {SOURCE: {"to": "*.blob.core.windows.net"}},
        {SOURCE: {"to": []}},
        {SOURCE: {"to": ["*.com"]}},  # a target the CLI would refuse: damage, never a grant
        {"api.github.com": {"to": [BLOB]}},  # a whole-host source
        {SOURCE: {"to": [BLOB], "expires": "2000-01-01T00:00:00"}},  # naive: never compared
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
