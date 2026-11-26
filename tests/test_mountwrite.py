"""mountwrite — host writes into the repo mount never follow a symlink the box planted.

The box can replace any path in the checkout with a symlink to a file the operator owns
(``~/.ssh/authorized_keys``, the project's ``host.env``). Every host-side write there — the
supervisor's posture mirror each tick, the git heal's sync marker, the staged proxy CA, the build
gate's edit of ``foldyard.toml`` — must land in the checkout or not at all."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from foldyard import box, config, devmode, githeal, mountwrite
from foldyard.plugins import proxy


@pytest.fixture
def victim(tmp_path):
    v = tmp_path / "outside" / "authorized_keys"
    v.parent.mkdir()
    v.write_text("ssh-ed25519 AAAA operator\n")
    return v


@pytest.fixture
def checkout(tmp_path):
    c = tmp_path / "repo"
    c.mkdir()
    return c


def test_writes_creating_the_dirs_it_needs(checkout):
    mountwrite.write(checkout, "a/b/f.txt", b"hi")
    assert (checkout / "a" / "b" / "f.txt").read_bytes() == b"hi"
    mountwrite.write(checkout, "a/b/f.txt", b"again")  # and overwrites in place
    assert (checkout / "a" / "b" / "f.txt").read_bytes() == b"again"


def test_a_symlinked_file_is_replaced_not_written_through(checkout, victim):
    (checkout / "f.txt").symlink_to(victim)
    mountwrite.write(checkout, "f.txt", b"posture")
    assert victim.read_text() == "ssh-ed25519 AAAA operator\n"
    assert not (checkout / "f.txt").is_symlink()
    assert (checkout / "f.txt").read_bytes() == b"posture"


def test_a_symlinked_dir_is_refused(checkout, victim):
    (checkout / "a").symlink_to(victim.parent)
    with pytest.raises(OSError):
        mountwrite.write(checkout, "a/authorized_keys", b"posture")
    assert victim.read_text() == "ssh-ed25519 AAAA operator\n"
    assert sorted(p.name for p in victim.parent.iterdir()) == ["authorized_keys"]


@pytest.mark.parametrize("rel", ["/etc/x", "../x", "a/../../x", ""])
def test_a_path_leaving_the_root_is_refused(checkout, rel):
    with pytest.raises(ValueError):
        mountwrite.write(checkout, rel, b"x")


# ── the callers ───────────────────────────────────────────────────────────────────────


def test_the_supervisors_mirror_never_writes_through_a_planted_symlink(
    isolated_state, victim, monkeypatch
):
    mirror = isolated_state["mirror"]
    monkeypatch.setattr(config, "mirror_file", lambda: mirror)
    mirror.symlink_to(victim)
    devmode.write_mirror({}, {}, {}, {})
    assert victim.read_text() == "ssh-ed25519 AAAA operator\n"
    assert json.loads(mirror.read_text())  # the mirror itself was written


def test_the_heal_marker_never_writes_through_a_planted_symlink(tmp_path, victim):
    main = tmp_path / "repo"
    gitdir = main / ".git"
    gitdir.mkdir(parents=True)
    (gitdir / githeal.SYNC_FILE).symlink_to(victim)
    githeal._record(main, ".git", "abc123")
    assert victim.read_text() == "ssh-ed25519 AAAA operator\n"
    assert (gitdir / githeal.SYNC_FILE).read_text() == "abc123\n"


def test_the_ca_is_never_staged_through_a_planted_symlink(checkout, victim, tmp_path, monkeypatch):
    ca = tmp_path / "mitmproxy-ca-cert.pem"
    ca.write_text("-----BEGIN CERTIFICATE-----\n")
    monkeypatch.setattr(proxy, "_mitm_ca", lambda: ca)
    (checkout / ".devbox-ca").symlink_to(victim.parent)
    with pytest.raises(SystemExit, match="symlink"):
        proxy.stage_ca_for_box(str(checkout), ".")
    assert sorted(p.name for p in victim.parent.iterdir()) == ["authorized_keys"]


def _fake_uv_build(seen: list[Path]):
    """`uv build --out-dir X`, mocked: records X and drops a wheel there."""

    def run(cmd, **kw):
        out = Path(cmd[cmd.index("--out-dir") + 1])
        seen.append(out)
        (out / "foldyard-1.0-py3-none-any.whl").write_bytes(b"WHEEL")
        return subprocess.CompletedProcess(cmd, 0, "", "")

    return run


def test_the_wheel_is_built_outside_the_mount_and_copied_in(checkout, tmp_path, monkeypatch):
    seen: list[Path] = []
    monkeypatch.setattr(box.subprocess, "run", _fake_uv_build(seen))
    dest = checkout / "dev" / ".devbox-foldyard"
    dest.mkdir(parents=True)
    (dest / "foldyard-0.9-py3-none-any.whl").write_bytes(b"STALE")
    wheel = box.stage_foldyard_for_box(str(checkout), "dev", tmp_path)
    assert wheel is not None and wheel == dest / "foldyard-1.0-py3-none-any.whl"
    assert wheel.read_bytes() == b"WHEEL"
    assert not seen[0].is_relative_to(checkout)  # uv never writes into the mount itself
    assert sorted(p.name for p in dest.iterdir()) == [".gitignore", wheel.name]  # stale one gone
    assert (dest / ".gitignore").read_text() == "*\n"


@pytest.mark.parametrize("planted", ["dev", "dev/.devbox-foldyard"])
def test_the_wheel_is_never_staged_through_a_planted_symlink(
    checkout, victim, tmp_path, monkeypatch, planted
):
    # A box that links the staging dir (or the dir above it) at a host dir would have had the
    # host delete every *.whl there, then build a wheel into it.
    (victim.parent / "precious.whl").write_bytes(b"OPERATOR'S")
    monkeypatch.setattr(box.subprocess, "run", _fake_uv_build([]))
    link = checkout / planted
    link.parent.mkdir(parents=True, exist_ok=True)
    link.symlink_to(victim.parent)
    with pytest.raises(SystemExit, match="symlink"):
        box.stage_foldyard_for_box(str(checkout), "dev", tmp_path)
    assert sorted(p.name for p in victim.parent.iterdir()) == ["authorized_keys", "precious.whl"]
    assert (victim.parent / "precious.whl").read_bytes() == b"OPERATOR'S"


@pytest.mark.parametrize("value", ["/Users/someone", "../elsewhere", "infra/../../x", "~/x"])
def test_dev_vm_dir_must_stay_inside_the_checkout(fresh_config, checkout, value):
    (checkout / "foldyard.toml").write_text(f'[project]\nname = "p"\ndev_vm_dir = "{value}"\n')
    fresh_config(FOLDYARD_REPO=checkout, FOLDYARD_DEV_VM_DIR=None)
    with pytest.raises(SystemExit, match="dev_vm_dir"):
        config.dev_vm_rel()


def test_dev_vm_dir_inside_the_checkout_is_fine(fresh_config, checkout):
    (checkout / "foldyard.toml").write_text('[project]\nname = "p"\ndev_vm_dir = "infra/dev"\n')
    fresh_config(FOLDYARD_REPO=checkout, FOLDYARD_DEV_VM_DIR=None)
    assert config.dev_vm_rel() == "infra/dev"


# ── makedirs: the directory-only twin (ADR-0033's from-box slots) ─────────────────────────


def test_makedirs_creates_every_missing_component(checkout):
    mountwrite.makedirs(checkout, "worktrees/feat/claude/projects")
    assert (checkout / "worktrees/feat/claude/projects").is_dir()
    mountwrite.makedirs(checkout, "worktrees/feat/claude/projects")  # idempotent


def test_makedirs_creates_private_dirs(checkout):
    mountwrite.makedirs(checkout, "main/claude")
    assert (checkout / "main").stat().st_mode & 0o077 == 0


@pytest.mark.parametrize("planted", ["worktrees", "worktrees/feat", "worktrees/feat/claude"])
def test_makedirs_refuses_a_planted_symlink_anywhere_on_the_way(checkout, victim, planted):
    # A box can plant `from-box/worktrees/<wt>` as a link to a host dir before the worktree's
    # first `fy box up`: `Path.mkdir(parents=True)` would create `claude/projects` inside it.
    (checkout / planted).parent.mkdir(parents=True, exist_ok=True)
    (checkout / planted).symlink_to(victim.parent)
    with pytest.raises(OSError):
        mountwrite.makedirs(checkout, "worktrees/feat/claude/projects")
    assert sorted(p.name for p in victim.parent.iterdir()) == ["authorized_keys"]


def test_makedirs_refuses_a_root_that_is_a_symlink(tmp_path, victim):
    (tmp_path / "root").symlink_to(victim.parent)
    with pytest.raises(OSError):
        mountwrite.makedirs(tmp_path / "root", "a")
    assert sorted(p.name for p in victim.parent.iterdir()) == ["authorized_keys"]


@pytest.mark.parametrize("rel", ["/etc/x", "../x", "a/../../x", ""])
def test_makedirs_refuses_a_path_leaving_the_root(checkout, rel):
    with pytest.raises(ValueError):
        mountwrite.makedirs(checkout, rel)


# ── listdir / unlink: clearing out what the host staged before ────────────────────────────


def test_listdir_and_unlink_work_below_the_root(checkout):
    mountwrite.write(checkout, "a/b/old.whl", b"x")
    mountwrite.write(checkout, "a/b/keep.txt", b"x")
    assert sorted(mountwrite.listdir(checkout, "a/b")) == ["keep.txt", "old.whl"]
    mountwrite.unlink(checkout, "a/b/old.whl")
    assert sorted(mountwrite.listdir(checkout, "a/b")) == ["keep.txt"]


@pytest.mark.parametrize("planted", ["a", "a/b"])
def test_listdir_and_unlink_refuse_a_planted_dir(checkout, victim, planted):
    (checkout / planted).parent.mkdir(parents=True, exist_ok=True)
    (checkout / planted).symlink_to(victim.parent)
    with pytest.raises(OSError):
        mountwrite.listdir(checkout, "a/b")
    with pytest.raises(OSError):
        mountwrite.unlink(checkout, "a/b/authorized_keys")
    assert victim.read_text() == "ssh-ed25519 AAAA operator\n"


def test_unlink_removes_a_planted_link_not_its_target(checkout, victim):
    (checkout / "old.whl").symlink_to(victim)
    mountwrite.unlink(checkout, "old.whl")
    assert not (checkout / "old.whl").exists() and victim.exists()


@pytest.mark.parametrize("rel", ["/etc/x", "../x", "a/../../x", ""])
def test_listdir_and_unlink_refuse_a_path_leaving_the_root(checkout, rel):
    with pytest.raises(ValueError):
        mountwrite.unlink(checkout, rel)
    if rel:  # "" is the root itself, which listdir may read
        with pytest.raises(ValueError):
            mountwrite.listdir(checkout, rel)


# ── reading a file the box can write: regular, bounded, never blocking ──────────────────


def test_read_regular_reads_a_regular_file_and_a_link_to_one(checkout, tmp_path):
    (checkout / "foldyard.toml").write_bytes(b"[project]\n")
    assert mountwrite.read_regular(checkout / "foldyard.toml", limit=100) == b"[project]\n"
    shared = tmp_path / "shared.toml"
    shared.write_bytes(b"x = 1\n")
    (checkout / "linked.toml").symlink_to(shared)  # a monorepo's shared config keeps working
    assert mountwrite.read_regular(checkout / "linked.toml", limit=100) == b"x = 1\n"
    with pytest.raises(OSError):
        mountwrite.read_regular(checkout / "linked.toml", limit=100, follow=False)


@pytest.mark.parametrize("kind", ["fifo", "endless", "device"])
@pytest.mark.parametrize("follow", [True, False])
def test_read_regular_refuses_a_fifo_or_device_without_blocking(checkout, special, kind, follow):
    path = special(checkout / "foldyard.toml", kind)
    with pytest.raises(OSError):
        special.promptly(lambda: mountwrite.read_regular(path, limit=1 << 20, follow=follow))


def test_read_regular_refuses_a_directory(checkout):
    (checkout / "foldyard.toml").mkdir()
    with pytest.raises(OSError):
        mountwrite.read_regular(checkout / "foldyard.toml", limit=100)


def test_read_regular_refuses_a_file_over_the_limit(checkout):
    (checkout / "big").write_bytes(b"x" * 101)
    with pytest.raises(OSError, match="over 100 bytes"):
        mountwrite.read_regular(checkout / "big", limit=100)
    assert mountwrite.read_regular(checkout / "big", limit=101) == b"x" * 101
    (checkout / "huge").write_bytes(b"x" * ((16 << 20) + 1))
    with pytest.raises(OSError, match="over 16 MiB"):  # the words a refusal note shows
        mountwrite.read_regular(checkout / "huge", limit=mountwrite.TEXT_LIMIT)


def test_read_regular_caps_a_file_that_grows_while_it_is_read(checkout, monkeypatch):
    # The size is checked before the read and again as it goes: the box can append meanwhile.
    (checkout / "growing").write_bytes(b"x" * 101)
    real = mountwrite.os.fstat
    monkeypatch.setattr(
        mountwrite.os,
        "fstat",
        lambda fd: os.stat_result((*tuple(real(fd))[:6], 1, *tuple(real(fd))[7:])),
    )
    with pytest.raises(OSError, match="over 100 bytes"):
        mountwrite.read_regular(checkout / "growing", limit=100)


def test_read_regular_reads_a_link_to_dev_null_as_empty(checkout):
    # People link `foldyard.local.toml` to /dev/null; it was read (and adopted) as empty, and a
    # read of it ends at once. Any other device is refused.
    (checkout / "foldyard.local.toml").symlink_to(os.devnull)
    assert mountwrite.read_regular(checkout / "foldyard.local.toml", limit=100) == b""


def test_read_regular_says_why_it_refused(checkout, special):
    path = special(checkout / "foldyard.toml", "fifo")
    with pytest.raises(OSError) as refused:
        mountwrite.read_regular(path, limit=100)
    assert refused.value.strerror == "not a regular file"


def test_read_regular_says_absent_as_file_not_found(checkout):
    # Callers tell "absent" from "refused" by it (configpin records absence as state).
    with pytest.raises(FileNotFoundError):
        mountwrite.read_regular(checkout / "nope", limit=100)
