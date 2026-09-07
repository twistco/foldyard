#!/usr/bin/env python3
"""Config pinning — the host runs the ``foldyard.toml`` it ADOPTED, not the one in the checkout.

``foldyard.toml`` (and its gitignored ``foldyard.local.toml`` overlay) is **inside the mount**:
an in-box agent, a package postinstall, or a branch you checked out to review can all rewrite it.
Yet the host supervisor used to re-read it from the working tree on EVERY reconcile tick, rebuild
its daemon specs from it, and restart the affected daemon on any change — so a repo edit reached
the Mac's credential daemons within ~2 seconds, unattended. That is the same class the host-exec
audit closed field by field (``[proxy] allow``, ``default_deny``, ``[[inject]] token_env``,
``[engine].cli``, ``[plugins.github].permissions`` — see
``ADR-0023``), but at the level of the CHANNEL rather than any one
key: while the file is live input, every field added to it re-opens the hole by default.

So the host reads a SNAPSHOT under ``~/.foldyard/adopted/<checkout>-<hash>/`` — outside the mount,
where nothing in the yard can reach it, and keyed by the checkout PATH so nothing the config
declares can redirect the lookup — and the working tree is only ever compared against it:

  - the supervisor reconciles from the pin, every tick, forever; a tree edit changes nothing,
  - drift is REPORTED (supervisor log line, one macOS notification, a doctor row), never applied,
  - adoption is an explicit host act: the ``adopt`` / ``revert`` / ``ignore`` prompt every
    ``fy up`` / ``fy box up`` / ``fy host`` runs before starting anything (:func:`gate`), or the
    ``fy config adopt|revert`` verbs.

Scope, stated honestly: the pin governs what the HOST does — the supervisor's daemons, injection
rules, capture/passthrough, the posture surface `fy mode`/the TUI render. Stack and box wiring
(compose files, ``[box]`` tools, ports) still read the working tree, because their blast radius is
the VM the yard already owns; the audit's triage question is "does this need host privileges?",
and that half doesn't.

Two things this deliberately does NOT claim. Adopting is a human reading a diff, so it is only as
strong as that reading — and foldyard itself is installed ``--editable`` from the consumer checkout
(ADR-0013's in-repo carve-out), so until the spinout the package's own code is box-writable and a
Mac-side launch adopts it via the code fingerprint. The pin closes the unattended config channel,
not those.

Host-only (like :mod:`foldyard.allowlist`): in the box there is no pin and everything falls back to
the working tree, which is what a box session should see anyway. Stdlib only.
"""

from __future__ import annotations

import dataclasses
import difflib
import hashlib
import json
import os
import re
import shutil
import sys
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from . import config, term

if TYPE_CHECKING:
    from collections.abc import Callable

# The consumer config files the host pins. Order matters: it is the deep-merge order
# (``foldyard.local.toml`` over ``foldyard.toml``), the same one ``config._toml_ambient`` uses.
# The local overlay is pinned TOO, and is if anything the sharper case: it's gitignored, so an
# entry added to it there is invisible in `git status` while still winning the merge.
PINNED_FILES = ("foldyard.toml", "foldyard.local.toml")

# Written beside the snapshot to record that an adoption HAPPENED, and to POINT AT the generation
# it adopted. Adoption state can't be inferred from the snapshot's contents: adopting a checkout
# that declares no config at all stores no files, which read as "never adopted" — so a
# `foldyard.toml` conjured in the box afterwards would be honoured live, with no adoption, which
# is the whole thing this prevents. (Reachable in practice: a worktree on a branch from before the
# file existed.)
ADOPTED_MARKER = "adopted.json"

# Each adoption writes a COMPLETE new snapshot under `<pin>/generations/gen-*` and only then swings
# the marker at it. The files are never edited in place, because in-place is not atomic ACROSS
# files: the supervisor re-reads the pin every ~2 s from another process, so a two-file adoption
# had a window where it read the new `foldyard.toml` beside the old `foldyard.local.toml` — a
# merged config that never existed anywhere, driving credential injection. Worse, an adopt
# interrupted mid-way left that mixture PERMANENTLY marked adopted (the marker-last ordering only
# fails safe on the very first adoption; on a re-adoption the previous marker is already there).
# A generation is written whole or not at all, and the pointer switch is one atomic rename.
GENERATIONS = "generations"

# A line that carries no TOML: blank, or a comment taking up the whole line. Both sides are
# stripped of these before diffing (:func:`_significant`), which is what keeps the drift report
# to REAL changes — a file whose comment block was rewritten used to bury a one-line
# `[proxy] passthrough` edit under hundreds of `#` lines, and then get cut off mid-review.
_NOISE = re.compile(r"\s*(#.*)?")

# A TOML table header — `[proxy]`, `[[inject]]`, `[claude.env]`. Used as the locator printed above
# a group of changed lines: with no context lines there is nothing else to say WHERE a change is.
_SECTION = re.compile(r"\[\[?[^]]*]]?")


