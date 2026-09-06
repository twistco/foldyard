"""foldyard — stack-colocated, secretless-by-default, laptop-local dev environment.

The unit of isolation is the project's whole dev stack, not an agent process. The design
decisions and their reasoning are in ``docs/adrs/``.

Keep this module import-light: the recipe hot path runs ``python3 -m foldyard mode
env`` under a plain system python3, so importing the package must not pull Textual
or any non-stdlib dependency (the CLI imports those lazily, per-verb).
"""

__version__ = "0.0.1"
