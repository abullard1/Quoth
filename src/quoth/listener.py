# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 Samuel Ruairí Bullard

"""The evdev hotkey listener.

Reports each press and release of one key or modifier chord from attached keyboards;
`trigger.hotkey_action` decides what they do.
"""

from __future__ import annotations

import selectors
import threading
import time
from collections.abc import Callable, Container
from dataclasses import dataclass
from enum import Enum, auto

# python-evdev is absent in some environments: the Flatpak does not bundle it and uses
# the shortcuts portal or `quoth --toggle`. The guard lets the module load anywhere;
# constructing a `KeyListener` without it raises.
try:
    import evdev
    from evdev import ecodes
except ImportError:
    evdev = None
    ecodes = None

from loguru import logger

from quoth import keynames
from quoth.trigger import TAP_TO_TOGGLE


class ListenerState(Enum):
    """The listener's availability, as reported through `on_state_change`."""

    STOPPED = auto()
    AVAILABLE = auto()
    WAITING = auto()
    FAILED = auto()


def evdev_available() -> bool:
    """Whether python-evdev imported, so the hotkey listener can run here."""
    return evdev is not None


def key_name_for_scancode(scancode: int) -> str | None:
    """Return the evdev token for a hardware scancode, or None if unmapped."""
    if ecodes is None:
        return None
    name = ecodes.KEY.get(scancode)
    if isinstance(name, (list, tuple)):
        name = name[0]
    return name


def scancode_for_key_name(token: str) -> int | None:
    """Return the hardware scancode for an evdev key token, or None if unknown.

    Only key and button names count: the same table also names event types and other
    codes.
    """
    if ecodes is None or not token.startswith(("KEY_", "BTN_")):
        return None
    return ecodes.ecodes.get(token)


@dataclass(frozen=True)
class _Binding:
    keycode: int
    modifier_groups: tuple[frozenset[int], ...] = ()
    required_modifiers: frozenset[int] = frozenset()


def _parse_hotkey(hotkey: str) -> _Binding:
    modifiers, key = keynames.split_hotkey(hotkey)
    keycode = scancode_for_key_name(key)
    if keycode is None:
        raise ValueError(f"Unknown key: {key}")
    groups = tuple(
        frozenset(
            code
            for token in keynames.HOTKEY_MODIFIERS[modifier]
            if (code := scancode_for_key_name(token)) is not None
        )
        for modifier in modifiers
    )
    required = frozenset(code for group in groups for code in group)
    return _Binding(keycode, groups, required)


def _name(source: object) -> str:
    return getattr(source, "name", "an unnamed device")


def _close_device(device: evdev.InputDevice) -> None:
    try:
        device.close()
    except OSError as error:
        logger.debug("Device close failed for {}: {}", device.path, error)


def _sends(device: evdev.InputDevice, keycode: int) -> bool:
    """Whether the device advertises the key.

    The input core drops key codes a device does not advertise, so a device without it
    can never deliver it.
    """
    return keycode in device.capabilities().get(ecodes.EV_KEY, ())


def _active_keys(device: evdev.InputDevice) -> list[int]:
    """Return the keys the kernel reports held, which a grab elsewhere does not hide.

    python-evdev raises a failed EVIOCGKEY, as on an unplugged device, as a bare
    SystemError, so it becomes the OSError that its other device calls raise.
    """
    try:
        return device.active_keys()
    except SystemError as error:
        raise OSError(f"Could not read the key state of {device.path}") from error


# Devices that last failed to open or inspect, so a rescan logs each failure once.
_unreadable: set[str] = set()


def _unreadable_once(path: str, action: str, error: OSError) -> None:
    if path not in _unreadable:
        _unreadable.add(path)
        logger.debug("Could not {} input device {}: {}", action, path, error)


