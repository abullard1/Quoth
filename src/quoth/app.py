# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 Samuel Ruairí Bullard

"""Quoth application: composition root.

Builds the modules, wires their callbacks together, and owns the GTK
application lifecycle. The recording state machine lives in
`RecordingController` (quoth/recording.py); this class translates listener and
tray events into controller calls and reflects controller state on the UI.

Phases: `do_handle_local_options` and `do_dbus_register` run in every process,
`do_startup` only in the primary. `_start` runs once, then onboarding until setup is
done, else `_finish_activation`, whose model load `_on_startup_context` completes.
`do_shutdown` stops the inputs, then the engine, then its outputs. Windows, CSS
providers and the download and update-check threads are left to process exit on
purpose.
"""

import functools
import os
import signal
import sys
import threading
from collections.abc import Callable
from datetime import date
from pathlib import Path

import gi
from loguru import logger

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("Gst", "1.0")

from gi.repository import Adw, Gdk, Gio, GLib, Gst, Gtk

import quoth
import quoth.log
from quoth import (
    APP_ID,
    SUPPORT_URL,
    __version__,
    autostart,
    brand,
    crash_reports,
    environment,
    ipc,
    paths,
    presentation,
    skin,
    stats,
    updates,
)
from quoth.about import (
    ISSUES_URL,
    SCRIPTING_URL,
    TROUBLESHOOTING_URL,
    open_uri,
    show_about_dialog,
)
from quoth.atomic_write import diagnostic
from quoth.benchmark_dialog import BenchmarkDialog
from quoth.calibration import DoneFn, ProgressFn, Report, preparation_note
from quoth.config import Config
from quoth.downloads import DownloadCoordinator
from quoth.engine_section import CalibrationBridge, fallback_note
from quoth.hardware import available_devices, fingerprint
from quoth.keynames import format_key
from quoth.languages import LANGUAGES, default_from_locale
from quoth.listener import KeyListener, ListenerState, evdev_available
from quoth.models import AVAILABLE_MODELS, ModelManager, model_label
from quoth.motion import ReducedMotionWatcher
from quoth.onboarding import OnboardingAssistant
from quoth.overlay import WaveformPill
from quoth.overlay_section import ThemeBridge
from quoth.portal_shortcuts import PortalShortcuts
from quoth.prepare import PrepareGate
from quoth.recording import (
    ContextLoadOutcome,
    ContextStatus,
    RecordingCallbacks,
    RecordingController,
    RecordingState,
    model_error,
)
from quoth.resolution import (
    current_ranking,
    evict_warmth,
    expects_slow_compile,
    provision_for,
    resolve_for,
    worth_offering,
)
from quoth.scheduler import GLibScheduler
from quoth.settings import ModelSwitcher
from quoth.settings_window import SettingsWindow, UpdateBridge, UsageInfo
from quoth.skin_runtime import SkinRuntime
from quoth.skin_section import SkinBridge
from quoth.sound import SoundPlayer
from quoth.theme import (
    DEFAULT_THEME,
    apply_theme_id,
    identify_theme,
    import_user_theme,
    list_presets,
    list_user_themes,
    read_theme,
    theme_from_id,
    user_theme_dir,
)
from quoth.tray import QuothTray, TrayMenu
from quoth.trigger import PUSH_TO_HOLD, TAP_TO_TOGGLE, hotkey_action
from quoth.typer import (
    InjectionResult,
    copy_text,
    init_typer,
    prewarm_typer,
    run_uinput_setup,
    shutdown_typer,
)
from quoth.typing_setup import TypingSetupDialog
from quoth.update_notifier import UpdateNotifier

Gst.init(None)

# What a failed paste tells the user, as (detail, pill guidance). Every other result
# leaves the transcript on the clipboard.
_PASTE_FAILURES = {
    InjectionResult.NO_WAYLAND: (
        "Quoth pastes only in a Wayland session, and it can't reach one here, so it "
        "can't paste or copy dictations.",
        "Needs a Wayland session",
    ),
    InjectionResult.NO_WL_CLIPBOARD: (
        "wl-clipboard isn't installed, so dictations can't be pasted or copied. Install "
        "it, then restart Quoth.",
        "Install wl-clipboard",
    ),
    InjectionResult.NO_CHORD_SENDER: (
        "Quoth found no wtype or ydotool it can use, so it can't send the paste key. "
        "Dictations are left on your clipboard instead, replacing what was there.",
        "Install wtype or ydotool",
    ),
    InjectionResult.FAILED: (
        "Couldn't paste the transcription or copy it to the clipboard. Dictate again, "
        "and if it keeps failing, open Troubleshooting.",
        "Try dictating again",
    ),
}
_PASTE_ON_CLIPBOARD = (
    "Couldn't send the paste key. The text is on your clipboard (replacing your previous "
    "clipboard).",
    "Text is on your clipboard",
)
# The notice that the hotkey toggles because the keyboard reports no release.
_UNHOLDABLE_NOTICE = "quoth-hotkey-hold"
# The script commands, as `--name` options with their help text.
_SCRIPT_COMMANDS = {
    "start": "Start dictation",
    "stop": "Stop dictation and paste the text",
    "cancel": "Discard the dictation in progress",
    "status": "Print the dictation state: idle, recording or transcribing",
}


