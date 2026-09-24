"""proxy plugin — the egress mitm proxy FRAMEWORK (ADR-0007).

The shared, injector-agnostic substrate that header-rewrite plugins (github-app, header-auth,
…) ride on. It owns the parts that are about the PROXY, not any one credential mechanism:

  - the single ``mitmdump`` daemon (lifecycle), built from the injection rules every plugin
    contributes via ``registry().proxy_rules(mode)`` — one proxy, however many injectors,
  - the proxy CA mount + the box's proxy env (``HTTPS_PROXY``/``NODE_EXTRA_CA_CERTS``/… +
    ``NO_PROXY`` for in-stack services), bolted on at box-up via ``box_args``,
  - ``FY_PROXY`` (route box egress through the proxy) — derived whenever ANY injector is active,
  - the per-request JSONL egress log + its **Network Log** TUI panel (shows all proxied
    traffic across all injectors; the "injected" flag is set by whichever rule injected).

An injector contributes only its ``InjectRule`` (+ axis/minter/doctor/verify); it never owns a
proxy daemon, CA, log, or panel. Built-in for now; the mitmdump *script* (``egress_proxy.py``)
now ships IN the package (``assets/proxy/``) and is snapshot to a stable launch path per run
(see ``_launch_addon_path``), so a consumer needn't vendor it and a checkout can't hot-reload it.
Stdlib only on the registry hot path (rich is imported lazily, only when the TUI paints).
"""

from __future__ import annotations

import os
import shlex
import shutil
import sys
from collections.abc import Iterable
from pathlib import Path

from .. import config
from . import DoctorContext, DoctorFix, PanelGroup, PanelTree, Plugin, TuiPanel
from ._passthrough_bundles import BUNDLES

# The host ports the mitmdump injection proxy listens on come from ``config.proxy_port()`` —
# per-project band + per-worktree offset; there is deliberately no module-level constant (it
# would freeze the env at import and bypass the band allocation).

# The proxy-URL user an image BUILD reaches the proxy as. A build container has no proxy CA, so a
# decrypted host fails its TLS verify; the addon blind-tunnels a CONNECT that carries this user
# (the client turns the URL's userinfo into a Basic Proxy-Authorization header) — still walled at
# CONNECT. It is a marker, not a credential: the box can present it too, and gains only an
# undecrypted tunnel to a host the wall already lets it reach. Duplicated in the addon (which
# can't import foldyard); a test pins the two equal.
BUILD_TUNNEL_USER = "fy-build"


def build_proxy_url(token: str | None = None) -> str | None:
    """The proxy URL an image build should use, or ``None`` when builds don't route through the
    proxy (no in-VM wall ⇒ they egress directly, as they always have). The MAIN proxy port, like
    the wall's own VM-level proxy env: building isn't per-worktree. ``token`` — the build's secret
    from the build gate — goes in the password; it is what unlocks build-scoped grants. Without
    it the build still tunnels, on runtime grants only."""
    if not config.machine_wall():
        return None
    user = BUILD_TUNNEL_USER
    password = token or user
    return f"http://{user}:{password}@{config.LIMA_HOST_GATEWAY}:{config.proxy_port_base()}"


def mitmdump_path() -> str | None:
    """Resolve the ``mitmdump`` executable, or ``None`` if it isn't installed.

    Prefer the copy BESIDE foldyard's own interpreter: the HOST install (`just install` →
    ``uv tool install …[host]``) puts mitmproxy in foldyard's venv, but ``uv tool install`` does NOT
    link a *dependency's* console scripts onto PATH — so for a tool install the venv copy is the
    only one that exists. Fall back to PATH (a standalone ``uv tool install mitmproxy`` / system).
    Returns ``None`` in the BOX, which installs foldyard bare (no ``[host]`` extra) since it never
    runs the proxy. Single source of truth for the daemon command (below) and the proxy doctor
    check, so they can't drift."""
    local = Path(sys.executable).parent / "mitmdump"
    if local.exists():
        return str(local)
    return shutil.which("mitmdump")


def _mitm_ca() -> Path:
    """The proxy CA path — ``$MITMPROXY_CA`` or mitmproxy's default. One helper so the doctor
    check, the 'generate CA' fix, and box_args (above) all point at the same file."""
    return Path(os.environ.get("MITMPROXY_CA", Path.home() / ".mitmproxy/mitmproxy-ca-cert.pem"))


