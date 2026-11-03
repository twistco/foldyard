"""foldyard — one CLI for the laptop-local isolated dev environment (typer app).

Distributed via `uv tool install` (ADR-0012), so `foldyard` is an on-PATH entry point
with its own per-machine venv — typer/textual live there, off the shared repo mount.

Step-1 surface: the host-side posture substrate. Engine verbs (up/down/nuke/ps/logs/
shell/box/verify/worktree/machine/transcripts) are ported from the `just` recipes in
later steps and will carry real typed options.

Each command's heavy imports are LAZY (inside the function): typer itself loads on
every invocation (incl. the per-recipe `foldyard env` hot path), but Textual — a
~290ms import — is pulled only by `tui`, never by `mode`/`host`/`doctor`/`env`. The
commands stay a thin, typed shell over `devmode`/`supervisor`, which own the
semantics; this keeps one source of truth while typer adds help, completion, typed
options, and `CliRunner` testability.
"""

from __future__ import annotations

import typer
from typer.core import TyperGroup


class _RootGroup(TyperGroup):
    """Keeps the command line for the root callback. Click hands the root callback only the verb
    name, having already set the rest aside for the subcommand — so `fy machine base --help` and
    `fy machine stop` look the same there. The callback needs the difference (see
    :func:`_command_path`)."""

    def invoke(self, ctx):
        ctx.meta["fy.argv"] = [*getattr(ctx, "_protected_args", []), *ctx.args]
        return super().invoke(ctx)


# Every Typer here renders help as MARKDOWN: under the default "rich" mode a config table named in
# help (`[claude]`, `[proxy]`) parses as a style tag and silently disappears.
app = typer.Typer(
    cls=_RootGroup,
    rich_markup_mode="markdown",
    name="foldyard",
    help="secretless-by-default, laptop-local isolated dev environment",
    no_args_is_help=True,
    add_completion=True,
    # Never rich-format tracebacks with local variables: this process handles
    # credentials, and a crash dump must not splash tokens/env across the terminal.
    pretty_exceptions_enable=False,
)


def _version(show: bool) -> None:
    """`fy --version`. Eager, so it answers BEFORE the callback body resolves a worktree — asking a
    binary to name itself must work from any directory, including one that is not a git repo."""
    if show:
        from . import __version__

        print(__version__)
        raise typer.Exit()


# Verbs that need no project — they read nothing from one, write nothing for one, and are what a
# person runs BEFORE there is one (`uvx foldyard prereq` in an empty directory, a workshop's first
# step). A prefix matches its subcommands. `skill install` is absent on purpose: it writes into
# the repo. Help (`--help`, a bare group) is project-free for every verb.
_PROJECT_FREE = (
    ("prereq",),
    ("docs",),
    ("changelog",),
    ("init",),
    ("machine", "base"),
    ("skill", "list"),
)

_NOT_A_PROJECT = (
    "✗ not in a foldyard project — no foldyard.toml here or in any directory above.\n"
    "  `fy init` makes this directory one; `fy prereq` checks your computer;\n"
    "  `fy --help` lists the verbs."
)


def _command_path(ctx: typer.Context) -> tuple[tuple[str, ...], bool]:
    """``(the verb's path, is this a help request)`` from the command line the root group kept.
    Help = a ``--help`` before any ``--``, or a path that stops at a group Typer answers with its
    help — not one that runs its own callback bare (`fy host` is its status)."""
    argv = ctx.meta.get("fy.argv", [])
    before_dashdash = argv[: argv.index("--")] if "--" in argv else argv
    node = ctx.command
    path: list[str] = []
    for token in argv:
        sub = getattr(node, "commands", {}).get(token)
        if sub is None:
            break
        path.append(token)
        node = sub
    shows_help = hasattr(node, "commands") and not getattr(node, "invoke_without_command", False)
    return tuple(path), "--help" in before_dashdash or shows_help


@app.callback()
def _resolve_worktree(
    ctx: typer.Context,
    version: bool = typer.Option(
        False,
        "--version",
        "-V",
        callback=_version,
        is_eager=True,
        help="print the installed foldyard version and exit",
    ),
) -> None:
    """Refuse a project verb outside a project, then pin the active worktree ONCE, up front, so
    posture (config) and the stack agree on WHICH checkout a command targets.

    Outside a project (no foldyard.toml here or above, no ``FOLDYARD_REPO``, not in a box) a
    project verb used to run anyway, as a made-up project named after the directory — writing a
    mode file and allocating a port band for it. Help and :data:`_PROJECT_FREE` verbs run
    anywhere and say nothing else.

    The stack infers the worktree from CWD (git common-dir + the worktrees root) when ``WORKTREE``
    isn't set; ``config.active_worktree`` reads ``WORKTREE`` only. Without this, a host ``fy mode``
    run from inside a worktree would write MAIN's posture while the stack acts on the worktree — the
    "changing mode changes both" footgun. Resolve it here (host only — the box already exports
    ``WORKTREE``) and export it into the process env so config + every child ``foldyard`` agree.
    Best-effort: a non-git dir leaves it unset, silently."""
    import os
    import sys

    from . import compat, config

    path, is_help = _command_path(ctx)
    project_free = is_help or any(path[: len(free)] == free for free in _PROJECT_FREE)
    if not (project_free or config.in_box() or config.in_project()):
        print(_NOT_A_PROJECT, file=sys.stderr)
        raise typer.Exit(1)
    try:
        if not (config.in_box() or os.environ.get("WORKTREE")):
            try:
                from . import stack

                main = stack.find_main_repo()
                if main is not None:
                    os.environ["WORKTREE"] = stack._active_worktree(stack.worktrees_root(main))
            except (Exception, SystemExit):
                # no worktrees root → leave WORKTREE unset (resolves to main). SystemExit too:
                # this best-effort pin must never abort the command it's fronting.
                pass
    finally:
        # AFTER the worktree pin, and reached on EVERY path through it — the in-box and
        # WORKTREE-already-set cases short-circuit the pin, and those are exactly where a
        # mismatched foldyard does the most damage. `--version` is eager and has already exited by
        # now, deliberately: asking a binary to name itself has to keep working when the answer
        # is what you are being told to change. Outside a project there is no window to read, so
        # the gate passes; inside one it holds for help and the project-free verbs too.
        #
        # The verb comes from the declared `ctx` and NOT click.get_current_context(): typer
        # vendors its own click, so the top-level click package reads a different context stack
        # and always answers None — which silently disabled the nudge until a wiring test caught
        # it. Anything reaching into click here must come from typer's copy.
        compat.gate_or_abort(ctx.invoked_subcommand)
        _say_if_unadopted(ctx.invoked_subcommand)


