"""box.py — `foldyard box build|up|shell|down|ps`. The engine is mocked (golden command
sequences): we assert the assembled `<engine> run`/`build`/`exec` shapes — incl. the
config-driven [box] bits + the plugin box env/mounts — without creating a real box."""

from __future__ import annotations

import shlex
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

import foldyard
from foldyard import box, config, stack, supervisor


class _Proc:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


@pytest.fixture
def fake(tmp_path, monkeypatch):
    """A resolved box context + [box] config, with subprocess.run recorded/dispatched."""
    main = tmp_path / "repo"
    main.mkdir()
    ctx = stack.Context(
        main=main,
        env={
            "FOLDYARD_CHECKOUT": str(main),
            "FOLDYARD_ENV_OVERRIDE": str(main / "sim.env"),
            "HERE": "dev-stack",
        },
        compose=[],
        app="app",
        project="tangible-podman",
        worktree="",
    )
    monkeypatch.setattr(box.stack, "resolve", lambda *a, **k: ctx)
    monkeypatch.setattr(
        config, "box_image", lambda: {"dockerfile": "dev-stack/box.Dockerfile", "tag": "img:tag"}
    )
    monkeypatch.setattr(box, "_box_image_fingerprint", lambda main: "expected-fingerprint")
    monkeypatch.setattr(
        config, "box_shadow_volumes", lambda: ["data/.venv", "platform/node_modules"]
    )
    monkeypatch.setattr(
        config,
        "box_caches",
        lambda: [
            {"volume": "tangible-podman-shared-pnpm", "path": ".local/share/pnpm/store"},
            {"volume": "cache", "path": ".cache"},
        ],
    )
    monkeypatch.setattr(
        config, "box_warmup", lambda: [{"dir": "platform", "run": "pnpm install --frozen-lockfile"}]
    )
    monkeypatch.setattr(
        config,
        "box_env",
        lambda: {"UV_LINK_MODE": "copy", "npm_config_store_dir": "~/.local/share/pnpm/store"},
    )
    monkeypatch.setattr(config, "box_sock_in_vm", lambda: "/run/docker.sock")
    # Stack-less by default (no compose file present) — the packaged generic-box path, where
    # box up must create `{project}_default` itself. The compose-project case opts in per test.
    monkeypatch.setattr(config, "compose_files", lambda: [])
    # Compose owns the network by default; the external_network consumer opts in per test.
    monkeypatch.setattr(config, "external_network", lambda: False)
    monkeypatch.setattr(config, "port_bases", lambda: {"APP_PORT": 3000, "PG_PORT": 5533})
    monkeypatch.setattr(config, "in_box", lambda: False)
    monkeypatch.setenv("DEVBOX_LOG_SA", "box@p.iam.gserviceaccount.com")  # avoid real toml
    # gcp configured → the gcp plugin LOADS (declared) and its SA label is baked in (project set).
    monkeypatch.setattr(config, "gcp_metadata_declared", lambda: True)
    monkeypatch.setattr(config, "gcp_project", lambda: "p")
    monkeypatch.setattr(config, "proxy_enabled", lambda: False)  # proxy off by default; opt in/test
    monkeypatch.setattr(config, "machine_wall", lambda: False)  # no in-VM wall; opt in per test
    # foldyard self-install resolution is exercised in its own tests; stub it here so the golden
    # sequences don't build a real wheel / probe the host's foldyard install.
    monkeypatch.setattr(
        box, "_foldyard_install_subst", lambda checkout, here: {"fy_wheel": "", "fy_version": ""}
    )
    monkeypatch.delenv("DEVBOX_SKIP_DEPS", raising=False)
    monkeypatch.delenv("DEVBOX_TRANSCRIPTS", raising=False)
    # No ambient proxy CA by default: point MITMPROXY_CA at a missing file so the golden-sequence
    # tests don't pick up the real ~/.mitmproxy CA a dev machine may have. Opt in per test.
    monkeypatch.setenv("MITMPROXY_CA", str(tmp_path / "no-such-ca.pem"))
    # Your computer's clock settings are stubbed absent (never the real /etc/localtime or the
    # operator's LANG); the timezone tests opt in.
    monkeypatch.setattr(box.hostclock, "zone", lambda: None)
    monkeypatch.setattr(box.hostclock, "time_locale", lambda: None)

    # box up ensures the host supervisor (always-on egress proxy) is running; stub it so the golden
    # sequences don't probe/spawn the real Mac daemons, and record that box up asked for it.
    hosted: list[bool] = []
    monkeypatch.setattr(supervisor, "ensure_background", lambda: hosted.append(True) or None)
    # `box.main("up")` runs the host preflight (backend CLI / proxy prereqs) before assembling the
    # run — these golden tests aren't its subject and the box test env has no real podman, so neuter
    # it here (test_preflight.py covers the checks; test_up_runs_preflight covers the wiring).
    from foldyard import preflight

    monkeypatch.setattr(preflight, "check_or_abort", lambda *a, **k: None)

    calls: list[list[str]] = []
    state: dict[str, Any] = {
        "running": False,
        "exists": False,
        "img": False,
        "image_fingerprint": None,
        "box_fingerprint": "expected-fingerprint",
        "net_exists": True,
        "baked_proxy_port": None,  # set to a string to simulate a box's baked FY_PROXY_PORT
        "baked_env": {},  # extra frozen Config.Env entries for the running box (_baked_env)
        # What `foldyard --version` answers inside the running box (_installed_foldyard):
        # None → whatever the host runs (no drift); "" → nothing answers.
        "box_foldyard": None,
        "oci_runtime": "crun",  # what `inspect {{.OCIRuntime}}` reports after create
        "image_locales": [],  # what `locale -a` lists in the image (the create-time probe)
        "image_lc_all": "",  # the image's own LC_ALL, as the same probe reads it
    }

    envs: list[dict | None] = []

    def fake_run(cmd, **kw):
        calls.append(cmd)
        envs.append(kw.get("env"))
        if cmd[1] == "inspect" and "{{.OCIRuntime}}" in cmd:
            return _Proc(0, state["oci_runtime"] + "\n")
        if cmd[1] == "ps" and ("-q" in cmd or "-aq" in cmd):
            # -q + status=running → the running probe; -aq → the exists probe.
            present = state["running"] if "status=running" in cmd else state["exists"]
            return _Proc(0, "deadbeef\n" if present else "")
        if cmd[1] == "inspect" and box._BOX_FINGERPRINT_LABEL in " ".join(cmd):
            value = state["box_fingerprint"]
            return _Proc(0, f"{value}\n" if value else "")
        if cmd[1] == "inspect":  # _baked_env reads the container's frozen Config.Env
            baked = dict(state["baked_env"])
            if state["baked_proxy_port"] is not None:
                baked["FY_PROXY_PORT"] = state["baked_proxy_port"]
            lines = "".join(f"{k}={v}\n" for k, v in baked.items())
            return _Proc(0, lines + "PATH=/usr/bin\n" if baked else "")
        if cmd[1] == "image":
            value = state["image_fingerprint"]
            return _Proc(0 if state["img"] else 1, f"{value}\n" if value else "")
        if cmd[1] == "network" and cmd[2] == "inspect":
            return _Proc(0 if state["net_exists"] else 1)
        if cmd[1:3] == ["run", "--rm"]:
            listed = "".join(f"\n{name}" for name in state["image_locales"])
            lc_all = f"\nlc_all={state['image_lc_all']}"
            return _Proc(0, "/home/vscode /home/vscode/.claude" + lc_all + listed)
        if cmd[1] == "exec" and cmd[-1] == "foldyard --version":
            v = state["box_foldyard"]
            v = foldyard.__version__ if v is None else v
            return _Proc(0 if v else 127, f"{v}\n" if v else "")
        return _Proc(0)

    monkeypatch.setattr(box.subprocess, "run", fake_run)
    # down/ps gate on a reachable engine before resolving (never provision a VM to tear down);
    # the golden tests fake the engine, so the gate passes. The gating itself is tested below.
    monkeypatch.setattr(stack, "engine_reachable", lambda *a, **k: True)
    # box down clears the checkout's posture mirror — point it at a tmp file so these tests
    # never unlink a real checkout's .dev-mode.json (and the removal is assertable).
    mirror = tmp_path / "dev-mode-mirror.json"
    monkeypatch.setattr(config, "mirror_file", lambda: mirror)
    return {
        "calls": calls,
        "envs": envs,
        "state": state,
        "main": main,
        "ctx": ctx,
        "hosted": hosted,
        "mirror": mirror,
    }


def _find(calls, *, has):
    return [c for c in calls if all(tok in c for tok in has)]


def test_up_runs_preflight_before_assembling(fake, monkeypatch):
    # `fy box up` must consult the preflight BEFORE _ctx()/run assembly — a failing prereq aborts
    # with no engine calls, instead of building a box that can't reach egress. (Fixture neutralises
    # preflight by default; re-arm it to raise here.)
    from foldyard import preflight

    def boom(context):
        raise SystemExit(1)

    monkeypatch.setattr(preflight, "check_or_abort", boom)
    with pytest.raises(SystemExit):
        box.main("up")
    assert fake["calls"] == []  # aborted before any engine command


# ── build ─────────────────────────────────────────────────────────────────────────────


def test_box_image_fingerprint_tracks_dockerfile_and_build_shape(tmp_path, monkeypatch):
    dockerfile = tmp_path / "box.Dockerfile"
    dockerfile.write_text("FROM alpine\n")
    image = {
        "dockerfile": "box.Dockerfile",
        "tag": "img:tag",
        "target": "dev",
        "build_args": {"TOOL": "one"},
    }
    monkeypatch.setattr(config, "box_image", lambda: image)
    first = box._box_image_fingerprint(tmp_path)

    dockerfile.write_text("FROM debian\n")
    assert box._box_image_fingerprint(tmp_path) != first

    dockerfile.write_text("FROM alpine\n")
    image["build_args"] = {"TOOL": "two"}
    assert box._box_image_fingerprint(tmp_path) != first


def test_build_command_shape(fake):
    assert box.main("build") == 0
    build = _find(fake["calls"], has=["build", "-t", "img:tag"])[0]
    assert build[0] == "podman" and "--format" in build  # podman → --format docker
    assert build[-1] == str(fake["main"])  # build context = the main checkout
    assert "dev-stack/box.Dockerfile" in build
    assert f"{box._BOX_FINGERPRINT_LABEL}=expected-fingerprint" in build


def _build_args(build: list[str]) -> list[str]:
    return [build[i + 1] for i, tok in enumerate(build) if tok == "--build-arg"]


def test_walled_build_routes_through_the_proxy_as_a_trusted_build(fake, monkeypatch):
    # Under the in-VM wall a build's RUN steps egress through the proxy, which decrypts every
    # host off `[proxy] passthrough` (ADR-0029) — and a build container has no proxy CA, so a
    # tool fetching an undecryptable-to-it host dies on UNABLE_TO_VERIFY_LEAF_SIGNATURE (the
    # playwright CDN, seen live). The build gets the proxy URL carrying the build marker, which
    # the addon blind-tunnels (still walled at CONNECT). Proxy build-args are predefined — no
    # ARG line needed — and never persisted into the image.
    from foldyard.plugins import proxy

    monkeypatch.setattr(config, "machine_wall", lambda: True)
    monkeypatch.setattr(config, "proxy_port_base", lambda: 41000)
    assert box.main("build") == 0
    args = _build_args(_find(fake["calls"], has=["build", "-t", "img:tag"])[0])
    from urllib.parse import urlsplit

    for key in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
        (value,) = [a.split("=", 1)[1] for a in args if a.startswith(f"{key}=")]
        url = urlsplit(value)
        assert (url.hostname, url.port) == ("192.168.5.2", 41000)
        # The marker user, with the gate's per-build secret as the password — never the marker.
        assert url.username == proxy.BUILD_TUNNEL_USER
        assert url.password and url.password != proxy.BUILD_TUNNEL_USER


