"""foldyard — stack-colocated, secretless-by-default, laptop-local dev environment.

The unit of isolation is the project's whole dev stack, not an agent process. The design
decisions and their reasoning are in ``docs/adrs/``.

Keep this module import-light: the recipe hot path runs ``python3 -m foldyard mode
env`` under a plain system python3, so importing the package must not pull Textual
or any non-stdlib dependency (the CLI imports those lazily, per-verb).
"""


def __getattr__(name: str) -> str:
    """``foldyard.__version__``, resolved LAZILY from the install metadata (PEP 562).

    Lazy because this module is on the recipe hot path (``python3 -m foldyard mode env`` runs per
    `just` recipe) and a plain import must stay free — attribute access pays the metadata read,
    nobody else does.

    Metadata rather than a literal because a literal is a SECOND copy of ``[project].version``:
    it sat at ``0.0.1`` with no consumers while ``box.py`` pinned the box off
    ``metadata.version("foldyard")``, so the first release bump would have moved the one the box
    reads and left this one behind. One source now — the wheel's own metadata.

    (An editable install bakes its metadata at install time, so bumping ``pyproject.toml`` in a dev
    checkout needs a reinstall before this reports the new number. That is the same reinstall the
    box's staged wheel already needs, so it isn't a new sharp edge.)"""
    if name == "__version__":
        from importlib import metadata

        try:
            return metadata.version("foldyard")
        except metadata.PackageNotFoundError:
            # Running from a source tree with no dist-info at all (a bare PYTHONPATH import).
            # Answer honestly rather than inventing a number a version floor might trust.
            return "0+unknown"
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
