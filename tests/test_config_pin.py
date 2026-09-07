"""Config pinning (`foldyard.configpin`) — the host runs the foldyard.toml it ADOPTED.

The bug these pin down: the supervisor rebuilt every worktree's config from the WORKING TREE on
each 2 s tick, so an edit to `[proxy]`/`[[inject]]` — a file anything in the box can write — was
live in the Mac's credential daemons within one tick, unattended. The pin makes repo config inert
until an operator adopts it; the tests below assert exactly that, and that the drift is never
silent.
"""

from __future__ import annotations

import shutil
import types
from pathlib import Path
from typing import cast

import pytest

from foldyard import config, configpin, devmode, plugins, supervisor

TOML = '[project]\nname = "acme"\n\n[proxy]\npassthrough = ["trusted.example"]\n'
EDITED = '[project]\nname = "acme"\n\n[proxy]\npassthrough = ["trusted.example", "evil.example"]\n'


@pytest.fixture
def checkout(tmp_path):
    """A consumer checkout with a foldyard.toml, plus its resolved Config. The pin dir is the
    per-test one conftest's ``isolated_config_pin`` installs, so nothing touches a real
    ``~/.foldyard``."""
    root = tmp_path / "repo"
    root.mkdir()
    (root / "foldyard.toml").write_text(TOML)
    return config.resolve(worktree="", repo=root)


@pytest.fixture
def worktree(checkout, monkeypatch):
    """A sibling worktree of ``checkout`` carrying its own foldyard.toml — the shape a FIRST
    adoption actually arrives in, and the one with a reviewed baseline to compare against."""
    root = checkout.repo_root.parent / "repo-worktrees" / "feat"
    root.mkdir(parents=True)
    (root / "foldyard.toml").write_text(TOML)
    monkeypatch.setattr(devmode, "main_repo", lambda: checkout.repo_root)
    return config.resolve(worktree="feat", repo=root)


def _passthrough_hosts(cfg) -> str:
    """The proxy daemon's PASSTHROUGH_HOSTS under ``cfg``'s ADOPTED config — resolved the way the
    supervisor tick does it (bind the config, then ask the registry), since the plugins read
    ``config.X()`` off the bound context."""
    effective = configpin.effective(cfg)
    with config.using(effective):
        spec = plugins.registry(effective).desired_daemons({"capture": "on"})
    return spec["egress-proxy"]["env"]["PASSTHROUGH_HOSTS"]


def _current(cfg) -> Path:
    """``configpin.current_dir`` for a checkout that IS adopted — the generation directory the
    marker selects, failing the test rather than the type-checker when there isn't one."""
    selected = configpin.current_dir(cfg)
    assert selected is not None, "expected an adopted generation"
    return selected


def _prompted(answer: str):
    """(prompt, echo, lines) for driving `resolve` with a canned answer, like keyless's tests."""
    lines: list[str] = []
    return (lambda _q: answer), lines.append, lines


# ── the core property: the tree is not what the host runs ─────────────────────────────


def test_effective_serves_the_adopted_copy_not_the_working_tree(checkout):
    configpin.adopt(checkout)
    (checkout.repo_root / "foldyard.toml").write_text(EDITED)

    assert configpin.effective(checkout).toml["proxy"]["passthrough"] == ["trusted.example"]
    with config.using(configpin.effective(checkout)):
        assert config.proxy_passthrough() == ["trusted.example"]


def test_the_proxy_daemon_keeps_the_adopted_passthrough_after_an_in_box_edit(checkout):
    """End to end through the plugin that owns the knob: an edit to `[proxy] passthrough` must not
    reach PASSTHROUGH_HOSTS (the hosts the proxy does NOT decrypt or request-log), because that is
    the reported bug — capture, switched off for a host of the checkout's choosing, in one tick."""
    configpin.adopt(checkout)
    (checkout.repo_root / "foldyard.toml").write_text(EDITED)

    assert _passthrough_hosts(checkout) == "trusted.example"

    # …and adopting is what makes it live — the pin is a gate, not a freeze.
    configpin.adopt(checkout)
    assert _passthrough_hosts(checkout) == "trusted.example,evil.example"


def test_worktree_config_is_the_pinned_one(checkout, monkeypatch):
    """The funnel every host-side reader goes through (supervisor tick, TUI, `fy state`)."""
    configpin.adopt(checkout)
    (checkout.repo_root / "foldyard.toml").write_text(EDITED)
    monkeypatch.setattr(devmode, "main_repo", lambda: checkout.repo_root)

    assert devmode.worktree_config("").toml["proxy"]["passthrough"] == ["trusted.example"]


def test_unadopted_and_in_box_both_fall_back_to_the_working_tree(checkout, monkeypatch):
    # Nothing adopted yet (first run, or a supervisor predating pinning): keep working. The pin
    # lives in the Mac home, so the box cannot manufacture this state by deleting it.
    assert configpin.effective(checkout).toml == checkout.toml

    # In the box there is no host state at all — a box session reads its own checkout, so a pin
    # (which it can't see anyway) must not shadow it.
    configpin.adopt(checkout)
    (checkout.repo_root / "foldyard.toml").write_text(EDITED)
    monkeypatch.setattr(config, "in_box", lambda: True)
    in_box_view = configpin.effective(config.resolve(worktree="", repo=checkout.repo_root))
    assert in_box_view.toml["proxy"]["passthrough"] == ["trusted.example", "evil.example"]


def test_a_damaged_pin_declares_nothing_rather_than_falling_back_to_the_tree(checkout):
    """Fail-safe: a pin we can't parse yields an empty config (no `[proxy]` ⇒ no injectors), never
    the tree — "my pin broke" must not mean "the yard runs whatever the checkout says"."""
    configpin.adopt(checkout)
    (_current(checkout) / "foldyard.toml").write_text("this is not = toml [")
    (checkout.repo_root / "foldyard.toml").write_text(EDITED)

    assert configpin.effective(checkout).toml == {}


# ── adopt / revert ────────────────────────────────────────────────────────────────────


