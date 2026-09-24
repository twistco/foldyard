"""vscode.py — `foldyard code`: open VS Code attached to the running dev box in an
ISOLATED `--user-data-dir` so the box's podman DOCKER_HOST can never leak into — or merge
with — the user's DEFAULT VS Code singleton. Engine + the `code` CLI are mocked; we assert
the launched argv/env shape and the extensions-config plumbing, never opening a real editor.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from foldyard import config, stack, vscode


class _Proc:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


@pytest.fixture
def fake(tmp_path, monkeypatch):
    """A resolved stack Context + a fake engine/`code`/generator, with subprocess recorded."""
    main = tmp_path / "repo"
    main.mkdir()
    sock = "unix:///var/folders/x/T/podman/tangible-api.sock"
    ctx = stack.Context(
        main=main,
        env={
            "FOLDYARD_CHECKOUT": str(main),
            "PODMAN_PROJECT": "tangible-podman",
            "DOCKER_HOST": sock,
            "CONTAINER_HOST": sock,
        },
        compose=[],
        app="app",
        project="tangible-podman",
        worktree="",
    )
    # `gated` records the gate and the stack resolve in CALL order: the gate must come first.
    gated: list[str] = []
    monkeypatch.setattr(vscode.stack, "resolve", lambda *a, **k: gated.append("resolve") or ctx)
    # The machine gate before resolve: the engine is faked, so it passes (tested below).
    monkeypatch.setattr(vscode.stack, "engine_reachable", lambda *a, **k: True)
    # The ADOPTED config for the checkout — what `fy code` reads `[vscode]` from. Tests mutate
    # `fake["vscode"]` (the table) directly; the tree's own foldyard.toml is never consulted.
    adopted = config.Config(
        repo_root=main,
        worktree="",
        toml={
            "project": {"name": "tangible"},
            "vscode": {
                "extensions": ["anthropic.claude-code", "nefrob.vscode-just-syntax"],
                "settings": {"github.gitAuthentication": False},
            },
        },
    )
    monkeypatch.setattr(vscode.devmode, "worktree_config", lambda wt: adopted)
    pin = {"status": "clean", "exists": True}  # what the gate answers / whether a pin exists
    monkeypatch.setattr(vscode.configpin, "gate", lambda verb: gated.append(verb) or pin["status"])
    monkeypatch.setattr(
        vscode.configpin, "inspect", lambda cfg: SimpleNamespace(pinned_exists=pin["exists"])
    )
    state = tmp_path / "state"
    monkeypatch.setattr(config, "state_dir", lambda: state)
    # The operator's own VS Code User folder, which a new instance is seeded from. Absent unless a
    # test creates it — the suite must never read the real one.
    global_user = tmp_path / "global-user"
    monkeypatch.setattr(vscode, "_global_user_dir", lambda: global_user)
    tools = {
        "code": "/usr/local/bin/code",
        "ssh-agent": "/usr/bin/ssh-agent",
        "ssh-add": "/usr/bin/ssh-add",
    }
    monkeypatch.setattr(vscode.shutil, "which", lambda name: tools.get(name))

    calls: list[dict] = []
    box_state = {"running": False, "installed": ""}

    agent_state = {"rc": 2}  # `ssh-add -l` against the fy agent: 2 = not answering, 1 = empty

    def fake_run(cmd, **kw):
        calls.append({"cmd": cmd, "env": kw.get("env")})
        if cmd[0] == "/usr/bin/ssh-add":
            return _Proc(agent_state["rc"])
        if cmd[0] == "/usr/bin/ssh-agent":
            agent_state["rc"] = 1  # started ⇒ alive and empty from now on
            return _Proc(0)
        if cmd[1] == "ps":  # running probe
            return _Proc(0, "deadbeef\n" if box_state["running"] else "")
        if cmd[1] == "exec":  # ls installed exts / marker reset
            return _Proc(0, box_state["installed"])
        return _Proc(0)  # the `code` launch (and anything else)

    monkeypatch.setattr(vscode.subprocess, "run", fake_run)
    return {
        "calls": calls,
        "state": box_state,
        "adopted": adopted,
        "vscode": adopted.toml["vscode"],
        "gated": gated,
        "pin": pin,
        "agent": agent_state,
        "ctx": ctx,
        "udd": state / "vscode" / "main",
        "sock": sock,
        "global": global_user,
    }


def _launch(calls):
    return [c for c in calls if c["cmd"][0] == "/usr/local/bin/code"]


def _recommend(main: Path, sub: str, ids: list, text: str | None = None) -> None:
    """Write ``<main>/<sub>/.vscode/extensions.json`` — VS Code's own recommendations file, which
    `fy code` must NOT read (it is mount data)."""
    f = main / sub / ".vscode" / "extensions.json"
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(text if text is not None else json.dumps({"recommendations": ids}))


def _settings(fake):
    return fake["udd"] / "User" / "settings.json"


def _execs(calls, *needles):
    return [
        c
        for c in calls
        if c["cmd"][1] == "exec" and all(any(n in t for t in c["cmd"]) for n in needles)
    ]


# ── pure helpers ──────────────────────────────────────────────────────────────────────


def test_uri_is_hex_of_box_name_plus_checkout():
    uri = vscode._uri("tangible-podman-devbox", "/home/vscode/repo")
    assert uri == (
        "vscode-remote://attached-container+"
        + b"tangible-podman-devbox".hex()
        + "/home/vscode/repo"
    )


def test_user_data_dir_is_under_state_not_default(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "state_dir", lambda: tmp_path / "st")
    udd = vscode._user_data_dir()
    assert udd == tmp_path / "st" / "vscode" / "main"
    # globalStorage (where Dev Containers reads attached-container configs) tracks the udd.
    assert vscode._globalstorage(udd).is_relative_to(udd)
    assert "ms-vscode-remote.remote-containers" in str(vscode._globalstorage(udd))


def test_user_data_dirs_are_isolated_between_worktrees(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "state_dir", lambda: tmp_path / "st")
    assert vscode._user_data_dir("one") == tmp_path / "st" / "vscode" / "one"
    assert vscode._user_data_dir("two") == tmp_path / "st" / "vscode" / "two"
    assert vscode._user_data_dir("one") != vscode._user_data_dir("two")


def test_user_data_dir_rejects_pathy_worktree_names(monkeypatch, tmp_path):
    # pathlib's `/` DISCARDS the base for an absolute right-hand side, and `..` walks out of
    # the state root — either would put the isolated instance's state (or an rmtree of it)
    # outside state_dir()/vscode. Reject anything but a single plain component.
    monkeypatch.setattr(config, "state_dir", lambda: tmp_path / "st")
    root = tmp_path / "st" / "vscode"
    for bad in ("/tmp/evil", "../escape", "..", ".", "a/b"):
        with pytest.raises(ValueError):
            vscode._user_data_dir(bad)
    # simple names (and the default) still map straight under the root.
    for ok in ("", "feat", "wt-2"):
        assert vscode._user_data_dir(ok).is_relative_to(root)


def _socket_path(udd) -> str:
    """What VS Code binds for its single-instance lock, at its widest version string."""
    return f"{udd}/1.999-main.sock"


def test_user_data_dir_fits_the_unix_socket_path_cap(monkeypatch):
    # The bug this guards: a 53-char worktree name under ~/.foldyard/<project>/vscode put the
    # socket at 106 chars, over macOS's 103 — VS Code died in claimInstance with an EINVAL that
    # never reached a log file, `code` exited 0, and `fy code` reported "✓ launched".
    monkeypatch.setattr(config, "state_dir", lambda: Path("/Users/dain/.foldyard/tangible"))
    long = "fix-sop-step-error-diagnostics-and-openrouter-routing"
    assert len(long) == 53  # the real name, unshortened, is what blew the cap
    udd = vscode._user_data_dir(long)
    assert len(_socket_path(udd)) <= vscode._SUN_PATH_MAX
    # …and the shortening is what bought that, not a shorter root.
    assert udd.name != long
    assert udd.name.startswith("fix-sop-step-error-diagnostics")


def test_short_worktree_names_are_left_alone(monkeypatch):
    # Only names that would BLOW the cap get rewritten: everyone else keeps the readable dir
    # they already have, so the fix doesn't orphan working state.
    monkeypatch.setattr(config, "state_dir", lambda: Path("/Users/dain/.foldyard/tangible"))
    for name in ("main", "feat", "wt-2", "a" * 49):
        assert vscode._user_data_dir(name).name == name


def test_fitted_leaves_stay_unique_across_a_shared_prefix(monkeypatch):
    # A plain truncation would collapse these two onto ONE user-data-dir — two windows silently
    # sharing instance state. The digest is of the full name, so they stay distinct and stable.
    monkeypatch.setattr(config, "state_dir", lambda: Path("/Users/dain/.foldyard/tangible"))
    a = "feat-checkout-flow-" + "a" * 40
    b = "feat-checkout-flow-" + "b" * 40
    assert vscode._user_data_dir(a) != vscode._user_data_dir(b)
    assert vscode._user_data_dir(a) == vscode._user_data_dir(a)  # stable across runs


def test_a_unicode_worktree_name_is_budgeted_in_BYTES(monkeypatch):
    # `sun_path` is a byte buffer, so the cap is on the ENCODED path. Budgeting in characters let
    # a name of emoji through at up to 4× the real length — the same silent claimInstance death,
    # reached by a name that "fits" on paper. The fitted leaf must also stay decodable, i.e. cut
    # on a character boundary, digest suffix included.
    monkeypatch.setattr(config, "state_dir", lambda: Path("/Users/dain/.foldyard/tangible"))
    name = "🙈" * 30  # 30 chars, 120 bytes
    assert len(name) == 30 and len(name.encode()) == 120
    udd = vscode._user_data_dir(name)
    assert len(_socket_path(udd).encode()) <= vscode._SUN_PATH_MAX
    udd.name.encode().decode()  # no half-written codepoint in the readable head
    assert udd.name != name and udd.name.startswith("🙈")


def test_fitting_stops_once_the_root_alone_has_spent_the_budget(monkeypatch):
    # A deep FOLDYARD_STATE_DIR is a state-dir problem, not a worktree-name one: hashing every
    # leaf couldn't rescue it, so short names must keep their readable directory.
    monkeypatch.setattr(config, "state_dir", lambda: Path("/" + "d" * 120))
    assert vscode._user_data_dir("wt-2").name == "wt-2"


# ── orchestration ─────────────────────────────────────────────────────────────────────


def test_refuses_when_box_not_running(fake, capsys):
    assert vscode.code() == 1
    assert "not running" in capsys.readouterr().err
    assert not _launch(fake["calls"])  # never launched VS Code


def test_worktree_hint_when_not_running(fake, capsys):
    fake["ctx"].worktree = "wt2"
    assert vscode.code() == 1
    assert "WORKTREE=wt2 fy box up" in capsys.readouterr().err


@pytest.mark.parametrize(
    ("worktree", "hint"), [("", "fy box up"), ("wt2", "WORKTREE=wt2 fy box up")]
)
def test_a_stopped_machine_refuses_without_booting_it(fake, monkeypatch, worktree, hint):
    # `stack.resolve()` ENSURES the machine, so reaching it with the VM stopped booted the whole
    # VM only for the box check to refuse. The read-only gate answers first, pointing at the verb
    # that starts both — the VM stays down.
    monkeypatch.setenv("WORKTREE", worktree)
    hints: list = []
    monkeypatch.setattr(vscode.stack, "engine_reachable", lambda _to, start: hints.append(start))
    assert vscode.code() == 1
    assert hints == [hint]
    assert "resolve" not in fake["gated"]
    assert not fake["calls"]


def test_launch_uses_isolated_user_data_dir(fake):
    fake["state"]["running"] = True
    assert vscode.code() == 0
    launch = _launch(fake["calls"])
    assert len(launch) == 1
    argv = launch[0]["cmd"]
    # the WHOLE point: a dedicated --user-data-dir, NOT VS Code's default singleton dir.
    assert argv[argv.index("--user-data-dir") + 1] == str(fake["udd"])
    # Always the checkout FOLDER: one attach shape for a worktree's lifetime, so window-scoped
    # state VS Code saves in one session (Peacock colours, …) is read by the next.
    assert "--file-uri" not in argv
    uri = argv[argv.index("--folder-uri") + 1]
    assert uri.startswith("vscode-remote://attached-container+")
    assert uri.endswith(str(fake["ctx"].main))


def test_launch_stamps_local_terminal_zdotdir_in_isolated_user_settings(fake):
    fake["state"]["running"] = True
    assert vscode.code() == 0
    settings = json.loads(_settings(fake).read_text())
    zdotdir = vscode._local_terminal_zdotdir(fake["udd"])
    terminal_env = settings["terminal.integrated.env.osx"]
    assert terminal_env == {
        "ZDOTDIR": str(zdotdir),
        "FOLDYARD_ZDOTDIR": str(zdotdir),
        "FOLDYARD_LOCAL_TERMINAL_CWD": str(fake["ctx"].main),
        "FOLDYARD_ORIGINAL_ZDOTDIR": "",
    }
    for name in (".zshenv", ".zprofile", ".zshrc", ".zlogin", ".zlogout"):
        assert (zdotdir / name).is_file()
        assert f'"$_fy_original_zdotdir/{name}"' in (zdotdir / name).read_text()
    zshrc = (zdotdir / ".zshrc").read_text()
    assert "if [[ ! -o login ]]" in zshrc
    assert zshrc.index('"$_fy_original_zdotdir/.zprofile"') < zshrc.index(
        '"$_fy_original_zdotdir/.zshrc"'
    )
    assert 'builtin cd -- "$FOLDYARD_LOCAL_TERMINAL_CWD"' in zshrc


def test_local_terminal_env_merge_preserves_settings_and_migrates_old_attempts(fake):
    fake["state"]["running"] = True
    path = _settings(fake)
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps(
            {
                "workbench.colorTheme": "Default Dark Modern",
                "terminal.integrated.profiles.osx": {
                    "Existing": {"path": "/bin/bash"},
                    "Foldyard Local": {"path": "/old/wrapper"},
                },
                "terminal.integrated.defaultProfile.osx": "Foldyard Local",
                "terminal.integrated.env.osx": {"EXISTING": "kept"},
                "terminal.integrated.cwd": "/obsolete/foldyard/value",
            },
            indent=2,
        )
    )
    assert vscode.code() == 0
    settings = json.loads(path.read_text())
    assert settings["workbench.colorTheme"] == "Default Dark Modern"
    assert settings["terminal.integrated.profiles.osx"]["Existing"] == {"path": "/bin/bash"}
    assert "Foldyard Local" not in settings["terminal.integrated.profiles.osx"]
    assert "terminal.integrated.defaultProfile.osx" not in settings
    assert settings["terminal.integrated.env.osx"]["EXISTING"] == "kept"
    assert "terminal.integrated.cwd" not in settings


def test_local_terminal_preserves_original_zdotdir(fake):
    fake["state"]["running"] = True
    fake["ctx"].env["ZDOTDIR"] = "/Users/dain/.config/zsh"
    assert vscode.code() == 0
    settings = json.loads(_settings(fake).read_text())
    assert settings["terminal.integrated.env.osx"]["FOLDYARD_ORIGINAL_ZDOTDIR"] == (
        "/Users/dain/.config/zsh"
    )


def test_invalid_settings_json_is_not_overwritten(fake, capsys):
    fake["state"]["running"] = True
    path = _settings(fake)
    path.parent.mkdir(parents=True)
    original = "{ // jsonc comment that stdlib json will not parse\n}"
    path.write_text(original)
    assert vscode.code() == 0
    # Rewriting would drop the comment, so a commented file is left alone and the step said.
    assert path.read_text() == original
    assert "has comments" in capsys.readouterr().err


def test_settings_json_with_trailing_commas_is_configured(fake):
    # VS Code's settings.json is JSONC: trailing commas are legal there (VS Code writes them itself
    # when an operator edits by hand), and they carry nothing a rewrite could lose.
    fake["state"]["running"] = True
    path = _settings(fake)
    path.parent.mkdir(parents=True)
    path.write_text(
        '{\n  "json.schemaDownload.trustedDomains": {"https://json.schemastore.org/": true,},\n'
        '  "editor.rulers": [100, 120,],\n  "odd.string": "a,}b",\n}\n'
    )
    assert vscode.code() == 0
    settings = json.loads(path.read_text())
    assert settings["json.schemaDownload.trustedDomains"] == {"https://json.schemastore.org/": True}
    assert settings["editor.rulers"] == [100, 120]
    assert settings["odd.string"] == "a,}b"
    assert "ZDOTDIR" in settings["terminal.integrated.env.osx"]


def test_launch_env_carries_docker_host_only_to_isolated_instance(fake):
    fake["state"]["running"] = True
    vscode.code()
    launch = _launch(fake["calls"])[0]
    # the box's podman socket reaches the launched (isolated) VS Code — and only it.
    assert launch["env"]["DOCKER_HOST"] == fake["sock"]


def _cfg_path(fake) -> Path:
    return vscode._globalstorage(fake["udd"]) / "nameConfigs" / "tangible-podman-devbox.json"


def _written(fake) -> dict:
    return json.loads(_cfg_path(fake).read_text())


def test_attached_config_is_built_by_foldyard_from_the_adopted_config(fake):
    # No consumer code runs anywhere, and nothing under the mount is read: the document is
    # foldyard's own, from the ADOPTED `[vscode]` table, and the one host-side write lands under
    # OUR user-data-dir's globalStorage.
    fake["state"]["running"] = True
    assert vscode.code() == 0
    written = _written(fake)
    assert written["_generatedBy"] == vscode._GENERATED_MARKER
    assert written["workspaceFolder"] == str(fake["ctx"].main)
    # `remoteUser` is foldyard's FACT, not a consumer choice: `fy shell`/`fy claude` exec `--user 0`,
    # so an attach as the image's baked user would split file ownership down the middle again.
    assert written["remoteUser"] == "root"
    exts = ["anthropic.claude-code", "nefrob.vscode-just-syntax"]
    assert written["extensions"] == exts
    assert written["settings"]["github.gitAuthentication"] is False
    # Both schemas, so the extensions are INSTALLED whichever Dev Containers version is running.
    assert written["customizations"]["vscode"]["extensions"] == exts
    assert written["customizations"]["vscode"]["settings"] == written["settings"]


def test_the_tree_is_never_the_source_and_drift_is_gated_first(fake):
    # The extensions list decides what the HOST installs (a UI-kind extension installs into the
    # operator's shared ~/.vscode/extensions, for their everyday VS Code too), and a `.vscode/
    # extensions.json` or a `foldyard.toml` under the mount is the box's to write. So the list is
    # config, read from the ADOPTED copy — a box edit is inert until an operator adopts it — and
    # the adopt/revert/ignore gate runs before the write, so the operator meets the drift here.
    main = fake["ctx"].main
    _recommend(main, ".", ["evil.helper"])
    (main / "foldyard.toml").write_text('[vscode]\nextensions = ["evil.helper"]\n')
    fake["state"]["running"] = True
    assert vscode.code() == 0
    assert "evil.helper" not in _cfg_path(fake).read_text()
    assert fake["gated"] == ["fy code", "resolve"]


@pytest.mark.parametrize("status", ["ignored", "unresolved", "adopted", "reverted", "pinned"])
def test_gate_outcomes_with_a_pin_in_place_proceed_on_the_adopted_copy(fake, status):
    # `ignored`/`unresolved` are the operator deferring: the ADOPTED copy stays in force (that is
    # what `worktree_config` returns), exactly as `fy up` proceeds on them. Refusing here would
    # override a deliberate "ignore for now" with nothing gained — the tree is not read either way.
    fake["pin"]["status"] = status
    fake["state"]["running"] = True
    assert vscode.code() == 0
    assert _written(fake)["_generatedBy"] == vscode._GENERATED_MARKER


def test_a_failed_gate_refuses_to_launch(fake, capsys):
    # "error" is the gate not knowing whether the tree drifted — and `effective()` degrades to the
    # working tree on an unreadable state dir, so proceeding could hand the host a tree-chosen
    # extension list. `fy up` keeps going on this (the supervisor reconciles from the pin anyway);
    # `fy code` has no such backstop, so it stops.
    fake["pin"]["status"] = "error"
    fake["state"]["running"] = True
    assert vscode.code() == 1
    assert "couldn't check foldyard.toml" in capsys.readouterr().err
    assert not _cfg_path(fake).exists()
    assert not _launch(fake["calls"])


def test_nothing_adopted_refuses_to_launch(fake, capsys):
    # The gate answers "ignored" when an operator declines the FIRST adoption too — and with no
    # pin, `effective()` falls back to the working tree: the one case where `[vscode]` would be
    # read from the mount. Checked as "does a pin exist", not by status string.
    fake["pin"]["status"] = "ignored"
    fake["pin"]["exists"] = False
    fake["state"]["running"] = True
    assert vscode.code() == 1
    assert "nothing adopted" in capsys.readouterr().err
    assert not _cfg_path(fake).exists()
    assert not _launch(fake["calls"])


def test_extension_ids_are_validated_and_the_attach_extension_dropped(fake):
    # Invalid ids are dropped rather than handed to VS Code; the Dev Containers extension is
    # dropped as meaningless inside the container it attached through; duplicates collapse.
    fake["vscode"]["extensions"] = [
        "biomejs.biome",
        "ms-vscode-remote.remote-containers",
        "../evil",
        "",
        "biomejs.biome",
    ]
    fake["state"]["running"] = True
    assert vscode.code() == 0
    assert _written(fake)["extensions"] == ["biomejs.biome"]


def test_settings_come_from_config_and_keep_the_daemon_port_pin(fake, monkeypatch):
    # The `[vscode.settings]` table is the consumer's lever for machine-scoped settings — the case
    # that motivated it is switching off auto port-forwarding wholesale. The pin still rides
    # alongside: a consumer setting can't turn a daemon port back into a forwardable one.
    monkeypatch.setattr(config, "gcp_minter_port", lambda: 8188)
    fake["vscode"]["settings"] = {
        "remote.autoForwardPorts": False,
        "remote.portsAttributes": {"8188": {"onAutoForward": "notify", "label": "Minter"}},
    }
    fake["state"]["running"] = True
    assert vscode.code() == 0
    settings = _written(fake)["settings"]
    assert settings["remote.autoForwardPorts"] is False
    assert settings["remote.portsAttributes"]["8188"] == {
        "onAutoForward": "ignore",
        "label": "Minter",
    }


def test_a_settings_change_resets_the_machine_settings_marker(fake):
    """Dev Containers writes the attached config's settings into the box's Machine settings ONCE
    per server install (.writeMachineSettingsMarker — the settings twin of the extensions
    marker), so a changed table would otherwise silently never apply to a box that has already
    been attached to. First write, and every change since the last write, clear it.
    The marker alone is NOT enough: the extension also refuses to rewrite an existing
    Machine/settings.json (its source: `markerCreated && !exists(settings.json)`), so the
    rendered file goes with the marker — a consumer sat on two-month-stale Machine settings
    with the marker dutifully reset on every `fy code`."""
    fake["state"]["running"] = True
    fake["state"]["installed"] = "anthropic.claude-code-1.2.3\nnefrob.vscode-just-syntax-0.5.0\n"
    assert vscode.code() == 0
    assert _execs(fake["calls"], "rm -f", ".writeMachineSettingsMarker", "Machine/settings.json")
    # …and the box's server is restarted in the same breath: a reset marker is only read during
    # set-up, and an attach that finds the old server running reconnects and skips set-up (seen
    # live: three attaches, zero of seven extensions installed, until the server was restarted).
    assert _execs(fake["calls"], "rm -f", "n=.vscode-server;", "/proc/[0-9]*", "kill")
    fake["calls"].clear()
    assert vscode.code() == 0  # same settings → the box's copy is current
    assert not _execs(fake["calls"], ".writeMachineSettingsMarker")
    assert not _execs(fake["calls"], "Machine/settings.json")
    # nothing to apply → the running server is left alone (harden.sh's reaper probe is `kill -0`,
    # so match the reset script's own /proc scan, not any `kill`)
    assert not _execs(fake["calls"], "/proc/[0-9]*")
    fake["vscode"]["settings"]["remote.autoForwardPorts"] = False
    assert vscode.code() == 0
    assert _execs(fake["calls"], "rm -f", ".writeMachineSettingsMarker", "Machine/settings.json")


def test_a_failed_marker_reset_leaves_the_config_unwritten_so_the_next_run_retries(
    fake, capsys, monkeypatch
):
    """The written config is the record of what the box holds, so it must be written AFTER the
    markers are gone: written first, a failed `rm -f` leaves a config that says "applied" and
    the next `fy code` diffs against it, sees no change, and the settings never land."""
    fake["state"]["running"] = True
    calls = fake["calls"]
    real = vscode.subprocess.run

    def flaky(cmd, **kw):
        """The fixture's engine, except the marker-reset exec fails."""
        if cmd[1] == "exec" and cmd[3:5] == ["sh", "-c"] and cmd[-1].startswith("rm -f"):
            calls.append({"cmd": cmd, "env": kw.get("env")})
            return _Proc(1)
        return real(cmd, **kw)

    monkeypatch.setattr(vscode.subprocess, "run", flaky)
    assert vscode.code() == 0  # the attach itself still proceeds
    monkeypatch.setattr(vscode.subprocess, "run", real)
    assert not _cfg_path(fake).exists()
    assert "next `fy code` retries" in capsys.readouterr().err
    calls.clear()
    assert vscode.code() == 0  # the reset now succeeds → the config lands, markers were reset
    assert _cfg_path(fake).exists()
    assert _execs(calls, "rm -f", "Machine/settings.json")


