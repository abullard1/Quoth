# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 Samuel Ruairí Bullard

"""Tests for `quoth.listener`, the native evdev hotkey listener."""

import os
import threading
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import MagicMock, patch

import pytest
from evdev import InputDevice, InputEvent, ecodes

from quoth.listener import (
    KeyListener,
    ListenerState,
    evdev_available,
    find_keyboards,
    find_keyboards_for_keys,
    key_name_for_scancode,
    keyboard_available,
    scancode_for_key_name,
)
from quoth.trigger import PUSH_TO_HOLD, TAP_TO_TOGGLE, hotkey_action
from tests.support import wait_until


@pytest.fixture(autouse=True)
def _fast_select(monkeypatch):
    # A stop waits out the read loop's select, so a short one keeps each test quick.
    monkeypatch.setattr("quoth.listener._SELECT_TIMEOUT_S", 0.01)


def _accept(listener: KeyListener) -> None:
    with listener._lock:
        listener._accepting = True


def _press(keycode: int, usec: int = 0):
    return InputEvent(0, usec, ecodes.EV_KEY, keycode, 1)


def _release(keycode: int, usec: int = 500_000):
    # Half a second after `_press`: a held key, unless a test says otherwise.
    return InputEvent(0, usec, ecodes.EV_KEY, keycode, 0)


def _syn(code: int):
    return InputEvent(0, 0, ecodes.EV_SYN, code, 0)


def _unplug(device: "_PipeDevice") -> None:
    # The real key-state call fails on the fake's pipe as on an unplugged device.
    device.fd = device.fileno()
    device.active_keys = lambda: InputDevice.active_keys(device)


def _stop_while_blocked(listener, release):
    """Stop the listener from another thread while its worker is blocked, then free it.

    Not a `with` block: the executor's shutdown would wait out a hung stop() rather
    than fail at the deadline.
    """
    stopping = ThreadPoolExecutor(1).submit(listener.stop)
    wait_until(listener._stopping.is_set)
    release.set()
    assert stopping.result(5) is True


class _PipeDevice:
    """A selectable evdev fake with controllable events and failures."""

    __hash__ = None

    def __init__(
        self,
        name: str,
        read_error: Exception | None = None,
        keys=(ecodes.KEY_F13, ecodes.KEY_PAUSE),
    ):
        self.name = name
        self.keys = list(keys)
        self.path = f"/dev/input/{name}"
        self._r, self._w = os.pipe()
        self._events: list[InputEvent] = []
        self._read_error = read_error
        self._active_keys: list[int] = []
        self.closed = False

    def fileno(self) -> int:
        return self._r

    def push(self, *events: InputEvent) -> None:
        for event in events:  # the kernel's key state, which active_keys reads
            if event.type != ecodes.EV_KEY:
                continue
            if event.value == 1:
                self._active_keys.append(event.code)
            elif event.value == 0 and event.code in self._active_keys:
                self._active_keys.remove(event.code)
        self._events.extend(events)
        os.write(self._w, b"x")

    def read(self):
        os.read(self._r, 1)
        if self._read_error is not None:
            error, self._read_error = self._read_error, None
            raise error
        events, self._events = self._events, []
        return iter(events)

    def active_keys(self):
        return self._active_keys

    def capabilities(self):
        return {ecodes.EV_KEY: self.keys}

    def close(self) -> None:
        self.closed = True
        for fd in (self._r, self._w):
            try:
                os.close(fd)
            except OSError:
                pass


