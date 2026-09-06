"""minter.py end-to-end — the real Mac-side SA-token minter as a subprocess.

Runs the actual `minter.py` with a FAKE `gcloud` on PATH (a one-line shim that prints a marker
token, so no real creds are touched), then drives it over HTTP. Proves the per-request JSONL log
the TUI's "GCP Tokens" panel tails: an allowlisted SA is granted and logged, an unlisted one is
refused and logged, and — the security-critical bit — the minted token NEVER lands in the log.

No engine / network / gcloud needed, so this always runs (unlike the opt-in box e2es)."""

from __future__ import annotations

import email.message
import io
import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from foldyard.plugins import gcp

MINTER_PY = gcp.METADATA_DIR / "minter.py"
_ALLOWED = "devbox-log-reader@acme-staging.iam.gserviceaccount.com"
_FAKE_TOKEN = "ya29.FAKE-TOKEN-DO-NOT-LOG"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _post(port: int, payload: dict) -> tuple[int, dict]:
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def _entries(log) -> list[dict]:
    return [json.loads(line) for line in log.read_text().splitlines() if line.strip()]


@pytest.fixture
def make_minter(tmp_path):
    """Factory launching the real minter with a given fake-gcloud body + a temp mint log;
    returns (port, log_path). Lets tests choose how `gcloud` behaves (mint vs. fail)."""
    procs: list[subprocess.Popen] = []

    def _make(gcloud_body: str, **env_extra: str) -> tuple[int, object]:
        # Per-invocation paths: a second _make in the same test must not overwrite the first
        # minter's fake gcloud (still live on its PATH) or interleave into its log.
        n = len(procs)
        bindir = tmp_path / f"bin{n}"
        bindir.mkdir()
        fake = bindir / "gcloud"  # earliest on PATH ⇒ the minter's `gcloud` resolves here
        fake.write_text(f"#!{sys.executable}\n{gcloud_body}\n")
        fake.chmod(0o755)
        log = tmp_path / f"gcp-minter-{n}.jsonl"
        port = _free_port()
        env = {
            **os.environ,
            "PATH": f"{bindir}{os.pathsep}{os.environ['PATH']}",
            "GCP_SA_ALLOWLIST": _ALLOWED,
            "GCP_MINTER_LOG_FILE": str(log),
            "LISTEN_PORT": str(port),
            "GCP_TOKEN_REFRESH": "60",
            **env_extra,
        }
        # stdin is a PIPE we never write to — the supervisor runs the minter from `fy host`'s
        # foreground terminal, so anything the mint subprocess inherits is a live, readable fd
        # that never delivers data. A `gcloud` that reads stdin must not be able to block on it.
        proc = subprocess.Popen(
            [sys.executable, str(MINTER_PY)],
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
        procs.append(proc)
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            with socket.socket() as s:
                s.settimeout(0.3)
                if s.connect_ex(("127.0.0.1", port)) == 0:
                    break
            time.sleep(0.1)
        else:
            raise AssertionError("minter did not start listening")
        return port, log

    yield _make
    for proc in procs:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()


@pytest.fixture
def minter(make_minter):
    """The happy-path minter: `gcloud` prints a marker token."""
    return make_minter(f"print({_FAKE_TOKEN!r})")


def test_minter_grants_allowed_sa_and_logs_without_the_token(minter):
    port, log = minter
    code, body = _post(port, {"sa": _ALLOWED})
    assert code == 200 and body["access_token"] == _FAKE_TOKEN
    entry = _entries(log)[-1]
    assert entry["sa"] == _ALLOWED and entry["status"] == 200 and entry["user"] is False
    # the security-critical invariant: the minted token is NEVER written to the log
    assert _FAKE_TOKEN not in log.read_text()


def test_minter_refuses_unlisted_sa_and_logs_the_refusal(minter):
    port, log = minter
    other = "intruder@acme-staging.iam.gserviceaccount.com"
    code, body = _post(port, {"sa": other})
    assert code == 403 and "allowlist" in body["error"]
    entry = _entries(log)[-1]
    assert entry["status"] == 403 and entry["sa"] == other and "allowlist" in entry["error"]


def test_minter_logs_the_gcloud_error_without_the_impersonation_warning(make_minter):
    # gcloud prefixes impersonated calls with a WARNING banner; verbatim it eats the log's
    # truncation budget and hides the ERROR line (which is exactly what made a real reauth
    # failure undiagnosable from the log). The minter must log the ERROR, not the banner.
    port, log = make_minter(
        "import sys\n"
        "sys.stderr.write('WARNING: This command is using service account impersonation. "
        "All API calls will be executed as [sa].\\n')\n"
        "sys.stderr.write('ERROR: (gcloud.auth.print-access-token) There was a problem "
        "refreshing your current auth tokens: invalid_rapt\\n')\n"
        "sys.exit(1)"
    )
    code, body = _post(port, {"sa": _ALLOWED})
    assert code == 502
    assert "invalid_rapt" in body["error"]
    assert "WARNING" not in body["error"]
    entry = _entries(log)[-1]
    assert entry["status"] == 502 and "invalid_rapt" in entry["error"]


def test_mint_never_waits_on_stdin(make_minter):
    """The mint subprocess must get a CLOSED stdin, so gcloud can never decide it is able to
    prompt. `fy host` runs the supervisor in a foreground terminal, so an inherited stdin is a
    real TTY: gcloud then answers a reauth requirement with an interactive prompt and blocks
    forever on input nobody will type — while `capture_output=True` swallows the prompt text, so
    the daemon looks merely slow. Observed live: the box saw NO response for 170s from a minter
    whose own subprocess timeout is 30s, across a clean restart and a fresh `gcloud auth login`.
    Non-interactive, gcloud instead exits with "cannot prompt during non-interactive execution",
    which is a 502 the caller can act on."""
    port, _log = make_minter(
        "import sys\n"
        # Blocks forever on an inherited pipe/TTY; returns '' at once on /dev/null.
        "sys.stdin.read()\n"
        f"print({_FAKE_TOKEN!r})"
    )
    code, body = _post(port, {"sa": _ALLOWED})
    assert code == 200 and body["access_token"] == _FAKE_TOKEN


def test_mint_timeout_bounds_the_RESPONSE_not_just_the_child(make_minter):
    """A mint that overruns must still ANSWER. `subprocess.run(timeout=…)` only bounds the direct
    child: on expiry it kills that child and then re-reads the pipes to reap, so a surviving
    grandchild holding the inherited stdout blocks the second read indefinitely and the handler
    thread never returns. gcloud is a wrapper that execs Python and can leave helpers behind, so
    the nominal 30s bound silently becomes unbounded — the caller gets no status at all."""
    port, log = make_minter(
        "import subprocess, sys\n"
        # A grandchild that outlives the killed parent while holding its stdout.
        "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])\n"
        "import time; time.sleep(30)",
        GCP_MINT_TIMEOUT="2",
    )
    start = time.monotonic()
    code, body = _post(port, {"sa": _ALLOWED})
    assert code == 502, f"expected a bounded failure, got {code}"
    assert time.monotonic() - start < 20, "handler outlived its own mint timeout"
    assert "error" in body
    assert _entries(log)[-1]["status"] == 502


def test_reauth_failure_names_the_host_side_fix(make_minter):
    """A lapsed host session is the one mint failure an operator can fix in one command, and it
    reads as a generic 502 buried in the mint log. Surface the remedy in the error itself."""
    port, _log = make_minter(
        "import sys\n"
        "sys.stderr.write('ERROR: (gcloud.auth.print-access-token) There was a problem "
        "refreshing your current auth tokens: Reauthentication failed. cannot prompt during "
        "non-interactive execution.\\n')\n"
        "sys.exit(1)"
    )
    code, body = _post(port, {"sa": _ALLOWED})
    assert code == 502
    assert "gcloud auth login" in body["error"], body["error"]


def test_capability_probe_mints_non_interactively_too(monkeypatch):
    """The probe is what makes a lapse VISIBLE, so it has to fail the same way the daemon does.
    Run with a terminal it reports `✓ mints ok` from an interactive reauth prompt the daemon can
    never satisfy — which is exactly the green-host/dead-box split seen live: `fy state` showed
    `✓ your gcloud identity mints ok` while every real mint hung.

    So the probe runs gcloud through the minter's OWN runner (closed stdin, its own process group,
    a bound that survives a grandchild holding the pipes) rather than a look-alike — the two
    cannot drift apart. Only the gcloud probes: the port round-trip is a live socket call."""
    from foldyard.plugins.gcp_metadata import minter

    calls: list[tuple[list[str], int]] = []

    def _fake_run(args, timeout):
        calls.append((list(args), timeout))
        return "tok"

    monkeypatch.setattr(minter, "_run_noninteractive", _fake_run)
    monkeypatch.setattr(gcp, "gcp_project", lambda: "acme-staging")
    monkeypatch.setattr(gcp, "app_sa", lambda: _ALLOWED)

    probes = [
        p for p in gcp.GcpPlugin().capability_probes({"gcp": "user"}) if p.name != "gcp-minter-port"
    ]
    assert probes, "gcp=user must publish gcloud probes"
    for probe in probes:
        ok, detail = probe.check()
        assert ok, detail
        assert "tok" not in detail  # the token itself is never surfaced
    assert calls, "probe never invoked gcloud"
    for args, timeout in calls:
        assert args[:3] == ["gcloud", "auth", "print-access-token"]
        assert timeout <= 20


def test_capability_probe_reports_the_gcloud_error_text(monkeypatch):
    """A non-zero gcloud is the finding; its stderr is the fix. Both must survive the runner's
    CalledProcessError."""
    from foldyard.plugins.gcp_metadata import minter

    def _fail(args, timeout):
        raise subprocess.CalledProcessError(1, args, "", "ERROR: Reauthentication failed.\n")

    monkeypatch.setattr(minter, "_run_noninteractive", _fail)
    monkeypatch.setattr(gcp, "gcp_project", lambda: "acme-staging")
    monkeypatch.setattr(gcp, "app_sa", lambda: _ALLOWED)

    probes = [
        p for p in gcp.GcpPlugin().capability_probes({"gcp": "user"}) if p.name != "gcp-minter-port"
    ]
    for probe in probes:
        ok, detail = probe.check()
        assert not ok
        assert "Reauthentication failed" in detail, detail


def test_emulator_keeps_a_refusal_body_that_is_not_json():
    """A shadowing forwarder or a proxy in the path answers with text or HTML, not the minter's
    JSON — exactly the case the reason exists to explain, and json.loads raising used to erase
    it down to a bare `minter 502`."""
    from foldyard.plugins.gcp_metadata import server

    err = urllib.error.HTTPError(
        url="http://minter",
        code=502,
        msg="Bad Gateway",
        hdrs=email.message.Message(),
        fp=io.BytesIO(b"<html><body>upstream connect error</body></html>"),
    )
    reason = server._reason(err)
    assert "502" in reason
    assert "upstream connect error" in reason, reason


def test_emulator_surfaces_the_minters_reason_not_just_no_token():
    """The emulator answers 404 for any mint failure (google-auth must see "no service account",
    not a 5xx) — but the REASON has to survive. A refusal is an HTTPError whose str() is just
    "Bad Gateway", so reporting the exception alone turns "run `gcloud auth login` on the host"
    into "minter down or mode off" and points debugging at the wrong machine entirely."""
    from foldyard.plugins.gcp_metadata import server

    err = urllib.error.HTTPError(
        url="http://minter",
        code=502,
        msg="Bad Gateway",
        hdrs=email.message.Message(),
        fp=io.BytesIO(
            json.dumps(
                {"error": "gcloud failed: Reauthentication failed — run `gcloud auth login`"}
            ).encode()
        ),
    )
    reason = server._reason(err)
    assert "502" in reason
    assert "gcloud auth login" in reason, reason


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v", *sys.argv[1:]]))


# ── the port round-trip probe ─────────────────────────────────────────────────────────
#
# "Listening" is not "ours". VS Code's port forwarding bound 127.0.0.1:<minter port> on the host
# and the daemon's own *:<port> bind lost every loopback connection to it, so the box's mints hung
# forever while `fy state` reported `✓ daemons gcp-minter listening on :8188` — true, and useless.
# These pin a check that can tell the three states apart: ours answering, nothing answering, and
# somebody else holding the port.


def _serve(handler_cls) -> tuple[int, HTTPServer]:
    """Run a throwaway HTTP server on a free port; returns (port, server). Caller shuts it down."""
    import threading

    srv = HTTPServer(("127.0.0.1", 0), handler_cls)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv.server_address[1], srv


def test_minter_answers_a_liveness_get_and_does_not_log_it(minter):
    """The probe needs an answer that only the real minter can give, and it must not pollute the
    mint log the TUI's GCP Tokens panel tails — a health check firing every interval would bury
    the actual token events it exists to show."""
    port, log = minter
    before = log.read_text() if log.exists() else ""

    with urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=10) as r:
        body = json.loads(r.read())

    assert r.status == 200
    assert body["foldyard"] == gcp._MINTER_MARKER
    assert (log.read_text() if log.exists() else "") == before


