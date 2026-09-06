"""gcp plugin — the ``gcp`` (identity) + ``storage`` (data-plane) axes (ADR-0015).

``gcp`` is IDENTITY ONLY — what GCP credential is reachable, never how the apps behave:

  off    no identity — zero secrets
  logs   dev box gets a read-only Cloud Logging token (the box log-reader SA)
  sa     + the app containers resolve the runtime SA via the metadata emulator (ADC)
  user   EMERGENCY: your own GCP identity inside the box; TTL-bound

``storage`` is the orthogonal data-plane swap (self-gated on the consumer configuring a
storage override):

  local     the stack's own emulators/containers (zero-secret default)
  staging   the app talks to REAL remote storage via ADC — requires ``gcp=sa``
            (a ``mode_issues`` error enforces it)

App *behaviour* toggles that merely CONSUME the identity (real-LLM modes, real-Auth0 via
Secret Manager) belong to their own axes/plugins (``llm``, ``auth0``) — that split is what
lets an always-offline e2e loop coexist with a credentialed browse stack.

The mechanism is the on-network GCE metadata emulator + a Mac-side allowlisted SA-token
minter (different from github's header injection — its own plugin, also the template for a
future AWS IMDS twin). Built-in for now; becomes the ``gcp-metadata`` package at extraction
time. Owns its axes, the gcp-minter daemon spec, the gcp recipe env, the SA-naming config,
the identity/storage compose overlays (paths from ``[plugins.gcp-metadata]``, never
hardcoded), and the gcp-side doctor checks (gcloud / ADC / PAM / [deep] SA impersonations).
Stdlib only.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib import request as urlrequest

from .. import config
from . import Axis, CapabilityProbe, DoctorContext, PanelData, Plugin, TuiPanel

# The host port the SA-token minter listens on comes from ``config.gcp_minter_port()`` —
# per-project band + per-worktree offset (the metadata emulator forwards to it).

# The metadata-emulator scripts (minter.py + server.py) live alongside this plugin — gcp-metadata
# capability owns both halves. Path is package-relative so a checkout/worktree move never breaks it.
# The Mac-side minter is run by path (below); the on-stack server is bind-mounted by the compose
# `metadata` profile from this same dir (see compose.podman.yml). Tests derive paths from here too.
METADATA_DIR = Path(__file__).resolve().parent / "gcp_metadata"

# The on-network metadata emulator's stable hostname:port. The box ALWAYS points here (it's
# mode-independent — the same string for every rung), so changing the gcp rung never re-bakes the
# box: only the Mac-side minter is reconciled live by `fy host`. When gcp=off the minter is down
# and the emulator returns "no token", so the box cleanly has no credentials.
METADATA_EMULATOR_HOST = "metadata-emulator:80"

# The emulator's compose SERVICE name (the consumer's compose file defines it, under the
# `metadata` profile — same baked-name contract as the profile). Declared as a posture service
# so the stack reconcile materializes it on a gcp rung even when the stack is down.
METADATA_EMULATOR_SERVICE = "metadata-emulator"

# The container label marking the dev box as the SOLE recipient of the emergency user token.
# App containers carry only `gcp.serviceAccount` (their own SA) and are NEVER escalatable; the
# minter grants the user token only when this label is present AND the live mode set
# GCP_ALLOW_USER_TOKEN=1 (gcp=user). Two independent conditions — a baked per-container opt-in and
# the live emergency mode — so no app can ever receive your identity, in any mode.
ESCALATE_LABEL = "gcp.userEscalatable"

# The marker the minter's liveness GET returns (minter.py `do_GET`). Duplicated as a literal there
# because that script is run BY PATH as a standalone process and never imports foldyard; the
# round-trip test asserts the two agree, so drift fails the suite rather than the probe.
_MINTER_MARKER = "gcp-minter"


def _minter_port_answering() -> tuple[bool, str]:
    """Is the minter port still served by OUR minter? Round-trip it and check the marker.

    "Listening" is not "ours", and the difference is invisible on every existing surface. VS Code's
    port forwarding bound 127.0.0.1:<minter port> on the host; for loopback traffic that specific
    bind beats the daemon's wildcard one, so the box's mints hung forever — TCP connecting, no HTTP
    ever returning — while `fy state` reported the daemon listening and the capability chain green.
    Neither was wrong; neither was the question.

    Probes 127.0.0.1 deliberately: that is the address the VM's gateway dials and the address a
    forwarder shadows. Probing the wildcard bind would pass while the box gets nothing.
    """
    port = config.gcp_minter_port()
    # No proxy, whatever the shell exports: HTTP_PROXY without a matching NO_PROXY would route even
    # loopback through it, and this probe exists to test the DIRECT path the VM gateway dials.
    opener = urlrequest.build_opener(urlrequest.ProxyHandler({}))
    try:
        with opener.open(f"http://127.0.0.1:{port}/", timeout=5) as r:
            body = json.loads(r.read() or b"{}")
    except Exception as e:  # unreachable, refused, or accepted-then-silent (the forwarder case)
        return False, (
            f"minter port {port} not answering ({type(e).__name__}) — the box gets no tokens; "
            "is `fy host` up?"
        )
    if not isinstance(body, dict) or body.get("foldyard") != _MINTER_MARKER:
        return False, (
            f"port {port} is held by another process, not the minter — a port forwarder "
            "(VS Code auto-forward?) shadows it; free the port, then restart `fy host`"
        )
    return True, f"minter answering on :{port}"


# What `expires_in` the minter reports to clients (and how long the emulator caches): a SHORT
# window so a mode change reconciled by `fy host` propagates to a running box within ~this many
# seconds (and gcp=off revokes access within it), instead of the box clinging to a ~1h token. The
# real impersonated token lives longer; clients just refetch early. Tunable via GCP_TOKEN_REFRESH.
TOKEN_REFRESH = int(os.environ.get("GCP_TOKEN_REFRESH", "60"))

# The per-request mint log the Mac minter writes and the "GCP Tokens" TUI panel tails. One
# filename constant so the daemon's GCP_MINTER_LOG_FILE and the panel can never drift (mirrors the
# proxy plugin's _PROXY_LOG / Network Log pairing).
_MINTER_LOG = "gcp-minter.jsonl"
_GCP_COLUMNS = ("time (utc)", "identity", "kind", "status", "note")
_GCP_ROWS = 200  # how many recent mint events the panel shows

_BLURB = {
    "off": "no GCP identity — zero secrets",
    "logs": "dev box: read-only Cloud Logging (devbox-log-reader SA)",
    "sa": "apps can reach GCP as the runtime SA (ADC via the metadata emulator) + dev-box logs",
    "user": "EMERGENCY: your own GCP identity in the box (+ everything sa grants)",
}

_STORAGE_BLURB = {
    "local": "storage on the stack's emulators (zero-secret default)",
    "staging": "app → REAL remote GCS/BigQuery via ADC (requires gcp=sa)",
}


# ── service-account naming (consumer config; foldyard.toml [plugins.gcp-metadata]) ──────


def gcp_project() -> str:
    """GCP project the minter's SAs live in (gcp modes need it set in foldyard.toml)."""
    return config.gcp_project()


