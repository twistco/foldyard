"""keyless.py — the credential taxonomy + the host-side (Mac) capture of the real key/token.

Pure logic (classify-by-prefix, host.env read/append, the ensure_cred state machine) with the
prompt/echo I/O injected — no real stdin, no real ~/.foldyard. The on-the-wire half (the proxy
actually rewriting the header) is the opt-in proxy e2e; here we prove the box never gets the secret
and the right host.env var is written from a pasted credential."""

from __future__ import annotations

import base64
import json
import os
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
        # GitHub token shapes Copilot accepts all name the Copilot var — including `gho_`, which
        # `gh auth token` mints (a gitz-hosted gh token, which Copilot refuses, lands here too).
        ("github_pat_11ABC", ("COPILOT_GITHUB_TOKEN", "GitHub fine-grained PAT (Copilot)")),
        ("gho_abc", ("COPILOT_GITHUB_TOKEN", "GitHub OAuth token (Copilot)")),
        ("ghu_abc", ("COPILOT_GITHUB_TOKEN", "GitHub user token (Copilot)")),
        # Classic PATs are refused by Copilot itself, so they are not stored under any name.
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


def test_host_env_has_treats_an_empty_value_as_absent(tmp_path):
    # `KEY=` (or `KEY=""`) is a placeholder, not a secret: the supervisor would export an empty
    # string and the minter would fail on it, so missing_secrets() must still ask for the value.
    f = tmp_path / "host.env"
    f.write_text(
        "GH_PEM_B64=\nANTHROPIC_API_KEY=''\nGH_APP_ID=7\nOPENAI_API_KEY=\nOPENAI_API_KEY=x\n"
    )
    assert not keyless.host_env_has(f, "GH_PEM_B64")
    assert not keyless.host_env_has(f, "ANTHROPIC_API_KEY")
    assert keyless.host_env_has(f, "GH_APP_ID")
    assert keyless.host_env_has(f, "OPENAI_API_KEY")  # last definition wins, as in the supervisor


def test_append_host_env_creates_0600_and_preserves(tmp_path):
    f = tmp_path / "sub" / "host.env"
    keyless.append_host_env(f, "ANTHROPIC_API_KEY", "sk-ant-real")
    assert f.read_text() == "ANTHROPIC_API_KEY=sk-ant-real\n"
    assert (f.stat().st_mode & 0o777) == 0o600
    # Appends without a trailing-newline gap and keeps the prior content.
    f.write_text("FOO=bar")  # no trailing newline
    keyless.append_host_env(f, "OPENAI_API_KEY", "sk-proj-real")
    assert f.read_text() == "FOO=bar\nOPENAI_API_KEY=sk-proj-real\n"


def test_append_host_env_never_leaves_the_secret_readable(tmp_path, monkeypatch):
    # The file is created 0600 at open (not written world-readable and chmod'd after — that window
    # is a real secret on disk), an existing looser file is tightened BEFORE the paste lands, and
    # a tightening that fails propagates rather than storing the secret under the wrong mode.
    f = tmp_path / "host.env"
    f.write_text("FOO=bar\n")
    f.chmod(0o644)
    keyless.append_host_env(f, "ANTHROPIC_API_KEY", "sk-ant-real")
    assert (f.stat().st_mode & 0o777) == 0o600
    f.chmod(0o644)

    def refuse(*_a, **_k):
        raise PermissionError("chmod refused")

    monkeypatch.setattr(keyless.os, "chmod", refuse)
    with pytest.raises(PermissionError):
        keyless.append_host_env(f, "OPENAI_API_KEY", "sk-proj-real")
    assert "OPENAI_API_KEY" not in f.read_text()  # refused ⇒ nothing stored


