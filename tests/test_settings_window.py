# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 Samuel Ruairí Bullard

"""Tests for `settings_window`.

Tests that build a real `SettingsWindow` take the `settings_window` fixture.
"""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from quoth import __version__
from quoth.config import Config
from quoth.listener import ListenerState
from quoth.settings_window import UpdateBridge, _feedback_value
from tests.support import (
    FakePortalShortcuts,
    accessible_mismatch,
    descendants,
    shown_texts,
)


@pytest.fixture
def settings_window(display):
    """Build a real `SettingsWindow`; each one built is closed and destroyed after."""
    from quoth.models import ModelManager
    from quoth.settings import ModelSwitcher
    from quoth.settings_window import SettingsWindow

    windows = []

    def build(config=None, **kwargs):
        window = SettingsWindow(
            Config() if config is None else config,
            ModelManager(),
            ModelSwitcher(
                None, None, None, reconcile=lambda: None, notify=lambda _: None
            ),
            reconcile_engine=lambda: None,
            enumerate_microphones=lambda: [],
            **kwargs,
        )
        windows.append(window)
        return window

    yield build
    for window in windows:
        window._on_settings_close()
        window.destroy()


@pytest.mark.parametrize(("minor", "announced"), [(5, True), (6, False), (9, False)])
def test_toasts_announce_only_before_native_adwaita_support(minor, announced):
    from quoth.settings_window import SettingsWindow

    view = MagicMock()
    with (
        patch("quoth.settings_window.Adw.get_major_version", return_value=1),
        patch("quoth.settings_window.Adw.get_minor_version", return_value=minor),
        patch("quoth.settings_window.Adw.Toast") as toast,
    ):
        SettingsWindow.show_toast(view, "Ready")
    view.add_toast.assert_called_once_with(toast.return_value)
    assert view.announce.called is announced


def test_toasts_never_render_markup_from_their_message():
    from quoth.settings_window import SettingsWindow

    view = MagicMock()
    message = 'Could not read the skin file (<a href="https://e.vil">x</a>).'
    SettingsWindow.show_toast(view, message)
    toast = view.add_toast.call_args.args[0]
    assert toast.get_title() == message
    assert toast.get_use_markup() is False


def test_capture_cannot_suspend_the_listener_during_dictation():
    from quoth.settings_window import SettingsWindow

    view = MagicMock()
    view._is_idle.return_value = False
    with patch("quoth.settings_window.hotkey_capture.capture_hotkey") as capture:
        SettingsWindow._on_hotkey_row_activated(view, None)
    view.show_toast.assert_called_once()
    view._on_hotkey_capture.assert_not_called()
    capture.assert_not_called()


class TestFeedbackValue:
    """A feedback switch commits only values the config accepts."""

    def test_maps_to_on_off(self):
        assert _feedback_value(True) == "on"
        assert _feedback_value(False) == "off"

    def test_values_are_accepted_by_config(self):
        config = Config()
        for active in (True, False):
            config.sound_feedback = _feedback_value(active)
            assert config.sound_feedback == _feedback_value(active)


def test_the_update_rows_help_shows_over_its_switch(settings_window):
    # A switch without a tooltip of its own shows its row's.
    from gi.repository import Gtk

    dialog = settings_window(
        update_bridge=_update_bridge(SimpleNamespace(available=None))
    )

    row = dialog._update_status_row
    switch = next(w for w in descendants(row) if isinstance(w, Gtk.Switch))
    assert row.get_tooltip_text() == (
        "Asks GitHub for the latest release; nothing is downloaded"
    )
    assert switch.get_tooltip_text() is None


def _update_bridge(state):
    return UpdateBridge(
        available=lambda: state.available,
        checking=lambda: getattr(state, "checking", False),
        checked=lambda: getattr(state, "checked", False),
        failed=lambda: getattr(state, "failed", False),
        check_now=lambda: None,
        view_release=lambda: None,
    )


def test_the_footer_shows_the_version(settings_window):
    dialog = settings_window()
    assert dialog._version_label.get_text() == f"v{__version__}"


def test_update_row_shows_the_live_state_on_refresh(settings_window):
    state = SimpleNamespace(available=None, checking=True)
    dialog = settings_window(update_bridge=_update_bridge(state))

    row = dialog._update_status_row
    assert row.get_subtitle() == "Checking…"
    state.checking, state.failed = False, True
    dialog.refresh_updates()
    assert row.get_subtitle() == "Couldn't check for updates"
    state.available = "9.9.9"
    dialog.refresh_updates()
    assert row.get_subtitle().startswith("9.9.9 available")
    assert dialog._version_label.get_text().endswith("(update available)")


