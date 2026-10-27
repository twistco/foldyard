"""The in-guest mask injector (`assets/crossmask/inject.py`): its argument handling, refusals and
the inject/check logic over a fake kernel. The syscalls themselves need a Linux guest — the live
proof is test_worktree_e2e's running-box mask, and the podman-backend run recorded with the
change. The module is a packaged in-guest asset (run by the guest's `python3 -I -`), so it's
loaded by path."""

from __future__ import annotations

import errno
import importlib.util
from collections.abc import Callable
from pathlib import Path

import pytest

_ASSET = Path(__file__).resolve().parents[1] / "src/foldyard/assets/crossmask/inject.py"
_spec = importlib.util.spec_from_file_location("fy_crossmask_inject", _ASSET)
assert _spec and _spec.loader
inj = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(inj)

CID = "c881fff5cd4433ca5a30bf65c5dd6ce1b5ab6436a1673f4a29314c1cf3706e9c"
SRC = "/home/u/.local/share/containers/storage/volumes/p-elsewhere/_data"
VOL = (7, 42)  # the note volume's (dev, ino)


class FakeKernel:
    """A box's mount namespace as a dict: path → (identity, read-only) or an errno, plus the
    mounts made. ``on_resolve`` lets a test act between two resolves (the rename race)."""

    def __init__(self, paths: dict[str, object], *, detach_ok: bool = True):
        self.paths = dict(paths)
        self.source_id = VOL
        self.mounts: dict[int, str] = {}  # tree fd → where it was attached
        self.at: dict[str, int] = {}  # path → the mount id seen there after an attach
        self.detached: list[int] = []
        self.detach_ok = detach_ok
        self.slept = 0.0
        self.resolves = 0
        self.on_resolve: Callable[[FakeKernel, str, int], None] | None = None
        self.open: set[int] = set()
        self._next = 100

    def _fd(self) -> int:
        self._next += 1
        self.open.add(self._next)
        return self._next

    def resolve(self, target):
        self.resolves += 1
        if self.on_resolve:
            self.on_resolve(self, target, self.resolves)
        got = self.paths.get(target, errno.ENOENT)
        if isinstance(got, int):
            raise OSError(got, "nope")
        fd = self._fd()
        self.mounts.setdefault(-fd, target)  # remember which path an fd resolved
        return fd

    def identity(self, fd):
        return self.paths[self.mounts[-fd]]

    def mount_id(self, fd):
        if fd in self.mounts:  # a tree
            return fd
        return self.at.get(self.mounts[-fd], 0)

    def clone(self):
        fd = self._fd()
        self.mounts[fd] = ""
        return fd

    def attach(self, tree, fd):
        path = self.mounts[-fd]
        self.mounts[tree] = path
        if path in self.paths:  # the mount lands on what that fd names
            self.paths[path] = (VOL, True)
            self.at[path] = tree

    def detach(self, tree):
        self.detached.append(tree)
        return self.detach_ok

    def close(self, fd):
        self.open.discard(fd)

    def sleep(self, s):
        self.slept += s


T = "/r/wt/a/node_modules"


# ── arguments ─────────────────────────────────────────────────────────────────────────


def test_parse_takes_a_mode_a_pid_a_container_id_a_source_and_targets():
    assert inj.parse(["inject", "123", CID, SRC, T, "/x"]) == ("inject", 123, CID, SRC, [T, "/x"])


@pytest.mark.parametrize(
    "argv",
    [
        [],
        ["mount", "123", CID, SRC, T],  # an unknown mode
        ["inject", "12x", CID, SRC, T],
        ["inject", "1", CID, SRC, T],  # pid 1 is the VM's init, never a box
        ["inject", "123", "not-hex!", SRC, T],
        ["inject", "123", CID, "relative/src", T],
        ["check", "123", CID, SRC],  # no target
    ],
)
def test_parse_refuses_what_it_cannot_take(argv):
    with pytest.raises(inj.Refused) as e:
        inj.parse(argv)
    assert e.value.reason == "usage"


