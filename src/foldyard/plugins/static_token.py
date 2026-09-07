"""Static-token "minter" for the generic :mod:`~foldyard.plugins.inject` plugin: echo a
host-side env var as the ``{"value", "ttl"}`` JSON the egress proxy's ``INJECT_COMMAND``
contract expects (see the packaged ``assets/proxy/egress_proxy.py`` addon).

The ``inject`` plugin points an injector's minter here when a ``[[inject]]`` entry gives a
``token_env`` (a long-lived static token) instead of its own ``minter`` command. The token
itself lives ONLY in ``~/.foldyard/<project>/host.env`` on the Mac and is read from the
process env here — never in the repo, never in the box, and never in the command string
(only the VARIABLE NAME is passed on argv, so the secret can't leak into a daemon-status or
command log). The proxy re-runs the minter every ``ttl`` seconds, so a token rotated in
host.env is picked up within ``ttl`` with no daemon restart.

Run (the inject plugin builds this command from ``sys.executable``)::

    python -m foldyard.plugins.static_token PENPOT_USER_TOKEN [TTL_SECONDS]

FUTURE: a sibling minter could source the value from the macOS Keychain / 1Password /
HashiCorp Vault instead of a host.env var — same ``{"value", "ttl"}`` contract, so neither
the ``inject`` plugin nor the proxy would change; only the ``[[inject]]`` entry's token
source (a new ``token_keychain``/``token_op`` key) would differ.
"""

from __future__ import annotations

import json
import os
import sys

# A static token doesn't expire, but we still re-mint periodically so a rotated value in
# host.env is picked up without a daemon restart. 12h balances freshness against noise.
_DEFAULT_TTL = 12 * 3600


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if not args:
        print("static_token: usage: static_token <ENV_VAR> [ttl_seconds]", file=sys.stderr)
        return 2
    var = args[0]
    try:
        ttl = int(args[1]) if len(args) > 1 else _DEFAULT_TTL
    except ValueError:
        print(f"static_token: ttl {args[1]!r} is not an integer", file=sys.stderr)
        return 2
    value = os.environ.get(var)
    if not value:
        # Non-zero so the proxy logs a clear mint failure (and keeps any cached value) rather
        # than injecting an empty token. The fix is to set the var in host.env on the Mac.
        print(f"static_token: ${var} is empty/unset — set it in host.env", file=sys.stderr)
        return 1
    json.dump({"value": value, "ttl": ttl}, sys.stdout)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
