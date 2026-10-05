"""Credential KINDS — the token protocols an ``[[inject]]`` row can name (ADR-0031).

A credential is an ``[[inject]]`` rule with a ``kind``. The kind is a PROTOCOL, shipped here as
package code with a FIXED shape of data fields; the row supplies only that data. ``static`` (the
default) echoes a host.env token; ``github-app`` exchanges an App JWT for an installation token;
``gh-cli`` injects the operator's own ``gh`` token. A new service whose token is static needs only
config, and a new protocol is a new kind — upstreamed, or shipped as an entry-point plugin — never a
command string in config (ADR-0023). The set stays closed and each shape stays fixed: that is the
boundary against a config DSL (no claim templates, no JSON paths).

What a kind decides, and nothing else: which fields its row takes, whether its host is pinned, the
minter command (a package module under foldyard's own interpreter), the secret it reads (always the
switch's derived ``FY_INJECT_<SWITCH>`` — never a name from the row) and how to check its shape
offline, and optionally a capability probe and the box-side end-to-end check. What EVERY row gets
whatever its kind — the switch, ``emergency``, ``box_env`` dummies and their ``fy verify`` row — is
:mod:`~foldyard.plugins.inject`'s.

**The credential owns its scope.** A ``github-app`` token carries whatever the App installation
grants; foldyard neither narrows nor caps it. For a different scope, create a different App and give
it its own switch. (The kind reports the scope; it doesn't enforce one.)

Stdlib only and import-light: the registry loads on the hot path, so the minter modules (and PyJWT)
are only ever imported lazily, inside a probe.
"""

from __future__ import annotations

import os
import re
import shlex
import sys
from collections.abc import Iterator

from .. import config
from . import CapabilityProbe, DoctorContext, InjectRule, Secret

# Fields every kind takes. `replay_on_401` is generic because whether a 401 is worth a re-mint is a
# property of the token, which the row may know better than the kind's default.
GENERIC_FIELDS = ("switch", "kind", "host", "label", "emergency", "box_env", "replay_on_401")
# Keys foldyard once honoured on a row and now IGNORES — reported, with their replacement, by
# `exposure.IGNORED_KEYS` (`fy config widenings`, the doctor row). Refusing them here would turn a
# config the report already explains into a registry that won't load.
RETIRED_FIELDS = ("minter", "token_env")

GITHUB_API = "api.github.com"

# The App private key's capture shape. A GLOB, not a regex (see keyless.secret_ok): `[[secret]]`
# patterns are repo-controlled, so the whole field is fnmatch-shaped and this one follows the same
# grammar. It demands the WHOLE shape — BEGIN, at least one body character (`?`), and a matching END
# — because the truncated paste this check exists to catch (a raw PEM's first line, pasted into a
# single-line prompt) satisfies the BEGIN marker on its own.
PEM_PATTERN = "*-----BEGIN *PRIVATE KEY-----?*-----END *PRIVATE KEY-----*"
PEM_HINT = (
    "download the GitHub App's private key (.pem) from the App settings, then "
    "`base64 < key.pem | tr -d '\\n'`"
)
"""The default "where do I get this?" line for the PEM capture prompt — the App settings, which
every consumer has. A consumer whose key lives in a vault overrides it with a `[[secret]]` row for
the switch's var carrying its own `how`. foldyard PRINTS this, never runs it, so no hint makes a
vault CLI a foldyard dependency."""

_NUMBER = re.compile(r"^[0-9]+$")
# A GitHub repository name: what `repositories` may hold. Never `owner/repo` — the installation
# already names the owner, and a slash would be a path segment in the request.
_REPO_NAME = re.compile(r"^[A-Za-z0-9._-]+$")