@pytest.mark.parametrize("t", ["/a/b", "/r/wt/a/node_modules", "/x"])
def test_valid_targets_are_absolute_and_normalised(t):
    assert inj.valid_target(t)


@pytest.mark.parametrize("t", ["", "/", "a/b", "/a/../b", "/a/./b", "/a//b", "/a/b/", "/a\x00b"])
def test_invalid_targets_are_refused(t):
    assert not inj.valid_target(t)


def test_ppid_survives_a_comm_with_spaces_and_parens():
    assert inj.ppid_of("4242 (we ird) (x)) S 4240 4242 4242 0 -1") == 4240


def test_only_the_conmon_of_that_container_counts():
    argv = b"/usr/bin/conmon\x00--api-version\x001\x00-c\x00" + CID.encode() + b"\x00-u\x00x\x00"
    assert inj.conmon_of("conmon\n", argv, CID)
    assert not inj.conmon_of("conmon\n", argv, "ab" + CID[2:])  # another container's
    assert not inj.conmon_of("sh\n", argv, CID)  # not conmon at all
    assert not inj.conmon_of("conmon\n", b"conmon\x00-u\x00" + CID.encode(), CID)  # -u isn't -c


# ── check ─────────────────────────────────────────────────────────────────────────────


def test_check_reads_each_target_without_changing_anything():
    k = FakeKernel(
        {
            "/m": (VOL, True),
            "/rw": (VOL, False),
            "/plain": ((7, 9), False),
            "/link": errno.ELOOP,
            "/file": errno.ENOTDIR,
        }
    )
    got = inj.run("check", ["/m", "/rw", "/plain", "/link", "/file", "/gone", "/a/../b"], k)
    assert got == {
        "/m": "masked",
        "/rw": "writable",  # the volume, but writable: an install would land in it
        "/plain": "unmasked",
        "/link": "symlink",
        "/file": "not-a-dir",
        "/gone": "missing",
        "/a/../b": "invalid",
    }
    assert not k.at and k.slept == 0  # check never mounts, and never waits for a dir
    assert not k.open


# ── inject ────────────────────────────────────────────────────────────────────────────


def test_inject_masks_an_unmasked_dir_and_proves_it_landed():
    k = FakeKernel({T: ((7, 9), False)})
    assert inj.run("inject", [T], k) == {T: "injected"}
    assert k.paths[T] == (VOL, True) and not k.detached
    assert not k.open  # every fd closed, the tree's included


def test_inject_leaves_an_existing_mask_alone():
    k = FakeKernel({T: (VOL, True)})
    assert inj.run("inject", [T], k) == {T: "masked"}
    assert not k.at


def test_inject_covers_a_writable_mount_of_the_volume():
    k = FakeKernel({T: (VOL, False)})
    assert inj.run("inject", [T], k) == {T: "injected"}


def test_inject_refuses_a_symlink_anywhere_on_the_path():
    k = FakeKernel({T: errno.ELOOP})
    assert inj.run("inject", [T], k) == {T: "symlink"}
    assert not k.at


def test_inject_waits_a_bounded_while_for_a_dir_the_host_just_made():
    k = FakeKernel({})

    def appears(kernel, target, n):
        if n == 4:
            kernel.paths[target] = ((7, 9), False)

    k.on_resolve = appears
    assert inj.run("inject", [T], k) == {T: "injected"}
    assert k.slept == pytest.approx(3 * inj.MISSING_STEP_S)


def test_inject_gives_up_on_a_dir_that_never_appears():
    k = FakeKernel({})
    assert inj.run("inject", [T], k) == {T: "missing"}
    assert k.slept == pytest.approx(inj.MISSING_WAIT_S)


