"""config.py — repo/project/path resolution. Env vars always win, then foldyard.toml,
then defaults (docs/configuration.md). All lookups go through tmp dirs + the fresh_config fixture so
nothing reads the real repo or ~/.foldyard."""

from __future__ import annotations

from pathlib import Path

import pytest

from foldyard import config


def test_repo_root_from_env(fresh_config, tmp_path):
    fresh_config(FOLDYARD_REPO=tmp_path)
    assert config.repo_root() == tmp_path.resolve()


def test_repo_root_walks_up_to_marker(fresh_config, tmp_path, monkeypatch):
    (tmp_path / "foldyard.toml").write_text('[project]\nname = "x"\n')
    sub = tmp_path / "a" / "b"
    sub.mkdir(parents=True)
    fresh_config(FOLDYARD_REPO=None)
    monkeypatch.chdir(sub)
    config.clear_caches()
    assert config.repo_root() == tmp_path.resolve()


def test_project_from_env_beats_toml(fresh_config, tmp_path):
    (tmp_path / "foldyard.toml").write_text('[project]\nname = "fromtoml"\n')
    fresh_config(FOLDYARD_REPO=tmp_path, FOLDYARD_PROJECT="fromenv")
    assert config.project() == "fromenv"


def test_project_from_toml(fresh_config, tmp_path):
    (tmp_path / "foldyard.toml").write_text('[project]\nname = "myproj"\n')
    fresh_config(FOLDYARD_REPO=tmp_path, FOLDYARD_PROJECT=None)
    assert config.project() == "myproj"


def test_external_network_defaults_off_and_reads_toml(fresh_config, tmp_path):
    # Off unless the consumer opts in — projects whose compose stack owns its own network
    # (the example consumer, any third-party repo) must see no behaviour change.
    (tmp_path / "foldyard.toml").write_text('[project]\nname = "p"\n')
    fresh_config(FOLDYARD_REPO=tmp_path)
    assert config.external_network() is False
    (tmp_path / "foldyard.toml").write_text('[project]\nname = "p"\nexternal_network = true\n')
    config.clear_caches()
    assert config.external_network() is True


def test_local_toml_deep_merges_over_base(fresh_config, tmp_path):
    # foldyard.local.toml (gitignored, per-dev) deep-merges OVER foldyard.toml: nested tables merge
    # key-by-key (a local [claude].keyless lands beside the committed [claude].system_prompt), and a
    # whole new table ([codex]) is added — without touching the shared file.
    (tmp_path / "foldyard.toml").write_text(
        '[project]\nname = "p"\n[claude]\nsystem_prompt = "shared"\n'
    )
    (tmp_path / "foldyard.local.toml").write_text(
        '[claude]\nkeyless = "oauth"\n[codex]\nkeyless = "chatgpt"\n'
    )
    fresh_config(FOLDYARD_REPO=tmp_path)
    assert config.claude_keyless() == "oauth"  # added by local
    assert config._table("claude")["system_prompt"] == "shared"  # base key preserved (deep merge)
    assert config.codex_keyless() == "chatgpt"  # whole table contributed by local


def test_codex_takes_a_system_prompt_like_claude(fresh_config, tmp_path):
    # Same key, same job, different delivery (`-c developer_instructions=…`, box._codex_argv).
    (tmp_path / "foldyard.toml").write_text(
        '[project]\nname = "p"\n[codex]\nsystem_prompt = "be grounded"\n'
    )
    fresh_config(FOLDYARD_REPO=tmp_path)
    assert config.codex_system_prompt() == "be grounded"
    assert config.claude_system_prompt() == ""  # no [claude] table — the two are independent


def test_agent_cli_config_tables_deep_merge_per_key(fresh_config, tmp_path):
    # `[claude.settings]` / `[codex.config]` are what the shared file says about the project's
    # agent sessions; the local file overrides ONE knob (a personal model) without redeclaring the
    # table — the whole reason these are tables rather than a pre-rendered args list.
    (tmp_path / "foldyard.toml").write_text(
        '[project]\nname = "p"\n'
        '[claude.settings]\nmodel = "claude-sonnet-4-5"\nspinnerTipsEnabled = false\n'
        '[codex.config]\nmodel_reasoning_summary = "auto"\n'
        "[codex.config.tui]\nraw_output_mode = false\n"
    )
    (tmp_path / "foldyard.local.toml").write_text(
        '[claude.settings]\nmodel = "claude-opus-4-8"\n[codex.config]\nhide_agent_reasoning = false\n'
    )
    fresh_config(FOLDYARD_REPO=tmp_path)
    assert config.claude_settings() == {
        "model": "claude-opus-4-8",  # local wins
        "spinnerTipsEnabled": False,  # …beside the shared key, not instead of it
    }
    assert config.codex_config() == {
        "model_reasoning_summary": "auto",
        "tui": {"raw_output_mode": False},  # nested tables survive; box._codex_overrides flattens
        "hide_agent_reasoning": False,
    }


def test_agent_cli_config_defaults_empty_and_ignores_a_non_table(fresh_config, tmp_path):
    # No table ⇒ no flag at all (box._claude_argv / _codex_argv), and a scalar written where a
    # table belongs contributes nothing rather than crashing the launcher.
    (tmp_path / "foldyard.toml").write_text(
        '[project]\nname = "p"\n[claude]\nsettings = "oops"\n[codex]\n'
    )
    fresh_config(FOLDYARD_REPO=tmp_path)
    assert config.claude_settings() == {}
    assert config.codex_config() == {}


