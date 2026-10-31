"""selfspawn.py — foldyard runs its own modules as children with ``python -P -m``, never plain
``-m``: plain ``-m`` puts the working directory first on ``sys.path``, the children usually run in
a checkout, and the box can write the checkout. These run the real interpreter (allowlisted by the
hermetic guard), not a stub."""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

from foldyard import __version__, selfspawn

SRC = Path(__file__).resolve().parents[1] / "src" / "foldyard"


def _plant(where: Path) -> Path:
    """A `foldyard/` package at ``where`` whose import leaves a marker; returns the marker."""
    marker = where / "planted-code-ran"
    (where / "foldyard").mkdir()
    (where / "foldyard" / "__init__.py").write_text(f"open({str(marker)!r}, 'w').close()\n")
    (where / "foldyard" / "__main__.py").write_text("")
    return marker


def test_a_package_planted_in_the_working_directory_never_runs(tmp_path):
    marker = _plant(tmp_path)
    out = subprocess.run(
        selfspawn.python_m("foldyard", "--version"),
        cwd=tmp_path,
        env=selfspawn.env(),  # a source-tree run's relative PYTHONPATH still finds foldyard
        capture_output=True,
        text=True,
        timeout=60,
    )
    # The INSTALLED foldyard answered (so the child got as far as importing it)…
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == __version__
    # …and the planted one never ran.
    assert not marker.exists()


def test_the_control_plain_dash_m_does_run_the_planted_package(tmp_path):
    # Shows the test above can fail: the same child without -P imports the planted package first.
    marker = _plant(tmp_path)
    argv = [a for a in selfspawn.python_m("foldyard", "--version") if a != "-P"]
    env = selfspawn.env()
    env.pop("PYTHONSAFEPATH", None)  # it would leave the working directory off too
    subprocess.run(argv, cwd=tmp_path, env=env, capture_output=True, timeout=60)
    assert marker.exists()


def test_no_module_spawns_its_own_python_with_plain_dash_m():
    # A new `[sys.executable, "-m", …]` anywhere in the package would reopen the hole, in whatever
    # directory that child inherits. Spawn through selfspawn.python_m instead.
    pattern = re.compile(r"sys\.executable\s*,\s*[\"']-m[\"']")
    offenders = [
        f"{path.relative_to(SRC)}:{text[: m.start()].count(chr(10)) + 1}"
        for path in sorted(SRC.rglob("*.py"))
        if path.name != "selfspawn.py"
        for text in [path.read_text()]
        for m in pattern.finditer(text)
    ]
    assert offenders == []


def test_env_pins_relative_pythonpath_entries_where_the_parent_resolved_them(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PYTHONPATH", f"src{os.pathsep}{os.pathsep}/abs/lib")
    got = selfspawn.env()["PYTHONPATH"].split(os.pathsep)
    assert got == [str(tmp_path / "src"), os.path.join(str(tmp_path), ""), "/abs/lib"]
    assert selfspawn.env({"PATH": "/bin"}) == {"PATH": "/bin"}  # no PYTHONPATH: nothing added


def test_a_relative_pythonpath_never_reaches_into_a_childs_other_directory(tmp_path, monkeypatch):
    # A child run elsewhere (confighash runs in the main checkout) with a relative PYTHONPATH entry:
    # left as is, `plant` would resolve under the CHILD's directory, where a box could plant a
    # `foldyard/`. Pinned by env(), it resolves under the parent's, where there is none.
    parent, child = tmp_path / "parent", tmp_path / "checkout"
    parent.mkdir()
    (child / "plant").mkdir(parents=True)
    marker = _plant(child / "plant")
    # Any PYTHONPATH this run already has (a source-tree run) stays, pinned, AFTER the planted one.
    keep = selfspawn.env().get("PYTHONPATH")
    monkeypatch.chdir(parent)
    monkeypatch.setenv("PYTHONPATH", os.pathsep.join(["plant", *([keep] if keep else [])]))
    argv = selfspawn.python_m("foldyard", "--version")
    out = subprocess.run(
        argv, cwd=child, env=selfspawn.env(), capture_output=True, text=True, timeout=60
    )
    assert out.returncode == 0, out.stderr
    assert not marker.exists()
    # The control: the same relative entry, NOT pinned, does reach the planted package.
    subprocess.run(argv, cwd=child, env=dict(os.environ), capture_output=True, timeout=60)
    assert marker.exists()