# Verbs that don't get the up-front "not adopted" line: the adoption surface itself (`config`
# says it in its own words), the manual, the scaffolder, the TUI and the supervisor (which report
# it in their own UI/log), and the launch verbs, whose gate asks or refuses on its own.
_NO_UNADOPTED_NOTICE = frozenset(
    {"config", "docs", "changelog", "init", "prereq", "doctor", "tui", "host", "up", "code"}
    | {"claude", "codex"}
)


def _say_if_unadopted(verb: str | None) -> None:
    """Once per command, on the host: say that this checkout's config isn't adopted, so what
    follows is foldyard reading NO config — never its consequences passed off as the config's.
    Best-effort: a hiccup here must never be what stops the command."""
    if verb is None or verb in _NO_UNADOPTED_NOTICE:
        return
    import sys

    from . import config

    if not config.in_project():  # nothing here to adopt (help, or a project-free verb)
        return

    try:
        notice = config.unadopted_notice()
    except Exception:
        return
    if notice:
        print(notice, file=sys.stderr)


@app.command()
def init(
    path: str = typer.Argument(".", help="target repo dir (default: current dir)"),
    name: str = typer.Option("", "--name", help="project name (default: target dir name)"),
    cpus: int = typer.Option(4, "--cpus", help="VM cpus (first-init sizing)"),
    memory: int = typer.Option(8192, "--memory", help="VM memory in MiB"),
    disk: int = typer.Option(60, "--disk", help="VM disk in GiB"),
    force: bool = typer.Option(False, "--force", "-f", help="overwrite an existing foldyard.toml"),
    with_: list[str] = typer.Option(
        None,
        "--with",
        help="write these blocks uncommented: claude, codex, vscode (repeat or comma-separate)",
    ),
) -> None:
    """Scaffold a commented, locked-down foldyard.toml (lima, VM firewall on) — then `fy box up`.

    The template is the guide: it starts stack-less and safe, with agents + the full feature set
    commented, and the header explains the order to uncomment them. No interactive wizard.
    `--with claude,vscode` writes those blocks already uncommented, so the first `fy box up`
    creates the box with them instead of a later adopt + recreate."""
    from . import init as init_mod

    raise typer.Exit(
        init_mod.init(
            path=path,
            name=name,
            cpus=cpus,
            memory=memory,
            disk=disk,
            force=force,
            blocks=with_ or [],
        )
    )


@app.command()
def prereq(
    fetch: bool = typer.Option(
        False,
        "--fetch",
        help="also do the big downloads now: bake the base VM new projects are cloned from",
    ),
) -> None:
    """Check what this computer needs for foldyard (Lima, podman, a hypervisor, KVM on Linux,
    disk, memory…) and print the command that installs each missing piece. Checks only — you run
    the installs. Needs no project: `uvx foldyard prereq` works before foldyard is installed.
    Exit 1 while anything required is missing."""
    from . import prereq as prereq_mod

    raise typer.Exit(prereq_mod.run(fetch=fetch))


@app.command()
def up() -> None:
    """Build + start the whole stack; show status."""
    from . import stack

    raise typer.Exit(stack.up())


@app.command()
def build(
    service: list[str] = typer.Argument(None, help="compose service(s) to build; defaults to all"),
    profile: list[str] = typer.Option(
        None, "--profile", "-p", help="compose profile(s) to include"
    ),
) -> None:
    """Build compose services with native Podman locally or Docker Compose in CI."""
    from . import stack

    raise typer.Exit(stack.build(service or [], extra_profiles=profile or None))


@app.command()
def down() -> None:
    """Stop + remove the project (keeps named volumes)."""
    from . import stack

    raise typer.Exit(stack.down())


@app.command()
def nuke() -> None:
    """Full teardown. Main project removes shared caches too; a worktree keeps them."""
    from . import stack

    raise typer.Exit(stack.nuke())


@app.command()
def reclaim() -> None:
    """Free engine-store space now — the sweeps `up` runs under low headroom, plus the
    project's [reclaim] script — without bouncing the box or the machine."""
    from . import stack

    raise typer.Exit(stack.reclaim_now())


@app.command()
def ps() -> None:
    """Show the stack's containers."""
    from . import stack

    raise typer.Exit(stack.ps())


@app.command()
def logs(svc: list[str] = typer.Argument(None, help="service name(s) to narrow to")) -> None:
    """Follow stack logs (optionally a single service)."""
    from . import stack

    raise typer.Exit(stack.logs(svc or []))


@app.command()
def shell() -> None:
    """Shell into the app container."""
    from . import stack

    raise typer.Exit(stack.shell())


@app.command()
def verify() -> None:
    """Isolation battery: prove the VM boundary + (in the box) that it holds no credentials."""
    from . import verify as verify_mod

    raise typer.Exit(verify_mod.verify())


@app.command(
    context_settings={"allow_extra_args": True, "ignore_unknown_options": True},
)
def claude(ctx: typer.Context) -> None:
    """Run Claude Code in the dev box, pre-oriented + skipping permission prompts (the box's
    rootless isolation is the safety boundary). Needs `[claude]` in foldyard.toml. Extra args
    pass through to `claude` (`fy claude --resume`, `fy claude -p "…"`)."""
    from . import box

    raise typer.Exit(box.claude(ctx.args))


@app.command(
    context_settings={"allow_extra_args": True, "ignore_unknown_options": True},
)
def codex(ctx: typer.Context) -> None:
    """Run the OpenAI Codex CLI in the dev box, bypassing approvals + its sandbox (the box's
    rootless isolation is the boundary). Needs `[codex]` in foldyard.toml. Extra args pass
    through to `codex`."""
    from . import box

    raise typer.Exit(box.codex(ctx.args))


@app.command()
def code() -> None:
    """Open VS Code attached to the running dev box, in an ISOLATED user-data-dir so the
    box's podman DOCKER_HOST can't leak into your default VS Code singleton (your computer only)."""
    from . import vscode

    raise typer.Exit(vscode.code())


@app.command("open")
def open_browser() -> None:
    """Open the selected worktree's app on localhost in the host browser."""
    from . import browser

    raise typer.Exit(browser.open_app())


@app.command()
def mode(
    spec: list[str] = typer.Argument(
        None,
        metavar="[switch=level ... | ttl=...]",
        help="No args: show the mode. Else SET switches (your computer only), e.g. "
        "`foldyard mode gcp=logs github=on ttl=1h` (ttl: emergency levels only). "
        "Which switches exist is per project: `fy mode` lists them.",
    ),
) -> None:
    """Show the dev mode (anywhere), or set it (your computer only)."""
    from . import devmode

    raise typer.Exit(devmode.main(["show"] if not spec else ["set", *spec]))