def test_the_volume_slider_is_named_by_its_row(
    settings_window, find_widget, monkeypatch
):
    from gi.repository import Adw, Gtk

    named = {}
    update_property = Gtk.Scale.update_property

    def record_property(scale, names, values):
        named.setdefault(scale, {}).update(zip(names, values, strict=True))
        return update_property(scale, names, values)

    monkeypatch.setattr(Gtk.Scale, "update_property", record_property)
    dialog = settings_window(listener_state=ListenerState.AVAILABLE)
    row = find_widget(dialog, Adw.ActionRow, "Sound volume")
    scale = next(w for w in descendants(row) if isinstance(w, Gtk.Scale))
    assert named[scale] == {
        Gtk.AccessibleProperty.LABEL: "Sound volume",
        Gtk.AccessibleProperty.DESCRIPTION: "Loudness of recording cues",
    }
    assert Gtk.test_accessible_has_property(scale, Gtk.AccessibleProperty.LABEL)
    # Naming the slider leaves the row itself inert.
    assert not row.get_activatable()


def test_cache_status_refresh_forwards_to_engine():
    from quoth.settings_window import SettingsWindow

    dialog = MagicMock()
    SettingsWindow.refresh_cache_status(dialog)
    dialog._engine_section.refresh_cache_status.assert_called_once_with()


def test_settings_always_offers_reporting_and_applies_changes(
    settings_window, find_widget, monkeypatch
):
    from gi.repository import Adw

    from quoth import crash_reports
    from quoth.about import PRIVACY_URL

    dialog = settings_window()
    row = find_widget(dialog, Adw.SwitchRow, crash_reports.SWITCH_TITLE)
    assert row is not None
    assert row.get_subtitle() == crash_reports.SWITCH_SUBTITLE
    assert row.get_tooltip_text() == crash_reports.SWITCH_HELP
    opened = []
    monkeypatch.setattr(
        "quoth.settings_window.open_uri", lambda url: opened.append(url) or True
    )
    notice = find_widget(dialog, Adw.ActionRow, crash_reports.NOTICE_TITLE)
    assert notice.get_tooltip_text() == crash_reports.NOTICE_HELP
    assert notice.get_activatable()
    notice.emit("activated")
    assert opened == [PRIVACY_URL]
    for kind, title in (
        (Adw.SwitchRow, "Launch at login"),
        (Adw.SwitchRow, "Translate to English"),
        (Adw.SwitchRow, "Recording sound"),
        (Adw.SwitchRow, "Recording notification"),
        (Adw.ExpanderRow, "Keep model loaded"),
    ):
        control = find_widget(dialog, kind, title)
        assert control.get_has_tooltip()
        assert control.get_tooltip_text() not in (
            control.get_title(),
            control.get_subtitle(),
        )
    assert not row.get_active()
    row.set_active(True)
    assert Config().crash_reports is True
    row.set_active(False)
    assert Config().crash_reports is False


def test_portal_shortcut_updates_in_place_and_unsubscribes(settings_window):
    portal = FakePortalShortcuts()
    config = Config()
    dialog = settings_window(config, portal_shortcuts=portal)

    assert dialog._portal_shortcut_row.get_title() == "Checking shortcut support"
    assert not dialog._manual_shortcut_row.get_visible()
    assert dialog._hotkey_row is None
    assert portal.configure_calls == 0

    portal.change("unbound")
    assert dialog._portal_shortcut_action.get_label() == "Set up shortcut"
    dialog._portal_shortcut_action.emit("clicked")
    assert portal.configure_calls == 1

    portal.change("ready", "Super+D")
    assert dialog._portal_shortcut_row.get_title() == "Super+D"
    assert (
        dialog._portal_shortcut_row.get_subtitle()
        == "Your desktop manages this shortcut."
    )
    assert dialog._mode_row.get_visible()
    dialog._mode_row.set_selected(1)
    assert config.mode == "push-to-hold"

    portal.version = 1
    portal.change("ready", "Super+D")
    assert not dialog._portal_shortcut_action.get_visible()
    assert (
        dialog._portal_shortcut_row.get_subtitle()
        == "Change this shortcut in your desktop's keyboard settings."
    )

    portal.change("unavailable")
    assert dialog._manual_shortcut_row.get_visible()
    assert not dialog._mode_row.get_visible()

    dialog._on_settings_close()
    dialog._on_settings_close()
    assert portal.unsubscribe_calls == 1


