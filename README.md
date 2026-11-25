<p align="center">
  <a href="https://quoth.dev">
    <picture>
      <source media="(max-width: 640px)" srcset=".github/media/banner-mobile.png">
      <img src=".github/media/banner.png" alt="Quoth: AI that listens. AI that understands." width="100%">
    </picture>
  </a>
</p>

<h1 align="center">Quoth: offline, private voice dictation for Linux</h1>

<p align="center">
  Press a key, speak, and your words are pasted into the focused app.<br>
  It works like Wispr Flow, without sending your voice anywhere.
</p>

<p align="center">
  <a href="https://github.com/abullard1/Quoth/releases/latest"><img src="https://img.shields.io/github/v/release/abullard1/Quoth?display_name=tag&sort=semver&style=flat-square&label=release&labelColor=08080A&color=3A3A42" alt="Latest release" height="20"></a>
  <a href="https://github.com/abullard1/Quoth/actions/workflows/ci.yml"><img src="https://img.shields.io/github/actions/workflow/status/abullard1/Quoth/ci.yml?branch=main&style=flat-square&label=CI&labelColor=08080A" alt="CI status on main" height="20"></a>
  <a href="https://github.com/abullard1/Quoth/actions/workflows/package.yml"><img src="https://img.shields.io/github/actions/workflow/status/abullard1/Quoth/package.yml?branch=main&style=flat-square&label=packages&labelColor=08080A" alt="Packages build status on main" height="20"></a>
  <a href="LICENSE"><img src="https://img.shields.io/github/license/abullard1/Quoth?style=flat-square&label=license&labelColor=08080A&color=3A3A42" alt="Licence: GPL-3.0-or-later" height="20"></a>
  <a href="#compatibility"><img src="https://img.shields.io/badge/Linux-Wayland_x86__64-3A3A42?style=flat-square&labelColor=08080A" alt="Linux Wayland x86_64" height="20"></a>
</p>

<p align="center">
  <a href="#install">Install</a> ·
  <a href="#how-it-works">How it works</a> ·
  <a href="#privacy">Privacy</a> ·
  <a href="SUPPORT.md">Support</a>
</p>

## Install

<p align="center">
  <a href="https://github.com/abullard1/Quoth/releases/download/v0.1.0/quoth_0.1.0_amd64.deb"><picture><source media="(prefers-color-scheme: dark)" srcset=".github/media/downloads/deb-dark.svg"><img src=".github/media/downloads/deb-light.svg" alt="Download .deb: setup for Ubuntu 24.04 and Pop!_OS 24.04" width="264" height="68"></picture></a>
  <a href="https://github.com/abullard1/Quoth/releases/download/v0.1.0/quoth.flatpak"><picture><source media="(prefers-color-scheme: dark)" srcset=".github/media/downloads/flatpak-dark.svg"><img src=".github/media/downloads/flatpak-light.svg" alt="Download quoth.flatpak for supported Wayland desktops" width="264" height="68"></picture></a>
  <a href="SUPPORT.md#arch-linux"><picture><source media="(prefers-color-scheme: dark)" srcset=".github/media/downloads/aur-dark.svg"><img src=".github/media/downloads/aur-light.svg" alt="Build with makepkg: setup for Arch Linux" width="264" height="68"></picture></a>
</p>

