"""`foldyard init` — the foldyard.toml scaffolder (ADR-0013)."""

from __future__ import annotations

import re
import tomllib

from foldyard import init


def _parse(opts: init.InitOptions) -> dict:
    """render() must always emit valid TOML."""
    return tomllib.loads(init.render(opts))


def test_render_default_is_a_locked_down_stackless_lima_wall_box():
    doc = _parse(init.InitOptions(name="Acme App"))
    assert doc["project"]["name"] == "Acme App"
    assert doc["project"]["prefix"] == "acme-app"  # slugified
    # Stack-less by default: no app/compose/ports active (they're commented, step 3).
    assert "app" not in doc["project"]
    assert "compose" not in doc["project"]
    assert "ports" not in doc
    # The locked-down posture is ACTIVE out of the box: lima + wall + an opted-in proxy.
    assert doc["machine"]["backend"] == "lima"
    assert doc["machine"]["firewall"] is True
    assert doc["machine"]["name"] == "acme-app"
    assert doc["machine"]["cpus"] == init.DEFAULT_CPUS
    assert "proxy" in doc  # [proxy] declared so the wall has a way out
    # Learns on the first launch, then enforces by itself (allowlist.seed_learning).
    assert doc["proxy"]["enforce"] == "learn"


def test_render_shows_the_passthrough_default_it_would_get_and_how_to_widen_it():
    # The scaffold teaches the knob by showing the value an undeclared key falls back to, so the
    # commented line must BE that default — and say how to get every bundle back.
    from foldyard import config

    body = init.render(init.InitOptions(name="x"))
    line = next(ln for ln in body.splitlines() if ln.startswith("# passthrough = "))
    shown = tomllib.loads(line.lstrip("# ").split("#", 1)[0])["passthrough"]
    assert shown == list(config.DEFAULT_PASSTHROUGH)
    assert '["@all"]' in body  # the way back to tunnelling the whole toolchain


def test_render_agents_and_features_are_present_but_commented():
    # The kitchen sink: agents + the rest ship as guidance, not active tables, so a first
    # `fy box up` is a plain safe box until the user opts in.
    doc = _parse(init.InitOptions(name="x"))
    for table in ("claude", "codex", "box", "vscode"):
        assert table not in doc, f"[{table}] must start commented, not active"
    body = init.render(init.InitOptions(name="x"))
    # …but discoverable in the text, with the bootstrap order spelled out.
    for hint in ("# [claude]", "# [codex]", "# [vscode]", "# [ports]", "step 2", "step 3"):
        assert hint in body


def test_render_no_box_image_so_generic_box_is_used():
    # The whole point of the generic box: a scaffolded project declares no active [box].image.
    doc = _parse(init.InitOptions(name="x"))
    assert "box" not in doc


def _uncomment(body: str, start: str, end: str) -> str:
    """The rendered block from `start` to `end` (inclusive) with ONE leading "# " stripped —
    i.e. exactly what a user gets by uncommenting that section. A `# # …` guidance line
    survives as a plain TOML comment, which is the template's own convention."""
    lines = body.splitlines()
    i = lines.index(start)
    j = lines.index(end, i)
    return "\n".join(line[2:] if line.startswith("# ") else line[1:] for line in lines[i : j + 1])


def test_render_box_block_uncomments_into_valid_toml_with_the_shadow_trio():
    # The scaffold's job on this key is to stop the reported failure: the box mounts the
    # checkout, so an in-tree .venv/node_modules is the SAME dir the host builds into and the
    # two clobber each other. shadow_volumes alone doesn't fix it — the volume starts empty
    # (needs warmup) and lives on another filesystem from the cache (needs the link mode), so
    # all three must be scaffolded together or the user gets a half-working box.
    body = init.render(init.InitOptions(name="x"))
    end = '# caches = [{ volume = "x-uv-cache", path = ".cache/uv" }]   # path is under HOME'
    doc = tomllib.loads(_uncomment(body, "# [box]", end))
    assert doc["box"]["shadow_volumes"] == [".venv"]
    assert doc["box"]["warmup"] == [{"dir": ".", "run": "uv sync --frozen"}]
    assert doc["box"]["env"] == {"UV_LINK_MODE": "copy"}
    assert doc["box"]["caches"] == [{"volume": "x-uv-cache", "path": ".cache/uv"}]
    # …and [[box.tools]] stays AFTER the plain keys, or uncommenting the lot is a TOML error.
    assert body.index("# env = {") < body.index("# [[box.tools]]")


