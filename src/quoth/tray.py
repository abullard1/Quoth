# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 Samuel Ruairí Bullard

"""The StatusNotifierItem tray icon and its context menu, served over raw D-Bus.

Registers an SNI item on the session bus so the desktop panel (COSMIC, KDE, GNOME with
AppIndicator, waybar) shows the Quoth mark. Left-click opens Settings (`Activate`);
right-click shows the `com.canonical.dbusmenu` menu the panel renders from the `Menu`
property. Both objects use `Gio.DBus` directly, with no external dependencies.

The icon shows the state (idle, recording, transcribing) and cycles frames while
recording or transcribing (`_PULSE`) unless `set_animated` turned that off; it is the
main recording indicator where the overlay pill is unavailable. `_menu_model()` rebuilds
the menu from the injected `TrayMenu` on every request, and `AboutToShow` answers
`needUpdate=true` only when an item's layout changed since `GetLayout`, so hosts
re-fetch stale state without rebuilding and closing submenus on GNOME AppIndicator.
"""

import traceback
from collections.abc import Callable
from dataclasses import dataclass

import gi

gi.require_version("Gio", "2.0")
from gi.repository import Gio, GLib
from loguru import logger

from quoth import APP_ID
from quoth.models import model_label
from quoth.scheduler import guarded

# --- Menu bridge ---


@dataclass
class TrayMenu:
    """The context menu's bridge to the app: action callbacks and live-state accessors.

    The tray reads state through it on every menu open and holds no config or
    controller references itself. `languages` is the `(label, code)` catalog,
    `list_models` returns the downloaded model names in catalog order, and
    `update_available` returns the newest known version or None.
    """

    languages: tuple[tuple[str, str], ...]
    get_language: Callable[[], str]
    set_language: Callable[[str], None]
    list_models: Callable[[], list[str]]
    get_model: Callable[[], str]
    set_model: Callable[[str], None]
    on_settings: Callable[[], None]
    on_about: Callable[[], None]
    on_quit: Callable[[], None]
    update_available: Callable[[], str | None]
    view_release: Callable[[], None]
    last_dictation_available: Callable[[], bool]
    copy_last_dictation: Callable[[], None]


# --- D-Bus interface XML ---

# The SNI properties must be declared here: Gio exposes org.freedesktop.DBus.Properties
# and routes Get/GetAll to our get_property callback only for declared properties.
# Without them GetAll returns {} and the host (COSMIC panel) shows no icon.
_SNI_XML = """
<node>
  <interface name="org.kde.StatusNotifierItem">
    <property type="s" name="Category" access="read"/>
    <property type="s" name="Id" access="read"/>
    <property type="s" name="Title" access="read"/>
    <property type="s" name="Status" access="read"/>
    <property type="i" name="WindowId" access="read"/>
    <property type="s" name="IconName" access="read"/>
    <property type="a(iiay)" name="IconPixmap" access="read"/>
    <property type="s" name="IconThemePath" access="read"/>
    <property type="s" name="OverlayIconName" access="read"/>
    <property type="a(iiay)" name="OverlayIconPixmap" access="read"/>
    <property type="s" name="AttentionIconName" access="read"/>
    <property type="a(iiay)" name="AttentionIconPixmap" access="read"/>
    <property type="s" name="AttentionMovieName" access="read"/>
    <property type="(sa(iiay)ss)" name="ToolTip" access="read"/>
    <property type="o" name="Menu" access="read"/>
    <property type="b" name="ItemIsMenu" access="read"/>
    <method name="Activate">
      <arg type="i" name="x" direction="in"/>
      <arg type="i" name="y" direction="in"/>
    </method>
    <method name="ContextMenu">
      <arg type="i" name="x" direction="in"/>
      <arg type="i" name="y" direction="in"/>
    </method>
    <method name="SecondaryActivate">
      <arg type="i" name="x" direction="in"/>
      <arg type="i" name="y" direction="in"/>
    </method>
    <signal name="NewIcon"/>
    <signal name="NewToolTip"/>
    <signal name="NewStatus">
      <arg type="s" name="status"/>
    </signal>
  </interface>
</node>
"""

