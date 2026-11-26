# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 Samuel Ruairí Bullard

"""Tests for `quoth.keynames`, the shared evdev key display names."""

import pytest

from quoth.keynames import (
    format_hotkey,
    format_key,
    hotkey_risk,
    hotkey_warning_text,
    printable_label,
    split_hotkey,
)


class TestFormatKey:
    @pytest.mark.parametrize(
        ("key", "label"),
        [
            ("KEY_A", "A"),
            # A modifier must read the same everywhere a key is shown.
            ("KEY_LEFTCTRL", "Ctrl (Left)"),
            ("KEY_RIGHTMETA", "Super (Right)"),
            ("KEY_CAPSLOCK", "Caps Lock"),
            ("KEY_ESC", "Escape"),
            ("KEY_COMPOSE", "Menu"),
            ("KEY_SLASH", "/"),
            # An unknown token passes through without its prefix.
            ("KEY_F13", "F13"),
            ("SPACE", "Space"),
        ],
    )
    def test_label(self, key, label):
        assert format_key(key) == label

    def test_modifier_chords_have_a_stable_display_order(self):
        assert split_hotkey("SHIFT+CTRL+KEY_F13") == (("CTRL", "SHIFT"), "KEY_F13")
        assert format_hotkey("SHIFT+CTRL+KEY_F13") == "Ctrl+Shift+F13"


class TestHotkeyRisk:
    """Classification for warn-and-allow: None for a dedicated key, else the risk."""

    def test_dedicated_keys_are_safe(self):
        for key in ("KEY_F13", "KEY_F24", "KEY_PAUSE", "KEY_SCROLLLOCK"):
            assert hotkey_risk(key) is None, key

    def test_keys_that_act_in_the_focused_app_are_flagged(self):
        # The listener does not take the key, so the focused app gets each press too.
        for key in (
            "KEY_F1",
            "KEY_F12",
            "KEY_DELETE",
            "KEY_HOME",
            "KEY_UP",
            "KEY_SYSRQ",
            # Capture closes on Escape, but a layout option such as caps:swapescape
            # gives the Esc key another keyval, so capture can still yield it.
            "KEY_ESC",
            "KEY_CAPSLOCK",
            "KEY_NUMLOCK",
            "KEY_COMPOSE",
        ):
            assert hotkey_risk(key) == "focused-app", key

    def test_insert_is_part_of_the_paste(self):
        assert hotkey_risk("KEY_INSERT") == "paste"

    def test_typing_keys_are_flagged(self):
        for key in (
            "KEY_A",
            "KEY_Z",
            "KEY_1",
            "KEY_0",
            "KEY_SPACE",
            "KEY_KP0",
            "KEY_COMMA",
            "KEY_102ND",
        ):
            assert hotkey_risk(key) == "typing", key

    def test_chords_keep_the_main_keys_risk(self):
        assert hotkey_risk("CTRL+KEY_A") == "typing"

    def test_bare_modifiers_are_flagged(self):
        for key in ("KEY_LEFTCTRL", "KEY_RIGHTSHIFT", "KEY_LEFTMETA"):
            assert hotkey_risk(key) == "modifier", key


class TestHotkeyWarningText:
    """The caution names the key by the caller's label and always stays permissive."""

    def test_typing_uses_the_label_and_invites_confirming(self):
        text = hotkey_warning_text("#", "typing")
        assert (
            text.startswith("# ") and "typing key" in text and "still choose it" in text
        )

    def test_focused_app_variant(self):
        text = hotkey_warning_text("Delete", "focused-app")
        assert "Delete" in text and "app" in text and "still choose it" in text

    def test_paste_variant(self):
        text = hotkey_warning_text("Insert", "paste")
        assert "Shift+Insert" in text and "still choose it" in text

    def test_modifier_variant(self):
        text = hotkey_warning_text("Ctrl (Left)", "modifier")
        assert (
            "Ctrl (Left)" in text
            and "modifier key" in text
            and "still choose it" in text
        )


class TestPrintableLabel:
    """The keycap glyph, or the evdev name for blank, combining and no-char keys."""

    def test_glyph_for_printable_keys(self):
        assert printable_label(ord("#"), "KEY_BACKSLASH") == "#"
        assert (
            printable_label(ord("z"), "KEY_Y") == "Z"
        )  # German Z on the physical Y key
        assert printable_label(ord("1"), "KEY_1") == "1"

    def test_multichar_uppercase_stays_lower(self):
        # ß.upper() is "SS", two chars, so the label keeps the keycap glyph instead.
        assert printable_label(ord("ß"), "KEY_MINUS") == "ß"

    def test_no_char_falls_back_to_name(self):
        assert printable_label(0, "KEY_F13") == "F13"  # F-keys print nothing

    def test_whitespace_falls_back_to_name(self):
        assert printable_label(ord(" "), "KEY_SPACE") == "Space"

    def test_control_character_falls_back_to_name(self):
        # Gdk gives Delete the DEL code point, as it gives Backspace and Escape theirs.
        assert printable_label(0x7F, "KEY_DELETE") == "Delete"

    def test_combining_mark_falls_back_to_name(self):
        assert printable_label(0x0301, "KEY_F13") == "F13"  # a dead-key acute accent