class QuothApp(Adw.Application):
    """The Adw application that builds the modules and wires their callbacks."""

    def __init__(self):
        super().__init__(
            application_id=APP_ID,
            flags=Gio.ApplicationFlags.HANDLES_COMMAND_LINE,
        )
        # CLI options. GApplication adds --version and prints it before registering.
        self.set_version(__version__)
        self.set_option_context_summary(
            "Offline voice dictation: press your shortcut, speak, and the text appears."
        )
        self.set_option_context_description(
            "--start, --stop, --cancel and --status never start Quoth. They exit with 0 "
            "on success; with 1 and the reason when it refuses, does not answer within "
            "25 seconds, or the command line is wrong; and with 2 when it is not "
            "running or there is no session bus.\n\n"
            "Examples:\n"
            "  quoth --toggle      Bind this to a desktop shortcut\n"
            "  quoth --background  Add this to your startup programs\n"
            "  quoth --status      Print idle, recording or transcribing\n\n"
            f"Scripting and settings: {SCRIPTING_URL}\n"
            f"Report bugs at {ISSUES_URL}"
        )
        self.add_main_option(
            "settings",
            ord("s"),
            GLib.OptionFlags.NONE,
            GLib.OptionArg.NONE,
            "Open Settings",
            None,
        )
        self.add_main_option(
            "quit",
            ord("q"),
            GLib.OptionFlags.NONE,
            GLib.OptionArg.NONE,
            "Quit Quoth",
            None,
        )
        self.add_main_option(
            "toggle",
            ord("t"),
            GLib.OptionFlags.NONE,
            GLib.OptionArg.NONE,
            "Start or stop dictation, or start Quoth if it is not running",
            None,
        )
        # `cli` runs a lone script command without loading the app; listing them here
        # puts them in --help and refuses them beside other options.
        for name, description in _SCRIPT_COMMANDS.items():
            self.add_main_option(
                name, 0, GLib.OptionFlags.NONE, GLib.OptionArg.NONE, description, None
            )
        self.add_main_option(
            "background",
            0,
            GLib.OptionFlags.NONE,
            GLib.OptionArg.NONE,
            "Start without opening a window, as at login",
            None,
        )
        self._dictation_service: ipc.DictationService | None = None

    def do_handle_local_options(self, options):
        """Open the settings first in every process, so a missing schema fails clearly.

        A missing schema exits with status 1, as does a script command beside other
        options.
        """
        for name in _SCRIPT_COMMANDS:
            if options.contains(name):
                print(f"quoth: --{name} takes no other options", file=sys.stderr)
                return 1
        try:
            self._config = Config()
        except RuntimeError as e:
            logger.critical("{}", e)
            return 1
        return -1

    def do_dbus_register(self, connection, object_path):
        """Export the script interface beside GApplication's own.

        It runs before the name is owned, so every instance registers; one that is not
        primary unregisters again.
        """
        self._dictation_service = ipc.DictationService(
            connection,
            state=lambda: self._script_state().value,
            start=self._script_start,
            stop=self._script_stop,
            cancel=self._script_cancel,
        )
        return Adw.Application.do_dbus_register(self, connection, object_path)

    def do_dbus_unregister(self, connection, object_path):
        self._dictation_service.close()
        self._dictation_service = None
        Adw.Application.do_dbus_unregister(self, connection, object_path)

    def do_startup(self):
        """Build the app's modules; only the primary instance runs this."""
        quoth.log.install_file_log()
        quoth.log.install_crash_hooks()
        # Consent applies before the modules are built, so it covers a startup failure.
        crash_reports.set_enabled(self._config.crash_reports)
        crash_reports.report_native_crash()
        # A logout or kill quits through do_shutdown, as Quit and Ctrl+C do; a signal
        # ignored at launch, as under nohup, stays ignored.
        self._signal_sources = [
            GLib.unix_signal_add(
                GLib.PRIORITY_DEFAULT, signum, self._quit_on_signal, signum
            )
            for signum in (signal.SIGTERM, signal.SIGHUP)
            if signal.getsignal(signum) is not signal.SIG_IGN
        ]
        Adw.Application.do_startup(self)
        self._model_mgr = ModelManager()
        # Stateless, so every service shares one.
        self._scheduler = GLibScheduler()
        # The one owner of model downloads, so a model is never fetched twice at once.
        self._downloads = DownloadCoordinator(
            self._model_mgr.models_dir, self._scheduler
        )
        # The one pending model or device change: it downloads, applies and announces
        # itself whether or not a Settings window is open.
        self._model_switcher = ModelSwitcher(
            self._config,
            self._model_mgr,
            self._downloads,
            reconcile=self._reconcile_engine,
            notify=lambda text: self._notice("quoth-model", "Quoth", text),
        )
        self._controller: RecordingController | None = None
        self._listener: KeyListener | None = None
        self._shutting_down = False
        # Devices whose fallback was announced; once per launch is enough.
        self._noted_fallbacks: set[str] = set()
        self._portal_shortcuts: PortalShortcuts | None = None
        self._pill: WaveformPill | None = None
        self._settings_window: SettingsWindow | None = None
        self._benchmark_dialog: BenchmarkDialog | None = None
        self._typing_setup_shown = False
        # Paste failures already explained in full; later ones get the short notice.
        self._paste_failures_explained: set[InjectionResult] = set()
        self._onboarding: OnboardingAssistant | None = None
        # Set once activation starts input, which outlasts onboarding's window.
        self._input_live = False
        self._device = "CPU"
        # The cold OpenVINO encoder compile as a gate on recording (prepare.py);
        # built with the controller, since it drives the async context load.
        self._gate: PrepareGate | None = None
        self._tray: QuothTray | None = None
        self._sound = SoundPlayer()
        self._theme_path = paths.config_dir() / "theme.toml"
        self._user_theme_dir = user_theme_dir(paths.config_dir())
        self._theme = DEFAULT_THEME
        self._theme_load_error: str | None = None
        self._theme_monitor: Gio.FileMonitor | None = None
        self._theme_target_monitor: Gio.FileMonitor | None = None
        self._theme_target_path: Path | None = None
        self._theme_reload_source = 0
        self._skins = SkinRuntime(
            paths.config_dir(),
            self._scheduler,
            render=brand.apply_skin,
            on_changed=self._refresh_skin_view,
        )
        self._user_css_provider: Gtk.CssProvider | None = None
        self._motion_watcher: ReducedMotionWatcher | None = None
        self._motion_enabled = True
        # Opt-in update checks; UpdateNotifier owns their state and throttle.
        self._updates = UpdateNotifier(
            __version__,
            updates.default_path(),
            is_enabled=lambda: self._config.notify_updates,
            push=self._notify_update,
            refresh=self._refresh_update_ui,
            scheduler=self._scheduler,
        )
        # Local usage stats: the dictation count behind the support nudge and the
        # Settings usage summary.
        self._stats_path = stats.default_path()
        self._stats = stats.load(self._stats_path)
        # The first run starts from the system language; after that it is the user's.
        self._config.seed("language", default_from_locale())
        _log_native_discovery()
        for name, activate in (
            # The hotkey's action. `quoth --toggle` reaches it over D-Bus (see ipc.py)
            # or, failing that, through do_command_line; both route the same way.
            ("toggle", lambda *_: self._request_toggle()),
            # Exported parameterless over D-Bus, as GNOME's app menu and Background Apps
            # require to quit through it; otherwise they close windows or kill Quoth.
            ("quit", lambda *_: self.quit()),
            # The rest are behind notification buttons.
            ("view-release", lambda *_: self._open_release_page()),
            ("run-benchmark", self._on_run_benchmark),
            ("support", self._on_support),
            ("troubleshooting", lambda *_: open_uri(TROUBLESHOOTING_URL)),
        ):
            action = Gio.SimpleAction.new(name, None)
            action.connect("activate", activate)
            self.add_action(action)

    def do_command_line(self, command_line):
        """Handle CLI arguments in the primary instance.

        The return value is the exit status of the process that ran the command.
        """
        extra = command_line.get_arguments()[1:]
        if extra:
            command_line.printerr_literal(f"quoth: unexpected argument {extra[0]}\n")
            return 1
        options = command_line.get_options_dict()

        if options.contains("quit"):
            logger.debug("Quit requested from the command line")
            self.quit()
            return 0

        if options.contains("toggle"):
            self._request_toggle()
            return 0

        # The login start opens no window: nobody at the desktop asked for one.
        if options.contains("background"):
            self._start()
            return 0

        if options.contains("settings"):
            self._start()
            self._show_settings()
            return 0

        self.activate()
        return 0

    def _bundled_icon_dir(self) -> Path | None:
        """Return the source tree's icon theme root, `data/icons`; None when installed.

        GTK's icon search path and the tray's IconThemePath both come from here.
        """
        icons_dir = Path(__file__).resolve().parents[2] / "data" / "icons"
        return icons_dir if icons_dir.is_dir() else None

    def _register_icons(self):
        """Let a source run's GTK find the app icon by name (About, first-launch page).

        Installed packages put it in the system theme; the dock icon comes from the
        installed .desktop either way.
        """
        icons_dir = self._bundled_icon_dir()
        display = Gdk.Display.get_default()
        if display and icons_dir is not None:
            Gtk.IconTheme.get_for_display(display).add_search_path(str(icons_dir))

    def do_activate(self):
        # Activation shows the open window (onboarding during setup), else Settings,
        # which shows Quoth is running even on a desktop without a tray.
        self._start()
        window = self.get_active_window()
        if window is not None:
            window.present()
        else:
            self._show_settings()

    def _start(self):
        """Bring Quoth up once: onboarding until setup is done, then live input.

        Beyond onboarding and the one-time typing setup it opens no window; the caller
        decides what to show.
        """
        if self._pill is not None:
            return
        logger.info("Quoth {} starting", __version__)
        logger.debug(
            "Desktop: {}; Wayland: {}",
            ":".join(environment.desktop_names()) or "unknown",
            environment.wayland_session(),
        )
        self._register_icons()
        self._reload_theme()
        self._motion_watcher = ReducedMotionWatcher(self._on_reduced_motion)
        self._motion_enabled = not self._motion_watcher.reduced
        brand.install(motion_enabled=self._motion_enabled)
        self._skins.start()  # restyles the provider with the active skin
        self._load_user_css()
        self._pill = WaveformPill(self._theme, style=self._config.overlay_style)
        self._pill.set_on_cancel(self._cancel)
        self._pill.set_motion_enabled(self._motion_enabled)
        self._watch_theme()

        self._apply_effective_device()
        logger.info(
            "Hardware device: {} (device preference: {})",
            self._device,
            self._config.device,
        )
        # The one engine, before onboarding, whose guided take and benchmark run on it.
        self._controller = RecordingController(
            self._config,
            self._model_mgr,
            self._device,
            self._scheduler,
            RecordingCallbacks(
                on_state_change=self._on_state_change,
                on_level=self._on_audio_level,
                on_error=self._show_error,
                on_typing_failed=self._on_typing_failed,
                on_no_input=self._on_no_input,
                on_dictation_success=self._on_dictation_success,
                on_context_changed=self._on_context_changed,
            ),
        )
        self._gate = PrepareGate(
            self._controller,
            notify=self._notify_preparing,
            withdraw=lambda: self.withdraw_notification("quoth-preparing"),
        )
        # The Flatpak's hotkey source, once the engine its presses drive exists.
        if environment.in_flatpak():
            self._portal_shortcuts = PortalShortcuts(
                lambda: self._on_hotkey(True),
                lambda: self._on_hotkey(False),
                self._on_portal_lost,
            )
            self._portal_shortcuts.subscribe(self._on_portal_status)
            self._portal_shortcuts.start(restore=self._config.portal_shortcut_enabled)
        # Once onboarding has written the autostart entry, that entry is the record: the
        # desktop's own controls may have switched it off. A Flatpak's is out of sight.
        if self._config.onboarding_complete and not environment.in_flatpak():
            entry_enabled = autostart.is_enabled()
            if entry_enabled != self._config.launch_at_login:
                logger.info(
                    "Launch at login set to {}: the autostart entry is {}",
                    entry_enabled,
                    "enabled" if entry_enabled else "missing, hidden or disabled",
                )
            self._config.launch_at_login = entry_enabled
        self._config.connect_changed(self._on_setting_changed)

        # Run onboarding until it has completed once, and again if every model was
        # deleted. QUOTH_FORCE_ONBOARDING=1 replays it every launch.
        if (
            os.environ.get("QUOTH_FORCE_ONBOARDING") == "1"
            or not self._config.onboarding_complete
            or not self._model_mgr.list_downloaded()
        ):
            self._show_onboarding()
            return

        self._finish_activation()

    def _show_onboarding(self):
        """Show the first-run onboarding assistant; activation finishes when done."""

        def on_complete(on_ready):
            # Setup's choices are fixed now: input and Settings stop waiting for it, so
            # a press during the model load dictates and `on_ready` can open Settings.
            self._onboarding = None
            autostart.set_enabled(
                self._config.launch_at_login, self._on_autostart_declined
            )

            def activate():
                try:
                    self._finish_activation(on_ready=on_ready)
                finally:
                    self.release()

            # A skipped benchmark's last measurement leaves the engine first; its
            # completion has re-resolved the device, so activation loads the winner.
            # Until then no window or tray may be left, so the hold keeps Quoth up.
            self.hold()
            logger.debug("Onboarding done; activating once the engine is idle")
            self._controller.when_idle(activate)

        self._onboarding = OnboardingAssistant(
            app=self,
            model_mgr=self._model_mgr,
            config=self._config,
            downloads=self._downloads,
            devices=available_devices(),
            on_complete=on_complete,
            on_open_settings=self._show_settings,
            engine=self._controller,
            start_benchmark=self._start_benchmark,
            portal_shortcuts=self._portal_shortcuts,
            motion_enabled=self._motion_enabled,
        )

    def _finish_activation(self, on_ready=None):
        """Go live: audio, tray, typer and input, then load the whisper model async.

        Activation completes after the preload, including a recoverable load failure, so
        Settings and shortcut-driven retries remain available.
        """
        self._controller.prepare_audio()
        # Pre-map the pill surface and, with sound on, preroll the earcons now, so the
        # first hotkey press pays no first-show or cold-playbin latency.
        self._pill.prewarm()
        if presentation.feedback_enabled(self._config.sound_feedback):
            self._sound.prewarm()
        # Said once here, before a first dictation fails for the same reason.
        if (unavailable := init_typer()) is not None:
            self._on_typing_failed(unavailable, at_startup=True)
        else:
            # Start ydotoold now: its device setup would delay the first paste a second.
            prewarm_typer()
        icon_dir = self._bundled_icon_dir()
        self._tray = QuothTray(
            TrayMenu(
                languages=LANGUAGES,
                get_language=lambda: self._config.language,
                set_language=self._change_language,
                list_models=self._downloaded_models,
                get_model=lambda: self._config.model,
                set_model=self._change_model,
                on_settings=self._show_settings,
                on_about=self._show_about,
                on_quit=self.quit,
                update_available=lambda: self._updates.available,
                view_release=self._open_release_page,
                last_dictation_available=lambda: self._controller.last_text is not None,
                copy_last_dictation=self._copy_last_dictation,
            ),
            icon_theme_path=str(icon_dir) if icon_dir else "",
        )
        self._tray.start()
        self._tray.set_animated(self._motion_enabled)
        self.hold()

        # Input is live from here: a press during the load records and transcribes once
        # the model is ready; a startup NPU compile's gate refuses it with a notice.
        startup_model, startup_device = self._config.model, self._device
        # A download onboarding left running becomes the app's pending change, so its
        # landing is applied and announced.
        if self._model_switcher.pending is None and self._downloads.is_downloading(
            startup_model
        ):
            self._model_switcher.request(
                "model",
                startup_model,
                startup_model,
                provision_for(startup_model, self._config.device, self._model_mgr),
            )
        if expects_slow_compile(startup_model, startup_device):
            self._gate.raise_for(startup_model, startup_device)
        self._input_live = True
        self._start_listener()
        self._controller.ensure_context_async(
            startup_model,
            startup_device,
            functools.partial(self._on_startup_context, on_ready),
        )

    def _on_startup_context(
        self,
        on_ready: Callable[[], None] | None,
        model: str,
        device: str,
        outcome: ContextLoadOutcome,
    ) -> None:
        """Complete activation once the startup load ends, loaded or not.

        A model or device changed meanwhile is loaded first; `on_ready` is onboarding's.
        """
        if self._shutting_down:
            return
        self._gate.finish(model, device, outcome)
        current = (self._config.model, self._device)
        if (model, device) != current:
            self._controller.ensure_context_async(
                *current, functools.partial(self._on_startup_context, on_ready)
            )
            return
        if outcome is ContextLoadOutcome.FAILED:
            # Surface startup load failures before the first dictation.
            self._show_error(*model_error(downloaded=True))
        elif outcome is ContextLoadOutcome.MISSING:
            if self._model_switcher.pending is not None:
                self._notify_downloading()
            # A transfer that landed after the load looked has already applied.
            elif not self._model_mgr.is_downloaded(model):
                self._show_error(*model_error(downloaded=False))
        if on_ready:
            on_ready()
        self._log_ready(model, device, outcome)
        self._updates.maybe_check()
        self._maybe_offer_calibration()
        self._maybe_nudge_support()

    def _log_ready(self, model: str, device: str, outcome: ContextLoadOutcome) -> None:
        """Log that startup is done, and how to dictate or why no model is ready."""
        if not outcome.loaded:
            logger.warning(
                "Quoth started without a ready model (model={}, device={}, outcome={})",
                model,
                device,
                outcome.name,
            )
        elif self._portal_shortcuts is not None:
            logger.success(
                "Quoth ready ({}): desktop shortcut {} ({})",
                self._device,
                self._portal_shortcuts.binding,
                self._portal_shortcuts.status,
            )
        elif (
            self._listener is not None
            and self._listener.state is ListenerState.AVAILABLE
        ):
            logger.success(
                "Quoth ready: press {} to dictate ({} mode, {})",
                self._config.hotkey,
                self._config.mode,
                self._device,
            )
        else:
            logger.success(
                "Quoth ready ({}): dictate with `quoth --toggle` (bind it to a key)",
                self._device,
            )

    def _start_listener(self):
        """Start native key capture; Flatpak's shortcut session has its own owner."""
        if (
            self._shutting_down
            or self._portal_shortcuts is not None
            or self._listener is not None
        ):
            return
        if not evdev_available():
            logger.warning(
                "evdev hotkey listener unavailable; dictate with `quoth --toggle`"
            )
            return
        try:
            candidate = KeyListener(
                hotkey=self._config.hotkey,
                # PRIORITY_HIGH so a press is handled ahead of the next frame redraw
                # (default idle dispatch sits below GTK's redraw priority).
                on_press=lambda mode=None: GLib.idle_add(
                    self._on_hotkey, True, mode, priority=GLib.PRIORITY_HIGH
                ),
                on_release=lambda: GLib.idle_add(
                    self._on_hotkey, False, priority=GLib.PRIORITY_HIGH
                ),
                on_state_change=lambda state: GLib.idle_add(
                    self._on_listener_state, state
                ),
                on_unholdable=lambda marked: GLib.idle_add(
                    self._on_unholdable_hotkey, marked
                ),
            )
            if candidate.start():
                self._listener = candidate
            else:
                logger.warning("No accessible keyboard device; check keyboard access")
            self._refresh_listener_ui()
        except ValueError:
            self._reject_hotkey()
        except Exception:
            logger.exception("Failed to start key listener")
            self._refresh_listener_ui()
            self._toast_or_notice(
                "quoth-keyboard",
                "Keyboard capture couldn't start. Restart Quoth to try again.",
            )

    def _on_unholdable_hotkey(self, marked: bool) -> bool:
        """Notice once per marking in push-to-hold; withdraw it once none is marked.

        A notification, not a toast: the key is pressed over another app's window.
        """
        if self._shutting_down or self._listener is None:
            return False
        # A rebind may have cleared the mark since it was reported.
        unholdable = self._listener.unholdable
        if marked and unholdable and self._config.mode == PUSH_TO_HOLD:
            self._notice_unholdable_hotkey()
        elif not marked and not unholdable:
            self.withdraw_notification(_UNHOLDABLE_NOTICE)
        return False

    def _notice_unholdable_hotkey(self) -> None:
        self._notice(
            _UNHOLDABLE_NOTICE,
            "Quoth",
            "This key can't be held on this keyboard, so it toggles: press it once "
            "to start dictating and again to stop.",
        )

    def _reject_hotkey(self) -> None:
        """Fall back to the default key. Writing it applies it, as any hotkey change."""
        fallback = self._config.default("hotkey")
        logger.warning(
            "Invalid hotkey '{}', falling back to {}", self._config.hotkey, fallback
        )
        self._toast_or_notice(
            "quoth-keyboard",
            f"The saved dictation key isn't recognized. Using {format_key(fallback)} "
            "instead.",
        )
        self._config.hotkey = fallback

    def _on_listener_state(self, state) -> bool:
        if self._shutting_down or self._listener.state is not state:
            return False
        if state is ListenerState.FAILED:
            # The hotkey can no longer end this dictation.
            if self._controller.state is RecordingState.RECORDING:
                self._controller.stop()
            self._toast_or_notice(
                "quoth-keyboard",
                "Keyboard capture stopped. Restart Quoth to reconnect.",
            )
        self._refresh_listener_ui()
        return False

    def _refresh_listener_ui(self) -> None:
        if self._settings_window is not None:
            self._settings_window.refresh_listener(
                self._listener.state if self._listener else ListenerState.STOPPED
            )

    # --- Recording bridges (listener/overlay -> controller) ---

    def _on_portal_status(self):
        status = self._portal_shortcuts.status
        if status == "error":
            # A shortcut that could not be restored leaves the hotkey dead; Settings and
            # onboarding show that themselves.
            if (
                self._config.portal_shortcut_enabled
                and self._settings_window is None
                and self._onboarding is None
            ):
                self._notify_shortcut_setup()
            return
        if status not in {"ready", "needs-key", "denied", "closed", "unbound"}:
            return
        # The desktop keeps a shortcut it accepted without a key, and binding a known
        # shortcut again opens no dialog, so launch re-binds it too.
        enabled = status in {"ready", "needs-key"}
        if self._config.portal_shortcut_enabled != enabled:
            self._config.portal_shortcut_enabled = enabled

    def _on_portal_lost(self):
        # Onboarding shows the session's state itself.
        if self._shutting_down or self._onboarding is not None:
            return
        # The session will not report the release, so end a held dictation as it would.
        self._on_hotkey(False)
        if self._portal_shortcuts.status in {"closed", "unbound"}:
            self._notify_shortcut_setup()

    def _notify_shortcut_setup(self) -> None:
        self._notice(
            "quoth-shortcut",
            "Shortcut needs setup",
            "Set up your dictation shortcut again in Settings.",
        )

    def _on_hotkey(self, pressed: bool, mode: str | None = None) -> None:
        """Act on a press or release from a hotkey, `--toggle` or a script command.

        `--toggle` and scripts tap in any mode. While onboarding a press confirms the
        shortcut, and while transcribing it cancels; otherwise `hotkey_action` decides.
        """
        if self._shutting_down:
            return
        if self._onboarding is not None:
            if pressed:
                logger.debug("Hotkey press: onboarding's shortcut check")
                self._onboarding.notify_toggle_detected()
            return
        state = self._controller.state
        if state is RecordingState.TRANSCRIBING:
            if pressed:
                logger.debug("Hotkey press while transcribing: cancelling")
                self._cancel()
            return
        mode = mode or self._config.mode
        action = hotkey_action(mode, state is RecordingState.RECORDING, pressed)
        logger.debug(
            "Hotkey {}: state={} mode={} -> {}",
            "press" if pressed else "release",
            state.value,
            mode,
            action,
        )
        if action == "stop":
            self._controller.stop()
        elif action == "start" and not self._refuse_start():
            self._controller.start()

    def _refuse_start(self) -> bool:
        """Whether a start must wait for a benchmark, NPU compile or download.

        It tells the user why.
        """
        if not self._controller.is_idle:
            logger.info("Dictation refused: the engine is busy with the benchmark")
            self._notice(
                "quoth-benchmark",
                "Quoth",
                "Quoth is busy with the benchmark. Dictate once it finishes.",
            )
            return True
        if self._gate.refuse_start():
            return True
        if self._model_switcher.pending is None or self._controller.model_available():
            return False
        logger.info("Dictation refused: the model is still downloading")
        self._notify_downloading()
        return True

    def _request_toggle(self) -> None:
        """Handle a toggle from outside, such as a keybinding or `gapplication action`.

        Once Quoth has started it is a hotkey tap; before that, it is what starts Quoth.
        Runs on the main thread either way.
        """
        if self._controller is None:
            # Started quietly: a window would take focus from the field to dictate into.
            logger.debug("IPC: toggle before start, starting")
            self._start()
        else:
            logger.debug("IPC: toggle received")
            self._on_hotkey(True, TAP_TO_TOGGLE)

    def _script_state(self) -> RecordingState:
        """Return the state scripts see.

        An instance run with `--gapplication-service` is on the bus before it starts.
        """
        return self._controller.state if self._controller else RecordingState.IDLE

    def _script_start(self) -> str | None:
        """Start a dictation for `quoth --start`, as a tap from idle, or say why not.

        A script never confirms onboarding's shortcut step, and already recording is
        success.
        """
        logger.debug("IPC: start received")
        if self._controller is None:
            return "Quoth has not started yet"
        if self._onboarding is not None:
            return "Finish setting up Quoth first"
        state = self._controller.state
        if state is RecordingState.TRANSCRIBING:
            return "Quoth is still transcribing the last dictation"
        if state is RecordingState.IDLE:
            self._on_hotkey(True, TAP_TO_TOGGLE)
        if self._controller.state is not RecordingState.RECORDING:
            return "Dictation did not start; Quoth shows why on screen"
        return None

    def _script_stop(self) -> None:
        """Stop a dictation for `quoth --stop`, as a tap; otherwise change nothing."""
        logger.debug("IPC: stop received")
        if (
            self._onboarding is None
            and self._script_state() is RecordingState.RECORDING
        ):
            self._on_hotkey(True, TAP_TO_TOGGLE)

    def _script_cancel(self) -> None:
        """Discard a dictation for `quoth --cancel`, like a click on the pill."""
        logger.debug("IPC: cancel received")
        if self._onboarding is None and self._script_state() is not RecordingState.IDLE:
            self._cancel()

    def _on_autostart_declined(self, unsupported: bool = False) -> None:
        """Turn the setting back off when the desktop declines launch at login."""
        if not self._config.launch_at_login:
            return
        self._config.launch_at_login = False
        if unsupported:
            title = "Launch at login isn't available"
            body = "Add Quoth to your desktop's startup programs instead."
        else:
            title = "Launch at login was not allowed"
            body = "You can allow it in your desktop's application settings."
        self._notice("quoth-autostart", title, body, low=True)

    def _cancel(self):
        """Cancel recording or transcription, from the shortcut or an overlay click."""
        self._controller.cancel()

    def _on_audio_level(self, amplitude: float) -> None:
        """Send one microphone level to whoever is showing it.

        The pill gets it only while recording: Settings and onboarding meter the input
        too, and their levels would animate the pill with no recording.
        """
        if self._controller.state is RecordingState.RECORDING:
            self._pill.push_amplitude(amplitude)
        if self._settings_window is not None:
            self._settings_window.push_level(amplitude)
        if self._onboarding is not None:
            self._onboarding.push_level(amplitude)

    def _on_state_change(self, state: RecordingState):
        """Reflect controller state on the overlay pill and tray icon.

        It also fires the optional sound and notification feedback, meant for users
        without the pill.
        """
        if state is RecordingState.RECORDING:
            self._pill.show_recording()
            self._feedback(sound="start", notify="Recording…")
        elif state is RecordingState.TRANSCRIBING:
            self._pill.show_transcribing()
            self._feedback(sound="stop", notify="Transcribing…")
        else:
            self._pill.hide_pill()
            # The pasted text is the completion signal.
            self.withdraw_notification("quoth-status")
        if self._tray:
            self._tray.set_state(state.value)
        if self._dictation_service is not None:
            self._dictation_service.publish(state.value)

    # --- Recording feedback (sound + self-cleaning notification) ---

    def _feedback(self, *, sound: str, notify: str):
        """Fire the per-channel recording cues, each gated by its tri-state setting.

        Each setting resolves against overlay availability: `auto` means on only where
        there is no pill.
        """
        if presentation.feedback_enabled(self._config.sound_feedback):
            self._sound.play(sound, self._config.sound_volume)
        if presentation.feedback_enabled(self._config.notification_feedback):
            self._notify_status(notify)

    def _notice(
        self,
        notification_id: str,
        title: str,
        body: str | None = None,
        *,
        low: bool = False,
        button: tuple[str, str] | None = None,
    ) -> None:
        """Send a desktop notification; every one goes through here.

        A fixed id means a repeat replaces the last. `low` marks ambient status, and
        `button` adds one (label, action) button.
        """
        notification = Gio.Notification.new(title)
        if body:
            notification.set_body(body)
        if low:
            notification.set_priority(Gio.NotificationPriority.LOW)
        if button:
            notification.add_button(*button)
        logger.debug("Notification {}: {}", notification_id, title)
        self.send_notification(notification_id, notification)

    def _notify_status(self, body: str, *, low: bool = True):
        """Show the one status notification; a new status replaces the last.

        Status is ambient, so low priority, unless the user must act on it: GNOME Shell
        shows no banner for a low-priority notification.
        """
        self._notice("quoth-status", "Quoth", body, low=low)

    # --- Update notifier (opt-in) ---

    def _refresh_update_ui(self) -> None:
        """Mirror the notifier's state onto Settings and the tray."""
        if self._settings_window:
            self._settings_window.refresh_updates()
        if self._tray and not self._updates.is_checking:
            # Nudge hosts that cache the menu to re-fetch its "Update available" item.
            self._tray.refresh_menu()

    def _notify_update(self, version: str) -> None:
        """Announce an available update in a desktop notification; never install it."""
        self._notice(
            "quoth-update",
            f"Quoth {version} is available",
            updates.update_hint(),
            button=("View release", "app.view-release"),
        )

    def _copy_last_dictation(self) -> None:
        text = self._controller.last_text

        def copy() -> None:
            # A silent failure would leave the previous clipboard for the next paste.
            if not copy_text(text):
                GLib.idle_add(
                    self._show_error, "Copy failed", "Couldn't copy the last dictation."
                )

        # wl-copy may host-spawn under Flatpak; keep it off the main thread.
        threading.Thread(target=copy, name="copy-last", daemon=True).start()

    def _open_release_page(self) -> None:
        """Open the GitHub releases page, the one update path every install has."""
        open_uri(updates.RELEASES_URL)

    # --- Appearance (overlay theme, app skin, style.css, motion) ---

    def _watch_theme(self):
        """Hot-reload theme.toml in the config directory while the app runs."""
        try:
            self._theme_path.parent.mkdir(parents=True, exist_ok=True)
            gfile = Gio.File.new_for_path(str(self._theme_path))
            # The reference must stay on self: unreferenced monitors are
            # silently garbage-collected and stop delivering events.
            self._theme_monitor = gfile.monitor_file(Gio.FileMonitorFlags.NONE, None)
            self._theme_monitor.connect(
                "changed", self._on_theme_file_changed, self._theme_path
            )
        except (GLib.Error, OSError) as e:
            logger.warning("Theme hot-reload unavailable: {}", e)
        self._sync_theme_target_monitor()

    def _sync_theme_target_monitor(self) -> None:
        # A name monitor on a symlink does not observe atomic saves to its target.
        try:
            target = self._theme_path.resolve()
        except (OSError, RuntimeError) as e:
            logger.debug(
                "theme.toml link unresolvable ({}); not watching its target", e
            )
            target = None
        if target == self._theme_path:
            target = None
        if target == self._theme_target_path:
            return
        if self._theme_target_monitor is not None:
            self._theme_target_monitor.cancel()
            self._theme_target_monitor = None
        self._theme_target_path = target
        if target is None:
            return
        try:
            self._theme_target_monitor = Gio.File.new_for_path(
                str(target)
            ).monitor_file(Gio.FileMonitorFlags.NONE, None)
            self._theme_target_monitor.connect(
                "changed", self._on_theme_file_changed, target
            )
        except GLib.Error as e:
            logger.warning("Linked theme hot-reload unavailable: {}", e.message)

    def _unwatch_theme(self) -> None:
        """Stop the hot-reload `_watch_theme` started, and any reload still pending."""
        if self._theme_monitor:
            self._theme_monitor.cancel()
            self._theme_monitor = None
        if self._theme_target_monitor:
            self._theme_target_monitor.cancel()
            self._theme_target_monitor = None
        if self._theme_reload_source:
            GLib.source_remove(self._theme_reload_source)
            self._theme_reload_source = 0

    def _on_theme_file_changed(self, monitor, gfile, other_file, event, watched_path):
        if not any(
            file is not None and file.get_path() == str(watched_path)
            for file in (gfile, other_file)
        ):
            return
        if event not in (
            Gio.FileMonitorEvent.CHANGES_DONE_HINT,
            Gio.FileMonitorEvent.DELETED,
            Gio.FileMonitorEvent.RENAMED,
            Gio.FileMonitorEvent.MOVED_IN,
            Gio.FileMonitorEvent.MOVED_OUT,
        ):
            return
        if not self._theme_reload_source:
            self._theme_reload_source = GLib.idle_add(self._reload_theme_idle)

    def _reload_theme_idle(self) -> bool:
        self._theme_reload_source = 0
        self._reload_theme()
        return False

    def _reload_theme(self) -> None:
        message = None
        previous = self._theme
        try:
            current = read_theme(self._theme_path)
        except FileNotFoundError:
            current = DEFAULT_THEME
        except (ValueError, OSError) as e:
            logger.warning("Could not load overlay theme {}: {}", self._theme_path, e)
            message = "Couldn't load the theme. Keeping the current appearance."
        if message is None:
            try:
                if self._pill is not None and current != previous:
                    self._pill.apply_theme(current)
            except Exception:
                logger.exception("Could not render overlay theme")
                message = "Couldn't update the overlay appearance."
            else:
                if current != previous:
                    logger.debug(
                        "Overlay theme loaded from {} (name={}, anchor={})",
                        self._theme_path,
                        diagnostic(current.meta.name),
                        current.position.anchor,
                    )
                self._theme = current
        self._theme_load_error = message
        if self._theme_monitor is not None or self._theme_target_monitor is not None:
            self._sync_theme_target_monitor()
        if self._settings_window is not None:
            self._settings_window.refresh_theme(message)

    def _apply_theme(self, theme_id: str) -> None:
        try:
            apply_theme_id(theme_id, self._user_theme_dir, self._theme_path)
        finally:
            self._reload_theme()

    def _refresh_skin_view(self, message: str | None) -> None:
        if self._settings_window is None:
            return
        self._settings_window.refresh_skin()
        if message is not None:
            self._settings_window.show_toast(message)

    def _load_user_css(self):
        """Apply the optional style.css in the config directory to GTK widgets."""
        css_path = paths.config_dir() / "style.css"
        if not css_path.is_file():
            return
        provider = Gtk.CssProvider()
        provider.connect(
            "parsing-error",
            lambda _provider, _section, error: logger.warning(
                "User style.css: {}", error.message
            ),
        )
        provider.load_from_path(str(css_path))
        self._user_css_provider = provider
        self._sync_user_css_motion()
        Gtk.StyleContext.add_provider_for_display(
            Gdk.Display.get_default(),
            provider,
            Gtk.STYLE_PROVIDER_PRIORITY_USER + 1,
        )
        logger.debug("User style.css loaded from {}", css_path)

    def _sync_user_css_motion(self) -> None:
        provider = self._user_css_provider
        if provider is not None and provider.find_property("prefers-reduced-motion"):
            provider.set_property(
                "prefers-reduced-motion",
                Gtk.ReducedMotion.NO_PREFERENCE
                if self._motion_enabled
                else Gtk.ReducedMotion.REDUCE,
            )

    def _on_reduced_motion(self, reduced: bool):
        """Remember the system preference and update each existing view owner."""
        self._motion_enabled = not reduced
        brand.set_motion_enabled(self._motion_enabled)
        self._sync_user_css_motion()
        if self._pill is not None:
            self._pill.set_motion_enabled(self._motion_enabled)
        if self._tray:
            self._tray.set_animated(self._motion_enabled)
        for view in (self._settings_window, self._onboarding, self._benchmark_dialog):
            if view is not None:
                view.set_motion_enabled(self._motion_enabled)

    # --- Error notification ---

    def _show_error(
        self,
        message: str,
        detail: str | None = None,
        essential: bool = False,
        guidance: str | None = None,
        button: tuple[str, str] | None = None,
    ):
        """Show an error on the pill, in a notification, or both.

        The heading and guidance go to the pill while idle and the fuller detail to
        notifications. Essential errors also notify beside the pill so guidance outlives
        auto-dismiss.
        """
        on_pill = self._pill_carries(
            RecordingState.IDLE,
            lambda: self._pill.show_error(message, guidance=guidance),
        )
        if presentation.error_uses_notification(essential, on_pill):
            self._notice(
                "quoth-error",
                "Quoth",
                detail or (f"{message}. {guidance}." if guidance else message),
                button=button,
            )

    def _pill_carries(self, state: RecordingState, show: Callable[[], None]) -> bool:
        """Put a message raised in `state` on the pill; return whether its words show.

        `show` runs only while the controller is in `state`, since on another state's
        pill the message would replace that view. The orb renders no text, so it counts
        as no pill.
        """
        if not presentation.overlay_available() or self._controller.state is not state:
            return False
        show()
        return self._pill.shows_text

    def _on_no_input(self):
        """Say that a recording has captured no audio: the source is dead or misrouted.

        The pill's hint clears on the next level or state change; where no pill shows
        text, the status notification says it instead.
        """
        message, guidance = "No input detected", "Check your microphone"
        if not self._pill_carries(
            RecordingState.RECORDING,
            lambda: self._pill.show_hint(message, guidance=guidance),
        ):
            self._notify_status(f"{message}. {guidance}.", low=False)

    def _on_typing_failed(self, result: InjectionResult, *, at_startup: bool = False):
        """Say why a paste failed and whether the transcript is on the clipboard.

        At startup it says why no dictation can paste. The first time each reason occurs
        it also notifies beside the pill; after that the short pill notice is enough. A
        notification always links troubleshooting.
        """
        if (
            result is InjectionResult.UINPUT_SETUP_REQUIRED
            and not self._typing_setup_shown
        ):
            self._typing_setup_shown = True
            logger.info("Host /dev/uinput access missing; offering the uinput setup")
            # The dialog presents itself, and the application holds its window.
            TypingSetupDialog(
                self, on_run=run_uinput_setup, transcript_on_clipboard=not at_startup
            )
            return
        detail, guidance = _PASTE_FAILURES.get(result, _PASTE_ON_CLIPBOARD)
        first = result not in self._paste_failures_explained
        self._paste_failures_explained.add(result)
        self._show_error(
            "Can't paste dictations" if at_startup else "Paste failed",
            detail,
            essential=first,
            guidance=guidance,
            button=("Troubleshooting", "app.troubleshooting"),
        )

    def _toast_or_notice(
        self, notification_id: str, message: str, *, low: bool = False
    ) -> None:
        """Say `message` in a toast while Settings is open, else in a notification."""
        if self._settings_window is not None:
            self._settings_window.show_toast(message)
        else:
            self._notice(notification_id, "Quoth", message, low=low)

    # --- Speech engine context ---

    def _current_context_status(self) -> ContextStatus | None:
        status = self._controller.context_status if self._controller else None
        if status is not None and (status.model, status.requested_device) == (
            self._config.model,
            self._device,
        ):
            return status
        return None

    def _refresh_context_ui(self) -> None:
        status = self._current_context_status()
        crash_reports.note_context(
            status.outcome.openvino_active if status is not None else None
        )
        if self._settings_window is not None:
            self._settings_window.refresh_runtime(
                fallback_note(status.requested_device, status.runs_on)
                if status is not None and status.fallback
                else None
            )

    def _on_context_changed(self, status: ContextStatus | None) -> None:
        if self._shutting_down or self._controller.context_status is not status:
            return
        self._refresh_context_ui()
        if (
            status is not None
            and self._current_context_status() is status
            and status.fallback
            and status.requested_device not in self._noted_fallbacks
        ):
            self._noted_fallbacks.add(status.requested_device)
            self._toast_or_notice(
                "quoth-acceleration",
                fallback_note(status.requested_device, status.runs_on),
                low=True,
            )

    # --- Acceleration backend resolution ---

    def _apply_effective_device(self) -> None:
        """Resolve and apply the effective device for the model and device settings.

        A degrade, an explicit NPU or GPU choice this model or machine cannot honour, is
        announced in Settings when it is open, else in a low-priority notice. `auto`
        chooses rather than degrades, so it never nags.
        """
        res = resolve_for(self._config.model, self._config.device, self._model_mgr)
        self._device = res.device
        crash_reports.note_device(res.device)
        self._refresh_context_ui()
        if self._controller:
            # set_device only stores the string; the keyed context cache reconciles on
            # the next _ensure_context.
            self._controller.set_device(res.device)
            # A backend that needs an OpenVINO compile prepares now, gated, rather than
            # stalling the first dictation; _finish_activation raises the startup gate.
            if expects_slow_compile(self._config.model, res.device):
                self._gate.prepare(self._config.model, res.device)
        if res.degraded:
            # Its id is the load fallback's: each says which device runs instead.
            self._toast_or_notice("quoth-acceleration", res.reason, low=True)

    # --- NPU encoder preparation ---

    def _notify_preparing(self) -> None:
        """Show the notification while the compile runs; the gate withdraws it.

        One fixed id, so re-surfacing it on a gated keypress just replaces the last.
        Normal priority: it says why dictation cannot start yet.
        """
        self._notice(
            "quoth-preparing",
            "Quoth",
            "Preparing NPU acceleration. This can take several minutes.",
        )

    # --- Hardware benchmark ---

    def _open_benchmark(self) -> None:
        """Open the staged benchmark dialog: Prepare -> Measure -> Result.

        The dialog is a view; it runs the benchmark through `_start_benchmark`.
        """
        if self._benchmark_dialog is not None:
            self._benchmark_dialog.present(self._settings_window)
            return
        models = self._model_mgr.list_downloaded()
        if not models:
            logger.info("Benchmark not opened: no downloaded model")
            self._settings_window.show_toast("Download a model before benchmarking")
            return
        dialog = BenchmarkDialog(
            models,
            available_devices(),
            start=self._start_benchmark,
            cancel=self._controller.cancel_calibration,
            motion_enabled=self._motion_enabled,
        )
        dialog.connect("closed", self._on_benchmark_closed)
        self._benchmark_dialog = dialog
        dialog.present(self._settings_window)

    def _start_benchmark(
        self,
        models: list[str],
        devices: list[str],
        on_progress: ProgressFn,
        on_done: DoneFn,
    ) -> bool:
        """Drive the engine over the cells the dialog displays; False if it is busy.

        The effective device is re-resolved as the run finishes, so a fresh ranking
        takes effect before the dialog shows its verdict.
        """
        if self._gate.active:
            # An NPU compile holds the engine, and the run would wait behind it.
            logger.debug("Benchmark refused: NPU preparation in progress")
            return False
        return self._controller.calibrate(
            models,
            devices,
            fingerprint(),
            on_progress=on_progress,
            on_done=lambda report: self._benchmark_done(report, on_done),
        )

    def _benchmark_done(self, report: Report | None, on_done: DoneFn) -> None:
        self.withdraw_notification("quoth-benchmark")
        self._apply_effective_device()
        on_done(report)

    def _on_benchmark_closed(self, dialog) -> None:
        if self._benchmark_dialog is not dialog:
            return
        self._benchmark_dialog = None
        if self._settings_window:
            self._settings_window.refresh_benchmark_status(
                current_ranking() is not None
            )
            self._settings_window.refresh_cache_status()

    def _maybe_offer_calibration(self) -> None:
        """Offer users who skipped onboarding a one-time prompt to benchmark.

        It comes when there is an accelerator to rank and no ranking yet. Onboarding
        sets the same flag, so a new user is never asked twice.
        """
        if self._config.calibration_offered or not worth_offering():
            return
        # Normal priority: the user needs time to see and click it.
        self._notice(
            "quoth-calibrate",
            "Optimize Quoth for your hardware",
            "Benchmark your downloaded models to find the fastest measured device "
            "for each." + preparation_note(available_devices()),
            button=("Optimize", "app.run-benchmark"),
        )
        self._config.calibration_offered = True

    def _on_run_benchmark(self, _action, _param):
        """Open Settings and the benchmark dialog from the offer's "Optimize" button.

        Settings is the dialog's parent and where the benchmarked status then shows.
        """
        self.withdraw_notification("quoth-calibrate")
        self._show_settings()
        # Running onboarding comes forward instead; it has its own Optimize step.
        if self._settings_window is not None:
            self._open_benchmark()

    # --- Usage stats + support nudge (local, private) ---

    def _on_dictation_success(self) -> None:
        """Bump the local usage count (this machine only) after a pasted dictation."""
        self._stats = stats.record_dictation(
            self._stats, today=date.today().isoformat()
        )
        stats.save(self._stats_path, self._stats)
        if self._tray:
            self._tray.refresh_menu()

    def _usage_since_label(self) -> str | None:
        """Return the first dictation's month and year for the Settings usage summary.

        None before any dictation or if the stored date is unparseable.
        """
        first = self._stats["first_seen"]
        if not first:
            return None
        try:
            return date.fromisoformat(first).strftime("%B %Y")
        except ValueError:
            return None

    def _maybe_nudge_support(self) -> None:
        """Nudge the user once, after enough dictations, to support development."""
        if not stats.due_for_nudge(self._stats):
            return
        # Normal priority: the user needs time to see and click it.
        self._notice(
            "quoth-support",
            "Enjoying Quoth?",
            f"You've dictated {self._stats['dictations_total']} times. Quoth is free and "
            "open source; if it helps you, you can support its development.",
            button=("Support", "app.support"),
        )
        self._stats = stats.mark_nudged(self._stats)
        stats.save(self._stats_path, self._stats)

    def _on_support(self, _action, _param):
        """Open the funding page from the support nudge's "Support" button."""
        self.withdraw_notification("quoth-support")
        open_uri(SUPPORT_URL)

    # --- Settings reactions ---

    def _clear_acceleration_cache(self) -> bool:
        """Clear prepared NPU files while idle, after persisting their cold state."""
        if self._gate.active or not self._controller.is_idle:
            logger.debug("Refusing to clear acceleration cache: engine busy")
            return False
        # Persist cold state first: a failed or partial disk deletion must never leave
        # calibration claiming the cache is warm. Measured timings remain intact.
        evict_warmth()
        self._model_mgr.clear_cache()
        return True

    def _on_setting_changed(self, name: str) -> None:
        """Apply a setting as it changes, whoever wrote it, `gsettings` included.

        A setting read at use-time needs nothing here.
        """
        value = getattr(self._config, name)
        logger.debug("Setting changed: {} = {}", name, value)
        if name == "launch_at_login":
            autostart.set_enabled(value, self._on_autostart_declined)
        elif name == "crash_reports":
            crash_reports.set_enabled(value)
            # A stack kept from before consent goes out once it is given.
            crash_reports.report_native_crash()
        elif name == "overlay_style":
            self._pill.set_style(value)
        elif name == "sound_feedback":
            # A prerolled cue holds an output stream open: only enabled sound keeps one.
            if presentation.feedback_enabled(value):
                self._sound.prewarm()
            else:
                self._sound.shutdown()
        elif name in ("device", "model"):
            self._reconcile_engine()
            if name == "model" and self._tray:
                self._tray.refresh_menu()
        elif name == "language":
            if self._tray:
                self._tray.refresh_menu()
        elif name == "mic_source":
            self._controller.apply_mic_source()
        elif name == "vad":
            # A context keeps the VAD choice it opened with; the next dictation reopens.
            self._controller.when_idle(self._controller.free_context)
        elif name == "hotkey":
            if self._listener is not None:
                try:
                    self._listener.rebind(value)
                except ValueError:
                    self._reject_hotkey()
            elif self._input_live:
                # A listener that could not start gets another try.
                self._start_listener()
        elif name == "mode":
            if value == TAP_TO_TOGGLE:
                self.withdraw_notification(_UNHOLDABLE_NOTICE)
            elif self._listener is not None and self._listener.unholdable:
                self._notice_unholdable_hotkey()
        elif name == "keep_warm":
            self._controller.apply_keep_warm()
        elif name == "keep_warm_timeout":
            self._controller.reschedule_warm_timeout()
        elif name == "notify_updates":
            # Enabling it checks now (instant feedback), bypassing the throttle.
            if value:
                self._updates.maybe_check(force=True)

    def _reconcile_engine(self) -> None:
        """Fit the engine to the model and device settings and the files on disk.

        It waits until no dictation or benchmark holds the engine: an NPU compile
        started mid-dictation would take the inference lock its transcription waits on.
        """
        self._controller.when_idle(self._fit_engine)
        if self._tray:
            self._tray.refresh_menu()

    def _fit_engine(self) -> None:
        """Re-resolve the effective device and drop a context whose weights are gone.

        The keyed context cache would otherwise keep using a deleted model's context.
        """
        self._apply_effective_device()
        if not self._model_mgr.is_downloaded(self._config.model):
            self._controller.free_context()

    # --- Tray quick-switch (Language / Model submenus) ---

    def _downloaded_models(self) -> list[str]:
        """Return the downloaded model names in catalog order.

        The tray's Model submenu offers only models already on disk, so a switch
        downloads at most the NPU encoder its device needs.
        """
        return [
            m.name for m in AVAILABLE_MODELS if self._model_mgr.is_downloaded(m.name)
        ]

    def _change_language(self, code: str) -> None:
        """Apply the tray's language choice; the next recording uses it, no reload."""
        self._config.language = code

    def _change_model(self, name: str) -> None:
        """Switch the model from the tray by the `ModelSwitcher` rule Settings uses."""
        self._model_switcher.select("model", name)

    def _notify_downloading(self) -> None:
        """Tell the user the model cannot be used or switched yet: it is downloading.

        It shares the id of the download's own announcement, which replaces it when the
        files land. Normal priority: it says why dictation cannot start yet.
        """
        name = model_label(self._model_switcher.pending.model)
        self._notice("quoth-model", "Quoth", f"{name} is still downloading")

    # --- About and Settings windows ---

    def _show_about(self):
        parent = self._settings_window or self.get_active_window()
        show_about_dialog(parent)

    def _show_settings(self):
        if self._onboarding is not None:
            # Settings would change what setup is still choosing.
            self._onboarding.present()
            return
        if self._settings_window:
            logger.debug("Settings window presented")
            self._settings_window.present()
            return
        logger.debug("Settings window opened")
        self._settings_window = SettingsWindow(
            self._config,
            self._model_mgr,
            self._model_switcher,
            reconcile_engine=self._reconcile_engine,
            enumerate_microphones=self._controller.list_microphones,
            portal_shortcuts=self._portal_shortcuts,
            motion_enabled=self._motion_enabled,
            listener_state=(
                self._listener.state if self._listener else ListenerState.STOPPED
            ),
            update_bridge=UpdateBridge(
                available=lambda: self._updates.available,
                checking=lambda: self._updates.is_checking,
                checked=lambda: self._updates.checked_once,
                failed=lambda: self._updates.last_reached is False,
                check_now=self._updates.check_now,
                view_release=self._open_release_page,
            ),
            is_idle=lambda: self._controller.is_idle,
            calibration_bridge=CalibrationBridge(
                ranking=current_ranking(),
                run=self._open_benchmark,
                clear_cache=self._clear_acceleration_cache,
            ),
            on_hotkey_capture=self._set_hotkey_capture,
            theme_bridge=ThemeBridge(
                presets=list_presets(),
                user_themes=lambda: list_user_themes(self._user_theme_dir),
                current=lambda: identify_theme(self._theme_path, self._user_theme_dir),
                theme_of=lambda tid: theme_from_id(tid, self._user_theme_dir),
                live=lambda: self._theme,
                apply=self._apply_theme,
                import_file=lambda path: import_user_theme(path, self._user_theme_dir),
                load_error=lambda: self._theme_load_error,
            ),
            skin_bridge=SkinBridge(
                presets=skin.list_presets(),
                user_skins=self._skins.user_skins,
                current=self._skins.current,
                skin_of=self._skins.skin_of,
                apply=self._skins.apply,
                import_file=self._skins.import_file,
                load_error=self._skins.load_error,
            ),
            usage=UsageInfo(
                dictations_total=self._stats["dictations_total"],
                since=self._usage_since_label(),
            ),
        )
        self._settings_window.set_application(self)
        self._settings_window.connect("close-request", self._on_settings_closed)
        self._refresh_context_ui()
        self._settings_window.present()
        # The theme preview doubles as the input meter, so it needs real levels while
        # it is on screen. Paired with the teardown in _on_settings_closed.
        self._controller.set_input_monitoring(self._settings_window, True)

    def _on_settings_closed(self, window):
        logger.debug("Settings window closed")
        self._controller.set_input_monitoring(window, False)
        self._settings_window = None
        if self._tray:
            self._tray.refresh_menu()
        return False

    def _set_hotkey_capture(self, active: bool):
        """Suspend the live hotkey while its capture dialog is open.

        evdev reads below GTK, so the keypress that picks a new hotkey also reaches the
        running listener, and the current hotkey would start recording mid-capture.
        """
        if self._listener:
            self._listener.suspended = active

    # --- Cleanup ---

    def _quit_on_signal(self, signum: int) -> bool:
        logger.info("Received {}; quitting", signal.Signals(signum).name)
        self.quit()
        return GLib.SOURCE_CONTINUE

    def do_shutdown(self):
        """Stop what startup started: inputs, then the engine and its outputs.

        It runs once the main loop has stopped; GApplication then syncs settings and
        flushes the bus. An exit without `quit()`, as when onboarding's window closes
        first, also runs callbacks already queued, so those check `_shutting_down`.
        """
        logger.info("Quoth shutting down")
        self._shutting_down = True
        # The signals' default action again, so one ends a stuck shutdown at once.
        for source in self._signal_sources:
            GLib.source_remove(source)
        # Inputs first, so nothing new reaches the engine; the tray icon goes at once.
        if self._portal_shortcuts:
            self._portal_shortcuts.close()
        if self._listener:
            self._listener.stop()
            self._listener = None
        if self._tray:
            self._tray.stop()
        # The engine joins its workers, whose pastes need the typer.
        if self._controller:
            self._controller.shutdown()
        shutdown_typer()
        self._sound.shutdown()
        crash_reports.note_context(None)
        # Notices about work that ends with the process.
        for notice in (
            "quoth-status",
            "quoth-preparing",
            "quoth-benchmark",
            _UNHOLDABLE_NOTICE,
        ):
            self.withdraw_notification(notice)
        self._unwatch_theme()
        self._skins.shutdown()
        Adw.Application.do_shutdown(self)


