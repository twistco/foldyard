"""codex_chatgpt_token.py — the ChatGPT-subscription refresh-minter for keyless Codex.

The security-load-bearing piece: it reads/refreshes the Mac's real ~/.codex/auth.json and the proxy
injects the current access token into the box. These tests drive it against a TEMP auth.json with a
STUBBED HTTP layer (no network, no real OpenAI), proving: a still-fresh token mints with no refresh;
a near-expiry token triggers exactly one refresh with the verified request shape; the rotated tokens
are written back (preserving every other field); and a refresh failure exits non-zero (so the proxy
keeps its cached value rather than injecting nothing). The on-the-wire injection is the proxy e2e."""

from __future__ import annotations

import base64
import io
import json
import time

from foldyard.plugins import codex_chatgpt_token as minter


def _jwt(exp: int | None) -> str:
    def seg(obj: dict) -> str:
        return base64.urlsafe_b64encode(json.dumps(obj).encode()).rstrip(b"=").decode()

    payload = {"exp": exp} if exp is not None else {"sub": "x"}
    return f"{seg({'alg': 'none'})}.{seg(payload)}.sig"


def _auth_json(path, *, access_exp, refresh="rt-1", account="acc-1"):
    path.write_text(
        json.dumps(
            {
                "auth_mode": "chatgpt",
                "OPENAI_API_KEY": None,
                "tokens": {
                    "id_token": _jwt(access_exp),
                    "access_token": _jwt(access_exp),
                    "refresh_token": refresh,
                    "account_id": account,
                },
                "last_refresh": "2026-01-01T00:00:00Z",
            }
        )
    )
    return path


def _run(monkeypatch, path) -> tuple[int, dict | None]:
    buf = io.StringIO()
    monkeypatch.setattr("sys.stdout", buf)
    rc = minter.main([str(path)])
    out = buf.getvalue()
    return rc, (json.loads(out) if out.strip() else None)


def test_fresh_token_mints_without_refreshing(monkeypatch, tmp_path):
    # Access token valid for another hour → return it as-is, NEVER hit the network.
    auth = _auth_json(tmp_path / "auth.json", access_exp=int(time.time()) + 3600)

    def _boom(_rt):
        raise AssertionError("must not refresh a fresh token")

    monkeypatch.setattr(minter, "_refresh", _boom)
    rc, out = _run(monkeypatch, auth)
    assert rc == 0 and out is not None
    assert out["value"] == json.loads(auth.read_text())["tokens"]["access_token"]
    assert 3000 < out["ttl"] <= 3600  # ttl ≈ exp-now (minus the safety margin)


def test_near_expiry_refreshes_and_writes_back_rotated_tokens(monkeypatch, tmp_path):
    # Access token expires in 60s (< the 5-min window) → exactly one refresh; rotated tokens land in
    # auth.json (preserving auth_mode/account_id), and the minted value is the NEW access token.
    auth = _auth_json(tmp_path / "auth.json", access_exp=int(time.time()) + 60, refresh="rt-OLD")
    new_access = _jwt(int(time.time()) + 3600)
    calls = []

    def _fake_refresh(refresh_token):
        calls.append(refresh_token)
        return {"access_token": new_access, "refresh_token": "rt-NEW", "id_token": _jwt(None)}

    monkeypatch.setattr(minter, "_refresh", _fake_refresh)
    rc, out = _run(monkeypatch, auth)
    assert rc == 0 and out is not None
    assert calls == ["rt-OLD"]  # refreshed once, with the on-disk refresh token
    assert out["value"] == new_access  # minted the freshly-refreshed access token
    saved = json.loads(auth.read_text())
    assert saved["tokens"]["access_token"] == new_access
    assert saved["tokens"]["refresh_token"] == "rt-NEW"  # rotation written back
    assert saved["tokens"]["account_id"] == "acc-1"  # untouched fields preserved
    assert saved["auth_mode"] == "chatgpt"
    assert saved["last_refresh"].endswith("Z") and saved["last_refresh"] != "2026-01-01T00:00:00Z"


def test_refresh_response_may_omit_refresh_token(monkeypatch, tmp_path):
    # The grant response's fields are all optional — a response without a new refresh_token keeps the
    # old one (we must never null it out and lock the user's codex login).
    auth = _auth_json(tmp_path / "auth.json", access_exp=int(time.time()) - 1, refresh="rt-keep")
    monkeypatch.setattr(
        minter, "_refresh", lambda _rt: {"access_token": _jwt(int(time.time()) + 999)}
    )
    rc, _ = _run(monkeypatch, auth)
    assert rc == 0
    assert json.loads(auth.read_text())["tokens"]["refresh_token"] == "rt-keep"


def test_refresh_failure_exits_nonzero_and_leaves_auth_untouched(monkeypatch, tmp_path):
    # A refresh error → non-zero exit (the proxy keeps its cached value), and auth.json is NOT
    # corrupted (the real codex login survives a transient refresh outage).
    auth = _auth_json(tmp_path / "auth.json", access_exp=int(time.time()) - 1, refresh="rt-x")
    before = auth.read_text()

    def _fail(_rt):
        raise RuntimeError("network down")

    monkeypatch.setattr(minter, "_refresh", _fail)
    rc, out = _run(monkeypatch, auth)
    assert rc == 1 and out is None
    assert auth.read_text() == before  # untouched


def test_missing_or_malformed_auth_json_exits_nonzero(monkeypatch, tmp_path):
    rc, _ = _run(monkeypatch, tmp_path / "absent.json")
    assert rc == 1
    bad = tmp_path / "bad.json"
    bad.write_text("{not json")
    rc, _ = _run(monkeypatch, bad)
    assert rc == 1
    nofields = tmp_path / "nf.json"
    nofields.write_text(json.dumps({"auth_mode": "chatgpt", "tokens": {}}))
    rc, _ = _run(monkeypatch, nofields)
    assert rc == 1  # no access/refresh token


def test_jwt_exp_parses_padding_and_rejects_garbage():
    assert minter._jwt_exp(_jwt(123)) == 123
    assert minter._jwt_exp("not.a.jwt") is None
    assert minter._jwt_exp("only-one-segment") is None
