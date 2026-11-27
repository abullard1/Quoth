# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 Samuel Ruairí Bullard

"""Tests for `tray`: the StatusNotifierItem icon and its dbusmenu context menu."""

import io
import itertools
import re
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from gi.repository import Gio, GLib

from quoth import APP_ID
from quoth.tray import (
    _ID_COPY_LAST,
    _ID_LANG_BASE,
    _ID_LANGUAGE,
    _ID_MODEL,
    _ID_MODEL_BASE,
    _ID_QUIT,
    _ID_SETTINGS,
    _PULSE,
    _RECORDING_PULSE,
    _TRANSCRIBING_PULSE,
    QuothTray,
    TrayMenu,
)
from tests.support import wait_until


def test_symbolic_tray_icons_use_the_freedesktop_base_colour():
    """The symbolic marks ship in the freedesktop base colour #bebebe, never #000000.

    Recolouring hosts (COSMIC, GTK) discard the RGB, but one that draws a symbolic icon
    literally then shows the conventional neutral. The red recording icon is exempt.
    """
    apps = Path(__file__).resolve().parents[1] / "data/icons/hicolor/symbolic/apps"
    for name in (f"{APP_ID}-symbolic.svg", f"{APP_ID}-transcribing-symbolic.svg"):
        svg = (apps / name).read_text()
        assert "#bebebe" in svg, f"{name} lost its symbolic base colour"
        assert "#000000" not in svg, (
            f"{name} uses pure black, not the freedesktop symbolic base"
        )


def _make_menu() -> TrayMenu:
    # The actions run as idle callbacks, which GLib repeats while they return a truthy
    # value, as a bare MagicMock does.
    return TrayMenu(
        languages=(("English", "en"), ("German", "de"), ("Auto-detect", "auto")),
        get_language=lambda: "en",
        set_language=MagicMock(return_value=None),
        list_models=lambda: ["base", "small"],
        get_model=lambda: "small",
        set_model=MagicMock(return_value=None),
        on_settings=MagicMock(return_value=None),
        on_about=MagicMock(return_value=None),
        on_quit=MagicMock(return_value=None),
        update_available=lambda: None,
        view_release=MagicMock(return_value=None),
        last_dictation_available=lambda: False,
        copy_last_dictation=MagicMock(return_value=None),
    )


def _tray() -> QuothTray:
    return QuothTray(_make_menu())


_ITEM = ("/StatusNotifierItem", "org.kde.StatusNotifierItem")
_MENU = ("/StatusNotifierItem/Menu", "com.canonical.dbusmenu")
_NO_ARGS = GLib.Variant("()", ())


@pytest.fixture
def host(private_bus, request):
    """A tray started on a private bus, and what a panel host sees of it.

    Yields `tray`, its menu bridge `menu`, `call(path, interface, method, args)`, the
    `signals` received as (name, args), and `pulses`, the armed pulse timers by source
    id as (interval_ms, tick); an indirect parametrize passes QuothTray's keyword
    arguments. Only a real connection checks a reply against the declared out
    arguments: GDBus drops one that does not match, and the host never hears back. A
    sender's messages arrive in order, so once a call returns, every signal sent
    before it is in `signals`.
    """
    flags = (
        Gio.DBusConnectionFlags.AUTHENTICATION_CLIENT
        | Gio.DBusConnectionFlags.MESSAGE_BUS_CONNECTION
    )
    service = Gio.DBusConnection.new_for_address_sync(private_bus, flags, None, None)
    client = Gio.DBusConnection.new_for_address_sync(private_bus, flags, None, None)
    menu = _make_menu()
    tray = QuothTray(menu, **getattr(request, "param", {}))
    with patch("quoth.tray.Gio.bus_get_sync", return_value=service):
        tray.start()

    def call(path, interface, method, args):
        replies = []
        client.call(
            service.get_unique_name(),
            path,
            interface,
            method,
            args,
            None,
            Gio.DBusCallFlags.NONE,
            -1,
            None,
            lambda _conn, result: replies.append(result),
        )
        wait_until(lambda: replies)
        return client.call_finish(replies[0])

    signals = []
    for path, interface in (_ITEM, _MENU):
        client.signal_subscribe(
            None,
            interface,
            None,
            path,
            None,
            Gio.DBusSignalFlags.NONE,
            lambda _conn, _sender, path, iface, name, args: signals.append(
                ((path, iface), name, args)
            ),
        )
    # A round trip makes the bus apply the match rules before the test emits.
    call("/", "org.freedesktop.DBus.Peer", "Ping", None)
    # The pulse runs on a hand-cranked clock that outlives teardown's stop(), so stop()
    # removes the same fake sources the test armed.
    pulses = {}
    source_ids = itertools.count(1)

    def timeout_add(interval, tick):
        pulses[source := next(source_ids)] = (interval, tick)
        return source

    with (
        patch("quoth.tray.GLib.timeout_add", side_effect=timeout_add),
        patch("quoth.tray.GLib.source_remove", side_effect=pulses.pop),
    ):
        yield SimpleNamespace(
            tray=tray, menu=menu, call=call, signals=signals, pulses=pulses
        )
        tray.stop()
    for connection in (service, client):
        connection.close_sync(None)


