# Design: Quoth app

Quoth uses bone-coloured text on ink surfaces, matching its website. This file records the default GTK tokens, surface rules and interaction conventions implemented in `brand.py` and the overlay themes. App skins replace named GTK palette roles only, never images, fonts, widget geometry or motion; see the [customisation guide](SUPPORT.md#app-skins).

## Tokens in use

| Token | Value | Role |
|---|---|---|
| ink-950 | `#08080a` | Default window and header-bar background |
| ink-900 | `#0e0e11` | Pill and Default dialog background; app icon tile |
| surface | bone 5% wash, 8% hairline | Cards and rows (flattened to opaque on popovers) |
| bone | `#f2f0ea` | Default text, pill waveform and indicators, GTK accent |
| bone-dim | `#cfccc3` | Secondary lines over hero artwork (in place of `dim-label`) |
| red | `#9a1f2e` fill, `#dc5a4a` text | Destructive buttons and error text |
| error dot | `#cc2936` | Overlay pill error indicator |
| recording red | `#e5484d` | Tray icon while recording |
| pill radius | fully round | Primary/ghost CTAs, status chips, and overlay pill caps |

Storefronts (such as Flathub) use vellum (`#dcc49c`) for light and sepia (`#614730`) for dark in the AppStream `<branding>` metadata; the application itself does not use them.

## GTK mapping (`src/quoth/brand.py`)

- **Priority and overrides**: `brand.py` loads `@define-color accent_*` and surface overrides at `USER` priority so desktop theme overrides (such as COSMIC's GTK config) do not override Quoth's skin; the user's `~/.config/quoth/style.css` loads at `USER+1` and always wins.
- **Dark ink scale vs light/high-contrast**: the Default skin supplies the dark ink palette (`window_bg_color`, `dialog_bg_color`, `view_bg_color`, `card_bg_color`, `popover_bg_color`), deriving contrasting standalone accent ink via OKLab lightness, while leaving light schemes and High Contrast on native Adwaita surfaces.
- **Skins vs overlay themes**: app skins (`skin.py`) only recolour the desktop window; overlay colours and geometry (`theme.py` / `theme.toml`) remain separate. Skin selectors preview palette bands using resolved colours (or neutral patterned bands for native/derived roles).
- **Buttons**: primary actions use a pill CTA styling (`suggested-action`), with at most one filled primary CTA per page; secondary actions take a hairline ghost style or standard Adwaita pills.
- **Hero artwork contrast**: onboarding Welcome and About headers keep light bone typography in both light and dark schemes by pairing imagery with a dark gradient scrim beneath the text so contrast holds at any viewport size.

## Type

GTK uses the system UI font (Cantarell or the desktop's choice), not Geist: the app should look native on the desktop it runs on. Hierarchy comes from Adwaita's `title-1`, `heading`, `dim-label` and `caption` classes, except over hero artwork, where `dim-label` opacity would let the background bleed through (`bone-dim` is used there).

## Interaction and help

- **Pointer cursors**: `brand.pointer_cursors` owns the hand-cursor policy (`pointer`) on interactive controls; disabled and inert information rows keep the default cursor, expander headers share one cursor across title and arrow, and idle overlays remain click-through (active overlays carry a click-to-cancel cursor).
- **Settings tooltips**: every row in a Settings group either carries a tooltip or none does (lists count as their own group). Each tooltip adds one concise present-tense fact not already stated by the title or subtitle, without repeating them or hiding essential decision details inside hover-only text.
- **Language**: UI labels, schema summaries and AppStream metadata use American English (matching GNOME conventions); documentation uses British English without em dashes.
- **Toasts vs notifications**: toasts are short label-like phrases (one-sentence toasts omit a trailing full stop; multi-sentence toasts punctuate each sentence); notifications use prose sentences, except bare status phrases (`Recording...`) or trailing CLI commands.
- **Accessibility**: keep explicit accessible names on icon-only buttons and custom controls, avoid relying on hover tooltips for critical guidance, and honour high-contrast and reduced-motion desktop preferences.

## Rules

- Default uses bone on ink for reading text, with no second accent colour.
- Keyboard focus uses GTK's own focus ring.
- Depth is tonal (Adwaita cards and hairlines), no floating shadows. Settings
  owns the custom row shading; high contrast restores native separators and
  nested shading.
- One authored motion moment per page (the welcome mark; lines arrive after
  it), never an identical entrance on every page.
