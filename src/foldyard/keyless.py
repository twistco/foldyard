"""Keyless agent auth — the credential taxonomy + the host-side (Mac) capture of the real secret.

The prize (ADR-0008): the box runs Claude Code / Codex with a PLACEHOLDER
credential and the egress proxy rewrites it in flight with the real one, which lives ONLY in
``~/.foldyard/<project>/host.env`` on the Mac — never in the box, never in the repo. The same shape
as the github injector (``[[inject]]`` + a dummy + the ``static_token`` minter).

This module owns two things, both stdlib-only so the agent plugins can import the taxonomy on the
registry hot path:

  - **The taxonomy** (:data:`CLAUDE_KEYLESS`, :data:`CLAUDE_KEYLESS_HOST`): per keyless MODE, the
    box env var to bake a dummy into, the auth HEADER the client then emits, the DUMMY value, and —
    for the OAuth/``authorization`` case the ``value_prefix`` the proxy must prepend (``Bearer ``).
    The plugin reads this to derive its :class:`~foldyard.plugins.InjectRule` + dummy ``box_args``;
    the capture below reads it to know which host.env var to store under.

  - **The capture** (:func:`classify`, :func:`ensure_cred`): on ``fy box up`` (Mac, TTY) with a
    keyless mode configured and no matching cred in host.env yet, prompt ONCE for "an API key or
    token", classify it by prefix (no need to ask which — ``sk-ant-api`` → Claude key,
    ``sk-ant-oat`` → Claude OAuth, ``sk-`` → OpenAI/Codex key), and append it to host.env. The
    I/O (prompt/echo) is injected so the logic is pure + unit-testable.
"""

from __future__ import annotations

import base64
import json
import os
from collections.abc import Callable
from pathlib import Path

# ── the keyless credential taxonomy ────────────────────────────────────────────────────────────

# The Anthropic host whose auth header the Claude keyless injector rewrites.
CLAUDE_KEYLESS_HOST = "api.anthropic.com"

# Claude keyless modes → how the box authenticates keylessly. ``env`` is the var the box bakes a
# ``dummy`` into (so the client sends ``header``); the proxy overwrites ``header`` on
# CLAUDE_KEYLESS_HOST with the real value from the SAME-named host.env var. ``value_prefix`` is what
# the proxy prepends to that value (egress_proxy.py overwrites verbatim, so the ``Bearer `` for the
# OAuth ``authorization`` case is carried here — the bare token stays in host.env).
#   api-key  — ``sk-ant-api…`` keys: ``x-api-key`` carries the key bare (no prefix).
#   oauth    — ``sk-ant-oat…`` tokens (≈1yr, no refresh → static injection): the client sends
#              ``authorization: Bearer <token>`` (+ auto ``anthropic-beta``), so prepend Bearer.
# ``how`` is the one-line "where do I get this?" hint echoed at the capture prompt (below) — so a
# first-timer isn't left guessing what to paste.
CLAUDE_KEYLESS: dict[str, dict[str, str]] = {
    "api-key": {
        "env": "ANTHROPIC_API_KEY",
        "header": "x-api-key",
        "dummy": "sk-ant-dummy",
        "value_prefix": "",
        "how": "create one at https://console.anthropic.com/settings/keys",
    },
    "oauth": {
        "env": "CLAUDE_CODE_OAUTH_TOKEN",
        "header": "authorization",
        "dummy": "sk-ant-oat-dummy",
        "value_prefix": "Bearer ",
        "how": "run `claude setup-token` on the Mac to mint one",
    },
}

# The OpenAI host whose Authorization header the Codex keyless injector rewrites, and the Codex
# modes. ``api-key``: the box bakes a dummy ``OPENAI_API_KEY``; the client sends ``Authorization:
# Bearer <dummy>``; the proxy rewrites it with ``Bearer `` + the real key from host.env. (ChatGPT-
# subscription/OAuth keyless is deferred — see config.codex_keyless — so it's absent here.)
CODEX_KEYLESS_HOST = "api.openai.com"
CODEX_KEYLESS: dict[str, dict[str, str]] = {
    "api-key": {
        "env": "OPENAI_API_KEY",
        "header": "Authorization",
        "dummy": "sk-dummy",
        "value_prefix": "Bearer ",
        "how": "create one at https://platform.openai.com/api-keys",
    },
}


