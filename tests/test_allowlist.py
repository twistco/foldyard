"""The egress allow-store: grants at once/session/permanent levels, TTL expiry, the effective file
the proxy reads, and the box-can't-grant guard — which is why EVERY level lives in the Mac-home
store, `foldyard.toml` included (a permanent grant in repo config is one the box can write itself)."""

from __future__ import annotations

import json

import pytest

from foldyard import allowlist, config


@pytest.fixture
def env(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "foldyard.toml").write_text("[proxy]\ndefault_deny = true\n")
    state = tmp_path / "state"
    state.mkdir()
    monkeypatch.setenv("FOLDYARD_REPO", str(repo))
    monkeypatch.setenv("FOLDYARD_STATE_DIR", str(state))
    monkeypatch.setattr(config, "in_box", lambda: False)
    config.clear_caches()
    yield {"repo": repo, "state": state}
    config.clear_caches()


def _expire_in_past(host):
    store = json.loads(config.allow_store_file().read_text())
    store["hosts"][host]["expires"] = "2000-01-01T00:00:00+00:00"
    config.allow_store_file().write_text(json.dumps(store))


def test_effective_reads_the_flag_and_only_the_host_store(env):
    eff = allowlist.effective()
    assert eff["default_deny"] is True  # the on/off switch stays consumer config…
    assert eff["allow"] == []  # …but no grant comes from the repo, at any level
    # A `[proxy] allow` list in repo config grants NOTHING — the box can write that file.
    (env["repo"] / "foldyard.toml").write_text(
        '[proxy]\ndefault_deny = true\nallow = ["box.wrote.this"]\n'
    )
    config.clear_caches()
    assert allowlist.effective()["allow"] == []


def test_grant_session_lands_in_effective_file(env):
    allowlist.grant("api.example.com", "session")
    assert "api.example.com" in allowlist.live_hosts()
    eff = json.loads(config.allow_effective_file().read_text())
    assert "api.example.com" in eff["allow"]


def test_grant_once_expires(env):
    allowlist.grant("once.example.com", "once", ttl=120)
    assert "once.example.com" in allowlist.live_hosts()
    _expire_in_past("once.example.com")
    assert "once.example.com" not in allowlist.live_hosts()


def test_sweep_drops_expired(env):
    allowlist.grant("once.example.com", "once", ttl=120)
    _expire_in_past("once.example.com")
    assert allowlist.sweep() is True
    assert allowlist.sweep() is False  # nothing left to expire


def test_grant_permanent_lands_in_the_host_store_never_the_repo(env):
    allowlist.grant("perm.example.com", "permanent")
    assert "perm.example.com" in allowlist.effective()["allow"]
    store = json.loads(config.allow_store_file().read_text())
    assert store["hosts"]["perm.example.com"]["expires"] is None  # no TTL, unlike `once`
    assert "perm.example.com" not in (env["repo"] / "foldyard.toml").read_text()


def test_box_cannot_grant(env, monkeypatch):
    monkeypatch.setattr(config, "in_box", lambda: True)
    with pytest.raises(SystemExit):
        allowlist.grant("x.example.com", "session")


def test_clear_ephemeral_keeps_permanent(env):
    allowlist.grant("sess.example.com", "session")
    allowlist.grant("perm.example.com", "permanent")
    allowlist.clear_ephemeral()  # supervisor start: "until restart" grants end here
    assert allowlist.live_hosts() == ["perm.example.com"]
    assert "sess.example.com" not in allowlist.effective()["allow"]


def test_revoke_session_and_permanent(env):
    allowlist.grant("sess.example.com", "session")
    allowlist.grant("perm.example.com", "permanent")
    allowlist.revoke("sess.example.com")
    allowlist.revoke("perm.example.com")
    assert allowlist.live_hosts() == []


