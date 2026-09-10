"""devmode.py — the posture substrate: TTL parsing, state round-trips + expiry, env
derivation, and the per-mode daemon specs. State I/O is isolated to a tmp dir via the
`isolated_state` fixture (Mac side); no real ~/.foldyard, GCP, or ports are touched."""

from __future__ import annotations

import json
import subprocess
from datetime import timedelta

import pytest

from foldyard import devmode

# The substrate tests assume the Tangible-shaped axes (gcp/github/capture/dump); bind a full
# resolved config so the live registry provides them (per-consumer-registry-plan.md test strategy).
pytestmark = pytest.mark.usefixtures("full_config_bound")

# ── parse_ttl ────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("text,secs", [("90s", 90), ("30m", 1800), ("2h", 7200), ("3600", 3600)])
def test_parse_ttl_units(text, secs):
    assert devmode.parse_ttl(text) == secs


def test_parse_ttl_clamps_to_max():
    assert devmode.parse_ttl("100h") == devmode.MAX_TTL


@pytest.mark.parametrize("bad", ["0", "-5", "0s"])
def test_parse_ttl_rejects_nonpositive(bad):
    with pytest.raises(ValueError):
        devmode.parse_ttl(bad)


# ── set_mode / read round trips ───────────────────────────────────────────────────────


def test_set_mode_round_trip(isolated_state):
    devmode.set_mode({"gcp": "logs"})
    state = devmode.read()
    assert state["mode"] == {
        "gcp": "logs",
        "storage": "local",
        "github": "off",
        "capture": "off",
        "auth0": "sim",
        "llm": "off",
    }
    assert isolated_state["auth"].exists()


def test_set_mode_writes_mirror_when_box_up(isolated_state, monkeypatch):
    monkeypatch.setattr(devmode, "up_worktrees", lambda: [""])
    devmode.set_mode({"github": "app"})
    data = json.loads(isolated_state["mirror"].read_text())
    assert data["github"] == "app" and "written" in data


def test_set_mode_refreshes_an_existing_mirror_even_with_box_down(isolated_state, monkeypatch):
    monkeypatch.setattr(devmode, "up_worktrees", lambda: [])
    isolated_state["mirror"].write_text("{}\n")
    devmode.set_mode({"github": "app"})
    assert json.loads(isolated_state["mirror"].read_text())["github"] == "app"


def test_set_mode_does_not_create_a_mirror_when_box_down(isolated_state, monkeypatch):
    # `fy mode` against a fully-down project must not re-dirty a clean checkout with
    # .dev-mode.json — the supervisor seeds the mirror within a tick of a box coming up.
    monkeypatch.setattr(devmode, "up_worktrees", lambda: [])
    devmode.set_mode({"github": "app"})
    assert not isolated_state["mirror"].exists()


def test_read_defaults_when_files_missing(isolated_state):
    # Each axis rests at its OWN default rung (rungs[0]) — not a blanket "off": storage rests at
    # "local", auth0 at "sim" (the axis-defaults semantics the storage/auth0/llm rework added).
    assert devmode.read()["mode"] == {
        "gcp": "off",
        "storage": "local",
        "github": "off",
        "capture": "off",
        "auth0": "sim",
        "llm": "off",
    }


def test_set_mode_user_records_expiry(isolated_state):
    devmode.set_mode({"gcp": "user"}, ttl=60)
    assert "gcp" in devmode.read(apply_expiry=False)["expires"]


def test_user_mode_lapses_to_off(isolated_state):
    devmode.set_mode({"gcp": "user"}, ttl=60)
    raw = json.loads(isolated_state["auth"].read_text())
    raw["expires"]["gcp"] = devmode._iso(devmode.now() - timedelta(hours=1))
    isolated_state["auth"].write_text(json.dumps(raw))
    assert devmode.read()["mode"]["gcp"] == "off"  # apply_expiry default


# ── capability results on the dashboards (consolidation proposal B) ─────────────────────


def test_show_renders_a_degraded_axis_from_the_host_state_file(isolated_state, monkeypatch, capsys):
    devmode.set_mode({"gcp": "sa", "llm": "record"})
    caps = isolated_state["dir"] / "capabilities.json"
    monkeypatch.setenv("FOLDYARD_CAPABILITIES_FILE", str(caps))
    caps.write_text(
        json.dumps(
            {
                "": {
                    "gcp": {
                        "ok": False,
                        "detail": "impersonation failing — PAM grant lapsed? just gcp-elevate",
                        "checked": devmode._iso(devmode.now()),
                    }
                }
            }
        )
    )
    devmode.show()
    out = capsys.readouterr().out
    assert "DEGRADED" in out and "gcp-elevate" in out


def test_show_renders_degraded_in_the_box_from_the_mirror(isolated_state, monkeypatch, capsys):
    devmode.set_mode({"gcp": "sa"})
    state = devmode.read()
    devmode.write_mirror(
        state["mode"],
        state["expires"],
        {},
        capabilities={"gcp": {"ok": False, "detail": "fake lapse", "checked": "now"}},
    )
    monkeypatch.setattr(devmode, "in_box", lambda: True)
    devmode.show()
    assert "DEGRADED — fake lapse" in capsys.readouterr().out


def test_degraded_capabilities_lists_only_active_failing_axes(isolated_state, monkeypatch):
    # The packaged form of the dashboard's DEGRADED claim (consumed by `fy up`): only an axis
    # that is BOTH off its default rung AND probed-and-failing makes the list.
    devmode.set_mode({"gcp": "sa", "llm": "record"})
    caps = isolated_state["dir"] / "capabilities.json"
    monkeypatch.setenv("FOLDYARD_CAPABILITIES_FILE", str(caps))
    caps.write_text(
        json.dumps(
            {
                "": {
                    "gcp": {"ok": False, "detail": "PAM lapsed — just gcp-elevate", "checked": "t"},
                    "llm": {"ok": True, "detail": "chain ok", "checked": "t"},
                }
            }
        )
    )
    assert devmode.degraded_capabilities() == [("gcp", "PAM lapsed — just gcp-elevate")]
    # Back at the default rung, a stale failing claim on file is no longer a warning.
    devmode.set_mode({"gcp": "off", "llm": "off"})
    assert devmode.degraded_capabilities() == []


