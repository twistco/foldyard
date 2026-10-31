"""How foldyard runs its own modules as a child: ``python -P -m <module>``, never plain ``-m``.

Plain ``python -m`` puts the WORKING DIRECTORY first on ``sys.path``, ahead of the installed
package, and foldyard's children usually run in a checkout: they inherit it from a `fy` command
started there (the supervisor keeps `fy up`'s directory, and the proxy's minters keep the
supervisor's), or are given it outright (`confighash`). The box can write the checkout, so a
``foldyard/`` package (or a stdlib-named ``json.py``) planted at its root would be imported in place
of foldyard's own: code from the mount run on your computer (ADR-0023), and in a minter's case
beside the credential it was handed. ``-P`` (Python 3.11+, the floor) leaves the working directory
off ``sys.path`` and changes nothing else: ``PYTHONPATH`` and the user site still apply, so a
source-tree run (``PYTHONPATH=src``) keeps working, which ``-I`` would break.

Stdlib-only and import-free: the plugins and the hot path use it.
"""

from __future__ import annotations

import sys


def python_m(module: str, *args: str) -> list[str]:
    """``[this interpreter, -P, -m, module, *args]``: the argv that runs ``module`` as a child."""
    return [sys.executable, "-P", "-m", module, *args]