def test_local_toml_can_disable_a_shared_block(fresh_config, tmp_path):
    # The deep merge can only ADD, and [codex] is PRESENCE-gated — so without `disabled` a colleague
    # who doesn't have a Codex subscription still gets the CLI installed, the credential prompt and
    # the codex mode row from a table the team committed. `disabled = true` removes it outright,
    # while the tables around it are untouched.
    (tmp_path / "foldyard.toml").write_text(
        '[project]\nname = "p"\n[claude]\nsystem_prompt = "shared"\n[codex]\nkeyless = "chatgpt"\n'
    )
    (tmp_path / "foldyard.local.toml").write_text("[codex]\ndisabled = true\n")
    fresh_config(FOLDYARD_REPO=tmp_path)
    assert config.codex_enabled() is False
    assert config.codex_keyless() == ""
    assert config.claude_enabled() is True  # only the named block goes


def test_disable_works_at_depth_and_the_marker_never_survives(fresh_config, tmp_path):
    # Nested tables ([plugins.<name>], the other presence-gated tier) disable the same way, and the
    # marker itself is stripped from blocks that STAY — nothing downstream should ever meet it.
    (tmp_path / "foldyard.toml").write_text(
        '[project]\nname = "p"\n'
        '[plugins.github]\napp_id = "1"\n'
        '[plugins.gcp-metadata]\nproject = "proj"\n'
    )
    (tmp_path / "foldyard.local.toml").write_text(
        "[plugins.github]\ndisabled = true\n[plugins.gcp-metadata]\ndisabled = false\n"
    )
    fresh_config(FOLDYARD_REPO=tmp_path)
    assert config.github_declared() is False
    assert config.gcp_metadata_declared() is True  # explicitly kept
    assert "disabled" not in config._toml()["plugins"]["gcp-metadata"]


def test_local_disabled_false_re_enables_a_block_the_shared_file_switched_off(
    fresh_config, tmp_path
):
    # Pruning happens AFTER the merge, which is what makes the override direction work both ways:
    # a project can park a block as disabled and a developer can opt INTO it.
    (tmp_path / "foldyard.toml").write_text('[project]\nname = "p"\n[codex]\ndisabled = true\n')
    fresh_config(FOLDYARD_REPO=tmp_path)
    assert config.codex_enabled() is False
    (tmp_path / "foldyard.local.toml").write_text("[codex]\ndisabled = false\n")
    config.clear_caches()
    assert config.codex_enabled() is True


def test_disabled_entries_drop_out_of_an_array_of_tables(fresh_config, tmp_path):
    # Same marker one tier down: an [[inject]] row can be parked without deleting it. (The array
    # itself is still replaced wholesale by a local one — the merge rule, unchanged.)
    (tmp_path / "foldyard.toml").write_text(
        '[project]\nname = "p"\n'
        '[[inject]]\naxis = "keep"\nhost = "a.example.com"\n'
        '[[inject]]\naxis = "parked"\nhost = "b.example.com"\ndisabled = true\n'
    )
    fresh_config(FOLDYARD_REPO=tmp_path)
    assert [spec["axis"] for spec in config.inject_specs()] == ["keep"]


def test_disabled_blocks_are_reportable_with_the_file_that_switched_them_off(tmp_path):
    # Subtraction leaves no trace in the resolved config, so the surfaces that explain a checkout
    # (`fy config widenings`) read it from the raw files — including WHICH file, since "nobody has
    # codex" and "I don't have codex" are different answers.
    shared = {"codex": {"keyless": "chatgpt"}, "plugins": {"github": {"app_id": "1"}}}
    local = {"codex": {"disabled": True}}
    assert config.disabled_blocks(shared, local) == [("codex", True)]
    assert config.disabled_blocks({"plugins": {"github": {"disabled": True}}}, {}) == [
        ("plugins.github", False)
    ]


def test_local_toml_absent_is_a_noop(fresh_config, tmp_path):
    # No local file → behaviour is exactly the base foldyard.toml (and no codex prompt/warning).
    (tmp_path / "foldyard.toml").write_text(
        '[project]\nname = "p"\n[claude]\nsystem_prompt = "x"\n'
    )
    fresh_config(FOLDYARD_REPO=tmp_path)
    assert config.claude_keyless() == "" and config.codex_keyless() == ""


def test_project_falls_back_to_dir_name_lowercased(fresh_config, tmp_path):
    repo = tmp_path / "CoolRepo"
    repo.mkdir()
    fresh_config(FOLDYARD_REPO=repo, FOLDYARD_PROJECT=None)
    assert config.project() == "coolrepo"


def test_dev_vm_dir_default_is_repo_root(fresh_config, tmp_path):
    # Project-agnostic default: a consumer that doesn't set dev_vm_dir keeps its
    # (transitional) assets at the repo root, not under any one project's layout.
    fresh_config(FOLDYARD_REPO=tmp_path, FOLDYARD_DEV_VM_DIR=None)
    assert config.dev_vm_dir() == config.repo_root()