def _pin_root() -> Path:
    """The host-side root holding every checkout's adopted copy.

    Deliberately NOT ``config.state_dir()``: that resolves through ``config.project()``, which
    reads ``[project].name`` **out of the working tree**. A checkout that renamed itself would
    therefore look up its adoption in a different directory, find none, and fall back to the
    working tree — the config pin unlocked by editing the config. The lookup key must be something
    the mount can't restate, so it isn't derived from the config at all. ``FOLDYARD_STATE_DIR`` is
    still honoured: it's host env, and the tests + the state-relocation escape hatch need it."""
    env = os.environ.get("FOLDYARD_STATE_DIR")
    base = Path(env).expanduser().resolve() if env else Path.home() / ".foldyard"
    return base / "adopted"


def pin_dir(cfg: config.Config) -> Path:
    """Where ``cfg``'s adopted copy lives — keyed by the CHECKOUT PATH (see :func:`_pin_root`),
    which the box cannot change, rather than by anything the config declares. Per checkout, so a
    worktree keeps its own adoption: its ``foldyard.toml`` may legitimately differ per branch.

    The directory name carries the checkout's basename purely so the tree is browsable; the hash
    is what identifies it."""
    path = str(cfg.repo_root)
    key = hashlib.sha256(path.encode()).hexdigest()[:16]
    name = re.sub(r"[^A-Za-z0-9_.-]+", "-", Path(path).name) or "repo"
    return _pin_root() / f"{name}-{key}"


def _read_files(base: Path) -> dict[str, bytes | None]:
    """Each pinned file's bytes under ``base``, ``None`` when absent — absence is meaningful
    state (no ``foldyard.local.toml`` is different from an empty one), so it's recorded rather
    than collapsed to ``b""``."""
    out: dict[str, bytes | None] = {}
    for name in PINNED_FILES:
        try:
            out[name] = (base / name).read_bytes()
        except OSError:
            out[name] = None
    return out


def digest(files: dict[str, bytes | None]) -> str:
    """A short content fingerprint of one file set — the identity the supervisor's drift log and
    the notification dedupe on (re-report a NEW change, not the same one every 2 s tick)."""
    h = hashlib.sha256()
    for name in PINNED_FILES:
        data = files.get(name)
        h.update(name.encode() + b"\x00")
        h.update(b"-" if data is None else f"{len(data)}:".encode() + data)
        h.update(b"\x00")
    return h.hexdigest()[:16]


def _parse(data: bytes | None) -> dict:
    """One pinned file's bytes → its TOML table. Mirrors ``config._read_toml``: absent or
    malformed contributes ``{}`` rather than raising, so a damaged pin degrades to "this consumer
    declares nothing" (no proxy table ⇒ no injectors) instead of crashing the reconcile loop."""
    if not data:
        return {}
    try:
        import tomllib
    except ModuleNotFoundError:  # pragma: no cover — <3.11 only
        return {}
    try:
        return tomllib.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return {}


def merged_toml(files: dict[str, bytes | None]) -> dict:
    """The resolved config a file set represents — ``foldyard.local.toml`` deep-merged OVER
    ``foldyard.toml`` and ``disabled`` blocks dropped, exactly as ``config._toml_ambient`` resolves
    the working tree (both go through ``config.merge_config``, so the host can't resolve a pinned
    file set differently from the box reading the same bytes)."""
    return config.merge_config(_parse(files[PINNED_FILES[0]]), _parse(files[PINNED_FILES[1]]))


@dataclass(frozen=True)
class Drift:
    """One checkout's working tree vs the copy the host adopted."""

    cfg: config.Config
    tree: dict[str, bytes | None]
    pinned: dict[str, bytes | None]
    adopted: bool  # the marker is present — an adoption happened, whatever it stored

    @property
    def worktree(self) -> str:
        return self.cfg.worktree

    @property
    def label(self) -> str:
        return f"worktree {self.cfg.worktree}" if self.cfg.worktree else "main"

    @property
    def pinned_exists(self) -> bool:
        """Has anything been adopted yet? Reads the recorded MARKER, not the stored bytes: "we
        adopted a checkout with no config" and "nobody has adopted anything" are different states
        and only one of them may fall back to the working tree. ``False`` on a first run (and only
        then — the record lives in the Mac home, out of the yard's reach)."""
        return self.adopted

    @property
    def changed(self) -> bool:
        """Does the working tree differ from what the host adopted? Always ``False`` when nothing
        is pinned yet: "not adopted" is a first-run state to resolve, not a change to report."""
        return self.pinned_exists and self.tree != self.pinned

    def tree_digest(self) -> str:
        return digest(self.tree)

    def pinned_digest(self) -> str:
        """The ADOPTED copy's fingerprint — what the host is running (``""`` when nothing is
        adopted). Distinct from :meth:`tree_digest` exactly when the checkout has drifted, which
        is when a report naming one of them has to be clear about which."""
        return digest(self.pinned) if self.adopted else ""

    def summary(self) -> str:
        """``N added, M removed`` across the changed files — the one line that says how big this
        is before anyone reads the diff (and the whole answer when the change is one line).

        Counts the same lines :meth:`diff` prints, so the two can't disagree; when a file changed
        but nothing meaningful did, it says so rather than reporting a confusing ``0, 0``."""
        added, removed = _tally(self.diff())
        files = ", ".join(n for n in PINNED_FILES if self.pinned.get(n) != self.tree.get(n))
        if not (added or removed):
            return f"only blank/comment lines changed in {files}"
        return f"{added} line(s) added, {removed} removed in {files} (blank/comment lines ignored)"

    def diff(self) -> str:
        """The CHANGED lines, adopted → working tree, across every pinned file
        (:func:`diff_files`) — what adopting would change about what the host runs."""
        return diff_files(self.pinned, self.tree, old="adopted", new="tree")