def test_adopt_records_absence_so_a_deleted_local_overlay_stays_deleted(checkout):
    local = checkout.repo_root / "foldyard.local.toml"
    local.write_text('[proxy]\npassthrough = ["local.example"]\n')
    configpin.adopt(checkout)
    assert configpin.effective(checkout).toml["proxy"]["passthrough"] == ["local.example"]

    local.unlink()
    configpin.adopt(checkout)
    # Absence is structural now: the new generation is written from scratch, so a file the tree
    # doesn't have simply isn't in it — it can't be inherited from the generation it replaced.
    assert not (_current(checkout) / "foldyard.local.toml").exists()
    assert configpin.effective(checkout).toml["proxy"]["passthrough"] == ["trusted.example"]


def test_revert_restores_the_tree_and_moves_an_unadopted_file_aside(checkout):
    configpin.adopt(checkout)
    (checkout.repo_root / "foldyard.toml").write_text(EDITED)
    # The sharp case: foldyard.local.toml is gitignored, so an entry conjured there wins the merge
    # while `git status` stays clean.
    (checkout.repo_root / "foldyard.local.toml").write_text("[proxy]\npassthrough = []\n")

    done = configpin.revert(checkout)

    assert (checkout.repo_root / "foldyard.toml").read_text() == TOML
    assert not (checkout.repo_root / "foldyard.local.toml").exists()
    # Moved aside, never deleted: revert must be safe to pick in a hurry, and that file might
    # have been the operator's own.
    aside = configpin.pin_dir(checkout) / "removed-foldyard.local.toml"
    assert aside.read_text() == "[proxy]\npassthrough = []\n"
    assert any("restored foldyard.toml" in line for line in done)
    assert not configpin.inspect(checkout).changed


def test_revert_refuses_when_nothing_was_adopted(checkout):
    with pytest.raises(SystemExit, match="nothing adopted"):
        configpin.revert(checkout)


def test_adopt_and_revert_refuse_in_the_box(checkout, monkeypatch):
    """Same boundary as `allowlist.grant`: the yard must not adopt its own config."""
    monkeypatch.setattr(config, "in_box", lambda: True)
    with pytest.raises(SystemExit, match="Mac-only"):
        configpin.adopt(checkout)
    with pytest.raises(SystemExit, match="Mac-only"):
        configpin.revert(checkout)


# ── the adopt / revert / ignore gate ──────────────────────────────────────────────────


def test_first_run_adopts_once_and_says_so(checkout):
    prompt, echo, lines = _prompted("")  # Enter accepts
    assert configpin.resolve(checkout, interactive=True, prompt=prompt, echo=echo) == "pinned"
    assert configpin.inspect(checkout).pinned_exists
    assert any("nothing adopted yet" in line for line in lines)
    assert any("✓ adopted" in line for line in lines)
    # …and it's a one-off: an unchanged tree is silent from then on.
    prompt, echo, lines = _prompted("")
    assert configpin.resolve(checkout, interactive=True, prompt=prompt, echo=echo) == "clean"
    assert lines == []


@pytest.mark.parametrize(
    ("answer", "status", "tree_after"),
    [
        ("a", "adopted", EDITED),
        ("adopt", "adopted", EDITED),
        ("r", "reverted", TOML),
        ("i", "ignored", EDITED),
        ("", "ignored", EDITED),  # empty input takes the option that changes nothing
        ("what?", "ignored", EDITED),
        # Prefix matching read these as adopt/revert — the two words someone types when they want
        # OUT, one of which would have handed the host an unreviewed config.
        ("abort", "ignored", EDITED),
        ("anything", "ignored", EDITED),
        ("reset", "ignored", EDITED),
    ],
)
def test_the_prompt_offers_adopt_revert_ignore(checkout, answer, status, tree_after):
    configpin.adopt(checkout)
    (checkout.repo_root / "foldyard.toml").write_text(EDITED)

    prompt, echo, lines = _prompted(answer)
    assert configpin.resolve(checkout, interactive=True, prompt=prompt, echo=echo) == status

    assert (checkout.repo_root / "foldyard.toml").read_text() == tree_after
    running = configpin.effective(checkout).toml["proxy"]["passthrough"]
    if status == "adopted":
        assert running == ["trusted.example", "evil.example"]
    else:
        # Reverting and ignoring both leave the ADOPTED config in force; only ignoring leaves the
        # tree differing from it, and that has to be re-asked next time rather than remembered.
        assert running == ["trusted.example"]
        assert configpin.inspect(checkout).changed is (status == "ignored")
    assert any("changed since the host adopted it" in line for line in lines)
    assert any("evil.example" in line for line in lines)  # the diff is shown before the choice


def test_no_tty_never_adopts(checkout):
    """The detached supervisor launch / CI: warn, keep the adopted copy, resolve on a Mac later."""
    configpin.adopt(checkout)
    (checkout.repo_root / "foldyard.toml").write_text(EDITED)

    prompt, echo, lines = _prompted("a")  # would have adopted, if it were asked
    assert configpin.resolve(checkout, interactive=False, prompt=prompt, echo=echo) == "unresolved"
    assert configpin.inspect(checkout).changed
    assert configpin.effective(checkout).toml["proxy"]["passthrough"] == ["trusted.example"]
    assert any("fy config adopt" in line for line in lines)


def test_comment_and_blank_noise_never_reaches_the_diff(checkout):
    """The whole point of the filter: a rewritten comment block is not a config change, and it
    used to bury (then truncate away) the one line that was."""
    configpin.adopt(checkout)
    (checkout.repo_root / "foldyard.toml").write_text(
        "# a fresh header\n#\n\n" + EDITED.replace("\n\n", "\n\n\n  # why we do this\n")
    )
    drift = configpin.inspect(checkout)

    assert drift.changed  # the bytes differ…
    assert drift.diff().splitlines() == [  # …but only one line of CONFIG did
        "--- adopted/foldyard.toml",
        "+++ tree/foldyard.toml",
        "@@ [proxy] @@",
        '-passthrough = ["trusted.example"]',
        '+passthrough = ["trusted.example", "evil.example"]',
    ]
    assert drift.summary().startswith("1 line(s) added, 1 removed")