def stage_ca_for_box(checkout: str, here: str) -> Path | None:
    """Stage the (public) proxy CA under the checkout so podman in the VM can bind-mount it.

    The machine virtiofs-mounts ONLY the repo + worktrees root, so a CA under ``~/.mitmproxy`` is
    invisible to podman inside the VM (``box_args``' ``-v`` would die with ``statfs … : no such
    file or directory``). Copy the public cert into a gitignored dir under the checkout (which IS
    mounted) and re-point ``MITMPROXY_CA`` there, so ``box_args`` mounts the VM-visible copy.
    AMBIENT: stage whenever a CA exists, independent of routing. Returns the staged path, or
    ``None`` when no CA exists yet (nothing to stage — box_args then emits no CA args)."""
    src = _mitm_ca()
    if not src.exists():
        return None
    staged = Path(checkout) / here / ".devbox-ca" / "mitmproxy-ca-cert.pem"
    if src.resolve() != staged.resolve():  # idempotent: skip a redundant self-copy on re-run
        staged.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, staged)
    os.environ["MITMPROXY_CA"] = str(staged)
    return staged


def _foldyard_src() -> Path | None:
    """The editable foldyard source dir (the one carrying pyproject.toml), so the doctor's reinstall
    fix can reinstall foldyard from source. None when foldyard isn't an editable from-source install
    — then there's no local dir to hand uv (the fix falls back to the bare name)."""
    # …/foldyard/src/foldyard/plugins/proxy.py → parents[3] = the project root …/foldyard
    root = Path(__file__).resolve().parents[3]
    return root if (root / "pyproject.toml").exists() else None


def host_install_cmd() -> list[str]:
    """Reinstall foldyard WITH the ``[host]`` extra (mitmproxy → foldyard's own venv): the ONE
    command every "mitmproxy is missing" surface names — the doctor row, preflight, the PyJWT
    error — and the TUI's fix button runs, so the advice and the button can never disagree.
    Editable from the source dir for a from-source install (a bare ``uv tool install
    foldyard[host]`` on top of an editable would silently swap it for PyPI's); the bare name is
    the fallback. Not ``uv tool upgrade``: that re-resolves the receipt as it stands, so it can
    never ADD an extra. Host-side only — the box never runs the proxy."""
    src = _foldyard_src()
    cmd = ["uv", "tool", "install", "--force"]
    return cmd + (["--editable", f"{src}[host]"] if src else ["foldyard[host]"])


def host_install_hint() -> str:
    """:func:`host_install_cmd` as a line a reader can paste — quoted, since zsh globs a bare
    ``[host]``. (It was `just install` — foldyard's own dev recipe in its origin
    monorepo's spelling, which no consumer's justfile has.)"""
    return shlex.join(host_install_cmd())


# Where the proxy CA is mounted inside the box; its presence is also how `verify` (in-box)
# tells a proxy mode from off. One constant so the mount target and the posture probe (the
# github plugin reads `proxy.BOX_CA`) can never drift apart.
BOX_CA = Path("/etc/dev-proxy-ca.pem")

# The COMBINED trust bundle the box-up snippet builds (system roots + the mitm CA). Phase A′
# always-routes, and a `[proxy] passthrough` host is tunnelled un-decrypted, presenting its REAL
# certs end-to-end — so the bundle-replacing vars (REQUESTS_CA_BUNDLE/GIT_SSL_CAINFO) must trust
# both real and mitm certs, not ONLY the mitm CA (which would reject every passthrough host). See
# box._CA_TRUST_SNIPPET, which writes this file additively after the system-trust install.
BOX_CA_BUNDLE = Path("/etc/dev-proxy-ca-combined.pem")

# The per-project egress log: egress_proxy.py writes it host-side; the Network Log panel tails it.
# One filename constant so the daemon's PROXY_LOG_FILE and the panel never drift.
_PROXY_LOG = "egress.jsonl"

_NET_ROWS = 400  # how many recent log lines the panel groups (across all hosts)


def _proxy_log() -> Path:
    return config.log_dir() / _PROXY_LOG


def _live_file() -> Path:
    """This worktree's live proxy settings (rules, wall, passthrough) — host-side state, beside
    the posture it's derived from, never in the mount."""
    return config.posture_dir() / "proxy-live.json"