def diff_files(
    old_files: dict[str, bytes | None],
    new_files: dict[str, bytes | None],
    *,
    old: str,
    new: str,
) -> str:
    """The CHANGED lines between two pinned-file sets, labelled ``old``/``new``.

    Deliberately not a unified diff. This is read in two places where scrolling isn't
    available — the launch gate's prompt and a terminal — and the thing being reviewed is a
    security decision, so the failure mode that matters is a real change lost in noise. Two
    rules follow: blank and comment-only lines are dropped from BOTH sides before comparing
    (so a rewritten comment block reports nothing), and no context lines are printed, which
    is what makes it short enough to show in full — there is no truncation. The enclosing
    table header (``@@ [proxy] @@``) stands in for context as the locator.

    Decoded with ``replace`` (never raise on a file someone made non-UTF-8). Plain text:
    color is applied at the print site (``term.paint_diff``), where the stream is known.

    The one thing it can hide: a line INSIDE a multi-line string that looks like a comment or
    is blank (the filter is line-based, not a TOML parse). That can never make a difference
    silent — the callers compare BYTES to decide whether there is one, and report an empty diff
    as "only blank/comment lines changed" rather than as no change.

    Shared by the two comparisons that matter, so neither can render differently from the other:
    adopted → tree (:meth:`Drift.diff`) and main's adopted copy → a worktree's tree
    (:func:`main_baseline`)."""
    lines: list[str] = []
    for name in PINNED_FILES:
        before, after = old_files.get(name), new_files.get(name)
        if before == after:
            continue
        body = _changed_lines(_significant(_text(before)), _significant(_text(after)))
        if not body:
            continue
        lines.append(f"--- {old}/{name}" + ("  (absent)" if before is None else ""))
        lines.append(f"+++ {new}/{name}" + ("  (absent)" if after is None else ""))
        lines.extend(body)
    return "\n".join(lines)


def _tally(body: str) -> tuple[int, int]:
    """``(added, removed)`` over a :func:`diff_files` body — the ``+++``/``---`` headers start
    with the same characters as the lines they introduce, so they're excluded explicitly."""
    added = removed = 0
    for line in body.splitlines():
        if line.startswith("+") and not line.startswith("+++"):
            added += 1
        elif line.startswith("-") and not line.startswith("---"):
            removed += 1
    return added, removed


def _text(data: bytes | None) -> str:
    return "" if data is None else data.decode("utf-8", "replace")


def _open_multiline_after(line: str, open_delim: str | None) -> str | None:
    """The TOML multiline-string delimiter (``\"\"\"`` or ``\'\'\'``) still open at the end of
    ``line``, given the one open at its start. A small tokenizer rather than a regex because the
    three things that look alike — a comment, a `#` inside a single-line string, and a `#` inside
    a multiline string — are told apart only by what came before them on the line."""
    i, n = 0, len(line)
    while i < n:
        if open_delim == "'''":
            # A multiline LITERAL string: TOML defines no escapes inside one, so the first ''' ends
            # it and a scan is exactly a find.
            j = line.find(open_delim, i)
            if j == -1:
                return open_delim
            i, open_delim = j + 3, None
            continue
        if open_delim:
            # A multiline BASIC string, where `\` escapes the next character — so `\"""` is a quote
            # followed by two more (which don't close it) and `\\` is a backslash that escapes
            # nothing after it. A plain find() reads that first `"""` as the terminator, closes the
            # string early, and hands every line below it back to the noise filter: comments
            # dropped, `[…]` read as a table header. That is the silencing :func:`_significant`
            # exists to prevent, reached from inside a string the diff never left.
            while i < n:
                if line[i] == "\\":
                    i += 2  # past the line's end ⇒ a line-ending backslash; the string stays open
                    continue
                if line.startswith('"""', i):
                    i, open_delim = i + 3, None
                    break
                i += 1
            if open_delim:
                return open_delim
            continue
        c = line[i]
        if c == "#":
            return None  # the rest is a comment
        if line.startswith('"""', i) or line.startswith("'''", i):
            open_delim = line[i : i + 3]
            i += 3
            continue
        if c in "\"'":  # a single-line string: skip to its close (basic strings escape)
            j = i + 1
            while j < n and line[j] != c:
                j += 2 if c == '"' and line[j] == "\\" else 1
            i = j + 1
            continue
        i += 1
    return open_delim


