"""The credential scope record (ADR-0031 decision 3): what a credential was last OBSERVED to grant,
kept host-side so `fy config widenings` / `fy mode` can show it offline, and diffed by the
supervisor so a widening on the provider's side is never silent.

Pure file I/O and data — no network, no supervisor (its reporting is in test_supervisor.py)."""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from foldyard import config, credscope
from foldyard.plugins import CredentialScope

_APP = "App 4008762, installation 139125083"


def _scope(permissions: dict, reach: str = "selected repositories", identity: str = _APP):
    return CredentialScope(identity=identity, permissions=permissions, reach=reach)


def test_the_record_lives_in_the_project_state_dir_unless_pinned(monkeypatch, tmp_path):
    monkeypatch.delenv("FOLDYARD_CREDENTIAL_SCOPES_FILE", raising=False)
    monkeypatch.setenv("FOLDYARD_STATE_DIR", str(tmp_path))
    assert config.credential_scopes_file() == tmp_path.resolve() / "credential-scopes.json"


def test_the_first_observation_is_a_baseline_not_a_drift():
    event, changes = credscope.observe("github", _scope({"issues": "write"}), "t1")
    assert (event, changes) == ("baseline", [])
    record = credscope.last("github", _APP)
    assert record == {
        "permissions": {"issues": "write"},
        "reach": "selected repositories",
        "checked": "t1",
    }


def test_an_unchanged_scope_is_quiet_but_restamps_when_it_was_checked():
    credscope.observe("github", _scope({"issues": "write"}), "t1")
    assert credscope.observe("github", _scope({"issues": "write"}), "t2") == ("same", [])
    record = credscope.last("github", _APP)
    assert record is not None and record["checked"] == "t2"


def test_a_change_names_what_was_added_removed_and_relevelled():
    credscope.observe(
        "github",
        _scope({"issues": "write", "checks": "read", "contents": "read"}, "selected repositories"),
        "t1",
    )
    event, changes = credscope.observe(
        "github",
        _scope({"issues": "write", "actions": "read", "contents": "write"}, "all repositories"),
        "t2",
    )
    assert event == "changed"
    assert changes == [
        "added actions:read",
        "removed checks:read",
        "contents read → write",
        "reach selected repositories → all repositories",
    ]
    # …and the new scope is the next comparison's baseline: one change, one report.
    assert credscope.observe(
        "github",
        _scope({"issues": "write", "actions": "read", "contents": "write"}, "all repositories"),
        "t3",
    ) == ("same", [])


def test_another_credential_on_the_same_switch_is_its_own_baseline():
    # A worktree adopting a different installation_id for the same switch name: its scope is a
    # different credential's, not a drift of the first — and alternating probes of the two must
    # not erase each other's baseline (which would hide a real change in either).
    credscope.observe("github", _scope({"issues": "write"}), "t1")
    other = _scope({"contents": "write"}, identity="App 1, installation 2")
    assert credscope.observe("github", other, "t2") == ("baseline", [])
    assert credscope.observe("github", _scope({"issues": "write"}), "t3") == ("same", [])
    assert credscope.observe("github", other, "t4") == ("same", [])


def test_an_unreadable_record_is_no_record(tmp_path, monkeypatch):
    path = tmp_path / "scopes.json"
    monkeypatch.setenv("FOLDYARD_CREDENTIAL_SCOPES_FILE", str(path))
    for junk in ("not json", "[]", json.dumps({"github": "x"}), json.dumps({"github": {_APP: 1}})):
        path.write_text(junk)
        assert credscope.last("github", _APP) is None
    # …and the next observation is a fresh baseline, which repairs the file.
    assert credscope.observe("github", _scope({"issues": "read"}), "t1")[0] == "baseline"
    assert credscope.last("github", _APP) is not None