@app.command()
def state() -> None:
    """Every state tier's desired vs observed mode (files, daemons, capability, stack, box)."""
    from . import state_view

    raise typer.Exit(state_view.main([]))


@app.command()
def clock(
    spec: list[str] = typer.Argument(
        None,
        metavar="[ff <90s|30m|2h> | reset]",
        help="No args: show the mode-clock skew. `ff <duration>` fast-forwards TTL "
        "expiry/auto-revert for testing (host only, zero secrets needed); `reset` returns "
        "to real time.",
    ),
) -> None:
    """TESTING: skew the mode clock so TTL machinery can be exercised without waiting."""
    from . import devmode

    raise typer.Exit(devmode.main(["clock", *(spec or [])]))


host_app = typer.Typer(
    rich_markup_mode="markdown",
    name="host",
    help="the project's host supervisor — the credential daemons + the egress proxy the box "
    "rides. `fy up` / `fy box up` start it with the VM and `fy machine stop` stops it with the "
    "VM, so there is no `stop`. Bare `fy host` = status. Host only.",
    invoke_without_command=True,
)
app.add_typer(host_app)


@host_app.callback()
def host(ctx: typer.Context) -> None:
    # The root callback sees every `fy host …` as a bare "host"; the version nudge belongs on
    # `host restart` only (bare `fy host` is a status check) — see _box_version_nudge.
    from . import compat

    compat.nudge(f"host {ctx.invoked_subcommand}" if ctx.invoked_subcommand else None)
    if ctx.invoked_subcommand is None:
        from . import supervisor

        raise typer.Exit(supervisor.status())


@host_app.command("status")
def host_status() -> None:
    """Is this project's supervisor running current code, with its daemons up? Non-zero if not."""
    from . import supervisor

    raise typer.Exit(supervisor.status())


@host_app.command("restart")
def host_restart() -> None:
    """Replace the supervisor with a fresh one (or start it), and wait until it is ticking."""
    from . import supervisor

    raise typer.Exit(supervisor.restart())


@host_app.command("logs")
def host_logs(
    lines: int = typer.Option(50, "--lines", "-n", help="how many lines to show"),
    follow: bool = typer.Option(False, "--follow", "-f", help="keep following until Ctrl-C"),
) -> None:
    """The supervisor's log: its own lines and every daemon's, timestamped."""
    from . import supervisor

    raise typer.Exit(supervisor.logs(lines=lines, follow=follow))


@host_app.command("run", hidden=True)
def host_run() -> None:
    """The supervisor process itself — what `fy up` launches detached. Not for running by hand."""
    from . import supervisor

    raise typer.Exit(supervisor.main())


@app.command()
def docs(
    topic: str = typer.Argument("", help="topic to print (prefix match); omit to list them"),
) -> None:
    """Read foldyard's manual — THIS install's copy, offline, one topic at a time.

    Version-matched by construction: the docs ship in the wheel, so they describe the code you
    are running rather than whatever is on the project's main branch. `fy docs` lists topics.
    """
    from . import docs as docs_mod

    raise typer.Exit(docs_mod.main(topic))


@app.command()
def changelog(
    since: str | None = typer.Option(
        None, "--since", help="start AFTER this version, or from this date (YYYY-MM-DD)"
    ),
    until: str | None = typer.Option(
        None, "--until", help="stop at this version, inclusive (default: the installed one)"
    ),
    action: bool = typer.Option(
        False, "--action", help="only what can need a step: summary, Security/Removed/Changed"
    ),
) -> None:
    """What changed between the foldyard this project declares and the one installed.

    Prints only the releases in between, oldest first, from THIS install's own changelog —
    offline. The starting point is `[project].min_foldyard_version` (or the recommended version,
    if higher); once the config is caught up, raising it moves the next run's starting point.
    A project that declares neither starts from foldyard.toml's modification date — an estimate.
    """
    from . import changelog as changelog_mod

    raise typer.Exit(changelog_mod.main(since, until, action=action))


@app.command()
def doctor(
    deep: bool = typer.Option(
        False, "--deep", "-d", help="add live IAM probes (your computer only)"
    ),
) -> None:
    """ "What can this machine grant?" setup checks (ok/warn/fail)."""
    from . import devmode

    raise typer.Exit(devmode.main(["doctor", "deep"] if deep else ["doctor"]))


monitor_app = typer.Typer(
    rich_markup_mode="markdown",
    name="monitor",
    help="What the guest monitor recorded: processes, connections, selected files (your "
    "computer). Bare `fy monitor` shows the recent events, `--since`/`--until` any stretch the "
    "store still holds; `fy monitor hook` sends them on to a file, a program or a URL, and "
    "`fy monitor check` verifies a file a hook wrote.",
    invoke_without_command=True,
)
app.add_typer(monitor_app)


@monitor_app.callback()
def monitor(
    ctx: typer.Context,
    limit: int | None = typer.Option(
        None,
        "--limit",
        "-n",
        help="how many events, the latest (default 40; with --since/--until, all in the range)",
    ),
    worktree: str = typer.Option("", "--worktree", "-w", help="only this worktree ('main' too)"),
    as_json: bool = typer.Option(False, "--json", help="one JSON object per event"),
    since: str = typer.Option(
        "",
        "--since",
        help="events from this time on: ISO (UTC unless it gives an offset, e.g. "
        "2026-10-21T19:55) or back from now (90s, 15m, 2h, 1d)",
    ),
    until: str = typer.Option("", "--until", help="events before this time (as --since)"),
) -> None:
    if ctx.invoked_subcommand is None:
        from . import monitorlog

        raise typer.Exit(monitorlog.show(limit, worktree, as_json, since, until))


def _monitor_host_only() -> None:
    """Hooks, their delivery state and the sink's signing key live in the host's state dir —
    out of the box's reach by design (a hook command in reach of the box would be host code
    execution for it; the key would let it forge the file it is scored by)."""
    from . import config

    if config.in_box():
        typer.echo(
            "✗ run on your computer — monitor hooks, their state and the sink's key live there "
            "(~/.foldyard/<project>/), out of the box's reach"
        )
        raise typer.Exit(1)


hook_app = typer.Typer(
    rich_markup_mode="markdown",
    name="hook",
    help="Where the monitor's events go besides `fy monitor`: a file, your program or a URL, "
    "kept in host-only config. Bare `fy monitor hook` lists them with their delivery health.",
    invoke_without_command=True,
)
monitor_app.add_typer(hook_app)