def test_render_claude_system_prompt_is_a_working_box_only_prompt():
    # It used to end "Edit me." — a stub that shipped as the default for anyone who just
    # uncommented [claude]. It must now stand on its own: valid TOML when uncommented, and
    # covering the facts an unoriented agent otherwise gets WRONG about the box.
    body = init.render(init.InitOptions(name="x"))
    doc = tomllib.loads(_uncomment(body, "# [claude]", '# """'))
    prompt = doc["claude"]["system_prompt"]
    assert "Edit me" not in prompt
    for fact in (
        "FULL engine access",  # the preset DOCKER_HOST is not a locked-down shell
        "ROOTLESS",  # …what actually bounds it
        "fy verify",  # …and how to prove that
        "CANNOT push",  # no origin credential
        "fy allow add",  # blocked egress → ask the operator, don't route around
        "attach to this one box",  # sessions can share the box + checkout
    ):
        assert fact in prompt, f"the scaffolded prompt no longer orients the agent on: {fact}"


def test_render_claude_prompt_is_box_only_with_the_stack_half_parked_for_step_3():
    # The prompt follows the file's own progressive opt-in: [claude] lands at step 2, when
    # there may be no stack at all, so the ACTIVE prompt must claim nothing about one — a
    # box-only project told to `fy up` is being lied to. The stack half is parked as
    # commented guidance to paste at step 3, and [project].compose points at it.
    body = init.render(init.InitOptions(name="x"))
    prompt = tomllib.loads(_uncomment(body, "# [claude]", '# """'))["claude"]["system_prompt"]
    for stack_only in ("fy up", "compose stack", "CONTAINER NAME", "localhost"):
        assert stack_only not in prompt, f"box-only prompt must not assume a stack: {stack_only}"
    # …and the parked paragraph is present, as comments, with the pointer from the opt-in site.
    for line in (
        "# # This project also drives a compose stack.",
        "# # CONTAINER NAME, not localhost",
    ):
        assert line in body, f"the step-3 stack paragraph is missing: {line}"
    assert "extend [claude].system_prompt" in body


def test_render_prompt_says_host_not_mac():
    # Windows/Linux hosts are in scope, so the scaffolded text must not hard-code macOS. The
    # word also can't leak back via the surrounding guidance the user reads alongside it.
    body = init.render(init.InitOptions(name="x"))
    assert "Mac" not in body


def test_render_honours_sizing_overrides():
    doc = _parse(init.InitOptions(name="big", cpus=12, memory_mib=64000, disk_gib=200))
    assert doc["machine"]["backend"] == "lima"
    assert doc["machine"]["cpus"] == 12
    assert doc["machine"]["memory_mib"] == 64000
    assert doc["machine"]["disk_gib"] == 200


def test_render_memory_is_a_plain_int_not_a_rounded_gib():
    # The sizing-bug guard at the scaffold layer: memory_mib must be a bare integer (any whole
    # MiB), never a GiB string — the value Virtualization.framework would reject.
    doc = _parse(init.InitOptions(name="x", memory_mib=4000))
    assert doc["machine"]["memory_mib"] == 4000


def test_render_stamps_the_scaffolding_fy_as_the_floor():
    # The scaffold declares a version window from the start: the fy that wrote the file is the
    # only version it is known to be right for, and a floor nobody wrote is a floor of zero.
    doc = _parse(init.InitOptions(name="x", version="1.4.2"))
    assert doc["project"]["min_foldyard_version"] == "1.4.2"
    # The soft half + the ledger ship COMMENTED — a repo on day one has no history with the tool.
    assert "recommended_foldyard_version" not in doc["project"]
    assert "foldyard_version_reasons" not in doc["project"]
    body = init.render(init.InitOptions(name="x", version="1.4.2"))
    assert '# recommended_foldyard_version = "1.4.2"' in body
    assert "# [project.foldyard_version_reasons]" in body