def _packaged_addon() -> Path:
    """The mitmdump injection addon, shipped INSIDE the package (``assets/proxy/egress_proxy.py``).
    Was consumer-side (``<dev_vm_dir>/proxy/egress_proxy.py``); now packaged so consumers get the
    proxy/capture/keyless path without vendoring the script, and the tests load this one source."""
    return Path(__file__).resolve().parent.parent / "assets" / "proxy" / "egress_proxy.py"


def _launch_addon_path() -> Path:
    """The STABLE path the proxy's ``-s`` points at — a snapshot under ``state_dir`` (outside any
    working tree). mitmdump watches its ``-s`` file and hot-reloads on change; if that file lived in
    the repo, a ``git checkout``/rebase would rewrite it under the running proxy, which silently
    un-loads the addon → a dumb passthrough that drops token injection (upstreams then 401). The
    supervisor snapshots the packaged addon here before each (re)launch (the daemon spec's ``stage``
    step), so the watched file never changes under a live mitmdump. Pure path here — no I/O, since
    ``daemons()`` is on the registry hot path / called bare in tests."""
    return config.state_dir() / "egress_proxy.py"


def _tail_network_log(limit: int = _NET_ROWS) -> list[dict]:
    """The last ``limit`` JSON lines across the rotated + live egress log (oldest→newest). Tails
    only the END of the (possibly multi-MB, capture-fed) log — see ``config.tail_jsonl`` — so the
    panel's per-second refresh stays cheap regardless of how big the log has grown."""
    return config.tail_jsonl(_proxy_log(), limit)


def _net_leaf(e: dict, escape) -> str:
    """One request as a leaf line under its host group: time · method · path · status · marks.

    A passthrough row (a trusted host, TLS not decrypted) has no method/path/status — only the SNI
    host + time — so it renders as a dim `tls` tunnel marker instead of a request line."""
    ts = escape(e.get("ts", "")[11:19])
    if e.get("blocked"):
        # Refused by the default-deny egress wall — no upstream contacted. Red so it stands out as
        # the row you act on (press the allow key in the TUI to let the host through).
        return f"[dim]{ts}[/dim] [red]⛔ blocked by the egress wall[/red]"
    if e.get("would_block"):
        # The wall is observing (`fy allow enforce off`, or a learn window): this host went through,
        # but enforcing would refuse it — what `fy allow learn` offers. The UA names the tool.
        ua = f" [dim]{escape(e['ua'][:60])}[/dim]" if e.get("ua") else ""
        return f"[dim]{ts}[/dim] [yellow]◌ the wall would refuse this[/yellow]{ua}"
    if e.get("passthrough"):
        return f"[dim]{ts}[/dim] [dim]· tls tunnel (passthrough, not decrypted)[/dim]"
    status = e.get("status", 0)
    colour = "green" if status < 400 else "red"
    mark = ("inj" if e.get("injected") else "") + ("+replay" if e.get("replayed") else "")
    line = (
        f"[dim]{ts}[/dim] {escape(e.get('method', '')):<6} "
        f"{escape(e.get('path', '')[:80])} [{colour}]{status}[/{colour}]"
    )
    if mark:
        line += f" [cyan]{mark}[/cyan]"
    # The upstream's own error reason (captured for 4xx/5xx by egress_proxy.py) — shown inline so a
    # failure says WHY, not just the code. Untrusted text: escaped, snippet-capped host-side.
    body = e.get("error_body")
    if body and status >= 400:
        line += f"  [red dim]{escape(body[:120])}[/red dim]"
    return line