@hook_app.callback()
def _hook_default(ctx: typer.Context) -> None:
    if ctx.invoked_subcommand is None:
        hook_list()


@hook_app.command("list")
def hook_list() -> None:
    """Each hook: what it sends where, its filters, what it delivered, and whether it is failing."""
    _monitor_host_only()
    from . import monitorhooks

    print("\n".join(monitorhooks.listing()))
    raise typer.Exit(0)


@monitor_app.command("hooks", hidden=True)  # the plural people reach for
def monitor_hooks() -> None:
    hook_list()


@hook_app.command("add")
def hook_add(
    name: str = typer.Argument(..., help="unique; keys the hook's place in the event stream"),
    command: list[str] | None = typer.Argument(
        None, help="after `--`: a program (absolute path) and its arguments; no shell"
    ),
    file: str = typer.Option(
        "", "--file", help="append each batch as a signed JSON line (the directory must exist)"
    ),
    to_box: bool = typer.Option(
        False,
        "--to-box",
        help="the signed file sink in the to-box, which every box reads and none can write "
        "(lima backend): <state dir>/to-box/monitor/NAME.jsonl",
    ),
    url: str = typer.Option("", "--url", help="POST each batch as JSON"),
    who: list[str] | None = typer.Option(
        None,
        "--who",
        help="container, build, unattributed, ambiguous, podman, vm (repeat or comma-separate); "
        "without it, all but vm and podman",
    ),
    kinds: list[str] | None = typer.Option(
        None, "--kinds", help="exec, exit, connect, file, other (repeat or comma-separate)"
    ),
    worktrees: list[str] | None = typer.Option(
        None, "--worktree", "-w", help="only these worktrees"
    ),
) -> None:
    """Add a hook: `--to-box`, `--file PATH`, `--url URL`, or `-- /abs/program args…` — one.

    A file hook is foldyard's own sink: never written through a symlink, each line signed with a
    key only your computer holds, so `fy monitor check PATH` tells whether anything edited it.
    `--to-box` is the place for events a box reads: the file sits in the to-box, read-only to the
    VM and every box (the box sees it under /run/foldyard/to-box/monitor/), so the box can read it
    but not change it; the signature still travels with any copy. Events are sent once settled,
    starting from the recent ones; the supervisor picks a new hook up within seconds."""
    _monitor_host_only()
    from . import monitorhooks

    raise typer.Exit(
        monitorhooks.cli_add(
            name,
            command=tuple(command or ()),
            file=file,
            to_box=to_box,
            url=url,
            who=tuple(who or ()),
            kinds=tuple(kinds or ()),
            worktrees=tuple(worktrees or ()),
        )
    )


@hook_app.command("remove")
def hook_remove(name: str = typer.Argument(..., help="the hook to remove")) -> None:
    """Remove a hook, keeping the rest of the file and your edits. Its place in the event stream
    is kept: re-adding the name resumes there, and a file it wrote stays checkable."""
    _monitor_host_only()
    from . import monitorhooks

    raise typer.Exit(monitorhooks.cli_remove(name))


@monitor_app.command("check")
def monitor_check(
    path: str = typer.Argument(..., help="a file a `file` hook wrote"),
    hook: str = typer.Option("", "--hook", help="the hook that wrote it (default: from config)"),
) -> None:
    """Verify a file a hook wrote: every line signed by this computer's sink, none missing,
    moved or added, and nothing cut from the end. Non-zero on the first problem, with its line."""
    _monitor_host_only()
    from . import monitorhooks

    raise typer.Exit(monitorhooks.check(path, hook))


@app.command()
def tui() -> None:
    """Interactive mode TUI (your computer only; Textual)."""
    from . import term
    from . import tui as tui_mod

    term.uninstall()  # Textual owns the terminal — its frames must not pass a rewriting wrapper
    raise typer.Exit(tui_mod.main())


@app.command()
def transcripts(
    dest: str = typer.Argument(
        "", help="archive dir (default: ~/.claude/projects, Codex's under ~/.foldyard/<project>)"
    ),
) -> None:
    """Copy the dev box's Claude and configured Codex transcripts to archives on your computer."""
    from . import transcripts as transcripts_mod

    raise typer.Exit(transcripts_mod.transcripts(dest))


@app.command(hidden=True)
def env() -> None:
    """Shell `export` lines deriving recipe env from the mode (internal; `_common.sh` evals it)."""
    from . import devmode

    raise typer.Exit(devmode.main(["env"]))


@app.command(hidden=True)
def workspaces() -> None:
    """main + worktrees with stack/devbox status (internal; the TUI's left column)."""
    from . import devmode

    raise typer.Exit(devmode.main(["workspaces"]))


@app.command(hidden=True)
def shellenv(
    no_machine: bool = typer.Option(False, "--no-machine", help="skip machine/DOCKER_HOST setup"),
) -> None:
    """Emit shell to `eval` at the top of a dev-VM recipe — drop-in for the old _common.sh
    (vars + COMPOSE array + the dev_vm_banner shim). Internal."""
    from . import stack

    raise typer.Exit(stack.shellenv(no_machine=no_machine))


@app.command(hidden=True)
def banner() -> None:
    """Print the dev-VM banner (PODMAN_PROJECT / DOCKER_HOST / env override) (internal)."""
    from . import stack

    raise typer.Exit(stack.banner())


machine_app = typer.Typer(
    rich_markup_mode="markdown",
    name="machine",
    help="the project's VM lifecycle (your computer only; no-op/refused in the box)",
    no_args_is_help=True,
)
app.add_typer(machine_app)


@machine_app.command("ensure")
def machine_ensure() -> None:
    """Init the rootless machine if absent, start it if stopped."""
    from . import machine, stack

    main = stack.main_repo()
    machine.ensure(main, stack.worktrees_root(main))
    raise typer.Exit(0)


@machine_app.command("recreate")
def machine_recreate(
    yes: bool = typer.Option(False, "--yes", "-y", help="skip the confirmation prompt"),
) -> None:
    """Stop + remove + re-init the machine so it mounts the worktrees root (VM mount sets are
    init-only). Named cache volumes are lost and rebuild on next `fy up`."""
    from . import machine, stack

    main = stack.main_repo()
    raise typer.Exit(machine.recreate(main, stack.worktrees_root(main), assume_yes=yes))


@machine_app.command("stop")
def machine_stop() -> None:
    """Stop the machine VM and host supervisor (containers + volumes kept; `fy up` restarts)."""
    from . import machine

    raise typer.Exit(machine.stop())


