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

app = typer.Typer(
    name="foldyard",
    help="secretless-by-default, laptop-local isolated dev environment",
    no_args_is_help=True,
    add_completion=True,
    # Never rich-format tracebacks with local variables: this process handles
    # credentials, and a crash dump must not splash tokens/env across the terminal.
    pretty_exceptions_enable=False,
)


@app.callback()
def _resolve_worktree() -> None:
    """Pin the active worktree ONCE, up front, so posture (config) and the stack agree on WHICH
    checkout a command targets.

    The stack infers the worktree from CWD (git common-dir + the worktrees root) when ``WORKTREE``
    isn't set; ``config.active_worktree`` reads ``WORKTREE`` only. Without this, a host ``fy mode``
    run from inside a worktree would write MAIN's posture while the stack acts on the worktree — the
    "changing mode changes both" footgun. Resolve it here (host only — the box already exports
    ``WORKTREE``) and export it into the process env so config + every child ``foldyard`` agree.
    Best-effort: a non-git dir (e.g. ``init`` scaffolding) just leaves it unset."""
    import os

    from . import config

    if config.in_box() or os.environ.get("WORKTREE"):
        return
    try:
        from . import stack

        os.environ["WORKTREE"] = stack._active_worktree(stack.worktrees_root(stack.main_repo()))
    except (Exception, SystemExit):
        # not a git repo / no worktrees root → leave WORKTREE unset (resolves to main). SystemExit
        # too: stack.main_repo() raises it (not an Exception) from a non-git dir, and this best-
        # effort pin must never abort the command it's fronting (e.g. `fy init` scaffolding).
        pass


@app.command()
def init(
    path: str = typer.Argument(".", help="target repo dir (default: current dir)"),
    name: str = typer.Option("", "--name", help="project name (default: target dir name)"),
    cpus: int = typer.Option(4, "--cpus", help="VM cpus (first-init sizing)"),
    memory: int = typer.Option(8192, "--memory", help="VM memory in MiB"),
    disk: int = typer.Option(60, "--disk", help="VM disk in GiB"),
    force: bool = typer.Option(False, "--force", "-f", help="overwrite an existing foldyard.toml"),
) -> None:
    """Scaffold a commented, locked-down foldyard.toml (a lima+wall dev box) — then `fy box up`.

    The template is the guide: it starts stack-less and safe, with agents + the full feature set
    commented, and the header explains the order to uncomment them. No interactive wizard."""
    from . import init as init_mod

    raise typer.Exit(
        init_mod.init(path=path, name=name, cpus=cpus, memory=memory, disk=disk, force=force)
    )


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
    """Isolation battery: prove the VM boundary + (in the box) the credential-less posture."""
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
    box's podman DOCKER_HOST can't leak into your default VS Code singleton (Mac only)."""
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
        metavar="[axis=value ... | ttl=...]",
        help="No args: show the posture. Else SET axes (Mac only), e.g. "
        "`foldyard mode gcp=logs github=app ttl=1h`. "
        "Axes: gcp=off|logs|sa|user, github=off|app|user.",
    ),
) -> None:
    """Show the dev posture (anywhere), or set it (Mac only)."""
    from . import devmode

    raise typer.Exit(devmode.main(["show"] if not spec else ["set", *spec]))


@app.command()
def state() -> None:
    """Every state tier's desired vs observed posture (files, daemons, capability, stack, box)."""
    from . import state_view

    raise typer.Exit(state_view.main([]))


@app.command()
def clock(
    spec: list[str] = typer.Argument(
        None,
        metavar="[ff <90s|30m|2h> | reset]",
        help="No args: show the posture-clock skew. `ff <duration>` fast-forwards TTL "
        "expiry/auto-revert for testing (host only, zero secrets needed); `reset` returns "
        "to real time.",
    ),
) -> None:
    """TESTING: skew the posture clock so TTL machinery can be exercised without waiting."""
    from . import devmode

    raise typer.Exit(devmode.main(["clock", *(spec or [])]))


