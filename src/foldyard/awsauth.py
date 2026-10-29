"""awsauth.py — AWS authentication on your computer: a profile's credentials, and SigV4 presigning.

The host half of anything foldyard does with AWS, kept apart from any one service so the next user
of it doesn't re-derive SigV4. Today that is the Bedrock short-term key minter
(:mod:`~foldyard.plugins.bedrock_token`); an AWS metadata emulator (the twin of
:mod:`~foldyard.plugins.gcp`'s, docs/adrs/0035) would mint STS role credentials from the same
profile.

**Credentials come from the AWS CLI, never from foldyard's own parsing of ``~/.aws``.**
``aws configure export-credentials`` resolves every source a profile can name — SSO, ``aws login``,
an assumed role, a ``credential_process``, static keys — and refreshes SSO role credentials from
its token cache. A fixed command in package code, like ``gh auth token`` for the ``gh-cli`` kind:
nothing in config names a command (ADR-0023), only which profile.

Stdlib only; imported only by host-side minters and probes, never on the registry hot path.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import subprocess
from dataclasses import dataclass
from datetime import UTC, datetime
from urllib.parse import quote

# What a profile name may be: it lands on the AWS CLI's argv, and in a config a box can propose.
PROFILE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.+@-]{0,63}$")


class CredentialError(RuntimeError):
    """The profile gave no usable credentials. The message names the fix and never a secret."""


@dataclass(frozen=True)
class Credentials:
    access_key_id: str
    secret_access_key: str
    session_token: str = ""
    expiration: datetime | None = None  # None: long-lived keys

    def remaining(self, now: datetime) -> float | None:
        """Seconds until these expire, or None for credentials that don't."""
        return None if self.expiration is None else (self.expiration - now).total_seconds()


def parse_process_output(raw: str) -> Credentials:
    """``credential_process``-shaped JSON (what ``export-credentials --format process`` prints)."""
    try:
        data = json.loads(raw)
        key, secret = str(data["AccessKeyId"]), str(data["SecretAccessKey"])
    except (ValueError, KeyError, TypeError) as e:
        raise CredentialError("the AWS CLI printed something that isn't credentials") from e
    if not key or not secret:
        raise CredentialError("the AWS CLI printed empty credentials")
    expiration = None
    if data.get("Expiration"):
        try:
            expiration = datetime.fromisoformat(str(data["Expiration"]).replace("Z", "+00:00"))
        except ValueError as e:
            raise CredentialError("the AWS CLI printed an unreadable Expiration") from e
        if expiration.tzinfo is None:
            expiration = expiration.replace(tzinfo=UTC)
    return Credentials(key, secret, str(data.get("SessionToken") or ""), expiration)


def profile_credentials(profile: str, *, timeout: float = 20.0) -> Credentials:
    """The credentials ``profile`` resolves to on this computer, via the AWS CLI v2."""
    if not PROFILE.match(profile):
        raise CredentialError(f"{profile!r} isn't an AWS profile name")
    cmd = ["aws", "configure", "export-credentials", "--profile", profile, "--format", "process"]
    try:
        out = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=True,
            stdin=subprocess.DEVNULL,
        )
    except FileNotFoundError as e:
        raise CredentialError(
            "the AWS CLI isn't installed on your computer (v2.9 or later: "
            "https://aws.amazon.com/cli/ — `brew install awscli` on macOS)"
        ) from e
    except subprocess.TimeoutExpired as e:
        raise CredentialError(f"`aws configure export-credentials` timed out ({timeout:g}s)") from e
    except subprocess.CalledProcessError as e:
        said = (e.stderr or "").strip().splitlines()
        reason = said[-1] if said else f"exit {e.returncode}"
        if "invalid choice" in reason:
            reason = "this AWS CLI has no `configure export-credentials` (needs v2.9 or later)"
        raise CredentialError(
            f"profile {profile!r} gave no credentials — {reason} "
            f"(for an SSO profile: `aws sso login --profile {profile}`)"
        ) from e
    return parse_process_output(out.stdout)


def _sign(key: bytes, msg: str) -> bytes:
    return hmac.new(key, msg.encode(), hashlib.sha256).digest()


def _encode(value: str) -> str:
    return quote(value, safe="-_.~")


def presign(
    creds: Credentials,
    *,
    method: str,
    host: str,
    service: str,
    region: str,
    params: dict[str, str],
    expires: int,
    now: datetime,
    path: str = "/",
) -> str:
    """A SigV4 query-presigned URL without its scheme: ``host/path?params&auth&X-Amz-Signature=…``.

    Only ``host`` is signed and the payload is empty, which is all a presigned call carries. The
    operation's ``params`` come first, then the auth ones, as botocore's ``SigV4QueryAuth`` lays
    them out; ``tests/test_awsauth.py`` pins the result against a token it generated."""
    stamp = now.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")
    scope = f"{stamp[:8]}/{region}/{service}/aws4_request"
    auth = {
        "X-Amz-Algorithm": "AWS4-HMAC-SHA256",
        "X-Amz-Credential": f"{creds.access_key_id}/{scope}",
        "X-Amz-Date": stamp,
        "X-Amz-Expires": str(int(expires)),
        "X-Amz-SignedHeaders": "host",
    }
    if creds.session_token:
        auth["X-Amz-Security-Token"] = creds.session_token
    pairs = [(_encode(k), _encode(v)) for k, v in [*params.items(), *auth.items()]]
    query = "&".join(f"{k}={v}" for k, v in pairs)
    canonical = "\n".join(
        [
            method.upper(),
            path,
            "&".join(f"{k}={v}" for k, v in sorted(pairs)),
            f"host:{host}\n",
            "host",
            hashlib.sha256(b"").hexdigest(),
        ]
    )
    to_sign = "\n".join(
        ["AWS4-HMAC-SHA256", stamp, scope, hashlib.sha256(canonical.encode()).hexdigest()]
    )
    key = _sign(f"AWS4{creds.secret_access_key}".encode(), stamp[:8])
    for part in (region, service, "aws4_request"):
        key = _sign(key, part)
    signature = hmac.new(key, to_sign.encode(), hashlib.sha256).hexdigest()
    return f"{host}{path}?{query}&X-Amz-Signature={signature}"
