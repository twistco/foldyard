"""The two packaged github minter KINDS — `github_app_token` and `gh_cli_token`.

These replaced consumer scripts under the repo's `dev_vm_dir` that the host executed on every mint
(≈hourly while `github=app` is on, plus at every proxy relaunch and 401 replay). That made any
write to the checkout — an in-box agent, a package postinstall, a branch under review — code
execution as the operator, in the process tree holding every credential, and `uv run --script`
resolved PEP 723 dependencies from the network at mint time. See
ADR-0023; here we pin the replacement's behaviour. Since ADR-0031 they are `[[inject]]` KINDS:
the App identity arrives on argv from the row, the PEM by the NAME of the switch's derived var, and
the token carries whatever the App installation grants — foldyard neither narrows nor caps it.

No network: the HTTP call is driven through a stub opener, so these always run.
"""

from __future__ import annotations

import base64
import io
import json
import subprocess
import sys

import pytest

from foldyard.plugins import gh_cli_token
from foldyard.plugins import github_app_token as gat

# A syntactically-real PEM is only needed where signing happens (see _key); everywhere else the
# PEM is opaque bytes, so a marker string keeps the tests fast and readable.
_FAKE_PEM = "-----BEGIN RSA PRIVATE KEY-----\nnot-a-real-key\n-----END RSA PRIVATE KEY-----\n"


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    """No ambient GH_*/proxy vars leaking in from the developer's shell or the CI runner."""
    for var in (
        "FY_INJECT_GITHUB",
        "http_proxy",
        "https_proxy",
        "HTTP_PROXY",
        "HTTPS_PROXY",
    ):
        monkeypatch.delenv(var, raising=False)


# ── the PEM source ladder ─────────────────────────────────────────────────────────────


def test_pem_comes_from_the_named_var_base64(monkeypatch):
    # ONE source: host.env is single-line KEY=VALUE (supervisor.load_host_env), so base64 is the
    # only shape a PEM can take there — and it's what the capture prompt writes. The var is named on
    # argv by the rule (FY_INJECT_<SWITCH>), and it's the only host.env name the addon lets this
    # minter read.
    monkeypatch.setenv("FY_INJECT_GITHUB", base64.b64encode(_FAKE_PEM.encode()).decode())
    assert gat.pem("FY_INJECT_GITHUB") == _FAKE_PEM


def test_pem_absent_names_the_var_and_the_capture_path(monkeypatch):
    # The error is the operator's next action, not a stack trace: the prompt captures it.
    with pytest.raises(SystemExit) as e:
        gat.pem("FY_INJECT_GITHUB")
    assert "fy mode" in str(e.value) and "FY_INJECT_GITHUB" in str(e.value)


def test_pem_b64_garbage_fails_loudly_rather_than_signing_junk(monkeypatch):
    monkeypatch.setenv("FY_INJECT_GITHUB", "-----BEGIN RSA PRIV")  # a truncated raw paste
    with pytest.raises(SystemExit) as e:
        gat.pem("FY_INJECT_GITHUB")
    assert "not valid base64" in str(e.value)


def test_the_permission_ceiling_is_gone():
    # ADR-0031: the App installation's permissions ARE the scope. A second place that had to agree
    # with the App (the package default as a ceiling, a consumer map under it) is what turned a
    # granted `actions: read` into a morning of 401s.
    for name in ("permissions", "_DEFAULT_PERMISSIONS", "_LEVELS"):
        assert not hasattr(gat, name)


# ── the App JWT ───────────────────────────────────────────────────────────────────────


def _key():
    """A throwaway RSA key. cryptography rides in with pyjwt[crypto]; skip if absent (an install
    stripped of it — the box's shape — can't mint anyway)."""
    pytest.importorskip("jwt")
    rsa = pytest.importorskip("cryptography.hazmat.primitives.asymmetric.rsa")
    serialization = pytest.importorskip("cryptography.hazmat.primitives.serialization")
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()
    return key, pem


