"""auth0-sim plugin — the ``auth0`` axis: which issuer the browsable app trusts.

  sim   default — browsable app points at the in-repo Auth0 SIMULATOR seeded from the
        imported dump DB: log in as a real dump user, fully offline, zero secrets. This is
        the zero-secret resting posture (matches the repo's "offline by default"), and it's
        what the always-on data services notify — their sim-mode callback issuer alignment
        rides this overlay. Prereq: a dump imported (``just db-import``) so the sim has users.
  real  browsable app points at real staging Auth0. Under ``gcp=sa`` the ``real_override``
        overlay un-pins the placeholder client id/secret so the app's ``loadSecrets()``
        fetches the real ones from Secret Manager via ADC at runtime; without an identity the
        placeholders stand and login can't complete (a posture, not an error).

Unlike gcp/github this axis grants NO credential and runs NO daemon: it's a pure
app-posture swap. ``auth0=sim`` makes ``fy up`` layer the simulator compose override onto the
chain (real-Auth0 endpoints → the simulator on ``https://localhost:<SIM_PORT>``). The sim is
published on ``[ports].SIM_PORT`` (offset per worktree, like every other port — so two worktrees
can dump-browse concurrently); the compose override templates ``AUTH0_DOMAIN`` + the app's
loopback off that port so the Host-derived issuer stays consistent. The simulator container
self-seeds its login allow-list from the DB at startup.

PROJECT-AGNOSTIC: every consumer-specific value (the simulator harness dir, the compose
overrides, the container suffix) is read from ``[plugins.auth0-sim]`` in ``foldyard.toml`` via
``config``; the published port is ``[ports].SIM_PORT`` — nothing consumer-specific is baked in here.
Stdlib only (loads on the recipe hot path). Assumes the dump is already imported.
(Spinout note: this plugin is Tangible-bound — it migrates into the consumer repo via
``[plugins].load`` in spinout Phase 2, decisions D3/D4.)
"""

from __future__ import annotations

import shlex
from collections.abc import Iterable
from pathlib import Path

from .. import config
from . import Axis, DoctorContext, DoctorFix, Plugin

_BLURB = {
    "sim": "browse a dump: app → Auth0 simulator seeded from the dump DB (offline default)",
    "real": "app → real staging Auth0 (creds from GSM under gcp=sa; placeholders otherwise)",
}


