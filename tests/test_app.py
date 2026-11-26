# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 Samuel Ruairí Bullard

"""Tests for `app`.

Only the composition root's wiring is tested here; the recording state machine itself
is tested in `test_recording.py`.
"""

import subprocess
import sys
from unittest.mock import MagicMock, call, create_autospec, patch

import pytest
from gi.repository import Gio, GLib

from quoth import ipc
from quoth.accel import BackendResolution
from quoth.benchmark_dialog import BenchmarkDialog
from quoth.config import Config
from quoth.downloads import DownloadCoordinator
from quoth.listener import KeyListener, ListenerState
from quoth.models import ModelManager
from quoth.motion import ReducedMotionWatcher
from quoth.onboarding import OnboardingAssistant
from quoth.overlay import WaveformPill
from quoth.portal_shortcuts import PortalShortcuts
from quoth.prepare import PrepareGate
from quoth.recording import (
    ContextLoadOutcome,
    ContextStatus,
    RecordingController,
    RecordingState,
    model_error,
)
from quoth.scheduler import GLibScheduler
from quoth.settings import ModelSwitcher
from quoth.settings_window import SettingsWindow
from quoth.skin_runtime import SkinRuntime
from quoth.sound import SoundPlayer
from quoth.tray import QuothTray
from quoth.trigger import PUSH_TO_HOLD, TAP_TO_TOGGLE
from quoth.typer import InjectionResult
from quoth.update_notifier import UpdateNotifier
from tests.support import wait_until


def _spec(cls, **attrs):
    """A double of one of our classes, held to its members and their signatures.

    `spec_set` also refuses setting a member the class lacks, which a plain spec allows.
    """
    return create_autospec(cls, spec_set=True, instance=True, **attrs)


def _make_app():
    """Create a QuothApp instance without running GTK init."""
    from quoth.app import QuothApp

    app = QuothApp.__new__(QuothApp)
    # The pill and tray check method names only: autospeccing them for every test would
    # nearly double this file's run time.
    app._pill = MagicMock(spec_set=WaveformPill)
    app._tray = MagicMock(spec_set=QuothTray)
    app._controller = create_autospec(RecordingController, instance=True)
    app._controller.context_status = None
    app._listener = create_autospec(KeyListener, instance=True)
    app._shutting_down = False
    app._noted_fallbacks = set()
    app._paste_failures_explained = set()
    app._typing_setup_shown = False
    app._device = "CPU"
    app._portal_shortcuts = None
    app._onboarding = None
    app._input_live = True
    app._sound = create_autospec(SoundPlayer, instance=True)
    app._config = Config()
    app._config.model = "base"
    # Notifications need a real GApplication; mock them so state-change tests
    # exercise the gating, not the GTK plumbing.
    app._notify_status = MagicMock()
    app.withdraw_notification = MagicMock()
    # The app builds a clear NPU-compile gate alongside the controller.
    app._gate = PrepareGate(app._controller, notify=MagicMock(), withdraw=MagicMock())
    app._scheduler = create_autospec(GLibScheduler, instance=True)
    app._skins = create_autospec(SkinRuntime, instance=True)
    app._downloads = create_autospec(DownloadCoordinator, instance=True)
    app._downloads.is_downloading.return_value = False
    app._model_switcher = create_autospec(ModelSwitcher, instance=True)
    app._model_switcher.pending = None
    app._theme_monitor = None
    app._theme_target_monitor = None
    app._theme_target_path = None
    app._theme_reload_source = 0
    app._theme_load_error = None
    app._settings_window = None
    app._benchmark_dialog = None
    app._user_css_provider = None
    app._motion_enabled = True
    app._dictation_service = None
    app._signal_sources = []
    return app


@pytest.fixture
def live_config():
    """Make an app's settings changes reach it, as `_start` connects them.

    Each handler is disconnected afterwards, so no app hears a later test's writes.
    """
    wired = []

    def wire(app):
        config = app._config
        wired.append((config, config.connect_changed(app._on_setting_changed)))
        return config

    yield wire
    for config, handler in wired:
        config.settings.disconnect(handler)


class TestListenerLifecycle:
    def test_edges_reach_the_trigger_rule_ahead_of_redraws(self):
        from gi.repository import GLib

        app = _make_app()
        app._listener = None
        with (
            patch("quoth.app.evdev_available", return_value=True),
            patch("quoth.app.KeyListener", autospec=True) as listener,
            patch("quoth.app.GLib.idle_add") as idle_add,
        ):
            app._start_listener()
            callbacks = listener.call_args.kwargs
            callbacks["on_press"]()
            callbacks["on_press"](TAP_TO_TOGGLE)
            callbacks["on_release"]()
        assert app._listener is listener.return_value
        assert [c.args for c in idle_add.call_args_list] == [
            (app._on_hotkey, True, None),
            (app._on_hotkey, True, TAP_TO_TOGGLE),
            (app._on_hotkey, False),
        ]
        assert {c.kwargs["priority"] for c in idle_add.call_args_list} == {
            GLib.PRIORITY_HIGH
        }

    _TOGGLES = (
        "This key can't be held on this keyboard, so it toggles: press it once to "
        "start dictating and again to stop."
    )

    def _report_unholdable(self, app, marked: bool, still_unholdable: bool = True):
        # The listener's report, as the main loop runs it.
        app._listener = None
        with (
            patch("quoth.app.evdev_available", return_value=True),
            patch("quoth.app.KeyListener", autospec=True) as listener,
            patch("quoth.app.GLib.idle_add") as idle_add,
        ):
            app._start_listener()
            app._listener.unholdable = still_unholdable
            listener.call_args.kwargs["on_unholdable"](marked)
        [report] = idle_add.call_args_list
        assert report.args[0](*report.args[1:]) is False

    @pytest.mark.parametrize(
        ("mode", "noticed"), [(PUSH_TO_HOLD, 1), (TAP_TO_TOGGLE, 0)]
    )
    def test_a_key_found_unholdable_is_noticed_only_when_holding(self, mode, noticed):
        app = _make_app()
        app._config.mode = mode
        app._notice = MagicMock()

        self._report_unholdable(app, True)

        assert app._notice.call_count == noticed
        if noticed:
            assert app._notice.call_args.args == (
                "quoth-hotkey-hold",
                "Quoth",
                self._TOGGLES,
            )

    def test_a_rebind_before_the_marking_report_leaves_no_notice(self):
        # The read thread reports a marking after its lock; a rebind on the main
        # thread can clear the mark and queue its own report first.
        from evdev import InputEvent, ecodes

        app = _make_app()
        app._config.mode = PUSH_TO_HOLD
        app._notice = MagicMock()
        reports = []
        app._listener = KeyListener(hotkey="KEY_PAUSE", on_unholdable=reports.append)
        with app._listener._lock:
            app._listener._accepting = True
        keyboard = object()

        def rebind_first(marked):
            if marked:
                app._listener.rebind("KEY_F13")
            reports.append(marked)

        app._listener._on_unholdable = rebind_first
        for usec, value in ((0, 1), (3_000, 0)):
            event = InputEvent(0, usec, ecodes.EV_KEY, ecodes.KEY_PAUSE, value)
            app._listener._handle_event(event, keyboard)
        assert reports == [False, True]
        for marked in reports:
            app._on_unholdable_hotkey(marked)

        app._notice.assert_not_called()

    def test_no_notice_once_shutting_down(self):
        app = _make_app()
        app._config.mode = PUSH_TO_HOLD
        app._notice = MagicMock()
        app._shutting_down = True

        assert app._on_unholdable_hotkey(True) is False

        app._notice.assert_not_called()

    @pytest.mark.parametrize(("still_unholdable", "withdrawn"), [(False, 1), (True, 0)])
    def test_the_notice_is_withdrawn_once_no_key_is_unholdable(
        self, still_unholdable, withdrawn
    ):
        app = _make_app()
        app._config.mode = PUSH_TO_HOLD
        app._notice = MagicMock()

        self._report_unholdable(app, False, still_unholdable)

        app._notice.assert_not_called()
        assert (
            app.withdraw_notification.call_args_list
            == [call("quoth-hotkey-hold")] * withdrawn
        )

    @pytest.mark.parametrize(
        ("mode", "unholdable", "noticed", "withdrawn"),
        [
            (PUSH_TO_HOLD, True, 1, 0),
            (PUSH_TO_HOLD, False, 0, 0),
            (TAP_TO_TOGGLE, True, 0, 1),
        ],
    )
    def test_the_mode_decides_whether_an_unholdable_key_is_noticed(
        self, mode, unholdable, noticed, withdrawn
    ):
        app = _make_app()
        app._notice = MagicMock()
        app._listener.unholdable = unholdable
        app._config.mode = mode

        app._on_setting_changed("mode")

        assert app._notice.call_count == noticed
        if noticed:
            assert app._notice.call_args.args[2] == self._TOGGLES
        assert (
            app.withdraw_notification.call_args_list
            == [call("quoth-hotkey-hold")] * withdrawn
        )

    def test_failure_stops_recording_and_retains_owner(self):
        app = _make_app()
        listener = app._listener
        listener.state = ListenerState.FAILED
        app._config.hotkey = "KEY_F13"
        app._controller.state = RecordingState.RECORDING
        app._settings_window = _spec(SettingsWindow)
        owners_at_stop = []
        app._controller.stop.side_effect = lambda: owners_at_stop.append(app._listener)
        app._on_listener_state(ListenerState.FAILED)
        assert owners_at_stop == [listener]
        assert app._listener is listener
        assert app._config.hotkey == "KEY_F13"
        app._settings_window.refresh_listener.assert_called_once_with(
            ListenerState.FAILED
        )
        app._settings_window.show_toast.assert_called_once_with(
            "Keyboard capture stopped. Restart Quoth to reconnect."
        )

    def test_a_superseded_state_is_ignored(self):
        app = _make_app()
        app._listener.state = ListenerState.AVAILABLE
        app._controller.state = RecordingState.RECORDING
        app._settings_window = _spec(SettingsWindow)
        app._on_listener_state(ListenerState.FAILED)
        app._controller.stop.assert_not_called()
        app._settings_window.refresh_listener.assert_not_called()

    def test_a_keyboard_coming_back_only_refreshes_settings(self):
        app = _make_app()
        app._listener.state = ListenerState.AVAILABLE
        app._controller.state = RecordingState.RECORDING
        app._settings_window = _spec(SettingsWindow)
        app._on_listener_state(ListenerState.AVAILABLE)
        app._settings_window.refresh_listener.assert_called_once_with(
            ListenerState.AVAILABLE
        )
        app._controller.stop.assert_not_called()
        app._settings_window.show_toast.assert_not_called()

    def test_failure_notifies_when_settings_is_closed(self):
        app = _make_app()
        app._listener.state = ListenerState.FAILED
        app._notice = MagicMock()
        app._on_listener_state(ListenerState.FAILED)
        app._notice.assert_called_once_with(
            "quoth-keyboard",
            "Quoth",
            "Keyboard capture stopped. Restart Quoth to reconnect.",
            low=False,
        )

    @pytest.mark.parametrize("window", [False, True])
    def test_a_listener_that_cannot_start_says_so(self, window):
        app = _make_app()
        app._listener = None
        app._notice = MagicMock()
        app._settings_window = _spec(SettingsWindow) if window else None
        message = "Keyboard capture couldn't start. Restart Quoth to try again."
        with (
            patch("quoth.app.evdev_available", return_value=True),
            patch("quoth.app.KeyListener", autospec=True) as listener,
        ):
            listener.return_value.start.side_effect = RuntimeError("no new thread")
            app._start_listener()
        assert app._listener is None
        if window:
            app._settings_window.show_toast.assert_called_once_with(message)
            app._notice.assert_not_called()
        else:
            app._notice.assert_called_once_with(
                "quoth-keyboard", "Quoth", message, low=False
            )

    @pytest.mark.parametrize("active", [True, False])
    def test_a_hotkey_capture_suspends_the_live_hotkey(self, active):
        app = _make_app()
        app._set_hotkey_capture(active)
        assert app._listener.suspended is active

    def test_a_hotkey_change_rebinds_in_place_and_leaves_a_dictation(self, live_config):
        app = _make_app()
        app._controller.state = RecordingState.RECORDING
        live_config(app).hotkey = "KEY_F14"
        app._listener.rebind.assert_called_once_with("KEY_F14")
        app._listener.stop.assert_not_called()
        app._controller.stop.assert_not_called()
        app._controller.cancel.assert_not_called()

    def test_a_hotkey_change_retries_a_listener_that_never_started(self, live_config):
        app = _make_app()
        app._listener = None
        app._start_listener = MagicMock()
        live_config(app).hotkey = "KEY_F14"
        app._start_listener.assert_called_once_with()

    def test_a_hotkey_chosen_before_activation_waits_for_it(self, live_config):
        app = _make_app()
        app._listener = None
        app._input_live = False
        app._start_listener = MagicMock()
        live_config(app).hotkey = "KEY_F14"  # onboarding's shortcut step
        app._start_listener.assert_not_called()

    def test_a_rejected_hotkey_falls_back_to_the_default(self, live_config):
        app = _make_app()

        def rebind(key):
            if key == "KEY_BOGUS":
                raise ValueError("Unknown key: KEY_BOGUS")

        app._listener.rebind.side_effect = rebind
        app._notice = MagicMock()
        config = live_config(app)
        config.hotkey = "KEY_BOGUS"  # `gsettings set`, say
        assert config.hotkey == "KEY_PAUSE"
        app._notice.assert_called_once_with(
            "quoth-keyboard",
            "Quoth",
            "The saved dictation shortcut isn't recognized. Using Pause instead.",
            low=False,
        )
        assert [c.args for c in app._listener.rebind.call_args_list] == [
            ("KEY_BOGUS",),
            ("KEY_PAUSE",),
        ]

    @pytest.mark.parametrize("onboarding", [None, object()])
    def test_a_listener_rejecting_the_stored_key_starts_on_the_default(
        self, live_config, onboarding
    ):
        # Activation starts input while onboarding's window is still up.
        app = _make_app()
        app._listener = None
        app._onboarding = onboarding
        app._input_live = False
        app._notice = MagicMock()
        config = live_config(app)
        config.hotkey = "KEY_BOGUS"
        app._input_live = True
        started = []

        def listener(hotkey, **_callbacks):
            if hotkey == "KEY_BOGUS":
                raise ValueError("Unknown key: KEY_BOGUS")
            started.append(hotkey)
            return _spec(KeyListener, **{"start.return_value": True})

        with (
            patch("quoth.app.evdev_available", return_value=True),
            patch("quoth.app.KeyListener", side_effect=listener),
        ):
            app._start_listener()
        assert config.hotkey == "KEY_PAUSE"
        assert started == ["KEY_PAUSE"]

    def test_empty_scan_does_not_retain_listener(self):
        app = _make_app()
        app._listener = None
        app._settings_window = _spec(SettingsWindow)
        with (
            patch("quoth.app.evdev_available", return_value=True),
            patch("quoth.app.KeyListener", autospec=True) as listener,
        ):
            listener.return_value.start.return_value = False
            app._start_listener()
        assert app._listener is None
        app._settings_window.refresh_listener.assert_called_once_with(
            ListenerState.STOPPED
        )