def test_a_comment_only_edit_is_reported_as_drift_with_nothing_to_review(checkout):
    """Filtering must never make a drift SILENT — the gate still asks, it just has no hunks."""
    configpin.adopt(checkout)
    (checkout.repo_root / "foldyard.toml").write_text(TOML + "\n# just documenting this\n")
    drift = configpin.inspect(checkout)

    assert drift.changed and drift.diff() == ""
    assert drift.summary() == "only blank/comment lines changed in foldyard.toml"

    prompt, echo, lines = _prompted("i")
    configpin.resolve(checkout, interactive=True, prompt=prompt, echo=echo)
    assert any("only blank/comment lines changed" in line for line in lines)


def test_every_real_change_is_shown_however_many_there_are(checkout):
    """No truncation and no `… N more` tail: the inline prompt is the only place some of these
    get read, and a cut-off diff can't be scrolled back."""
    configpin.adopt(checkout)
    (checkout.repo_root / "foldyard.toml").write_text(
        TOML + "\n[[inject]]\n" + "\n".join(f'host = "h{i}.example"' for i in range(400))
    )
    body = configpin.inspect(checkout).diff()

    assert body.count("\n+host = ") == 400
    assert "more diff lines" not in body
    assert body.splitlines()[2] == "+[[inject]]"


def test_changed_lines_are_located_by_their_table(checkout):
    """With no context lines the table header is the only "where" — and a changed header
    locates itself rather than being announced twice."""
    configpin.adopt(checkout)
    (checkout.repo_root / "foldyard.toml").write_text(
        TOML + '\n[[inject]]\nhost = "api.example"\n\n[claude]\nenabled = true\n'
    )
    body = configpin.inspect(checkout).diff().splitlines()

    assert body[2:] == [
        "+[[inject]]",
        '+host = "api.example"',
        "+[claude]",
        "+enabled = true",
    ]


# ── the supervisor's drift reporting ──────────────────────────────────────────────────


def _capture_supervisor_output(monkeypatch):
    logs: list[str] = []
    notes: list[tuple[str, str]] = []
    monkeypatch.setattr(supervisor, "log", logs.append)
    monkeypatch.setattr(supervisor, "_notify", lambda title, body: notes.append((title, body)))
    return logs, notes


def test_drift_is_reported_once_per_change_and_notified_on_the_edge(checkout, monkeypatch):
    """Reconciling from the pin makes a repo edit silent by construction — so the tick has to say
    it happened, without re-saying it every 2 seconds."""
    logs, notes = _capture_supervisor_output(monkeypatch)
    configpin.adopt(checkout)

    supervisor._report_config_drift("", checkout)
    assert logs == [] and notes == []  # clean: silent

    (checkout.repo_root / "foldyard.toml").write_text(EDITED)
    for _ in range(3):  # three ticks, one edit
        supervisor._report_config_drift("", checkout)
    assert len(logs) == 1
    assert "STILL RUNNING THE ADOPTED ONE" in logs[0]
    assert len(notes) == 1

    # A SECOND distinct edit gets its own log line (the sequence stays reconstructable) but no
    # second notification — a file being rewritten repeatedly is one event to an operator.
    (checkout.repo_root / "foldyard.toml").write_text(EDITED + "# again\n")
    supervisor._report_config_drift("", checkout)
    assert len(logs) == 2
    assert len(notes) == 1

    configpin.adopt(checkout)
    supervisor._report_config_drift("", checkout)
    assert "matches the adopted foldyard.toml again" in logs[-1]


def test_the_launch_gate_runs_before_the_supervisor_starts(monkeypatch, checkout):
    """`fy up`/`fy box up` reach the gate through ensure_background — and it must run BEFORE the
    "a current supervisor already holds the lock" short-circuit, or the prompt is skipped exactly
    when a supervisor is running to be affected by the change."""
    called: list[str] = []
    monkeypatch.setattr(configpin, "gate", lambda verb: called.append(verb) or "clean")
    monkeypatch.setattr(supervisor, "which", lambda _name: "/usr/bin/podman")
    monkeypatch.setattr(supervisor.devmode, "in_box", lambda: False)
    monkeypatch.setattr(supervisor, "_supervisor_running", lambda: True)
    monkeypatch.setattr(supervisor, "_holder_stale_reason", lambda: None)

    assert supervisor.ensure_background() is None  # current holder → no second supervisor
    assert called == ["fy up"]


def test_fy_host_gates_even_when_a_healthy_supervisor_already_holds_the_lock(monkeypatch, capsys):
    """`fy host` returns 0 early when a current supervisor owns the singleton — the COMMON case,
    and the one where a supervisor is actually running to be affected by a config change. So the
    gate has to run before that check, not after it."""
    called: list[str] = []
    monkeypatch.setattr(configpin, "gate", lambda verb: called.append(verb) or "clean")
    monkeypatch.setattr(supervisor.devmode, "in_box", lambda: False)
    monkeypatch.setattr(supervisor, "acquire_singleton", lambda: False)
    monkeypatch.setattr(supervisor, "_holder_stale_reason", lambda: None)

    assert supervisor.main() == 0  # deferred to the running one…
    assert called == ["fy host"]  # …but the drift was still surfaced
    assert "already running and current" in capsys.readouterr().err


def test_gate_is_a_noop_in_the_box(monkeypatch):
    monkeypatch.setattr(config, "in_box", lambda: True)
    assert configpin.gate("fy up") == "clean"


def test_gate_never_blocks_a_launch_verb(monkeypatch, capsys):
    """A broken gate loses the prompt, not the boundary — the supervisor reconciles from the pin
    either way, so a launch verb must not die here."""
    monkeypatch.setattr(config, "in_box", lambda: False)
    monkeypatch.setattr(
        devmode, "worktree_config", lambda wt: (_ for _ in ()).throw(RuntimeError("no repo"))
    )
    assert configpin.gate("fy up") == "error"
    assert "couldn't check foldyard.toml" in capsys.readouterr().out