@machine_app.command("desktop")
def machine_desktop() -> None:
    """List this project's VM in Podman Desktop, beside every other project's.

    Registers an `fy-<machine>` podman connection, which Podman Desktop lists once its "Load
    remote system connections (ssh)" preference is on. Where Podman Desktop is installed,
    `fy up`/`fy box up` keep it current themselves, pinning the VM's ssh port so it survives
    reboots; FOLDYARD_PODMAN_DESKTOP=0 opts out, =1 forces it where it isn't detected."""
    from . import machine

    raise typer.Exit(machine.point_podman_desktop())


@machine_app.command("rm")
def machine_rm(
    yes: bool = typer.Option(False, "--yes", "-y", help="skip the confirmation prompt"),
) -> None:
    """Stop + DELETE the machine VM and host supervisor — every container and named volume in it
    is lost (the repo is untouched). `fy up` / `fy machine ensure` re-creates it."""
    from . import machine

    raise typer.Exit(machine.delete(assume_yes=yes))


@machine_app.command("host-firewall")
@machine_app.command("host-wall", hidden=True)  # the old name, kept as an alias
def machine_host_wall(
    uninstall: bool = typer.Option(False, "--uninstall", help="print the removal steps instead"),
) -> None:
    """Install the host firewall: print the nftables table and the system unit foldyard rendered,
    the exact root commands that install them, and whether the firewall is enforcing now.
    foldyard never runs them — you do, with the files in front of you. Exit 1 when not
    enforcing. Linux + `[machine] host_firewall = true`; a one-off install, not part of the VM's
    lifecycle."""
    from . import machine

    raise typer.Exit(machine.host_wall(uninstall=uninstall))


base_app = typer.Typer(
    rich_markup_mode="markdown",
    name="base",
    help=(
        "the base VM a new project's VM is cloned from — one per computer, project-less "
        "(`fy docs base-vm`)"
    ),
    invoke_without_command=True,
)
machine_app.add_typer(base_app)


@base_app.callback()
def _base_default(ctx: typer.Context) -> None:
    """With no subcommand: the base VM's status."""
    if ctx.invoked_subcommand is None:
        base_status()


@base_app.command("status")
def base_status() -> None:
    """Whether a base VM is baked, how old it is, and whether it is still current."""
    from . import basevm

    st = basevm.status()
    glyph = {"ok": "✓", "warn": "⚠", "optional": "ℹ"}[st.level]
    print(f"{glyph} base VM '{basevm.NAME}': {st.detail}")
    if st.fix:
        print(f"  → {st.fix}")
    raise typer.Exit(0)


@base_app.command("bake")
def base_bake() -> None:
    """(Re)create the base VM: Lima's podman template, nothing mounted, booted once, foldyard's
    generic box image built in it, stopped. Every project VM created afterwards is a clone of it
    (seconds, no download). Existing project VMs are never touched."""
    from . import basevm

    raise typer.Exit(basevm.bake())


@base_app.command("rm")
def base_rm() -> None:
    """Delete the base VM. VMs already cloned from it are independent and unaffected; new ones
    are created from the template again."""
    from . import basevm

    raise typer.Exit(basevm.rm())


@machine_app.command("delete", hidden=True)  # alias: limactl users reach for `delete`
def machine_delete(
    yes: bool = typer.Option(False, "--yes", "-y", help="skip the confirmation prompt"),
) -> None:
    machine_rm(yes=yes)  # single source of the exit logic; keep the hidden alias thin


box_app = typer.Typer(
    rich_markup_mode="markdown",
    name="box",
    help="the long-lived, per-worktree dev box (engine socket + repo mount)",
    no_args_is_help=True,
)
app.add_typer(box_app)


@box_app.callback()
def _box_version_nudge(ctx: typer.Context) -> None:
    """The root callback runs before this sub-app resolves its verb, so every `fy box …` looks
    like a bare "box" up there — and scoping the nudge to that group put it on `fy box exec`,
    which git hooks dispatch through once per commit. The floor already ran at the root (and
    exited if unmet); this adds back only the recommendation, for the resolved path."""
    from . import compat

    compat.nudge(f"box {ctx.invoked_subcommand}" if ctx.invoked_subcommand else None)


@box_app.command("build")
def box_build() -> None:
    """Build the dev-box base image (from `[box].image`)."""
    from . import box

    raise typer.Exit(box.main("build"))


@box_app.command("up")
def box_up() -> None:
    """Create + start the dev box (idempotent); install agent tooling; warm deps."""
    from . import box

    raise typer.Exit(box.main("up"))


@box_app.command("shell")
def box_shell() -> None:
    """Attach a shell to the running dev box (repeatable for multiple sessions)."""
    from . import box

    raise typer.Exit(box.main("shell"))


@box_app.command(
    "exec",
    context_settings={"allow_extra_args": True, "ignore_unknown_options": True},
)
def box_exec(ctx: typer.Context) -> None:
    """Run a command non-interactively in the running dev box, at the caller's cwd
    (`foldyard box exec '<cmd>'`). Behind the git hooks' dispatch into the box;
    `--skip-if-down` no-ops cleanly when the box is stopped."""
    from . import box

    raise typer.Exit(box.main("exec", ctx.args))


@box_app.command("down")
def box_down() -> None:
    """Stop + remove the dev box (login/CLI volumes kept)."""
    from . import box

    raise typer.Exit(box.main("down"))


@box_app.command("ps")
def box_ps() -> None:
    """Show the dev box's container."""
    from . import box

    raise typer.Exit(box.main("ps"))


allow_app = typer.Typer(
    rich_markup_mode="markdown",
    name="allow",
    help="the egress allowlist the proxy enforces under [proxy] enforce (your computer only)",
    no_args_is_help=True,
)
app.add_typer(allow_app)


@allow_app.command("add")
def allow_add(
    host: str = typer.Argument(
        ..., help="exact host or *.suffix glob (:443); host:port for one other tunnel port"
    ),
    level: str = typer.Option(
        "session",
        "--level",
        "-l",
        help="once (short TTL) | session (until the supervisor restarts) | permanent",
    ),
    ttl: int = typer.Option(0, "--ttl", help="seconds for --level once (default 120)"),
    build: bool = typer.Option(
        False, "--build", help="for image builds only (fy box build, the stack build), not the box"
    ),
) -> None:
    """Allow a host through the allowlist.

    Grants live in the allow-store on your computer at every level, deliberately NOT in
    foldyard.toml: repo config travels with the branch and the box can write it, so a committed
    allowlist would let the VM widen its own allowlist.

    A local address (loopback, your network, link-local) is refused in every mode unless a grant
    names it exactly, e.g. 127.0.0.1:8123 — and only this command makes one.
    """
    from . import allowlist

    eff = allowlist.grant(host, level, ttl or None, build=build)
    scope = "for builds, " if build else ""
    listed = ", ".join(eff["allow"]) or "empty"
    print(f"✓ {host} allowed ({scope}{level}). Effective allowlist: {listed}")
    if allowlist.local_key(host):
        print(
            f"  ⚠ {host} is on your computer or its network: the box can now reach whatever "
            "listens there, through the proxy. `fy allow remove` it when you're done."
        )


