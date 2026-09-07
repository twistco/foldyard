"""`foldyard skill list|install` — bundled Claude Code skills."""

from __future__ import annotations

from foldyard import config, skills


def test_bundle_ships_bootstrap_devbox():
    assert "bootstrap-devbox" in skills.bundled()


def test_bundled_skills_have_name_and_description_frontmatter():
    for name in skills.bundled():
        text = (skills._bundled_dir() / name / "SKILL.md").read_text()
        assert text.startswith("---"), f"{name} SKILL.md needs YAML frontmatter"
        assert "\nname:" in text and "\ndescription:" in text
        assert skills._describe(name), f"{name} should expose a one-line description"


def test_install_copies_into_repo_dot_claude(fresh_config, tmp_path):
    fresh_config(FOLDYARD_REPO=tmp_path)
    assert skills.install("bootstrap-devbox") == 0
    dest = tmp_path / ".claude" / "skills" / "bootstrap-devbox" / "SKILL.md"
    assert dest.is_file()


def test_install_unknown_skill_fails(fresh_config, tmp_path, capsys):
    fresh_config(FOLDYARD_REPO=tmp_path)
    assert skills.install("does-not-exist") == 1
    assert "no bundled skill" in capsys.readouterr().out


def test_install_refuses_existing_without_force_then_overwrites(fresh_config, tmp_path):
    fresh_config(FOLDYARD_REPO=tmp_path)
    assert skills.install("bootstrap-devbox") == 0
    dest = tmp_path / ".claude" / "skills" / "bootstrap-devbox"
    (dest / "SKILL.md").write_text("# clobbered\n")
    assert skills.install("bootstrap-devbox") == 1  # exists, no force
    assert (dest / "SKILL.md").read_text() == "# clobbered\n"
    assert skills.install("bootstrap-devbox", force=True) == 0  # force re-copies
    assert (dest / "SKILL.md").read_text() != "# clobbered\n"


def test_install_targets_the_resolved_repo_root(fresh_config, tmp_path):
    fresh_config(FOLDYARD_REPO=tmp_path)
    skills.install("bootstrap-devbox")
    assert (config.repo_root() / ".claude" / "skills" / "bootstrap-devbox").is_dir()