def test_shortcut_text_is_shown_verbatim(settings_window, monkeypatch):
    # GNOME describes a binding as "Press <Super>d", and a keycap can read "<" or "&".
    monkeypatch.setattr(
        "quoth.settings_window.hotkey_capture.hotkey_label", lambda _t: "&"
    )
    native = settings_window(listener_state=ListenerState.AVAILABLE)
    portal = FakePortalShortcuts()
    sandboxed = settings_window(portal_shortcuts=portal)

    assert "&" in shown_texts(native._hotkey_row)
    portal.change("ready", "Press <Super>d")
    assert "Press <Super>d" in shown_texts(sandboxed._portal_shortcut_row)


def test_native_shortcut_remains_without_portal(settings_window):
    config = Config()
    config.hotkey = "KEY_F13"
    dialog = settings_window(config, listener_state=ListenerState.AVAILABLE)

    assert dialog._hotkey_row.get_title() == "Dictation shortcut"
    assert dialog._portal_shortcut_row is None
    assert dialog._recording_group.get_visible()
    assert not dialog._shortcut_group.get_visible()
    dialog.refresh_listener(ListenerState.WAITING)
    assert dialog._recording_group.get_visible()
    assert "Waiting" in dialog._recording_group.get_description()
    assert "different key" in dialog._recording_group.get_description()
    # Readable input may never send the key, as with only a gamepad.
    assert dialog._shortcut_group.get_visible()
    dialog.refresh_listener(ListenerState.FAILED)
    assert dialog._recording_group.get_visible()
    assert not dialog._shortcut_group.get_visible()
    assert dialog._hotkey_row.get_subtitle() == "F13"
    assert (
        dialog._recording_group.get_description()
        == "Keyboard capture stopped. Restart Quoth to reconnect."
    )
    dialog.refresh_listener(ListenerState.STOPPED)
    assert not dialog._recording_group.get_visible()
    assert dialog._shortcut_group.get_visible()
    dialog.refresh_listener(ListenerState.AVAILABLE)
    assert dialog._recording_group.get_visible()
    assert not dialog._recording_group.get_description()


def test_an_open_window_shows_changes_made_elsewhere(settings_window, find_widget):
    from gi.repository import Adw

    from quoth.languages import LANGUAGES

    dialog = settings_window(listener_state=ListenerState.AVAILABLE)
    elsewhere = Config()  # the tray, the app or `gsettings`
    login = find_widget(dialog, Adw.SwitchRow, "Launch at login")
    keep_warm = find_widget(dialog, Adw.ExpanderRow, "Keep model loaded")
    unload = find_widget(dialog, Adw.SpinRow, "Unload after")
    language = find_widget(dialog, Adw.ComboRow, "Spoken language")
    elsewhere.launch_at_login = False  # the desktop declined it
    elsewhere.hotkey = "KEY_F13"  # the listener's fallback, say
    elsewhere.mode = "push-to-hold"
    elsewhere.keep_warm = False
    elsewhere.keep_warm_timeout = 600
    elsewhere.language = "de"
    assert not login.get_active()
    assert dialog._hotkey_row.get_subtitle() == "F13"
    assert dialog._mode_row.get_selected() == 1
    assert not keep_warm.get_enable_expansion()
    assert unload.get_value() == 600
    codes = [code for _, code in LANGUAGES]
    assert language.get_selected() == codes.index("de")
    assert elsewhere.mode == "push-to-hold"  # showing it wrote nothing back


def test_rows_write_their_setting(settings_window, find_widget):
    from gi.repository import Adw

    config = Config()
    dialog = settings_window(config, listener_state=ListenerState.AVAILABLE)
    find_widget(dialog, Adw.SwitchRow, "Launch at login").set_active(False)
    find_widget(dialog, Adw.ExpanderRow, "Keep model loaded").set_enable_expansion(
        False
    )
    find_widget(dialog, Adw.SpinRow, "Unload after").set_value(90)
    modes = dialog._mode_row.get_model()
    assert [modes.get_string(i) for i in range(modes.get_n_items())] == [
        "Tap to start and stop",
        "Hold while you talk",
    ]
    dialog._mode_row.set_selected(1)
    with patch.object(dialog, "show_toast") as toast:
        dialog._apply_hotkey("KEY_F14")
    assert not config.launch_at_login and not config.keep_warm
    assert (config.keep_warm_timeout, config.mode) == (90, "push-to-hold")
    assert dialog._hotkey_row.get_subtitle() == "F14"
    toast.assert_called_once_with("Dictation shortcut set to F14")


