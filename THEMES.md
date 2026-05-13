# Hermes Web UI — Themes

Hermes Web UI splits **appearance** into two independent pickers:

- **Theme** — the mode: `System`, `Dark`, or `Light`. Drives the background,
  text, surface, and chrome colors.
- **Skin** — the accent palette: eight named skins ship built-in. Drives only
  the `--accent` family (active states, links, focus rings, primary actions).

You pick one of each and they combine, so the look adapts to your environment
without losing your favorite accent — pure CSS, no Python changes needed.

---

## Switching Appearance

**Settings panel:** Click the gear icon → **Appearance**. The **Theme** card
toggles Light/Dark/System; the **Skin** grid offers eight accent palettes.
Preview is instant — the UI updates as you click.

**Slash command:** Type `/theme <name>` in the composer. The command accepts
both theme names (`system`, `dark`, `light`) and skin names (`default`, `ares`,
`mono`, `slate`, `poseidon`, `sisyphus`, `charizard`, `sienna`). It updates the
matching axis and leaves the other one alone.

**Persistence:** Both choices are stored in `localStorage` for flicker-free
loading, and saved server-side via `POST /api/settings` (under `theme` and
`skin` keys in `settings.json`).

---

## Built-in Themes

| Theme | Description |
|-------|-------------|
| **System** (default) | Follows the OS `prefers-color-scheme` preference and updates live. |
| **Dark** | Deep dark surfaces, low-glare for long sessions. |
| **Light** | Bright surfaces with dark text, high contrast for daylight environments. |

The theme is applied as a class on `<html>`: `.dark` is present for dark mode,
absent for light. System mode tracks the OS preference at runtime.

---

## Built-in Skins

| Skin | Description |
|------|-------------|
| **Default** | The original Hermes gold accent. Warm and understated. |
| **Ares** | Fiery red. High-energy and assertive. |
| **Mono** | Neutral gray. Distraction-free, for deep focus. |
| **Slate** | Slate blue-gray. Subtle and grown-up. |
| **Poseidon** | Ocean blue. Calm and focused for long sessions. |
| **Sisyphus** | Vivid purple. Distinctive without being loud. |
| **Charizard** | Warm orange. Energetic and easy on the eyes. |
| **Sienna** | Warm clay and sand earth palette. Soft and natural. |

Each skin defines paired light + dark variants so it reads cleanly on either
theme. The skin is applied as `data-skin="<name>"` on `<html>` (the default
skin clears the attribute).

---

## Creating a Custom Skin

A skin is a small CSS block that overrides the accent variables for both the
light and dark variants:

```css
/* Light variant */
:root[data-skin="my-skin"] {
  --accent:           #2E7D32;                   /* Active states, links, primary buttons */
  --accent-hover:     #1B5E20;                   /* Hover */
  --accent-bg:        rgba(46,125,50,0.08);      /* Soft tinted backgrounds */
  --accent-bg-strong: rgba(46,125,50,0.15);      /* Highlighted backgrounds */
  --accent-text:      #1B5E20;                   /* Text on accent bg */
}

/* Dark variant — usually lighter or more saturated for contrast */
:root.dark[data-skin="my-skin"] {
  --accent:           #66BB6A;
  --accent-hover:     #43A047;
  --accent-bg:        rgba(102,187,106,0.08);
  --accent-bg-strong: rgba(102,187,106,0.15);
  --accent-text:      #66BB6A;
}
```

Two ways to ship it:

1. **In the repo (built-in):** add the block to `static/style.css`, register it
   in the Settings skin picker (`static/index.html`) and in the `/theme` command
   list (`static/commands.js`), then open a PR.

2. **Self-hosted (no fork):** use the WebUI extensions surface — see
   `docs/EXTENSIONS.md`. Drop your CSS in `HERMES_WEBUI_EXTENSION_DIR` and
   declare it in `HERMES_WEBUI_EXTENSION_STYLESHEET_URLS`. No code changes
   needed; the skin attribute can be set from your own JS.

### Tips

- **Test both themes.** A skin that pops on Dark can be illegible on Light.
  Always check `:root[data-skin]` (light) *and* `:root.dark[data-skin]` (dark).
- **Pick contrasting `--accent-text` on `--accent-bg`.** The strong variant
  appears behind small labels and chips; weak contrast there reads as blur.
- **The logo gradient uses `--accent` automatically**, so it adapts to your
  skin without any extra work.
- **No server changes needed.** The `skin` setting in `settings.json` accepts
  any string, so your custom skin name persists without code changes once you
  load the CSS.

---

## Creating a Custom Theme

A full custom *theme* (a different overall mood, not just an accent change) is
a larger task than a skin: it has to redefine the core palette variables
(`--bg`, `--surface`, `--text`, `--border`, `--code-bg`, and friends) for one
or both modes. The contract is defined in the top `:root` and `:root.dark`
blocks of `static/style.css` — start there.

Most of the time, a custom **skin** is what you actually want. Reach for a
custom theme only when the existing Light/Dark modes don't fit (for example,
a high-contrast accessibility theme or an OLED black variant).

---

## Font Size

Right under Theme/Skin in **Settings → Appearance**: `Small`, `Default`,
`Large`. Applied as `data-font-size` on `<html>` and scales the WebUI's root
font size. Persists alongside theme and skin.

---

## How It Works Internally

1. **Theme:** `document.documentElement.classList.toggle('dark', isDark)` —
   light mode removes the class. System mode tracks
   `matchMedia('(prefers-color-scheme: dark)')`.
2. **Skin:** `document.documentElement.dataset.skin = name` (or remove the
   attribute for `default`).
3. **Font size:** `document.documentElement.dataset.fontSize = size` (or
   remove for `default`).
4. **No flash on load:** a tiny inline `<script>` in `<head>` reads
   `localStorage` before the stylesheet does, so the right look is applied
   before paint.
5. **Server sync:** preferences are saved via `POST /api/settings` and
   rehydrated on boot via `GET /api/settings`.

---

## Contributing a Skin

Skins are the easiest extension point — pure CSS, no Python, no JS logic. To
contribute one upstream:

1. Add your `:root[data-skin="name"]` and `:root.dark[data-skin="name"]`
   blocks to `static/style.css`.
2. Register it in the Settings skin picker in `static/index.html` and in the
   skin list used by `cmdTheme()` in `static/commands.js`.
3. Test on desktop and mobile across both Light and Dark themes.
4. Open a PR — skins are pure CSS additions with no backend changes needed.

For a custom *theme* (overriding the base palette), prefer opening an issue
first to discuss scope, since it touches many selectors.