@allow_app.command("list")
def allow_list() -> None:
    """Show the effective allowlist the proxy is enforcing (and whether it enforces at all)."""
    from . import allowlist

    eff = allowlist.effective()
    window = allowlist.learning()
    if window:
        print(
            f"default_deny: LEARNING until {_local_hm(window['until'])} — nothing refused, "
            "would-be refusals recorded (`fy allow learn` to review); enforcing after"
        )
    else:
        print(
            f"default_deny: {'on — unlisted hosts are refused' if eff['default_deny'] else 'off'}"
        )
    for host in eff["allow"]:
        print(f"  {host}")
    if not eff["allow"]:
        print("  (no grants — `fy allow add <host>`, or the TUI's `a` key on a blocked row)")
    if eff.get("build_allow"):
        print("for image builds only (the build proves itself; the box can't use these):")
        for host in eff["build_allow"]:
            print(f"  {host}")
    with _config_bound():
        pending = allowlist.pending_recommendations()
    if pending:
        n = len(pending)
        print(f"recommended, not yet answered ({n}) — `fy allow sync`:")
        for e in pending:
            print(f"  {e['host']}" + (f" — {e['why']}" if e["why"] else ""))


@allow_app.command("sync")
def allow_sync(
    yes: bool = typer.Option(
        False, "--yes", "-y", help="grant every pending host permanently, without asking"
    ),
) -> None:
    """Review the recommended egress hosts — yes/session/not-now/never, one host at a time.

    Two sources, both advisory: this repo's ADOPTED foldyard.toml (`[proxy] recommend`) and the
    agents/plugins it declares (Claude Code's installer, npm for Codex). Neither can grant
    anything — entries reach this prompt after the file carrying them passed the adoption gate,
    and only your per-host answer here grants (also asked at `fy up`/`fy host restart`, and
    offered in the TUI's Network Log).

    `--yes` is the UNATTENDED path, for a scripted first box-up with no terminal to answer on: it
    takes every pending recommendation at `permanent` in one go. Review the list with
    `fy allow list` first — it's the same trust decision, made in bulk.
    """
    import sys

    from . import allowlist

    with _config_bound():
        counts = allowlist.offer_recommendations(
            interactive=sys.stdin.isatty(),
            prompt=input,
            echo=print,
            accept_all=yes,
        )
    if counts == {"granted": 0, "declined": 0, "deferred": 0}:
        # All-zero is ambiguous: offer_recommendations() returns exactly this in the box, where it
        # is a no-op (grants are host-side only) — so don't report "nothing pending" from there.
        if allowlist.in_box():
            print(
                "• egress grants are host-side only — run `fy allow sync` on the host "
                "to answer this checkout's recommendations (`fy allow list` shows what's pending)."
            )
        else:
            print("✓ nothing pending — every recommended host is granted or answered.")
            _point_at_refused_hosts()


def _point_at_refused_hosts() -> None:
    """Refused hosts aren't recommendations, but the TUI's Network Log offers them for the same
    `a`: without this line "nothing pending" reads as "nothing to answer"."""
    from . import allowlist, config
    from .plugins import proxy

    with _config_bound():
        log = proxy._proxy_log()
        refused = allowlist.refused_hosts(allowlist.read_log_rows([*config.rotated_logs(log), log]))
    if not refused:
        return
    n, shown = len(refused), ", ".join(refused[:5]) + (", …" if len(refused) > 5 else "")
    print(
        f"• {n} host{'s' if n != 1 else ''} the allowlist refused, still unanswered: {shown}\n"
        "  Not recommendations — answer them in the TUI's Network Log, or `fy allow add <host>`."
    )


def _config_bound():
    """The ADOPTED config for the active checkout, bound — what every recommendation read keys
    off (`devmode.worktree_config` funnels through the pin)."""
    from . import config, devmode

    return config.using(devmode.worktree_config(config.active_worktree()))


def _local_hm(iso: str) -> str:
    """An ISO timestamp as local HH:MM, for a deadline the operator reads."""
    from datetime import datetime

    try:
        return datetime.fromisoformat(iso).astimezone().strftime("%H:%M")
    except (ValueError, TypeError):
        return iso


@allow_app.command("enforce")
@allow_app.command("wall", hidden=True)  # the old name, kept as an alias
def allow_wall(
    state: str = typer.Argument(
        ..., help="on (enforce) | off (observe only) | learn (observe for a while, then enforce)"
    ),
    duration: str = typer.Option(
        "1h", "--for", help="learn: how long before enforcing again (90s · 30m · 2h; max 8h)"
    ),
) -> None:
    """Turn allowlist ENFORCEMENT on or off — or `learn`: observe for a while, then enforce.

    Kept on your computer, like the grants: `[proxy] enforce` only seeds the first answer, because
    the box can write repo config, and an enforcement switch the box can flip is no switch.

    `learn` lets everything through while the proxy records each host it WOULD refuse, and turns
    enforcement back on by itself when the window ends — unlike `off`, it can't be forgotten open.
    Use it while a new dependency's egress is unknown, then `fy allow learn` to grant what it
    recorded in one reviewed batch.
    """
    from . import allowlist, devmode

    if state == "learn":
        try:
            seconds = devmode.parse_ttl(duration)
        except ValueError as e:
            raise typer.BadParameter(str(e), param_hint="--for")
        window = allowlist.start_learning(seconds)
        print(
            f"✓ allowlist LEARNING until {_local_hm(window['until'])} — nothing is refused, "
            "every host it would refuse is recorded; enforcing after. "
            "Review: `fy allow learn`"
        )
        return
    if state not in ("on", "off"):
        raise typer.BadParameter("expected `on`, `off` or `learn`")
    eff = allowlist.set_wall(state == "on")
    print(f"✓ allowlist {'ENFORCING' if eff['default_deny'] else 'observing only'}")


def _who_asked():
    """For the learn review: how a row's attributing-forwarder stamp (ADR-0034) names who asked,
    resolved against the supervisor's container snapshots; ``None`` when no row could carry one
    (this project never had attribution on), so the review reads as it always did."""
    from . import attribution, config

    if not config.forwarder_key_file().is_file():
        return None
    resolver = attribution.resolver()

    def name(origin: object, ts: object) -> str | None:
        who = resolver.who(origin, ts)
        return who.label if who is not None else None

    return name