# The scan is box-wide, so the tests aim it at a server dir of their own: with the real
# `.vscode-server` a run inside a dev box killed that box's live VS Code server.
_TEST_SERVER_DIR = f".vscode-server-fy-test-{os.getpid()}"


def _run_reset_script(tmp_path, rm_target=None, wait: int = 10):
    """Run the REAL reset script under `sh` and return its exit status. ``PATH`` holds only the
    system dirs of a Debian box — so a script reaching for a tool the packaged image lacks (it
    once called `pgrep`, and there is no procps in there) is caught wherever that tool is absent.
    ``rm_target`` is the path to remove."""
    import subprocess

    target = str(rm_target or tmp_path / "marker")
    return subprocess.run(
        ["sh", "-c", vscode._reset_script(f'"{target}"', _TEST_SERVER_DIR, wait)],
        env={"PATH": "/usr/bin:/bin"},
    ).returncode


def _fake_server(argv0: str = f"/nonexistent/{_TEST_SERVER_DIR}/bin/0123abcd/node"):
    """A `sleep` whose command line reads like the box's VS Code server (argv[0] is only the
    name the child sees; ``executable`` is what runs) — the thing the reset script must find."""
    import subprocess

    return subprocess.Popen([argv0, "60"], executable="/bin/sleep")


