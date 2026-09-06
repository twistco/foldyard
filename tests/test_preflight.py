"""preflight.py — the hard-prerequisite gate for `fy up` / `fy box up`.

Pure-ish: it reads config + host PATH + posture and returns a list of blocking problems. Tests drive
it by monkeypatching those inputs, so no real machine/proxy is touched.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from foldyard import preflight


class _FakeBackend:
    def __init__(self, cli: str, available: bool, name: str = "podman"):
        self.name = name
        self.cli = cli
        self.install_hint = f"brew install {cli}"
        self._available = available

    def available(self) -> bool:
        return self._available


def _wire(
    monkeypatch,
    *,
    backend="podman",
    backend_available=True,
    proxy_enabled=False,
    claude_keyless="",
    codex_keyless="",
    github="off",
    mitmdump: str | None = "/venv/bin/mitmdump",
    tables=None,
    in_box=False,
    wall=False,
    explicit=True,
):
    """Set every input preflight reads to a known value. `tables` maps a table name to its dict
    (for the raw `keyless` typo check); defaults to empty tables."""
    tables = tables or {}
    monkeypatch.setattr(preflight.config, "machine_backend", lambda: backend)
    # `explicit` distinguishes a backend the consumer NAMED from the inherited default.
    monkeypatch.setattr(
        preflight.config, "machine_backend_explicit", lambda: backend if explicit else ""
    )
    monkeypatch.setattr(preflight.config, "machine_wall", lambda: wall)
    cli = {"podman": "podman", "lima": "limactl", "native": "podman"}.get(backend, backend)
    monkeypatch.setattr(
        preflight.machine_backend,
        "get_backend",
        lambda name: _FakeBackend(cli, backend_available, name=backend),
    )
    monkeypatch.setattr(preflight.config, "proxy_enabled", lambda: proxy_enabled)
    monkeypatch.setattr(preflight.config, "claude_keyless", lambda: claude_keyless)
    monkeypatch.setattr(preflight.config, "codex_keyless", lambda: codex_keyless)
    monkeypatch.setattr(preflight.config, "_table", lambda name: tables.get(name, {}))
    monkeypatch.setattr(preflight.config, "in_box", lambda: in_box)
    monkeypatch.setattr(preflight.devmode, "read", lambda: {"mode": {"github": github}})
    # Default the nested-project check to a marker-less root so it short-circuits — the AMBIENT
    # root is itself nested (foldyard/foldyard.toml inside the monorepo checkout). The nested
    # tests below patch repo_root to their own tmp sandboxes.
    monkeypatch.setattr(preflight.config, "repo_root", lambda: Path("/fy-no-such-dir"))
    # Default to the main checkout (offset 0, check skipped); the offset tests override these.
    monkeypatch.setattr(preflight.config, "active_worktree", lambda: "")
    monkeypatch.setattr(preflight.config, "worktree_offset", lambda name: 0)
    # mitmdump_path is imported lazily from the proxy plugin inside issues(); patch the source.
    from foldyard.plugins import proxy

    monkeypatch.setattr(proxy, "mitmdump_path", lambda: mitmdump)


def test_all_present_no_proxy_needed_is_clean(monkeypatch):
    _wire(monkeypatch)
    assert preflight.issues() == []


def test_missing_backend_cli_blocks(monkeypatch):
    # CHOSEN explicitly → a missing CLI is a misconfiguration, and silently falling back to
    # another backend would swap the isolation profile without saying so.
    _wire(monkeypatch, backend="lima", backend_available=False, explicit=True)
    problems = preflight.issues()
    assert any("limactl" in p and "isn't installed" in p for p in problems)


def test_missing_cli_on_the_INHERITED_default_backend_blocks_too(monkeypatch):
    # Nobody named a backend → the lima DEFAULT, with no limactl. Blocking is the point: letting
    # it through leaves DOCKER_HOST/CONTAINER_HOST unset and drops the whole stack onto the host's
    # own podman socket (the native profile, unchosen). Different WORDING from the explicit case —
    # the fix is "install it, or NAME the weaker backend" — but the same hard stop.
    _wire(monkeypatch, backend="lima", backend_available=False, explicit=False)
    problems = preflight.issues()
    assert any("limactl" in p and "DEFAULT" in p and 'backend = "native"' in p for p in problems)


def test_proxy_required_but_mitmproxy_missing_blocks(monkeypatch):
    # A [proxy] table (or keyless) means the box routes through the proxy — mitmproxy must exist.
    _wire(monkeypatch, proxy_enabled=True, mitmdump=None)
    assert any("mitmproxy" in p and "[host] extra" in p for p in preflight.issues())


def test_mitmproxy_not_checked_when_no_proxy_needed(monkeypatch):
    # No injector/keyless/[proxy] ⇒ clean box, direct egress ⇒ mitmproxy is irrelevant.
    _wire(monkeypatch, mitmdump=None)
    assert preflight.issues() == []


def test_lima_with_keyless_is_clean(monkeypatch):
    # Keyless on lima is ROUTED now (config.host_alias emits the Lima host gateway, so the box
    # reaches the Mac proxy) — the old hard-block must be gone.
    _wire(monkeypatch, backend="lima", claude_keyless="oauth")
    assert preflight.issues() == []


def test_lima_without_proxy_is_fine(monkeypatch):
    # Plain lima box (no injector) routes egress DIRECT — no proxy, so no routing problem.
    _wire(monkeypatch, backend="lima")
    assert preflight.issues() == []


def test_wall_on_non_lima_backend_blocks(monkeypatch):
    # The wall provisions nftables into a lima VM; podman-machine/native have nothing to provision.
    _wire(monkeypatch, backend="podman", proxy_enabled=True, wall=True)
    assert any("[machine].wall" in p and "lima" in p for p in preflight.issues())


def test_wall_without_proxy_routing_blocks(monkeypatch):
    # wall=true + nothing routing through the proxy = an airgapped box (default-deny, no way out).
    _wire(monkeypatch, backend="lima", wall=True)
    problems = preflight.issues()
    assert any("[machine].wall" in p and "NO way out" in p for p in problems)


def test_wall_with_lima_and_keyless_is_clean(monkeypatch):
    # The intended locked-down posture: lima + wall + keyless (proxy routed + enforced in-VM).
    _wire(monkeypatch, backend="lima", claude_keyless="oauth", wall=True)
    assert preflight.issues() == []


def test_unrecognised_keyless_value_blocks(monkeypatch):
    # `keyless = "oath"` normalises to "" (silently off) — flag it loudly.
    _wire(monkeypatch, claude_keyless="", tables={"claude": {"keyless": "oath"}})
    assert any("[claude].keyless" in p and "oath" in p for p in preflight.issues())


def test_recognised_keyless_value_does_not_block(monkeypatch):
    # A valid value that normalises fine raises no typo complaint.
    _wire(monkeypatch, claude_keyless="oauth", tables={"claude": {"keyless": "oauth"}})
    assert not any("isn't recognised" in p for p in preflight.issues())


def test_nested_project_inside_another_git_repo_blocks(monkeypatch, tmp_path):
    # A foldyard.toml INSIDE a bigger repo (running an example in-place from the foldyard
    # checkout) would bind the ENCLOSING repo as the checkout — abort with the copy-out recipe.
    outer = tmp_path / "outer"
    inner = outer / "sub"
    inner.mkdir(parents=True)
    (inner / "foldyard.toml").write_text('[project]\nname = "sub"\n')
    (outer / ".git").mkdir()
    _wire(monkeypatch)
    monkeypatch.setattr(preflight.config, "repo_root", lambda: inner)
    problems = preflight.issues()
    assert any("INSIDE another git repo" in p and "cp -r" in p for p in problems)


def test_project_at_its_own_git_root_is_clean(monkeypatch, tmp_path):
    root = tmp_path / "proj"
    root.mkdir()
    (root / "foldyard.toml").write_text('[project]\nname = "proj"\n')
    (root / ".git").mkdir()
    _wire(monkeypatch)
    monkeypatch.setattr(preflight.config, "repo_root", lambda: root)
    assert preflight.issues() == []


def test_project_as_a_linked_worktree_is_clean(monkeypatch, tmp_path):
    # A linked git worktree has a .git FILE, not a dir — still the project's own checkout.
    root = tmp_path / "wt"
    root.mkdir()
    (root / "foldyard.toml").write_text('[project]\nname = "proj"\n')
    (root / ".git").write_text("gitdir: /somewhere/.git/worktrees/wt\n")
    (tmp_path / ".git").mkdir()  # an enclosing repo above — must NOT trip the check
    _wire(monkeypatch)
    monkeypatch.setattr(preflight.config, "repo_root", lambda: root)
    assert preflight.issues() == []


def test_project_outside_any_git_repo_passes_preflight(monkeypatch, tmp_path):
    # Not-a-repo is stack.main_repo's own (later, louder) failure — the nested trap stays quiet.
    root = tmp_path / "proj"
    root.mkdir()
    (root / "foldyard.toml").write_text('[project]\nname = "proj"\n')
    _wire(monkeypatch)
    monkeypatch.setattr(preflight.config, "repo_root", lambda: root)
    assert preflight.issues() == []


def test_proxy_required_includes_generic_inject_axes(monkeypatch):
    # An [[inject]]-only project (no [proxy], no keyless, no github) still routes through the
    # proxy — its derive_env sets FY_PROXY off any non-empty proxy_rules. Preflight must mirror
    # that, or it skips the mitmproxy-missing check (cryptic PyPI-refuse at bootstrap).
    _wire(monkeypatch, mitmdump=None)
    from foldyard import plugins

    class _Reg:
        def proxy_rules(self, mode):
            return [object()]  # one active injection rule

    monkeypatch.setattr(plugins, "registry", lambda: _Reg())
    assert any("mitmproxy" in p and "[host] extra" in p for p in preflight.issues())


def test_inject_only_with_wall_is_not_wrongly_aborted(monkeypatch):
    # The mirror's other half: inject-only + wall must NOT trip 'nothing routes through the
    # proxy' (the old hand-rolled _proxy_required missed [[inject]] and aborted a valid config).
    _wire(monkeypatch, backend="lima", wall=True)
    from foldyard import plugins

    class _Reg:
        def proxy_rules(self, mode):
            return [object()]

    monkeypatch.setattr(plugins, "registry", lambda: _Reg())
    assert not any("NO way out" in p for p in preflight.issues())


def test_worktree_offset_over_89_blocks(monkeypatch):
    _wire(monkeypatch)
    monkeypatch.setattr(preflight.config, "active_worktree", lambda: "feat")
    monkeypatch.setattr(preflight.config, "worktree_offset", lambda name: 95)
    assert any("outside the required 0..89" in p for p in preflight.issues())


def test_worktree_offset_in_range_is_clean(monkeypatch):
    _wire(monkeypatch)
    monkeypatch.setattr(preflight.config, "active_worktree", lambda: "feat")
    monkeypatch.setattr(preflight.config, "worktree_offset", lambda name: 42)
    assert not any("0..89" in p for p in preflight.issues())


def test_check_or_abort_is_a_noop_in_the_box(monkeypatch):
    # In the box these host concerns don't apply — never abort, even with a "problem" configured.
    _wire(monkeypatch, backend_available=False, in_box=True)
    preflight.check_or_abort("fy up")  # must not raise


def test_check_or_abort_raises_on_a_problem(monkeypatch, capsys):
    _wire(monkeypatch, backend="lima", backend_available=False)
    with pytest.raises(SystemExit):
        preflight.check_or_abort("fy up")
    assert "prerequisites not met" in capsys.readouterr().err


@pytest.mark.parametrize("verb", ["fy up", "fy box up"])
def test_inherited_lima_without_limactl_aborts_both_launch_verbs(monkeypatch, capsys, verb):
    # The regression: BOTH launch verbs gate here before machine/engine resolution, so neither can
    # proceed to an unset DOCKER_HOST/CONTAINER_HOST and the host's podman socket. (`fy up` wires
    # this in stack.up, `fy box up` in box.main — each pinned by its own module's test.)
    _wire(monkeypatch, backend="lima", backend_available=False, explicit=False)
    with pytest.raises(SystemExit):
        preflight.check_or_abort(verb)
    err = capsys.readouterr().err
    assert verb in err and "limactl" in err and 'backend = "native"' in err
