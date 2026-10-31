"""selfspawn.py — foldyard runs its own modules as children with ``python -P -m``, never plain
``-m``: plain ``-m`` puts the working directory first on ``sys.path``, the children usually run in
a checkout, and the box can write the checkout. These run the real interpreter (allowlisted by the
hermetic guard), not a stub."""

from __future__ import annotations

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
    subprocess.run(argv, cwd=tmp_path, capture_output=True, timeout=60)
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