@pytest.mark.parametrize(
    ("permissions", "expected"),
    [
        ({"issues": "write", "actions": "read", "members": "admin"}, ["issues", "members"]),
        ({"actions": "read"}, []),
    ],
)
def test_elevated_flags_write_and_admin(permissions, expected):
    assert credscope.elevated(permissions) == expected


def test_the_summary_is_sorted_and_shouts_the_elevated_levels():
    record = {
        "permissions": {"pull_requests": "write", "actions": "read", "metadata": "read"},
        "reach": "selected repositories",
        "checked": "2026-10-05T12:03:00Z",
    }
    assert credscope.summary(record) == (
        "actions:read, metadata:read, pull_requests:WRITE — selected repositories"
    )
    assert credscope.summary({"permissions": {}, "reach": ""}) == "no permissions"


def test_a_level_foldyard_does_not_know_is_flagged_as_possibly_elevated():
    # GitHub may add a level; unknown must not read as safe, so it is flagged with write/admin,
    # named as unrecognised, and shown as it reads (in capitals, as every flagged level is).
    permissions = {"actions": "read", "issues": "write", "workflows": "maintain"}
    assert credscope.elevated(permissions) == ["issues", "workflows"]
    assert credscope.unrecognised(permissions) == ["workflows"]
    assert credscope.summary({"permissions": permissions}) == (
        "actions:read, issues:WRITE, workflows:MAINTAIN"
    )


def test_a_new_level_drifts_like_any_other():
    credscope.observe("github", _scope({"workflows": "maintain"}), "t1")
    assert credscope.observe("github", _scope({"workflows": "admin"}), "t2") == (
        "changed",
        ["workflows maintain → admin"],
    )


def test_an_unreadable_probe_marks_the_record_stale_until_the_next_good_read():
    credscope.observe("github", _scope({"issues": "write"}), "t1")
    assert credscope.mark_unread("github", _APP, "t2") == {
        "permissions": {"issues": "write"},
        "reach": "selected repositories",
        "checked": "t1",
        "unread": "t2",
    }
    # The FIRST failed read is kept: "unreadable since" means since then.
    marked = credscope.mark_unread("github", _APP, "t3")
    assert marked is not None and marked["unread"] == "t2"
    record = credscope.last("github", _APP)
    assert record is not None and record["unread"] == "t2"
    # A good read replaces the record, and the mark with it.
    assert credscope.observe("github", _scope({"issues": "write"}), "t4") == ("same", [])
    record = credscope.last("github", _APP)
    assert record is not None and "unread" not in record and record["checked"] == "t4"


def test_marking_a_credential_never_read_writes_nothing():
    # Nothing is shown for it, so nothing can be stale: no record is invented.
    assert credscope.mark_unread("github", _APP, "t1") is None
    assert credscope.last("github", _APP) is None


@pytest.mark.parametrize(
    ("checked", "expected"),
    [
        ("2026-10-05T12:02:30+00:00", "just now"),
        ("2026-10-05T11:20:00+00:00", "43m ago"),
        ("2026-10-05T09:03:00+00:00", "3h ago"),
        ("2026-10-01T12:03:00+00:00", "4d ago"),
        ("2026-10-05T12:10:00+00:00", "just now"),  # a skewed clock, not a negative age
        ("t1", ""),  # unparseable: no age rather than a wrong one
    ],
)
def test_ago_is_coarse_because_it_answers_is_this_stale(checked, expected):
    now = datetime(2026, 10, 5, 12, 3, tzinfo=UTC)
    assert credscope.ago(checked, now) == expected


def test_freshness_says_when_it_was_read_and_whether_reads_have_failed_since():
    now = datetime(2026, 10, 5, 14, 3, tzinfo=UTC)
    record = {"permissions": {}, "checked": "2026-10-05T12:03:00+00:00"}
    assert credscope.freshness(record, now) == "probed 2h ago"
    record["unread"] = "2026-10-05T13:03:00+00:00"
    assert credscope.freshness(record, now) == (
        "probed 2h ago; unreadable since 1h ago, may be stale"
    )