@pytest.mark.spawns("sh")  # runs the REAL in-box reset script under sh
def test_reset_script_propagates_rm_failure_and_treats_no_server_as_success(tmp_path):
    """The script's exit status is `_reset_markers`' whole signal. A trailing `true` once made
    every outcome a success — including a failed `rm`, which then wrote a config saying
    "applied" with nothing left to retry. So: `rm` failing fails the script, and finding no
    server is the normal case and passes."""
    (tmp_path / "marker").write_text("")
    assert _run_reset_script(tmp_path) == 0  # marker removed, no server: fine
    assert not (tmp_path / "marker").exists()
    assert _run_reset_script(tmp_path) == 0  # rm -f of a missing file is still fine
    # A directory: `rm -f` refuses it even as root (the box runs tests as root, where a
    # read-only parent dir does not stop an unlink).
    (tmp_path / "a-dir").mkdir()
    assert _run_reset_script(tmp_path, rm_target=tmp_path / "a-dir") == 1


def test_reset_script_needs_no_procps():
    """The packaged box image has no procps: a `pgrep` in the script exited 127, `fy code`
    reported the reset failed, and no extension or setting ever landed on a packaged box."""
    script = vscode._reset_script('"/x"')
    for tool in ("pgrep", "pkill", "pidof", "ps "):
        assert tool not in script, tool
    assert "n=.vscode-server;" in script  # the real server dir by default (tests aim it away)