# Codex ChatGPT-subscription keyless: the box can't use a dummy ENV var (chatgpt mode reads
# ~/.codex/auth.json, not OPENAI_API_KEY), so it's structurally different from the api-key modes
# above and handled directly in the codex plugin. The proxy injects Authorization on this host+path;
# the account_id is baked into the box's dummy auth.json (an identifier, not a secret), and the
# Bearer access token is minted+refreshed host-side from the Mac's real auth.json.
CODEX_CHATGPT_HOST = "chatgpt.com"
CODEX_CHATGPT_PATH_PREFIX = "/backend-api/codex"
_FAR_FUTURE_EXP = 4102444800  # 2100-01-01 — codex refreshes at exp-5min, so this never triggers


def codex_auth_json_path() -> Path:
    """The Mac's Codex credential file the ChatGPT minter reads/refreshes: ``$CODEX_HOME``/auth.json
    (default ``~/.codex/auth.json``). The same path codex itself uses, so there's ONE source of
    truth — the minter refreshing it keeps a real codex on the Mac working too."""
    base = os.environ.get("CODEX_HOME")
    return (Path(base) if base else Path.home() / ".codex").expanduser() / "auth.json"


def codex_account_id(path: Path | None = None) -> str | None:
    """The ChatGPT ``account_id`` from the Mac's ``auth.json`` (``tokens.account_id``), or ``None``
    if absent/unreadable/not-chatgpt. Read host-side at box-up to bake into the box's dummy
    auth.json (so codex sends the right ``ChatGPT-Account-Id`` header). An id, not a secret."""
    try:
        data = json.loads((path or codex_auth_json_path()).read_text())
    except (OSError, ValueError):
        return None
    tokens = data.get("tokens")
    return tokens.get("account_id") if isinstance(tokens, dict) else None


def _dummy_jwt(exp: int) -> str:
    """A structurally-valid JWT (dummy, unverified signature) carrying just an ``exp`` claim. codex
    reads ``exp`` to decide whether to refresh (it never verifies its OWN token's signature), so a
    far-future exp makes the box's codex treat the dummy as fresh and NEVER refresh it (it would
    otherwise hit the dummy refresh token). The signature segment must be NON-EMPTY — codex rejects
    a JWT whose third segment is empty with "invalid ID token format" — so emit a fixed dummy one.
    Not on the hot path — built only when box_bootstrap writes the file."""

    def seg(obj: dict) -> str:
        return (
            base64.urlsafe_b64encode(json.dumps(obj, separators=(",", ":")).encode())
            .rstrip(b"=")
            .decode()
        )

    signature = base64.urlsafe_b64encode(b"fy-dummy-signature").rstrip(b"=").decode()
    return f"{seg({'alg': 'none', 'typ': 'JWT'})}.{seg({'exp': exp})}.{signature}"


def dummy_codex_auth_json(account_id: str) -> str:
    """The box's dummy ``~/.codex/auth.json`` for ChatGPT keyless: ``auth_mode: chatgpt`` + a
    far-future-exp dummy access/id token (so codex in the box never refreshes) + a dummy refresh
    token + the REAL ``account_id`` (so it emits the right ``ChatGPT-Account-Id``). codex thus sends
    ``Authorization: Bearer <dummy>`` + the account header, and the proxy overwrites the Bearer with
    the freshly-minted real token in flight. The real access/refresh tokens never enter the box."""
    return json.dumps(
        {
            "auth_mode": "chatgpt",
            "OPENAI_API_KEY": None,
            "tokens": {
                "id_token": _dummy_jwt(_FAR_FUTURE_EXP),
                "access_token": _dummy_jwt(_FAR_FUTURE_EXP),
                "refresh_token": "fy-dummy-refresh-token-never-used",
                "account_id": account_id,
            },
            "last_refresh": "2099-01-01T00:00:00Z",
        },
        indent=2,
    )


def inject_spec(taxonomy: dict, host: str, kind: str, label: str) -> dict | None:
    """The ``[[inject]]``-style spec dict for a keyless mode (host + header + token_env + the scheme
    value_prefix), or ``None`` if ``kind`` isn't in ``taxonomy``. The agent plugin feeds this to
    ``inject._spec_to_rule`` to build its :class:`~foldyard.plugins.InjectRule` — so claude + codex
    derive their rule identically, without either importing the other. Pure (no plugins import)."""
    spec = taxonomy.get(kind)
    if spec is None:
        return None
    return {
        "host": host,
        "header": spec["header"],
        "token_env": spec["env"],
        "value_prefix": spec["value_prefix"],
        # Static long-lived key/token — no rotation benefit to a 401 re-mint (re-reading the same
        # host.env value wouldn't fix a real 401).
        "replay_on_401": False,
        "label": label,
    }