def _significant(text: str) -> list[tuple[str, str]]:
    """``text``'s config-carrying lines as ``(enclosing table, line)``.

    Blank and comment-only lines are dropped, and trailing whitespace with them — none of it
    reaches the merged TOML, so none of it belongs in a diff someone has to read a decision out
    of. The table each line sits under is captured here rather than recovered later, because
    after the diff there is no way back to a dropped line's position.

    Inside a multiline string NOTHING is noise and nothing is a table header: `#changed` in a
    `[claude] prompt` is data the host reads, and filtering it used to yield "changed, nothing to
    show" — ``Drift.changed`` true, the diff body empty."""
    out: list[tuple[str, str]] = []
    section = ""
    open_delim: str | None = None
    for raw in text.splitlines():
        if open_delim:
            line = raw
        else:
            line = raw.rstrip()
            if _NOISE.fullmatch(line):
                continue
            if _SECTION.fullmatch(line.strip()):
                section = line.strip()
        out.append((section, line))
        open_delim = _open_multiline_after(line, open_delim)
    return out


def _changed_lines(old: list[tuple[str, str]], new: list[tuple[str, str]]) -> list[str]:
    """The ``-``/``+`` lines between two :func:`_significant` sequences, each run preceded by its
    table header when that moves — every changed line, no context, no elision.

    ``autojunk=False``: difflib's default treats a line appearing in >1% of a 200+ line sequence
    as junk, and a config file is full of repeated lines (``enabled = true`` under a dozen
    tables) — with it on, a long file's diff quietly degrades into "replaced everything"."""
    matcher = difflib.SequenceMatcher(None, [ln for _s, ln in old], [ln for _s, ln in new], False)
    out: list[str] = []
    locator: str | None = None
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            continue
        for sign, (section, line) in [
            *(("-", row) for row in old[i1:i2]),
            *(("+", row) for row in new[j1:j2]),
        ]:
            if section != locator:
                # A changed line that IS its own table header locates itself; don't say it twice.
                if section and line.strip() != section:
                    out.append(f"@@ {section} @@")
                locator = section
            out.append(f"{sign}{line}")
    return out


def size(drift: Drift) -> str:
    """How big the thing being adopted is, for the first-adoption line — the floor under the
    report when there is no reviewed baseline to diff against (:func:`main_baseline`)."""
    files = [n for n in PINNED_FILES if drift.tree.get(n) is not None]
    lines = sum(len(_text(drift.tree.get(n)).splitlines()) for n in files)
    return f"{lines} lines across {', '.join(files)}" if files else "no config files at all"


def _legacy_pin_dir(cfg: config.Config) -> Path:
    """Where adoptions lived before they were keyed by checkout path: ``<posture_dir>/config/``.

    Read-only, and read ONLY when the current location holds nothing — see :func:`inspect`. Losing
    track of an adoption is not a neutral event here: "never adopted" is the one state that adopts
    the working tree, so a layout change that orphans a pin would silently adopt whatever the tree
    happens to say. That is what happened when this move landed."""
    with config.using(cfg):
        return config.posture_dir() / "config"


def _marker(pinned: Path) -> dict | None:
    """The adoption record, or ``None`` when there isn't a usable one.

    Unparseable counts as none: the marker is what SELECTS the generation, so a damaged one leaves
    us unable to say which snapshot was adopted — and "read whatever else is in the directory" is
    precisely the mixed-snapshot read generations exist to rule out. Falling through to "never
    adopted" puts it in front of an operator instead."""
    try:
        return json.loads((pinned / ADOPTED_MARKER).read_bytes())
    except (OSError, ValueError):
        return None


def current_dir(cfg: config.Config) -> Path | None:
    """The directory holding ``cfg``'s adopted files — the marker's generation, or the pin root
    itself for a pre-generation adoption. ``None`` when nothing usable is adopted."""
    pinned = pin_dir(cfg)
    record = _marker(pinned)
    if record is None:
        return None
    gen = str(record.get("generation") or "")
    if not gen:
        return pinned  # adopted before generations existed: the files sit at the root
    target = pinned / GENERATIONS / gen
    # A marker naming a generation that isn't there is a LOST adoption, not a licence to read
    # whatever else is lying around. Fail closed to "never adopted" — the gate then asks.
    return target if target.is_dir() else None


def inspect(cfg: config.Config) -> Drift:
    """Compare ``cfg``'s checkout against its adopted copy. Pure reads — safe on any tick.

    Reads EXCLUSIVELY from the one generation the marker selects (:func:`current_dir`), so a tick
    landing during an adoption sees the old snapshot whole or the new snapshot whole — never a
    file from each.

    Nothing here consults the parsed TOML (``cfg.toml``): the snapshot is located by checkout path
    (:func:`pin_dir`), so a mutable field like ``[project].name`` can't redirect the lookup. The
    one exception is the legacy fallback, which by definition has to look where the old layout
    put things; the next :func:`adopt` rewrites it to the current location."""
    selected = current_dir(cfg)
    empty: dict[str, bytes | None] = dict.fromkeys(PINNED_FILES)
    files = _read_files(selected) if selected else empty
    adopted = selected is not None
    if not adopted:
        legacy = _read_files(_legacy_pin_dir(cfg))
        if any(v is not None for v in legacy.values()):
            files, adopted = legacy, True  # a pre-move adoption is still an adoption
    return Drift(cfg=cfg, tree=_read_files(cfg.repo_root), pinned=files, adopted=adopted)


