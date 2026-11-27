# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 Samuel Ruairí Bullard

"""First-run onboarding assistant.

A short, capability-aware setup flow shown once (gated by `config.onboarding_complete`)
that takes a new user from "what is this" to their first dictation: Welcome -> Try it
-> Optimize (only with an accelerator) -> Shortcut -> Privacy -> Ready. There is no
model step: the schema's default model starts downloading as onboarding opens, and the
picker lives in Settings, so the flow never asks a question a new user cannot answer.

The spine is an Adw.NavigationView. The shortcut and ready steps are channel-specific:
a desktop portal owns the key in supported sandboxes, native channels use evdev, and the
manual fallback guides binding `quoth --toggle`; an external binding is confirmed live
when it fires (`notify_toggle_detected`, called by app.py). The one-time uinput
setup is not a step: once onboarding ends, `typing_setup.TypingSetupDialog` offers it
where the Flatpak lacks host /dev/uinput access.
"""

from __future__ import annotations

from collections.abc import Callable
from importlib import resources
from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, GLib, GObject, Gtk
from loguru import logger

from quoth import (
    APP_ID,
    SUPPORT_URL,
    WEBSITE_URL,
    brand,
    crash_reports,
    hotkey_capture,
    presentation,
    shortcut_setup,
)
from quoth.about import PRIVACY_URL, open_uri
from quoth.accel import has_accelerator
from quoth.animation import (
    HALO_REACH_PX,
    AnimationView,
    Breakwater,
    Halo,
    Ripple,
)
from quoth.backdrop import Backdrop
from quoth.calibration import DoneFn, ProgressFn
from quoth.config import Config
from quoth.download_format import format_download_progress
from quoth.downloads import DownloadCoordinator, Subscription
from quoth.listener import keyboard_available
from quoth.models import MODELS_BY_NAME, ModelManager
from quoth.optimize import OptimizePage
from quoth.recording import RecordingController
from quoth.resolution import provision_for
from quoth.scheduler import guarded
from quoth.trigger import PUSH_TO_HOLD, TAP_TO_TOGGLE, hotkey_action
from quoth.try_it import TryItPage

# The welcome centres one column, and becomes the site's hero once there is room both
# ways. A short window takes the compact sizes at any width; it is registered last,
# since the last matching breakpoint wins. Sizes are (mark width, headline size class,
# action width); thresholds are in sp, so they follow the text scale.
_NARROW = "max-width: 480sp"
_WELCOME_HERO = "min-width: 1100sp and min-height: 620sp"
_SHORT = "max-height: 560sp"
_WELCOME_SIZES = {
    "compact": (260, "compact", 240),
    "default": (340, "", 320),
    "hero": (480, "roomy", 240),
}
_HERO_WIDTH = 1160
_INTRO_ASPECT = 492 / 1072
# The Shortcut page's bigger cap once there is room both ways. Its size names are CSS
# classes on the column, which brand.py sizes the cap from.
_SHORTCUT_ROOMY = "min-width: 760sp and min-height: 680sp"
# The Ready page's mark: the loader clip draws the Q by frame 27 of 191 and holds
# it to frame 66 before undrawing; stopping at 36 rests on the finished mark.
_READY_MARK_END = 36 / 191
_READY_MARK_PX = 128
_PRIVACY_RING_PX = 220
_PRIVACY_ROOMY = "min-width: 700sp and min-height: 500sp"
# The copy arriving after the mark lands: each line rises a little as it fades
# in, one after the other, the same arrival quoth.dev gives its pages.
_ARRIVE_MS = 480
_ARRIVE_STAGGER_MS = 110
_ARRIVE_RISE_PX = 14
# The smallest the window may be dragged to and still be worth looking at.
_MIN_WIDTH = 400
_MIN_HEIGHT = 420


def _backdrop(name: str) -> Path:
    """Return a bundled onboarding background."""
    return Path(str(resources.files("quoth") / "backgrounds" / name))


def _busy_button(
    label: str, busy_label: str = "Starting…"
) -> tuple[Gtk.Button, Callable[[], None]]:
    """Return a button and a `set_busy()` that disables it behind a spinner.

    The spinner replaces the label, beside `busy_label`.
    """
    btn = Gtk.Button(label=label)

    def set_busy() -> None:
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        row.set_halign(Gtk.Align.CENTER)
        spinner = Gtk.Spinner()
        spinner.start()
        row.append(spinner)
        row.append(Gtk.Label(label=busy_label))
        btn.set_child(row)
        btn.set_sensitive(False)

    return btn, set_busy


def _breakpoint(
    condition: str, setters: list[tuple[Gtk.Widget, str, object]]
) -> Adw.Breakpoint:
    breakpoint = Adw.Breakpoint.new(Adw.BreakpointCondition.parse(condition))
    for widget, prop, value in setters:
        breakpoint.add_setter(widget, prop, _boxed(value))
    return breakpoint


def _boxed(value: object) -> GObject.Value:
    """Box `value` for a breakpoint setter, picking the GType from the Python type.

    A list becomes a string array; bool is tested before int, since bool is an int;
    enums carry their own GType.
    """
    if isinstance(value, list):
        return GObject.Value(GObject.TYPE_STRV, value)
    if isinstance(value, GObject.GEnum):
        return GObject.Value(type(value).__gtype__, value)
    if isinstance(value, bool):
        return GObject.Value(bool, value)
    if isinstance(value, float):
        return GObject.Value(float, value)
    return GObject.Value(int, value)


