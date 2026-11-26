# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 Samuel Ruairí Bullard

"""The evdev hotkey listener.

Reports each press and release of a key or modifier chord from attached keyboards;
`trigger.hotkey_action` decides what they do.
"""

from __future__ import annotations

import selectors
import threading
import time
from collections.abc import Callable, Container
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


def _keycode(hotkey: str) -> int:
    keycode = scancode_for_key_name(hotkey)
    if keycode is None:
        raise ValueError(f"Unknown key: {hotkey}")
    return keycode


def _hotkey_parts(hotkey: str) -> tuple[int, tuple[frozenset[int], ...]]:
    modifiers, key = keynames.split_hotkey(hotkey)
    keycode = _keycode(key)
    groups = []
    for modifier in modifiers:
        codes = frozenset(
            code
            for token in keynames.HOTKEY_MODIFIER_KEYS[modifier]
            if (code := scancode_for_key_name(token)) is not None
        )
        if not codes:
            raise ValueError(f"Unsupported hotkey modifier: {modifier}")
        groups.append(codes)
    return keycode, tuple(groups)


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


def _sends_any(device: evdev.InputDevice, keycodes: Container[int]) -> bool:
    return bool(set(device.capabilities().get(ecodes.EV_KEY, ())) & set(keycodes))


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
    """Open every device that can send `keycode`."""
    return _find_keyboards(lambda device: _sends(device, keycode), known)


def find_keyboards_for_keys(
    keycodes: Container[int], known: Container[str] = ()
) -> list[evdev.InputDevice]:
    """Open devices that can send any key needed by a modifier chord."""
    return _find_keyboards(lambda device: _sends_any(device, keycodes), known)


