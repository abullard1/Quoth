# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 Samuel Ruairí Bullard

"""Settings window: the GTK view that `SettingsController` drives.

It lays out the four pages; the logic lives in settings.py, and engine_section.py,
overlay_section.py and skin_section.py build their own groups.
"""

from collections.abc import Callable
from dataclasses import dataclass

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, Gdk, Gtk

from quoth import (
    APP_ID,
    SUPPORT_URL,
    __version__,
    brand,
    crash_reports,
    hotkey_capture,
    presentation,
    shortcut_setup,
    updates,
)
from quoth.about import PRIVACY_URL, open_uri, show_about_dialog
from quoth.config import Config
from quoth.engine_section import CalibrationBridge, EngineSection
from quoth.listener import ListenerState
from quoth.models import ModelManager
from quoth.overlay_section import OverlaySection, ThemeBridge
from quoth.settings import ModelSwitcher, SettingsController, SettingsView
from quoth.skin_section import SkinBridge, SkinSection
from quoth.trigger import MODES


def _feedback_value(active: bool) -> str:
    return "on" if active else "off"


# Installed once and kept for the process. A display provider is never removed, and the
# Settings window is rebuilt on every open, so a per-instance provider would pile up.
_ROW_CSS: Gtk.CssProvider | None = None


@dataclass
class UpdateBridge:
    """App-side update state and affordances the settings view consumes.

    The state is read live, and the app calls `refresh_updates` whenever it changes.
    """

    # The newest release known this run, kept through a failed re-check.
    available: Callable[[], str | None]
    checking: Callable[[], bool]
    checked: Callable[[], bool]  # a check has reached GitHub this run
    failed: Callable[[], bool]  # the last check could not reach GitHub
    check_now: Callable[[], None]
    view_release: Callable[[], None]


@dataclass(frozen=True)
class UsageInfo:
    """Read-only usage figures for the Settings summary.

    `since` is a display-ready label, or None before the first dictation.
    """

    dictations_total: int
    since: str | None


