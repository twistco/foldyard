"""paste.py — `fy paste`: what counts as an image (the guard on what leaves your computer), where
the clipboard is read from, and the argv that writes into the box and types into a session. No
clipboard, engine or tmux runs: subprocess.run is stubbed wherever one would."""

from __future__ import annotations

import base64
import contextlib
import os
import subprocess

import pytest

from foldyard import agents, config, paste

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32


class _Proc:
    def __init__(self, rc: int = 0, out: bytes | str = b"", err: bytes | str = b"") -> None:
        self.returncode, self.stdout, self.stderr = rc, out, err


@pytest.fixture
def host(monkeypatch):
    """On your computer, nothing on PATH, every subprocess call recorded and answered by `replies`
    (first key found in the argv wins)."""
    monkeypatch.setattr(config, "in_box", lambda: False)
    monkeypatch.setattr(paste.shutil, "which", lambda name: None)
    calls: list[tuple[list[str], dict]] = []
    replies: dict[str, _Proc] = {}

    def run(cmd, **kw):
        calls.append((list(cmd), kw))
        for key, proc in replies.items():
            if key in cmd:
                return proc
        return _Proc(0, b"" if not kw.get("text") else "")

    def bounded(argv, *, data=None, out_limit=0, stop_over=False, timeout, env=None):
        calls.append((list(argv), {"input": data, "env": env, "out_limit": out_limit}))
        for key, proc in replies.items():
            if key in argv:
                out, err = (
                    x.encode() if isinstance(x, str) else x for x in (proc.stdout, proc.stderr)
                )
                over = len(out) > out_limit
                return paste._Ran(
                    None if over and stop_over else proc.returncode,
                    out[:out_limit],
                    err[: paste._ERR_KEEP],
                    over,
                    True,
                )
        return paste._Ran(0, b"", b"", False, True)

    monkeypatch.setattr(subprocess, "run", run)
    monkeypatch.setattr(paste, "_bounded", bounded)
    return calls, replies


# ── what is sent ────────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("data", "kind"),
    [
        (PNG, "png"),
        (b"\xff\xd8\xff\xe0" + b"\x00" * 8, "jpg"),
        (b"GIF89a....", "gif"),
        (b"GIF87a....", "gif"),
        (b"RIFF\x10\x00\x00\x00WEBPVP8 ", "webp"),
        (b"RIFF\x10\x00\x00\x00WAVEfmt ", None),
        (b"-----BEGIN OPENSSH PRIVATE KEY-----\n", None),
        (b"", None),
    ],
)
def test_image_kind_by_leading_bytes(data, kind):
    assert paste.image_kind(data) == kind


def test_check_refuses_what_isnt_an_image_or_is_too_large(monkeypatch):
    assert paste.check(PNG) == ("png", "")
    assert paste.check(b"hello")[0] is None
    monkeypatch.setattr(paste, "MAX_BYTES", 16)
    kind, why = paste.check(PNG)
    assert kind is None and "too large" in why


def test_from_file_reads_a_regular_file_and_stops_past_the_cap(tmp_path, monkeypatch):
    f = tmp_path / "shot.png"
    f.write_bytes(PNG)
    assert paste.from_file(str(f)) == (PNG, "")
    monkeypatch.setattr(paste, "MAX_BYTES", 4)
    data, _ = paste.from_file(str(f))
    assert data == PNG[:5]  # one byte past the cap: enough to say "too large", no more


def test_from_file_refuses_a_fifo_without_blocking(tmp_path):
    fifo = tmp_path / "pipe.png"
    os.mkfifo(fifo)
    data, why = paste.from_file(str(fifo))
    assert data is None and "regular file" in why


def test_from_file_says_why_a_missing_file_fails(tmp_path):
    data, why = paste.from_file(str(tmp_path / "nope.png"))
    assert data is None and "can't open" in why