def _network_panel_tree() -> PanelTree:
    """The Network Log tab's data, GROUPED BY HOST into a collapsible tree: one group per domain
    (header = request count + injected/error tallies) whose children are its requests, newest
    first; groups ordered by most-recent activity. The summary counts the whole capture. Called by
    the TUI on a timer, so the rich import is lazy here — the module stays stdlib-only on the
    registry hot path. Untrusted text (hosts, paths) is escaped before it carries any markup."""
    from rich.markup import escape  # lazy: only when the TUI paints this panel

    log = _proxy_log()
    entries = _tail_network_log()  # oldest→newest
    by_host: dict[str, list[dict]] = {}
    for e in entries:
        by_host.setdefault(e.get("host", "—"), []).append(e)
    # Most recently active host first (its last entry's timestamp); ISO ts sorts lexically.
    ordered = sorted(by_host.items(), key=lambda kv: kv[1][-1].get("ts", ""), reverse=True)

    groups: list[PanelGroup] = []
    injected = 0
    for host, evs in ordered:
        inj = sum(bool(e.get("injected")) for e in evs)
        blocked = sum(bool(e.get("blocked")) for e in evs)
        unlisted = sum(bool(e.get("would_block")) for e in evs)
        # A blocked request synthesises a 403, so it's already in `errs` — subtract it so the
        # tallies don't double-count the same row (blocked is the more specific, actionable label).
        errs = sum(1 for e in evs if e.get("status", 0) >= 400) - blocked
        injected += inj
        header = f"[bold]{escape(host)}[/bold]  [dim]{len(evs)} req[/dim]"
        if inj:
            header += f" · [cyan]{inj} inj[/cyan]"
        if blocked:
            header += f" · [red]⛔ {blocked} blocked[/red]"
        if unlisted:
            header += " · [yellow]◌ not granted[/yellow]"
        if errs:
            header += f" · [red]{errs} err[/red]"
        children = [_net_leaf(e, escape) for e in reversed(evs)]  # newest first within the host
        groups.append(PanelGroup(key=host, label=header, children=children))

    summary = (
        f"{len(entries)} requests across {len(groups)} hosts · {injected} with injected "
        f"Authorization · [dim]{log}[/dim]"
        if entries
        else "no proxied traffic yet — the box routes through the always-on proxy; make a request "
        f"(or check the supervisor: `fy host`) · [dim]{log}[/dim]"
    )
    return PanelTree(summary=summary, groups=groups)


def _rule_to_json(rule) -> dict:
    """One :class:`InjectRule` → the JSON object egress_proxy.py's live-file rule expects
    (``minter``→``command``, ``replay_on_401``→``retry_401``). Only non-empty optional fields are
    emitted, so the serialized rule set stays compact + stable. Used for the multi-injector case."""
    out: dict = {"host": rule.host, "command": rule.minter, "retry_401": rule.replay_on_401}
    if rule.env:
        out["env"] = list(rule.env)  # the only host env this rule's minter may read
    if rule.query_param:
        out["query_param"] = rule.query_param
    else:
        out["header"] = rule.header
    if rule.path_prefix:
        out["path_prefix"] = rule.path_prefix
    if rule.value_prefix:
        out["value_prefix"] = rule.value_prefix
    return out


def _default_deny() -> bool:
    """Enforcement on/off, from the HOST-owned allow-store (``allowlist.default_deny``) — not
    straight from ``[proxy] enforce``, which the box could edit to switch its own wall off."""
    from .. import allowlist  # lazy: stdlib-only, but the registry hot path needn't import it

    return allowlist.default_deny()


def _observing_since() -> str | None:
    """The open learn window's start, or None (host-owned, like :func:`_default_deny`)."""
    from .. import allowlist  # lazy, as in _default_deny

    window = allowlist.learning()
    return window["since"] if window else None


def _resolve_passthrough(entries: list[str]) -> list[str]:
    """Expand the ``[proxy] passthrough`` entries into a flat, deduped, order-preserving pattern
    list the daemon matches against: ``@all`` → every built-in bundle, ``@name`` → that bundle,
    anything else → a literal host or ``*.suffix`` glob. Unknown ``@name`` refs expand to nothing
    (a typo silently trusts less, never more — fail safe toward MORE decryption)."""
    out: list[str] = []
    seen: set[str] = set()

    def add(pattern: str) -> None:
        if pattern and pattern not in seen:
            seen.add(pattern)
            out.append(pattern)

    for entry in entries:
        if entry == "@all":
            for hosts in BUNDLES.values():
                for h in hosts:
                    add(h)
        elif entry.startswith("@"):
            for h in BUNDLES.get(entry[1:], ()):
                add(h)
        else:
            add(entry)
    return out


