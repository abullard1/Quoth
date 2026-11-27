# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 Samuel Ruairí Bullard

"""Tests for `quoth.hotkey_capture`, whose widgets need a display."""

import os

import pytest
from gi.repository import Gdk, Gtk

from tests.support import button, shown_texts


def test_a_safe_key_is_selected_by_its_evdev_token(display):
    from quoth import hotkey_capture

    selected = []
    capture = hotkey_capture._HotkeyCapture(None, selected.append)
    capture._show_prompt()
    # GTK's hardware keycode is the evdev scancode plus 8: F13 is 183 in evdev.
    capture._on_key_pressed(None, Gdk.KEY_F13, 191, 0)

    assert selected == ["KEY_F13"]


def test_a_modifier_chord_matches_gtk_key_events():
    from quoth import hotkey_capture

    # Evdev 183 (F13) -> GTK 191; evdev 29 (KEY_LEFTCTRL) -> GTK 37.
    assert hotkey_capture.hotkey_matches_event(
        "CTRL+KEY_F13", 191, Gdk.ModifierType.CONTROL_MASK
    )
    assert not hotkey_capture.hotkey_matches_event(
        "CTRL+KEY_F13", 191, Gdk.ModifierType(0)
    )
    assert not hotkey_capture.hotkey_matches_event(
        "CTRL+KEY_F13",
        191,
        Gdk.ModifierType.CONTROL_MASK | Gdk.ModifierType.SHIFT_MASK,
    )
    assert hotkey_capture.hotkey_uses_keycode("CTRL+KEY_F13", 37)
    assert hotkey_capture.hotkey_uses_keycode("CTRL+KEY_F13", 191)
    assert not hotkey_capture.hotkey_uses_keycode("CTRL+KEY_F13", 38)


def test_a_modifier_chord_is_captured_and_bare_modifier_warns(display):
    from quoth import hotkey_capture

    selected = []
    capture = hotkey_capture._HotkeyCapture(None, selected.append)
    capture._show_prompt()
    capture._on_key_pressed(None, Gdk.KEY_Control_L, 37, 0)
    capture._on_key_pressed(None, Gdk.KEY_F13, 191, Gdk.ModifierType.CONTROL_MASK)
    assert selected == ["CTRL+KEY_F13"]

    selected.clear()
    capture = hotkey_capture._HotkeyCapture(None, selected.append)
    capture._show_prompt()
    capture._on_key_pressed(None, Gdk.KEY_Control_L, 37, 0)
    capture._on_key_released(None, Gdk.KEY_Control_L, 37, 0)
    assert selected == []
    assert any(
        "Ctrl (Left) is a modifier key" in t
        for t in shown_texts(capture._dialog.get_child())
    )
    button(capture._dialog.get_child(), "Use anyway").emit("clicked")
    assert selected == ["KEY_LEFTCTRL"]


def test_a_risky_key_is_selected_only_once_confirmed(display):
    from quoth import hotkey_capture

    selected = []
    capture = hotkey_capture._HotkeyCapture(None, selected.append)
    capture._show_prompt()
    capture._on_key_pressed(None, Gdk.KEY_a, 38, 0)  # KEY_A (evdev 30): a typing key
    assert selected == []

    button(capture._dialog.get_child(), "Use anyway").emit("clicked")
    assert selected == ["KEY_A"]


def test_keys_reach_the_warning_buttons(display):
    from quoth import hotkey_capture

    capture = hotkey_capture._HotkeyCapture(None, lambda _token: None)
    capture._show_prompt()
    capture._on_key_pressed(None, Gdk.KEY_a, 38, 0)

    # Enter (evdev 28) activates the focused button instead of becoming the key.
    assert capture._on_key_pressed(None, Gdk.KEY_Return, 36, 0) is False


def test_the_warning_shows_the_key_label_verbatim(display, monkeypatch):
    from quoth import hotkey_capture

    monkeypatch.setattr(hotkey_capture, "hotkey_label", lambda _t: "<")
    capture = hotkey_capture._HotkeyCapture(None, lambda _token: None)
    capture._show_warning("KEY_102ND", "typing")
    assert any(
        t.startswith("< is a normal typing key")
        for t in shown_texts(capture._dialog.get_child())
    )


@pytest.mark.skipif(os.environ.get("GTK_A11Y") != "test", reason="needs GTK_A11Y=test")
def test_each_stage_is_a_named_group(display):
    # The focus lands on this box, and a screen reader speaks a named group.
    from quoth import hotkey_capture

    capture = hotkey_capture._HotkeyCapture(None, lambda _token: None)
    capture._show_prompt()
    box = capture._dialog.get_child().get_content()
    assert Gtk.test_accessible_has_role(box, Gtk.AccessibleRole.GROUP)
    for prop in (Gtk.AccessibleProperty.LABEL, Gtk.AccessibleProperty.DESCRIPTION):
        assert Gtk.test_accessible_has_property(box, prop)