# The scan reads /proc; where there is none (a macOS host running the suite) it finds nothing,
# so these two would prove nothing — skipped, while the procps check above runs everywhere.
_needs_proc = pytest.mark.skipif(
    not Path("/proc/self/cmdline").exists(), reason="the server scan reads /proc (Linux)"
)


@_needs_proc
@pytest.mark.spawns("sh", "/bin/sleep")  # the REAL reset script under sh; a sleep as the "server"
def test_reset_script_kills_the_server_and_nothing_else(tmp_path):
    """A process whose command line names the server is killed — and is already GONE when the
    script returns (`fy code` attaches right after, and an attach that still finds the old server
    reconnects to it, skipping set-up). An unrelated process is left alone, and the script's own
    `sh -c` (whose text mentions the server dir) never matches itself."""
    import os
    import signal

    server = _fake_server()
    bystander = _fake_server(f"/nonexistent/{_TEST_SERVER_DIR}-not/bin/node")
    try:
        assert _run_reset_script(tmp_path) == 0
        # No polling here: the script's own wait is the property. (poll() reaps the zombie —
        # the script counted it as gone, since it had exited.)
        assert server.poll() is not None, "the script returned before the server exited"
        assert bystander.poll() is None, "a process that is not the server was killed"
    finally:
        for proc in (server, bystander):
            if proc.poll() is None:
                os.kill(proc.pid, signal.SIGKILL)
            proc.wait()