def test_degraded_capabilities_in_the_box_reads_the_mirror(isolated_state, monkeypatch):
    devmode.set_mode({"gcp": "sa"})
    state = devmode.read()
    devmode.write_mirror(
        state["mode"],
        state["expires"],
        {},
        capabilities={"gcp": {"ok": False, "detail": "fake lapse", "checked": "now"}},
    )
    monkeypatch.setattr(devmode, "in_box", lambda: True)
    assert devmode.degraded_capabilities() == [("gcp", "fake lapse")]


def test_mirror_carries_capabilities_only_when_given(isolated_state):
    devmode.write_mirror({"gcp": "sa"}, {}, {}, capabilities={"gcp": {"ok": True}})
    assert json.loads(isolated_state["mirror"].read_text())["capabilities"] == {"gcp": {"ok": True}}
    devmode.write_mirror({"gcp": "sa"}, {}, {})  # `fy mode` has no probe results — no claim
    assert "capabilities" not in json.loads(isolated_state["mirror"].read_text())


# ── settle_incoherent — the expiry cascade (consolidation proposal E) ───────────────────


def test_settle_incoherent_downgrades_stranded_dependents(isolated_state):
    # llm=live with the identity gone is a mode_issues ERROR (it can only fail) — settle flips
    # it (and storage=staging, the other gcp=sa dependent) down to their defaults.
    mode = devmode.read()["mode"]
    assert devmode.settle_incoherent({**mode, "gcp": "off", "llm": "live"}) == {"llm": "off"}
    assert devmode.settle_incoherent(
        {**mode, "gcp": "off", "llm": "record", "storage": "staging"}
    ) == {"llm": "off", "storage": "local"}


def test_settle_incoherent_noop_when_coherent(isolated_state):
    mode = devmode.read()["mode"]
    assert devmode.settle_incoherent(mode) == {}
    assert devmode.settle_incoherent({**mode, "gcp": "sa", "llm": "live"}) == {}


def test_settle_never_escalates(isolated_state):
    # The cascade only ever flips axes DOWN to their default — an incoherent mode whose fix
    # would be raising another axis (gcp=off + llm=live "fixed" by gcp=sa) must settle the
    # dependent down instead.
    mode = devmode.read()["mode"]
    flips = devmode.settle_incoherent({**mode, "gcp": "off", "llm": "live"})
    defaults = devmode.axis_defaults()
    assert all(rung == defaults[axis] for axis, rung in flips.items())


# ── the test clock (fy clock — TTL machinery without waiting) ───────────────────────────


def test_clock_offset_env_wins(monkeypatch):
    monkeypatch.setenv("FOLDYARD_CLOCK_OFFSET", "7200")
    assert devmode.clock_offset() == 7200.0


def test_clock_offset_reads_state_file(isolated_state, monkeypatch):
    monkeypatch.delenv("FOLDYARD_CLOCK_OFFSET", raising=False)
    (isolated_state["dir"] / "clock-offset").write_text("3600\n")
    assert devmode.clock_offset() == 3600.0
    skewed = devmode.now()
    monkeypatch.setenv("FOLDYARD_CLOCK_OFFSET", "0")  # back to real time
    assert (skewed - devmode.now()).total_seconds() > 3500  # the file skewed now() ~1h forward


def test_clock_ff_accumulates_and_reset_clears(isolated_state, monkeypatch, capsys):
    monkeypatch.delenv("FOLDYARD_CLOCK_OFFSET", raising=False)
    offset_file = isolated_state["dir"] / "clock-offset"
    assert devmode.clock_cli(["ff", "30m"]) == 0
    assert devmode.clock_cli(["ff", "30m"]) == 0
    assert offset_file.read_text().strip() == "3600"
    assert devmode.clock_cli(["reset"]) == 0
    assert not offset_file.exists()


def test_clock_fast_forward_lapses_a_ttl(isolated_state, monkeypatch):
    # The point of the rig: an emergency rung + `fy clock ff` = observable expiry, no waiting,
    # no real credential. (gcp=user carries the default 1h TTL.)
    monkeypatch.delenv("FOLDYARD_CLOCK_OFFSET", raising=False)
    devmode.set_mode({"gcp": "user"}, ttl=3600)
    assert devmode.read()["mode"]["gcp"] == "user"
    devmode.clock_cli(["ff", "2h"])
    assert devmode.read()["mode"]["gcp"] == "off"  # lapsed TTL reads as the default rung


def test_clock_write_is_host_only(isolated_state, monkeypatch):
    monkeypatch.setattr(devmode, "in_box", lambda: True)
    with pytest.raises(SystemExit, match="host-only"):
        devmode.clock_cli(["ff", "1h"])


@pytest.mark.parametrize("update", [{"gcp": "bogus"}, {"nope": "off"}])
def test_set_mode_validates(isolated_state, update):
    with pytest.raises(SystemExit):
        devmode.set_mode(update)


def test_set_mode_refuses_incoherent_combos(isolated_state):
    # llm=record consumes the identity only gcp=sa grants — refused with the full fix in the
    # message; the same updates applied ATOMICALLY with the identity are accepted.
    with pytest.raises(SystemExit, match="gcp=sa llm=record"):
        devmode.set_mode({"llm": "record"})
    state = devmode.set_mode({"gcp": "sa", "llm": "record"})
    assert state["mode"]["llm"] == "record"
    # De-escalation is never trapped: dropping gcp alone would strand llm=record (refused), but
    # the atomic pair goes through.
    with pytest.raises(SystemExit):
        devmode.set_mode({"gcp": "off"})
    assert devmode.set_mode({"gcp": "off", "llm": "off"})["mode"]["gcp"] == "off"


def test_set_mode_force_applies_incoherent_combo(isolated_state, capsys):
    # force (the supervisor's TTL-expiry path) downgrades the refusal to a printed warning —
    # an expiry de-escalation must never crash the supervisor loop.
    devmode.set_mode({"gcp": "sa", "llm": "record"})
    devmode.set_mode({"gcp": "off"}, force=True)
    assert devmode.read()["mode"] == {**devmode.read()["mode"], "gcp": "off", "llm": "record"}
    assert "llm=record" in capsys.readouterr().err


def test_set_mode_warns_but_applies(isolated_state, capsys):
    # A "warn" issue (mixed data planes) prints to stderr but the mode still applies.
    state = devmode.set_mode({"gcp": "sa", "storage": "staging", "auth0": "sim"})
    assert state["mode"]["storage"] == "staging"
    assert "mixes" in capsys.readouterr().err


