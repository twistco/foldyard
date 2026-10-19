"""changelog.py — `fy changelog`, the releases between a project's declared foldyard and the
installed one, sliced out of the changelog the wheel ships."""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest

from foldyard import changelog

ROOT = Path(__file__).resolve().parents[1]

SAMPLE = """# Changelog

Intro prose that belongs to no release.

## 0.4.0 — 2026-10-08

Every VM needs one restart.

### Security

- sec-040

### Added

- added-040

### Changed

- changed-040

## 0.3.1 — 2026-09-18

### Fixed

- fixed-031

## 0.3.0 — 2026-09-18

### Removed

- removed-030

## not-a-version

- stray
"""


@pytest.fixture
def sample(tmp_path, monkeypatch):
    path = tmp_path / "CHANGELOG.md"
    path.write_text(SAMPLE)
    monkeypatch.setattr(changelog, "changelog_path", lambda: path)
    monkeypatch.setattr("foldyard.__version__", "0.4.0", raising=False)
    return path


@pytest.fixture
def repo_declaring(fresh_config, tmp_path, monkeypatch):
    def _make(**keys):
        repo = tmp_path / "repo"
        repo.mkdir(exist_ok=True)
        decl = "".join(f'{k} = "{v}"\n' for k, v in keys.items())
        (repo / "foldyard.toml").write_text(f'[project]\nname = "p"\n{decl}')
        fresh_config(FOLDYARD_REPO=repo)
        monkeypatch.chdir(repo)
        return repo

    return _make


# ── parsing + slicing ────────────────────────────────────────────────────────────────


def test_releases_are_read_in_file_order_and_unversioned_headings_skipped():
    rels = changelog.releases(SAMPLE)
    assert [r.version for r in rels] == ["0.4.0", "0.3.1", "0.3.0"]
    assert rels[0].date == "2026-10-08"
    assert rels[0].body.startswith("Every VM needs one restart.")
    assert "fixed-031" not in rels[0].body  # a body stops at the next release


def test_between_is_half_open_and_oldest_first():
    rels = changelog.releases(SAMPLE)
    # You already have `since`; you run `until`.
    assert [r.version for r in changelog.between(rels, "0.3.0", "0.4.0")] == ["0.3.1", "0.4.0"]
    assert [r.version for r in changelog.between(rels, "0.3.1", "0.3.1")] == []
    # Equivalent spellings place the same.
    assert [r.version for r in changelog.between(rels, "0.3", "v0.3.1")] == ["0.3.1"]


def test_an_unorderable_installed_version_bounds_nothing():
    rels = changelog.releases(SAMPLE)
    assert [r.version for r in changelog.between(rels, "0.3.0", None)] == ["0.3.1", "0.4.0"]


def test_action_only_keeps_the_summary_and_the_sections_that_can_need_a_step():
    body = changelog.releases(SAMPLE)[0].body
    cut = changelog.action_only(body)
    assert "Every VM needs one restart." in cut
    assert "sec-040" in cut and "changed-040" in cut
    assert "added-040" not in cut


# ── the starting point ───────────────────────────────────────────────────────────────


def test_declared_takes_the_higher_of_the_two_bounds(repo_declaring):
    repo_declaring(min_foldyard_version="0.3.0", recommended_foldyard_version="0.3.1")
    assert changelog.declared() == ("0.3.1", "recommended_foldyard_version")


def test_declared_falls_back_to_whichever_is_set(repo_declaring):
    repo_declaring(min_foldyard_version="0.3.0")
    assert changelog.declared() == ("0.3.0", "min_foldyard_version")


def test_declared_is_none_with_no_window(repo_declaring):
    repo_declaring()
    assert changelog.declared() is None


# ── the verb ─────────────────────────────────────────────────────────────────────────


def test_defaults_to_the_declared_version_up_to_the_installed_one(sample, repo_declaring, capsys):
    repo_declaring(min_foldyard_version="0.3.0")
    assert changelog.main() == 0
    out = capsys.readouterr().out
    assert "0.3.0 → 0.4.0: 2 release(s)" in out
    assert "[project].min_foldyard_version" in out
    assert out.index("## 0.3.1") < out.index("## 0.4.0")  # oldest first
    assert "removed-030" not in out  # the declared version's own changes are already absorbed
    assert "raise min_foldyard_version to 0.4.0" in out


def test_action_filter_drops_added_and_fixed(sample, capsys):
    assert changelog.main("0.3.0", action=True) == 0
    out = capsys.readouterr().out
    assert "changed-040" in out
    assert "added-040" not in out and "fixed-031" not in out
    assert "(nothing in these sections)" in out  # 0.3.1 had only Fixed


def test_explicit_bounds_need_no_project(sample, capsys):
    assert changelog.main("0.3.0", "0.3.1") == 0
    out = capsys.readouterr().out
    assert "## 0.3.1" in out and "## 0.4.0" not in out
    assert "raise" not in out  # no project declaration to raise


def test_up_to_date_says_so(sample, repo_declaring, capsys):
    repo_declaring(min_foldyard_version="0.4.0")
    assert changelog.main() == 0
    assert "Nothing to read" in capsys.readouterr().out


def test_project_ahead_of_the_install_says_upgrade(sample, capsys):
    assert changelog.main("0.5.0") == 0
    assert "upgrade fy" in capsys.readouterr().out


def test_no_declaration_and_no_since_is_a_usage_error(sample, repo_declaring, capsys):
    repo_declaring()
    assert changelog.main() == 2
    assert "fy changelog --since" in capsys.readouterr().err


def test_an_unparseable_since_is_refused(sample, capsys):
    assert changelog.main("latest") == 2


def test_a_wheel_without_the_changelog_degrades_to_a_pointer(monkeypatch, capsys):
    monkeypatch.setattr(changelog, "changelog_path", lambda: None)
    assert changelog.main("0.1.0") == 1
    assert "releases" in capsys.readouterr().err


def test_doctor_row_points_at_the_changelog_when_the_repo_is_behind(
    sample, repo_declaring, monkeypatch
):
    from foldyard import devmode

    repo_declaring(min_foldyard_version="0.3.0")
    monkeypatch.setattr("foldyard.__version__", "0.4.0", raising=False)
    [(status, _, detail)] = list(devmode._version_window_check())
    assert status == "ok"
    assert "2 release(s) since its 0.3.0" in detail and "`fy changelog`" in detail


# ── the real file + packaging ────────────────────────────────────────────────────────


def test_the_real_changelog_parses_into_ordered_releases():
    path = changelog.changelog_path()
    assert path is not None
    rels = changelog.releases(path.read_text())
    assert len(rels) >= 2
    from foldyard.compat import _parse

    versions = [_parse(r.version) or () for r in rels]  # releases() kept only parseable ones
    assert versions == sorted(versions, reverse=True)  # newest first, as written


def test_the_wheel_ships_the_changelog_where_the_verb_looks():
    build = tomllib.loads((ROOT / "pyproject.toml").read_text())
    mapping = build["tool"]["hatch"]["build"]["targets"]["wheel"]["force-include"]
    assert mapping.get("CHANGELOG.md") == "foldyard/assets/CHANGELOG.md"
