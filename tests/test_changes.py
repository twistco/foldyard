"""Changelog fragments (tests/tools/changes.py): one ``.changes/<slug>.md`` per change, folded
into CHANGELOG.md by ``just release`` — so two PRs never conflict over one ``## Unreleased``.

The pure parts (parse, versions, render, roll, notes) are tested directly; ``prepare`` — what
``just release`` runs before ``check``/``build``/commit/tag — against a scratch git repo."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from tools import changes

ROOT = Path(__file__).resolve().parents[1]


def _fragment(tmp_path: Path, text: str, name: str = "a-change.md") -> Path:
    p = tmp_path / name
    p.write_text(text)
    return p


GOOD = "---\nsection: Fixed\nbump: patch\n---\n\n- **A thing works.** Because.\n"


# ── parse ─────────────────────────────────────────────────────────────────────────────────────


def test_a_fragment_carries_its_section_bump_and_body(tmp_path):
    f = changes.parse(_fragment(tmp_path, GOOD))
    assert (f.section, f.bump, f.body) == ("Fixed", "patch", "- **A thing works.** Because.")


@pytest.mark.parametrize(
    ("text", "complaint"),
    [
        ("- no frontmatter\n", "frontmatter"),
        ("---\nsection: Fixed\nbump: patch\n- unterminated\n", "frontmatter"),
        ("---\nsection: Bugs\nbump: patch\n---\n\n- x\n", "section"),
        ("---\nsection: Fixed\nbump: tiny\n---\n\n- x\n", "bump"),
        ("---\nbump: patch\n---\n\n- x\n", "section"),
        ("---\nsection: Fixed\n---\n\n- x\n", "bump"),
        ("---\nsection: Fixed\nbump: patch\npr: 3\n---\n\n- x\n", "pr"),
        ("---\nsection: Fixed\nbump: patch\n---\n\n", "empty"),
        ("---\nsection: Fixed\nbump: patch\n---\n\nprose, not a bullet\n", "bullet"),
    ],
)
def test_a_malformed_fragment_is_refused_by_name(tmp_path, text, complaint):
    p = _fragment(tmp_path, text)
    with pytest.raises(changes.FragmentError, match=complaint) as e:
        changes.parse(p)
    assert p.name in str(e.value)


def test_load_skips_the_readme_and_non_markdown(tmp_path):
    _fragment(tmp_path, GOOD, "one.md")
    _fragment(tmp_path, "# how to write one\n", "README.md")
    _fragment(tmp_path, "notes\n", "scratch.txt")
    assert [f.path.name for f in changes.load(tmp_path)] == ["one.md"]


def test_the_pending_fragments_in_this_repo_are_valid():
    # The gate `just check` puts on every PR's fragment: a malformed one fails here, not at release.
    changes.load(ROOT / ".changes")


# ── versions ──────────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("current", "keyword", "expected"),
    [("0.3.2", "patch", "0.3.3"), ("0.3.2", "minor", "0.4.0"), ("0.3.2", "major", "1.0.0")],
)
def test_a_bump_keyword_names_the_next_version(current, keyword, expected):
    assert changes.next_version(current, keyword) == expected


@pytest.mark.parametrize(
    ("current", "new", "level"),
    [
        ("0.3.2", "0.3.3", "patch"),
        ("0.3.2", "0.4.0", "minor"),
        ("0.3.2", "1.0.0", "major"),
        ("0.3.2", "0.3.2", None),
        ("0.3.2", "0.3.1", None),
    ],
)
def test_the_level_of_a_version_step(current, new, level):
    assert changes.bump_level(current, new) == level


def test_the_required_bump_is_the_largest_a_fragment_asks_for(tmp_path):
    frags = [
        changes.parse(_fragment(tmp_path, GOOD, "a.md")),
        changes.parse(_fragment(tmp_path, GOOD.replace("patch", "minor"), "b.md")),
    ]
    assert changes.required_bump(frags) == "minor"


# ── render / roll / notes ─────────────────────────────────────────────────────────────────────


def _frag(section: str, body: str) -> changes.Fragment:
    return changes.Fragment(Path(f"{body}.md"), section, "patch", body)


def test_render_groups_by_section_in_canonical_order_keeping_fragment_order():
    out = changes.render(
        "0.4.0",
        "2026-09-28",
        [_frag("Fixed", "- f1"), _frag("Security", "- s1"), _frag("Fixed", "- f2")],
    )
    assert out == "## 0.4.0 — 2026-09-28\n\n### Security\n\n- s1\n\n### Fixed\n\n- f1\n- f2\n"


def test_a_summary_is_prose_above_the_sections_under_no_heading():
    out = changes.render(
        "0.4.0",
        "2026-09-28",
        [_frag("Fixed", "- f1"), _frag("Summary", "A security release: upgrade now.")],
    )
    assert out == "## 0.4.0 — 2026-09-28\n\nA security release: upgrade now.\n\n### Fixed\n\n- f1\n"


def test_a_summary_fragment_need_not_be_a_bullet(tmp_path):
    text = "---\nsection: Summary\nbump: patch\n---\n\nA security release.\n"
    assert changes.parse(_fragment(tmp_path, text)).body == "A security release."


HEAD = "# Changelog\n\nIntro.\n\n"
OLD = "## 0.3.2 — 2026-09-27\n\n### Fixed\n\n- old\n"


def test_roll_puts_the_new_section_above_the_last_release():
    new = "## 0.4.0 — 2026-09-28\n\n### Fixed\n\n- new\n"
    assert changes.roll(HEAD + OLD, new) == HEAD + new + "\n" + OLD


def test_notes_are_one_releases_body():
    log = HEAD + "## 0.4.0 — 2026-09-28\n\n### Fixed\n\n- new\n\n" + OLD
    assert changes.notes(log, "0.4.0") == "### Fixed\n\n- new\n"
    assert changes.notes(log, "0.3.2") == "### Fixed\n\n- old\n"
    with pytest.raises(changes.FragmentError, match=r"0\.9\.9"):
        changes.notes(log, "0.9.9")


# ── prepare: what `just release` runs, against a scratch repo ─────────────────────────────────


def _git(repo: Path, *args: str) -> str:
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env |= {"GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}
    r = subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", *args],
        cwd=repo,
        env=env,
        capture_output=True,
        text=True,
    )
    assert r.returncode == 0, r.stderr
    return r.stdout


LOCK = 'version = 1\n\n[[package]]\nname = "foldyard"\nversion = "0.3.2"\n'
TOML = """[project]
recommended_foldyard_version = "{rec}"