class TestFindKeyboards:
    def _devices(self, mock_evdev, names):
        devices = {}
        for index, name in enumerate(names):
            device = MagicMock()
            device.name = name
            device.path = f"/dev/input/event{index}"
            device.capabilities.return_value = {ecodes.EV_KEY: [ecodes.KEY_F13]}
            devices[device.path] = device
        mock_evdev.list_devices.return_value = list(devices)
        mock_evdev.InputDevice.side_effect = lambda path: devices[path]
        return list(devices.values())

    @patch("quoth.listener.evdev")
    def test_a_remappers_keyboard_is_read_beside_the_physical_one(self, mock_evdev):
        # keyd ignores the keyboards no config matches, so their presses stay physical.
        laptop, keyd = self._devices(
            mock_evdev, ["AT Translated Set 2 keyboard", "keyd virtual keyboard"]
        )

        assert find_keyboards(ecodes.KEY_F13) == [laptop, keyd]
        laptop.close.assert_not_called()
        keyd.close.assert_not_called()

    @patch("quoth.listener.evdev")
    def test_all_keyboards_return_without_keyd(self, mock_evdev):
        laptop, external, mouse = self._devices(
            mock_evdev, ["Laptop", "USB Keyboard", "Mouse"]
        )
        mouse.capabilities.return_value = {ecodes.EV_REL: []}

        assert find_keyboards(ecodes.KEY_F13) == [laptop, external]
        mouse.close.assert_called_once()
        laptop.close.assert_not_called()
        external.close.assert_not_called()

    @patch("quoth.listener.evdev")
    def test_chord_discovery_includes_modifier_and_main_key_devices(self, mock_evdev):
        modifier, main, unrelated = self._devices(
            mock_evdev, ["Modifier", "Main key", "Unrelated"]
        )
        modifier.capabilities.return_value = {ecodes.EV_KEY: [ecodes.KEY_LEFTCTRL]}
        unrelated.capabilities.return_value = {ecodes.EV_KEY: [ecodes.KEY_A]}

        assert find_keyboards_for_keys({ecodes.KEY_LEFTCTRL, ecodes.KEY_F13}) == [
            modifier,
            main,
        ]
        unrelated.close.assert_called_once()

    @patch("quoth.listener.evdev")
    def test_a_device_without_the_key_is_not_a_keyboard(self, mock_evdev):
        # A gamepad or mouse reports EV_KEY buttons but can never send the hotkey.
        gamepad, keyboard = self._devices(mock_evdev, ["Gamepad", "Keyboard"])
        gamepad.capabilities.return_value = {ecodes.EV_KEY: [ecodes.BTN_SOUTH]}

        assert find_keyboards(ecodes.KEY_F13) == [keyboard]
        gamepad.close.assert_called_once()

    @patch("quoth.listener.evdev")
    def test_known_paths_are_not_opened_again(self, mock_evdev):
        held, new = self._devices(mock_evdev, ["Held", "New"])

        assert find_keyboards(ecodes.KEY_F13, known={held.path}) == [new]
        opened = [c.args[0] for c in mock_evdev.InputDevice.call_args_list]
        assert opened == [new.path]

    @patch("quoth.listener.evdev")
    def test_per_device_probe_errors_close_only_that_handle(
        self, mock_evdev, monkeypatch, caplog
    ):
        monkeypatch.setattr("quoth.listener._unreadable", set())
        broken, keyboard = self._devices(mock_evdev, ["Broken", "Keyboard"])
        broken.capabilities.side_effect = OSError("gone")

        assert find_keyboards(ecodes.KEY_F13) == [keyboard]
        broken.close.assert_called_once()
        keyboard.close.assert_not_called()
        # Kept in the file log, once per device however often the rescan meets it.
        find_keyboards(ecodes.KEY_F13)
        [record] = [r for r in caplog.records if "input device" in r.getMessage()]
        assert record.levelname == "DEBUG"
        assert record.getMessage() == (
            "Could not inspect input device /dev/input/event0: gone"
        )

    @patch("quoth.listener.evdev")
    def test_open_error_does_not_abort_other_devices(
        self, mock_evdev, monkeypatch, caplog
    ):
        monkeypatch.setattr("quoth.listener._unreadable", set())
        keyboard = MagicMock(
            name="Keyboard",
            path="/dev/input/event1",
            capabilities=MagicMock(return_value={ecodes.EV_KEY: [ecodes.KEY_F13]}),
        )
        mock_evdev.list_devices.return_value = ["bad", "good"]
        mock_evdev.InputDevice.side_effect = [PermissionError("denied"), keyboard]

        assert find_keyboards(ecodes.KEY_F13) == [keyboard]
        assert "Could not open input device bad: denied" in caplog.messages

    @patch("quoth.listener.evdev")
    def test_empty_probe_returns_nothing(self, mock_evdev):
        mock_evdev.list_devices.return_value = []
        assert find_keyboards(ecodes.KEY_F13) == []

    def test_availability_probe_closes_every_selected_handle(self):
        first, second = MagicMock(), MagicMock()
        with patch(
            "quoth.listener.find_keyboards", return_value=[first, second]
        ) as find:
            assert keyboard_available("KEY_PAUSE") is True
        find.assert_called_once_with(ecodes.KEY_PAUSE)
        first.close.assert_called_once()
        second.close.assert_called_once()

    def test_availability_probe_handles_global_discovery_error(self):
        with patch("quoth.listener.find_keyboards", side_effect=OSError("missing")):
            assert keyboard_available("KEY_PAUSE") is False

    def test_availability_probe_rejects_an_unknown_key(self):
        with patch("quoth.listener.find_keyboards") as find:
            assert keyboard_available("KEY_NOPE") is False
        find.assert_not_called()

    def test_availability_probe_handles_missing_optional_evdev(self):
        with (
            patch("quoth.listener.evdev", None),
            patch("quoth.listener.find_keyboards") as find,
        ):
            assert keyboard_available("KEY_PAUSE") is False
        find.assert_not_called()


class TestMappingsAndGuards:
    def test_evdev_availability_tracks_import(self):
        assert evdev_available() is True
        with patch("quoth.listener.evdev", None):
            assert evdev_available() is False

    def test_listener_rejects_missing_evdev(self):
        with patch("quoth.listener.evdev", None):
            with pytest.raises(RuntimeError, match="evdev"):
                KeyListener(hotkey="KEY_F13")

    def test_key_name_accepts_tuple_aliases_from_evdev_1_6(self):
        with patch("quoth.listener.ecodes.KEY", {999: ("KEY_FIRST", "KEY_ALIAS")}):
            assert key_name_for_scancode(999) == "KEY_FIRST"

    def test_key_name_and_scancode_round_trip(self):
        for token in ("KEY_A", "KEY_Y", "KEY_BACKSLASH", "KEY_F13"):
            assert key_name_for_scancode(scancode_for_key_name(token)) == token
        assert key_name_for_scancode(999999) is None
        assert scancode_for_key_name("KEY_NOPE") is None

    @pytest.mark.parametrize("token", ["KEY", "EV_KEY", "SYN_REPORT", "ecodes"])
    def test_names_that_are_not_keys_are_unknown(self, token):
        assert scancode_for_key_name(token) is None

    def test_a_button_name_is_a_key(self):
        # A button, such as a mouse's side button, can be the hotkey too.
        assert scancode_for_key_name("BTN_SIDE") == ecodes.BTN_SIDE


