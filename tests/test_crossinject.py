"""crossinject — masking another checkout's dirs in a box that is already running, from your
computer: the engine reads, the host-made dirs, the one ssh call per box on each backend, and every
refusal falling back to the nag. The guest side is `assets/crossmask/inject.py`
(test_crossmask_inject.py); the live proof is test_worktree_e2e's running-box mask."""

from __future__ import annotations

import json
import shlex
import subprocess

import pytest

from foldyard import config, crossinject, crossmask
from foldyard.crossmask import Mask
from foldyard.machine_backend import SshTarget

CID = "c881fff5cd4433ca5a30bf65c5dd6ce1b5ab6436a1673f4a29314c1cf3706e9c"
SRC = "/home/u.guest/.local/share/containers/storage/volumes/acme-elsewhere/_data"
ENV = {"CONTAINER_HOST": "unix:///nonexistent/fy-test.sock"}


class _Backend:
    def __init__(self, name: str, target: SshTarget | None):
        self.name, self._target = name, target

    def ssh_target(self, name):
        assert name == "acme"
        return self._target


LIMA = SshTarget(user="dain", port=60022, identity="/h/.lima/_config/user")
PODMAN = SshTarget(user="core", port=50501, identity="/h/.local/share/containers/machine/m")


@pytest.fixture
def world(tmp_path, monkeypatch):
    """A main checkout + worktrees root on disk, a fake engine (box inspect, volume inspect) and a
    fake ssh whose helper answers ``state["answer"]`` — recording every call."""
    main = tmp_path / "repo"
    wt_root = tmp_path / "repo-worktrees"
    (wt_root / "b").mkdir(parents=True)
    main.mkdir()
    monkeypatch.setenv("FOLDYARD_PROJECT_PREFIX", "acme")
    monkeypatch.setattr(config, "in_box", lambda: False)
    monkeypatch.setattr(crossinject.machine, "MACHINE", "acme")
    monkeypatch.setattr(crossinject.machine, "BACKEND", _Backend("lima", LIMA))
    state: dict = {
        "box": f"4242\t{CID}\tcrun\tlocalhost/acme-box:latest\n",
        "volume": [SRC],  # successive answers to `volume inspect`; "" = no such volume
        "answer": None,  # the helper's JSON; None → every target "injected"
        "ssh_rc": 0,
        "ssh_timeout": False,
    }
    calls: list[dict] = []

    def run(cmd, **kw):
        calls.append({"cmd": cmd, **kw})
        if cmd[0] == "ssh":
            if state["ssh_timeout"]:
                raise subprocess.TimeoutExpired(cmd, kw.get("timeout") or 0)
            script = shlex.split(cmd[-1])[2]
            argv = shlex.split(script)[4:]  # past `exec /usr/bin/python3 -I -`
            answer = state["answer"] or {
                "refused": None,
                "detail": "",
                "results": dict.fromkeys(argv[4:], "injected"),
            }
            return subprocess.CompletedProcess(
                cmd, state["ssh_rc"], json.dumps(answer).encode(), b""
            )
        if cmd[1] == "inspect":
            if state["box"] is TimeoutError:
                raise subprocess.TimeoutExpired(cmd, kw.get("timeout") or 0)
            return subprocess.CompletedProcess(cmd, 0 if state["box"] else 1, state["box"], "")
        if cmd[1:3] == ["volume", "inspect"]:
            src = state["volume"].pop(0) if len(state["volume"]) > 1 else state["volume"][0]
            return subprocess.CompletedProcess(cmd, 0 if src else 1, src + "\n", "")
        raise AssertionError(f"unexpected call {cmd}")

    monkeypatch.setattr(crossinject.subprocess, "run", run)
    return {"main": main, "wt_root": wt_root, "state": state, "calls": calls}


def _masks(world, name="b"):
    wt_root = world["wt_root"]
    return [
        Mask(name, f"{wt_root}/{name}/data/.venv"),
        Mask(name, f"{wt_root}/{name}/platform/node_modules"),
    ]


def _inject(world, masks, notes=None):
    return crossinject.inject(
        "podman",
        "acme-devbox",
        ENV,
        masks,
        main=world["main"],
        wt_root=world["wt_root"],
        ensure_note=(notes.append if notes is not None else lambda image: True),
    )


def _ssh_calls(world):
    return [c for c in world["calls"] if c["cmd"][0] == "ssh"]