@_needs_proc
@pytest.mark.spawns("sh")  # the REAL reset script under sh; a TERM-ignoring sh as the "server"
def test_reset_script_fails_when_the_server_outlives_the_wait(tmp_path):
    """Signalled is not gone: a server still running after the wait fails the script, so
    `_reset_markers` reports it and the next `fy code` retries instead of attaching to it."""
    import os
    import signal
    import subprocess
    import time

    ready = tmp_path / "trapped"
    # The trailing `:` keeps the shell itself running (dash execs a -c script's LAST command in
    # place, which would swap this command line for plain `sleep 30`), and `ready` is written
    # only once the trap is in, so the TERM can't land first.
    stubborn = subprocess.Popen(
        [
            f"/nonexistent/{_TEST_SERVER_DIR}/bin/0123abcd/node",
            "-c",
            f"trap '' TERM; : > '{ready}'; sleep 30; :",
        ],
        executable="/bin/sh",
        env={"PATH": "/usr/bin:/bin"},  # the suite's scrubbed PATH has no `sleep`
        start_new_session=True,  # its `sleep` child goes with it in the cleanup below
    )
    for _ in range(100):
        if ready.exists():
            break
        time.sleep(0.05)
    try:
        assert _run_reset_script(tmp_path, wait=1) == 1
        assert stubborn.poll() is None  # still running: exactly what the failure reports
    finally:
        os.killpg(stubborn.pid, signal.SIGKILL)
        stubborn.wait()


def test_user_owned_config_is_never_clobbered(fake, capsys):
    # Removing the `_generatedBy` marker is how you take ownership of the file; we then leave it
    # (and skip the marker resets, since we didn't change what's configured).
    fake["state"]["running"] = True
    path = _cfg_path(fake)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('{"extensions": ["mine.only"]}')
    assert vscode.code() == 0
    assert json.loads(path.read_text()) == {"extensions": ["mine.only"]}
    assert "kept your customised" in capsys.readouterr().out
    assert not _execs(fake["calls"], "Marker")


