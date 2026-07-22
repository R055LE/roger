"""Guard rules — pure functions, exhaustively unit-tested."""

import pytest

from roger.tools.guard import (
    GuardError,
    check_no_duplicate,
    parse_color,
    resolve_one,
    sanitize_channel_name,
    sanitize_display_name,
)


def test_channel_name_slugified():
    assert sanitize_channel_name("  Podcast Room! ") == "podcast-room"


def test_channel_name_collapses_dashes():
    assert sanitize_channel_name("a   b") == "a-b"


def test_channel_name_rejects_empty_after_sanitize():
    with pytest.raises(GuardError):
        sanitize_channel_name("!!!")


def test_display_name_keeps_case_strips_control_chars():
    assert sanitize_display_name("  DJs\x07  ") == "DJs"


def test_display_name_rejects_empty():
    with pytest.raises(GuardError):
        sanitize_display_name("   ")


def test_duplicate_is_case_insensitive():
    with pytest.raises(GuardError):
        check_no_duplicate("role", "DJs", ["djs", "Mods"])


def test_no_duplicate_passes():
    check_no_duplicate("role", "DJs", ["Mods", "Admins"])  # no raise


def test_resolve_by_name_case_insensitive():
    assert resolve_one("media", [(1, "Media"), (2, "General")]) == (1, "Media")


def test_resolve_by_id():
    assert resolve_one("2", [(1, "Media"), (2, "General")]) == (2, "General")


def test_resolve_ambiguous_raises():
    with pytest.raises(GuardError):
        resolve_one("media", [(1, "Media"), (2, "media")])


def test_resolve_not_found_raises():
    with pytest.raises(GuardError):
        resolve_one("nope", [(1, "Media")])


def test_parse_color_accepts_hex_with_and_without_hash():
    assert parse_color("#00FF00") == 0x00FF00
    assert parse_color("0000ff") == 0x0000FF


def test_parse_color_rejects_garbage():
    with pytest.raises(GuardError):
        parse_color("green")