def test_box_build_runs_under_the_build_gate(fake, monkeypatch):
    from foldyard import buildgate

    gated: list[str] = []

    def fake_gate(build, *, what, **_):
        gated.append(what)
        return build(None)

    monkeypatch.setattr(buildgate, "run", fake_gate)
    assert box.main("build") == 0
    assert gated == ["box image build"]


def test_unwalled_build_gets_no_proxy_args(fake, monkeypatch):
    # No wall ⇒ builds egress directly, as before; routing them through the proxy would be new.
    monkeypatch.setattr(config, "machine_wall", lambda: False)
    assert box.main("build") == 0
    assert _build_args(_find(fake["calls"], has=["build", "-t", "img:tag"])[0]) == []


def test_consumer_build_args_come_after_the_proxy_ones(fake, monkeypatch):
    # A consumer that sets its own proxy build-arg wins: the engine takes the last occurrence.
    monkeypatch.setattr(config, "machine_wall", lambda: True)
    monkeypatch.setattr(
        config,
        "box_image",
        lambda: {
            "dockerfile": "dev-stack/box.Dockerfile",
            "tag": "img:tag",
            "build_args": {"HTTPS_PROXY": "http://mine:1"},
        },
    )
    assert box.main("build") == 0
    args = _build_args(_find(fake["calls"], has=["build", "-t", "img:tag"])[0])
    assert args[-1] == "HTTPS_PROXY=http://mine:1"
    assert any(a.startswith("HTTPS_PROXY=http://fy-build") for a in args[:-1])


# ── up ────────────────────────────────────────────────────────────────────────────────


def test_up_assembles_run(fake):
    assert box.main("up") == 0
    run = _find(fake["calls"], has=["run", "-d", "sleep"])[0]
    assert "--name" in run and "tangible-podman-devbox" in run
    assert "--network" in run and "tangible-podman_default" in run
    assert run[run.index("-w") + 1] == str(fake["main"])
    assert run[-3:] == ["img:tag", "sleep", "infinity"]
    # config-driven [box] bits
    assert any("shadow-data--venv" in tok for tok in run)  # shadow vol (tr '/.' '--')
    assert "tangible-podman-shared-pnpm:/home/vscode/.local/share/pnpm/store" in run  # cache
    assert "npm_config_store_dir=/home/vscode/.local/share/pnpm/store" in run  # ~ → BOX_HOME
    assert "IN_DEVBOX=1" in run and "APP_PORT" in run  # port passthrough
    # the project's Mac daemon port bases are PINNED into the box (in-box derivations can't
    # read the Mac's ports.json registry; the env vars win over allocation on both sides)
    assert "FY_PROXY_PORT=41000" in run and "GCP_MINTER_PORT=41100" in run
    # The operator's home, for the in-box mount audit (verify._host_home).
    assert f"FY_HOST_HOME={Path.home()}" in run
    # plugin box_args (gcp SA label, always)
    assert "--label" in run and "gcp.serviceAccount=box@p.iam.gserviceaccount.com" in run
    # clean DOCKER_CONFIG (default on) — sidesteps the editor-attach credsStore helper
    assert "DOCKER_CONFIG=/home/vscode/.docker-fy" in run


def _env_values(run: list[str]) -> list[str]:
    """Every ``-e`` value, in order."""
    return [run[i + 1] for i, tok in enumerate(run) if tok == "-e"]


def _clock(monkeypatch, zone=None, locale=None):
    monkeypatch.setattr(box.hostclock, "zone", lambda: zone)
    monkeypatch.setattr(box.hostclock, "time_locale", lambda: locale)


def test_up_passes_your_computers_timezone_before_the_box_env(fake, monkeypatch):
    _clock(monkeypatch, zone="Europe/London")
    assert box.main("up") == 0
    values = _env_values(_find(fake["calls"], has=["run", "-d", "sleep"])[0])
    assert "TZ=Europe/London" in values
    # Before `[box].env`: the consumer's table is where an override lives.
    assert values.index("TZ=Europe/London") < values.index("UV_LINK_MODE=copy")


def test_up_passes_no_timezone_when_your_computer_names_none(fake):
    assert box.main("up") == 0
    values = _env_values(_find(fake["calls"], has=["run", "-d", "sleep"])[0])
    assert not [v for v in values if v.startswith(("TZ=", "LC_TIME="))]


def test_up_box_env_overrides_the_timezone_and_time_locale(fake, monkeypatch):
    # One value per key, the consumer's: a duplicate `-e` would leave the winner to the engine.
    _clock(monkeypatch, zone="Europe/London", locale="en_GB.UTF-8")
    fake["state"]["image_locales"] = ["C.utf8", "en_GB.utf8"]
    monkeypatch.setattr(config, "box_env", lambda: {"TZ": "UTC", "LC_TIME": "C.UTF-8"})
    assert box.main("up") == 0
    values = _env_values(_find(fake["calls"], has=["run", "-d", "sleep"])[0])
    assert [v for v in values if v.startswith("TZ=")] == ["TZ=UTC"]
    assert [v for v in values if v.startswith("LC_TIME=")] == ["LC_TIME=C.UTF-8"]


def test_up_passes_the_time_locale_when_the_image_has_it(fake, monkeypatch):
    _clock(monkeypatch, locale="en_GB.UTF-8")
    fake["state"]["image_locales"] = ["C", "C.utf8", "POSIX", "en_GB.utf8"]
    assert box.main("up") == 0
    values = _env_values(_find(fake["calls"], has=["run", "-d", "sleep"])[0])
    assert "LC_TIME=en_GB.UTF-8" in values
    assert values.index("LC_TIME=en_GB.UTF-8") < values.index("UV_LINK_MODE=copy")


def test_up_skips_a_time_locale_the_image_lacks(fake, monkeypatch):
    # A locale the image can't load falls back to C anyway, and perl warns on every run.
    _clock(monkeypatch, zone="Europe/London", locale="en_GB.UTF-8")
    fake["state"]["image_locales"] = ["C", "C.utf8", "POSIX"]
    assert box.main("up") == 0
    values = _env_values(_find(fake["calls"], has=["run", "-d", "sleep"])[0])
    assert "TZ=Europe/London" in values
    assert not [v for v in values if v.startswith("LC_TIME=")]


def test_up_skips_the_time_locale_when_lc_all_would_override_it(fake, monkeypatch):
    # LC_ALL beats LC_TIME (glibc, and Claude Code's own LC_ALL || LC_TIME || LANG): under an
    # image's or `[box].env`'s LC_ALL, a passed LC_TIME would be silently ignored.
    _clock(monkeypatch, zone="Europe/London", locale="en_GB.UTF-8")
    fake["state"]["image_locales"] = ["C.utf8", "en_GB.utf8"]
    fake["state"]["image_lc_all"] = "C.UTF-8"
    assert box.main("up") == 0
    values = _env_values(_find(fake["calls"], has=["run", "-d", "sleep"])[0])
    assert "TZ=Europe/London" in values
    assert not [v for v in values if v.startswith("LC_TIME=")]

    fake["calls"].clear()
    fake["state"]["image_lc_all"] = ""
    monkeypatch.setattr(config, "box_env", lambda: {"LC_ALL": "C.UTF-8"})
    assert box.main("up") == 0
    values = _env_values(_find(fake["calls"], has=["run", "-d", "sleep"])[0])
    assert not [v for v in values if v.startswith("LC_TIME=")]


def test_up_lists_the_images_locales_in_its_home_probe(fake):
    # One probe container, not two: the locales ride the HOME probe.
    assert box.main("up") == 0
    probes = [c for c in fake["calls"] if c[1:3] == ["run", "--rm"]]
    assert len(probes) == 1 and "locale -a" in probes[0][-1]


def _gvisor(fake, monkeypatch):
    """The gVisor machine posture: the runsc endpoint is a ssh:// URI the sandbox module derives."""
    monkeypatch.setattr(config, "machine_runtime", lambda: "gvisor")
    uri = "ssh://dain@127.0.0.1:60022/run/user/501/podman/podman-runsc.sock"
    monkeypatch.setattr(
        box.sandbox,
        "engine_env",
        lambda env, *a: {
            **env,
            "CONTAINER_HOST": uri,
            "DOCKER_HOST": uri,
            "CONTAINER_SSHKEY": "/k",
        },
    )
    monkeypatch.setattr(
        box.sandbox, "box_socket", lambda: "/run/user/501/podman/podman-runsc-filtered.sock"
    )
    fake["state"]["oci_runtime"] = "runsc-fy"
    return uri


def test_up_under_gvisor_creates_through_the_runsc_endpoint_and_mounts_that_socket(
    fake, monkeypatch
):
    uri = _gvisor(fake, monkeypatch)
    assert box.main("up") == 0
    calls, envs = fake["calls"], fake["envs"]
    i = next(i for i, c in enumerate(calls) if c[1:3] == ["run", "-d"])
    run, env = calls[i], envs[i]
    # ONLY the create goes through the runsc socket — the bootstrap exec, the probes and the
    # image build stay on the stack env (same store, and the runtime is fixed at create)
    assert env["CONTAINER_HOST"] == uri and env["CONTAINER_SSHKEY"] == "/k"
    others = [e for j, e in enumerate(envs) if j != i and e is not None]
    assert others and all(e.get("CONTAINER_HOST") != uri for e in others)
    # the box's OWN socket is the NARROWED (filtered) runsc socket: whatever it creates runs
    # under gVisor too, and the filter strips any runtime opt-out from the create
    assert "/run/user/501/podman/podman-runsc-filtered.sock:/var/run/docker.sock" in run
    assert "/run/docker.sock:/var/run/docker.sock" not in run
    assert "FY_MACHINE_RUNTIME=gvisor" in run  # baked for the already-up nag + in-box verify
    assert "--runtime" not in run and "--annotation" not in run  # the SOCKET decides, not the box
    # and the create is CHECKED: inspect confirms the runtime before the bootstrap runs
    insp = next(j for j, c in enumerate(calls) if c[1] == "inspect" and "{{.OCIRuntime}}" in c)
    first_exec = next(j for j, c in enumerate(calls) if c[1] == "exec")
    assert i < insp < first_exec


def test_up_under_gvisor_fails_closed_when_the_box_came_up_under_another_runtime(
    fake, monkeypatch, capsys
):
    _gvisor(fake, monkeypatch)
    fake["state"]["oci_runtime"] = "crun"
    assert box.main("up") == 1
    calls = fake["calls"]
    assert not [c for c in calls if c[1] == "exec"]  # no bootstrap into an unsandboxed box
    assert [c for c in calls if c[1:3] == ["rm", "-f"]]  # and it is torn down, not left running
    captured = capsys.readouterr()
    assert "crun" in captured.out + captured.err


def test_up_without_the_posture_keeps_the_default_socket(fake, monkeypatch):
    monkeypatch.setattr(config, "machine_runtime", lambda: "")
    assert box.main("up") == 0
    run = _find(fake["calls"], has=["run", "-d", "sleep"])[0]
    assert "/run/docker.sock:/var/run/docker.sock" in run
    assert not [t for t in run if t.startswith("FY_MACHINE_RUNTIME")]
    assert not [c for c in fake["calls"] if c[1] == "inspect" and "{{.OCIRuntime}}" in c]