def test_grant_rejects_bad_level_and_host(env):
    with pytest.raises(SystemExit):
        allowlist.grant("ok.example.com", "forever")
    with pytest.raises(SystemExit):
        allowlist.grant("not a host", "session")


@pytest.mark.parametrize(
    "host,ok",
    [
        ("example.com", True),
        ("*.example.com", True),
        ("sub.api.example.com", True),
        ("nodot", False),
        ("has space.com", False),
        ("http://x.com", False),
        ("a/b.com", False),
        ("", False),
        # host:port — one CONNECT tunnel port; what the blocked row carries when the port was
        # the reason, so `fy allow add github.com:22` and the TUI's allow action both take it.
        ("github.com:22", True),
        ("*.internal.example:8443", True),
        ("github.com:0", False),
        ("github.com:65536", False),
        ("github.com:ssh", False),
        ("github.com:", False),
        (":22", False),
        ("a:github.com:22", False),  # one separator: the host part keeps no colon
        ("[::1]:22", False),
    ],
)
def test_valid_host(host, ok):
    assert allowlist.valid_host(host) is ok


def test_enforcement_is_host_owned_once_set(env):
    # `[proxy] default_deny` only SEEDS the answer: repo config is writable from inside the box, so a
    # committed enforcement switch is one the yard can flip off for itself — strictly worse than the
    # per-host grants, since it drops the wall entirely rather than widening it by one host.
    assert allowlist.default_deny() is True  # seeded from the repo's [proxy] default_deny
    allowlist.set_wall(True)  # an operator pins it host-side
    (env["repo"] / "foldyard.toml").write_text("[proxy]\ndefault_deny = false\n")
    config.clear_caches()
    assert allowlist.default_deny() is True  # the box's edit changes nothing
    assert allowlist.effective()["default_deny"] is True
    assert json.loads(config.allow_effective_file().read_text())["default_deny"] is True
    # …and the operator can still turn it off, from the host.
    allowlist.set_wall(False)
    assert allowlist.default_deny() is False


def test_box_cannot_set_the_wall(env, monkeypatch):
    monkeypatch.setattr(config, "in_box", lambda: True)
    with pytest.raises(SystemExit):
        allowlist.set_wall(False)


def test_a_damaged_store_fails_closed_and_refuses_to_be_rebuilt(env, capsys):
    # This file backs a security control, so its own damage must never weaken it: enforcement can't
    # fall back to `[proxy] default_deny` (repo config the box writes), and grants read as none.
    # Mutators refuse rather than rebuild — a rebuild would silently drop every grant it held.
    allowlist.grant("keep.example.com", "permanent")
    config.allow_store_file().write_text("{ not json")
    assert allowlist.default_deny() is True
    assert allowlist.live_hosts() == []
    assert allowlist.sweep() is False  # the supervisor tick survives it
    with pytest.raises(SystemExit, match="Repair or delete"):
        allowlist.grant("new.example.com", "session")
    assert "unreadable" in capsys.readouterr().err


def test_a_missing_store_is_just_first_run(env):
    # Absent ≠ damaged: no file means nothing has been granted yet, and the repo's seed still sets
    # the starting enforcement position.
    assert not config.allow_store_file().exists()
    assert allowlist.live_hosts() == []
    assert allowlist.default_deny() is True  # from the [proxy] default_deny seed
    allowlist.grant("ok.example.com", "session")  # and mutating works


def test_writes_are_atomic_so_a_reader_never_sees_a_torn_file(env):
    # The proxy addon re-reads the effective file PER REQUEST; a truncate+write would let it observe
    # an empty allowlist mid-write and spuriously block egress (it fails closed).
    allowlist.grant("a.example.com", "permanent")
    for path in (config.allow_store_file(), config.allow_effective_file()):
        assert json.loads(path.read_text())  # complete JSON after the write
        assert not list(path.parent.glob(f".{path.name}.tmp"))  # temp cleaned up by os.replace


