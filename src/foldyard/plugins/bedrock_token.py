"""Bedrock short-term API key minter — keyless Claude on Amazon Bedrock, from your own AWS login.

The minter of ``[claude] keyless = "bedrock"`` when ``[claude.bedrock].profile`` names a profile on
your computer. A short-term Bedrock API key is a SigV4-presigned ``CallWithBearerToken`` URL,
base64-encoded behind ``bedrock-api-key-`` (what AWS's ``aws-bedrock-token-generator`` builds), so
it carries the profile's permissions and lapses with them. The proxy injects it as the bearer on
the Bedrock hosts; the box only ever holds a dummy, and the profile's credentials never leave this
process.

Same ``{"value", "ttl"}`` contract as the other minters. Each key is signed for an hour and
reported for less, so the proxy re-mints well inside both that hour and the session behind it —
an SSO session that ends stops the keys within minutes, and the next mint says what to run.

Run (the claude plugin builds this command from ``sys.executable``)::

    python -m foldyard.plugins.bedrock_token --region eu-west-2 --profile my-sso
"""

from __future__ import annotations

import argparse
import base64
import json
import sys
from datetime import UTC, datetime

from .. import awsauth

PREFIX = "bedrock-api-key-"
_SIGNED_FOR = 3600  # X-Amz-Expires; AWS allows up to 12 h
_MARGIN = 300  # re-mint this long before the key or its session lapses
_MIN_TTL = 60  # less left than this: the session is as good as gone


def token(creds: awsauth.Credentials, region: str, now: datetime) -> str:
    url = awsauth.presign(
        creds,
        method="POST",
        host="bedrock.amazonaws.com",
        service="bedrock",
        region=region,
        params={"Action": "CallWithBearerToken"},
        expires=_SIGNED_FOR,
        now=now,
    )
    return PREFIX + base64.b64encode(f"{url}&Version=1".encode()).decode()


def ttl(creds: awsauth.Credentials, now: datetime) -> int:
    """How long the proxy may reuse a key minted now: the signing window or the session, whichever
    ends first, less the margin. Raises when the session ends too soon to be worth a key."""
    left = creds.remaining(now)
    usable = _SIGNED_FOR if left is None else min(_SIGNED_FOR, left)
    if usable - _MARGIN < _MIN_TTL:
        raise awsauth.CredentialError(
            f"the profile's session ends in {max(0, int(usable))}s — log in again on your computer"
        )
    return int(usable - _MARGIN)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="bedrock_token")
    parser.add_argument("--region", required=True)
    parser.add_argument("--profile", required=True)
    args = parser.parse_args(argv)
    now = datetime.now(UTC)
    try:
        creds = awsauth.profile_credentials(args.profile)
        value = token(creds, args.region, now)
        seconds = ttl(creds, now)
    except awsauth.CredentialError as e:
        print(f"bedrock_token: {e}", file=sys.stderr)
        return 1
    json.dump({"value": value, "ttl": seconds}, sys.stdout)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
