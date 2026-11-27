"""features.py — what foldyard can do that a project's config does or doesn't use, and the keys in
it foldyard no longer reads (or never did): typos, removed keys, renamed keys, old defaults."""

from __future__ import annotations

import json
import re
import tomllib
from pathlib import Path

import pytest
from typer.testing import CliRunner

from foldyard import compat, config, experimental, exposure, features, init
from foldyard.plugins import kinds

ROOT = Path(__file__).resolve().parents[1]
DOCS = ROOT / "docs" / "configuration.md"


def _v(version: str) -> tuple[int, ...]:
    parsed = compat._parse(version)
    assert parsed is not None, version
    return parsed


# ── the register against the docs ──────────────────────────────────────────────────────────────

_SECTION = re.compile(r"^## `\[\[?([a-z_.-]+)\]?\]`")
_BULLET = re.compile(r"^( *)- \*\*`([^`]+)`\*\*(.*)")
_ALSO = re.compile(r"\*\*`([^`]+)`\*\*")
_NAME = re.compile(r"[a-z_0-9]+")


def _documented() -> set[tuple[str, str]]:
    """``(table, field)`` for every key docs/configuration.md describes in a bullet of its own
    (``""`` for a table). A sub-bullet counts under a ``[table]`` bullet; names that aren't keys
    (the to-box's directory names, a monitor value) don't match :data:`_NAME`."""
    found: set[tuple[str, str]] = set()
    table: str | None = None
    parent: str | None = None
    for line in DOCS.read_text().splitlines():
        if line.startswith("## "):
            m = _SECTION.match(line)
            table = m.group(1) if m else ("" if line.startswith("## Supervisor") else None)
            parent = None
            continue
        m = _BULLET.match(line)
        if table is None or m is None:
            continue
        indent, first, rest = m.groups()
        for name in [first, *_ALSO.findall(rest.split(" — ")[0])]:
            if name.startswith("["):
                # `[host] notifications` · `[claude.settings]` (a field of [claude]) ·
                # `[[box.tools]]` · `[claude]` (a table, and the parent of the bullets under it)
                head, _, tail = name.partition("] ")
                head = head.strip("[]")
                table_, _, field = head.partition(".")
                found.add((table_, tail or field))
                if not indent and not field:
                    parent = table_
                continue
            if not _NAME.fullmatch(name):
                continue
            if indent and parent:
                found.add((parent, name))
            elif not indent and table:
                found.add((table, name))
    return {k for k in found if not k[0].startswith("plugins")}


def test_every_documented_key_is_registered():
    known = {(k.table, k.field) for k in features.KEYS}
    renamed = {(t, old) for t, old, _ in config.RENAMED_KEYS}
    removed = {tuple(k.split(".", 1)) for k in exposure.IGNORED_KEYS}
    missing = _documented() - known - renamed - removed
    assert not missing, f"documented in docs/configuration.md but not in features.KEYS: {missing}"


@pytest.mark.parametrize("key", features.KEYS, ids=lambda k: f"{k.table}.{k.field}")
def test_every_registered_key_is_named_in_the_docs(key):
    # Not every key has a bullet ([[require]]'s are in a TOML block), so the name in a code span
    # or as an assignment anywhere in the page is what this holds.
    text = DOCS.read_text()
    name = key.field or key.table
    spellings = (f"`{name}`", f"{name} =", f"[{name}]", f".{name}]", f"] {name}`")
    assert any(s in text for s in spellings), name


@pytest.mark.parametrize("key", features.KEYS, ids=lambda k: f"{k.table}.{k.field}")
def test_every_since_is_a_release_or_the_next_one(key):
    # A key added since the last release says the release it will ship in (its fragment's bump).
    since = _v(key.since)
    changelog = (ROOT / "CHANGELOG.md").read_text()
    released = [_v(v) for v in re.findall(r"^## (\d+\.\d+\.\d+) —", changelog, re.M)]
    assert key.since == "0.0.1" or since in released or since > max(released), key.since


def test_the_inject_fields_are_exactly_what_the_kinds_take():
    registered = {k.field for k in features.KEYS if k.table == "inject" and k.field}
    taken = set(kinds.GENERIC_FIELDS) | {f for kind in kinds.KINDS.values() for f in kind.fields}
    assert registered == taken


def test_every_experimental_feature_is_a_listed_feature_with_its_value():
    listed = {(k.table, k.field, k.on) for k in features.KEYS if k.pitch}
    for f in experimental.REGISTER:
        table, field = f.key.split(".", 1)
        assert (table, field, f.value) in listed, f.key