class TestContextFeedback:
    def test_each_device_falls_back_aloud_once_per_launch(self):
        app = _make_app()
        app._settings_window = _spec(SettingsWindow)
        toasts = app._settings_window.show_toast
        for device, runs_on in (("NPU", "GPU"), ("NPU", "GPU"), ("GPU", "CPU")):
            # A reload after an idle unload reports the same fallback again.
            app._device = device
            loaded = ContextLoadOutcome.LOADED
            status = ContextStatus("base", device, loaded, runs_on)
            app._controller.context_status = status
            with patch("quoth.app.crash_reports.note_context") as note:
                app._on_context_changed(status)
                note.assert_called_with(False)
            app._settings_window.refresh_runtime.assert_called_with(
                f"{device} couldn't start; using {runs_on}"
            )
            app._controller.context_status = None
            app._on_context_changed(None)
            app._settings_window.refresh_runtime.assert_called_with(None)
        assert [c.args for c in toasts.call_args_list] == [
            ("NPU couldn't start; using GPU",),
            ("GPU couldn't start; using CPU",),
        ]

    def test_stale_context_cannot_announce_or_replace_observed_state(self):
        app = _make_app()
        app._device = "NPU"
        app._settings_window = _spec(SettingsWindow)
        stale = ContextStatus("base", "NPU", ContextLoadOutcome.LOADED, "CPU")
        current = ContextStatus("base", "NPU", ContextLoadOutcome.OPENVINO, "NPU")
        app._controller.context_status = current
        with patch("quoth.app.crash_reports.note_context") as note:
            app._on_context_changed(stale)
            note.assert_not_called()
            app._on_context_changed(current)
            note.assert_called_once_with(True)
        app._settings_window.show_toast.assert_not_called()

        app._settings_window.refresh_runtime.assert_called_once_with(None)

    def test_changed_selection_does_not_claim_previous_context(self):
        app = _make_app()
        app._device = "CPU"
        app._settings_window = _spec(SettingsWindow)
        status = ContextStatus("base", "NPU", ContextLoadOutcome.LOADED, "CPU")
        app._controller.context_status = status
        with patch("quoth.app.crash_reports.note_context") as note:
            app._on_context_changed(status)
            note.assert_called_once_with(None)
        app._settings_window.refresh_runtime.assert_called_once_with(None)
        app._settings_window.show_toast.assert_not_called()


class TestCrashReportWiring:
    def test_settings_switch_applies_consent_at_once(self, live_config):
        app = _make_app()
        config = live_config(app)
        with patch("quoth.app.crash_reports.set_enabled") as enable:
            config.crash_reports = True
            config.crash_reports = False
        assert [c.args for c in enable.call_args_list] == [(True,), (False,)]

    def test_consent_sends_the_stack_kept_from_before_it(self, live_config):
        app = _make_app()
        config = live_config(app)
        with patch("quoth.app.crash_reports") as reports:
            config.crash_reports = True
        assert reports.mock_calls == [
            call.set_enabled(True),
            call.report_native_crash(),
        ]

    def test_startup_applies_consent_before_building_anything(self):
        # A failure anywhere in startup is then reported for an opted-in user. The file
        # log comes first, so the hooks' own warnings reach it.
        app = _make_app()
        app._config.crash_reports = True
        order = []

        class Built(Exception):
            pass

        with (
            patch(
                "quoth.app.quoth.log.install_file_log",
                side_effect=lambda: order.append("file log"),
            ),
            patch(
                "quoth.app.quoth.log.install_crash_hooks",
                side_effect=lambda: order.append("hooks"),
            ),
            patch("quoth.app.crash_reports") as reports,
            patch("quoth.app.GLib.unix_signal_add"),
            patch("quoth.app.Adw.Application.do_startup", side_effect=Built),
        ):
            reports.set_enabled.side_effect = lambda on: order.append(("consent", on))
            reports.report_native_crash.side_effect = lambda: order.append("native")
            with pytest.raises(Built):
                app.do_startup()
        assert order == ["file log", "hooks", ("consent", True), "native"]

    def test_a_signal_ignored_at_launch_stays_ignored(self):
        # Under nohup, closing the terminal must not end Quoth.
        import signal

        app = _make_app()

        class Built(Exception):
            pass

        ignored = {signal.SIGHUP}
        with (
            patch("quoth.app.quoth.log.install_file_log"),
            patch("quoth.app.quoth.log.install_crash_hooks"),
            patch("quoth.app.crash_reports"),
            patch(
                "quoth.app.signal.getsignal",
                side_effect=lambda s: (
                    signal.SIG_IGN if s in ignored else signal.SIG_DFL
                ),
            ),
            patch("quoth.app.GLib.unix_signal_add", return_value=7) as add,
            patch("quoth.app.Adw.Application.do_startup", side_effect=Built),
        ):
            with pytest.raises(Built):
                app.do_startup()
        assert [c.args[1:] for c in add.call_args_list] == [
            (signal.SIGTERM, app._quit_on_signal, signal.SIGTERM)
        ]
        assert app._signal_sources == [7]

    def test_shutdown_clears_the_reported_context(self):
        app = _make_app()
        with (
            patch("quoth.app.Adw.Application.do_shutdown"),
            patch("quoth.app.crash_reports.note_context") as note,
        ):
            app.do_shutdown()
        note.assert_called_once_with(None)


class TestBenchmarkOwnership:
    def test_existing_dialog_is_presented_again(self):
        app = _make_app()
        dialog = MagicMock()
        app._benchmark_dialog = dialog
        with patch("quoth.app.BenchmarkDialog") as create:
            app._open_benchmark()
        create.assert_not_called()
        dialog.present.assert_called_once_with(app._settings_window)

    def test_old_close_cannot_drop_live_dialog(self):
        app = _make_app()
        current = MagicMock()
        app._benchmark_dialog = current
        app._on_benchmark_closed(MagicMock())
        assert app._benchmark_dialog is current
        app._on_benchmark_closed(current)
        assert app._benchmark_dialog is None

    def test_close_refreshes_benchmark_and_cache_rows(self):
        app = _make_app()
        dialog = MagicMock()
        app._benchmark_dialog = dialog
        app._settings_window = _spec(SettingsWindow)
        with patch("quoth.app.current_ranking", return_value={"base": ["CPU"]}):
            app._on_benchmark_closed(dialog)
        app._settings_window.refresh_benchmark_status.assert_called_once_with(True)
        app._settings_window.refresh_cache_status.assert_called_once_with()

    def test_a_start_while_it_holds_the_engine_says_why(self, caplog):
        app = _make_app()
        app._notice = MagicMock()
        app._controller.state = RecordingState.IDLE
        app._controller.is_idle = False
        app._controller.start.return_value = False
        assert app._script_start() == (
            "Dictation did not start; Quoth shows why on screen"
        )
        app._controller.start.assert_not_called()
        app._notice.assert_called_once_with(
            "quoth-benchmark",
            "Quoth",
            "Quoth is busy with the benchmark. Dictate once it finishes.",
        )
        assert "Dictation refused: the engine is busy with the benchmark" in (
            caplog.messages
        )

    def test_its_end_withdraws_the_busy_notice(self):
        app = _make_app()
        app.withdraw_notification = MagicMock()
        app._apply_effective_device = MagicMock()
        on_done = MagicMock()
        app._benchmark_done(None, on_done)
        app.withdraw_notification.assert_called_once_with("quoth-benchmark")
        on_done.assert_called_once_with(None)

    def test_its_ranking_takes_effect_before_the_verdict_shows(self):
        app = _make_app()
        order = []
        app._apply_effective_device = MagicMock(
            side_effect=lambda: order.append("device")
        )
        app._benchmark_done(None, lambda _report: order.append("verdict"))
        assert order == ["device", "verdict"]


class TestAccelerationCache:
    def _app(self):
        app = _make_app()
        app._model_mgr = _spec(ModelManager)
        app._controller.is_idle = True
        return app

    def test_busy_engine_refuses_without_touching_persistent_state(self):
        app = self._app()
        app._controller.is_idle = False
        with patch("quoth.app.evict_warmth") as evict:
            assert app._clear_acceleration_cache() is False
        evict.assert_not_called()
        app._model_mgr.clear_cache.assert_not_called()

    def test_active_prepare_refuses_without_touching_persistent_state(self):
        app = self._app()
        app._gate.raise_for("small", "NPU")
        with patch("quoth.app.evict_warmth") as evict:
            assert app._clear_acceleration_cache() is False
        evict.assert_not_called()
        app._model_mgr.clear_cache.assert_not_called()

    def test_persists_cold_state_before_deleting_cache(self):
        app = self._app()
        order = []
        app._model_mgr.clear_cache.side_effect = lambda: order.append("delete")
        with patch("quoth.app.evict_warmth", side_effect=lambda: order.append("evict")):
            assert app._clear_acceleration_cache() is True
        assert order == ["evict", "delete"]

    def test_persistence_failure_aborts_deletion(self):
        app = self._app()
        with (
            patch("quoth.app.evict_warmth", side_effect=OSError("save failed")),
            pytest.raises(OSError, match="save failed"),
        ):
            app._clear_acceleration_cache()
        app._model_mgr.clear_cache.assert_not_called()

    def test_partial_deletion_stays_cold_and_reports_failure(self):
        app = self._app()
        order = []

        def fail_delete():
            order.append("delete")
            raise OSError("partial delete")

        app._model_mgr.clear_cache.side_effect = fail_delete
        with (
            patch("quoth.app.evict_warmth", side_effect=lambda: order.append("evict")),
            pytest.raises(OSError, match="partial delete"),
        ):
            app._clear_acceleration_cache()
        assert order == ["evict", "delete"]


class TestLocalOptions:
    def test_the_settings_open_before_startup(self):
        from gi.repository import GLib

        from quoth.app import QuothApp
        from quoth.config import Config

        app = QuothApp()
        assert app.do_handle_local_options(GLib.VariantDict()) == -1
        assert isinstance(app._config, Config)

    def test_a_missing_schema_ends_the_launch_with_an_error(self):
        from gi.repository import GLib

        from quoth.app import QuothApp

        app = QuothApp()
        with patch("quoth.app.Config", side_effect=RuntimeError("no schema")):
            assert app.do_handle_local_options(GLib.VariantDict()) == 1

    @pytest.mark.parametrize("name", ["start", "stop", "cancel", "status"])
    def test_a_script_command_beside_other_options_is_refused(self, name, capsys):
        # `cli` runs one alone; with company it would otherwise start or show Quoth.
        from gi.repository import GLib

        from quoth.app import QuothApp

        options = GLib.VariantDict()
        options.insert_value(name, GLib.Variant("b", True))
        options.insert_value("background", GLib.Variant("b", True))
        app = QuothApp()
        with patch("quoth.app.Config") as config:
            assert app.do_handle_local_options(options) == 1
        assert capsys.readouterr().err == f"quoth: --{name} takes no other options\n"
        config.assert_not_called()