def test_render_commented_version_window_uncomments_into_valid_toml():
    # Uncommenting is how this file is meant to GROW, so what it parks must parse — and must not
    # be inert once uncommented (an unparseable version is a key foldyard silently has no opinion
    # on, which is the exact failure the floor above exists to prevent).
    body = init.render(init.InitOptions(name="x", version="1.4.2"))
    lines = []
    for raw in body.splitlines():
        stripped = raw.lstrip("# ")
        if stripped.startswith(("recommended_foldyard_version", "[project.foldyard_version")) or (
            lines and re.match(r'"[\d.]+" = ', stripped)
        ):
            lines.append(stripped)
    doc = tomllib.loads("[project]\n" + "\n".join(lines))
    assert doc["project"]["recommended_foldyard_version"] == "1.4.2"
    assert doc["project"]["foldyard_version_reasons"] == {
        "1.4.2": "the version this project started on"
    }


def test_render_leaves_the_floor_commented_when_fy_cant_name_its_own_version():
    # `0+unknown` (a bare source-tree import) and any local build are versions compat REFUSES to
    # order. Stamping one reads as protection and is none, so the key is parked instead.
    body = init.render(init.InitOptions(name="x", version="0+unknown"))
    assert (
        "min_foldyard_version"
        not in _parse(init.InitOptions(name="x", version="0+unknown"))["project"]
    )
    assert "could not tell its own version" in body
    assert '# min_foldyard_version = "0.1.0"' in body
    # Every EXAMPLE line is meant to be uncommented verbatim, so none of them may carry the
    # unorderable string itself — compat has no opinion on `0+unknown`, which makes the key
    # silently inert exactly where this file is trying to prevent silence.
    assert "0+unknown" not in body.replace("it reported '0+unknown'", "")
    assert '# "0.1.0" = "the version this project started on"' in body


def test_init_stamps_the_running_fy_and_says_so(tmp_path, capsys):
    # End to end: the default InitOptions.version is the fy actually running, and the write is
    # REPORTED — a floor that appears in the file without being mentioned is a surprise the first
    # time it refuses someone. Branching rather than skipping on an unorderable version, so this
    # keeps asserting something in a bare source tree (where __version__ is "0+unknown").
    from foldyard import __version__, compat

    assert init.init(path=str(tmp_path), name="fresh") == 0
    project = tomllib.loads((tmp_path / "foldyard.toml").read_text())["project"]
    out = capsys.readouterr().out
    if compat._parse(__version__) is not None:
        assert project["min_foldyard_version"] == __version__
        assert f"floor: foldyard >= {__version__}" in out
    else:
        assert "min_foldyard_version" not in project
        assert "floor:" not in out


def test_init_writes_file_and_defaults_name_to_dir(tmp_path):
    proj = tmp_path / "my-repo"
    rc = init.init(path=str(proj))
    assert rc == 0
    written = (proj / "foldyard.toml").read_text()
    assert tomllib.loads(written)["project"]["name"] == "my-repo"


def test_init_refuses_existing_without_force(tmp_path, capsys):
    (tmp_path / "foldyard.toml").write_text("# pre-existing\n")
    assert init.init(path=str(tmp_path)) == 1
    assert "already exists" in capsys.readouterr().out
    # untouched
    assert (tmp_path / "foldyard.toml").read_text() == "# pre-existing\n"


def test_init_force_overwrites(tmp_path):
    (tmp_path / "foldyard.toml").write_text("# old\n")
    assert init.init(path=str(tmp_path), name="fresh", force=True) == 0
    assert tomllib.loads((tmp_path / "foldyard.toml").read_text())["project"]["name"] == "fresh"


def test_slug_sanitises():
    assert init._slug("My Project!! 2") == "my-project-2"
    assert init._slug("---") == "project"  # never empty