def test_a_symlink_the_box_planted_at_a_secret_sends_nothing(host, tmp_path, capsys):
    """A path in the checkout may be a link the box made to a file of yours: only image bytes
    leave your computer, and the refusal comes before any engine call."""
    calls, _ = host
    secret = tmp_path / "id_ed25519"
    secret.write_text("-----BEGIN OPENSSH PRIVATE KEY-----\n")
    link = tmp_path / "checkout-shot.png"
    link.symlink_to(secret)
    assert paste.paste(str(link)) == 1
    assert calls == []
    assert "symlink" in capsys.readouterr().err


def test_a_symlink_to_another_of_your_images_sends_nothing(host, tmp_path, capsys):
    """The type check can't tell which image: the box swaps `shot.png` for a link to another one
    of yours (a scanned ID, a screenshot on your desktop). The link itself is refused."""
    calls, _ = host
    private = tmp_path / "Desktop" / "passport.png"
    private.parent.mkdir()
    private.write_bytes(PNG)
    shot = tmp_path / "checkout" / "shot.png"
    shot.parent.mkdir()
    shot.symlink_to(private)
    assert paste.paste(str(shot)) == 1
    assert calls == []
    assert "symlink" in capsys.readouterr().err


def test_a_planted_directory_link_on_the_way_is_refused(tmp_path):
    (tmp_path / "Desktop").mkdir()
    (tmp_path / "Desktop" / "passport.png").write_bytes(PNG)
    (tmp_path / "checkout").mkdir()
    (tmp_path / "checkout" / "shots").symlink_to(tmp_path / "Desktop")
    data, why = paste.from_file(str(tmp_path / "checkout" / "shots" / "passport.png"))
    assert data is None and "symlink" in why


def test_a_relative_path_is_read_from_the_working_directory(tmp_path, monkeypatch):
    (tmp_path / "shot.png").write_bytes(PNG)
    monkeypatch.chdir(tmp_path)
    assert paste.from_file("shot.png") == (PNG, "")


# ── the clipboard ───────────────────────────────────────────────────────────────────────────────


def test_no_clipboard_reader_says_to_name_a_file(host, monkeypatch):
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    monkeypatch.delenv("DISPLAY", raising=False)
    data, why = paste.from_clipboard()
    assert data is None and "fy paste <file>" in why


def test_osascript_reads_the_pasteboard_as_base64(host, monkeypatch):
    calls, replies = host
    monkeypatch.setattr(
        paste.shutil, "which", lambda n: "/usr/bin/osascript" if n == "osascript" else None
    )
    replies["osascript"] = _Proc(0, base64.b64encode(PNG).decode() + "\n")
    assert paste.from_clipboard() == (PNG, "")
    argv = calls[0][0]
    assert argv[:4] == ["osascript", "-l", "JavaScript", "-e"]
    assert "public.png" in argv[4] and "public.tiff" in argv[4]


def test_an_empty_pasteboard_is_no_image(host, monkeypatch):
    _, replies = host
    monkeypatch.setattr(
        paste.shutil, "which", lambda n: "/usr/bin/osascript" if n == "osascript" else None
    )
    replies["osascript"] = _Proc(0, "\n")
    data, why = paste.from_clipboard()
    assert data is None and "no image" in why


def test_a_clipboard_image_past_the_limit_is_refused_not_read_in_full(host, monkeypatch):
    calls, replies = host
    monkeypatch.setattr(
        paste.shutil, "which", lambda n: "/usr/bin/osascript" if n == "osascript" else None
    )
    monkeypatch.setattr(paste, "_B64_LIMIT", 8)
    replies["osascript"] = _Proc(0, base64.b64encode(PNG).decode())
    data, why = paste.from_clipboard()
    assert data is None and "too large" in why
    assert calls[0][1]["out_limit"] == 8


def test_wl_paste_asks_for_png_only_when_the_clipboard_offers_it(host, monkeypatch):
    calls, replies = host
    monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-0")
    monkeypatch.setattr(
        paste.shutil, "which", lambda n: "/usr/bin/wl-paste" if n == "wl-paste" else None
    )
    replies["--list-types"] = _Proc(0, "text/plain\nUTF8_STRING\n")
    assert paste.from_clipboard()[0] is None
    assert [c[0] for c in calls] == [["wl-paste", "--list-types"]]
    replies["--list-types"] = _Proc(0, "image/png\n")
    replies["--type"] = _Proc(0, PNG)
    assert paste.from_clipboard() == (PNG, "")