class TestDispatch:
    def test_one_press_at_a_time_and_each_is_released_once(self):
        edges = []
        listener = KeyListener(
            hotkey="KEY_F13",
            on_press=lambda: edges.append("down"),
            on_release=lambda: edges.append("up"),
        )
        _accept(listener)

        listener._press()
        listener._press()
        listener._release()
        listener._release()
        listener._press()

        assert edges == ["down", "up", "down"]

    def test_release_must_come_from_the_held_source(self):
        edges = []
        listener = KeyListener(
            hotkey="KEY_F13",
            on_press=lambda: edges.append("down"),
            on_release=lambda: edges.append("up"),
        )
        _accept(listener)
        first, second = object(), object()

        listener._press(first)
        listener._press(second)
        assert edges == ["down"]
        listener._release(second)
        assert edges == ["down"]
        listener._release(first)
        assert edges == ["down", "up"]

    def test_nonmatching_and_nonkey_events_are_ignored(self):
        started = []
        listener = KeyListener(hotkey="KEY_F13", on_press=lambda: started.append(1))
        _accept(listener)

        listener._handle_event(_press(listener._keycode + 1))
        listener._handle_event(InputEvent(0, 0, ecodes.EV_MSC, 0, 0))

        assert started == []

    def test_modifier_chord_can_span_keyboards_and_releases_on_modifier_up(self):
        edges = []
        listener = KeyListener(
            hotkey="CTRL+KEY_F13",
            on_press=lambda: edges.append("down"),
            on_release=lambda: edges.append("up"),
        )
        _accept(listener)
        modifiers, function_key = object(), object()

        listener._handle_event(_press(ecodes.KEY_LEFTCTRL), modifiers)
        listener._handle_event(_press(ecodes.KEY_F13), function_key)
        assert edges == ["down"]

        listener._handle_event(_release(ecodes.KEY_LEFTCTRL), modifiers)
        assert edges == ["down", "up"]

    def test_modifier_chord_does_not_fire_without_its_modifier(self):
        started = []
        listener = KeyListener(
            hotkey="CTRL+KEY_F13", on_press=lambda: started.append(True)
        )
        _accept(listener)

        listener._handle_event(_press(ecodes.KEY_F13))

        assert started == []

    def test_modifier_chord_ignores_extra_modifiers(self):
        edges = []
        listener = KeyListener(
            hotkey="CTRL+KEY_F13",
            on_press=lambda: edges.append("down"),
            on_release=lambda: edges.append("up"),
        )
        _accept(listener)
        source = object()

        listener._handle_event(_press(ecodes.KEY_LEFTCTRL), source)
        listener._handle_event(_press(ecodes.KEY_LEFTSHIFT), source)
        listener._handle_event(_press(ecodes.KEY_F13), source)
        assert edges == []

        listener._handle_event(_release(ecodes.KEY_LEFTSHIFT), source)
        assert edges == []
        listener._handle_event(_release(ecodes.KEY_F13), source)
        listener._handle_event(_press(ecodes.KEY_F13), source)
        assert edges == ["down"]
        listener._handle_event(_release(ecodes.KEY_LEFTCTRL), source)

        assert edges == ["down", "up"]

    def test_quick_modifier_release_does_not_mark_the_main_key_unholdable(self):
        listener = KeyListener(hotkey="CTRL+KEY_F13")
        _accept(listener)
        source = object()

        listener._handle_event(_press(ecodes.KEY_LEFTCTRL), source)
        listener._handle_event(_press(ecodes.KEY_F13), source)
        listener._handle_event(_release(ecodes.KEY_LEFTCTRL, usec=10_000), source)

        assert not listener.unholdable

    def test_autorepeat_keeps_a_held_press(self):
        # The kernel repeats a held key as value 2: neither a press nor a release.
        edges = []
        listener = KeyListener(
            hotkey="KEY_F13",
            on_press=lambda: edges.append("down"),
            on_release=lambda: edges.append("up"),
        )
        _accept(listener)

        listener._handle_event(_press(ecodes.KEY_F13))
        listener._handle_event(InputEvent(0, 0, ecodes.EV_KEY, ecodes.KEY_F13, 2))

        assert edges == ["down"]

    def test_suspended_is_a_scalar_gate(self):
        started = []
        listener = KeyListener(hotkey="KEY_F13", on_press=lambda: started.append(1))
        _accept(listener)

        listener.suspended = True
        listener._press()
        assert started == []
        listener.suspended = False
        listener._press()
        assert started == [1]

    def test_a_press_held_into_suspension_is_still_released(self):
        edges = []
        listener = KeyListener(
            hotkey="KEY_F13",
            on_press=lambda: edges.append("down"),
            on_release=lambda: edges.append("up"),
        )
        _accept(listener)

        listener._press()
        listener.suspended = True
        listener._release()
        listener.suspended = False
        listener._press()

        assert edges == ["down", "up", "down"]

    def test_rebind_watches_the_new_key_from_the_next_event(self):
        edges = []
        listener = KeyListener(hotkey="KEY_F13", on_press=lambda: edges.append(1))
        _accept(listener)

        listener.rebind("KEY_F14")
        listener._handle_event(_press(ecodes.KEY_F13))
        assert edges == []
        listener._handle_event(_press(ecodes.KEY_F14))

        assert edges == [1]

    def test_rebind_releases_a_held_press_of_the_old_key(self):
        edges = []
        listener = KeyListener(
            hotkey="KEY_F13",
            on_press=lambda: edges.append("down"),
            on_release=lambda: edges.append("up"),
        )
        _accept(listener)

        listener._handle_event(_press(ecodes.KEY_F13))
        listener.rebind("KEY_F14")
        listener._handle_event(_release(ecodes.KEY_F13))
        listener._handle_event(_press(ecodes.KEY_F14))

        assert edges == ["down", "up", "down"]

    def test_rebind_to_an_unknown_key_keeps_the_old_one(self):
        edges = []
        listener = KeyListener(hotkey="KEY_F13", on_press=lambda: edges.append(1))
        _accept(listener)

        with pytest.raises(ValueError, match="KEY_NOPE"):
            listener.rebind("KEY_NOPE")
        listener._handle_event(_press(ecodes.KEY_F13))

        assert edges == [1]

    def test_stop_is_immediately_nonaccepting(self):
        edges = []
        listener = KeyListener(hotkey="KEY_F13", on_press=lambda: edges.append(1))
        _accept(listener)

        assert listener.stop() is True
        listener._press()

        assert edges == []

    def test_disconnect_releases_the_held_key(self):
        released = []
        device = _PipeDevice("keyboard")
        listener = KeyListener(hotkey="KEY_F13", on_release=lambda: released.append(1))
        _accept(listener)
        listener._devices = [device]
        listener._press(device)

        listener._drop_device(device)

        assert released == [1]
        assert listener._accepting is False


