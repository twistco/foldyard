"""The gcp metadata emulator (plugins/gcp_metadata/server.py) — unit tier. The protocol itself
is exercised by the opt-in box e2e; this pins the server's own housekeeping."""

from __future__ import annotations

import importlib.util
import socket

import pytest

from foldyard.plugins import gcp


@pytest.fixture(scope="module")
def server_mod():
    spec = importlib.util.spec_from_file_location(
        "fy_metadata_server", gcp.METADATA_DIR / "server.py"
    )
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _raise_in_handle_error(srv, exc: BaseException):
    """Call ``handle_error`` the way socketserver does — from inside the ``except`` that caught
    ``exc``, so ``sys.exc_info()`` carries it."""
    try:
        raise exc
    except BaseException:
        srv.handle_error(socket.socket(), ("10.89.0.16", 58614))


def test_a_client_hanging_up_is_one_log_line_not_a_traceback(server_mod, capsys):
    # A token client that gives up mid-response (its own timeout, a container stopping) used to
    # dump a full socketserver traceback per request — read as the emulator being broken, when it
    # was the caller leaving.
    srv = server_mod.Server(("127.0.0.1", 0), server_mod.Handler)
    try:
        _raise_in_handle_error(srv, BrokenPipeError(32, "Broken pipe"))
        _raise_in_handle_error(srv, ConnectionResetError(104, "Connection reset by peer"))
    finally:
        srv.server_close()
    err = capsys.readouterr().err
    assert "Traceback" not in err
    assert err.count("[metadata] client 10.89.0.16 hung up") == 2


def test_other_handler_errors_still_trace(server_mod, capsys):
    srv = server_mod.Server(("127.0.0.1", 0), server_mod.Handler)
    try:
        _raise_in_handle_error(srv, RuntimeError("a real bug"))
    finally:
        srv.server_close()
    err = capsys.readouterr().err
    assert "Traceback" in err and "a real bug" in err