@app.command()
def host(
    restart: bool = typer.Option(
        False,
        "--restart",
        "-r",
        help="bounce an already-running supervisor first (it's replaced automatically anyway "
        "when the installed foldyard code changed or its heartbeat is stale)",
    ),
) -> None:
    """Run the credential daemons the current mode demands (Mac; one foreground terminal)."""
    from . import supervisor

    raise typer.Exit(supervisor.main(restart=restart))


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
def doctor(
    deep: bool = typer.Option(False, "--deep", "-d", help="add live IAM probes (Mac)"),
) -> None:
    """ "What can this machine grant?" setup checks (ok/warn/fail)."""
    from . import devmode

    raise typer.Exit(devmode.main(["doctor", "deep"] if deep else ["doctor"]))


@app.command()
def tui() -> None:
    """Interactive posture TUI (Mac; Textual)."""
    from . import term
    from . import tui as tui_mod

    term.uninstall()  # Textual owns the terminal — its frames must not pass a rewriting wrapper
    raise typer.Exit(tui_mod.main())


@app.command()
def transcripts(
    dest: str = typer.Argument("", help="archive dir (default ~/.claude/projects)"),
) -> None:
    """Copy the dev box's Claude and configured Codex transcripts to host archives (Mac only)."""
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
    (vars + COMPOSE array + ensure_stubs/dev_vm_banner shims). Internal."""
    from . import stack

    raise typer.Exit(stack.shellenv(no_machine=no_machine))


@app.command(hidden=True)
def stubs() -> None:
    """Create the empty GCP credential stubs the compose file bind-mounts (internal)."""
    from . import stack

    raise typer.Exit(stack.stubs())


@app.command(hidden=True)
def banner() -> None:
    """Print the dev-VM banner (PODMAN_PROJECT / DOCKER_HOST / env override) (internal)."""
    from . import stack

    raise typer.Exit(stack.banner())


machine_app = typer.Typer(
    name="machine",
    help="rootless podman-machine lifecycle (Mac; no-op/refused in the box)",
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


@machine_app.command("rm")
def machine_rm(
    yes: bool = typer.Option(False, "--yes", "-y", help="skip the confirmation prompt"),
) -> None:
    """Stop + DELETE the machine VM and host supervisor — every container and named volume in it
    is lost (the repo is untouched). `fy up` / `fy machine ensure` re-creates it."""
    from . import machine

    raise typer.Exit(machine.delete(assume_yes=yes))


@machine_app.command("delete", hidden=True)  # alias: limactl users reach for `delete`
def machine_delete(
    yes: bool = typer.Option(False, "--yes", "-y", help="skip the confirmation prompt"),
) -> None:
    machine_rm(yes=yes)  # single source of the exit logic; keep the hidden alias thin


box_app = typer.Typer(
    name="box",
    help="the long-lived, per-worktree dev box (engine socket + repo mount)",
    no_args_is_help=True,
)
app.add_typer(box_app)


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
    name="allow",
    help="the egress allowlist the proxy enforces under [proxy] default_deny (Mac)",
    no_args_is_help=True,
)
app.add_typer(allow_app)


@allow_app.command("add")
def allow_add(
    host: str = typer.Argument(..., help="exact host or *.suffix glob"),
    level: str = typer.Option(
        "session",
        "--level",
        "-l",
        help="once (short TTL) | session (until the supervisor restarts) | permanent",
    ),
    ttl: int = typer.Option(0, "--ttl", help="seconds for --level once (default 120)"),
) -> None:
    """Allow a host through the egress wall.

    Grants live in the Mac-home allow-store at every level, deliberately NOT in foldyard.toml: repo
    config travels with the branch and the box can write it, so a committed allowlist would let the
    yard widen its own wall.
    """
    from . import allowlist

    eff = allowlist.grant(host, level, ttl or None)
    print(f"✓ {host} allowed ({level}). Effective allowlist: {', '.join(eff['allow']) or 'empty'}")


@allow_app.command("list")
def allow_list() -> None:
    """Show the effective allowlist the proxy is enforcing (and whether it enforces at all)."""
    from . import allowlist

    eff = allowlist.effective()
    print(f"default_deny: {'on — unlisted hosts are refused' if eff['default_deny'] else 'off'}")
    for host in eff["allow"]:
        print(f"  {host}")
    if not eff["allow"]:
        print("  (no grants — `fy allow add <host>`, or the TUI's `a` key on a blocked row)")
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
    and only your per-host answer here grants (also asked at `fy up`/`fy host`, and offered in the
    TUI's Network Log).

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


def _config_bound():
    """The ADOPTED config for the active checkout, bound — what every recommendation read keys
    off (`devmode.worktree_config` funnels through the pin)."""
    from . import config, devmode

    return config.using(devmode.worktree_config(config.active_worktree()))


@allow_app.command("wall")
def allow_wall(
    state: str = typer.Argument(..., help="on (enforce the allowlist) | off (observe only)"),
) -> None:
    """Turn egress ENFORCEMENT on or off.

    Host-owned, like the grants: `[proxy] default_deny` only seeds the first answer, because repo
    config is writable from inside the box and an enforcement switch the yard can flip is no switch.
    """
    from . import allowlist

    if state not in ("on", "off"):
        raise typer.BadParameter("expected `on` or `off`")
    eff = allowlist.set_wall(state == "on")
    print(f"✓ egress wall {'ENFORCING' if eff['default_deny'] else 'observing only'}")


@allow_app.command("remove")
def allow_remove(
    host: str = typer.Argument(..., help="host/glob to revoke, at whatever level it was granted"),
) -> None:
    """Revoke a host's grant."""
    from . import allowlist

    eff = allowlist.revoke(host)
    print(f"✓ {host} revoked. Effective allowlist: {', '.join(eff['allow']) or 'empty'}")


