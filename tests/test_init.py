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
    assert doc["machine"]["wall"] is True
    assert doc["machine"]["name"] == "acme-app"
    assert doc["machine"]["cpus"] == init.DEFAULT_CPUS
    assert "proxy" in doc  # [proxy] declared so the wall has a way out
    assert doc["proxy"]["default_deny"] is True  # enforcing from the first run


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


def test_template_enforces_the_wall_and_recommends_the_bootstrap_hosts():
    """The starter is ENFORCING from day one (`default_deny = true`), which is only survivable
    because it also seeds `[proxy] recommend` with the box-bootstrap hosts: one round of consented
    per-host yeses and the box builds walled. The entries are OFFERS the launch verbs surface,
    never grants — so the two must ship together."""
    doc = _parse(init.InitOptions(name="x"))
    assert doc["proxy"]["default_deny"] is True
    entries = {e["host"]: e["why"] for e in doc["proxy"]["recommend"]}
    assert "pypi.org" in entries and "github.com" in entries
    assert all(why for why in entries.values())  # every recommendation carries its why
