"""The helper that puts a quick worktree's volume on a dir of a running box, its logic over a fake
kernel (the real one: test_box_e2e's quick worktree)."""

from __future__ import annotations

import errno
import importlib.util
from pathlib import Path

import pytest

from foldyard import quick

_spec = importlib.util.spec_from_file_location("fy_quick_attach", quick.HELPER)
assert _spec and _spec.loader
attach = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(attach)


class Fake:
    """``mounts``: path → what is mounted there (``vol0`` …, or anything else). ``errors``: path →
    errno its resolve raises."""

    def __init__(self, mounts=None, errors=None, moved=False):
        self.mounts, self.errors, self.moved = dict(mounts or {}), dict(errors or {}), moved
        self.fds: dict[int, str] = {}

    def resolve(self, t):
        if t in self.errors:
            raise OSError(self.errors[t], "no")
        fd = len(self.fds) + 10
        self.fds[fd] = t
        return fd

    def is_volume(self, n, fd):
        return self.mounts.get(self.fds[fd]) == f"vol{n}"

    def mount_id(self, fd):
        return 7 if self.fds[fd] in self.mounts else 1

    def attach(self, n, fd):
        if not self.moved:
            self.mounts[self.fds[fd]] = f"vol{n}"

    def detach(self, fd):
        del self.mounts[self.fds[fd]]

    def close(self, fd):
        pass


def test_each_target_gets_its_own_volume():
    k = Fake()
    assert attach.run("attach", ["/w/a", "/w/b"], k) == {"/w/a": "attached", "/w/b": "attached"}
    assert k.mounts == {"/w/a": "vol0", "/w/b": "vol1"}


def test_again_changes_nothing():
    k = Fake({"/w/a": "vol0"})
    assert attach.run("attach", ["/w/a"], k) == {"/w/a": "already"}


def test_a_registered_worktrees_mask_is_never_covered():
    # The note volume, or a volume of the box's own: whatever is there isn't ours to mount over.
    k = Fake({"/w/a": "note"})
    assert attach.run("attach", ["/w/a"], k) == {"/w/a": "busy"}
    assert k.mounts == {"/w/a": "note"}


def test_detach_takes_off_only_its_own_volume():
    k = Fake({"/w/a": "vol0", "/w/b": "note"})
    got = attach.run("detach", ["/w/a", "/w/b", "/w/c"], k)
    assert got == {"/w/a": "detached", "/w/b": "busy", "/w/c": "absent"}
    assert k.mounts == {"/w/b": "note"}


@pytest.mark.parametrize(
    "code,status",
    [(errno.ELOOP, "symlink"), (errno.ENOENT, "missing"), (errno.ENOTDIR, "not-a-dir")],
)
def test_a_target_that_cannot_be_resolved_says_why(code, status):
    assert attach.run("attach", ["/w/a"], Fake(errors={"/w/a": code})) == {"/w/a": status}


def test_a_dir_moved_meanwhile_is_not_called_attached():
    assert attach.run("attach", ["/w/a"], Fake(moved=True))["/w/a"].startswith("error")


@pytest.mark.parametrize("t", ["w/a", "/", "/w/../a", "/w//a", "/w/a/", "/w/./a"])
def test_only_absolute_normalised_targets(t):
    assert attach.run("attach", [t], Fake()) == {t: "invalid"}


def test_usage_and_a_refusal_are_json():
    assert attach.main(["mount", "/w/a"])["refused"]
    assert attach.main([])["refused"]

    def no(count):
        raise OSError(errno.EPERM, "setns")

    assert attach.main(["attach", "/w/a"], no) == {"refused": "setns", "results": {}}
    assert Path(quick.HELPER).is_file()