def _get(host, name: str) -> GLib.Variant:
    """An item property as a host reads it, with Properties.Get."""
    reply = host.call(
        _ITEM[0],
        "org.freedesktop.DBus.Properties",
        "Get",
        GLib.Variant("(ss)", (_ITEM[1], name)),
    )
    return reply.get_child_value(0).get_variant()


def _items(host) -> list:
    """The menu's top-level items as a host unpacks GetLayout: (id, props, children)."""
    reply = host.call(*_MENU, "GetLayout", GLib.Variant("(iias)", (0, -1, [])))
    _revision, (_root_id, _props, items) = reply.unpack()
    return items


def _labels(items) -> list[str]:
    """What a host shows for each item: its label, or its type for a separator."""
    return [props.get("label", props.get("type")) for _id, props, _children in items]


def _event(host, item_id: int, event_id: str):
    """Send the dbusmenu Event a host sends when an item is hovered or clicked."""
    host.call(
        *_MENU,
        "Event",
        GLib.Variant("(isvu)", (item_id, event_id, GLib.Variant("i", 0), 0)),
    )


class TestQuothTrayState:
    def test_initial_state_is_idle(self, host):
        assert _get(host, "IconName") == GLib.Variant("s", f"{APP_ID}-symbolic")
        assert _get(host, "ToolTip").unpack()[2] == "Quoth: ready"

    def test_set_state_updates_state(self, host):
        host.tray.set_state("recording")
        # ToolTip is (icon name, icon pixmap, title, description).
        assert _get(host, "ToolTip").unpack()[2] == "Quoth: recording…"

    def test_set_state_no_change_no_signal(self, host):
        host.tray.set_state("idle")  # already idle
        assert _get(host, "IconName") == GLib.Variant("s", f"{APP_ID}-symbolic")
        assert host.signals == []

    def test_set_state_emits_signals(self, host):
        host.tray.set_state("recording")
        wait_until(lambda: len(host.signals) == 2)
        assert sorted(host.signals) == [
            (_ITEM, "NewIcon", _NO_ARGS),
            (_ITEM, "NewToolTip", _NO_ARGS),
        ]


class TestIconMapping:
    @pytest.mark.parametrize(
        ("state", "icon", "tooltip"),
        [
            ("idle", f"{APP_ID}-symbolic", "Quoth: ready"),
            ("recording", f"{APP_ID}-recording", "Quoth: recording…"),
            ("transcribing", f"{APP_ID}-transcribing-symbolic", "Quoth: transcribing…"),
        ],
        ids=["idle", "recording", "transcribing"],
    )
    def test_each_state_shows_its_icon_and_tooltip_without_animation(
        self, host, state, icon, tooltip
    ):
        host.tray.set_animated(False)
        host.tray.set_state(state)
        assert _get(host, "IconName") == GLib.Variant("s", icon)
        assert _get(host, "ToolTip").unpack()[2] == tooltip


class TestSNIProperties:
    def test_property_values(self, host):
        # Every state is Active, so hosts that hide Passive items (COSMIC does) keep the
        # tray visible; it is the app's only access point. Installed, no bundled icon
        # dir is advertised, and the host uses the system icon theme.
        assert _get(host, "Status") == GLib.Variant("s", "Active")
        assert _get(host, "Menu") == GLib.Variant("o", "/StatusNotifierItem/Menu")
        assert _get(host, "IconThemePath") == GLib.Variant("s", "")

    @pytest.mark.parametrize(
        "host", [{"icon_theme_path": "/opt/quoth/data/icons"}], indirect=True
    )
    def test_icon_theme_path_advertises_the_bundled_dir(self, host):
        # Run from source: the app hands the tray its data/icons root, so the host
        # resolves our own icons without a system-theme install.
        assert _get(host, "IconThemePath") == GLib.Variant("s", "/opt/quoth/data/icons")