def _sa_email(role: str, default_label: str) -> str:
    label = config.gcp_sa_labels().get(role, default_label)
    return f"{label}@{gcp_project()}.iam.gserviceaccount.com"


def devbox_log_sa() -> str:
    """Read-only Cloud Logging SA the dev box impersonates (gcp=logs/sa/user)."""
    return os.environ.get("DEVBOX_LOG_SA") or _sa_email("box", "devbox-log-reader")


def app_sa() -> str:
    """App runtime SA the apps reach in gcp=sa (real staging)."""
    return os.environ.get("APP_SERVICE_ACCOUNT") or _sa_email("app", "app-runtime")


def data_sas() -> set[str]:
    """Per-service runtime SAs for the DATA services under gcp=sa — every sa_labels role
    beyond the app/box pair (queue-worker, graph-api, asset-tape-extract). These are what
    compose.identity-data.yml labels the data containers with; the minter must allow them so
    each resolves its OWN identity rather than sharing the app's."""
    return {_sa_email(role, role) for role in config.gcp_sa_labels() if role not in ("app", "box")}


# ── PAM-grant timing (self-contained so the plugin never imports devmode) ───────────────


def _now() -> datetime:
    return datetime.now(UTC)


def _parse(iso: str) -> datetime | None:
    try:
        return datetime.fromisoformat(iso)
    except (ValueError, TypeError):
        return None