def test_xclip_under_x11(host, monkeypatch):
    _, replies = host
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    monkeypatch.setenv("DISPLAY", ":0")
    monkeypatch.setattr(paste.shutil, "which", lambda n: "/usr/bin/xclip" if n == "xclip" else None)
    replies["TARGETS"] = _Proc(0, "TARGETS\nimage/png\n")
    replies["image/png"] = _Proc(0, PNG)
    assert paste.from_clipboard() == (PNG, "")


# ── into the box ────────────────────────────────────────────────────────────────────────────────


def test_box_path_is_fresh_and_plain():
    a, b = paste.box_path("png"), paste.box_path("png")
    assert a != b
    assert a.startswith(f"{paste.IN_BOX_DIR}/") and a.endswith(".png")
    assert all(c.isalnum() or c in "/-." for c in a)


def test_write_argv_passes_the_path_as_an_argument_never_in_the_script():
    argv = paste.write_argv("podman", "acme-devbox", "/tmp/fy-paste/x.png")
    assert argv == ["podman", "exec", "-i", "acme-devbox", "sh", "-c", paste._WRITE, "sh",
                    "/tmp/fy-paste/x.png"]  # fmt: skip


@pytest.mark.spawns("sh")
def test_the_write_script_lands_the_whole_file_and_leaves_no_part(tmp_path):
    # The suite's PATH holds no mkdir/cat/mv (conftest's hermetic section); the box's does.
    target = tmp_path / "fy-paste" / "x.png"
    env = {"PATH": "/usr/bin:/bin"}
    subprocess.run(["sh", "-c", paste._WRITE, "sh", str(target)], input=PNG, env=env, check=True)
    assert target.read_bytes() == PNG
    assert sorted(p.name for p in target.parent.iterdir()) == ["x.png"]
    assert oct(target.stat().st_mode & 0o777) == oct(0o600)


def test_paste_argv_is_one_bracketed_paste_into_the_exact_session():
    argv = paste.paste_argv("claude-3f2c9a1e", "/tmp/fy-paste/x.png")
    assert argv == [
        "tmux", "-L", "fy",
        "set-buffer", "-b", "fy-paste", "/tmp/fy-paste/x.png", ";",
        "paste-buffer", "-p", "-d", "-b", "fy-paste", "-t", "=claude-3f2c9a1e:",
    ]  # fmt: skip


def _box_target(monkeypatch, pane=""):
    b = agents.Box("acme-devbox", "", "podman", {"CONTAINER_HOST": "unix:///nonexistent/fy.sock"})
    monkeypatch.setattr(paste, "_target", lambda session: (b, pane))
    return b


def test_paste_writes_into_the_box_and_copies_the_path(host, monkeypatch, tmp_path, capsys):
    calls, _ = host
    _box_target(monkeypatch)
    monkeypatch.setattr(paste, "_copy_argv", lambda: ["pbcopy"])
    f = tmp_path / "shot.png"
    f.write_bytes(PNG)
    assert paste.paste(str(f)) == 0
    (write, kw), (copy, ckw) = calls
    assert write[:7] == ["podman", "exec", "-i", "acme-devbox", "sh", "-c", paste._WRITE]
    assert kw["input"] == PNG and kw["env"]["CONTAINER_HOST"].startswith("unix:///nonexistent")
    assert copy == ["pbcopy"] and ckw["input"] == write[-1]
    # xclip/wl-copy fork a child that holds any captured pipe open: never capture
    assert ckw["stdout"] is subprocess.DEVNULL and ckw["stderr"] is subprocess.DEVNULL
    assert "capture_output" not in ckw
    out = capsys.readouterr().out
    assert write[-1] in out and "path copied" in out


