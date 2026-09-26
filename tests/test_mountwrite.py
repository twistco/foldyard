"""mountwrite — host writes into the repo mount never follow a symlink the box planted.

The box can replace any path in the checkout with a symlink to a file the operator owns
(``~/.ssh/authorized_keys``, the project's ``host.env``). Every host-side write there — the
supervisor's posture mirror each tick, the git heal's sync marker, the staged proxy CA, the build
gate's edit of ``foldyard.toml`` — must land in the checkout or not at all."""

from __future__ import annotations

import json

import pytest

from foldyard import config, devmode, githeal, mountwrite
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
    gitdir = tmp_path / "gitdir"
    gitdir.mkdir()
    (gitdir / githeal.SYNC_FILE).symlink_to(victim)
    githeal._record(gitdir, "abc123")
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