class TestSkinWiring:
    def test_a_skin_change_refreshes_settings_and_toasts_its_message(self):
        app = _make_app()
        window = app._settings_window = _spec(SettingsWindow)
        app._refresh_skin_view(None)
        window.show_toast.assert_not_called()
        app._refresh_skin_view("Couldn't update the app appearance.")
        assert window.refresh_skin.call_count == 2
        window.show_toast.assert_called_once_with("Couldn't update the app appearance.")

    def test_shutdown_stops_the_runtime(self):
        app = _make_app()
        with (
            patch("quoth.app.Adw.Application.do_shutdown"),
        ):
            app.do_shutdown()
        app._skins.shutdown.assert_called_once_with()


class TestAppThemes:
    def _app(self, tmp_path):
        from quoth.theme import DEFAULT_THEME

        app = _make_app()
        app._theme = DEFAULT_THEME
        app._theme_path = tmp_path / "theme.toml"
        app._user_theme_dir = tmp_path / "themes"
        app._settings_window = _spec(SettingsWindow)
        return app

    def test_invalid_edits_retain_appearance_and_valid_load_clears_status(
        self, tmp_path, caplog
    ):
        from quoth.theme import DEFAULT_THEME

        app = self._app(tmp_path)
        app._theme_path.write_text("broken = [")
        app._reload_theme()
        assert app._theme == DEFAULT_THEME
        assert caplog.messages[-1].startswith(
            f"Could not load overlay theme {app._theme_path}: "
        )
        assert (
            app._theme_load_error
            == "Couldn't load the theme. Keeping the current appearance."
        )
        app._settings_window.refresh_theme.assert_called_with(app._theme_load_error)
        assert app._theme_path.read_text() == "broken = ["
        app._theme_path.write_text('[colors]\naccent = "#123456"')
        app._reload_theme()
        valid = app._theme
        assert app._theme_load_error is None
        assert caplog.messages[-1] == (
            f"Overlay theme loaded from {app._theme_path} (name=None, anchor=bottom-center)"
        )
        app._theme_path.write_bytes(b"\xff")
        app._reload_theme()
        assert app._theme is valid
        assert (
            app._theme_load_error
            == "Couldn't load the theme. Keeping the current appearance."
        )
        app._settings_window.refresh_theme.assert_called_with(app._theme_load_error)
        app._theme_path.unlink()
        app._reload_theme()
        assert app._theme == DEFAULT_THEME
        assert app._theme_load_error is None
        app._settings_window.refresh_theme.assert_called_with(None)

    def test_apply_without_monitor_reloads_once_and_retains_new_metadata(
        self, tmp_path
    ):
        app = self._app(tmp_path)
        app._user_theme_dir.mkdir()
        content = b'# comment\r\n[colors]\r\naccent = "#123456"\r\n'
        (app._user_theme_dir / "ink.toml").write_bytes(content)
        app._apply_theme("user:ink")
        assert app._theme_path.read_bytes() == content
        app._pill.apply_theme.assert_called_once_with(app._theme)
        app._reload_theme()
        app._theme_path.write_bytes(content + b'[meta]\r\nname = "New name"\r\n')
        app._reload_theme()
        assert app._theme.meta.name == "New name"
        assert app._pill.apply_theme.call_count == 1

    def test_failed_publication_sync_rereads_actual_disk_state(self, tmp_path):
        app = self._app(tmp_path)

        def publish_then_fail(*_args):
            app._theme_path.write_text('[meta]\nname = "Published"')
            raise OSError("directory sync failed")

        with (
            patch("quoth.app.apply_theme_id", side_effect=publish_then_fail),
            pytest.raises(OSError, match="directory sync"),
        ):
            app._apply_theme("user:requested")
        assert app._theme.meta.name == "Published"
        app._pill.apply_theme.assert_not_called()
        app._settings_window.refresh_theme.assert_called_once_with(None)

    def test_failed_render_is_reported_and_retried_on_reload(self, tmp_path):
        app = self._app(tmp_path)
        previous = app._theme
        app._theme_path.write_text('[colors]\naccent = "#123456"')
        app._pill.apply_theme.side_effect = [RuntimeError("renderer failed"), None]
        app._reload_theme()
        assert app._theme is previous
        assert app._theme_load_error == "Couldn't update the overlay appearance."
        app._settings_window.refresh_theme.assert_called_with(app._theme_load_error)
        app._reload_theme()
        assert app._pill.apply_theme.call_count == 2
        assert app._theme != previous
        assert app._theme_load_error is None

    def test_monitor_failure_does_not_disable_apply(self, tmp_path):
        from gi.repository import GLib

        app = self._app(tmp_path)
        with patch("quoth.app.Gio.File.new_for_path") as file:
            file.return_value.monitor_file.side_effect = GLib.Error("unavailable")
            app._watch_theme()
        assert app._theme_monitor is None
        app._apply_theme("nord")
        assert app._theme.meta.name == "Nord"
        assert app._theme_load_error is None

    def test_monitor_filters_and_coalesces_events(self, tmp_path):
        from gi.repository import Gio

        app = self._app(tmp_path)
        active = Gio.File.new_for_path(str(app._theme_path))
        temporary = Gio.File.new_for_path(str(tmp_path / ".edit.tmp"))
        with patch("quoth.app.GLib.idle_add", return_value=42) as idle:
            app._on_theme_file_changed(
                None, temporary, None, Gio.FileMonitorEvent.DELETED, app._theme_path
            )
            idle.assert_not_called()
            for event in (
                Gio.FileMonitorEvent.RENAMED,
                Gio.FileMonitorEvent.CHANGES_DONE_HINT,
            ):
                app._on_theme_file_changed(
                    None, temporary, active, event, app._theme_path
                )
            idle.assert_called_once_with(app._reload_theme_idle)
        with patch.object(app, "_reload_theme") as reload:
            assert app._reload_theme_idle() is False
        reload.assert_called_once()
        assert app._theme_reload_source == 0

    def test_real_monitor_tracks_atomic_target_saves_and_link_repointing(
        self, tmp_path
    ):
        from quoth.theme import DEFAULT_THEME

        app = self._app(tmp_path)
        target = tmp_path / "linked.toml"
        app._theme_path.symlink_to(target)

        # As at startup: the first load comes before the watch.
        app._reload_theme()
        app._watch_theme()
        try:
            assert app._theme == DEFAULT_THEME
            target.write_text('[meta]\nname = "Created"')
            wait_until(lambda: app._theme.meta.name == "Created")
            temporary = tmp_path / ".edit.tmp"
            temporary.write_text('[meta]\nname = "Replaced"')
            temporary.replace(target)
            wait_until(lambda: app._theme.meta.name == "Replaced")
            target.unlink()
            wait_until(lambda: app._theme.meta.name is None)
            replacement = tmp_path / "second.toml"
            replacement.write_text('[meta]\nname = "Repointed"')
            temporary.symlink_to(replacement)
            temporary.replace(app._theme_path)
            wait_until(lambda: app._theme.meta.name == "Repointed")
            assert app._theme_target_path == replacement
            temporary.write_text('[meta]\nname = "Second save"')
            temporary.replace(replacement)
            wait_until(lambda: app._theme.meta.name == "Second save")
        finally:
            app._theme_monitor.cancel()
            if app._theme_target_monitor is not None:
                app._theme_target_monitor.cancel()
            if app._theme_reload_source:
                GLib.source_remove(app._theme_reload_source)

    def test_shutdown_cancels_both_monitors_and_pending_reload(self, tmp_path):
        app = self._app(tmp_path)
        entry = app._theme_monitor = MagicMock()
        target = app._theme_target_monitor = MagicMock()
        app._theme_reload_source = 42
        with (
            patch("quoth.app.GLib.source_remove") as remove,
            patch("quoth.app.Adw.Application.do_shutdown"),
        ):
            app.do_shutdown()
        entry.cancel.assert_called_once()
        target.cancel.assert_called_once()
        remove.assert_called_once_with(42)
        assert app._theme_monitor is app._theme_target_monitor is None
        assert app._theme_reload_source == 0


class TestMotionPropagation:
    def test_latest_preference_reaches_all_open_surfaces(self):
        app = _make_app()
        app._settings_window = _spec(SettingsWindow)
        app._onboarding = _spec(OnboardingAssistant)
        app._benchmark_dialog = _spec(BenchmarkDialog)
        with patch("quoth.app.brand.set_motion_enabled") as styling:
            app._on_reduced_motion(True)
        assert app._motion_enabled is False
        styling.assert_called_once_with(False)
        app._tray.set_animated.assert_called_once_with(False)
        for view in (
            app._pill,
            app._settings_window,
            app._onboarding,
            app._benchmark_dialog,
        ):
            view.set_motion_enabled.assert_called_once_with(False)

    def test_preference_is_retained_before_surfaces_exist(self):
        app = _make_app()
        app._pill = app._tray = None
        with patch("quoth.app.brand.set_motion_enabled"):
            app._on_reduced_motion(True)
        assert app._motion_enabled is False

    def test_personal_provider_uses_supported_motion_preference(self):
        app = _make_app()
        app._user_css_provider = MagicMock()
        app._motion_enabled = False
        with patch("quoth.app.Gtk.ReducedMotion", create=True) as preference:
            app._sync_user_css_motion()
        app._user_css_provider.set_property.assert_called_once_with(
            "prefers-reduced-motion", preference.REDUCE
        )
        app._user_css_provider.reset_mock()
        app._user_css_provider.find_property.return_value = None
        app._sync_user_css_motion()
        app._user_css_provider.set_property.assert_not_called()

    def test_a_user_stylesheet_logs_its_path_as_detail(self, display, caplog):
        # Like the overlay theme and the app skin: a path is DEBUG, not a milestone.
        from gi.repository import Gdk, Gtk

        from quoth import paths

        css_path = paths.config_dir() / "style.css"
        css_path.parent.mkdir(parents=True, exist_ok=True)
        css_path.write_text("window {}\n")
        app = _make_app()
        app._load_user_css()
        Gtk.StyleContext.remove_provider_for_display(
            Gdk.Display.get_default(), app._user_css_provider
        )
        assert [
            r.levelname for r in caplog.records if "User style.css loaded" in r.message
        ] == ["DEBUG"]


class TestStateReflection:
    def test_recording_shows_pill_and_tray(self):
        app = _make_app()
        app._on_state_change(RecordingState.RECORDING)
        app._pill.show_recording.assert_called_once()
        app._tray.set_state.assert_called_once_with("recording")

    def test_transcribing_updates_pill_and_tray(self):
        app = _make_app()
        app._on_state_change(RecordingState.TRANSCRIBING)
        app._pill.show_transcribing.assert_called_once()
        app._tray.set_state.assert_called_once_with("transcribing")

    def test_idle_hides_pill(self):
        app = _make_app()
        app._on_state_change(RecordingState.IDLE)
        app._pill.hide_pill.assert_called_once()
        app._tray.set_state.assert_called_once_with("idle")


class TestAudioLevelRouting:
    """Levels also flow while a meter is open, so the pill is gated on recording."""

    def _app(self, state):
        app = _make_app()
        app._controller.state = state
        app._settings_window = None
        return app

    def test_pill_is_fed_while_recording(self):
        app = self._app(RecordingState.RECORDING)
        app._on_audio_level(0.5)
        app._pill.push_amplitude.assert_called_once_with(0.5)

    def test_pill_is_not_fed_while_idle(self):
        # The hazard: push_amplitude drops the pill out of its no-input hint, so an
        # ungated feed would animate a wave for a recording that isn't happening.
        app = self._app(RecordingState.IDLE)
        app._on_audio_level(0.5)
        app._pill.push_amplitude.assert_not_called()

    def test_pill_is_not_fed_while_transcribing(self):
        # Transcribing draws a generated wave; a live level would fight it.
        app = self._app(RecordingState.TRANSCRIBING)
        app._on_audio_level(0.5)
        app._pill.push_amplitude.assert_not_called()

    def test_settings_preview_is_fed_while_idle(self):
        app = self._app(RecordingState.IDLE)
        app._settings_window = _spec(SettingsWindow)
        app._on_audio_level(0.5)
        app._settings_window.push_level.assert_called_once_with(0.5)

    def test_both_are_fed_while_recording_with_settings_open(self):
        app = self._app(RecordingState.RECORDING)
        app._settings_window = _spec(SettingsWindow)
        app._on_audio_level(0.5)
        app._pill.push_amplitude.assert_called_once_with(0.5)
        app._settings_window.push_level.assert_called_once_with(0.5)

    def test_onboarding_meter_is_fed_while_it_is_open(self):
        app = self._app(RecordingState.IDLE)
        app._onboarding = _spec(OnboardingAssistant)
        app._on_audio_level(0.5)
        app._onboarding.push_level.assert_called_once_with(0.5)
        app._pill.push_amplitude.assert_not_called()


