"""awsauth.py + the Bedrock short-term key minter — SigV4 presigning, a profile's credentials via
the AWS CLI (stubbed: the suite never runs `aws`), and the ``{"value", "ttl"}`` the proxy gets.

The known-answer URLs below are what AWS's ``aws-bedrock-token-generator`` 1.1.0 (botocore
``SigV4QueryAuth``) produced for these inputs, with the clock frozen at ``_NOW`` — so a drift in
the canonical request, the key derivation or the parameter order fails here, not against AWS."""

from __future__ import annotations

import base64
import json
import subprocess
from datetime import UTC, datetime, timedelta

import pytest

from foldyard import awsauth
from foldyard.plugins import bedrock_token

_NOW = datetime(2026, 10, 29, 12, 34, 56, tzinfo=UTC)
_KEYS = ("AKIAIOSFODNN7EXAMPLE", "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY")
_SESSION = "IQoJb3JpZ2luX2VjEJr//////////wEaCXVzLWVhc3QtMSJHMEUCIQ+/=="
_SCOPE = "AKIAIOSFODNN7EXAMPLE%2F20261029%2Feu-west-2%2Fbedrock%2Faws4_request"
_HEAD = (
    "bedrock.amazonaws.com/?Action=CallWithBearerToken&X-Amz-Algorithm=AWS4-HMAC-SHA256"
    f"&X-Amz-Credential={_SCOPE}&X-Amz-Date=20261029T123456Z&X-Amz-Expires=3600"
    "&X-Amz-SignedHeaders=host"
)
_LONG_LIVED = (
    f"{_HEAD}&X-Amz-Signature="
    "d5d60ed830198b23ee476922af8828de14378c093bd5277bb60c9c3edccfdbf7&Version=1"
)
_WITH_SESSION = (
    f"{_HEAD}&X-Amz-Security-Token="
    "IQoJb3JpZ2luX2VjEJr%2F%2F%2F%2F%2F%2F%2F%2F%2F%2FwEaCXVzLWVhc3QtMSJHMEUCIQ%2B%2F%3D%3D"
    "&X-Amz-Signature=2c2582e865c84d675323200aa8bc60047d48a3e04fcac55dbf00eb77c1063ac5&Version=1"
)


def _decoded(token: str) -> str:
    assert token.startswith(bedrock_token.PREFIX)
    return base64.b64decode(token[len(bedrock_token.PREFIX) :]).decode()


@pytest.mark.parametrize(
    ("session", "expected"), [("", _LONG_LIVED), (_SESSION, _WITH_SESSION)], ids=["keys", "session"]
)
def test_the_short_term_key_is_awss_own_generators(session, expected):
    creds = awsauth.Credentials(*_KEYS, session_token=session)
    assert _decoded(bedrock_token.token(creds, "eu-west-2", _NOW)) == expected


def test_the_signature_covers_the_region_and_the_clock():
    creds = awsauth.Credentials(*_KEYS)
    base = bedrock_token.token(creds, "eu-west-2", _NOW)
    assert bedrock_token.token(creds, "us-east-1", _NOW) != base
    assert bedrock_token.token(creds, "eu-west-2", _NOW + timedelta(seconds=1)) != base


@pytest.mark.parametrize(
    ("left", "ttl"),
    [
        (None, 3300),  # long-lived keys: the signing hour, less the margin
        (7200, 3300),  # a long session: still the signing hour
        (1200, 900),  # a short one: the session, less the margin
    ],
)
def test_the_ttl_ends_before_the_key_or_its_session(left, ttl):
    expiry = None if left is None else _NOW + timedelta(seconds=left)
    creds = awsauth.Credentials(*_KEYS, expiration=expiry)
    assert bedrock_token.ttl(creds, _NOW) == ttl


def test_a_session_about_to_end_is_refused_with_the_fix():
    creds = awsauth.Credentials(*_KEYS, expiration=_NOW + timedelta(seconds=200))
    with pytest.raises(awsauth.CredentialError, match="log in again"):
        bedrock_token.ttl(creds, _NOW)


