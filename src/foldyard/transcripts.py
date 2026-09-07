"""`foldyard transcripts [dest]` — copy the dev box's agent transcripts out to a
stable host archive (RUN ON THE MAC).

Faithful port of the `transcripts` recipe. In-box agents write session transcripts to
gitignored bound-out dirs inside the repo. Those dirs survive `nuke`, but they're fragile
for long-term keeping (`git clean -fdx` wipes them, the worktree may be removed, they carry
two-hop ownership). This copies configured agents' transcripts into their native Mac history
dirs — or a dir you pass / set in FOLDYARD_TRANSCRIPTS_ARCHIVE.

SAFE by construction: we only ever read an agent's transcript subtree (Claude ``projects/``
or Codex ``sessions/``) — never sibling credential/config files or settings. The copy is
ADDITIVE: rsync with --update and NO --delete, so the archive only grows and nothing there
is removed or overwritten by an older copy (set DRY_RUN=1 to preview).

RUN ON THE MAC: the archive lives in the Mac's home, which the box can't reach.
Becomes part of the `claude-code` plugin later (ADR-0015). Stdlib only.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from shutil import which

from . import config, stack


def _err(*a: object) -> None:
    print(*a, file=sys.stderr, flush=True)


def _box_running(engine: str, box: str) -> bool:
    out = subprocess.run(
        [engine, "ps", "-q", "-f", f"name=^{box}$", "-f", "status=running"],
        capture_output=True,
        text=True,
    )
    return bool(out.stdout.strip())


def _archive_dest(dest: str = "") -> Path:
    """The durable Mac archive: an explicit ``dest`` / ``$FOLDYARD_TRANSCRIPTS_ARCHIVE``,
    else the Mac's own ``~/.claude/projects`` (so ``claude --resume`` lists in-box sessions)."""
    return Path(
        dest or os.environ.get("FOLDYARD_TRANSCRIPTS_ARCHIVE") or (Path.home() / ".claude/projects")
    ).expanduser()


def _codex_archive_dest(dest: str = "") -> Path:
    """Codex's native history, or a distinct child of an explicit shared archive."""
    if dest or os.environ.get("FOLDYARD_TRANSCRIPTS_ARCHIVE"):
        return _archive_dest(dest) / "codex-sessions"
    return Path.home() / ".codex/sessions"


# rsync exit codes that are NOT a failure when the source is a LIVE transcript tree: 24 = "some
# files vanished before transfer" and 23 = "partial transfer due to error", both of which a session
# file being appended/rotated mid-sync produces routinely. `fy transcripts` never saw these because
# it runs against a QUIESCENT tree at box-down; the per-interval sweep runs against a tree the agent
# is actively writing, so without this whitelist the sweep would report a failure on a regular basis
# and its notification would become noise.
#
# Scoped to the sweep (``quiet=True``) ON PURPOSE — the destructive-op path stays strict. There the
# tree IS quiescent, so 23/24 mean a real partial archive, and that rc is what makes
# `worktree remove` refuse to delete the checkout it just failed to save.
BENIGN_RC = frozenset({0, 23, 24})

# The rsync flag set, deliberately FROZEN at what macOS can run everywhere: `-rtu --no-perms` is
# rsync 2.6.9 vocabulary. Do NOT reach for --append-verify (rsync 3.0+, absent from the 2.6.9 macOS
# ships) or other modern flags — macOS 15+ substitutes openrsync, a different implementation with a
# narrower flag set again, so any addition is a portability bet across three implementations. A
# transcript is small and append-only; whole-file copy is fine, and a torn final line (copied
# mid-append) is invalid JSONL in the ARCHIVE only until the next pass rewrites it.
_FLAGS = ["-rtu", "--no-perms"]


def _rsync_additive(
    stage: Path, dest_dir: Path, dry: bool, *, quiet: bool = False
) -> tuple[int, str]:
    """Additive rsync (``-u``, NO ``--delete``) of a transcript tree into the archive, so
    the archive only grows and nothing is overwritten by an older copy. ``-rt`` keeps mtimes
    (needed for ``-u``); no ``-p/-o/-g`` so the archive gets clean Mac-user ownership/perms.

    Returns ``(returncode, stderr)``. ``quiet`` drops both the echoed command and ``--stats`` and
    captures the output instead of inheriting our fds — the supervisor tees stdout to
    ``host-supervisor.log``, so an unconditionally-chatty sweep would write a stats block there
    every interval, forever."""
    dest_dir.mkdir(parents=True, exist_ok=True)
    stats = [] if quiet else ["--stats"]
    cmd = (
        ["rsync"] + (["--dry-run"] if dry else []) + _FLAGS + stats + [f"{stage}/", f"{dest_dir}/"]
    )
    if quiet:
        out = subprocess.run(cmd, capture_output=True, text=True)
        return out.returncode, (out.stderr or "").strip()
    _err("+ " + " ".join(cmd))
    return subprocess.run(cmd).returncode, ""


