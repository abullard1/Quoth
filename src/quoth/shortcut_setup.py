# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 Samuel Ruairí Bullard

"""Shared presentation for desktop-managed shortcuts and manual toggle setup.

The Settings window and the onboarding page both show this: the `--toggle` command to
bind by hand, per-desktop steps and settings launchers, and `portal_presentation` for
the Flatpak's GlobalShortcuts session.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass

from loguru import logger

from quoth import APP_ID, environment
from quoth.portal_shortcuts import SHORTCUT_ID

# Shown by Settings and onboarding when the keyboard-settings launcher fails.
KEYBOARD_SETTINGS_FAILED_MSG = (
    "Couldn't open settings: copy the command and bind it manually"
)

# Launchers for each desktop's keyboard-shortcut settings page, run natively, keyed by a
# `desktop_names()` entry. Desktops with no settings GUI (Sway, Hyprland) and unknown
# ones get None; the copyable command and steps still guide them.
_SETTINGS_ARGV: dict[str, list[str]] = {
    "cosmic": ["cosmic-settings", "keyboard"],
    "gnome": ["gnome-control-center", "keyboard", "shortcuts"],
    "kde": ["systemsettings", "kcm_keys"],
    "plasma": ["systemsettings", "kcm_keys"],
}


def toggle_command() -> list[str]:
    """Return the command a user binds to a key to toggle dictation.

    `flatpak run <id> --toggle` inside a Flatpak, else `quoth --toggle`.
    """
    if environment.in_flatpak():
        return ["flatpak", "run", APP_ID, "--toggle"]
    return ["quoth", "--toggle"]


def toggle_command_str() -> str:
    """Return `toggle_command()` as a copy-pasteable string."""
    return " ".join(toggle_command())


def keyboard_settings_argv() -> list[str] | None:
    """Return the argv that opens this desktop's keyboard-settings page, or None.

    None when no launcher is known or inside a Flatpak, so the view shows no "Open
    settings" button; the copyable command and steps stay.
    """
    if environment.in_flatpak():
        return None
    names = environment.desktop_names()
    for key, argv in _SETTINGS_ARGV.items():
        if key in names:
            return argv
    return None


def open_keyboard_settings() -> bool:
    """Open the desktop's keyboard-settings page; return whether it launched.

    Non-blocking, since the settings app outlives us. The caller keeps the copyable
    command and steps visible either way.
    """
    argv = keyboard_settings_argv()
    if not argv:
        return False
    try:
        subprocess.Popen(
            argv,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return True
    except OSError as e:
        logger.warning("Could not open keyboard settings: {}", e)
        return False


def setup_steps() -> str:
    """Return short instructions for binding the toggle command on this desktop.

    A config snippet for tiling WMs, a custom-shortcut walkthrough where a settings GUI
    exists, else a generic line. The view shows the copyable command itself.
    """
    names = environment.desktop_names()
    cmd = toggle_command_str()
    # Super+Shift+D is free in the stock configs and is not the native default, so a
    # desktop binding and a native listener never both toggle on one press.
    if "hyprland" in names:
        return (
            "Add to your Hyprland config and reload. In hyprland.lua:\n"
            f'    hl.bind("SUPER + SHIFT + D", hl.dsp.exec_cmd("{cmd}"))\n'
            f"In hyprland.conf:\n    bind = SUPER SHIFT, D, exec, {cmd}"
        )
    if "sway" in names:
        return (
            "Add to your Sway config and reload:\n"
            f"    bindsym --no-repeat Mod4+Shift+d exec {cmd}"
        )
    if keyboard_settings_argv():
        return (
            "Open keyboard settings, add a custom shortcut, paste the command shown, "
            "and press the key you want to use."
        )
    return (
        "In your desktop's keyboard-shortcut settings, add a custom shortcut that runs "
        "the command shown and assign it a key."
    )


def _in_key_settings(lead: str) -> str:
    """Return `lead` and where this desktop sets the portal shortcut's key."""
    names = environment.desktop_names()
    if "hyprland" in names:
        name = f"{APP_ID}:{SHORTCUT_ID}"
        return (
            f"{lead} in your Hyprland config, then reload. In hyprland.lua:\n"
            f'    hl.bind("SUPER + SHIFT + D", hl.dsp.global("{name}"))\n'
            f"In hyprland.conf:\n    bind = SUPER SHIFT, D, global, {name}"
        )
    if "gnome" in names:
        # GNOME lists portal shortcuts under the app, not in its keyboard settings.
        return f"{lead} in Settings → Apps → Quoth → Global Shortcuts."
    return f"{lead} in your desktop's keyboard settings."


@dataclass(frozen=True)
class PortalPresentation:
    """What a setup view shows for the GlobalShortcuts session's current state.

    `action` labels the button that calls `PortalShortcuts.configure`, None for no
    button, and `can_configure` says whether it is enabled. `manual` shows the toggle
    command to bind by hand; `ready` means a key is bound.
    """

    title: str
    description: str
    action: str | None = None
    can_configure: bool = False
    manual: bool = False
    ready: bool = False


def portal_presentation(owner) -> PortalPresentation:
    """Return what a setup view shows for the `PortalShortcuts` session `owner`."""
    if owner.status == "ready":
        return PortalPresentation(
            owner.binding or "Your dictation shortcut",
            "Your desktop manages this shortcut."
            if owner.version >= 2
            else _in_key_settings("Change this shortcut"),
            action="Change shortcut" if owner.version >= 2 else None,
            can_configure=owner.version >= 2,
            ready=True,
        )
    if owner.status == "needs-key":
        return PortalPresentation(
            "Choose a dictation shortcut",
            "Your desktop added the shortcut without a key. "
            + _in_key_settings("You can also assign one")
            if owner.version >= 2
            else _in_key_settings("Assign it a key"),
            action="Choose a shortcut" if owner.version >= 2 else None,
            can_configure=owner.version >= 2,
        )
    if owner.status == "unavailable":
        return PortalPresentation(
            "Bind a dictation shortcut",
            "Tap your shortcut to start and stop dictation. " + setup_steps(),
            manual=True,
        )
    title, description, action = {
        "checking": (
            "Checking shortcut support",
            "Checking your desktop's shortcut service.",
            None,
        ),
        "unbound": (
            "Set up your dictation shortcut",
            "Choose a global shortcut through your desktop.",
            "Set up shortcut",
        ),
        "requesting": (
            "Waiting for your desktop",
            "Complete the shortcut setup in your desktop's dialog.",
            None,
        ),
        "denied": (
            "Shortcut not allowed",
            "You can try again when you want to allow a shortcut.",
            "Try again",
        ),
        "closed": (
            "Shortcut needs setup",
            "The desktop shortcut session closed. Set it up again to dictate with a shortcut.",
            "Set up shortcut",
        ),
        "error": (
            "Couldn't set up the shortcut",
            "You can try setting up the shortcut again.",
            "Try again",
        ),
    }[owner.status]
    return PortalPresentation(
        title, description, action, can_configure=action is not None
    )
