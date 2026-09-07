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

import hashlib
import json
import os
import shutil
import sys
from collections.abc import Iterable
from pathlib import Path

from .. import config
from . import Axis, DoctorContext, DoctorFix, PanelGroup, PanelTree, Plugin, TuiPanel
from ._passthrough_bundles import BUNDLES

# The host ports the mitmdump injection proxy listens on come from ``config.proxy_port()`` —
# per-project band + per-worktree offset; there is deliberately no module-level constant (it
# would freeze the env at import and bypass the band allocation).


def mitmdump_path() -> str | None:
    """Resolve the ``mitmdump`` executable, or ``None`` if it isn't installed.

    Prefer the copy BESIDE foldyard's own interpreter: the HOST install (`just foldyard install` →
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


# Where the proxy CA is mounted inside the box; its presence is also how `verify` (in-box)
# tells a proxy mode from off. One constant so the mount target and the posture probe (the
# github plugin reads `proxy.BOX_CA`) can never drift apart.
BOX_CA = Path("/etc/dev-proxy-ca.pem")

# The COMBINED trust bundle the box-up snippet builds (system roots + the mitm CA). Phase A′
# always-routes, and capture=off TLS-passthrough leaves un-decrypted hosts presenting their REAL
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


def _packaged_addon() -> Path:
    """The mitmdump injection addon, shipped INSIDE the package (``assets/proxy/egress_proxy.py``).
    Was consumer-side (``<dev_vm_dir>/proxy/egress_proxy.py``); now packaged so consumers get the
    proxy/capture/keyless path without vendoring the script, and the tests load this one source."""
    return Path(__file__).resolve().parent.parent / "assets" / "proxy" / "egress_proxy.py"


def _secret_stamp(keys: Iterable[str]) -> str:
    """A short fingerprint of the named env vars' CURRENT values (missing hashes as empty), for the
    daemon spec env — so a captured/rotated secret changes the spec and the supervisor restarts the
    daemon that reads it (see the INJECT_ENV_STAMP comment in :meth:`ProxyPlugin.daemons`).
    Truncated sha256: enough to never collide in practice, and not invertible for the high-entropy
    values it hashes — the stamp is visible in ``ps eww`` on the child, the secret must not be."""
    digest = hashlib.sha256()
    for key in keys:
        digest.update(key.encode())
        digest.update(b"\x00")
        digest.update(os.environ.get(key, "").encode())
        digest.update(b"\x00")
    return digest.hexdigest()[:16]


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

    A passthrough row (capture=off, TLS not decrypted) has no method/path/status — only the SNI
    host + time — so it renders as a dim `tls` tunnel marker instead of a request line."""
    ts = escape(e.get("ts", "")[11:19])
    if e.get("blocked"):
        # Refused by the default-deny egress wall — no upstream contacted. Red so it stands out as
        # the row you act on (press the allow key in the TUI to let the host through).
        return f"[dim]{ts}[/dim] [red]⛔ blocked by the egress wall[/red]"
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
        # A blocked request synthesises a 403, so it's already in `errs` — subtract it so the
        # tallies don't double-count the same row (blocked is the more specific, actionable label).
        errs = sum(1 for e in evs if e.get("status", 0) >= 400) - blocked
        injected += inj
        header = f"[bold]{escape(host)}[/bold]  [dim]{len(evs)} req[/dim]"
        if inj:
            header += f" · [cyan]{inj} inj[/cyan]"
        if blocked:
            header += f" · [red]⛔ {blocked} blocked[/red]"
        if errs:
            header += f" · [red]{errs} err[/red]"
        children = [_net_leaf(e, escape) for e in reversed(evs)]  # newest first within the host
        groups.append(PanelGroup(key=host, label=header, children=children))

    summary = (
        f"{len(entries)} requests across {len(groups)} hosts · {injected} with injected "
        f"Authorization · [dim]{log}[/dim]"
        if entries
        else "no proxied traffic yet — the box routes through the always-on proxy; make a request "
        f"(or check `fy host` is running) · [dim]{log}[/dim]"
    )
    return PanelTree(summary=summary, groups=groups)


def _rule_to_json(rule) -> dict:
    """One :class:`InjectRule` → the JSON object egress_proxy.py's ``INJECT_RULES`` rule expects
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
    straight from ``[proxy] default_deny``, which the box could edit to switch its own wall off."""
    from .. import allowlist  # lazy: stdlib-only, but the registry hot path needn't import it

    return allowlist.default_deny()