class TestInputMonitoringLifecycle:
    """The meter stops with Settings, so an idle app does not keep posting levels."""

    def test_starts_when_settings_opens(self):
        from quoth import stats

        app = _make_app()
        app._model_mgr = _spec(ModelManager)
        app._updates = _spec(UpdateNotifier)
        app._stats = dict(stats._DEFAULTS)
        with patch("quoth.app.SettingsWindow", autospec=True) as window:
            app._show_settings()
        app._controller.set_input_monitoring.assert_called_once_with(
            window.return_value, True
        )

    def test_stops_when_settings_closes(self):
        app = _make_app()
        window = app._settings_window = _spec(SettingsWindow)
        app._on_settings_closed(window)
        app._controller.set_input_monitoring.assert_called_once_with(window, False)
        assert app._settings_window is None


class TestErrorRouting:
    def test_proven_host_uinput_failure_offers_setup(self):
        app = _make_app()
        app._typing_setup_shown = False
        app._show_error = MagicMock()
        with patch("quoth.app.TypingSetupDialog", autospec=True) as dialog:
            app._on_typing_failed(InjectionResult.UINPUT_SETUP_REQUIRED)
        dialog.assert_called_once()
        assert dialog.call_args.kwargs["transcript_on_clipboard"] is True
        app._show_error.assert_not_called()
        with patch("quoth.app.TypingSetupDialog") as dialog:
            app._on_typing_failed(InjectionResult.UINPUT_SETUP_REQUIRED)
        dialog.assert_not_called()
        assert (
            app._show_error.call_args.kwargs["guidance"] == "Text is on your clipboard"
        )

    @pytest.mark.parametrize(
        ("result", "guidance", "on_clipboard"),
        [
            (InjectionResult.CLIPBOARD_ONLY, "Text is on your clipboard", True),
            (InjectionResult.NO_CHORD_SENDER, "Install wtype or ydotool", True),
            (InjectionResult.FAILED, "Try dictating again", False),
            (InjectionResult.NO_WAYLAND, "Needs a Wayland session", False),
            (InjectionResult.NO_WL_CLIPBOARD, "Install wl-clipboard", False),
        ],
    )
    def test_names_the_reason_and_where_the_text_is(
        self, result, guidance, on_clipboard
    ):
        app = _make_app()
        app._typing_setup_shown = False
        app._show_error = MagicMock()
        app._on_typing_failed(result)
        assert app._show_error.call_args.kwargs["guidance"] == guidance
        detail = app._show_error.call_args.args[1]
        assert ("your clipboard" in detail) is on_clipboard

    def test_explains_each_reason_once_with_troubleshooting(self):
        app = _make_app()
        app._typing_setup_shown = False
        app._show_error = MagicMock()
        troubleshooting = ("Troubleshooting", "app.troubleshooting")
        for result in (
            InjectionResult.CLIPBOARD_ONLY,
            InjectionResult.CLIPBOARD_ONLY,
            InjectionResult.FAILED,
        ):
            app._on_typing_failed(result)
        first, repeat, other = (c.kwargs for c in app._show_error.call_args_list)
        assert (first["essential"], first["button"]) == (True, troubleshooting)
        assert (repeat["essential"], repeat["button"]) == (False, troubleshooting)
        assert (other["essential"], other["button"]) == (True, troubleshooting)

    def test_a_repeat_notice_keeps_the_troubleshooting_button(self):
        # Without a pill every failure notifies, and each notice replaces the last.
        app = _make_app()
        app._typing_setup_shown = False
        app._notice = MagicMock()
        with patch("quoth.app.presentation.overlay_available", return_value=False):
            app._on_typing_failed(InjectionResult.CLIPBOARD_ONLY)
            app._on_typing_failed(InjectionResult.CLIPBOARD_ONLY)
        buttons = [c.kwargs["button"] for c in app._notice.call_args_list]
        assert buttons == [("Troubleshooting", "app.troubleshooting")] * 2

    def test_notification_carries_the_button(self):
        app = _make_app()
        app._notice = MagicMock()
        with patch("quoth.app.presentation.overlay_available", return_value=False):
            app._show_error("Paste failed", "Detail.", button=("Go", "app.go"))
        app._notice.assert_called_once_with(
            "quoth-error", "Quoth", "Detail.", button=("Go", "app.go")
        )

    def test_a_notice_carries_only_the_body_priority_and_button_it_is_given(self):
        app = _make_app()
        app.send_notification = MagicMock()
        bare, full = MagicMock(), MagicMock()
        with patch("quoth.app.Gio.Notification.new", side_effect=[bare, full]):
            app._notice("quoth-bare", "Quoth")
            app._notice(
                "quoth-full", "Quoth", "Body", low=True, button=("Go", "app.go")
            )
        assert bare.method_calls == []
        full.set_body.assert_called_once_with("Body")
        full.set_priority.assert_called_once_with(Gio.NotificationPriority.LOW)
        full.add_button.assert_called_once_with("Go", "app.go")
        assert app.send_notification.call_args_list == [
            call("quoth-bare", bare),
            call("quoth-full", full),
        ]

    def test_error_uses_pill_when_overlay_available(self):
        app = _make_app()
        app._notice = MagicMock()
        app._controller.state = RecordingState.IDLE
        with patch("quoth.app.presentation.overlay_available", return_value=True):
            app._show_error("boom")
        app._pill.show_error.assert_called_once_with("boom", guidance=None)
        app._notice.assert_not_called()

    @pytest.mark.parametrize(
        "state", [RecordingState.RECORDING, RecordingState.TRANSCRIBING]
    )
    def test_an_error_during_a_dictation_leaves_its_pill_and_notifies(self, state):
        # The dictation's pill would lose its view, then hide with the next state.
        app = _make_app()
        app._notice = MagicMock()
        app._controller.state = state
        with patch("quoth.app.presentation.overlay_available", return_value=True):
            app._show_error("Transcription failed", guidance="Try dictating again")
        app._pill.show_error.assert_not_called()
        app._notice.assert_called_once_with(
            "quoth-error",
            "Quoth",
            "Transcription failed. Try dictating again.",
            button=None,
        )

    def test_error_uses_notification_when_no_overlay(self):
        app = _make_app()
        app._notice = MagicMock()
        app._controller.state = RecordingState.IDLE
        with patch("quoth.app.presentation.overlay_available", return_value=False):
            app._show_error("boom", guidance="Try again")
        app._notice.assert_called_once_with(
            "quoth-error", "Quoth", "boom. Try again.", button=None
        )
        app._pill.show_error.assert_not_called()

    def test_orb_style_errors_tint_the_orb_and_notify(self):
        # The orb carries no text, so the words must reach a notification even
        # though the overlay is up and showing (tinting) the error.
        app = _make_app()
        app._notice = MagicMock()
        app._pill.shows_text = False
        app._controller.state = RecordingState.IDLE
        with patch("quoth.app.presentation.overlay_available", return_value=True):
            app._show_error("boom", guidance="Try again")
        app._pill.show_error.assert_called_once_with("boom", guidance="Try again")
        app._notice.assert_called_once_with(
            "quoth-error", "Quoth", "boom. Try again.", button=None
        )

    def test_essential_error_keeps_full_notification_detail(self):
        app = _make_app()
        app._notice = MagicMock()
        app._controller.state = RecordingState.IDLE
        with patch("quoth.app.presentation.overlay_available", return_value=True):
            app._show_error(
                "Model failed to load",
                "Re-download the model.",
                True,
                "Check model in Settings",
            )
        app._pill.show_error.assert_called_once_with(
            "Model failed to load", guidance="Check model in Settings"
        )
        app._notice.assert_called_once_with(
            "quoth-error", "Quoth", "Re-download the model.", button=None
        )

    def test_orb_style_no_input_hint_dims_and_notifies(self):
        app = _make_app()
        app._notify_status = MagicMock()
        app._pill.shows_text = False
        app._controller.state = RecordingState.RECORDING
        with patch("quoth.app.presentation.overlay_available", return_value=True):
            app._on_no_input()
        app._pill.show_hint.assert_called_once_with(
            "No input detected", guidance="Check your microphone"
        )
        app._notify_status.assert_called_once_with(
            "No input detected. Check your microphone.", low=False
        )

    def test_pill_style_no_input_hint_stays_on_the_pill(self):
        app = _make_app()
        app._notify_status = MagicMock()
        app._pill.shows_text = True
        app._controller.state = RecordingState.RECORDING
        with patch("quoth.app.presentation.overlay_available", return_value=True):
            app._on_no_input()
        app._pill.show_hint.assert_called_once_with(
            "No input detected", guidance="Check your microphone"
        )
        app._notify_status.assert_not_called()

    def test_no_overlay_no_input_uses_status_notification(self):
        # Normal priority: GNOME Shell shows no banner for a low-priority one.
        app = _make_app()
        del app._notify_status
        app._notice = MagicMock()
        app._controller.state = RecordingState.RECORDING
        with patch("quoth.app.presentation.overlay_available", return_value=False):
            app._on_no_input()
        app._pill.show_hint.assert_not_called()
        app._notice.assert_called_once_with(
            "quoth-status",
            "Quoth",
            "No input detected. Check your microphone.",
            low=False,
        )

    def test_overlay_style_setting_hot_applies(self, live_config):
        app = _make_app()
        live_config(app).overlay_style = "orb"
        app._pill.set_style.assert_called_once_with("orb")

    def test_sound_holds_its_output_only_while_enabled(self, live_config):
        app = _make_app()
        config = live_config(app)
        config.sound_feedback = "on"
        app._sound.prewarm.assert_called_once_with()
        config.sound_feedback = "off"
        app._sound.shutdown.assert_called_once_with()


class TestFeedback:
    def test_recording_fires_sound_and_notification_when_enabled(self):
        app = _make_app()
        app._config.sound_volume = 40
        with patch("quoth.app.presentation.feedback_enabled", return_value=True):
            app._on_state_change(RecordingState.RECORDING)
        app._sound.play.assert_called_once_with("start", app._config.sound_volume)
        app._notify_status.assert_called_once()

    def test_transcribing_plays_stop_cue(self):
        app = _make_app()
        app._config.sound_volume = 40
        with patch("quoth.app.presentation.feedback_enabled", return_value=True):
            app._on_state_change(RecordingState.TRANSCRIBING)
        app._sound.play.assert_called_once_with("stop", app._config.sound_volume)

    def test_no_feedback_when_disabled(self):
        app = _make_app()
        with patch("quoth.app.presentation.feedback_enabled", return_value=False):
            app._on_state_change(RecordingState.RECORDING)
        app._sound.play.assert_not_called()
        app._notify_status.assert_not_called()

    @pytest.mark.parametrize(
        ("sound", "notification", "plays", "notifies"),
        [("on", "off", True, False), ("off", "on", False, True)],
    )
    def test_each_cue_follows_its_own_setting(
        self, sound, notification, plays, notifies
    ):
        app = _make_app()
        app._config.sound_feedback = sound
        app._config.notification_feedback = notification
        app._on_state_change(RecordingState.RECORDING)
        assert app._sound.play.called is plays
        assert app._notify_status.called is notifies

    @pytest.mark.parametrize(
        ("state", "body"),
        [
            (RecordingState.RECORDING, "Recording…"),
            (RecordingState.TRANSCRIBING, "Transcribing…"),
        ],
    )
    def test_status_notification_names_the_state_in_words(self, state, body):
        # A screen reader would read a symbol's name before the state.
        app = _make_app()
        with patch("quoth.app.presentation.feedback_enabled", return_value=True):
            app._on_state_change(state)
        app._notify_status.assert_called_once_with(body)

    def test_idle_withdraws_status(self):
        app = _make_app()
        with patch("quoth.app.presentation.feedback_enabled", return_value=True):
            app._on_state_change(RecordingState.IDLE)
        app.withdraw_notification.assert_called_once_with("quoth-status")


def _command_line(*options, arguments=()):
    command_line = MagicMock()
    command_line.get_arguments.return_value = ["quoth", *arguments]
    command_line.get_options_dict.return_value.contains.side_effect = lambda name: (
        name in options
    )
    return command_line


def _stub_start(monkeypatch, app):
    """Stub the display, engine and next steps `_start` reaches, so a test can run it.

    `monkeypatch` fails the test on a stub for a name the app does not have.
    """
    app._pill = app._controller = app._gate = None
    app._theme = MagicMock()
    app._model_mgr = _spec(ModelManager)
    for name in (
        "_register_icons",
        "_reload_theme",
        "_load_user_css",
        "_watch_theme",
        "_apply_effective_device",
        "_finish_activation",
        "_show_onboarding",
    ):
        monkeypatch.setattr(app, name, MagicMock())
    monkeypatch.setattr(
        "quoth.app.ReducedMotionWatcher",
        lambda _fn: _spec(ReducedMotionWatcher, reduced=False),
    )
    monkeypatch.setattr("quoth.app.brand.install", MagicMock())
    monkeypatch.setattr("quoth.app.WaveformPill", create_autospec(WaveformPill))
    monkeypatch.setattr("quoth.app.environment.in_flatpak", lambda: False)
    monkeypatch.setattr(
        "quoth.app.RecordingController", create_autospec(RecordingController)
    )
    # Real settings would keep `_start`'s handler past the test.
    monkeypatch.setattr(Config, "connect_changed", MagicMock())


