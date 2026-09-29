"""foldyard — stack-colocated, secretless-by-default, laptop-local dev environment.

The unit of isolation is the project's whole dev stack, not an agent process. The design
decisions and their reasoning are in ``docs/adrs/``.

Keep this module import-light: the recipe hot path runs ``python3 -m foldyard mode
env`` under a plain system python3, so importing the package must not pull Textual
or any non-stdlib dependency (the CLI imports those lazily, per-verb).
"""


def __getattr__(name: str) -> str:
    """``foldyard.__version__``, resolved LAZILY from the install (PEP 562).

    Lazy because this module is on the recipe hot path (``python3 -m foldyard mode env`` runs per
    `just` recipe) and a plain import must stay free — attribute access pays the metadata read,
    nobody else does.

    Metadata rather than a literal because a literal is a SECOND copy of ``[project].version``:
    it sat at ``0.0.1`` with no consumers while ``box.py`` pinned the box off
    ``metadata.version("foldyard")``, so the first release bump would have moved the one the box
    reads and left this one behind. One source now — ``[project].version``, as the install sees it.

    An editable install is the exception to "the metadata": it records the version once, at install
    time, while the code it runs keeps moving with the checkout — so a bump in ``pyproject.toml``
    left this naming the OLD number, and the box drift nag, the version window and doctor all
    believed it. There the checkout's own ``pyproject.toml`` is read instead
    (:func:`_editable_version`)."""
    if name == "__version__":
        from importlib import metadata
        from pathlib import Path

        try:
            dist = metadata.distribution("foldyard")
        except metadata.PackageNotFoundError:
            # Running from a source tree with no dist-info at all (a bare PYTHONPATH import).
            # Answer honestly rather than inventing a number a version floor might trust.
            return "0+unknown"
        return _editable_version(dist, Path(__file__).parent) or dist.version
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def _editable_version(dist, package_dir) -> str | None:
    """``[project].version`` from the checkout an editable install runs, or None.

    Two conditions, both required. The install's PEP 610 record (``direct_url.json``) must say
    editable, from a local ``file:`` directory — a wheel reports what it was built as, and a
    bare source tree with no record stays ``0+unknown``. And the package actually running must be
    that directory's ``src/foldyard``: the record names a checkout, but a second checkout on
    ``PYTHONPATH`` can shadow it, and then the recorded one's pyproject describes other code.

    Anything short of a static string version (no record, another checkout, a missing
    or malformed pyproject, a dynamic version) is None, and the caller keeps the metadata.

    No new trust: an editable install already executes this checkout's code, so reading a version
    string beside it widens nothing."""
    import json
    import tomllib
    from pathlib import Path
    from urllib.parse import unquote, urlsplit

    try:
        record = json.loads(dist.read_text("direct_url.json") or "null")
        if not isinstance(record, dict) or not isinstance(record.get("dir_info"), dict):
            return None
        if record["dir_info"].get("editable") is not True or not isinstance(record.get("url"), str):
            return None
        url = urlsplit(record["url"])
        if url.scheme != "file" or url.netloc not in ("", "localhost"):
            return None
        root = Path(unquote(url.path)).resolve()
        if Path(package_dir).resolve() != root / "src" / "foldyard":
            return None
        with (root / "pyproject.toml").open("rb") as f:
            project = tomllib.load(f).get("project")
    except (OSError, ValueError):  # unreadable, undecodable, not JSON/TOML
        return None
    version = project.get("version") if isinstance(project, dict) else None
    return version if isinstance(version, str) else None  # "" is falsy: the caller's `or` skips it
