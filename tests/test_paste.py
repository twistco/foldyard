"""paste.py — `fy paste`: what counts as an image (the guard on what leaves your computer), where
the clipboard is read from, and the argv that writes into the box and types into a session. No
clipboard, engine or tmux runs: subprocess.run is stubbed wherever one would."""

from __future__ import annotations

import base64
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

    monkeypatch.setattr(subprocess, "run", run)
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
    assert "not a PNG" in capsys.readouterr().err


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