class TestActivation:
    """A launch, launcher action or notification click starts Quoth or reaches it."""

    def test_the_engine_comes_before_onboarding(self, monkeypatch):
        # Onboarding's guided take and benchmark run on the engine, and a setting
        # written there applies to it.
        app = _make_app()
        _stub_start(monkeypatch, app)
        app._config.onboarding_complete = False
        order = []
        monkeypatch.setattr(
            "quoth.app.RecordingController",
            lambda *_a, **_k: order.append("engine") or MagicMock(),
        )
        app._config.connect_changed.side_effect = lambda _fn: order.append("settings")
        app._show_onboarding.side_effect = lambda: order.append("onboarding")
        app._start()
        assert order == ["engine", "settings", "onboarding"]
        assert app._gate is not None
        app._finish_activation.assert_not_called()

    @pytest.mark.parametrize(
        ("onboarded", "flatpak", "reads"),
        [(True, False, True), (False, False, False), (True, True, False)],
    )
    def test_a_start_reads_the_autostart_entry_back_once_onboarded(
        self, monkeypatch, caplog, onboarded, flatpak, reads
    ):
        # Before onboarding writes it there is no entry to read; a Flatpak's is unseen.
        app = _make_app()
        _stub_start(monkeypatch, app)
        app._config.onboarding_complete = onboarded
        app._config.launch_at_login = True
        monkeypatch.setattr("quoth.app.environment.in_flatpak", lambda: flatpak)
        monkeypatch.setattr(
            "quoth.app.PortalShortcuts", create_autospec(PortalShortcuts)
        )
        monkeypatch.setattr("quoth.app.autostart.is_enabled", lambda: False)
        app._start()
        assert app._config.launch_at_login is (not reads)
        # The reset happens before changes are logged, so it says so itself.
        reset = "Launch at login set to False: the autostart entry is missing, hidden "
        assert any(m.startswith(reset) for m in caplog.messages) is reads

    @pytest.mark.parametrize(
        ("onboarded", "models", "onboards"),
        [(True, ["base"], False), (False, ["base"], True), (True, [], True)],
        ids=["set-up", "first-run", "every-model-deleted"],
    )
    def test_onboarding_runs_until_setup_is_done_with_a_model_on_disk(
        self, monkeypatch, onboarded, models, onboards
    ):
        monkeypatch.delenv("QUOTH_FORCE_ONBOARDING", raising=False)
        app = _make_app()
        _stub_start(monkeypatch, app)
        app._model_mgr.list_downloaded.return_value = models
        app._config.onboarding_complete = onboarded
        monkeypatch.setattr("quoth.app.autostart.is_enabled", lambda: True)
        app._start()
        assert app._show_onboarding.called is onboards
        assert app._finish_activation.called is not onboards

    def test_onboarding_writes_the_autostart_entry_when_it_completes(self):
        app = _make_app()
        app._model_mgr = _spec(ModelManager)
        app.hold = MagicMock()
        app._config.launch_at_login = True
        with (
            patch("quoth.app.OnboardingAssistant", autospec=True) as assistant,
            patch("quoth.app.autostart.set_enabled") as write,
        ):
            app._show_onboarding()
            write.assert_not_called()
            assistant.call_args.kwargs["on_complete"](MagicMock())
        write.assert_called_once_with(True, app._on_autostart_declined)

    def test_onboarding_hands_over_once_the_engine_is_free(self):
        app = _make_app()
        app._model_mgr = _spec(ModelManager)
        app._finish_activation = MagicMock()
        app.hold = app.release = MagicMock()
        with patch("quoth.app.OnboardingAssistant", autospec=True) as assistant:
            app._show_onboarding()
        kwargs = assistant.call_args.kwargs
        assert kwargs["engine"] is app._controller
        assert kwargs["start_benchmark"] == app._start_benchmark
        ready = MagicMock()
        assert app._onboarding is not None
        with patch("quoth.app.autostart.set_enabled"):
            kwargs["on_complete"](ready)
        # Finished setup no longer holds input or Settings; its window closes once the
        # startup load ends.
        assert app._onboarding is None
        app._finish_activation.assert_not_called()
        (activate,) = app._controller.when_idle.call_args.args
        activate()
        app._finish_activation.assert_called_once_with(on_ready=ready)

    @pytest.mark.parametrize("fails", [False, True])
    def test_quoth_stays_up_between_finish_and_a_deferred_activation(self, fails):
        # The onboarding window may close while a benchmark cell still holds the
        # engine; no tray exists yet, so only the hold keeps the app running.
        app = _make_app()
        app._model_mgr = _spec(ModelManager)
        calls = []
        app.hold = MagicMock(side_effect=lambda: calls.append("hold"))
        app.release = MagicMock(side_effect=lambda: calls.append("release"))
        app._controller.when_idle.side_effect = lambda _fn: calls.append("deferred")

        def finish(on_ready):
            calls.append("activation")
            if fails:
                raise RuntimeError("activation failed")

        app._finish_activation = MagicMock(side_effect=finish)
        with (
            patch("quoth.app.OnboardingAssistant", autospec=True) as assistant,
            patch("quoth.app.autostart.set_enabled"),
        ):
            app._show_onboarding()
            assistant.call_args.kwargs["on_complete"](MagicMock())
        assert calls == ["hold", "deferred"]
        (activate,) = app._controller.when_idle.call_args.args
        if fails:
            with pytest.raises(RuntimeError):
                activate()
        else:
            activate()
        assert calls == ["hold", "deferred", "activation", "release"]

    def test_reactivation_presents_the_open_window(self):
        app = _make_app()
        window = MagicMock()
        app.get_active_window = MagicMock(return_value=window)
        app._show_settings = MagicMock()
        app.do_activate()
        window.present.assert_called_once()
        app._show_settings.assert_not_called()

    def test_reactivation_without_a_window_opens_settings(self):
        app = _make_app()
        app.get_active_window = MagicMock(return_value=None)
        app._show_settings = MagicMock()
        app.do_activate()
        app._show_settings.assert_called_once()

    def test_the_first_user_launch_starts_then_opens_settings(self):
        # Without a tray (GNOME without AppIndicator) the window is the only sign.
        app = _make_app()
        calls = []
        app._start = MagicMock(side_effect=lambda: calls.append("start"))
        app.get_active_window = MagicMock(return_value=None)
        app._show_settings = MagicMock(side_effect=lambda: calls.append("settings"))
        app.do_activate()
        assert calls == ["start", "settings"]

    def test_a_started_app_starts_only_once(self):
        app = _make_app()
        app._register_icons = MagicMock()
        app._start()
        app._register_icons.assert_not_called()

    def test_a_background_launch_starts_without_a_window(self):
        app = _make_app()
        app._start = MagicMock()
        app.activate = MagicMock()
        app._show_settings = MagicMock()
        assert app.do_command_line(_command_line("background")) == 0
        app._start.assert_called_once()
        app.activate.assert_not_called()
        app._show_settings.assert_not_called()

    def test_quit_stops_the_instance_without_a_window(self):
        app = _make_app()
        app.quit = MagicMock()
        app.activate = MagicMock()
        assert app.do_command_line(_command_line("quit")) == 0
        app.quit.assert_called_once_with()
        app.activate.assert_not_called()

    def test_the_desktop_quits_a_running_instance_through_its_action(
        self, display, private_bus, monkeypatch
    ):
        # GNOME's app menu and Background Apps activate `quit` over D-Bus. An instance
        # in service mode that never got it would exit only after ten idle seconds.
        monkeypatch.setenv("DBUS_SESSION_BUS_ADDRESS", private_bus)
        instance = subprocess.Popen(
            [sys.executable, "-m", "quoth", "--gapplication-service"]
        )
        flags = (
            Gio.DBusConnectionFlags.AUTHENTICATION_CLIENT
            | Gio.DBusConnectionFlags.MESSAGE_BUS_CONNECTION
        )
        bus = Gio.DBusConnection.new_for_address_sync(private_bus, flags, None, None)

        def call(name, path, interface, method, args):
            return bus.call_sync(
                name, path, interface, method, args, None, 0, -1, None
            ).unpack()

        try:
            wait_until(
                lambda: (
                    instance.poll() is not None
                    or call(
                        "org.freedesktop.DBus",
                        "/org/freedesktop/DBus",
                        "org.freedesktop.DBus",
                        "NameHasOwner",
                        GLib.Variant("(s)", (ipc.APP_ID,)),
                    )[0]
                ),
                timeout=30,
            )
            assert instance.poll() is None
            call(
                ipc.APP_ID,
                ipc.OBJECT_PATH,
                "org.gtk.Actions",
                "Activate",
                GLib.Variant("(sava{sv})", ("quit", [], {})),
            )
            assert instance.wait(timeout=5) == 0
        finally:
            instance.kill()
            instance.wait()
            bus.close_sync(None)

    def test_toggle_goes_to_the_trigger_without_a_window(self):
        app = _make_app()
        app._request_toggle = MagicMock()
        app.activate = MagicMock()
        assert app.do_command_line(_command_line("toggle")) == 0
        app._request_toggle.assert_called_once_with()
        app.activate.assert_not_called()

    def test_a_plain_launch_activates(self):
        app = _make_app()
        app.activate = MagicMock()
        app._show_settings = MagicMock()
        assert app.do_command_line(_command_line()) == 0
        app.activate.assert_called_once()
        app._show_settings.assert_not_called()

    def test_settings_on_a_running_instance_opens_only_settings(self):
        app = _make_app()
        app.activate = MagicMock()
        app._show_settings = MagicMock()
        app.do_command_line(_command_line("settings"))
        app.activate.assert_not_called()
        app._show_settings.assert_called_once()

    def test_an_unexpected_argument_is_refused(self):
        app = _make_app()
        app.activate = MagicMock()
        command_line = _command_line(arguments=["foo"])
        assert app.do_command_line(command_line) == 1
        command_line.printerr_literal.assert_called_once_with(
            "quoth: unexpected argument foo\n"
        )
        app.activate.assert_not_called()

    def test_settings_on_first_launch_starts_then_opens_settings(self):
        app = _make_app()
        calls = []
        app._start = MagicMock(side_effect=lambda: calls.append("start"))
        app._show_settings = MagicMock(side_effect=lambda: calls.append("settings"))
        app.do_command_line(_command_line("settings"))
        assert calls == ["start", "settings"]

    def test_the_optimize_offer_waits_for_onboarding(self):
        # Its notification can outlive the session that showed it.
        app = _make_app()
        app._onboarding = _spec(OnboardingAssistant)
        app._open_benchmark = MagicMock()
        app._on_run_benchmark(None, None)
        app._onboarding.present.assert_called_once_with()
        app._open_benchmark.assert_not_called()

    def test_settings_waits_for_onboarding(self):
        # It would change the model, device or hotkey that setup is choosing.
        app = _make_app()
        app._onboarding = _spec(OnboardingAssistant)
        with patch("quoth.app.SettingsWindow") as window:
            app._show_settings()
        window.assert_not_called()
        app._onboarding.present.assert_called_once_with()

    @pytest.mark.parametrize(
        ("shortcuts", "listener", "shown"),
        [
            (
                _spec(PortalShortcuts, binding="Ctrl+Alt+D", status="needs-key"),
                None,
                ("Ctrl+Alt+D", "needs-key"),
            ),
            (
                None,
                _spec(KeyListener, state=ListenerState.AVAILABLE),
                ("KEY_F13", "tap-to-toggle", "CPU"),
            ),
            (None, None, ("quoth --toggle",)),
            (None, _spec(KeyListener, state=ListenerState.FAILED), ("quoth --toggle",)),
        ],
        ids=["portal", "listener", "toggle", "failed-listener"],
    )
    def test_the_ready_line_says_how_to_dictate(
        self, caplog, shortcuts, listener, shown
    ):
        app = _make_app()
        app._portal_shortcuts = shortcuts
        app._listener = listener
        app._config.hotkey = "KEY_F13"
        app._config.mode = TAP_TO_TOGGLE
        app._device = "CPU"
        app._log_ready("base", "CPU", ContextLoadOutcome.LOADED)
        (line,) = [r.getMessage() for r in caplog.records if r.levelname == "SUCCESS"]
        assert all(word in line for word in shown)


class TestRequestToggle:
    """The one routing seam behind `--toggle` and the `toggle` action."""

    def test_onboarding_gets_the_shortcut_confirmation(self):
        app = _make_app()
        app._onboarding = _spec(OnboardingAssistant)
        app._request_toggle()
        app._onboarding.notify_toggle_detected.assert_called_once()
        app._controller.start.assert_not_called()

    def test_live_app_flips_recording(self):
        app = _make_app()
        app._onboarding = None
        app._controller.state = RecordingState.IDLE
        app._request_toggle()
        app._controller.start.assert_called_once()

    def test_before_start_starts_without_a_window(self):
        # A window would take focus from the field the user is about to dictate into.
        app = _make_app()
        app._onboarding = None
        app._controller = None
        app._start = MagicMock()
        app.activate = MagicMock()
        app._request_toggle()
        app._start.assert_called_once()
        app.activate.assert_not_called()