def _human_left(seconds: int) -> str:
    """Coarse 'time remaining' for long windows (PAM grants run up to 12h)."""
    if seconds <= 0:
        return "EXPIRED"
    h, m = seconds // 3600, (seconds % 3600) // 60
    return f"{h}h{m:02d}m" if h else f"{m}m{seconds % 60:02d}s"


def _pam_left(out: str) -> str | None:
    """`createTime requestedDuration` (e.g. '2026-06-12T03:00:00Z 43200s') → remaining."""
    line = next((ln for ln in out.splitlines() if ln.strip()), "")
    parts = line.split()
    if len(parts) < 2:
        return None
    created = _parse(parts[0].replace("Z", "+00:00"))
    try:
        secs = int(parts[1].rstrip("s"))
    except ValueError:
        return None
    if created is None:
        return None
    return _human_left(int((created + timedelta(seconds=secs) - _now()).total_seconds()))


def _pam_summary(out: str, account: str | None = None) -> str | None:
    """`gcloud pam grants list` value rows → a one-line summary of YOUR live grant.

    A grant counts only if its state is exactly ACTIVE, it was requested by ``account``
    (the entitlement lists everyone's grants — a teammate's must not read as ours), and
    its window hasn't elapsed. Returns None otherwise (the impersonation probes are the
    real capability test)."""
    for line in out.splitlines():
        fields = line.split()
        if len(fields) < 4 or fields[0].upper() != "ACTIVE":
            continue
        requester = fields[1]
        if account and requester.lower() != account.strip().lower():
            continue
        left = _pam_left(" ".join(fields[2:4]))
        if left is None:
            return f"active grant ({requester})"
        if left != "EXPIRED":
            return f"active grant ({requester}) — expires in {left}"
    return None


# ── the "GCP Tokens" TUI panel (the minter's mint log) ──────────────────────────────────
# The GCP credential path doesn't ride the mitmproxy egress proxy (that's github's Network Log) —
# tokens are minted Mac-side by the minter and served by the on-network metadata emulator. So gcp
# gets its OWN panel: the minter's per-request log, showing WHICH identity was minted, granted or
# refused. The token itself is never logged, so this panel never carries a credential.


def _minter_log() -> Path:
    return config.log_dir() / _MINTER_LOG


def _launch_minter_path() -> Path:
    """The STABLE per-project path the minter daemon launches from (the supervisor's ``stage``
    snapshots the packaged ``minter.py`` here just before launch, mirroring the proxy addon). Two
    reasons over launching the packaged copy directly: a `uv tool install --force` can rewrite the
    package under a live process, and — load-bearing for orphan reaping — the state_dir path makes
    the daemon's command line PROJECT-SCOPED, so the supervisor's ``_is_our_daemon`` can tell this
    project's orphaned minter (safe to reap) from a sibling project's live one (never touch). The
    packaged path can't do that: every project's minter would share it."""
    return config.state_dir() / "gcp_minter.py"


def _tail_minter_log(limit: int = _GCP_ROWS) -> list[dict]:
    """The last ``limit`` JSON lines across the rotated + live mint log (oldest→newest). Tails only
    the END of the log (see ``config.tail_jsonl``) so the panel's per-second refresh is bounded."""
    return config.tail_jsonl(_minter_log(), limit)


def _gcp_panel_data() -> PanelData:
    """The GCP Tokens tab's data: one row per mint request (newest first) + a summary counting
    granted tokens. Called by the TUI on a timer, so the rich import is lazy here — the plugin
    module stays stdlib-only on the registry hot path."""
    from rich.markup import escape  # lazy: only when the TUI paints this panel

    log = _minter_log()
    entries = _tail_minter_log()
    rows: list[tuple[str, ...]] = []
    granted = 0
    for e in reversed(entries):
        status = e.get("status", 0)
        ok = status == 200
        granted += ok
        colour = "green" if ok else "red"
        kind = "user" if e.get("user") else "SA"
        rows.append(
            (
                e.get("ts", "")[11:19],
                escape((e.get("sa") or "—")[:46]),
                kind,
                f"[{colour}]{status}[/{colour}]",
                escape(str(e.get("error", "")))[:50],
            )
        )
    summary = (
        f"{len(entries)} token mints · {granted} granted · [dim]{log}[/dim]"
        if entries
        else f"no token mints yet — appears once `fy host` runs a gcp mode · [dim]{log}[/dim]"
    )
    return PanelData(summary=summary, rows=rows)