def _find_keyboards(
    matches: Callable[[evdev.InputDevice], bool], known: Container[str]
) -> list[evdev.InputDevice]:
    """Open each matching input device and close every device not returned.

    A remapper such as keyd grabs the keyboards it handles, so each press arrives once:
    from the physical device or from the remapper's virtual keyboard. Paths in `known`
    are already open, so they are skipped unopened.
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
            if matches(device):
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
        self._keycode, self._modifier_groups = _hotkey_parts(hotkey)
        self._modifier_codes = frozenset(
            code for group in self._modifier_groups for code in group
        )
        self._all_modifier_codes = frozenset(
            code
            for tokens in keynames.HOTKEY_MODIFIER_KEYS.values()
            for token in tokens
            if (code := scancode_for_key_name(token)) is not None
        )
        self._listen_keycodes = frozenset(
            {self._keycode}
            | (self._all_modifier_codes if self._modifier_groups else frozenset())
        )
        # A rebind makes the next reconcile drop devices unrelated to the new chord.
        self._filtered_for = self._listen_keycodes
        self._pressed_by_source: dict[int, tuple[object, set[int]]] = {}
        self._chord_active = False
        self._chord_reported = False
        self._chord_source: object | None = None
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

    def _remember_active_keys(self, source: evdev.InputDevice) -> None:
        keys = set(_active_keys(source)) & self._listen_keycodes
        self._pressed_by_source[id(source)] = (source, keys)

    def _chord_is_down(self) -> bool:
        pressed = set().union(
            *(keys for _source, keys in self._pressed_by_source.values())
        )
        if self._keycode not in pressed or not all(
            group & pressed for group in self._modifier_groups
        ):
            return False
        extra_modifiers = (pressed & self._all_modifier_codes) - self._modifier_codes
        return not extra_modifiers

    def _source_for_key(self, keycode: int) -> object | None:
        return next(
            (
                source
                for source, keys in self._pressed_by_source.values()
                if keycode in keys
            ),
            None,
        )

    def _sync_chord_state(
        self,
        source: object | None = None,
        at: float | None = None,
        *,
        activate: bool = False,
        released_key: int | None = None,
    ) -> None:
        active = self._chord_is_down()
        self._chord_active = active
        if not active:
            held_source, self._chord_source = self._chord_source, None
            reported, self._chord_reported = self._chord_reported, False
            if reported and held_source is not None:
                self._release(
                    held_source,
                    at,
                    detect_unholdable=released_key == self._keycode,
                )
            return
        if self._chord_reported or not activate:
            return
        self._chord_source = self._source_for_key(self._keycode) or source
        if self._chord_source is None:
            self._chord_source = _DIRECT_SOURCE
        self._chord_reported = True
        self._press(self._chord_source, at)

    def _prime_chord_state(self, devices: list[evdev.InputDevice]) -> None:
        if not self._modifier_groups:
            return
        for device in devices:
            try:
                self._remember_active_keys(device)
            except OSError as error:
                logger.debug("Could not read keys held on {}: {}", device.path, error)
        self._chord_active = self._chord_is_down()
        self._chord_reported = False
        self._chord_source = None

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
        self,
        source: object = _DIRECT_SOURCE,
        at: float | None = None,
        *,
        detect_unholdable: bool = True,
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
                detect_unholdable
                and held
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
        self._pressed_by_source.clear()
        self._chord_active = False
        self._chord_reported = False
        self._chord_source = None
        try:
            with selectors.DefaultSelector() as selector:
                try:
                    self._register_initial_devices(selector, unpublished)
                    self._prime_chord_state(self._devices)
                    if (
                        not self._has_primary_key_device()
                        and not self._stopping.is_set()
                    ):
                        logger.warning(
                            "No keyboard can send {}; waiting for one",
                            key_name_for_scancode(self._keycode),
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
        if self._has_primary_key_device():
            with self._lock:
                self._accepting = True
            self._notify_state(ListenerState.AVAILABLE)
            return
        with self._lock:
            self._accepting = False
        self._notify_state(ListenerState.WAITING)

    def _has_primary_key_device(self) -> bool:
        for device in self._devices:
            try:
                if _sends(device, self._keycode):
                    return True
            except OSError:
                continue
        return False

    def _reconcile_devices(self, selector: selectors.BaseSelector) -> None:
        listen_keycodes = self._listen_keycodes
        if listen_keycodes != self._filtered_for:
            self._filtered_for = listen_keycodes
            for device in list(self._devices):
                sends_hotkey = (
                    _sends_any(device, listen_keycodes)
                    if self._modifier_groups
                    else _sends(device, self._keycode)
                )
                if not sends_hotkey:
                    self._drop_device(device, selector, deliberate=True)
        # A program that grabs the device mid-press, such as a remapper starting, takes
        # its key-up, while the kernel's key state shows the key released.
        if self._modifier_groups:
            for device in list(self._devices):
                try:
                    self._remember_active_keys(device)
                except OSError:
                    self._drop_device(device, selector)
            self._sync_chord_state()
        else:
            with self._lock:
                held = self._held_source
            if held in self._devices:
                try:
                    if self._keycode not in _active_keys(held):
                        self._release(held)
                except OSError:
                    self._drop_device(held, selector)
        try:
            known = {device.path for device in self._devices}
            if self._modifier_groups:
                found = find_keyboards_for_keys(listen_keycodes, known=known)
            else:
                found = find_keyboards(self._keycode, known=known)
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
            self._prime_chord_state(registered)
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
        if self._modifier_groups:
            self._pressed_by_source.pop(id(device), None)
            self._sync_chord_state()
        with self._lock:
            forgotten = self._unholdable.pop(id(device), False) is not False
        if forgotten:
            self._on_unholdable(False)
        if not self._modifier_groups:
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
        source_id = id(source)
        if event.type == ecodes.EV_SYN:
            if event.code == ecodes.SYN_DROPPED:
                self._dropped_sources.add(source_id)
            elif event.code == ecodes.SYN_REPORT and source_id in self._dropped_sources:
                self._dropped_sources.remove(source_id)
                if self._modifier_groups:
                    self._remember_active_keys(source)
                    self._sync_chord_state(source)
                else:
                    active_keys = _active_keys(source)
                    with self._lock:
                        held_here = self._held_source is source
                    if held_here and self._keycode not in active_keys:
                        self._release(source)
            return
        if source_id in self._dropped_sources or event.type != ecodes.EV_KEY:
            return
        key_event = evdev.categorize(event)
        if self._modifier_groups:
            if key_event.scancode not in self._listen_keycodes:
                return
            source_keys = self._pressed_by_source.setdefault(
                source_id, (source, set())
            )[1]
            if key_event.keystate == evdev.KeyEvent.key_down:
                source_keys.add(key_event.scancode)
            elif key_event.keystate == evdev.KeyEvent.key_up:
                source_keys.discard(key_event.scancode)
            else:
                return
            self._sync_chord_state(
                source,
                event.timestamp(),
                activate=(
                    key_event.keystate == evdev.KeyEvent.key_down
                    and key_event.scancode in (self._modifier_codes | {self._keycode})
                ),
                released_key=(
                    key_event.scancode
                    if key_event.keystate == evdev.KeyEvent.key_up
                    else None
                ),
            )
            return
        if key_event.scancode != self._keycode:
            return
        if key_event.keystate == evdev.KeyEvent.key_down:
            self._press(source, event.timestamp())
        elif key_event.keystate == evdev.KeyEvent.key_up:
            self._release(source, event.timestamp())

    def _fail(self) -> None:
        with self._lock:
            if self._stopping.is_set() or self._state is ListenerState.FAILED:
                return
            self._held_source = None
            self._accepting = False
        self._chord_active = False
        self._chord_reported = False
        self._chord_source = None
        self._notify_state(ListenerState.FAILED)

    def start(self, devices: list[evdev.InputDevice] | None = None) -> bool:
        """Start the read thread; return False when it is running or no device exists.

        Until a device can send the hotkey, such as a keyboard still connecting, the
        thread waits for one.
        """
        if self._thread is not None:
            return False
        if devices is not None:
            found = list(devices)
        elif self._modifier_groups:
            found = find_keyboards_for_keys(self._listen_keycodes)
        else:
            found = find_keyboards(self._keycode)
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
        """Watch another key or chord from the next event; reject unknown tokens.

        A held press of the old key is released, since its key-up will no longer match.
        The next reconcile opens the devices that can send the new key and drops those
        that cannot.
        """
        keycode, modifier_groups = _hotkey_parts(hotkey)
        modifier_codes = frozenset(code for group in modifier_groups for code in group)
        with self._lock:
            self._keycode = keycode
            self._modifier_groups = modifier_groups
            self._modifier_codes = modifier_codes
            self._listen_keycodes = frozenset(
                {keycode}
                | (self._all_modifier_codes if modifier_groups else frozenset())
            )
            forgotten = bool(self._unholdable)
            self._unholdable.clear()
            held = self._held_source
        if forgotten:
            self._on_unholdable(False)
        if held is not None:
            self._release(held)
        self._chord_active = self._chord_is_down() if modifier_groups else False
        self._chord_reported = False
        self._chord_source = None

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
