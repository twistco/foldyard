"""keyless.py — the credential taxonomy + the host-side (Mac) capture of the real key/token.

Pure logic (classify-by-prefix, host.env read/append, the ensure_cred state machine) with the
prompt/echo I/O injected — no real stdin, no real ~/.foldyard. The on-the-wire half (the proxy
actually rewriting the header) is the opt-in proxy e2e; here we prove the box never gets the secret
and the right host.env var is written from a pasted credential."""

from __future__ import annotations

import base64
import json
import time

import pytest

from foldyard import keyless


@pytest.mark.parametrize(
    "secret,expected",
    [
        ("sk-ant-api03-abc", ("ANTHROPIC_API_KEY", "Claude API key")),
        ("sk-ant-oat01-xyz", ("CLAUDE_CODE_OAUTH_TOKEN", "Claude OAuth token")),
        ("sk-proj-deadbeef", ("OPENAI_API_KEY", "OpenAI / Codex API key")),
        ("sk-svcacct-foo", ("OPENAI_API_KEY", "OpenAI / Codex API key")),
        ("  sk-ant-api03-x  ", ("ANTHROPIC_API_KEY", "Claude API key")),  # trims first
        ("ghp_nope", None),
        ("", None),
    ],
)
def test_classify_by_prefix(secret, expected):
    # The Claude sk-ant-… prefixes must beat the bare sk- (order-sensitive) — the "no need to ask
    # which" classifier. An unrecognised value classifies to None so the caller refuses it.
    assert keyless.classify(secret) == expected


def test_host_env_has_matches_supervisor_parsing(tmp_path):
    f = tmp_path / "host.env"
    f.write_text("# a comment\n\nANTHROPIC_API_KEY=real\nNO_EQUALS_LINE\nGH_APP_ID = 7\n")
    assert keyless.host_env_has(f, "ANTHROPIC_API_KEY")
    assert keyless.host_env_has(f, "GH_APP_ID")  # whitespace around = is tolerated
    assert not keyless.host_env_has(f, "OPENAI_API_KEY")
    assert not keyless.host_env_has(tmp_path / "absent.env", "ANYTHING")


def test_append_host_env_creates_0600_and_preserves(tmp_path):
    f = tmp_path / "sub" / "host.env"
    keyless.append_host_env(f, "ANTHROPIC_API_KEY", "sk-ant-real")
    assert f.read_text() == "ANTHROPIC_API_KEY=sk-ant-real\n"
    assert (f.stat().st_mode & 0o777) == 0o600
    # Appends without a trailing-newline gap and keeps the prior content.
    f.write_text("FOO=bar")  # no trailing newline
    keyless.append_host_env(f, "OPENAI_API_KEY", "sk-proj-real")
    assert f.read_text() == "FOO=bar\nOPENAI_API_KEY=sk-proj-real\n"


def _recorder():
    out: list[str] = []
    return out, out.append


def test_ensure_cred_present_is_a_noop(tmp_path):
    f = tmp_path / "host.env"
    f.write_text("ANTHROPIC_API_KEY=already\n")
    out, echo = _recorder()
    status = keyless.ensure_cred(
        f,
        "ANTHROPIC_API_KEY",
        "Claude api-key credential",
        interactive=True,
        prompt=lambda _: "should-not-be-asked",
        echo=echo,
    )
    assert status == "present" and out == []
    assert f.read_text() == "ANTHROPIC_API_KEY=already\n"  # untouched


def test_ensure_cred_non_tty_warns_but_does_not_block(tmp_path):
    f = tmp_path / "host.env"
    out, echo = _recorder()
    status = keyless.ensure_cred(
        f,
        "ANTHROPIC_API_KEY",
        "Claude api-key credential",
        interactive=False,
        prompt=lambda _: "",
        echo=echo,
    )
    assert status == "skipped"
    assert not f.exists()  # nothing stored
    assert any("ANTHROPIC_API_KEY" in m and "host.env" in m for m in out)