def test_up_nags_when_the_running_box_predates_the_posture(fake, monkeypatch, capsys):
    _gvisor(fake, monkeypatch)
    fake["state"]["running"] = True
    fake["state"]["exists"] = True
    fake["state"]["img"] = True
    fake["state"]["image_fingerprint"] = "expected-fingerprint"
    assert box.main("up") == 0
    out = capsys.readouterr().out
    assert "[machine].runtime" in out and "gvisor" in out and "fy box down && fy box up" in out


def test_up_no_runtime_nag_when_the_running_box_matches(fake, monkeypatch, capsys):
    _gvisor(fake, monkeypatch)
    fake["state"].update(
        running=True, exists=True, img=True, image_fingerprint="expected-fingerprint"
    )
    fake["state"]["baked_env"] = {"FY_MACHINE_RUNTIME": "gvisor"}
    assert box.main("up") == 0
    assert "[machine].runtime" not in capsys.readouterr().out  # other nags are not this one


def test_up_clean_docker_config_opt_out(fake, monkeypatch):
    monkeypatch.setattr(box.config, "box_clean_docker_config", lambda: False)
    assert box.main("up") == 0
    run = _find(fake["calls"], has=["run", "-d", "sleep"])[0]
    assert not any("DOCKER_CONFIG" in tok for tok in run)


def test_up_builds_image_when_absent_then_installs_and_warms(fake):
    box.main("up")
    kinds = [c[1] for c in fake["calls"]]
    assert "build" in kinds  # img absent → built
    execs = [c for c in fake["calls"] if c[1] == "exec"]
    assert any("foldyard" in " ".join(c) for c in execs)  # install dance
    assert any(c[2] == "-d" for c in execs)  # detached deps warm-up


def test_up_reuses_current_cached_image(fake):
    fake["state"].update(img=True, image_fingerprint="expected-fingerprint")
    assert box.main("up") == 0
    assert not _find(fake["calls"], has=["build", "-t", "img:tag"])


def test_up_rebuilds_stale_cached_image(fake):
    fake["state"].update(img=True, image_fingerprint="old-fingerprint")
    assert box.main("up") == 0
    assert _find(fake["calls"], has=["build", "-t", "img:tag"])


def test_up_creates_network_when_stackless_and_missing(fake):
    # Stack-less (fixture default) + network absent → box up creates `{project}_default` itself,
    # since no `compose up` ever will. This is the "network not found" regression's fix.
    fake["state"]["net_exists"] = False
    assert box.main("up") == 0
    assert _find(fake["calls"], has=["network", "create", "tangible-podman_default"])


def test_up_reuses_network_when_present(fake):
    # Already there (stack or a prior box-up made it) → inspect short-circuits, no create.
    fake["state"]["net_exists"] = True
    assert box.main("up") == 0
    assert not _find(fake["calls"], has=["network", "create"])


def test_up_leaves_network_to_compose_and_hints_when_missing(fake, monkeypatch, capsys):
    # A compose file present (and no external_network opt-in) → the stack owns (and labels)
    # `{project}_default`; box up must NOT create it — pre-creating it would clash with
    # compose's network labels. Missing means the stack has never been up: abort with the
    # `foldyard up` hint instead of podman's cryptic "network not found".
    compose = fake["main"] / "compose.podman.yml"
    compose.write_text("services: {}\n")
    monkeypatch.setattr(config, "compose_files", lambda: [str(compose)])
    fake["state"]["net_exists"] = False
    assert box.main("up") == 1
    assert not _find(fake["calls"], has=["network", "create"])
    assert not _find(fake["calls"], has=["run", "-d"])  # aborted before creating the box
    assert "foldyard up" in capsys.readouterr().err


def test_up_leaves_existing_network_to_compose(fake, monkeypatch):
    # Same compose-owned case with the network already there (stack has been up): box up
    # attaches without creating anything.
    compose = fake["main"] / "compose.podman.yml"
    compose.write_text("services: {}\n")
    monkeypatch.setattr(config, "compose_files", lambda: [str(compose)])
    fake["state"]["net_exists"] = True
    assert box.main("up") == 0
    assert not _find(fake["calls"], has=["network", "create"])


def test_up_creates_network_for_external_network_consumer(fake, monkeypatch):
    # external_network opt-in (compose declares `{project}_default` external) → foldyard owns
    # the network, so box up creates it even for a compose project. This is what makes a cold
    # `box up` (no prior `up`, e.g. a fresh worktree) work.
    compose = fake["main"] / "compose.podman.yml"
    compose.write_text("services: {}\n")
    monkeypatch.setattr(config, "compose_files", lambda: [str(compose)])
    monkeypatch.setattr(config, "external_network", lambda: True)
    fake["state"]["net_exists"] = False
    assert box.main("up") == 0
    assert _find(fake["calls"], has=["network", "create", "tangible-podman_default"])


def test_up_idempotent_when_running(fake, capsys):
    fake["state"]["running"] = True
    assert box.main("up") == 0
    assert "already up" in capsys.readouterr().out
    assert not _find(fake["calls"], has=["run", "-d", "sleep"])  # no create


def test_up_nags_when_running_box_uses_stale_image(fake, capsys):
    fake["state"].update(running=True, box_fingerprint="old-fingerprint")
    assert box.main("up") == 0
    out = capsys.readouterr().out
    assert "older image definition" in out and "fy box down && fy box up" in out


def test_up_nags_when_running_box_has_a_stale_proxy_port(fake, capsys):
    # A box built before the project's port band moved has FY_PROXY_PORT baked at the old base
    # while the host now serves the new one — its egress silently connection-refuses. The reuse
    # path must warn to recreate (env can't change in a running box).
    fake["state"]["running"] = True
    fake["state"]["baked_proxy_port"] = "8088"  # host now serves the 41000 band (fixture default)
    assert box.main("up") == 0
    out = capsys.readouterr().out
    assert "8088" in out and "recreate" in out


def test_up_no_nag_when_running_box_port_matches(fake, capsys):
    fake["state"]["running"] = True
    fake["state"]["baked_proxy_port"] = "41000"  # matches the allocated band base
    assert box.main("up") == 0
    assert "recreate" not in capsys.readouterr().out


# The [claude]/[codex] drift rows above fire on the same "recreate" advice, so the foldyard
# rows below bake their keys to keep those quiet and leave only the row under test speaking.
_QUIET = {"CLAUDE_CONFIG_DIR": "/home/vscode/.claude", "CODEX_HOME": "/home/vscode/.codex"}


def test_up_nags_when_running_box_has_a_different_foldyard(fake, capsys, monkeypatch):
    # The box's foldyard is installed by the bootstrap, which runs ONLY on a freshly created
    # box — so after a host upgrade the two sides sit on different versions until a recreate,
    # and every `fy box up` in between prints "already up" while changing nothing. Without this
    # row the mismatch is silent: it surfaces only as an in-box version-window refusal, whose
    # own advice is the recreate this row is asking for.
    fake["state"]["running"] = True
    fake["state"]["baked_env"] = dict(_QUIET)
    fake["state"]["box_foldyard"] = "0.2.1"
    monkeypatch.setattr(foldyard, "__version__", "0.3.0", raising=False)
    assert box.main("up") == 0
    out = capsys.readouterr().out
    assert "0.2.1" in out and "0.3.0" in out
    assert "fy box down && fy box up" in out


def test_up_asks_the_box_which_foldyard_it_runs(fake, monkeypatch):
    # The truth is what the box's PATH resolves, asked over the same login-shell route as
    # `fy box exec` — NOT a create-time stamp: the bootstrap skips its install step when a
    # foldyard is already present (image-baked, or a tool dir retained across recreates), so a
    # stamp of the host's version would silence this row in exactly the retained case.
    fake["state"]["running"] = True
    fake["state"]["baked_env"] = dict(_QUIET)
    monkeypatch.setattr(foldyard, "__version__", "0.3.0", raising=False)
    assert box.main("up") == 0
    probe = _find(fake["calls"], has=["exec", "foldyard --version"])
    assert probe and probe[0][-3:-1] == ["bash", "-lc"]
    create = _find(fake["calls"], has=["run", "-d"])
    assert not create and not any("FY_VERSION" in tok for c in fake["calls"] for tok in c)


def test_up_nags_when_running_box_has_no_working_foldyard(fake, capsys, monkeypatch):
    # Nothing answers `--version` — no install, a broken one, or one too old to know the
    # flag: the long-lived box whose foldyard is furthest behind. Drift, same as an absent
    # CLAUDE_CONFIG_DIR/CODEX_HOME above; the recreate reinstalls it.
    fake["state"]["running"] = True
    fake["state"]["baked_env"] = dict(_QUIET)
    fake["state"]["box_foldyard"] = ""
    monkeypatch.setattr(foldyard, "__version__", "0.3.0", raising=False)
    assert box.main("up") == 0
    out = capsys.readouterr().out
    assert "no working foldyard" in out and "fy box down && fy box up" in out


def test_up_no_nag_when_running_box_foldyard_matches(fake, capsys, monkeypatch):
    fake["state"]["running"] = True
    fake["state"]["baked_env"] = dict(_QUIET)
    fake["state"]["box_foldyard"] = "0.3.0"
    monkeypatch.setattr(foldyard, "__version__", "0.3.0", raising=False)
    assert box.main("up") == 0
    assert "fy box down && fy box up" not in capsys.readouterr().out


def test_bootstrap_reinstalls_a_retained_foldyard_that_is_not_the_hosts(monkeypatch):
    # The step's skip guard is "present AND the host's version", not "present": a foldyard
    # baked into the image or kept on a retained uv tool dir would otherwise survive the very
    # recreate the stale-foldyard row prescribes. A guard `command -v` would pass is the bug.
    monkeypatch.setattr(foldyard, "__version__", "0.3.0", raising=False)
    monkeypatch.setattr(
        box, "_foldyard_install_subst", lambda c, h: {"fy_wheel": "", "fy_version": ""}
    )
    monkeypatch.setattr(box.config, "box_tools", lambda: [])
    monkeypatch.setattr(box.config, "box_bootstrap", lambda: "")
    monkeypatch.setattr(box, "registry", lambda: _StubRegistry([]))
    script = box._bootstrap_script("/repo", "dev-stack", {})
    step = next(line for line in script.splitlines() if "'foldyard CLI'" in line)
    assert "foldyard --version" in step and "0.3.0" in step
    assert "command -v foldyard" not in step


def test_up_ensures_host_supervisor(fake):
    # The box always routes through the host supervisor's proxy, so box up must (idempotently) make
    # sure it's running — both on a fresh create and on an already-up box (the early-return case).
    assert box.main("up") == 0
    assert fake["hosted"] == [True]
    fake["hosted"].clear()
    fake["state"]["running"] = True
    assert box.main("up") == 0
    assert fake["hosted"] == [True]  # still ensured even when the box is already up


def test_up_proxy_ca_missing_blocks(fake, monkeypatch, tmp_path):
    fake["ctx"].env["FY_PROXY"] = "h:8088"  # proxy routing on
    monkeypatch.setenv("MITMPROXY_CA", str(tmp_path / "nope.pem"))
    assert box.main("up") == 1  # CA hard-fail surfaces as a clean non-zero
    assert not _find(fake["calls"], has=["run", "-d", "sleep"])


def test_up_stages_and_mounts_ambient_ca(fake, monkeypatch, tmp_path):
    # CA exists but NO routing (no FY_PROXY): up() stages it under the checkout and box_args mounts
    # the staged (VM-visible) copy + additively trusts it — no HTTPS_PROXY (ambient, not routed).
    monkeypatch.setattr(config, "proxy_enabled", lambda: True)
    src = tmp_path / "mitm" / "mitmproxy-ca-cert.pem"
    src.parent.mkdir()
    src.write_text("CERT")
    monkeypatch.setenv("MITMPROXY_CA", str(src))
    assert box.main("up") == 0
    run = _find(fake["calls"], has=["run", "-d", "sleep"])[0]
    staged = fake["main"] / "dev-stack/.devbox-ca/mitmproxy-ca-cert.pem"
    assert staged.read_text() == "CERT"  # staged onto a repo-mounted (VM-visible) path
    assert f"{staged}:/etc/dev-proxy-ca.pem:ro" in run
    assert "NODE_EXTRA_CA_CERTS=/etc/dev-proxy-ca.pem" in run
    assert not any("HTTPS_PROXY" in tok for tok in run)  # ambient trust, no routing