class TestRecordingPulse:
    def test_recording_starts_pulse_timer(self, host):
        host.tray.set_state("recording")
        assert [interval for interval, _tick in host.pulses.values()] == [400]

    def test_transcribing_pulses(self, host):
        host.tray.set_state("transcribing")
        assert [interval for interval, _tick in host.pulses.values()] == [200]

    def test_idle_does_not_pulse(self, host):
        host.tray.set_state("transcribing")
        host.tray.set_state("idle")
        assert host.pulses == {}

    def test_every_pulse_frame_is_a_generated_icon(self):
        # The frames are named here but written by scripts/generate_icons.py, so a
        # renamed or dropped frame would only surface as a tray that stops animating.
        icons = Path(__file__).resolve().parents[1] / "data" / "icons" / "hicolor"
        present = {p.stem for p in icons.rglob("*.svg")} | {
            p.stem for p in icons.rglob("*.png")
        }
        for frames, _interval in _PULSE.values():
            for name in frames:
                assert name in present, f"{name} is not in the generated icon pack"

    def test_leaving_recording_stops_pulse(self, host):
        host.tray.set_state("recording")
        host.tray.set_state("idle")
        assert host.pulses == {}
        host.tray.set_state("recording")  # and the next recording pulses again
        assert len(host.pulses) == 1

    def test_iconname_cycles_through_frames(self, host):
        host.tray.set_state("recording")
        [(_interval, tick)] = host.pulses.values()
        # Frame 0 is the static recording icon: resting state is identical.
        assert _get(host, "IconName") == GLib.Variant("s", f"{APP_ID}-recording")
        host.signals.clear()
        tick()
        assert _get(host, "IconName") == GLib.Variant(
            "s", f"{APP_ID}-recording-pulse-1"
        )
        assert host.signals == [(_ITEM, "NewIcon", _NO_ARGS)]
        tick()
        assert _get(host, "IconName") == GLib.Variant(
            "s", f"{APP_ID}-recording-pulse-2"
        )

    def test_pulse_tick_wraps_and_keeps_ticking(self, host):
        host.tray.set_state("recording")
        [(_interval, tick)] = host.pulses.values()
        for _ in range(len(_RECORDING_PULSE)):
            assert tick() is True
        # Wrapped back to the static frame.
        assert _get(host, "IconName") == GLib.Variant("s", f"{APP_ID}-recording")

    def test_set_animated_false_disables_and_snaps_to_static(self, host):
        host.tray.set_state("recording")
        [(_interval, tick)] = host.pulses.values()
        tick()  # move off the static frame
        assert _get(host, "IconName") == GLib.Variant(
            "s", f"{APP_ID}-recording-pulse-1"
        )
        host.signals.clear()
        host.tray.set_animated(False)
        assert host.pulses == {}
        assert _get(host, "IconName") == GLib.Variant("s", f"{APP_ID}-recording")
        assert host.signals == [(_ITEM, "NewIcon", _NO_ARGS)]

    def test_no_pulse_when_animation_disabled(self, host):
        host.tray.set_animated(False)
        host.tray.set_state("recording")
        assert host.pulses == {}


class TestDbusmenuProperties:
    def test_property_values(self, host):
        # Version is the required one: without it COSMIC drops the menu.
        reply = host.call(
            _MENU[0],
            "org.freedesktop.DBus.Properties",
            "GetAll",
            GLib.Variant("(s)", (_MENU[1],)),
        )
        assert reply.unpack() == (
            {
                "Version": 3,
                "TextDirection": "ltr",
                "Status": "normal",
                "IconThemePath": [],
            },
        )