def test_render_local_example_is_valid_toml_and_wholly_commented():
    # Copying the template must change NOTHING until a line is uncommented — the same contract the
    # main scaffold keeps. An accidentally-active `keyless` here would prompt every copier for a
    # credential they never asked for.
    body = init.render_local(init.InitOptions(name="x"))
    assert tomllib.loads(body) == {}


def test_render_local_example_teaches_the_opt_out_and_the_personal_half():
    body = init.render_local(init.InitOptions(name="x"))
    # The subtraction mechanism, with the case it exists for (a committed [codex] you don't want).
    assert "disabled = true" in body
    assert "# [codex]" in body
    # …and the things that belong here BECAUSE they're per-developer, not per-project.
    for personal in ("[claude]", "[machine]", "[worktree-offsets]", "[[inject]]"):
        assert f"# {personal}" in body, f"the overrides template no longer offers {personal}"
    # It's pinned like foldyard.toml, so an edit is inert until adopted — say so where it's edited.
    assert "fy config adopt" in body


def test_render_local_example_never_teaches_a_key_foldyard_stopped_honouring():
    # Same guard as the main scaffold: this template is a menu of things to uncomment, so a key
    # foldyard deleted must not appear in it either.
    body = init.render_local(init.InitOptions(name="x"))
    for dotted in ("proxy.allow", "inject.minter", "inject.token_env"):
        key = dotted.split(".", 1)[1]
        assert not re.search(rf"^#?\s*{key}\s*=", body, re.MULTILINE), (
            f"the overrides template offers `{key}`, which foldyard no longer reads"
        )


def test_init_writes_the_local_overrides_template_and_ignores_the_copy(tmp_path):
    # The template is COMMITTED; the copy it's a template for is not. Shipping only one of the two
    # is how a repo ends up with a gitignored file nobody knows exists — or, worse, with someone's
    # personal keyless posture committed and imposed on the team.
    assert init.init(path=str(tmp_path), name="demo") == 0
    assert (tmp_path / init.LOCAL_EXAMPLE).is_file()
    assert "**/foldyard.local.toml" in (tmp_path / ".gitignore").read_text()
    assert init.LOCAL_EXAMPLE not in (tmp_path / ".gitignore").read_text()


def test_init_never_clobbers_an_edited_local_example_without_force(tmp_path):
    (tmp_path / init.LOCAL_EXAMPLE).write_text("# our own menu\n")
    assert init.init(path=str(tmp_path), name="demo") == 0
    assert (tmp_path / init.LOCAL_EXAMPLE).read_text() == "# our own menu\n"
    # --force is the explicit "give me the current template back".
    assert init.init(path=str(tmp_path), name="demo", force=True) == 0
    assert "disabled = true" in (tmp_path / init.LOCAL_EXAMPLE).read_text()


def test_init_creates_gitignore_with_generated_artifacts(tmp_path):
    proj = tmp_path / "fresh-repo"
    assert init.init(path=str(proj)) == 0
    gi = (proj / ".gitignore").read_text()
    for entry in init.GITIGNORE_ENTRIES:
        assert entry in gi
    # the box dirs AND the host-written posture mirror are all covered
    assert ".devbox-ca/" in gi
    assert ".dev-mode.json" in gi
    assert gi.count(init.GITIGNORE_BEGIN) == 1


def test_init_appends_block_preserving_existing_gitignore(tmp_path):
    (tmp_path / ".gitignore").write_text("node_modules/\n.env\n")
    assert init.init(path=str(tmp_path)) == 0
    gi = (tmp_path / ".gitignore").read_text()
    # existing rules untouched…
    assert "node_modules/" in gi
    assert ".env" in gi
    # …and the managed block appended once, separated by a blank line.
    assert init.GITIGNORE_BEGIN in gi
    assert ".env\n\n" + init.GITIGNORE_BEGIN in gi


def test_init_gitignore_is_idempotent_across_force_reruns(tmp_path):
    assert init.init(path=str(tmp_path)) == 0
    first = (tmp_path / ".gitignore").read_text()
    assert init.init(path=str(tmp_path), force=True) == 0
    second = (tmp_path / ".gitignore").read_text()
    # No duplicated block, and the content is stable.
    assert second == first
    assert second.count(init.GITIGNORE_BEGIN) == 1


