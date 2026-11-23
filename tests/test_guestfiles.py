"""guestfiles.py — the VM lets go of shared files nothing is using: the kernel's own FUSE setting,
written as root by Lima's boot script. The script runs for real against a stand-in for /sys."""

from __future__ import annotations

import subprocess

import pytest

from foldyard import config, guestfiles


def _run(tmp_path, seconds: int, *, kernel: str = "has it"):
    knob = tmp_path / "inval_wq"
    if kernel == "has it":
        knob.write_text("0\n")
    elif kernel == "refuses":
        knob.mkdir()  # a write to it fails whoever runs the suite, root included
    snippet = guestfiles.release_snippet(seconds).replace(guestfiles.KNOB, str(knob))
    # As guest-boot.sh runs it: errexit on, and the rest of the boot script after it.
    proc = subprocess.run(
        ["bash", "-c", f"set -uo pipefail\nset -e\n{snippet}\necho reached"],
        capture_output=True,
        text=True,
    )
    return proc, knob


@pytest.mark.spawns("bash")
def test_the_boot_script_sets_the_period(tmp_path):
    proc, knob = _run(tmp_path, 60)
    assert knob.read_text().strip() == "60"
    assert "reached" in proc.stdout


@pytest.mark.spawns("bash")
def test_a_kernel_without_the_setting_still_boots(tmp_path):
    proc, knob = _run(tmp_path, 60, kernel="lacks it")
    assert proc.returncode == 0
    assert "reached" in proc.stdout
    assert not knob.exists()  # a kernel parameter is never created, only written


@pytest.mark.spawns("bash")
def test_a_refused_write_is_reported_and_never_fails_the_boot(tmp_path):
    proc, _ = _run(tmp_path, 60, kernel="refuses")
    assert proc.returncode == 0
    assert "reached" in proc.stdout
    assert "refused inval_wq=60" in proc.stdout


@pytest.mark.spawns("bash")
def test_off_writes_nothing(tmp_path):
    proc, knob = _run(tmp_path, 0)
    assert proc.returncode == 0
    assert knob.read_text() == "0\n"


def test_on_by_default(monkeypatch):
    monkeypatch.setattr(config, "_table", lambda name: {})
    monkeypatch.delenv("MACHINE_RELEASE_FILES", raising=False)
    assert config.machine_release_files() == 60


@pytest.mark.parametrize("value", [0, 5, 60, 300, 65535])
def test_the_values_the_kernel_takes(monkeypatch, value):
    monkeypatch.delenv("MACHINE_RELEASE_FILES", raising=False)
    monkeypatch.setattr(config, "_table", lambda name: {"release_files": value})
    assert config.machine_release_files() == value


@pytest.mark.parametrize("value", [-1, 1, 4, 65536, "soon", "60; reboot", True, False, 7.5, None])
def test_anything_else_is_refused_by_name(monkeypatch, value):
    # It lands in a script root runs, so only an integer the kernel takes gets that far.
    monkeypatch.delenv("MACHINE_RELEASE_FILES", raising=False)
    monkeypatch.setattr(config, "_table", lambda name: {"release_files": value})
    with pytest.raises(SystemExit, match="release_files"):
        config.machine_release_files()


def test_env_wins(monkeypatch):
    monkeypatch.setattr(config, "_table", lambda name: {"release_files": 300})
    monkeypatch.setenv("MACHINE_RELEASE_FILES", "0")
    assert config.machine_release_files() == 0
    monkeypatch.setenv("MACHINE_RELEASE_FILES", "3")
    with pytest.raises(SystemExit, match="release_files"):
        config.machine_release_files()