def test_up_stages_ambient_ca_even_with_proxy_off(fake, monkeypatch, tmp_path):
    # Regression: the box_args CA mount is AMBIENT (mounts whenever the CA exists), so staging must
    # be ambient too — even with NO [proxy] table and no injector. A host that happens to carry a
    # ~/.mitmproxy CA must still get the STAGED (VM-visible) path mounted, never the raw host path
    # (the machine mounts only the repo, so a ~/.mitmproxy mount source dies `statfs …: no such
    # file or directory` inside the VM — the homelab `fy box up` failure).
    monkeypatch.setattr(config, "proxy_enabled", lambda: False)
    src = tmp_path / "mitm" / "mitmproxy-ca-cert.pem"
    src.parent.mkdir()
    src.write_text("CERT")
    monkeypatch.setenv("MITMPROXY_CA", str(src))
    assert box.main("up") == 0
    run = _find(fake["calls"], has=["run", "-d", "sleep"])[0]
    staged = fake["main"] / "dev-stack/.devbox-ca/mitmproxy-ca-cert.pem"
    assert f"{staged}:/etc/dev-proxy-ca.pem:ro" in run  # VM-visible staged copy, not src
    assert not any(str(src) in tok for tok in run)  # the raw ~/.mitmproxy path never leaks in


# ── foldyard self-install resolution (Item 1: `fy` in any box, never assuming editable) ──


# These exercise the REAL box._foldyard_install_subst, so they deliberately do NOT use the `fake`
# fixture (which stubs that very function for the golden sequences).
def test_install_subst_prefers_vendored_repo(tmp_path, monkeypatch):
    # Repo vendors foldyard → empty subst (bootstrap installs editable from the mount: live
    # dogfood). stage_foldyard_for_box must NOT run (no wheel build for the vendored case).
    (tmp_path / "foldyard").mkdir()
    (tmp_path / "foldyard" / "pyproject.toml").write_text("[project]\n")
    monkeypatch.setattr(
        box, "stage_foldyard_for_box", lambda *a: pytest.fail("should not stage when vendored")
    )
    assert box._foldyard_install_subst(str(tmp_path), "dev-stack") == {
        "fy_wheel": "",
        "fy_version": "",
    }


def test_install_subst_stages_wheel_from_editable_host(tmp_path, monkeypatch):
    # Repo does NOT vendor foldyard, host is an editable dev install → build + stage a wheel
    # (the homelab case: a Mac dev checkout driving a repo without foldyard).
    from foldyard.plugins import proxy

    monkeypatch.setattr(proxy, "_foldyard_src", lambda: tmp_path / "src")
    monkeypatch.setattr(box, "stage_foldyard_for_box", lambda c, h, src: tmp_path / "fy.whl")
    out = box._foldyard_install_subst(str(tmp_path), "dev-stack")
    assert out == {"fy_wheel": str(tmp_path / "fy.whl"), "fy_version": ""}


def test_install_subst_pins_version_for_published_host(tmp_path, monkeypatch):
    # Repo doesn't vendor foldyard, host is a PUBLISHED (non-editable) install → pin the box to
    # the host version (no source to build from).
    from foldyard.plugins import proxy

    monkeypatch.setattr(proxy, "_foldyard_src", lambda: None)
    monkeypatch.setattr(box.metadata, "version", lambda name: "9.9.9")
    out = box._foldyard_install_subst(str(tmp_path), "dev-stack")
    assert out == {"fy_wheel": "", "fy_version": "9.9.9"}


def test_up_threads_staged_wheel_into_install(fake, monkeypatch):
    # End-to-end: a resolved wheel path lands in the box's bootstrap as `uv tool install --force`.
    wheel = str(fake["main"] / "dev-stack" / ".devbox-foldyard" / "foldyard-1.0-py3.whl")
    monkeypatch.setattr(
        box, "_foldyard_install_subst", lambda c, h: {"fy_wheel": wheel, "fy_version": ""}
    )
    assert box.main("up") == 0
    install = _find(fake["calls"], has=["exec", "bash", "-lc"])[0]
    script = install[-1]
    # shlex-quoted (no special chars → bare), and stripped of the host-only dependencies
    assert f'uv tool install --force --overrides "$_fy_ov" {wheel}' in script
    assert "run_step" in script  # delivered as a monitored step


def test_foldyard_run_forces_every_install_branch():
    # `_foldyard_check` fails the guard on a RETAINED stale foldyard, so every branch must
    # `--force` — a bare install of a spec matching the retained receipt is a no-op.
    script = box._foldyard_run("/w/acme", {"fy_wheel": "", "fy_version": "9.9.9"})
    assert 'uv tool install --force --overrides "$_fy_ov" "foldyard==9.9.9"' in script
    assert 'uv tool install --force --overrides "$_fy_ov" --editable /w/acme/foldyard' in script
    assert "uv tool install " not in script.replace("uv tool install --force", "")


def test_every_box_install_branch_strips_the_host_only_deps():
    # mitmproxy + PyJWT are CORE (a plain `uv tool install foldyard` is the complete host install);
    # the box routes through the proxy and never mints, so its install removes them. A branch
    # without the override would pull cryptography + mitmproxy's wheels through the proxy just to
    # put `fy` on PATH — no failure, only a slower, heavier bootstrap nobody would notice.
    script = box._foldyard_run("/w/acme", {"fy_wheel": "/w/fy.whl", "fy_version": "9.9.9"})
    installs = script.count("uv tool install")
    assert installs == 3
    assert script.count('--overrides "$_fy_ov"') == installs


def test_host_only_deps_are_core_dependencies():
    # The override removes packages BY NAME: rename or drop one in pyproject and it silently stops
    # removing anything (or, the other way, a new host-only dependency lands in every box). And the
    # `host` extra stays declared — empty — so `uv tool install "foldyard[host]"`, the line every
    # older doc and receipt carries, still installs without uv warning about an unknown extra.
    import re
    import tomllib

    project = tomllib.loads((Path(__file__).parents[1] / "pyproject.toml").read_text())["project"]
    core = {re.split(r"[\[<>=~!; ]", req, maxsplit=1)[0].lower() for req in project["dependencies"]}
    assert set(box.HOST_ONLY_DEPS) <= core
    assert project["optional-dependencies"]["host"] == []


@pytest.mark.spawns("bash")  # runs the REAL command under bash, `uv`/`mktemp`/`rm` as functions
@pytest.mark.parametrize("uv_rc", [0, 7])
def test_box_install_overrides_file_and_status(tmp_path, uv_rc):
    # What uv is handed (every host-only dep under a marker that never holds — the removal idiom),
    # that the install's status is the step's (run_step reports ✓/✗ off it), and that the temp file
    # is gone either way. Builtins only: the suite's PATH carries no mktemp/rm, so they're stubbed.
    import subprocess

    ov = tmp_path / "ov"
    stubs = f"""
mktemp() {{ printf '%s' {shlex.quote(str(ov))}; }}
rm() {{ printf 'rm %s\n' "$*"; }}
uv() {{ printf 'uv %s\n' "$*"; while IFS= read -r l; do printf 'ov %s\n' "$l"; done < "$5"; return {uv_rc}; }}
"""
    run = box._foldyard_run("/w/acme", {"fy_wheel": "", "fy_version": "9.9.9"})
    out = subprocess.run(
        ["bash", "-c", f'{stubs}\neval {shlex.quote(run)}\nprintf "rc=%s\\n" "$?"'],
        capture_output=True,
        text=True,
        env={"PATH": "/usr/bin:/bin"},
    )
    assert out.returncode == 0, out.stderr
    lines = out.stdout.splitlines()
    assert lines[0] == f"uv tool install --force --overrides {ov} foldyard==9.9.9"
    assert [ln for ln in lines if ln.startswith("ov ")] == [
        f'ov {name}; sys_platform == "never"' for name in box.HOST_ONLY_DEPS
    ]
    assert f"rm -f {ov}" in lines
    assert lines[-1] == ("rc=0" if uv_rc == 0 else "rc=1")


def test_stage_foldyard_builds_wheel(tmp_path, monkeypatch):
    # uv build succeeds (mocked to drop a .whl) → returns the staged wheel; failure → None.
    src = tmp_path / "src"
    src.mkdir()
    dest = tmp_path / "co" / "dev-stack" / ".devbox-foldyard"

    def fake_build(cmd, **kw):
        out = cmd[cmd.index("--out-dir") + 1]
        (Path(out) / "foldyard-1.0-py3-none-any.whl").write_text("WHEEL")
        return _Proc(0)

    monkeypatch.setattr(box.subprocess, "run", fake_build)
    wheel = box.stage_foldyard_for_box(str(tmp_path / "co"), "dev-stack", src)
    assert wheel and wheel.name == "foldyard-1.0-py3-none-any.whl" and wheel.parent == dest

    monkeypatch.setattr(box.subprocess, "run", lambda cmd, **kw: _Proc(1, ""))
    assert box.stage_foldyard_for_box(str(tmp_path / "co"), "dev-stack", src) is None


def test_stage_failure_prints_the_cause_and_names_any_proxy_env(tmp_path, monkeypatch, capsys):
    """The regression: uv's error pyramid was cut at 200 chars, which is exactly where the
    `╰─▶` root-cause line lives — and the one real failure seen so far (a shell exporting
    HTTPS_PROXY at foldyard's own egress proxy, which refuses pypi.org under default_deny)
    was undiagnosable without it."""
    src = tmp_path / "src"
    src.mkdir()
    err = (
        "Building wheel...\n"
        "  × Failed to build `/x/foldyard`\n"
        "  ├─▶ Failed to resolve requirements from `build-system.requires`\n"
        "  ├─▶ No solution found when resolving: `hatchling`\n"
        "  ╰─▶ tunnel error: unsuccessful"
    )
    monkeypatch.setattr(box.subprocess, "run", lambda cmd, **kw: _Proc(1, "", err))
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:41000")
    assert box.stage_foldyard_for_box(str(tmp_path / "co"), "dev-stack", src) is None
    out = capsys.readouterr().err
    assert "tunnel error: unsuccessful" in out  # the root-cause tail line survives
    assert "HTTPS_PROXY" in out  # the likely culprit is named


def test_install_subst_flags_a_failed_stage(tmp_path, monkeypatch):
    """Editable host + failed wheel build must not read as "no source anywhere": the box-side
    chain fails closed either way, but its message has to point at the actual failure (the
    host-side build), not claim there was nothing to install from."""
    from foldyard.plugins import proxy

    monkeypatch.setattr(proxy, "_foldyard_src", lambda: tmp_path / "src")
    monkeypatch.setattr(box, "stage_foldyard_for_box", lambda c, h, src: None)
    out = box._foldyard_install_subst(str(tmp_path), "dev-stack")
    assert out["fy_wheel"] == "" and out["fy_version"] == ""
    assert out["fy_stage_failed"] == "1"


def test_bootstrap_fail_closed_message_names_the_failed_wheel_build(monkeypatch):
    monkeypatch.setattr(
        box,
        "_foldyard_install_subst",
        lambda c, h: {"fy_wheel": "", "fy_version": "", "fy_stage_failed": "1"},
    )
    monkeypatch.setattr(box.config, "box_tools", lambda: [])
    monkeypatch.setattr(box.config, "box_bootstrap", lambda: "")
    monkeypatch.setattr(box, "registry", lambda: _StubRegistry([]))
    script = box._bootstrap_script("/repo", "dev-stack", {})
    assert "wheel build FAILED" in script


