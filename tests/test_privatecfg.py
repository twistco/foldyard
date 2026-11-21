"""A private foldyard setup on a repo that doesn't use foldyard (ADR-0037): `fy init --private`,
each worktree's copy from main's adopted config, the agent's note and doctor's row."""

from __future__ import annotations

import os
import subprocess
import tomllib
from pathlib import Path

import pytest

from foldyard import config, init, privatecfg


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True
    ).stdout


@pytest.fixture
def repo(tmp_path) -> Path:
    """Someone else's project: a clone with a tracked .gitignore and one commit."""
    r = tmp_path / "upstream"
    r.mkdir()
    _git(r, "init", "-q", "-b", "main")
    _git(r, "config", "user.email", "t@example.com")
    _git(r, "config", "user.name", "t")
    (r / ".gitignore").write_text("node_modules/\n")
    (r / "README.md").write_text("upstream\n")
    _git(r, "add", "-A")
    _git(r, "commit", "-q", "-m", "init")
    return r


def _exclude(r: Path) -> str:
    return (r / ".git" / "info" / "exclude").read_text()


# ── fy init --private ────────────────────────────────────────────────────────────────────


def test_private_init_leaves_git_seeing_nothing(repo):
    assert init.init(path=str(repo), private=True) == 0
    assert _git(repo, "status", "--porcelain") == ""
    assert (repo / ".gitignore").read_text() == "node_modules/\n"  # never touched
    assert not (repo / init.LOCAL_EXAMPLE).exists()  # a team's template: no team here
    assert (repo / ".claude" / "skills" / "foldyard" / "SKILL.md").is_file()
    toml = tomllib.loads((repo / "foldyard.toml").read_text())
    assert toml["project"]["private"] is True


def test_private_init_puts_the_ignore_block_in_the_clones_own_exclude_list(repo):
    assert init.init(path=str(repo), private=True) == 0
    text = _exclude(repo)
    assert text.count(init.GITIGNORE_BEGIN) == 1
    for entry in (*init.GITIGNORE_ENTRIES, *privatecfg.EXCLUDE_ENTRIES):
        assert entry in text


def test_private_init_again_keeps_one_block_and_the_users_own_lines(repo):
    exclude = repo / ".git" / "info" / "exclude"
    exclude.write_text(exclude.read_text() + "my-notes.txt\n")
    assert init.init(path=str(repo), private=True) == 0
    assert init.init(path=str(repo), private=True) == 0
    text = _exclude(repo)
    assert text.count(init.GITIGNORE_BEGIN) == 1
    assert "my-notes.txt" in text


def test_private_init_refuses_a_checkout_that_already_tracks_foldyard(repo, capsys):
    (repo / "foldyard.toml").write_text('[project]\nname = "x"\n')
    _git(repo, "add", "foldyard.toml")
    _git(repo, "commit", "-q", "-m", "fy")
    before = _exclude(repo)
    assert init.init(path=str(repo), private=True) == 1
    out = capsys.readouterr().out
    assert "git tracks foldyard.toml" in out
    assert _exclude(repo) == before  # nothing written


def test_private_init_refuses_outside_a_main_checkout(tmp_path, capsys):
    assert init.init(path=str(tmp_path), private=True) == 1
    assert "git" in capsys.readouterr().out
    assert not (tmp_path / "foldyard.toml").exists()


def test_private_init_converts_a_setup_made_the_usual_way(repo):
    # `fy init` as usual, used for a while, nothing committed: the config is kept as it is, plus
    # `private = true`; the template goes, and .gitignore is back to what upstream has.
    assert init.init(path=str(repo), name="mine") == 0
    edited = (repo / "foldyard.toml").read_text().replace('name = "mine"', 'name = "mine2"')
    (repo / "foldyard.toml").write_text(edited)
    assert init.init(path=str(repo), private=True) == 0
    toml = tomllib.loads((repo / "foldyard.toml").read_text())
    assert toml["project"]["name"] == "mine2" and toml["project"]["private"] is True
    assert (repo / ".gitignore").read_text() == "node_modules/\n"
    assert not (repo / init.LOCAL_EXAMPLE).exists()
    assert _git(repo, "status", "--porcelain") == ""


def test_converting_drops_a_gitignore_init_created(tmp_path):
    r = tmp_path / "bare"
    r.mkdir()
    _git(r, "init", "-q")
    assert init.init(path=str(r)) == 0  # creates .gitignore: upstream has none
    assert init.init(path=str(r), private=True) == 0
    assert not (r / ".gitignore").exists()


@pytest.mark.parametrize(
    ("text", "want"),
    [
        ('[project]\nname = "a"\n\n[box]\n', '[project]\nprivate = true\nname = "a"\n\n[box]\n'),
        ('[project]\nname = "a"\nprivate = false\n', '[project]\nname = "a"\nprivate = true\n'),
        ('[project]\nprivate = true\nname = "a"\n', '[project]\nprivate = true\nname = "a"\n'),
        ("[box]\nx = 1\n", "[project]\nprivate = true\n\n[box]\nx = 1\n"),
    ],
    ids=["added", "flipped", "kept", "no-table"],
)
def test_marking_a_config_private_edits_only_that_key(text, want):
    assert privatecfg.mark_private(text) == want