@allow_app.command("learn")
def allow_learn(
    yes: bool = typer.Option(
        False, "--yes", "-y", help="grant every learned host permanently, without asking"
    ),
) -> None:
    """Review what the last learn window recorded, and grant it in one go.

    Lists each host the allowlist would have refused during the window (open or last closed) with
    how often and WHICH TOOL reached it (the User-Agent the proxy saw), minus anything granted or
    declined since. Then: grant them all, go one by one, or leave them. It also prints the
    `[proxy] recommend` lines that share the result with the team — a repo edit you review and
    commit, adopted on each computer like any other.
    """
    import sys

    from . import allowlist, config
    from .plugins import proxy

    if allowlist.in_box():
        print(
            "• learn windows and grants are host-side only — run `fy allow learn` on your "
            "computer. From here, a blocked host is one to name to the person running foldyard."
        )
        return
    window = allowlist.last_window()
    if window is None:
        print("• no learn window yet — `fy allow enforce learn [--for 1h]` starts one")
        return
    with _config_bound():
        log = proxy._proxy_log()
        rows = allowlist.read_log_rows([*config.rotated_logs(log), log])
        learned = allowlist.learned_hosts(rows, window, who=_who_asked())
    state = "open" if allowlist.learning() else "closed"
    span = f"{_local_hm(window['since'])}–{_local_hm(window['until'])}, {state}"
    allowlist.mark_reviewed()  # shown now: the next window starts fresh instead of carrying this
    if not learned:
        print(
            f"✓ nothing to grant from the learn window ({span}) — the allowlist refused nothing new"
        )
        return
    n = len(learned)
    print(f"▶ the allowlist would have refused {n} host{'s' if n != 1 else ''} ({span}):")
    width = max(len(e["host"]) for e in learned)
    for e in learned:
        print(f"    {e['host']:<{width}}  {e['count']:>4}×  {', '.join(e['uas']) or '(no UA)'}")
        if e["by"]:
            print(f"    {'':<{width}}         asked by {', '.join(e['by'])}")
        if e["paths"]:
            more = f" (+{e['more_paths']} more)" if e["more_paths"] else ""
            print(f"    {'':<{width}}         fetched {', '.join(e['paths'])}{more}")
    if yes:
        choice = "y"
    elif sys.stdin.isatty():
        choice = (
            input(f"  grant all {n} permanently?  [y]es · [r]eview one by one · [n]o (default): ")
            .strip()
            .lower()
        )
    else:
        print("  No terminal here — grant them all with `fy allow learn --yes`, or one at a time.")
        choice = "n"
    granted = []
    for e in learned:
        if choice in ("y", "yes"):
            ok = True
        elif choice in ("r", "review"):
            answer = input(f"  allow {e['host']}?  [y]es permanent · [n]o (default): ")
            ok = answer.strip().lower() in ("y", "yes")
        else:
            ok = False
        if ok:
            allowlist.grant(e["host"], "permanent")
            granted.append(e)
    if granted:
        print(
            f"✓ granted {len(granted)} (permanent). To share them with the team, add to "
            "foldyard.toml — each `why` is what the box was SEEN doing; replace it with the "
            "reason the project needs the host before you commit:"
        )
        print("\n".join(allowlist.recommend_block(granted)))


@allow_app.command("remove")
def allow_remove(
    host: str = typer.Argument(..., help="host/glob to revoke, at whatever level it was granted"),
) -> None:
    """Revoke a host's grant."""
    from . import allowlist

    eff = allowlist.revoke(host)
    print(f"✓ {host} revoked. Effective allowlist: {', '.join(eff['allow']) or 'empty'}")


config_app = typer.Typer(
    rich_markup_mode="markdown",
    name="config",
    help="the foldyard.toml you ADOPTED — what the supervisor actually runs (your computer only)",
    no_args_is_help=True,
)
app.add_typer(config_app)


def _active_config():
    """The active checkout's resolved config (worktree-aware), for the `fy config` verbs."""
    from . import config, devmode

    return devmode.worktree_config(config.active_worktree())


def _refuse_in_box() -> None:
    """`status`/`diff` READ host state, so in the box they'd compare the checkout against an empty
    box-side state dir and report "nothing adopted yet" — which reads as "the host isn't running
    my config" when the real answer is "you can't see that from here". Say so instead; the
    mutating verbs have their own refusal in :mod:`foldyard.configpin`."""
    from . import config

    if config.in_box():
        raise typer.Exit(
            typer.echo(
                "ℹ the adopted config lives on the HOST, outside this box — run `fy config …`"
                " there. From in here, `fy config widenings` shows what THIS checkout declares."
            )
            or 0
        )


@config_app.command("status")
def config_status() -> None:
    """Does this checkout's foldyard.toml match the copy the host adopted?

    The host reconciles its credential daemons from the ADOPTED copy (kept in your home,
    outside the repo mount) — repo config is writable from inside the box, so a live-read
    foldyard.toml would let the VM retarget its own injection and switch off its own capture.
    """
    _refuse_in_box()

    from . import configpin

    drift = configpin.inspect(_active_config())
    if not drift.pinned_exists:
        print(
            f"✗ nothing adopted yet [{drift.label}] — `fy up`, `fy host restart`, "
            "or `fy config adopt`."
        )
        raise typer.Exit(1)
    if not drift.changed:
        print(f"✓ [{drift.label}] the checkout matches the adopted config ({drift.tree_digest()})")
        raise typer.Exit(0)
    print(f"⚠ [{drift.label}] foldyard.toml differs from the adopted copy — the host runs the")
    print("  adopted one. `fy config diff`, then `fy config adopt` or `fy config revert`.")
    raise typer.Exit(1)


