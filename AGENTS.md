# Quoth

Offline, private, instant voice dictation for Linux. Press a key, speak, text appears in the focused window. Runs on the CPU everywhere, and on a Vulkan GPU or an Intel NPU where one is present.

## Project Identity

- **Repository**: https://github.com/abullard1/Quoth
- **License**: GPL-3.0-or-later
- **Python**: 3.12+ (use `uv` for all tooling)
- **API floors**: GTK 4.14, libadwaita 1.5 and GLib 2.80, as on Ubuntu 24.04; use no newer GTK, libadwaita or GLib API without a version or feature check
- **Scope**: A nested `AGENTS.md` closer to the files being edited wins over this root file; keep this file to what holds across the whole repository
- **Contributing**: source setup, checks, test conventions, commit style and the AI-assistance rule are in `CONTRIBUTING.md`

## Security

- **Never commit secrets**: no tokens, API keys, credentials, or signing keys in any file, commit message, or log. They are provisioned outside the repository (CI secrets, local keyrings).
- **Keep this file public-safe.** It ships in the public repository: no internal hostnames, private paths, personal details, or unpublished release material here; internal working docs live in private files that are not exported.

## Architecture

The import contract in `pyproject.toml` sorts every module into six tiers (`entry`, `app`, `ui`, `services`, `adapters`, `domain`) and enforces them with `uv run lint-imports`; `pyproject.toml` and module docstrings are the live module map. Core boundaries:

- **Composition and state**: `app.py` composes the application and owns component lifecycle; `recording.py` (`RecordingController`) owns the `IDLE` → `RECORDING` → `TRANSCRIBING` → `IDLE` state machine.
- **Single sources of truth**: `data/dev.quoth.Quoth.gschema.xml` owns settings keys, defaults and valid values; `accel.py` defines what `CPU`/`GPU`/`NPU` backends mean; `models.py` owns the model catalogue; `downloads.py` owns model downloads.
- **Appearance**: overlay themes live in `theme.py` (Pill or Orb layouts), while app skins are colour-only and separate (`skin.py` and `brand.py`).
- **Privacy boundaries**: `crash_reports.py` (opt-in) is the only module permitted to touch `sentry_sdk`, and `log.py` keeps transcript text off disk.

**Flow**: hotkey press/release (native evdev, Flatpak GlobalShortcuts portal, or `quoth` CLI / D-Bus) → shared trigger rule → `RecordingController` → `AudioPipeline` (PulseAudio/PipeWire float32 16 kHz mono) → `whisper.cpp` on a background thread (cancellable) → clipboard paste (`wl-copy` + `Shift+Insert` via `ydotool` or `wtype`).

## Critical Invariants (DO NOT BREAK)

These guard against crashes, memory corruption and lost dictations.

- **Never hide the overlay with `set_visible(False)` while layer-shell is active**: it crashes COSMIC with a "Broken pipe". Use `hide_pill()`, which renders transparent instead.
- **Never create/destroy the GStreamer pipeline between recordings**: create once, destroy on shutdown. Gate delivery with the software `_recording` flag. While neither a recording nor the level meter needs it, a Pulse or auto source is stopped (NULL), closing the microphone; pipewiresrc stays PLAYING, since its PAUSED→PLAYING resume is unreliable. Two exceptions, both while idle: a microphone change rebuilds it through `RecordingController.apply_mic_source()`, deferred until the engine is idle; and `warm_up()`, at startup and after such a change, may rebuild the cold pipeline to beat pipewiresrc's first-buffer race (it runs only when the selected source is pipewiresrc, which the audio source seam decides) and self-aborts once `_recording` is true.
- **Never call whisper struct-by-value functions from ctypes**: that needs a Python copy of the struct's layout, which whisper.h changes between releases, and a stale copy corrupts memory without an error. Always go through `libwhisper_helper.so` (the C shim in `lib/`), which builds those structs against the header.
- **Touch GTK only on the main thread**: GTK4 is single-threaded. Background threads (evdev, GStreamer, whisper) hand UI work to it: GLib-free code such as `RecordingController` through the injected scheduler (`scheduler.py`, whose `GLibScheduler` wraps `GLib.idle_add()`), GTK code through `GLib.idle_add()`.
- **Guard the audio buffer with `RecordingController._buffer_lock`**: the GStreamer thread appends to it and the main thread drains it.
- **Drive recording only through `RecordingController`** (`start`/`stop`/`cancel`): its `_transition_to()` validates state moves. Don't reassign state outside it.
- **Keep the small delay before text injection** (`recording.TYPE_DELAY_MS`, 50 ms): without it the injected keystrokes can be dropped.