class TestScriptCommands:
    """The script commands act like a tap or a click on the pill, never a toggle."""

    def _app(self, state=RecordingState.IDLE):
        app = _make_app()
        app._config.mode = PUSH_TO_HOLD
        app._controller.state = state
        return app

    def test_start_from_idle_records(self):
        app = self._app()

        def start():
            app._controller.state = RecordingState.RECORDING
            return True

        app._controller.start.side_effect = start
        assert app._script_start() is None
        app._controller.start.assert_called_once_with()

    def test_a_refused_start_says_so(self):
        app = self._app()
        app._controller.start.return_value = False
        assert app._script_start() == (
            "Dictation did not start; Quoth shows why on screen"
        )

    def test_start_while_recording_already_succeeds(self):
        app = self._app(RecordingState.RECORDING)
        assert app._script_start() is None
        app._controller.start.assert_not_called()
        app._controller.stop.assert_not_called()

    def test_start_while_transcribing_is_refused_and_cancels_nothing(self):
        app = self._app(RecordingState.TRANSCRIBING)
        assert app._script_start() == "Quoth is still transcribing the last dictation"
        app._controller.cancel.assert_not_called()

    def test_a_script_never_drives_onboarding(self):
        # Onboarding's shortcut step waits for the user's own press.
        app = self._app(RecordingState.RECORDING)
        app._onboarding = _spec(OnboardingAssistant)
        assert app._script_start() == "Finish setting up Quoth first"
        app._script_stop()
        app._script_cancel()
        app._onboarding.notify_toggle_detected.assert_not_called()
        app._controller.stop.assert_not_called()
        app._controller.cancel.assert_not_called()

    def test_stop_finishes_only_a_recording(self):
        for state in (RecordingState.IDLE, RecordingState.TRANSCRIBING):
            app = self._app(state)
            app._script_stop()
            app._controller.stop.assert_not_called()
            app._controller.cancel.assert_not_called()
        app = self._app(RecordingState.RECORDING)
        app._script_stop()
        app._controller.stop.assert_called_once_with()

    @pytest.mark.parametrize(
        "state", [RecordingState.RECORDING, RecordingState.TRANSCRIBING]
    )
    def test_cancel_discards_a_dictation_in_progress(self, state):
        app = self._app(state)
        app._script_cancel()
        app._controller.cancel.assert_called_once_with()

    def test_cancel_while_idle_changes_nothing(self):
        app = self._app()
        app._script_cancel()
        app._controller.cancel.assert_not_called()

    def test_the_bus_interface_drives_the_script_commands(self):
        app = self._app(RecordingState.RECORDING)
        with (
            patch("quoth.app.ipc.DictationService", autospec=True) as service,
            patch("quoth.app.Adw.Application.do_dbus_register", return_value=True),
            patch("quoth.app.Adw.Application.do_dbus_unregister"),
        ):
            assert app.do_dbus_register(MagicMock(), "/dev/quoth/Quoth")
            kwargs = service.call_args.kwargs
            assert kwargs["state"]() == "recording"
            assert (kwargs["start"], kwargs["stop"], kwargs["cancel"]) == (
                app._script_start,
                app._script_stop,
                app._script_cancel,
            )
            app.do_dbus_unregister(MagicMock(), "/dev/quoth/Quoth")
        service.return_value.close.assert_called_once_with()
        assert app._dictation_service is None

    def test_an_instance_not_yet_started_answers_scripts(self):
        # `--gapplication-service` registers on the bus without starting Quoth.
        app = _make_app()
        app._controller = None
        with (
            patch("quoth.app.ipc.DictationService", autospec=True) as service,
            patch("quoth.app.Adw.Application.do_dbus_register", return_value=True),
        ):
            app.do_dbus_register(MagicMock(), "/dev/quoth/Quoth")
        assert service.call_args.kwargs["state"]() == "idle"
        assert app._script_start() == "Quoth has not started yet"
        app._script_stop()
        app._script_cancel()

    def test_every_state_change_reaches_script_watchers(self):
        app = self._app()
        app._dictation_service = _spec(ipc.DictationService)
        for state in RecordingState:
            app._on_state_change(state)
        assert [c.args for c in app._dictation_service.publish.call_args_list] == [
            ("idle",),
            ("recording",),
            ("transcribing",),
        ]


class TestAutostartDeclined:
    @pytest.mark.parametrize(
        ("unsupported", "title"),
        [
            (False, "Launch at login was not allowed"),
            (True, "Launch at login isn't available"),
        ],
    )
    def test_a_declined_request_turns_the_setting_off(self, unsupported, title):
        app = _make_app()
        app._config.launch_at_login = True
        app.send_notification = MagicMock()
        with patch("quoth.app.Gio.Notification.new") as notification:
            app._on_autostart_declined(unsupported)
        assert app._config.launch_at_login is False
        notification.assert_called_once_with(title)
        app.send_notification.assert_called_once()

    def test_turning_it_off_withdraws_the_request(self, live_config):
        app = _make_app()
        config = live_config(app)
        app.send_notification = MagicMock()
        with patch("quoth.app.autostart.set_enabled") as request:
            app._on_autostart_declined()
        assert config.launch_at_login is False
        request.assert_called_once_with(False, app._on_autostart_declined)

    def test_a_decline_after_turning_it_off_changes_nothing(self):
        app = _make_app()
        app._config.launch_at_login = False
        app.send_notification = MagicMock()
        app._on_autostart_declined()
        app.send_notification.assert_not_called()


class TestTriggerPolicy:
    """Every trigger (listener, portal, `--toggle`) is decided by `_on_hotkey`."""

    def _app(self, mode=TAP_TO_TOGGLE, state=RecordingState.IDLE):
        app = _make_app()
        app._config.mode = mode
        app._controller.state = state
        return app

    @pytest.mark.parametrize("mode", [TAP_TO_TOGGLE, PUSH_TO_HOLD])
    def test_the_hotkey_stops_a_toggle_started_dictation(self, mode):
        app = self._app(mode)
        app._request_toggle()
        app._controller.start.assert_called_once()
        app._controller.state = RecordingState.RECORDING
        app._on_hotkey(True)
        app._on_hotkey(False)
        app._controller.stop.assert_called_once()

    @pytest.mark.parametrize("mode", [TAP_TO_TOGGLE, PUSH_TO_HOLD])
    def test_toggle_stops_a_hotkey_started_dictation(self, mode):
        app = self._app(mode)
        app._on_hotkey(True)
        app._controller.start.assert_called_once()
        app._controller.state = RecordingState.RECORDING
        app._request_toggle()
        app._controller.stop.assert_called_once()

    def test_tap_ignores_the_release(self):
        app = self._app(state=RecordingState.RECORDING)
        app._on_hotkey(False)
        app._controller.stop.assert_not_called()

    def test_hold_stops_on_release_only(self):
        app = self._app(PUSH_TO_HOLD, RecordingState.RECORDING)
        app._on_hotkey(True)
        app._controller.stop.assert_not_called()
        app._on_hotkey(False)
        app._controller.stop.assert_called_once()

    def test_a_release_after_idle_changes_nothing(self):
        app = self._app(PUSH_TO_HOLD)
        app._on_hotkey(False)
        app._controller.start.assert_not_called()
        app._controller.stop.assert_not_called()

    @pytest.mark.parametrize("mode", [TAP_TO_TOGGLE, PUSH_TO_HOLD])
    def test_a_press_cancels_transcription_before_the_gate(self, mode):
        app = self._app(mode, RecordingState.TRANSCRIBING)
        app._gate = _spec(PrepareGate)
        app._on_hotkey(True)
        app._controller.cancel.assert_called_once()
        app._on_hotkey(False)
        app._controller.cancel.assert_called_once()
        app._gate.refuse_start.assert_not_called()
        app._controller.start.assert_not_called()

    def test_toggle_is_a_tap_whatever_the_mode(self):
        app = self._app(PUSH_TO_HOLD, RecordingState.RECORDING)
        app._request_toggle()
        app._controller.stop.assert_called_once()

    def test_no_trigger_acts_once_shutdown_begins(self):
        app = self._app(state=RecordingState.RECORDING)
        app._shutting_down = True
        app._on_hotkey(True)
        app._on_hotkey(False)
        app._request_toggle()
        app._controller.stop.assert_not_called()
        app._controller.start.assert_not_called()

    def test_onboarding_takes_a_press_as_confirmation(self):
        app = self._app()
        app._onboarding = _spec(OnboardingAssistant)
        app._on_hotkey(True)
        app._on_hotkey(False)
        app._onboarding.notify_toggle_detected.assert_called_once()
        app._controller.start.assert_not_called()


def _prep_app(model="small", device="NPU"):
    """An app shell wired for the prepare-gate / device-resolution tests."""
    app = _make_app()
    app._config.model = model
    app._device = device
    app._settings_window = None
    app._model_mgr = _spec(ModelManager)
    app.withdraw_notification = MagicMock()
    app.send_notification = MagicMock()
    from quoth.prepare import PrepareGate

    app._gate = PrepareGate(
        app._controller,
        notify=app._notify_preparing,
        withdraw=lambda: app.withdraw_notification("quoth-preparing"),
    )
    return app


def _resolved(device):
    return BackendResolution(device, False, None)


def _stub_activation(monkeypatch, app):
    """Stub the tray and the steps `_finish_activation` and its startup load reach.

    `monkeypatch` fails the test on a stub for a name the app does not have.
    """
    app._updates = _spec(UpdateNotifier)
    for name in (
        "_start_listener",
        "_show_error",
        "_notice",
        "_maybe_offer_calibration",
        "_maybe_nudge_support",
        "hold",
    ):
        monkeypatch.setattr(app, name, MagicMock())
    monkeypatch.setattr(app, "_bundled_icon_dir", lambda: None)
    monkeypatch.setattr("quoth.app.QuothTray", create_autospec(QuothTray))
    monkeypatch.setattr("quoth.app.presentation.feedback_enabled", lambda _s: False)


class TestCalibrationNotices:
    def test_offer_describes_downloaded_models_and_is_saved_once(self):
        app = _make_app()
        app._config.calibration_offered = False
        app._notice = MagicMock()

        with patch("quoth.app.worth_offering", return_value=True):
            app._maybe_offer_calibration()
            app._maybe_offer_calibration()

        app._notice.assert_called_once_with(
            "quoth-calibrate",
            "Optimize Quoth for your hardware",
            "Benchmark your downloaded models to find the fastest measured device "
            "for each.",
            button=("Optimize", "app.run-benchmark"),
        )
        assert app._config.calibration_offered is True

    def test_ineligible_offer_does_not_mark_it_as_shown(self):
        app = _make_app()
        app._config.calibration_offered = False
        app._notice = MagicMock()

        with patch("quoth.app.worth_offering", return_value=False):
            app._maybe_offer_calibration()

        app._notice.assert_not_called()
        assert app._config.calibration_offered is False

    def test_preparation_notice_does_not_promise_a_one_time_compile(self):
        app = _make_app()
        app._notice = MagicMock()

        app._notify_preparing()

        app._notice.assert_called_once_with(
            "quoth-preparing",
            "Quoth",
            "Preparing NPU acceleration. This can take several minutes.",
        )