class TestMenuModel:
    def test_top_level_order_without_update(self, host):
        items = _items(host)
        assert _labels(items) == [
            "Language",
            "Model",
            "separator",
            "Settings",
            "About Quoth",
            "separator",
            "Quit Quoth",
        ]
        # A host routes a click by item id, so no two items share one.
        ids = [item_id for item_id, _props, _children in items]
        assert len(set(ids)) == len(ids)

    def test_update_item_appears_when_available(self, host):
        host.menu.update_available = lambda: "9.9.9"
        labels = _labels(_items(host))
        assert labels[labels.index("Settings") + 1] == "Update available (9.9.9)"

    def test_copy_item_appears_once_there_is_a_dictation(self, host):
        host.menu.last_dictation_available = lambda: True
        labels = _labels(_items(host))
        assert labels[labels.index("Settings") - 1] == "Copy last dictation"
        _event(host, _ID_COPY_LAST, "clicked")
        wait_until(lambda: host.menu.copy_last_dictation.called)
        host.menu.copy_last_dictation.assert_called_once_with()

    def test_language_submenu_lists_all_with_active_checkmarked(self, host):
        host.menu.get_language = lambda: "de"
        languages = next(
            children
            for _id, props, children in _items(host)
            if props.get("label") == "Language"
        )
        assert [(p["label"], p["toggle-state"]) for _id, p, _c in languages] == [
            ("English", 0),
            ("German", 1),
            ("Auto-detect", 0),
        ]

    def test_model_submenu_is_downloaded_only_with_active_checkmarked(self, host):
        host.menu.list_models = lambda: ["base", "small"]
        host.menu.get_model = lambda: "small"
        models = next(
            children
            for _id, props, children in _items(host)
            if props.get("label") == "Model"
        )
        assert [(p["label"], p["toggle-state"]) for _id, p, _c in models] == [
            ("Base", 0),
            ("Small", 1),
        ]

    def test_submenu_parents_declare_children_display(self, host):
        parents = {
            props["label"]: props["children-display"]
            for _id, props, children in _items(host)
            if children
        }
        assert parents == {"Language": "submenu", "Model": "submenu"}

    def test_separators_typed_and_disabled(self, host):
        separators = [p for _id, p, _children in _items(host) if "label" not in p]
        assert separators == [{"type": "separator", "enabled": False}] * 2


class TestMenuLayout:
    def test_children_are_single_wrapped(self, host):
        # Each av element must be a variant directly holding (ia{sv}av); wrapping it
        # again with GLib.Variant("v", item) gives variant(variant(struct)), which hosts
        # cannot deserialise.
        reply = host.call(*_MENU, "GetLayout", GLib.Variant("(iias)", (0, -1, [])))
        root = reply.get_child_value(1)
        assert root.get_child_value(0).get_int32() == 0  # the root's id
        children = root.get_child_value(2)
        assert children.n_children(), "root should have children"
        for i in range(children.n_children()):
            child = children.get_child_value(i).get_variant()
            assert child.get_type_string() == "(ia{sv}av)"

    def test_get_layout_unknown_parent_errors(self, host):
        with pytest.raises(GLib.Error):
            host.call(*_MENU, "GetLayout", GLib.Variant("(iias)", (9999, -1, [])))


class TestMenuMethods:
    def test_get_group_properties_filters_names(self, host):
        reply = host.call(
            *_MENU,
            "GetGroupProperties",
            GLib.Variant("(aias)", ([_ID_SETTINGS], ["label"])),
        )
        assert reply.unpack() == ([(_ID_SETTINGS, {"label": "Settings"})],)

    def test_get_property_missing_errors(self, host):
        with pytest.raises(GLib.Error):
            host.call(
                *_MENU, "GetProperty", GLib.Variant("(is)", (_ID_SETTINGS, "nope"))
            )


class TestMenuEvents:
    def test_settings_click_invokes_callback(self, host):
        _event(host, _ID_SETTINGS, "clicked")
        wait_until(lambda: host.menu.on_settings.called)
        host.menu.on_settings.assert_called_once_with()

    def test_quit_click_invokes_callback(self, host):
        _event(host, _ID_QUIT, "clicked")
        wait_until(lambda: host.menu.on_quit.called)
        host.menu.on_quit.assert_called_once_with()

    def test_language_click_sets_that_language(self, host):
        # languages: en(100), de(101), auto(102)
        _event(host, _ID_LANG_BASE + 1, "clicked")
        wait_until(lambda: host.menu.set_language.called)
        host.menu.set_language.assert_called_once_with("de")

    def test_model_click_sets_that_model(self, host):
        # models: base(200), small(201)
        _event(host, _ID_MODEL_BASE + 0, "clicked")
        wait_until(lambda: host.menu.set_model.called)
        host.menu.set_model.assert_called_once_with("base")

    def test_non_clicked_event_is_ignored(self, host):
        # A click's action is queued before the reply, and `call` runs the main loop
        # until the reply arrives, so a dispatched action would already have run.
        _event(host, _ID_SETTINGS, "hovered")
        host.menu.on_settings.assert_not_called()

    def test_event_group_dispatches_and_reports_errors(self, host):
        reply = host.call(
            *_MENU,
            "EventGroup",
            GLib.Variant(
                "(a(isvu))",
                (
                    [
                        (_ID_SETTINGS, "clicked", GLib.Variant("i", 0), 0),
                        (9999, "clicked", GLib.Variant("i", 0), 0),
                    ],
                ),
            ),
        )
        assert reply.unpack() == ([9999],)
        wait_until(lambda: host.menu.on_settings.called)
        host.menu.on_settings.assert_called_once_with()