class ProxyPlugin(Plugin):
    name = "proxy"

    # No axis of its own. There was a `capture` axis (off = blind-tunnel everything, on = decrypt);
    # it is gone (ADR-0029): the proxy ALWAYS decrypts and logs, except the trusted `[proxy]
    # passthrough` hosts, which is what `capture=on` meant. Off bought ~3 ms per new connection and
    # bulk throughput a download link rarely reaches — for a posture switch an operator had to
    # understand. The injector axes (github/gcp/…) still map to this plugin's daemon.

    def _rules(self, mode: dict) -> list:
        """The injection rules from every plugin (this registry's, not necessarily the global
        one — `_registry` is bound in Registry.__init__)."""
        return self._registry.proxy_rules(mode) if self._registry else []

    def _rule_defaults(self, mode: dict, rules: list) -> dict[str, str]:
        names = {key for r in rules for key in r.env}
        derived = self._registry.env_defaults(mode) if self._registry else {}
        return {k: v for k, v in derived.items() if k in names}

    def daemons(self, mode: dict) -> dict[str, dict]:
        rules = self._rules(mode)
        # Same opt-in gate as axes()/derive_env(): a generic/stack-less consumer that declares no
        # `[proxy]` and has no active injector gets NO listener — else desired_daemons() would still
        # expose :8088 and Doctor / the mode dashboard would flag it perpetually DOWN. An opted-in
        # consumer (proxy_enabled) or any active rule keeps the always-on proxy, as Phase A′ needs.
        if not config.proxy_enabled() and not rules:
            return {}
        # Phase A′ — ALWAYS-ON: the proxy runs unconditionally (was: only for an injector), because
        # the box ALWAYS routes through it (derive_env). A dead :8088 would connection-refuse every
        # box request, so the daemon must never be absent while a box exists.
        #
        # Always CAPTURE_MODE=full (ADR-0029): decrypt + log every request EXCEPT the trusted hosts
        # in the passthrough list, which are blind-tunnelled with an SNI-only log row. The addon
        # keeps its "passthrough" mode as a standalone option; foldyard no longer asks for it.
        #
        # The launch env holds only what never changes with posture. Everything that does — the
        # injection rules, the wall switch, the passthrough list — goes in the LIVE file the
        # supervisor writes and the addon re-reads (see `live` below). The supervisor restarts a
        # daemon whose cmd/env changed, and a restart cuts every connection in flight: a mode
        # switch used to kill a running apt download mid-package. So a posture change must never
        # reach the env.
        #
        # Secrets aren't here either, as values or as a stamp: the addon reads each rule's
        # declared names from host.env (HOST_ENV_FILE) whenever that file changes, and the
        # supervisor strips host.env's keys from this daemon's environment (`scrub_host_env`).
        env = {
            "PROXY_LOG_FILE": str(_proxy_log()),
            "CAPTURE_MODE": "full",
            # The effective allowlist, re-read per request (mtime-cached) — grants never restart.
            "ALLOW_FILE": str(config.allow_effective_file()),
            "LIVE_FILE": str(_live_file()),
            "HOST_ENV_FILE": str(config.host_env_file()),
            # The hashes of the live build secrets (buildgate): what unlocks build-scoped grants.
            "BUILD_TOKENS_FILE": str(config.build_tokens_file()),
        }
        live = {
            "rules": [_rule_to_json(r) for r in rules],
            # From the HOST-owned allow-store, never straight from the repo's `[proxy]`.
            "default_deny": _default_deny(),
            "passthrough": _resolve_passthrough(config.proxy_passthrough()),
            # The open learn window's start: a new window resets the addon's per-host would-block
            # rate limit, or a host seen just before it would get no row inside it.
            "observing_since": _observing_since(),
            # Derived, non-secret identity a rule's minter reads (github=app's GH_APP_ID & co.,
            # from env_defaults): the running proxy never restarts to inherit the supervisor's env,
            # so it gets them here. After exports and host.env, as the supervisor's setdefault.
            "defaults": self._rule_defaults(mode, rules),
        }
        if rules:
            label = "egress proxy (" + ", ".join(r.label or r.host for r in rules) + ")"
        else:
            label = "egress proxy (decrypt + log; trusted hosts tunnelled)"
        requires = sorted({req for r in rules for req in r.requires})
        # One proxy listener PER WORKTREE (ADR-0016): the daemon name carries the
        # worktree suffix and the port is the worktree's offset port, so the ONE supervisor can run
        # N listeners without name/port collisions. The main checkout keeps the bare "egress-proxy"
        # name + the project band's base port — byte-identical to the single-worktree world. The
        # box's FY_PROXY (derive_env) points at this same per-worktree port, so they always agree.
        port = config.proxy_port()
        return {
            # Keep the main daemon name "egress-proxy" + the INJECT_*/PROXY_LOG_FILE/-s
            # egress_proxy.py contract byte-identical (a worktree appends "@<name>"): the injector
            # axes map to this daemon, and the addon reads these exact keys.
            # For github the env equals egress_proxy.py's defaults.
            f"egress-proxy{config.worktree_suffix()}": {
                "label": label,
                "port": port,
                # Snapshot the packaged addon to the stable launch path BEFORE (re)launch — the
                # supervisor copies each (src, dst) here, so mitmdump's `-s` watches a file under
                # state_dir that no git checkout can rewrite under it (see _launch_addon_path).
                "stage": [(str(_packaged_addon()), str(_launch_addon_path()))],
                "cmd": [
                    # Resolved from foldyard's own venv (the proxy extra) or PATH; the bare
                    # fallback keeps the supervisor's "mitmproxy installed?" OSError nag firing
                    # when it's genuinely absent. basename stays "mitmdump" either way.
                    mitmdump_path() or "mitmdump",
                    "-s",
                    str(_launch_addon_path()),
                    "--listen-host",
                    "0.0.0.0",
                    "--listen-port",
                    str(port),
                    # Silence mitmdump's per-request flow dump (the `GET … << 200 OK` blocks):
                    # egress_proxy.py already records every request to egress.jsonl, which the TUI's
                    # Network Log tab renders grouped-by-host — so echoing each request to the
                    # supervisor's stdout (→ the Mode tab's host-log) is pure duplication. Keeps
                    # connect/listening/error lines (those are termlog, not flow_detail).
                    "--set",
                    "flow_detail=0",
                    # Relay bodies past 1 MiB as they arrive instead of buffering the whole body
                    # in the proxy's memory first. mitmproxy buffers by default, so a decrypted
                    # 2 GB image layer or model download was held in RAM before the box saw a
                    # byte; streaming also doubled decrypted throughput (~270 → ~590 MB/s on
                    # loopback, 2026-09-22). Error bodies (the log's snippet) and 401 re-issues
                    # stay small, so nothing the addon reads is lost.
                    "--set",
                    "stream_large_bodies=1m",
                ],
                "env": env,
                # Written by the supervisor (whole, renamed into place) whenever it changes; the
                # addon picks it up within a second, with no restart. See `env` above.
                "live": {"path": str(_live_file()), "data": live},
                "scrub_host_env": True,
                "requires": requires,
            }
        }

    def derive_env(self, mode: dict) -> dict[str, str]:
        # Phase A′ — ALWAYS route box egress through the always-on proxy (was: only for an
        # injector). The routing env is baked once at box-up and never changes; what the proxy does
        # with it (inject, wall) is decided host-side. An explicit FY_PROXY still wins downstream.
        # The cost: a box always needs the proxy alive — `machine up` launches `foldyard
        # host` so it is (and the supervisor auto-restarts it). box_args keys off this for routing.
        #
        # Gated on the consumer OPTING IN (`[proxy]` table) — without it, a generic/stack-less
        # project gets a clean box (no FY_PROXY ⇒ box_args adds no HTTPS_PROXY/NO_PROXY env). ANY
        # active injector (github=app/user, a `[[inject]]` axis, or claude keyless) needs the proxy,
        # so an active rule lights routing up too — not just github (the rule set is the general
        # signal; github is one case of it).
        if not config.proxy_enabled() and not self._rules(mode):
            return {}
        # Per-worktree port (config.proxy_port) so each worktree's box routes to ITS OWN proxy
        # listener — the supervisor runs one per worktree on the matching offset port. The address
        # is backend-dependent (config.host_alias): host.containers.internal under podman-machine/
        # native, Lima's guest→host gateway IP under lima (where that alias points at the VM, not
        # the Mac).
        return {"FY_PROXY": f"{config.host_alias()}:{config.proxy_port()}"}

    def box_args(self, env: dict) -> list[str]:
        # Two SEPARATE concerns, deliberately decoupled (the ambient-CA rule):
        #
        #  1. CA TRUST — AMBIENT: mount + additively trust the CA whenever it EXISTS, regardless
        #     of routing. `box_up`'s `_CA_TRUST_SNIPPET` appends it to the system store and
        #     NODE_EXTRA_CA_CERTS adds it to Node's built-in roots — both ADDITIVE, so a box with
        #     no proxy routing keeps verifying real certs normally. Pre-positions trust so an
        #     injector can later be enabled host-side without re-creating the box (env can't
        #     change in a running box). The bind SOURCE must be VM-visible — `box_up` stages it
        #     under the checkout and points MITMPROXY_CA there (the machine mounts ONLY repo +
        #     worktrees).
        #
        #  2. ROUTING — GATED on FY_PROXY (derive_env now ALWAYS sets it — Phase A′ always-route;
        #     an explicit env var still wins). Sends egress through the host proxy. Here we set
        #     REQUESTS_CA_BUNDLE/GIT_SSL_CAINFO/SSL_CERT_FILE to the COMBINED bundle (BOX_CA_BUNDLE
        #     = system roots + mitm CA, built by the box-up snippet) — NOT the mitm CA alone. Under
        #     always-route, a `[proxy] passthrough` host is tunnelled un-decrypted and presents its
        #     REAL cert, so a mitm-only bundle would reject them; the combined bundle trusts both.
        #     Routing requires the CA (decrypted/MITM'd flows are mitm-signed), so no CA with
        #     routing is a hard error — `machine up`'s `foldyard host` generates it on first run.
        #
        # The dummy GH_TOKEN is the INJECTOR's concern (github contributes it), not the proxy's.
        proxy = env.get("FY_PROXY")
        ca = _mitm_ca()
        args: list[str] = []
        if ca.exists():
            args += ["-e", f"NODE_EXTRA_CA_CERTS={BOX_CA}", "-v", f"{ca}:{BOX_CA}:ro"]
        if not proxy:
            return args
        if not ca.exists():
            raise SystemExit(
                f"✗ FY_PROXY set ({proxy}) but no CA at {ca} — `fy host restart` on the host "
                "once to generate it (or 'fy doctor', which offers the same fix)."
            )
        # In-stack services bypass the proxy (NO_PROXY): it runs on the Mac and can't resolve a
        # stack-network hostname, so a proxied call to one 502s (after hanging). Three sources,
        # and the split is the point — foldyard names NO consumer service:
        #   loopback   always, for every consumer;
        #   plugins    each contributes its OWN services (the gcp metadata emulator), config-gated
        #              so an unconfigured plugin adds nothing;
        #   consumer   `[proxy] no_proxy`, with {project} expanded to the compose project name so
        #              one entry covers every worktree's container-name prefix.
        # An entry is inert when its container isn't running, so listing a service a rung hasn't
        # started costs nothing.
        project = env.get("PODMAN_PROJECT", "")
        declared = [h.replace("{project}", project) for h in config.proxy_no_proxy()]
        # `_registry` (bound in Registry.__init__) — THIS registry, not the global cached one.
        from_plugins = self._registry.no_proxy_hosts() if self._registry else []
        no_proxy = ",".join(["localhost", "127.0.0.1", *from_plugins, *declared])
        return [
            *args,
            "-e",
            f"HTTPS_PROXY=http://{proxy}",
            "-e",
            f"HTTP_PROXY=http://{proxy}",
            "-e",
            f"https_proxy=http://{proxy}",
            "-e",
            f"http_proxy=http://{proxy}",
            "-e",
            f"NO_PROXY={no_proxy}",
            "-e",
            f"no_proxy={no_proxy}",
            "-e",
            f"REQUESTS_CA_BUNDLE={BOX_CA_BUNDLE}",
            "-e",
            f"GIT_SSL_CAINFO={BOX_CA_BUNDLE}",
            # SSL_CERT_FILE is the OpenSSL-standard var — covers runtimes the three above miss:
            # uv (its bundled rustls roots ignore those), curl, Python ssl, openssl. Same combined
            # bundle; same routing gating.
            "-e",
            f"SSL_CERT_FILE={BOX_CA_BUNDLE}",
        ]

    def _ever_rules(self, mode: dict) -> bool:
        """Could ANY rung of this registry's axes put an injection rule on the proxy? The current
        mode's rules, or those another rung would activate (asked of the registry, as
        `exposure._targets` does — so an odd-shaped mechanism reports as it behaves)."""
        if not self._registry:
            return False
        if self._registry.proxy_rules(mode):
            return True
        for axis, rungs in self._registry.axis_rungs().items():
            for rung in rungs:
                if rung != mode.get(axis, rungs[0]) and self._registry.proxy_rules(
                    {**mode, axis: rung}
                ):
                    return True
        return False

    def doctor_checks(self, ctx: DoctorContext) -> Iterable[tuple[str, str, str]]:
        # Whole-hook gate, matching daemons()/derive_env(): a consumer with no [proxy] and no
        # injector that could ever ride the proxy gets NO proxy rows. They used to be
        # unconditional, which left such a consumer's `fy doctor` failing forever with "the box
        # always routes through it" — false for it (found on the Linux rig, 2026-09-13). A
        # DECLARED-but-off injector keeps the prerequisites below (the operator should see a
        # missing mitmproxy/CA BEFORE arming it); the listener row stays behind the live gate.
        if not config.proxy_enabled() and not self._ever_rules(ctx.mode):
            return
        # Proxy-FRAMEWORK setup, host-side (the proxy daemon + CA live on the host): is mitmdump
        # installed, and has its CA been generated? These were the github plugin's, but they're
        # proxy concerns — github is just one injector that rides the proxy. Resolve mitmdump the
        # same way the daemon does (foldyard's own venv OR PATH), not via ctx.which (PATH-only),
        # since installing foldyard lands mitmdump in its own venv, off PATH.
        yield ctx.result(
            mitmdump_path() is not None,
            "mitmproxy",
            "installed",
            f"missing — reinstall with the [host] extra: {host_install_hint()}",
        )
        ca = _mitm_ca()
        yield ctx.result(
            ca.exists() or None,
            "mitm CA",
            str(ca),
            "not generated yet — the supervisor's first run creates it (`fy host restart`)",
        )
        # RUNTIME: is the always-on egress proxy actually listening? Phase A′ ALWAYS-routes the box
        # through it, so a down proxy means EVERY box request connection-refuses — this is the check
        # that tells you why "requests in the box aren't working". Only a finding when the
        # supervisor is asked to run it (the same gate as daemons(): opted in, or a rule active
        # NOW) — a declared-but-off injector has no listener to be down. Probe handles box→host.
        if not config.proxy_enabled() and not self._rules(ctx.mode):
            return
        log = config.supervisor_log_file()
        port = config.proxy_port()  # this worktree's listener port (project band base + offset)
        yield ctx.result(
            ctx.probe(port),
            "egress proxy",
            f"running on :{port} (the box routes through it)",
            f"NOT running on :{port} — the box always routes through it, so its egress will "
            f"hang/refuse. Start it: `fy host restart` (or `fy up`). Log: {log}",
        )

    def doctor_fixes(self) -> Iterable[DoctorFix]:
        # One-click repairs for the two checks above. Both NON-INTERACTIVE (run in a TUI worker).
        # The reinstall is the same command the doctor row prints (host_install_cmd).
        # Generate the CA with no server/port via mitmproxy's own API (sys.executable is foldyard's
        # venv python — it carries mitmproxy). Confdir = the CA's dir.
        confdir = _mitm_ca().parent
        gen_ca = (
            "from pathlib import Path; from mitmproxy.certs import CertStore; "
            f"d = Path({str(confdir)!r}); d.mkdir(parents=True, exist_ok=True); "
            "CertStore.create_store(d, 'mitmproxy', 2048); print('mitm CA written to', d)"
        )
        return [
            DoctorFix(check="mitmproxy", label="reinstall foldyard", cmd=host_install_cmd()),
            DoctorFix(check="mitm CA", label="generate CA", cmd=[sys.executable, "-c", gen_ca]),
        ]

    def tui_panels(self) -> list[TuiPanel]:
        # The egress Network Log: the proxy's per-request JSONL, GROUPED BY HOST into a collapsible
        # tree (expand a domain to see its requests). A proxy concern — it shows ALL traffic the
        # proxy sees (every request + injector), with host-injected Authorization marked.
        return [
            TuiPanel(
                id="network",
                title="Network Log",
                columns=(),
                refresh=_network_panel_tree,
                kind="tree",
            )
        ]