@config_app.command("diff")
def config_diff() -> None:
    """Show what changed in this checkout's foldyard.toml since the host adopted it.

    On a worktree that has never been adopted there is nothing of its own to diff, so it shows
    the comparison that does exist: this checkout against the config the host runs for main.
    """
    _refuse_in_box()

    from . import configpin, term

    drift = configpin.inspect(_active_config())
    if not drift.pinned_exists:
        print(f"✗ nothing adopted yet [{drift.label}] — `fy config adopt` to adopt this checkout.")
        # The adopt prompt points people HERE, so an unadopted checkout must not be a dead end —
        # that was the state it pointed at most often (a fresh worktree, first `fy box up`).
        base = configpin.main_baseline(drift)
        print(f"  {configpin.first_adoption_headline(drift, base)}")
        body = configpin.first_adoption_body(drift, base)
        if body:
            if base is not None:
                print("  `-` is what the host runs for main · `+` is what adopting here would add.")
            print()
            print(term.paint_diff(body) if term.color_enabled() else body)
            print("\n  What these declarations let the host DO: `fy config widenings`.")
        raise typer.Exit(1)
    body = drift.diff()
    if not body:
        if not drift.changed:
            print(f"✓ [{drift.label}] no difference — the host runs exactly this config.")
            raise typer.Exit(0)
        # The files differ byte-wise but nothing the merge reads does. Still a drift the gate will
        # keep asking about, so it exits non-zero like any other — just with nothing to review.
        print(f"⚠ [{drift.label}] {drift.summary()} — nothing the host reads would change.")
        print("  `fy config adopt` to sync the copy it holds, or leave it.")
        raise typer.Exit(1)
    print(f"⚠ [{drift.label}] adopted {drift.pinned_digest()} → checkout: {drift.summary()}")
    print("  `-` is what the host RUNS today · `+` is what adopting would change it to.\n")
    print(term.paint_diff(body) if term.color_enabled() else body)
    raise typer.Exit(1)


@config_app.command("widenings")
def config_widenings() -> None:
    """Inventory what this config asks the HOST to allow: capture exemptions, injection targets,
    agent prompts — plus any key that reads as security config but is no longer honoured.

    The counterweight to `@all` and to defaults: one token in `[proxy] passthrough` can exempt
    ~200 hosts from decryption, and nothing else prints that number.
    """
    from . import config, devmode, exposure

    cfg = _active_config()
    with config.using(cfg):
        mode = devmode.read(apply_expiry=True)["mode"]
        report = exposure.render(exposure.collect(cfg, mode))
    print("\n".join(report))


@config_app.command("adopt")
def config_adopt() -> None:
    """Adopt this checkout's foldyard.toml: the host starts running it (within one tick).

    Read `fy config diff` first — adopting is the moment of trust, and this file decides which
    hosts get your credentials injected and which egress stays undecrypted.
    """
    from . import configpin

    drift = configpin.adopt(_active_config())
    print(f"✓ adopted [{drift.label}] ({drift.tree_digest()}) — the supervisor picks it up live.")


@config_app.command("revert")
def config_revert() -> None:
    """Put this checkout's foldyard.toml back to the copy the host adopted ("I didn't write this").

    A file the adopted copy doesn't have (e.g. a gitignored foldyard.local.toml that appeared) is
    moved aside rather than deleted.
    """
    from . import configpin

    for line in configpin.revert(_active_config()):
        print(f"✓ {line}")


worktree_app = typer.Typer(
    rich_markup_mode="markdown",
    name="worktree",
    help="host-sibling worktrees sharing the one rootless VM (your computer only)",
    no_args_is_help=True,
)
app.add_typer(worktree_app)


@worktree_app.command("add")
def worktree_add(
    name: str = typer.Argument(..., help="worktree dir name (under the worktrees root)"),
    branch: str = typer.Argument("", help="branch to check out (default wt/<name>)"),
    base: str = typer.Option(
        "",
        "--from",
        help="base ref for a NEW branch (default: the repo's main/default branch, "
        "not the primary checkout's current HEAD)",
    ),
    yes: bool = typer.Option(
        False,
        "--yes",
        "-y",
        help="register an EXISTING worktree without asking (never adopts a config: see below)",
    ),
    box_up: bool = typer.Option(
        False, "--box", help="then bring its box up (`WORKTREE=<name> fy box up`)"
    ),
) -> None:
    """Create a host-sibling worktree, init its per-project config, and adopt that config.

    A config identical to the one adopted for main is adopted without asking; one that differs
    is shown as a diff against main and adopted only if you say so at the prompt. With `--box`,
    its box comes up next, once its config is adopted.

    On a worktree that already exists, registers it with your computer instead (after showing its
    real path), so its daemons run again."""
    from . import worktree

    raise typer.Exit(worktree.add(name, branch, base, assume_yes=yes, box_up=box_up))


@worktree_app.command("init")
def worktree_init(
    name: str = typer.Argument(..., help="worktree dir name (under the worktrees root)"),
) -> None:
    """Re-run the consumer's worktree-init script for a worktree, in the VM.

    `worktree add` does this for you; use this when it skipped (no box image built yet) or after
    changing the init script."""
    from . import worktree

    raise typer.Exit(worktree.init_config(name))


@worktree_app.command("remove")
def worktree_remove(
    name: str = typer.Argument(..., help="worktree dir name to remove (under the worktrees root)"),
    force: bool = typer.Option(
        False,
        "--force",
        "-f",
        help="remove even if archiving fails or the tree is dirty (its uncommitted work is "
        "saved on refs/fy/removed/<name>-<time> first)",
    ),
    yes: bool = typer.Option(False, "--yes", "-y", help="skip the confirmation prompt"),
    discard: bool = typer.Option(
        False,
        "--discard",
        help="don't check for or save uncommitted work — for when the VM or the box image isn't "
        "there to check it in; anything uncommitted is LOST",
    ),
) -> None:
    """Tear down the worktree's dev box + stack, archive agent transcripts, then remove it.

    Uncommitted work is looked for in a container from the box image, never on your computer (git
    there would run the filter drivers the shared .git names), so this needs the box image and the
    VM — or --discard."""
    from . import worktree

    raise typer.Exit(worktree.remove(name, force, assume_yes=yes, discard=discard))


@worktree_app.command("list")
def worktree_list() -> None:
    """List main + each worktree with its branch."""
    from . import worktree

    raise typer.Exit(worktree.list_())


skill_app = typer.Typer(
    rich_markup_mode="markdown",
    name="skill",
    help="Claude Code skills bundled with foldyard (list / install into .claude/skills)",
    no_args_is_help=True,
)
app.add_typer(skill_app)


@skill_app.command("list")
def skill_list() -> None:
    """List the skills shipped in the package."""
    from . import skills

    raise typer.Exit(skills.list_())


@skill_app.command("install")
def skill_install(
    name: str = typer.Argument(..., help="bundled skill name (see `foldyard skill list`)"),
    force: bool = typer.Option(False, "--force", "-f", help="overwrite if already installed"),
) -> None:
    """Copy a bundled skill into this repo's .claude/skills/ for Claude Code to discover."""
    from . import skills

    raise typer.Exit(skills.install(name, force=force))


def main() -> None:
    """Console-script entry point (`foldyard = foldyard.cli:main`)."""
    from . import term

    term.install()  # color the ✓/✗/▶/⚠ glyphs when stdout/stderr are real TTYs
    app()


if __name__ == "__main__":
    main()