class Kind:
    """One token protocol. Subclasses set the class attributes and override the hooks they need;
    every hook receives the row (``spec``, already validated) and the switch's derived token var."""

    name = ""
    fields: tuple[str, ...] = ()  # the kind's OWN fields, beyond GENERIC_FIELDS
    required: tuple[str, ...] = ()  # of `fields`, the ones a row must give
    host = ""  # a pinned host ("" = the row names its own); a minted token goes nowhere else
    replay_on_401 = False  # the default for the row's `replay_on_401`
    emergency_only = False  # the switch must be `emergency = true` (its `on` expires)
    # Box env names this kind's client would read a REAL credential from. `fy verify` watches them
    # even where no `box_env` dummy names them: a real token there is exactly the leak to catch.
    credential_env: tuple[str, ...] = ()
    default_label = ""

    def allowed(self) -> tuple[str, ...]:
        return (*GENERIC_FIELDS, *self.fields)

    def check(self, spec: dict, where: str) -> None:
        """Kind-specific value checks, raising ``ValueError`` (the caller checks the shape)."""

    def source(self, var: str) -> str:
        """Where the credential comes from, for a report (`fy config widenings`). Never a value."""
        return f"token: ${var}"

    def label(self, spec: dict) -> str:
        return str(spec.get("label") or self.default_label or f"inject proxy ({spec.get('host')})")

    def rule(self, spec: dict, var: str) -> InjectRule | None:
        raise NotImplementedError

    def secret(self, spec: dict, var: str) -> Secret | None:
        return None

    def probes(self, spec: dict, var: str) -> list[CapabilityProbe]:
        return []

    def doctor_checks(
        self, spec: dict, var: str, active: bool, ctx: DoctorContext
    ) -> Iterator[tuple[str, str, str]]:
        return iter(())

    def box_doctor_checks(self, spec: dict, ctx: DoctorContext) -> Iterator[tuple[str, str, str]]:
        return iter(())


class StaticKind(Kind):
    """A long-lived token from host.env, echoed by :mod:`~foldyard.plugins.static_token` — the
    plain "rewrite ONE thing on ONE host" case, and the default when a row names no kind."""

    name = "static"
    fields = ("header", "query_param", "path_prefix", "value_prefix", "ttl")

    def rule(self, spec: dict, var: str) -> InjectRule | None:
        from .inject import _spec_to_rule  # the builder the keyless agents share

        return _spec_to_rule({**spec, "token_env": var})

    def secret(self, spec: dict, var: str) -> Secret | None:
        return Secret(var=var, label=f"{spec['switch']} token (injected on {spec.get('host')})")


class _GithubKind(Kind):
    """What the two GitHub protocols share: the pinned host, the client's credential vars, and the
    box-side check that the token is really reaching requests."""

    host = GITHUB_API
    replay_on_401 = True
    # gh reads GH_TOKEN first, then GITHUB_TOKEN; the old github verify row checked both.
    credential_env = ("GH_TOKEN", "GITHUB_TOKEN")

    def box_doctor_checks(self, spec: dict, ctx: DoctorContext) -> Iterator[tuple[str, str, str]]:
        """Is the token actually being injected into this box's requests?

        The check that was missing. When the host-side mint broke, `fy mode` still showed the switch
        on with the proxy `● up`, the capability probe still said the key was valid, and `fy doctor`
        was ALL PASS — all true statements about host-side configuration — while every request out
        of the box went unauthenticated and `gh` returned 401s. Only an end-to-end probe tells the
        two apart.

        `/rate_limit` is the cheapest witness and needs no scopes: GitHub reports 60 req/h for an
        anonymous caller and 5000+ for an authenticated one, so the LIMIT alone says whether
        injection happened. It never reveals the token — the box only ever holds the dummy."""
        name = f"{spec['switch']} injection"
        yield ("running", name, "")
        rc, out = ctx.run(
            ["curl", "-sS", "-o", "/dev/null", "-D", "-", f"https://{GITHUB_API}/rate_limit"],
            timeout=10,
        )
        if rc != 0:
            yield ctx.result(False, name, "", f"couldn't reach {GITHUB_API}")
            return
        limit = 0
        for line in out.splitlines():
            key, _, value = line.partition(":")
            if key.strip().lower() == "x-ratelimit-limit":
                limit = int(value.strip()) if value.strip().isdigit() else 0
        yield ctx.result(
            limit > 60,
            name,
            f"token reaching requests (rate limit {limit}/h)",
            (
                f"NOT injected — requests leave this box anonymous (rate limit {limit}/h). "
                f"`fy mode` can still read {spec['switch']}=on: the switch and the proxy are "
                "host-side state, this is the wire. `fy host restart` on your computer and re-run; "
                "if it persists, `fy doctor` there names the credential's problem."
            ),
        )