def bound_out_dir(checkout: Path, here: str) -> Path:
    """The Mac-side dir a box binds Claude's ``projects/`` out to. It survives ``nuke`` (a
    plain host dir), but is fragile to ``git clean -fdx`` and worktree removal — which is
    why destructive ops :func:`sync_archive` it to the durable store first."""
    return checkout / here / ".devbox-claude/projects"


def codex_bound_out_dir(checkout: Path, here: str) -> Path:
    """The Mac-side directory bound to Codex's resumable ``~/.codex/sessions`` tree."""
    return checkout / here / ".devbox-codex/sessions"


def sync_archive(
    src_dir: Path, *, dest: str = "", what: str = "transcripts", quiet: bool = False
) -> int:
    """Additively copy one bound-out agent transcript dir → its durable Mac archive BEFORE a
    destructive op deletes the source. Returns 0 on success or nothing-to-do, non-zero only when
    a real copy was attempted and rsync failed. Never raises.

    No-op (0) inside the box (the archive lives in the Mac's home, unreachable there) and when
    ``src_dir`` is missing/empty. SAFE by construction: only ever reads the transcript tree handed
    in — never sibling credential/config files.

    ``quiet`` suppresses the progress lines and returns rc only (for :func:`sweep`, which runs on
    the supervisor tick and reports on EDGES instead); :data:`BENIGN_RC` still applies."""
    if config.in_box():
        return 0
    if not (src_dir.is_dir() and any(src_dir.iterdir())):
        return 0  # nothing worth keeping
    if not which("rsync"):
        if not quiet:
            _err("⚠ rsync not found (ships with macOS) — can't archive transcripts.")
        return 1
    dest_dir = _archive_dest(dest)
    if not quiet:
        print(f"▶ archiving {what} → {dest_dir}")
    rc, _err_text = _rsync_additive(src_dir, dest_dir, bool(os.environ.get("DRY_RUN")), quiet=quiet)
    if quiet:
        return 0 if rc in BENIGN_RC else rc
    if rc == 0:
        print(f"✓ {what} archived → {dest_dir}")
    else:
        _err(f"✗ archiving {what} failed (rsync rc={rc}).")
    return rc


# ── the supervisor's per-interval auto-sync (`[claude]/[codex] transcript_sync_seconds`) ──────
#
# Why a HOST-side timer and not something in the box: the box cannot reach the Mac's home, so it
# can never push. It doesn't have to — `FY_TRANSCRIPTS` bind-mounts the bound-out dir into the box
# at the agent's own `projects/`/`sessions/` path, so an in-box write lands on the host path as it
# happens. The sweep is therefore a HOST-LOCAL rsync between two host paths: no engine call, no box
# round-trip. It is also why polling is right rather than a watcher — Mac-side inotify doesn't fire
# for guest writes (virtiofs, podman#22343), the very gap that makes hot reload miss Mac edits.
#
# What it buys, given the data is already on the host: the bound-out dir is fragile (a `git clean
# -fdx` wipes it, `worktree remove` takes it away) and invisible to `claude --resume`, which reads
# the native `~/.claude/projects`. Promoting continuously means a crashed box or a reclaimed
# worktree costs you nothing, instead of losing everything since the last `fy box down`.

# Per-(worktree, agent) auto-sync state: the tick countdown, the interval it was computed for, and
# the last verdict + failure streak that make reporting EDGE-triggered. Module-global, like
# supervisor._probe_state — there is one supervisor process.
_sweep_state: dict[tuple[str, str], dict] = {}

# Consecutive failed passes before the log line escalates to a push notification. A failed ARCHIVE
# sync degrades nothing the box does — the bound-out dir still holds every transcript, live — so
# this is deliberately not urgent, and deliberately not a `fy doctor` row.
NOTIFY_AFTER_FAILURES = 3


def _sweep_plan() -> list[tuple[str, float, Path, str]]:
    """``(label, interval, bound-out source, archive dest)`` for each configured agent whose
    auto-sync is ON, resolved under the CURRENTLY BOUND config — so the caller's
    ``with config.using(worktree_config(wt))`` is what points this at the right checkout."""
    checkout = config.repo_root()
    here = config.dev_vm_rel()
    plan: list[tuple[str, float, Path, str]] = []
    if config.claude_enabled() and (secs := config.claude_transcript_sync_seconds()):
        plan.append(("Claude", secs, bound_out_dir(checkout, here), ""))
    if config.codex_enabled() and (secs := config.codex_transcript_sync_seconds()):
        plan.append(
            ("Codex", secs, codex_bound_out_dir(checkout, here), str(_codex_archive_dest()))
        )
    return plan