def test_update_gitignore_refreshes_managed_block_in_place(tmp_path):
    # A drifted managed block (e.g. an old dir list) is replaced, not duplicated.
    stale = init.GITIGNORE_BEGIN + "\n.devbox-old/\n" + init.GITIGNORE_END + "\n"
    (tmp_path / ".gitignore").write_text("keep-me/\n" + stale)
    status = init._update_gitignore(tmp_path)
    assert status == "updated"
    gi = (tmp_path / ".gitignore").read_text()
    assert "keep-me/" in gi
    assert ".devbox-old/" not in gi
    assert gi.count(init.GITIGNORE_BEGIN) == 1
    assert ".devbox-ca/" in gi
    # A second run is a no-op.
    assert init._update_gitignore(tmp_path) == "unchanged"


def test_template_learns_then_enforces_and_recommends_the_bootstrap_hosts():
    """The starter LEARNS for its first launch window and then ENFORCES by itself
    (`enforce = "learn"`) — so setup isn't a wall of refusals, and the wall can't be left
    open by mistake either. It still seeds `[proxy] recommend` with the box-bootstrap hosts:
    they're the team-shared half, OFFERED per host at the launch verbs, never grants."""
    from foldyard import config

    doc = _parse(init.InitOptions(name="x"))
    assert doc["proxy"]["enforce"] == "learn"
    with config.using(config.Config(repo_root=config.repo_root(), worktree="", toml=doc)):
        assert config.proxy_default_deny_seed() == "learn"
        assert config.proxy_default_deny() is True  # enforces until the window opens
    entries = {e["host"]: e["why"] for e in doc["proxy"]["recommend"]}
    assert "pypi.org" in entries and "github.com" in entries
    assert all(why for why in entries.values())  # every recommendation carries its why


# ── what the scaffold teaches must match what fy does today ───────────────────────────────


def _both(opts: init.InitOptions | None = None) -> str:
    opts = opts or init.InitOptions(name="x")
    return init.render(opts) + init.render_local(opts)


def test_every_experimental_setting_the_scaffold_offers_says_so():
    # The register (experimental.py) is the one list; docs/configuration.md is held to it by
    # test_experimental.py, and the two scaffolds are held to it here: a line offering an
    # experimental value must say so on that line or the comment line after it.
    from foldyard import experimental

    lines = _both().splitlines()
    for feature in experimental.REGISTER:
        field = feature.key.split(".", 1)[1]
        offer = re.compile(rf'^#?\s*{re.escape(field)}\s*=\s*"{re.escape(feature.value)}"')
        for i, line in enumerate(lines):
            if offer.search(line):
                said = " ".join(lines[i : i + 2]).lower()
                assert "experimental" in said, f"the scaffold offers {feature.label} untagged"


def test_step_two_recreates_the_box_because_an_agent_is_installed_at_create():
    # `fy box up` on a running box only says "already up": the agent install and its dummy are
    # create-time, and the adopt prompt defaults to ignore. The sequence the quickstart teaches:
    header = init.render(init.InitOptions(name="x")).split("[project]", 1)[0]
    assert "+ `fy box up` again" not in header
    for step in ("fy config adopt", "fy box down && fy box up", "fy mode claude=on"):
        assert step in header, f"step 2 no longer says `{step}`"


def test_keyless_says_where_it_belongs_on_a_team():
    # Which subscription you pay for is personal; a shared `keyless` prompts every colleague.
    body = init.render(init.InitOptions(name="x"))
    for table in ("# [claude]", "# [codex]"):
        i = body.index(f"\n{table}\n")
        block = body[i - 300 : i + 400]  # the table's intro comment, or the lines under it
        assert "foldyard.local.toml" in block, f"{table}'s keyless doesn't say where a team puts it"


