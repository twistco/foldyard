"""composeguard — compose paths that would make the HOST-side compose client read outside the checkout.

The compose files are box-writable, and the client resolves `env_file:`, build contexts, Dockerfiles,
`include:`/`extends:` files and secrets/configs files on the operator's computer, handing their
content to containers or images the box can read. Each form a key takes is a separate way out, so
each gets a case. Mutations that must turn these red: `_inside` comparing unresolved paths (the
symlink cases); dropping any one key from `_service_paths`/`_top_level_paths`; letting `$` through
in an included/extended file (`literal_only`); not following `include`/`extends` into the raw scan.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from foldyard import composeguard


@pytest.fixture
def tree(tmp_path):
    """A checkout, a sibling "host" dir holding a secret, and a helper to write files."""
    repo = tmp_path / "repo"
    (repo / "api").mkdir(parents=True)
    host = tmp_path / "host"
    host.mkdir()
    (host / "creds.env").write_text("AWS_SECRET_ACCESS_KEY=s3cret\n")
    (repo / "api" / ".env").write_text("IN=1\n")

    def write(rel: str, text: str) -> Path:
        p = repo / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
        return p

    return repo, host, write


def rendered(repo: Path, doc: dict, roots=None) -> list[str]:
    return composeguard.rendered_problems(doc, repo, roots or [repo])


# ── the rendered config (compose's own interpolation) ─────────────────────────────────────


def test_an_in_checkout_stack_passes(tree):
    repo, _host, _write = tree
    doc = {
        "services": {
            "api": {
                "env_file": ["./api/.env", {"path": "api/.env", "required": False}],
                "build": {
                    "context": str(repo / "api"),
                    "dockerfile": "Dockerfile",
                    "additional_contexts": {"shared": "./api", "base": "docker-image://alpine:3"},
                },
            }
        },
        "secrets": {"s": {"file": "./api/.env"}},
    }
    assert rendered(repo, doc) == []


@pytest.mark.parametrize(
    "service",
    [
        {"env_file": "../host/creds.env"},
        {"env_file": ["./api/.env", "../host/creds.env"]},
        {"env_file": [{"path": "../host/creds.env"}]},
        {"label_file": "../host/creds.env"},
        {"build": "../host"},
        {"build": {"context": "../host"}},
        {"build": {"context": ".", "dockerfile": "../host/creds.env"}},
        {"build": {"context": ".", "additional_contexts": {"x": "../host"}}},
        {"build": {"context": ".", "additional_contexts": ["x=../host"]}},
        {"build": {"context": ".", "ssh": ["id=../host/creds.env"]}},
        {"build": {"context": ".", "ssh": {"id": "../host/creds.env"}}},
        {"extends": {"file": "../host/base.yml", "service": "b"}},
    ],
)
def test_a_service_path_outside_the_checkout_is_refused(tree, service):
    repo, _host, _write = tree
    problems = rendered(repo, {"services": {"api": service}})
    assert len(problems) == 1 and "outside the checkout" in problems[0]
    assert "api" in problems[0]  # names the service, so the operator can find it


@pytest.mark.parametrize("table", ["secrets", "configs"])
def test_a_secret_or_config_file_outside_the_checkout_is_refused(tree, table):
    repo, _host, _write = tree
    problems = rendered(repo, {table: {"s": {"file": "../host/creds.env"}}})
    assert len(problems) == 1 and f"{table}.s.file" in problems[0]


@pytest.mark.parametrize(
    "context", ["https://github.com/x/y.git", "git@github.com:x/y.git", "github.com/x/y.git#main"]
)
def test_a_remote_build_context_is_refused(tree, context):
    # The client fetches it host-side (with whatever credentials git finds there).
    repo, _host, _write = tree
    problems = rendered(repo, {"services": {"api": {"build": {"context": context}}}})
    assert len(problems) == 1


def test_an_absolute_path_outside_is_refused_and_one_inside_passes(tree):
    repo, host, _write = tree
    assert rendered(repo, {"services": {"a": {"env_file": str(host / "creds.env")}}})
    assert not rendered(repo, {"services": {"a": {"env_file": str(repo / "api" / ".env")}}})


def test_a_symlink_out_of_the_checkout_is_refused(tree):
    repo, host, _write = tree
    (repo / "api" / "link.env").symlink_to(host / "creds.env")
    problems = rendered(repo, {"services": {"api": {"env_file": "api/link.env"}}})
    assert len(problems) == 1 and "outside the checkout" in problems[0]


def test_the_main_checkout_is_an_allowed_root_for_a_worktree(tree, tmp_path):
    # MAIN_REPO is exported for resources shared across worktrees; it is box-writable too.
    repo, _host, _write = tree
    wt = tmp_path / "repo-worktrees" / "feat"
    wt.mkdir(parents=True)
    doc = {"services": {"a": {"env_file": str(repo / "api" / ".env")}}}
    assert composeguard.rendered_problems(doc, wt, [wt, repo]) == []
    assert composeguard.rendered_problems(doc, wt, [wt]) != []


# ── the raw files: what rendering consumes or leaves unresolved ───────────────────────────


def test_an_include_outside_the_checkout_is_refused(tree):
    # podman-compose merges `include:` away — the rendered config no longer names the file.
    repo, host, write = tree
    (host / "inc.yml").write_text("services: {x: {image: busybox}}\n")
    f = write("compose.yml", "include:\n  - ../host/inc.yml\nservices: {}\n")
    problems = composeguard.raw_problems([f], repo, [repo])
    assert len(problems) == 1 and "include" in problems[0]


def test_an_included_files_own_paths_are_checked(tree):
    repo, _host, write = tree
    write(
        "stack/inc.yml", "services:\n  x:\n    image: busybox\n    env_file: ../../host/creds.env\n"
    )
    f = write("compose.yml", "include:\n  - path: stack/inc.yml\nservices: {}\n")
    problems = composeguard.raw_problems([f], repo, [repo])
    assert len(problems) == 1 and "stack/inc.yml" in problems[0]


@pytest.mark.parametrize("tag", ["!override", "!reset"])
def test_composes_merge_tags_are_readable(tree, tag):
    # The Compose spec's merge tags — a consumer's e2e overlay sets `depends_on: !override []`.
    # safe_load knows neither, so the raw scan read a valid overlay as unreadable and refused it.
    repo, _host, write = tree
    f = write("compose.e2e.yml", f"services:\n  app:\n    depends_on: {tag} []\n")
    assert composeguard.raw_problems([f], repo, [repo]) == []


@pytest.mark.parametrize("tag", ["!override", "!reset"])
def test_a_path_under_a_merge_tag_is_still_checked(tree, tag):
    # The tag wraps the value, it doesn't hide it: an included file's env_file never reaches the
    # render, so the raw scan is the only look it gets.
    repo, _host, write = tree
    write(
        "inc.yml", f"services:\n  x:\n    image: busybox\n    env_file: {tag} ../host/creds.env\n"
    )
    f = write("compose.yml", "include:\n  - inc.yml\nservices: {}\n")
    problems = composeguard.raw_problems([f], repo, [repo])
    assert len(problems) == 1 and "inc.yml" in problems[0] and "unreadable" not in problems[0]


def test_an_unknown_tag_still_fails_closed(tree):
    repo, _host, write = tree
    f = write("compose.yml", "services:\n  app:\n    env_file: !planted ../host/creds.env\n")
    problems = composeguard.raw_problems([f], repo, [repo])
    assert len(problems) == 1 and "unreadable" in problems[0]


def test_an_extended_files_own_paths_are_checked(tree):
    # `config` prints `extends` unresolved, so the extended file's env_file never shows there.
    repo, _host, write = tree
    write("base.yml", "services:\n  b:\n    image: busybox\n    env_file: ../host/creds.env\n")
    f = write("compose.yml", "services:\n  a:\n    extends: {file: base.yml, service: b}\n")
    problems = composeguard.raw_problems([f], repo, [repo])
    assert len(problems) == 1 and "base.yml" in problems[0]


def test_interpolation_is_refused_where_the_render_cannot_vouch_for_it(tree):
    # An included/extended file's paths never reach the rendered config, so a `${VAR}` there
    # can't be checked after compose resolves it — refused rather than guessed at.
    repo, _host, write = tree
    write("inc.yml", "services:\n  x:\n    image: busybox\n    env_file: ${HOME}/creds.env\n")
    f = write("compose.yml", "include:\n  - inc.yml\nservices: {}\n")
    problems = composeguard.raw_problems([f], repo, [repo])
    assert len(problems) == 1 and "interpolat" in problems[0]
    g = write("c2.yml", "include:\n  - ${HOME}/inc.yml\nservices: {}\n")
    assert composeguard.raw_problems([g], repo, [repo])


def test_include_cycles_terminate(tree):
    repo, _host, write = tree
    write("a.yml", "include:\n  - b.yml\nservices: {}\n")
    write("b.yml", "include:\n  - a.yml\nservices: {}\n")
    assert composeguard.raw_problems([repo / "a.yml"], repo, [repo]) == []


def test_a_literal_env_file_in_a_top_level_file_is_checked_raw_too(tree):
    # Belt to the rendered check: a provider that inlines env_file into `environment` on
    # `config` (docker compose) would otherwise hide it.
    repo, _host, write = tree
    f = write(
        "compose.yml", "services:\n  a:\n    image: busybox\n    env_file: ../host/creds.env\n"
    )
    assert len(composeguard.raw_problems([f], repo, [repo])) == 1


def test_a_project_dotenv_symlinked_out_is_refused(tree):
    repo, host, write = tree
    f = write("compose.yml", "services: {}\n")
    (repo / ".env").symlink_to(host / "creds.env")
    problems = composeguard.raw_problems([f], repo, [repo])
    assert len(problems) == 1 and ".env" in problems[0]


def test_a_compose_file_symlinked_out_is_refused(tree):
    repo, host, _write = tree
    (host / "evil.yml").write_text("services: {}\n")
    (repo / "compose.yml").symlink_to(host / "evil.yml")
    assert composeguard.raw_problems([repo / "compose.yml"], repo, [repo])


def test_an_operator_overlay_outside_the_checkout_is_trusted(tree):
    # Overlay paths come from the ADOPTED config or the operator's own environment; a file that
    # lives outside the checkout isn't box-writable, so its contents aren't scanned.
    repo, host, write = tree
    overlay = host / "overlay.yml"
    overlay.write_text("services:\n  a:\n    env_file: /etc/hosts\n")
    f = write("compose.yml", "services: {}\n")
    assert composeguard.raw_problems([f, overlay], repo, [repo]) == []


def test_an_interpolated_extends_file_is_refused_even_in_a_top_level_file(tree):
    # The render would vouch for the PATH, but nothing would then scan the extended file's own
    # paths — so the target of `extends`/`include` must always be written literally.
    repo, _host, write = tree
    write("base.yml", "services:\n  b:\n    image: busybox\n    env_file: ../host/creds.env\n")
    f = write(
        "compose.yml", "services:\n  a:\n    extends: {file: '${PWD}/base.yml', service: b}\n"
    )
    problems = composeguard.raw_problems([f], repo, [repo])
    assert len(problems) == 1 and "interpolat" in problems[0]


def test_an_include_project_directory_rebases_the_included_files_paths(tree):
    # `project_directory` is what the included file's relative paths resolve against: pointed
    # outside, an innocent-looking `env_file: creds.env` would read the host's.
    repo, _host, write = tree
    write("inc.yml", "services:\n  x:\n    image: busybox\n    env_file: creds.env\n")
    f = write("compose.yml", "include:\n  - path: inc.yml\n    project_directory: ../host\n")
    problems = composeguard.raw_problems([f], repo, [repo])
    assert problems and all("outside the checkout" in p for p in problems)
    assert any("project_directory" in p for p in problems)