def test_append_host_env_serialises_concurrent_captures(tmp_path, monkeypatch):
    # Two captures at once (the TUI modal and a `fy up` prompt) must not each read the same body
    # and overwrite the other's line. Simulated interleaving: a rival append lands just before the
    # first capture takes its lock — so everything the first capture reads must come AFTER it.
    f = tmp_path / "host.env"
    real_flock = keyless.fcntl.flock
    raced = False

    def racing_flock(fd, op):
        nonlocal raced
        if not raced:
            raced = True
            keyless.append_host_env(f, "OPENAI_API_KEY", "sk-proj-rival")
        real_flock(fd, op)

    monkeypatch.setattr(keyless.fcntl, "flock", racing_flock)
    keyless.append_host_env(f, "ANTHROPIC_API_KEY", "sk-ant-real")
    assert f.read_text() == "OPENAI_API_KEY=sk-proj-rival\nANTHROPIC_API_KEY=sk-ant-real\n"


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
        how="run `claude setup-token` on your computer to mint one",
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
        "dummies": ("Bearer sk-dummy",),  # what the box sends: how a request names this rule
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
    # `|` separates whole-value alternatives (Copilot accepts three token shapes): any of them
    # matches, and nothing else does — including a value that only contains one as a substring.
    pattern = "github_pat_*|gho_*|ghu_*"
    for value in ("github_pat_11ABC", "gho_abc", "ghu_abc"):
        assert keyless.secret_ok(value, pattern, False) == value
    for value in ("ghp_abc", "xgithub_pat_abc", ""):
        assert keyless.secret_ok(value, pattern, False) is None
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


# ── a base64 secret's FILE, by path: foldyard encodes it, so the key never reaches a terminal ──


def _key_file(tmp_path, name: str = "my-app.private-key.pem", body: str = _PEM):
    path = tmp_path / name
    path.write_text(body)
    return path


def test_ensure_secret_reads_a_key_file_by_path_and_stores_its_base64(tmp_path):
    # The old hint had the operator run `base64 < key.pem | tr -d '\n'` and copy the output:
    # the key in their scrollback and clipboard, which the hidden prompt exists to avoid. Given
    # the path instead, foldyard encodes the file itself and stores the very same line.
    key = _key_file(tmp_path)
    f = tmp_path / "host.env"
    out, echo = _recorder()
    status = keyless.ensure_secret(
        f, _secret(), interactive=True, prompt=lambda _p: str(key), echo=echo
    )
    assert status == "stored"
    assert f.read_text() == f"GH_PEM_B64={base64.b64encode(key.read_bytes()).decode()}\n"
    assert any(str(key) in line for line in out)  # says which file it read
    assert any("path" in line for line in out if "value (hidden)" not in line)  # offered up front
    # The prompt is hidden, so a drag shows nothing: say so, or it looks like the drag failed.
    assert any("press Enter" in line and "nothing shows" in line for line in out)


@pytest.mark.parametrize(
    "entry",
    [
        "'{p}'",  # a terminal that quotes a dragged-in path
        '"{p}"',
        "{e}",  # one that backslash-escapes its spaces
        "  {p}  ",  # a file manager's "copy path", spaces left bare
    ],
)
def test_a_path_is_read_however_it_was_spelled(tmp_path, entry):
    folder = tmp_path / "with space"
    folder.mkdir()
    key = _key_file(folder)
    escaped = str(key).replace(" ", "\\ ")
    to_store, _note = keyless.secret_entry(entry.format(p=key, e=escaped), _secret())
    assert to_store == base64.b64encode(key.read_bytes()).decode()