def test_host_firewall_warns_it_refuses_colleagues_who_cant_enforce_it():
    body = init.render(init.InitOptions(name="x"))
    i = body.index("#   host_firewall = true")
    assert "every developer" in body[i : i + 400]


def test_render_github_block_uncomments_into_the_app_row_and_your_own_emergency_row():
    body = init.render(init.InitOptions(name="x"))
    start = body.index("# [[inject]]")
    block = body[start : body.index("\n\n", start)]  # the two rows, up to the section's end
    doc = tomllib.loads(
        "\n".join(line.removeprefix("#").removeprefix(" ") for line in block.splitlines())
    )
    app, user = doc["inject"]
    assert (app["switch"], app["kind"]) == ("github", "github-app")
    assert (user["switch"], user["kind"], user["emergency"]) == ("github-user", "gh-cli", True)
    assert app["box_env"] == user["box_env"] == {"GH_TOKEN": "x"}
    # The packaged box has no `gh`, so the dummy does nothing without it; and the rows are
    # create-time, like every box setting.
    block = body[body.index("── GitHub") : body.index("# [[inject]]")]
    assert 'packages = ["gh"]' in block
    assert "fy docs github-app" in block


def test_render_proxy_offers_no_proxy_for_the_stacks_service_names():
    # The parked step-3 prompt tells the agent to reach services by container name; under the
    # proxy those calls hang and 502 unless the names are listed.
    body = init.render(init.InitOptions(name="x"))
    line = next(ln for ln in body.splitlines() if ln.startswith("# no_proxy = "))
    assert tomllib.loads(line.lstrip("# ").split("#", 1)[0])["no_proxy"]


def test_render_project_comment_offers_worktree_init():
    body = init.render(init.InitOptions(name="x"))
    line = next(ln for ln in body.splitlines() if ln.startswith("#   worktree_init = "))
    assert tomllib.loads(line[1:].strip().split("#", 1)[0])["worktree_init"]


def test_local_private_injector_has_a_dummy_and_no_false_allowlist_step():
    body = init.render_local(init.InitOptions(name="x"))
    section = body[body.index("── A private injector") :]
    doc = tomllib.loads(_uncomment(section, "# [[inject]]", section.rstrip("\n").splitlines()[-1]))
    assert doc["inject"][0]["box_env"], "without a dummy the box has nothing for the proxy to swap"
    # An injector's host is allowed while its switch is on; telling people to grant it is wrong.
    assert "fy allow add" not in section
    # …and a local [[inject]] REPLACES the shared array, so it must say so where it's offered.
    assert "WHOLESALE" in section or "wholesale" in section


def test_local_example_offers_the_personal_settings_the_docs_call_personal():
    body = init.render_local(init.InitOptions(name="x"))
    for hint in ("# [claude.settings]", "# vmtype = ", "# [host]", "# notifications = false"):
        assert hint in body, f"the overrides template no longer offers {hint!r}"


def test_init_points_at_fy_prereq_for_what_the_computer_needs(tmp_path, capsys):
    init.init(path=str(tmp_path), name="x")
    out = capsys.readouterr().out
    assert "fy prereq" in out
    assert "brew install lima" not in out


# ── `fy init --with <block>` ─────────────────────────────────────────────────────────────────
# A block named on the command line ships ACTIVE instead of commented — exactly what uncommenting
# it gives — so the first `fy box up` creates the box with it (an agent and the vscode-server
# volume are create-time) instead of a later adopt + recreate.


def _with(*blocks: str) -> init.InitOptions:
    return init.InitOptions(name="x", blocks=frozenset(blocks))


def _scaffolded_prompt() -> str:
    body = init.render(init.InitOptions(name="x"))
    return tomllib.loads(_uncomment(body, "# [claude]", '# """'))["claude"]["system_prompt"]


def test_with_claude_is_the_claude_block_uncommented():
    doc = _parse(_with("claude"))
    assert doc["claude"]["keyless"] == "oauth"
    assert doc["claude"]["system_prompt"] == _scaffolded_prompt()
    assert "settings" not in doc["claude"]  # [claude.settings] is a preference, left commented
    assert "codex" not in doc and "vscode" not in doc