def test_set_mode_refused_in_box(isolated_state, monkeypatch):
    monkeypatch.setattr(devmode, "in_box", lambda: True)
    with pytest.raises(SystemExit):
        devmode.set_mode({"gcp": "logs"})


def test_box_reads_mirror_not_auth(isolated_state, monkeypatch):
    devmode.write_mirror({"gcp": "logs", "github": "off"}, {}, {})
    monkeypatch.setattr(devmode, "in_box", lambda: True)
    assert devmode.read()["mode"]["gcp"] == "logs"


# ── per-worktree reconcile set (the ONE supervisor serves N worktrees) ──────────────────


def test_active_worktrees_from_engine_devboxes(monkeypatch):
    from foldyard import config

    monkeypatch.setattr(config, "project_prefix", lambda: "proj")
    monkeypatch.setattr(devmode, "worktree_keys", lambda: ["", "feat", "idle"])

    # The engine reports main's + feat's devbox up, but not idle's → only those two are reconciled.
    class _R:
        returncode = 0
        stdout = "proj-devbox\nproj-feat-devbox\nproj-postgres\n"

    monkeypatch.setattr(devmode.subprocess, "run", lambda *a, **k: _R())
    assert devmode.up_worktrees() == ["", "feat"]
    assert devmode.active_worktrees() == ["", "feat"]


def test_engine_probes_carry_the_backend_socket_env(monkeypatch):
    # The TUI/state probes (`workspaces`, `up_worktrees`, `_box_env_hint`, reconcile's scopes) shell
    # the engine CLI directly. Bare, they only worked on the podman backend BY ACCIDENT — `podman
    # machine init` registers itself as podman's default connection, lima does not — so with
    # backend = "lima" a bare `podman ps` saw nothing and the TUI reported "□ no devbox" for a box
    # that was up the whole time. Every probe must carry the backend-resolved socket env.
    from foldyard import config, machine

    monkeypatch.setattr(config, "project_prefix", lambda: "proj")
    monkeypatch.setattr(devmode, "worktree_keys", lambda: [""])
    monkeypatch.delenv("DOCKER_HOST", raising=False)
    monkeypatch.setattr(machine, "socket", lambda: "unix:///lima/sock/podman.sock")

    seen: dict = {}

    class _R:
        returncode = 0
        stdout = "proj-devbox\n"

    def run(cmd, **kw):
        seen["env"] = kw.get("env")
        return _R()

    monkeypatch.setattr(devmode.subprocess, "run", run)
    assert devmode.up_worktrees() == [""]
    assert seen["env"]["CONTAINER_HOST"] == "unix:///lima/sock/podman.sock"
    assert seen["env"]["DOCKER_HOST"] == "unix:///lima/sock/podman.sock"


def test_engine_probe_env_preset_docker_host_wins(monkeypatch):
    # In-box (or an operator override) DOCKER_HOST is already the right socket — never clobber it,
    # and never touch the backend (in-box there is no VM to ask).
    from foldyard import machine

    monkeypatch.setenv("DOCKER_HOST", "unix:///preset.sock")
    called: list[int] = []
    monkeypatch.setattr(machine, "socket", lambda: called.append(1) or "unix:///other.sock")
    env = devmode._engine_env()
    assert env["DOCKER_HOST"] == "unix:///preset.sock" and not called


def test_engine_probe_env_degrades_to_ambient_without_a_machine(monkeypatch):
    # No machine yet (socket() raises) → the probe runs with the ambient env and fails exactly as
    # before, surfacing as "engine unreachable" rather than a crash.
    from foldyard import machine

    monkeypatch.delenv("DOCKER_HOST", raising=False)

    def boom():
        raise RuntimeError("no machine")

    monkeypatch.setattr(machine, "socket", boom)
    env = devmode._engine_env()
    assert "DOCKER_HOST" not in env


def test_up_worktrees_sanitizes_slashes_to_match_stack_podman_project(monkeypatch):
    # stack._context sanitizes '/' -> '-' when computing PODMAN_PROJECT (a branch-derived
    # worktree name like "feat/x" can't appear in a podman project/container name), so the
    # devbox container is actually named "proj-feat-x-devbox". up_worktrees must compute the
    # same sanitized name or it'll never match and silently report the worktree as down.
    from foldyard import config

    monkeypatch.setattr(config, "project_prefix", lambda: "proj")
    monkeypatch.setattr(devmode, "worktree_keys", lambda: ["", "feat/x"])

    class _R:
        returncode = 0
        stdout = "proj-devbox\nproj-feat-x-devbox\n"

    monkeypatch.setattr(devmode.subprocess, "run", lambda *a, **k: _R())
    assert devmode.up_worktrees() == ["", "feat/x"]


def test_active_worktrees_falls_back_to_main_when_engine_unreachable(monkeypatch):
    monkeypatch.setattr(devmode, "worktree_keys", lambda: ["", "feat"])

    def _boom(*a, **k):
        raise OSError("no engine")

    monkeypatch.setattr(devmode.subprocess, "run", _boom)
    # A deleted/stopped machine has NO up boxes (nothing may write repo mirrors)…
    assert devmode.up_worktrees() == []
    # …but the daemon-serving set still falls back to main's proxy.
    assert devmode.active_worktrees() == [""]


def test_no_up_boxes_still_serves_main_daemons_but_reports_none_up(monkeypatch):
    from foldyard import config

    monkeypatch.setattr(config, "project_prefix", lambda: "proj")
    monkeypatch.setattr(devmode, "worktree_keys", lambda: ["", "feat"])

    class _R:
        returncode = 0
        stdout = "proj-postgres\n"  # engine reachable, but no devbox container up

    monkeypatch.setattr(devmode.subprocess, "run", lambda *a, **k: _R())
    assert devmode.up_worktrees() == []
    assert devmode.active_worktrees() == [""]


def _worktree_layout(tmp_path):
    """A primary checkout + one sibling worktree on disk, as `git worktree add` leaves them."""
    main = tmp_path / "repo"
    main.mkdir()
    (main / "foldyard.toml").write_text('[project]\nname = "p"\n')
    feat = tmp_path / "repo-worktrees" / "feat"
    feat.mkdir(parents=True)
    (feat / "foldyard.toml").write_text('[project]\nname = "p"\n')
    (feat / ".git").write_text("gitdir: ../../repo/.git/worktrees/feat\n")
    return main, feat