def test_doctor_warns_on_drift_and_says_which_verbs_resolve_it(checkout, monkeypatch):
    monkeypatch.setattr(devmode, "worktree_config", lambda wt: checkout)
    monkeypatch.setattr(devmode.config, "active_worktree", lambda: "")

    status, name, detail = devmode._config_pin_check()
    assert (status, name) == ("warn", "adopted config") and "nothing adopted yet" in detail

    configpin.adopt(checkout)
    assert devmode._config_pin_check()[0] == "ok"

    (checkout.repo_root / "foldyard.toml").write_text(EDITED)
    status, _name, detail = devmode._config_pin_check()
    assert status == "warn"
    assert "fy config adopt" in detail and "fy config revert" in detail


def test_drift_report_survives_an_unreadable_state_dir(monkeypatch):
    """The tick must not die on it (the pin is still what's serving)."""
    logs, _notes = _capture_supervisor_output(monkeypatch)
    monkeypatch.setattr(
        configpin, "inspect", lambda cfg: (_ for _ in ()).throw(OSError("state dir gone"))
    )
    fake_cfg = cast("config.Config", types.SimpleNamespace(worktree=""))
    supervisor._report_config_drift("", fake_cfg)
    assert "drift check failed" in logs[0]


def test_the_diff_stays_plain_text_and_the_summary_sizes_it(checkout):
    """Color is applied at the print site (`term.paint_diff`), never baked into the diff — the
    gate echoes the same string to a log with no terminal, and these assertions compare bytes."""
    configpin.adopt(checkout)
    (checkout.repo_root / "foldyard.toml").write_text(EDITED)
    drift = configpin.inspect(checkout)

    assert "\x1b" not in drift.diff()
    # One line replaced: the `+++`/`---` headers must not be counted as an add/remove.
    assert drift.summary() == (
        "1 line(s) added, 1 removed in foldyard.toml (blank/comment lines ignored)"
    )


def test_the_prompt_leads_with_the_size_of_the_change(checkout):
    configpin.adopt(checkout)
    (checkout.repo_root / "foldyard.toml").write_text(EDITED)

    prompt, echo, lines = _prompted("i")
    configpin.resolve(checkout, interactive=True, prompt=prompt, echo=echo)
    assert any("1 line(s) added, 1 removed" in line for line in lines)


def test_renaming_the_project_cannot_unlock_the_pin(checkout):
    """The adoption is found by CHECKOUT PATH. Locating it through `config.project()` would read
    `[project].name` out of the working tree — so renaming the project in the box would find no
    adoption and fall back to the tree, which is the pin unlocked by editing the config."""
    configpin.adopt(checkout)
    (checkout.repo_root / "foldyard.toml").write_text(
        '[project]\nname = "not-acme"\n\n[proxy]\npassthrough = ["evil.example"]\n'
    )
    renamed = config.resolve(worktree="", repo=checkout.repo_root)

    assert configpin.inspect(renamed).changed  # still found, still compared
    assert configpin.effective(renamed).toml["proxy"]["passthrough"] == ["trusted.example"]


def test_adopting_a_checkout_with_no_config_is_recorded(checkout):
    """Adopting "this project declares nothing" stores no FILES, so adoption state can't be read
    off the snapshot: a `foldyard.toml` conjured afterwards would go live unadopted. Reachable for
    real — a worktree on a branch from before the file existed."""
    (checkout.repo_root / "foldyard.toml").unlink()
    empty = config.resolve(worktree="", repo=checkout.repo_root)

    configpin.adopt(empty)
    assert configpin.inspect(empty).pinned_exists  # the marker, not the (absent) bytes

    (checkout.repo_root / "foldyard.toml").write_text(EDITED)
    after = config.resolve(worktree="", repo=checkout.repo_root)
    assert configpin.effective(after).toml == {}  # the host still runs what it adopted: nothing
    assert configpin.inspect(after).changed  # …and the new file is reported as drift


def test_an_interrupted_adopt_reads_as_not_adopted(checkout):
    """The marker is written last, so a crash mid-adopt falls back to the tree and re-adopts next
    launch, rather than claiming an adoption over half-written files."""
    configpin.adopt(checkout)
    (configpin.pin_dir(checkout) / configpin.ADOPTED_MARKER).unlink()

    assert not configpin.inspect(checkout).pinned_exists
    assert configpin.effective(checkout).toml == checkout.toml


def test_an_adoption_from_the_previous_store_layout_still_counts(checkout):
    """The incident this exists for: moving where adoptions are kept orphaned the old one, and
    "no adoption" is the one state that adopts the working tree — so an operator's `ignore` at
    `fy up` was overridden by a silent adopt at `fy box up` seconds later."""
    legacy = configpin._legacy_pin_dir(checkout)
    legacy.mkdir(parents=True, exist_ok=True)
    (legacy / "foldyard.toml").write_text(TOML)  # adopted under the old layout: no marker
    (checkout.repo_root / "foldyard.toml").write_text(EDITED)

    drift = configpin.inspect(checkout)
    assert drift.pinned_exists and drift.changed  # NOT "never adopted"
    assert configpin.effective(checkout).toml["proxy"]["passthrough"] == ["trusted.example"]

    prompt, echo, _lines = _prompted("i")
    assert configpin.resolve(checkout, interactive=True, prompt=prompt, echo=echo) == "ignored"


def test_adopting_migrates_a_legacy_pin_to_the_current_location(checkout):
    legacy = configpin._legacy_pin_dir(checkout)
    legacy.mkdir(parents=True, exist_ok=True)
    (legacy / "foldyard.toml").write_text(TOML)

    configpin.adopt(checkout)
    assert (configpin.pin_dir(checkout) / configpin.ADOPTED_MARKER).is_file()
    assert not configpin.inspect(checkout).changed


def test_a_first_adoption_can_be_declined_on_a_terminal(checkout):
    """Enter is still enough, but it can't pass unseen — that's what let a declined config get
    adopted a moment later."""
    prompt, echo, lines = _prompted("i")
    assert configpin.resolve(checkout, interactive=True, prompt=prompt, echo=echo) == "ignored"

    assert not configpin.inspect(checkout).pinned_exists
    assert any("nothing adopted yet" in line for line in lines)
    assert any("lines across foldyard.toml" in line for line in lines)  # what it would adopt