# ── monitored, pluggable bootstrap (Item 3a/3b) ──────────────────────────────────────────


def test_bootstrap_includes_core_and_consumer_tools(monkeypatch):
    # Core step (foldyard) + consumer [[box.tools]] all become monitored run_step calls; a tool's
    # explicit check is honoured, else it defaults to `command -v <name>`. (Claude is NOT core —
    # it's the gated [claude] plugin, contributed via registry().box_bootstrap.)
    monkeypatch.setattr(
        box, "_foldyard_install_subst", lambda c, h: {"fy_wheel": "", "fy_version": ""}
    )
    monkeypatch.setattr(
        box.config, "box_tools", lambda: [{"name": "pulumi", "install": "curl x | sh"}]
    )
    monkeypatch.setattr(box.config, "box_bootstrap", lambda: "")
    monkeypatch.setattr(box, "registry", lambda: _StubRegistry([]))
    script = box._bootstrap_script("/repo", "dev-stack", {})
    assert "run_step" in script and "_FY_BOOTSTRAP_FAILS" in script  # monitored
    assert "foldyard --version" in script  # core step (guard: present AND the host's version)
    assert "pulumi" in script and "command -v pulumi" in script  # consumer tool + default check
    assert "curl x | sh" in script


def test_bootstrap_includes_plugin_steps_and_custom_shell(monkeypatch):
    monkeypatch.setattr(
        box, "_foldyard_install_subst", lambda c, h: {"fy_wheel": "", "fy_version": ""}
    )
    monkeypatch.setattr(box.config, "box_tools", lambda: [])
    monkeypatch.setattr(box.config, "box_bootstrap", lambda: "echo hi from consumer")
    plugin_step = {
        "label": "zed sshd",
        "check": "command -v sshd",
        "run": "dnf install -y openssh-server",
    }
    monkeypatch.setattr(box, "registry", lambda: _StubRegistry([plugin_step]))
    script = box._bootstrap_script("/repo", "dev-stack", {})
    assert "zed sshd" in script and "dnf install -y openssh-server" in script  # plugin step
    assert (
        "custom bootstrap" in script and "echo hi from consumer" in script
    )  # free-form escape hatch


def test_bootstrap_installs_git_index_shim_by_default(monkeypatch):
    # The index-split shim ships as a monitored step (default on): the packaged shim script is
    # heredoc'd to /usr/local/bin/git. `[box] git_index_split = false` drops the step entirely.
    monkeypatch.setattr(
        box, "_foldyard_install_subst", lambda c, h: {"fy_wheel": "", "fy_version": ""}
    )
    monkeypatch.setattr(box.config, "box_tools", lambda: [])
    monkeypatch.setattr(box.config, "box_bootstrap", lambda: "")
    monkeypatch.setattr(box, "registry", lambda: _StubRegistry([]))
    script = box._bootstrap_script("/repo", "dev-stack", {})
    assert "git index shim" in script and "/usr/local/bin/git" in script
    assert "index-box" in script  # the packaged shim body made it into the heredoc
    monkeypatch.setattr(box.config, "box_git_index_split", lambda: False)
    script = box._bootstrap_script("/repo", "dev-stack", {})
    assert "index-box" not in script and "git index shim" not in script


class _StubRegistry:
    def __init__(self, steps):
        self._steps = steps

    def box_bootstrap(self, env):
        return self._steps


# ── a new box reports its setup: the in-box `fy doctor`, once, last ─────────────────────────


def test_bootstrap_ends_with_the_in_box_doctor(monkeypatch):
    # Nobody runs doctor in a box without a reason to suspect something, and what the git shim
    # rows guard against (an image putting another git first on PATH) only changes when a box is
    # created — so a new box reports once, AFTER the steps and their failure summary (the rows
    # judge what the steps left behind), as the very last thing (its status is the script's).
    monkeypatch.setattr(
        box, "_foldyard_install_subst", lambda c, h: {"fy_wheel": "", "fy_version": ""}
    )
    monkeypatch.setattr(box.config, "box_tools", lambda: [])
    monkeypatch.setattr(box.config, "box_bootstrap", lambda: "echo custom")
    monkeypatch.setattr(box, "registry", lambda: _StubRegistry([]))
    script = box._bootstrap_script("/repo", "dev-stack", {})
    assert script.endswith(box._doctor_report("/repo"))
    assert script.rstrip().splitlines()[-1] == "fy_doctor_report || true"
    assert script.index("bootstrap: step(s) failed") < script.index("fy_doctor_report()")
    assert script.index("custom bootstrap") < script.index("fy_doctor_report()")
    res = subprocess.run(["bash", "-n"], input=script, text=True, capture_output=True)
    assert res.returncode == 0, res.stderr


def _run_doctor_report(
    tmp_path: Path,
    foldyard_body: str | None,
    timeout_body: str | None = None,
    before: str = "",
    after: str = "",
) -> tuple[subprocess.CompletedProcess, Path]:
    """Run the REAL `_doctor_report` snippet under bash with a PATH holding only fakes: a
    `foldyard` (and optionally a `timeout`) written as ``/bin/sh`` scripts over builtins, so
    nothing real is reached. The shell starts in `/`, as a bootstrap step that `cd`s away would
    leave it, and is offered a line on stdin that a prompt would swallow."""
    bindir, checkout = tmp_path / "bin", tmp_path / "checkout"
    bindir.mkdir()
    checkout.mkdir()
    for name, body in (("foldyard", foldyard_body), ("timeout", timeout_body)):
        if body is not None:
            (bindir / name).write_text("#!/bin/sh\n" + body)
            (bindir / name).chmod(0o755)
    script = "\n".join([before, "cd /", box._doctor_report(str(checkout)), after])
    bash = shutil.which("bash")  # resolved here: the child's PATH holds only the fakes
    assert bash
    out = subprocess.run(
        [bash, "-c", script],
        input="y\n",
        capture_output=True,
        text=True,
        env={"HOME": str(tmp_path), "PATH": str(bindir)},
    )
    return out, checkout


# A doctor with a fail row: prints where it ran, how, and whatever it could read from stdin.
_FAILING_DOCTOR = (
    'echo "args=$*"\n'
    'echo "pwd=$(pwd)"\n'
    'echo "unbuffered=$PYTHONUNBUFFERED"\n'
    'read -r line && echo "stdin=$line"\n'
    "echo '  ✗ git shim  /usr/bin/git is not foldyard'\"'\"'s shim'\n"
    "exit 1\n"
)


@pytest.mark.spawns("bash")  # runs the REAL snippet under bash (fakes on PATH, builtins only)
def test_doctor_report_is_report_only(tmp_path):
    # Doctor exits 1 on any fail row. Report-only means that never becomes the bootstrap's
    # outcome — not even under a `set -e` a consumer's `[box].bootstrap` left switched on — and
    # the lines after it still run.
    out, checkout = _run_doctor_report(
        tmp_path, _FAILING_DOCTOR, before="set -e", after="echo after-doctor"
    )
    assert out.returncode == 0, out.stderr
    lines = out.stdout.splitlines()
    assert lines[0].startswith("▶ fy doctor (in the box")  # the lead line, before its rows
    assert "args=doctor" in lines
    assert f"pwd={checkout}" in lines  # the checkout, wherever a step left the shell
    assert "unbuffered=1" in lines  # rows stream as they come, not in one block at exit
    assert not any(ln.startswith("stdin=") for ln in lines)  # never reads (or prompts on) stdin
    assert "  ✗ git shim  /usr/bin/git is not foldyard's shim" in lines
    assert lines[-1] == "after-doctor"


@pytest.mark.spawns("bash")  # runs the REAL snippet under bash (builtins only)
def test_doctor_report_is_silent_without_the_foldyard_cli(tmp_path):
    # The "foldyard CLI" step can fail, and the summary above already names it: no lead line
    # for a doctor that can't run, no "command not found".
    out, _ = _run_doctor_report(tmp_path, None, after="echo after-doctor")
    assert out.returncode == 0
    assert out.stdout == "after-doctor\n" and out.stderr == ""


@pytest.mark.spawns("bash")  # runs the REAL snippet under bash (fakes on PATH, builtins only)
def test_doctor_report_is_bounded_by_timeout_when_it_works(tmp_path):
    # Every engine/network call in the in-box doctor has its own timeout; the whole-run bound is
    # for whatever a future row forgets. A usable `timeout` (it passes a one-second probe) wraps
    # the run with the budget.
    recorder = '[ "$1 $2" = "1 true" ] && exit 0\necho "timeout $*"\nshift\nexec "$@"\n'
    out, checkout = _run_doctor_report(tmp_path, _FAILING_DOCTOR, timeout_body=recorder)
    assert out.returncode == 0, out.stderr
    assert f"timeout {box._DOCTOR_BUDGET_S} foldyard doctor" in out.stdout
    assert f"pwd={checkout}" in out.stdout and "unbuffered=1" in out.stdout
    assert "stopped after" not in out.stdout


@pytest.mark.spawns("bash")  # runs the REAL snippet under bash (fakes on PATH, builtins only)
def test_doctor_report_says_when_the_bound_cut_it_short(tmp_path):
    expired = '[ "$1 $2" = "1 true" ] && exit 0\necho "  ✓ first row"\nexit 124\n'
    out, _ = _run_doctor_report(tmp_path, _FAILING_DOCTOR, timeout_body=expired)
    assert out.returncode == 0, out.stderr
    assert f"stopped after {box._DOCTOR_BUDGET_S}s" in out.stdout
    assert "`fy doctor` in the box" in out.stdout  # how to see the rest


@pytest.mark.spawns("bash")  # runs the REAL snippet under bash (fakes on PATH, builtins only)
def test_doctor_report_runs_unbounded_when_timeout_is_unusable(tmp_path):
    # The box contract is git + uv + an engine client: `timeout` may be missing, or an old
    # busybox one that wants `-t SECS` and refuses `timeout 1 true`. Either way doctor still runs
    # (the missing case is test_doctor_report_is_report_only: no `timeout` on its PATH at all).
    refuses = 'echo "usage: timeout -t SECS PROG" >&2\nexit 1\n'
    out, checkout = _run_doctor_report(tmp_path, _FAILING_DOCTOR, timeout_body=refuses)
    assert out.returncode == 0, out.stderr
    assert "args=doctor" in out.stdout and f"pwd={checkout}" in out.stdout
    assert "usage: timeout" not in out.stderr  # the probe's refusal is not the operator's business


# ── fy claude (Item 2: the agent launcher, replacing just claude-yolo) ────────────────────


def test_claude_argv_skips_permissions_and_appends_prompt():
    argv = box._claude_argv("be grounded", {}, ["--resume"])
    assert argv[:3] == ["claude", "--verbose", "--dangerously-skip-permissions"]
    assert "--append-system-prompt" in argv and "be grounded" in argv
    assert argv[-1] == "--resume"  # caller args passed through
    # empty prompt → no --append-system-prompt flag
    assert "--append-system-prompt" not in box._claude_argv("  ", {}, [])


def test_claude_refuses_outside_box(monkeypatch, capsys):
    monkeypatch.delenv("IN_DEVBOX", raising=False)
    monkeypatch.delenv("DOCKER_HOST", raising=False)
    assert box.claude([]) == 1
    assert "INSIDE the dev box" in capsys.readouterr().err