def test_workspaces_lists_every_checkout_from_inside_a_worktree(monkeypatch, tmp_path):
    # Run from a worktree, `config.repo_root()` stops at THAT checkout's foldyard.toml — so
    # anchoring the workspace list on it resolved worktrees_root to a `<worktree>-worktrees` dir
    # that doesn't exist, and the TUI showed a single card labelled "main" whose path was the
    # worktree: no sibling worktree could be seen or acted on from any worktree. The set of
    # workspaces is a property of the REPO, so it anchors on main_repo() and is identical wherever
    # fy runs.
    from pathlib import Path

    from foldyard import config

    main, feat = _worktree_layout(tmp_path)
    monkeypatch.setattr(config, "repo_root", lambda: feat)  # standing in the worktree
    monkeypatch.setattr(devmode, "main_repo", lambda: main)
    # The real default, kept a function of its base so the test still pins WHICH base is passed.
    monkeypatch.setattr(config, "worktrees_root", lambda base: Path(f"{base}-worktrees"))
    monkeypatch.setattr(config, "project_prefix", lambda: "proj")

    class _R:
        returncode = 0
        stdout = ""

    monkeypatch.setattr(devmode.subprocess, "run", lambda *a, **k: _R())
    spaces = devmode.workspaces()
    assert [(w["name"], w["path"]) for w in spaces] == [
        ("main", str(main)),
        ("feat", str(feat)),
    ]
    assert [w["project"] for w in spaces] == ["proj", "proj-feat"]


def test_current_workspace_follows_the_checkout_you_are_standing_in(monkeypatch, tmp_path):
    # What the TUI opens on: the whole list is visible from anywhere now, so the useful default
    # selection is the checkout you invoked fy from — the one a bare `fy up` here would act on.
    from pathlib import Path

    from foldyard import config

    main, feat = _worktree_layout(tmp_path)
    monkeypatch.setattr(devmode, "main_repo", lambda: main)
    monkeypatch.setattr(config, "worktrees_root", lambda base: Path(f"{base}-worktrees"))

    monkeypatch.setenv("WORKTREE", "feat")  # explicit target wins (the box always exports it)
    assert devmode.current_workspace() == "feat"
    monkeypatch.delenv("WORKTREE")
    monkeypatch.chdir(feat)  # …else inferred from CWD, exactly as the stack verbs infer it
    assert devmode.current_workspace() == "feat"
    monkeypatch.chdir(main)
    assert devmode.current_workspace() == "main"


def test_current_workspace_falls_back_to_main_when_it_cannot_resolve(monkeypatch, tmp_path):
    # Nothing about picking a default row may be able to take the TUI down: a non-git dir makes
    # main_repo() raise SystemExit (not an Exception), which an `except Exception` would let past.
    def boom():
        raise SystemExit(1)

    monkeypatch.setattr(devmode, "main_repo", boom)
    assert devmode.current_workspace() == "main"


def test_worktree_config_keys_on_the_checkout(monkeypatch, tmp_path):
    from foldyard import config

    main = tmp_path / "repo"
    main.mkdir()
    (main / "foldyard.toml").write_text('[project]\nname = "p"\n')
    wt = tmp_path / "repo-worktrees" / "feat"
    wt.mkdir(parents=True)
    (wt / "foldyard.toml").write_text('[project]\nname = "p"\n[proxy]\n')
    monkeypatch.setattr(config, "repo_root", lambda: main)
    monkeypatch.setattr(config, "worktrees_root", lambda _base: tmp_path / "repo-worktrees")

    # main → the ACTIVE config (current()); a worktree → resolved from ITS own checkout on disk.
    assert devmode.worktree_config("").worktree == ""
    feat_cfg = devmode.worktree_config("feat")
    assert feat_cfg.worktree == "feat" and feat_cfg.repo_root == wt.resolve()
    assert feat_cfg.has_table("proxy")  # reads the worktree's OWN foldyard.toml


def test_worktree_config_main_ignores_ambient_worktree(monkeypatch, tmp_path):
    from foldyard import config

    main = tmp_path / "repo"
    main.mkdir()
    (main / "foldyard.toml").write_text('[project]\nname = "primary"\n')
    feat = tmp_path / "repo-worktrees" / "feat"
    feat.mkdir(parents=True)
    (feat / "foldyard.toml").write_text('[project]\nname = "feat"\n[proxy]\n')
    monkeypatch.setenv("WORKTREE", "feat")
    monkeypatch.setattr(devmode, "main_repo", lambda: main)
    monkeypatch.setattr(config, "repo_root", lambda: feat)
    monkeypatch.setattr(config, "worktrees_root", lambda _base: tmp_path / "repo-worktrees")

    cfg = devmode.worktree_config("")
    assert cfg.worktree == ""
    assert cfg.repo_root == main.resolve()
    assert cfg.project() == "primary"


def test_worktree_config_main_ignores_bound_worktree(monkeypatch, tmp_path):
    from foldyard import config

    main = tmp_path / "repo"
    main.mkdir()
    (main / "foldyard.toml").write_text('[project]\nname = "primary"\n')
    monkeypatch.setattr(devmode, "main_repo", lambda: main)
    bound = config.Config(repo_root=tmp_path / "repo-worktrees" / "feat", worktree="feat", toml={})
    with config.using(bound):
        cfg = devmode.worktree_config("")
    assert cfg.worktree == ""
    assert cfg.repo_root == main.resolve()


# ── env derivation ────────────────────────────────────────────────────────────────────


def test_derive_env_clean_when_proxy_not_opted_in(monkeypatch):
    # Gated: with no [proxy] table declared and github off, NOTHING routes — a generic consumer's
    # box stays clean (no FY_PROXY ⇒ no HTTPS_PROXY/NO_PROXY env baked in).
    from foldyard import config

    monkeypatch.setattr(config, "proxy_enabled", lambda: False)
    assert devmode.derive_env({"gcp": "off", "github": "off"}) == {}


