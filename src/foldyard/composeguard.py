"""Refuse compose paths that would make the host-side compose client read outside the checkout.

The compose client runs on the operator's computer and opens files BY PATH: ``env_file:``,
``label_file:``, build contexts, Dockerfiles, additional contexts and ``ssh`` keys,
``include:``/``extends:`` files, ``secrets``/``configs`` files. The compose files are box-writable,
so any of those naming a host path hands its content to a container or image the box can read.
(Bind mounts are not in the list: the engine resolves them inside the VM, which holds only the
repo.)

This is an INTERIM clamp, and it says so: a key-by-key list the compose spec can outgrow, and a
check on a path the client opens moments later by name — a box that swaps a checked file for a
symlink in between can still win that race. The structural fix runs the compose client in the VM,
where host files are unreachable by construction.

Two passes, because rendering both helps and hides:

- :func:`rendered_problems` reads ``compose config`` output: compose's OWN interpolation (``${VAR}``
  against the allowlisted env and the project ``.env``), so there is nothing to second-guess.
- :func:`raw_problems` reads the files themselves for what rendering consumes or leaves unresolved:
  ``include:`` is merged away, and ``extends:`` is printed as-is, so the paths inside an included
  or extended file never reach the render. Those files must use literal paths.

Stdlib + PyYAML (a podman-compose dependency, so present wherever the client is), imported lazily.
"""

from __future__ import annotations

import os
from collections.abc import Iterable, Iterator
from pathlib import Path

# `additional_contexts` values that name an image or another build stage, not a directory.
_IMAGE_REFS = ("docker-image://", "container-image://", "oci-layout://", "service:", "target:")


def _real(path: Path) -> Path:
    return Path(os.path.realpath(path))


def _inside(path: Path, roots: list[Path]) -> bool:
    """Does ``path`` — every symlink resolved — land in one of ``roots``?"""
    real = _real(path)
    return any(real == r or r in real.parents for r in roots)


def _remote(value: str) -> bool:
    """A build context the client would FETCH (git/http), with whatever credentials it finds."""
    return "://" in value or value.startswith("git@") or ".git#" in value or value.endswith(".git")


def _as_list(value) -> list:
    if value is None:
        return []
    return list(value) if isinstance(value, list) else [value]


def _service_paths(svc: dict) -> Iterator[tuple[str, object, str]]:
    """``(key, value, base)`` for each path a service names. ``base`` says what a relative value
    is relative to: ``"dir"`` (the file's/project's directory), ``"context"`` (the build context),
    or ``"any"`` (an additional context — providers disagree, so every candidate must hold)."""
    for key in ("env_file", "label_file"):
        for entry in _as_list(svc.get(key)):
            yield key, entry.get("path") if isinstance(entry, dict) else entry, "dir"
    extends = svc.get("extends")
    if isinstance(extends, dict) and extends.get("file") is not None:
        yield "extends.file", extends["file"], "dir"
    build = svc.get("build")
    if isinstance(build, str):
        yield "build", build, "dir"
    elif isinstance(build, dict):
        yield "build.context", build.get("context", "."), "dir"
        if build.get("dockerfile") is not None:
            yield "build.dockerfile", build["dockerfile"], "context"
        extra = build.get("additional_contexts") or {}
        items = (
            extra.items()
            if isinstance(extra, dict)
            else (str(e).split("=", 1) if "=" in str(e) else (str(e), "") for e in extra)
        )
        for name, value in items:
            if not str(value).startswith(_IMAGE_REFS):
                yield f"build.additional_contexts.{name}", value, "any"
        ssh = build.get("ssh")
        ssh_items = ssh.values() if isinstance(ssh, dict) else _as_list(ssh)
        for entry in ssh_items:
            text = str(entry)
            path = text.split("=", 1)[1] if "=" in text else ("" if text == "default" else text)
            if path:
                yield "build.ssh", path, "dir"


def _top_level_paths(doc: dict) -> Iterator[tuple[str, object]]:
    for table in ("secrets", "configs"):
        entries = doc.get(table)
        if isinstance(entries, dict):
            for name, spec in entries.items():
                if isinstance(spec, dict) and spec.get("file") is not None:
                    yield f"{table}.{name}.file", spec["file"]


class _Checker:
    def __init__(self, roots: Iterable[Path]) -> None:
        self.roots = [_real(r) for r in roots]
        self.problems: list[str] = []

    def refuse(self, where: str, key: str, value: object, why: str) -> None:
        self.problems.append(f"{where}: `{key}: {value}` {why}")

    def path(
        self,
        where: str,
        key: str,
        value: object,
        bases: list[Path],
        *,
        literal_only: bool = False,
    ) -> Path | None:
        """Check one path value against every candidate base; the resolved path, or None."""
        if not isinstance(value, str) or not value:
            self.refuse(where, key, value, "is not a path")
            return None
        if literal_only and "$" in value:
            self.refuse(
                where, key, value, "uses interpolation, which isn't checked here — write it"
            )
            return None
        if key.startswith("build") and _remote(value):
            self.refuse(where, key, value, "is a remote source the client would fetch on the host")
            return None
        resolved = [Path(value) if Path(value).is_absolute() else base / value for base in bases]
        if not all(_inside(p, self.roots) for p in resolved):
            self.refuse(where, key, value, "is outside the checkout")
            return None
        return resolved[0]

    def service(
        self,
        where: str,
        svc: dict,
        base: Path,
        *,
        cwd: Path,
        literal_only: bool = False,
        skip_interpolated: bool = False,
    ) -> list[Path]:
        """Check a service's paths; return the extended files it names (for the raw scan)."""
        context = base
        extended: list[Path] = []
        for key, value, kind in _service_paths(svc):
            # An extended file is scanned from the raw text, so its path must be literal here
            # whatever the file: the render could vouch for the path but never for its content.
            literal = literal_only or key == "extends.file"
            if skip_interpolated and not literal and isinstance(value, str) and "$" in value:
                continue  # left to the rendered pass, which sees compose's own interpolation
            bases = {"dir": [base], "context": [context], "any": [context, base, cwd]}[kind]
            got = self.path(where, key, value, bases, literal_only=literal)
            if got is not None and key in ("build", "build.context"):
                context = got
            if got is not None and key == "extends.file":
                extended.append(got)
        return extended