def dummy_box_args(taxonomy: dict, kind: str) -> list[str]:
    """The ``-e <ENV>=<dummy>`` the box bakes so the client emits a (rewritable) auth header — for
    the keyless ``kind`` in ``taxonomy``, or ``[]`` if unknown. The real value never enters the box;
    the proxy overwrites it in flight. Shared by the claude + codex plugins' ``box_args``."""
    spec = taxonomy.get(kind)
    return ["-e", f"{spec['env']}={spec['dummy']}"] if spec else []


# Prefix → (host.env var, human label). Order matters: the Claude ``sk-ant-…`` prefixes are more
# specific than the bare OpenAI ``sk-`` and so must be tested first. ``sk-proj-…`` (OpenAI project
# keys) falls under ``sk-``. This is the "no need to ask which" classifier from the design.
_CLASSIFY: list[tuple[str, tuple[str, str]]] = [
    ("sk-ant-api", ("ANTHROPIC_API_KEY", "Claude API key")),
    ("sk-ant-oat", ("CLAUDE_CODE_OAUTH_TOKEN", "Claude OAuth token")),
    ("sk-", ("OPENAI_API_KEY", "OpenAI / Codex API key")),
]


def classify(secret: str) -> tuple[str, str] | None:
    """Classify a pasted secret by prefix → ``(host.env var, label)``, or ``None`` if unrecognised
    (so the caller can reject it rather than store a mystery value under the wrong name)."""
    s = secret.strip()
    for prefix, who in _CLASSIFY:
        if s.startswith(prefix):
            return who
    return None


# ── host.env read / append (matches supervisor.load_host_env's KEY=VALUE parsing) ───────────────


def host_env_has(path: Path, var: str) -> bool:
    """True when host.env defines ``var`` on a non-comment ``KEY=…`` line — the same parsing the
    supervisor's ``load_host_env`` uses, so "present" here means "the minter will see it"."""
    if not path.exists():
        return False
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.split("=", 1)[0].strip() == var:
            return True
    return False


def host_env_value(path: Path, var: str) -> str:
    """``var``'s value from host.env, or ``""`` — the same KEY=VALUE parsing (and quote-stripping)
    the supervisor's ``load_host_env`` applies, so what a caller reads here is exactly what a minter
    will see in its environment. For CHECKING a secret's shape, never for logging it."""
    if not path.exists():
        return ""
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        if key.strip() == var:
            return value.strip().strip("'\"")
    return ""


def append_host_env(path: Path, var: str, value: str) -> None:
    """Append ``VAR=value`` to host.env (creating it 0600), preserving any existing content. The
    file holds real secrets, so it lives OUTSIDE the repo mount (``config.host_env_file``) and is
    owner-only. We append rather than rewrite so a hand-edited host.env keeps its comments/order."""
    path.parent.mkdir(parents=True, exist_ok=True)
    body = path.read_text() if path.exists() else ""
    if body and not body.endswith("\n"):
        body += "\n"
    path.write_text(f"{body}{var}={value}\n")
    try:
        path.chmod(0o600)
    except OSError:  # pragma: no cover — best-effort tightening (e.g. exotic FS)
        pass


def secret_ok(value: str, pattern: str, b64: bool) -> str | None:
    """Validate a pasted secret against a :class:`~foldyard.plugins.Secret`'s shape and return the
    string to STORE, or ``None`` when it doesn't match (so the caller stores nothing rather than a
    mystery value under the wrong name — the same rule as :func:`classify`).

    ``pattern`` is a GLOB (``fnmatch``: ``*``, ``?``, ``[seq]``), matched case-sensitively against
    the whole value — deliberately NOT a regex. It comes from a consumer's ``[[secret]]`` row, i.e.
    from repo config that anything able to write the checkout can edit, and feeding that to Python's
    backtracking ``re`` engine makes `fy box up` hangable by a crafted pattern (`(.*)*x` against a
    2 KiB paste). A glob can't nest quantifiers, so the whole class is gone rather than mitigated —
    and every real shape check here is a prefix or a substring anyway.

    For a ``b64`` secret the paste must be valid base64 whose DECODED text matches, and the paste is
    stored verbatim. That's deliberately strict: host.env is single-line, so the declared ``how``
    hint ends in ``| base64`` and a raw multi-line PEM pasted into a single-line prompt would arrive
    truncated to its first line — which isn't valid base64, so it's refused loudly instead of stored
    as a broken key."""
    from fnmatch import fnmatchcase

    if b64:
        try:
            decoded = base64.b64decode(value, validate=True).decode()
        except (ValueError, UnicodeDecodeError):
            return None
        return value if (not pattern or fnmatchcase(decoded, pattern)) else None
    return value if (not pattern or fnmatchcase(value, pattern)) else None