def test_derive_env_offline_routes_when_proxy_opted_in(monkeypatch):
    # Phase A′ — once the consumer opts into the proxy ([proxy] table), the box always routes,
    # even fully off (so capture toggles on a running box). gcp off contributes nothing.
    from foldyard import config

    monkeypatch.setattr(config, "proxy_enabled", lambda: True)
    assert devmode.derive_env({"gcp": "off", "github": "off"}) == {
        "FY_PROXY": f"{config.host_alias()}:41000"  # the project band's proxy base
    }


def test_derive_env_github_app_sets_proxy():
    assert "FY_PROXY" in devmode.derive_env({"gcp": "off", "github": "app"})


def test_derive_env_gcp_logs():
    env = devmode.derive_env({"gcp": "logs", "github": "off"})
    assert env["GCP_METADATA_HOST"] == "metadata-emulator:80"
    assert env["COMPOSE_PROFILES"] == "metadata"


def test_derive_env_gcp_sa_is_identity_only_storage_adds_cloudsql():
    # gcp=sa alone: metadata profile + the identity overlay, NO cloudsql (that's a data-plane
    # concern riding the storage axis now).
    # auth0=real pinned so this stays focused on the gcp/storage overlays (auth0 defaults to sim,
    # which would otherwise layer its own overlay — covered separately in test_plugins).
    env = devmode.derive_env({"gcp": "sa", "github": "off", "auth0": "real"})
    assert env["COMPOSE_PROFILES"] == "metadata"
    assert "FOLDYARD_COMPOSE_EXTRA" not in env
    from foldyard.plugins import registry

    overlays = registry().compose_overlays({"gcp": "sa", "github": "off", "auth0": "real"})
    assert any(o.endswith("compose.identity.yml") for o in overlays)
    # gcp=sa also layers the data-service identity overlay (per-service SAs — Phase 2).
    assert any(o.endswith("compose.identity-data.yml") for o in overlays)
    assert not any("storage" in o for o in overlays)
    # storage=staging (with the identity it requires) adds the cloudsql profile + its overlay.
    env = devmode.derive_env({"gcp": "sa", "storage": "staging", "github": "off", "auth0": "real"})
    assert env["COMPOSE_PROFILES"] == "metadata,cloudsql"
    overlays = registry().compose_overlays(
        {"gcp": "sa", "storage": "staging", "github": "off", "auth0": "real"}
    )
    assert [o.rsplit("/", 1)[-1] for o in overlays] == [
        "compose.identity.yml",
        "compose.identity-data.yml",
        "compose.storage-staging.yml",
    ]


def test_derive_env_gcp_user_no_box_relabel():
    # gcp=user no longer relabels the box (DEVBOX_SA sentinel is gone — the escalation moved to the
    # live minter), so the box wiring is mode-independent and a switch doesn't force a recreate.
    env = devmode.derive_env({"gcp": "user", "github": "off"})
    assert "DEVBOX_SA" not in env and env["GCP_METADATA_HOST"] == "metadata-emulator:80"


# ── desired_daemons (what `fy host` must run) ───────────────────────────────────────


def test_desired_daemons_offline_runs_only_the_always_on_proxy():
    # Phase A′ — always-on: the egress proxy runs for every mode (passthrough when capture=off), so
    # the box never hits a dead :8088. No gcp/github daemons when both are off.
    daemons = devmode.desired_daemons({"gcp": "off", "github": "off"})
    assert set(daemons) == {"egress-proxy"}
    assert daemons["egress-proxy"]["env"]["CAPTURE_MODE"] == "passthrough"


def test_gh_proxy_app_spec():
    # The egress-proxy daemon is now built by the `proxy` plugin from github's injection rule.
    from foldyard import config

    spec = devmode.desired_daemons({"gcp": "off", "github": "app"})["egress-proxy"]
    cmd = spec["env"]["INJECT_COMMAND"]
    # A PACKAGE module under foldyard's own interpreter — never a path inside the repo mount (that
    # was host-side execution of agent-writable code; see ADR-0023).
    assert "-m foldyard.plugins.github_app_token" in cmd
    assert f"--own-proxy-port {config.proxy_port()}" in cmd
    assert "PROXY_LOG_FILE" in spec["env"]
    assert "GH_APP_ID" in spec["requires"]
    assert spec["port"] == config.proxy_port()  # the project band's proxy base (main: no offset)


def test_gh_proxy_user_spec_needs_no_app_keys():
    spec = devmode.desired_daemons({"gcp": "off", "github": "user"})["egress-proxy"]
    assert spec["env"]["INJECT_COMMAND"].endswith("-m foldyard.plugins.gh_cli_token")
    assert spec["requires"] == []


# The SA emails are config-driven (foldyard.toml / env); set them via env so these
# assertions don't depend on the consumer repo's foldyard.toml or where tests run.
@pytest.fixture
def gcp_sas(monkeypatch):
    monkeypatch.setenv("DEVBOX_LOG_SA", "devbox-log-reader@p.iam.gserviceaccount.com")
    monkeypatch.setenv("APP_SERVICE_ACCOUNT", "app-runtime@p.iam.gserviceaccount.com")


def test_gcp_minter_logs_allowlist_is_log_reader_only(gcp_sas):
    allow = devmode.desired_daemons({"gcp": "logs", "github": "off"})["gcp-minter"]["env"][
        "GCP_SA_ALLOWLIST"
    ]
    assert "devbox-log-reader" in allow and "app-runtime" not in allow


def test_gcp_minter_sa_adds_runtime_sa(gcp_sas):
    allow = devmode.desired_daemons({"gcp": "sa", "github": "off"})["gcp-minter"]["env"][
        "GCP_SA_ALLOWLIST"
    ]
    assert "app-runtime" in allow


def test_gcp_minter_user_allows_user_token():
    spec = devmode.desired_daemons({"gcp": "user", "github": "off"})["gcp-minter"]
    assert spec["env"]["GCP_ALLOW_USER_TOKEN"] == "1"


# ── the shadow-volume doctor check (in-tree dep dirs the box + host would share) ───────


def _tree(root, *paths: str) -> None:
    for p in paths:
        (root / p).mkdir(parents=True, exist_ok=True)


def _shadow_row(monkeypatch, root, covered: list[str]):
    monkeypatch.setattr(devmode.config, "repo_root", lambda: root)
    monkeypatch.setattr(devmode.config, "box_shadow_volumes", lambda: covered)
    (status, name, detail) = next(iter(devmode._shadow_volume_check()))
    assert name == "shadow volumes"
    return status, detail


