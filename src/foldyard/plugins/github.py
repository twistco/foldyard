"""github plugin — the ``github`` credential axis + its egress-proxy injection rule (ADR-0015).

  off    no GitHub credential anywhere
  app    PR/issue comments on the one repo (App installation token, injected HOST-side by
         the egress proxy — only a dummy GH_TOKEN ever lives in the box)
  user   EMERGENCY: your own gh token injected (push becomes possible); TTL-bound

Built-in for now; splits into the ``github-app`` + ``header-auth`` packages at extraction time
(ADR-0015). The whole surface self-gates on the consumer declaring ``[plugins.github]``
(even empty — the ``user`` emergency needs no App fields), per the registry's inert-until-declared
contract: no declaration ⇒ no axis, no doctor rows, no box plumbing.
Owns its axis (rungs/blurb/emergency), its ``proxy_rules`` injection rule
(``api.github.com`` Authorization ← the App/user token minter), the dummy ``GH_TOKEN=x`` box
env, the github-side verify posture, and the github-side doctor checks + fixes (gh CLI / gh login /
host.env keys / [deep] PEM). The egress proxy ITSELF — the mitmdump daemon, the CA mount + proxy
env, the egress log + Network Log panel, AND the mitmproxy/CA doctor checks+fixes — lives in the
``proxy`` plugin (github just rides it). Stdlib only.
"""

from __future__ import annotations

import json
import os
import shlex
import sys
from collections.abc import Iterable, Iterator
from pathlib import Path

from .. import config
from . import (
    Axis,
    CapabilityProbe,
    DoctorContext,
    DoctorFix,
    InjectRule,
    Plugin,
    Secret,
    VerifyContext,
    proxy,
)

_BLURB = {
    "off": "no GitHub credential anywhere",
    "app": "PR/issue comments on the one repo (App token, host-injected)",
    "user": "EMERGENCY: your own gh token injected (push possible)",
}


def _minter(rung: str) -> str:
    """The token minter command the proxy injects with: :mod:`~foldyard.plugins.github_app_token`
    for ``app``, :mod:`~foldyard.plugins.gh_cli_token` for the ``user`` emergency — both PACKAGE
    modules, run under foldyard's own interpreter exactly as ``inject``/``codex`` already run
    theirs. Never a path inside the mount and never a command from config: the mint path runs
    host-side beside the credential daemons, so whoever chooses that code owns the host
    (ADR-0023)."""
    if rung != "app":
        return shlex.join([sys.executable, "-m", "foldyard.plugins.gh_cli_token"])
    # Tell the minter which local proxy is OURS, so it honours a real (corporate) egress proxy but
    # never mints through the listener that rewrites Authorization on api.github.com — that would
    # replace the App JWT with the token being minted. Per-worktree, hence resolved here.
    return shlex.join(
        [
            sys.executable,
            "-m",
            "foldyard.plugins.github_app_token",
            "--own-proxy-port",
            str(config.proxy_port()),
        ]
    )


# The App private key's host.env var + the shape a paste must have. base64 because
# `supervisor.load_host_env` is single-line KEY=VALUE and a PEM isn't (see plugins.Secret).
_PEM_VAR = "GH_PEM_B64"
# A GLOB, not a regex (see keyless.secret_ok): `[[secret]]` patterns are repo-controlled, so the
# whole field is fnmatch-shaped and this one follows the same grammar. It demands the WHOLE shape —
# BEGIN, at least one body character (`?`), and a matching END — because the truncated paste this
# check exists to catch (a raw PEM's first line, pasted into a single-line prompt) satisfies the
# BEGIN marker on its own.
_PEM_PATTERN = "*-----BEGIN *PRIVATE KEY-----?*-----END *PRIVATE KEY-----*"


def _pem_b64() -> str:
    """The base64 PEM the minter would read: an ambient export wins, then host.env — the same
    precedence (and the same KEY=VALUE parsing) the supervisor applies, so this is exactly what the
    minter will see. Returns ``""`` when absent. For shape checks, never for logging."""
    from .. import keyless  # lazy: the registry hot path needn't import it

    return os.environ.get(_PEM_VAR) or keyless.host_env_value(config.host_env_file(), _PEM_VAR)


def _pem_present() -> bool:
    """Is the App private key where the minter reads it? PRESENCE only — foldyard deliberately
    doesn't know or care how it got there (that's the ``[[secret]]`` ``how`` hint's job, which the
    operator runs themselves)."""
    return bool(_pem_b64())