[project.foldyard_version_reasons]
"0.3.2" = "the last one"
{extra}"""


@pytest.fixture
def repo(tmp_path, monkeypatch):
    r = tmp_path / "repo"
    (r / ".changes").mkdir(parents=True)
    (r / "pyproject.toml").write_text('[project]\nname = "foldyard"\nversion = "0.3.2"\n')
    (r / "uv.lock").write_text(LOCK)
    (r / "CHANGELOG.md").write_text(HEAD + OLD)
    (r / "foldyard.toml").write_text(TOML.format(rec="0.3.2", extra=""))
    (r / ".changes" / "README.md").write_text("# how\n")
    _git(r, "init", "-q", "-b", "main")
    _git(r, "add", "-A")
    _git(r, "commit", "-qm", "0.3.2")
    _git(r, "tag", "v0.3.2")
    (r / ".changes" / "b-fix.md").write_text(GOOD.replace("A thing", "B"))
    (r / ".changes" / "a-sec.md").write_text(
        GOOD.replace("Fixed", "Security").replace("patch", "minor").replace("A thing", "A")
    )
    _git(r, "add", "-A")
    _git(r, "commit", "-qm", "two changes")
    monkeypatch.chdir(r)
    return r


def _reasons(repo: Path, version: str) -> None:
    (repo / "foldyard.toml").write_text(
        TOML.format(rec=version, extra=f'"{version}" = "why this one"\n')
    )


def test_prepare_folds_the_fragments_in_and_bumps_all_three_files(repo):
    _reasons(repo, "0.4.0")
    assert changes.prepare("minor", "2026-09-28") == "0.4.0"
    assert 'version = "0.4.0"' in (repo / "pyproject.toml").read_text()
    assert 'name = "foldyard"\nversion = "0.4.0"' in (repo / "uv.lock").read_text()
    log = (repo / "CHANGELOG.md").read_text()
    assert changes.notes(log, "0.4.0") == "### Security\n\n- **A works.** Because.\n\n" + (
        "### Fixed\n\n- **B works.** Because.\n"
    )
    assert sorted(p.name for p in (repo / ".changes").iterdir()) == ["README.md"]


def test_prepare_orders_a_sections_fragments_by_when_they_landed(repo):
    (repo / ".changes" / "a-later.md").write_text(GOOD.replace("A thing", "Later"))
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "later")
    _reasons(repo, "0.4.0")
    changes.prepare("0.4.0", "2026-09-28")
    body = changes.notes((repo / "CHANGELOG.md").read_text(), "0.4.0")
    assert body.index("B works") < body.index("Later works")  # not by name: by merge


@pytest.mark.parametrize("name", ["z early.md", 'z"early.md'], ids=["space", "git-quoted"])
def test_prepare_orders_a_fragment_whose_name_git_would_split_or_quote(repo, name):
    (repo / ".changes" / name).write_text(GOOD.replace("A thing", "Early"))
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "early")
    (repo / ".changes" / "a-later.md").write_text(GOOD.replace("A thing", "Later"))
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "later")
    _reasons(repo, "0.4.0")
    changes.prepare("0.4.0", "2026-09-28")
    body = changes.notes((repo / "CHANGELOG.md").read_text(), "0.4.0")
    assert body.index("Early works") < body.index("Later works")


def test_prepare_orders_a_re_added_fragment_by_its_latest_landing(repo):
    again = repo / ".changes" / "a-again.md"
    again.write_text(GOOD.replace("A thing", "Gone"))
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "first time")
    _git(repo, "rm", "-q", ".changes/a-again.md")
    _git(repo, "commit", "-qm", "released or reverted")
    (repo / ".changes" / "c-between.md").write_text(GOOD.replace("A thing", "Between"))
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "between")
    again.write_text(GOOD.replace("A thing", "Again"))
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "the same name, again")
    _reasons(repo, "0.4.0")
    changes.prepare("0.4.0", "2026-09-28")
    body = changes.notes((repo / "CHANGELOG.md").read_text(), "0.4.0")
    assert body.index("Between works") < body.index("Again works")


@pytest.mark.parametrize(
    ("rec", "extra"),
    [("0.3.2", ""), ("0.3.2", '"0.4.0" = "why"\n'), ("0.4.0", "")],
    ids=["neither", "reason-only", "recommended-only"],
)
def test_prepare_refuses_without_this_repos_own_reasons_entry_and_writes_nothing(repo, rec, extra):
    (repo / "foldyard.toml").write_text(TOML.format(rec=rec, extra=extra))
    before = {p: p.read_text() for p in repo.iterdir() if p.is_file()}
    with pytest.raises(changes.FragmentError, match=r"foldyard_version_reasons.*0\.4\.0"):
        changes.prepare("minor", "2026-09-28")
    assert {p: p.read_text() for p in repo.iterdir() if p.is_file()} == before
    assert len(list((repo / ".changes").glob("*.md"))) == 3


def test_prepare_refuses_an_unrelated_change_in_the_tree(repo):
    _reasons(repo, "0.4.0")  # foldyard.toml may be dirty — the release commit carries it
    (repo / "stray.py").write_text("x = 1\n")
    with pytest.raises(changes.FragmentError, match=r"stray\.py"):
        changes.prepare("minor", "2026-09-28")


def test_prepare_refuses_with_nothing_to_release(repo):
    _git(repo, "rm", "-q", ".changes/a-sec.md", ".changes/b-fix.md")
    _git(repo, "commit", "-qm", "none")
    with pytest.raises(changes.FragmentError, match="nothing to release"):
        changes.prepare("patch", "2026-09-28")


def test_prepare_warns_when_the_step_is_smaller_than_a_fragment_asks(repo, capsys):
    _reasons(repo, "0.3.3")
    assert changes.prepare("patch", "2026-09-28") == "0.3.3"
    assert "a-sec.md asks for a minor" in capsys.readouterr().err


def test_prepare_resumes_a_prepared_untagged_release_by_its_number_only(repo):
    _reasons(repo, "0.4.0")
    changes.prepare("minor", "2026-09-28")
    log = (repo / "CHANGELOG.md").read_text()
    # `minor` again would mean 0.5.0 — name the prepared one to resume it
    with pytest.raises(changes.FragmentError, match=r"just release 0\.4\.0"):
        changes.prepare("minor", "2026-09-28")
    assert changes.prepare("0.4.0", "2026-09-28") == "0.4.0"
    assert (repo / "CHANGELOG.md").read_text() == log  # nothing rolled twice


def test_prepare_refuses_a_version_that_is_already_tagged(repo):
    with pytest.raises(changes.FragmentError, match="already tagged"):
        changes.prepare("0.3.2", "2026-09-28")


def test_prepare_refuses_a_version_that_is_not_a_step_up(repo):
    _reasons(repo, "0.3.1")
    with pytest.raises(changes.FragmentError, match=r"not above 0\.3\.2"):
        changes.prepare("0.3.1", "2026-09-28")


@pytest.mark.parametrize("bad", ["01.2.3", "0.4", "v0.4.0", "0.4.0rc1"])
def test_prepare_refuses_a_version_that_is_not_x_y_z(repo, bad):
    with pytest.raises(changes.FragmentError, match=r"X\.Y\.Z"):
        changes.prepare(bad, "2026-09-28")