def test_app_jwt_claims_are_inside_githubs_10_minute_cap():
    import jwt

    key, pem = _key()
    token = gat.app_jwt(pem, "1234567", now=1_000_000)
    claims = jwt.decode(
        token, key.public_key(), algorithms=["RS256"], options={"verify_exp": False}
    )
    assert claims["iss"] == "1234567"
    assert claims["iat"] == 1_000_000 - 60  # backdated for clock skew
    assert claims["exp"] - claims["iat"] <= 600  # GitHub rejects anything beyond 10 min
    assert claims["exp"] == 1_000_000 + 540


# ── proxy selection: never mint through OUR own listener ──────────────────────────────


def test_env_proxies_skips_only_our_own_port(monkeypatch):
    # Minting through foldyard's proxy would hand the App JWT to the very rule that rewrites
    # Authorization on api.github.com — so our port is excluded...
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:41000")
    assert gat._env_proxies(skip_port=41000) == {}
    # ...but another LOCAL proxy (a corporate client on loopback) is honoured: on such a Mac it's
    # the only way out, and a mint that can't reach GitHub is a hard failure.
    assert gat._env_proxies(skip_port=8088) == {"https": "http://127.0.0.1:41000"}
    # A remote proxy is always honoured.
    monkeypatch.setenv("HTTPS_PROXY", "http://corp.example:3128")
    assert gat._env_proxies(skip_port=41000) == {"https": "http://corp.example:3128"}


def test_env_proxies_without_a_known_own_port_drops_local(monkeypatch):
    # A hand-run mint can't know our port; dropping local is the safe direction (a failed mint is
    # loud, a JWT handed to our own rewriter is not).
    monkeypatch.setenv("HTTPS_PROXY", "http://localhost:41000")
    assert gat._env_proxies() == {}


def test_opener_never_uses_implicit_env_discovery(monkeypatch):
    # An explicit ProxyHandler is installed even when it's empty, so urllib's implicit env lookup
    # can't reintroduce the self-injection case.
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:41000")
    seen: list = []
    monkeypatch.setattr(gat.urllib.request, "ProxyHandler", lambda proxies: seen.append(proxies))
    monkeypatch.setattr(gat.urllib.request, "build_opener", lambda *handlers: None)
    gat._opener(skip_port=41000)
    assert seen == [{}]  # an EXPLICIT empty proxy map, so urllib never reads the env itself


@pytest.mark.parametrize(
    "args,expected",
    [
        ([], None),
        (["--own-proxy-port", "41000"], 41000),
        (["--own-proxy-port"], None),  # tolerant: a mint must not die on a malformed flag
        (["--own-proxy-port", "nope"], None),
    ],
)
def test_skip_port_parsing_is_tolerant(args, expected):
    assert gat._skip_port(args) == expected


# ── the exchange itself ───────────────────────────────────────────────────────────────


class _StubOpener:
    def __init__(self, payload: dict, status: int = 200):
        self.payload, self.status, self.seen = payload, status, []

    def open(self, req, timeout=None):
        self.seen.append(req)
        return io.BytesIO(json.dumps(self.payload).encode())


def test_installation_token_asks_for_the_installations_own_scope(monkeypatch):
    stub = _StubOpener({"token": "ghs_realtoken"})
    monkeypatch.setattr(gat, "_opener", lambda skip_port=None: stub)
    monkeypatch.setattr(gat, "app_jwt", lambda key, app_id, now=None: "signed.jwt")
    token = gat.installation_token("1", "22", [], _FAKE_PEM)
    assert token == "ghs_realtoken"
    (req,) = stub.seen
    assert req.full_url.endswith("/app/installations/22/access_tokens")
    # No `permissions` key: GitHub then issues the installation's full scope — the one place the
    # operator changes it (the App's settings) is the one place that decides it.
    assert json.loads(req.data) == {}
    assert req.headers["Authorization"] == "Bearer signed.jwt"