## Conventions

- **Design and refactoring**: favour strategic design over tactical patches. Every change should read as if the codebase had been designed that way from the start, with one cohesive model and clean data flow. Existing code is not fixed in stone: reshape, move, simplify or delete outdated seams first ("make the change easy, then make the easy change") rather than bolting workarounds onto them, while keeping the critical invariants, import tiers and stable user-facing interfaces intact.
- **Type hints**: Python 3.12+ style (`str | None`, `list[str]`); never `Optional`/`List`.
- **Comments**: explain what the code does and why it must be this way, never how it came to be. **No workflow leakage**: no narration of the change, the review, the alternatives considered, or the conversation that produced it; no issue, PR or decision-record references; state the reason inline instead. Write for the next reader, not the reviewer. Keep them short: the median block here is **2 lines**. No em dashes.
- **Private members**: underscore prefix (`_config`, `_state`).
- **Logging**: loguru. `INFO` milestones, `DEBUG` lifecycle detail, `WARNING` recoverable degradation, `ERROR`/`exception` failures; transcript text only ever at `TRACE`, which no sink writes to disk; lazy `{}` placeholders, never f-strings; errors in background threads and scheduled callbacks must be logged.
- **XDG base dirs**: name every location through `paths.py`, which applies the spec's rules to `XDG_CONFIG_HOME`, `XDG_STATE_HOME`, `XDG_DATA_HOME` and `XDG_CACHE_HOME`.
- **GTK/Adw**: no Glade/XML UI files; build UI programmatically. `Adw.PreferencesWindow` for settings; `Adw.StatusPage`, or `brand.status_column` when the visual is a widget, for first-launch pages. `gi.require_version()` before importing any namespace other than GLib, GObject and Gio.
- **Error handling**: graceful fallbacks (layer-shell unavailable → pill suppressed, state via tray and notifications; a device that can't run the model → the next one, down to the CPU). `logger.exception()` on failure; user-facing toasts for recoverable errors.

## Native Dependencies

- **whisper.cpp**: `libwhisper.so` and the C shim `libwhisper_helper.so`, built into `lib/` by `make build` (a source checkout also finds `~/whisper.cpp/build/bin/`). The built `.so` files are gitignored.
- **GStreamer**: the base and good plugins plus PipeWire's plugin, for capture in `audio.py` as float32 16 kHz mono.
- **gtk4-layer-shell**: the overlay, where the compositor grants layer-shell (`presentation.py`). Elsewhere the pill is suppressed and the tray carries state, never a regular window, which would take focus and break the paste.
- **Paste**: `wl-copy` (wl-clipboard) puts the text on the clipboard, then `ydotool` (kernel `uinput`, needing `/dev/uinput` access; 1.x also needs `ydotoold`, which Quoth starts unless one runs) or `wtype` (the compositor's virtual keyboard) sends Shift+Insert. Neither types text. Native installs try ydotool first; the Flatpak does once the host's `/dev/uinput` is writable.
- **OpenVINO**: optional; runs the encoder on an Intel NPU. GPU acceleration is Vulkan through GGML.
- **ThorVG** (`libthorvg`): renders onboarding's Lottie animations, built into `lib/` by `make vendor-thorvg`. Without it the animations draw nothing and onboarding still works.
- **libportal**: the Flatpak's GlobalShortcuts session and its Background portal request for launch at login; the Flatpak bundles it with GIR support.

## Development Commands

Run `uv run pytest`, `uv run ruff check .`, `uv run ruff format --check .` and `uv run lint-imports` after code changes (plus `uv run zizmor .` when a workflow changes, and `uv run quoth --help` when CLI flags change):

```bash
uv run quoth                       # Run the app
uv run quoth -s                    # Open the Settings window
uv run quoth -q                    # Quit running instance
uv run quoth -t                    # Toggle dictation, starting Quoth if needed
uv run quoth --status              # Print the running instance's state (also --start/--stop/--cancel)
uv run pytest                      # Run the test suite (skips widget tests without a display)
dbus-run-session -- xvfb-run -a env GDK_BACKEND=x11 GSK_RENDERER=cairo GTK_A11Y=test \
  uv run --locked --group icons pytest -ra --cov=quoth  # The headless suite as CI runs it
uv run ruff check .                # Lint (matches CI)
uv run ruff format --check .       # Check formatting (matches CI)
uv run lint-imports                # Enforce the layered import contract
make schemas                       # Compile the settings schema for source runs; rerun after editing it
QUOTH_LOG_LEVEL=DEBUG uv run quoth # Verbose console logging
```
