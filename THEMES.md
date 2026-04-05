# Hermes Web UI — Themes

Hermes Web UI supports pluggable color themes. Six themes ship built-in, and
you can create your own with pure CSS — no Python changes needed.

---

## Switching Themes

**Settings panel:** Click the gear icon, select a theme from the dropdown. The
preview is instant — the UI updates as you click through options.

**Slash command:** Type `/theme dark` or `/theme light` in the composer.

**Themes persist** across page reloads and server restarts (stored in
`settings.json` server-side, with `localStorage` for flicker-free loading).

---

## Built-in Themes

| Theme | Description |
|-------|-------------|
| **Dark** (default) | Deep navy/indigo with muted blue accents. Easy on the eyes for long sessions. |
| **Light** | Warm off-white with dark text. High contrast for bright environments. |
| **Slate** | Warm charcoal, lighter than Dark. Easier on the eyes for extended use. |
| **Solarized Dark** | Ethan Schoonover's classic dark palette. Teal background, warm accents. |
| **Monokai** | Warm dark theme inspired by the Monokai editor scheme. Green/pink accents. |
| **Nord** | Arctic blue-gray palette from the Nord color system. Calm and minimal. |
| **Custom themes** | Any string accepted by `settings.json`, `POST /api/settings`, and `/theme` if added to the picker/command list. Pure CSS variables only. |

---

## Creating a Custom Theme

A theme is a CSS block that overrides the color variables. Add it to
`static/style.css` (or a separate file that you link after the main stylesheet).

### Step 1: Define your theme block

Every color in the UI comes from these CSS variables:

```css
:root[data-theme="your-theme-name"] {
  /* Core palette */
  --bg: #1a1a2e;          /* Main background */
  --sidebar: #16213e;      /* Sidebar background */
  --border: rgba(255,255,255,0.08);   /* Subtle borders */
  --border2: rgba(255,255,255,0.14);  /* Stronger borders */
  --text: #e8e8f0;         /* Primary text color */
  --muted: #8888aa;        /* Secondary/muted text */
  --accent: #e94560;       /* Accent color (errors, warnings, delete) */
  --blue: #7cb9ff;         /* Primary action color (links, active states) */
  --gold: #c9a84c;         /* Secondary accent (pinned items, gold highlights) */
  --code-bg: #0d1117;      /* Code block background */

  /* Surface and chrome (required for full theme polish) */
  --surface: #1a2535;      /* Dropdowns, popups, toast, approval card */
  --topbar-bg: rgba(22,33,62,.98);   /* Topbar background */
  --main-bg: rgba(26,26,46,0.5);    /* Main chat area background */
  --input-bg: rgba(255,255,255,.04); /* Input/button subtle backgrounds */
  --hover-bg: rgba(255,255,255,.06); /* Hover state backgrounds */
  --focus-ring: rgba(124,185,255,.35); /* Focus border color */
  --focus-glow: rgba(124,185,255,.08); /* Focus box-shadow glow */

  /* Typography (required for readable text across themes) */
  --strong: #fff;          /* Bold text in messages */
  --em: #c9c9e8;           /* Italic text in messages */
  --code-text: #f0c27f;    /* Inline code text color */
  --code-inline-bg: rgba(0,0,0,.35); /* Inline code background */
  --pre-text: #e2e8f0;     /* Code block text color */
}
```

The **core palette** controls the overall mood. The **surface/chrome** and
**typography** variables are part of the standard theme contract — define all
of them for a complete theme.

For **light themes**, you also need `:root[data-theme="name"]` overrides
for elements that use `rgba(255,255,255,.XX)` hover/border effects (these
are invisible on light backgrounds). See the built-in light theme for the
full pattern — it overrides ~45 selectors for proper dark-on-light contrast
on hover states, borders, chips, role labels, session items, and
interactive elements.

### Step 2: Add it to the theme picker (optional)

To make your theme appear in the Settings dropdown, add an `<option>` to the
theme `<select>` in `static/index.html`:

```html
<option value="your-theme-name">Your Theme Name</option>
```

And update the `/theme` command's valid theme list in `static/commands.js`.

### Step 3: Test it

Switch to your theme via `/theme your-theme-name` or the Settings panel.
Check these areas:
- Sidebar session list (hover states, active state, project borders)
- Message bubbles (user vs assistant styling)
- Code blocks (background contrast, copy button visibility)
- Tool cards (running indicator, expand/collapse)
- Settings panel and login page
- Mobile layout (hamburger sidebar, bottom nav)

### Tips

- **Light themes** need scrollbar and selection overrides, plus the full
  text/code set (`--strong`, `--em`, `--code-text`, `--code-inline-bg`,
  `--pre-text`) or they will look broken.
- The **logo gradient** uses `--accent` automatically, so it adapts to your
  theme without extra work.
- **Prism.js syntax highlighting** uses its own CDN stylesheet (Tomorrow theme).
  It works well on dark themes; on light themes the contrast is acceptable but
  not perfect. Custom Prism theme support is planned for a future update.
- **No server changes needed.** The `theme` setting in `settings.json` accepts
  any string — your custom theme name will persist without code changes.

---

## How Themes Work Internally

1. Each theme is a `:root[data-theme="name"]` CSS block that overrides variables.
2. Switching themes sets `document.documentElement.dataset.theme = name` in JS.
3. A tiny inline `<script>` in `<head>` reads `localStorage` before the
   stylesheet loads — this prevents a flash of the wrong theme on page load.
4. The theme preference is saved server-side via `POST /api/settings` and
   loaded on boot via `GET /api/settings`.
5. The `/theme` command and Settings dropdown both update the DOM, localStorage,
   and server settings simultaneously.

---

## Contributing a Theme

To contribute a new built-in theme:

1. Add your `:root[data-theme="name"]` block to `static/style.css`
2. Add the `<option>` to the Settings panel in `static/index.html`
3. Add the theme name to the valid list in `cmdTheme()` in `static/commands.js`
4. Test on desktop and mobile
5. Open a PR — themes are pure CSS additions with no backend changes needed