# The full com.canonical.dbusmenu contract. Implementing all of it, not just
# GetLayout, is what makes the menu render identically across COSMIC, KDE,
# GNOME(+ext) and waybar.
_DBUSMENU_XML = """
<node>
  <interface name="com.canonical.dbusmenu">
    <property type="u" name="Version" access="read"/>
    <property type="s" name="TextDirection" access="read"/>
    <property type="s" name="Status" access="read"/>
    <property type="as" name="IconThemePath" access="read"/>
    <method name="GetLayout">
      <arg type="i" name="parentId" direction="in"/>
      <arg type="i" name="recursionDepth" direction="in"/>
      <arg type="as" name="propertyNames" direction="in"/>
      <arg type="u" name="revision" direction="out"/>
      <arg type="(ia{sv}av)" name="layout" direction="out"/>
    </method>
    <method name="GetGroupProperties">
      <arg type="ai" name="ids" direction="in"/>
      <arg type="as" name="propertyNames" direction="in"/>
      <arg type="a(ia{sv})" name="properties" direction="out"/>
    </method>
    <method name="GetProperty">
      <arg type="i" name="id" direction="in"/>
      <arg type="s" name="name" direction="in"/>
      <arg type="v" name="value" direction="out"/>
    </method>
    <method name="Event">
      <arg type="i" name="id" direction="in"/>
      <arg type="s" name="eventId" direction="in"/>
      <arg type="v" name="data" direction="in"/>
      <arg type="u" name="timestamp" direction="in"/>
    </method>
    <method name="EventGroup">
      <arg type="a(isvu)" name="events" direction="in"/>
      <arg type="ai" name="idErrors" direction="out"/>
    </method>
    <method name="AboutToShow">
      <arg type="i" name="id" direction="in"/>
      <arg type="b" name="needUpdate" direction="out"/>
    </method>
    <method name="AboutToShowGroup">
      <arg type="ai" name="ids" direction="in"/>
      <arg type="ai" name="updatesNeeded" direction="out"/>
      <arg type="ai" name="idErrors" direction="out"/>
    </method>
    <signal name="ItemsPropertiesUpdated">
      <arg type="a(ia{sv})" name="updatedProps"/>
      <arg type="a(ias)" name="removedProps"/>
    </signal>
    <signal name="LayoutUpdated">
      <arg type="u" name="revision"/>
      <arg type="i" name="parent"/>
    </signal>
    <signal name="ItemActivationRequested">
      <arg type="i" name="id"/>
      <arg type="u" name="timestamp"/>
    </signal>
  </interface>
</node>
"""

# State-independent dbusmenu properties. Version is REQUIRED: without it COSMIC
# drops the menu and right-click does nothing.
_DBUSMENU_PROPS = {
    "Version": GLib.Variant("u", 3),
    "Status": GLib.Variant("s", "normal"),
    "TextDirection": GLib.Variant("s", "ltr"),
    "IconThemePath": GLib.Variant("as", []),
}

# Stable menu item ids. Fixed items take low ids; the dynamic submenu children are
# numbered from a base + catalog index so an id always maps back to one choice.
_ID_SETTINGS = 1
_ID_ABOUT = 2
_ID_QUIT = 3
_ID_UPDATE = 4
_ID_COPY_LAST = 5
_ID_LANGUAGE = 10
_ID_MODEL = 11
_ID_SEP_TOP = 20
_ID_SEP_BOTTOM = 21
_ID_LANG_BASE = 100
_ID_MODEL_BASE = 200

# --- Icon mapping ---

# Idle and transcribing are recolourable symbolic marks; recording fills the ring's
# counter red, the recording indicator on GNOME, where there is no overlay. The panel
# looks these names up in the system theme.
_ICONS = {
    "idle": f"{APP_ID}-symbolic",
    "recording": f"{APP_ID}-recording",
    "transcribing": f"{APP_ID}-transcribing-symbolic",
}

_TOOLTIPS = {
    "idle": "Quoth: ready",
    "recording": "Quoth: recording…",
    "transcribing": "Quoth: transcribing…",
}