@pytest.mark.parametrize(
    "doc,why",
    [
        ('{"hosts": []}', "hosts is not an object"),
        ('{"hosts": {"a.test": "session"}}', "an entry isn't an object"),
        ('{"default_deny": "yes"}', "default_deny isn't a bool"),
    ],
)
def test_wrong_shaped_fields_are_damage_not_defaults(env, doc, why):
    # Present-but-wrong-shaped is damage, not "unset". Reading it as empty would silently drop every
    # grant on the next write, and a non-bool default_deny falling through to the repo seed would let
    # a mangled store hand enforcement back to config the box can write.
    config.allow_store_file().write_text(doc)
    assert allowlist.default_deny() is True, why
    assert allowlist.live_hosts() == []
    with pytest.raises(SystemExit, match="Repair or delete"):
        allowlist.grant("new.example.com", "session")


# ── repo-recommended grants ([proxy] recommend → the host offers, the operator answers) ──


def _recommend(env, entries: str) -> None:
    """Point the bound config's [proxy] recommend at ``entries`` (TOML inline)."""
    (env["repo"] / "foldyard.toml").write_text(
        f"[proxy]\ndefault_deny = true\nrecommend = {entries}\n"
    )
    config.clear_caches()


def _offer(answers: list[str]):
    """(prompt, echo, lines) driving offer_recommendations with canned per-host answers.
    ``lines`` records BOTH streams — the question is where the host+why appear, so tests
    assert against the whole exchange."""
    lines: list[str] = []
    queue = iter(answers)

    def prompt(q: str) -> str:
        lines.append(q)
        return next(queue)

    return prompt, lines.append, lines


def test_recommend_parses_tables_strings_and_drops_junk(env):
    _recommend(
        env,
        '[{ host = "pypi.org", why = "box bootstrap" }, "unpkg.com", '
        '{ host = "not a host" }, { why = "no host" }, 42, "pypi.org"]',
    )
    assert config.proxy_recommend() == [
        {"host": "pypi.org", "why": "box bootstrap"},
        {"host": "unpkg.com", "why": ""},
    ]


def test_pending_excludes_granted_and_declined(env):
    _recommend(env, '["a.example.com", "b.example.com", "c.example.com"]')
    allowlist.grant("a.example.com", "session")
    allowlist.decline("b.example.com")
    assert [e["host"] for e in allowlist.pending_recommendations()] == ["c.example.com"]


def test_granting_clears_a_decline_so_the_operator_can_change_their_mind(env):
    _recommend(env, '["a.example.com"]')
    allowlist.decline("a.example.com")
    assert allowlist.pending_recommendations() == []
    allowlist.grant("a.example.com", "permanent")
    store = json.loads(config.allow_store_file().read_text())
    assert "a.example.com" not in store.get("declined", {})
    assert allowlist.pending_recommendations() == []  # now granted, still not re-offered


def test_offer_grants_declines_and_defers_per_answer(env):
    _recommend(
        env,
        '[{ host = "a.example.com", why = "deps" }, "b.example.com", '
        '"c.example.com", "d.example.com"]',
    )
    prompt, echo, lines = _offer(["y", "v", "", "s"])
    counts = allowlist.offer_recommendations(interactive=True, prompt=prompt, echo=echo)

    assert counts == {"granted": 2, "declined": 1, "deferred": 1}
    eff = allowlist.effective()["allow"]
    assert "a.example.com" in eff and "d.example.com" in eff  # y → permanent, s → session
    assert "b.example.com" not in eff and "c.example.com" not in eff
    # The declined host stays quiet; the deferred one is offered again.
    assert [e["host"] for e in allowlist.pending_recommendations()] == ["c.example.com"]
    assert any("deps" in line for line in lines)  # the why is shown at the decision moment