def test_dev_vm_dir_from_toml(fresh_config, tmp_path):
    (tmp_path / "foldyard.toml").write_text('[project]\nname = "p"\ndev_vm_dir = "infra/x"\n')
    fresh_config(FOLDYARD_REPO=tmp_path, FOLDYARD_DEV_VM_DIR=None)
    assert config.dev_vm_dir() == config.repo_root() / "infra/x"


def test_dev_vm_dir_absolute_env_override(fresh_config, tmp_path):
    fresh_config(FOLDYARD_REPO=tmp_path, FOLDYARD_DEV_VM_DIR=tmp_path / "assets")
    assert config.dev_vm_dir() == tmp_path / "assets"


def test_state_paths_from_state_dir_env(fresh_config, tmp_path):
    fresh_config(
        FOLDYARD_STATE_DIR=tmp_path,
        FOLDYARD_MODE_FILE=None,
        FOLDYARD_HOST_ENV=None,
        FOLDYARD_LOG_DIR=None,
        WORKTREE=None,  # main checkout → posture under <state_dir>/main/
    )
    sd = tmp_path.resolve()
    assert config.state_dir() == sd
    # POSTURE (mode, logs) is per-worktree under <state_dir>/<worktree-or-'main'>/; host.env (shared
    # identity) stays at the state-dir root (ADR-0016 split).
    assert config.posture_dir() == sd / "main"
    assert config.mode_file() == sd / "main" / "dev-mode.json"
    assert config.log_dir() == sd / "main" / "logs"
    assert config.host_env_file() == sd / "host.env"


def test_posture_dir_is_per_worktree(fresh_config, tmp_path):
    # A worktree keys its posture under its own subdir, so two branches hold distinct postures
    # under the one shared state dir (and the one supervisor).
    fresh_config(FOLDYARD_STATE_DIR=tmp_path, FOLDYARD_MODE_FILE=None, WORKTREE="featbranch")
    # Worktrees nest under worktrees/ so one named "main" can't collide with the primary's main/.
    assert config.posture_dir() == tmp_path.resolve() / "worktrees" / "featbranch"
    assert config.mode_file() == tmp_path.resolve() / "worktrees" / "featbranch" / "dev-mode.json"


def test_posture_dir_main_and_worktree_named_main_dont_collide(fresh_config, tmp_path):
    # The bug the user hit: the primary checkout (key "") and a worktree literally NAMED "main" both
    # mapped to <state_dir>/main, so a mode change to one changed the other. They must be distinct.
    fresh_config(FOLDYARD_STATE_DIR=tmp_path, FOLDYARD_MODE_FILE=None, WORKTREE=None)
    primary = config.posture_dir()
    fresh_config(FOLDYARD_STATE_DIR=tmp_path, FOLDYARD_MODE_FILE=None, WORKTREE="main")
    worktree_named_main = config.posture_dir()
    assert primary != worktree_named_main
    assert primary == tmp_path.resolve() / "main"
    assert worktree_named_main == tmp_path.resolve() / "worktrees" / "main"


def test_worktree_offset_and_per_worktree_ports(fresh_config, monkeypatch):
    # Per-worktree daemon ports = base + the worktree's deterministic offset (0 for main). WT_OFFSET
    # pins the offset so the test doesn't depend on the cksum of a name.
    fresh_config(WORKTREE=None)
    assert config.worktree_offset("") == 0
    # main: the project's allocated band bases (first band in the test-isolated registry)
    assert config.proxy_port() == 41000 and config.gcp_minter_port() == 41100
    assert config.worktree_suffix() == ""

    monkeypatch.setenv("WT_OFFSET", "7")
    fresh_config(WORKTREE="feat")  # clears the offset cache
    assert config.worktree_offset("feat") == 7
    assert config.proxy_port() == 41000 + 7 and config.gcp_minter_port() == 41100 + 7
    assert config.worktree_suffix() == "@feat"


def test_mode_file_legacy_fallback_for_main(fresh_config, tmp_path):
    # A pre-per-worktree install kept dev-mode.json at the state-dir root. Until the new
    # <state_dir>/main/dev-mode.json exists, the main checkout keeps reading the legacy file so an
    # upgrade doesn't reset posture to offline. Worktrees never fall back.
    fresh_config(FOLDYARD_STATE_DIR=tmp_path, FOLDYARD_MODE_FILE=None, WORKTREE=None)
    legacy = tmp_path.resolve() / "dev-mode.json"
    legacy.write_text("{}")
    assert config.mode_file() == legacy  # legacy honoured while the new file is absent
    new = tmp_path.resolve() / "main" / "dev-mode.json"
    new.parent.mkdir(parents=True)
    new.write_text("{}")
    assert config.mode_file() == new  # once migrated, the per-worktree file wins


def test_state_dir_defaults_to_per_project_home(fresh_config, tmp_path):
    fresh_config(FOLDYARD_REPO=tmp_path, FOLDYARD_PROJECT="acme", FOLDYARD_STATE_DIR=None)
    assert config.state_dir() == Path.home() / ".foldyard" / "acme"


