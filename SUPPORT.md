# Using Quoth

[Overview](README.md) · [Installation](#install) · [Contributing](CONTRIBUTING.md)

## Get help

Check [Troubleshooting](#troubleshooting) first. To report a bug,
[open a bug report](https://github.com/abullard1/Quoth/issues/new?template=bug-report.yml)
with your package and desktop, the steps that show the problem and a redacted
log; leave out recordings and dictated text. To suggest a feature,
[open a feature request](https://github.com/abullard1/Quoth/issues/new?template=feature-request.yml);
for a question, [open a blank issue](https://github.com/abullard1/Quoth/issues/new). Report
security or privacy problems privately, as [SECURITY.md](SECURITY.md) describes.

## Install

Quoth needs x86_64 Linux and a Wayland session; X11 is not supported.
[Desktop compatibility](#desktop-compatibility) says what each desktop supports.

### Ubuntu and Pop!_OS 24.04

Add Quoth's signed APT repository so updates arrive with the rest of the system:

```sh
sudo install -d -m 0755 /etc/apt/keyrings
sudo wget -qO /etc/apt/keyrings/quoth.gpg https://packages.quoth.dev/quoth.gpg
sudo wget -qO /etc/apt/sources.list.d/quoth.sources https://packages.quoth.dev/quoth.sources
sudo apt update && sudo apt install quoth
```

The repository holds only Quoth, and `quoth.sources` trusts its key for that repository alone.
`gpg --show-keys /etc/apt/keyrings/quoth.gpg` shows the key; its fingerprint is
`4FDF9B2D9536D35965B12B2E14213C5630FC4554`.

Or install the `.deb` from the [latest release](https://github.com/abullard1/Quoth/releases/latest)
with `sudo apt install ./quoth_*_amd64.deb`; installed this way it gets no updates.
[Check a download](#check-a-download) shows how to check it first.

### Flatpak

```sh
flatpak install https://packages.quoth.dev/quoth.flatpakref
```

This adds Quoth's signed update remote, and Flathub for the GNOME runtime if it is missing;
`flatpak update` keeps Quoth current. The `quoth.flatpak` file on the release page installs the
same app, but gets no updates and needs Flathub added first for the GNOME runtime;
[Check a download](#check-a-download) shows how to check it.

The Flatpak can run commands on the host, for text insertion, and has broad
device access for GPU and NPU support, so it is not a strong sandbox. It is not
on Flathub yet.

### Arch Linux

Build the package from Quoth's repository at the release tag, with `base-devel` and `git` installed:

```sh
git clone --branch v0.1.0 https://github.com/abullard1/Quoth.git
cd Quoth/packaging/aur && makepkg -si
```

`makepkg -s` installs the dependencies with pacman, and `-i` installs the built package. The build
downloads the release's source tarball from GitHub rather than using the cloned source, plus the
whisper.cpp, OpenVINO and ThorVG sources, which it checks against pinned checksums; it does not
check the release tarball's checksum. To check it before you build, run `makepkg --verifysource`
first, in place of `makepkg -si`: it downloads the sources and checks the pinned ones without
extracting them or running anything from them. Then check `quoth-0.1.0.tar.gz`, which it saves
beside the PKGBUILD (or in `SRCDEST`, if you set one), as [Check a download](#check-a-download)
shows, and run `makepkg -si`. To update, run `git fetch --tags`, check out the new release's tag
and build again.

On an Intel Core Ultra machine the NPU works out of the box: the package depends on Arch's
`intel-npu-driver` and `intel-npu-compiler`, and the kernel driver and firmware come with the
stock kernel and `linux-firmware`, exposed as `/dev/accel/accel0` behind the package's udev rule.

Quoth is not on the AUR yet. When its own package is published there, this section will link it;
until then, a `quoth` package on the AUR is not from this project.

### Check a download

These checks show that a file from a release page is the one Quoth published, unchanged. The
release's `SHA256SUMS` lists every other uploaded file on its release page, but not GitHub's
automatic Source code archives; download it beside your files, keeping their names, and check
them; each file you have prints `OK`:

```sh
sha256sum -c --ignore-missing SHA256SUMS
```

The `.deb`, `quoth.flatpak` and their source bundles also carry GitHub build attestations, which
show that Quoth's package workflow built them from the release tag. Checking one needs the
[GitHub CLI](https://cli.github.com/) 2.93 or later, signed in with `gh auth login`:

```sh
gh attestation verify quoth_0.1.0_amd64.deb --repo abullard1/Quoth \
  --signer-workflow abullard1/Quoth/.github/workflows/package.yml \
  --source-ref refs/tags/v0.1.0
```

`gh release verify-asset v0.1.0 FILE --repo abullard1/Quoth` checks any uploaded file,
`quoth-0.1.0.tar.gz` included, against the release's signed record. If a check fails, do not
install the file: download it again, and if it still fails, report it as
[SECURITY.md](SECURITY.md) describes.

## First launch

First launch downloads the Base speech model (82 MB, plus its 40 MB NPU encoder
where Quoth can use an Intel NPU) and guides you through an optional trial, shortcut
setup and privacy choices. When Quoth finds a GPU or NPU it can use, setup adds
a hardware optimisation step. The download continues in the background if setup
finishes first. Once the model is downloaded, transcription works offline.

## Shortcuts and commands

Use the guided shortcut setup first. **Hold while you talk** works with native
keyboard capture and with portals that report key releases. A key that cannot
report its release, such as Pause on most built-in laptop keyboards, toggles
instead of holding with native keyboard capture. Through the shortcuts portal,
as in the Flatpak, such a key's release arrives with its press, so holding it
records nothing: choose **Tap to start and stop** for it. If a portal is
unavailable, bind the toggle command in your desktop's keyboard settings; that
shortcut toggles and cannot hold.

Native shortcut capture accepts a single key or a modifier chord, such as
Ctrl+F13. Hold the modifier keys and press the main key while the capture dialog
is open.

| Action | Command |
|---|---|
| Open Settings | `quoth --settings` |
| Desktop shortcut command | `quoth --toggle` |
| Quit | `quoth --quit` |
| Start at login without a window | `quoth --background` |

In the Flatpak, replace `quoth` with `flatpak run dev.quoth.Quoth`.

Press the shortcut while transcription is running to cancel it.

The Flatpak starts at login through the desktop's Background portal, which COSMIC,
Sway and Hyprland lack; there, add `flatpak run dev.quoth.Quoth --background` to
your session's startup programs.

## Scripting

Scripts and AI agents can drive a running Quoth. These commands never start it.
In the Flatpak, run them as `flatpak run dev.quoth.Quoth --status` and so on.

| Command | What it does |
|---|---|
| `quoth --start` | Starts dictation; succeeds if Quoth is already recording |
| `quoth --stop` | Stops dictation and pastes the text; otherwise does nothing |
| `quoth --cancel` | Discards the dictation in progress |
| `quoth --status` | Prints `idle`, `recording` or `transcribing` |

They exit with 0 on success, including when there is nothing to do: `--start`
while recording, `--stop` or `--cancel` while idle. They exit with 1 and the
reason when Quoth refuses, doesn't answer within 25 seconds, or the command line
is wrong (an unknown option, or anything beside the command), and with 2 when
Quoth isn't running (start it with `quoth --background`) or there is no session
bus. A lone `quoth --quit` always exits with 0.

`quoth --stop` returns once transcription starts and doesn't report the paste,
which lands about half a second after the state returns to `idle`. Wait for it
before a script presses keys or moves focus. The text goes to the window with
keyboard focus, which for a script run in a terminal is that terminal. In a
script:

```sh
quoth --start || exit
sleep 5                    # speak now
quoth --stop || exit
while state=$(quoth --status) && [ "$state" != idle ]; do sleep 0.1; done
sleep 0.6                  # the paste lands about half a second after idle
```

### D-Bus

The same calls are the `dev.quoth.Dictation1` D-Bus interface, on the
`dev.quoth.Quoth` bus name at `/dev/quoth/Quoth`: `Start`, `Stop` and `Cancel`
methods and a `State` property. State changes arrive as `PropertiesChanged`,
which `gdbus monitor --session --dest dev.quoth.Quoth` shows. Dictated text
never crosses D-Bus. To call a method and read the state, which prints
`(<'idle'>,)` and so on:

```sh
gdbus call --session --dest dev.quoth.Quoth --object-path /dev/quoth/Quoth \
  --method dev.quoth.Dictation1.Stop
gdbus call --session --dest dev.quoth.Quoth --object-path /dev/quoth/Quoth \
  --method org.freedesktop.DBus.Properties.Get dev.quoth.Dictation1 State
```

These never start Quoth, and `gdbus` exits with 1 when it isn't running. They
reach the Flatpak from the host too, so a script that polls the state should use
them there: each `flatpak run` sets up a new sandbox.

### Access from other programs

Quoth does not check who calls it: any program running as you can start, stop or
cancel a dictation, read its state, quit Quoth or use the tray menu, including
**Copy last dictation**. A Flatpak app reaches Quoth only with the whole session
bus or permission to talk to `dev.quoth.Quoth`; grant that only to an app you
would let use your microphone and type into the focused window. A dictation
started by a script shows the same overlay, tray icon and cues as any other.
The [privacy notice](PRIVACY.md#on-your-computer) says what the clipboard and
Quoth's memory hold.

### Settings from a script

Settings are GSettings keys, and a running Quoth applies a change at once:

```sh
gsettings list-recursively dev.quoth.Quoth   # every key and its value
gsettings reset-recursively dev.quoth.Quoth  # every key back to its default
gsettings describe dev.quoth.Quoth model     # what a key means
gsettings range dev.quoth.Quoth model        # the values it accepts
gsettings set dev.quoth.Quoth language 'de'
```

Choose a model that is already downloaded; Settings downloads the others. In the
Flatpak, run gsettings inside the sandbox:
`flatpak run --command=gsettings dev.quoth.Quoth list-recursively dev.quoth.Quoth`.

### Stable interfaces

Through 0.x these stay compatible; a breaking change to the D-Bus interface would
come as `Dictation2` beside it:

- the options `--toggle`, `--settings`, `--quit`, `--background`, `--start`,
  `--stop`, `--cancel` and `--status`, the short forms `-t`, `-s` and `-q`, and
  the exit codes above;
- `dev.quoth.Dictation1`: its methods, `State` and the three state names;
- the GSettings keys, their types and accepted values (new ones may be added);
- theme and skin files with `schema = 1` and the file locations in this guide.

Not stable: `style.css` selectors and widget structure, log and message text,
environment variables other than `QUOTH_LOG_LEVEL`, GApplication action names,
and the bookkeeping keys `onboarding-complete` and `calibration-offered`.

## Troubleshooting

Quoth pastes in two steps: it copies the text to the clipboard with `wl-copy`,
then sends <kbd>Shift</kbd>+<kbd>Insert</kbd> with `ydotool` or `wtype`. When a
paste fails, Quoth says why and where the text is:

- **Needs a Wayland session.** Sign in to a Wayland session; under X11, Quoth
  can't paste or copy dictations. If you started Quoth outside the desktop
  session, start it from the desktop instead.
- **Install wl-clipboard**, or **Install wtype or ydotool**. The packages include
  these; for a source install, install them and restart Quoth.
- **Text is on your clipboard.** The paste key couldn't be sent, so paste it
  yourself. The log names the tool's error. To fix it:
  - In the Flatpak, accept the one-time `/dev/uinput` setup Quoth offers, then
    log out and back in. It needs `pkexec` on the host and a running Polkit
    authentication agent in your desktop session to show the password prompt;
    `polkitd` alone is not enough. On sessions such as Sway,
    [start your distribution's normal agent](https://github.com/swaywm/sway/wiki#wayland-wont-let-me-run-apps-as-root).
    Until setup is complete, use **Copy last dictation** from the tray and paste
    it yourself.
  - A native install pastes with `ydotool` through the package's `/dev/uinput`
    rule, falling back to `wtype`: check that you haven't masked the rule.
- **Try dictating again.** The clipboard couldn't be written, so the text was
  neither pasted nor copied. If it keeps failing, the log names wl-copy's error.

If Orca is running and nothing is pasted, with no error from Quoth, see
[Screen readers](#screen-readers).

Quoth records from **Settings → Dictation → Audio → Microphone**, where
**Auto** follows your system's default input, which can be a Bluetooth
headset's own microphone once it connects. That microphone works only in the
headset's call profile, which drops playback to call quality, so choose
another microphone to keep the headphones in high-quality playback.

Model downloads, the update check and error reports follow the `https_proxy`
environment variable, not your desktop's proxy settings. Behind a proxy, set it
before starting Quoth; for the Flatpak, run
`flatpak override --user --env=https_proxy=http://proxy:port dev.quoth.Quoth`.

Quoth's log is `~/.local/state/quoth/quoth.log`, or
`$XDG_STATE_HOME/quoth/quoth.log` when that is set; the Flatpak keeps it in
`~/.var/app/dev.quoth.Quoth/.local/state/quoth/`. It never contains dictated
text. If Quoth uses a different device from the one you chose, the log shows the
Vulkan and Intel NPU state it detected. `quoth --version` prints the version to
include in a report.

## Desktop compatibility

Text insertion and the recording overlay depend on your desktop, and the
focused application must accept <kbd>Shift</kbd>+<kbd>Insert</kbd> for
automatic paste.

| Wayland desktop | Text insertion | Recording feedback |
|---|---|---|
| COSMIC, Hyprland, niri, wlroots (such as Sway) | `ydotool`, else `wtype` | Pill in the native packages; tray and notifications in the Flatpak |
| KDE Plasma | `ydotool` | Pill |
| GNOME | `ydotool` | Tray and notifications; no pill |

The native packages give `ydotool` its `/dev/uinput` access; the Flatpak needs a
one-time setup for it, which Quoth offers when it starts. GNOME needs an
AppIndicator extension for the tray. Without it, launching Quoth opens Settings
and notifications still work.

### Tested combinations

Automated desktop tests have covered:

- `.deb`: Pop!_OS/COSMIC and Ubuntu/GNOME.
- Arch package: Sway and Plasma.
- Direct Flatpak: Pop!_OS/COSMIC and Fedora/GNOME.

### Permissions and known limitations

Native shortcut capture needs read access to `/dev/input`, a separate permission
from paste through `/dev/uinput`. That access can expose every input device, so
Quoth does not assume you are in the `input` group. If no keyboard can be
opened, bind `quoth --toggle` in your desktop's keyboard settings instead.

> [!CAUTION]
> The `/dev/uinput` access from the `.deb` or Arch package, or from the
> Flatpak's guided setup, belongs to whoever is signed in at the computer's own
> screen, not to one account, and to every program they run, other Flatpak apps
> with device access included. A program started by one such user can keep that
> access after they sign out or switch users, and type into a later session. On
> a computer shared with users you do not trust, turn it off with
> `sudo ln -sf /dev/null /etc/udev/rules.d/60-quoth-uinput.rules`, then reboot.
> On GNOME and KDE, dictated text then stays on the clipboard for you to paste.

The Flatpak's sandbox limits are under [Flatpak](#flatpak).

In the Flatpak on KDE Plasma 6.0 to 6.3, as on Debian 13, System Settings
opens its Shortcuts page each time Quoth starts; Plasma 6.4 fixed this. Close
it, or use the `.deb`, whose native shortcut needs no portal. Plasma 5 can't
register the Flatpak's shortcut, so Quoth shows steps to bind
`flatpak run dev.quoth.Quoth --toggle` instead.

`wtype` can report success without pasting: under Sway with wtype 0.4, a GTK
keymap bug stops <kbd>Shift</kbd>+<kbd>Insert</kbd> reaching GTK 3 applications
such as Firefox, and on COSMIC a
[cosmic-comp bug](https://github.com/pop-os/cosmic-comp/issues/2945) can stop it
pasting anywhere. So Quoth pastes with `ydotool` where it can.

### Screen readers

With a screen reader, turn on **Recording sound** in **Settings → Feedback →
Cues** to hear when recording starts and stops.

Orca's default Desktop layout uses <kbd>Insert</kbd> and <kbd>Keypad Insert</kbd>
as its modifier keys. GNOME 48 and later, and COSMIC, hold those keys back for
Orca whether or not <kbd>Shift</kbd> is down, so Quoth's
<kbd>Shift</kbd>+<kbd>Insert</kbd> paste never reaches the app. In Orca's
Preferences, set the screen reader modifier keys to Keypad Insert alone or to
Caps Lock, and paste works with Orca running.

## Models, languages and acceleration

Choose Auto-detect to use your model's full language range: 99 languages with
Tiny, Base or Small, and 100 with Turbo, including Cantonese. Settings
also offers 19 named spoken-language choices. The interface is in English.

Tiny is fastest but less accurate; Base is the balanced default for everyday
use; Small can improve recognition at a larger storage and CPU cost. Turbo
(Whisper large-v3-turbo) offers high accuracy and benefits from GPU or NPU
acceleration, but has no translation support. Accuracy also depends on the
language, microphone, background noise and accent.

| Model | Download | Optional NPU encoder | English translation |
|---|---:|---:|---|
| Tiny | 44 MB | 16 MB | Yes |
| Base (default) | 82 MB | 40 MB | Yes |
| Small | 264 MB | 169 MB | Yes |
| Turbo | 874 MB | 1.2 GB | No |

Translation always outputs English. It can miss or change meaning even when the
transcription is correct, so check the result.

The NPU runs the speech encoder; the CPU still decodes the text.

GPU acceleration requires a working Vulkan driver. NPU acceleration requires
compatible Intel hardware, a compatible host kernel, firmware and device, plus
Intel's NPU driver and compiler, which the `.deb` and the Flatpak bundle and
use even where Intel's packages are installed. On Arch, install
`intel-npu-driver` and `intel-npu-compiler`. The `.deb` and Arch package give
the account signed in at the computer access to the NPU. If your account still
can't open `/dev/accel/accel0`, as in the Flatpak on Debian 13 or Ubuntu 24.04,
run `sudo usermod -aG render "$USER"` and log in again.
Settings shows the acceleration available on your system. When OpenVINO fails
to start, Quoth uses the GPU, else the CPU. When the GPU can't run the model,
as on Intel Gen7 graphics (Ivy Bridge, Haswell, Bay Trail), it uses the CPU.
Either way, it tells you.

## Customise Quoth

Open **Settings → Feedback → Overlay** to choose **Default**, **Catppuccin
Mocha**, **Dracula** or **Nord**, preview the result, and switch between the
default **Pill** overlay and the audio-reactive **Orb** (Experimental). Overlay
availability depends on your desktop and package; see
[Desktop compatibility](#desktop-compatibility).

Quoth has two independent appearance files:

- **Overlay themes** control the Pill and Orb.
- **App skins** control Settings, onboarding and About, not the overlay.

Picking a preset replaces the whole active theme, including custom geometry,
position, font and motion; the three colour presets leave those at their
defaults. There is no visual theme editor.

For a custom theme, download the
[Default theme TOML](src/quoth/themes/default.toml), which sets every key (on
GitHub, choose **Download raw file**). Save it as `theme.toml` in the directory
for your install:

| Install | Active theme | Reusable themes |
|---|---|---|
| Native | `~/.config/quoth/theme.toml` | `~/.config/quoth/themes/*.toml` |
| Flatpak | `~/.var/app/dev.quoth.Quoth/config/quoth/theme.toml` | `~/.var/app/dev.quoth.Quoth/config/quoth/themes/*.toml` |

Native installs follow `XDG_CONFIG_HOME` in place of `~/.config` when it is set.
Saving the active `theme.toml` updates the overlay live. If the file can't be
loaded, Quoth keeps the last working theme, or Default at startup, and leaves the
file for you to fix. Invalid values fall back to their defaults and numbers out
of range are clamped; the log names each one. Imported and reusable themes with
invalid values are rejected or left out of the picker.

Each setting is optional. Colours accept `#RRGGBB` or `#RRGGBBAA`, where `AA`
sets opacity. In the Orb, background alpha is ignored and alpha on the other
colours sets how strongly they show.

| Group | Settings |
|---|---|
| Colours and opacity, `[colors]` | `background`, `border`, `accent`, `text`, `dot_recording`, `dot_transcribing`, `dot_error` |
| Pill geometry, `[geometry]` | `width` (height–2000 px), `height` (24–200 px), `border_width` (0–20 px) |
| Pill waveform, `[geometry]` | `bar_gap` (0–50 px; smaller is denser), `line_width` (0.1–20 px) |
| Position, `[position]` | `anchor`: `top-left`, `top-center`, `top-right`, `center-left`, `center`, `center-right`, `bottom-left`, `bottom-center` or `bottom-right`; `margin`: one integer or `[top, right, bottom, left]` |
| Pill font, `[font]` | `family` (a fontconfig family or alias), `size` (6–72 px) |
| Motion, `[animation]` | `enabled`; `duration_scale` (0.1–10); `[animation.entry]` and `[animation.exit]` each accept `duration` (0–5 seconds) and `easing` |
| Metadata, `[meta]` | `name`, `author`, `description`; these label a theme in Settings and do not alter its appearance |

Valid easing names are `linear`, plus `ease-in-*`, `ease-out-*` and
`ease-in-out-*` for `sine`, `quad`, `cubic`, `quart`, `expo`, `circ` and
`back`. Text that doesn't fit a compact pill is shortened or hidden, never
shrunk. Desktop reduced-motion settings override theme motion. Colours, position
and motion apply to both overlay styles; geometry, waveform and font apply only
to Pill.

A theme may set a top-level `schema = 1`; any other value is rejected. Without
`schema`, an imported or reusable theme must set at least one appearance
setting. Unknown keys are ignored.

To keep or share a theme, use **Import…** in the Overlay picker or put the file
in the `themes/` directory from the table. Import adds an unchanged copy to the
picker without applying it, up to 50 themes of 256 KiB each; select its row to
apply it, which replaces the whole active file. Choose **Default**, or delete
`theme.toml`, to go back to the default theme.

The active `theme.toml` can be a symlink: applying writes to its target, and
edits to the target apply live.

### App skins

Choose skins under **Settings → General → Appearance → App skin**: Default, the
bundled Nord, Dracula and Catppuccin presets, or your own. The active file is
`skin.toml`, and reusable `.toml` skins live in `skins/` beside the theme paths
in the table above. A skin sets colours only; a minimal one is:

```toml
kind = "skin"

[dark]
window = "#08080A"
text = "#F2F0EA"
accent = "#F2F0EA"
on_accent = "#0E0E11"
```

`[light]` and `[dark]` are optional. Each accepts opaque `#RRGGBB` values for
`window`, `view`, `card`, `popover`, `text`, `accent`, and `on_accent`; omitted
roles inherit the scheme default. An optional `[meta]` table with `name`,
`author` and `description` labels the skin in Settings. Quoth follows the system
light/dark scheme. While high contrast is on, Quoth uses the system colours.

Import works as for themes, up to 50 skins, and a skin file can be at most
256 KiB. Applying replaces `skin.toml`, and choosing **Default** removes it. An
invalid `skin.toml` stays on disk while Quoth shows the last valid skin, or
Default at startup. Unlike `theme.toml`, applying over a `skin.toml` symlink
replaces the link itself, not its target. An active skin that matches no row in
the picker shows as Custom, and Quoth asks before replacing it.

Advanced GTK styling can go in `style.css` beside `theme.toml`. It needs a
restart and styles standard windows such as Settings, not the Pill or Orb.
Quoth's widget structure and `quoth-*` style classes may change in any release.

## Uninstall

Quit Quoth first (`quoth --quit`, or `flatpak run dev.quoth.Quoth --quit`).
Removing a package keeps what Quoth stored for you: settings, downloaded models
(up to 2.7 GB with all four and their NPU encoders), themes, skins, logs,
calibration and your dictation count. A shortcut you bound in your desktop's
keyboard settings stays there.

### Remove the .deb or Arch package

If `dconf` is missing (on Ubuntu and Debian it comes in the `dconf-cli`
package), first run `gsettings reset-recursively dev.quoth.Quoth` while the
package is still installed. Then remove the package:

```sh
sudo apt purge quoth && sudo apt autoremove   # .deb
sudo pacman -Rns quoth                        # Arch
```

Either command deletes every file the package installed, including its
`/dev/uinput` rule, but the access the rule granted lasts until you reboot. If
you masked the rule, delete the mask with
`sudo rm -f /etc/udev/rules.d/60-quoth-uinput.rules`.

Then delete the login entry, the settings and Quoth's files:

```sh
rm -f ~/.config/autostart/dev.quoth.Quoth.desktop
dconf reset -f /dev/quoth/Quoth/
rm -rf ~/.config/quoth ~/.local/share/quoth ~/.local/state/quoth ~/.cache/quoth
```

Where `XDG_CONFIG_HOME`, `XDG_DATA_HOME`, `XDG_STATE_HOME` or `XDG_CACHE_HOME`
is set, Quoth uses it in place of `~/.config`, `~/.local/share`,
`~/.local/state` or `~/.cache`.

If you added Quoth's APT repository, remove it too:
`sudo rm -f /etc/apt/sources.list.d/quoth.sources /etc/apt/keyrings/quoth.gpg`.

### Remove the Flatpak

```sh
flatpak uninstall --delete-data dev.quoth.Quoth
```

`--delete-data` deletes `~/.var/app/dev.quoth.Quoth`, which holds all of the
Flatpak's settings, models and other files, and resets its permissions. The
update remote that `quoth.flatpakref` added goes with the app.
`flatpak uninstall --unused` then removes runtimes no installed app needs, such
as the GNOME runtime Quoth used.

Two files on the host outlive the app: its login entry, and the `/dev/uinput`
rule that Quoth's one-time typing setup installs. Delete both, then reboot to
end the access the rule granted:

```sh
rm -f ~/.config/autostart/dev.quoth.Quoth.desktop
sudo rm -f /etc/udev/rules.d/60-quoth-uinput.rules
sudo udevadm control --reload-rules
```