def test_with_codex_carries_the_real_prompt_not_the_placeholder():
    # The commented [codex] says `system_prompt = """…"""   # paste [claude]'s` — an ACTIVE "…"
    # would orient the agent with an ellipsis.
    doc = _parse(_with("codex"))
    assert doc["codex"]["keyless"] == "api-key"
    assert "…" not in doc["codex"]["system_prompt"]
    assert "config" not in doc["codex"]
    assert "claude" not in doc


def test_with_codex_prompt_names_its_own_launcher_and_a_skill_it_can_read():
    # [claude]'s prompt word for word would tell a Codex session it was started by `fy claude`
    # and send it to a skill directory only Claude Code discovers. Everything else is shared.
    claude = _scaffolded_prompt().splitlines()
    codex = _parse(_with("codex"))["codex"]["system_prompt"].splitlines()
    assert len(codex) == len(claude)
    changed = [(c, x) for c, x in zip(claude, codex, strict=True) if c != x]
    assert len(changed) == 2
    assert "`fy codex`" in changed[0][1] and "`fy claude`" not in changed[0][1]
    assert ".claude/skills/foldyard/SKILL.md" in changed[1][1]
    assert "`fy claude`" not in "\n".join(codex)


def test_with_vscode_is_the_vscode_block_uncommented():
    doc = _parse(_with("vscode"))
    assert doc["vscode"]["extensions"] == ["anthropic.claude-code"]
    assert doc["vscode"]["settings"] == {"remote.autoForwardPorts": False}
    assert "claude" not in doc and "codex" not in doc


def test_with_every_block_at_once_is_valid_toml_and_keeps_the_rest_commented():
    doc = _parse(_with(*init.BLOCKS))
    assert {"claude", "codex", "vscode"} <= doc.keys()
    for table in ("box", "ports", "inject"):
        assert table not in doc


def test_with_only_strips_its_own_blocks_comment_level():
    # Everything outside the named block is byte-for-byte the default scaffold.
    default = init.render(init.InitOptions(name="x")).splitlines()
    active = init.render(_with("vscode")).splitlines()
    assert len(active) == len(default)
    changed = [(d, a) for d, a in zip(default, active, strict=True) if d != a]
    assert changed and all(d.removeprefix("# ") == a for d, a in changed)


def test_with_an_agent_step_two_no_longer_says_uncomment_and_recreate():
    header = init.render(_with("claude")).split("[project]", 1)[0]
    assert "Uncomment ONE agent" not in header
    assert "fy box down && fy box up" not in header.split("#   3.", 1)[0]
    assert "fy mode claude=on" in header and "fy claude" in header
    both = init.render(_with("claude", "codex")).split("[project]", 1)[0]
    assert "fy mode claude=on" in both and "fy mode codex=on" in both
    # …and a non-agent block leaves step 2 as the default teaches it.
    assert "Uncomment ONE agent" in init.render(_with("vscode"))


def test_init_with_accepts_repeats_and_commas_and_says_what_it_declared(tmp_path, capsys):
    assert init.init(path=str(tmp_path), name="x", blocks=["claude,vscode", "vscode"]) == 0
    doc = tomllib.loads((tmp_path / "foldyard.toml").read_text())
    assert "claude" in doc and "vscode" in doc and "codex" not in doc
    assert "[claude] [vscode]" in capsys.readouterr().out


def test_init_with_an_unknown_block_refuses_and_writes_nothing(tmp_path, capsys):
    assert init.init(path=str(tmp_path), name="x", blocks=["claude", "box"]) == 1
    out = capsys.readouterr().out
    assert "'box'" in out and "claude, codex, vscode" in out
    assert not (tmp_path / "foldyard.toml").exists()


def test_cli_init_with_reaches_the_scaffold(tmp_path):
    from typer.testing import CliRunner

    from foldyard import cli

    result = CliRunner().invoke(cli.app, ["init", str(tmp_path), "--with", "vscode"])
    assert result.exit_code == 0, result.output
    assert "vscode" in tomllib.loads((tmp_path / "foldyard.toml").read_text())