def _due(key: tuple[str, str], interval: float, tick_seconds: float) -> bool:
    """Is this agent due this tick? The interval is expressed in TICKS — ``interval // tick``,
    floored, minimum 1 — so a configured 15s on a 2s tick runs every 7 ticks (14s effective).
    Counting ticks rather than holding a monotonic deadline is what makes that floor exact; it also
    means a slow tick (a reconcile pass with capability probes due can take seconds) stretches the
    real interval rather than firing a catch-up burst. Re-arms from zero when the configured
    interval changes, so an adopted config edit takes effect on the next tick."""
    ticks = max(1, int(interval // tick_seconds))
    state = _sweep_state.setdefault(key, {"left": 0, "ticks": ticks, "failing": False, "streak": 0})
    if state["ticks"] != ticks:
        state["ticks"], state["left"] = ticks, 0
    if state["left"] > 0:
        state["left"] -= 1
        return False
    state["left"] = ticks - 1
    return True


def _report(log, notify, key: tuple[str, str], label: str, wt: str, rc: int, detail: str) -> None:
    """Report one pass on its EDGES only — the same discipline supervisor.run_capability_probes
    uses. A healthy sweep says NOTHING (it runs every few seconds forever, and the supervisor tees
    stdout to host-supervisor.log); a broken one logs once when it breaks and once when it heals,
    then escalates to the operator only if it stays broken."""
    state = _sweep_state[key]
    where = f" ({wt})" if wt else ""
    if rc == 0:
        if state["failing"]:
            log(f"transcript-sync: {label}{where} recovered")
        state["failing"], state["streak"] = False, 0
        return
    state["streak"] += 1
    if not state["failing"]:
        state["failing"] = True
        log(f"transcript-sync: {label}{where} FAILED — {detail}")
    # Exactly ON the threshold, so a sync that stays broken notifies once, not every interval.
    if notify and state["streak"] == NOTIFY_AFTER_FAILURES:
        notify(
            "foldyard: transcript sync failing",
            f"{label}{where}: {detail} — archive is going stale. Run `fy transcripts`.",
        )


def sweep(log, wt: str = "", *, tick_seconds: float = 2.0, notify=None) -> None:
    """One supervisor tick's auto-sync pass for the BOUND worktree's configured agents. Off (a
    no-op with no state touched) unless that checkout's ADOPTED config sets a positive
    ``transcript_sync_seconds`` — so a working-tree edit needs ``fy config adopt`` first, like every
    other host-side read. Never raises: one agent's failure must not wedge the reconcile loop."""
    if config.in_box():  # the archive is in the host's home, unreachable from the box
        return
    try:
        plan = _sweep_plan()
    except Exception as e:  # a config hiccup must never wedge the reconcile loop
        log(f"transcript-sync: sweep failed: {type(e).__name__}: {e}")
        return
    for label, interval, src, dest in plan:
        key = (wt, label)
        try:
            if not _due(key, interval, tick_seconds):
                continue
            rc = sync_archive(src, dest=dest, quiet=True)
            detail = f"rsync rc={rc}"
        except Exception as e:  # one agent's hiccup must not skip the other
            rc, detail = 1, f"{type(e).__name__}: {e}"
        _report(log, notify, key, label, wt, rc, detail)


def sync_current(env: dict, *, what: str = "transcripts", dest: str = "") -> int:
    """Best-effort archive of the CURRENT checkout's configured agents' bound-out transcripts.

    Used by box-down / nuke, which keep the bound-out dirs but should still promote them to the
    durable store. Honours the same ``DEVBOX_TRANSCRIPTS``, ``DEVBOX_CODEX_TRANSCRIPTS``, and
    ``HERE`` overrides the box used at up-time. Each agent is attempted independently so one
    failed or absent source never prevents the other from being archived.
    """
    checkout = Path(env.get("FOLDYARD_CHECKOUT") or stack.main_repo())
    here = env.get("HERE") or config.dev_vm_rel()
    src = Path(os.environ.get("DEVBOX_TRANSCRIPTS") or bound_out_dir(checkout, here))
    rc = sync_archive(src, what=what, dest=dest)

    if config.codex_enabled():
        codex_src = Path(
            os.environ.get("DEVBOX_CODEX_TRANSCRIPTS") or codex_bound_out_dir(checkout, here)
        )
        codex_what = "Codex transcripts" if what == "transcripts" else f"{what} (Codex transcripts)"
        codex_rc = sync_archive(
            codex_src,
            what=codex_what,
            dest=str(_codex_archive_dest(dest)),
        )
        rc = rc or codex_rc
    return rc


def transcripts(dest: str = "") -> int:
    # Same box-detection `verify`/the recipe use: IN_DEVBOX, or the preset in-box socket.
    if config.in_box():
        _err("✗ run this ON THE MAC — the archive is in the Mac's home, unreachable from the box.")
        return 1
    if not which("rsync"):
        _err("✗ rsync not found (ships with macOS).")
        return 1

    ctx = stack.resolve()
    env = ctx.env
    engine = config.engine()
    box = f"{ctx.project}-devbox"
    checkout = Path(env["FOLDYARD_CHECKOUT"])
    here = env.get("HERE") or config.dev_vm_rel()
    # The Claude bound-out dir on the Mac, honouring the same override as `devbox up`.
    src_dir = Path(os.environ.get("DEVBOX_TRANSCRIPTS") or bound_out_dir(checkout, here))
    # Default into the Mac's own history so `claude --resume` lists these in-box sessions.
    claude_dest = _archive_dest(dest)
    dry = bool(os.environ.get("DRY_RUN"))

    rc = _sync_source(
        src_dir,
        claude_dest,
        dry,
        engine=engine,
        box=box,
        box_home_cmd='printf %s "${CLAUDE_CONFIG_DIR:-$HOME/.claude}"',
        box_rel="projects",
        label="Claude transcripts",
        # A Codex-only project has no Claude source; continue on to its configured agent.
        missing_ok=config.codex_enabled(),
    )

    # A [codex] table opts into Codex installation and transcript persistence. Keep its archive in
    # Codex's native history directory so `codex resume` can see the sessions. For the legacy
    # explicit-destination form, use a child dir to avoid mixing two agents' unrelated layouts.
    if config.codex_enabled():
        codex_src = Path(
            os.environ.get("DEVBOX_CODEX_TRANSCRIPTS") or codex_bound_out_dir(checkout, here)
        )
        codex_dest = _codex_archive_dest(dest)
        codex_rc = _sync_source(
            codex_src,
            codex_dest,
            dry,
            engine=engine,
            box=box,
            box_home_cmd='printf %s "${CODEX_HOME:-$HOME/.codex}"',
            box_rel="sessions",
            label="Codex transcripts",
            missing_ok=rc == 0,
        )
        rc = rc or codex_rc
    return rc


def _sync_source(
    src_dir: Path,
    dest_dir: Path,
    dry: bool,
    *,
    engine: str,
    box: str,
    box_home_cmd: str,
    box_rel: str,
    label: str,
    missing_ok: bool = False,
) -> int:
    """Sync one agent's bound history, falling back to its live in-box directory."""
    # Stage the source. Prefer the bound-out dir (already on the Mac, no engine needed);
    # if it's empty, fall back to pulling the live projects/ tree out of the running box
    # over the socket with `<engine> cp` — projects/ ONLY, so creds never come along.
    tmp: str | None = None
    if src_dir.is_dir() and any(src_dir.iterdir()):
        stage, origin = src_dir, "bound-out dir"
    elif _box_running(engine, box):
        # Resolve where Claude actually writes IN the box: the image bakes HOME=/home/vscode,
        # so even as root ~/.claude is /home/vscode/.claude. Honour CLAUDE_CONFIG_DIR too.
        out = subprocess.run(
            [engine, "exec", box, "sh", "-c", box_home_cmd],
            capture_output=True,
            text=True,
        )
        box_agent_home = out.stdout.strip()
        if not box_agent_home:
            box_agent_home = f"/home/vscode/{'.claude' if box_rel == 'projects' else '.codex'}"
        tmp = tempfile.mkdtemp()
        box_src = f"{box_agent_home}/{box_rel}"
        _err(f"+ {engine} cp {box}:{box_src}/. {tmp}")
        if subprocess.run([engine, "cp", f"{box}:{box_src}/.", tmp]).returncode != 0:
            _err(f"✗ couldn't read {label.lower()} from {box}:{box_src}.")
            shutil.rmtree(tmp, ignore_errors=True)
            return 1
        stage, origin = Path(tmp), f"live box {box} ({box_src})"
    else:
        if missing_ok:
            print(f"✓ no {label.lower()} yet — nothing to do.")
            return 0
        _err(f"✗ nothing to copy: {src_dir} is empty and the box ({box}) isn't running.")
        return 1

    try:
        if not any(stage.iterdir()):
            print(f"✓ no {label.lower()} yet (source is empty) — nothing to do.")
            return 0

        print(f"▶ source: {origin}")
        print(f"  from:   {stage}")
        print(f"  to:     {dest_dir}" + ("   (DRY RUN — no changes)" if dry else ""))
        rc, _ = _rsync_additive(stage, dest_dir, dry)
        if rc == 0:
            print(f"✓ {label} {'would be ' if dry else ''}synced → {dest_dir}")
        return rc
    finally:
        if tmp:
            shutil.rmtree(tmp, ignore_errors=True)