def test_claude_errors_when_not_installed(monkeypatch, capsys):
    monkeypatch.setenv("IN_DEVBOX", "1")
    monkeypatch.setattr(box.shutil, "which", lambda name: None)
    assert box.claude([]) == 1
    assert "[claude]" in capsys.readouterr().err  # points at the enabling config


def test_claude_execs_with_env_when_ready(monkeypatch):
    monkeypatch.setenv("IN_DEVBOX", "1")
    monkeypatch.setenv("IS_SANDBOX", "1")  # set at box-create; claude() passes the env through
    monkeypatch.delenv("CLAUDE_CODE_NO_FLICKER", raising=False)
    monkeypatch.setattr(box.shutil, "which", lambda name: "/usr/bin/claude")
    monkeypatch.setattr(box.config, "claude_system_prompt", lambda: "oriented")
    monkeypatch.setattr(box.config, "claude_settings", lambda: {})
    captured = {}

    def fake_exec(file, argv, env):
        captured["argv"], captured["env"] = argv, env
        raise SystemExit(0)  # stand in for the process replacement

    monkeypatch.setattr(box.os, "execvpe", fake_exec)
    with pytest.raises(SystemExit):
        box.claude(["-p", "hi"])
    assert captured["argv"][0] == "claude" and "oriented" in captured["argv"]
    # claude() forwards the ambient box env untouched — IS_SANDBOX carries through and it no
    # longer injects CLAUDE_CODE_NO_FLICKER (dropped in the per-kernel-index split).
    assert captured["env"]["IS_SANDBOX"] == "1"
    assert "CLAUDE_CODE_NO_FLICKER" not in captured["env"]


def test_codex_argv_bypasses_the_sandbox_and_carries_the_prompt():
    argv = box._codex_argv("be grounded", {}, ["resume"])
    assert argv[:2] == ["codex", "--dangerously-bypass-approvals-and-sandbox"]
    # Codex has no --append-system-prompt; `-c developer_instructions=…` is its analogue (it ADDS
    # a developer-message item, unlike base_instructions, which would replace the base prompt).
    assert argv[2] == "-c" and argv[3] == 'developer_instructions="be grounded"'
    assert argv[-1] == "resume"  # caller args passed through
    assert "-c" not in box._codex_argv("  ", {}, [])  # no prompt, no config → no override at all


def test_codex_prompt_is_emitted_as_a_quoted_toml_string():
    """`-c` parses its value AS TOML and only falls back to a literal when that fails, so a raw
    prompt is silently mangled whenever it happens to parse — verified against codex-cli 0.150.1:
    `Be terse. "Ship it"` came back missing its final quote, and a prompt of `true` was a hard
    error ("invalid type: boolean"). Quoting round-trips both."""
    import tomllib

    for prompt in ('Be terse. "Ship it"', "true", "line one\nline two\\end", "[not a table]"):
        override = box._codex_argv(prompt, {}, [])[3]
        key, _, value = override.partition("=")
        assert key == "developer_instructions"
        assert tomllib.loads(f"x = {value}")["x"] == prompt  # …reaches codex intact


def test_claude_settings_ride_along_as_one_json_blob():
    """`[claude.settings]` → `--settings <json>`: the flag takes a literal JSON string and Claude
    MERGES it into the settings hierarchy, so the table overrides per key."""
    argv = box._claude_argv("", {"model": "claude-opus-4-8", "env": {"FOO": "1"}}, ["-p", "hi"])
    assert argv[argv.index("--settings") + 1] == '{"model": "claude-opus-4-8", "env": {"FOO": "1"}}'
    assert argv[-2:] == ["-p", "hi"]  # …before the caller's args, so an explicit one still wins
    assert "--settings" not in box._claude_argv("p", {}, [])  # empty table → no flag


def test_codex_config_flattens_to_dotted_overrides_with_toml_values():
    """`[codex.config]` → one `-c` per LEAF. Nested tables flatten to Codex's dotted paths (a
    `-c tui={…}` would replace the whole `tui` table in ~/.codex/config.toml), and every value is
    emitted as TOML, which `-c` parses — a bool as `true`, not JSON-by-accident."""
    argv = box._codex_argv(
        "",
        {
            "hide_agent_reasoning": False,
            "model_reasoning_summary": "auto",
            "tui": {"raw_output_mode": False},
        },
        [],
    )
    assert argv[1:] == [
        "--dangerously-bypass-approvals-and-sandbox",
        "-c",
        "hide_agent_reasoning=false",
        "-c",
        'model_reasoning_summary="auto"',
        "-c",
        "tui.raw_output_mode=false",
    ]


def test_codex_config_values_round_trip_through_toml():
    """Every leaf reaches codex as the value it was written as. `-c` falls back to a raw literal
    STRING when the TOML parse fails, so a wrong rendering is silent, not an error."""
    import tomllib

    table = {
        "s": 'quote " and \\ back',
        "i": 7,
        "f": 1.5,
        "b": True,
        "arr": ["a", 2, False],
        "tbl": [{"k": "v"}],  # a dict inside an ARRAY can't flatten — inline table, `=` not `:`
        "dotted key": "quoted only when not a bare key",
    }
    rendered = {}
    for override in box._codex_overrides(table):
        key, _, value = override.partition("=")
        rendered[key] = value
    assert set(rendered) == {"s", "i", "f", "b", "arr", "tbl", '"dotted key"'}
    for key, value in rendered.items():
        assert tomllib.loads(f"x = {value}")["x"] == table[key.strip('"')]


def test_codex_refuses_outside_box_and_execs_inside(monkeypatch):
    monkeypatch.delenv("IN_DEVBOX", raising=False)
    monkeypatch.delenv("DOCKER_HOST", raising=False)
    assert box.codex([]) == 1  # refused outside the box
    monkeypatch.setenv("IN_DEVBOX", "1")
    monkeypatch.setattr(box.shutil, "which", lambda name: "/usr/bin/codex")
    monkeypatch.setattr(box.config, "codex_system_prompt", lambda: "oriented")
    monkeypatch.setattr(box.config, "codex_config", lambda: {})
    captured = {}

    def fake_exec(file, argv, env):
        captured["argv"] = argv
        raise SystemExit(0)

    monkeypatch.setattr(box.os, "execvpe", fake_exec)
    with pytest.raises(SystemExit):
        box.codex(["resume"])
    assert captured["argv"][:2] == ["codex", "--dangerously-bypass-approvals-and-sandbox"]
    assert 'developer_instructions="oriented"' in captured["argv"]
    assert captured["argv"][-1] == "resume"  # caller args passed through


# ── the launch gate: a keyless agent whose credential mode is off ─────────────────────


@pytest.fixture
def gate(monkeypatch, tmp_path):
    """An in-box launch of a keyless agent: the mirror present, exec captured, the mode + stdin
    scripted. Returns the namespace the tests steer (``modes`` is consumed one read at a time)."""
    import types

    monkeypatch.setenv("IN_DEVBOX", "1")
    mirror = tmp_path / ".dev-mode.json"
    mirror.write_text("{}")
    monkeypatch.setattr(box.config, "mirror_file", lambda: mirror)
    monkeypatch.setattr(box.shutil, "which", lambda name: f"/usr/bin/{name}")
    for agent in ("claude", "codex"):
        monkeypatch.setattr(box.config, f"{agent}_keyless", lambda: "oauth")
        monkeypatch.setattr(box.config, f"{agent}_system_prompt", lambda: "")
    monkeypatch.setattr(box.config, "claude_settings", lambda: {})
    monkeypatch.setattr(box.config, "codex_config", lambda: {})
    ns = types.SimpleNamespace(
        modes=["off"], tty=False, enter=False, execed=None, polls=0, stamps=None, clock=[0.0]
    )

    def mode_of(axis):
        return ns.modes.pop(0) if len(ns.modes) > 1 else ns.modes[0]

    def fake_select(r, w, x, timeout):
        ns.polls += 1
        return (r if ns.enter else [], [], [])

    def fake_exec(file, argv, env):
        ns.execed = argv
        raise SystemExit(0)

    def mirror_written():
        # Default: every read is a fresh supervisor write (the tick is always advancing).
        if ns.stamps is None:
            return f"t{ns.polls}-{len(ns.modes)}-{id(object())}"
        return ns.stamps.pop(0) if len(ns.stamps) > 1 else ns.stamps[0]

    def monotonic():
        ns.clock[0] += 1.0
        return ns.clock[0]

    monkeypatch.setattr(box, "_mode_of", mode_of)
    monkeypatch.setattr(box, "_mirror_written", mirror_written)
    monkeypatch.setattr(box.time, "monotonic", monotonic)
    monkeypatch.setattr(box.time, "sleep", lambda s: None)
    monkeypatch.setattr(box.select, "select", fake_select)
    monkeypatch.setattr(box.sys.stdin, "isatty", lambda: ns.tty, raising=False)
    monkeypatch.setattr(box.sys.stdin, "readline", lambda: "\n", raising=False)
    monkeypatch.setattr(box.os, "execvpe", fake_exec)
    return ns


# Every keyless KIND: the gate asks only whether keyless is set, never which — pinned here so an
# api-key yard (a placeholder key, not a token) is never quietly left out.
_AGENT_KINDS = [
    ("claude", "api-key"),
    ("claude", "oauth"),
    ("codex", "api-key"),
    ("codex", "chatgpt"),
]


def _launch(agent: str) -> int | None:
    try:
        return getattr(box, agent)([])
    except SystemExit:
        return None  # exec'd


@pytest.mark.parametrize(("agent", "kind"), _AGENT_KINDS)
def test_launch_is_silent_when_the_mode_is_on(gate, agent, kind, monkeypatch, capsys):
    monkeypatch.setattr(box.config, f"{agent}_keyless", lambda: kind)
    gate.modes = ["on"]
    _launch(agent)
    assert gate.execed and gate.execed[0] == agent
    assert capsys.readouterr().err == ""


@pytest.mark.parametrize(("agent", "kind"), _AGENT_KINDS)
def test_launch_off_without_a_terminal_warns_and_launches(gate, agent, kind, monkeypatch, capsys):
    # `fy codex exec …` from a script must never hang on a prompt nobody can answer.
    monkeypatch.setattr(box.config, f"{agent}_keyless", lambda: kind)
    _launch(agent)
    assert gate.execed and gate.polls == 0
    err = capsys.readouterr().err
    assert "credential mode is off" in err and f"`fy mode {agent}=on`" in err


@pytest.mark.parametrize(("agent", "kind"), _AGENT_KINDS)
def test_launch_off_at_a_terminal_waits_for_the_host_to_switch_it_on(
    gate, agent, kind, monkeypatch, capsys
):
    monkeypatch.setattr(box.config, f"{agent}_keyless", lambda: kind)
    gate.tty = True
    gate.modes = ["off", "off", "off", "on"]
    _launch(agent)
    assert gate.execed and gate.polls >= 2  # it waited, then launched by itself
    err = capsys.readouterr().err
    assert "Waiting" in err and f"{agent}=on" in err


def test_launch_after_the_flip_waits_for_the_proxy_to_catch_up(gate, capsys):
    # `fy mode` writes the mirror at once; the proxy learns on the supervisor's next tick, which
    # writes the mirror BEFORE the proxy's live settings. So "on" in the mirror isn't "injecting"
    # yet: wait for two supervisor writes past the flip, so a whole tick has applied it.
    gate.tty = True
    gate.modes = ["off", "on"]
    gate.stamps = ["flip", "flip", "tick1", "tick1", "tick2"]
    _launch("claude")
    assert gate.execed and gate.stamps == ["tick2"]  # consumed through the second tick


def test_launch_after_the_flip_does_not_hang_on_a_stalled_supervisor(gate):
    gate.tty = True
    gate.modes = ["off", "on"]
    gate.stamps = ["flip"]  # the mirror never advances again
    _launch("codex")
    assert gate.execed  # bounded: it launches anyway