def test_every_feature_has_a_one_line_pitch():
    for k in features.KEYS:
        assert "\n" not in k.pitch and len(k.pitch) <= 90, (k.table, k.field)


# ── the configs foldyard itself writes and ships have nothing to report ────────────────────────


def _load(path: Path) -> dict:
    return tomllib.loads(path.read_text())


@pytest.mark.parametrize("path", ["foldyard.toml", "example/foldyard.toml"])
def test_shipped_configs_have_no_problems(path):
    assert features.check(_load(ROOT / path), {}).problems == []


def test_a_scaffold_with_every_block_turned_on_has_no_problems():
    opts = init.InitOptions(name="acme", version="0.14.0", blocks=frozenset(init.BLOCKS))
    shared = tomllib.loads(init.render(opts))
    local = tomllib.loads(init.render_local(opts))
    assert features.check(shared, local).problems == []


# ── problems ───────────────────────────────────────────────────────────────────────────────────


def _problems(shared: dict, local: dict | None = None) -> list[tuple[str, str, str]]:
    return [(p.kind, p.label, p.origin) for p in features.check(shared, local or {}).problems]


def test_a_misspelt_key_is_unknown_with_a_suggestion():
    report = features.check({"machine": {"firwall": True}}, {})
    [p] = report.problems
    assert (p.kind, p.label, p.origin) == ("unknown", "[machine] firwall", features.SHARED)
    assert "`firewall`" in p.detail


def test_a_misspelt_table_is_unknown():
    [p] = features.check({"machien": {"firewall": True}}, {}).problems
    assert (p.kind, p.label) == ("unknown", "[machien]")
    assert "`[machine]`" in p.detail


def test_an_unknown_field_in_an_array_of_tables_names_its_entry():
    shared = {"inject": [{"switch": "gh", "kind": "github-app", "app_idd": 1}]}
    [p] = features.check(shared, {}).problems
    assert (p.kind, p.label) == ("unknown", "[[inject]] app_idd")
    assert "`app_id`" in p.detail and "gh" in p.detail


def test_the_local_file_is_named_as_the_origin():
    assert _problems({}, {"box": {"pakages": []}}) == [("unknown", "[box] pakages", features.LOCAL)]


def test_disabled_is_a_key_in_every_table_and_entry():
    shared = {"codex": {"disabled": True}, "inject": [{"switch": "x", "disabled": True}]}
    assert _problems(shared, {"claude": {"disabled": True}}) == []


def test_free_form_tables_and_values_are_not_inspected():
    shared = {
        "ports": {"WEB_PORT": 3000},
        "plugins": {"someone-elses": {"anything": 1}},
        "resnapshot_on_capability": {"gcp": ["api"]},
        "box": {"env": {"WHATEVER": "1"}, "image": {"dockerfile": "x", "nonsense": 1}},
        "claude": {"settings": {"model": "x"}},
    }
    assert _problems(shared) == []


def test_a_removed_key_says_what_replaced_it():
    [p] = features.check({"proxy": {"allow": ["github.com"]}}, {}).problems
    assert (p.kind, p.label) == ("removed", "[proxy] allow")
    assert p.detail == exposure.IGNORED_KEYS["proxy.allow"]


def test_a_removed_inject_field_and_plugin_table_are_removed_not_unknown():
    shared = {
        "inject": [{"switch": "x", "minter": "./mint.sh"}],
        "plugins": {"github": {"app_id": 1}},
    }
    assert sorted(_problems(shared)) == [
        ("removed", "[[inject]] minter", features.SHARED),
        ("removed", "[plugins.github]", features.SHARED),
    ]


def test_a_renamed_key_names_its_new_spelling():
    [p] = features.check({"machine": {"wall": True}}, {}).problems
    assert (p.kind, p.label) == ("renamed", "[machine] wall")
    assert "[machine] firewall" in p.detail


def test_an_old_default_set_explicitly_is_reported_with_the_release_that_changed_it():
    [p] = features.check({"proxy": {"passthrough": ["@all"]}}, {}).problems
    assert (p.kind, p.label) == ("old-default", '[proxy] passthrough = ["@all"]')
    assert "0.4.0" in p.detail and '"@jvm"' in p.detail