class OnboardingAssistant:
    """The first-run setup window.

    Builds pages lazily as the user advances. The Ready step sets
    `config.onboarding_complete` and calls `on_complete(on_ready)`, which finishes app
    activation asynchronously; `on_ready` is invoked on the main thread when the app is
    fully live, and the window closes then. `on_open_settings` backs the "Customize in
    Settings" button. The Try it and Optimize steps run on the app's `engine`, the
    benchmark through `start_benchmark` like the benchmark dialog's.
    """

    def __init__(
        self,
        app: Adw.Application,
        model_mgr: ModelManager,
        config: Config,
        downloads: DownloadCoordinator,
        devices: list[str],
        on_complete: Callable[[Callable[[], None]], None],
        on_open_settings: Callable[[], None],
        motion_enabled: bool = True,
        *,
        engine: RecordingController,
        start_benchmark: Callable[[list[str], list[str], ProgressFn, DoneFn], bool],
        portal_shortcuts=None,
    ):
        self._model_mgr = model_mgr
        self._config = config
        self._devices = devices
        self._on_complete = on_complete
        self._on_open_settings = on_open_settings
        self._motion_enabled = motion_enabled
        self._engine = engine
        self._start_benchmark = start_benchmark
        self._finished = False
        self._closed = False
        self._portal_shortcuts = portal_shortcuts
        self._portal_unsubscribe: Callable[[], None] | None = None

        # The live "try it" step, built when its page is first constructed.
        self._try_it: TryItPage | None = None
        # The intro plays once, on first entry to the welcome page.
        self._intro_played = False
        self._ready_shown = False
        self._welcome_arrived = False
        self._welcome_arrival_timeouts: set[int] = set()
        self._welcome_arrivals: list[Adw.TimedAnimation] = []
        self._intro: AnimationView | None = None
        self._backdrop: Backdrop | None = None
        self._halo: Halo | None = None
        self._privacy: Breakwater | None = None
        self._ready_mark: AnimationView | None = None
        self._ready_ripple: Ripple | None = None

        # The native shortcut page's demo: the key held, and dictation listening.
        self._key_down = False
        self._listening = False
        # Live shortcut-confirmation state for desktop-owned and manual shortcuts.
        self._shortcut_status: Gtk.Label | None = None
        self._shortcut_next: Gtk.Button | None = None
        self._shortcut_confirmed = False
        self._shortcut_confirmed_binding: str | None = None
        self._portal_shortcut_page: Adw.StatusPage | None = None
        self._portal_shortcut_action: Gtk.Button | None = None
        self._portal_shortcut_modes: Gtk.Box | None = None
        self._portal_manual_command: Gtk.Box | None = None
        self._portal_manual_open: Gtk.Button | None = None
        self._ready_title: Gtk.Label | None = None
        self._ready_description: Gtk.Label | None = None

        # The Optimize step, built when its page is first constructed.
        self._optimize: OptimizePage | None = None

        # Page sequence. The Optimize step is inserted only when there's an accelerator
        # to rank: a CPU-only machine has one device and nothing to choose.
        self._builders = [self._build_welcome, self._build_try_it]
        if has_accelerator(self._devices):
            self._builders.append(self._build_optimize)
        self._builders += [self._build_shortcut, self._build_privacy, self._build_ready]

        # The app's coordinator: the prefetch below and the Try it step request the same
        # model, and single-flight makes the second a join rather than a duplicate.
        self._downloads = downloads
        self._prefetch: Subscription | None = None

        logger.info("First launch: starting onboarding ({} steps)", len(self._builders))
        self._nav = Adw.NavigationView()
        # Adwaita's own animation check reads gtk-enable-animations, not the portal's
        # reduced-motion key, so the app's preference is applied to the slides here.
        self._nav.set_animate_transitions(motion_enabled)
        self._win = Adw.ApplicationWindow(application=app)
        # Fits a 768px screen with panels, and scaled text: every page scrolls, so a
        # shorter default costs nothing and avoids opening taller than the display.
        self._win.set_default_size(560, 600)
        # A floor, so the window cannot be dragged down to where the brand page
        # clips and the buttons leave the screen.
        self._win.set_size_request(_MIN_WIDTH, _MIN_HEIGHT)
        self._win.set_title("Welcome to Quoth")
        self._win.set_content(self._nav)
        self._win.connect("close-request", self._on_close_request)
        if self._portal_shortcuts is not None:
            self._portal_unsubscribe = self._portal_shortcuts.subscribe(
                self._refresh_portal_shortcut
            )
        self._page(0)
        self._win.present()
        self._prefetch_model()

    def _prefetch_model(self) -> None:
        """Start the model download as onboarding opens, so the demo need not wait.

        The welcome note shows its progress, so the fetch is never silent.
        """
        model = self._config.model
        if self._model_mgr.is_downloaded(model):
            self._welcome_note.set_visible(False)
            return
        logger.info("Onboarding: prefetching the '{}' model in the background", model)
        size = MODELS_BY_NAME[model].ggml_size_mb
        self._welcome_note.set_label(
            f"Getting ready: downloading the speech model ({size} MB)."
        )
        self._prefetch = self._downloads.start_or_join(
            model,
            provision_for(model, self._config.device, self._model_mgr),
            on_progress=lambda p: self._welcome_note.set_label(
                format_download_progress(p)
            ),
            on_done=lambda: self._welcome_note.set_label("Ready to go."),
            on_error=lambda _message: self._welcome_note.set_visible(False),
        )

    def _on_close_request(self, _win) -> bool:
        """Stop the pages' work when the window closes. Never blocks the close.

        Abandoning onboarding stops a calibration at its next cell and drops a take in
        flight; shutdown then joins the engine's workers.
        """
        if self._closed:
            return False
        self._closed = True
        if not self._finished:
            # Without the hold activation takes, the app quits once this window goes.
            logger.info(
                "Onboarding closed at step {}/{} before finishing",
                self._nav.get_navigation_stack().get_n_items(),
                len(self._builders),
            )
        self._cancel_welcome_arrivals(settle=False)
        if self._intro is not None:
            self._intro.set_motion_enabled(False)
        if self._backdrop is not None:
            self._backdrop.close()
        if self._optimize is not None:
            self._optimize.close()
        if self._try_it is not None:
            self._try_it.stop()
        if self._portal_unsubscribe is not None:
            self._portal_unsubscribe()
            self._portal_unsubscribe = None
        return False

    def present(self) -> None:
        """Bring the setup window to the front."""
        self._win.present()

    def push_level(self, amplitude: float) -> None:
        """Forward one input level from the engine to the Try it pill."""
        if self._try_it is not None:
            self._try_it.push_level(amplitude)

    def set_motion_enabled(self, enabled: bool) -> None:
        """Apply the reduced-motion preference to every page built so far."""
        if enabled == self._motion_enabled or self._closed:
            return
        self._motion_enabled = enabled
        self._nav.set_animate_transitions(enabled)
        if self._backdrop is not None:
            self._backdrop.set_motion_enabled(enabled)
        if self._try_it is not None:
            self._try_it.set_motion_enabled(enabled)
        if self._optimize is not None:
            self._optimize.set_motion_enabled(enabled)
        if self._halo is not None:
            self._halo.set_motion_enabled(enabled)
        if self._privacy is not None:
            self._privacy.set_motion_enabled(enabled)
        # Disable the ripple before settling its mark, whose completion requests it.
        if self._ready_ripple is not None:
            self._ready_ripple.set_motion_enabled(enabled)
        if self._ready_mark is not None:
            self._ready_mark.set_motion_enabled(enabled)
        if self._intro is not None:
            self._intro.set_motion_enabled(enabled)
        if not enabled:
            self._cancel_welcome_arrivals(settle=True)
            if self._intro_played:
                self._finish_intro()

    def _cancel_welcome_arrivals(self, *, settle: bool) -> None:
        for source_id in self._welcome_arrival_timeouts:
            GLib.source_remove(source_id)
        self._welcome_arrival_timeouts.clear()
        for animation in self._welcome_arrivals:
            (animation.skip if settle else animation.pause)()
        self._welcome_arrivals.clear()
        if settle and self._welcome_arrived:
            self._settle_welcome_lines()

    def _settle_welcome_lines(self) -> None:
        """Show every welcome line at rest, where its arrival would have left it."""
        for line in self._welcome_lines:
            line.set_opacity(1.0)
            line.set_margin_top(0)
            line.set_margin_bottom(_ARRIVE_RISE_PX)

    # --- navigation ---

    def _page(self, index: int) -> Adw.NavigationPage:
        """Return step `index`'s page, building and adding it on first use.

        Added pages stay in the navigation view, so each step and its work stay alive
        until the assistant closes.
        """
        tag = f"step-{index}"
        page = self._nav.find_page(tag)
        if page is None:
            page = self._builders[index]()
            page.set_tag(tag)
            self._nav.add(page)
        return page

    def _advance(self, *_args) -> None:
        if self._closed:
            return
        # Pages are pushed in order, so the stack's depth is the next page's index;
        # the back button pops without telling us, so a counter would drift.
        index = self._nav.get_navigation_stack().get_n_items()
        logger.debug(
            "Onboarding step {}/{}: {}",
            index + 1,
            len(self._builders),
            self._builders[index].__name__,
        )
        self._nav.push(self._page(index))

    def _finish(self, open_settings: bool = False) -> None:
        if self._finished or self._closed:
            return
        self._finished = True
        # Activation consumes the saved choices; earlier pages must no longer edit them.
        self._nav.replace([self._nav.get_visible_page()])
        self._config.onboarding_complete = True
        # New users had their chance in the Optimize step, so no startup offer follows.
        self._config.calibration_offered = True
        logger.info("Onboarding complete (open_settings={})", open_settings)

        def on_ready() -> None:
            # Activation has adopted a transfer still running. An abandoned one runs
            # on until the process exits, which keeps its partial file to resume.
            if self._prefetch is not None:
                self._prefetch.cancel()
            if self._closed:
                return
            self._win.close()
            if open_settings:
                self._on_open_settings()

        self._on_complete(on_ready)

    def _chrome(
        self,
        title: str,
        content: Gtk.Widget,
        actions: list[Gtk.Button] | Gtk.Widget,
        breakpoints: tuple[Adw.Breakpoint, ...] = (),
    ) -> Adw.NavigationPage:
        """Wrap a page's content and actions in one scrollable area under a header.

        `actions` is a list of buttons or a widget of the page's own. Each page owns its
        `breakpoints` so they follow its allocation, not the window's.
        """
        toolbar = Adw.ToolbarView()
        header = Adw.HeaderBar()
        header.add_css_class("flat")
        toolbar.add_top_bar(header)
        scroller = Gtk.ScrolledWindow(vexpand=True)
        scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        if isinstance(actions, list):
            actions = brand.action_row(actions)
        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        content.set_vexpand(True)
        body.append(content)
        body.append(actions)
        scroller.set_child(body)
        toolbar.set_content(scroller)
        brand.pointer_cursors(toolbar)
        if not breakpoints:
            return Adw.NavigationPage(child=toolbar, title=title)
        # A breakpoint bin needs a minimum size; under the window's, so it never
        # becomes the reason the window will not shrink.
        bin = Adw.BreakpointBin(child=toolbar, width_request=360, height_request=360)
        for breakpoint in breakpoints:
            bin.add_breakpoint(breakpoint)
        return Adw.NavigationPage(child=bin, title=title)

    # --- pages ---

    def _build_welcome(self) -> Adw.NavigationPage:
        """Build the brand page: the mark draws itself and the copy follows.

        Built without `_chrome` so the backdrop reaches the window edges: chrome keeps
        its scrolling content below a header, which would leave a strip of plain window
        above the image.
        """
        # The site's hero, in a window: tagline, the mark drawing itself, the
        # headline and one action, centred over the smoke.
        self._intro = AnimationView("intro", motion_enabled=self._motion_enabled)
        self._intro.set_halign(Gtk.Align.CENTER)

        tagline = Gtk.Label(label="Local. Private. Yours.")
        tagline.add_css_class("quoth-tagline")

        headline = Gtk.Label(
            wrap=True, use_markup=True, justify=Gtk.Justification.CENTER
        )
        headline.set_markup(
            'AI that <span weight="600">listens.</span>\n'
            'AI that <span weight="600">understands.</span>'
        )
        headline.add_css_class("quoth-headline")

        self._welcome_note = Gtk.Label(wrap=True, justify=Gtk.Justification.CENTER)
        self._welcome_note.add_css_class("caption")
        self._welcome_note.add_css_class("quoth-dim")
        self._welcome_note.set_max_width_chars(44)

        # The one action on the page, sized like the site's primary button.
        start = Gtk.Button(label="Get started")
        start.add_css_class("pill")
        start.add_css_class("suggested-action")
        start.add_css_class("quoth-cta")
        start.set_halign(Gtk.Align.CENTER)
        start.set_sensitive(False)
        start.connect("clicked", self._advance)
        self._welcome_start = start

        # The hero's second column, shown only where there is room: the site's
        # paragraph and a ghost button to the site.
        blurb = Gtk.Label(
            label=(
                "Voice dictation for Linux that runs entirely on your computer. "
                "Press a key, speak, and your words appear wherever you're typing."
            ),
            wrap=True,
            xalign=0,
            visible=False,
        )
        blurb.add_css_class("quoth-blurb")
        blurb.set_max_width_chars(40)
        site = Gtk.Button(visible=False)
        site_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        site_row.append(Gtk.Image.new_from_icon_name(f"{APP_ID}-symbolic"))
        site_row.append(Gtk.Label(label="quoth.dev"))
        site.set_child(site_row)
        site.add_css_class("pill")
        site.add_css_class("quoth-cta")
        site.add_css_class("quoth-ghost")
        site.connect("clicked", lambda _b: open_uri(WEBSITE_URL))
        actions = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        actions.set_halign(Gtk.Align.CENTER)
        actions.append(start)
        actions.append(site)

        # The copy arrives once the mark has drawn itself. Each line keeps its space
        # while invisible and carries its rise as margin, so nothing re-flows.
        self._welcome_lines = (tagline, headline, blurb, actions, self._welcome_note)
        for line in self._welcome_lines:
            line.set_opacity(0.0)
            if self._motion_enabled:
                line.set_margin_top(_ARRIVE_RISE_PX)
            else:
                line.set_margin_bottom(_ARRIVE_RISE_PX)
        self._welcome_arrivals.clear()

        # Tight spacing: the clip pads a fifth of its height around the glyphs and
        # every line carries its rise, so the drawn gaps still land near 32px. The
        # headline and actions share a row that the hero layout turns sideways.
        aside = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        aside.append(blurb)
        aside.append(self._welcome_note)
        aside.append(actions)
        row = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        row.append(headline)
        row.append(aside)
        column = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        column.set_valign(Gtk.Align.CENTER)
        column.set_margin_start(32)
        column.set_margin_end(32)
        column.set_margin_top(24)
        column.set_margin_bottom(24)
        # Reserve the tagline's height so enlarged text cannot overlap the mark.
        mark = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        mark.append(tagline)
        mark.append(self._intro)
        tagline.set_halign(Gtk.Align.CENTER)
        for child in (mark, row):
            column.append(child)

        # Clamped, since max-width-chars only caps a wrapping label's natural width:
        # without it the copy stretches to one long line on a wide window.
        clamp = Adw.Clamp(maximum_size=brand.STATUS_WIDTH, child=column)
        clamp.add_css_class("quoth-scrim")
        self._layout_welcome(
            clamp=clamp,
            column=column,
            row=row,
            aside=aside,
            mark=mark,
            tagline=tagline,
            intro=self._intro,
            headline=headline,
            note=self._welcome_note,
            blurb=blurb,
            actions=actions,
            start=start,
            site=site,
        )

        # Scrolled like every _chrome page: at small sizes or large text the column
        # has to give way rather than clip, and the backdrop stays behind it.
        scroller = Gtk.ScrolledWindow(vexpand=True)
        scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroller.set_child(clamp)

        # Plays once and rests: the clip is authored to end where it began, so the
        # rewind a finished stream performs is invisible, and it settles on the same
        # image the still shows under reduced motion.
        self._backdrop = Backdrop(
            still=_backdrop("onboarding.jpg"),
            video=_backdrop("onboarding.webm"),
            motion_enabled=self._motion_enabled,
        )

        overlay = Gtk.Overlay()
        overlay.set_child(self._backdrop)
        overlay.add_overlay(scroller)

        header = Adw.HeaderBar()
        header.add_css_class("flat")
        header.set_title_widget(Gtk.Label())  # the mark is the title here
        toolbar = Adw.ToolbarView()
        toolbar.add_css_class("quoth-welcome")
        toolbar.add_top_bar(header)
        toolbar.set_extend_content_to_top_edge(True)
        toolbar.set_content(overlay)

        brand.pointer_cursors(toolbar)
        page = Adw.NavigationPage(child=toolbar, title="Welcome")
        page.connect("shown", self._on_welcome_shown)
        # Nothing decodes once the step is behind them.
        page.connect("hidden", lambda _p: self._backdrop.stop())
        # A click anywhere on the page or any key skips ahead: a first-run animation
        # must never stand between the user and the button. The key still reaches
        # the focused widget.
        skip = Gtk.GestureClick()
        skip.connect("pressed", lambda *_: self._finish_intro(skipped=True))
        overlay.add_controller(skip)
        keys = Gtk.EventControllerKey()
        keys.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        keys.connect("key-pressed", lambda *_: self._finish_intro(skipped=True))
        overlay.add_controller(keys)
        return page

    def _layout_welcome(self, **w: Gtk.Widget) -> None:
        """Register the window breakpoints that re-lay the welcome.

        A breakpoint sets its values on entry and restores the built defaults on exit,
        so the widgets are constructed in the default (centred) layout and only the
        differences are listed here.
        """

        def sizes(size: str) -> list[tuple[Gtk.Widget, str, object]]:
            width, headline_class, cta = _WELCOME_SIZES[size]
            classes = ["quoth-headline"] + ([headline_class] if headline_class else [])
            return [
                (w["intro"], "width-request", width),
                (w["intro"], "height-request", round(width * _INTRO_ASPECT)),
                (w["headline"], "css-classes", classes),
                (w["start"], "width-request", cta),
            ]

        hero = [
            (w["clamp"], "maximum-size", _HERO_WIDTH),
            (w["column"], "valign", Gtk.Align.END),
            (w["column"], "margin-start", 48),
            (w["column"], "margin-end", 48),
            (w["column"], "margin-bottom", 40),
            (w["tagline"], "halign", Gtk.Align.START),
            (w["mark"], "halign", Gtk.Align.START),
            (w["row"], "orientation", Gtk.Orientation.HORIZONTAL),
            (w["row"], "spacing", 40),
            (w["headline"], "xalign", 0.0),
            (w["headline"], "justify", Gtk.Justification.LEFT),
            (w["headline"], "hexpand", True),
            (w["headline"], "wrap", False),
            (w["aside"], "halign", Gtk.Align.END),
            (w["aside"], "valign", Gtk.Align.END),
            (w["note"], "xalign", 0.0),
            (w["note"], "justify", Gtk.Justification.LEFT),
            (w["blurb"], "visible", True),
            (w["blurb"], "margin-bottom", 12),
            (w["actions"], "halign", Gtk.Align.START),
            (w["site"], "visible", True),
        ]

        for widget, prop, value in sizes("default"):
            widget.set_property(prop, value)
        layouts = (
            (_NARROW, sizes("compact")),
            (_WELCOME_HERO, sizes("hero") + hero),
            (_SHORT, sizes("compact")),
        )
        for condition, setters in layouts:
            self._win.add_breakpoint(_breakpoint(condition, setters))

    def _on_welcome_shown(self, _page) -> None:
        if self._closed:
            return
        # Only on first entry: a back-navigation must not replay it.
        if self._intro_played:
            self._finish_intro()
            return
        self._intro_played = True
        self._backdrop.start()
        self._intro.play(self._finish_intro)

    def _finish_intro(self, skipped: bool = False) -> None:
        if self._closed:
            return
        if skipped:
            self._intro.skip()
        if self._welcome_arrived:
            return
        self._welcome_arrived = True
        self._welcome_start.set_sensitive(True)
        if not self._motion_enabled:
            self._settle_welcome_lines()
            return
        for index, line in enumerate(self._welcome_lines):
            source_id = GLib.timeout_add(
                index * _ARRIVE_STAGGER_MS,
                guarded(self._run_arrival, on_error=self._forget_current_arrival),
                line,
            )
            self._welcome_arrival_timeouts.add(source_id)

    def _forget_current_arrival(self) -> None:
        source = GLib.main_current_source()
        if source is not None:
            self._welcome_arrival_timeouts.discard(source.get_id())

    def _run_arrival(self, line: Gtk.Widget) -> bool:
        self._forget_current_arrival()
        return self._arrive(line)

    def _arrive(self, line: Gtk.Widget) -> bool:
        """Fade one line in while it rises into place.

        GTK offers no transform on a plain widget, so the margins carry the rise: top
        shrinks as bottom grows, keeping the line's height constant so nothing below it
        shifts.
        """
        if self._closed:
            return GLib.SOURCE_REMOVE

        def step(value: float) -> None:
            if self._closed:
                return
            line.set_opacity(value)
            rise = round(_ARRIVE_RISE_PX * (1.0 - value))
            line.set_margin_top(rise)
            line.set_margin_bottom(_ARRIVE_RISE_PX - rise)

        animation = Adw.TimedAnimation.new(
            line, 0.0, 1.0, _ARRIVE_MS, Adw.CallbackAnimationTarget.new(step)
        )
        animation.set_easing(Adw.Easing.EASE_OUT_CUBIC)
        # Kept so a close or reduced motion can pause or skip it.
        self._welcome_arrivals.append(animation)
        animation.play()
        return GLib.SOURCE_REMOVE

    # The demo (try_it.py) avoids the two fragile parts of real dictation: a button
    # triggers it (not the global hotkey) and the text lands in an in-app field (not the
    # clipboard/paste path), so it behaves identically on every compositor.

    def _build_try_it(self) -> Adw.NavigationPage:
        # Skip until a take comes back with words; then the same button continues.
        nxt = Gtk.Button(label="Skip for now")
        nxt.add_css_class("flat")
        nxt.connect("clicked", self._advance)

        def on_success() -> None:
            nxt.set_label("Continue")
            nxt.remove_css_class("flat")
            nxt.add_css_class("suggested-action")

        self._try_it = TryItPage(
            self._config,
            self._model_mgr,
            self._devices,
            engine=self._engine,
            downloads=self._downloads,
            motion_enabled=self._motion_enabled,
            on_success=on_success,
        )
        page = self._chrome(
            "Try it", self._try_it.content, [nxt], breakpoints=self._try_it.breakpoints
        )
        # NavigationPage emits these on navigation either way, so the meter and any take
        # follow the page. A window close emits neither; `_on_close_request` stops it.
        try_it = self._try_it
        page.connect("shown", lambda _p: try_it.start())
        page.connect("hidden", lambda _p: try_it.stop())
        return page

    def _build_optimize(self) -> Adw.NavigationPage:
        self._optimize = OptimizePage(
            self._config,
            self._devices,
            start=self._start_benchmark,
            cancel=self._engine.cancel_calibration,
            on_advance=self._advance,
            # Earlier steps can use the engine only once the run has ended.
            on_lock=lambda locked: page.set_can_pop(not locked),
            motion_enabled=self._motion_enabled,
        )
        page = self._chrome("Optimize", self._optimize.content, self._optimize.actions)
        return page

    def _build_shortcut(self) -> Adw.NavigationPage:
        if self._portal_shortcuts is not None:
            return self._build_shortcut_portal()
        if keyboard_available(self._config.hotkey):
            return self._build_shortcut_native()
        return self._build_shortcut_sandbox()

    def _build_shortcut_native(self) -> Adw.NavigationPage:
        """Build the native shortcut page: the real key presses the cap on screen.

        The page acts out the chosen mode (tap or hold), a ring leaving the cap for as
        long as dictation would listen.
        """
        # A boxed keycap, not a ShortcutLabel, which renders US-QWERTY; hotkey_label
        # follows the user's keymap.
        self._shortcut_keys = Gtk.Label(
            label=hotkey_capture.hotkey_label(self._config.hotkey)
        )
        self._shortcut_keys.add_css_class("title-1")
        self._keycap = Gtk.Box(halign=Gtk.Align.CENTER, valign=Gtk.Align.CENTER)
        self._keycap.add_css_class("quoth-keycap")
        self._keycap.append(self._shortcut_keys)
        # The halo fills the overlay and the cap sits inset by its reach, so the
        # rings have room of their own and never reach the title below.
        self._keycap.set_margin_start(HALO_REACH_PX)
        self._keycap.set_margin_end(HALO_REACH_PX)
        self._keycap.set_margin_top(HALO_REACH_PX)
        self._keycap.set_margin_bottom(HALO_REACH_PX)
        self._halo = Halo(brand.KEYCAP_CORNER_PX, motion_enabled=self._motion_enabled)
        cap = Gtk.Overlay(child=self._keycap)
        cap.add_overlay(self._halo)
        cap.set_margin_top(24 - HALO_REACH_PX)

        column = brand.status_column(cap, "Your dictation key", "")
        cap.set_margin_bottom(0)  # the halo's room is the gap to the title
        self._shortcut_lead = column.get_last_child()

        # Tap or hold: the one choice worth making here, since the cap shows it.
        modes = self._mode_toggles()
        modes.set_margin_top(12)
        column.append(modes)

        change = Gtk.Button(label="Pick a different key", halign=Gtk.Align.CENTER)
        # Flat, so Continue stays the only primary action on the page.
        change.add_css_class("flat")
        change.set_margin_top(12)
        change.connect(
            "clicked",
            lambda *_: hotkey_capture.capture_hotkey(self._win, self._on_hotkey_chosen),
        )
        column.append(change)
        status = Adw.Clamp(maximum_size=brand.STATUS_WIDTH, child=column)

        nxt = Gtk.Button(label="Continue")
        nxt.add_css_class("suggested-action")
        nxt.connect("clicked", self._advance)

        page = self._chrome(
            "Shortcut",
            status,
            [nxt],
            breakpoints=(
                _breakpoint(_SHORT, [(column, "css-classes", ["compact"])]),
                _breakpoint(
                    _NARROW,
                    [
                        (column, "css-classes", ["compact"]),
                        (modes, "orientation", Gtk.Orientation.VERTICAL),
                    ],
                ),
                _breakpoint(_SHORTCUT_ROOMY, [(column, "css-classes", ["roomy"])]),
            ),
        )

        # The real key presses the cap. Capture phase, so a focused button does not
        # swallow it; only the configured key is taken, everything else passes.
        keys = Gtk.EventControllerKey()
        keys.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        keys.connect("key-pressed", self._on_shortcut_key_pressed)
        keys.connect("key-released", self._on_shortcut_key_released)
        page.add_controller(keys)
        # Leaving mid-take (or a release that went to another window) must not
        # leave the cap down or the ring running when the page comes back.
        page.connect("hidden", lambda _p: self._reset_shortcut_demo())
        self._reset_shortcut_demo()
        return page

    def _on_shortcut_key_pressed(self, _c, _keyval, keycode: int, state) -> bool:
        if not hotkey_capture.hotkey_matches_event(self._config.hotkey, keycode, state):
            return False
        if self._key_down:
            return True  # the key repeating while held
        self._key_down = True
        self._keycap.add_css_class("pressed")
        self._demo_hotkey(pressed=True)
        return True

    def _on_shortcut_key_released(self, _c, _keyval, keycode: int, _state) -> None:
        if not self._key_down or not hotkey_capture.hotkey_uses_keycode(
            self._config.hotkey, keycode
        ):
            return
        self._key_down = False
        self._keycap.remove_css_class("pressed")
        self._demo_hotkey(pressed=False)

    def _demo_hotkey(self, pressed: bool) -> None:
        # The rule the real hotkey follows.
        action = hotkey_action(self._config.mode, self._listening, pressed)
        if action == "start":
            self._start_listening()
        elif action == "stop":
            self._stop_listening()

    def _start_listening(self) -> None:
        self._listening = True
        self._halo.start()
        self._say_demo_lead(
            "Listening. Let go to stop."
            if self._config.mode == PUSH_TO_HOLD
            else "Listening. Tap again to stop."
        )

    def _stop_listening(self) -> None:
        self._listening = False
        self._halo.stop()
        verb = "Hold" if self._config.mode == PUSH_TO_HOLD else "Tap"
        key = hotkey_capture.hotkey_label(self._config.hotkey)
        self._say_demo_lead(f"That's it. {verb} {key} anywhere to dictate.")

    def _say_demo_lead(self, text: str) -> None:
        # Besides the halo, this line is the demo's only answer to the key.
        self._shortcut_lead.set_label(text)
        self._shortcut_lead.announce(text, Gtk.AccessibleAnnouncementPriority.MEDIUM)

    def _reset_shortcut_demo(self) -> None:
        self._listening = False
        self._halo.stop()
        self._key_down = False
        self._keycap.remove_css_class("pressed")
        self._shortcut_lead.set_label(
            "Hold it now." if self._config.mode == PUSH_TO_HOLD else "Tap it now."
        )

    def _mode_toggles(self) -> Gtk.Box:
        """Build the linked tap and hold toggles, the current mode active."""
        modes = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, halign=Gtk.Align.CENTER)
        modes.add_css_class("linked")
        tap = Gtk.ToggleButton(label=hotkey_capture.MODE_LABELS[TAP_TO_TOGGLE])
        hold = Gtk.ToggleButton(
            label=hotkey_capture.MODE_LABELS[PUSH_TO_HOLD], group=tap
        )
        (hold if self._config.mode == PUSH_TO_HOLD else tap).set_active(True)
        tap.connect("toggled", self._on_mode_toggled, TAP_TO_TOGGLE)
        hold.connect("toggled", self._on_mode_toggled, PUSH_TO_HOLD)
        modes.append(tap)
        modes.append(hold)
        return modes

    def _on_mode_toggled(self, button: Gtk.ToggleButton, mode: str) -> None:
        """Persist the mode; the trigger rule reads `config.mode` on every press."""
        if not button.get_active() or self._config.mode == mode:
            return
        self._config.mode = mode
        if self._halo is not None:  # the native page acts the mode out
            self._reset_shortcut_demo()
        logger.debug("Onboarding: mode set to '{}'", mode)

    def _on_hotkey_chosen(self, name: str) -> None:
        """Persist the captured key, update the keycap and restart the demo.

        The listener reads `config.hotkey` when it starts in `_finish_activation`, so no
        live restart is needed.
        """
        logger.debug("Onboarding: hotkey set to '{}'", name)
        self._config.hotkey = name
        self._shortcut_keys.set_label(hotkey_capture.hotkey_label(name))
        self._reset_shortcut_demo()

    def _build_shortcut_sandbox(self) -> Adw.NavigationPage:
        status = Adw.StatusPage(
            title="Bind a key to dictate",
            description=shortcut_setup.setup_steps(),
        )
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        self._append_command(box)
        self._append_open_settings(box)

        self._shortcut_status = Gtk.Label(
            label="Waiting: press your new key to test it…", wrap=True
        )
        self._shortcut_status.add_css_class("dim-label")
        box.append(self._shortcut_status)
        status.set_child(box)
        return self._chrome("Shortcut", status, self._skip_and_continue())

    def _build_shortcut_portal(self) -> Adw.NavigationPage:
        status = Adw.StatusPage()
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)

        action = Gtk.Button(halign=Gtk.Align.CENTER)
        action.connect("clicked", lambda _button: self._portal_shortcuts.configure())
        box.append(action)

        modes = self._mode_toggles()
        box.append(modes)
        self._portal_manual_command = self._append_command(box)
        self._portal_manual_open = self._append_open_settings(box)

        self._shortcut_status = Gtk.Label(wrap=True)
        self._shortcut_status.add_css_class("dim-label")
        box.append(self._shortcut_status)
        status.set_child(box)
        actions = self._skip_and_continue()

        self._portal_shortcut_page = status
        self._portal_shortcut_action = action
        self._portal_shortcut_modes = modes
        self._refresh_portal_shortcut()
        return self._chrome("Shortcut", status, actions)

    def _append_command(self, box: Gtk.Box) -> Gtk.Box:
        """Append the toggle command, selectable, beside a button that copies it."""
        command = shortcut_setup.toggle_command_str()
        cmd_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        cmd_row.set_halign(Gtk.Align.CENTER)
        cmd_label = Gtk.Label(label=command, selectable=True)
        cmd_label.add_css_class("monospace")
        copy = Gtk.Button(
            icon_name="edit-copy-symbolic",
            tooltip_text="Copy command",
            valign=Gtk.Align.CENTER,
        )
        copy.add_css_class("flat")
        copy.update_property([Gtk.AccessibleProperty.LABEL], ["Copy command"])
        copy.connect("clicked", lambda _button: self._copy_command(command))
        cmd_row.append(cmd_label)
        cmd_row.append(copy)
        box.append(cmd_row)
        return cmd_row

    def _append_open_settings(self, box: Gtk.Box) -> Gtk.Button | None:
        """Append a button opening the desktop's keyboard settings, where it has any."""
        if not shortcut_setup.keyboard_settings_argv():
            return None
        open_btn = Gtk.Button(label="Open keyboard settings", halign=Gtk.Align.CENTER)
        open_btn.connect("clicked", self._on_open_keyboard_settings)
        box.append(open_btn)
        return open_btn

    def _skip_and_continue(self) -> list[Gtk.Button]:
        """Build Skip, always available, and Continue, enabled once the key fires."""
        skip = Gtk.Button(label="Skip")
        skip.connect("clicked", self._advance)
        nxt = Gtk.Button(label="Continue")
        nxt.add_css_class("suggested-action")
        nxt.set_sensitive(self._shortcut_confirmed)
        nxt.connect("clicked", self._advance)
        self._shortcut_next = nxt
        return [skip, nxt]

    def _refresh_portal_shortcut(self) -> None:
        info = shortcut_setup.portal_presentation(self._portal_shortcuts)
        binding = self._portal_shortcuts.binding
        if self._shortcut_confirmed and (
            self._portal_shortcuts.status == "unbound"
            or (info.ready and binding != self._shortcut_confirmed_binding)
            or (self._shortcut_confirmed_binding is not None and not binding)
        ):
            self._shortcut_confirmed = False
            self._shortcut_confirmed_binding = None
            if self._shortcut_next is not None:
                self._shortcut_next.set_sensitive(False)

        if self._portal_shortcut_page is not None:
            self._portal_shortcut_page.set_title(info.title)
            self._portal_shortcut_page.set_description(info.description)
            self._portal_shortcut_action.set_visible(info.action is not None)
            if info.action is not None:
                self._portal_shortcut_action.set_label(info.action)
            self._portal_shortcut_action.set_sensitive(info.can_configure)
            self._portal_shortcut_modes.set_visible(info.ready)
            self._portal_manual_command.set_visible(info.manual)
            if self._portal_manual_open is not None:
                self._portal_manual_open.set_visible(info.manual)
            self._shortcut_status.set_visible(info.ready or info.manual)
            if not self._shortcut_confirmed and (info.ready or info.manual):
                self._shortcut_status.set_label(
                    "Waiting: press your new key to test it…"
                )
        self._refresh_ready()

    def _say_shortcut_status(self, text: str) -> None:
        """Show `text` as the shortcut status and read it out, as a toast would be."""
        self._shortcut_status.set_label(text)
        self._shortcut_status.announce(text, Gtk.AccessibleAnnouncementPriority.MEDIUM)

    def _copy_command(self, command: str) -> None:
        self._win.get_clipboard().set(command)
        if not self._shortcut_confirmed:
            self._say_shortcut_status("Copied. Now bind it in your keyboard settings.")

    def _on_open_keyboard_settings(self, _btn: Gtk.Button) -> None:
        if not shortcut_setup.open_keyboard_settings():
            self._say_shortcut_status(shortcut_setup.KEYBOARD_SETTINGS_FAILED_MSG)

    def notify_toggle_detected(self) -> None:
        """Confirm that the binding works; app.py calls this when the shortcut fires."""
        if self._finished or self._shortcut_status is None or self._shortcut_confirmed:
            return
        if self._portal_shortcuts is not None:
            info = shortcut_setup.portal_presentation(self._portal_shortcuts)
            if not info.ready and not info.manual:
                return
            self._shortcut_confirmed_binding = (
                self._portal_shortcuts.binding if info.ready else None
            )
        self._shortcut_confirmed = True
        logger.debug("Onboarding: shortcut confirmed")
        self._say_shortcut_status("Shortcut detected. You're ready to dictate.")
        if self._shortcut_next is not None:
            self._shortcut_next.set_sensitive(True)

    def _build_privacy(self) -> Adw.NavigationPage:
        ring = Breakwater(_PRIVACY_RING_PX, motion_enabled=self._motion_enabled)
        self._privacy = ring
        column = brand.status_column(ring, "Private", None)
        column.set_margin_top(12)
        column.set_margin_bottom(12)
        row = Adw.SwitchRow(
            title=crash_reports.SWITCH_TITLE,
            subtitle=crash_reports.SWITCH_SUBTITLE,
            tooltip_text=crash_reports.SWITCH_HELP,
        )
        self._config.bind("crash_reports", row, "active")
        notice = Adw.ActionRow(
            title=crash_reports.NOTICE_TITLE,
            subtitle=crash_reports.NOTICE_SUBTITLE,
            tooltip_text=crash_reports.NOTICE_HELP,
            activatable=True,
        )
        notice.add_suffix(Gtk.Image.new_from_icon_name("adw-external-link-symbolic"))
        notice.connect("activated", lambda _r: open_uri(PRIVACY_URL))
        rows = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        rows.add_css_class("boxed-list")
        rows.append(row)
        rows.append(notice)
        column.append(rows)
        status = Adw.Clamp(maximum_size=brand.STATUS_WIDTH, child=column)

        nxt = Gtk.Button(label="Continue")
        nxt.add_css_class("suggested-action")
        nxt.connect("clicked", self._advance)
        return self._chrome(
            "Privacy",
            status,
            [nxt],
            breakpoints=(
                _breakpoint(
                    _PRIVACY_ROOMY,
                    [(ring, "width-request", 320), (ring, "height-request", 320)],
                ),
                _breakpoint(
                    _SHORT,
                    [(ring, "width-request", 160), (ring, "height-request", 160)],
                ),
            ),
        )

    def _build_ready(self) -> Adw.NavigationPage:
        # The mark draws itself once as the page opens and holds on the finished Q.
        # Built by hand like a status page, whose icon cannot be a widget.
        mark = AnimationView(
            "loader", end_fraction=_READY_MARK_END, motion_enabled=self._motion_enabled
        )
        mark.set_size_request(_READY_MARK_PX, _READY_MARK_PX)
        mark.set_halign(Gtk.Align.CENTER)
        mark.set_valign(Gtk.Align.CENTER)
        # Once the Q closes, one ring leaves it: the ripple sits under the mark in
        # an overlay sized for its reach, so it is never clipped.
        ripple = Ripple(_READY_MARK_PX, motion_enabled=self._motion_enabled)
        self._ready_mark = mark
        self._ready_ripple = ripple
        ripple.set_halign(Gtk.Align.CENTER)
        emblem = Gtk.Overlay(child=ripple)
        emblem.add_overlay(mark)
        column = brand.status_column(emblem, "You're all set!", "")
        description = column.get_last_child()
        self._ready_title = description.get_prev_sibling()
        self._ready_description = description
        download_note = Gtk.Label(
            label=(
                "Your speech model is still downloading in the background. "
                "Dictation will be ready when it finishes."
            ),
            wrap=True,
            justify=Gtk.Justification.CENTER,
            visible=False,
        )
        download_note.add_css_class("dim-label")
        column.insert_child_after(download_note, description.get_prev_sibling())
        download_subscription = None

        def clear_download_note(*_args) -> bool:
            nonlocal download_subscription
            if download_subscription is not None:
                download_subscription.cancel()
                download_subscription = None
            download_note.set_visible(False)
            return False

        def download_failed(_message: str) -> None:
            clear_download_note()
            self._refresh_ready()

        # The optional native-overlay hint and the support note share one box so
        # neither clobbers the other.
        content = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL, spacing=12, halign=Gtk.Align.CENTER
        )
        content.set_margin_top(24)
        if presentation.suggests_native_overlay():
            content.append(self._native_overlay_hint())
        content.append(self._support_hint())
        column.append(content)
        status = Adw.Clamp(maximum_size=brand.STATUS_WIDTH, child=column)

        settings_btn, _set_settings_busy = _busy_button(
            "Customize in Settings", "Opening…"
        )
        # Flat, so Start dictating stays the one primary action.
        settings_btn.add_css_class("flat")
        start_btn, _set_busy = _busy_button("Start dictating")
        start_btn.add_css_class("suggested-action")

        def _on_start(_btn: Gtk.Button) -> None:
            settings_btn.set_sensitive(False)
            _set_busy()
            self._finish()

        def _on_settings(_btn: Gtk.Button) -> None:
            start_btn.set_sensitive(False)
            _set_settings_busy()
            self._finish(open_settings=True)

        start_btn.connect("clicked", _on_start)
        settings_btn.connect("clicked", _on_settings)
        actions = brand.action_row([settings_btn, start_btn])
        page = self._chrome(
            "Ready",
            status,
            actions,
            breakpoints=(
                _breakpoint(
                    _NARROW, [(actions, "orientation", Gtk.Orientation.VERTICAL)]
                ),
            ),
        )

        def on_shown(_page) -> None:
            nonlocal download_subscription
            if self._closed:
                return
            self._refresh_ready()
            clear_download_note()
            # Observing Ready must not start a download or retry a failed one.
            if self._downloads.is_downloading(self._config.model):
                download_note.set_visible(True)
                model = self._config.model
                download_subscription = self._downloads.start_or_join(
                    model,
                    provision_for(model, self._config.device, self._model_mgr),
                    on_done=clear_download_note,
                    on_error=download_failed,
                )
            # Once: coming back to this page shows the finished mark, not a replay.
            if self._ready_shown:
                mark.skip()
                return
            self._ready_shown = True
            mark.play(lambda: None if self._closed else ripple.play())

        page.connect("shown", on_shown)
        page.connect("hidden", clear_download_note)
        self._win.connect("close-request", clear_download_note)
        self._refresh_ready()
        return page

    def _refresh_ready(self) -> None:
        if self._ready_description is None:
            return
        model = self._config.model
        if not (
            self._model_mgr.is_downloaded(model)
            or self._downloads.is_downloading(model)
        ):
            self._ready_title.set_label("Almost ready")
            self._ready_description.set_label(
                "The speech model didn't download. Open Settings to download it, "
                "then start dictating."
            )
            return
        self._ready_title.set_label("You're all set!")
        if self._portal_shortcuts is not None:
            info = shortcut_setup.portal_presentation(self._portal_shortcuts)
            if info.ready:
                desc = f"Quoth is ready. Shortcut: {info.title}. Use it any time to dictate."
            elif info.manual and self._shortcut_confirmed:
                desc = (
                    "Quoth is ready. Press the shortcut you bound to start dictating."
                )
            else:
                desc = (
                    "Quoth is ready. Set up your dictation shortcut in Settings before "
                    "you start."
                )
        elif keyboard_available(self._config.hotkey):
            key = hotkey_capture.hotkey_label(self._config.hotkey)
            desc = f"Quoth is ready. Press {key} any time to dictate."
        elif self._shortcut_confirmed:
            desc = "Quoth is ready. Press the shortcut you bound to start dictating."
        else:
            desc = (
                "Quoth is ready. Set up your dictation shortcut in Settings before you "
                "start."
            )
        self._ready_description.set_label(desc)

    def _native_overlay_hint(self) -> Gtk.Widget:
        """Build the note pointing to the native package's recording overlay.

        Shown only in the Flatpak on desktops where the native package would add the
        on-screen recording overlay the sandbox can't (COSMIC, Sway, Hyprland, niri).
        """
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        label = Gtk.Label(
            label="On this desktop, the native package adds an on-screen recording overlay.",
            wrap=True,
            justify=Gtk.Justification.CENTER,
        )
        label.add_css_class("dim-label")
        link = Gtk.LinkButton(
            uri=presentation.NATIVE_INSTALL_URL, label="How to install it"
        )
        link.set_halign(Gtk.Align.CENTER)
        box.append(label)
        box.append(link)
        return box

    def _support_hint(self) -> Gtk.Widget:
        """Build the optional invitation to support development."""
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        label = Gtk.Label(
            label="Quoth is free and open source.",
            wrap=True,
            justify=Gtk.Justification.CENTER,
        )
        label.add_css_class("dim-label")
        # The site's ghost pill with its heart, rather than a bare link.
        support = Gtk.Button(halign=Gtk.Align.CENTER, margin_top=6)
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        heart = Gtk.Image.new_from_icon_name(f"{APP_ID}-heart-symbolic")
        heart.add_css_class("quoth-heart")
        row.append(heart)
        row.append(Gtk.Label(label="Support Quoth"))
        support.set_child(row)
        support.add_css_class("pill")
        support.add_css_class("quoth-ghost")
        support.add_css_class("quoth-support")  # the heart beats on hover
        support.connect("clicked", lambda _b: open_uri(SUPPORT_URL))
        box.append(label)
        box.append(support)
        return box