def _pem_b64_ok(raw: str) -> bool:
    """Cheap OFFLINE sanity: ``raw`` is base64 of something PEM-private-key-shaped. Catches the
    classic truncated/half-pasted key, which otherwise surfaces as an opaque 401 at mint time."""
    from .. import keyless

    return keyless.secret_ok(raw, _PEM_PATTERN, True) is not None


_PEM_HINT = (
    "download the GitHub App's private key (.pem) from the App settings, then "
    "`base64 < key.pem | tr -d '\\n'`"
)
"""The default "where do I get this?" line for the PEM capture prompt — the App settings, which
every consumer has. A consumer whose key lives in a vault overrides it with a `[[secret]]` row for
`GH_PEM_B64` carrying its own `how` (Tangible's names its GSM secret; see `foldyard.toml`).
foldyard PRINTS this, never runs it, so no hint makes a vault CLI a foldyard dependency."""


def _pem_hint() -> str:
    """The hint the operator actually sees, for the DOCTOR rows. The capture prompt gets the
    consumer's override from ``Registry.secrets``; doctor reads plugins directly, so it applies the
    same override itself rather than quoting a generic default beside a vault-specific prompt."""
    for entry in config.secret_specs():
        if entry.get("var") == _PEM_VAR and entry.get("how"):
            return str(entry["how"])
    return _PEM_HINT


def _box_github_mode(env: dict) -> str:
    """The box's GitHub posture, mode-aware (ported from verify). No proxy CA mounted ⇒
    ``off``. The CA can now be mounted by the proxy's ``capture`` axis with github OFF, so read
    the actual github rung from the gitignored repo mirror — ``off``/``app``/``user`` all honoured
    (capture-only ⇒ ``off``); only a missing/corrupt mirror falls back to ``app`` (CA present, mode
    unknown → assume the historical github-proxy reason rather than under-reporting). A consumer
    with no ``[plugins.github]`` is ``off`` outright: the CA is ambient proxy substrate (capture,
    claude keyless, …), so its presence must never read as github intent — the old fallback made
    verify assert app-mode invariants in boxes that never had github at all."""
    if not config.github_declared() or not proxy.BOX_CA.exists():
        return "off"
    from .. import stack  # lazy: keep the registry-load hot path import-light

    checkout = env.get("FOLDYARD_CHECKOUT") or str(stack.main_repo())
    mirror = Path(checkout) / config.dev_vm_rel() / ".dev-mode.json"
    try:
        mode = json.loads(mirror.read_text()).get("github")
    except (OSError, ValueError):
        mode = None
    return mode if mode in ("off", "app", "user") else "app"


def _verify_rows(
    mode: str, gh_present: bool, gh_token: str, github_token: str
) -> Iterator[tuple[str, str]]:
    """The github posture assertions as ``(status, message)`` rows (status pass|fail|info).
    Split out from ``verify_checks`` so the row logic is unit-testable apart from the
    CA/mirror mode-detection."""
    if mode == "off":
        # The invariant is NO GITHUB ACCESS, not binary absence: the dummy 'x' and the gh CLI
        # are ambient box plumbing (baked with the proxy substrate so the axis flips live,
        # host-side — see box_args) and grant nothing while the host injects no credential.
        # A real-looking token is the actual leak, and the core's git-push-refused backstop
        # covers the push path.
        if gh_token in ("", "x") and not github_token:
            yield ("pass", "no real GitHub token (dummy 'x' is inert without host-side injection)")
        else:
            yield ("fail", "GitHub token in env")
        if gh_present:
            yield ("pass", "gh CLI present but credential-less (github=off: nothing injected)")
        else:
            yield ("pass", "no gh CLI")
        return

    if mode == "user":
        yield (
            "info",
            "⚠ github=user EMERGENCY mode — your own token is injected host-side "
            "(TTL-bound, auto-reverts)",
        )
    if gh_token == "x" and not github_token:
        yield (
            "pass",
            f"github={mode} proxy mode: GH_TOKEN is the dummy 'x' (real token stays host-side)",
        )
    else:
        yield (
            "fail",
            f"github={mode} proxy mode but GH_TOKEN/GITHUB_TOKEN is not the dummy 'x' "
            "— possible real token in the box",
        )
    kind = "comment-only App token" if mode == "app" else "EMERGENCY user token"
    yield ("pass", f"gh CLI allowed in proxy mode ({kind}; push via ssh still refused below)")


