"""box.py — `foldyard box build|up|shell|down|ps`. The engine is mocked (golden command
sequences): we assert the assembled `<engine> run`/`build`/`exec` shapes — incl. the
config-driven [box] bits + the plugin box env/mounts — without creating a real box."""

from __future__ import annotations

import shlex
from pathlib import Path
from typing import Any

import pytest

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
    }

    def fake_run(cmd, **kw):
        calls.append(cmd)
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
            return _Proc(0, "/home/vscode /home/vscode/.claude")
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
    # plugin box_args (gcp SA label, always)
    assert "--label" in run and "gcp.serviceAccount=box@p.iam.gserviceaccount.com" in run
    # clean DOCKER_CONFIG (default on) — sidesteps the editor-attach credsStore helper
    assert "DOCKER_CONFIG=/home/vscode/.docker-fy" in run


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
    fake["ctx"].env["FY_PROXY"] = "h:8088"  # github mode on
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
    # be ambient too — even with NO [proxy] table and no GH_INJECT. A Mac that happens to carry a
    # ~/.mitmproxy CA must still get the STAGED (VM-visible) path mounted, never the raw host path
    # (the machine mounts only the repo, so a ~/.mitmproxy mount source dies `statfs …: no such
    # file or directory` inside the VM — the homelab `fy box up` failure).
    monkeypatch.setattr(config, "proxy_enabled", lambda: False)
    fake["ctx"].env.pop("GH_INJECT", None)
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
    assert f"uv tool install --force {wheel}" in script  # shlex-quoted (no special chars → bare)
    assert "run_step" in script  # delivered as a monitored step


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
    assert "command -v foldyard" in script  # core step
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


def test_path_prepend_restores_order_rather_than_skipping():
    # Why it's remove-then-prepend and NOT skip-if-present: ~/.local/bin must beat
    # /opt/fy-tools/bin (the native Claude installer hardcodes ~/.local/bin/claude; an npm install
    # would pin the stale prefix). A parent shell that already put them the other way round must
    # be CORRECTED — skip-if-present would silently inherit the wrong order.
    out = _run_path_snippet(_FY_DIRS, start="/opt/fy-tools/bin:/home/vscode/.local/bin:/usr/bin")
    assert out == "/home/vscode/.local/bin:/opt/fy-tools/bin:/usr/bin"


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
    # `github=app` on with a box already running must still prompt for the PEM, and declaring
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
    # No keyless ⇒ ClaudePlugin.axes() returns NOTHING, so there is no rung to arm: that box logs
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