class TestWorkerLifecycle:
    def test_no_readable_input_has_no_worker_or_waiting_state(self, monkeypatch):
        states = []
        monkeypatch.setattr("quoth.listener.find_keyboards", lambda _keycode: [])
        monkeypatch.setattr("quoth.listener.evdev.list_devices", lambda: [])
        listener = KeyListener(
            hotkey="KEY_F13",
            on_state_change=lambda state: states.append(state),
        )

        assert listener.start() is False
        assert listener._thread is None
        assert listener.state is ListenerState.STOPPED
        assert states == []

    def test_start_opens_the_keyboards_that_can_send_the_hotkey(self, monkeypatch):
        states = []
        keyboard = _PipeDevice("keyboard", keys=[ecodes.KEY_F13])
        monkeypatch.setattr(
            "quoth.listener.evdev.list_devices", lambda: [keyboard.path]
        )
        monkeypatch.setattr("quoth.listener.evdev.InputDevice", lambda _path: keyboard)
        listener = KeyListener(
            hotkey="KEY_F13", on_state_change=lambda state: states.append(state)
        )

        assert listener.start() is True
        wait_until(lambda: states)
        try:
            assert states == [ListenerState.AVAILABLE]
        finally:
            listener.stop()

    def test_a_keyboard_that_connects_after_start_is_picked_up(
        self, monkeypatch, caplog
    ):
        # Readable input that cannot send the key yet, such as the power button while
        # a Bluetooth keyboard is still connecting.
        keyboard = _PipeDevice("keyboard")
        presses = []
        scans = iter([[], [keyboard]])
        monkeypatch.setattr(
            "quoth.listener.find_keyboards",
            lambda *_args, **_kwargs: next(scans, []),
        )
        monkeypatch.setattr(
            "quoth.listener.evdev.list_devices", lambda: ["/dev/input/event0"]
        )
        monkeypatch.setattr("quoth.listener._RESCAN_S", 0.05)
        listener = KeyListener(hotkey="KEY_F13", on_press=lambda: presses.append(1))
        try:
            assert listener.start() is True
            wait_until(lambda: listener.state is ListenerState.AVAILABLE)
            keyboard.push(_press(ecodes.KEY_F13))
            wait_until(lambda: presses)
        finally:
            listener.stop()
        assert "No keyboard can send KEY_F13; waiting for one" in caplog.messages
        assert [m for m in caplog.messages if m.startswith("Hotkey listener:")] == [
            "Hotkey listener: WAITING",
            "Hotkey listener: AVAILABLE",
            "Hotkey listener: STOPPED",
        ]

    def test_thread_start_failure_closes_discovered_handles(self):
        device = _PipeDevice("keyboard")
        listener = KeyListener(hotkey="KEY_F13")

        with patch.object(threading.Thread, "start", side_effect=RuntimeError("limit")):
            with pytest.raises(RuntimeError, match="limit"):
                listener.start([device])

        assert device.closed
        assert listener._thread is None

    def test_available_only_after_real_selector_registration(self):
        states = []
        device = _PipeDevice("keyboard")
        listener = KeyListener(
            hotkey="KEY_F13",
            on_state_change=lambda state: states.append(state),
        )

        assert listener.start([device]) is True
        wait_until(lambda: states)
        try:
            assert states == [ListenerState.AVAILABLE]
            assert listener._accepting
        finally:
            assert listener.stop() is True
        assert device.closed

    def test_any_registered_keyboard_can_fire(self):
        starts = []
        first, second = _PipeDevice("laptop"), _PipeDevice("external")
        listener = KeyListener(hotkey="KEY_PAUSE", on_press=lambda: starts.append(1))
        assert listener.start([first, second]) is True
        wait_until(lambda: listener.state is ListenerState.AVAILABLE)
        try:
            second.push(_press(ecodes.KEY_PAUSE))
            wait_until(lambda: starts)
            assert starts == [1]
        finally:
            listener.stop()

    def test_a_press_and_its_release_arrive_through_the_read_loop(self):
        # A real device ends each batch of events with a SYN_REPORT.
        edges = []
        device = _PipeDevice("keyboard")
        listener = KeyListener(
            hotkey="KEY_F13",
            on_press=lambda: edges.append("down"),
            on_release=lambda: edges.append("up"),
        )
        assert listener.start([device]) is True
        wait_until(lambda: listener.state is ListenerState.AVAILABLE)
        try:
            device.push(_press(ecodes.KEY_F13), _syn(ecodes.SYN_REPORT))
            device.push(_release(ecodes.KEY_F13), _syn(ecodes.SYN_REPORT))
            wait_until(lambda: len(edges) == 2)
            assert edges == ["down", "up"]
        finally:
            listener.stop()

    def test_surviving_keyboard_does_not_block_partial_reconnect(self, monkeypatch):
        starts = []
        laptop = _PipeDevice("laptop")
        duplicate_laptop = _PipeDevice("laptop")
        external = _PipeDevice("external")
        scans = []

        def rescan(keycode, known=()):
            scans.append((keycode, set(known)))
            candidates = [duplicate_laptop, external] if len(scans) == 1 else []
            return [d for d in candidates if d.path not in known]

        monkeypatch.setattr("quoth.listener.find_keyboards", rescan)
        monkeypatch.setattr("quoth.listener._RESCAN_S", 0.05)
        listener = KeyListener(hotkey="KEY_F13", on_press=lambda: starts.append(1))
        listener._handle_event(_syn(ecodes.SYN_DROPPED), laptop)
        assert listener.start([laptop]) is True
        wait_until(lambda: listener.state is ListenerState.AVAILABLE)
        wait_until(lambda: external in listener._devices)
        try:
            assert listener._devices == [laptop, external]
            assert id(laptop) in listener._dropped_sources
            assert scans[0] == (ecodes.KEY_F13, {laptop.path})
            assert laptop.closed is False
            external.push(_press(ecodes.KEY_F13))
            wait_until(lambda: starts)
            assert starts == [1]
        finally:
            listener.stop()
            duplicate_laptop.close()

    def test_a_remappers_arrival_keeps_the_physical_keyboard(self, monkeypatch):
        started = []
        physical = _PipeDevice("physical")
        keyd = _PipeDevice("keyd virtual keyboard")
        scans = []

        def rescan(*_args, **_kwargs):
            scans.append(1)
            return [keyd] if len(scans) == 1 else []

        monkeypatch.setattr("quoth.listener.find_keyboards", rescan)
        monkeypatch.setattr("quoth.listener._RESCAN_S", 0.01)
        listener = KeyListener(hotkey="KEY_F13", on_press=lambda: started.append(1))
        assert listener.start([physical]) is True
        try:
            wait_until(lambda: listener._devices == [physical, keyd])
            physical.push(_press(ecodes.KEY_F13))
            wait_until(lambda: started)
            assert physical.closed is False
        finally:
            listener.stop()

    def test_a_held_key_released_out_of_sight_ends_at_the_rescan(self, monkeypatch):
        # A grab taken mid-press, a remapper starting, keeps the key-up from Quoth.
        edges = []
        physical = _PipeDevice("physical")
        scans = []

        def rescan(*_args, **_kwargs):
            scans.append(1)
            return []

        monkeypatch.setattr("quoth.listener.find_keyboards", rescan)
        monkeypatch.setattr("quoth.listener._RESCAN_S", 0.01)
        listener = KeyListener(
            hotkey="KEY_F13",
            on_press=lambda: edges.append("down"),
            on_release=lambda: edges.append("up"),
        )
        assert listener.start([physical]) is True
        try:
            wait_until(lambda: listener.state is ListenerState.AVAILABLE)
            physical.push(_press(ecodes.KEY_F13))
            wait_until(lambda: edges)
            pressed_at = len(scans)
            # Rescans while the key is still down keep the press.
            wait_until(lambda: len(scans) >= pressed_at + 2)
            assert edges == ["down"]
            physical._active_keys.clear()
            wait_until(lambda: len(edges) == 2)
            assert edges == ["down", "up"]
        finally:
            listener.stop()

    def test_a_held_device_gone_at_the_rescan_is_dropped(self, monkeypatch):
        edges = []
        gone, other = _PipeDevice("gone"), _PipeDevice("other")
        monkeypatch.setattr("quoth.listener.find_keyboards", lambda *_a, **_k: [])
        monkeypatch.setattr("quoth.listener._RESCAN_S", 0.01)
        listener = KeyListener(
            hotkey="KEY_F13",
            on_press=lambda: edges.append("down"),
            on_release=lambda: edges.append("up"),
        )
        assert listener.start([gone, other]) is True
        try:
            wait_until(lambda: listener.state is ListenerState.AVAILABLE)
            _unplug(gone)
            gone.push(_press(ecodes.KEY_F13))
            wait_until(lambda: edges == ["down", "up"])
            assert listener._devices == [other]
            assert listener.state is ListenerState.AVAILABLE
        finally:
            listener.stop()

    def test_a_resync_on_an_unplugged_device_drops_it(self, monkeypatch):
        gone, other = _PipeDevice("gone"), _PipeDevice("other")
        monkeypatch.setattr("quoth.listener.find_keyboards", lambda *_a, **_k: [])
        listener = KeyListener(hotkey="KEY_F13")
        assert listener.start([gone, other]) is True
        try:
            wait_until(lambda: listener.state is ListenerState.AVAILABLE)
            _unplug(gone)
            gone.push(_syn(ecodes.SYN_DROPPED), _syn(ecodes.SYN_REPORT))
            wait_until(lambda: listener._devices == [other])
            assert listener.state is ListenerState.AVAILABLE
        finally:
            listener.stop()

    def test_a_failed_registration_keeps_the_open_keyboard(self, monkeypatch):
        physical = _PipeDevice("physical")
        broken = MagicMock(path="/dev/input/broken")
        broken.name = "broken"
        broken.fileno.return_value = -1
        scans = []

        def rescan(*_args, **_kwargs):
            scans.append(1)
            return [broken] if len(scans) == 1 else []

        monkeypatch.setattr("quoth.listener.find_keyboards", rescan)
        monkeypatch.setattr("quoth.listener._RESCAN_S", 0.01)
        listener = KeyListener(hotkey="KEY_F13")
        assert listener.start([physical]) is True
        wait_until(lambda: broken.close.called)
        try:
            assert listener._devices == [physical]
            assert physical.closed is False
            assert listener.state is ListenerState.AVAILABLE
        finally:
            listener.stop()

    def test_discovery_failure_preserves_live_handle_and_logs_transitions(
        self, monkeypatch, caplog
    ):
        device = _PipeDevice("keyboard")
        scans = []

        def rescan(*_args, **_kwargs):
            scans.append(1)
            if len(scans) <= 2:
                raise OSError("temporary")
            return []

        monkeypatch.setattr("quoth.listener.find_keyboards", rescan)
        monkeypatch.setattr("quoth.listener._RESCAN_S", 0.01)
        listener = KeyListener(hotkey="KEY_F13")
        caplog.set_level("INFO")
        assert listener.start([device]) is True
        # The fifth scan starts once the fourth, a second healthy one, has finished.
        wait_until(lambda: len(scans) >= 5)
        try:
            assert listener._devices == [device]
            assert device.closed is False
            assert [(r.levelname, r.getMessage()) for r in caplog.records] == [
                ("WARNING", "Hotkey device discovery failed: temporary"),
                ("INFO", "Hotkey device discovery recovered"),
            ]
        finally:
            listener.stop()

    def test_stop_during_partial_scan_closes_unpublished_handle(self, monkeypatch):
        device = _PipeDevice("keyboard")
        found = _PipeDevice("found")
        entered, release = threading.Event(), threading.Event()

        def rescan(*_args, **_kwargs):
            entered.set()
            release.wait(5)
            return [found]

        monkeypatch.setattr("quoth.listener.find_keyboards", rescan)
        monkeypatch.setattr("quoth.listener._RESCAN_S", 0.01)
        listener = KeyListener(hotkey="KEY_F13")
        assert listener.start([device]) is True
        assert entered.wait(1)

        _stop_while_blocked(listener, release)

        assert found.closed
        assert device.closed

    def test_stop_during_failed_scan_does_not_log_health_change(
        self, monkeypatch, caplog
    ):
        device = _PipeDevice("keyboard")
        entered, release = threading.Event(), threading.Event()

        def rescan(*_args, **_kwargs):
            entered.set()
            release.wait(5)
            raise OSError("temporary")

        monkeypatch.setattr("quoth.listener.find_keyboards", rescan)
        monkeypatch.setattr("quoth.listener._RESCAN_S", 0.01)
        listener = KeyListener(hotkey="KEY_F13")
        caplog.set_level("INFO")
        assert listener.start([device]) is True
        assert entered.wait(1)
        _stop_while_blocked(listener, release)

        assert caplog.records == []

    def test_stop_before_discovery_deadline_does_not_start_scan(self, monkeypatch):
        device = _PipeDevice("keyboard")
        scan = MagicMock(return_value=[])
        monkeypatch.setattr("quoth.listener.find_keyboards", scan)
        monkeypatch.setattr("quoth.listener._RESCAN_S", 10.0)
        listener = KeyListener(hotkey="KEY_F13")
        assert listener.start([device]) is True
        wait_until(lambda: listener.state is ListenerState.AVAILABLE)

        assert listener.stop() is True
        scan.assert_not_called()

    def test_rebind_drops_devices_that_cannot_send_the_new_key(self, monkeypatch):
        states = []
        f13_only = _PipeDevice("macro pad", keys=[ecodes.KEY_F13])
        both = _PipeDevice("keyboard", keys=[ecodes.KEY_F13, ecodes.KEY_PAUSE])
        monkeypatch.setattr(
            "quoth.listener.find_keyboards", lambda *_args, **_kwargs: []
        )
        monkeypatch.setattr("quoth.listener._RESCAN_S", 0.01)
        listener = KeyListener(
            hotkey="KEY_F13", on_state_change=lambda state: states.append(state)
        )
        assert listener.start([f13_only, both]) is True
        wait_until(lambda: listener.state is ListenerState.AVAILABLE)
        try:
            listener.rebind("KEY_PAUSE")
            wait_until(lambda: f13_only.closed)
            assert listener._devices == [both]
            listener.rebind("KEY_F14")
            wait_until(lambda: listener.state is ListenerState.WAITING)
            assert both.closed
        finally:
            listener.stop()

    def test_registration_value_error_drops_one_device(self):
        bad = MagicMock(path="/dev/input/bad", fileno=MagicMock(return_value=-1))
        good = _PipeDevice("good")
        listener = KeyListener(hotkey="KEY_F13")

        assert listener.start([bad, good]) is True
        wait_until(lambda: listener.state is ListenerState.AVAILABLE)
        try:
            bad.close.assert_called_once()
            assert listener._devices == [good]
        finally:
            listener.stop()

    def test_blocking_io_error_keeps_device_registered(self):
        starts = []
        device = _PipeDevice("keyboard", read_error=BlockingIOError())
        listener = KeyListener(hotkey="KEY_F13", on_press=lambda: starts.append(1))
        listener.start([device])
        wait_until(lambda: listener.state is ListenerState.AVAILABLE)
        try:
            device.push(_press(ecodes.KEY_F13))
            wait_until(lambda: device._read_error is None)  # the read raised
            device.push(_press(ecodes.KEY_F13))
            wait_until(lambda: starts)
            assert starts == [1]
            assert device.closed is False
        finally:
            listener.stop()

    def test_disconnect_waits_then_resumes(self, monkeypatch):
        states = []
        gone = _PipeDevice("gone", read_error=OSError("gone"))
        unplugged_fd = gone.fileno()

        def rescan(_keycode, known=()):
            # Opened here, as find_keyboards opens it, the keyboard plugged back in
            # takes the lowest free descriptor: the one its unplugged handle gave up.
            return [_PipeDevice("back")] if gone.closed and not known else []

        monkeypatch.setattr("quoth.listener.find_keyboards", rescan)
        monkeypatch.setattr("quoth.listener._RESCAN_S", 0.01)
        listener = KeyListener(
            hotkey="KEY_F13",
            on_state_change=lambda state: states.append(state),
        )
        listener.start([gone])
        wait_until(lambda: listener.state is ListenerState.AVAILABLE)
        try:
            gone.push(_press(ecodes.KEY_F13))
            wait_until(lambda: ListenerState.WAITING in states)
            wait_until(lambda: states.count(ListenerState.AVAILABLE) == 2)
            assert states == [
                ListenerState.AVAILABLE,
                ListenerState.WAITING,
                ListenerState.AVAILABLE,
            ]
            assert listener._devices[0].fileno() == unplugged_fd
        finally:
            listener.stop()

    def test_stop_during_rescan_closes_newly_found_handles(self, monkeypatch):
        gone = _PipeDevice("gone", read_error=OSError("gone"))
        found = _PipeDevice("found")
        entered, release = threading.Event(), threading.Event()

        def rescan(*_args, **_kwargs):
            entered.set()
            release.wait(5)
            return [found]

        monkeypatch.setattr("quoth.listener.find_keyboards", rescan)
        monkeypatch.setattr("quoth.listener._RESCAN_S", 0.01)
        listener = KeyListener(hotkey="KEY_F13")
        listener.start([gone])
        wait_until(lambda: listener.state is ListenerState.AVAILABLE)
        gone.push(_press(ecodes.KEY_F13))
        assert entered.wait(1)

        _stop_while_blocked(listener, release)

        assert found.closed

    def test_timed_out_stop_prevents_replacement(self, monkeypatch):
        device = _PipeDevice("blocked")
        entered, release = threading.Event(), threading.Event()
        states = []
        original_read = device.read

        def blocked_read():
            entered.set()
            release.wait(5)
            return original_read()

        device.read = blocked_read
        monkeypatch.setattr("quoth.listener._JOIN_TIMEOUT_S", 0.01)
        listener = KeyListener(
            hotkey="KEY_F13",
            on_state_change=lambda state: states.append((state, device.closed)),
        )
        listener.start([device])
        wait_until(lambda: listener.state is ListenerState.AVAILABLE)
        device.push(_press(ecodes.KEY_F13))
        assert entered.wait(1)

        assert listener.stop() is False
        assert listener.state is ListenerState.STOPPED
        assert states == [(ListenerState.AVAILABLE, False)]
        assert device.closed is False
        replacement = _PipeDevice("replacement")
        assert listener.start([replacement]) is False
        assert replacement.closed is False

        release.set()
        wait_until(
            lambda: any(state is ListenerState.STOPPED for state, _closed in states)
        )
        assert states[-1] == (ListenerState.STOPPED, True)
        assert listener.stop() is True
        replacement.close()

    def test_explicit_stop_reports_stopped(self):
        states = []
        device = _PipeDevice("keyboard")
        listener = KeyListener(
            hotkey="KEY_F13",
            on_state_change=lambda state: states.append(state),
        )
        listener.start([device])
        wait_until(lambda: listener.state is ListenerState.AVAILABLE)

        assert listener.stop() is True
        assert states == [ListenerState.AVAILABLE, ListenerState.STOPPED]

    def test_stop_wins_a_race_with_fatal_reporting(self, monkeypatch):
        states = []
        device = _PipeDevice("keyboard")
        entered, release = threading.Event(), threading.Event()

        def pause_log(*_args, **_kwargs):
            entered.set()
            release.wait(5)

        def fail():
            raise KeyError("callback failed")

        monkeypatch.setattr("quoth.listener.logger.exception", pause_log)
        listener = KeyListener(
            hotkey="KEY_F13",
            on_press=fail,
            on_state_change=lambda state: states.append(state),
        )
        listener.start([device])
        wait_until(lambda: listener.state is ListenerState.AVAILABLE)
        device.push(_press(ecodes.KEY_F13))
        assert entered.wait(1)

        _stop_while_blocked(listener, release)

        assert ListenerState.FAILED not in states