def test_shadow_check_flags_undeclared_dep_dirs(tmp_path, monkeypatch):
    _tree(tmp_path, "api/.venv", "web/node_modules", "api/src")
    status, detail = _shadow_row(monkeypatch, tmp_path, [])
    assert status == "warn"  # never fail: sharing may be deliberate, or setup half-done
    assert '"api/.venv", "web/node_modules"' in detail
    assert "shadow_volumes" in detail and "warmup" in detail


def test_shadow_check_is_ok_once_every_dir_is_declared(tmp_path, monkeypatch):
    _tree(tmp_path, "api/.venv", "web/node_modules")
    status, detail = _shadow_row(monkeypatch, tmp_path, ["api/.venv", "web/node_modules"])
    assert status == "ok" and "2 shadowed" in detail


def test_shadow_check_never_descends_into_a_dep_dir(tmp_path, monkeypatch):
    # A venv/node_modules vendors its own copies; reporting them would bury the real finding
    # (and, unpruned, is what makes the walk unbounded). Pruned whether or not it's declared.
    _tree(tmp_path, "web/node_modules/pkg/node_modules", "api/.venv/lib/python3.12/venv")
    _, detail = _shadow_row(monkeypatch, tmp_path, ["web/node_modules"])
    # web/node_modules is declared, so it's silent — and its VENDORED copy inside must not
    # resurface as a separate, undeclared finding.
    assert "node_modules" not in detail
    assert '"api/.venv"' in detail and "python3.12" not in detail  # nor the venv's internals


def test_shadow_check_ambiguous_names_need_their_sibling_manifest(tmp_path, monkeypatch):
    # `target` is a Rust build dir AND an ordinary folder name — a bare match would warn on
    # every project that happens to have one, which is how a check trains people to ignore it.
    _tree(tmp_path, "docs/target", "rust/target")
    (tmp_path / "rust" / "Cargo.toml").write_text("")
    _, detail = _shadow_row(monkeypatch, tmp_path, [])
    assert '"rust/target"' in detail and "docs/target" not in detail


def test_shadow_check_respects_max_depth_and_survives_unreadable_dirs(tmp_path, monkeypatch):
    # Depth is the walk's only hard bound on an arbitrary consumer tree; an unreadable dir is
    # a permission/build race, not a reason to blow up the whole doctor run.
    deep = "/".join(f"d{i}" for i in range(devmode._SCAN_MAX_DEPTH + 2))
    _tree(tmp_path, f"{deep}/node_modules", "a/b/node_modules")
    locked = tmp_path / "locked"
    locked.mkdir()
    locked.chmod(0o000)
    try:
        status, detail = _shadow_row(monkeypatch, tmp_path, [])
    finally:
        locked.chmod(0o755)
    assert '"a/b/node_modules"' in detail and deep not in detail
    assert status == "warn"


def _stub_engine(monkeypatch, names: list[str], specs: list[dict], seen: list | None = None):
    """Stub devmode's engine calls for the stack check: `ps` returns `names`, `inspect` returns
    `specs`. NEVER let these tests reach a real socket — inside the box `_engine_env()` resolves
    the LIVE machine, and a stray verb there acts on the running stack for real (DEVELOPMENT.md
    "Golden tests must never see a live engine socket")."""

    def fake_run(cmd, **kw):
        if seen is not None:
            seen.append(cmd)
        out = "\n".join(names) if cmd[1] == "ps" else json.dumps(specs)
        return subprocess.CompletedProcess(cmd, 0, out, "")

    monkeypatch.setattr(devmode.subprocess, "run", fake_run)
    monkeypatch.setattr(devmode, "_engine_env", dict)


def _spec(*mounts: tuple[str, str, str, bool], name: str = "") -> dict:
    spec: dict = {
        "Mounts": [{"Type": t, "Source": s, "Destination": d, "RW": rw} for t, s, d, rw in mounts]
    }
    if name:
        spec["Name"] = name
    return spec


def _stack_row(rows: list) -> tuple[str, str, str]:
    """The resolved row, having asserted doctor's placeholder contract: when the check speaks at
    all it emits exactly one 'running' spinner placeholder, resolved by a later REAL row of the
    same name. An unresolved placeholder leaves a live UI spinning on that row forever."""
    running = [r for r in rows if r[0] == "running"]
    real = [r for r in rows if r[0] != "running"]
    assert len(running) == 1 and len(real) == 1
    assert running[0][1] == real[0][1] == "stack shadowing"
    return real[0]


def test_stack_check_flags_dep_dirs_a_service_leaves_on_the_mount(tmp_path, monkeypatch):
    _tree(tmp_path, "web/node_modules", "api/.venv")
    monkeypatch.setattr(devmode.config, "repo_root", lambda: tmp_path)
    _stub_engine(
        monkeypatch,
        ["proj-web"],
        [_spec(("bind", f"{tmp_path}/web", "/app", True))],
    )
    (status, name, detail) = _stack_row(list(devmode._stack_shadow_check()))
    assert (status, name) == ("warn", "stack shadowing")
    assert "web/node_modules  →  proj-web" in detail
    assert "api/.venv" not in detail  # outside the service's bind — not its problem


def test_stack_check_counts_a_volume_on_the_path_or_any_parent_as_shadowed(tmp_path, monkeypatch):
    # `/app:vol` protects `/app/node_modules` exactly as `/app/node_modules:vol` does, so the
    # walk-up matters: testing the leaf alone would report a dir that is in fact masked.
    _tree(tmp_path, "web/node_modules", "api/.venv")
    monkeypatch.setattr(devmode.config, "repo_root", lambda: tmp_path)
    _stub_engine(
        monkeypatch,
        ["proj-web", "proj-api"],
        [
            _spec(
                ("bind", f"{tmp_path}/web", "/app", True),
                ("volume", "/vol/nm", "/app/node_modules", True),  # leaf
            ),
            _spec(
                ("bind", f"{tmp_path}/api", "/src", True),
                ("volume", "/vol/src", "/src", True),  # parent
            ),
        ],
    )
    (status, _, detail) = _stack_row(list(devmode._stack_shadow_check()))
    assert status == "ok" and "2 containers clean" in detail


