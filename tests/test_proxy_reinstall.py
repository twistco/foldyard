"""A reinstall of foldyard under a running supervisor (`just pin`, `uv tool install --force`)
removes the venv `mitmdump` lives in and makes it again. A supervisor tick in that gap fell back to a
bare `mitmdump`, saw a changed command and restarted a healthy proxy: ~2.3 s without egress, every
connection cut, and a "not started" notification (nine times in November 2026 on one computer)."""

from __future__ import annotations

import sys

import pytest

from foldyard import supervisor
from foldyard.plugins import Registry, proxy
from test_plugins import _Github
from test_supervisor import _fake_child


@pytest.fixture
def venv(tmp_path, monkeypatch):
    """foldyard's own venv, its `mitmdump` in place, in a process that hasn't launched a proxy."""
    bin_dir = tmp_path / "venv" / "bin"
    bin_dir.mkdir(parents=True)
    (bin_dir / "mitmdump").write_text("")
    monkeypatch.setattr(sys, "executable", str(bin_dir / "python"))
    monkeypatch.setattr(proxy, "_launched_mitmdump", None)
    return bin_dir / "mitmdump"


def _spec() -> dict:
    reg = Registry([_Github(), proxy.ProxyPlugin()])
    return reg.desired_daemons({"github": "on"})["egress-proxy"]


def test_a_running_proxy_is_kept_while_the_venv_is_remade(venv):
    spec = _spec()
    child = _fake_child(signature=repr((spec["cmd"], sorted(spec["env"].items()))), alive=True)
    venv.unlink()
    assert supervisor._child_step(child, _spec()) is supervisor.ChildStep.KEEP


def test_another_mitmdump_on_path_does_not_replace_the_venvs_in_the_gap(venv, monkeypatch):
    assert _spec()["cmd"][0] == str(venv)
    venv.unlink()
    monkeypatch.setattr(proxy.shutil, "which", lambda name: f"/elsewhere/{name}")
    assert _spec()["cmd"][0] == str(venv)


def test_the_path_copy_is_kept_while_it_is_missing_and_followed_when_it_moves(venv, monkeypatch):
    venv.unlink()
    found = {"mitmdump": "/a/mitmdump"}
    monkeypatch.setattr(proxy.shutil, "which", lambda name: found.get(name))
    assert _spec()["cmd"][0] == "/a/mitmdump"
    found.clear()
    assert _spec()["cmd"][0] == "/a/mitmdump"
    found["mitmdump"] = "/b/mitmdump"
    assert _spec()["cmd"][0] == "/b/mitmdump"


def test_the_venvs_copy_takes_over_from_path(venv, monkeypatch):
    venv.unlink()
    monkeypatch.setattr(proxy.shutil, "which", lambda name: "/a/mitmdump")
    assert _spec()["cmd"][0] == "/a/mitmdump"
    venv.write_text("")
    assert _spec()["cmd"][0] == str(venv)


def test_one_never_seen_is_still_the_bare_name(venv, monkeypatch):
    # The supervisor's "mitmproxy installed?" nag fires on it.
    venv.unlink()
    monkeypatch.setattr(proxy.shutil, "which", lambda name: None)
    assert _spec()["cmd"][0] == "mitmdump"


def test_doctor_and_preflight_still_see_it_is_gone(venv, monkeypatch):
    _spec()
    venv.unlink()
    monkeypatch.setattr(proxy.shutil, "which", lambda name: None)
    assert proxy.mitmdump_path() is None