class TestFailure:
    def test_key_error_is_fatal_and_closes_worker_devices(self):
        states = []
        device = _PipeDevice("keyboard")

        class KeyErrorSelector:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def register(self, *args):
                raise KeyError("duplicate")

        listener = KeyListener(
            hotkey="KEY_F13",
            on_state_change=lambda state: states.append(state),
        )
        with patch("quoth.listener.selectors.DefaultSelector", KeyErrorSelector):
            assert listener.start([device]) is True
            wait_until(lambda: listener.state is ListenerState.FAILED)
            assert listener.stop() is True

        assert states == [ListenerState.FAILED]
        assert device.closed

    def test_a_raising_callback_is_fatal_once(self):
        states = []
        device = _PipeDevice("keyboard")

        def fail():
            raise KeyError("callback failed")

        listener = KeyListener(
            hotkey="KEY_F13",
            on_press=fail,
            on_state_change=lambda state: states.append(state),
        )
        listener.start([device])
        wait_until(lambda: listener.state is ListenerState.AVAILABLE)
        device.push(_press(ecodes.KEY_F13))
        wait_until(lambda: ListenerState.FAILED in states)
        assert listener.stop() is True

        assert states.count(ListenerState.FAILED) == 1
        assert listener._held_source is None


class TestSynDropped:
    def test_ignores_source_until_report_then_releases_missing_held_key(self):
        stopped = []
        device = _PipeDevice("keyboard")
        listener = KeyListener(
            hotkey="KEY_F13",
            on_release=lambda: stopped.append(1),
        )
        _accept(listener)

        listener._handle_event(_press(ecodes.KEY_F13), device)
        listener._handle_event(_syn(ecodes.SYN_DROPPED), device)
        listener._handle_event(_release(ecodes.KEY_F13), device)
        assert stopped == []
        listener._handle_event(_syn(ecodes.SYN_REPORT), device)

        assert stopped == [1]

    def test_resync_releases_a_missing_key_while_events_are_suspended(self):
        stopped = []
        device = _PipeDevice("keyboard")
        listener = KeyListener(
            hotkey="KEY_F13",
            on_release=lambda: stopped.append(1),
        )
        _accept(listener)
        listener._handle_event(_press(ecodes.KEY_F13), device)
        listener.suspended = True

        listener._handle_event(_syn(ecodes.SYN_DROPPED), device)
        listener._handle_event(_syn(ecodes.SYN_REPORT), device)

        assert stopped == [1]

    def test_resync_keeps_a_key_that_is_still_down(self):
        edges = []
        device = _PipeDevice("keyboard")
        device._active_keys = [ecodes.KEY_F13]
        listener = KeyListener(
            hotkey="KEY_F13",
            on_press=lambda: edges.append("down"),
            on_release=lambda: edges.append("up"),
        )
        _accept(listener)

        listener._handle_event(_press(ecodes.KEY_F13), device)
        listener._handle_event(_syn(ecodes.SYN_DROPPED), device)
        listener._handle_event(_syn(ecodes.SYN_REPORT), device)
        assert edges == ["down"]
        listener._handle_event(_release(ecodes.KEY_F13), device)
        assert edges == ["down", "up"]

    def test_resync_does_not_synthesize_a_press(self):
        started = []
        device = _PipeDevice("keyboard")
        device._active_keys = [ecodes.KEY_F13]
        listener = KeyListener(hotkey="KEY_F13", on_press=lambda: started.append(1))
        _accept(listener)

        listener._handle_event(_syn(ecodes.SYN_DROPPED), device)
        listener._handle_event(_press(ecodes.KEY_F13), device)
        listener._handle_event(_syn(ecodes.SYN_REPORT), device)

        assert started == []

    def test_dropped_state_is_isolated_per_unhashable_source(self):
        edges = []
        dropped = _PipeDevice("keyboard")
        active = _PipeDevice("keyboard")
        listener = KeyListener(
            hotkey="KEY_F13",
            on_press=lambda: edges.append("down"),
            on_release=lambda: edges.append("up"),
        )
        _accept(listener)

        listener._handle_event(_syn(ecodes.SYN_DROPPED), dropped)
        listener._handle_event(_press(ecodes.KEY_F13), dropped)
        listener._handle_event(_press(ecodes.KEY_F13), active)
        listener._handle_event(_release(ecodes.KEY_F13), active)
        listener._handle_event(_syn(ecodes.SYN_REPORT), dropped)
        listener._handle_event(_press(ecodes.KEY_F13), dropped)

        # The dropped source's press counts only after its report.
        assert edges == ["down", "up", "down"]

    def test_dropping_device_clears_its_resync_state(self):
        dropped = _PipeDevice("dropped")
        active = _PipeDevice("active")
        listener = KeyListener(hotkey="KEY_F13")
        _accept(listener)
        listener._devices = [dropped, active]
        listener._handle_event(_syn(ecodes.SYN_DROPPED), dropped)

        listener._drop_device(dropped)

        assert listener._dropped_sources == set()
        assert listener._devices == [active]
        assert dropped.closed
        active.close()