def test_process_output_parses_with_and_without_an_expiry():
    full = awsauth.parse_process_output(
        json.dumps(
            {
                "Version": 1,
                "AccessKeyId": "AKIA",
                "SecretAccessKey": "s",
                "SessionToken": "t",
                "Expiration": "2026-10-29T13:34:56Z",
            }
        )
    )
    assert full.session_token == "t" and full.remaining(_NOW) == 3600
    static = awsauth.parse_process_output(
        '{"Version": 1, "AccessKeyId": "A", "SecretAccessKey": "s"}'
    )
    assert static.session_token == "" and static.remaining(_NOW) is None


@pytest.mark.parametrize(
    "raw", ["", "not json", "{}", '{"AccessKeyId": "", "SecretAccessKey": ""}']
)
def test_process_output_that_isnt_credentials_is_refused(raw):
    with pytest.raises(awsauth.CredentialError):
        awsauth.parse_process_output(raw)


def _aws(monkeypatch, *, stdout="", stderr="", code=0, missing=False):
    calls: list[list[str]] = []

    def run(cmd, **kw):
        calls.append(cmd)
        if missing:
            raise FileNotFoundError(cmd[0])
        if code:
            raise subprocess.CalledProcessError(code, cmd, output=stdout, stderr=stderr)
        return subprocess.CompletedProcess(cmd, 0, stdout=stdout, stderr="")

    monkeypatch.setattr(awsauth.subprocess, "run", run)
    return calls


def test_a_profile_is_asked_of_the_aws_cli_by_name(monkeypatch):
    out = '{"Version": 1, "AccessKeyId": "AKIA", "SecretAccessKey": "s", "SessionToken": "t"}'
    calls = _aws(monkeypatch, stdout=out)
    assert awsauth.profile_credentials("my-sso").access_key_id == "AKIA"
    assert calls == [
        ["aws", "configure", "export-credentials", "--profile", "my-sso", "--format", "process"]
    ]


@pytest.mark.parametrize(
    ("kw", "said"),
    [
        ({"missing": True}, "isn't installed"),
        ({"code": 255, "stderr": "Error loading SSO Token: Token has expired"}, "aws sso login"),
        ({"code": 2, "stderr": "aws: error: argument operation: invalid choice"}, "v2.9"),
    ],
    ids=["no-cli", "sso-lapsed", "old-cli"],
)
def test_the_cli_failing_names_the_fix(monkeypatch, kw, said):
    _aws(monkeypatch, **kw)
    with pytest.raises(awsauth.CredentialError, match=said):
        awsauth.profile_credentials("my-sso")


def test_a_profile_name_never_reaches_argv_unchecked(monkeypatch):
    calls = _aws(monkeypatch, stdout="{}")
    with pytest.raises(awsauth.CredentialError):
        awsauth.profile_credentials("--debug")
    assert calls == []


def test_the_minter_prints_the_proxys_contract(monkeypatch, capsys):
    expiry = (datetime.now(UTC) + timedelta(hours=8)).strftime("%Y-%m-%dT%H:%M:%SZ")
    out = json.dumps(
        {"Version": 1, "AccessKeyId": "AKIA", "SecretAccessKey": "s", "Expiration": expiry}
    )
    _aws(monkeypatch, stdout=out)
    assert bedrock_token.main(["--region", "eu-west-2", "--profile", "p"]) == 0
    minted = json.loads(capsys.readouterr().out)
    assert minted["ttl"] == 3300 and "X-Amz-Credential=AKIA%2F" in _decoded(minted["value"])


def test_the_minter_fails_loudly_and_prints_no_value(monkeypatch, capsys):
    _aws(monkeypatch, code=255, stderr="The SSO session has expired")
    assert bedrock_token.main(["--region", "eu-west-2", "--profile", "p"]) == 1
    captured = capsys.readouterr()
    assert captured.out == "" and "SSO session has expired" in captured.err
