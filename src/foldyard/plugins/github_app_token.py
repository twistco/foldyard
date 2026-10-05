"""``github-app`` minter — a short-lived GitHub App INSTALLATION token, minted HOST-side.

Packaged, not a repo script: the proxy's mint path runs this on the host, next to the credential
daemons, so a minter living in the mount would hand anything that can write the checkout code
execution there — roughly hourly, unattended (ADR-0023). It is the minter of the ``github-app``
``[[inject]]`` kind (ADR-0031; the row's shape is :mod:`~foldyard.plugins.kinds`).

Prints the ``{"value", "ttl"}`` JSON the egress proxy's ``INJECT_COMMAND`` contract expects (see
``assets/proxy/egress_proxy.py``), so the token is injected as the ``Authorization`` header in
flight and NEVER materialises in the box — not as env, not as a file.

The token is an *installation* token: ≤1 h, and it carries **whatever the App installation
grants** — no ``permissions`` are asked for, so GitHub issues the installation's own scope. That is
deliberate (ADR-0031): the App's settings are the one place the box's GitHub scope is decided, and
widening them needs an org owner to accept GitHub's own permissions review. A second scope here
(once a package-default ceiling with a consumer map under it) only ever had to AGREE with the App,
and the day it didn't, a granted permission read as a broken credential. ``--repository`` narrows
the token to named repos of the installation (GitHub refuses one outside it, so it can only
narrow). A long-lived user token is the ``gh-cli`` kind, kept on an emergency switch.

Argv (non-secret, from the ``[[inject]]`` row — repo config, adopted on the host)::

  --app-id <n>            the App's numeric id
  --installation-id <n>   the installation id
  --repository <name>     optional, repeatable: bare repo names (NOT owner/repo)
  --pem-env <VAR>         the NAME of the env var holding base64 of the App private key (PEM) —
                          the switch's derived ``FY_INJECT_<SWITCH>``, the one host.env name the
                          rule lets this minter read (the addon withholds the rest)
  --own-proxy-port <n>    foldyard's own proxy port, never minted through (see _env_proxies)

Deliberately NOT here: any vault client. Where the PEM comes from is the operator's business —
foldyard checks PRESENCE (a doctor row) and the capture prompt echoes the ``how`` hint (e.g. a
``gcloud secrets versions access …`` command) for the human to run themselves. Reading a vault from
inside the minter is what coupled the App token to a live gcloud/PAM chain and inverted the
privilege scope — the highest-value identity on the host gating a PR comment.

Run (the inject plugin builds this command from ``sys.executable``)::

    python -m foldyard.plugins.github_app_token --app-id 1 --installation-id 2 \
        --pem-env FY_INJECT_GITHUB
"""

from __future__ import annotations

import base64
import binascii
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

# GitHub caps an App JWT's `exp` at 10 minutes out; 9 leaves room for clock skew.
_JWT_LIFETIME = 540
_JWT_BACKDATE = 60
# An installation token lives ~1 h. Report it minus a 5-min margin so the proxy re-mints before
# expiry (the addon subtracts a further 30 s of its own).
_TOKEN_TTL = 3600
_TTL_SAFETY_MARGIN = 300

_API = "https://api.github.com"


def decode_pem(raw: str, var: str) -> str:
    """The App private key from its host.env form: base64, the only shape host.env can carry (it's
    single-line) and the one the capture prompt writes. ``var`` names where it came from, for the
    error — which is the operator's next action, not a stack trace."""
    if not raw:
        raise SystemExit(
            f"github_app_token: no App private key — set {var} in host.env "
            "(`fy mode` prompts for it when the switch goes on)"
        )
    try:
        return base64.b64decode(raw.strip(), validate=True).decode()
    except (binascii.Error, ValueError, UnicodeDecodeError) as e:
        raise SystemExit(f"github_app_token: {var} is not valid base64 of a PEM ({e})")


def pem(var: str) -> str:
    """The App private key from the env var the rule named. ONE source: a second (a host file)
    bought nothing — a host file is exactly as trusted as host.env — and cost a branch in every
    place that asks whether the key is present."""
    return decode_pem(os.environ.get(var, ""), var)