@pytest.mark.parametrize(
    "text",
    [
        '{"extensions": ["mine.only",],}',
        '{\n  // my own list\n  "extensions": ["mine.only"] /* keep */\n}',
    ],
)
def test_a_user_owned_jsonc_config_is_never_clobbered(fake, capsys, text):
    # The operator edits this file in VS Code, which accepts JSONC — a trailing comma or a comment
    # there is still THEIR file, not "garbage, safe to replace".
    fake["state"]["running"] = True
    path = _cfg_path(fake)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    assert vscode.code() == 0
    assert path.read_text() == text
    assert "kept your customised" in capsys.readouterr().out


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ('{"a": 1,}', ({"a": 1}, False)),
        ('{"a": [1, 2,\n],\n}', ({"a": [1, 2]}, False)),
        ('{"u": "https://x.org/*y*/"}', ({"u": "https://x.org/*y*/"}, False)),
        ('{"q": "say \\"hi\\", }", }', ({"q": 'say "hi", }'}, False)),
        ('{"a": 1, // note\n}', ({"a": 1}, True)),
        ('{"a": /* x */ 1 /* y */,}', ({"a": 1}, True)),
    ],
)
def test_loads_jsonc(text, expected):
    assert vscode._loads_jsonc(text) == expected


@pytest.mark.parametrize("text", ['{"a": 1,,}', "[,]", '{"a": 1 /* unterminated'])
def test_loads_jsonc_still_rejects_malformed(text):
    with pytest.raises(ValueError):
        vscode._loads_jsonc(text)


def test_a_foreign_marker_is_someone_elses_file_too(fake, capsys):
    # Ownership is "the marker is OURS", not "a marker exists": another tool stamping its own
    # `_generatedBy` owns the file just as much as a user who removed ours.
    fake["state"]["running"] = True
    path = _cfg_path(fake)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('{"_generatedBy": "some other tool", "extensions": ["theirs.ext"]}')
    assert vscode.code() == 0
    assert json.loads(path.read_text())["extensions"] == ["theirs.ext"]
    assert "kept your customised" in capsys.readouterr().out


def test_an_unreadable_existing_config_is_kept_not_replaced(fake, capsys, monkeypatch):
    # Malformed is "nothing to preserve"; UNREADABLE is "we don't know whose this is" — and the
    # answer to "may I overwrite a file I can't inspect?" is no, or a permissions blip on the
    # operator's own customised config would silently revert it to foldyard's.
    fake["state"]["running"] = True
    path = _cfg_path(fake)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('{"extensions": ["mine.only"]}')
    real_read_text = Path.read_text

    def refuse(self, *a, **kw):
        if self == path:
            raise PermissionError(13, "Permission denied", str(self))
        return real_read_text(self, *a, **kw)

    monkeypatch.setattr(Path, "read_text", refuse)
    assert vscode.code() == 0
    monkeypatch.undo()
    assert json.loads(path.read_text()) == {"extensions": ["mine.only"]}
    assert "could not read it" in capsys.readouterr().out
    assert not _execs(fake["calls"], "Marker")


@pytest.mark.parametrize("junk", ["null", "[1, 2]", "not json at all"])
def test_a_malformed_existing_config_is_replaced_not_a_traceback(fake, junk):
    # `null` parses fine and then answers every lookup with a TypeError — the one shape that used
    # to escape `fy code` as a stack trace rather than a replaced file.
    fake["state"]["running"] = True
    path = _cfg_path(fake)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(junk)
    assert vscode.code() == 0
    assert _written(fake)["_generatedBy"] == vscode._GENERATED_MARKER


def test_the_in_box_harden_is_ensured_before_the_attach(fake):
    # The attach forwards the host's SSH agent + git credentials into the box; the in-box hygiene
    # (box._HARDEN_SNIPPET: unset vars, reap the sockets) must be in place and its reaper running
    # BEFORE VS Code's server lands — so the exec comes before the `code` launch, every time.
    fake["state"]["running"] = True
    assert vscode.code() == 0
    cmds = [c["cmd"] for c in fake["calls"]]
    harden = [i for i, c in enumerate(cmds) if "exec" in c and "harden.sh" in c[-1]]
    launch = [i for i, c in enumerate(cmds) if c[0] == "/usr/local/bin/code"]
    assert harden and launch and harden[0] < launch[0]


def test_vscode_is_launched_with_foldyards_empty_agent_not_the_operators(fake, monkeypatch):
    # The attach forwards whatever SSH_AUTH_SOCK the VS Code process holds, verbatim (shown live:
    # the process keeps the launch env's value; only an UNSET var makes the extension find the
    # host's real agent). So the launch hands it foldyard's own agent, holding nothing — what
    # reaches the box is empty by construction, no race to win.
    monkeypatch.setenv("SSH_AUTH_SOCK", "/private/tmp/com.apple.launchd.x/Listeners")
    fake["state"]["running"] = True
    assert vscode.code() == 0
    launch = _launch(fake["calls"])[0]
    sock = str(fake["udd"] / "fy-empty-agent.sock")
    assert launch["env"]["SSH_AUTH_SOCK"] == sock
    started = [c["cmd"] for c in fake["calls"] if c["cmd"][0] == "/usr/bin/ssh-agent"]
    assert started == [["/usr/bin/ssh-agent", "-a", sock]]


def test_a_live_empty_agent_is_reused_not_restarted(fake):
    fake["agent"]["rc"] = 1  # already answering, no identities
    fake["state"]["running"] = True
    assert vscode.code() == 0
    assert not [c for c in fake["calls"] if c["cmd"][0] == "/usr/bin/ssh-agent"]
    assert _launch(fake["calls"])[0]["env"]["SSH_AUTH_SOCK"].endswith("fy-empty-agent.sock")


def test_an_agent_that_holds_identities_refuses_the_launch(fake, capsys):
    # Someone `ssh-add`ed a key into the fy agent. Forwarding THAT is the leak this exists to
    # prevent, so the launch stops and says how to empty it.
    fake["agent"]["rc"] = 0  # identities listed
    fake["state"]["running"] = True
    assert vscode.code() == 1
    assert "holds identities" in capsys.readouterr().err
    assert not _launch(fake["calls"])


def test_the_git_credential_bridge_is_switched_off_at_both_scopes(fake):
    # `git.terminalAuthentication` off means the git extension never sets GIT_ASKPASS in a box
    # terminal — the bridge is not installed, rather than removed after the fact. Pinned in the
    # instance's own settings (always written) and in the attached config (machine scope).
    fake["state"]["running"] = True
    assert vscode.code() == 0
    for doc in (json.loads(_settings(fake).read_text()), _written(fake)["settings"]):
        assert doc["git.terminalAuthentication"] is False
        assert doc["git.useIntegratedAskPass"] is False