class SettingsWindow(Adw.PreferencesWindow):
    """Settings window for Quoth configuration.

    A standalone PreferencesWindow, since Quoth has no application window to parent
    a PreferencesDialog.
    """

    def __init__(
        self,
        config: Config,
        model_mgr: ModelManager,
        model_switcher: ModelSwitcher,
        update_bridge: UpdateBridge | None = None,
        calibration_bridge: CalibrationBridge | None = None,
        is_idle: Callable[[], bool] | None = None,
        on_hotkey_capture: Callable[[bool], None] | None = None,
        theme_bridge: ThemeBridge | None = None,
        usage: UsageInfo | None = None,
        skin_bridge: SkinBridge | None = None,
        *,
        reconcile_engine: Callable[[], None],
        enumerate_microphones: Callable[[], list[dict]],
        portal_shortcuts=None,
        listener_state: ListenerState = ListenerState.STOPPED,
        motion_enabled: bool = True,
    ):
        super().__init__(title="Quoth Settings")
        self.add_css_class("quoth-settings")
        self._motion_enabled = motion_enabled
        # Wide enough for the page switcher to sit in the header bar (libadwaita
        # drops it to a bottom bar under ~550sp); on a narrow screen it still does.
        self.set_default_size(640, 640)
        self._config = config
        self._model_mgr = model_mgr
        self._enumerate_microphones = enumerate_microphones
        self._is_idle = is_idle
        # Suspends the live listener while a hotkey is captured.
        self._on_hotkey_capture = on_hotkey_capture
        self._portal_shortcuts = portal_shortcuts
        self._recording_group: Adw.PreferencesGroup | None = None
        self._mode_row: Adw.ComboRow | None = None
        self._hotkey_row: Adw.ActionRow | None = None
        self._sound_row: Adw.SwitchRow | None = None
        self._notification_row: Adw.SwitchRow | None = None
        self._shortcut_group: Adw.PreferencesGroup | None = None
        self._portal_unsubscribe: Callable[[], None] | None = None
        self._portal_shortcut_row: Adw.ActionRow | None = None
        self._portal_shortcut_action: Gtk.Button | None = None
        self._manual_shortcut_row: Adw.ActionRow | None = None
        self._manual_open_row: Adw.ActionRow | None = None
        # Update surfaces (only built when the app supplies a bridge).
        self._update_bridge = update_bridge
        self._version_label: Gtk.Label | None = None
        self._update_status_row: Adw.ActionRow | None = None
        self._update_spinner: Gtk.Spinner | None = None
        self._update_view_btn: Gtk.Button | None = None
        self._update_check_btn: Gtk.Button | None = None
        self._update_check_stack: Gtk.Stack | None = None
        # The transcription engine section (model/language/device/benchmark/mic) owns
        # the calibration bridge; the app supplies it only when benchmarking is offered.
        self._calibration_bridge = calibration_bridge
        self._engine_section: EngineSection | None = None
        # The Overlay group (theme picker, preview and orb switch) is built only when
        # the app supplies a theme bridge with presets and the overlay can render.
        self._theme_bridge = theme_bridge
        self._overlay_section: OverlaySection | None = None
        # Read-only local usage summary (only built when the app supplies the figures).
        self._usage = usage
        self._skin_bridge = skin_bridge
        self._skin_section: SkinSection | None = None
        self._apply_css()
        self._build_ui()  # creates the widgets the SettingsView binds to
        self.refresh_listener(listener_state)
        if self._portal_shortcuts is not None:
            self._portal_unsubscribe = self._portal_shortcuts.subscribe(
                self._refresh_portal_shortcut
            )
            self._refresh_portal_shortcut()
        brand.pointer_cursors(self)
        # The engine section is built first (in _build_ui), since the controller's view
        # callbacks point at it.
        self._controller = SettingsController(
            config,
            model_mgr,
            model_switcher,
            SettingsView(
                set_download_active=self._engine_section.set_download_active,
                set_download_progress=self._engine_section.set_download_progress,
                toast=self.show_toast,
                revert=self._engine_section.revert,
                on_model_changed=self._engine_section.on_model_changed,
            ),
            reconcile=reconcile_engine,
        )
        self._engine_section.attach_controller(self._controller)
        self._controller.open()
        self._config_handler = config.connect_changed(self._on_config_changed)
        self.connect("close-request", self._on_settings_close)

    def _apply_css(self):
        """Indent nested row content; the brand provider owns its palette."""
        global _ROW_CSS
        if _ROW_CSS is not None:
            return
        _ROW_CSS = Gtk.CssProvider()
        _ROW_CSS.load_from_string(
            ".quoth-settings row.expander list.nested > row > box.header"
            " { margin-left: 32px; }"
        )
        Gtk.StyleContext.add_provider_for_display(
            self.get_display() or Gdk.Display.get_default(),
            _ROW_CSS,
            Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION,
        )

    def _build_ui(self):
        """Build the four pages, one per question a user brings to Settings."""
        # Underlines are mnemonics: Alt+D/F/P/G switch pages from the keyboard.
        dictation = self._add_page("_Dictation", "audio-input-microphone-symbolic")
        feedback = self._add_page("_Feedback", "view-reveal-symbolic")
        performance = self._add_page("_Performance", f"{APP_ID}-gauge-symbolic")
        general = self._add_page("_General", "preferences-system-symbolic")

        # Dictation: the key, the model that listens, the microphone it hears through.
        # The portal owns sandbox shortcuts; native controls follow the live listener.
        if self._portal_shortcuts is not None:
            self._build_portal_shortcut_group(dictation)
        else:
            self._build_recording_group(dictation)
            self._build_shortcut_group(dictation)
        self._engine_section = EngineSection(
            self._config,
            self._model_mgr,
            calibration_bridge=self._calibration_bridge,
            enumerate_microphones=self._enumerate_microphones,
            toast=self.show_toast,
        )
        self._engine_section.attach(self)
        self._engine_section.build_transcription(dictation)
        self._engine_section.build_audio(dictation)

        # Feedback: the overlay and cues for setups without one. Without layer-shell the
        # overlay never renders, so its group is left out.
        if (
            self._theme_bridge is not None
            and self._theme_bridge.presets
            and presentation.overlay_available()
        ):
            self._overlay_section = OverlaySection(
                self._config,
                self._theme_bridge,
                toast=self.show_toast,
                motion_enabled=self._motion_enabled,
            )
            self._overlay_section.build(feedback, self)
        self._build_feedback_group(feedback)

        # Performance: speed against memory and disk.
        self._engine_section.build_acceleration(performance)
        self._build_memory_group(performance)

        if self._skin_bridge is not None:
            self._skin_section = SkinSection(self._skin_bridge, toast=self.show_toast)
            self._skin_section.build(general, self)
        self._build_general_group(general)
        if self._usage is not None:
            self._build_usage_group(general)
        self._build_about_group(general)

    def _add_page(self, title: str, icon_name: str) -> Adw.PreferencesPage:
        page = Adw.PreferencesPage(
            title=title,
            icon_name=icon_name,
            use_underline=True,
        )
        self.add(page)
        return page

    def _build_recording_group(self, page: Adw.PreferencesPage):
        """Build the input mode and hotkey rows for the app's own evdev key capture."""
        group = Adw.PreferencesGroup(title="Recording")
        self._recording_group = group
        page.add(group)

        self._add_mode_row(group, "How the dictation shortcut controls recording")

        hotkey_row = Adw.ActionRow(
            title="Dictation shortcut",
            activatable=True,
            use_markup=False,
            tooltip_text="The keys in the shortcut also reach the focused app.",
        )
        # A keycap can be "<" or "&": set it once markup is off, or construction
        # parses it as markup.
        hotkey_row.set_subtitle(hotkey_capture.hotkey_label(self._config.hotkey))
        hotkey_row.add_suffix(Gtk.Image.new_from_icon_name("go-next-symbolic"))
        hotkey_row.connect("activated", self._on_hotkey_row_activated)
        group.add(hotkey_row)
        self._hotkey_row = hotkey_row

    def _build_general_group(self, page: Adw.PreferencesPage):
        """Build the General group: set-once options outside the dictation flow."""
        group = Adw.PreferencesGroup(title="General")
        page.add(group)

        login_row = Adw.SwitchRow(
            title="Launch at login",
            subtitle="Start Quoth when you log in",
            tooltip_text=(
                "Runs in the background, so dictation is ready right after login"
            ),
        )
        self._config.bind("launch_at_login", login_row, "active")
        group.add(login_row)

        if self._update_bridge is not None:
            self._build_update_row(group)

        reports_row = Adw.SwitchRow(
            title=crash_reports.SWITCH_TITLE,
            subtitle=crash_reports.SWITCH_SUBTITLE,
            tooltip_text=crash_reports.SWITCH_HELP,
        )
        self._config.bind("crash_reports", reports_row, "active")
        group.add(reports_row)
        notice_row = Adw.ActionRow(
            title=crash_reports.NOTICE_TITLE,
            subtitle=crash_reports.NOTICE_SUBTITLE,
            tooltip_text=crash_reports.NOTICE_HELP,
            activatable=True,
        )
        notice_row.add_suffix(
            Gtk.Image.new_from_icon_name("adw-external-link-symbolic")
        )
        notice_row.connect("activated", lambda _r: self._open_privacy_notice())
        group.add(notice_row)

        self._build_support_row(group)

    def _build_support_row(self, group: Adw.PreferencesGroup):
        """Add a link to the funding page."""
        row = Adw.ActionRow(
            title="Support Quoth",
            subtitle="Quoth is free and open source",
            tooltip_text="Opens the Buy Me a Coffee page in the browser",
            activatable=True,
        )
        heart = Gtk.Image.new_from_icon_name(f"{APP_ID}-heart-symbolic")
        heart.add_css_class("quoth-heart")
        row.add_css_class("quoth-support")  # the heart beats on hover, see brand.py
        row.add_prefix(heart)
        row.add_suffix(Gtk.Image.new_from_icon_name("adw-external-link-symbolic"))
        row.connect("activated", lambda _r: self._open_support())
        group.add(row)

    def _open_support(self):
        """Open the funding page in the user's browser."""
        if not open_uri(SUPPORT_URL):
            self.show_toast("Couldn't open the support page")

    def _open_privacy_notice(self):
        """Open the error-report privacy notice in the user's browser."""
        if not open_uri(PRIVACY_URL):
            self.show_toast("Couldn't open the privacy notice")

    def _build_usage_group(self, page: Adw.PreferencesPage):
        """Build the read-only usage summary; its figures never leave this machine."""
        group = Adw.PreferencesGroup(title="Usage")
        page.add(group)

        count_row = Adw.ActionRow(
            title="Dictations",
            subtitle="Transcriptions completed on this machine",
            tooltip_text="Counts only dictations that were pasted",
        )
        total = Gtk.Label(label=f"{self._usage.dictations_total:,}")
        total.add_css_class("dim-label")
        count_row.add_suffix(total)
        group.add(count_row)

        if self._usage.since is not None:
            since_row = Adw.ActionRow(
                title="Using Quoth since",
                tooltip_text="Shows the month of the first dictation",
            )
            since = Gtk.Label(label=self._usage.since)
            since.add_css_class("dim-label")
            since_row.add_suffix(since)
            group.add(since_row)

    def _build_update_row(self, group: Adw.PreferencesGroup):
        """Build the one row for the whole update feature.

        Following the HIG's "one state, one control", the switch is the opt-in automatic
        check, the subtitle carries live status, and the suffix buttons run a manual
        check and open the release.
        """
        row = Adw.ActionRow(
            title="Check for updates",
            tooltip_text="Asks GitHub for the latest release; nothing is downloaded",
        )
        switch = Gtk.Switch(valign=Gtk.Align.CENTER)
        self._config.bind("notify_updates", switch, "active")
        # "Check now" works regardless of the toggle (an explicit click is consent);
        # "View release" appears only once an update is known.
        self._update_spinner = Gtk.Spinner(
            halign=Gtk.Align.CENTER, valign=Gtk.Align.CENTER
        )
        self._update_view_btn = Gtk.Button(
            label="View release",
            valign=Gtk.Align.CENTER,
            tooltip_text="Opens the release page in the browser",
        )
        self._update_view_btn.add_css_class("suggested-action")
        self._update_view_btn.connect(
            "clicked", lambda _b: self._update_bridge.view_release()
        )
        self._update_check_btn = Gtk.Button(
            label="Check now",
            valign=Gtk.Align.CENTER,
            tooltip_text="Checks once now and shows the result in this row",
        )
        self._update_check_btn.add_css_class("flat")
        self._update_check_btn.connect(
            "clicked", lambda _b: self._update_bridge.check_now()
        )
        # The button and spinner share one Stack slot, so swapping them never shifts
        # the row.
        self._update_check_stack = Gtk.Stack()
        self._update_check_stack.add_named(self._update_check_btn, "button")
        self._update_check_stack.add_named(self._update_spinner, "spinner")
        # Suffixes pack left to right: the switch last, in line with "Launch at login".
        row.add_suffix(self._update_view_btn)
        row.add_suffix(self._update_check_stack)
        row.add_suffix(switch)
        row.set_activatable_widget(switch)
        group.add(row)
        self._update_status_row = row
        self.refresh_updates()

    def _build_shortcut_group(self, page: Adw.PreferencesPage):
        """Offer a desktop shortcut when native keyboard capture is unavailable."""
        group = Adw.PreferencesGroup(
            title="Dictation shortcut",
            description=shortcut_setup.setup_steps(),
        )
        self._shortcut_group = group
        page.add(group)
        self._add_command_row(group)
        self._add_open_settings_row(group)

    def _build_portal_shortcut_group(self, page: Adw.PreferencesPage) -> None:
        group = Adw.PreferencesGroup(title="Dictation shortcut")
        page.add(group)

        # The desktop describes the binding, as in GNOME's "Press <Super>d".
        row = Adw.ActionRow(
            use_markup=False,
            tooltip_text="Once set, works in every app while Quoth is running",
        )
        action = Gtk.Button(valign=Gtk.Align.CENTER)
        action.connect("clicked", lambda _button: self._portal_shortcuts.configure())
        row.add_suffix(action)
        group.add(row)
        self._portal_shortcut_row = row
        self._portal_shortcut_action = action

        self._add_mode_row(group, "How the shortcut controls recording")
        self._manual_shortcut_row = self._add_command_row(group)
        self._manual_open_row = self._add_open_settings_row(group)

    def _add_mode_row(self, group: Adw.PreferencesGroup, subtitle: str) -> None:
        mode_row = Adw.ComboRow(
            title="Input mode",
            subtitle=subtitle,
            tooltip_text="Takes effect at the next key press",
        )
        mode_row.set_model(
            Gtk.StringList.new([hotkey_capture.MODE_LABELS[mode] for mode in MODES])
        )
        mode_row.set_selected(MODES.index(self._config.mode))
        mode_row.connect("notify::selected", self._on_mode_changed)
        group.add(mode_row)
        self._mode_row = mode_row

    def _add_command_row(self, group: Adw.PreferencesGroup) -> Adw.ActionRow:
        """Add the toggle command with a button that copies it."""
        command = shortcut_setup.toggle_command_str()
        cmd_row = Adw.ActionRow(
            title=command,
            subtitle="Bind this command to a key",
            tooltip_text="If Quoth isn't running, the command only starts it",
        )
        cmd_row.add_css_class("monospace")
        copy = Gtk.Button(
            icon_name="edit-copy-symbolic",
            valign=Gtk.Align.CENTER,
            tooltip_text="Copy command",
        )
        copy.add_css_class("flat")
        copy.update_property([Gtk.AccessibleProperty.LABEL], ["Copy command"])
        copy.connect("clicked", lambda _button: self._copy_command(command))
        cmd_row.add_suffix(copy)
        group.add(cmd_row)
        return cmd_row

    def _add_open_settings_row(
        self, group: Adw.PreferencesGroup
    ) -> Adw.ActionRow | None:
        """Add a row opening the desktop's keyboard settings, where it has any."""
        if not shortcut_setup.keyboard_settings_argv():
            return None
        open_row = Adw.ActionRow(
            title="Open keyboard settings",
            tooltip_text=(
                "Opens the desktop's keyboard settings, where shortcuts are added"
            ),
            activatable=True,
        )
        open_row.add_suffix(Gtk.Image.new_from_icon_name("go-next-symbolic"))
        open_row.connect("activated", self._on_open_keyboard_settings)
        group.add(open_row)
        return open_row

    def _refresh_portal_shortcut(self) -> None:
        info = shortcut_setup.portal_presentation(self._portal_shortcuts)
        self._portal_shortcut_row.set_title(info.title)
        self._portal_shortcut_row.set_subtitle(info.description)
        self._portal_shortcut_action.set_visible(info.action is not None)
        if info.action is not None:
            self._portal_shortcut_action.set_label(info.action)
        self._portal_shortcut_action.set_sensitive(info.can_configure)
        self._mode_row.set_visible(info.ready)
        self._manual_shortcut_row.set_visible(info.manual)
        if self._manual_open_row is not None:
            self._manual_open_row.set_visible(info.manual)

    def _copy_command(self, command: str):
        self.get_clipboard().set(command)
        self.show_toast("Command copied")

    def _on_open_keyboard_settings(self, _row):
        if not shortcut_setup.open_keyboard_settings():
            self.show_toast(shortcut_setup.KEYBOARD_SETTINGS_FAILED_MSG)

    def _build_feedback_group(self, page: Adw.PreferencesPage):
        # Cues for setups without an overlay. Each switch shows what "auto" resolves to
        # (on without an overlay), and "auto" stays stored until the user flips it.
        feedback = Adw.PreferencesGroup(
            title="Cues",
            description=(
                "Sound and notification cues for recording, on by default where the "
                "overlay can't show. Also useful with a screen reader."
            ),
        )
        page.add(feedback)

        sound_row = Adw.SwitchRow(
            title="Recording sound",
            subtitle="A tone when recording starts and stops",
            tooltip_text="Audible feedback for dictating without looking at the screen",
        )
        sound_row.set_active(presentation.feedback_enabled(self._config.sound_feedback))
        sound_row.connect("notify::active", self._on_sound_feedback_changed)
        feedback.add(sound_row)
        self._sound_row = sound_row

        volume_row = Adw.ActionRow(
            title="Sound volume",
            subtitle="Loudness of recording cues",
            tooltip_text="Applies from the next tone that plays",
        )
        volume_scale = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 0, 100, 1)
        self._config.bind("sound_volume", volume_scale.get_adjustment(), "value")
        volume_scale.set_draw_value(True)
        volume_scale.set_hexpand(True)
        volume_row.add_suffix(volume_scale)
        # Only a row's activatable widget is named by the row, and that would make the
        # whole row clickable; the slider takes the row's words instead.
        volume_scale.update_property(
            [Gtk.AccessibleProperty.LABEL, Gtk.AccessibleProperty.DESCRIPTION],
            [volume_row.get_title(), volume_row.get_subtitle()],
        )
        feedback.add(volume_row)

        notif_row = Adw.SwitchRow(
            title="Recording notification",
            subtitle="Status while recording and transcribing",
            tooltip_text=(
                "Shows one notification, removed when transcription ends or is canceled"
            ),
        )
        notif_row.set_active(
            presentation.feedback_enabled(self._config.notification_feedback)
        )
        notif_row.connect("notify::active", self._on_notification_feedback_changed)
        feedback.add(notif_row)
        self._notification_row = notif_row

    def push_level(self, amplitude: float) -> None:
        """Feed the theme preview a real microphone level for its input meter.

        A no-op when the Overlay group is not built.
        """
        if self._overlay_section is not None:
            self._overlay_section.push_level(amplitude)

    def _on_settings_close(self, *_args) -> bool:
        # Drop the preview-cycle timer so it can't outlive the window, and detach from
        # a running model download: it carries on and is announced without the window.
        if self._overlay_section is not None:
            self._overlay_section.close()
        if self._skin_section is not None:
            self._skin_section.close()
        if self._portal_unsubscribe is not None:
            self._portal_unsubscribe()
            self._portal_unsubscribe = None
        if self._config_handler:
            self._config.settings.disconnect(self._config_handler)
            self._config_handler = 0
        self._controller.close()
        return False

    def refresh_theme(self, message: str | None = None) -> None:
        """Refresh the theme picker after theme.toml changes and toast any `message`."""
        if self._overlay_section is not None:
            self._overlay_section.refresh_theme()
        if message is not None:
            self.show_toast(message)

    def refresh_skin(self, message: str | None = None) -> None:
        """Refresh the skin picker after skin.toml changes and toast any `message`."""
        if self._skin_section is not None:
            self._skin_section.refresh_skin()
        if message is not None:
            self.show_toast(message)

    def set_motion_enabled(self, enabled: bool) -> None:
        """Follow the system motion preference in the previews, as the overlay does."""
        self._motion_enabled = enabled
        if self._overlay_section is not None:
            self._overlay_section.set_motion_enabled(enabled)

    def _build_memory_group(self, page: Adw.PreferencesPage):
        group = Adw.PreferencesGroup(title="Memory")
        page.add(group)

        # One state, one control (HIG): the expander's enable switch is keep-warm, and
        # "Unload after" nests beneath it because it matters only while that is on.
        expander = Adw.ExpanderRow(
            title="Keep model loaded",
            subtitle="Faster response, uses more memory",
            tooltip_text=(
                "Skips reloading the speech model for dictations that follow soon"
            ),
        )
        # Named by its title: an expander row has no name of its own, and GTK
        # would otherwise take its tooltip.
        expander.update_property([Gtk.AccessibleProperty.LABEL], [expander.get_title()])
        expander.set_show_enable_switch(True)
        self._config.bind("keep_warm", expander, "enable-expansion")
        group.add(expander)

        timeout_row = Adw.SpinRow.new_with_range(30, 1800, 30)
        timeout_row.set_title("Unload after")
        timeout_row.set_subtitle("Seconds of idle before freeing the model")
        # Its own tooltip, or it would show the expander's above.
        timeout_row.set_tooltip_text("The timer restarts after every dictation")
        self._config.bind("keep_warm_timeout", timeout_row, "value")
        expander.add_row(timeout_row)

    def _build_about_group(self, page: Adw.PreferencesPage):
        # Muted footer: the monochrome mark beside the name, the version dimmed below.
        group = Adw.PreferencesGroup()
        button = Gtk.Button(halign=Gtk.Align.CENTER)
        button.add_css_class("flat")

        stack = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL, spacing=2, halign=Gtk.Align.CENTER
        )
        title_row = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL, spacing=6, halign=Gtk.Align.CENTER
        )
        logo = Gtk.Image.new_from_icon_name(f"{APP_ID}-symbolic")
        logo.set_pixel_size(16)
        logo.add_css_class("dim-label")
        title = Gtk.Label(label="About Quoth")
        title.add_css_class("dim-label")
        title.add_css_class("caption")
        title_row.append(logo)
        title_row.append(title)

        version = Gtk.Label()
        version.add_css_class("dim-label")
        version.add_css_class("caption")
        self._version_label = version

        stack.append(title_row)
        stack.append(version)
        button.set_child(stack)
        button.connect("clicked", lambda *_: show_about_dialog(self))
        group.add(button)
        page.add(group)
        self._refresh_update_badge()

    def refresh_listener(self, state: ListenerState) -> None:
        """Show the native hotkey group, the shortcut group, or both to match `state`.

        While waiting, both show: the readable input may never send the key.
        """
        if self._recording_group is None:
            return
        self._recording_group.set_visible(state is not ListenerState.STOPPED)
        self._shortcut_group.set_visible(
            state in (ListenerState.STOPPED, ListenerState.WAITING)
        )
        descriptions = {
            ListenerState.WAITING: (
                "No keyboard Quoth can read has this key. Waiting for one to connect; "
                "a desktop shortcut needs a different key, or each press acts twice."
            ),
            ListenerState.FAILED: (
                "Keyboard capture stopped. Restart Quoth to reconnect."
            ),
        }
        self._recording_group.set_description(descriptions.get(state))

    def refresh_runtime(self, fallback: str | None) -> None:
        """Show the engine's runtime state; `fallback` notes a device that failed."""
        self._engine_section.refresh_runtime(fallback)

    def show_toast(self, message: str):
        """Show a transient plain-text toast in the settings window.

        Markup stays off because messages can quote names from imported files.
        """
        self.add_toast(Adw.Toast(title=message, use_markup=False))
        if (Adw.get_major_version(), Adw.get_minor_version()) < (1, 6):
            self.announce(message, Gtk.AccessibleAnnouncementPriority.MEDIUM)

    def _on_config_changed(self, name: str) -> None:
        """Show a changed setting on its row, whoever wrote it.

        Writers include this window, the tray, the app and `gsettings`; switch and
        number rows follow their key through a bind instead.
        """
        if name == "mode" and self._mode_row is not None:
            self._mode_row.set_selected(MODES.index(self._config.mode))
        elif name == "hotkey" and self._hotkey_row is not None:
            self._hotkey_row.set_subtitle(
                hotkey_capture.hotkey_label(self._config.hotkey)
            )
        elif name == "sound_feedback":
            self._sound_row.set_active(
                presentation.feedback_enabled(self._config.sound_feedback)
            )
        elif name == "notification_feedback":
            self._notification_row.set_active(
                presentation.feedback_enabled(self._config.notification_feedback)
            )
        self._engine_section.sync(name)
        if self._overlay_section is not None:
            self._overlay_section.sync(name)

    def _on_mode_changed(self, row, pspec):
        self._config.mode = MODES[row.get_selected()]

    def _on_sound_feedback_changed(self, row, pspec):
        self._set_feedback("sound_feedback", row.get_active())

    def _on_notification_feedback_changed(self, row, pspec):
        self._set_feedback("notification_feedback", row.get_active())

    def _set_feedback(self, name: str, active: bool) -> None:
        # A switch that shows what the setting already resolves to keeps "auto".
        if active != presentation.feedback_enabled(getattr(self._config, name)):
            setattr(self._config, name, _feedback_value(active))

    # --- update surfaces (driven by the app via UpdateBridge) ---

    def refresh_updates(self) -> None:
        """Show the current update state on its row and the About footer."""
        self._refresh_update_badge()
        row = self._update_status_row
        if row is None:
            return
        bridge = self._update_bridge
        available, checking = bridge.available(), bridge.checking()
        # Swap the shared slot to the spinner while a check runs (same width, no shift).
        self._update_check_stack.set_visible_child_name(
            "spinner" if checking else "button"
        )
        if checking:
            self._update_spinner.start()
        else:
            self._update_spinner.stop()
        self._update_view_btn.set_visible(bool(available))
        # updates.status_line decides the subtitle, precedence rule included.
        row.set_subtitle(
            updates.status_line(
                available,
                checking=checking,
                checked=bridge.checked(),
                failed=bridge.failed(),
            )
        )

    # --- Calibration (the hardware benchmark) ---

    def refresh_benchmark_status(self, benchmarked: bool) -> None:
        """Show the benchmark state on its row after the benchmark dialog closes."""
        self._engine_section.refresh_benchmark_status(benchmarked)

    def refresh_cache_status(self) -> None:
        """Refresh the NPU-cache summary after another view changes it."""
        self._engine_section.refresh_cache_status()

    def _refresh_update_badge(self) -> None:
        """Show or clear the update accent on the About footer's version."""
        label = self._version_label
        if label is None:
            return
        if self._update_bridge is not None and self._update_bridge.available():
            label.set_text(f"v{__version__} (update available)")
            label.remove_css_class("dim-label")
            label.add_css_class("accent")
        else:
            label.set_text(f"v{__version__}")
            label.remove_css_class("accent")
            label.add_css_class("dim-label")

    def _on_hotkey_row_activated(self, row):
        """Capture a shortcut in the shared GTK dialog, then apply its evdev binding.

        Search and the listener pause until the dialog closes: typeahead search's
        root key handler would swallow the first keystroke, and the listener reads
        evdev below GTK, so the chosen key would also fire the current hotkey.
        """
        if self._is_idle is not None and not self._is_idle():
            self.show_toast("Finish dictating before changing the shortcut")
            return
        self.set_search_enabled(False)
        if self._on_hotkey_capture:
            self._on_hotkey_capture(True)
        hotkey_capture.capture_hotkey(
            self, self._apply_hotkey, on_closed=self._on_capture_closed
        )

    def _on_capture_closed(self) -> None:
        """Restore search and the listener whichever way the capture dialog closes."""
        self.set_search_enabled(True)
        if self._on_hotkey_capture:
            self._on_hotkey_capture(False)

    def _apply_hotkey(self, name: str) -> None:
        """Persist the evdev binding `name` and toast the shortcut now in force.

        The app rebinds its listener on the write and restores the default if the key
        is rejected, so the toast reads the setting back.
        """
        self._config.hotkey = name
        label = hotkey_capture.hotkey_label(self._config.hotkey)
        self.show_toast(f"Dictation shortcut set to {label}")
