from __future__ import annotations

from types import SimpleNamespace

from foldyard import browser, config, stack


def test_opens_configured_app_port_without_starting_machine(monkeypatch, capsys):
    monkeypatch.setattr(config, "in_box", lambda: False)
    monkeypatch.setattr(config, "app_port_key", lambda: "WEB_PORT")
    resolved: list[bool] = []
    monkeypatch.setattr(
        stack,
        "resolve",
        lambda no_machine=False: (
            resolved.append(no_machine) or SimpleNamespace(env={"WEB_PORT": "3073"})
        ),
    )
    opened: list[tuple[str, int]] = []
    monkeypatch.setattr(
        browser.webbrowser, "open", lambda url, new=0: opened.append((url, new)) or True
    )

    assert browser.open_app() == 0
    assert resolved == [True]
    assert opened == [("http://localhost:3073", 2)]
    assert "opening http://localhost:3073" in capsys.readouterr().out


def test_refuses_inside_box_before_resolving_stack(monkeypatch, capsys):
    monkeypatch.setattr(config, "in_box", lambda: True)
    monkeypatch.setattr(stack, "resolve", lambda **_: (_ for _ in ()).throw(AssertionError))

    assert browser.open_app() == 1
    assert "must run on the host" in capsys.readouterr().err


def test_requires_explicit_app_port_key(monkeypatch, capsys):
    monkeypatch.setattr(config, "in_box", lambda: False)
    monkeypatch.setattr(config, "app_port_key", lambda: None)
    monkeypatch.setattr(stack, "resolve", lambda **_: SimpleNamespace(env={}))

    assert browser.open_app() == 1
    assert "[project].app_port" in capsys.readouterr().err


def test_fails_when_configured_port_value_missing(monkeypatch, capsys):
    # app_port names a key, but [ports].{key} has no value in the resolved env — fail via the
    # error path, never open http://localhost: with an empty port.
    monkeypatch.setattr(config, "in_box", lambda: False)
    monkeypatch.setattr(config, "app_port_key", lambda: "WEB_PORT")
    monkeypatch.setattr(stack, "resolve", lambda **_: SimpleNamespace(env={"WEB_PORT": ""}))
    opened: list[str] = []
    monkeypatch.setattr(browser.webbrowser, "open", lambda url, new=0: opened.append(url) or True)

    assert browser.open_app() == 1
    assert opened == []
    assert "[ports].WEB_PORT is not configured" in capsys.readouterr().err