def test_the_prompt_flags_config_nothing_consumes(checkout):
    """The incident this pins down: the drift was `[proxy] allow` — a key foldyard no longer
    honours (grants live in the host-side allow-store) — and the diff read exactly like a
    security change. The operator adopted it, nothing changed, and the box's egress stayed
    refused. The gate must say the key is dead AT the decision moment, not only in a doctor
    row nobody was looking at."""
    configpin.adopt(checkout)
    (checkout.repo_root / "foldyard.toml").write_text(
        TOML.replace(
            'passthrough = ["trusted.example"]',
            'passthrough = ["trusted.example"]\nallow = ["unpkg.com", "pypi.org"]',
        )
    )

    prompt, echo, lines = _prompted("i")
    assert configpin.resolve(checkout, interactive=True, prompt=prompt, echo=echo) == "ignored"

    note = "\n".join(lines)
    assert "[proxy] allow" in note and "IGNORED" in note
    assert "fy allow add" in note  # the verb that replaces it, right where the decision is made


def test_a_first_adoption_flags_config_nothing_consumes(checkout):
    """Same signal on the first-run branch — a fresh checkout can carry the stale key too."""
    (checkout.repo_root / "foldyard.toml").write_text(
        TOML.replace(
            'passthrough = ["trusted.example"]',
            'passthrough = ["trusted.example"]\nallow = ["unpkg.com"]',
        )
    )
    prompt, echo, lines = _prompted("i")
    assert configpin.resolve(checkout, interactive=True, prompt=prompt, echo=echo) == "ignored"
    note = "\n".join(lines)
    assert "[proxy] allow" in note and "IGNORED" in note


def test_a_first_adoption_without_a_terminal_does_NOT_happen(checkout):
    """The other end of the unattended-config channel: adopting with no TTY handed the host
    whatever the tree said, with the acknowledgement scrolling past in a log file. Nothing is
    adopted, and the status says so."""
    prompt, echo, lines = _prompted("i")
    assert configpin.resolve(checkout, interactive=False, prompt=prompt, echo=echo) == "unadopted"
    assert not configpin.inspect(checkout).pinned_exists  # nothing was written
    assert any("nothing adopted yet" in line for line in lines)
    assert any("fy config adopt" in line for line in lines)  # …and how to fix it


def test_the_launch_gate_refuses_an_unadopted_checkout_with_no_terminal(monkeypatch, checkout):
    """`unadopted` must BLOCK, not warn: with an empty pin, effective() falls back to the working
    tree, so waving the launch through is the host running a config nobody has read."""
    monkeypatch.setattr(configpin.config, "in_box", lambda: False)
    monkeypatch.setattr(configpin.config, "active_worktree", lambda: "")
    from foldyard import devmode

    monkeypatch.setattr(devmode, "worktree_config", lambda _wt: checkout)
    monkeypatch.setattr(configpin.sys.stdin, "isatty", lambda: False)
    with pytest.raises(SystemExit, match="never been adopted"):
        configpin.gate("fy up")
    assert not configpin.inspect(checkout).pinned_exists


# ── a worktree's first adoption, measured against main ────────────────────────────────


def test_a_worktrees_first_adoption_is_diffed_against_mains_adopted_config(checkout, worktree):
    """The comparison a first adoption actually needs. There is nothing to diff a fresh worktree
    against on its OWN axis, so the report used to be a digest and a line count — a security
    decision with no view of the content. A worktree is a branch off main, and main is normally
    adopted already, so the answerable question is what this branch would grant that main hasn't
    already been granted."""
    configpin.adopt(checkout)
    (worktree.repo_root / "foldyard.toml").write_text(EDITED)

    base = configpin.main_baseline(configpin.inspect(worktree))

    assert base is not None and not base.identical
    assert base.digest == configpin.inspect(checkout).pinned_digest()
    assert base.body.splitlines() == [
        "--- main-adopted/foldyard.toml",
        "+++ tree/foldyard.toml",
        "@@ [proxy] @@",
        '-passthrough = ["trusted.example"]',
        '+passthrough = ["trusted.example", "evil.example"]',
    ]
    assert base.summary() == "1 line(s) added, 1 removed"


def test_a_worktree_matching_main_says_it_grants_nothing_new(checkout, worktree):
    """The common case, and the whole payoff: most branches don't touch foldyard.toml, so the
    adoption collapses to one line and one keypress."""
    configpin.adopt(checkout)
    drift = configpin.inspect(worktree)
    base = configpin.main_baseline(drift)

    assert base is not None and base.identical and base.body == ""
    assert "grants nothing new" in configpin.first_adoption_headline(drift, base)


def test_a_comment_only_difference_from_main_still_grants_nothing_new(checkout, worktree):
    """Same filter as the drift diff: a branch that only rewrote the comment block declares the
    same config, and the headline has to say so rather than report an empty change."""
    configpin.adopt(checkout)
    (worktree.repo_root / "foldyard.toml").write_text("# a branch header\n\n" + TOML)
    drift = configpin.inspect(worktree)
    base = configpin.main_baseline(drift)

    assert base is not None and not base.identical and base.body == ""
    headline = configpin.first_adoption_headline(drift, base)
    assert "only blank/comment lines differ" in headline and "nothing new" in headline


def test_the_baseline_is_mains_ADOPTED_copy_never_its_working_tree(checkout, worktree):
    """The security property. Main's foldyard.toml is inside the mount like every other one, so
    diffing against it would let unreviewed config launder itself through a worktree — "no change
    vs main" while main's tree was rewritten in the box an hour ago. The baseline has to be
    something an operator approved."""
    configpin.adopt(checkout)  # what the host actually runs for main
    (checkout.repo_root / "foldyard.toml").write_text(EDITED)  # …and an in-box edit to its tree
    (worktree.repo_root / "foldyard.toml").write_text(EDITED)

    base = configpin.main_baseline(configpin.inspect(worktree))

    assert base is not None and not base.identical
    assert "+passthrough = [" in base.body  # measured against the ADOPTED copy, so it shows


