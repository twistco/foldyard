"""``gh-cli`` minter — inject the host user's OWN ``gh`` token, on an EMERGENCY switch.

Packaged rather than a consumer script, for the reason in ADR-0023. It is the minter of the
``gh-cli`` ``[[inject]]`` kind (ADR-0031). Same ``{"value", "ttl"}`` contract as
:mod:`~foldyard.plugins.github_app_token`, but the value is full user authority — including push —
so the kind refuses a row that isn't ``emergency = true``: its ``on`` carries a mandatory TTL and
auto-reverts (``Switch.emergency``). The token is still injected
host-side by the proxy, so it never enters the box.

The reported ttl is deliberately short: the proxy re-runs this every few minutes, so revoking is
just ``gh auth logout`` (or letting the mode TTL lapse) rather than waiting out a cached value.

Run (the inject plugin builds this command from ``sys.executable``)::

    python -m foldyard.plugins.gh_cli_token [--hostname github.com] [--user LOGIN]

The flags are optional and forwarded to ``gh auth token``. With neither, the kind keeps its
active-host semantics. Copilot's ``oauth`` rung pins ``--hostname github.com`` and may name one
login, so a host's GHE login can never be minted for github.com by accident.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys

# Short on purpose — see the module docstring: it bounds how long a revoked token keeps working.
_TTL = 240


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="gh_cli_token")
    parser.add_argument("--hostname", help="the GitHub host to read the token for")
    parser.add_argument("--user", help="the account on that host whose token to print")
    args = parser.parse_args(argv)
    command = ["gh", "auth", "token"]
    if args.hostname:
        command += ["--hostname", args.hostname]
    if args.user:
        command += ["--user", args.user]
    try:
        out = subprocess.run(command, capture_output=True, text=True, timeout=15, check=True)
    except FileNotFoundError:
        print(
            "gh_cli_token: the gh CLI isn't installed "
            "(https://cli.github.com — `brew install gh` on macOS)",
            file=sys.stderr,
        )
        return 1
    except subprocess.TimeoutExpired:
        print("gh_cli_token: `gh auth token` timed out", file=sys.stderr)
        return 1
    except subprocess.CalledProcessError as e:
        # gh's own message names the fix ("gh auth login"); it never prints the token on failure.
        print(f"gh_cli_token: `gh auth token` failed — {e.stderr.strip()}", file=sys.stderr)
        return 1
    token = out.stdout.strip()
    if not token:
        print(
            "gh_cli_token: `gh auth token` printed nothing — run `gh auth login`", file=sys.stderr
        )
        return 1
    json.dump({"value": f"Bearer {token}", "ttl": _TTL}, sys.stdout)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