class TestMenuRevision:
    def test_update_item_change_bumps_revision_and_signals(self, host):
        layout = GLib.Variant("(iias)", (0, -1, []))
        before, _root = host.call(*_MENU, "GetLayout", layout).unpack()
        host.menu.update_available = lambda: "1.2.3"
        host.tray.refresh_menu()
        wait_until(lambda: host.signals)
        # The new revision, and the root as the changed parent.
        assert host.signals == [
            (_MENU, "LayoutUpdated", GLib.Variant("(ui)", (before + 1, 0)))
        ]
        assert host.call(*_MENU, "GetLayout", layout).unpack()[0] == before + 1

    def test_about_to_show_tracks_layout_changes(self, host):
        layout = GLib.Variant("(iias)", (0, -1, []))
        host.call(*_MENU, "GetLayout", layout)
        for item_id in (0, _ID_LANGUAGE, _ID_MODEL):
            assert not host.call(
                *_MENU, "AboutToShow", GLib.Variant("(i)", (item_id,))
            ).unpack()[0]

        host.menu.get_language = lambda: "de"
        assert host.call(*_MENU, "AboutToShow", GLib.Variant("(i)", (0,))).unpack() == (
            True,
        )
        assert host.call(
            *_MENU, "AboutToShow", GLib.Variant("(i)", (_ID_LANGUAGE,))
        ).unpack() == (True,)
        assert host.call(
            *_MENU, "AboutToShow", GLib.Variant("(i)", (_ID_MODEL,))
        ).unpack() == (False,)

        host.call(*_MENU, "GetLayout", layout)
        assert host.call(
            *_MENU, "AboutToShow", GLib.Variant("(i)", (_ID_LANGUAGE,))
        ).unpack() == (False,)

        host.menu.list_models = lambda: ["small"]
        host.menu.get_model = lambda: "small"
        assert host.call(
            *_MENU, "AboutToShow", GLib.Variant("(i)", (_ID_MODEL,))
        ).unpack() == (True,)

        host.call(*_MENU, "GetLayout", layout)
        assert host.call(
            *_MENU, "AboutToShow", GLib.Variant("(i)", (_ID_MODEL,))
        ).unpack() == (False,)

    def test_about_to_show_group_reports_changed_and_missing_ids(self, host):
        host.call(*_MENU, "GetLayout", GLib.Variant("(iias)", (0, -1, [])))
        host.menu.get_language = lambda: "de"
        assert host.call(
            *_MENU,
            "AboutToShowGroup",
            GLib.Variant("(ai)", ([0, _ID_LANGUAGE, _ID_MODEL, 9999],)),
        ).unpack() == ([0, _ID_LANGUAGE], [9999])

    def test_refresh_menu_signals_only_when_layout_changed(self, host):
        layout = GLib.Variant("(iias)", (0, -1, []))
        before, _root = host.call(*_MENU, "GetLayout", layout).unpack()
        host.tray.refresh_menu()
        assert host.signals == []

        host.menu.get_language = lambda: "de"
        host.tray.refresh_menu()
        wait_until(lambda: host.signals)
        assert host.signals == [
            (_MENU, "LayoutUpdated", GLib.Variant("(ui)", (before + 1, 0)))
        ]