def test_a_failed_harden_refuses_the_attach(fake, monkeypatch, capsys):
    # If the in-box hygiene can't be applied, attaching would forward host credentials into an
    # unguarded box — so it doesn't.
    from foldyard import box as box_mod

    monkeypatch.setattr(box_mod, "ensure_harden", lambda *a: False)
    fake["state"]["running"] = True
    assert vscode.code() == 1
    assert "not attaching" in capsys.readouterr().err
    assert not _launch(fake["calls"])


def test_resets_install_marker_when_extensions_missing(fake):
    fake["state"]["running"] = True
    fake["state"]["installed"] = "anthropic.claude-code-1.2.3\n"  # just-syntax is NOT installed
    assert vscode.code() == 0
    assert _execs(fake["calls"], "rm -f", ".installExtensionsMarker")  # marker cleared


def test_no_marker_reset_when_everything_is_installed(fake):
    fake["state"]["running"] = True
    fake["state"]["installed"] = "anthropic.claude-code-1.2.3\nnefrob.vscode-just-syntax-0.5.0\n"
    assert vscode.code() == 0
    assert not _execs(fake["calls"], ".installExtensionsMarker")


def test_errors_when_code_not_on_path(fake, monkeypatch, capsys):
    fake["state"]["running"] = True
    monkeypatch.setattr(vscode.shutil, "which", lambda name: None)
    assert vscode.code() == 1
    err = capsys.readouterr().err
    assert "isn't on your PATH" in err
    assert "Attach to Running Container" in err
    assert not _launch(fake["calls"])


def test_refuses_inside_the_box_before_doing_anything(fake, monkeypatch, capsys):
    # In-box there's no host bridge: say so and point at the host — FIRST. The refusal used to sit
    # at the `code` lookup, after the gate, the resolve, a config write and the marker reset,
    # which from in here execs into this very box (its own markers deleted, its own VS Code
    # server stopped under an attached window) — and with `code` on PATH it was never reached.
    fake["state"]["running"] = True
    monkeypatch.setattr(config, "in_box", lambda: True)
    assert vscode.code() == 1
    err = capsys.readouterr().err
    assert "has to run on your computer" in err and "`fy code`" in err
    assert "Install it" not in err
    assert fake["gated"] == []  # no gate, no stack resolve
    assert fake["calls"] == []  # no engine call at all: no probe, no exec, no launch
    assert not fake["udd"].exists()  # nothing written under the (box's) home


def test_the_in_box_refusal_names_the_worktree(fake, monkeypatch, capsys):
    monkeypatch.setattr(config, "in_box", lambda: True)
    monkeypatch.setenv("WORKTREE", "feat-x")
    assert vscode.code() == 1
    assert "`WORKTREE=feat-x fy code`" in capsys.readouterr().err


def test_refuses_when_vscode_table_absent(fake, monkeypatch, capsys):
    # `[vscode]` is the opt-in that mounts the vscode-server volume; without it the attach would
    # work once and re-download the server on every box recreation — so refuse with the fix.
    del fake["adopted"].toml["vscode"]
    assert vscode.code() == 1
    assert "[vscode] table" in capsys.readouterr().err
    assert not _launch(fake["calls"])


# ── host-daemon ports are never auto-forwarded ────────────────────────────────────────


def test_host_daemon_ports_are_never_auto_forwarded(monkeypatch):
    """VS Code's auto port-forwarding BINDS the port it forwards on the host's loopback, and a
    loopback bind beats the daemon's wildcard one for connections to 127.0.0.1 — so forwarding a
    port the box DIALS OUT to silently shadows the daemon behind it. Seen live: VS Code read
    `[minter] :8188` out of `fy host`'s own startup line, forwarded 8188, and every mint from the
    box then hung with TCP connecting and no HTTP ever returning. The proxy port is the same
    hazard with a worse blast radius (all egress), so both are pinned."""
    monkeypatch.setattr(config, "gcp_minter_port", lambda: 8188)
    monkeypatch.setattr(config, "proxy_port", lambda: 8088)

    cfg = vscode._attached_config("/repo", [], {"editor.fontSize": 12})

    attrs = cfg["settings"]["remote.portsAttributes"]
    assert attrs["8188"]["onAutoForward"] == "ignore"
    assert attrs["8088"]["onAutoForward"] == "ignore"
    # Scoped, not blunt: the consumer's own settings survive and other ports stay forwardable
    # unless the consumer says otherwise (`[vscode.settings]`).
    assert cfg["settings"]["editor.fontSize"] == 12
    assert "remote.autoForwardPorts" not in cfg["settings"]


def test_daemon_port_pin_survives_a_consumer_that_sets_its_own(monkeypatch):
    """The pin is a host-consequence guard, so it wins over the config — same rule as
    workspaceFolder. A table re-enabling the very port that shadows the minter would reintroduce
    a failure that presents as a dead credential path, not as a VS Code setting."""
    monkeypatch.setattr(config, "gcp_minter_port", lambda: 8188)
    monkeypatch.setattr(config, "proxy_port", lambda: 8088)

    cfg = vscode._attached_config(
        "/repo",
        [],
        {
            "remote.portsAttributes": {
                "8188": {"label": "Minter", "onAutoForward": "notify", "protocol": "http"}
            }
        },
    )

    pinned = cfg["settings"]["remote.portsAttributes"]["8188"]
    assert pinned["onAutoForward"] == "ignore"
    # The consumer's other attributes for that port are their business and survive.
    assert pinned["label"] == "Minter" and pinned["protocol"] == "http"


def test_daemon_port_pin_is_applied_when_config_has_no_settings(monkeypatch):
    """No `[vscode.settings]` is the common case — the pin has to create the settings object, or
    the protection only exists for consumers who happen to declare settings."""
    monkeypatch.setattr(config, "gcp_minter_port", lambda: 8188)
    monkeypatch.setattr(config, "proxy_port", lambda: 8088)

    cfg = vscode._attached_config("/repo", [], {})

    assert cfg["settings"]["remote.portsAttributes"]["8188"]["onAutoForward"] == "ignore"


