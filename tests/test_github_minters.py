"""The two packaged github minter KINDS — `github_app_token` and `gh_cli_token`.

These replaced consumer scripts under the repo's `dev_vm_dir` that the host executed on every mint
(≈hourly while `github=app` is on, plus at every proxy relaunch and 401 replay). That made any
write to the checkout — an in-box agent, a package postinstall, a branch under review — code
execution as the operator, in the process tree holding every credential, and `uv run --script`
resolved PEP 723 dependencies from the network at mint time. See
ADR-0023; here we pin the replacement's behaviour.

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
        "GH_PEM_B64",
        "GH_APP_ID",
        "GH_INSTALLATION_ID",
        "GH_REPO",
        "GH_APP_PERMISSIONS",
        "http_proxy",
        "https_proxy",
        "HTTP_PROXY",
        "HTTPS_PROXY",
    ):
        monkeypatch.delenv(var, raising=False)


# ── the PEM source ladder ─────────────────────────────────────────────────────────────


def test_pem_comes_from_host_env_base64(monkeypatch):
    # ONE source: host.env is single-line KEY=VALUE (supervisor.load_host_env), so base64 is the
    # only shape a PEM can take there — and it's what the capture prompt writes. A second source
    # (a host file) bought nothing — a host file is exactly as trusted as host.env — and cost a
    # branch in every place that asks whether the key is present.
    monkeypatch.setenv("GH_PEM_B64", base64.b64encode(_FAKE_PEM.encode()).decode())
    assert gat.pem() == _FAKE_PEM


def test_pem_absent_names_the_capture_path(monkeypatch):
    # The error is the operator's next action, not a stack trace: the prompt captures it.
    with pytest.raises(SystemExit) as e:
        gat.pem()
    assert "fy box up" in str(e.value) and "GH_PEM_B64" in str(e.value)


def test_pem_b64_garbage_fails_loudly_rather_than_signing_junk(monkeypatch):
    monkeypatch.setenv("GH_PEM_B64", "-----BEGIN RSA PRIV")  # a truncated raw paste, not base64
    with pytest.raises(SystemExit) as e:
        gat.pem()
    assert "not valid base64" in str(e.value)


# ── permissions (the token's down-scoping) ────────────────────────────────────────────


def test_permissions_default_is_the_comment_only_pair():
    assert gat.permissions() == {"pull_requests": "write", "issues": "write"}


def test_permissions_override_narrows(monkeypatch):
    monkeypatch.setenv("GH_APP_PERMISSIONS", '{"issues": "read"}')
    assert gat.permissions() == {"issues": "read"}


@pytest.mark.parametrize("bad", ["{not json", '["a"]', "42"])
def test_permissions_malformed_is_fatal_not_a_silent_widening(monkeypatch, bad):
    # A consumer who MEANT to narrow the token must never get the wider default from a typo.
    monkeypatch.setenv("GH_APP_PERMISSIONS", bad)
    with pytest.raises(SystemExit):
        gat.permissions()


# ── the App JWT ───────────────────────────────────────────────────────────────────────


def _key():
    """A throwaway RSA key. cryptography rides in with pyjwt[crypto]; skip if absent (a bare
    install without the host extra can't mint anyway)."""
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


def test_installation_token_posts_a_downscoped_body(monkeypatch):
    stub = _StubOpener({"token": "ghs_realtoken"})
    monkeypatch.setattr(gat, "_opener", lambda skip_port=None: stub)
    monkeypatch.setattr(gat, "app_jwt", lambda key, app_id, now=None: "signed.jwt")
    token = gat.installation_token("1", "22", "Tangible", _FAKE_PEM)
    assert token == "ghs_realtoken"
    (req,) = stub.seen
    assert req.full_url.endswith("/app/installations/22/access_tokens")
    body = json.loads(req.data)
    # Re-scoped to the ONE repo + comment permissions, belt-and-braces against App-permission drift.
    assert body == {
        "repositories": ["Tangible"],
        "permissions": {"pull_requests": "write", "issues": "write"},
    }
    assert req.headers["Authorization"] == "Bearer signed.jwt"


def test_installation_token_without_a_token_in_the_response_fails(monkeypatch):
    monkeypatch.setattr(gat, "_opener", lambda skip_port=None: _StubOpener({"message": "nope"}))
    monkeypatch.setattr(gat, "app_jwt", lambda key, app_id, now=None: "signed.jwt")
    with pytest.raises(SystemExit) as e:
        gat.installation_token("1", "22", "Tangible", _FAKE_PEM)
    assert "no token" in str(e.value)


def test_main_prints_the_value_ttl_contract(monkeypatch, capsys):
    monkeypatch.setenv("GH_APP_ID", "1")
    monkeypatch.setenv("GH_INSTALLATION_ID", "22")
    monkeypatch.setenv("GH_REPO", "Tangible")
    monkeypatch.setenv("GH_PEM_B64", base64.b64encode(_FAKE_PEM.encode()).decode())
    monkeypatch.setattr(gat, "installation_token", lambda *a: "ghs_x")
    assert gat.main([]) == 0
    out = json.loads(capsys.readouterr().out)
    # The proxy re-mints `ttl` seconds after each mint, so it must be SHORTER than GitHub's 1 h.
    assert out == {"value": "Bearer ghs_x", "ttl": 3300}


def test_main_missing_identity_exits_nonzero_without_minting(monkeypatch, capsys):
    # Non-zero ⇒ the addon logs a mint failure and keeps any cached token, degrading THIS host only.
    monkeypatch.setattr(
        gat, "installation_token", lambda *a: pytest.fail("must not mint without the identity")
    )
    assert gat.main([]) == 1
    assert "GH_APP_ID" in capsys.readouterr().err


def test_module_runs_as_a_subprocess_and_reports_a_missing_identity():
    # The real invocation shape the daemon uses (`python -m …`), proving the module is importable
    # with no package-level import of pyjwt and that a SystemExit(str) becomes exit 1 + stderr.
    out = subprocess.run(
        [sys.executable, "-m", "foldyard.plugins.github_app_token", "--own-proxy-port", "41000"],
        capture_output=True,
        text=True,
        timeout=60,
        env={"PATH": "/usr/bin:/bin", "PYTHONPATH": "src"},
        cwd=str(__import__("pathlib").Path(__file__).resolve().parent.parent),
    )
    assert out.returncode == 1
    assert "GH_APP_ID" in out.stderr


# ── the gh-cli kind (the github=user emergency) ────────────────────────────────────────


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


# ── the minter env allowlist (the addon withholds the rest of host.env) ────────────────


def test_app_rule_declares_exactly_the_env_its_minter_reads():
    from foldyard import config
    from foldyard.plugins import github

    (rule,) = github.GithubPlugin().proxy_rules({"github": "app"})
    assert set(rule.env) == {
        "GH_APP_ID",
        "GH_INSTALLATION_ID",
        "GH_REPO",
        "GH_PEM_B64",
        "GH_APP_PERMISSIONS",
    }
    # `requires` (the daemon spawn gate) stays narrower than `env` (what the minter may read) —
    # they answer different questions and conflating them either kills all egress or over-shares.
    assert set(rule.requires) < set(rule.env)
    # The gh-cli kind needs nothing from host.env: `gh` holds its own credential.
    (user,) = github.GithubPlugin().proxy_rules({"github": "user"})
    assert user.env == ()
    del config


# ── the capability probe (does github=app still work RIGHT NOW?) ───────────────────────


def test_app_reachable_reports_the_app_slug(monkeypatch):
    monkeypatch.setenv("GH_APP_ID", "1234567")
    monkeypatch.setenv("GH_PEM_B64", base64.b64encode(_FAKE_PEM.encode()).decode())
    monkeypatch.setattr(gat, "app_jwt", lambda key, app_id, now=None: "signed.jwt")
    stub = _StubOpener({"slug": "tangible-pr-bot"})
    monkeypatch.setattr(gat, "_opener", lambda skip_port=None: stub)
    ok, detail = gat.app_reachable()
    assert ok and "tangible-pr-bot" in detail
    # /app is METADATA — a probe on a timer must not manufacture installation tokens.
    (req,) = stub.seen
    assert req.full_url.endswith("/app") and req.get_method() == "GET"


def test_app_reachable_names_the_actual_problem(monkeypatch):
    ok, detail = gat.app_reachable()
    assert not ok and "GH_APP_ID" in detail  # no identity
    monkeypatch.setenv("GH_APP_ID", "1234567")
    ok, detail = gat.app_reachable()
    assert not ok and "GH_PEM_B64" in detail  # no key
    # A PEM-shaped-but-unusable key fails at signing, and says so rather than raising.
    monkeypatch.setenv("GH_PEM_B64", base64.b64encode(_FAKE_PEM.encode()).decode())
    ok, detail = gat.app_reachable()
    assert not ok and "App key" in detail


def test_app_reachable_never_leaks_the_jwt_on_failure(monkeypatch):
    import email.message
    import urllib.error

    monkeypatch.setenv("GH_APP_ID", "1234567")
    monkeypatch.setenv("GH_PEM_B64", base64.b64encode(_FAKE_PEM.encode()).decode())
    monkeypatch.setattr(gat, "app_jwt", lambda key, app_id, now=None: "signed.SECRET.jwt")

    class _Failing:
        def open(self, req, timeout=None):
            raise urllib.error.HTTPError(
                req.full_url, 401, "Unauthorized", email.message.Message(), io.BytesIO(b"bad")
            )

    monkeypatch.setattr(gat, "_opener", lambda skip_port=None: _Failing())
    ok, detail = gat.app_reachable()
    # The detail is rendered on the posture dashboards, so it must carry the FIX, not the credential.
    assert not ok and "401" in detail and "rotated" in detail
    assert "SECRET" not in detail


def test_probe_is_contributed_only_on_the_app_rung(monkeypatch, tmp_path):
    from foldyard import config
    from foldyard.plugins import github

    monkeypatch.setattr(config, "host_env_file", lambda: tmp_path / "host.env")
    p = github.GithubPlugin()
    assert p.capability_probes({"github": "off"}) == []
    assert p.capability_probes({"github": "user"}) == []  # gh's own failure is local + immediate
    (probe,) = p.capability_probes({"github": "app"})
    assert probe.axis == "github" and probe.name == "github-app-identity"
    # A probe error must degrade the axis, never take the supervisor tick down.
    monkeypatch.setattr(
        gat, "app_reachable", lambda *a: (_ for _ in ()).throw(RuntimeError("boom"))
    )
    ok, detail = probe.check()
    assert not ok and "RuntimeError" in detail


def test_permissions_override_may_narrow_but_never_widen(monkeypatch):
    # GH_APP_PERMISSIONS comes from [plugins.github].permissions — REPO config, which anything able
    # to write the checkout can edit. GitHub caps a token at what the App installation holds, but
    # WITHIN that cap a widened map is real escalation (a contents:write App would hand the box a
    # push), so the default is also the ceiling here.
    monkeypatch.setenv("GH_APP_PERMISSIONS", '{"contents": "write"}')
    with pytest.raises(SystemExit, match="may only NARROW"):
        gat.permissions()
    monkeypatch.setenv("GH_APP_PERMISSIONS", '{"issues": "admin"}')
    with pytest.raises(SystemExit, match="exceeds the default"):
        gat.permissions()
    # Narrowing stays fine — that's what the knob is for.
    monkeypatch.setenv("GH_APP_PERMISSIONS", '{"issues": "read", "pull_requests": "write"}')
    assert gat.permissions() == {"issues": "read", "pull_requests": "write"}