def test_offer_without_a_terminal_changes_nothing_and_points_at_sync(env):
    _recommend(env, '["a.example.com"]')
    prompt, echo, lines = _offer([])  # would raise if the prompt were consulted
    counts = allowlist.offer_recommendations(interactive=False, prompt=prompt, echo=echo)
    assert counts == {"granted": 0, "declined": 0, "deferred": 1}
    assert allowlist.effective()["allow"] == []
    assert any("fy allow sync" in line for line in lines)


def test_sync_yes_grants_every_pending_without_asking(env):
    """The unattended path (`fy allow sync --yes`): a scripted first box-up has no terminal to
    answer on, so the operator declares the bulk yes up front instead."""
    _recommend(env, '["a.example.com", "b.example.com"]')
    prompt, echo, lines = _offer([])  # would raise if any prompt were consulted
    counts = allowlist.offer_recommendations(
        interactive=False, prompt=prompt, echo=echo, accept_all=True
    )
    assert counts == {"granted": 2, "declined": 0, "deferred": 0}
    assert set(allowlist.effective()["allow"]) == {"a.example.com", "b.example.com"}
    assert allowlist.pending_recommendations() == []
    assert any("permanent" in line for line in lines)


def test_plugins_recommend_their_own_install_hosts(env):
    """A declared `[claude]` contributes the hosts ITS installer reaches — packaged in the plugin,
    never scaffolded into foldyard.toml — and only while that table is declared. Same consent path:
    declaring the agent ASKS, it does not grant."""
    _recommend(env, '["a.example.com"]')
    assert [e["host"] for e in allowlist.recommendations()] == ["a.example.com"]

    (env["repo"] / "foldyard.toml").write_text(
        '[proxy]\ndefault_deny = true\nrecommend = ["a.example.com"]\n[claude]\n'
    )
    config.clear_caches()
    entries = allowlist.recommendations()
    hosts = [e["host"] for e in entries]
    assert hosts[0] == "a.example.com"  # the repo's own list is offered first
    assert {"claude.ai", "downloads.claude.ai"} <= set(hosts)
    assert all(e["why"] for e in entries if e["host"] != "a.example.com")  # each carries its why
    # This `[claude]` is BARE, so it's the manual-login box: no injector exists to exempt the API
    # host structurally, and a wall that blocks it leaves you logged in and unable to work.
    assert "api.anthropic.com" in hosts
    assert "claude.ai" in [e["host"] for e in allowlist.pending_recommendations()]
    assert allowlist.effective()["allow"] == []  # …still granted by nothing but an answer

    # Under keyless it drops back off: the proxy has to reach it to mint, so it's already exempt
    # and offering it would ask for a grant that grants nothing.
    (env["repo"] / "foldyard.toml").write_text(
        '[proxy]\ndefault_deny = true\nrecommend = ["a.example.com"]\n[claude]\nkeyless = "oauth"\n'
    )
    config.clear_caches()
    hosts = [e["host"] for e in allowlist.recommendations()]
    assert "claude.ai" in hosts and "api.anthropic.com" not in hosts


def test_box_cannot_answer_recommendations(env, monkeypatch):
    _recommend(env, '["a.example.com"]')
    monkeypatch.setattr(config, "in_box", lambda: True)
    prompt, echo, lines = _offer(["y"])
    counts = allowlist.offer_recommendations(interactive=True, prompt=prompt, echo=echo)
    assert counts == {"granted": 0, "declined": 0, "deferred": 0} and lines == []
    # …and not through the unattended door either: the box must not grant its own egress.
    counts = allowlist.offer_recommendations(
        interactive=False, prompt=prompt, echo=echo, accept_all=True
    )
    assert counts == {"granted": 0, "declined": 0, "deferred": 0} and lines == []
    with pytest.raises(SystemExit):
        allowlist.decline("a.example.com")