def find_keyboards(keycode: int, known: Container[str] = ()) -> list[evdev.InputDevice]:
    """Open every device that can send `keycode`.

    A remapper such as keyd grabs the keyboards it handles, so each press arrives once:
    from the physical device or from the remapper's virtual keyboard. Paths in `known`
    are already open, so they are skipped unopened. Every other device opened here and
    not returned is closed again.
    """
    devices = []
    for path in evdev.list_devices():
        if path in known:
            continue
        try:
            devices.append(evdev.InputDevice(path))
        except OSError as error:
            _unreadable_once(path, "open", error)

    keyboards = []
    for device in devices:
        try:
            if _sends(device, keycode):
                keyboards.append(device)
            else:
                _close_device(device)
            _unreadable.discard(device.path)
        except OSError as error:
            _unreadable_once(device.path, "inspect", error)
            _close_device(device)
    return keyboards


def keyboard_available(hotkey: str) -> bool:
    """Whether a device that can send `hotkey` can be opened now; none is kept open."""
    try:
        _modifiers, key = keynames.split_hotkey(hotkey)
    except ValueError:
        return False
    keycode = scancode_for_key_name(key)
    if not evdev_available() or keycode is None:
        return False
    try:
        devices = find_keyboards(keycode)
    except OSError as error:
        logger.debug("Could not scan input devices: {}", error)
        return False
    try:
        return bool(devices)
    finally:
        for device in devices:
            _close_device(device)


# Seconds between device rescans, which pick up keyboards that appear later
# (plugged back in, keyd restarting).
_RESCAN_S = 2.0
# How often the read loop looks up from select() to notice stop().
_SELECT_TIMEOUT_S = 0.5
_JOIN_TIMEOUT_S = 2.0
_DIRECT_SOURCE = object()
# A release this soon after its press is no person's tap: the key has no break code, as
# Pause on a PS/2 keyboard, and the kernel reports its release with its press.
_NO_RELEASE_S = 0.020