def test_no_baseline_when_main_itself_was_never_adopted(checkout, worktree):
    """Nothing reviewed to compare against — and the prompt says that rather than staying silent,
    because "no diff shown" would otherwise read as "nothing changed"."""
    drift = configpin.inspect(worktree)

    assert configpin.main_baseline(drift) is None
    assert "no reviewed config" in configpin.first_adoption_headline(drift, None)


def test_main_has_no_baseline_of_its_own(checkout):
    """Main IS the baseline; nothing sits above it. So its own first adoption falls back to the
    other reviewable thing — the config itself, with the comments taken out."""
    drift = configpin.inspect(checkout)
    assert configpin.main_baseline(drift) is None
    assert "comments" in configpin.first_adoption_headline(drift, None)


# ── with no baseline, the review is the config itself ─────────────────────────────────


def test_a_first_adoption_with_no_baseline_lists_the_config_commentless(checkout):
    """A brand-new project's first `fy up`: nothing is adopted anywhere, so there is nothing to
    diff against — but "190 lines across foldyard.toml" is not a review either. Show what the file
    DECLARES, which is the same filtered view every other surface here prints."""
    (checkout.repo_root / "foldyard.toml").write_text(
        "# The project's proxy posture.\n#\n# Long rationale nobody needs at this moment.\n"
        "\n" + TOML + '\n# why we inject here\n[[inject]]\nhost = "api.vendor.example"\n'
    )
    prompt, echo, lines = _prompted("i")
    configpin.resolve(checkout, interactive=True, prompt=prompt, echo=echo)
    note = "\n".join(lines)

    assert '+passthrough = ["trusted.example"]' in note
    assert '+host = "api.vendor.example"' in note
    # Every line is an addition here, so each table header locates its own block as a `+` line —
    # no `@@` locators, and the listing reads like the file itself.
    assert "+[[inject]]" in note and "@@" not in note
    # The comments are the reason a 190-line file was unreadable at a prompt in the first place.
    assert "Long rationale" not in note and "why we inject here" not in note


def test_the_listing_says_when_a_checkout_declares_nothing(checkout):
    """Files that are all comments, and no files at all, are the same answer to an operator: this
    grants the host nothing. An empty diff must not read as "shown nothing, decide anyway"."""
    (checkout.repo_root / "foldyard.toml").write_text("# just a header\n\n")
    drift = configpin.inspect(checkout)

    assert drift.diff() == ""
    assert "declares nothing" in configpin.first_adoption_headline(drift, None)


def test_a_worktree_with_a_baseline_does_not_also_dump_the_whole_config(checkout, worktree):
    """The listing is the FALLBACK. Where a reviewed baseline exists, the diff against it is both
    smaller and more informative — printing both would bury it."""
    configpin.adopt(checkout)
    (worktree.repo_root / "foldyard.toml").write_text(EDITED + "\n[claude]\nenabled = true\n")

    prompt, echo, lines = _prompted("i")
    configpin.resolve(worktree, interactive=True, prompt=prompt, echo=echo)
    note = "\n".join(lines)

    assert "+[claude]" in note  # what this branch adds…
    assert '+name = "acme"' not in note  # …not what it shares with main


def test_a_worktree_first_adoption_prompt_leads_with_the_diff_from_main(checkout, worktree):
    configpin.adopt(checkout)
    (worktree.repo_root / "foldyard.toml").write_text(EDITED)

    prompt, echo, lines = _prompted("i")
    assert configpin.resolve(worktree, interactive=True, prompt=prompt, echo=echo) == "ignored"

    note = "\n".join(lines)
    assert "nothing adopted yet" in note
    assert "the config the host runs for main" in note
    assert '+passthrough = ["trusted.example", "evil.example"]' in note


def test_a_failing_baseline_never_breaks_the_first_adoption_prompt(checkout, worktree, monkeypatch):
    """It decorates a decision; it must not be able to block one."""
    monkeypatch.setattr(
        devmode, "main_repo", lambda: (_ for _ in ()).throw(RuntimeError("no git here"))
    )
    prompt, echo, lines = _prompted("")
    assert configpin.resolve(worktree, interactive=True, prompt=prompt, echo=echo) == "pinned"
    assert any("nothing adopted yet" in line for line in lines)


def test_fy_config_diff_reviews_an_unadopted_worktree_against_main(checkout, worktree, monkeypatch):
    """`fy config diff` used to be a dead end on exactly the checkout that most needs it: with
    nothing pinned it printed "nothing adopted yet" and exited, while the adopt prompt was
    pointing people AT it. Now the unadopted branch shows the one diff that exists."""
    from typer.testing import CliRunner

    from foldyard import cli

    configpin.adopt(checkout)
    (worktree.repo_root / "foldyard.toml").write_text(EDITED)
    monkeypatch.setattr(cli, "_active_config", lambda: worktree)

    result = CliRunner().invoke(cli.app, ["config", "diff"])

    assert result.exit_code == 1  # still unadopted — the gate will still ask
    assert "nothing adopted yet" in result.output
    assert "the config the host runs for main" in result.output
    assert '+passthrough = ["trusted.example", "evil.example"]' in result.output


def test_fy_config_diff_lists_an_unadopted_MAIN_instead_of_refusing(checkout, monkeypatch):
    """No baseline exists above main, so the reviewable thing is the config itself."""
    from typer.testing import CliRunner

    from foldyard import cli

    monkeypatch.setattr(cli, "_active_config", lambda: checkout)
    result = CliRunner().invoke(cli.app, ["config", "diff"])

    assert result.exit_code == 1  # still unadopted
    assert "nothing adopted yet" in result.output
    assert '+passthrough = ["trusted.example"]' in result.output
    assert "fy config widenings" in result.output  # …and where to learn what it MEANS