config_app = typer.Typer(
    name="config",
    help="the foldyard.toml this host ADOPTED — what the supervisor actually runs (Mac)",
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

    The host reconciles its credential daemons from the ADOPTED copy (kept in the Mac home,
    outside the repo mount) — repo config is writable from inside the box, so a live-read
    foldyard.toml would let the yard retarget its own injection and switch off its own capture.
    """
    _refuse_in_box()

    from . import configpin

    drift = configpin.inspect(_active_config())
    if not drift.pinned_exists:
        print(f"✗ nothing adopted yet [{drift.label}] — `fy up`, `fy host`, or `fy config adopt`.")
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

    On a worktree that has never been adopted there is nothing on that axis to diff, so it shows
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
    name="worktree",
    help="host-sibling worktrees sharing the one rootless machine (Mac)",
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
) -> None:
    """Create a host-sibling worktree + init its per-project config."""
    from . import worktree

    raise typer.Exit(worktree.add(name, branch, base))


@worktree_app.command("init")
def worktree_init(
    name: str = typer.Argument(..., help="worktree dir name (under the worktrees root)"),
) -> None:
    """Re-run the consumer's worktree-init script for a worktree, in the yard.

    `worktree add` does this for you; use this when it skipped (no box image built yet) or after
    changing the init script."""
    from . import worktree

    raise typer.Exit(worktree.init_config(name))


@worktree_app.command("remove")
def worktree_remove(
    name: str = typer.Argument(..., help="worktree dir name to remove (under the worktrees root)"),
    force: bool = typer.Option(
        False, "--force", "-f", help="remove even if archiving fails or the tree is dirty"
    ),
    yes: bool = typer.Option(False, "--yes", "-y", help="skip the confirmation prompt"),
) -> None:
    """Tear down the worktree's dev box + stack, archive agent transcripts, then remove it."""
    from . import worktree

    raise typer.Exit(worktree.remove(name, force, assume_yes=yes))


@worktree_app.command("list")
def worktree_list() -> None:
    """List main + each worktree with its branch."""
    from . import worktree

    raise typer.Exit(worktree.list_())


skill_app = typer.Typer(
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