def test_number_rows_span_the_schema_range(settings_window, find_widget):
    # A narrower row would show a value set elsewhere clamped, and never offer it.
    from gi.repository import Adw, Gtk

    config = Config()
    dialog = settings_window(config, listener_state=ListenerState.AVAILABLE)
    unload = find_widget(dialog, Adw.SpinRow, "Unload after").get_adjustment()
    volume_row = find_widget(dialog, Adw.ActionRow, "Sound volume")
    scale = next(w for w in descendants(volume_row) if isinstance(w, Gtk.Scale))
    volume = scale.get_adjustment()
    schema = config.settings.props.settings_schema
    _, timeouts = schema.get_key("keep-warm-timeout").get_range()
    _, volumes = schema.get_key("sound-volume").get_range()
    assert (unload.get_lower(), unload.get_upper()) == timeouts
    assert (volume.get_lower(), volume.get_upper()) == volumes


@pytest.mark.parametrize(
    ("title", "name"),
    [
        ("Recording sound", "sound_feedback"),
        ("Recording notification", "notification_feedback"),
    ],
)
def test_a_feedback_switch_keeps_automatic_until_flipped(
    settings_window, find_widget, title, name
):
    from gi.repository import Adw

    from quoth import presentation

    config = Config()
    dialog = settings_window(config, listener_state=ListenerState.AVAILABLE)
    row = find_widget(dialog, Adw.SwitchRow, title)
    automatic = presentation.feedback_enabled("auto")
    setattr(Config(), name, "on" if not automatic else "off")
    assert row.get_active() is not automatic
    setattr(Config(), name, "auto")
    assert row.get_active() is automatic
    assert getattr(config, name) == "auto"
    row.set_active(not automatic)
    assert getattr(config, name) == _feedback_value(not automatic)


def test_a_closed_window_stops_following_settings(settings_window):
    from quoth.hotkey_capture import hotkey_label

    config = Config()
    dialog = settings_window(config, listener_state=ListenerState.AVAILABLE)
    dialog._on_settings_close()
    dialog.destroy()
    config.hotkey = "KEY_F13"
    assert dialog._hotkey_row.get_subtitle() == hotkey_label("KEY_PAUSE")


# The expanders that list models, themes and skins: their rows count as the list row.
_LISTS = ("Model", "Overlay theme", "App skin")


@pytest.fixture
def every_group(settings_window, layer_shell, monkeypatch, tmp_path):
    """Build a window that shows every Settings group and row, natively or sandboxed."""
    from quoth import shortcut_setup, skin, theme
    from quoth.engine_section import CalibrationBridge
    from quoth.models import ModelManager
    from quoth.overlay_section import ThemeBridge
    from quoth.settings_window import UsageInfo
    from quoth.skin_section import SkinBridge

    monkeypatch.setattr(shortcut_setup, "keyboard_settings_argv", lambda: ["true"])
    monkeypatch.setattr(ModelManager, "cache_size_bytes", lambda _self: 1)
    # One model on disk, so its row has a delete button.
    monkeypatch.setattr(ModelManager, "is_downloaded", lambda _self, m: m == "base")
    themes = ThemeBridge(
        presets=theme.list_presets(),
        user_themes=lambda: [],
        current=lambda: "default",
        theme_of=lambda theme_id: theme.theme_from_id(theme_id, tmp_path),
        live=lambda: theme.DEFAULT_THEME,
        load_error=lambda: None,
        apply=MagicMock(),
        import_file=MagicMock(),
    )
    skins = SkinBridge(
        presets=skin.list_presets(),
        user_skins=lambda: [],
        current=lambda: "default",
        skin_of=lambda skin_id: skin.skin_from_id(skin_id, tmp_path),
        load_error=lambda: None,
        apply=MagicMock(),
        import_file=MagicMock(),
    )

    def build(**kwargs):
        window = settings_window(
            update_bridge=_update_bridge(SimpleNamespace(available="9.9.9")),
            calibration_bridge=CalibrationBridge(
                ranking=None, run=lambda: None, clear_cache=lambda: True
            ),
            theme_bridge=themes,
            usage=UsageInfo(dictations_total=3, since="March 2026"),
            skin_bridge=skins,
            **kwargs,
        )
        authored = {
            *(themes.theme_of(name).meta.description for name in themes.presets),
            *(skins.skin_of(name).meta.description for name in skins.presets),
        }
        return window, authored - {None}

    return build