class TestPrepareGateWiring:
    """How the app drives the gate; `test_prepare.py` tests the gate's own rules."""

    def _app_with_gate(self, device):
        return _prep_app(device=device)

    def test_a_switch_away_keeps_the_gate_until_the_compile_lands(self):
        app = self._app_with_gate("CPU")
        app._controller.state = RecordingState.IDLE
        app._gate.raise_for("small", "NPU")  # the compile holds the engine
        with (
            patch("quoth.app.resolve_for", return_value=_resolved("CPU")),
            patch("quoth.app.expects_slow_compile", return_value=False),
        ):
            app._apply_effective_device()
        app._on_hotkey(True)
        app._controller.start.assert_not_called()
        app._gate.finish("small", "NPU", ContextLoadOutcome.LOADED)
        app._on_hotkey(True)
        app._controller.start.assert_called_once_with()

    def test_a_benchmark_waits_for_no_compile(self, caplog):
        app = self._app_with_gate("NPU")
        app._gate.raise_for("small", "NPU")
        assert (
            app._start_benchmark(["small"], ["NPU"], MagicMock(), MagicMock()) is False
        )
        assert "Benchmark refused: NPU preparation in progress" in caplog.messages
        app._controller.calibrate.assert_not_called()
        app._gate.finish("small", "NPU", ContextLoadOutcome.LOADED)
        app._controller.calibrate.return_value = True
        assert app._start_benchmark(["small"], ["NPU"], MagicMock(), MagicMock())
        app._controller.calibrate.assert_called_once()

    def test_switch_to_slow_device_prepares_it(self):
        app = self._app_with_gate("CPU")
        with (
            patch("quoth.app.resolve_for", return_value=_resolved("NPU")),
            patch("quoth.app.expects_slow_compile", return_value=True),
        ):
            app._apply_effective_device()
        app._controller.set_device.assert_called_once_with("NPU")
        assert app._gate.active
        app._controller.ensure_context_async.assert_called_once_with(
            "small", "NPU", app._gate.finish
        )

    def test_start_is_refused_while_preparing(self):
        app = self._app_with_gate("NPU")
        app._gate.raise_for("small", "NPU")
        app._controller.state = RecordingState.IDLE
        app._on_hotkey(True)
        app._controller.start.assert_not_called()

    @pytest.mark.parametrize(
        "outcome",
        [
            ContextLoadOutcome.LOADED,
            ContextLoadOutcome.FAILED,
            ContextLoadOutcome.MISSING,
        ],
    )
    def test_startup_follows_current_pair_and_only_reports_ready_on_success(
        self, caplog, monkeypatch, outcome
    ):
        app = self._app_with_gate("CPU")
        app._model_mgr.is_downloaded.return_value = (
            outcome is not ContextLoadOutcome.MISSING
        )
        _stub_activation(monkeypatch, app)
        controller = app._controller
        gate = app._gate = _spec(PrepareGate)
        callbacks = []
        controller.ensure_context_async.side_effect = lambda model, device, on_ready: (
            callbacks.append(on_ready)
        )
        ready = MagicMock()

        with (
            patch("quoth.app.init_typer", return_value=None),
            patch("quoth.app.prewarm_typer"),
            patch("quoth.app.expects_slow_compile", return_value=False),
        ):
            app._finish_activation(on_ready=ready)
            assert len(callbacks) == 1
            # Input is live before the model loads.
            app._start_listener.assert_called_once_with()

            app._config.model = "base"
            app._device = "GPU"
            callbacks[0]("small", "CPU", ContextLoadOutcome.FAILED)
            app._show_error.assert_not_called()
            ready.assert_not_called()
            assert "SUCCESS" not in [r.levelname for r in caplog.records]
            assert len(callbacks) == 2
            controller.ensure_context_async.assert_called_with(
                "base", "GPU", callbacks[1]
            )
            callbacks[1]("base", "GPU", outcome)
            levels = [r.levelname for r in caplog.records]
            assert ("SUCCESS" in levels) is outcome.loaded
            assert ("WARNING" in levels) is not outcome.loaded

        assert gate.finish.call_count == 2
        assert app._show_error.called is not outcome.loaded
        if outcome is ContextLoadOutcome.FAILED:
            # The same words a dictation that hits the failure gets.
            app._show_error.assert_called_once_with(*model_error(downloaded=True))
        app._start_listener.assert_called_once_with()
        ready.assert_called_once()


class TestStartupDownload:
    """Onboarding's download becomes the pending change and reads as downloading."""

    def _activate(
        self,
        monkeypatch,
        *,
        downloading,
        pending=None,
        downloaded=False,
        unavailable=None,
        slow=False,
    ):
        from quoth.settings import ModelSwitcher

        app = _prep_app(model="base", device="CPU")
        app._downloads.is_downloading.return_value = downloading
        app._model_mgr.is_downloaded.return_value = downloaded
        app._model_switcher = ModelSwitcher(
            app._config,
            app._model_mgr,
            app._downloads,
            reconcile=MagicMock(),
            notify=MagicMock(),
        )
        if pending is not None:
            app._model_switcher.request("model", pending, pending, "CPU")
        _stub_activation(monkeypatch, app)
        app._tray = None  # activation builds it
        loads = []
        app._controller.ensure_context_async.side_effect = lambda *a: loads.append(a)
        ready = MagicMock()
        with (
            patch("quoth.app.init_typer", return_value=unavailable),
            patch("quoth.app.prewarm_typer") as self.prewarm,
            patch("quoth.app.expects_slow_compile", return_value=slow),
        ):
            app._finish_activation(on_ready=ready)
        ((model, device, on_loaded),) = loads
        on_loaded(model, device, ContextLoadOutcome.MISSING)
        ready.assert_called_once_with()
        return app

    @pytest.mark.parametrize("unavailable", [None, InjectionResult.NO_WAYLAND])
    def test_the_paste_daemon_starts_only_where_paste_can_work(
        self, monkeypatch, unavailable
    ):
        self._activate(
            monkeypatch, downloading=False, downloaded=True, unavailable=unavailable
        )
        assert self.prewarm.called is (unavailable is None)

    def test_a_session_that_cannot_paste_is_told_at_startup(self, monkeypatch):
        app = self._activate(
            monkeypatch,
            downloading=False,
            downloaded=True,
            unavailable=InjectionResult.NO_WAYLAND,
        )
        ((args, kwargs),) = app._show_error.call_args_list
        assert args[0] == "Can't paste dictations"
        assert kwargs["essential"] is True
        assert app._paste_failures_explained == {InjectionResult.NO_WAYLAND}

    def test_a_session_that_needs_uinput_is_offered_the_setup_at_startup(
        self, monkeypatch
    ):
        with patch("quoth.app.TypingSetupDialog", autospec=True) as dialog:
            app = self._activate(
                monkeypatch,
                downloading=False,
                downloaded=True,
                unavailable=InjectionResult.UINPUT_SETUP_REQUIRED,
            )
        # No dictation has run, so no transcript waits on the clipboard.
        assert dialog.call_args.kwargs["transcript_on_clipboard"] is False
        app._show_error.assert_not_called()

    def test_a_startup_compile_gates_recording_with_a_notice(self, monkeypatch):
        app = self._activate(monkeypatch, downloading=False, downloaded=True, slow=True)
        app._notice.assert_any_call(
            "quoth-preparing",
            "Quoth",
            "Preparing NPU acceleration. This can take several minutes.",
        )
        # Only a gate raised for the startup pair comes down with its load.
        app.withdraw_notification.assert_called_once_with("quoth-preparing")

    def test_activation_starts_audio_and_a_tray_that_keeps_quoth_running(
        self, monkeypatch
    ):
        app = self._activate(monkeypatch, downloading=False, downloaded=True)
        app._controller.prepare_audio.assert_called_once_with()
        app._tray.start.assert_called_once_with()
        app.hold.assert_called_once_with()

    def test_the_startup_load_ends_with_the_update_check_and_offers(self, monkeypatch):
        app = self._activate(monkeypatch, downloading=False, downloaded=True)
        app._updates.maybe_check.assert_called_once_with()
        app._maybe_offer_calibration.assert_called_once_with()
        app._maybe_nudge_support.assert_called_once_with()

    def test_a_download_for_a_model_on_disk_does_not_block_dictation(self):
        # A pending encoder download leaves the model's weights usable meanwhile.
        app = _prep_app()
        app._model_switcher.pending = object()
        app._controller.model_available.return_value = True
        app._notify_downloading = MagicMock()
        assert app._refuse_start() is False
        app._notify_downloading.assert_not_called()

    def test_the_startup_load_says_it_is_downloading(self, monkeypatch):
        app = self._activate(monkeypatch, downloading=True)
        assert app._model_switcher.pending.model == "base"
        app._show_error.assert_not_called()
        app._notice.assert_called_once_with(
            "quoth-model", "Quoth", "Base is still downloading"
        )

    def test_a_change_already_pending_is_kept(self, monkeypatch):
        # A model picked in Settings during onboarding is the change that will land.
        app = self._activate(monkeypatch, downloading=True, pending="small")
        assert app._model_switcher.pending.model == "small"
        app._downloads.start_or_join.assert_called_once()
        app._notice.assert_called_once_with(
            "quoth-model", "Quoth", "Small is still downloading"
        )

    def test_a_missing_model_with_no_download_is_reported(self, monkeypatch):
        app = self._activate(monkeypatch, downloading=False)
        app._downloads.start_or_join.assert_not_called()
        app._show_error.assert_called_with(*model_error(downloaded=False))

    def test_files_that_landed_before_the_result_are_not_missing(self, monkeypatch):
        # The load saw no files, then the transfer finished ahead of its result.
        app = self._activate(monkeypatch, downloading=False, downloaded=True)
        app._show_error.assert_not_called()

    def test_a_press_while_it_downloads_says_why_and_records_once_it_lands(
        self, monkeypatch, caplog
    ):
        app = self._activate(monkeypatch, downloading=True)
        app._notice.reset_mock()
        app._controller.state = RecordingState.IDLE
        app._controller.model_available.return_value = False
        app._on_hotkey(True, TAP_TO_TOGGLE)
        app._controller.start.assert_not_called()
        app._notice.assert_called_once_with(
            "quoth-model", "Quoth", "Base is still downloading"
        )
        assert "Dictation refused: the model is still downloading" in caplog.messages
        app._downloads.start_or_join.call_args.kwargs["on_done"]()
        app._controller.model_available.return_value = True
        app._on_hotkey(True, TAP_TO_TOGGLE)
        app._controller.start.assert_called_once_with()


class TestDegradeNotice:
    """An explicit device that cannot be honoured says why, in Settings or not."""

    REASON = "This model's NPU encoder isn't downloaded; using GPU instead"

    @pytest.mark.parametrize("window", [False, True])
    def test_a_degraded_choice_is_explained(self, window):
        app = _prep_app(device="CPU")
        app._notice = MagicMock()
        app._settings_window = _spec(SettingsWindow) if window else None
        res = BackendResolution("GPU", True, self.REASON)
        with (
            patch("quoth.app.resolve_for", return_value=res),
            patch("quoth.app.expects_slow_compile", return_value=False),
        ):
            app._apply_effective_device()
        if window:
            app._settings_window.show_toast.assert_called_once_with(self.REASON)
            app._notice.assert_not_called()
        else:
            # The load fallback's id: a newer note on the device in use replaces it.
            app._notice.assert_called_once_with(
                "quoth-acceleration", "Quoth", self.REASON, low=True
            )

    def test_an_honoured_choice_says_nothing(self):
        app = _prep_app(device="CPU")
        app._notice = MagicMock()
        with (
            patch("quoth.app.resolve_for", return_value=_resolved("GPU")),
            patch("quoth.app.expects_slow_compile", return_value=False),
        ):
            app._apply_effective_device()
        app._controller.set_device.assert_called_once_with("GPU")
        app._notice.assert_not_called()

    def test_an_npu_choice_whose_encoder_failed_is_explained_once(
        self, live_config, detected_devices
    ):
        # The weights land without the encoder; Settings hears only the degrade.
        from quoth.downloads import DownloadCoordinator
        from quoth.settings import ModelSwitcher, SettingsView
        from tests.support import FakeModelManager, FakeScheduler

        detected_devices.extend(["NPU", "GPU"])
        app = _make_app()
        config = live_config(app)
        config.model, config.device = "small", "CPU"
        app._model_mgr = FakeModelManager(ggml=True, encoder=False)
        app._controller.when_idle.side_effect = lambda fit: fit()
        toasts = []
        app._settings_window = _spec(SettingsWindow)
        app._settings_window.show_toast.side_effect = toasts.append

        def lands_without_encoder(*_args, on_done, **_kwargs):
            on_done()

        downloads = DownloadCoordinator(
            "/models", FakeScheduler(), download=lands_without_encoder
        )
        switcher = ModelSwitcher(
            config,
            app._model_mgr,
            downloads,
            reconcile=app._reconcile_engine,
            notify=MagicMock(),
        )
        switcher.attach(
            SettingsView(
                MagicMock(), MagicMock(), toasts.append, MagicMock(), MagicMock()
            )
        )
        with patch("quoth.resolution.calibration.current_ranking", return_value=None):
            assert switcher.select("device", "NPU")
        assert config.device == "NPU"
        assert toasts == [self.REASON]