# ── a worktree measured against main ──────────────────────────────────────────────────


@dataclass(frozen=True)
class Baseline:
    """A checkout's working tree against the config the host ALREADY runs for main.

    The comparison a FIRST adoption needs. On its own axis a fresh worktree has nothing to diff
    against — the report is a digest and a line count, which is not a review — yet a worktree is a
    branch off main, and main is normally adopted already. So the answerable question is not "what
    are these 200 lines" but "what would this branch grant the host that main hasn't already been
    granted". Usually nothing, and saying so is what turns the decision into one keypress.

    Measured against main's ADOPTED snapshot, never main's working tree: that file is inside the
    mount like every other one, so a tree baseline would let unreviewed config launder itself
    through a worktree ("no change vs main", while main's tree was rewritten in the box an hour
    ago). A baseline is only worth anything if an operator approved it."""

    digest: str  # main's adopted fingerprint — WHICH reviewed config this was measured against
    body: str  # the changed lines (:func:`diff_files`); ``""`` when nothing meaningful differs
    identical: bool  # byte-for-byte the same file set as main's adopted copy

    def summary(self) -> str:
        """``N added, M removed`` — the size of what this branch declares on top of main."""
        added, removed = _tally(self.body)
        return f"{added} line(s) added, {removed} removed"


def main_baseline(drift: Drift) -> Baseline | None:
    """``drift``'s working tree against what the host runs for main — ``None`` when there is no
    such baseline: this checkout IS main (nothing sits above it), main was never adopted, or the
    lookup failed.

    Best-effort by construction, like :func:`_stale_key_notes`: it decorates a decision and must
    never be able to block one. Read-only — it inspects main's pin, it never adopts anything."""
    if not drift.worktree:
        return None
    try:
        from . import devmode

        main_cfg = config.resolve(worktree="", repo=devmode.main_repo())
        if main_cfg.repo_root == drift.cfg.repo_root:
            return None  # a "worktree" resolving to the primary checkout has nothing above it
        main = inspect(main_cfg)
        if not main.adopted:
            return None
        return Baseline(
            digest=digest(main.pinned),
            body=diff_files(main.pinned, drift.tree, old="main-adopted", new="tree"),
            identical=main.pinned == drift.tree,
        )
    except Exception:  # defensive: a baseline must never be what blocks a launch verb
        return None


def first_adoption_headline(drift: Drift, base: Baseline | None) -> str:
    """The one line introducing what a checkout with NOTHING adopted has to show for itself.
    Unindented and uncolored — every surface renders it differently (stderr, a typer print, a
    Textual modal), so they share the words rather than the formatting.

    With a baseline that's the comparison against main. Without one — main's own first adoption, or
    a worktree cut before main was ever adopted — it introduces :meth:`Drift.diff` against the
    empty pin, i.e. the config itself with its blank and comment lines taken out. That fallback is
    the point: "190 lines across foldyard.toml" is a measurement, not a review, and the comment
    stripping is what makes the real thing short enough to read at a prompt (a typical consumer
    config is ~40% config-carrying lines)."""
    if base is None:
        if not drift.diff():
            return "It declares nothing the host would read — no config lines at all."
        lead = (
            "Nothing is adopted for main either, so there is no reviewed config to measure this "
            "against. "
            if drift.worktree
            else ""
        )
        return f"{lead}Everything it declares is below, with comments and blank lines stripped:"
    if base.identical:
        return "Byte-identical to the config the host runs for main — adopting grants nothing new."
    if not base.body:
        return (
            "The same config the host runs for main (only blank/comment lines differ) — adopting "
            "grants nothing new."
        )
    return f"Against the config the host runs for main (adopted {base.digest}): {base.summary()}."


def first_adoption_body(drift: Drift, base: Baseline | None) -> str:
    """The reviewable block under :func:`first_adoption_headline`: the diff against main when
    there is a reviewed baseline, otherwise the config itself (:meth:`Drift.diff` against an empty
    pin renders every config-carrying line as an addition).

    Never both. Where a baseline exists it is smaller AND more informative — dumping the whole
    file underneath it would bury the handful of lines the branch actually changed."""
    return base.body if base is not None else drift.diff()


def effective(cfg: config.Config) -> config.Config:
    """``cfg`` with its ``toml`` replaced by the ADOPTED snapshot — what every host-side reader of
    a worktree's config gets (``devmode.worktree_config``), and therefore what the supervisor
    reconciles from.

    Falls back to the working tree in two cases, both deliberate: **in the box**, where there is no
    host state dir and a box session should see its own checkout; and when **nothing is pinned
    yet**, so a first run (or a supervisor that predates pinning) keeps working instead of losing
    every daemon — the ``gate`` on the launch verbs pins it moments later. Never raises: a config
    read on the reconcile hot path must degrade, not crash."""
    if config.in_box():
        return cfg
    try:
        drift = inspect(cfg)
        if not drift.pinned_exists:
            return cfg
        return dataclasses.replace(cfg, toml=merged_toml(drift.pinned))
    except (OSError, ValueError):  # pragma: no cover — unreadable state dir
        return cfg


