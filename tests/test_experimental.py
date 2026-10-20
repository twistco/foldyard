"""experimental.py — the register of experimental features: every entry says what would graduate
it, docs/configuration.md tags exactly the register's entries (both ways), and `fy doctor` names
the ones the config turns on."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from foldyard import config, devmode, experimental

DOCS = Path(__file__).resolve().parents[1] / "docs" / "configuration.md"
# A tag that marks a setting follows its code span: "**`runtime`** *(experimental)*" or
# "`krunkit` *(experimental)*". The section that explains the label mentions it bare.
TAG = re.compile(r"`[^`\n]+`(?:\*\*)? \*\(experimental\)\*")


@pytest.mark.parametrize("f", experimental.REGISTER, ids=lambda f: f.key)
def test_every_entry_says_what_is_unproven_and_what_would_graduate_it(f):
    # a label with no exit criterion stays forever, or is dropped quietly with nothing changed
    assert f.kinds and set(f.kinds) <= {experimental.MAY_CHANGE, experimental.UNPROVEN}
    assert f.unproven.strip() and f.graduates_when.strip()
    assert callable(getattr(config, f.reader)), f"config has no reader {f.reader!r}"


def test_the_docs_tag_exactly_the_registers_entries():
    text = DOCS.read_text()
    for f in experimental.REGISTER:
        assert f.doc_mark in text, f"docs/configuration.md lacks {f.doc_mark!r} for {f.key}"
    tags = TAG.findall(text)
    marks = [f.doc_mark for f in experimental.REGISTER]
    stray = [t for t in tags if not any(t in m for m in marks)]
    assert not stray, f"tagged in the docs but not in experimental.REGISTER: {stray}"
    assert len(tags) == len(marks), "a register entry's tag appears more than once"


def test_nothing_turned_on_is_one_quiet_green_row(monkeypatch):
    monkeypatch.delenv("MACHINE_RUNTIME", raising=False)
    monkeypatch.delenv("MACHINE_VMTYPE", raising=False)
    monkeypatch.setattr(config, "_table", lambda name: {})
    assert experimental.turned_on() == []
    assert experimental.doctor_row() == (True, "experimental", "none turned on")


def test_a_turned_on_feature_is_named_with_what_is_unproven(monkeypatch):
    monkeypatch.setenv("MACHINE_RUNTIME", "gvisor")
    monkeypatch.setenv("MACHINE_VMTYPE", "vz")
    on = experimental.turned_on()
    assert [f.key for f in on] == ["machine.runtime"]
    ok, name, detail = experimental.doctor_row()
    assert ok is True and name == "experimental"
    assert '[machine] runtime = "gvisor"' in detail and experimental.UNPROVEN in detail
    assert "krunkit" not in detail


def test_a_value_the_reader_refuses_reads_as_off_here(monkeypatch):
    # an invalid [machine] runtime is preflight's to refuse; the report doesn't crash on it
    monkeypatch.setenv("MACHINE_RUNTIME", "runc")
    assert all(f.key != "machine.runtime" for f in experimental.turned_on())


def test_the_doctor_row_reads_the_config_in_force(monkeypatch):
    monkeypatch.setenv("MACHINE_VMTYPE", "krunkit")
    status, name, detail = devmode._experimental_check()
    assert (status, name) == ("ok", "experimental")
    assert '[machine] vmtype = "krunkit"' in detail