def test_a_home_relative_path_is_expanded(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    key = _key_file(tmp_path)
    to_store, _note = keyless.secret_entry(f"~/{key.name}", _secret())
    assert to_store == base64.b64encode(key.read_bytes()).decode()


def test_a_relative_path_is_never_read(tmp_path, monkeypatch):
    # The prompt runs in the checkout, which the box can write: a bare `key.pem` there would be
    # whatever the box put there. Only a full path (or ~/…) is the operator's own choice.
    monkeypatch.chdir(tmp_path)
    key = _key_file(tmp_path)
    to_store, note = keyless.secret_entry(key.name, _secret())
    assert to_store is None and "full path" in note


def test_a_file_that_is_not_the_secret_is_refused_and_nothing_stored(tmp_path):
    other = _key_file(tmp_path, "id_ed25519.pub", "ssh-ed25519 AAAA… me@host\n")
    f = tmp_path / "host.env"
    out, echo = _recorder()
    status = keyless.ensure_secret(
        f, _secret(), interactive=True, prompt=lambda _p: str(other), echo=echo
    )
    assert status == "mismatch" and not f.exists()
    assert any(str(other) in line and "doesn't look like" in line for line in out)


@pytest.mark.parametrize("entry", ["/9j/4AAQSkZJRgABAQ+mangled", "/no/such/key.pem"])
def test_an_entry_that_names_no_file_is_never_echoed(tmp_path, entry):
    # Base64 can start with `/`, so a mangled key paste can look like a path. Whatever was typed
    # into the hidden prompt stays hidden: the refusal names no part of it.
    f = tmp_path / "host.env"
    out, echo = _recorder()
    status = keyless.ensure_secret(
        f, _secret(), interactive=True, prompt=lambda _p: entry, echo=echo
    )
    assert status == "mismatch" and not f.exists()
    assert not any(entry in line or "4AAQ" in line or "no/such" in line for line in out)


def test_a_directory_is_refused(tmp_path):
    to_store, note = keyless.secret_entry(str(tmp_path), _secret())
    assert to_store is None and "not a file" in note
    assert str(tmp_path) in note  # it opened, so the entry was a real path: safe to name


def test_a_fifo_is_refused_without_blocking(tmp_path):
    # Opening a FIFO for reading waits for a writer; the prompt must answer at once instead.
    fifo = tmp_path / "pipe"
    os.mkfifo(fifo)
    start = time.monotonic()
    to_store, note = keyless.secret_entry(str(fifo), _secret())
    assert to_store is None and "not a file" in note and str(fifo) in note
    assert time.monotonic() - start < 1.0


def test_a_file_far_larger_than_a_key_is_refused_unread(tmp_path):
    big = tmp_path / "big.pem"
    big.write_bytes(b"x" * (keyless.SECRET_FILE_MAX + 1))
    to_store, note = keyless.secret_entry(str(big), _secret())
    assert to_store is None and "larger than" in note and str(big) in note


def test_a_pasted_base64_value_still_works_and_needs_no_file(tmp_path):
    assert keyless.secret_entry(_PEM_B64, _secret()) == (_PEM_B64, "")


def test_a_plain_secret_never_reads_a_file(tmp_path):
    # Only a base64 secret is a file's contents; a plain token named by a path stays a value, so
    # this matches its pattern or is refused — the file is never opened.
    key = _key_file(tmp_path)
    plain = _secret(pattern="*-----BEGIN *", b64=False)
    to_store, _note = keyless.secret_entry(str(key), plain)
    assert to_store is None


def test_an_unknown_account_after_a_tilde_is_refused_not_raised(tmp_path):
    # `Path.expanduser` raises RuntimeError for `~someone/…` when there's no such account: that
    # escaped both prompts instead of reaching their refusal.
    to_store, note = keyless.secret_entry("~no-such-account-fy/key.pem", _secret())
    assert to_store is None and "full path" in note and "no-such" not in note


def test_a_literal_backslash_in_a_real_path_is_kept(tmp_path):
    # A backslash is an ordinary filename character: shell-decoding `/…/prod\key.pem` would read
    # `/…/prodkey.pem` instead — and if THAT exists, store a different key than the one named.
    key = _key_file(tmp_path, "prod\\key.pem")
    _key_file(tmp_path, "prodkey.pem", _PEM.replace("abc", "other"))
    to_store, _note = keyless.secret_entry(str(key), _secret())
    assert to_store == base64.b64encode(key.read_bytes()).decode()


@pytest.mark.skipif(os.geteuid() == 0, reason="root traverses a mode-000 directory anyway")
def test_a_path_under_a_directory_you_cannot_enter_is_refused_not_raised(tmp_path):
    # Python ≤ 3.13's `Path.exists()` raises PermissionError there rather than answering False,
    # and the as-typed check runs before the read's own refusal could.
    locked = tmp_path / "restricted"
    locked.mkdir()
    locked.chmod(0)
    try:
        to_store, note = keyless.secret_entry(f"{locked}/app\\ key.pem", _secret())
    finally:
        locked.chmod(0o700)
    assert to_store is None and "restricted" not in note  # never echoed: it didn't open


def test_an_entry_with_a_nul_byte_is_refused_not_raised(tmp_path):
    # os.open raises ValueError, not OSError, for an embedded NUL: it escaped the prompt.
    to_store, note = keyless.secret_entry(f"{tmp_path}/key\x00.pem", _secret())
    assert to_store is None and "key" not in note


def test_a_read_error_after_the_file_opened_is_refused_not_raised(tmp_path, monkeypatch):
    # An I/O error mid-read (a flaky mount) must reach the refusal, not escape the prompt.
    key = _key_file(tmp_path)

    class _Failing:
        def __init__(self, fd, _mode):
            self.fd = fd

        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            os.close(self.fd)

        def read(self, _n):
            raise OSError(5, "Input/output error")

    monkeypatch.setattr(keyless.os, "fdopen", _Failing)
    to_store, note = keyless.secret_entry(str(key), _secret())
    assert to_store is None and str(key) in note and "couldn't be read" in note


def test_host_env_value_matches_supervisor_parsing(tmp_path):
    f = tmp_path / "host.env"
    f.write_text("# c\nGH_PEM_B64='quoted'\nOTHER = spaced \nOTHER=rotated\n")
    assert keyless.host_env_value(f, "GH_PEM_B64") == "quoted"  # quotes stripped, as the supervisor
    assert (
        keyless.host_env_value(f, "OTHER") == "rotated"
    )  # last definition wins, as in the supervisor
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


# ── a login stored in the box (ADR-0008, amended 2026-11-18: no in-box login) ───────────────
#
# Keyless is the only supported way an agent is credentialed: the box holds a dummy, the proxy
# injects the real credential on your computer. A login made in the box anyway (`claude`'s own
# login, `codex login`) leaves a real credential in a volume every box of the project mounts, so
# `fy verify` and `fy doctor` look for one. These read the files the agents themselves write.


def test_no_claude_login_in_an_empty_or_keyless_config_dir(tmp_path):
    assert keyless.claude_stored_login(tmp_path / "absent") == []
    assert keyless.claude_stored_login(tmp_path) == []
    # What a keyless box's Claude writes on its own: onboarding and trust state, no credential.
    (tmp_path / ".claude.json").write_text(json.dumps({"hasCompletedOnboarding": True}))
    (tmp_path / "settings.json").write_text("{}")
    assert keyless.claude_stored_login(tmp_path) == []


@pytest.mark.parametrize(
    "content", [json.dumps({"claudeAiOauth": {"accessToken": "sk-ant-oat01-real"}}), "{torn"]
)
def test_a_claude_login_in_the_box_is_found(tmp_path, content):
    # A file that can't be read counts, as Codex's does: it exists only to hold a credential.
    (tmp_path / ".credentials.json").write_text(content)
    assert keyless.claude_stored_login(tmp_path) == [str(tmp_path / ".credentials.json")]


@pytest.mark.parametrize("content", ["{}", json.dumps({"claudeAiOauth": None}), ""])
def test_an_emptied_claude_credentials_file_is_not_a_login(tmp_path, content):
    # Whatever `/logout` leaves behind, a file holding nothing is no credential.
    (tmp_path / ".credentials.json").write_text(content)
    assert keyless.claude_stored_login(tmp_path) == []


def test_a_claude_console_key_in_its_config_is_found(tmp_path):
    (tmp_path / ".claude.json").write_text(json.dumps({"primaryApiKey": "sk-ant-api03-real"}))
    found = keyless.claude_stored_login(tmp_path)
    assert found == [f"{tmp_path / '.claude.json'} (primaryApiKey)"]


def test_an_unreadable_claude_config_is_not_a_login(tmp_path):
    # `.claude.json` is Claude's state file, rewritten constantly; a torn read is not a credential.
    (tmp_path / ".claude.json").write_text("{not json")
    assert keyless.claude_stored_login(tmp_path) == []


def _dummy_chatgpt_auth() -> str:
    return keyless.dummy_codex_auth_json("acct-123")


def test_no_codex_login_without_auth_json_or_with_foldyards_dummy(tmp_path):
    assert keyless.codex_stored_login(tmp_path / "absent") == []
    assert keyless.codex_stored_login(tmp_path) == []
    (tmp_path / "auth.json").write_text(_dummy_chatgpt_auth())  # ChatGPT keyless seeds this
    assert keyless.codex_stored_login(tmp_path) == []
    (tmp_path / "auth.json").write_text(json.dumps({"OPENAI_API_KEY": "sk-dummy"}))
    assert keyless.codex_stored_login(tmp_path) == []


@pytest.mark.parametrize(
    "auth",
    [
        {"OPENAI_API_KEY": "sk-proj-real"},  # `codex login --api-key`
        {  # `codex login` with a ChatGPT account
            "auth_mode": "chatgpt",
            "OPENAI_API_KEY": None,
            "tokens": {"access_token": "eyJreal", "refresh_token": "rt_real", "account_id": "a"},
        },
        {  # the dummy's access token kept, a real refresh token beside it
            "OPENAI_API_KEY": None,
            "tokens": {
                "access_token": keyless.codex_chatgpt_dummy_token(),
                "refresh_token": "rt_real",
            },
        },
    ],
)
def test_a_codex_login_in_the_box_is_found(tmp_path, auth):
    (tmp_path / "auth.json").write_text(json.dumps(auth))
    assert keyless.codex_stored_login(tmp_path) == [str(tmp_path / "auth.json")]


def test_an_unreadable_codex_auth_json_counts_as_a_login(tmp_path):
    # Unlike Claude's state file, auth.json exists only to hold a credential: if it can't be read
    # as foldyard's dummy, verify can't vouch for it.
    (tmp_path / "auth.json").write_text("{torn")
    assert keyless.codex_stored_login(tmp_path) == [str(tmp_path / "auth.json")]


def test_credential_env_holding_anything_but_the_dummy_is_named():
    env = {"ANTHROPIC_API_KEY": "sk-ant-dummy", "CLAUDE_CODE_OAUTH_TOKEN": "sk-ant-oat01-real"}
    assert keyless.credential_env_leaks(keyless.CLAUDE_KEYLESS, env) == ["CLAUDE_CODE_OAUTH_TOKEN"]
    assert keyless.credential_env_leaks(keyless.CLAUDE_KEYLESS, {}) == []
    assert keyless.credential_env_leaks(keyless.CODEX_KEYLESS, {"OPENAI_API_KEY": "sk-dummy"}) == []
    assert keyless.credential_env_leaks(keyless.CODEX_KEYLESS, {"OPENAI_API_KEY": "sk-x"}) == [
        "OPENAI_API_KEY"
    ]


@pytest.mark.parametrize(("table", "kinds"), [("claude", "oauth"), ("codex", "chatgpt")])
def test_the_no_keyless_refusal_names_the_personal_file_and_the_kinds(table, kinds):
    text = keyless.no_keyless_block(table)
    assert f"[{table}]" in text and "keyless" in text
    assert "foldyard.local.toml" in text and kinds in text and "api-key" in text


def test_a_claude_state_file_elsewhere_is_read_where_given(tmp_path):
    (tmp_path / "home").mkdir()
    state = tmp_path / "home" / ".claude.json"
    state.write_text(json.dumps({"primaryApiKey": "sk-ant-api03-real"}))
    assert keyless.claude_stored_login(tmp_path / "home" / ".claude") == []
    found = keyless.claude_stored_login(tmp_path / "home" / ".claude", state=state)
    assert found == [f"{state} (primaryApiKey)"]