def test_no_copy_only_prints_the_path(host, monkeypatch, tmp_path, capsys):
    calls, _ = host
    _box_target(monkeypatch)
    monkeypatch.setattr(paste, "_copy_argv", lambda: ["pbcopy"])
    f = tmp_path / "shot.png"
    f.write_bytes(PNG)
    assert paste.paste(str(f), copy=False) == 0
    assert len(calls) == 1
    assert "give the agent that path" in capsys.readouterr().out


def test_paste_types_the_path_into_a_session(host, monkeypatch, tmp_path, capsys):
    calls, _ = host
    _box_target(monkeypatch, pane="claude-3f2c9a1e")
    f = tmp_path / "shot.png"
    f.write_bytes(PNG)
    assert paste.paste(str(f), session="3f2c9a1e") == 0
    write, typed = calls[0][0], calls[1][0]
    assert typed == ["podman", "exec", "acme-devbox",
                     *paste.paste_argv("claude-3f2c9a1e", write[-1])]  # fmt: skip
    assert "typed into claude-3f2c9a1e" in capsys.readouterr().out


def test_a_session_that_cant_be_typed_into_still_names_the_path(
    host, monkeypatch, tmp_path, capsys
):
    calls, _ = host
    _box_target(monkeypatch, pane="claude-3f2c9a1e")

    def hang(cmd, **kw):  # the box's tmux never answers
        raise subprocess.TimeoutExpired(cmd, 10)

    monkeypatch.setattr(subprocess, "run", hang)
    f = tmp_path / "shot.png"
    f.write_bytes(PNG)
    assert paste.paste(str(f), session="3f2c9a1e") == 1
    err = capsys.readouterr().err
    assert calls[0][0][-1] in err and "typing it into claude-3f2c9a1e failed" in err


def test_a_session_tmux_refuses_silently_is_still_a_failure(host, monkeypatch, tmp_path, capsys):
    """A non-zero exit with nothing on stderr is a failure too — never "typed into"."""
    _, replies = host
    _box_target(monkeypatch, pane="claude-3f2c9a1e")
    replies["paste-buffer"] = _Proc(1, "", "")
    f = tmp_path / "shot.png"
    f.write_bytes(PNG)
    assert paste.paste(str(f), session="3f2c9a1e") == 1
    captured = capsys.readouterr()
    assert "tmux exited 1" in captured.err and "typed into" not in captured.out


def test_an_incomplete_clipboard_read_is_no_image(monkeypatch):
    """A reader that exits 0 while something it started still holds its stdout may have handed
    over half an image, which would still pass the type check."""
    monkeypatch.setattr(
        paste, "_bounded", lambda *a, **k: paste._Ran(0, PNG[:10], b"", False, False)
    )
    assert paste._capture(["wl-paste", "--type", "image/png"], paste.MAX_BYTES) is None


def test_a_failed_write_says_so_with_the_boxs_words_cut_to_printable(
    host, monkeypatch, tmp_path, capsys
):
    _, replies = host
    _box_target(monkeypatch)
    replies["exec"] = _Proc(1, b"", b"no space\x1b]52;c;evil\x07")
    f = tmp_path / "shot.png"
    f.write_bytes(PNG)
    assert paste.paste(str(f)) == 1
    err = capsys.readouterr().err
    assert "couldn't write into acme-devbox: no space" in err and "\x1b" not in err


def test_refused_in_a_box(monkeypatch, capsys):
    monkeypatch.setattr(config, "in_box", lambda: True)
    assert paste.paste() == 1
    assert "on your computer" in capsys.readouterr().err