def test_a_damaged_store_stops_offers_and_a_malformed_declined_is_damage(env):
    _recommend(env, '["a.example.com"]')
    config.allow_store_file().write_text("{broken")
    assert allowlist.pending_recommendations() == []  # fail closed: no offers on a broken store
    config.allow_store_file().write_text('{"declined": ["a.example.com"]}')
    assert allowlist.live_hosts() == []  # damage, not defaults — same posture as hosts damage
    assert allowlist.pending_recommendations() == []


# ── the learn window (observe, record, then enforce by itself) ─────────────────────────


def _at(monkeypatch, iso: str) -> None:
    """Pin allowlist's clock — the window's deadline is compared against it."""
    from datetime import datetime

    monkeypatch.setattr(allowlist, "_now", lambda: datetime.fromisoformat(iso))


def _seed(env, value: str) -> None:
    (env["repo"] / "foldyard.toml").write_text(f"[proxy]\ndefault_deny = {value}\n")
    config.clear_caches()


def test_a_learn_window_observes_then_enforces_by_itself(env, monkeypatch):
    # The property the whole feature rests on: a window cannot be forgotten into an open wall.
    # Even an operator who had the wall OFF comes back to enforcing when the window lapses.
    _at(monkeypatch, "2026-09-22T10:00:00+00:00")
    allowlist.set_wall(False)
    window = allowlist.start_learning(3600)
    assert window["until"] == "2026-09-22T11:00:00+00:00"
    assert allowlist.default_deny() is False and allowlist.learning() == window

    _at(monkeypatch, "2026-09-22T11:00:01+00:00")
    assert allowlist.default_deny() is True  # the deadline alone restores enforcement
    assert allowlist.learning() is None
    assert allowlist.sweep() is True  # …and the tick tidies the window into its record
    raw = json.loads(config.allow_store_file().read_text())
    assert "learn" not in raw and raw["learned"]["since"] == "2026-09-22T10:00:00+00:00"
    assert allowlist.last_window() == raw["learned"]
    assert allowlist.sweep() is False


def test_a_learn_window_is_capped(env, monkeypatch):
    _at(monkeypatch, "2026-09-22T10:00:00+00:00")
    window = allowlist.start_learning(10 * 24 * 3600)
    assert window["until"] == "2026-09-22T18:00:00+00:00"  # LEARN_MAX_SECONDS (8h)


def test_wall_on_or_off_ends_a_window_early_but_keeps_its_record(env, monkeypatch):
    _at(monkeypatch, "2026-09-22T10:00:00+00:00")
    allowlist.start_learning(3600)
    _at(monkeypatch, "2026-09-22T10:20:00+00:00")
    allowlist.set_wall(True)
    assert allowlist.learning() is None and allowlist.default_deny() is True
    assert allowlist.last_window() == {
        "since": "2026-09-22T10:00:00+00:00",
        "until": "2026-09-22T10:20:00+00:00",  # ended now, not at its old deadline
    }


def test_box_cannot_open_a_learn_window(env, monkeypatch):
    monkeypatch.setattr(config, "in_box", lambda: True)
    with pytest.raises(SystemExit):
        allowlist.start_learning()
    assert allowlist.seed_learning(print) is None


@pytest.mark.parametrize(
    "value,seed,enforcing",
    [
        ("true", "on", True),
        ("false", "off", False),
        ('"learn"', "learn", True),  # enforces until a launch verb opens the window
        ('"lern"', "on", True),  # a typo in a loosening control must not loosen it
        ("0", "off", False),  # the historical bool() truthiness is kept
    ],
)
def test_the_default_deny_seed(env, value, seed, enforcing):
    _seed(env, value)
    assert config.proxy_default_deny_seed() == seed
    assert allowlist.default_deny() is enforcing