def test_stack_check_ignores_read_only_binds(tmp_path, monkeypatch):
    # A :ro checkout mount can't be clobbered by the container — Tangible's auth0-simulator
    # mounts its harness read-only and rsyncs out of it, which is correct and must not warn.
    _tree(tmp_path, "sim/node_modules")
    monkeypatch.setattr(devmode.config, "repo_root", lambda: tmp_path)
    _stub_engine(monkeypatch, ["proj-sim"], [_spec(("bind", f"{tmp_path}/sim", "/ro", False))])
    (status, _, detail) = _stack_row(list(devmode._stack_shadow_check()))
    assert status == "ok" and "1 container clean" in detail


def test_stack_check_is_scoped_to_the_active_worktrees_project(tmp_path, monkeypatch):
    # Each worktree's stack is its OWN compose project mounting its OWN checkout. Filtering by
    # the active project is what stops a worktree reporting main's paths (and vice versa).
    from conftest import FULL_TOML, make_config

    monkeypatch.setattr(devmode.config, "repo_root", lambda: tmp_path)
    monkeypatch.setattr(devmode.config, "project_prefix", lambda: "acme")
    seen: list = []
    _stub_engine(monkeypatch, [], [], seen=seen)
    assert devmode.active_project() == "acme"  # main checkout
    list(devmode._stack_shadow_check())
    assert "label=com.docker.compose.project=acme" in seen[0]
    seen.clear()
    with devmode.config.using(make_config(FULL_TOML, worktree="my-feature")):
        assert devmode.active_project() == "acme-my-feature"
        list(devmode._stack_shadow_check())
    assert "label=com.docker.compose.project=acme-my-feature" in seen[0]


def test_stack_check_names_each_row_from_its_own_spec(tmp_path, monkeypatch):
    # `inspect` promises nothing about echoing its arguments back in order, and pairing the `ps`
    # names with the specs by position turns any reordering into a finding filed against the wrong
    # container — which sends the reader to a service whose mounts are fine. Docker's Name carries
    # a leading slash, podman's doesn't; both must resolve. The devbox filter is re-applied to the
    # derived name, since the `ps` list no longer decides who is who.
    _tree(tmp_path, "web/node_modules", "api/.venv")
    monkeypatch.setattr(devmode.config, "repo_root", lambda: tmp_path)
    _stub_engine(
        monkeypatch,
        ["proj-web", "proj-api"],
        [  # reversed relative to the `ps` order, plus a devbox the ps filter never saw
            _spec(("bind", f"{tmp_path}/api", "/app", True), name="proj-api"),
            _spec(("bind", f"{tmp_path}/web", "/app", True), name="/proj-web"),
            _spec(("bind", str(tmp_path), "/repo", True), name="proj-devbox"),
        ],
    )
    (status, _, detail) = _stack_row(list(devmode._stack_shadow_check()))
    assert status == "warn"
    assert "web/node_modules  →  proj-web" in detail
    assert "api/.venv  →  proj-api" in detail
    assert "devbox" not in detail


def test_stack_check_skips_the_devbox_and_says_nothing_with_no_stack(tmp_path, monkeypatch):
    # The devbox is the OTHER row's subject; reporting it twice teaches people to skim. And a
    # stack-less project must get no stack row at all — "no containers" is not a finding.
    _tree(tmp_path, "web/node_modules")
    monkeypatch.setattr(devmode.config, "repo_root", lambda: tmp_path)
    _stub_engine(monkeypatch, ["acme-devbox"], [_spec(("bind", str(tmp_path), "/repo", True))])
    assert list(devmode._stack_shadow_check()) == []


def test_stack_check_stays_silent_when_the_engine_is_unreachable(tmp_path, monkeypatch):
    # doctor already carries an "engine socket" row; a second red row for the same cause is noise.
    monkeypatch.setattr(devmode.config, "repo_root", lambda: tmp_path)
    monkeypatch.setattr(devmode, "_engine_env", dict)
    monkeypatch.setattr(
        devmode.subprocess, "run", lambda cmd, **kw: subprocess.CompletedProcess(cmd, 1, "", "no")
    )
    assert list(devmode._stack_shadow_check()) == []


# ── the engine-disk doctor row (shares `fy up`'s reclaim threshold) ────────────────────


def _disk_row(monkeypatch, headroom, seen: dict | None = None):
    from foldyard import stack

    def probe(env=None, **kw):
        if seen is not None:
            seen.update({"env": env, **kw})
        return headroom

    monkeypatch.setattr(stack, "disk_headroom", probe)
    return list(devmode._disk_headroom_check())


def test_disk_row_probes_like_the_other_read_only_engine_calls(monkeypatch):
    # Bare, the probe misses a lima backend (which registers no default podman connection), and
    # a 15s hang would stall the TUI's 5s doctor refresh.
    from foldyard import stack

    monkeypatch.setattr(devmode, "_engine_env", lambda: {"DOCKER_HOST": "unix:///nonexistent/x"})
    seen: dict = {}
    _disk_row(monkeypatch, stack.DiskHeadroom(used=1, total=90 * 1024**3), seen)
    assert seen["env"] == {"DOCKER_HOST": "unix:///nonexistent/x"} and seen["timeout"] == 5


def test_disk_row_warns_when_the_store_is_low(monkeypatch):
    from foldyard import stack

    rows = _disk_row(monkeypatch, stack.DiskHeadroom(used=87 * 1024**3, total=90 * 1024**3))
    assert len(rows) == 1
    status, name, detail = rows[0]
    assert (status, name) == ("warn", "engine disk")  # warn, never fail — it's still working
    assert "no space left on device" in detail and "until=24h" in detail


def test_disk_row_is_ok_with_headroom(monkeypatch):
    from foldyard import stack

    rows = _disk_row(monkeypatch, stack.DiskHeadroom(used=40 * 1024**3, total=90 * 1024**3))
    assert rows[0][0] == "ok"


def test_disk_row_is_absent_when_the_figure_is_unknown(monkeypatch):
    # No machine yet, a docker engine, a stopped VM: "unknown" is not a finding, and a row
    # that can't say anything true is worse than no row.
    assert _disk_row(monkeypatch, None) == []


# ── run_stream (the doctor-fix runner: streams output, returns rc) ─────────────────────


def test_run_stream_streams_lines_and_returns_rc():
    lines: list[str] = []
    rc = devmode.run_stream(["python3", "-c", "print('one'); print('two')"], lines.append)
    assert rc == 0 and lines == ["one", "two"]