def test_the_baseline_is_only_for_a_FIRST_adoption(checkout, worktree):
    """Once a worktree has its own pin, adopted→tree is the diff that matters and a second one is
    noise — the drift branch keeps reporting against what THIS checkout runs."""
    configpin.adopt(checkout)
    configpin.adopt(worktree)
    (worktree.repo_root / "foldyard.toml").write_text(EDITED)

    prompt, echo, lines = _prompted("i")
    assert configpin.resolve(worktree, interactive=True, prompt=prompt, echo=echo) == "ignored"

    note = "\n".join(lines)
    assert "changed since the host adopted it" in note
    assert "for main" not in note


def test_adopt_switches_generations_whole(checkout):
    """A generation is written complete before the marker names it, and the previous one is kept
    (a reader that resolved the pointer a moment earlier is still inside it)."""
    local = checkout.repo_root / "foldyard.local.toml"
    local.write_text('[proxy]\npassthrough = ["local.example"]\n')
    configpin.adopt(checkout)
    first = _current(checkout)
    local.write_text('[proxy]\npassthrough = ["second.example"]\n')
    configpin.adopt(checkout)
    second = _current(checkout)

    assert first != second and first.is_dir()  # the superseded generation survives the switch
    assert second.parent.name == configpin.GENERATIONS
    # Each generation is self-contained — no file is shared with, or left behind in, another.
    assert (first / "foldyard.local.toml").read_bytes() != (
        second / "foldyard.local.toml"
    ).read_bytes()
    assert configpin.effective(checkout).toml["proxy"]["passthrough"] == ["second.example"]


def test_a_marker_pointing_at_a_missing_generation_reads_as_never_adopted(checkout):
    """Fail closed: a lost generation must not fall back to whatever else is in the pin dir —
    that is exactly the mixed/partial read generations exist to rule out."""
    configpin.adopt(checkout)
    shutil.rmtree(_current(checkout))
    assert configpin.current_dir(checkout) is None
    assert not configpin.inspect(checkout).pinned_exists


# ── recommended hosts ride the same launch path, AFTER the gate ───────────────────────


def test_recommended_hosts_are_offered_right_after_the_gate(monkeypatch):
    """The bootstrap story: a first `fy box up` adopts the config (the gate), and is THEN
    offered its `[proxy] recommend` hosts — so a locked-down repo comes up with consented
    grants instead of a wall of refusals. Order matters: the offer reads the ADOPTED copy."""
    calls: list[str] = []
    monkeypatch.setattr(configpin, "gate", lambda verb: calls.append("gate") or "clean")
    monkeypatch.setattr(supervisor, "_offer_recommended", lambda: calls.append("offer"))
    monkeypatch.setattr(supervisor, "which", lambda _name: "/usr/bin/podman")
    monkeypatch.setattr(supervisor.devmode, "in_box", lambda: False)
    monkeypatch.setattr(supervisor, "_supervisor_running", lambda: True)
    monkeypatch.setattr(supervisor, "_holder_stale_reason", lambda: None)

    assert supervisor.ensure_background() is None
    assert calls == ["gate", "offer"]


def test_offer_reads_the_adopted_copy_not_the_tree(monkeypatch, checkout, capsys):
    """An in-box edit that queues a recommendation must not reach the operator until the file
    carrying it survives the adoption gate — recommend rides the pin like everything else."""
    configpin.adopt(checkout)  # the adopted copy declares no recommendations
    (checkout.repo_root / "foldyard.toml").write_text(
        TOML.replace(
            'passthrough = ["trusted.example"]',
            'passthrough = ["trusted.example"]\nrecommend = ["conjured.example.com"]',
        )
    )
    monkeypatch.setattr(
        supervisor.devmode, "worktree_config", lambda wt: configpin.effective(checkout)
    )

    supervisor._offer_recommended()  # no TTY under pytest → the listing path, no prompt
    assert "conjured.example.com" not in capsys.readouterr().err

    configpin.adopt(checkout)  # the operator adopts the edit — NOW it may be offered
    supervisor._offer_recommended()
    assert "conjured.example.com" in capsys.readouterr().err


# ── PR #215 review: the diff filter vs TOML multiline strings, and adopt-after-review ──


@pytest.mark.parametrize("quote", ['"""', "'''"])
def test_a_change_inside_a_multiline_string_reaches_the_diff(checkout, quote):
    """The noise filter dropped any line that LOOKED like a comment — but inside a TOML multiline
    string `#changed` is data (a `[claude] prompt`, say), so a change to it read as "changed,
    nothing to show": ``Drift.changed`` true, the diff body empty, and the operator told that
    nothing the host reads had moved. Both string forms, since only one of them takes escapes."""

    def toml(marker: str) -> str:
        return TOML + f"\n[claude]\nprompt = {quote}\nBe brief.\n{marker}\n{quote}\n"

    (checkout.repo_root / "foldyard.toml").write_text(toml("#before"))
    configpin.adopt(checkout)
    (checkout.repo_root / "foldyard.toml").write_text(toml("#after"))

    drift = configpin.inspect(checkout)
    assert drift.changed
    lines = drift.diff().splitlines()
    assert "-#before" in lines and "+#after" in lines, lines
    assert "@@ [claude] @@" in lines


def test_a_table_header_inside_a_multiline_string_is_not_a_locator(checkout):
    """The section tracker has the same blind spot the other way round: `[x]` inside a string
    must not relabel the lines after it."""
    (checkout.repo_root / "foldyard.toml").write_text(
        TOML + '\n[claude]\nprompt = """\n[proxy]\n"""\n'
    )
    configpin.adopt(checkout)
    (checkout.repo_root / "foldyard.toml").write_text(
        TOML + '\n[claude]\nprompt = """\n[proxy]\n"""\nkeyless = true\n'
    )
    lines = configpin.inspect(checkout).diff().splitlines()
    assert "@@ [claude] @@" in lines and "+keyless = true" in lines, lines
    assert "@@ [proxy] @@" not in lines