class GithubAppKind(_GithubKind):
    """A GitHub App INSTALLATION token (:mod:`~foldyard.plugins.github_app_token`): ≤1 h, minted
    host-side from the App's private key, scoped to whatever the installation grants."""

    name = "github-app"
    fields = ("app_id", "installation_id", "repositories")
    required = ("app_id", "installation_id")
    default_label = "GitHub App token (host-injected)"

    def check(self, spec: dict, where: str) -> None:
        for key in ("app_id", "installation_id"):
            value = spec[key]
            # A TOML integer or a string of digits. They land on the minter's argv and in a URL
            # path on api.github.com, and a number is all they are (bool is an int subclass).
            if isinstance(value, bool) or not _NUMBER.match(str(value)):
                raise ValueError(f"{where}: `{key}` must be a number, got {value!r}")
        repos = spec.get("repositories", [])
        if not isinstance(repos, list) or not all(
            isinstance(r, str) and _REPO_NAME.match(r) for r in repos
        ):
            raise ValueError(
                f"{where}: `repositories` must be a list of bare repository names "
                f'(e.g. ["Tangible"], not "owner/Tangible"), got {repos!r}'
            )

    def rule(self, spec: dict, var: str) -> InjectRule:
        cmd = [
            sys.executable,
            "-m",
            "foldyard.plugins.github_app_token",
            "--app-id",
            str(spec["app_id"]),
            "--installation-id",
            str(spec["installation_id"]),
        ]
        for repo in spec.get("repositories", []):
            cmd += ["--repository", repo]
        cmd += [
            # The PEM by NAME: the switch's derived var, the one host.env name `env` lets the
            # minter read (the addon resolves it and withholds the rest).
            "--pem-env",
            var,
            # Tell the minter which local proxy is OURS, so it honours a real (corporate) egress
            # proxy but never mints through the listener that rewrites Authorization on
            # api.github.com — that would replace the App JWT with the token being minted.
            # Per-worktree, hence resolved here.
            "--own-proxy-port",
            str(config.proxy_port()),
        ]
        return InjectRule(
            host=GITHUB_API,
            header="Authorization",
            minter=shlex.join(cmd),
            replay_on_401=bool(spec.get("replay_on_401", self.replay_on_401)),
            # NOT `requires` — the PEM gates nothing but this host (see InjectRule).
            env=(var,),
            label=self.label(spec),
        )

    def source(self, var: str) -> str:
        return f"App private key: ${var}"

    def secret(self, spec: dict, var: str) -> Secret:
        return Secret(
            var=var,
            label=f"GitHub App private key (PEM) for `{spec['switch']}`",
            how=PEM_HINT,
            pattern=PEM_PATTERN,
            b64=True,
        )

    def probes(self, spec: dict, var: str) -> list[CapabilityProbe]:
        # The continuous version of the PEM doctor rows. The switch promises "we can act as the
        # App"; the key can be rotated, the App uninstalled, a paste can be shaped like a PEM
        # without being one — each of which surfaced as `gh` 401ing inside the box while `fy mode`
        # showed green. /app (JWT-authenticated metadata) rather than a mint: a timer that
        # manufactures installation tokens is a worse idea than one that doesn't.
        app_id = str(spec["app_id"])

        def _check() -> tuple[bool, str]:
            from . import github_app_token  # lazy: pyjwt stays off the hot path

            try:
                return github_app_token.app_reachable(
                    app_id, _secret_value(var), config.proxy_port(), var=var
                )
            except Exception as e:  # a probe must never take the supervisor tick down
                return False, f"probe error ({type(e).__name__}: {e})"

        return [
            CapabilityProbe(
                switch=str(spec["switch"]),
                name=f"{spec['switch']}-github-app",
                check=_check,
                interval=300.0,  # a rotated key is rare; the call is cheap but not free
            )
        ]

    def doctor_checks(
        self, spec: dict, var: str, active: bool, ctx: DoctorContext
    ) -> Iterator[tuple[str, str, str]]:
        # PRESENCE, offline (ADR-0023): is the key where the minter reads it, shaped like a key?
        # Not "can a vault serve it?" — provenance is the operator's business. A resting switch
        # only WARNS: a second App kept off for emergencies mustn't fail doctor for a key nobody
        # needs until it's turned on.
        switch = spec["switch"]
        hint = _secret_hint(var, PEM_HINT)
        raw = _secret_value(var)
        yield ctx.result(
            True if raw else (False if active else None),
            f"{switch} PEM",
            f"present — ${var}",
            f"missing — `fy mode {switch}=on` prompts for it, or: {hint}",
        )
        if raw:
            from .. import keyless  # lazy: the registry hot path needn't import it

            yield ctx.result(
                keyless.secret_ok(raw, PEM_PATTERN, True) is not None,
                f"{switch} PEM shape",
                "decodes to a PEM private key",
                f"${var} isn't base64 of a PEM — re-capture it ({hint})",
            )