def test_target_finds_the_box_holding_the_session(monkeypatch):
    from foldyard import stack

    monkeypatch.setattr(stack, "engine_reachable", lambda *a, **k: True)
    monkeypatch.setattr(config, "active_worktree", lambda: "")
    boxes = [
        agents.Box("acme-devbox", "", "podman", {}),
        agents.Box("acme-feat-devbox", "feat", "podman", {}),
    ]
    monkeypatch.setattr(agents, "project_boxes", lambda: (boxes, None))
    session = agents.Session("claude-3f2c9a1e", "3f2c9a1e-0b4d-4c3e-9f1a-2b3c4d5e6f70", "claude",
                             "", "", 0, True, None)  # fmt: skip
    monkeypatch.setattr(
        agents,
        "sessions_in",
        lambda b: [agents._place(session, b)] if b.worktree == "feat" else [],
    )
    b, pane = paste._target("3f2c9a1e")
    assert b is not None
    assert (b.name, pane) == ("acme-feat-devbox", "claude-3f2c9a1e")
    assert paste._target("nope") == (None, "")


# ── the bounded runner, for real (a shell's builtins only: the suite's PATH has no programs) ──────


@pytest.mark.spawns("sh")
def test_bounded_feeds_stdin_and_keeps_exit_status_and_stderr():
    ran = paste._bounded(
        [
            "sh",
            "-c",
            'n=0; while IFS= read -r l; do n=$((n+1)); done; printf "$n lines" >&2; exit 3',
        ],
        data=b"a\nb\nc\n",
        timeout=10,
    )
    assert ran is not None
    assert (ran.rc, ran.err, ran.out, ran.over) == (3, b"3 lines", b"", False)


@pytest.mark.spawns("sh")
def test_bounded_keeps_only_a_prefix_of_a_flood_of_stderr():
    ran = paste._bounded(
        ["sh", "-c", "i=0; while [ $i -lt 2000 ]; do printf 0123456789 >&2; i=$((i+1)); done"],
        data=b"",
        timeout=10,
    )
    assert ran is not None and ran.rc == 0
    assert len(ran.err) == paste._ERR_KEEP


@pytest.mark.spawns("sh")
def test_bounded_stops_a_reader_past_its_limit():
    ran = paste._bounded(
        ["sh", "-c", "while :; do printf 0123456789; done"],
        out_limit=100,
        stop_over=True,
        timeout=10,
    )
    assert ran is not None
    assert (ran.rc, ran.over, len(ran.out)) == (None, True, 100)


@pytest.mark.spawns("sh")
def test_bounded_kills_at_the_timeout():
    ran = paste._bounded(["sh", "-c", "while :; do :; done"], data=b"x", timeout=0.3)
    assert ran is not None and ran.rc is None and not ran.over


@pytest.mark.spawns("sh")
def test_bounded_survives_a_child_that_never_reads_its_stdin():
    ran = paste._bounded(["sh", "-c", "exit 0"], data=PNG * 100_000, timeout=10)
    assert ran is not None and ran.rc == 0


@pytest.mark.spawns("sh")
def test_bounded_says_incomplete_when_a_grandchild_holds_stdout(monkeypatch, tmp_path):
    """The child exits 0, but a background subshell it left keeps stdout open past the grace —
    blocked on a FIFO until the test lets it go, so nothing outlives the test."""
    monkeypatch.setattr(paste, "_DRAIN_GRACE", 0.2)
    fifo = tmp_path / "hold"
    os.mkfifo(fifo)
    script = '(read -r x < "$1") & printf head; exit 0'
    try:
        ran = paste._bounded(["sh", "-c", script, "sh", str(fifo)], out_limit=100, timeout=10)
    finally:
        # The subshell's read sees EOF and it exits. Non-blocking: with no reader left, an open
        # for writing would wait forever (ENXIO instead).
        with contextlib.suppress(OSError):
            os.close(os.open(fifo, os.O_WRONLY | os.O_NONBLOCK))
    assert ran is not None
    assert (ran.rc, ran.out, ran.complete) == (0, b"head", False)


@pytest.mark.spawns("sh")
def test_bounded_reports_its_status_when_it_exits_on_its_own():
    ran = paste._bounded(["sh", "-c", "printf ok; exit 0"], out_limit=100, timeout=10)
    assert ran is not None
    assert (ran.rc, ran.out, ran.complete) == (0, b"ok", True)