class TestEngineReconcile:
    """Setting changes reach the engine and the update check.

    A deleted model keeps its setting, so a reconcile must free the stale context.
    """

    def test_absent_model_frees_context(self):
        app = _prep_app()
        app._model_mgr.is_downloaded.return_value = False
        with (
            patch("quoth.app.resolve_for", return_value=_resolved("CPU")),
            patch("quoth.app.expects_slow_compile", return_value=False),
        ):
            app._fit_engine()
        app._controller.free_context.assert_called_once()

    def test_present_model_does_not_free_context(self):
        app = _prep_app()
        app._model_mgr.is_downloaded.return_value = True
        with (
            patch("quoth.app.resolve_for", return_value=_resolved("CPU")),
            patch("quoth.app.expects_slow_compile", return_value=False),
        ):
            app._fit_engine()
        app._controller.free_context.assert_not_called()

    def test_it_waits_for_the_engine_to_be_free(self):
        # A download landing mid-dictation must not start a compile that the
        # dictation's transcription would wait behind.
        app = _prep_app()
        app._reconcile_engine()
        app._controller.when_idle.assert_called_once_with(app._fit_engine)

    def test_a_model_or_device_change_reconciles(self, live_config):
        app = _make_app()
        app._reconcile_engine = MagicMock()
        config = live_config(app)
        config.model = "small"
        config.device = "CPU"
        config.language = "de"  # read when recording starts
        assert app._reconcile_engine.call_count == 2

    @pytest.mark.parametrize(
        ("key", "value", "method"),
        [
            ("keep_warm", False, "apply_keep_warm"),
            ("keep_warm_timeout", 60, "reschedule_warm_timeout"),
            ("mic_source", "alsa_input.usb", "apply_mic_source"),
        ],
    )
    def test_an_engine_setting_reaches_the_engine(
        self, live_config, key, value, method
    ):
        app = _make_app()
        setattr(live_config(app), key, value)
        getattr(app._controller, method).assert_called_once_with()

    def test_a_vad_change_reopens_the_context_once_the_engine_is_free(
        self, live_config
    ):
        # The context is built with or without VAD, so it must not outlive the change.
        app = _make_app()
        live_config(app).vad = False
        app._controller.when_idle.assert_called_once_with(app._controller.free_context)

    def test_turning_update_checks_on_checks_at_once(self, live_config):
        app = _make_app()
        app._updates = _spec(UpdateNotifier)
        config = live_config(app)
        config.notify_updates = True
        config.notify_updates = False
        app._updates.maybe_check.assert_called_once_with(force=True)


class TestDictationRecording:
    """A successful paste bumps the local usage count and persists it."""

    def test_increments_and_persists(self, tmp_path):
        from quoth import stats

        app = _make_app()
        app._stats = dict(stats._DEFAULTS)
        app._stats_path = tmp_path / "stats.json"
        app._on_dictation_success()
        assert app._stats["dictations_total"] == 1
        assert stats.load(app._stats_path)["dictations_total"] == 1


class TestSupportNudge:
    """The support nudge fires once the dictation count earns it, and only once."""

    def _app(self, state, tmp_path):
        app = _make_app()
        app._stats = state
        app._stats_path = tmp_path / "stats.json"
        app.send_notification = MagicMock()
        return app

    def test_fires_when_due_and_marks_shown(self, tmp_path):
        from quoth import stats

        app = self._app(
            {**stats._DEFAULTS, "dictations_total": stats.NUDGE_THRESHOLD}, tmp_path
        )
        app._maybe_nudge_support()
        app.send_notification.assert_called_once()
        assert app._stats["nudge_shown"] is True
        # Persisted, so a restart won't re-nudge.
        assert stats.load(app._stats_path)["nudge_shown"] is True

    def test_silent_below_threshold(self, tmp_path):
        from quoth import stats

        app = self._app(
            {**stats._DEFAULTS, "dictations_total": stats.NUDGE_THRESHOLD - 1}, tmp_path
        )
        app._maybe_nudge_support()
        app.send_notification.assert_not_called()

    def test_silent_once_shown(self, tmp_path):
        from quoth import stats

        app = self._app(
            {
                **stats._DEFAULTS,
                "dictations_total": stats.NUDGE_THRESHOLD,
                "nudge_shown": True,
            },
            tmp_path,
        )
        app._maybe_nudge_support()
        app.send_notification.assert_not_called()


class TestUsageSinceLabel:
    """The Settings "using since" label: the first-seen month and year, or None."""

    def test_none_before_first_dictation(self):
        from quoth import stats

        app = _make_app()
        app._stats = dict(stats._DEFAULTS)
        assert app._usage_since_label() is None

    def test_formats_month_year(self):
        from quoth import stats

        app = _make_app()
        app._stats = {**stats._DEFAULTS, "first_seen": "2026-07-17"}
        assert app._usage_since_label() == "July 2026"

    def test_none_when_unparseable(self):
        from quoth import stats

        app = _make_app()
        app._stats = {**stats._DEFAULTS, "first_seen": "not-a-date"}
        assert app._usage_since_label() is None


class TestTrayModelSwitch:
    def test_switches_by_the_same_rule_as_settings(self):
        # The switcher fetches what the device needs and refuses during a download.
        app = _make_app()
        app._change_model("small")
        app._model_switcher.select.assert_called_once_with("model", "small")


class TestPortalRecordingBridges:
    def _app(self, mode=TAP_TO_TOGGLE):
        app = _make_app()
        app._portal_shortcuts = _spec(PortalShortcuts)
        app._config.mode = mode
        app._controller.state = RecordingState.IDLE
        app._notice = MagicMock()
        return app

    def test_flatpak_does_not_start_native_listener(self):
        app = self._app()
        app._listener = None
        with (
            patch("quoth.app.evdev_available", return_value=True),
            patch("quoth.app.KeyListener") as listener,
        ):
            app._start_listener()
        listener.assert_not_called()

    @pytest.mark.parametrize("phase", ["onboarding", "shutdown"])
    def test_a_lost_session_is_not_reported_while_onboarding_or_quitting(self, phase):
        app = self._app(PUSH_TO_HOLD)
        app._controller.state = RecordingState.RECORDING
        app._portal_shortcuts.status = "closed"
        if phase == "onboarding":
            app._onboarding = _spec(OnboardingAssistant)
        else:
            app._shutting_down = True
        app._on_portal_lost()
        app._controller.stop.assert_not_called()
        app._notice.assert_not_called()

    @pytest.mark.parametrize("status", ["closed", "unbound"])
    def test_lost_session_ends_a_held_dictation_and_reports_setup(self, status):
        app = self._app(PUSH_TO_HOLD)
        app._controller.state = RecordingState.RECORDING
        app._portal_shortcuts.status = status
        app._on_portal_lost()
        app._controller.stop.assert_called_once()
        app._notice.assert_called_once()

    def test_lost_session_leaves_a_tap_dictation_running(self):
        app = self._app()
        app._controller.state = RecordingState.RECORDING
        app._on_portal_lost()
        app._controller.stop.assert_not_called()
        app._controller.cancel.assert_not_called()

    def test_rebind_does_not_warn_about_setup(self):
        app = self._app(PUSH_TO_HOLD)
        app._portal_shortcuts.status = "ready"
        app._on_portal_lost()
        app._notice.assert_not_called()


class TestCopyLastDictation:
    @pytest.mark.parametrize(("copied", "told"), [(True, False), (False, True)])
    def test_a_failed_copy_is_shown(self, copied, told):
        app = _make_app()
        app._controller.last_text = "hello world"
        app._show_error = MagicMock()

        class Inline:
            def __init__(self, target, name, daemon):
                self.target = target

            def start(self):
                self.target()

        with (
            patch("quoth.app.copy_text", return_value=copied) as copy,
            patch("quoth.app.threading.Thread", Inline),
            patch("quoth.app.GLib.idle_add", side_effect=lambda fn, *a: fn(*a)),
        ):
            app._copy_last_dictation()
        copy.assert_called_once_with("hello world")
        assert app._show_error.called is told


class TestPortalIntent:
    def test_binding_approval_survives_restart(self, tmp_path):
        app = _make_app()
        app._portal_shortcuts = _spec(PortalShortcuts, status="ready")
        app._on_portal_status()
        assert Config().portal_shortcut_enabled is True
        app._portal_shortcuts.status = "denied"
        app._on_portal_status()
        assert Config().portal_shortcut_enabled is False

    def test_a_shortcut_without_a_key_is_rebound_at_login(self):
        app = _make_app()
        app._config.portal_shortcut_enabled = False
        app._portal_shortcuts = _spec(PortalShortcuts, status="needs-key")
        app._on_portal_status()
        assert app._config.portal_shortcut_enabled is True

    def test_transient_error_does_not_erase_previous_intent(self):
        app = _make_app()
        app._notice = MagicMock()
        app._config.portal_shortcut_enabled = True
        for status in ("checking", "requesting", "unavailable", "error"):
            app._portal_shortcuts = _spec(PortalShortcuts, status=status)
            app._on_portal_status()
        assert app._config.portal_shortcut_enabled is True

    def test_a_shortcut_that_fails_to_restore_asks_for_setup(self):
        app = _make_app()
        app._notice = MagicMock()
        app._config.portal_shortcut_enabled = True
        app._portal_shortcuts = _spec(PortalShortcuts, status="error")
        app._on_portal_status()
        app._notice.assert_called_once_with(
            "quoth-shortcut",
            "Shortcut needs setup",
            "Set up your dictation shortcut again in Settings.",
        )

    @pytest.mark.parametrize("shown_by", ["settings", "onboarding", "no shortcut"])
    def test_a_failure_elsewhere_or_without_a_shortcut_sends_no_notice(self, shown_by):
        app = _make_app()
        app._notice = MagicMock()
        app._config.portal_shortcut_enabled = shown_by != "no shortcut"
        app._settings_window = _spec(SettingsWindow) if shown_by == "settings" else None
        app._onboarding = (
            _spec(OnboardingAssistant) if shown_by == "onboarding" else None
        )
        app._portal_shortcuts = _spec(PortalShortcuts, status="error")
        app._on_portal_status()
        app._notice.assert_not_called()

    def test_unexpected_close_requires_explicit_setup_after_restart(self):
        app = _make_app()
        app._config.portal_shortcut_enabled = True
        app._portal_shortcuts = _spec(PortalShortcuts, status="closed")
        app._on_portal_status()
        assert app._config.portal_shortcut_enabled is False

    def test_removed_binding_is_not_restored_on_restart(self, tmp_path):
        app = _make_app()
        app._portal_shortcuts = _spec(PortalShortcuts, status="ready")
        app._on_portal_status()
        app._portal_shortcuts.status = "unbound"
        app._on_portal_status()
        assert Config().portal_shortcut_enabled is False

    def test_shutdown_stops_every_input_before_recording(self):
        app = _make_app()
        app._portal_shortcuts = _spec(PortalShortcuts)
        calls = []
        app._portal_shortcuts.close.side_effect = lambda: calls.append("shortcuts")
        app._listener.stop.side_effect = lambda: calls.append("listener")
        app._tray.stop.side_effect = lambda: calls.append("tray")
        app._controller.shutdown.side_effect = lambda: calls.append("recording")
        with (
            patch("quoth.app.Adw.Application.do_shutdown"),
        ):
            app.do_shutdown()
        assert sorted(calls[:-1]) == ["listener", "shortcuts", "tray"]
        assert calls[-1] == "recording"
        assert app._shutting_down
        # Natively no shortcut session owns the hotkey, so only shutdown keeps it off.
        app._portal_shortcuts = None
        with (
            patch("quoth.app.evdev_available", return_value=True),
            patch("quoth.app.KeyListener") as listener,
        ):
            app._start_listener()
        listener.assert_not_called()

    def test_shutdown_stops_the_paste_daemon_once_recording_has_stopped(self):
        app = _make_app()
        calls = []
        app._controller.shutdown.side_effect = lambda: calls.append("recording")
        with (
            patch("quoth.app.Adw.Application.do_shutdown"),
            patch(
                "quoth.app.shutdown_typer", side_effect=lambda: calls.append("typer")
            ),
        ):
            app.do_shutdown()
        assert calls == ["recording", "typer"]

    def test_shutdown_withdraws_the_notices_about_work_it_ends(self):
        app = _make_app()
        with patch("quoth.app.Adw.Application.do_shutdown"):
            app.do_shutdown()
        withdrawn = {c.args[0] for c in app.withdraw_notification.call_args_list}
        assert withdrawn == {
            "quoth-status",
            "quoth-preparing",
            "quoth-benchmark",
            "quoth-hotkey-hold",
        }

    def test_a_termination_signal_quits(self, caplog):
        import signal

        app = _make_app()
        app.quit = MagicMock()
        # Shutdown removes the source, and removing one already gone is a GLib critical.
        assert app._quit_on_signal(signal.SIGTERM) is GLib.SOURCE_CONTINUE
        app.quit.assert_called_once_with()
        assert "Received SIGTERM; quitting" in caplog.messages

    def test_shutdown_first_gives_the_signals_back(self):
        # A signal then ends a stuck shutdown at once, as it would without Quoth's.
        app = _make_app()
        app._signal_sources = [7, 8]
        app._portal_shortcuts = _spec(PortalShortcuts)
        calls = []
        app._portal_shortcuts.close.side_effect = lambda: calls.append("inputs")
        with (
            patch("quoth.app.Adw.Application.do_shutdown"),
            patch(
                "quoth.app.GLib.source_remove",
                side_effect=lambda source: calls.append(source),
            ),
        ):
            app.do_shutdown()
        assert calls[:3] == [7, 8, "inputs"]


def test_main_names_the_application_for_notifications():
    # GLib otherwise names it by the program name, which stays lower case. GLib keeps
    # the first name a process sets, so the test process sets none.
    from quoth import app as app_module

    steps = MagicMock()
    with (
        patch.object(app_module.GLib, "set_application_name", steps.name),
        patch("quoth.app.QuothApp", steps.app),
    ):
        steps.app.return_value.run.return_value = 0
        assert app_module.main() == 0

    assert steps.mock_calls[:2] == [call.name("Quoth"), call.app()]