def test_the_wait_is_one_allowance_shared_by_every_target():
    # Several missing dirs must not add up past the host's ssh timeout (crossinject.TIMEOUT_S):
    # the run waits MISSING_WAIT_S in all, not per target — and still masks one that's there.
    targets = [f"/r/wt/{n}/node_modules" for n in "abcdefgh"]
    k = FakeKernel({"/r/wt/h/node_modules": ((7, 9), False)})
    got = inj.run("inject", targets, k)
    assert got == {**dict.fromkeys(targets[:-1], "missing"), targets[-1]: "injected"}
    assert k.slept == pytest.approx(inj.MISSING_WAIT_S)
    from foldyard import crossinject

    assert inj.MISSING_WAIT_S < crossinject.TIMEOUT_S / 2  # room for ssh + python start


def _rename_after_attach_resolve(kernel, target, n):
    # 1: the probe, 2: the resolve the attach uses — the box renames the dir away right after it
    # and makes a fresh one in its place, which the mount never reaches.
    if n == 3:
        kernel.paths[target + ".moved"] = kernel.paths[target]
        kernel.paths[target] = ((7, 10), False)
        kernel.at.pop(target, None)


def test_a_rename_during_the_attach_is_caught_and_the_mount_detached():
    k = FakeKernel({T: ((7, 9), False)})
    k.on_resolve = _rename_after_attach_resolve
    assert inj.run("inject", [T], k) == {T: "moved"}
    assert len(k.detached) == 1
    assert not k.open


def test_a_mount_that_cannot_be_detached_again_is_reported_as_kept():
    k = FakeKernel({T: ((7, 9), False)}, detach_ok=False)
    k.on_resolve = _rename_after_attach_resolve
    assert inj.run("inject", [T], k) == {T: "moved-kept"}


def test_another_mask_moved_onto_the_target_is_not_taken_for_this_one():
    # The target resolves to the volume read-only after the attach — but through a mount this run
    # didn't make (the box renamed a masked dir onto it). Only the mount just made counts.
    k = FakeKernel({T: ((7, 9), False)})

    def swap(kernel, target, n):
        if n == 3:
            kernel.at[target] = 999

    k.on_resolve = swap
    assert inj.run("inject", [T], k) == {T: "moved"}


def test_a_target_swapped_for_a_link_before_the_attach_is_not_mounted():
    k = FakeKernel({T: ((7, 9), False)})

    def link(kernel, target, n):
        if n == 2:
            kernel.paths[target] = errno.ELOOP

    k.on_resolve = link
    assert inj.run("inject", [T], k) == {T: "symlink"}
    assert not k.at and not k.open


# ── refusals ──────────────────────────────────────────────────────────────────────────


def test_a_refusal_from_the_kernel_layer_is_the_whole_answer():
    def rootful(pid, cid, source):
        raise inj.Refused("rootful", "the box's user namespace belongs to uid 0")

    out = inj.main(["inject", "123", CID, SRC, T], kernel=rootful)
    assert out == {
        "refused": "rootful",
        "detail": "the box's user namespace belongs to uid 0",
        "results": {},
    }


def test_a_kernel_without_the_mount_api_refuses_the_run():
    class NoMountApi(FakeKernel):
        def clone(self):
            raise inj.Refused("enosys", "open_tree: the kernel has no such call")

    out = inj.main(
        ["inject", "123", CID, SRC, T], kernel=lambda *a: NoMountApi({T: ((7, 9), False)})
    )
    assert out["refused"] == "enosys" and out["results"] == {}


def test_usage_is_refused_before_any_namespace_is_touched():
    def never(*a):
        raise AssertionError("joined a namespace for a bad command line")

    assert inj.main(["inject"], kernel=never)["refused"] == "usage"


def test_an_unexpected_os_error_is_reported_not_raised():
    def broken(*a):
        raise OSError(errno.EIO, "boom")

    assert inj.main(["check", "123", CID, SRC, T], kernel=broken)["refused"] == "error"