class TestTrayLifecycle:
    @patch("quoth.tray.Gio.bus_watch_name_on_connection")
    @patch("quoth.tray.Gio.bus_get_sync")
    def test_start_registers_objects_and_announces_unique_name(
        self, mock_get, mock_watch
    ):
        # A Flatpak's D-Bus proxy denies owning `org.kde.StatusNotifierItem-<pid>-N`,
        # so the item is announced under the unique name once the watcher appears.
        conn = MagicMock()
        conn.get_unique_name.return_value = ":1.42"
        mock_get.return_value = conn
        tray = _tray()
        tray.start()
        paths = [c.args[0] for c in conn.register_object.call_args_list]
        assert "/StatusNotifierItem" in paths
        assert "/StatusNotifierItem/Menu" in paths
        assert mock_watch.call_args.args[1] == "org.kde.StatusNotifierWatcher"
        appeared = mock_watch.call_args.args[3]
        appeared(conn, "org.kde.StatusNotifierWatcher", ":1.7")
        watcher_call = conn.call_sync.call_args
        assert watcher_call.args[:4] == (
            "org.kde.StatusNotifierWatcher",
            "/StatusNotifierWatcher",
            "org.kde.StatusNotifierWatcher",
            "RegisterStatusNotifierItem",
        )
        assert watcher_call.args[4].unpack() == (":1.42",)

    @patch("quoth.tray.Gio.bus_watch_name_on_connection")
    @patch("quoth.tray.Gio.bus_get_sync")
    def test_a_missing_tray_host_is_logged(self, mock_get, mock_watch, caplog):
        mock_get.return_value = MagicMock()
        tray = _tray()
        caplog.set_level("WARNING")
        tray.start()
        vanished = mock_watch.call_args.args[4]
        vanished(mock_get.return_value, "org.kde.StatusNotifierWatcher")
        [record] = caplog.records
        assert "no StatusNotifierWatcher" in record.getMessage()

    @patch("quoth.tray.Gio.bus_get_sync", side_effect=GLib.Error("no bus"))
    def test_no_session_bus_is_logged_not_raised(self, _get, caplog):
        caplog.set_level("WARNING")
        _tray().start()
        assert [r.levelname for r in caplog.records] == ["WARNING"]

    def test_reannounces_when_watcher_owner_reappears(self):
        # A tray host/panel that restarts after us comes up with an empty item list;
        # the second owner-appeared must re-register or the icon vanishes for good.
        conn = MagicMock()
        conn.get_unique_name.return_value = ":1.42"
        tray = _tray()
        tray._conn = conn
        tray._on_watcher_appeared(conn, "org.kde.StatusNotifierWatcher", ":1.7")
        tray._on_watcher_appeared(conn, "org.kde.StatusNotifierWatcher", ":1.9")
        register_calls = [
            c
            for c in conn.call_sync.call_args_list
            if c.args[3] == "RegisterStatusNotifierItem"
        ]
        assert len(register_calls) == 2

    def test_stop_unregisters_objects_and_watch(self, host):
        with patch(
            "quoth.tray.Gio.bus_unwatch_name", wraps=Gio.bus_unwatch_name
        ) as unwatch:
            host.tray.stop()
        unwatch.assert_called_once()
        # A host still holding the item's address gets an error, not a stale menu.
        with pytest.raises(GLib.Error):
            _get(host, "IconName")
        with pytest.raises(GLib.Error):
            host.call(*_MENU, "AboutToShow", GLib.Variant("(i)", (0,)))


