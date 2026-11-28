# Skins

One CSS file per skin. Each file is scoped to `[data-skin="<key>"]` (and
`:root.dark[data-skin="<key>"]` for its dark palette), so **a skin file can only
match when that skin is active** — skins cannot leak into each other, and the
order of these files among themselves does not matter.

## Load order (contract)

`static/index.html` links them in this exact order:

1. `static/style.css` — base palette (`:root` / `:root.dark`) and every
   component rule that is not skin-specific.
2. `static/skins/<key>.css` — one per skin, any order among themselves.
3. `static/scaffold.css` — the shared, valueless `:root[data-skin]` resets that
   apply to **all** skins. It loads **last**.

Skins must come **after** `style.css` so a skin can override a base rule with a
plain selector instead of `!important`.

`scaffold.css` must come after the skins: those resets used to sit after the skin
block inside `style.css`, and they have to keep beating equal-specificity skin
rules. Loading them in front of the skins flips the winner for every dark
full-palette skin (e.g. dark Neon worklog rows go from `transparent` to
`rgba(179,71,255,.1)`).

Do not move skin CSS back into `style.css`, and do not add a skin file that is
not linked — `tests/test_skin_css_split.py` fails on both, and it also requires
every linked stylesheet to appear in `SHELL_ASSETS` in `static/sw.js` with the
same spelling and `?v=` token.

## Adding a skin

1. `static/skins/<key>.css` — two blocks:
   `:root[data-skin="<key>"]{…}` and `:root.dark[data-skin="<key>"]{…}`.
   Everything is scoped to the attribute; never write an unscoped selector here.
2. `<link rel="stylesheet" href="static/skins/<key>.css?v=__WEBUI_VERSION__">`
   in `static/index.html`, after the `style.css` link and **before** the
   `scaffold.css` link (scaffold must stay last).
3. `'./static/skins/<key>.css' + VQ,` in `SHELL_ASSETS` in `static/sw.js`, so the
   file is cached and available offline.
4. `_SKINS` entry in `static/boot.js`.
5. `<key>` in the pre-paint `skins={…}` allowlist in `static/index.html`.
6. `"<key>"` in `_SETTINGS_SKIN_VALUES` in `api/config.py` (server persistence).
7. `/theme` list in `static/i18n.js` — all 15 locales.
8. `THEMES.md` table row.
9. `tests/test_<key>_skin.py`, modelled on an existing one.

`static/commands.js` needs no change: `cmdTheme()` reads `_SKINS`.

## Why one file per skin

Not for CSS isolation (the `[data-skin]` scoping already guarantees that) — for
review and merge cost: 20 skins previously shared one 95 KB contiguous block
inside a 529 KB stylesheet, so every skin change touched the same file and every
skin PR conflicted with every other one. Splitting also lets a new skin be
reviewed as one self-contained file rather than a diff inside a giant stylesheet.