def rendered_problems(
    doc: dict, project_dir: Path, roots: Iterable[Path], *, cwd: Path | None = None
) -> list[str]:
    """Paths in ``compose config`` output that leave ``roots``. Relative values resolve against
    ``project_dir`` (compose's rule); ``cwd`` is where the host runs the build from (the main
    checkout), one more base an additional context must also hold for."""
    check = _Checker(roots)
    for name, svc in (doc.get("services") or {}).items():
        if isinstance(svc, dict):
            check.service(f"service {name}", svc, project_dir, cwd=cwd or project_dir)
    for key, value in _top_level_paths(doc):
        check.path("top level", key, value, [project_dir])
    return check.problems


def _load(text: str):
    """``yaml.safe_load`` plus the Compose spec's merge tags (``!override`` / ``!reset``), read
    as the value they wrap — so a path under one is still checked. Any other tag stays a
    YAMLError: fail closed."""
    import yaml

    class _ComposeLoader(yaml.SafeLoader):
        pass

    def wrapped(loader, node):
        if isinstance(node, yaml.MappingNode):
            return loader.construct_mapping(node, deep=True)
        if isinstance(node, yaml.SequenceNode):
            return loader.construct_sequence(node, deep=True)
        return loader.construct_scalar(node)

    for tag in ("!override", "!reset"):
        _ComposeLoader.add_constructor(tag, wrapped)
    return yaml.load(text, Loader=_ComposeLoader)  # a SafeLoader subclass: no arbitrary objects


def raw_problems(files: list[Path], project_dir: Path, roots: Iterable[Path]) -> list[str]:
    """What the render can't vouch for, read from the files: the ``-f`` files themselves and the
    project ``.env`` (no symlink out), every ``include:``/``extends:`` target (literal, inside,
    followed recursively) and every path in those targets (literal — they never reach the
    render). A ``-f`` file OUTSIDE the checkout came from the adopted config or the operator's
    environment, not the box, and is not scanned."""
    import yaml

    check = _Checker(roots)
    seen: set[Path] = set()
    dotenv = project_dir / ".env"
    if os.path.lexists(dotenv) and not _inside(dotenv, check.roots):
        check.refuse("project", ".env", dotenv, "is a symlink outside the checkout")

    def scan(path: Path, *, top: bool, base: Path | None = None) -> None:
        real = _real(path)
        if real in seen:
            return
        seen.add(real)
        label = os.path.relpath(path, project_dir)
        try:
            doc = _load(path.read_text()) or {}
        except (OSError, yaml.YAMLError) as e:
            check.problems.append(f"{label}: unreadable ({e})")
            return
        if not isinstance(doc, dict):
            return
        # A top-level file's relative paths resolve against the project directory; an included
        # file's against its `project_directory` (default: its own directory); an extended
        # file's against its own directory.
        base = base or (project_dir if top else path.parent)
        follow: list[tuple[Path, Path | None]] = []
        for entry in _as_list(doc.get("include")):
            spec = entry if isinstance(entry, dict) else {"path": entry}
            rebase = None
            if spec.get("project_directory") is not None:
                rebase = check.path(
                    label,
                    "include.project_directory",
                    spec["project_directory"],
                    [path.parent],
                    literal_only=True,
                )
            for p in _as_list(spec.get("path")):
                got = check.path(label, "include", p, [path.parent], literal_only=True)
                if got is not None:
                    follow.append((got, rebase))
            for env_file in _as_list(spec.get("env_file")):
                check.path(label, "include.env_file", env_file, [path.parent], literal_only=True)
        for name, svc in (doc.get("services") or {}).items():
            if isinstance(svc, dict):
                follow += (
                    (extended, None)
                    for extended in check.service(
                        f"{label} service {name}",
                        svc,
                        base,
                        cwd=base,
                        literal_only=not top,
                        skip_interpolated=top,
                    )
                )
        for key, value in _top_level_paths(doc):
            if not (top and isinstance(value, str) and "$" in value):
                check.path(label, key, value, [base], literal_only=not top)
        for target, rebase in follow:
            if target.is_file():
                # An include pointed outside was already refused; its content is not read.
                scan(target, top=False, base=rebase or target.parent)

    for f in files:
        if not _inside(f, check.roots):
            if any(r in Path(os.path.abspath(f)).parents for r in check.roots):
                check.refuse(os.path.relpath(f, project_dir), "-f", f, "is a symlink out")
            continue
        scan(f, top=True)
    return check.problems