def _capturing(mode: dict) -> bool:
    """The capture axis: MITM-log all dev-box egress through the proxy even with no injector."""
    return mode.get("capture", "off") == "on"


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

    def axes(self) -> list[Axis]:
        # The proxy plugin owns its own `capture` axis (the github/gcp axes are the injectors').
        # Phase A′ — ALWAYS-ROUTE: the box's egress always goes through the always-on proxy
        # (derive_env always sets FY_PROXY), so `capture` is a HOST-SIDE decision about what the
        # proxy DOES, toggleable on a RUNNING box (no recreate). `off` TLS-passthrough (blind-
        # tunnel, real certs end-to-end, SNI-only log); `on` MITM-decrypts + logs every request.
        # It composes with the injector axes (github=app always MITM-rewrites api.github.com on
        # top, whatever capture is). Maps to the same "egress-proxy" daemon.
        #
        # Self-gate on the consumer OPTING IN ([proxy] table; registry plan Step D): a generic /
        # stack-less repo that declares no [proxy] gets no `capture` axis (and derive_env/box_args
        # already emit no proxy wiring for it), so the mode TUI doesn't advertise a posture that
        # means nothing there. An active injector still lights routing up via derive_env.
        if not config.proxy_enabled():
            return []
        return [
            Axis(
                name="capture",
                rungs=("off", "on"),
                blurb={
                    "off": "route egress through the proxy, TLS-passthrough + SNI-log (no decrypt)",
                    "on": "MITM-decrypt + log UNTRUSTED egress; trusted hosts pass through",
                },
                daemon="egress-proxy",
            )
        ]

    def _rules(self, mode: dict) -> list:
        """The injection rules from every plugin (this registry's, not necessarily the global
        one — `_registry` is bound in Registry.__init__)."""
        return self._registry.proxy_rules(mode) if self._registry else []

    def daemons(self, mode: dict) -> dict[str, dict]:
        rules = self._rules(mode)
        # Same opt-in gate as axes()/derive_env(): a generic/stack-less consumer that declares no
        # `[proxy]` and has no active injector gets NO listener — else desired_daemons() would still
        # expose :8088 and Doctor / the mode dashboard would flag it perpetually DOWN. An opted-in
        # consumer (proxy_enabled) or any active rule keeps the always-on proxy, as Phase A′ needs.
        if not config.proxy_enabled() and not rules:
            return {}
        # Phase A′ — ALWAYS-ON: the proxy runs unconditionally (was: only for an injector or
        # capture=on), because the box ALWAYS routes through it (derive_env). A dead :8088 would
        # connection-refuse every box request, so the daemon must never be absent while a box
        # exists. CAPTURE_MODE tells egress_proxy.py what to do with HTTPS it isn't injecting:
        #   on  → "full":        MITM-decrypt + log every request
        #   off → "passthrough": blind-tunnel (real certs end-to-end), SNI-only log
        # An injector host (github=app) is ALWAYS decrypted + rewritten, whatever CAPTURE_MODE is.
        # Flipping capture host-side changes only this env → the supervisor restarts the daemon
        # (signature change) live, with the box still routing + trusting — no box recreate.
        capture_mode = "full" if _capturing(mode) else "passthrough"
        # The TRUSTED hosts to TLS-passthrough even under capture=on (everything else is decrypted).
        # Resolved host-side from `[proxy] passthrough` (+ @bundles) and handed to the addon as a
        # comma-list. Only consulted in full mode, but always emitted (harmless, keeps the contract
        # simple). The injector host overrides this — it's always decrypted to rewrite its header.
        passthrough = ",".join(_resolve_passthrough(config.proxy_passthrough()))
        base_env = {
            "PROXY_LOG_FILE": str(_proxy_log()),
            "CAPTURE_MODE": capture_mode,
            "PASSTHROUGH_HOSTS": passthrough,
            # The egress wall (foldyard.allowlist). DEFAULT_DENY is the static on/off — toggling it
            # changes the daemon signature → the supervisor restarts the proxy with it. ALLOW_FILE
            # is the resolved effective allowlist the addon re-reads PER REQUEST (mtime-cached),
            # so a host-side grant takes effect with NO restart (the supervisor just sweeps + writes
            # that file). Always emitted (harmless when default-deny is off — the addon ignores it).
            "DEFAULT_DENY": "1" if _default_deny() else "",
            "ALLOW_FILE": str(config.allow_effective_file()),
        }
        if len(rules) == 1:
            # ONE injector: emit the legacy single INJECT_* env (byte-identical to before — every
            # single-rule caller reads these keys). The addon uses them when INJECT_RULES is
            # empty, so this is the MAINLINE path, not a shim. Its label names the credential.
            rule = rules[0]
            env = {
                **base_env,
                "INJECT_HOST": rule.host,
                # Query-param injection clears the header (the addon: query_param wins). Emitted
                # ONLY when set, so the github (header) case stays byte-identical to before.
                "INJECT_HEADER": "" if rule.query_param else rule.header,
                "INJECT_RETRY_401": "1" if rule.replay_on_401 else "0",
                "INJECT_COMMAND": rule.minter,
                # The minter's env allowlist — the addon withholds everything else, because the
                # supervisor's environment carries every axis's host.env secret.
                "INJECT_ENV_KEYS": ",".join(rule.env),
            }
            if rule.query_param:
                env["INJECT_QUERY_PARAM"] = rule.query_param
            if rule.path_prefix:
                env["INJECT_PATH_PREFIX"] = rule.path_prefix
            if rule.value_prefix:
                # The proxy PREPENDS this to the minted value (e.g. "Bearer " for an OAuth
                # `authorization` header) — emitted only when set, so github stays byte-identical.
                env["INJECT_VALUE_PREFIX"] = rule.value_prefix
            label, requires = rule.label, list(rule.requires)
        elif rules:
            # MANY injectors (the rule-set contract): hand the addon ALL of them as INJECT_RULES
            # JSON — it injects on each host with that host's own minter + token cache + 401 retry.
            # INJECT_RULES wins over the single INJECT_* (left empty), so the injectors coexist.
            env = {
                **base_env,
                "INJECT_HOST": "",
                "INJECT_HEADER": "",
                "INJECT_RETRY_401": "0",
                "INJECT_COMMAND": "",
                "INJECT_RULES": json.dumps([_rule_to_json(r) for r in rules]),
            }
            label = "egress proxy (" + ", ".join(r.label or r.host for r in rules) + ")"
            requires = sorted({req for r in rules for req in r.requires})
        else:
            # No injector: EMPTY INJECT_HOST/COMMAND → the addon rewrites nothing; CAPTURE_MODE
            # alone decides decrypt-and-log (capture=on) vs passthrough+SNI-log (capture=off).
            env = {
                **base_env,
                "INJECT_HOST": "",
                "INJECT_HEADER": "",
                "INJECT_RETRY_401": "0",
                "INJECT_COMMAND": "",
            }
            label = (
                "egress capture proxy (MITM-log all)"
                if _capturing(mode)
                else "egress proxy (passthrough + SNI log)"
            )
            requires = []
        # A fingerprint of every rule-declared secret VALUE (never the value itself — the spec env
        # lands in the child's `ps eww` and the supervisor's Child.signature). The daemon reads
        # secrets from its OWN environment, frozen at spawn — so a keyless token captured to
        # host.env AFTER the proxy launched (`fy box up` prompting once the box was already routing)
        # or a rotated secret would otherwise never reach the minter: the mint fails forever and the
        # box's dummy credential goes upstream verbatim (days of Anthropic 401s with a valid token
        # sitting in host.env). The supervisor merges host.env into its environment every tick, so
        # stamping the values here flips the spec signature → it restarts the daemon with the fresh
        # value within a tick. Same mechanism CAPTURE_MODE already rides (env change → restart).
        secret_keys = sorted({key for r in rules for key in r.env})
        if secret_keys:
            env["INJECT_ENV_STAMP"] = _secret_stamp(secret_keys)
        # One proxy listener PER WORKTREE (ADR-0016): the daemon name carries the
        # worktree suffix and the port is the worktree's offset port, so the ONE supervisor can run
        # N listeners without name/port collisions. The main checkout keeps the bare "egress-proxy"
        # name + the project band's base port — byte-identical to the single-worktree world. The
        # box's FY_PROXY (derive_env) points at this same per-worktree port, so they always agree.
        port = config.proxy_port()
        return {
            # Keep the main daemon name "egress-proxy" + the INJECT_*/PROXY_LOG_FILE/-s
            # egress_proxy.py contract byte-identical (a worktree appends "@<name>"): the github/
            # capture axes map to this daemon, and the addon reads these exact keys.
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
                ],
                "env": env,
                "requires": requires,
            }
        }

    def derive_env(self, mode: dict) -> dict[str, str]:
        # Phase A′ — ALWAYS route box egress through the always-on proxy (was: only for an injector
        # or capture=on). This is what makes `capture` toggleable on a RUNNING box: the routing env
        # is baked once at box-up and never changes; flipping capture only changes the daemon's
        # CAPTURE_MODE host-side. An explicit FY_PROXY still wins downstream. The cost (accepted in
        # the 2-rung model): a box always needs the proxy alive — `machine up` launches `foldyard
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
        #     no proxy routing keeps verifying real certs normally. Pre-positions trust so capture
        #     can later be flipped host-side without re-creating the box (env can't change in a
        #     running box). The bind SOURCE must be VM-visible — `box_up` stages it under the
        #     checkout and points MITMPROXY_CA there (the machine mounts ONLY repo + worktrees).
        #
        #  2. ROUTING — GATED on FY_PROXY (derive_env now ALWAYS sets it — Phase A′ always-route;
        #     an explicit env var still wins). Sends egress through the host proxy. Here we set
        #     REQUESTS_CA_BUNDLE/GIT_SSL_CAINFO/SSL_CERT_FILE to the COMBINED bundle (BOX_CA_BUNDLE
        #     = system roots + mitm CA, built by the box-up snippet) — NOT the mitm CA alone. Under
        #     always-route, capture=off TLS-passthrough leaves un-decrypted hosts presenting REAL
        #     certs, so a mitm-only bundle would reject them; the combined bundle trusts both.
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
                f"✗ FY_PROXY set ({proxy}) but no CA at {ca} — run 'fy host' on the host "
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

    def doctor_checks(self, ctx: DoctorContext) -> Iterable[tuple[str, str, str]]:
        # Proxy-FRAMEWORK setup, host-side (the proxy daemon + CA live on the Mac): is mitmdump
        # installed, and has its CA been generated? These were the github plugin's, but they're
        # proxy concerns — github is just one injector that rides the proxy. Resolve mitmdump the
        # same way the daemon does (foldyard's own venv OR PATH), not via ctx.which (PATH-only),
        # since installing foldyard lands mitmdump in its own venv, off PATH.
        yield ctx.result(
            mitmdump_path() is not None,
            "mitmproxy",
            "installed",
            "missing — reinstall foldyard: just foldyard install",
        )
        ca = _mitm_ca()
        yield ctx.result(
            ca.exists() or None,
            "mitm CA",
            str(ca),
            "not generated yet — first `fy host` run creates it",
        )
        # RUNTIME: is the always-on egress proxy actually listening? Phase A′ ALWAYS-routes the box
        # through it, so a down proxy means EVERY box request connection-refuses — this is the check
        # that tells you why "requests in the box aren't working". Probe handles box→host vs Mac.
        log = config.supervisor_log_file()
        port = config.proxy_port()  # this worktree's listener port (project band base + offset)
        yield ctx.result(
            ctx.probe(port),
            "egress proxy",
            f"running on :{port} (the box routes through it)",
            f"NOT running on :{port} — the box always routes through it, so its egress will "
            f"hang/refuse. Start it: `fy up` (or `fy host`). Log: {log}",
        )

    def doctor_fixes(self) -> Iterable[DoctorFix]:
        # One-click repairs for the two checks above. Both NON-INTERACTIVE (run in a TUI worker).
        src = _foldyard_src()
        # Reinstall foldyard WITH the `[host]` extra (mitmproxy → foldyard's own venv). This is a
        # HOST-side fix (the box never runs the proxy, so it never needs this), hence the extra.
        # Editable from the source dir when we can find it; the bare name is a best-effort fallback.
        install = ["uv", "tool", "install", "--force"]
        install += ["--editable", f"{src}[host]"] if src else ["foldyard[host]"]
        # Generate the CA with no server/port via mitmproxy's own API (sys.executable is foldyard's
        # venv python — it carries mitmproxy). Confdir = the CA's dir.
        confdir = _mitm_ca().parent
        gen_ca = (
            "from pathlib import Path; from mitmproxy.certs import CertStore; "
            f"d = Path({str(confdir)!r}); d.mkdir(parents=True, exist_ok=True); "
            "CertStore.create_store(d, 'mitmproxy', 2048); print('mitm CA written to', d)"
        )
        return [
            DoctorFix(check="mitmproxy", label="reinstall foldyard", cmd=install),
            DoctorFix(check="mitm CA", label="generate CA", cmd=[sys.executable, "-c", gen_ca]),
        ]

    def tui_panels(self) -> list[TuiPanel]:
        # The egress Network Log: the proxy's per-request JSONL, GROUPED BY HOST into a collapsible
        # tree (expand a domain to see its requests). A proxy concern — it shows ALL traffic the
        # proxy sees (capture + every injector), with host-injected Authorization marked.
        return [
            TuiPanel(
                id="network",
                title="Network Log",
                columns=(),
                refresh=_network_panel_tree,
                kind="tree",
            )
        ]
