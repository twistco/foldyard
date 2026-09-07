"""``github-app`` minter — a short-lived GitHub App INSTALLATION token, minted HOST-side.

Packaged, not a repo script: the proxy's mint path runs this on the host, next to the credential
daemons, so a minter living in the mount would hand anything that can write the checkout code
execution there — roughly hourly, unattended (ADR-0023).

Prints the ``{"value", "ttl"}`` JSON the egress proxy's ``INJECT_COMMAND`` contract expects (see
``assets/proxy/egress_proxy.py``), so the token is injected as the ``Authorization`` header in
flight and NEVER materialises in the box — not as env, not as a file.

The token is an *installation* token: ≤1 h, scoped to ONE repo and (by default) exactly
``pull_requests:write`` + ``issues:write``. A leaked one self-expires and can do nothing but
comment — the guarantee a long-lived ``gh auth token`` can't give (that's the ``gh-cli`` kind, for
the ``github=user`` emergency).

Env (HOST-only — never set these in the box):
  GH_APP_ID            the App's numeric id
  GH_INSTALLATION_ID   the installation id
  GH_REPO              bare repo name, NOT owner/repo
  GH_PEM_B64           base64 of the App private key (PEM) — the host.env form, since
                       ``load_host_env`` is line-based and can't hold a multi-line value.
                       ``fy box up`` captures it interactively (see :mod:`foldyard.keyless`).
  GH_APP_PERMISSIONS   optional JSON overriding the down-scoped permissions

Deliberately NOT here: any vault client. Where the PEM comes from is the operator's business —
foldyard checks PRESENCE (a doctor row) and the ``[[secret]]`` capture prompt echoes the consumer's
``how`` hint (e.g. a ``gcloud secrets versions access …`` command) for the human to run themselves.
Reading a vault from inside the minter is what coupled `github=app` to a live gcloud/PAM chain and
inverted the privilege scope — the highest-value identity on the Mac gating a PR comment.

Run (the github plugin builds this command from ``sys.executable``)::

    python -m foldyard.plugins.github_app_token
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
# Re-scoped DOWN even though the App itself holds only these — belt and braces against future
# App-permission drift. Override with GH_APP_PERMISSIONS (the github plugin derives it from
# `[plugins.github].permissions` when declared).
_DEFAULT_PERMISSIONS = {"pull_requests": "write", "issues": "write"}
# GitHub's permission levels, ordered — so `permissions()` can tell narrowing from widening.
_LEVELS = {"read": 1, "write": 2, "admin": 3}

_API = "https://api.github.com"


def pem() -> str:
    """The App private key. ONE source: base64 in host.env, which is the only shape host.env can
    carry (it's single-line) and the one the capture prompt writes. A second source bought nothing —
    a host file is exactly as trusted as host.env — and cost a branch in every place that asks
    whether the key is present."""
    raw = os.environ.get("GH_PEM_B64")
    if not raw:
        raise SystemExit(
            "github_app_token: no App private key — set GH_PEM_B64 in host.env "
            "(a TTY `fy box up` prompts for it)"
        )
    try:
        return base64.b64decode(raw.strip(), validate=True).decode()
    except (binascii.Error, ValueError, UnicodeDecodeError) as e:
        raise SystemExit(f"github_app_token: $GH_PEM_B64 is not valid base64 of a PEM ({e})")


def permissions() -> dict:
    """The permissions to scope the installation token down to — an override that may only NARROW.

    ``GH_APP_PERMISSIONS`` is derived from ``[plugins.github].permissions``, i.e. from REPO config,
    which anything that can write the checkout can edit. GitHub caps a token at what the App
    installation actually holds, but within that cap a widened map is a real escalation (a
    ``contents: write`` App would hand the box a push), so the ceiling is enforced here too: only
    keys in :data:`_DEFAULT_PERMISSIONS`, and never a higher level than the default's. Malformed
    input is fatal rather than a silent fallback — a consumer who MEANT to narrow must not get the
    wider default from a typo."""
    raw = os.environ.get("GH_APP_PERMISSIONS")
    if not raw:
        return dict(_DEFAULT_PERMISSIONS)
    try:
        parsed = json.loads(raw)
    except ValueError as e:
        raise SystemExit(f"github_app_token: $GH_APP_PERMISSIONS is not valid JSON ({e})")
    if not isinstance(parsed, dict):
        raise SystemExit("github_app_token: $GH_APP_PERMISSIONS must be a JSON object")
    for key, level in parsed.items():
        ceiling = _DEFAULT_PERMISSIONS.get(key)
        if ceiling is None:
            raise SystemExit(
                f"github_app_token: $GH_APP_PERMISSIONS may only NARROW the default "
                f"({', '.join(sorted(_DEFAULT_PERMISSIONS))}) — {key!r} isn't one of them"
            )
        if _LEVELS.get(str(level), 99) > _LEVELS[ceiling]:
            raise SystemExit(
                f"github_app_token: $GH_APP_PERMISSIONS {key}={level!r} exceeds the default "
                f"{ceiling!r} — this override may only narrow"
            )
    return parsed


def app_jwt(key: str, app_id: str, now: int | None = None) -> str:
    """Sign the App JWT (RS256). PyJWT is imported HERE, not at module load: this module is only
    ever run as a subprocess, but the import cost/failure should belong to the mint, and the
    dependency lives in the ``[host]`` extra (the box installs foldyard bare)."""
    try:
        import jwt
    except ImportError:  # pragma: no cover — depends on the install shape, not on logic
        raise SystemExit(
            "github_app_token: PyJWT is missing — reinstall the host extra "
            "(`just foldyard install`, i.e. `uv tool install 'foldyard[host]'`)"
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
    trying to obtain. Everything else is honoured, because on a Mac behind a mandatory egress proxy
    (corporate, or a local Zscaler-style client) it is the only way out.

    ``skip_port`` is foldyard's proxy port, passed on argv by the github plugin, which knows it
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
    app_id: str, installation_id: str, repo: str, key: str, skip_port: int | None = None
) -> str:
    body = json.dumps({"repositories": [repo], "permissions": permissions()}).encode()
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


def app_reachable(skip_port: int | None = None) -> tuple[bool, str]:
    """Can we act as the App right now? Signs a JWT and GETs ``/app`` — the metadata endpoint, which
    authenticates with the App JWT and mints NOTHING, so a continuous probe doesn't manufacture
    installation tokens on a timer. Returns ``(ok, human detail)``; the detail never carries a
    credential (it is rendered on the posture dashboards).

    This is the capability behind ``github=app``: App id + private key valid, and GitHub reachable.
    Everything the rung promises rides on it, and each link can lapse (a rotated key, a deleted App,
    a paste that only LOOKS like a PEM) while the mode dashboard shows green."""
    app_id = os.environ.get("GH_APP_ID")
    if not app_id:
        return False, "GH_APP_ID unset — set [plugins.github].app_id"
    try:
        key = pem()
    except SystemExit as e:
        return False, str(e.code).replace("github_app_token: ", "")
    try:
        token = app_jwt(key, app_id)
    except SystemExit as e:
        return False, str(e.code).replace("github_app_token: ", "")
    except Exception as e:  # a malformed key surfaces from the crypto layer, not as SystemExit
        return False, f"can't sign with the App key ({type(e).__name__}) — re-capture GH_PEM_B64"
    req = urllib.request.Request(
        f"{_API}/app",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    try:
        with _opener(skip_port).open(req, timeout=10) as resp:
            slug = json.load(resp).get("slug") or app_id
    except urllib.error.HTTPError as e:
        detail = e.read(200).decode(errors="replace").strip().replace("\n", " ")
        hint = " — key rotated or App deleted?" if e.code == 401 else ""
        return False, f"GitHub returned {e.code}{hint} ({detail[:100]})"
    except OSError as e:
        return False, f"can't reach {_API} ({e})"
    return True, f"App '{slug}' authenticates (key valid)"


def _skip_port(args: list[str]) -> int | None:
    """``--own-proxy-port <n>`` off argv (see :func:`_env_proxies`). Tolerant by design: a mint must
    not die on a malformed flag when the flag is only an optimisation — an unparseable value falls
    back to None (drop local proxies)."""
    if "--own-proxy-port" not in args:
        return None
    idx = args.index("--own-proxy-port")
    try:
        return int(args[idx + 1])
    except (IndexError, ValueError):
        return None


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    missing = [v for v in ("GH_APP_ID", "GH_INSTALLATION_ID", "GH_REPO") if not os.environ.get(v)]
    if missing:
        # Non-zero so the proxy logs a clear mint failure and keeps any cached token, rather than
        # injecting nothing.
        print(
            f"github_app_token: missing {', '.join(missing)} — set [plugins.github] in "
            "foldyard.toml (or host.env as the override)",
            file=sys.stderr,
        )
        return 1
    token = installation_token(
        os.environ["GH_APP_ID"],
        os.environ["GH_INSTALLATION_ID"],
        os.environ["GH_REPO"],
        pem(),
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