def test_explicit_file_env_overrides_win(fresh_config, tmp_path):
    mode = tmp_path / "m.json"
    host = tmp_path / "h.env"
    logs = tmp_path / "lg"
    fresh_config(FOLDYARD_MODE_FILE=mode, FOLDYARD_HOST_ENV=host, FOLDYARD_LOG_DIR=logs)
    assert config.mode_file() == mode
    assert config.host_env_file() == host
    assert config.log_dir() == logs.resolve()


def test_mirror_file_under_dev_vm_dir(fresh_config, tmp_path):
    fresh_config(FOLDYARD_REPO=tmp_path, FOLDYARD_DEV_VM_DIR=tmp_path / "dv")
    assert config.mirror_file() == tmp_path / "dv" / ".dev-mode.json"


# ── machine_backend (podman default vs opt-in lima) ───────────────────────────────────


def test_machine_backend_defaults_to_lima(fresh_config, tmp_path):
    # Lima is the default: it is the only backend with concurrent per-project VMs AND the in-VM
    # fail-closed wall, and `fy init` has scaffolded it since it shipped (ADR-0011 amendment).
    (tmp_path / "foldyard.toml").write_text('[project]\nname = "p"\n')
    fresh_config(FOLDYARD_REPO=tmp_path, MACHINE_BACKEND=None)
    assert config.machine_backend() == "lima"
    assert config.machine_backend_explicit() == ""  # nobody CHOSE it — it was inherited


def test_machine_backend_native_is_explicit_toml_opt_in(fresh_config, tmp_path):
    (tmp_path / "foldyard.toml").write_text('[machine]\nbackend = "native"\n')
    fresh_config(FOLDYARD_REPO=tmp_path, MACHINE_BACKEND=None)
    assert config.machine_backend() == "native"


def test_machine_backend_env_can_select_native(fresh_config, tmp_path):
    (tmp_path / "foldyard.toml").write_text('[project]\nname = "p"\n')
    fresh_config(FOLDYARD_REPO=tmp_path, MACHINE_BACKEND="native")
    assert config.machine_backend() == "native"


def test_machine_backend_default_does_not_change_in_the_box(fresh_config, tmp_path, monkeypatch):
    # The box must resolve the SAME backend as the host: it reads the machine's socket, it doesn't
    # pick a backend of its own, and a divergence here would point the box at another VM's paths.
    (tmp_path / "foldyard.toml").write_text('[project]\nname = "p"\n')
    fresh_config(FOLDYARD_REPO=tmp_path, MACHINE_BACKEND=None)
    monkeypatch.setattr(config, "in_box", lambda: True)
    assert config.machine_backend() == "lima"


def test_machine_backend_from_toml(fresh_config, tmp_path):
    (tmp_path / "foldyard.toml").write_text('[machine]\nbackend = "podman"\n')
    fresh_config(FOLDYARD_REPO=tmp_path, MACHINE_BACKEND=None)
    assert config.machine_backend() == "podman"
    # Same NAME as the default would give for lima, but chosen — which is what decides whether a
    # missing CLI is "this host has no VM tooling" (skip) or a misconfiguration (abort).
    assert config.machine_backend_explicit() == "podman"


def test_machine_backend_env_beats_toml_and_is_lowercased(fresh_config, tmp_path):
    (tmp_path / "foldyard.toml").write_text('[machine]\nbackend = "lima"\n')
    fresh_config(FOLDYARD_REPO=tmp_path, MACHINE_BACKEND="Podman")
    assert config.machine_backend() == "podman"


# ── host_alias (the box→Mac address — backend-dependent) ───────────────────────────────


def test_host_alias_is_containers_internal_on_podman(fresh_config, tmp_path):
    (tmp_path / "foldyard.toml").write_text('[machine]\nbackend = "podman"\n')
    fresh_config(FOLDYARD_REPO=tmp_path, MACHINE_BACKEND=None, FY_HOST_ALIAS=None)
    assert config.host_alias() == "host.containers.internal"


def test_host_alias_is_the_lima_host_gateway_on_lima(fresh_config, tmp_path):
    # Under lima, host.containers.internal resolves to the VM's own gateway — the alias must be
    # Lima's guest→host address instead, so box egress actually reaches the Mac proxy.
    (tmp_path / "foldyard.toml").write_text('[machine]\nbackend = "lima"\n')
    fresh_config(FOLDYARD_REPO=tmp_path, MACHINE_BACKEND=None, FY_HOST_ALIAS=None)
    assert config.host_alias() == config.LIMA_HOST_GATEWAY == "192.168.5.2"


def test_host_alias_env_escape_hatch_wins(fresh_config, tmp_path):
    (tmp_path / "foldyard.toml").write_text('[machine]\nbackend = "lima"\n')
    fresh_config(FOLDYARD_REPO=tmp_path, MACHINE_BACKEND=None, FY_HOST_ALIAS="10.9.8.7")
    assert config.host_alias() == "10.9.8.7"


# ── machine_wall ([machine].wall — lima in-VM egress enforcement) ──────────────────────


def test_machine_wall_defaults_off(fresh_config, tmp_path):
    (tmp_path / "foldyard.toml").write_text('[machine]\nbackend = "lima"\n')
    fresh_config(FOLDYARD_REPO=tmp_path, MACHINE_WALL=None)
    assert config.machine_wall() is False