class GcpPlugin(Plugin):
    name = "gcp"

    def axes(self) -> list[Axis]:
        # Self-gate on the gcp config (registry plan Step D): even though the plugin only LOADS
        # when [plugins.gcp-metadata] is declared (Step C), the axis appears only once a real GCP
        # project is configured — without it the rungs (`devbox-log-reader`, `app-runtime` SAs)
        # would name a malformed `…@.iam…` identity. So a declared-but-unconfigured gcp table
        # contributes no axis (mirrors claude.py's keyless gate).
        if not gcp_project():
            return []
        axes = [
            Axis(
                name="gcp",
                # IDENTITY ONLY (what credential is reachable). App behaviour that consumes it
                # (real LLM, real-Auth0-from-GSM, real storage) lives on its own axes.
                rungs=("off", "logs", "sa", "user"),
                blurb=_BLURB,
                daemon="gcp-minter",
                emergency=("user",),
            )
        ]
        # The storage axis additionally self-gates on the consumer wiring an overlay onto it — an
        # `[[overlay]] when = { storage = "staging" }` in foldyard.toml (config.overlay_when_axes).
        # That's the opt-in; without one, `staging` would be a rung that changes nothing.
        if "storage" in config.overlay_when_axes():
            axes.append(
                Axis(
                    name="storage",
                    rungs=("local", "staging"),
                    blurb=_STORAGE_BLURB,
                    # No `requires` here: staging→gcp=sa is a consequence of the CONSUMER's
                    # storage overlay (the swapped endpoints resolve via ADC), so it's declared
                    # beside that overlay — `[[require]] axis = "storage" … needs = "gcp"` in
                    # foldyard.toml — like the overlay itself, which is what summons this axis.
                )
            )
        return axes

    def daemons(self, mode: dict) -> dict[str, dict]:
        rung = mode.get("gcp", "off")
        if rung == "off":
            return {}
        # One minter PER WORKTREE (ADR-0016 routing): the daemon name carries the
        # worktree suffix and listens on the worktree's offset port. Each worktree's metadata-
        # emulator forwards to ITS port (GCP_MINTER_URL, derive_env), so its gcp rung is enforced by
        # whether THIS minter is up — a sibling worktree on gcp=off simply has no minter on its port
        # and gets no token (no cross-posture leak, the gap a single shared minter would open).
        port = config.gcp_minter_port()
        allow = {devbox_log_sa()}
        if rung in ("sa", "user"):  # the app + data-service containers resolve their runtime SAs
            # user is a TRUE ladder rung — a superset of sa, adding YOUR token for the
            # escalatable box ON TOP of the sa grants. It used to drop the app/data SAs
            # instead, which silently broke every sa-consuming rung (llm=live, storage=staging)
            # the moment you escalated — "going higher" must never grant the stack less.
            allow.add(app_sa())
            allow |= data_sas()
        env = {
            "GCP_SA_ALLOWLIST": ",".join(sorted(allow)),
            "LISTEN_PORT": str(port),
            "GCP_TOKEN_REFRESH": str(
                TOKEN_REFRESH
            ),  # short expires_in ⇒ live mode-change propagation
            # Per-request mint log → the "GCP Tokens" TUI panel tails it (token is never logged).
            "GCP_MINTER_LOG_FILE": str(_minter_log()),
        }
        if rung == "user":
            env["GCP_ALLOW_USER_TOKEN"] = "1"
        return {
            f"gcp-minter{config.worktree_suffix()}": {
                "label": "GCP SA-token minter" + (" (+ YOUR user token)" if rung == "user" else ""),
                "port": port,
                # Launch the staged snapshot, not the packaged file — see _launch_minter_path.
                "stage": [(str(METADATA_DIR / "minter.py"), str(_launch_minter_path()))],
                "cmd": [sys.executable, str(_launch_minter_path())],
                "env": env,
                "requires": [],
            }
        }

    def posture_services(self, mode: dict) -> dict[str, bool]:
        # The emulator is the in-VM half of EVERY gcp rung — the Mac minter alone grants nothing,
        # so a rung declared while the stack is down (fresh worktree, devbox-only session) read
        # as granted while the box couldn't mint a token (2026-08-07: a gcp=user prod
        # investigation burned a session on exactly this). Same config gate as stage_assets:
        # no gcp-metadata project configured, no service claimed.
        if not gcp_project():
            return {}
        return {METADATA_EMULATOR_SERVICE: mode.get("gcp", "off") != "off"}

    def stage_assets(self, mode: dict, checkout: str, here: str) -> None:
        # The metadata-emulator container bind-mounts server.py, but the machine mounts ONLY the
        # repo — and server.py ships inside the foldyard package (off the mount, esp. once foldyard
        # isn't vendored). So stage the packaged copy into the VM-visible, gitignored
        # `.devbox-foldyard/` dir (the box wheel-staging dir) for compose to mount. Stage whenever
        # the metadata profile will actually run: a gcp rung is active OR it's been forced on via an
        # explicit `COMPOSE_PROFILES=metadata` (the documented manual path — README/justfile) — else
        # that path starts the emulator with no server.py staged. Either way only when CONFIGURED
        # (`[plugins.gcp-metadata].project`, the gate box_args uses too), so we never stage (or
        # mount) a server.py for a consumer that runs no emulator.
        forced = "metadata" in os.environ.get("COMPOSE_PROFILES", "").split(",")
        if (mode.get("gcp", "off") == "off" and not forced) or not gcp_project():
            return
        src = METADATA_DIR / "server.py"
        dest = Path(checkout) / here / ".devbox-foldyard" / "server.py"
        if not dest.exists() or dest.read_bytes() != src.read_bytes():  # idempotent
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(src, dest)

    def derive_env(self, mode: dict) -> dict[str, str]:
        # Stack-side (not box) env: which compose profiles `fy up` runs. The metadata emulator
        # runs whenever a gcp rung is active; the box wiring is mode-INDEPENDENT (see box_args), so
        # switching rungs never re-bakes the box — only the Mac minter is reconciled live, and
        # crossing off↔non-off just toggles the emulator via `fy up` (which leaves the box alone).
        rung = mode.get("gcp", "off")
        if rung == "off":
            return {}
        env = {
            "GCP_METADATA_HOST": METADATA_EMULATOR_HOST,
            "COMPOSE_PROFILES": "metadata",
            # Point THIS worktree's metadata-emulator at THIS worktree's minter port (the project
            # band's minter base + worktree offset — config.gcp_minter_port). A compose that reads
            # ${GCP_MINTER_URL} routes per-worktree. This is what makes one worktree's gcp=sa not
            # leak tokens to a sibling on gcp=off — each emulator hits its OWN minter. The address
            # is backend-dependent (config.host_alias — lima needs the host gateway IP); under the
            # wall, that IP is in NO_PROXY (machine-wall.sh) so the container reaches it DIRECT, not
            # via the mitmdump proxy (which can't reach the Lima gateway from the Mac).
            "GCP_MINTER_URL": f"http://{config.host_alias()}:{config.gcp_minter_port()}",
        }
        if mode.get("storage") == "staging":
            # Staging Cloud SQL via the proxy (browse on 127.0.0.1:5434) — a data-plane concern,
            # so it rides the storage axis (it needs the identity anyway; the storage axis's
            # `requires` row enforces gcp=sa whenever storage=staging).
            env["COMPOSE_PROFILES"] = "metadata,cloudsql"
        return env

    # The gcp/storage compose overlays (identity, identity-data, storage-staging) are declared as
    # `[[overlay]]` entries in foldyard.toml now (config-only, matched on their `when`) — see
    # docs/compose-overlays.md. This plugin keeps only the identity/minter behaviour; the storage
    # rung's identity requirement is declared on its Axis (`requires`), and it no longer
    # hardcodes any overlay path.

    def no_proxy_hosts(self) -> list[str]:
        # The emulator lives on the STACK network, so a token fetch routed through the Mac-side
        # egress proxy 502s (the proxy can't resolve a stack hostname). Config-gated, not
        # mode-gated, for the reason in the hook's docstring — the box bakes NO_PROXY once.
        if not gcp_project():
            return []
        return [METADATA_EMULATOR_HOST.split(":", 1)[0]]

    def box_args(self, env: dict) -> list[str]:
        # Gate on the plugin being CONFIGURED (`[plugins.gcp-metadata].project`). Without it a
        # generic consumer would get a malformed `…@.iam.gserviceaccount.com` label + a
        # GCE_METADATA_HOST for an emulator its stack doesn't run (see `config.gcp_project`).
        if not gcp_project():
            return []
        # MODE-INDEPENDENT box wiring (baked once, never re-baked for a gcp change): always point at
        # the emulator and always carry both the dev box's read-only SA label and the escalatable
        # marker. The rung is then served entirely by the live Mac minter — gcp=off ⇒ minter down ⇒
        # emulator returns no token; logs/sa ⇒ impersonate the SA; user ⇒ the minter swaps in YOUR
        # token for the escalatable box (apps, lacking the marker, only ever get their own SA).
        return [
            "--label",
            f"gcp.serviceAccount={devbox_log_sa()}",
            "--label",
            f"{ESCALATE_LABEL}=1",
            # GCE_METADATA_HOST is the google-auth libraries' var (the apps use it); the gcloud CLI
            # reads the legacy GCE_METADATA_ROOT (default metadata.google.internal) for its own GCE
            # detection + reads. The box runs gcloud (read-logs skill), so it needs BOTH pointed at
            # the emulator — else gcloud probes metadata.google.internal, fails DNS, and reports
            # "no active account" even though the token path works for the libraries.
            # Python google-auth adds a THIRD: its ADC-detection ping resolves via GCE_METADATA_IP
            # (not HOST; default 169.254.169.254), so python clients in the box need it too or
            # google.auth.default() reports no-credentials despite the emulator serving tokens.
            "-e",
            f"GCE_METADATA_HOST={METADATA_EMULATOR_HOST}",
            "-e",
            f"GCE_METADATA_ROOT={METADATA_EMULATOR_HOST}",
            "-e",
            f"GCE_METADATA_IP={METADATA_EMULATOR_HOST}",
        ]

    def capability_probes(self, mode: dict) -> list[CapabilityProbe]:
        # The continuous version of doctor's deep impersonation check: `gcp=sa` (and logs/user)
        # promises a capability chain — operator ADC → PAM grant → tokenCreator on the SA — every
        # link of which can lapse while the posture dashboard shows green (the gcp-elevate-lapse
        # incident: minting fails silently per-request, services boot with blank secrets). Probe
        # the chain end-to-end with a real dry-run impersonation of the identity the ACTIVE rung
        # promises, so a lapse surfaces as a DEGRADED axis with the fix, within ~interval.
        rung = mode.get("gcp", "off")
        if rung == "off" or not gcp_project():
            return []

        def _mint_check(sa: str | None):
            def _check() -> tuple[bool, str]:
                args = ["gcloud", "auth", "print-access-token"]
                who = "your gcloud identity"
                if sa:
                    args.append(f"--impersonate-service-account={sa}")
                    who = f"impersonation of {sa.split('@')[0]}"
                # Through the minter's OWN runner, not a look-alike: stdin CLOSED (a probe that
                # keeps the supervisor's terminal can satisfy a reauth prompt the daemon never
                # can, then report `mints ok` while every real mint hangs — the green-host/
                # dead-box split this probe exists to catch), its own process group, and a bound
                # that holds when a gcloud helper keeps the pipes open — otherwise the supervisor
                # tick stalls past HEARTBEAT_STALE_SECONDS instead of reporting a timed-out probe.
                # Same code path, or it is not the same check. Lazy import: the minter module is
                # stdlib-only but is the daemon's script, not registry-load material.
                from .gcp_metadata import minter

                try:
                    minter._run_noninteractive(args, timeout=20)  # the token is never surfaced
                except FileNotFoundError:
                    return False, "gcloud not installed"
                except subprocess.TimeoutExpired:
                    return False, f"{who} probe timed out"
                except subprocess.CalledProcessError as failed:
                    text = failed.stderr or failed.stdout or ""
                    lines = [ln for ln in text.splitlines() if ln.strip()]
                    err = " ".join(ln for ln in lines if not ln.startswith("WARNING")).strip()
                    hint = (
                        " — PAM grant lapsed? just gcp-elevate" if sa else " — gcloud auth login?"
                    )
                    return False, f"{who} failing: {err[:120]}{hint}"
                return True, f"{who} mints ok"

            return _check

        # Probe the identity chain(s) each rung PROMISES: logs = the box SA; sa = the runtime
        # SA (the chain `just gcp-elevate` backs); user = your own token AND the runtime SA
        # (user ⊇ sa — the axis merge shows the weakest link, so a lapsed PAM under user still
        # reads DEGRADED even while your own token works).
        if rung == "logs":
            targets: list[tuple[str, str | None]] = [("gcp-mint", devbox_log_sa())]
        elif rung == "sa":
            targets = [("gcp-mint", app_sa())]
        else:  # user
            targets = [("gcp-user-token", None), ("gcp-mint", app_sa())]
        # The port round-trip comes FIRST and runs on every rung that has a minter: the mint checks
        # above exercise the credential chain host-side, so they stay green when the chain is fine
        # and the box still can't reach it. Cheaper than a mint, so a shorter interval.
        return [
            CapabilityProbe(
                axis="gcp", name="gcp-minter-port", check=_minter_port_answering, interval=60.0
            ),
            *(
                CapabilityProbe(axis="gcp", name=name, check=_mint_check(sa), interval=120.0)
                for name, sa in targets
            ),
        ]

    def tui_panels(self) -> list[TuiPanel]:
        # The GCP Tokens log: the minter's per-request mint events (which identity, granted/denied).
        # Separate from github's Network Log because GCP tokens don't ride the egress proxy — they
        # are minted Mac-side, served by the metadata emulator. off ⇒ "no token mints yet".
        return [
            TuiPanel(id="gcp", title="GCP Tokens", columns=_GCP_COLUMNS, refresh=_gcp_panel_data)
        ]

    def doctor_checks(self, ctx: DoctorContext):
        yield ctx.result(
            ctx.which("gcloud"),
            "gcloud CLI",
            "installed",
            "missing — brew install google-cloud-sdk",
        )
        account = ""  # the active gcloud identity — gates the PAM + impersonation checks
        if ctx.which("gcloud"):
            yield ("running", "gcloud account", "")
            rc, acct = ctx.run(["gcloud", "config", "get-value", "account"], timeout=8)
            has_account = rc == 0 and bool(acct) and "unset" not in acct
            account = acct if has_account else ""
            yield ctx.result(
                has_account, "gcloud account", acct, "no active account — gcloud auth login"
            )
            adc = Path.home() / ".config/gcloud/application_default_credentials.json"
            yield ctx.result(
                adc.exists(),
                "ADC (application-default)",
                str(adc),
                "missing — gcloud auth application-default login "
                "(needed by the minters; NOT an SA key)",
            )

        # PAM elevation (the 12h grant `just gcp-elevate` creates). Runs whenever we have a
        # gcloud identity — incl. the fast pass — since it's a single read and gates the
        # deep impersonations. Filtered to YOUR account so others' grants never surface.
        if account:
            entitlement = os.environ.get(
                "PAM_ENTITLEMENT",
                f"projects/{gcp_project()}/locations/global/entitlements/pam-entitlement",
            )
            yield ("running", "PAM elevation", "")
            rc, out = ctx.run(
                [
                    "gcloud",
                    "beta",
                    "pam",
                    "grants",
                    "list",
                    f"--entitlement={entitlement}",
                    f"--filter=requester:{account}",
                    "--format=value(state,requester,createTime,requestedDuration)",
                ],
                timeout=25,
            )
            summary = _pam_summary(out, account) if rc == 0 else None
            if summary:
                yield ctx.result(True, "PAM elevation", summary, "")
            else:
                yield ctx.result(
                    None,
                    "PAM elevation",
                    "",
                    "no active grant of yours — `just gcp-elevate` if "
                    "the SA impersonations below fail"
                    + (f" [{out[:80]}]" if rc != 0 and out else ""),
                )

        if ctx.deep and account:
            # Prove the IAM story end-to-end, not just the local files.
            yield ("running", "impersonate devbox-log-reader", "")
            rc, out = ctx.run(
                [
                    "gcloud",
                    "auth",
                    "print-access-token",
                    f"--impersonate-service-account={devbox_log_sa()}",
                ],
                timeout=20,
            )
            yield ctx.result(
                rc == 0,
                "impersonate devbox-log-reader",
                "tokenCreator works (gcp=logs ready)",
                f"failed — devbox:logReaderImpersonators deployed? ({out[:120]})",
            )
            yield ("running", "impersonate runtime SA", "")
            rc, out = ctx.run(
                [
                    "gcloud",
                    "auth",
                    "print-access-token",
                    f"--impersonate-service-account={app_sa()}",
                ],
                timeout=20,
            )
            yield ctx.result(
                rc == 0,
                "impersonate runtime SA",
                "tokenCreator works (gcp=sa ready)",
                f"failed — PAM elevation lapsed? just gcp-elevate ({out[:120]})",
            )