def app_jwt(key: str, app_id: str, now: int | None = None) -> str:
    """Sign the App JWT (RS256). PyJWT is imported HERE, not at module load: this module is only
    ever run as a subprocess, but the import cost/failure should belong to the mint, and the
    dependency is one the box's install strips (``box.HOST_ONLY_DEPS``)."""
    try:
        import jwt
    except ImportError:  # pragma: no cover — depends on the install shape, not on logic
        from .proxy import host_install_hint

        raise SystemExit(
            "github_app_token: PyJWT is missing — reinstall foldyard: " + host_install_hint()
        )
    stamp = int(time.time()) if now is None else now
    return jwt.encode(
        {"iat": stamp - _JWT_BACKDATE, "exp": stamp + _JWT_LIFETIME, "iss": app_id},
        key,
        algorithm="RS256",
    )


_LOCAL_HOSTS = ("localhost", "127.0.0.1", "::1")


def _env_proxies(skip_port: int | None = None) -> dict[str, str]:
    """Env proxies to honour — all of them EXCEPT foldyard's own listener.

    The shell that launched ``fy host`` may export ``HTTPS_PROXY``. If that is foldyard's own
    egress proxy we must not mint through it: the inject rule we are minting FOR rewrites
    ``Authorization`` on api.github.com, so our App JWT would be replaced by the very token we're
    trying to obtain. Everything else is honoured, because on a host behind a mandatory egress proxy
    (corporate, or a local Zscaler-style client) it is the only way out.

    ``skip_port`` is foldyard's proxy port, passed on argv by the inject plugin, which knows it
    per worktree — so the exclusion is EXACT rather than "distrust anything local". Without it
    (a hand-run mint) fall back to dropping local proxies, which is the safe direction: a failed
    mint is loud, a JWT handed to our own rewriter is not."""
    out: dict[str, str] = {}
    for scheme in ("http", "https"):
        url = os.environ.get(f"{scheme}_proxy") or os.environ.get(f"{scheme.upper()}_PROXY") or ""
        if not url:
            continue
        split = urllib.parse.urlsplit(url)
        local = (split.hostname or "") in _LOCAL_HOSTS
        if local and (skip_port is None or split.port == skip_port):
            continue
        out[scheme] = url
    return out


def _opener(skip_port: int | None = None) -> urllib.request.OpenerDirector:
    """An opener with an EXPLICIT proxy set (see :func:`_env_proxies`) — never urllib's implicit
    env discovery, so the self-injection case above can't sneak back in."""
    return urllib.request.build_opener(urllib.request.ProxyHandler(_env_proxies(skip_port)))


def installation_token(
    app_id: str,
    installation_id: str,
    repositories: list[str],
    key: str,
    skip_port: int | None = None,
) -> str:
    # No `permissions`: the installation's own scope is the token's (see the module docstring).
    # `repositories` only when declared — an empty list would be a request for none.
    body = json.dumps({"repositories": repositories} if repositories else {}).encode()
    req = urllib.request.Request(
        f"{_API}/app/installations/{installation_id}/access_tokens",
        data=body,
        method="POST",
        headers={
            "Authorization": f"Bearer {app_jwt(key, app_id)}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "Content-Type": "application/json",
        },
    )
    try:
        with _opener(skip_port).open(req, timeout=15) as resp:
            payload = json.load(resp)
    except urllib.error.HTTPError as e:
        # The body carries GitHub's reason ("Integration not found", a bad JWT, …) — worth
        # surfacing, and it never contains our credential.
        detail = e.read(500).decode(errors="replace").strip()
        raise SystemExit(f"github_app_token: GitHub returned {e.code} — {detail}")
    except OSError as e:
        raise SystemExit(f"github_app_token: couldn't reach {_API} ({e})")
    token = payload.get("token")
    if not token:
        raise SystemExit("github_app_token: GitHub's response carried no token")
    return str(token)


_ACCESS_LEVELS = ("read", "write", "admin")


def _scope(payload: dict) -> dict | None:
    """The installation's scope from its metadata — ``{"permissions": {name: level},
    "repository_selection": "all" | "selected"}`` — or None when the payload doesn't carry one
    this can read. Never a guess: an empty map would read as "grants nothing", which the next good
    read would then report as a widening."""
    permissions = payload.get("permissions")
    if not isinstance(permissions, dict) or not all(
        isinstance(k, str) and v in _ACCESS_LEVELS for k, v in permissions.items()
    ):
        return None
    return {
        "permissions": dict(permissions),
        "repository_selection": str(payload.get("repository_selection") or ""),
    }