class KeyListener:
    """An evdev hotkey watcher that reports each press and its release.

    Callbacks run on the background read thread, except what a `rebind` reports, the
    release of a held press and `on_unholdable(False)` for the marks it forgets, which
    run on its caller's thread. While `suspended`, presses are ignored. A press
    from a device whose key cannot be held passes `TAP_TO_TOGGLE` to `on_press`, as the
    mode to apply in place of the configured one. `on_unholdable` gets True when a
    device is found so, and False when one stops being so.
    """

    def __init__(
        self,
        hotkey: str,
        on_press: Callable[..., None] | None = None,
        on_release: Callable[[], None] | None = None,
        on_state_change: Callable[[ListenerState], None] | None = None,
        on_unholdable: Callable[[bool], None] | None = None,
    ):
        if evdev is None:
            raise RuntimeError(
                "python-evdev is unavailable: the evdev hotkey listener cannot start "
                "(the Flatpak uses the shortcuts portal or `quoth --toggle`)"
            )
        self._binding = _parse_hotkey(hotkey)
        # shortcut: chords check _active_keys on the triggering keyboard; add
        # cross-device state if users need split-keyboard chords
        self._all_mod_codes = frozenset(
            code
            for tokens in keynames.HOTKEY_MODIFIERS.values()
            for token in tokens
            if (code := scancode_for_key_name(token)) is not None
        )
        # The key the open devices were chosen for; a rebind makes the next
        # reconcile drop the devices that cannot send the new one.
        self._filtered_for = self._binding.keycode
        self._lock = threading.Lock()
        self._held_source: object | None = None
        self._held_since: float | None = None
        # Sources, by id, whose hotkey release arrives with its press, each with the
        # time of its last press.
        self._unholdable: dict[int, float | None] = {}
        self._accepting = False
        self._suspended = False
        self._on_press = on_press or (lambda *_: None)
        self._on_release = on_release or (lambda: None)
        self._on_state_change = on_state_change or (lambda _state: None)
        self._on_unholdable = on_unholdable or (lambda _marked: None)
        self._state = ListenerState.STOPPED
        self._thread: threading.Thread | None = None
        self._stopping = threading.Event()
        self._devices: list[evdev.InputDevice] = []
        self._dropped_sources: set[int] = set()
        self._discovery_healthy = True

    def _notify_state(self, state: ListenerState, *, force: bool = False) -> None:
        with self._lock:
            if (
                self._state is ListenerState.FAILED
                or (state is ListenerState.FAILED and self._stopping.is_set())
                or (self._state is state and not force)
            ):
                return
            self._state = state
        logger.debug("Hotkey listener: {}", state.name)
        try:
            self._on_state_change(state)
        except Exception:
            logger.exception("Hotkey state callback failed")

    def _press(self, source: object = _DIRECT_SOURCE, at: float | None = None) -> None:
        """Report a press at event time `at`; one that cannot be held is a tap."""
        with self._lock:
            taps = id(source) in self._unholdable
            if taps:
                self._unholdable[id(source)] = at
            if not self._accepting or self._suspended:
                logger.debug("Hotkey press ignored (listener unavailable or suspended)")
                return
            if self._held_source is not None:
                return
            if not taps:
                self._held_source = source
                self._held_since = at
        logger.debug("Hotkey down")
        if taps:
            self._on_press(TAP_TO_TOGGLE)
        else:
            self._on_press()

    def _release(
        self, source: object = _DIRECT_SOURCE, at: float | None = None
    ) -> None:
        """End the held press: its key went up, or its source stopped reporting it.

        Only a reported press is ever released, so suspension needs no check here. A
        key-up at `at` too soon after its press marks the source's key as unholdable,
        and is not reported; a later one that comes in good time clears the mark.
        """
        with self._lock:
            pressed_at = self._unholdable.get(id(source))
            cleared = (
                at is not None
                and pressed_at is not None
                and at - pressed_at >= _NO_RELEASE_S
            )
            if cleared:
                del self._unholdable[id(source)]
            held = self._held_source is source
            if held:
                self._held_source = None
            # A clock stepped back makes the gap negative, which proves nothing.
            instant = (
                held
                and at is not None
                and self._held_since is not None
                and 0 <= at - self._held_since < _NO_RELEASE_S
            )
            if instant:
                self._unholdable[id(source)] = self._held_since
        if cleared:
            logger.debug("Hotkey on {} reports releases again", _name(source))
            self._on_unholdable(False)
        if not held:
            return
        if instant:
            logger.info(
                "Hotkey on {} reports no release; it toggles instead of holding",
                _name(source),
            )
            self._on_unholdable(True)
            return
        logger.debug("Hotkey up")
        self._on_release()

    def _read_loop(self, devices: list[evdev.InputDevice]) -> None:
        unpublished = list(devices)
        self._devices = []
        try:
            with selectors.DefaultSelector() as selector:
                try:
                    self._register_initial_devices(selector, unpublished)
                    if not self._devices and not self._stopping.is_set():
                        logger.warning(
                            "No keyboard can send {}; waiting for one",
                            key_name_for_scancode(self._binding.keycode),
                        )
                    self._set_availability()
                    next_discovery = time.monotonic() + _RESCAN_S

                    while not self._stopping.is_set():
                        remaining = max(0.0, next_discovery - time.monotonic())
                        timeout = min(_SELECT_TIMEOUT_S, remaining)
                        for key, _mask in selector.select(timeout):
                            device = key.fileobj
                            try:
                                for event in device.read():
                                    self._handle_event(event, device)
                            except BlockingIOError:
                                continue
                            except OSError:
                                self._drop_device(device, selector)
                                self._set_availability()

                        if self._stopping.is_set():
                            break
                        if time.monotonic() >= next_discovery:
                            self._reconcile_devices(selector)
                            self._set_availability()
                            next_discovery = time.monotonic() + _RESCAN_S
                except Exception:
                    if not self._stopping.is_set():
                        logger.exception("Hotkey read loop crashed")
                        self._fail()
                finally:
                    for device in list(self._devices):
                        self._drop_device(device, selector, deliberate=True)
        finally:
            for device in unpublished:
                _close_device(device)
            self._devices = []
            self._dropped_sources.clear()
            with self._lock:
                self._accepting = False
            if self._stopping.is_set():
                self._notify_state(ListenerState.STOPPED, force=True)

    def _register_initial_devices(
        self,
        selector: selectors.BaseSelector,
        unpublished: list[evdev.InputDevice],
    ) -> None:
        for device in list(unpublished):
            try:
                selector.register(device, selectors.EVENT_READ)
            except (OSError, ValueError):
                _close_device(device)
            else:
                self._devices.append(device)
                logger.debug("Keyboard registered: {} at {}", device.name, device.path)
            unpublished.remove(device)

    def _set_availability(self) -> None:
        if self._stopping.is_set():
            return
        if self._devices:
            with self._lock:
                self._accepting = True
            self._notify_state(ListenerState.AVAILABLE)
            return
        with self._lock:
            self._accepting = False
        self._notify_state(ListenerState.WAITING)

    def _modifiers_active(self, active: Container[int], binding: _Binding) -> bool:
        if not binding.modifier_groups:
            return True
        held = set(active)
        return all(group & held for group in binding.modifier_groups) and not (
            (held & self._all_mod_codes) - binding.required_modifiers
        )

    def _modifiers_match(self, source: object, binding: _Binding) -> bool:
        if not binding.modifier_groups:
            return True
        if not hasattr(source, "active_keys"):
            return False
        try:
            active = _active_keys(source)
        except OSError:
            return False
        return self._modifiers_active(active, binding)

    def _reconcile_devices(self, selector: selectors.BaseSelector) -> None:
        with self._lock:
            binding = self._binding
            held = self._held_source
        if binding.keycode != self._filtered_for:
            self._filtered_for = binding.keycode
            for device in list(self._devices):
                if not _sends(device, binding.keycode):
                    self._drop_device(device, selector, deliberate=True)
        # A program that grabs the device mid-press, such as a remapper starting, takes
        # its key-up, while the kernel's key state shows the key released.
        if held in self._devices:
            try:
                active = _active_keys(held)
                if binding.keycode not in active or not self._modifiers_active(
                    active, binding
                ):
                    self._release(held)
            except OSError:
                self._drop_device(held, selector)
        try:
            found = find_keyboards(
                binding.keycode, known={d.path for d in self._devices}
            )
        except OSError as error:
            if self._stopping.is_set():
                return
            if self._discovery_healthy:
                logger.warning("Hotkey device discovery failed: {}", error)
                self._discovery_healthy = False
            return

        unpublished = list(found)
        registered: list[evdev.InputDevice] = []
        published = False
        try:
            if self._stopping.is_set():
                return
            if not self._discovery_healthy:
                logger.info("Hotkey device discovery recovered")
                self._discovery_healthy = True
            for device in list(unpublished):
                if self._stopping.is_set():
                    return
                try:
                    selector.register(device, selectors.EVENT_READ)
                except (OSError, ValueError):
                    _close_device(device)
                else:
                    registered.append(device)
                unpublished.remove(device)

            if self._stopping.is_set():
                return
            self._devices.extend(registered)
            published = True
            for device in registered:
                logger.debug("Keyboard registered: {} at {}", device.name, device.path)
        finally:
            if not published:
                for device in registered:
                    try:
                        selector.unregister(device)
                    except (KeyError, ValueError):
                        pass
                    _close_device(device)
            for device in unpublished:
                _close_device(device)

    def _drop_device(
        self,
        device: evdev.InputDevice,
        selector: selectors.BaseSelector | None = None,
        *,
        deliberate: bool = False,
    ) -> None:
        if selector is not None:
            try:
                selector.unregister(device)
            except (KeyError, ValueError):
                pass
        self._devices.remove(device)
        self._dropped_sources.discard(id(device))
        with self._lock:
            forgotten = self._unholdable.pop(id(device), False) is not False
        if forgotten:
            self._on_unholdable(False)
        self._release(device)
        _close_device(device)
        if deliberate:
            logger.debug("Hotkey device {} retired", device.path)
        elif self._devices:
            logger.warning("Hotkey device {} disconnected; others remain", device.path)
        else:
            with self._lock:
                self._accepting = False
            logger.warning(
                "Hotkey device {} disconnected; waiting for a keyboard", device.path
            )

    def _handle_event(
        self, event: evdev.InputEvent, source: object = _DIRECT_SOURCE
    ) -> None:
        with self._lock:
            binding = self._binding
        source_id = id(source)
        if event.type == ecodes.EV_SYN:
            if event.code == ecodes.SYN_DROPPED:
                self._dropped_sources.add(source_id)
            elif event.code == ecodes.SYN_REPORT and source_id in self._dropped_sources:
                self._dropped_sources.remove(source_id)
                active_keys = _active_keys(source)
                with self._lock:
                    held_here = self._held_source is source
                if held_here and (
                    binding.keycode not in active_keys
                    or not self._modifiers_active(active_keys, binding)
                ):
                    self._release(source)
            return
        if source_id in self._dropped_sources or event.type != ecodes.EV_KEY:
            return
        key_event = evdev.categorize(event)
        if key_event.scancode == binding.keycode:
            if key_event.keystate == evdev.KeyEvent.key_down:
                if self._modifiers_match(source, binding):
                    self._press(source, event.timestamp())
            elif key_event.keystate == evdev.KeyEvent.key_up:
                self._release(source, event.timestamp())
        elif (
            binding.modifier_groups
            and key_event.scancode in binding.required_modifiers
            and key_event.keystate == evdev.KeyEvent.key_up
            and not self._modifiers_match(source, binding)
        ):
            self._release(source)

    def _fail(self) -> None:
        with self._lock:
            if self._stopping.is_set() or self._state is ListenerState.FAILED:
                return
            self._held_source = None
            self._accepting = False
        self._notify_state(ListenerState.FAILED)

    def start(self, devices: list[evdev.InputDevice] | None = None) -> bool:
        """Start the read thread; return False when it is running or no device exists.

        Until a device can send the hotkey, such as a keyboard still connecting, the
        thread waits for one.
        """
        if self._thread is not None:
            return False
        found = (
            list(devices)
            if devices is not None
            else find_keyboards(self._binding.keycode)
        )
        if not found and not evdev.list_devices():
            return False
        paths = [device.path for device in found]
        self._stopping.clear()
        self._thread = threading.Thread(
            target=self._read_loop, args=(found,), name="hotkey-listener", daemon=True
        )
        try:
            self._thread.start()
        except Exception:
            self._thread = None
            for device in found:
                _close_device(device)
            raise
        logger.debug("Hotkey listener launched (devices={})", paths)
        return True

    def rebind(self, hotkey: str) -> None:
        """Watch another key or chord from the next event; reject an unknown one.

        A held press of the old key is released, since its key-up will no longer match.
        The next reconcile opens the devices that can send the new key and drops those
        that cannot.
        """
        binding = _parse_hotkey(hotkey)
        with self._lock:
            self._binding = binding
            forgotten = bool(self._unholdable)
            self._unholdable.clear()
            held = self._held_source
        if forgotten:
            self._on_unholdable(False)
        if held is not None:
            self._release(held)

    def stop(self) -> bool:
        """Stop accepting immediately, then wait briefly for the worker to finish.

        Return False when the worker is still alive afterwards or this runs on it.
        """
        self._stopping.set()
        with self._lock:
            self._accepting = False
            self._held_source = None
        thread = self._thread
        if thread is not None:
            if thread is threading.current_thread():
                return False
            thread.join(timeout=_JOIN_TIMEOUT_S)
            if thread.is_alive():
                logger.warning("Hotkey read thread did not stop in time")
                return False
            self._thread = None
        return True

    @property
    def state(self) -> ListenerState:
        """The current state: STOPPED from the moment `stop` begins, unless FAILED."""
        with self._lock:
            if self._stopping.is_set() and self._state is not ListenerState.FAILED:
                return ListenerState.STOPPED
            return self._state

    @property
    def unholdable(self) -> bool:
        """Whether some device's hotkey reports no release, so it toggles."""
        with self._lock:
            return bool(self._unholdable)

    @property
    def suspended(self) -> bool:
        with self._lock:
            return self._suspended

    @suspended.setter
    def suspended(self, value: bool) -> None:
        logger.debug("Hotkey listener suspended: {}", value)
        with self._lock:
            self._suspended = value