def ensure_secret(
    host_env: Path,
    secret,
    *,
    interactive: bool,
    prompt: Callable[[str], str],
    echo: Callable[[str], None],
) -> str:
    """Make sure a declared :class:`~foldyard.plugins.Secret` is in host.env, prompting once on a
    Mac TTY. Same status vocabulary as :func:`ensure_cred` (``present`` / ``skipped`` / ``stored`` /
    ``mismatch`` / ``empty``) and the same non-blocking contract: without a TTY it WARNS and carries
    on, because a missing credential must never stop the box from booting — the minter degrades that
    one host (the proxy logs the mint failure) while everything else works.

    ``secret.how`` is echoed as the "where do I get this?" line and is **never executed** — the
    human runs it themselves and pastes the result. See :class:`~foldyard.plugins.Secret`.

    Pure but for the injected ``prompt``/``echo``, like :func:`ensure_cred`."""
    if host_env_has(host_env, secret.var):
        return "present"
    if not interactive:
        echo(
            f"⚠ {secret.label} isn't in {host_env} (${secret.var}) — the posture that needs it "
            f"can't mint until it's set. Run `fy box up` on the Mac to be prompted."
        )
        return "skipped"
    echo(f"▶ {secret.label}: paste it (stored on the host at 0600, never in the box or the repo).")
    if secret.how:
        echo(f"  (get it with: {secret.how})")
    if secret.b64:
        echo("  (single-line base64 — pipe the value through `base64` if the hint didn't)")
    value = prompt("  value (hidden): ").strip()
    if not value:
        echo("  (nothing entered — skipped; set it later with a TTY `fy box up`.)")
        return "empty"
    to_store = secret_ok(value, secret.pattern, secret.b64)
    if to_store is None:
        echo(
            f"✗ that doesn't look like {secret.label}"
            + (" (expected single-line base64)" if secret.b64 else "")
            + " — stored nothing, so nothing wrong-shaped reaches the minter."
        )
        return "mismatch"
    append_host_env(host_env, secret.var, to_store)
    echo(f"✓ stored {secret.label} in {host_env} (0600). The host minter reads it at mint time.")
    return "stored"


def ensure_cred(
    host_env: Path,
    expected_var: str,
    expected_label: str,
    *,
    interactive: bool,
    prompt: Callable[[str], str],
    echo: Callable[[str], None],
    how: str = "",
) -> str:
    """Make sure ``expected_var`` is in host.env, prompting once on a TTY if not. Returns a status:

      - ``"present"`` — already set (nothing to do).
      - ``"skipped"`` — not set but no TTY: warn (the Mac user must add it) and carry on (box-up
        must NOT block on a missing keyless cred — the box still boots, just can't reach Anthropic).
      - ``"stored"`` — prompted, classified, and appended under ``expected_var``.
      - ``"mismatch"`` / ``"empty"`` — the pasted value didn't classify to ``expected_var`` (or was
        blank): explain and store NOTHING, so the proxy never injects a wrong-shaped secret.

    Pure but for the injected ``prompt``/``echo`` (so tests drive it with no real stdin)."""
    if host_env_has(host_env, expected_var):
        return "present"
    if not interactive:
        echo(
            f"⚠ keyless is on but ${expected_var} isn't in {host_env} — the box will hold only a "
            f"dummy, can't reach the provider. Set it on the Mac (a TTY `fy box up` prompts)."
        )
        return "skipped"
    echo(f"▶ keyless auth: paste your {expected_label} (stored on the host, never in the box).")
    if how:
        echo(f"  ({how})")
    # The paste is hidden (no echo) — see box.py's getpass prompt — so it never lands in scrollback.
    secret = prompt("  key/token (hidden): ").strip()
    if not secret:
        echo("  (nothing entered — skipped; the box holds only a dummy until you set it.)")
        return "empty"
    who = classify(secret)
    if who is None or who[0] != expected_var:
        got = who[1] if who else "an unrecognised value"
        echo(
            f"✗ that looks like {got}, but [claude].keyless expects a {expected_label} "
            f"(${expected_var}). Stored nothing — fix [claude].keyless or paste the right value."
        )
        return "mismatch"
    append_host_env(host_env, expected_var, secret)
    echo(
        f"✓ stored your {expected_label} in {host_env} (0600). The proxy will inject it in flight."
    )
    return "stored"