@pytest.mark.parametrize(
    ("backend", "target"), [("lima", LIMA), ("podman", PODMAN)], ids=["lima", "podman"]
)
def test_inject_runs_the_packaged_helper_once_over_the_backends_ssh(
    world, monkeypatch, backend, target
):
    monkeypatch.setattr(crossinject.machine, "BACKEND", _Backend(backend, target))
    masks = _masks(world)
    got = _inject(world, masks)
    assert got.done == masks and got.left == [] and got.refused == ""
    (call,) = _ssh_calls(world)
    cmd = call["cmd"]
    assert cmd[:5] == ["ssh", "-p", str(target.port), "-i", target.identity]
    assert cmd[-2] == f"{target.user}@127.0.0.1"
    assert shlex.split(cmd[-1])[:2] == ["bash", "-c"]
    assert shlex.split(shlex.split(cmd[-1])[2]) == [
        "exec",
        "/usr/bin/python3",
        "-I",  # isolated: no PYTHON* env, no user site, no script dir on the path
        "-",
        "inject",
        "4242",
        CID,
        SRC,
        *[m.path for m in masks],
    ]
    # The helper travels on stdin — nothing written into the VM, nothing run from the mount.
    assert call["input"] == crossinject.HELPER.read_bytes()
    assert call["timeout"] == crossinject.TIMEOUT_S


def test_the_engine_reads_go_to_the_stack_env(world):
    _inject(world, _masks(world))
    inspect, volume = [c for c in world["calls"] if c["cmd"][0] == "podman"]
    assert inspect["cmd"][:3] == ["podman", "inspect", "acme-devbox"]
    assert "{{.State.Pid}}" in inspect["cmd"][-1] and "{{.OCIRuntime}}" in inspect["cmd"][-1]
    assert volume["cmd"] == [
        "podman",
        "volume",
        "inspect",
        "acme-elsewhere",
        "--format",
        "{{.Mountpoint}}",
    ]
    assert inspect["env"] is ENV and volume["env"] is ENV


def test_inject_makes_the_target_dirs_on_your_computer_first(world):
    masks = _masks(world)
    _inject(world, masks)
    for m in masks:
        assert (world["wt_root"] / m.path.split("/repo-worktrees/", 1)[1]).is_dir()


def test_a_link_the_box_planted_on_the_way_is_never_followed_or_masked(world, tmp_path):
    # The box writes the worktrees root: `b/platform` as a link to somewhere of yours.
    outside = tmp_path / "yours"
    outside.mkdir()
    (world["wt_root"] / "b" / "platform").symlink_to(outside)
    masks = _masks(world)
    got = _inject(world, masks)
    assert not (outside / "node_modules").exists()
    assert got.done == [masks[0]]
    assert [(m, "symlink" in why) for m, why in got.left] == [(masks[1], True)]
    (call,) = _ssh_calls(world)
    assert masks[1].path not in call["cmd"][-1]


def test_a_mask_outside_both_bind_roots_is_left_unmade(world):
    stray = Mask("x", "/elsewhere/x/node_modules")
    got = _inject(world, [stray])
    assert got.left and got.left[0][0] == stray and not _ssh_calls(world)


def test_a_missing_note_volume_is_made_from_the_boxs_own_image_first(world):
    world["state"]["volume"] = ["", SRC]
    notes: list[str] = []
    got = _inject(world, _masks(world), notes)
    assert notes == ["localhost/acme-box:latest"]
    assert got.done


def test_a_note_volume_that_cannot_be_made_refuses(world):
    world["state"]["volume"] = [""]
    got = _inject(world, _masks(world), [])
    assert got.refused and not got.done and not _ssh_calls(world)


def test_already_masked_targets_are_neither_done_nor_left(world):
    masks = _masks(world)
    world["state"]["answer"] = {
        "refused": None,
        "detail": "",
        "results": {masks[0].path: "masked", masks[1].path: "injected"},
    }
    got = _inject(world, masks)
    assert got.done == [masks[1]] and got.left == []


@pytest.mark.parametrize(
    ("status", "says"),
    [
        ("symlink", "symlink"),
        ("missing", "VM"),
        ("moved", "renamed"),
        ("moved-kept", "renamed"),
        ("not-a-dir", "not a directory"),
        ("error: Permission denied", "Permission denied"),
    ],
)
def test_each_target_the_helper_could_not_mask_says_why(world, status, says):
    masks = _masks(world)
    world["state"]["answer"] = {
        "refused": None,
        "detail": "",
        "results": {masks[0].path: "injected", masks[1].path: status},
    }
    got = _inject(world, masks)
    assert got.done == [masks[0]]
    ((mask, why),) = got.left
    assert mask == masks[1] and says in why


def test_a_target_the_helper_never_answered_for_is_left(world):
    masks = _masks(world)
    world["state"]["answer"] = {"refused": None, "detail": "", "results": {}}
    got = _inject(world, masks)
    assert not got.done and [m for m, _ in got.left] == masks