def test_installation_token_narrows_to_the_declared_repositories(monkeypatch):
    # `repositories` can only narrow (GitHub refuses one outside the installation), so it stays.
    stub = _StubOpener({"token": "ghs_x"})
    monkeypatch.setattr(gat, "_opener", lambda skip_port=None: stub)
    monkeypatch.setattr(gat, "app_jwt", lambda key, app_id, now=None: "signed.jwt")
    gat.installation_token("1", "22", ["Tangible", "docs"], _FAKE_PEM)
    assert json.loads(stub.seen[0].data) == {"repositories": ["Tangible", "docs"]}


def test_installation_token_without_a_token_in_the_response_fails(monkeypatch):
    monkeypatch.setattr(gat, "_opener", lambda skip_port=None: _StubOpener({"message": "nope"}))
    monkeypatch.setattr(gat, "app_jwt", lambda key, app_id, now=None: "signed.jwt")
    with pytest.raises(SystemExit) as e:
        gat.installation_token("1", "22", ["Tangible"], _FAKE_PEM)
    assert "no token" in str(e.value)


_ARGV = ["--app-id", "1", "--installation-id", "22", "--pem-env", "FY_INJECT_GITHUB"]


def test_main_reads_the_identity_from_argv_and_prints_the_value_ttl_contract(monkeypatch, capsys):
    monkeypatch.setenv("FY_INJECT_GITHUB", base64.b64encode(_FAKE_PEM.encode()).decode())
    seen = []
    monkeypatch.setattr(gat, "installation_token", lambda *a: seen.append(a) or "ghs_x")
    argv = [*_ARGV, "--repository", "Tangible", "--repository", "docs", "--own-proxy-port", "41000"]
    assert gat.main(argv) == 0
    assert seen == [("1", "22", ["Tangible", "docs"], _FAKE_PEM, 41000)]
    out = json.loads(capsys.readouterr().out)
    # The proxy re-mints `ttl` seconds after each mint, so it must be SHORTER than GitHub's 1 h.
    assert out == {"value": "Bearer ghs_x", "ttl": 3300}


@pytest.mark.parametrize("drop", ["--app-id", "--installation-id", "--pem-env"])
def test_main_missing_identity_exits_nonzero_without_minting(monkeypatch, capsys, drop):
    # Non-zero ⇒ the addon logs a mint failure and keeps any cached token, degrading THIS host only.
    monkeypatch.setattr(
        gat, "installation_token", lambda *a: pytest.fail("must not mint without the identity")
    )
    i = _ARGV.index(drop)
    assert gat.main(_ARGV[:i] + _ARGV[i + 2 :]) == 1
    assert drop in capsys.readouterr().err


def test_module_runs_as_a_subprocess_and_reports_a_missing_key():
    # The real invocation shape the daemon uses (`python -m …`), proving the module is importable
    # with no package-level import of pyjwt and that a SystemExit(str) becomes exit 1 + stderr.
    out = subprocess.run(
        [sys.executable, "-m", "foldyard.plugins.github_app_token", *_ARGV],
        capture_output=True,
        text=True,
        timeout=60,
        env={"PATH": "/usr/bin:/bin", "PYTHONPATH": "src"},
        cwd=str(__import__("pathlib").Path(__file__).resolve().parent.parent),
    )
    assert out.returncode == 1
    assert "FY_INJECT_GITHUB" in out.stderr


# ── the gh-cli kind (the operator's own token, an emergency switch) ───────────────────


def _fake_gh(monkeypatch, *, stdout="", stderr="", rc=0, missing=False):
    def fake_run(cmd, **kwargs):
        assert cmd == ["gh", "auth", "token"]
        if missing:
            raise FileNotFoundError("gh")
        if rc:
            raise subprocess.CalledProcessError(rc, cmd, stderr=stderr)
        return subprocess.CompletedProcess(cmd, 0, stdout=stdout, stderr="")

    monkeypatch.setattr(gh_cli_token.subprocess, "run", fake_run)