def test_published_ports_are_never_auto_forwarded(monkeypatch):
    """The mirror hazard: a port the host PUBLISHES into the stack. Under podman machine a publish
    of `127.0.0.1:P` is a gvproxy bind on the host's loopback, so once VS Code has auto-forwarded
    P (it read `127.0.0.1:4400->4400/tcp` out of a `ps` line while the stack was down after a
    reboot) the container can never start: `listen tcp 127.0.0.1:4400: bind: address already in
    use`, on every `up`, until someone finds the Ports view. Pinned as the RANGE a worktree
    offset can land on — the main instance forwarded a worktree's `APP_PORT+1` the same way."""
    monkeypatch.setattr(config, "gcp_minter_port", lambda: 8188)
    monkeypatch.setattr(config, "proxy_port", lambda: 8088)
    monkeypatch.setattr(config, "port_bases", lambda: {"APP_PORT": 3000, "SIM_PORT": 4400})

    clean = vscode._attached_config("/repo", [], {"editor.fontSize": 12})

    attrs = clean["settings"]["remote.portsAttributes"]
    assert attrs["3000-3089"]["onAutoForward"] == "ignore"
    assert attrs["4400-4489"]["onAutoForward"] == "ignore"
    assert attrs["8188"]["onAutoForward"] == "ignore"
    assert clean["settings"]["editor.fontSize"] == 12
    assert "remote.autoForwardPorts" not in clean["settings"]


def test_published_port_pin_is_absent_without_a_ports_table(monkeypatch):
    """A consumer with no ``[ports]`` publishes nothing, so there is nothing to guard."""
    monkeypatch.setattr(config, "gcp_minter_port", lambda: 8188)
    monkeypatch.setattr(config, "proxy_port", lambda: 8088)
    monkeypatch.setattr(config, "port_bases", lambda: {})

    clean = vscode._attached_config("/repo", [], {})

    assert set(clean["settings"]["remote.portsAttributes"]) == {"8188", "8088"}


def test_instance_settings_pin_daemon_ports(fake, monkeypatch):
    """The user-data-dir settings.json is written on every `fy code`, so it carries the pins that
    must not depend on the attached config still being foldyard-owned. `update.mode` is NOT one of
    them: it is the operator's preference, and arrives with the seed from their own settings."""
    monkeypatch.setattr(config, "gcp_minter_port", lambda: 8188)
    monkeypatch.setattr(config, "proxy_port", lambda: 8088)
    fake["state"]["running"] = True

    assert vscode.code() == 0

    settings = json.loads(_settings(fake).read_text())
    assert "update.mode" not in settings
    attrs = settings["remote.portsAttributes"]
    assert attrs["8188"]["onAutoForward"] == "ignore"
    assert attrs["8088"]["onAutoForward"] == "ignore"
    # The pins merge — they must not cost the local-terminal wiring this file exists for.
    assert vscode._TERMINAL_ENV_OSX in settings


def _global(fake, settings: str | None = None) -> Path:
    """Populate the fake operator's VS Code User folder."""
    g = fake["global"]
    (g / "snippets").mkdir(parents=True)
    if settings is not None:
        (g / "settings.json").write_text(settings)
    (g / "keybindings.json").write_text('[ // mine\n  {"key": "cmd+k", "command": "x"},\n]\n')
    (g / "snippets" / "python.json").write_text('{"p": {"body": "print()"}}')
    return g


def test_a_new_instance_is_seeded_from_the_operators_vscode(fake, capsys):
    # The isolated instance shares nothing with the operator's own VS Code but the extensions
    # folder, so without a seed every worktree starts from factory defaults. The seed is theirs
    # (update.mode included); the pins still apply on top.
    fake["state"]["running"] = True
    _global(fake, '{\n  // mine\n  "editor.fontSize": 15,\n  "update.mode": "manual",\n}\n')

    assert vscode.code() == 0

    settings = json.loads(_settings(fake).read_text())
    assert settings["editor.fontSize"] == 15
    assert settings["update.mode"] == "manual"
    assert settings["git.useIntegratedAskPass"] is False
    assert vscode._TERMINAL_ENV_OSX in settings
    user = fake["udd"] / "User"
    assert (user / "keybindings.json").read_text() == (
        fake["global"] / "keybindings.json"
    ).read_text()
    assert (user / "snippets" / "python.json").exists()
    assert "seeded" in capsys.readouterr().out


def test_the_seed_drops_settings_that_point_at_another_engine(fake):
    # A global Dev Containers / Docker setting naming Docker Desktop's socket would send the attach
    # to the wrong engine — the leak the isolated instance exists to prevent.
    fake["state"]["running"] = True
    _global(
        fake,
        json.dumps(
            {
                "dev.containers.dockerSocketPath": "/Users/me/.docker/run/docker.sock",
                "dev.containers.dockerPath": "docker",
                "remote.containers.dockerPath": "docker",
                "docker.environment": {"DOCKER_HOST": "unix:///elsewhere.sock"},
                "docker.host": "unix:///elsewhere.sock",
                "docker.context": "desktop-linux",
                "containers.environment": {"DOCKER_HOST": "unix:///elsewhere.sock"},
                "containers.containerClient": "com.microsoft.visualstudio.containers.docker",
                "editor.fontSize": 15,
            }
        ),
    )

    assert vscode.code() == 0

    settings = json.loads(_settings(fake).read_text())
    assert settings["editor.fontSize"] == 15
    assert not [k for k in settings if k.startswith(("dev.containers", "remote.containers"))]
    assert not [k for k in settings if k.startswith(("docker.", "containers."))]


def test_an_existing_instance_is_never_reseeded(fake):
    # Seeding happens once, when the instance is created; after that its settings are its own
    # (edited in that window) and a later global change must not overwrite them.
    fake["state"]["running"] = True
    _global(fake, '{"editor.fontSize": 15}')
    path = _settings(fake)
    path.parent.mkdir(parents=True)
    path.write_text('{"editor.fontSize": 11}')

    assert vscode.code() == 0

    assert json.loads(path.read_text())["editor.fontSize"] == 11
    assert not (fake["udd"] / "User" / "keybindings.json").exists()


def test_an_unparseable_global_settings_still_seeds_the_rest(fake, capsys):
    fake["state"]["running"] = True
    _global(fake, '{"editor.fontSize": }')

    assert vscode.code() == 0

    assert "editor.fontSize" not in json.loads(_settings(fake).read_text())
    assert (fake["udd"] / "User" / "keybindings.json").exists()
    assert "couldn't seed" in capsys.readouterr().err


def test_no_operator_vscode_seeds_nothing(fake):
    fake["state"]["running"] = True

    assert vscode.code() == 0

    assert not (fake["udd"] / "User" / "keybindings.json").exists()
    assert "update.mode" not in json.loads(_settings(fake).read_text())