@pytest.mark.parametrize(
    ("code", "says"),
    [
        ("rootful", "`fy verify` FAILs it"),
        ("enosys", "5.12"),
        ("stale", "restarted"),
    ],
)
def test_a_helper_refusal_leaves_every_mask_with_its_reason(world, code, says):
    world["state"]["answer"] = {"refused": code, "detail": "d", "results": {}}
    masks = _masks(world)
    got = _inject(world, masks)
    assert not got.done and [m for m, _ in got.left] == masks
    assert says in got.refused


def test_gvisor_is_refused_before_anything_reaches_the_vm(world):
    world["state"]["box"] = f"4242\t{CID}\trunsc-fy\timg\n"
    got = _inject(world, _masks(world))
    assert "gVisor" in got.refused and not _ssh_calls(world)


def test_in_a_box_nothing_is_asked_of_the_engine_or_the_vm(world, monkeypatch):
    monkeypatch.setattr(config, "in_box", lambda: True)
    got = _inject(world, _masks(world))
    assert "your computer" in got.refused and world["calls"] == []


def test_no_ssh_route_into_the_vm_refuses(world, monkeypatch):
    monkeypatch.setattr(crossinject.machine, "BACKEND", _Backend("podman", None))
    got = _inject(world, _masks(world))
    assert "ssh" in got.refused and not _ssh_calls(world)


def test_a_box_whose_pid_cannot_be_read_refuses(world):
    world["state"]["box"] = "0\t\t\t\n"  # stopped: podman reports pid 0
    got = _inject(world, _masks(world))
    assert got.refused and not _ssh_calls(world)


def test_an_engine_that_does_not_answer_refuses_rather_than_raising(world):
    # A raise here would be swallowed by the add's best-effort report — and the nag with it.
    world["state"]["box"] = TimeoutError
    got = _inject(world, _masks(world))
    assert got.refused and not got.done and not _ssh_calls(world)
    assert _check(world, [m.path for m in _masks(world)]) is None


def test_a_vm_that_does_not_answer_refuses_within_the_bound(world):
    world["state"]["ssh_timeout"] = True
    got = _inject(world, _masks(world))
    assert "answer" in got.refused and not got.done


def test_unreadable_helper_output_refuses(world):
    world["state"]["answer"] = "not an object"
    got = _inject(world, _masks(world))
    assert got.refused and not got.done


def test_nothing_to_mask_asks_nothing(world):
    got = _inject(world, [])
    assert got == crossinject.Injected([], [], "") and world["calls"] == []


# ── check ─────────────────────────────────────────────────────────────────────────────


def _check(world, paths, *, mine=False):
    return crossinject.check("podman", "acme-devbox", ENV, paths, timeout=5, mine=mine)


def test_check_asks_the_helper_without_making_anything(world):
    masks = _masks(world)
    paths = [m.path for m in masks]
    world["state"]["answer"] = {
        "refused": None,
        "detail": "",
        "results": {paths[0]: "masked", paths[1]: "unmasked"},
    }
    assert _check(world, paths) == {paths[0]}
    (call,) = _ssh_calls(world)
    assert shlex.split(shlex.split(call["cmd"][-1])[2])[4] == "check"
    assert call["timeout"] == 5
    assert not (world["wt_root"] / "b" / "data").exists()  # a check makes no dir


def test_check_cannot_say_when_the_helper_cannot_run(world):
    world["state"]["answer"] = {"refused": "rootful", "detail": "", "results": {}}
    assert _check(world, [m.path for m in _masks(world)]) is None


def test_check_in_a_box_reads_its_own_mount_table(world, monkeypatch, tmp_path):
    monkeypatch.setattr(config, "in_box", lambda: True)
    mi = tmp_path / "mountinfo"
    path = _masks(world)[0].path
    mi.write_text(f"5 1 0:3 {SRC} {path} ro,nosuid - btrfs /dev/vda2 rw\n")
    monkeypatch.setattr(crossinject, "_OWN_MOUNTINFO", mi)
    assert _check(world, [path, "/x"], mine=True) == {path}
    assert world["calls"] == []
    # another box's mounts are beyond a box: no claim either way
    assert _check(world, [path], mine=False) is None


def test_check_with_nothing_to_ask_is_empty(world):
    assert _check(world, []) == set() and world["calls"] == []


def test_the_helper_ships_in_the_package():
    assert crossinject.HELPER.is_file()
    assert crossinject.HELPER.parent.name == "crossmask"
    assert crossmask.note_volume("acme") == "acme-elsewhere"
