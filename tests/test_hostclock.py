"""hostclock.py — your computer's timezone name and time locale, read for the dev box. Every
test points the readers at tmp-path stand-ins: never the real ``/etc/localtime`` or the
operator's environment."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from foldyard import hostclock


def _link(tmp_path: Path, target: str) -> Path:
    link = tmp_path / "localtime"
    os.symlink(target, link)
    return link


def _zone(tmp_path: Path, localtime: Path, timezone: str | None = None, **env: str) -> str | None:
    tz_file = tmp_path / "timezone"
    if timezone is not None:
        tz_file.write_text(timezone)
    return hostclock.zone(environ=env, localtime=localtime, timezone_file=tz_file)


# ── zone(): the IANA name ─────────────────────────────────────────────────────────────


def test_zone_from_the_macos_symlink_shape(tmp_path):
    link = _link(tmp_path, "/var/db/timezone/zoneinfo/Europe/London")
    assert _zone(tmp_path, link) == "Europe/London"


def test_zone_from_the_linux_relative_symlink_shape(tmp_path):
    link = _link(tmp_path, "../usr/share/zoneinfo/America/Argentina/Buenos_Aires")
    assert _zone(tmp_path, link) == "America/Argentina/Buenos_Aires"


def test_zone_names_with_signs_and_digits(tmp_path):
    assert _zone(tmp_path, _link(tmp_path, "/usr/share/zoneinfo/Etc/GMT+1")) == "Etc/GMT+1"


@pytest.mark.parametrize("variant", ["posix", "right"])
def test_zone_drops_the_posix_and_right_trees(tmp_path, variant):
    # Same zone, a different rules tree: the box's runtimes know the bare name, not the tree.
    link = _link(tmp_path, f"/usr/share/zoneinfo/{variant}/Asia/Tokyo")
    assert _zone(tmp_path, link) == "Asia/Tokyo"


def test_zone_follows_a_chain_whose_first_hop_names_no_zoneinfo(tmp_path):
    # NixOS-style: /etc/localtime → /etc/static/… → …/zoneinfo/<zone>.
    store = tmp_path / "share" / "zoneinfo" / "Europe"
    store.mkdir(parents=True)
    (store / "Rome").write_bytes(b"TZif")
    hop = tmp_path / "static-localtime"
    os.symlink(store / "Rome", hop)
    assert _zone(tmp_path, _link(tmp_path, str(hop))) == "Europe/Rome"


def test_zone_falls_back_to_etc_timezone_when_localtime_is_a_plain_file(tmp_path):
    localtime = tmp_path / "localtime"
    localtime.write_bytes(b"TZif")  # a copied zone file names nothing
    assert _zone(tmp_path, localtime, "Europe/Berlin\n") == "Europe/Berlin"


def test_zone_falls_back_to_etc_timezone_when_the_link_names_no_zoneinfo(tmp_path):
    link = _link(tmp_path, "/etc/static/localtime")
    assert _zone(tmp_path, link, "Pacific/Auckland") == "Pacific/Auckland"


def test_zone_is_none_when_nothing_names_a_zone(tmp_path):
    assert _zone(tmp_path, tmp_path / "missing") is None
    assert _zone(tmp_path, _link(tmp_path, "/nowhere/at/all")) is None


@pytest.mark.parametrize(
    "bad",
    [
        "../../etc/passwd",
        "Europe/London; rm -rf /",
        "Europe/London\nEVIL=1",
        "Europe//London",
        "/Europe/London",
        "Europe/",
        "",
    ],
)
def test_zone_refuses_a_name_outside_the_strict_pattern(tmp_path, bad):
    assert _zone(tmp_path, tmp_path / "missing", bad) is None


def test_zone_refuses_a_link_whose_name_fails_the_pattern(tmp_path):
    link = _link(tmp_path, "/usr/share/zoneinfo/Europe/Lon don")
    assert _zone(tmp_path, link, "Europe/Paris") == "Europe/Paris"


def test_an_exported_tz_wins_over_the_system_zone(tmp_path):
    link = _link(tmp_path, "/usr/share/zoneinfo/Europe/London")
    assert _zone(tmp_path, link, TZ="America/New_York") == "America/New_York"
    assert _zone(tmp_path, link, TZ=":Asia/Kolkata") == "Asia/Kolkata"


def test_a_posix_rule_tz_falls_through_to_the_system_zone(tmp_path):
    # A POSIX rule string is no IANA name — the box's ICU runtimes can't read it.
    link = _link(tmp_path, "/usr/share/zoneinfo/Europe/London")
    assert _zone(tmp_path, link, TZ="GMT0BST,M3.5.0/1,M10.5.0") == "Europe/London"


# ── time_locale(): the locale that formats times ──────────────────────────────────────


@pytest.mark.parametrize(
    ("env", "want"),
    [
        ({"LANG": "en_GB.UTF-8"}, "en_GB.UTF-8"),
        ({"LANG": "en_US.UTF-8", "LC_TIME": "en_GB.UTF-8"}, "en_GB.UTF-8"),
        ({"LC_TIME": "en_GB.UTF-8", "LC_ALL": "de_DE.UTF-8"}, "de_DE.UTF-8"),
        ({"LC_ALL": "", "LANG": "fr_FR.UTF-8"}, "fr_FR.UTF-8"),
        ({"LANG": "sr_RS.UTF-8@latin"}, "sr_RS.UTF-8@latin"),
        ({"LANG": "es_419.UTF-8"}, "es_419.UTF-8"),
        ({}, None),
        ({"LANG": "C"}, None),
        ({"LANG": "POSIX"}, None),
        ({"LANG": "C.UTF-8"}, None),
        ({"LANG": "en_GB.UTF-8 EVIL=1"}, None),
        ({"LANG": "../../tmp/x"}, None),
    ],
)
def test_time_locale_follows_the_lc_time_precedence(env, want):
    assert hostclock.time_locale(environ=env) == want


# ── locale_available(): does the box image have it? ───────────────────────────────────


@pytest.mark.parametrize(
    ("name", "listed", "want"),
    [
        ("en_GB.UTF-8", ["C", "C.utf8", "POSIX", "en_GB.utf8"], True),
        ("en_GB.utf8", ["en_GB.UTF-8"], True),
        ("sr_RS.UTF-8@latin", ["sr_RS.utf8@latin"], True),
        ("en_GB.UTF-8", ["C", "C.utf8", "POSIX"], False),
        ("en_GB.UTF-8", ["en_GB"], False),
        ("en_GB", ["en_GB.utf8"], False),
        ("en_GB.UTF-8", ["en_US.utf8"], False),
        ("sr_RS.UTF-8@latin", ["sr_RS.utf8"], False),
    ],
)
def test_locale_available_matches_glibc_codeset_spellings(name, listed, want):
    assert hostclock.locale_available(name, listed) is want
