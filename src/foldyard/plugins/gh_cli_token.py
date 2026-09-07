"""``gh-cli`` minter — inject the Mac user's OWN ``gh`` token (the ``github=user`` EMERGENCY rung).

Packaged rather than a consumer script, for the reason in ADR-0023. Same
``{"value", "ttl"}`` contract as :mod:`~foldyard.plugins.github_app_token`, but the value is full
user authority — including push — so it is only ever wired up while the mode is ``github=user``,
which carries a mandatory TTL and auto-reverts (``Axis.emergency``). The token is still injected
host-side by the proxy, so it never enters the box.

The reported ttl is deliberately short: the proxy re-runs this every few minutes, so revoking is
just ``gh auth logout`` (or letting the mode TTL lapse) rather than waiting out a cached value.

Run (the github plugin builds this command from ``sys.executable``)::

    python -m foldyard.plugins.gh_cli_token
"""

from __future__ import annotations

import json
import subprocess
import sys

# Short on purpose — see the module docstring: it bounds how long a revoked token keeps working.
_TTL = 240


def main(argv: list[str] | None = None) -> int:
    try:
        out = subprocess.run(
            ["gh", "auth", "token"], capture_output=True, text=True, timeout=15, check=True
        )
    except FileNotFoundError:
        print("gh_cli_token: the gh CLI isn't installed (brew install gh)", file=sys.stderr)
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
