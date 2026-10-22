"""hostgit.py — your computer's git identity, read for the dev box. Every test points git's
global config at a tmp-path file (``GIT_CONFIG_GLOBAL``, which conftest already aims at a missing
one): never the operator's real ``~/.gitconfig``."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from foldyard import hostgit


def _global(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, text: str) -> Path:
    cfg = tmp_path / "gitconfig"
    cfg.write_text(text)
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(cfg))
    return cfg


def test_reads_name_and_email_from_the_global_config(tmp_path, monkeypatch):
    _global(tmp_path, monkeypatch, "[user]\n\tname = Ada Lovelace\n\temail = ada@example.test\n")
    assert hostgit.identity() == {"user.name": "Ada Lovelace", "user.email": "ada@example.test"}


def test_only_what_is_set(tmp_path, monkeypatch):
    _global(tmp_path, monkeypatch, "[user]\n\temail = ada@example.test\n")
    assert hostgit.identity() == {"user.email": "ada@example.test"}


def test_nothing_when_there_is_no_global_config(tmp_path, monkeypatch):
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(tmp_path / "missing"))
    assert hostgit.identity() == {}


def test_nothing_without_git(tmp_path, monkeypatch):
    _global(tmp_path, monkeypatch, "[user]\n\tname = Ada\n")
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    assert hostgit.identity() == {}


def test_follows_a_plain_include(tmp_path, monkeypatch):
    # A dotfiles setup that keeps the identity in a second file it includes.
    (tmp_path / "identity").write_text("[user]\n\tname = Ada\n")
    _global(tmp_path, monkeypatch, f"[include]\n\tpath = {tmp_path / 'identity'}\n")
    assert hostgit.identity() == {"user.name": "Ada"}


def test_never_reads_the_checkout(tmp_path, monkeypatch):
    # The repo's own .git/config is box-writable (ADR-0022): run from inside a repo that sets an
    # identity, with an empty global config, and nothing comes back.
    repo = tmp_path / "repo"
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "From The Box"], check=True)
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(tmp_path / "missing"))
    monkeypatch.chdir(repo)
    assert hostgit.identity() == {}


@pytest.mark.parametrize("value", ['"   "', '"Ada\\nLovelace"', '"Ada\\tLovelace"'])
def test_drops_a_blank_value_or_one_with_control_characters(tmp_path, monkeypatch, value):
    _global(tmp_path, monkeypatch, f"[user]\n\tname = {value}\n\temail = ada@example.test\n")
    assert hostgit.identity() == {"user.email": "ada@example.test"}


def test_keeps_quotes_and_non_ascii(tmp_path, monkeypatch):
    # The value is shell-quoted where it is used, so a name is never "cleaned".
    _global(tmp_path, monkeypatch, '[user]\n\tname = Dáire O\'Brien \\"Dee\\"\n')
    assert hostgit.identity() == {"user.name": 'Dáire O\'Brien "Dee"'}


def test_an_inherited_git_dir_never_points_the_read_at_the_checkout(tmp_path, monkeypatch):
    # `fy box up` run from a git hook (or any shell with GIT_DIR exported) must not let
    # `includeIf gitdir:` match the checkout: the read is of your global config alone.
    repo = tmp_path / "repo"
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    (tmp_path / "work").write_text("[user]\n\temail = work@example.test\n")
    _global(
        tmp_path,
        monkeypatch,
        f'[user]\n\temail = me@example.test\n[includeIf "gitdir:{repo}/"]\n\tpath = {tmp_path / "work"}\n',
    )
    monkeypatch.setenv("GIT_DIR", str(repo / ".git"))
    monkeypatch.setenv("GIT_WORK_TREE", str(repo))
    assert hostgit.identity() == {"user.email": "me@example.test"}


def test_conditional_includes_says_whether_any_includeif_rule_exists(tmp_path, monkeypatch):
    _global(tmp_path, monkeypatch, "[user]\n\tname = Ada\n")
    assert hostgit.conditional_includes() is False
    _global(tmp_path, monkeypatch, '[includeIf "gitdir:~/work/"]\n\tpath = ~/.gitconfig-work\n')
    assert hostgit.conditional_includes() is True


def test_conditional_includes_sees_a_rule_in_an_included_file(tmp_path, monkeypatch):
    (tmp_path / "more").write_text('[includeIf "gitdir:~/work/"]\n\tpath = ~/.gitconfig-work\n')
    _global(tmp_path, monkeypatch, f"[include]\n\tpath = {tmp_path / 'more'}\n")
    assert hostgit.conditional_includes() is True


def test_conditional_includes_is_false_without_git_or_a_config(tmp_path, monkeypatch):
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(tmp_path / "missing"))
    assert hostgit.conditional_includes() is False
    _global(tmp_path, monkeypatch, '[includeIf "gitdir:~/work/"]\n\tpath = x\n')
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    assert hostgit.conditional_includes() is False