def test_an_old_default_no_longer_honoured_says_so_not_keep_it_if_you_mean_it():
    # `[machine] compose = "host"` was the default before 0.16.0, and does nothing since: there is
    # nothing left to mean by keeping it. Reported, never refused (adoption over strictness).
    report = features.check({"machine": {"compose": "host"}}, {})
    [p] = report.problems
    assert (p.kind, p.label) == ("old-default", '[machine] compose = "host"')
    assert "0.16.0" in p.detail and "no longer honoured" in p.detail
    assert "if you mean it" not in p.detail
    assert not report.failed


@pytest.mark.parametrize("value", ["HOST", " host "])
def test_an_old_default_is_known_however_it_was_spelt(value):
    # The old reader took any case and spacing, so a config spelling it so meant the same thing.
    [p] = features.check({"machine": {"compose": value}}, {}).problems
    assert p.kind == "old-default" and "no longer honoured" in p.detail


def test_a_compose_vm_line_says_what_happens_anyway_so_nothing_is_reported():
    assert _problems({"machine": {"compose": "vm"}}) == []


def test_todays_default_or_another_value_is_not_an_old_default():
    assert _problems({"proxy": {"passthrough": ["@jvm", "@linux"]}}) == []
    assert _problems({"proxy": {"passthrough": ["@all", "@vcs"]}}) == []


def test_a_non_table_where_a_table_belongs_is_left_to_the_loader():
    # `machine = 1` is a config error foldyard's readers already treat as "absent" — not a typo.
    assert _problems({"machine": 1}) == []


# ── features ───────────────────────────────────────────────────────────────────────────────────


def _row(report: features.Report, table: str, field: str = "", on: object = None):
    [row] = [
        u for u in report.features if (u.key.table, u.key.field, u.key.on) == (table, field, on)
    ]
    return row


def test_a_set_key_is_in_use_and_names_its_file():
    report = features.check({"box": {"packages": ["jq"]}}, {"box": {"git_hooks": ".githooks"}})
    assert _row(report, "box", "packages").used
    assert _row(report, "box", "packages").origin == features.SHARED
    assert _row(report, "box", "git_hooks").origin == features.LOCAL
    assert not _row(report, "box", "warmup").used


def test_a_table_feature_is_in_use_when_the_table_is_declared():
    assert _row(features.check({"proxy": {}}, {}), "proxy").used


def test_a_valued_feature_is_in_use_only_at_its_value():
    report = features.check({"machine": {"runtime": "crun"}}, {})
    row = _row(report, "machine", "runtime", "gvisor")
    assert not row.used and row.value == "crun"
    assert _row(
        features.check({"machine": {"runtime": "gvisor"}}, {}), "machine", "runtime", "gvisor"
    ).used


def test_a_valued_feature_in_an_array_is_in_use_when_any_entry_has_it():
    shared = {"inject": [{"switch": "a"}, {"switch": "b", "kind": "github-app"}]}
    assert _row(features.check(shared, {}), "inject", "kind", "github-app").used


def test_a_disabled_table_is_not_in_use():
    report = features.check({"codex": {"keyless": True}}, {"codex": {"disabled": True}})
    assert not _row(report, "codex").used


def test_an_array_whose_every_entry_is_disabled_is_not_in_use():
    shared = {"inject": [{"switch": "a", "disabled": True}]}
    assert not _row(features.check(shared, {}), "inject").used


def test_a_renamed_key_set_locally_is_credited_to_the_local_file():
    report = features.check({"machine": {"firewall": False}}, {"machine": {"wall": True}})
    row = _row(report, "machine", "firewall")
    assert row.used and row.value is True and row.origin == features.LOCAL


def test_the_reviewed_version_is_the_recommended_one():
    shared = {"project": {"recommended_foldyard_version": "0.12.0"}}
    assert features.check(shared, {}).reviewed == "0.12.0"
    assert features.check({}, {}).reviewed is None


# ── rendering ──────────────────────────────────────────────────────────────────────────────────


def _versions(lines: list[str]) -> list[str]:
    return [line.strip() for line in lines if re.fullmatch(r"  \d+\.\d+\.\d+", line)]


def test_features_are_grouped_by_release_newest_first():
    lines = features.render(features.check({}, {}))
    versions = _versions(lines)
    assert versions == sorted(set(versions), key=_v, reverse=True)
    assert versions[0] == max((k.since for k in features.KEYS if k.pitch), key=_v)


def test_the_reviewed_line_falls_between_newer_and_reviewed_releases():
    shared = {"project": {"recommended_foldyard_version": "0.6.0"}}
    lines = features.render(features.check(shared, {}))
    [mark] = [i for i, line in enumerate(lines) if "reviewed up to 0.6.0" in line]
    above, below = _versions(lines[:mark]), _versions(lines[mark:])
    assert above and all(_v(v) > _v("0.6.0") for v in above)
    assert below and all(_v(v) <= _v("0.6.0") for v in below)