def test_a_learn_seed_opens_one_window_on_first_launch_only(env, monkeypatch):
    _at(monkeypatch, "2026-09-22T10:00:00+00:00")
    _seed(env, '"learn"')
    said: list[str] = []
    window = allowlist.seed_learning(said.append)
    assert window is not None and allowlist.default_deny() is False
    assert "LEARNING" in said[0] and "fy allow learn" in said[0]
    # Once is the rule: an open window, a closed one, or any stored answer means no second window.
    assert allowlist.seed_learning(said.append) is None
    _at(monkeypatch, "2026-09-22T12:00:00+00:00")
    allowlist.sweep()
    assert allowlist.seed_learning(said.append) is None and allowlist.default_deny() is True
    assert len(said) == 1


def test_a_learn_seed_never_overrides_an_operator_answer(env):
    _seed(env, '"learn"')
    allowlist.set_wall(True)
    assert allowlist.seed_learning(print) is None
    assert allowlist.default_deny() is True


def test_only_a_true_or_learn_seed_needs_no_window(env):
    for value in ("true", "false"):
        _seed(env, value)
        assert allowlist.seed_learning(print) is None


@pytest.mark.parametrize(
    "doc",
    [
        '{"learn": "yes"}',
        '{"learn": {"since": "2026-09-22T10:00:00+00:00"}}',
        '{"learned": {"since": "x", "until": "y"}}',
    ],
)
def test_a_malformed_window_is_damage_and_enforces(env, doc):
    # A window SUSPENDS enforcement, so a half-written one must not read as open.
    config.allow_store_file().write_text(doc)
    assert allowlist.default_deny() is True
    assert allowlist.learning() is None


def _row(host: str, ts: str, ua: str = "", **extra) -> dict:
    return {"ts": ts, "host": host, "would_block": True, **({"ua": ua} if ua else {}), **extra}


def test_learned_hosts_groups_the_window_and_drops_answered_hosts(env):
    window = {"since": "2026-09-22T10:00:00+00:00", "until": "2026-09-22T11:00:00+00:00"}
    allowlist.grant("*.granted.dev", "permanent")
    allowlist.decline("never.example.com")
    rows = [
        _row("before.example.com", "2026-09-22T09:59:59+00:00"),  # outside the window
        _row("registry.npmjs.org", "2026-09-22T10:01:00+00:00", "npm/10.8.2 node/v22"),
        _row("registry.npmjs.org", "2026-09-22T10:02:00+00:00", "npm/10.8.2 node/v22"),
        _row("registry.npmjs.org", "2026-09-22T10:03:00+00:00", "pnpm/9.1.0"),
        {"ts": "2026-09-22T10:04:00+00:00", "host": "seen.example.com", "status": 200},
        _row("api.granted.dev", "2026-09-22T10:05:00+00:00"),  # granted since → not offered
        _row("never.example.com", "2026-09-22T10:06:00+00:00"),  # declined → not offered
        _row("github.com:22", "2026-09-22T10:07:00+00:00", "git/2.45"),
        _row("after.example.com", "2026-09-22T11:00:01+00:00"),
    ]
    learned = allowlist.learned_hosts(rows, window)
    assert [e["host"] for e in learned] == ["registry.npmjs.org", "github.com:22"]
    npm = learned[0]
    assert npm["count"] == 3 and npm["uas"] == ["npm/10.8.2 node/v22", "pnpm/9.1.0"]
    assert (npm["first"], npm["last"]) == ("2026-09-22T10:01:00+00:00", "2026-09-22T10:03:00+00:00")


def test_a_port_key_is_only_covered_by_that_exact_grant(env):
    window = {"since": "2026-09-22T10:00:00+00:00", "until": "2026-09-22T11:00:00+00:00"}
    allowlist.grant("github.com", "permanent")  # :443 only
    rows = [_row("github.com:22", "2026-09-22T10:01:00+00:00")]
    assert [e["host"] for e in allowlist.learned_hosts(rows, window)] == ["github.com:22"]
    allowlist.grant("github.com:22", "permanent")
    assert allowlist.learned_hosts(rows, window) == []