# Recording-pulse animation: cycle the recording disc through reduced-opacity frames
# for a slow "breath" while recording. Frame 0 is the static recording icon, so
# stopping snaps cleanly back to it.
_RECORDING_PULSE = (
    f"{APP_ID}-recording",
    f"{APP_ID}-recording-pulse-1",
    f"{APP_ID}-recording-pulse-2",
    f"{APP_ID}-recording-pulse-1",
)
# Transcribing runs the brand loader. Frame 0, the static icon, is caught mid-unfurl,
# so hosts that never animate still read as working. The frames are spaced by equal
# motion, so no tick sits still.
_TRANSCRIBING_PULSE = (
    f"{APP_ID}-transcribing-symbolic",
    *(f"{APP_ID}-transcribing-pulse-{i}-symbolic" for i in range(1, 16)),
)
# The animated states, each as (frames, interval_ms).
_PULSE: dict[str, tuple[tuple[str, ...], int]] = {
    "recording": (_RECORDING_PULSE, 400),  # ~2.5 Hz: a calm ~1.6 s breath
    "transcribing": (_TRANSCRIBING_PULSE, 200),  # the loader's whole loop, ~3.2 s
}

# Constant SNI properties (state-independent). The per-state ones (IconName,
# ToolTip) are computed in _get_sni_property.
_STATIC_SNI_PROPS = {
    "Category": GLib.Variant("s", "ApplicationStatus"),
    "Id": GLib.Variant("s", "quoth"),
    "Title": GLib.Variant("s", "Quoth"),
    # Active in every state: the tray is Quoth's only access point, and SNI hosts may
    # hide a Passive item (COSMIC does). The icon, not the visibility, shows the state.
    "Status": GLib.Variant("s", "Active"),
    "IconPixmap": GLib.Variant("a(iiay)", []),
    "OverlayIconName": GLib.Variant("s", ""),
    "OverlayIconPixmap": GLib.Variant("a(iiay)", []),
    "AttentionIconName": GLib.Variant("s", ""),
    "AttentionIconPixmap": GLib.Variant("a(iiay)", []),
    "AttentionMovieName": GLib.Variant("s", ""),
    "Menu": GLib.Variant("o", "/StatusNotifierItem/Menu"),
    "ItemIsMenu": GLib.Variant("b", False),
    "WindowId": GLib.Variant("i", 0),
}


@dataclass
class _Item:
    """A node of the dbusmenu tree.

    `props` are the dbusmenu item properties as GVariants; `action` runs on click (None
    for separators and submenu parents); `children` are nested nodes (a submenu when
    non-empty).
    """

    id: int
    props: dict[str, GLib.Variant]
    action: Callable[[], None] | None = None
    children: tuple["_Item", ...] = ()