class Auth0SimPlugin(Plugin):
    name = "auth0-sim"

    def axes(self) -> list[Axis]:
        # Self-gate on the consumer declaring [plugins.auth0-sim] (registry plan Step D): the auth0
        # axis is meaningless without a simulator harness to point the app at, so a generic repo
        # never gets an `auth0` axis it can't back. (The plugin already only LOADS when the table is
        # declared — Step C — so this is belt-and-suspenders for a bare/extra-injected plugin.)
        if not config.auth0_sim_declared():
            return []
        # `sim` first ⇒ rung 0 ⇒ the zero-secret resting default (fully offline; matches the
        # repo's "offline by default" posture, and the always-on data services' callbacks expect
        # it). `real` reaches real staging Auth0 and needs an identity to fully log in, so it's the
        # opt-in rung. (Was the `dump` axis off|on — renamed so the axis names what it selects.)
        return [Axis(name="auth0", rungs=("sim", "real"), blurb=_BLURB)]

    # The auth0 compose overlays are declared as `[[overlay]]` entries in foldyard.toml now
    # (config-only, matched on their `when`) — see docs/compose-overlays.md:
    #   auth0=sim              → compose.auth0-sim.yml (sim endpoints + dump seeding; the published
    #                            host port is [ports].SIM_PORT, per-worktree, so the overlay
    #                            templates AUTH0_DOMAIN off it — never pinned here);
    #   auth0=real AND gcp=sa  → compose.auth0-real-gsm.yml (app: un-pin the placeholders so
    #                            loadSecrets fetches real creds from GSM via ADC) + its data-plane
    #                            twin compose.auth0-real-data.yml (re-point the data services at
    #                            real Auth0). The gcp=sa condition is why it's a posture, not a
    #                            mode_issues nag: without an identity the placeholders just stand.
    # This plugin keeps only the axis, the sim-cert doctor, and the sim-container bounce (stack.up).

    def mode_issues(self, mode: dict) -> Iterable[tuple[str, str]]:
        # Dump-browsing (sim login, local dump DB) while the app's storage points at the REAL
        # remote data plane mixes fixture data with live buckets — legal (a deliberate hybrid)
        # but rarely what you meant, so warn rather than refuse. Deliberately a HOOK, not an
        # Axis.requires row: this is a combination WARNING whose absence semantics are the
        # OPPOSITE of a requirement — with no storage axis loaded there is nothing to warn
        # about, whereas a requires row treats an absent axis as unmet and would fire.
        if mode.get("auth0") == "sim" and mode.get("storage") == "staging":
            yield (
                "warn",
                "auth0=sim (dump browsing) with storage=staging mixes the local dump DB with the "
                "REAL remote storage data plane — writes go to staging buckets",
            )

    def _cert_dir(self) -> Path:
        # The gitignored dir (mounted read-only into the sim) where the developer's mkcert-signed
        # localhost cert lives; the sim installs it at startup. Empty sim_dir ⇒ no cert checks.
        return config.repo_root() / config.auth0_sim_dir() / ".certs-local"

    def doctor_checks(self, ctx: DoctorContext) -> Iterable[tuple[str, str, str]]:
        # No simulator harness configured ⇒ the consumer doesn't use the dump axis; skip the
        # cert checks entirely (they'd point at a meaningless <repo>/.certs-local).
        if not config.auth0_sim_dir():
            return
        # Dump-browse serves the Auth0 sim over HTTPS on https://localhost:<sim_port>; without a
        # locally-trusted cert the Mac browser shows net::ERR_CERT_AUTHORITY_INVALID. mkcert signs
        # the cert (its nss-provided certutil installs the CA into the Firefox/Chrome trust stores).
        # All WARN, not fail: only needed for auth0=sim, and the cert warning is click-through-able.
        # Mac-only — doctor's in_box() branch returns before plugin checks run. The TUI fix
        # buttons (or the consumer's own cert recipe) repair these.
        yield ctx.result(
            ctx.which("mkcert") or None,
            "mkcert",
            "installed (trusted local HTTPS for the dump-browse Auth0 sim)",
            "missing — dump-browse shows a cert warning. `brew install mkcert nss` (or the fix)",
        )
        yield ctx.result(
            ctx.which("certutil") or None,
            "mkcert nss",
            "installed (browser trust-store support for mkcert)",
            "missing — Firefox/Chrome won't trust mkcert's CA. `brew install nss` (or the fix)",
        )
        cert = self._cert_dir() / "localhost.pem"
        yield ctx.result(
            cert.exists() or None,
            "sim cert",
            "generated in .certs-local (the sim installs it at startup)",
            "not generated — the sim falls back to a self-signed cert the browser rejects. Use "
            "the fix (or this project's own cert recipe)",
        )

    def doctor_fixes(self) -> Iterable[DoctorFix]:
        # NON-INTERACTIVE repairs (TUI worker, no TTY): `brew install` needs no sudo, and cert
        # generation only writes the gitignored .certs-local/. The CA-TRUST step (`mkcert -install`)
        # needs sudo, so it is NOT a button — run it in a terminal yourself (one-time), or via the
        # consumer's own cert recipe if it has one. The
        # generate fix assumes mkcert is installed + its CA trusted (click "install mkcert" first if
        # not).
        if not config.auth0_sim_dir():
            return []
        certs = self._cert_dir()
        gen_cert = (
            f"mkdir -p {shlex.quote(str(certs))} && "
            f"mkcert -cert-file {shlex.quote(str(certs / 'localhost.pem'))} "
            f"-key-file {shlex.quote(str(certs / 'localhost-key.pem'))} localhost 127.0.0.1"
        )
        # Bounce the sim so it actually SERVES the new cert: it installs the cert only at (re)start.
        # `foldyard up` now restarts the sim itself when auth0=sim (so a later `up` would pick the
        # cert up too — see stack.up()), but this fix applies it immediately without a full up.
        # Worktree-aware via shellenv; best-effort + no-op when down.
        bounce = (
            '( eval "$(foldyard shellenv)"; '
            f'"$ENGINE" restart "${{PODMAN_PROJECT}}-{config.auth0_sim_container()}" ) '
            ">/dev/null 2>&1 || true"
        )
        return [
            DoctorFix(
                check="mkcert", label="install mkcert+nss", cmd=["brew", "install", "mkcert", "nss"]
            ),
            DoctorFix(check="mkcert nss", label="install nss", cmd=["brew", "install", "nss"]),
            DoctorFix(
                check="sim cert",
                label="generate cert",
                cmd=["bash", "-lc", f"{gen_cert} && {bounce}"],
            ),
        ]