class GithubPlugin(Plugin):
    name = "github"

    def axes(self) -> list[Axis]:
        # Self-gated on [plugins.github] — any table, even empty (the user emergency needs no App
        # fields) — like claude/codex keyless: the registry contract is CORE plugins stay inert
        # until their own config is declared, and github was the last always-on exception. No
        # declaration ⇒ no TUI mode row, and `fy mode github=…` is an unknown-axis error.
        if not config.github_declared():
            return []
        return [
            Axis(
                name="github",
                rungs=("off", "app", "user"),
                blurb=_BLURB,
                daemon="egress-proxy",
                emergency=("user",),
            )
        ]

    def proxy_rules(self, mode: dict) -> list[InjectRule]:
        # github's egress-proxy injection: rewrite Authorization on api.github.com with a
        # host-minted token (App installation token for app, your own gh token for the user
        # emergency), re-minting + re-issuing once on a 401. The proxy plugin turns this into the
        # single mitmdump daemon + the FY_PROXY box env; the real token never enters the box.
        rung = mode.get("github", "off")
        if rung == "off":
            return []
        label = "GitHub injection proxy" + (
            " (App token)" if rung == "app" else " (YOUR user token)"
        )
        # The App identity trio only (all derived from [plugins.github], so present by
        # construction; host.env stays the override). The PEM is deliberately NOT in `requires`:
        # `requires` is the supervisor's spawn gate for the WHOLE proxy daemon, and under Phase A′
        # always-route a proxy that refuses to launch connection-refuses every box request — a
        # missing credential must degrade ONE host (the addon logs the mint failure), never all
        # egress. Presence is surfaced by the doctor row + the `secrets` capture prompt instead.
        requires: tuple[str, ...] = ()
        # `env` is what the minter may READ (the addon withholds the rest of host.env). The gh-cli
        # kind needs nothing beyond the base PATH/HOME — `gh` holds its own credential.
        env: tuple[str, ...] = ()
        if rung == "app":
            requires = ("GH_APP_ID", "GH_INSTALLATION_ID", "GH_REPO")
            env = (*requires, _PEM_VAR, "GH_APP_PERMISSIONS")
        return [
            InjectRule(
                host="api.github.com",
                header="Authorization",
                minter=_minter(rung),
                replay_on_401=True,
                requires=requires,
                env=env,
                label=label,
            )
        ]

    def env_defaults(self, mode: dict) -> dict[str, str]:
        # github=app's non-secret identity is DERIVABLE, not something a human must hand-type: app
        # id/installation id/repo are static, non-secret Pulumi config already committed to
        # foldyard.toml ([plugins.github], mirroring [plugins.gcp-metadata]). host.env / an ambient
        # export remain the override for a different repo/App (the supervisor applies these via
        # setdefault — see plugins.Plugin.env_defaults). The PEM is NOT here: it's the one secret,
        # declared via `secrets` and read by the minter from host.env alone.
        if mode.get("github", "off") != "app":
            return {}
        values = {
            "GH_APP_ID": config.github_app_id(),
            "GH_INSTALLATION_ID": config.github_installation_id(),
            "GH_REPO": config.github_repo(),
            "GH_APP_PERMISSIONS": config.github_permissions(),
        }
        return {k: v for k, v in values.items() if v}

    def secrets(self, mode: dict) -> list[Secret]:
        # github=app needs the App private key present host-side. Declared as DATA so the capture
        # prompt, the doctor row and the docs all read from one place — and so the "where do I get
        # it?" hint is a string foldyard prints, not a command it runs (plugins.Secret).
        if mode.get("github", "off") != "app":
            return []
        return [
            Secret(
                var=_PEM_VAR,
                label="GitHub App private key (PEM)",
                how=_PEM_HINT,
                pattern=_PEM_PATTERN,
                b64=True,
            )
        ]

    def derive_env(self, mode: dict) -> dict[str, str]:
        # A github-owned marker (GH_INJECT=<rung>) recording that a github rung is on — it lights
        # up `config.proxy_enabled` for proxy-less consumers and makes the rung visible to
        # reconcile. It does NOT gate the box's dummy GH_TOKEN anymore (that's ambient with the
        # proxy substrate — see box_args), so a github flip changes no box-side state.
        rung = mode.get("github", "off")
        return {"GH_INJECT": rung} if rung != "off" else {}

    def box_args(self, env: dict) -> list[str]:
        # The dummy GH_TOKEN, AMBIENT with the proxy substrate (FY_PROXY — always set under
        # Phase A′ always-route), NOT gated on a github rung. Mirrors the proxy plugin's ambient
        # CA trust: pre-position the inert box-side plumbing so the github axis flips LIVE,
        # host-side only (`fy mode github=app` → the supervisor starts injecting), with no box
        # recreate — exactly how the gcp axis already works (metadata env always baked, the
        # host-side minter is the gate). The dummy grants nothing: it's the literal string "x",
        # and whether api.github.com requests gain a real credential is decided solely by the
        # host proxy's inject rule. The real token never enters the box in any mode. A consumer
        # with no [plugins.github] has no axis to flip, so it gets no dummy either.
        return ["-e", "GH_TOKEN=x"] if env.get("FY_PROXY") and config.github_declared() else []

    def box_bootstrap(self, env: dict) -> list[dict]:
        # The gh CLI rides the same ambient gate as the dummy token; the `check` skips it when a
        # consumer's own box image already bakes gh. gh without a real
        # credential is inert (the dummy 'x' 401s unless the host injects), so presence is not a
        # posture leak; verify asserts the REAL invariant instead (no real token in env, push
        # refused). Install is IMAGE-AGNOSTIC — a static release binary into the persisted
        # /opt/fy-tools volume, the house pattern for every bootstrap step — never a distro
        # package manager: a package-manager install ties the step to one image family — the
        # packaged box has been Fedora (dnf) and is now Debian (apt), and an `apt-get` here
        # once failed every bootstrap on the Fedora base ("apt-get: command not found").
        # Undeclared consumers skip the install entirely — no axis means gh could never
        # gain a credential here.
        if not (env.get("FY_PROXY") and config.github_declared()):
            return []
        return [
            {
                "label": "gh CLI (rides the injection proxy)",
                "check": "command -v gh",
                "run": (
                    'arch=$(uname -m) && case "$arch" in x86_64) arch=amd64;; '
                    "aarch64|arm64) arch=arm64;; esac && "
                    "ver=$(curl -fsSLI -o /dev/null -w '%{url_effective}' "
                    "https://github.com/cli/cli/releases/latest) && "
                    "ver=${ver##*/v} && mkdir -p /opt/fy-tools/bin && "
                    'curl -fsSL "https://github.com/cli/cli/releases/download/'
                    'v${ver}/gh_${ver}_linux_${arch}.tar.gz" '
                    '| tar -xzO "gh_${ver}_linux_${arch}/bin/gh" > /opt/fy-tools/bin/gh && '
                    "chmod 755 /opt/fy-tools/bin/gh"
                ),
            }
        ]

    def verify_checks(self, ctx: VerifyContext) -> Iterator[tuple[str, str]]:
        # In-box only: EVERY proxied box legitimately carries the proxy CA + a DUMMY GH_TOKEN=x
        # + the gh CLI (ambient plumbing so the axis flips live — box_args). What verify asserts
        # is ACCESS: no real-looking token in any mode, and the core's git-push-refused backstop
        # as the real guarantee. Mode only changes the wording (off ⇒ "inert", app/user ⇒
        # "injected host-side").
        if not ctx.in_box:
            return
        yield from _verify_rows(
            _box_github_mode(ctx.env),
            ctx.which("gh"),
            os.environ.get("GH_TOKEN", ""),
            os.environ.get("GITHUB_TOKEN", ""),
        )

    def box_doctor_checks(self, ctx: DoctorContext) -> Iterator[tuple[str, str, str]]:
        """
        Is the App token actually being injected into this box's requests?

        The check that was missing. When the host-side mint broke, `fy mode` still showed
        `github app` with the proxy `● up`, the mirror's capability probe still said the App
        key was valid, and `fy doctor` was ALL PASS — all of them true statements about
        host-side configuration — while every request out of the box went unauthenticated and
        `gh` returned 401s. Only an end-to-end probe distinguishes the two.

        `/rate_limit` is the cheapest witness and needs no scopes: GitHub reports 60 req/h for
        an anonymous caller and 5000+ for an authenticated one, so the LIMIT alone says whether
        injection happened. It never reveals the token — the box only ever holds the dummy.
        """
        if _box_github_mode({}) != "app":
            return
        yield ("running", "github injection", "")
        rc, out = ctx.run(
            ["curl", "-sS", "-o", "/dev/null", "-D", "-", "https://api.github.com/rate_limit"],
            timeout=10,
        )
        if rc != 0:
            yield ctx.result(False, "github injection", "", "couldn't reach api.github.com")
            return
        limit = 0
        for line in out.splitlines():
            name, _, value = line.partition(":")
            if name.strip().lower() == "x-ratelimit-limit":
                limit = int(value.strip()) if value.strip().isdigit() else 0
        yield ctx.result(
            limit > 60,
            "github injection",
            f"App token reaching requests (rate limit {limit}/h)",
            (
                f"NOT injected — requests leave this box anonymous (rate limit {limit}/h). "
                "`fy mode` can still read github=app: the axis and the proxy are host-side "
                "state, this is the wire. Restart `fy host` on the Mac and re-run; if it "
                "persists the App installation likely needs re-authorizing."
            ),
        )

    def doctor_checks(self, ctx: DoctorContext):
        # Whole-hook gate, matching the axis: a consumer with no [plugins.github] gets NO github
        # rows — with no rung to serve, even the gh CLI/login rows are noise (they nagged every
        # undeclared project's doctor/TUI).
        if not config.github_declared():
            return
        yield ctx.result(
            ctx.which("gh"),
            "gh CLI",
            "installed",
            "missing — brew install gh (needed for github=user)",
        )
        if ctx.which("gh"):
            yield ("running", "gh login", "")
            rc, _ = ctx.run(["gh", "auth", "token"], timeout=5)
            yield ctx.result(rc == 0, "gh login", "token present", "not logged in — gh auth login")
        # NB: the `mitmproxy` + `mitm CA` checks are the PROXY plugin's now (github is just one
        # injector that rides the proxy) — see proxy.ProxyPlugin.doctor_checks.

        # The App's non-secret identity is DERIVABLE (config.github_*, mirroring gcp._sa_email) —
        # from [plugins.github], not something a human must type into host.env. Check via those
        # functions (which also honour an env/host.env override).
        resolved = {
            "GH_APP_ID": config.github_app_id(),
            "GH_INSTALLATION_ID": config.github_installation_id(),
            "GH_REPO": config.github_repo(),
        }
        # The App-rung rows below additionally need App INTENT — a resolved identity value or a
        # captured PEM — so a bare [plugins.github] table (a user-emergency-only consumer, which
        # needs no App config) keeps the gh rows above without an eternal "✗ github=app keys /
        # PEM missing" nag.
        if not (any(resolved.values()) or _pem_present()):
            return
        missing = [k for k, v in resolved.items() if not v]
        yield ctx.result(
            not missing,
            "github=app keys",
            "resolved (foldyard.toml, or host.env/env override)",
            f"missing {', '.join(missing)} — set [plugins.github] in foldyard.toml or "
            f"{config.host_env_file()}",
        )

        # The App PEM: PRESENCE, offline. Deliberately NOT "can a vault serve it?" — the packaged
        # minter reads it from host.env, so this needs no gcloud, no PAM grant, no --deep, and works
        # in the box. Provenance is the operator's business (the `secrets` capture hint); foldyard's
        # is whether the minter will find something usable.
        yield ctx.result(
            _pem_present(),
            "github=app PEM",
            f"present — ${_PEM_VAR}",
            f"missing — a TTY `fy box up` prompts for it, or: {_pem_hint()}",
        )
        raw = _pem_b64()
        if raw:
            yield ctx.result(
                _pem_b64_ok(raw),
                "github=app PEM shape",
                "decodes to a PEM private key",
                f"${_PEM_VAR} isn't base64 of a PEM — re-capture it ({_pem_hint()})",
            )

    def capability_probes(self, mode: dict) -> list[CapabilityProbe]:
        # The continuous version of the PEM doctor rows. `github=app` promises "we can act as the
        # App"; the key can be rotated, the App uninstalled, a paste can be shaped like a PEM
        # without being one — each of which used to surface as `gh` 401ing inside the box while
        # `fy mode` showed green. The probe hits /app (App metadata, JWT-authenticated) rather than
        # minting: a timer that manufactures installation tokens is a worse idea than one that
        # doesn't. `github=user` needs no probe — `gh auth token` failing is immediate and local.
        if mode.get("github", "off") != "app":
            return []

        def _check() -> tuple[bool, str]:
            from . import github_app_token  # lazy: pyjwt is a [host] extra, off the hot path

            try:
                return github_app_token.app_reachable(config.proxy_port())
            except Exception as e:  # a probe must never take the supervisor tick down
                return False, f"probe error ({type(e).__name__}: {e})"

        return [
            CapabilityProbe(
                axis="github",
                name="github-app-identity",
                check=_check,
                interval=300.0,  # a rotated key is rare; the call is cheap but not free
            )
        ]

    def doctor_fixes(self) -> Iterable[DoctorFix]:
        # Only the gh CLI install is non-interactive enough to one-click. `gh auth login` and the
        # host.env keys need a human (a browser OAuth / real secrets), so they stay as detail text.
        if not config.github_declared():
            return []
        return [DoctorFix(check="gh CLI", label="install gh", cmd=["brew", "install", "gh"])]