[Install &amp; update guides →](https://quoth.dev/install) · [Compatibility](#compatibility)

## How it works

Focus any input field. **Tap your shortcut to start and stop recording**, or choose
**Hold while you talk** in Settings. Quoth transcribes locally with whisper.cpp and pastes into the focused app.

<p align="center">
  <picture>
    <source media="(max-width: 640px) and (prefers-reduced-motion: reduce)" srcset=".github/media/workflow-mobile.png">
    <source media="(max-width: 640px)" srcset=".github/media/workflow-mobile.webp">
    <source media="(prefers-reduced-motion: reduce)" srcset=".github/media/workflow.png">
    <img src=".github/media/workflow.webp" alt="Tap or hold your shortcut to dictate into a document, message or terminal. Text is pasted after transcription finishes." width="820">
  </picture>
</p>

## Privacy

<p align="center">
  <picture>
    <source media="(max-width: 640px) and (prefers-reduced-motion: reduce)" srcset=".github/media/privacy-mobile.png">
    <source media="(max-width: 640px)" srcset=".github/media/privacy-mobile.webp">
    <source media="(prefers-reduced-motion: reduce)" srcset=".github/media/privacy.png">
    <img src=".github/media/privacy.webp" alt="Your voice stays yours: 100% on-device transcription with no account required, and privacy by default." width="820">
  </picture>
</p>

Quoth connects to the internet only for:

- **Model downloads** when you choose a speech model.
- **Update checks** (off by default).
- **Crash reports** (off by default, never containing audio or dictated text; see the [privacy notice](PRIVACY.md)).

[What Quoth keeps on your computer →](PRIVACY.md#on-your-computer)

## Languages and models

Supports up to **100 languages** with auto-detect, and translates speech into English.

<p align="center">
  <picture>
    <source media="(max-width: 640px) and (prefers-reduced-motion: reduce)" srcset=".github/media/languages-mobile.png">
    <source media="(max-width: 640px)" srcset=".github/media/languages-mobile.webp">
    <source media="(prefers-reduced-motion: reduce)" srcset=".github/media/languages.png">
    <img src=".github/media/languages.webp" alt="Examples of speech translated into English, starting with a German train announcement." width="820">
  </picture>
</p>

### Choose your model

Base is installed by default; pick a smaller or larger Whisper model in Settings. Once downloaded, your model works completely offline.

<p align="center">
  <picture>
    <source media="(max-width: 640px) and (prefers-reduced-motion: reduce)" srcset=".github/media/models-mobile.png">
    <source media="(max-width: 640px)" srcset=".github/media/models-mobile.webp">
    <source media="(prefers-reduced-motion: reduce)" srcset=".github/media/models.png">
    <img src=".github/media/models.webp" alt="Choose Tiny, Base (default), Small or Turbo in Settings. Approximate model downloads: 44 MB, 82 MB, 264 MB and 874 MB." width="820">
  </picture>
</p>

[Languages, translation and model guide →](SUPPORT.md#models-languages-and-acceleration)

## Hardware acceleration

Run on CPU, or use a compatible Vulkan GPU or Intel NPU to accelerate transcription.

<p align="center">
  <picture>
    <source media="(max-width: 640px) and (prefers-reduced-motion: reduce)" srcset=".github/media/acceleration-mobile.png">
    <source media="(max-width: 640px)" srcset=".github/media/acceleration-mobile.webp">
    <source media="(prefers-reduced-motion: reduce)" srcset=".github/media/acceleration.png">
    <img src=".github/media/acceleration.webp" alt="Animation cycling through three local paths: Intel NPU speech encoding with CPU text decoding, Vulkan GPU encoding and decoding, and CPU encoding and decoding." width="820">
  </picture>
</p>

[Hardware requirements →](SUPPORT.md#models-languages-and-acceleration)

## Overlays and themes

Choose an overlay style, theme, or window skin in Settings; changes appear live.

### Overlay styles

<p align="center">
  <picture>
    <source media="(max-width: 640px) and (prefers-reduced-motion: reduce)" srcset=".github/media/overlays-mobile.png">
    <source media="(max-width: 640px)" srcset=".github/media/overlays-mobile.webp">
    <source media="(prefers-reduced-motion: reduce)" srcset=".github/media/overlays.png">
    <img src=".github/media/overlays.webp" alt="Pill recording overlay with a moving waveform and timer, next to the Orb (Experimental) overlay with flowing monochrome colour" width="820">
  </picture>
</p>

### Themes and presets

Four presets, or your own colours, layout and motion.

<p align="center">
  <picture>
    <source media="(max-width: 640px) and (prefers-reduced-motion: reduce)" srcset=".github/media/themes-mobile.png">
    <source media="(max-width: 640px)" srcset=".github/media/themes-mobile.webp">
    <source media="(prefers-reduced-motion: reduce)" srcset=".github/media/themes.png">
    <img src=".github/media/themes.webp" alt="Animated gallery cycling through Default, Nord, Catppuccin Mocha and Dracula, with wide material cards, soft coloured light and the theme name below." width="820">
  </picture>
</p>

- **Look:** colours and transparency.
- **Layout:** size, position, waveform and typography.
- **Motion:** timing, easing, speed and reduced motion.

Choose or import a theme in Settings, or edit the active theme file.

[Customisation guide](SUPPORT.md#customise-quoth) · [Example theme](src/quoth/themes/default.toml)

## Compatibility

Native packages show the floating recording pill on COSMIC, KDE Plasma, Hyprland, niri, and Sway; Flatpak shows it on KDE Plasma. On other desktops (including GNOME), the tray icon and notifications show recording state. See [Desktop compatibility →](SUPPORT.md#desktop-compatibility) for details.

## Help and contribution

- Text does not appear? See [Troubleshooting](SUPPORT.md#troubleshooting).
- [Get help](SUPPORT.md#get-help), [report a bug](https://github.com/abullard1/Quoth/issues/new?template=bug-report.yml)
  or [suggest a feature](https://github.com/abullard1/Quoth/issues/new?template=feature-request.yml).
- [Script Quoth](SUPPORT.md#scripting) from a terminal, D-Bus or an AI agent.
- [Report a security or privacy issue privately](SECURITY.md).
- [Contribute](CONTRIBUTING.md): setup, checks and architecture. See the [design guide](DESIGN.md) for the visual language.
- Everyone taking part follows the [Code of Conduct](CODE_OF_CONDUCT.md).
- [Sponsor Quoth](https://quoth.dev/sponsors).

Quoth is built with AI assistance.

## Acknowledgements

Quoth is built with [GTK 4](https://gtk.org),
[libadwaita](https://gnome.pages.gitlab.gnome.org/libadwaita/),
[PyGObject](https://gitlab.gnome.org/GNOME/pygobject),
[GStreamer](https://gstreamer.freedesktop.org),
[gtk4-layer-shell](https://github.com/wmww/gtk4-layer-shell),
[OpenVINO](https://github.com/openvinotoolkit/openvino),
[ThorVG](https://github.com/thorvg/thorvg) and
[libportal](https://github.com/flatpak/libportal), on top of whisper.cpp and the
Whisper models. It pastes with [wl-clipboard](https://github.com/bugaevc/wl-clipboard),
[ydotool](https://github.com/ReimuNotMoe/ydotool) and
[wtype](https://github.com/atx/wtype), and detects speech with
[Silero VAD](https://github.com/snakers4/silero-vad).

## License

Copyright 2026 Samuel Ruairí Bullard. Quoth is free software under the
[GPL-3.0-or-later](LICENSE). [Third-party notices](THIRD-PARTY-LICENSES.md) cover
the bundled components and models.