def test_ensure_cred_stores_a_matching_pasted_secret(tmp_path):
    f = tmp_path / "host.env"
    _out, echo = _recorder()
    status = keyless.ensure_cred(
        f,
        "ANTHROPIC_API_KEY",
        "Claude api-key credential",
        interactive=True,
        prompt=lambda _: "sk-ant-api03-REAL",
        echo=echo,
    )
    assert status == "stored"
    assert keyless.host_env_has(f, "ANTHROPIC_API_KEY")
    assert "sk-ant-api03-REAL" in f.read_text()


def test_ensure_cred_echoes_the_how_hint_before_prompting(tmp_path):
    # A first-timer needs to know WHERE to get the value — the `how` hint is echoed at the prompt.
    f = tmp_path / "host.env"
    out, echo = _recorder()
    keyless.ensure_cred(
        f,
        "CLAUDE_CODE_OAUTH_TOKEN",
        "Claude oauth credential",
        interactive=True,
        prompt=lambda _: "sk-ant-oat01-REAL",
        echo=echo,
        how="run `claude setup-token` on the Mac to mint one",
    )
    assert any("claude setup-token" in m for m in out)


def test_claude_oauth_taxonomy_carries_the_setup_token_hint():
    # The OAuth mode's how-to is `claude setup-token` (mirrors what the box-up capture echoes).
    assert "claude setup-token" in keyless.CLAUDE_KEYLESS["oauth"]["how"]
    assert "console.anthropic.com" in keyless.CLAUDE_KEYLESS["api-key"]["how"]


def test_ensure_cred_rejects_a_mismatched_secret(tmp_path):
    # The config says api-key (ANTHROPIC_API_KEY) but the user pastes an OAuth token: store NOTHING
    # so the proxy never injects a wrong-shaped secret; the message names what to fix.
    f = tmp_path / "host.env"
    out, echo = _recorder()
    status = keyless.ensure_cred(
        f,
        "ANTHROPIC_API_KEY",
        "Claude api-key credential",
        interactive=True,
        prompt=lambda _: "sk-ant-oat01-WRONG",
        echo=echo,
    )
    assert status == "mismatch"
    assert not f.exists()
    assert any("Claude OAuth token" in m for m in out)


def test_ensure_cred_empty_input_stores_nothing(tmp_path):
    f = tmp_path / "host.env"
    _out, echo = _recorder()
    status = keyless.ensure_cred(
        f,
        "ANTHROPIC_API_KEY",
        "Claude api-key credential",
        interactive=True,
        prompt=lambda _: "   ",
        echo=echo,
    )
    assert status == "empty" and not f.exists()


def test_taxonomy_shapes():
    # The shared taxonomy the plugin + capture both read. api-key: x-api-key, no scheme prefix.
    api = keyless.CLAUDE_KEYLESS["api-key"]
    assert api["env"] == "ANTHROPIC_API_KEY" and api["header"] == "x-api-key"
    assert api["dummy"].startswith("sk-ant") and api["value_prefix"] == ""
    # oauth: authorization header, "Bearer " prefix the proxy prepends to the bare host.env token.
    oa = keyless.CLAUDE_KEYLESS["oauth"]
    assert oa["env"] == "CLAUDE_CODE_OAUTH_TOKEN" and oa["header"] == "authorization"
    assert oa["dummy"].startswith("sk-ant-oat") and oa["value_prefix"] == "Bearer "
    assert keyless.CLAUDE_KEYLESS_HOST == "api.anthropic.com"
    # The classifier routes a pasted oat token to the same env the oauth mode bakes — they agree.
    classified = keyless.classify("sk-ant-oat01-x")
    assert classified is not None and classified[0] == oa["env"]


def test_codex_taxonomy_shape():
    cx = keyless.CODEX_KEYLESS["api-key"]
    assert cx["env"] == "OPENAI_API_KEY" and cx["header"] == "Authorization"
    assert cx["value_prefix"] == "Bearer " and cx["dummy"].startswith("sk-")
    assert keyless.CODEX_KEYLESS_HOST == "api.openai.com"
    # The classifier routes a pasted OpenAI key to the env the codex mode bakes — they agree.
    classified = keyless.classify("sk-proj-abc")
    assert classified is not None and classified[0] == cx["env"]