def test_ctrl_c_while_waiting_for_the_proxy_cancels(gate, monkeypatch):
    gate.tty = True
    gate.modes = ["off", "on"]

    def interrupted(seconds):
        raise KeyboardInterrupt

    monkeypatch.setattr(box.time, "sleep", interrupted)
    gate.stamps = ["flip"]
    assert _launch("claude") == 130 and gate.execed is None


def test_launch_off_at_a_terminal_enter_launches_anyway(gate):
    gate.tty, gate.enter = True, True
    _launch("claude")
    assert gate.execed and gate.polls == 1


def test_launch_off_at_a_terminal_ctrl_c_cancels(gate, monkeypatch):
    gate.tty = True

    def interrupted(r, w, x, timeout):
        raise KeyboardInterrupt

    monkeypatch.setattr(box.select, "select", interrupted)
    assert _launch("claude") == 130 and gate.execed is None


@pytest.mark.parametrize("agent", ["claude", "codex"])
def test_launch_gate_says_nothing_without_keyless_or_a_mirror(gate, agent, monkeypatch, capsys):
    # A bare [agent] logs in for real (no mode to wait for); no mirror = the box knows nothing.
    monkeypatch.setattr(box.config, f"{agent}_keyless", lambda: "")
    _launch(agent)
    assert gate.execed and capsys.readouterr().err == ""
    gate.execed = None
    monkeypatch.setattr(box.config, f"{agent}_keyless", lambda: "oauth")
    box.config.mirror_file().unlink()
    _launch(agent)
    assert gate.execed and capsys.readouterr().err == ""


# ── shell / down / ps ────────────────────────────────────────────────────────────────


def test_shell_refuses_when_down(fake, capsys):
    assert box.main("shell") == 1
    assert "not running" in capsys.readouterr().err


def test_shell_execs_when_running(fake):
    fake["state"]["running"] = True
    box.main("shell")
    sh = _find(fake["calls"], has=["exec", "-it", "tangible-podman-devbox"])
    assert sh and "bash" in sh[0] and sh[0][-2] == "-lc"


# ── exec (non-interactive; the git-hooks dispatch primitive) ──────────────────────────


def test_exec_refuses_when_down(fake, capsys):
    assert box.main("exec", ["pnpm lint"]) == 1
    assert "not running" in capsys.readouterr().err


def test_exec_skips_when_down_with_flag(fake, capsys):
    # --skip-if-down turns a stopped box into a clean no-op (so a Mac commit isn't blocked).
    assert box.main("exec", ["--skip-if-down", "pnpm lint"]) == 0
    assert "skipping" in capsys.readouterr().err


def test_exec_runs_command_when_running(fake):
    fake["state"]["running"] = True
    assert box.main("exec", ["pnpm lint"]) == 0
    ex = _find(fake["calls"], has=["exec", "-w", "tangible-podman-devbox", "bash", "-lc"])
    assert ex and ex[0][-1] == "pnpm lint"  # the shell string runs verbatim in the box


def test_exec_requires_a_command(fake, capsys):
    fake["state"]["running"] = True
    assert box.main("exec", ["--skip-if-down"]) == 2
    assert "usage" in capsys.readouterr().err


def test_down_removes_when_exists(fake, capsys):
    fake["state"]["exists"] = True
    assert box.main("down") == 0
    assert _find(fake["calls"], has=["rm", "-f", "tangible-podman-devbox"])
    assert "stopped" in capsys.readouterr().out


def test_down_archives_transcripts_first(fake, monkeypatch):
    from foldyard import transcripts

    seen = []
    monkeypatch.setattr(transcripts, "sync_current", lambda env, **k: (seen.append(env), 0)[1])
    fake["state"]["exists"] = True
    assert box.main("down") == 0
    assert seen  # transcripts archived before the container is dropped


def test_down_removes_the_posture_mirror(fake):
    # The mirror exists FOR box sessions — with the box gone it's just an untracked file
    # dirtying the checkout (release flows choke on it), so `box down` drops it.
    fake["state"]["exists"] = True
    fake["mirror"].write_text("{}\n")
    assert box.main("down") == 0
    assert not fake["mirror"].exists()


def test_down_noops_without_provisioning_when_machine_gone(fake, monkeypatch):
    # `fy box down` after the machine was deleted must not ensure/provision a VM just to
    # find no box in it — but it still clears a stale posture mirror.
    resolved: list = []
    monkeypatch.setattr(box.stack, "resolve", lambda *a, **k: resolved.append(1))
    monkeypatch.setattr(box.stack, "engine_reachable", lambda *a, **k: False)
    fake["mirror"].write_text("{}\n")
    assert box.main("down") == 0
    assert resolved == [] and fake["calls"] == []
    assert not fake["mirror"].exists()


def test_ps_noops_when_machine_gone(fake, monkeypatch):
    monkeypatch.setattr(box.stack, "engine_reachable", lambda *a, **k: False)
    assert box.main("ps") == 0
    assert fake["calls"] == []


@pytest.mark.parametrize(
    ("cmd", "args", "rc"),
    [("shell", None, 1), ("exec", ["true"], 1), ("exec", ["--skip-if-down", "true"], 0)],
)
def test_attach_verbs_never_boot_a_stopped_machine(fake, monkeypatch, cmd, args, rc):
    # No box runs on a stopped VM, so shell/exec refuse from the read-only probe — letting _ctx()
    # resolve would ENSURE (boot) the machine only to say "not running". `--skip-if-down` (the
    # git-hook path) stays a clean no-op. The gate's note points at `fy box up`, not `fy up`.
    resolved: list = []
    monkeypatch.setattr(box.stack, "resolve", lambda *a, **k: resolved.append(1))
    hints: list = []
    monkeypatch.setattr(box.stack, "engine_reachable", lambda _to, start: hints.append(start))
    assert box.main(cmd, args) == rc
    assert resolved == [] and fake["calls"] == []
    assert hints == ["fy box up"]


def test_start_hint_keeps_the_worktree_name_verbatim():
    # Built directly, never by rewriting the attach hint: a `.replace("shell", "up")` mangled a
    # worktree whose NAME contains "shell" into one that doesn't exist.
    assert box._start_hint("myshell") == "WORKTREE=myshell fy box up"
    assert box._start_hint("") == "fy box up"


def test_ps_lists_box(fake):
    assert box.main("ps") == 0
    ps = _find(fake["calls"], has=["ps", "-a"])
    assert ps and "name=^tangible-podman-devbox$" in ps[0]


def test_unknown_subcommand(fake):
    assert box.main("bogus") == 2


def test_up_mounts_the_worktrees_root_so_boxes_see_siblings(fake, monkeypatch, tmp_path):
    # The MACHINE has always mounted the worktrees root; the box container didn't, so an agent in
    # main's box couldn't read a worktree copy at all. Mounting it also lets `worktree init_config`
    # run the consumer's init script in here rather than on the host. Cross-worktree reach sits in
    # the existing trust tier (ADR-0004) and posture state stays host-side, so no box picks up
    # another worktree's credentials.
    wt_root = tmp_path / "repo-worktrees"
    wt_root.mkdir()
    monkeypatch.setattr(config, "worktrees_root", lambda m: wt_root)
    assert box.main("up") == 0
    run = _find(fake["calls"], has=["run", "-d", "sleep"])[0]
    assert f"{wt_root}:{wt_root}" in run


def test_up_mounts_the_shared_shell_volume_and_history_points_at_it(fake):
    # bash history lives in an agent-neutral persisted volume (devbox_shell → ~/.devbox), not in
    # ~/.claude: a shell-only box (no [claude]) must keep history across recreation too, and shell
    # state doesn't belong in an agent's config dir. The volume is unconditional like devbox_tools.
    assert box.main("up") == 0
    run = _find(fake["calls"], has=["run", "-d", "sleep"])[0]
    assert "devbox_shell:/home/vscode/.devbox" in run
    assert 'HISTFILE="$HOME/.devbox/.bash_history"' in box._BASE_SCRIPT
    assert ".claude/.bash_history" not in box._BASE_SCRIPT.replace(
        'cp "$HOME/.claude/.bash_history"',
        "",  # the one-time migration copy is the only mention
    ).replace('[ -f "$HOME/.claude/.bash_history" ]', "")


def _run_path_snippet(*applications: tuple[str, ...], start: str) -> str:
    """Execute the REAL `_PATH_PREPEND_SNIPPET` under bash and return the resulting PATH.

    Shell string-munging is exactly the kind of code that reads correct and isn't (one
    substitution pass can't remove ADJACENT duplicates), so this runs it rather than asserting on
    its text. Each entry in ``applications`` is one `fy_path_prepend` argument list — the nesting
    a `box shell` produces when `bash -l` re-reads the rc files on top of the wrapper's export.
    """
    import subprocess

    lines = [box._PATH_PREPEND_SNIPPET, f"PATH={shlex.quote(start)}"]
    lines += ["fy_path_prepend " + " ".join(shlex.quote(a) for a in args) for args in applications]
    lines.append('printf %s "$PATH"')
    out = subprocess.run(
        ["bash", "-c", "\n".join(lines)],
        capture_output=True,
        text=True,
        env={"HOME": "/home/vscode", "PATH": "/usr/bin:/bin"},
    )
    assert out.returncode == 0, out.stderr
    return out.stdout


_FY_DIRS = ("/opt/fy-tools/bin", "/home/vscode/.local/bin")


@pytest.mark.spawns("bash")  # runs the REAL snippet under bash (builtins only)
def test_path_prepend_is_idempotent_across_nested_shells():
    # The bug: four sites prepend the same two dirs and every one runs AGAIN in a nested shell
    # (`box shell` execs `bash -l`, which re-reads the rc files over the wrapper's export). None
    # checked what $PATH already held, so a live box reached 19 entries for 10 distinct dirs —
    # harmless for resolution, but `which -a claude` then prints one install six times, which is
    # the signature of a leftover npm install you'd be hunting for.
    once = _run_path_snippet(_FY_DIRS, start="/usr/bin:/bin")
    assert once == "/home/vscode/.local/bin:/opt/fy-tools/bin:/usr/bin:/bin"
    six = _run_path_snippet(*([_FY_DIRS] * 6), start="/usr/bin:/bin")
    assert six == once, "PATH grew across nested applications"


@pytest.mark.spawns("bash")  # runs the REAL snippet under bash (builtins only)
def test_path_prepend_restores_order_rather_than_skipping():
    # Why it's remove-then-prepend and NOT skip-if-present: ~/.local/bin must beat
    # /opt/fy-tools/bin (the native Claude installer hardcodes ~/.local/bin/claude; an npm install
    # would pin the stale prefix). A parent shell that already put them the other way round must
    # be CORRECTED — skip-if-present would silently inherit the wrong order.
    out = _run_path_snippet(_FY_DIRS, start="/opt/fy-tools/bin:/home/vscode/.local/bin:/usr/bin")
    assert out == "/home/vscode/.local/bin:/opt/fy-tools/bin:/usr/bin"


@pytest.mark.spawns("bash")  # runs the REAL snippet under bash (builtins only)
def test_path_prepend_collapses_adjacent_duplicates():
    # The one-pass version of this looked right and wasn't: bash substitutes non-overlapping
    # matches, so `:a:a:` → `:a:` and a duplicate survived every run. Hence the loop to fixpoint.
    out = _run_path_snippet(
        ("/home/vscode/.local/bin",),
        start="/home/vscode/.local/bin:/home/vscode/.local/bin:/usr/bin",
    )
    assert out == "/home/vscode/.local/bin:/usr/bin"


