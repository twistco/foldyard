"""boxpackages.py — adding a package to `[box].packages` as a TEXTUAL edit, proven by re-parsing:
comments and layout survive, the right file is chosen, and anything it can't change exactly (or a
file it must not follow) is refused with every file left as it was."""

from __future__ import annotations

import os
import tomllib

import pytest

from foldyard import boxpackages


def _add(tmp_path, shared: str | None, local: str | None = None, pkg: str = "tmux"):
    if shared is not None:
        (tmp_path / "foldyard.toml").write_text(shared)
    if local is not None:
        (tmp_path / "foldyard.local.toml").write_text(local)
    return boxpackages.add(tmp_path, pkg)


@pytest.mark.parametrize(
    ("before", "after"),
    [
        # This repo's own shape: a one-line array, a comment block above it.
        (
            '[box]\n# why each\npackages = ["nodejs", "just"]  # trailing\n',
            '[box]\n# why each\npackages = ["nodejs", "just", "tmux"]  # trailing\n',
        ),
        ("[box]\npackages = []\n", '[box]\npackages = ["tmux"]\n'),
        ('[box]\npackages = ["a",]\n', '[box]\npackages = ["a", "tmux"]\n'),
        (
            '[box]\npackages = [\n  "a",  # one\n  "b"  # two\n]\n',
            '[box]\npackages = [\n  "a",  # one\n  "b", "tmux"  # two\n]\n',
        ),
        (
            '[box]\npackages = [\n  "a",\n]\n',
            '[box]\npackages = [\n  "a", "tmux"\n]\n',
        ),
        # A `]` inside a comment or a string is not the array's end.
        (
            '[box]\npackages = ["a]"  # x]\n]\n',
            '[box]\npackages = ["a]", "tmux"  # x]\n]\n',
        ),
        # No packages key: a line straight under the header, the rest of the table kept.
        (
            '[box]  # the box\nshadow_volumes = [".venv"]\n\n[vscode]\n',
            '[box]  # the box\npackages = ["tmux"]\nshadow_volumes = [".venv"]\n\n[vscode]\n',
        ),
        # A packages key in ANOTHER table is not [box]'s.
        (
            '[box]\nx = 1\n[other]\npackages = ["z"]\n',
            '[box]\npackages = ["tmux"]\nx = 1\n[other]\npackages = ["z"]\n',
        ),
        # No [box] table at all, or only its sub-tables: one is appended.
        ("[claude]\n", '[claude]\n\n[box]\npackages = ["tmux"]\n'),
        (
            '[[box.tools]]\nname = "x"\n',
            '[[box.tools]]\nname = "x"\n\n[box]\npackages = ["tmux"]\n',
        ),
    ],
)
def test_the_edit_keeps_the_file_and_adds_the_package(tmp_path, before, after):
    written = _add(tmp_path, before)
    assert written == tmp_path / "foldyard.toml"
    assert written.read_text() == after
    assert tomllib.loads(after)["box"]["packages"][-1] == "tmux"
    assert boxpackages.listed(tmp_path)[-1] == "tmux"


def test_no_config_file_at_all_gets_one(tmp_path):
    assert _add(tmp_path, None).read_text() == '[box]\npackages = ["tmux"]\n'


def test_a_local_file_that_sets_the_list_is_the_one_edited(tmp_path):
    # An array in the local file replaces the shared one, so editing foldyard.toml changes nothing.
    shared = '[box]\npackages = ["a"]\n'
    written = _add(tmp_path, shared, '[box]\npackages = ["b"]\n')
    assert written.name == "foldyard.local.toml"
    assert written.read_text() == '[box]\npackages = ["b", "tmux"]\n'
    assert (tmp_path / "foldyard.toml").read_text() == shared
    assert boxpackages.listed(tmp_path) == ["b", "tmux"]


def test_a_local_file_without_the_list_leaves_it_to_the_shared_one(tmp_path):
    written = _add(tmp_path, '[box]\npackages = ["a"]\n', '[claude]\nkeyless = "oauth"\n')
    assert written.name == "foldyard.toml"
    assert (tmp_path / "foldyard.local.toml").read_text() == '[claude]\nkeyless = "oauth"\n'


@pytest.mark.parametrize(
    "text",
    [
        '[box]\npackages = ["tmux"]\n',  # already there
        'box.packages = ["a"]\n',  # a dotted key: no header to edit under
        'x = """\n[box]\n"""\n',  # the only "[box]" is inside a string
        "[box]\npackages = 3\n",  # not a list
        "[box\n",  # doesn't parse
    ],
)
def test_what_it_cant_change_exactly_is_refused_untouched(tmp_path, text):
    with pytest.raises(boxpackages.Refused):
        _add(tmp_path, text)
    assert (tmp_path / "foldyard.toml").read_text() == text


def test_a_planted_symlink_is_never_read_or_written_through(tmp_path):
    # The box can put a link where foldyard.toml was; following it would copy a file of yours
    # into the checkout (read) or write onto it (write).
    yours = tmp_path / "yours"
    yours.write_text('token = "secret"\n')  # parses: only the no-follow refuses it
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "foldyard.toml").symlink_to(yours)
    with pytest.raises(boxpackages.Refused):
        boxpackages.add(repo, "tmux")
    assert yours.read_text() == 'token = "secret"\n'
    assert (repo / "foldyard.toml").is_symlink()
    assert boxpackages.listed(repo) == []


def test_a_fifo_in_its_place_is_refused_without_hanging(tmp_path):
    os.mkfifo(tmp_path / "foldyard.toml")
    with pytest.raises(boxpackages.Refused):
        boxpackages.add(tmp_path, "tmux")


def test_a_name_that_isnt_a_package_is_refused(tmp_path):
    with pytest.raises(boxpackages.Refused):
        _add(tmp_path, "[box]\n", pkg='x"]\n[evil]')