def test_codex_account_id_reads_mac_auth_json(tmp_path):
    auth = tmp_path / "auth.json"
    auth.write_text('{"auth_mode":"chatgpt","tokens":{"account_id":"acc-9","access_token":"x"}}')
    assert keyless.codex_account_id(auth) == "acc-9"
    # Missing / malformed / no-tokens → None (so the plugin fails the bootstrap loudly, not silently).
    assert keyless.codex_account_id(tmp_path / "absent.json") is None
    (tmp_path / "bad.json").write_text("{nope")
    assert keyless.codex_account_id(tmp_path / "bad.json") is None


def test_dummy_codex_auth_json_is_chatgpt_with_far_future_exp(tmp_path):
    import base64

    doc = json.loads(keyless.dummy_codex_auth_json("acc-7"))
    assert doc["auth_mode"] == "chatgpt" and doc["tokens"]["account_id"] == "acc-7"
    assert doc["OPENAI_API_KEY"] is None
    assert doc["tokens"]["refresh_token"].startswith("fy-dummy")  # never a real refresh token
    # Both tokens are 3-segment JWTs with a NON-EMPTY signature: codex rejects an empty third
    # segment with "invalid ID token format" (it never verifies the signature, but demands the shape).
    for tok in (doc["tokens"]["id_token"], doc["tokens"]["access_token"]):
        segs = tok.split(".")
        assert len(segs) == 3 and all(segs), f"dummy JWT needs 3 non-empty segments: {tok!r}"
    # The dummy access token's exp is far in the future, so codex in the box never refreshes it.
    payload = doc["tokens"]["access_token"].split(".")[1]
    payload += "=" * (-len(payload) % 4)
    assert json.loads(base64.urlsafe_b64decode(payload))["exp"] == keyless._FAR_FUTURE_EXP


def test_inject_spec_and_dummy_box_args_helpers():
    # The shared helpers the claude + codex plugins both use to derive their rule + dummy.
    spec = keyless.inject_spec(keyless.CODEX_KEYLESS, "api.openai.com", "api-key", "Codex proxy")
    assert spec == {
        "host": "api.openai.com",
        "header": "Authorization",
        "token_env": "OPENAI_API_KEY",
        "value_prefix": "Bearer ",
        "replay_on_401": False,
        "label": "Codex proxy",
    }
    assert keyless.inject_spec(keyless.CODEX_KEYLESS, "h", "nope", "l") is None  # unknown mode
    assert keyless.dummy_box_args(keyless.CODEX_KEYLESS, "api-key") == [
        "-e",
        "OPENAI_API_KEY=sk-dummy",
    ]
    assert keyless.dummy_box_args(keyless.CODEX_KEYLESS, "nope") == []


# ── declared `[[secret]]` capture (plugins.Secret) ─────────────────────────────────────


def _secret(
    *,
    var: str = "GH_PEM_B64",
    label: str = "GitHub App private key (PEM)",
    how: str = "gcloud secrets versions access X | base64",
    pattern: str = "*-----BEGIN *PRIVATE KEY-----*",
    b64: bool = True,
):
    """A Secret with the PEM-ish defaults these tests use."""
    from foldyard.plugins import Secret

    return Secret(var=var, label=label, how=how, pattern=pattern, b64=b64)


# PEM-shaped on purpose (the pattern check needs the armour); the body is "abc". Not a key.
_PEM = "-----BEGIN RSA PRIVATE KEY-----\nabc\n-----END RSA PRIVATE KEY-----\n"  # gitleaks:allow
_PEM_B64 = base64.b64encode(_PEM.encode()).decode()


def test_secret_ok_accepts_base64_of_a_matching_value():
    assert keyless.secret_ok(_PEM_B64, _secret().pattern, True) == _PEM_B64


def test_secret_ok_rejects_a_raw_multiline_paste():
    # host.env is single-line, so the prompt reads ONE line: a raw PEM pasted into it arrives
    # truncated to "-----BEGIN RSA PRIVATE KEY-----". That isn't valid base64, so it's refused —
    # the alternative (storing it) is a broken key that fails as an opaque 401 at mint time.
    assert keyless.secret_ok("-----BEGIN RSA PRIVATE KEY-----", _secret().pattern, True) is None