def test_gh_cli_token_wraps_the_users_token_with_a_short_ttl(monkeypatch, capsys):
    _fake_gh(monkeypatch, stdout="gho_usertoken\n")
    assert gh_cli_token.main([]) == 0
    out = json.loads(capsys.readouterr().out)
    # Short on purpose: it bounds how long a revoked token keeps working (`gh auth logout`).
    assert out == {"value": "Bearer gho_usertoken", "ttl": 240}


def test_gh_cli_token_missing_cli_and_login_are_actionable(monkeypatch, capsys):
    _fake_gh(monkeypatch, missing=True)
    assert gh_cli_token.main([]) == 1
    assert "brew install gh" in capsys.readouterr().err
    _fake_gh(monkeypatch, rc=1, stderr="not logged in")
    assert gh_cli_token.main([]) == 1
    assert "not logged in" in capsys.readouterr().err


def test_gh_cli_token_empty_output_is_a_failure_not_an_empty_injection(monkeypatch, capsys):
    _fake_gh(monkeypatch, stdout="  \n")
    assert gh_cli_token.main([]) == 1
    assert "gh auth login" in capsys.readouterr().err


# ── the capability probe (can we still act as the App RIGHT NOW?) ───────────────────────

_PEM_B64 = base64.b64encode(_FAKE_PEM.encode()).decode()


_INSTALLATION = {
    "id": 22,
    "app_slug": "tangible-pr-bot",
    "repository_selection": "selected",
    "permissions": {"issues": "write", "pull_requests": "write", "actions": "read"},
    "suspended_at": None,
}


def test_the_probe_reads_the_installations_scope_with_the_app_jwt(monkeypatch):
    monkeypatch.setattr(gat, "app_jwt", lambda key, app_id, now=None: "signed.jwt")
    stub = _StubOpener(_INSTALLATION)
    monkeypatch.setattr(gat, "_opener", lambda skip_port=None: stub)
    ok, detail, scope = gat.installation_probe("1234567", "22", _PEM_B64)
    assert ok and "tangible-pr-bot" in detail
    # The detail is a summary (the dashboards give a switch one line); the scope carries the list.
    assert "3 permissions" in detail and "2 at write" in detail
    assert scope == {
        "permissions": {"issues": "write", "pull_requests": "write", "actions": "read"},
        "repository_selection": "selected",
    }
    # ONE metadata GET, authenticated as the App: a probe on a timer must not manufacture
    # installation tokens, and the call that proves the key is the one that reads the scope.
    (req,) = stub.seen
    assert req.full_url.endswith("/app/installations/22") and req.get_method() == "GET"
    assert req.headers["Authorization"] == "Bearer signed.jwt"


@pytest.mark.parametrize(
    "payload",
    [
        {"app_slug": "bot"},  # no permissions at all
        {"app_slug": "bot", "permissions": ["issues"]},  # not a map
        {"app_slug": "bot", "permissions": {"issues": 1}},  # not a level
        {"app_slug": "bot", "permissions": {"issues": "read", "checks": None}},  # one isn't
    ],
)
def test_an_unreadable_scope_is_unavailable_not_guessed(monkeypatch, payload):
    # The App authenticated, so the switch's capability holds; what it grants is unknown, and an
    # empty map would read as "grants nothing" — a guess the next good read would call a drift.
    monkeypatch.setattr(gat, "app_jwt", lambda key, app_id, now=None: "signed.jwt")
    monkeypatch.setattr(gat, "_opener", lambda skip_port=None: _StubOpener(payload))
    ok, detail, scope = gat.installation_probe("1", "22", _PEM_B64)
    assert ok and scope is None and "scope unavailable" in detail