class QuothTray:
    """The StatusNotifierItem tray icon and its dbusmenu context menu."""

    def __init__(self, menu: TrayMenu, icon_theme_path: str = ""):
        self._menu = menu
        # An extra icon-theme search root advertised to the host (SNI IconThemePath), so
        # the tray finds the app's own icons when running from source. Empty once
        # installed, since packages put the icons in the system theme.
        self._icon_theme_path = icon_theme_path
        self._state = "idle"
        self._conn: Gio.DBusConnection | None = None
        self._sni_reg_id = 0
        self._menu_reg_id = 0
        self._watcher_watch_id = 0
        self._menu_revision = 1
        self._served_layouts: dict[int, tuple] = {}
        self._menu_failures: set[tuple] = set()
        # dbusmenu method -> handler; a method missing here is an UnknownMethod error.
        self._menu_methods: dict[str, Callable] = {
            "GetLayout": self._menu_get_layout,
            "GetGroupProperties": self._menu_group_properties,
            "GetProperty": self._menu_get_property,
            "Event": self._menu_event,
            "EventGroup": self._menu_event_group,
            "AboutToShow": self._menu_about_to_show,
            "AboutToShowGroup": self._menu_about_to_show_group,
        }
        self._animated = True
        self._pulse_timer_id = 0
        self._pulse_index = 0

    def start(self):
        """Register the tray item on the session bus and announce it to the watcher.

        The item goes under our unique connection name, not a well-known
        `org.kde.StatusNotifierItem-<pid>-N`: a Flatpak may own names only under its
        own app id.
        """
        try:
            self._conn = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        except GLib.Error as e:
            logger.warning("Tray: no session bus; tray unavailable ({})", e.message)
            return
        self._register_objects(self._conn)
        # Announce through a name watch, which fires again whenever the watcher gains a
        # new owner: a restarted panel comes back with an empty item list.
        self._watcher_watch_id = Gio.bus_watch_name_on_connection(
            self._conn,
            "org.kde.StatusNotifierWatcher",
            Gio.BusNameWatcherFlags.NONE,
            self._on_watcher_appeared,
            self._on_watcher_vanished,
        )

    def _on_watcher_vanished(self, _conn, _name):
        """Log that no tray host is present, as on stock GNOME or a restarting panel.

        A host that appears later gets the item announced.
        """
        logger.warning("Tray: no StatusNotifierWatcher; the icon is hidden for now")

    def _on_watcher_appeared(self, conn, name, name_owner):
        """Announce our item to the StatusNotifierWatcher's new owner."""
        self._register_with_watcher(conn)

    def stop(self):
        """Unregister the tray icon."""
        self._stop_pulse()
        if self._watcher_watch_id:
            Gio.bus_unwatch_name(self._watcher_watch_id)
            self._watcher_watch_id = 0
        if self._conn:
            if self._sni_reg_id:
                self._conn.unregister_object(self._sni_reg_id)
                self._sni_reg_id = 0
            if self._menu_reg_id:
                self._conn.unregister_object(self._menu_reg_id)
                self._menu_reg_id = 0
        logger.debug("Tray stopped")

    def set_state(self, state: str):
        """Show the app state ("idle", "recording", "transcribing") in the icon."""
        if state == self._state:
            return
        logger.debug("Tray state {} -> {}", self._state, state)
        self._state = state
        self._stop_pulse()
        if self._conn and self._sni_reg_id:
            self._emit_signal("NewIcon")
            self._emit_signal("NewToolTip")
        self._maybe_start_pulse()

    def refresh_menu(self) -> None:
        """Emit LayoutUpdated if the menu tree changed since the last root GetLayout."""
        root, _by_id = self._menu_model()
        if (
            0 in self._served_layouts
            and self._snapshot(root) == self._served_layouts[0]
        ):
            return
        self._menu_revision += 1
        self._emit_menu_signal(
            "LayoutUpdated", GLib.Variant("(ui)", (self._menu_revision, 0))
        )

    def set_animated(self, animated: bool):
        """Turn the icon animation on or off; off snaps back to the static icon.

        The app sets it from the system reduced-motion preference.
        """
        if animated == self._animated:
            return
        self._animated = animated
        if animated:
            self._maybe_start_pulse()
        else:
            self._stop_pulse()
            if self._conn and self._sni_reg_id:
                self._emit_signal("NewIcon")

    def _maybe_start_pulse(self):
        """Start cycling frames if the current state animates and animation is enabled.

        A running timer is kept, so a second start never leaks another one.
        """
        if self._pulse_timer_id or not self._animated or self._state not in _PULSE:
            return
        self._pulse_index = 0
        self._pulse_timer_id = GLib.timeout_add(
            _PULSE[self._state][1],
            guarded(self._pulse_tick, on_error=self._forget_pulse),
        )

    def _forget_pulse(self):
        self._pulse_timer_id = 0

    def _stop_pulse(self):
        if self._pulse_timer_id:
            GLib.source_remove(self._pulse_timer_id)
            self._pulse_timer_id = 0
        self._pulse_index = 0

    def _pulse_tick(self) -> bool:
        # A state change stops the timer, so the state still animates here.
        frames, _interval = _PULSE[self._state]
        self._pulse_index = (self._pulse_index + 1) % len(frames)
        self._emit_signal("NewIcon")
        return True

    # --- D-Bus callbacks ---

    def _register_objects(self, conn):
        """Export the StatusNotifierItem and dbusmenu objects on the session connection.

        Gio serves org.freedesktop.DBus.Properties from the <property> declarations and
        the get_property callbacks.
        """
        sni_info = Gio.DBusNodeInfo.new_for_xml(_SNI_XML)
        self._sni_reg_id = conn.register_object(
            "/StatusNotifierItem",
            sni_info.interfaces[0],  # org.kde.StatusNotifierItem
            self._handle_sni_method,
            self._handle_sni_get_property,
            None,
        )
        menu_info = Gio.DBusNodeInfo.new_for_xml(_DBUSMENU_XML)
        self._menu_reg_id = conn.register_object(
            "/StatusNotifierItem/Menu",
            menu_info.interfaces[0],
            self._handle_menu_method,
            self._handle_menu_get_property,
            None,
        )

    def _register_with_watcher(self, conn):
        """Announce the item to the watcher under our unique connection name.

        The StatusNotifierWatcher then reads our objects at /StatusNotifierItem.
        """
        try:
            conn.call_sync(
                "org.kde.StatusNotifierWatcher",
                "/StatusNotifierWatcher",
                "org.kde.StatusNotifierWatcher",
                "RegisterStatusNotifierItem",
                GLib.Variant("(s)", (conn.get_unique_name(),)),
                None,
                Gio.DBusCallFlags.NONE,
                -1,
                None,
            )
            logger.info(
                "Tray: registered with StatusNotifierWatcher ({}; icons from {})",
                conn.get_unique_name(),
                self._icon_theme_path or "the system theme",
            )
        except GLib.Error as e:
            logger.warning("Tray: StatusNotifierWatcher not available ({})", e.message)

    # --- SNI method/property handlers ---

    def _handle_sni_method(self, conn, sender, path, iface, method, params, invocation):
        # Properties (Get/GetAll) are served by Gio via the get_property callback
        # and the <property> declarations in _SNI_XML, not routed here.
        if method == "Activate":
            logger.debug("Tray: Activate")
            # Left-click opens Settings. Right-click shows the dbusmenu (rendered by the
            # host from the Menu property), so ContextMenu stays a no-op.
            GLib.idle_add(self._menu.on_settings)
            invocation.return_value(None)
        elif method == "ContextMenu":
            invocation.return_value(None)
        elif method == "SecondaryActivate":
            invocation.return_value(None)
        else:
            invocation.return_dbus_error(
                "org.freedesktop.DBus.Error.UnknownMethod",
                f"Unknown method: {method}",
            )

    def _handle_sni_get_property(self, conn, sender, path, iface, prop_name):
        return self._get_sni_property(prop_name)

    def _get_sni_property(self, name: str) -> GLib.Variant | None:
        if name == "IconThemePath":
            return GLib.Variant("s", self._icon_theme_path)
        if name == "IconName":
            spec = _PULSE.get(self._state)
            if spec and self._animated:
                return GLib.Variant("s", spec[0][self._pulse_index])
            return GLib.Variant("s", _ICONS[self._state])
        if name == "ToolTip":
            # (icon_name, icon_pixmap, title, description)
            return GLib.Variant(
                "(sa(iiay)ss)",
                (
                    "",
                    [],
                    _TOOLTIPS[self._state],
                    "",
                ),
            )
        return _STATIC_SNI_PROPS.get(name)

    def _emit_signal(self, signal_name: str, params=None):
        if self._conn:
            self._conn.emit_signal(
                None,
                "/StatusNotifierItem",
                "org.kde.StatusNotifierItem",
                signal_name,
                params,
            )

    def _emit_menu_signal(self, signal_name: str, params=None):
        if self._conn and self._menu_reg_id:
            self._conn.emit_signal(
                None,
                "/StatusNotifierItem/Menu",
                "com.canonical.dbusmenu",
                signal_name,
                params,
            )

    # --- Menu model (rebuilt fresh per request) ---

    def _menu_items(self) -> list[_Item]:
        """Build the top-level items from the bridge's current state.

        Rebuilt on every request so labels and checkmarks are correct each time a host
        opens the menu.
        """
        m = self._menu
        items = [
            self._submenu(_ID_LANGUAGE, "Language", self._language_children()),
            self._submenu(_ID_MODEL, "Model", self._model_children()),
            self._separator(_ID_SEP_TOP),
        ]
        if m.last_dictation_available():
            items.append(
                self._leaf(_ID_COPY_LAST, "Copy last dictation", m.copy_last_dictation)
            )
        items.append(self._leaf(_ID_SETTINGS, "Settings", m.on_settings))
        available = m.update_available()
        if available:
            items.append(
                self._leaf(
                    _ID_UPDATE, f"Update available ({available})", m.view_release
                )
            )
        items.append(self._leaf(_ID_ABOUT, "About Quoth", m.on_about))
        items.append(self._separator(_ID_SEP_BOTTOM))
        items.append(self._leaf(_ID_QUIT, "Quit Quoth", m.on_quit))
        return items

    def _language_children(self) -> list[_Item]:
        current = self._menu.get_language()
        return [
            self._leaf(
                _ID_LANG_BASE + i,
                label,
                lambda c=code: self._menu.set_language(c),
                checked=(code == current),
            )
            for i, (label, code) in enumerate(self._menu.languages)
        ]

    def _model_children(self) -> list[_Item]:
        current = self._menu.get_model()
        return [
            self._leaf(
                _ID_MODEL_BASE + i,
                model_label(name),
                lambda n=name: self._menu.set_model(n),
                checked=(name == current),
            )
            for i, name in enumerate(self._menu.list_models())
        ]

    def _leaf(
        self,
        item_id: int,
        label: str,
        action: Callable[[], None],
        *,
        checked: bool | None = None,
    ) -> _Item:
        props = {
            "label": GLib.Variant("s", label),
            "enabled": GLib.Variant("b", True),
        }
        if checked is not None:  # a checkmark-toggle row (the active selection ticked)
            props["toggle-type"] = GLib.Variant("s", "checkmark")
            props["toggle-state"] = GLib.Variant("i", 1 if checked else 0)
        return _Item(id=item_id, props=props, action=action)

    def _separator(self, item_id: int) -> _Item:
        return _Item(
            id=item_id,
            props={
                "type": GLib.Variant("s", "separator"),
                "enabled": GLib.Variant("b", False),
            },
        )

    def _submenu(self, item_id: int, label: str, children: list[_Item]) -> _Item:
        return _Item(
            id=item_id,
            props={
                "label": GLib.Variant("s", label),
                "enabled": GLib.Variant("b", True),
                "children-display": GLib.Variant("s", "submenu"),
            },
            children=tuple(children),
        )

    def _menu_model(self) -> tuple[_Item, dict[int, _Item]]:
        """Build the whole tree (root id 0 and the items) and its id -> node index.

        One builder feeds GetLayout, GetGroupProperties, GetProperty and the Event
        methods.
        """
        root = _Item(
            id=0,
            props={"children-display": GLib.Variant("s", "submenu")},
            children=tuple(self._menu_items()),
        )
        by_id: dict[int, _Item] = {}

        def index(item: _Item):
            by_id[item.id] = item
            for child in item.children:
                index(child)

        index(root)
        return root, by_id

    @staticmethod
    def _snapshot(item: _Item, cache: dict[int, tuple] | None = None) -> tuple:
        snap = (
            item.id,
            tuple(sorted((k, v.unpack()) for k, v in item.props.items())),
            tuple(QuothTray._snapshot(child, cache) for child in item.children),
        )
        if cache is not None:
            cache[item.id] = snap
        return snap

    def _layout(self, item: _Item):
        """Return an item's (ia{sv}av) layout as a Python tuple.

        Children are `av` elements, each a Variant directly containing (ia{sv}av):
        single-wrapped, since a double wrap breaks deserialisation.
        """
        children = [GLib.Variant("(ia{sv}av)", self._layout(c)) for c in item.children]
        return (item.id, item.props, children)

    # --- Dbusmenu handlers ---

    def _handle_menu_get_property(self, conn, sender, path, iface, prop_name):
        return _DBUSMENU_PROPS.get(prop_name)

    def _handle_menu_method(
        self, conn, sender, path, iface, method, params, invocation
    ):
        handler = self._menu_methods.get(method)
        if handler is None:
            invocation.return_dbus_error(
                "org.freedesktop.DBus.Error.UnknownMethod",
                f"Unknown method: {method}",
            )
            return
        # A raising handler never answers, and the host's menu would hang until timeout.
        try:
            handler(params, invocation)
        except Exception as error:
            # Hosts ask on every menu open: each distinct failure logs its trace once.
            where = traceback.extract_tb(error.__traceback__)[-1]
            failure = (method, type(error), where.filename, where.lineno)
            if failure in self._menu_failures:
                logger.debug("Tray: menu {} failed again", method)
            else:
                self._menu_failures.add(failure)
                logger.exception("Tray: menu {} failed", method)
            invocation.return_dbus_error("org.freedesktop.DBus.Error.Failed", method)

    def _menu_get_layout(self, params, invocation):
        parent_id, _depth, _names = params.unpack()
        _root, by_id = self._menu_model()
        target = by_id.get(parent_id)
        if target is None:
            invocation.return_dbus_error(
                "com.canonical.dbusmenu.Error", f"No menu item {parent_id}"
            )
            return
        self._snapshot(target, self._served_layouts)
        layout = self._layout(target)
        invocation.return_value(
            GLib.Variant("(u(ia{sv}av))", (self._menu_revision, layout))
        )

    def _menu_group_properties(self, params, invocation):
        ids, names = params.unpack()
        _root, by_id = self._menu_model()
        wanted = ids if ids else list(by_id)
        result = [
            (i, self._filter_props(by_id[i].props, names)) for i in wanted if i in by_id
        ]
        invocation.return_value(GLib.Variant("(a(ia{sv}))", (result,)))

    def _menu_get_property(self, params, invocation):
        item_id, name = params.unpack()
        _root, by_id = self._menu_model()
        item = by_id.get(item_id)
        if item is None or name not in item.props:
            invocation.return_dbus_error(
                "com.canonical.dbusmenu.Error",
                f"No property {name!r} on item {item_id}",
            )
        else:
            invocation.return_value(GLib.Variant("(v)", (item.props[name],)))

    def _menu_event(self, params, invocation):
        item_id, event_id, _data, _ts = params.unpack()
        if event_id == "clicked":
            self._dispatch(item_id)
        invocation.return_value(None)

    def _menu_event_group(self, params, invocation):
        (events,) = params.unpack()
        _root, by_id = self._menu_model()
        id_errors = []
        for item_id, event_id, _data, _ts in events:
            if item_id not in by_id:
                id_errors.append(item_id)
            elif event_id == "clicked":
                self._dispatch(item_id, by_id)
        invocation.return_value(GLib.Variant("(ai)", (id_errors,)))

    def _menu_about_to_show(self, params, invocation):
        (item_id,) = params.unpack()
        _root, by_id = self._menu_model()
        item = by_id.get(item_id)
        need_update = item is not None and self._snapshot(
            item
        ) != self._served_layouts.get(item_id)
        invocation.return_value(GLib.Variant("(b)", (need_update,)))

    def _menu_about_to_show_group(self, params, invocation):
        (ids,) = params.unpack()
        _root, by_id = self._menu_model()
        updates_needed = []
        id_errors = []
        for item_id in ids:
            item = by_id.get(item_id)
            if item is None:
                id_errors.append(item_id)
            elif self._snapshot(item) != self._served_layouts.get(item_id):
                updates_needed.append(item_id)
        invocation.return_value(GLib.Variant("(aiai)", (updates_needed, id_errors)))

    @staticmethod
    def _filter_props(
        props: dict[str, GLib.Variant], names: list[str]
    ) -> dict[str, GLib.Variant]:
        """Return `props` filtered to `names`; empty `names` means all, per the spec."""
        if not names:
            return props
        return {k: v for k, v in props.items() if k in names}

    def _dispatch(self, item_id: int, by_id: dict[int, _Item] | None = None):
        """Run a clicked item's action on the main thread through `GLib.idle_add`.

        The model is rebuilt (or reused within an EventGroup) so the closure reflects
        the current state.
        """
        if by_id is None:
            _root, by_id = self._menu_model()
        item = by_id.get(item_id)
        if item and item.action:
            logger.debug(
                "Tray: menu action dispatched (item {}, {})",
                item_id,
                item.props["label"].get_string(),
            )
            GLib.idle_add(item.action)