# ── mutating the pin (Mac only) ───────────────────────────────────────────────────────


def _require_host() -> None:
    if config.in_box():
        raise SystemExit(
            "✗ adopting config is Mac-only: the box must not adopt its own foldyard.toml "
            "(the adopted copy lives in the Mac home, outside the shared mount)."
        )


def _write_file(path: Path, data: bytes) -> None:
    """Write ATOMICALLY (temp + ``os.replace``, unique temp name per writer). The supervisor
    re-reads these files every tick from another process, so a torn read would resolve as a
    malformed pin — i.e. a posture with no injectors — for however long the write took."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    tmp = Path(name)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def _prune_generations(gens: Path, keep: str) -> None:
    """Drop superseded generations — keeping the current one AND the most recent other.

    Not "keep only current": a reader that resolved the pointer a moment before the switch is
    still reading the previous directory, and deleting it out from under it would hand it exactly
    the half-missing snapshot generations exist to prevent. Staging dirs (``.staging-*``) are left
    alone — one may belong to an adoption still in flight."""
    try:
        others = sorted(
            (p for p in gens.iterdir() if p.is_dir() and p.name.startswith("gen-")),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
    except OSError:  # pragma: no cover — defensive: pruning must never fail an adoption
        return
    for old in [p for p in others if p.name != keep][1:]:
        shutil.rmtree(old, ignore_errors=True)


class ReviewStale(RuntimeError):
    """The tree changed between being shown to the operator and being adopted."""


def adopt(cfg: config.Config, *, reviewed: str | None = None, baseline: str | None = None) -> Drift:
    """Adopt the working tree: copy it into a NEW generation of the pin, then point the marker at
    it. THE moment of trust in this module — every other path only ever reads what this wrote.
    Returns the (now clean) drift.

    ``reviewed`` is the tree digest the operator was SHOWN (the prompt, the TUI modal). This
    re-reads the tree, so without it an edit landing between the review and the answer — the box
    writing the checkout while the operator reads — would be adopted unseen, which is the channel
    this module exists to close. A mismatch raises :class:`ReviewStale`; the caller asks again
    over the new tree. ``fy config adopt`` passes nothing: it IS the operator's explicit answer.

    ``baseline`` is the same guarantee for the OTHER side of a first adoption's comparison. What
    the operator reviewed there was not this config but its diff against main's adopted copy
    (:func:`main_baseline`) — "adopting grants nothing new" is a statement about main, and it stops
    being true the moment main adopts something else. Both sides move on their own: a second
    terminal running ``fy config adopt`` on main, or the supervisor's own gate, is enough, and the
    tree digest cannot see it. So the baseline is re-read here too and a move is
    :class:`ReviewStale` like any other — including main's pin disappearing, where the comparison
    shown has no basis left at all. Only checked when a baseline was SHOWN: passing ``None``
    (main's own first adoption, or a worktree cut before main was adopted) keeps the existing
    behaviour, and there the operator read the whole config rather than a diff against it.

    Staged whole, switched once (see :data:`GENERATIONS`): every file lands in a fresh directory
    that no reader can see yet, the directory is renamed into place, and only then does the marker
    move. Absence stays state for free — a file the tree doesn't have is simply never written into
    the generation, so it can't be inherited from the copy this one replaces. An interrupted adopt
    leaves an orphan staging dir and the PREVIOUS generation still selected, never a mixture."""
    _require_host()
    drift = inspect(cfg)
    if reviewed is not None and drift.tree_digest() != reviewed:
        raise ReviewStale(
            f"foldyard.toml changed again since it was reviewed ({reviewed} → "
            f"{drift.tree_digest()}) — nothing adopted; review the new version"
        )
    if baseline is not None:
        current = main_baseline(drift)
        if current is None or current.digest != baseline:
            now = current.digest if current is not None else "nothing adopted for main"
            raise ReviewStale(
                f"the config the host runs for main changed since this was measured against it "
                f"({baseline} → {now}) — nothing adopted; review the new comparison"
            )
    target = pin_dir(cfg)
    gens = target / GENERATIONS
    gens.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(dir=gens, prefix=".staging-"))
    try:
        for name in PINNED_FILES:
            data = drift.tree[name]
            if data is not None:
                _write_file(staging / name, data)
        gen = f"gen-{staging.name.removeprefix('.staging-')}"
        os.rename(staging, gens / gen)  # the generation becomes visible, complete, in one step
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    # The marker goes LAST and names the generation, so an interrupted adopt leaves the previous
    # pointer in force (or, on a first adoption, "not adopted") — never a marker over a partial
    # snapshot.
    _write_file(
        target / ADOPTED_MARKER,
        json.dumps(
            {
                "adopted": datetime.now(UTC).isoformat(timespec="seconds"),
                "checkout": str(cfg.repo_root),
                "worktree": cfg.worktree,
                "digest": digest(drift.tree),
                "generation": gen,
            },
            indent=2,
        ).encode()
        + b"\n",
    )
    _prune_generations(gens, keep=gen)
    return inspect(cfg)


def revert(cfg: config.Config) -> list[str]:
    """Restore the working tree to the adopted copy — the answer to "I didn't write this". Returns
    one human line per file acted on.

    A tree file the pin records as ABSENT (the classic case: a ``foldyard.local.toml`` conjured
    inside the box, gitignored so `git status` never showed it) is moved ASIDE into the pin dir
    rather than deleted: revert must be safe to choose in a hurry, and that file might have been
    the operator's own."""
    _require_host()
    drift = inspect(cfg)
    if not drift.pinned_exists:
        raise SystemExit("✗ nothing adopted yet for this checkout — `fy config adopt` first.")
    target = pin_dir(cfg)
    done: list[str] = []
    for name in PINNED_FILES:
        old, new = drift.pinned[name], drift.tree[name]
        if old == new:
            continue
        path = cfg.repo_root / name
        if old is None:
            aside = target / f"removed-{name}"
            _write_file(aside, new or b"")
            path.unlink(missing_ok=True)
            done.append(f"removed {name} (it wasn't in the adopted copy) — kept a copy at {aside}")
        else:
            _write_file(path, old)
            done.append(f"restored {name} from the adopted copy")
    return done


# ── the adopt / revert / ignore gate ──────────────────────────────────────────────────


def _stale_key_notes(cfg: config.Config, tree: dict[str, bytes | None]) -> list[str]:
    """Lines flagging keys the TREE declares that read as security config but are IGNORED
    (:data:`foldyard.exposure.IGNORED_KEYS`) — printed at the decision moment, because a diff
    that edits a dead key looks exactly like a security change and adopting it changes nothing.
    That is how a `[proxy] allow` edit got reviewed, adopted, and did nothing while the box's
    egress stayed refused. Best-effort: the gate must never break on its own footnote."""
    try:
        from . import exposure

        tree_cfg = dataclasses.replace(cfg, toml=merged_toml(tree))
        shared, local = (_parse(tree[name]) for name in PINNED_FILES)
        with config.using(tree_cfg):
            ignored = exposure._ignored(tree_cfg, shared, local)
    except Exception:  # pragma: no cover — defensive: a report must not block the gate
        return []
    notes = []
    for key, values, fix, _origin in ignored:
        n = len(values)
        notes.append(
            f"  ⚠ NB {key} ({n} entr{'y' if n == 1 else 'ies'}) is IGNORED — nothing consumes it."
        )
        notes.append(f"     {fix}")
    return notes


def resolve(
    cfg: config.Config,
    *,
    interactive: bool,
    prompt: Callable[[str], str],
    echo: Callable[[str], None],
) -> str:
    """Settle ``cfg``'s config drift before the host acts on it. Returns a status:

      - ``"clean"`` — the tree matches the adopted copy (the normal case; silent).
      - ``"pinned"`` — nothing was adopted yet, so the tree was adopted now (first run).
      - ``"adopted"`` / ``"reverted"`` — the operator chose one.
      - ``"ignored"`` — the operator deferred: the ADOPTED copy stays in force and the prompt
        returns next time. Also what an unparseable answer settles on — the safe default is the
        one that changes nothing.
      - ``"unresolved"`` — drift with no TTY (the detached supervisor launch, CI): warn loudly,
        keep running the adopted copy, resolve later on a Mac terminal.
      - ``"unadopted"`` — NOTHING adopted yet and no TTY: nothing was adopted, and there is no
        reviewed copy to fall back on either. :func:`gate` refuses the launch verb on this one.

    Pure but for the injected ``prompt``/``echo``, like ``keyless.ensure_cred`` — tests drive it
    with no real stdin."""
    if config.in_box():
        return "clean"
    drift = inspect(cfg)
    if not drift.pinned_exists:
        # Nothing adopted for this checkout. Usually a genuinely fresh one — but it is ALSO what a
        # lost adoption looks like (an upgrade that moves the store, a moved checkout, a pruned
        # ~/.foldyard), and adopting on sight turns that into "the host silently took whatever the
        # tree said". That happened for real: an operator answered `ignore` at `fy up`, a version
        # change orphaned the pin, and `fy box up` adopted the declined config seconds later. So on
        # a terminal it is ACKNOWLEDGED — Enter is still enough, but it can't pass unseen.
        echo(f"▶ nothing adopted yet for this checkout [{drift.label}].")
        echo(f"  Adopting means the host runs THIS config: {drift.tree_digest()}, {size(drift)}.")
        # …and what it actually SAYS. A first adoption used to print a digest and a line count,
        # which is not something anyone can decide on: against main where there's a reviewed
        # baseline, otherwise the config itself with the comments stripped out.
        base = main_baseline(drift)
        echo(f"  {first_adoption_headline(drift, base)}")
        body = first_adoption_body(drift, base)
        if body:
            echo(term.paint_diff(body) if term.color_enabled(sys.stderr) else body)
            echo("  What these declarations let the host DO: `fy config widenings`.")
        for note in _stale_key_notes(cfg, drift.tree):
            echo(note)
        if not interactive:
            # No TTY ⇒ nobody read it, so it does not get adopted. Adopting here was the same
            # unattended-config channel this module exists to close, reached from the other end:
            # the detached supervisor launch (or a scripted `fy up`) would hand the host whatever
            # the tree said, with the acknowledgement above scrolling past in a log file. The
            # launch gate turns this status into a refusal — see :func:`gate`.
            echo(
                "  No terminal here, so nothing was adopted — the host won't run a config nobody "
                "has read. On the Mac: `fy config diff`, then `fy config adopt`."
            )
            return "unadopted"
        answer = prompt("  [a]dopt (default) · [i]gnore for now: ").strip().lower()
        if answer in ("i", "ignore", "n", "no"):
            echo("  (ignored — the host reads the working tree until you adopt; asked again.)")
            return "ignored"
        try:
            # Both sides of what was shown: the tree, and — when the review WAS a diff against main
            # — the main pin it was measured against. See :func:`adopt`.
            adopt(
                cfg,
                reviewed=drift.tree_digest(),
                baseline=base.digest if base is not None else None,
            )
        except ReviewStale as e:
            echo(f"✗ {e}")
            return "unadopted"
        echo("✓ adopted — later edits need `fy config adopt` (`fy up`/`fy host` ask).")
        return "pinned"
    if not drift.changed:
        return "clean"

    echo(f"⚠ foldyard.toml changed since the host adopted it [{drift.label}] — {drift.summary()}")
    body = drift.diff()
    if body:
        # Colored here too, not just in `fy config diff`: this is the moment someone decides, and
        # a wall of monochrome context lines is where a one-line insertion hides.
        echo(term.paint_diff(body) if term.color_enabled(sys.stderr) else body)
    for note in _stale_key_notes(cfg, drift.tree):
        echo(note)
    echo(
        "  This file drives host-side credential injection and egress capture, and ANYTHING that "
        "can write the checkout can change it — so the host keeps running the adopted copy."
    )
    if not interactive:
        echo(
            "  No terminal here, so nothing was adopted. On the Mac: `fy config diff`, then "
            "`fy config adopt` (accept) or `fy config revert` (put the file back)."
        )
        return "unresolved"
    answer = prompt("  [a]dopt · [r]evert the file · [i]gnore for now (default): ").strip().lower()
    # EXACT matches only. A prefix test read "abort" as adopt and "reset" as revert — the two
    # words someone reaches for when they want out, mapped to the two irreversible-ish answers,
    # one of which hands the host a config nobody reviewed. Anything unrecognised falls through
    # to ignore, which changes nothing and asks again.
    if answer in ("a", "adopt"):
        try:
            adopt(cfg, reviewed=drift.tree_digest())
        except ReviewStale as e:
            echo(f"✗ {e}")
            return "unresolved"
        echo("✓ adopted — the host now runs the working tree's config.")
        return "adopted"
    if answer in ("r", "revert"):
        for line in revert(cfg):
            echo(f"✓ {line}")
        return "reverted"
    echo("  (ignored — still running the adopted copy; you'll be asked again next time.)")
    return "ignored"


def gate(verb: str) -> str:
    """The launch-path gate: resolve the ACTIVE checkout's config drift before ``verb`` brings any
    host daemon up. Called by ``supervisor.ensure_background`` (so `fy up` / `fy box up` / `fy
    claude` all inherit it) and by ``supervisor.main`` (`fy host`), on the operator's own terminal.

    Best-effort about ITS OWN failures: a broken gate must never be what stops the yard starting —
    the supervisor still reconciles from the pin either way, so failing here loses the prompt, not
    the boundary. One deliberate exception: ``"unadopted"`` (nothing pinned, no terminal to adopt
    on) is REFUSED rather than waved through, because there the pin is empty — ``effective()``
    would fall back to the working tree and the host would run a config nobody has read. The fix
    is one command, and it is in the message."""
    if config.in_box():
        return "clean"
    try:
        from . import devmode

        cfg = devmode.worktree_config(config.active_worktree())
        status = resolve(
            cfg,
            interactive=sys.stdin.isatty(),
            prompt=input,
            echo=lambda m: print(m, file=sys.stderr, flush=True),
        )
        if status == "unadopted":
            raise SystemExit(
                f"✗ {verb}: this checkout's foldyard.toml has never been adopted, and there is no "
                "terminal here to adopt it on.\n"
                "  Run `fy config adopt` on the Mac (`fy config diff` first), then retry."
            )
        return status
    except SystemExit:
        raise
    except Exception as e:  # pragma: no cover — defensive: never block a launch verb
        print(f"⚠ {verb}: couldn't check foldyard.toml against the adopted copy ({e})", flush=True)
        return "error"