def test_the_exclude_list_is_never_written_through_a_planted_link(repo, tmp_path):
    info = repo / ".git" / "info"
    for f in info.iterdir():
        f.unlink()
    info.rmdir()
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    info.symlink_to(elsewhere)  # the box can write .git/: a link where info/ was
    with pytest.raises(OSError):
        privatecfg.write_exclude(repo)
    assert list(elsewhere.iterdir()) == []


# ── the agent's note ─────────────────────────────────────────────────────────────────────


def _bound(tmp_path, text: str):
    (tmp_path / "foldyard.toml").write_text(text)
    return config.using(config.Config(repo_root=tmp_path, worktree="", toml=tomllib.loads(text)))


def test_a_private_setup_tells_each_agent_so(tmp_path):
    text = '[project]\nname = "a"\nprivate = true\n\n[claude]\nsystem_prompt = "Be kind."\n'
    with _bound(tmp_path, text):
        assert config.claude_system_prompt().startswith("Be kind.")
        assert privatecfg.AGENT_NOTE in config.claude_system_prompt()
        assert privatecfg.AGENT_NOTE in config.codex_system_prompt()


def test_an_ordinary_setup_has_no_such_note(tmp_path):
    with _bound(tmp_path, '[project]\nname = "a"\n\n[claude]\nsystem_prompt = "Be kind."\n'):
        assert config.claude_system_prompt() == "Be kind."


# ── doctor ───────────────────────────────────────────────────────────────────────────────


@pytest.fixture
def private_repo(repo, monkeypatch):
    from foldyard import stack

    assert init.init(path=str(repo), private=True) == 0
    monkeypatch.setattr(stack, "main_repo", lambda: repo)
    monkeypatch.setattr(config, "private", lambda: True)
    return repo


def _row():
    from foldyard import devmode

    return list(devmode._private_check())


def test_doctor_says_git_sees_none_of_it(private_repo):
    assert _row() == [("ok", "private setup", "git sees none of foldyard's files")]


def test_doctor_fails_a_staged_private_file(private_repo):
    _git(private_repo, "add", "-f", "foldyard.toml")  # what `git add -f` by an agent does
    [(status, _name, detail)] = _row()
    assert status == "fail" and "git tracks foldyard.toml" in detail


def test_doctor_warns_of_foldyards_block_in_gitignore(private_repo):
    (private_repo / ".gitignore").write_text("node_modules/\n" + init._gitignore_block() + "\n")
    [(status, _name, detail)] = _row()
    assert status == "warn" and ".gitignore" in detail


def test_doctor_has_no_row_for_an_ordinary_setup(monkeypatch):
    monkeypatch.setattr(config, "private", lambda: False)
    assert _row() == []


def test_converting_never_reads_a_config_the_box_linked_elsewhere(repo, tmp_path):
    # A link planted where foldyard.toml was would have your computer read the file it names and
    # write it back into the checkout, where the box can read it.
    secret = tmp_path / "id_ed25519"
    secret.write_text("PRIVATE KEY\n")
    (repo / "foldyard.toml").symlink_to(secret)
    assert init.init(path=str(repo), private=True) == 1
    assert (repo / "foldyard.toml").is_symlink()
    assert "PRIVATE KEY" not in (repo / ".git" / "info").joinpath("exclude").read_text()


@pytest.mark.parametrize("private", [True, False], ids=["private", "plain"])
def test_init_never_writes_through_a_dangling_link(repo, tmp_path, private):
    target = tmp_path / "authorized_keys"  # not there yet: the link dangles, so it isn't "existing"
    (repo / "foldyard.toml").symlink_to(target)
    init.init(path=str(repo), private=private, force=True)
    assert not target.exists()


def test_private_init_never_installs_the_skill_through_a_planted_link(repo, tmp_path):
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (repo / ".claude").symlink_to(elsewhere)
    init.init(path=str(repo), private=True)
    assert list(elsewhere.iterdir()) == []


def test_private_init_refuses_when_git_cant_say_what_it_tracks(repo, monkeypatch, capsys):
    # A git the box made fail (a corrupt index, say) must not read as "nothing tracked".
    monkeypatch.setattr(privatecfg, "_git", lambda main, *args: (128, []))
    assert init.init(path=str(repo), private=True) == 1
    assert "couldn't ask git" in capsys.readouterr().out
    assert not (repo / "foldyard.toml").exists()


def test_doctor_warns_when_git_cant_say_what_it_tracks(private_repo, monkeypatch):
    monkeypatch.setattr(privatecfg, "_git", lambda main, *args: (128, []))
    [(status, _name, detail)] = _row()
    assert status == "warn" and "couldn't ask git" in detail


def test_a_worktree_copy_never_reads_mains_tree(tmp_path, monkeypatch):
    # Only main's adopted copy is used; a FIFO planted as main's foldyard.toml would hang a read.
    from foldyard import configpin

    main = tmp_path / "main"
    main.mkdir()
    (main / "foldyard.toml").write_text('[project]\nname = "a"\nprivate = true\n')
    monkeypatch.setattr(config, "state_dir", lambda: tmp_path / "state")
    configpin.adopt(config.resolve(worktree="", repo=main))
    (main / "foldyard.toml").unlink()
    os.mkfifo(main / "foldyard.toml")
    wt_root = tmp_path / "wts"
    (wt_root / "feat").mkdir(parents=True)
    assert privatecfg.sync_worktree(main, wt_root, "feat") is True
    assert "private = true" in (wt_root / "feat" / "foldyard.toml").read_text()