def test_the_learn_cap_matches_the_posture_ttl_cap():
    from foldyard import devmode

    assert allowlist.LEARN_MAX_SECONDS == devmode.MAX_TTL


def test_learned_rows_are_untrusted_box_output(env):
    # Every field was written from traffic the box originated: a junk host must not reach grant()
    # (it would abort the batch), a UA must not carry terminal escapes to the operator, and the
    # printed `recommend` line must not be breakable from a UA.
    window = {"since": "2026-09-22T10:00:00+00:00", "until": "2026-09-22T11:00:00+00:00"}
    ts = "2026-09-22T10:01:00+00:00"
    rows = [
        _row("not a host", ts),
        _row("*.wild.example.com", ts),  # a glob is a grant SHAPE, never an observed host
        _row("ok.example.com", ts, 'evil/1 \x1b[2J"}, { host = "x.com'),
    ]
    (entry,) = allowlist.learned_hosts(rows, window)
    assert entry["host"] == "ok.example.com"
    assert "\x1b" not in entry["uas"][0]
    why = allowlist.recommend_why(entry)
    assert '"' not in why and "{" not in why and why == "observed: evil/1 — edit me"
    assert allowlist.recommend_why({"uas": [], "paths": []}) == (
        "observed: no detail (tunnelled) — edit me"
    )


def test_learned_hosts_say_what_was_fetched_without_what_could_be_secret(env):
    # The decrypted request rows for a learned host (same host, same window) say what the tool
    # FETCHED there — the part of a `why` a teammate can act on. Only a path's first segments are
    # kept: never the query (where tokens ride), and a segment long enough to be an id or a token
    # reads as `…`. The would-block row itself carries no path (it's the CONNECT).
    window = {"since": "2026-09-22T10:00:00+00:00", "until": "2026-09-22T11:00:00+00:00"}
    ts = "2026-09-22T10:01:00+00:00"
    token = "t" * 64

    def req(host, path, at=ts):
        return {"ts": at, "host": host, "method": "GET", "path": path, "status": 200}

    rows = [
        _row("registry.npmjs.org", ts, "npm/10.8.2 node/v22"),
        req("registry.npmjs.org", "/react"),
        req("registry.npmjs.org", "/react?token=SECRET"),  # same path once the query is gone
        req("registry.npmjs.org", "/@types/node/-/node-22.0.0.tgz?token=SECRET"),
        req("registry.npmjs.org", f"/private/{token}/pkg"),
        req("registry.npmjs.org", "/lodash#frag"),
        req("registry.npmjs.org", "/zod"),
        req("registry.npmjs.org", "/outside", at="2026-09-22T12:00:00+00:00"),  # not in window
        req("other.example.com", "/unrelated"),  # another host's requests stay its own
    ]
    (npm,) = allowlist.learned_hosts(rows, window)
    assert npm["paths"] == ["/react", "/@types/node", "/private/…"]
    assert npm["more_paths"] == 2  # /lodash, /zod
    assert not any("SECRET" in p or token in p for p in npm["paths"])
    assert allowlist.recommend_why(npm) == (
        "observed: npm/10.8.2 GET /react, /@types/node, /private/… (+2) — edit me"
    )


def test_a_port_keyed_host_borrows_no_paths_from_the_bare_host(env):
    # `github.com:22` was a raw tunnel; decrypted rows logged under bare `github.com` are :443
    # traffic — a different grant — and must not dress up the :22 entry.
    window = {"since": "2026-09-22T10:00:00+00:00", "until": "2026-09-22T11:00:00+00:00"}
    ts = "2026-09-22T10:01:00+00:00"
    rows = [
        _row("github.com:22", ts, "git/2.45"),
        {"ts": ts, "host": "github.com", "method": "GET", "path": "/org/repo", "status": 200},
    ]
    (ssh,) = allowlist.learned_hosts(rows, window)
    assert ssh["paths"] == [] and allowlist.recommend_why(ssh) == "observed: git/2.45 — edit me"