def installation_probe(
    app_id: str,
    installation_id: str,
    pem_b64: str,
    skip_port: int | None = None,
    *,
    var: str = "FY_INJECT_<SWITCH>",
) -> tuple[bool, str, dict | None]:
    """Can we act as the App right now, and what does its installation grant? Signs a JWT and GETs
    ``/app/installations/{id}`` — metadata, authenticated with the App JWT, minting NOTHING, so a
    continuous probe doesn't manufacture installation tokens on a timer. ``pem_b64`` is the key
    exactly as host.env holds it, under ``var`` (named in the detail, so a missing key says where it
    goes). Returns ``(ok, human detail, scope)``: the detail never carries a credential (it is
    rendered on the mode dashboards), and ``scope`` is :func:`_scope`'s, or None when unread.

    This is the capability behind a ``github-app`` switch: App id + private key valid, the
    installation there, and GitHub reachable. Everything the switch promises rides on it, and each
    link can lapse (a rotated key, a deleted App, an uninstall, a paste that only LOOKS like a PEM)
    while the mode dashboard shows green. The scope is the other half (ADR-0031): foldyard doesn't
    cap what the installation grants, so it reports it, and the supervisor says when it changes."""
    try:
        key = decode_pem(pem_b64, var)
        token = app_jwt(key, app_id)
    except SystemExit as e:
        return False, str(e.code).replace("github_app_token: ", ""), None
    except Exception as e:  # a malformed key surfaces from the crypto layer, not as SystemExit
        return False, f"can't sign with the App key ({type(e).__name__}) — re-capture it", None
    req = urllib.request.Request(
        f"{_API}/app/installations/{installation_id}",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    try:
        with _opener(skip_port).open(req, timeout=10) as resp:
            payload = json.load(resp)
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return (
                False,
                f"installation {installation_id} not found — the App was uninstalled, or the "
                "row's `installation_id` is wrong",
                None,
            )
        detail = e.read(200).decode(errors="replace").strip().replace("\n", " ")
        hint = " — key rotated or App deleted?" if e.code == 401 else ""
        return False, f"GitHub returned {e.code}{hint} ({detail[:100]})", None
    except (OSError, ValueError) as e:
        return False, f"can't reach {_API} ({e})", None
    payload = payload if isinstance(payload, dict) else {}
    slug = payload.get("app_slug") or app_id
    scope = _scope(payload)
    if payload.get("suspended_at"):
        return False, f"App '{slug}': installation {installation_id} is suspended", scope
    if scope is None:
        return True, f"App '{slug}' authenticates (key valid); scope unavailable", None
    levels = scope["permissions"].values()
    elevated = sum(1 for level in levels if level != "read")
    return (
        True,
        f"App '{slug}' authenticates (key valid); installation grants {len(levels)} "
        f"permission{'' if len(levels) == 1 else 's'}, {elevated} at write or admin",
        scope,
    )


def _flag(args: list[str], name: str) -> str:
    """The value after ``name`` on argv, or ``""``."""
    if name not in args:
        return ""
    idx = args.index(name)
    return args[idx + 1] if idx + 1 < len(args) else ""


def _skip_port(args: list[str]) -> int | None:
    """``--own-proxy-port <n>`` off argv (see :func:`_env_proxies`). Tolerant by design: a mint must
    not die on a malformed flag when the flag is only an optimisation — an unparseable value falls
    back to None (drop local proxies)."""
    try:
        return int(_flag(args, "--own-proxy-port"))
    except ValueError:
        return None


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    flags = {f: _flag(args, f) for f in ("--app-id", "--installation-id", "--pem-env")}
    missing = [f for f, value in flags.items() if not value]
    if missing:
        # Non-zero so the proxy logs a clear mint failure and keeps any cached token, rather than
        # injecting nothing. The inject plugin always passes all three, so this is a hand-run.
        print(f"github_app_token: missing {', '.join(missing)}", file=sys.stderr)
        return 1
    repositories = [args[i + 1] for i, a in enumerate(args[:-1]) if a == "--repository"]
    token = installation_token(
        flags["--app-id"],
        flags["--installation-id"],
        repositories,
        pem(flags["--pem-env"]),
        _skip_port(args),
    )
    json.dump({"value": f"Bearer {token}", "ttl": _TOKEN_TTL - _TTL_SAFETY_MARGIN}, sys.stdout)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SystemExit as e:
        # SystemExit(str) from the helpers above: print the reason to stderr and exit 1, so the
        # proxy's mint-failure log carries it (and the addon degrades only this host).
        if isinstance(e.code, str):
            print(e.code, file=sys.stderr)
            raise SystemExit(1) from None
        raise