def test_reviewed_releases_are_dimmed_only_with_colour():
    shared = {"project": {"recommended_foldyard_version": "0.6.0"}}
    plain = features.render(features.check(shared, {}))
    coloured = features.render(features.check(shared, {}), color=True)
    assert not any("\x1b" in line for line in plain)
    mark = next(i for i, line in enumerate(coloured) if "reviewed up to" in line)
    assert all(line.startswith("\x1b[2m") for line in coloured[mark + 1 :] if line.strip())
    assert not any("\x1b[2m" in line for line in coloured[:mark])


def test_no_recommended_version_means_no_reviewed_line():
    assert not any("reviewed up to" in line for line in features.render(features.check({}, {})))


def test_in_use_and_problems_are_rendered():
    shared = {"machine": {"monitor": "observe", "firwall": True}}
    text = "\n".join(features.render(features.check(shared, {})))
    assert re.search(r"● \[machine\] monitor = \"observe\" .*experimental", text)
    assert "✗ [machine] firwall" in text and "foldyard.toml" in text


def test_the_json_carries_every_feature_and_problem():
    doc = features.as_dict(features.check({"machine": {"firwall": True}}, {}))
    assert {"features", "problems", "reviewed"} <= set(doc)
    assert any(f["key"] == "[machine] monitor" for f in doc["features"])
    assert doc["problems"][0]["label"] == "[machine] firwall"
    json.dumps(doc)  # serialisable


def test_only_unknown_and_removed_keys_fail():
    assert features.check({"machine": {"firwall": True}}, {}).failed
    assert features.check({"proxy": {"allow": []}}, {}).failed
    assert not features.check({"machine": {"wall": True}}, {}).failed
    assert not features.check({"proxy": {"passthrough": ["@all"]}}, {}).failed


# ── what the config chose is shown, never obeyed by the terminal ───────────────────────────────

ESC = "\x1b]52;c;cHduZWQ=\x07\x1b[2K"  # write the clipboard, then erase the line


def test_a_key_or_value_carrying_terminal_escapes_is_shown_as_text():
    # The checkout is box-writable, so its key names and values are the box's choice.
    shared = {
        "machine": {ESC: True, "runtime": f"crun{ESC}"},
        "inject": [{"switch": f"gh{ESC}", "app_idd": 1}],
        ESC: {},
    }
    report = features.check(shared, {})
    text = "\n".join(features.render(report, color=True))
    assert "\x07" not in text and "\x1b]" not in text and "\x1b[2K" not in text
    assert "\\x1b]52;c;" in text  # kept visible: it's part of what the config says
    assert all(c.isprintable() for p in report.problems for c in p.label + p.detail)


def test_a_recommended_version_carrying_terminal_escapes_is_shown_as_text():
    # "99<ESC>…" still orders as 99, so the "reviewed up to" line prints it.
    report = features.check({"project": {"recommended_foldyard_version": f"99{ESC}"}}, {})
    assert report.reviewed is not None and report.reviewed.isprintable()
    text = "\n".join(features.render(report))
    assert "reviewed up to 99\\x1b]52" in text and "\x1b]" not in text


# ── the verb ───────────────────────────────────────────────────────────────────────────────────


@pytest.fixture
def files(monkeypatch):
    """Stand in for the adopted (or tree) files the verb reads."""
    docs: dict[str, dict] = {"shared": {}, "local": {}}
    monkeypatch.setattr(features, "_files", lambda cfg: (docs["shared"], docs["local"]))
    monkeypatch.setattr("foldyard.cli._active_config", lambda: object())
    return docs


def test_fy_config_features_exits_non_zero_on_an_unknown_key(files):
    from foldyard.cli import app

    files["shared"] = {"machine": {"firwall": True}}
    result = CliRunner().invoke(app, ["config", "features"])
    assert result.exit_code == 1, result.output
    assert "[machine] firwall" in result.output


def test_fy_config_features_json(files):
    from foldyard.cli import app

    files["shared"] = {"box": {"packages": ["jq"]}}
    result = CliRunner().invoke(app, ["config", "features", "--json"])
    assert result.exit_code == 0, result.output
    doc = json.loads(result.output)
    [row] = [f for f in doc["features"] if f["key"] == "[box] packages"]
    assert row["used"] is True