def test_probe_passes_against_the_real_minter(minter, monkeypatch):
    port, _log = minter
    monkeypatch.setattr(gcp.config, "gcp_minter_port", lambda: port)
    ok, detail = gcp._minter_port_answering()
    assert ok, detail


def test_probe_ignores_host_proxy_settings(minter, monkeypatch):
    """The probe must test the direct 127.0.0.1 path the VM gateway dials. With HTTP_PROXY set
    and no matching NO_PROXY, urllib routes even loopback through the proxy — so a dead proxy in
    the operator's shell read as a dead minter, and a live one tested the wrong path entirely."""
    port, _log = minter
    monkeypatch.setattr(gcp.config, "gcp_minter_port", lambda: port)
    dead = f"http://127.0.0.1:{_free_port()}"
    for var in ("HTTP_PROXY", "http_proxy", "HTTPS_PROXY", "https_proxy"):
        monkeypatch.setenv(var, dead)
    for var in ("NO_PROXY", "no_proxy"):
        monkeypatch.delenv(var, raising=False)

    ok, detail = gcp._minter_port_answering()
    assert ok, detail


def test_probe_fails_when_another_process_holds_the_port(monkeypatch):
    """The hijack case. A forwarder answers connections — it just isn't the minter — so anything
    that only checks "is the port open" passes here, which is exactly how this went undiagnosed."""

    class _Impostor(BaseHTTPRequestHandler):
        def do_GET(self):
            body = b"{}"
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: object) -> None:
            pass

    port, srv = _serve(_Impostor)
    try:
        monkeypatch.setattr(gcp.config, "gcp_minter_port", lambda: port)
        ok, detail = gcp._minter_port_answering()
    finally:
        srv.shutdown()
    assert not ok
    assert str(port) in detail


def test_probe_fails_when_nothing_listens(monkeypatch):
    monkeypatch.setattr(gcp.config, "gcp_minter_port", lambda: _free_port())
    ok, detail = gcp._minter_port_answering()
    assert not ok
    assert detail


def test_the_probe_is_published_on_every_rung_that_runs_a_minter(monkeypatch):
    """off has no minter, so no probe; every other rung promises tokens THROUGH that port."""
    monkeypatch.setattr(gcp, "gcp_project", lambda: "acme-staging")
    monkeypatch.setattr(gcp, "app_sa", lambda: _ALLOWED)
    monkeypatch.setattr(gcp, "devbox_log_sa", lambda: _ALLOWED)
    plugin = gcp.GcpPlugin()
    for rung in ("logs", "sa", "user"):
        names = [p.name for p in plugin.capability_probes({"gcp": rung})]
        assert "gcp-minter-port" in names, rung
    assert plugin.capability_probes({"gcp": "off"}) == []