@pytest.mark.parametrize(
    ("kwargs", "shortcut_groups"),
    [
        (
            {"listener_state": ListenerState.WAITING},
            ["Recording", "Dictation shortcut"],
        ),
        (
            {"portal_shortcuts": FakePortalShortcuts("ready", "Super+D")},
            ["Dictation shortcut"],
        ),
    ],
    ids=["native", "flatpak"],
)
def test_tooltips_cover_whole_groups_in_one_short_form(
    every_group, kwargs, shortcut_groups
):
    # A tooltip shows on hover only, so a group is uniform and each one stays short.
    from gi.repository import Adw, Gtk

    window, authored = every_group(**kwargs)
    # Another model downloading, so its row has a stop button.
    window._engine_section.set_download_active("small")
    groups = [w for w in descendants(window) if isinstance(w, Adw.PreferencesGroup)]
    # Every titled group this setup can show is built, so none escapes the checks.
    assert [g.get_title() for g in groups if g.get_title()] == [
        *shortcut_groups,
        *("Transcription", "Audio", "Overlay", "Cues", "Hardware acceleration"),
        *("Memory", "Appearance", "General", "Usage"),
    ]
    for group in groups:
        # The group's rows and those an expander nests, except a list's; an
        # expander's header row is the expander itself.
        rows = []
        for row in descendants(group):
            box = row.get_parent()
            if isinstance(row, Adw.PreferencesRow) and isinstance(box, Gtk.ListBox):
                owner = box.get_ancestor(Adw.ExpanderRow)
                if owner is None or (
                    box.has_css_class("nested") and owner.get_title() not in _LISTS
                ):
                    rows.append(row)
        helped = [(row.get_title(), bool(row.get_tooltip_text())) for row in rows]
        assert len({has for _, has in helped}) <= 1, (group.get_title(), helped)

        for widget in descendants(group):
            tip = widget.get_tooltip_text()
            if not tip:
                continue
            # A theme or skin row leads with its author's description, kept as written.
            first, _, rest = tip.partition("\n")
            if first in authored:
                tip = rest
            if tip:
                assert len(tip) <= 75, tip
                assert "\n" not in tip and not tip.endswith("."), tip
                assert not tip.startswith(("Turn off", "Turn on")), tip

    # Icon-only buttons name their action; of the labelled ones only these carry
    # help of their own, and the rest show their row's. A spin button's +/- are
    # GTK's own, and a button with a custom child, the About footer, is named by it.
    buttons = [
        w
        for group in groups
        for w in descendants(group)
        if isinstance(w, Gtk.Button)
        and (w.get_label() or w.get_icon_name())
        and w.get_ancestor(Gtk.SpinButton) is None
    ]
    for icon_only in (b for b in buttons if not b.get_label()):
        assert icon_only.get_tooltip_text(), icon_only.get_icon_name()
    explained = {
        b.get_label() for b in buttons if b.get_label() and b.get_tooltip_text()
    }
    # A labelled button that gains its own tooltip is added here on purpose.
    assert explained == {"Import…", "Check now", "View release"}


def test_unload_after_shows_its_own_help_not_its_expanders(
    settings_window, find_widget
):
    # GTK shows the nearest tooltip up the widget tree, so a nested row needs its own.
    from gi.repository import Adw

    dialog = settings_window()
    expander = find_widget(dialog, Adw.ExpanderRow, "Keep model loaded")
    row = find_widget(dialog, Adw.SpinRow, "Unload after")
    assert row.get_ancestor(Adw.ExpanderRow) is expander
    assert row.get_tooltip_text() == "The timer restarts after every dictation"
    assert expander.get_tooltip_text() != row.get_tooltip_text()


def test_expander_rows_are_named_by_their_titles_not_their_tooltips(every_group):
    # GTK names a widget with nothing else to name it by its tooltip.
    from gi.repository import Adw, Gtk

    window, _authored = every_group(listener_state=ListenerState.AVAILABLE)
    label = Gtk.AccessibleProperty.LABEL
    expanders = {
        w.get_title(): w for w in descendants(window) if isinstance(w, Adw.ExpanderRow)
    }
    assert set(expanders) == {"Model", "Overlay theme", "App skin", "Keep model loaded"}
    for title, expander in expanders.items():
        assert accessible_mismatch(expander, label, title) is None
    keep = expanders["Keep model loaded"]
    switch = next(w for w in descendants(keep) if isinstance(w, Gtk.Switch))
    relation = Gtk.AccessibleRelation.LABELLED_BY
    assert accessible_mismatch(switch, relation, keep) is None