def test_machine_wall_toml_opt_in(fresh_config, tmp_path):
    (tmp_path / "foldyard.toml").write_text('[machine]\nbackend = "lima"\nwall = true\n')
    fresh_config(FOLDYARD_REPO=tmp_path, MACHINE_WALL=None)
    assert config.machine_wall() is True


def test_machine_wall_env_overrides_toml_both_ways(fresh_config, tmp_path):
    (tmp_path / "foldyard.toml").write_text("[machine]\nwall = true\n")
    fresh_config(FOLDYARD_REPO=tmp_path, MACHINE_WALL="0")
    assert config.machine_wall() is False
    fresh_config(MACHINE_WALL="on")
    assert config.machine_wall() is True


# ── box_image (consumer dockerfile vs the packaged generic box; ADR-0014) ────────────


def test_box_image_consumer_dockerfile(fresh_config, tmp_path):
    (tmp_path / "foldyard.toml").write_text(
        '[project]\nname = "p"\n[box]\nimage = { dockerfile = "box.Dockerfile" }\n'
    )
    fresh_config(FOLDYARD_REPO=tmp_path)
    img = config.box_image()
    assert img["dockerfile"] == "box.Dockerfile"
    assert img["tag"] == "p-devbox:latest"  # tag defaulted from the prefix
    assert "context" not in img  # consumer dockerfile builds against the repo root


def test_box_image_falls_back_to_packaged_generic(fresh_config, tmp_path):
    # No [box].image declared → foldyard's packaged generic box, with the packaged dir as
    # the build context (so the generic box doesn't depend on repo contents).
    (tmp_path / "foldyard.toml").write_text('[project]\nname = "p"\n')
    fresh_config(FOLDYARD_REPO=tmp_path)
    img = config.box_image()
    dockerfile = Path(img["dockerfile"])
    assert dockerfile.name == "Dockerfile"
    assert dockerfile.is_file(), "the packaged generic Dockerfile must ship in the package"
    assert img["context"] == str(dockerfile.parent)
    assert img["tag"] == "p-devbox:latest"


# ── tail_jsonl (the bounded log tailer the TUI panels share) ──────────────────────────


import json  # noqa: E402 — grouped with the tail_jsonl tests, not the module's path tests


def _write_jsonl(path: Path, objs) -> None:
    path.write_text("".join(json.dumps(o) + "\n" for o in objs))


def test_tail_jsonl_returns_last_limit_oldest_to_newest(tmp_path):
    log = tmp_path / "x.jsonl"
    _write_jsonl(log, [{"i": i} for i in range(10)])
    assert config.tail_jsonl(log, 3) == [{"i": 7}, {"i": 8}, {"i": 9}]


def test_tail_jsonl_missing_file_is_empty(tmp_path):
    assert config.tail_jsonl(tmp_path / "nope.jsonl", 5) == []


def test_tail_jsonl_skips_malformed_lines(tmp_path):
    log = tmp_path / "x.jsonl"
    log.write_text('{"a":1}\nnot json\n{"a":2}\n')
    assert config.tail_jsonl(log, 10) == [{"a": 1}, {"a": 2}]


def test_tail_jsonl_only_reads_the_tail_of_a_big_log(tmp_path, monkeypatch):
    # A log far bigger than the tail budget: we must get the last `limit` records (not choke on
    # the whole file) and never mis-parse the partial first line the mid-file seek lands on.
    log = tmp_path / "big.jsonl"
    _write_jsonl(log, [{"i": i} for i in range(5000)])  # ~50 KB+
    monkeypatch.setattr(config, "LOG_TAIL_BYTES", 1024)  # force a tail much smaller than the file
    out = config.tail_jsonl(log, 5)
    assert out == [{"i": i} for i in range(4995, 5000)]  # exact last 5, correctly parsed


def test_tail_jsonl_falls_back_to_dated_backups_when_live_is_short(tmp_path):
    # Just-rotated: the live file holds only the newest lines; older lines live in DATED backups.
    # The tail must span backups (newest first) then live, oldest→newest, to fill `limit`.
    live = tmp_path / "x.jsonl"
    older = tmp_path / "x.20260101T000000Z.jsonl"  # chronological by name
    newer = tmp_path / "x.20260102T000000Z.jsonl"
    _write_jsonl(older, [{"i": 0}, {"i": 1}])
    _write_jsonl(newer, [{"i": 2}, {"i": 3}])
    _write_jsonl(live, [{"i": 4}])
    assert config.rotated_logs(live) == [older, newer]  # oldest→newest
    assert config.tail_jsonl(live, 4) == [{"i": 1}, {"i": 2}, {"i": 3}, {"i": 4}]


def test_rotated_logs_ignores_the_live_file(tmp_path):
    live = tmp_path / "x.jsonl"
    live.write_text("{}\n")
    (tmp_path / "x.20260101T000000Z.jsonl").write_text("{}\n")
    assert config.rotated_logs(live) == [tmp_path / "x.20260101T000000Z.jsonl"]  # live not matched


# ── environment detection ─────────────────────────────────────────────────────────────