def test_adopt_refuses_bytes_written_after_they_were_reviewed(checkout):
    """The review-then-adopt race: the prompt and the TUI modal show one tree, and ``adopt()``
    re-reads the tree. An edit in between — the box writing the checkout while the operator
    reads — would be adopted unseen, which is the channel this module exists to close."""
    reviewed = configpin.inspect(checkout).tree_digest()
    (checkout.repo_root / "foldyard.toml").write_text(EDITED)

    with pytest.raises(configpin.ReviewStale, match="changed"):
        configpin.adopt(checkout, reviewed=reviewed)
    assert not configpin.inspect(checkout).pinned_exists

    configpin.adopt(checkout, reviewed=configpin.inspect(checkout).tree_digest())  # a fresh review
    assert configpin.inspect(checkout).pinned_exists


def test_the_prompt_never_adopts_what_changed_while_it_was_open(checkout):
    configpin.adopt(checkout)
    (checkout.repo_root / "foldyard.toml").write_text(EDITED)
    lines: list[str] = []

    def prompt(_q: str) -> str:  # the tree moves while the operator is reading the diff
        (checkout.repo_root / "foldyard.toml").write_text(EDITED.replace("evil", "worse"))
        return "a"

    status = configpin.resolve(checkout, interactive=True, prompt=prompt, echo=lines.append)

    assert status == "unresolved"
    assert configpin.effective(checkout).toml["proxy"]["passthrough"] == ["trusted.example"]
    assert any("changed again" in line for line in lines), lines


def test_a_first_adoption_never_adopts_what_changed_while_it_was_open(checkout):
    lines: list[str] = []

    def prompt(_q: str) -> str:
        (checkout.repo_root / "foldyard.toml").write_text(EDITED)
        return "a"

    status = configpin.resolve(checkout, interactive=True, prompt=prompt, echo=lines.append)

    assert status == "unadopted"
    assert not configpin.inspect(checkout).pinned_exists
    assert any("changed again" in line for line in lines), lines


def test_an_escaped_delimiter_does_not_end_a_multiline_string_early(checkout):
    r"""The escape half of the multiline fix. Inside a multiline BASIC string a backslash-escaped
    quote followed by two more (`\` + three quotes) is CONTENT, not a terminator — so a prompt that
    quotes a delimiter leaves the string open, and the `#changed` line under it is still data the
    host reads. Scanning with a plain find() closes the string on that sequence instead, which
    hands every line below it back to the noise filter and reports "changed, nothing to show" —
    the exact silencing the multiline handling exists to prevent, reached from inside a string the
    diff never left."""

    def toml(marker: str) -> str:
        return TOML + f'\n[claude]\nprompt = """\nQuote a \\""" delimiter.\n{marker}\n"""\n'

    (checkout.repo_root / "foldyard.toml").write_text(toml("#before"))
    configpin.adopt(checkout)
    (checkout.repo_root / "foldyard.toml").write_text(toml("#after"))

    drift = configpin.inspect(checkout)
    assert drift.changed
    lines = drift.diff().splitlines()
    assert "-#before" in lines and "+#after" in lines, lines


def test_a_backslash_before_the_escape_still_closes_the_string(checkout):
    r"""The other side of the same rule, so the fix can't degrade into "a backslash anywhere means
    never close": `\\` is an escaped BACKSLASH, so the delimiter after it DOES terminate — and the
    `# comment` on the next line is noise again, not data."""
    body = '\n[claude]\nprompt = """\nends with a backslash \\\\"""\n# a real comment\n'
    (checkout.repo_root / "foldyard.toml").write_text(TOML + body)
    configpin.adopt(checkout)
    (checkout.repo_root / "foldyard.toml").write_text(TOML + body + "keyless = true\n")

    lines = configpin.inspect(checkout).diff().splitlines()
    assert "+keyless = true" in lines, lines
    assert not any("a real comment" in line for line in lines), lines


def test_adopt_refuses_a_baseline_that_moved_while_it_was_reviewed(checkout, worktree):
    """The review-then-adopt race on the OTHER side. A worktree's first adoption is reviewed as a
    diff against main's ADOPTED copy — "grants nothing new" is a claim about main, and main can be
    adopted again from a second terminal while the prompt or the TUI modal sits open. The tree
    digest cannot see that move, so without re-reading the baseline the operator's answer would
    apply to a comparison that no longer exists."""
    configpin.adopt(checkout)
    reviewed = configpin.inspect(worktree)
    base = configpin.main_baseline(reviewed)
    assert base is not None

    (checkout.repo_root / "foldyard.toml").write_text(EDITED)  # main adopts something else
    configpin.adopt(checkout)

    with pytest.raises(configpin.ReviewStale, match="main changed"):
        configpin.adopt(worktree, reviewed=reviewed.tree_digest(), baseline=base.digest)
    assert not configpin.inspect(worktree).pinned_exists

    fresh = configpin.main_baseline(configpin.inspect(worktree))
    assert fresh is not None
    configpin.adopt(worktree, reviewed=reviewed.tree_digest(), baseline=fresh.digest)
    assert configpin.inspect(worktree).pinned_exists


def test_the_first_adoption_prompt_rechecks_the_baseline_it_showed(checkout, worktree):
    """The prompt threads both sides through, so an operator who read "grants nothing new" can't
    adopt against a main that has since moved on."""
    configpin.adopt(checkout)
    lines: list[str] = []

    def prompt(_q: str) -> str:  # main is re-adopted while the operator reads the comparison
        (checkout.repo_root / "foldyard.toml").write_text(EDITED)
        configpin.adopt(checkout)
        return "a"

    status = configpin.resolve(worktree, interactive=True, prompt=prompt, echo=lines.append)

    assert status == "unadopted"
    assert not configpin.inspect(worktree).pinned_exists
    assert any("main changed" in line for line in lines), lines


def test_a_first_adoption_with_no_baseline_is_unaffected_by_the_recheck(checkout):
    """Main's own first adoption has no baseline to move — it was reviewed as the whole config, not
    as a diff against one — so the recheck must stay out of its way."""
    _prompt, echo, lines = _prompted("a")

    status = configpin.resolve(checkout, interactive=True, prompt=_prompt, echo=echo)

    assert status == "pinned"
    assert configpin.inspect(checkout).pinned_exists
    assert not any("main changed" in line for line in lines), lines
