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