def test_run_stream_merges_stderr_and_reports_failure():
    lines: list[str] = []
    rc = devmode.run_stream(
        ["python3", "-c", "import sys; print('boom', file=sys.stderr); sys.exit(3)"], lines.append
    )
    assert rc == 3 and "boom" in lines


def test_run_stream_missing_command_is_127_not_a_crash():
    lines: list[str] = []
    rc = devmode.run_stream(["this-command-does-not-exist-xyz"], lines.append)
    assert rc == 127 and any("✗" in line for line in lines)


def test_host_command_launches_foldyard_host():
    cmd = devmode.host_command()
    assert cmd[-1] == "host" and cmd[0].endswith("foldyard")


@pytest.mark.parametrize("bad", ["inf", "-inf", "nan", "999999999999999"])
def test_clock_offset_rejects_non_finite_and_absurd(isolated_state, monkeypatch, bad):
    # A corrupt offset (env typo or mangled state file) must read as "no skew" — never crash
    # the supervisor's now() arithmetic with an overflowing timedelta.
    monkeypatch.setenv("FOLDYARD_CLOCK_OFFSET", bad)
    assert devmode.clock_offset() == 0.0
    monkeypatch.setenv("FOLDYARD_CLOCK_OFFSET", "")
    (isolated_state["dir"] / "clock-offset").write_text(f"{bad}\n")
    monkeypatch.delenv("FOLDYARD_CLOCK_OFFSET")
    assert devmode.clock_offset() == 0.0
    devmode.now()  # and now() stays callable whatever the file says


@pytest.mark.parametrize("bad", ["soon", "2 hours", "1d", ""])
def test_clock_ff_rejects_malformed_durations_as_usage_error(isolated_state, monkeypatch, bad):
    monkeypatch.delenv("FOLDYARD_CLOCK_OFFSET", raising=False)
    with pytest.raises(SystemExit, match=r"duration|usage"):
        devmode.clock_cli(["ff", bad])


def test_settle_coordinated_fallback_when_no_single_downgrade_helps(monkeypatch):
    # Two axes whose ONE error only clears together: no single downgrade reduces the count, so
    # the greedy loop stalls — the coordinated fallback settles everything-at-rest instead.
    from foldyard.plugins import Axis, Plugin, Registry

    class Tangled(Plugin):
        name = "tangled"

        def axes(self):
            return [
                Axis(name="a", rungs=("off", "on"), blurb={"off": "-", "on": "-"}),
                Axis(name="b", rungs=("off", "on"), blurb={"off": "-", "on": "-"}),
            ]

        def mode_issues(self, mode):
            if mode.get("a") == "on" or mode.get("b") == "on":
                yield ("error", "a and b must both be off together")

    # Explicit generic config: this module binds FULL_TOML (whose [[require]] rows name the
    # real llm/storage axes), and a synthetic registry resolving it ambiently would trip the
    # unknown-owner validation.
    from conftest import GENERIC_TOML, make_config

    reg = Registry([Tangled()], config=make_config(GENERIC_TOML))
    monkeypatch.setattr(devmode, "registry", lambda: reg)
    assert devmode.settle_incoherent({"a": "on", "b": "on"}) == {"a": "off", "b": "off"}


def test_settle_leaves_unfixable_errors_visible(monkeypatch):
    # A plugin erroring even at all-defaults (a misconfiguration, not a combination) must not
    # make settle thrash — nothing helps, so nothing flips and `fy mode` keeps surfacing it.
    from foldyard.plugins import Axis, Plugin, Registry

    class Broken(Plugin):
        name = "broken"

        def axes(self):
            return [Axis(name="a", rungs=("off", "on"), blurb={"off": "-", "on": "-"})]

        def mode_issues(self, mode):
            yield ("error", "always broken")

    from conftest import GENERIC_TOML, make_config

    reg = Registry([Broken()], config=make_config(GENERIC_TOML))
    monkeypatch.setattr(devmode, "registry", lambda: reg)
    assert devmode.settle_incoherent({"a": "on"}) == {}


# ── gcp=user is a TRUE ladder rung (superset of sa) ──────────────────────────────────────


def test_llm_and_storage_accept_gcp_user(isolated_state):
    # Escalating the identity axis must never strand a dependent rung: user ⊇ sa, so raising
    # gcp sa→user under llm=live is a legal, coherent move (the rigidity that blocked switching
    # to your own access mid-llm-live session).
    devmode.set_mode({"gcp": "sa", "llm": "live"})
    state = devmode.set_mode({"gcp": "user"}, ttl=3600)
    assert state["mode"] == {**state["mode"], "gcp": "user", "llm": "live"}


def test_gcp_user_expiry_settles_llm(isolated_state):
    # …and when the user TTL lapses, gcp reverts to OFF (not sa — expiry always rests at the
    # default), stranding llm — the settle cascade flips it down in the same write.
    import json as _json
    from datetime import timedelta

    from foldyard import supervisor

    devmode.set_mode({"gcp": "sa", "llm": "live"})
    devmode.set_mode({"gcp": "user"}, ttl=60)
    raw = _json.loads(isolated_state["auth"].read_text())
    raw["expires"]["gcp"] = devmode._iso(devmode.now() - timedelta(hours=1))
    isolated_state["auth"].write_text(_json.dumps(raw))
    mode = supervisor.expire_user_modes()
    assert mode["gcp"] == "off" and mode["llm"] == "off"


def test_fy_subprocesses_never_inherit_the_terminal(monkeypatch):
    """The TUI shells out to `fy` verbs while Textual owns the terminal in raw mode. An inherited
    stdin is still a TTY, so configpin's launch gate decides it may prompt and then blocks on an
    `input()` nobody can see — which wedged `fy box up` from the TUI on any unadopted worktree.
    Closed stdin makes the gate read "no terminal here" and return its actionable error instead."""
    seen = {}

    class _Done:
        returncode = 0
        stdout = ""
        stderr = ""

    def _fake_run(args, **kwargs):
        seen.update(kwargs)
        return _Done()

    monkeypatch.setattr(devmode.subprocess, "run", _fake_run)
    monkeypatch.setattr(devmode, "main_repo", lambda: "/tmp")
    devmode._fy(["box", "up"])
    assert seen.get("stdin") is subprocess.DEVNULL