def test_in_box_detection(monkeypatch):
    monkeypatch.delenv("IN_DEVBOX", raising=False)
    monkeypatch.delenv("DOCKER_HOST", raising=False)
    assert config.in_box() is False
    monkeypatch.setenv("IN_DEVBOX", "1")
    assert config.in_box() is True
    monkeypatch.delenv("IN_DEVBOX")
    monkeypatch.setenv("DOCKER_HOST", "unix:///var/run/docker.sock")  # socket alone is not enough
    assert config.in_box() is False
    monkeypatch.setenv("DOCKER_HOST", "unix:///some/other.sock")  # a Mac-set podman socket
    assert config.in_box() is False


# ── engine selection ──────────────────────────────────────────────────────────────────


def test_engine_explicit_env_wins(fresh_config):
    fresh_config(FOLDYARD_ENGINE="docker")
    assert config.engine() == "docker"
    assert config.engine_compose() == ["docker", "compose"]


def test_engine_from_toml(fresh_config, tmp_path):
    (tmp_path / "foldyard.toml").write_text('[engine]\ncli = "docker"\n')
    fresh_config(FOLDYARD_REPO=tmp_path, FOLDYARD_ENGINE=None)
    assert config.engine() == "docker"


def test_engine_override_must_name_a_known_engine(fresh_config, tmp_path, monkeypatch):
    # `[engine].cli` is REPO config and this value is exec'd host-side by every engine verb, so an
    # unconstrained string would let anything that can write the checkout pick a binary the Mac then
    # runs — the hole closed everywhere else (ADR-0023).
    monkeypatch.delenv("FOLDYARD_ENGINE", raising=False)
    (tmp_path / "foldyard.toml").write_text('[engine]\ncli = "/tmp/evil"\n')
    fresh_config(FOLDYARD_REPO=tmp_path)
    with pytest.raises(SystemExit, match="not one of podman, docker"):
        config.engine()
    (tmp_path / "foldyard.toml").write_text('[engine]\ncli = "docker"\n')
    fresh_config(FOLDYARD_REPO=tmp_path)
    assert config.engine() == "docker"


def test_engine_autodetect_prefers_podman_then_docker(fresh_config, tmp_path, monkeypatch):
    import shutil

    fresh_config(FOLDYARD_REPO=tmp_path, FOLDYARD_ENGINE=None)  # no [engine] table either
    monkeypatch.delenv("DOCKER_HOST", raising=False)  # not the box → pure which() autodetect
    monkeypatch.setattr(shutil, "which", lambda cmd: "/x/podman" if cmd == "podman" else None)
    assert config.engine() == "podman"  # podman is the dependency — prefer it
    monkeypatch.setattr(shutil, "which", lambda cmd: None)
    assert config.engine() == "docker"  # neither present → docker fallback


def test_engine_podman_even_with_docker_host(fresh_config, tmp_path, monkeypatch):
    # podman is the engine even in the dev box (pre-set DOCKER_HOST): foldyard exports
    # CONTAINER_HOST = the same socket so plain podman drives it. docker is NOT preferred
    # just because a docker-compat socket is wired.
    import shutil

    fresh_config(FOLDYARD_REPO=tmp_path, FOLDYARD_ENGINE=None)
    monkeypatch.setenv("DOCKER_HOST", "unix:///var/run/docker.sock")
    monkeypatch.setattr(shutil, "which", lambda cmd: f"/x/{cmd}")  # both present
    assert config.engine() == "podman"


# ── stack naming / ports ──────────────────────────────────────────────────────────────


def test_project_prefix_defaults_to_project_name(fresh_config, tmp_path):
    fresh_config(FOLDYARD_REPO=tmp_path, FOLDYARD_PROJECT="acme", FOLDYARD_PROJECT_PREFIX=None)
    assert config.project_prefix() == "acme"


def test_project_prefix_from_toml(fresh_config, tmp_path):
    (tmp_path / "foldyard.toml").write_text('[project]\nname = "acme"\nprefix = "acme-podman"\n')
    fresh_config(FOLDYARD_REPO=tmp_path, FOLDYARD_PROJECT=None, FOLDYARD_PROJECT_PREFIX=None)
    assert config.project_prefix() == "acme-podman"


def test_app_port_key_is_explicit_and_env_overridable(fresh_config, tmp_path):
    (tmp_path / "foldyard.toml").write_text('[project]\napp_port = "WEB_PORT"\n')
    fresh_config(FOLDYARD_REPO=tmp_path, FOLDYARD_APP_PORT_KEY=None)
    assert config.app_port_key() == "WEB_PORT"
    fresh_config(FOLDYARD_APP_PORT_KEY="FRONTEND_PORT")
    assert config.app_port_key() == "FRONTEND_PORT"


def test_app_port_key_is_optional(fresh_config, tmp_path):
    fresh_config(FOLDYARD_REPO=tmp_path, FOLDYARD_APP_PORT_KEY=None)
    assert config.app_port_key() is None


def test_port_bases_from_toml_else_empty(fresh_config, tmp_path):
    (tmp_path / "foldyard.toml").write_text("[ports]\nAPP_PORT = 3000\nPG_PORT = 5533\n")
    fresh_config(FOLDYARD_REPO=tmp_path)
    assert config.port_bases() == {"APP_PORT": 3000, "PG_PORT": 5533}


def test_port_bases_empty_without_table(fresh_config, tmp_path):
    fresh_config(FOLDYARD_REPO=tmp_path)
    assert config.port_bases() == {}