class TestOnTheBus:
    """What a panel host receives over a real session bus."""

    @pytest.mark.parametrize(
        ("path", "interface", "types"),
        [
            (
                "/StatusNotifierItem",
                "org.kde.StatusNotifierItem",
                {
                    "Category": "s",
                    "Id": "s",
                    "Title": "s",
                    "Status": "s",
                    # KDE's reference XML, which hosts implement; the wiki says UINT32.
                    "WindowId": "i",
                    "IconName": "s",
                    "IconPixmap": "a(iiay)",
                    "IconThemePath": "s",
                    "OverlayIconName": "s",
                    "OverlayIconPixmap": "a(iiay)",
                    "AttentionIconName": "s",
                    "AttentionIconPixmap": "a(iiay)",
                    "AttentionMovieName": "s",
                    "ToolTip": "(sa(iiay)ss)",
                    "Menu": "o",
                    "ItemIsMenu": "b",
                },
            ),
            (
                "/StatusNotifierItem/Menu",
                "com.canonical.dbusmenu",
                {
                    "Version": "u",
                    "TextDirection": "s",
                    "Status": "s",
                    "IconThemePath": "as",
                },
            ),
        ],
        ids=["item", "menu"],
    )
    def test_get_all_serves_every_property_with_its_spec_type(
        self, host, path, interface, types
    ):
        # A property missing from GetAll is one the host never reads: no icon, or for
        # the menu's Version, no menu on COSMIC.
        reply = host.call(
            path,
            "org.freedesktop.DBus.Properties",
            "GetAll",
            GLib.Variant("(s)", (interface,)),
        )
        props = reply.get_child_value(0)
        served = {
            name: props.lookup_value(name, None).get_type_string()
            for name in props.keys()
        }
        assert served == types

    @pytest.mark.parametrize(
        ("method", "args", "signature", "value"),
        [
            (
                "GetLayout",
                GLib.Variant("(iias)", (_ID_SETTINGS, -1, [])),
                "(u(ia{sv}av))",
                (1, (_ID_SETTINGS, {"label": "Settings", "enabled": True}, [])),
            ),
            (
                "GetGroupProperties",
                GLib.Variant("(aias)", ([_ID_SETTINGS], [])),
                "(a(ia{sv}))",
                ([(_ID_SETTINGS, {"label": "Settings", "enabled": True})],),
            ),
            (
                "GetProperty",
                GLib.Variant("(is)", (_ID_SETTINGS, "label")),
                "(v)",
                ("Settings",),
            ),
            (
                "Event",
                GLib.Variant(
                    "(isvu)", (_ID_SETTINGS, "hovered", GLib.Variant("i", 0), 0)
                ),
                "()",
                (),
            ),
            (
                "EventGroup",
                GLib.Variant(
                    "(a(isvu))", ([(9999, "clicked", GLib.Variant("i", 0), 0)],)
                ),
                "(ai)",
                ([9999],),
            ),
            # Before the first GetLayout, every item needs a fetch.
            ("AboutToShow", GLib.Variant("(i)", (0,)), "(b)", (True,)),
            (
                "AboutToShowGroup",
                GLib.Variant("(ai)", ([0, _ID_SETTINGS],)),
                "(aiai)",
                ([0, _ID_SETTINGS], []),
            ),
        ],
    )
    def test_every_menu_method_replies_with_its_spec_signature(
        self, host, method, args, signature, value
    ):
        reply = host.call(*_MENU, method, args)
        assert reply.get_type_string() == signature
        assert reply.unpack() == value

    def test_a_failing_menu_request_is_answered_with_an_error(self, host, caplog):
        # Unanswered, the host's menu would hang until the D-Bus timeout.
        with (
            patch.object(host.tray, "_menu_items", side_effect=OSError("gone")),
            pytest.raises(GLib.Error, match="GetLayout"),
        ):
            host.call(*_MENU, "GetLayout", GLib.Variant("(iias)", (0, -1, [])))
        [record] = [r for r in caplog.records if r.levelname == "ERROR"]
        assert record.getMessage() == "Tray: menu GetLayout failed"

    def test_a_repeating_menu_failure_logs_its_traceback_once(self, host, caplog):
        # Hosts ask for the menu each time it opens.
        layout = GLib.Variant("(iias)", (0, -1, []))
        for error in (OSError("gone"), OSError("gone"), KeyError("other")):
            with (
                patch.object(host.tray, "_menu_items", side_effect=error),
                pytest.raises(GLib.Error),
            ):
                host.call(*_MENU, "GetLayout", layout)
        errors = [r for r in caplog.records if r.levelname == "ERROR"]
        assert [r.exc_info[0] for r in errors] == [OSError, KeyError]

    def test_activating_the_item_opens_settings(self, host):
        # A left click on the panel icon.
        host.call(*_ITEM, "Activate", GLib.Variant("(ii)", (0, 0)))
        wait_until(lambda: host.menu.on_settings.called)
        host.menu.on_settings.assert_called_once_with()


def test_symbolic_states_are_the_same_size_and_fill_their_canvas():
    """The idle and resting transcribing marks fill their canvas and match in size.

    An inset reads as smaller than the stock symbolics beside it, and a mismatch resizes
    the icon as transcription starts. Measured from renders: their unit spaces differ.
    """
    cairosvg = pytest.importorskip("cairosvg", reason="icons tooling group")
    from PIL import Image

    apps = Path(__file__).resolve().parents[1] / "data/icons/hicolor/symbolic/apps"
    extents = {}
    for name in (f"{APP_ID}-symbolic", f"{APP_ID}-transcribing-symbolic"):
        png = cairosvg.svg2png(
            url=str(apps / f"{name}.svg"), output_width=64, output_height=64
        )
        box = Image.open(io.BytesIO(png)).convert("RGBA").getbbox()
        extents[name] = max(box[2] - box[0], box[3] - box[1]) / 64

    for name, frac in extents.items():
        assert frac > 0.97, f"{name} fills only {frac:.0%} of its canvas"
    spread = max(extents.values()) - min(extents.values())
    assert spread < 0.03, (
        f"tray states differ in size and would resize between states: {extents}"
    )


def test_recording_pngs_match_their_scalable_form():
    """The colour PNGs are rasterised from the SVG they ship beside.

    Composited separately, the tile edge loses its antialiasing: each rim pixel is fully
    opaque or clear, a staircase beside the vector states.
    """
    cairosvg = pytest.importorskip("cairosvg", reason="icons tooling group")
    from PIL import Image, ImageChops

    root = Path(__file__).resolve().parents[1] / "data/icons/hicolor"
    for name in _RECORDING_PULSE:
        for size in (16, 24, 32):
            png = Image.open(root / f"{size}x{size}/apps/{name}.png").convert("RGBA")
            ref = Image.open(
                io.BytesIO(
                    cairosvg.svg2png(
                        url=str(root / f"scalable/apps/{name}.svg"),
                        output_width=size,
                        output_height=size,
                    )
                )
            ).convert("RGBA")
            worst = max(ImageChops.difference(png, ref).getextrema())[1]
            assert worst <= 2, f"{name} at {size}px is not its SVG (max delta {worst})"