def test_a_level_github_adds_later_is_kept_as_it_reads_not_dropped_with_the_rest(monkeypatch):
    # GitHub's levels are read/write/admin today. A new one must not make the whole map
    # "unavailable": the supervisor would keep the old record for good and stop seeing drift,
    # including a real widening beside the new level. So it is recorded as it reads, and the
    # detail counts it with the levels that can change things: unknown is not read as safe.
    monkeypatch.setattr(gat, "app_jwt", lambda key, app_id, now=None: "signed.jwt")
    payload = {**_INSTALLATION, "permissions": {"issues": "read", "workflows": "maintain"}}
    monkeypatch.setattr(gat, "_opener", lambda skip_port=None: _StubOpener(payload))
    ok, detail, scope = gat.installation_probe("1", "22", _PEM_B64)
    assert ok and scope is not None
    assert scope["permissions"] == {"issues": "read", "workflows": "maintain"}
    assert "2 permissions, 1 at write, admin or a level foldyard doesn't know" in detail


def test_a_suspended_installation_degrades_the_switch(monkeypatch):
    # GitHub refuses to mint for a suspended installation: the switch can't deliver what it says.
    monkeypatch.setattr(gat, "app_jwt", lambda key, app_id, now=None: "signed.jwt")
    payload = {**_INSTALLATION, "suspended_at": "2026-10-01T00:00:00Z"}
    monkeypatch.setattr(gat, "_opener", lambda skip_port=None: _StubOpener(payload))
    ok, detail, scope = gat.installation_probe("1", "22", _PEM_B64)
    assert not ok and "suspended" in detail
    assert scope is not None  # still what it grants: resuming the installation brings it back


def test_the_probe_names_the_actual_problem():
    ok, detail, scope = gat.installation_probe("1234567", "22", "")
    assert not ok and "private key" in detail and scope is None  # no key
    ok, detail, _ = gat.installation_probe("1234567", "22", "-----BEGIN RSA PRIV")
    assert not ok and "base64" in detail  # a truncated raw paste
    # A PEM-shaped-but-unusable key fails at signing, and says so rather than raising.
    ok, detail, _ = gat.installation_probe("1234567", "22", _PEM_B64)
    assert not ok and "App key" in detail


def _failing_opener(code: int, body: bytes = b"bad"):
    import email.message
    import urllib.error

    class _Failing:
        def open(self, req, timeout=None):
            raise urllib.error.HTTPError(
                req.full_url, code, "nope", email.message.Message(), io.BytesIO(body)
            )

    return lambda skip_port=None: _Failing()


def test_the_probe_never_leaks_the_jwt_on_failure(monkeypatch):
    monkeypatch.setattr(gat, "app_jwt", lambda key, app_id, now=None: "signed.SECRET.jwt")
    monkeypatch.setattr(gat, "_opener", _failing_opener(401))
    ok, detail, scope = gat.installation_probe("1234567", "22", _PEM_B64)
    # The detail is rendered on the posture dashboards, so it must carry the FIX, not the credential.
    assert not ok and "401" in detail and "rotated" in detail and scope is None
    assert "SECRET" not in detail


def test_an_unknown_installation_says_which_field_to_check(monkeypatch):
    monkeypatch.setattr(gat, "app_jwt", lambda key, app_id, now=None: "signed.jwt")
    monkeypatch.setattr(gat, "_opener", _failing_opener(404, b'{"message": "Not Found"}'))
    ok, detail, scope = gat.installation_probe("1", "22", _PEM_B64)
    assert not ok and "installation 22" in detail and "installation_id" in detail
    assert scope is None


def test_an_unreachable_github_is_a_failure_without_a_scope(monkeypatch):
    monkeypatch.setattr(gat, "app_jwt", lambda key, app_id, now=None: "signed.jwt")

    class _Down:
        def open(self, req, timeout=None):
            raise OSError("connection refused")

    monkeypatch.setattr(gat, "_opener", lambda skip_port=None: _Down())
    ok, detail, scope = gat.installation_probe("1", "22", _PEM_B64)
    assert not ok and "can't reach" in detail and scope is None