class GhCliKind(_GithubKind):
    """The operator's OWN ``gh`` token (:mod:`~foldyard.plugins.gh_cli_token`) — full user
    authority, push included, so only ever on an emergency switch whose ``on`` expires."""

    name = "gh-cli"
    emergency_only = True
    default_label = "EMERGENCY: your own gh token injected (push possible)"

    def source(self, var: str) -> str:
        return "your own gh token, from `gh auth token` on your computer"

    def rule(self, spec: dict, var: str) -> InjectRule:
        return InjectRule(
            host=GITHUB_API,
            header="Authorization",
            minter=shlex.join([sys.executable, "-m", "foldyard.plugins.gh_cli_token"]),
            replay_on_401=bool(spec.get("replay_on_401", self.replay_on_401)),
            env=(),  # `gh` holds its own credential; nothing from host.env
            label=self.label(spec),
        )

    def doctor_checks(
        self, spec: dict, var: str, active: bool, ctx: DoctorContext
    ) -> Iterator[tuple[str, str, str]]:
        # No install button: the minter runs `gh` on YOUR computer, and how that's installed is
        # yours to choose (the box's copy is the consumer's image's — ADR-0031).
        switch = spec["switch"]
        yield ctx.result(
            ctx.which("gh"),
            f"{switch} gh CLI",
            "installed",
            "missing — install gh (https://cli.github.com) on your computer",
        )
        if ctx.which("gh"):
            yield ("running", f"{switch} gh login", "")
            rc, _ = ctx.run(["gh", "auth", "token"], timeout=5)
            yield ctx.result(
                rc == 0, f"{switch} gh login", "token present", "not logged in — gh auth login"
            )


KINDS: dict[str, Kind] = {k.name: k for k in (StaticKind(), GithubAppKind(), GhCliKind())}
DEFAULT = "static"


def _secret_value(var: str) -> str:
    """``var`` as the minter would see it: an ambient export wins, then host.env — the precedence
    (and KEY=VALUE parsing) the addon applies. ``""`` when absent. For shape checks, never for
    logging."""
    from .. import keyless  # lazy: the registry hot path needn't import it

    return os.environ.get(var) or keyless.host_env_value(config.host_env_file(), var)


def _secret_hint(var: str, default: str) -> str:
    """The hint the operator actually sees, for doctor rows: a consumer's `[[secret]]` override for
    ``var`` if there is one (the capture prompt gets the same one from ``Registry.secrets``)."""
    for entry in config.secret_specs():
        if entry.get("var") == var and entry.get("how"):
            return str(entry["how"])
    return default