def test_secret_ok_rejects_base64_of_the_wrong_thing():
    wrong = base64.b64encode(b"ssh-rsa AAAA...").decode()
    assert keyless.secret_ok(wrong, _secret().pattern, True) is None


def test_secret_ok_non_b64_uses_the_pattern_directly():
    assert keyless.secret_ok("ghp_abc123", "ghp_*", False) == "ghp_abc123"
    assert keyless.secret_ok("nope", "ghp_*", False) is None
    # No pattern ⇒ anything non-empty is accepted (the caller still rejects blanks).
    assert keyless.secret_ok("whatever", "", False) == "whatever"


def test_ensure_secret_present_is_a_noop(tmp_path):
    f = tmp_path / "host.env"
    f.write_text(f"GH_PEM_B64={_PEM_B64}\n")
    status = keyless.ensure_secret(
        f,
        _secret(),
        interactive=True,
        prompt=lambda _p: pytest.fail("must not prompt when it's already set"),
        echo=lambda _m: None,
    )
    assert status == "present"


def test_ensure_secret_non_tty_warns_but_does_not_block(tmp_path):
    # A missing credential must never stop the box booting: the minter degrades THIS host (the proxy
    # logs the mint failure) while everything else works.
    f = tmp_path / "host.env"
    out, echo = _recorder()
    status = keyless.ensure_secret(f, _secret(), interactive=False, prompt=lambda _p: "", echo=echo)
    assert status == "skipped" and not f.exists()
    assert any("fy box up" in line for line in out)


def test_ensure_secret_stores_a_pasted_value_and_echoes_the_how_hint(tmp_path):
    f = tmp_path / "host.env"
    out, echo = _recorder()
    status = keyless.ensure_secret(
        f, _secret(), interactive=True, prompt=lambda _p: f"  {_PEM_B64}  ", echo=echo
    )
    assert status == "stored"
    assert f.read_text() == f"GH_PEM_B64={_PEM_B64}\n"
    assert (f.stat().st_mode & 0o777) == 0o600  # a real secret, owner-only
    # The hint is shown so a first-timer isn't guessing — and it's the ONLY thing foldyard does with
    # it: printing, not running.
    assert any("gcloud secrets versions access X | base64" in line for line in out)


def test_ensure_secret_refuses_a_wrong_shaped_paste(tmp_path):
    f = tmp_path / "host.env"
    out, echo = _recorder()
    status = keyless.ensure_secret(
        f,
        _secret(),
        interactive=True,
        prompt=lambda _p: "-----BEGIN RSA PRIVATE KEY-----",
        echo=echo,
    )
    assert status == "mismatch" and not f.exists()  # store NOTHING wrong-shaped
    assert any("base64" in line for line in out)


def test_ensure_secret_empty_paste_stores_nothing(tmp_path):
    f = tmp_path / "host.env"
    status = keyless.ensure_secret(
        f, _secret(), interactive=True, prompt=lambda _p: "   ", echo=lambda _m: None
    )
    assert status == "empty" and not f.exists()


def test_host_env_value_matches_supervisor_parsing(tmp_path):
    f = tmp_path / "host.env"
    f.write_text("# c\nGH_PEM_B64='quoted'\nOTHER = spaced \n")
    assert keyless.host_env_value(f, "GH_PEM_B64") == "quoted"  # quotes stripped, as the supervisor
    assert keyless.host_env_value(f, "OTHER") == "spaced"
    assert keyless.host_env_value(f, "ABSENT") == ""
    assert keyless.host_env_value(tmp_path / "nope.env", "GH_PEM_B64") == ""


def test_pattern_is_a_glob_so_no_pattern_can_hang_the_prompt():
    # `pattern` comes from repo config, which anything able to write the checkout can edit. As a
    # REGEX it was repo-controlled input to Python's backtracking engine — `(.*)*x` against a 2 KiB
    # paste hangs `fy box up` on the Mac. A glob can't nest quantifiers, so the class is gone.
    assert (
        keyless.secret_ok("anything", "([unclosed", False) is None
    )  # a stray regex: just no match
    evil, victim = "(.*)*x", "a" * 2048
    start = time.monotonic()
    assert keyless.secret_ok(victim, evil, False) is None
    assert time.monotonic() - start < 1.0  # as a regex this never returns