def test_compose_files_default_under_dev_vm_dir(fresh_config, tmp_path):
    fresh_config(FOLDYARD_REPO=tmp_path, FOLDYARD_DEV_VM_DIR=None)
    assert config.compose_files() == ["./compose.podman.yml"]


def test_compose_files_from_toml(fresh_config, tmp_path):
    (tmp_path / "foldyard.toml").write_text('[project]\ncompose = ["a.yml", "b.yml"]\n')
    fresh_config(FOLDYARD_REPO=tmp_path)
    assert config.compose_files() == ["a.yml", "b.yml"]


def test_machine_name_defaults_to_project(fresh_config, tmp_path):
    fresh_config(FOLDYARD_REPO=tmp_path, FOLDYARD_PROJECT="acme", PODMAN_MACHINE=None)
    assert config.machine_name() == "acme"


def test_inject_specs_parses_array_of_tables(fresh_config, tmp_path):
    (tmp_path / "foldyard.toml").write_text(
        '[[inject]]\naxis = "penpot"\nhost = "truenas.example.ts.net"\n'
        'query_param = "userToken"\ntoken_env = "PENPOT_USER_TOKEN"\n'
    )
    fresh_config(FOLDYARD_REPO=tmp_path)
    specs = config.inject_specs()
    assert specs == [
        {
            "axis": "penpot",
            "host": "truenas.example.ts.net",
            "query_param": "userToken",
            "token_env": "PENPOT_USER_TOKEN",
        }
    ]


def test_secret_specs_parses_array_of_tables(fresh_config, tmp_path):
    (tmp_path / "foldyard.toml").write_text(
        "[[secret]]\n"
        'var = "GH_PEM_B64"\n'
        'label = "GitHub App private key (PEM)"\n'
        'how = "gcloud secrets versions access X | base64"\n'
        "base64 = true\n"
        'when = { github = "app" }\n'
    )
    fresh_config(FOLDYARD_REPO=tmp_path)
    (spec,) = config.secret_specs()
    assert spec["var"] == "GH_PEM_B64" and spec["base64"] is True
    # `how` is DATA foldyard prints at the capture prompt — never a command it runs (executing a
    # repo-declared string host-side is the hole the packaged minters closed).
    assert spec["how"].startswith("gcloud secrets")
    assert spec["when"] == {"github": "app"}


def test_secret_specs_absent_or_malformed_is_empty(fresh_config, tmp_path):
    (tmp_path / "foldyard.toml").write_text("[project]\nname = 'acme'\n")
    fresh_config(FOLDYARD_REPO=tmp_path)
    assert config.secret_specs() == []
    (tmp_path / "foldyard.toml").write_text('secret = "not-a-table-array"\n')
    fresh_config(FOLDYARD_REPO=tmp_path)
    assert config.secret_specs() == []


def test_github_permissions_json_from_toml_or_env(fresh_config, tmp_path, monkeypatch):
    (tmp_path / "foldyard.toml").write_text('[plugins.github]\npermissions = { issues = "read" }\n')
    fresh_config(FOLDYARD_REPO=tmp_path)
    monkeypatch.delenv("GH_APP_PERMISSIONS", raising=False)
    assert config.github_permissions() == '{"issues": "read"}'
    monkeypatch.setenv("GH_APP_PERMISSIONS", '{"contents": "read"}')
    assert config.github_permissions() == '{"contents": "read"}'  # env wins, as everywhere


def test_github_permissions_absent_is_empty_so_the_minter_defaults(fresh_config, tmp_path):
    (tmp_path / "foldyard.toml").write_text('[plugins.github]\napp_id = "1"\n')
    fresh_config(FOLDYARD_REPO=tmp_path)
    assert config.github_permissions() == ""


def test_inject_specs_absent_is_empty(fresh_config, tmp_path):
    (tmp_path / "foldyard.toml").write_text('[project]\nname = "x"\n')
    fresh_config(FOLDYARD_REPO=tmp_path)
    assert config.inject_specs() == []


def test_resnapshot_on_capability_parses_and_drops_malformed(fresh_config, tmp_path):
    # axis → compose services restarted on that axis's capability heal. Malformed values
    # (non-list, empty list) are dropped, never raised — this is read on the supervisor tick.
    (tmp_path / "foldyard.toml").write_text(
        "[resnapshot_on_capability]\n"
        'gcp = ["queue-worker", "graph-api"]\n'
        'bad = "not-a-list"\n'
        "empty = []\n"
    )
    fresh_config(FOLDYARD_REPO=tmp_path)
    assert config.resnapshot_on_capability() == {"gcp": ["queue-worker", "graph-api"]}


def test_resnapshot_on_capability_absent_is_empty(fresh_config, tmp_path):
    (tmp_path / "foldyard.toml").write_text('[project]\nname = "x"\n')
    fresh_config(FOLDYARD_REPO=tmp_path)
    assert config.resnapshot_on_capability() == {}


def test_host_notifications_default_on_and_toml_opt_out(fresh_config, tmp_path):
    (tmp_path / "foldyard.toml").write_text('[project]\nname = "x"\n')
    fresh_config(FOLDYARD_REPO=tmp_path)
    assert config.host_notifications() is True  # push surface on by default
    (tmp_path / "foldyard.toml").write_text("[host]\nnotifications = false\n")
    config.clear_caches()
    assert config.host_notifications() is False