def test_path_prepend_is_wired_into_every_prepend_site(fake, monkeypatch):
    # Three sites, one snippet: the ~/.bashrc block the bootstrap writes, the bootstrap's own
    # in-process export, and the `box shell` wrapper. A raw `PATH=…:$PATH` reintroduces the growth.
    assert box._BASE_SCRIPT.count("fy_path_prepend()") == 2  # written into ~/.bashrc, and run now
    assert "<<'BASHPATH'" in box._BASE_SCRIPT  # the ~/.bashrc block, guarded by the grep above it
    monkeypatch.setattr(box, "_running", lambda *a, **k: True)
    box.main("shell")
    attach = _find(fake["calls"], has=["exec", "-it"])[0]
    assert "fy_path_prepend " in attach[-1] and "exec bash -l" in attach[-1]
    assert 'PATH="$HOME/.local/bin:/opt/fy-tools/bin:$PATH"' not in attach[-1]


# ── the editor attach's host bridges, neutralised in-box ─────────────────────────────────


def _run_harden(home, env_in: dict[str, str]) -> dict[str, str]:
    """Run the REAL `_HARDEN_SNIPPET` under bash with ``home`` as $HOME, then source the file it
    wrote in a shell that starts with ``env_in`` and print the bridge vars as they end up. The
    reaper is kept from spawning by seeding its pidfile with the running shell's own pid (its
    liveness probe is `kill -0`), so nothing is left behind and no real /tmp is touched."""
    import subprocess

    cfg = home / ".config" / "foldyard"
    cfg.mkdir(parents=True, exist_ok=True)
    (cfg / "bridge-reaper.pid").write_text("$$")  # rewritten with the real pid below
    script = "\n".join(
        [
            'echo $$ > "$HOME/.config/foldyard/bridge-reaper.pid"',
            box._HARDEN_SNIPPET,
            "for v in SSH_AUTH_SOCK GIT_ASKPASS VSCODE_GIT_IPC_HANDLE VSCODE_IPC_HOOK_CLI BROWSER; do",
            '  printf "%s=%s\\n" "$v" "${!v-<unset>}"',
            "done",
        ]
    )
    out = subprocess.run(
        ["bash", "-c", script],
        capture_output=True,
        text=True,
        env={"HOME": str(home), "PATH": "/usr/bin:/bin", **env_in},
    )
    assert out.returncode == 0, out.stderr
    return dict(line.split("=", 1) for line in out.stdout.splitlines())


@pytest.mark.spawns("bash")  # runs the REAL snippet under bash
def test_harden_unsets_the_attach_bridges_and_hooks_bashrc_once(tmp_path):
    # Seen live 2026-09-17: a `fy code` attach put a LIVE agent socket (1 key) and the git
    # credential bridge into a box whose posture read "never push". The hygiene half: every var
    # the attach sets is gone after the file is sourced, and the file is sourced from ~/.bashrc
    # line 1 — before the interactive guard — exactly once however many times the snippet runs.
    home = tmp_path / "home"
    home.mkdir()
    (home / ".bashrc").write_text("# existing rc\n")
    attached = {
        "SSH_AUTH_SOCK": "/tmp/vscode-ssh-auth-x.sock",
        "GIT_ASKPASS": "/root/.vscode-server/bin/x/extensions/git/dist/askpass.sh",
        "VSCODE_GIT_IPC_HANDLE": "/tmp/vscode-git-x.sock",
        "VSCODE_IPC_HOOK_CLI": "/tmp/vscode-ipc-x.sock",
        "BROWSER": "/root/.vscode-server/bin/x/bin/helpers/browser.sh",
    }
    seen = _run_harden(home, attached)
    assert seen["SSH_AUTH_SOCK"] == "" and seen["BROWSER"] == ""  # empty, so no default kicks in
    assert seen["GIT_ASKPASS"] == "<unset>" and seen["VSCODE_GIT_IPC_HANDLE"] == "<unset>"
    assert seen["VSCODE_IPC_HOOK_CLI"] == "<unset>"
    _run_harden(home, attached)  # idempotent
    rc = (home / ".bashrc").read_text().splitlines()
    assert rc[0].startswith('source "$HOME/.config/foldyard/harden.sh"')
    assert rc.count(rc[0]) == 1 and rc[-1] == "# existing rc"
    harden = (home / ".config" / "foldyard" / "harden.sh").read_text()
    assert "rm -f /tmp/vscode-ssh-auth-*.sock /tmp/vscode-git-*.sock" in harden  # the reaper


def test_harden_is_in_the_bootstrap_and_reapplied_to_an_already_up_box(fake, monkeypatch):
    # Both entry points: every new box gets it from the base bootstrap (image-agnostic — the
    # consumer image is not where a posture guard belongs), and a box created before it shipped
    # gets it on its next `box up` without a recreate.
    assert box._HARDEN_SNIPPET in box._BASE_SCRIPT
    monkeypatch.setattr(box, "_running", lambda *a, **k: True)
    assert box.main("up") == 0
    reapplied = [c for c in fake["calls"] if "exec" in c and any("harden.sh" in t for t in c)]
    assert reapplied and reapplied[0][-1] == box._HARDEN_SNIPPET


def test_up_skips_the_worktrees_mount_when_there_is_none(fake, monkeypatch, tmp_path):
    # A project that has never made a worktree gets no phantom mount (podman would create the dir).
    monkeypatch.setattr(config, "worktrees_root", lambda m: tmp_path / "never-created")
    assert box.main("up") == 0
    run = _find(fake["calls"], has=["run", "-d", "sleep"])[0]
    assert not any("never-created" in tok for tok in run)


def test_declared_secrets_and_keyless_creds_are_captured_even_when_the_box_is_already_up(
    fake, monkeypatch
):
    # Both captures feed HOST-side minters that read host.env at mint time, so what matters is
    # whether the posture needs the value — not whether the box was just created. Turning
    # a `github-app` switch on with a box already running must still prompt for the PEM, and declaring
    # [claude] keyless with a box already running must still prompt for the token (the regression
    # this pins: no prompt until `fy box down`, and the box 401'd for days). Only the DUMMY
    # credential baked into the container still needs a recreate — that's a nag, not a skip.
    calls: list[str] = []
    monkeypatch.setattr(box, "_capture_secrets", lambda: calls.append("secrets"))
    monkeypatch.setattr(box, "_capture_keyless", lambda: calls.append("keyless"))
    fake["state"]["running"] = True
    assert box.main("up") == 0
    assert calls == ["secrets", "keyless"]  # both ran despite the already-up early return


def test_up_nags_when_running_box_predates_a_declared_claude(fake, capsys, monkeypatch):
    # [claude] added with the box already running: the container has no Claude install, volumes or
    # dummy credential (all create-time), so `fy box up` must say so instead of "already up" alone.
    # A box created WITH [claude] bakes CLAUDE_CONFIG_DIR (claude.py box_args), so its absence in
    # the frozen Config.Env is the drift signal.
    monkeypatch.setattr(config, "claude_enabled", lambda: True)
    fake["state"]["running"] = True
    assert box.main("up") == 0
    out = capsys.readouterr().out
    assert "[claude]" in out and "fy box down && fy box up" in out


def test_up_no_claude_nag_when_the_box_was_created_with_it(fake, capsys, monkeypatch):
    monkeypatch.setattr(config, "claude_enabled", lambda: True)
    fake["state"]["running"] = True
    fake["state"]["baked_env"]["CLAUDE_CONFIG_DIR"] = "/home/vscode/.claude"
    assert box.main("up") == 0
    assert "[claude]" not in capsys.readouterr().out


def test_up_nags_when_running_box_predates_a_declared_codex(fake, capsys, monkeypatch):
    # Same drift as claude's, and it needs the same nag: without it the box simply has no codex,
    # `fy codex` answers "add a [codex] table" (already done), and nothing names the recreate.
    # CODEX_HOME (codex.py box_args) is the create-time signal.
    monkeypatch.setattr(config, "codex_enabled", lambda: True)
    fake["state"]["running"] = True
    assert box.main("up") == 0
    out = capsys.readouterr().out
    assert "[codex]" in out and "fy box down && fy box up" in out


def test_up_no_codex_nag_when_the_box_was_created_with_it(fake, capsys, monkeypatch):
    monkeypatch.setattr(config, "codex_enabled", lambda: True)
    fake["state"]["running"] = True
    fake["state"]["baked_env"]["CODEX_HOME"] = "/home/vscode/.codex"
    assert box.main("up") == 0
    assert "[codex]" not in capsys.readouterr().out


# ── a keyless agent whose axis is still at rest ──────────────────────────────────────


@pytest.fixture
def keyless_posture(monkeypatch):
    """Declare keyless for either agent and pin the stored posture, over the REAL plugins.

    The axis exists only because `keyless` is set (`ClaudePlugin.axes` returns nothing without
    it), so the registry is built from the real plugins rather than stand-ins — a synthetic axis
    would not pin the coupling this warning is about."""
    from foldyard import devmode
    from foldyard.plugins import Registry, claude, codex

    def _wire(mode, *, claude_keyless="", codex_keyless=""):
        from conftest import GENERIC_TOML, make_config

        monkeypatch.setattr(config, "claude_keyless", lambda: claude_keyless)
        monkeypatch.setattr(config, "codex_keyless", lambda: codex_keyless)
        reg = Registry(
            [claude.ClaudePlugin(), codex.CodexPlugin()], config=make_config(GENERIC_TOML)
        )
        monkeypatch.setattr(devmode, "registry", lambda: reg)
        monkeypatch.setattr(devmode, "read", lambda apply_expiry=True: {"mode": mode})

    return _wire


def test_up_warns_when_a_keyless_agent_axis_is_still_off(fake, capsys, keyless_posture):
    # The step-2 papercut: [claude].keyless declared, box built fine, but nobody ran `fy mode
    # claude=on` — so the proxy injects nothing, the box keeps its dummy, and Claude 401s against
    # a box where everything else is right.
    keyless_posture({"claude": "off"}, claude_keyless="oauth")
    assert box.main("up") == 0
    err = capsys.readouterr().err
    assert "[claude].keyless" in err and "claude=off" in err
    assert "fy mode claude=on" in err and "fy tui" in err


def test_up_is_quiet_once_the_axis_is_armed(fake, capsys, keyless_posture):
    keyless_posture({"claude": "on"}, claude_keyless="oauth")
    assert box.main("up") == 0
    assert "[claude].keyless" not in capsys.readouterr().err


def test_up_does_not_warn_for_a_bare_agent_table(fake, capsys, keyless_posture):
    # No keyless ⇒ ClaudePlugin.switches() returns NOTHING, so there is no rung to arm: that box logs
    # in inside the container. Telling this user to run `fy mode claude=on` would name an axis
    # that does not exist.
    keyless_posture({}, claude_keyless="")
    assert box.main("up") == 0
    assert "keyless" not in capsys.readouterr().err


def test_up_warns_per_agent_not_once_for_both(fake, capsys, keyless_posture):
    keyless_posture(
        {"claude": "off", "codex": "on"}, claude_keyless="oauth", codex_keyless="chatgpt"
    )
    assert box.main("up") == 0
    err = capsys.readouterr().err
    assert "[claude].keyless" in err
    assert "[codex].keyless" not in err  # codex is armed; only the one at rest speaks


def test_up_warns_about_a_resting_axis_even_when_the_box_is_already_up(
    fake, capsys, keyless_posture
):
    # Arming is host-side and needs no recreate, so an already-up box is exactly where this is
    # most likely to be the one thing still missing — it must not sit below the early return.
    keyless_posture({"claude": "off"}, claude_keyless="oauth")
    fake["state"]["running"] = True
    assert box.main("up") == 0
    assert "fy mode claude=on" in capsys.readouterr().err