def _log_native_discovery() -> None:
    """Log what the package found at import, now that the log sinks exist."""
    for path, reason in quoth.LOAD_FAILURES:
        logger.warning("{} exists but failed to load: {}", path, reason)
    if quoth.TYPELIB_DIR:
        logger.debug("GI_TYPELIB_PATH: added {}", quoth.TYPELIB_DIR)
    else:
        logger.debug("gtk4-layer-shell typelib dir not found")
    if quoth.LAYER_SHELL_LIB:
        logger.debug("gtk4-layer-shell: preloaded {}", quoth.LAYER_SHELL_LIB)
    else:
        logger.debug(
            "gtk4-layer-shell: not loaded; overlay stays inert, state shown via tray"
        )
    if quoth.OPENVINO_LIB:
        logger.debug("OpenVINO core preloaded: {}", quoth.OPENVINO_LIB)
    else:
        logger.debug("OpenVINO core: not loaded; NPU encoder offload unavailable")
    if quoth.NPU_DRIVER_DIR:
        logger.debug("Intel NPU driver: bundled in {}", quoth.NPU_DRIVER_DIR)


def main() -> int:
    """Run the application and return its exit status.

    `quoth.cli.main`, the entry point, comes here for any launch it does not answer.
    """
    # Notifications and untitled windows show this; unset, GLib falls back to the
    # program name "quoth". GLib warns if it is set twice, so only here.
    GLib.set_application_name("Quoth")
    app = QuothApp()
    return app.run(sys.argv)