def test_a_path_cannot_break_the_printed_toml(env):
    why = allowlist.recommend_why(
        {"uas": ["npm/1"], "paths": ['/a"}, { host = "evil.com'], "more_paths": 0}
    )
    assert '"' not in why and "{" not in why and "}" not in why and "=" not in why


def test_read_log_rows_skips_damage(env, tmp_path):
    good, bad = tmp_path / "a.jsonl", tmp_path / "b.jsonl"
    good.write_text('{"host": "a.example.com"}\nnot json\n[1]\n')
    assert allowlist.read_log_rows([good, bad]) == [{"host": "a.example.com"}]


# ── writing recommend lines into foldyard.toml (the build gate's "share with the team") ──

_NEW = [{"host": "storage.googleapis.com", "uas": ["node"], "paths": [], "more_paths": 0}]


def _recommended(text: str) -> list:
    import tomllib

    return tomllib.loads(text)["proxy"]["recommend"]


def test_with_recommends_appends_inside_an_existing_multiline_list_keeping_comments():
    text = (
        "[project]\nname = 'x'\n\n[proxy]\ndefault_deny = true\n# the team's list\nrecommend = [\n"
        '  { host = "pypi.org", why = "uv" },\n'
        "  # ── this project ──\n"
        "]\n\n[machine]\nwall = true\n"
    )
    out = allowlist.with_recommends(text, _NEW)
    assert out is not None
    assert [e["host"] for e in _recommended(out)] == ["pypi.org", "storage.googleapis.com"]
    assert "# the team's list" in out and "# ── this project ──" in out  # comments survive
    assert out.startswith(text.split("]\n\n[machine]")[0])  # nothing above the end is touched


def test_with_recommends_adds_the_missing_comma():
    text = '[proxy]\nrecommend = [\n  { host = "pypi.org", why = "uv" }\n]\n'
    out = allowlist.with_recommends(text, _NEW)
    assert out is not None and len(_recommended(out)) == 2


def test_with_recommends_handles_an_inline_list():
    text = '[proxy]\nrecommend = ["pypi.org"]\n'
    out = allowlist.with_recommends(text, _NEW)
    assert out is not None
    assert _recommended(out)[0] == "pypi.org"
    assert _recommended(out)[1]["host"] == "storage.googleapis.com"


def test_with_recommends_creates_the_key_and_the_table():
    no_key = "[proxy]\ndefault_deny = true\n\n[machine]\nwall = true\n"
    out = allowlist.with_recommends(no_key, _NEW)
    assert out is not None and _recommended(out)[0]["host"] == "storage.googleapis.com"
    no_table = "[project]\nname = 'x'\n"
    out = allowlist.with_recommends(no_table, _NEW)
    assert out is not None and _recommended(out)[0]["host"] == "storage.googleapis.com"


def test_with_recommends_skips_hosts_already_recommended():
    text = '[proxy]\nrecommend = [{ host = "storage.googleapis.com", why = "mine" }]\n'
    assert allowlist.with_recommends(text, _NEW) == text  # nothing to add, nothing changed


def test_with_recommends_ignores_brackets_inside_strings_and_comments():
    text = (
        "[proxy]\nrecommend = [\n"
        '  { host = "pypi.org", why = "has ] and [ in it" },  # and ] here\n'
        "]\n"
    )
    out = allowlist.with_recommends(text, _NEW)
    assert out is not None and [e["host"] for e in _recommended(out)] == [
        "pypi.org",
        "storage.googleapis.com",
    ]


def test_with_recommends_refuses_what_it_cannot_edit_safely():
    # Anything it can't prove is a pure append comes back None; the caller prints the block.
    assert allowlist.with_recommends("[proxy\nbroken", _NEW) is None
    assert allowlist.with_recommends('[proxy]\nrecommend = "not a list"\n', _NEW) is None