def test_no_transcribing_frame_sits_still():
    """Every tick advances the gesture: a spinner that stops reads as a hung app.

    Constant-time sampling puts several frames in the loader's eased plateau, where they
    come out identical, so the spinner holds for most of a second and then lurches.
    """
    cairosvg = pytest.importorskip("cairosvg", reason="icons tooling group")
    from PIL import Image, ImageChops

    apps = Path(__file__).resolve().parents[1] / "data/icons/hicolor/symbolic/apps"
    size = 64
    frames = [
        Image.open(
            io.BytesIO(
                cairosvg.svg2png(
                    url=str(apps / f"{name}.svg"),
                    output_width=size,
                    output_height=size,
                )
            )
        )
        .convert("RGBA")
        .getchannel("A")
        for name in _TRANSCRIBING_PULSE
    ]
    steps = []
    for i, frame in enumerate(frames):
        nxt = frames[(i + 1) % len(frames)]
        histogram = ImageChops.difference(frame, nxt).histogram()
        moved = sum(v * n for v, n in enumerate(histogram)) / (size * size * 255)
        assert moved > 0.02, f"frame {i} barely changes ({moved:.1%}), the loop stalls"
        steps.append(moved)

    # Evenly spaced in motion, so the cycle reads as one continuous gesture; uniform
    # time sampling gives a worst step of 2.7x the mean.
    assert max(steps) / (sum(steps) / len(steps)) < 1.5, f"uneven cadence: {steps}"


def test_transcribing_covers_both_halves_of_the_gesture():
    """Both wing peels are in the loop and equally covered, or one half plays smoother.

    The loop is one gesture twice with the wings swapped; a peel is its short, fast
    part, so sampling that ignores it drops it entirely.
    """
    pytest.importorskip("cairosvg", reason="icons tooling group")
    from scripts import generate_icons as gen

    loader = gen._LOADER.read_text()
    css = re.search(r"<style.*?</style>", loader, re.S).group(0)
    tracks = {name: gen._keyframes(css, name) for name in ("alt_a", "alt_b")}
    phases = gen._even_motion_phases(loader, tracks, gen.TRANSCRIBE_FRAMES)

    # A peel is the only time an offset runs negative: the ink slides off the far end
    # rather than retracting the way it came.
    peels = {
        wing: sum(1 for t in phases if gen._dash_offset(tracks[wing], t) < -1.0)
        for wing in ("alt_a", "alt_b")
    }
    assert peels["alt_a"] >= 1, "the first wing's peel is never shown"
    assert peels["alt_b"] >= 1, "the second wing's peel is never shown"
    assert peels["alt_a"] == peels["alt_b"], f"beats covered unequally: {peels}"


def test_recording_holds_the_marks_size_and_place():
    """Recording, the mark with its counter filled, sits exactly where idle does.

    A different extent would jump the panel icon the moment recording starts.
    """
    cairosvg = pytest.importorskip("cairosvg", reason="icons tooling group")
    from PIL import Image

    root = Path(__file__).resolve().parents[1] / "data/icons/hicolor"
    boxes = {}
    for label, url in (
        ("idle", root / f"symbolic/apps/{APP_ID}-symbolic.svg"),
        ("recording", root / f"scalable/apps/{APP_ID}-recording.svg"),
    ):
        png = cairosvg.svg2png(url=str(url), output_width=128, output_height=128)
        boxes[label] = Image.open(io.BytesIO(png)).convert("RGBA").getbbox()

    for a, b in zip(boxes["idle"], boxes["recording"], strict=True):
        assert abs(a - b) <= 2, f"recording is offset from idle: {boxes}"


def test_every_recording_frame_has_a_scalable_form():
    """Every recording frame has an SVG, so a host never mixes vector and bitmap frames.

    Mixed frames make the icon sharpen and soften as the pulse cycles.
    """
    scalable = Path(__file__).resolve().parents[1] / "data/icons/hicolor/scalable/apps"
    for name in _RECORDING_PULSE:
        assert (scalable / f"{name}.svg").is_file(), f"{name} has no scalable form"
