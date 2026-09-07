"""Anti-rot for the agent-facing surfaces: the bundled skills, the `fy init` scaffold, and
`fy docs`.

These three TEACH — a skill an agent loads before acting, a template a consumer copies verbatim,
a manual shipped in the wheel. Nothing fails when they drift from the CLI; they just quietly
instruct people to run commands that don't exist or set keys nothing reads. That already happened
twice: `fy init` kept emitting `[proxy] allow` for a release after grants moved host-side, and a
consumer config carried seven inert entries under `default_deny` as a result. So the guards are
mechanical rather than editorial.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

import pytest
import typer

from foldyard import cli, docs, exposure, init, skills

# Commands are only recognised inside a CODE SPAN. Matching prose too would mean maintaining a
# stop-list of English words that follow "foldyard" ("foldyard runs…", "the foldyard project"),
# which is a lint on writing style rather than on correctness — and a stop-list is exactly where a
# real typo would end up being excused.
_SPAN = re.compile(r"`([^`\n]+)`")
_VERB = re.compile(r"^[a-z][a-z-]*$")


def _command_names(app: typer.Typer, prefix: str = "") -> set[str]:
    """Every command path the CLI actually exposes ("mode", "config adopt", …), derived from the
    live typer app so a renamed verb fails this test instead of a user's terminal."""
    names: set[str] = set()
    for command in app.registered_commands:
        callback = command.callback
        name = command.name or (getattr(callback, "__name__", "").replace("_", "-"))
        if name:
            names.add(f"{prefix}{name}")
    for group in app.registered_groups:
        sub = group.typer_instance
        if sub is None:
            continue
        name = group.name or sub.info.name or ""
        if not name:
            continue
        names.add(f"{prefix}{name}")
        names |= _command_names(sub, prefix=f"{prefix}{name} ")
    return names


VALID = _command_names(cli.app)


def _mentions(text: str) -> set[str]:
    """The command paths a document tells the reader to run: every code span starting with
    ``fy``/``foldyard``, including each alternative in a `fy up | ps | logs` style list."""
    found: set[str] = set()
    for span in _SPAN.findall(text):
        segments = span.strip().split("|")
        head = segments[0].split()
        if not head or head[0] not in ("fy", "foldyard"):
            continue
        words = [w for w in head[1:] if _VERB.match(w)]
        if words:
            # Prefer the two-word reading when it IS a subcommand ("config adopt"), else the verb.
            pair = " ".join(words[:2])
            found.add(pair if len(words) > 1 and pair in VALID else words[0])
        if "=" in segments[0]:
            continue  # `fy mode capture=on|off` — that pipe is inside an ARGUMENT, not a verb list
        for segment in segments[1:]:  # `fy up | ps | down` — each alternative names a verb too
            rest = segment.split()
            if rest and _VERB.match(rest[0]):
                found.add(rest[0])
    return found


def _skill_docs() -> list[Path]:
    root = Path(skills.__file__).resolve().parent / "assets" / "skills"
    return sorted(root.rglob("*.md"))


def test_the_cli_surface_was_discovered_at_all():
    """A guard on the guard: if typer's internals change shape, `_command_names` could quietly
    return nothing and every assertion below would pass vacuously."""
    assert {"up", "docs", "mode", "config adopt", "allow add", "box shell"} <= VALID


@pytest.mark.parametrize("path", _skill_docs(), ids=lambda p: f"{p.parent.name}/{p.name}")
def test_bundled_skills_only_name_commands_that_exist(path):
    unknown = sorted(m for m in _mentions(path.read_text()) if m not in VALID)
    assert not unknown, f"{path.name} tells the reader to run: {unknown}"


def test_the_init_scaffold_only_names_commands_that_exist():
    """The template is copied verbatim into every new consumer — including into a `system_prompt`
    that an agent then treats as fact."""
    text = init.render(init.InitOptions(name="demo"))
    unknown = sorted(m for m in _mentions(text) if m not in VALID)
    assert not unknown, f"the foldyard.toml scaffold names: {unknown}"