def test_claude_keyless_modes(fresh_config, tmp_path):
    # true is an alias for api-key (the shipped step-1 mode); api-key explicit; an unknown value or
    # the absent key ⇒ off (""). oauth is recognised by config but the plugin gates it off until the
    # proxy value-prefix lands (step 2) — config still reports it so the box-up capture can warn.
    cases = {
        "keyless = true": "api-key",
        'keyless = "api-key"': "api-key",
        'keyless = "oauth"': "oauth",
        'keyless = "nope"': "",
        "": "",
    }
    for line, expected in cases.items():
        (tmp_path / "foldyard.toml").write_text(f'[project]\nname = "x"\n[claude]\n{line}\n')
        fresh_config(FOLDYARD_REPO=tmp_path)
        assert config.claude_keyless() == expected, line


def test_transcript_sync_seconds(fresh_config, tmp_path):
    # An INTERVAL: absent/0/negative and every non-numeric shape read as off, so a config typo can
    # never turn the supervisor's per-tick sweep into a hot loop. `true` is a bool, not 1.
    cases = {
        "transcript_sync_seconds = 30": 30.0,
        "transcript_sync_seconds = 15.5": 15.5,
        "transcript_sync_seconds = 0": 0.0,
        "transcript_sync_seconds = -5": 0.0,
        'transcript_sync_seconds = "30"': 0.0,
        "transcript_sync_seconds = true": 0.0,
        "": 0.0,
    }
    for line, expected in cases.items():
        (tmp_path / "foldyard.toml").write_text(
            f'[project]\nname = "x"\n[claude]\n{line}\n[codex]\n{line}\n'
        )
        fresh_config(FOLDYARD_REPO=tmp_path)
        assert config.claude_transcript_sync_seconds() == expected, line
        assert config.codex_transcript_sync_seconds() == expected, line


def test_transcript_sync_seconds_absent_without_table(fresh_config, tmp_path):
    (tmp_path / "foldyard.toml").write_text('[project]\nname = "x"\n')
    fresh_config(FOLDYARD_REPO=tmp_path)
    assert config.claude_transcript_sync_seconds() == 0.0
    assert config.codex_transcript_sync_seconds() == 0.0


def test_claude_keyless_absent_without_table(fresh_config, tmp_path):
    (tmp_path / "foldyard.toml").write_text('[project]\nname = "x"\n')
    fresh_config(FOLDYARD_REPO=tmp_path)
    assert config.claude_keyless() == ""


def test_codex_keyless_modes(fresh_config, tmp_path):
    # true / "api-key" → api-key; "chatgpt" → chatgpt (subscription); unknown / absent → off.
    cases = {
        "keyless = true": "api-key",
        'keyless = "api-key"': "api-key",
        'keyless = "chatgpt"': "chatgpt",
        'keyless = "nope"': "",
        "": "",
    }
    for line, expected in cases.items():
        (tmp_path / "foldyard.toml").write_text(f'[project]\nname = "x"\n[codex]\n{line}\n')
        fresh_config(FOLDYARD_REPO=tmp_path)
        assert config.codex_keyless() == expected, line
    # No [codex] table at all → off.
    (tmp_path / "foldyard.toml").write_text('[project]\nname = "x"\n')
    fresh_config(FOLDYARD_REPO=tmp_path)
    assert config.codex_keyless() == ""


def test_gcp_project_and_sa_labels_from_toml(fresh_config, tmp_path):
    (tmp_path / "foldyard.toml").write_text(
        '[plugins.gcp-metadata]\nproject = "acme-staging"\n'
        'sa_labels = { app = "app-runtime", box = "log-reader" }\n'
    )
    fresh_config(FOLDYARD_REPO=tmp_path, GCP_PROJECT=None)
    assert config.gcp_project() == "acme-staging"
    assert config.gcp_sa_labels() == {"app": "app-runtime", "box": "log-reader"}


def test_github_app_identity_from_toml(fresh_config, tmp_path):
    (tmp_path / "foldyard.toml").write_text(
        '[plugins.github]\napp_id = "1234567"\ninstallation_id = "12345678"\nrepo = "Tangible"\n'
    )
    fresh_config(FOLDYARD_REPO=tmp_path, GH_APP_ID=None, GH_INSTALLATION_ID=None, GH_REPO=None)
    assert config.github_app_id() == "1234567"
    assert config.github_installation_id() == "12345678"
    assert config.github_repo() == "Tangible"


def test_github_app_identity_env_wins_over_toml(fresh_config, tmp_path):
    (tmp_path / "foldyard.toml").write_text(
        '[plugins.github]\napp_id = "1234567"\ninstallation_id = "12345678"\nrepo = "Tangible"\n'
    )
    fresh_config(FOLDYARD_REPO=tmp_path, GH_APP_ID="9999999")
    assert config.github_app_id() == "9999999"


def test_github_app_identity_absent_is_empty(fresh_config, tmp_path):
    fresh_config(FOLDYARD_REPO=tmp_path, GH_APP_ID=None, GH_INSTALLATION_ID=None, GH_REPO=None)
    assert config.github_app_id() == config.github_installation_id() == config.github_repo() == ""