class TestKeyWithoutRelease:
    """A key without a break code toggles instead of holding.

    Pause on a PS/2 keyboard has none, so the kernel reports its release with its press.
    """

    def _dictation(self, mode: str):
        # The app's rule for each edge, with the mode a press may override.
        state = {"active": False, "edges": []}

        def apply(pressed: bool, override: str | None = None) -> None:
            action = hotkey_action(override or mode, state["active"], pressed)
            state["edges"].append(action)
            if action is not None:
                state["active"] = action == "start"

        listener = KeyListener(
            hotkey="KEY_PAUSE",
            on_press=lambda override=None: apply(True, override),
            on_release=lambda: apply(False),
        )
        _accept(listener)
        return listener, state

    def _instant_tap(self, listener, device, usec: int = 0, gap: int = 3_000) -> None:
        # A PS/2 Pause's break bytes follow its make bytes by about 2 to 5 ms.
        listener._handle_event(_press(ecodes.KEY_PAUSE, usec), device)
        listener._handle_event(_release(ecodes.KEY_PAUSE, usec + gap), device)

    def test_push_to_hold_starts_on_one_press_and_stops_on_the_next(self, caplog):
        listener, state = self._dictation(PUSH_TO_HOLD)
        laptop = _PipeDevice("AT Translated Set 2 keyboard")

        self._instant_tap(listener, laptop)
        assert state["active"] is True
        assert state["edges"] == ["start"]
        self._instant_tap(listener, laptop, usec=200_000)

        assert state["active"] is False
        assert state["edges"] == ["start", "stop"]
        notes = [r.message for r in caplog.records if r.levelname == "INFO"]
        assert notes == [
            "Hotkey on AT Translated Set 2 keyboard reports no release; it toggles "
            "instead of holding"
        ]

    @pytest.mark.parametrize(("gap", "holds"), [(19_900, False), (20_000, True)])
    def test_a_release_within_20_ms_is_no_release(self, gap, holds):
        listener, state = self._dictation(PUSH_TO_HOLD)

        self._instant_tap(listener, _PipeDevice("keyboard"), gap=gap)

        assert state["active"] is not holds

    def test_a_clock_stepped_back_during_a_hold_marks_nothing(self):
        listener, state = self._dictation(PUSH_TO_HOLD)
        keyboard = _PipeDevice("keyboard")

        listener._handle_event(
            InputEvent(10, 0, ecodes.EV_KEY, ecodes.KEY_PAUSE, 1), keyboard
        )
        listener._handle_event(
            InputEvent(8, 0, ecodes.EV_KEY, ecodes.KEY_PAUSE, 0), keyboard
        )

        assert state["edges"] == ["start", "stop"]
        assert id(keyboard) not in listener._unholdable

    def test_one_stalled_pair_costs_one_toggle_then_the_key_holds(self, caplog):
        # A Bluetooth link that stalls can deliver a real press and release together.
        listener, state = self._dictation(PUSH_TO_HOLD)
        keyboard = _PipeDevice("headset keyboard")

        self._instant_tap(listener, keyboard)
        self._instant_tap(listener, keyboard, usec=100_000, gap=600_000)
        assert state["edges"] == ["start", "stop"]
        listener._handle_event(_press(ecodes.KEY_PAUSE, 800_000), keyboard)
        assert state["active"] is True
        listener._handle_event(_release(ecodes.KEY_PAUSE, 900_000), keyboard)
        assert state["edges"] == ["start", "stop", "start", "stop"]
        self._instant_tap(listener, keyboard, usec=950_000)

        notes = [r.message for r in caplog.records if r.levelname == "INFO"]
        assert len(notes) == 2

    def _reporting(self):
        reports = []
        listener = KeyListener(hotkey="KEY_PAUSE", on_unholdable=reports.append)
        _accept(listener)
        return listener, reports

    def test_each_marking_is_reported_once(self):
        listener, reports = self._reporting()
        laptop = _PipeDevice("laptop")

        assert not listener.unholdable
        self._instant_tap(listener, laptop)
        self._instant_tap(listener, laptop, usec=100_000)

        assert reports == [True]
        assert listener.unholdable

    def test_a_key_that_holds_again_is_reported(self):
        listener, reports = self._reporting()
        keyboard = _PipeDevice("keyboard")
        self._instant_tap(listener, keyboard)

        self._instant_tap(listener, keyboard, usec=100_000, gap=600_000)

        assert reports == [True, False]
        assert not listener.unholdable

    def test_a_rebind_or_a_device_gone_reports_the_marks_forgotten(self):
        listener, reports = self._reporting()
        laptop = _PipeDevice("laptop")
        listener._devices = [laptop]
        self._instant_tap(listener, laptop)
        listener.rebind("KEY_PAUSE")
        listener.rebind("KEY_PAUSE")  # nothing left to forget
        self._instant_tap(listener, laptop, usec=100_000)
        listener._drop_device(laptop)

        assert reports == [True, False, True, False]

    def test_tap_to_toggle_is_unchanged(self):
        listener, state = self._dictation(TAP_TO_TOGGLE)
        laptop = _PipeDevice("laptop")

        self._instant_tap(listener, laptop)
        assert state["active"] is True
        self._instant_tap(listener, laptop, usec=200_000)

        assert state["active"] is False
        assert [edge for edge in state["edges"] if edge] == ["start", "stop"]

    def test_a_held_key_still_holds_beside_one_that_cannot(self):
        listener, state = self._dictation(PUSH_TO_HOLD)
        laptop, usb = _PipeDevice("laptop"), _PipeDevice("usb")

        self._instant_tap(listener, laptop)
        self._instant_tap(listener, laptop, usec=100_000)
        assert state["active"] is False
        listener._handle_event(_press(ecodes.KEY_PAUSE, 200_000), usb)
        assert state["active"] is True
        listener._handle_event(_release(ecodes.KEY_PAUSE, 225_000), usb)

        assert state["active"] is False
        assert state["edges"] == ["start", "stop", "start", "stop"]

    def test_a_rebind_forgets_the_key(self):
        listener, state = self._dictation(PUSH_TO_HOLD)
        laptop = _PipeDevice("laptop")
        self._instant_tap(listener, laptop)

        listener.rebind("KEY_PAUSE")
        listener._handle_event(_press(ecodes.KEY_PAUSE, 100_000), laptop)
        listener._handle_event(_release(ecodes.KEY_PAUSE, 600_000), laptop)

        # Held again: the press does not stop, and the release does.
        assert state["edges"] == ["start", None, "stop"]

    def test_a_device_that_goes_away_is_forgotten(self):
        listener, _state = self._dictation(PUSH_TO_HOLD)
        laptop = _PipeDevice("laptop")
        listener._devices = [laptop]
        self._instant_tap(listener, laptop)
        assert id(laptop) in listener._unholdable

        listener._drop_device(laptop)

        assert listener._unholdable == {}