def test_the_local_overrides_template_only_names_commands_that_exist():
    """`foldyard.local.toml.example` ships beside the scaffold and is copied just as verbatim —
    and it's the file that talks about credentials, so a stale `fy` verb in it strands someone
    mid-setup."""
    text = init.render_local(init.InitOptions(name="demo"))
    unknown = sorted(m for m in _mentions(text) if m not in VALID)
    assert not unknown, f"the foldyard.local.toml.example template names: {unknown}"


def test_the_init_scaffold_never_teaches_a_key_foldyard_stopped_honouring():
    """The `[proxy] allow` regression, mechanised: `exposure.IGNORED_KEYS` is the list of keys we
    deleted, so the scaffold must not mention any of them — not even commented out, since the
    template is a menu of things to uncomment."""
    text = init.render(init.InitOptions(name="demo"))
    for dotted in exposure.IGNORED_KEYS:
        table, key = dotted.split(".", 1)
        assert not re.search(rf"^#?\s*{key}\s*=", text, re.MULTILINE), (
            f"the scaffold offers `{key}` ({table}), which foldyard no longer reads"
        )


def test_the_init_scaffold_is_valid_toml_and_declares_the_proxy():
    doc = tomllib.loads(init.render(init.InitOptions(name="demo")))
    assert doc["proxy"]["default_deny"] is True  # enforcing, carried by `recommend` (test_init)
    assert "allow" not in doc["proxy"]  # grants are host-side (`fy allow add`), never config


def test_init_installs_the_orientation_skill(tmp_path, capsys):
    """A fresh consumer gets the agent guide with the scaffold — the whole point is that an agent
    learns what it can't do BEFORE it burns a session finding out."""
    assert init.init(path=str(tmp_path), name="demo") == 0

    skill = tmp_path / ".claude" / "skills" / "foldyard" / "SKILL.md"
    assert skill.is_file()
    assert (skill.parent / "references").is_dir()
    assert "cannot push" in skill.read_text().lower()


def test_init_never_clobbers_a_consumers_own_skill(tmp_path):
    mine = tmp_path / ".claude" / "skills" / "foldyard"
    mine.mkdir(parents=True)
    (mine / "SKILL.md").write_text("my own notes")

    init.init(path=str(tmp_path), name="demo")
    assert (mine / "SKILL.md").read_text() == "my own notes"


# ── `fy docs` ─────────────────────────────────────────────────────────────────────────


def test_docs_topics_resolve_by_prefix_and_ambiguity_is_not_guessed():
    root = docs.docs_dir()
    assert root is not None
    topics = docs._topics(root)

    assert docs._resolve(topics, "network") == topics["networking"]
    assert docs._resolve(topics, "adr-0022") is not None
    assert docs._resolve(topics, "no-such-topic") is None


def test_every_docs_topic_the_skills_cite_is_actually_shipped():
    """A skill that says `fy docs modes` must not be pointing at a page consumers don't get: the
    wheel ships a SUBSET (contributor archaeology stays in the repo), and running from source
    hides that — the topic resolves here and 404s for everyone else."""
    shipped = _packaged_doc_topics()
    cited = set()
    for path in _skill_docs():
        cited |= set(re.findall(r"fy docs ([a-z0-9][a-z0-9-]*)", path.read_text()))
    missing = sorted(t for t in cited if t not in shipped)
    assert not missing, f"skills cite doc topics the wheel doesn't ship: {missing}"


def _packaged_doc_topics() -> set[str]:
    """Topic names the WHEEL will carry, read from pyproject's force-include map (the build's own
    source of truth) rather than from the docs dir on disk."""
    root = Path(docs.__file__).resolve().parents[2]
    build = tomllib.loads((root / "pyproject.toml").read_text())
    mapping = build["tool"]["hatch"]["build"]["targets"]["wheel"]["force-include"]
    topics = set()
    for src in mapping:
        if src.endswith(".md"):
            topics.add(Path(src).stem)
        else:  # a directory — the ADRs, keyed as `adr-NNNN`
            topics |= {f"adr-{p.name.split('-')[0]}" for p in (root / src).glob("[0-9]*.md")}
    return topics
