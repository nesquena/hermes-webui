"""Compute WCAG 2.x relative-luminance contrast for gruvbox palette pairs.

Standalone module (no pytest import) so it doubles as a manual check:

    python3 tests/_gruvbox_contrast.py        # print full matrix
    python3 tests/_gruvbox_contrast.py --fail # exit 1 if any pair < AA

Every foreground token is checked against every surface it can render on
(bg/sidebar/surface/code-bg) plus the toast context (token color over a
14% color-mix of itself onto --surface), mirroring how the stylesheet
composes those values. AA = 4.5:1 for normal text.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

REPO = Path(__file__).parent.parent
CSS_PATH = REPO / "static" / "style.css"

AA = 4.5
SURFACES = ("bg", "sidebar", "surface", "code-bg")
TEXT_TOKENS = (
    "text", "strong", "muted", "em", "accent", "accent-text",
    "gold", "code-text", "pre-text", "success", "warning", "error", "blue", "info",
    "user-bubble-placeholder",
)
LIGHT_PREFIX = ':root[data-skin="gruvbox"] '
DARK_PREFIX = ':root.dark[data-skin="gruvbox"] '
GRUVBOX_PREFIXES = (LIGHT_PREFIX, DARK_PREFIX)

# Component-level paint model. Ink colours, repaired opacities/foregrounds
# and the tree/diff syntax steps are parsed out of static/style.css itself,
# so mutating (or reverting) a repaired declaration flips these checks the
# way it flips the browser. Only the surface each element paints on is
# declared here: the sidebar list paints on --sidebar, tool-card toggles on
# --surface, and the tree/diff viewer inside code-block wraps (--code-bg).
DARK_INK_RULE = DARK_PREFIX + ".msg-edit-send"
DARK_INK_COVERED = (
    DARK_PREFIX + ".msg-edit-send",
    DARK_PREFIX + "#mainSettings .settings-btn",
    DARK_PREFIX + '#mainSettings .sm-btn[onclick^="saveSettings"]',
    DARK_PREFIX + "#mainSettings .settings-autosave-status button",
    DARK_PREFIX + '#settingsUnsavedBar button[onclick^="saveSettings"]',
)
PROMOTED_INK_ELEMENTS = (".new-chat-btn", "button.send-btn:not(:disabled)")  # promoted accent fills also carry #1D2021 ink
TREE_COLOR_ELEMENTS = (".tree-num", ".tree-bool")
COMPONENT_CASES = (
    # (base selector, gruvbox override element, surface the element paints on)
    (".session-item.active .session-meta", ".session-item.active .session-meta", "--sidebar"),
    (".session-item.selected .session-meta", ".session-item.active .session-meta", "--sidebar"),
    (".session-search-preview", ".session-item .session-search-preview", "--sidebar"),
    (".session-item.active .session-search-preview", ".session-item .session-search-preview", "--sidebar"),
    (".project-picker-create", ".project-picker-create", "--sidebar"),
    (".project-create-btn", ".project-create-btn", "--sidebar"),
    (".tool-card-more", ".tool-card-more", "--surface"),
    (".tool-cards-toggle button", ".tool-cards-toggle button", "--surface"),
    (".provider-card-meta", ".provider-card-meta", "--sidebar"),
    (".session-date-header", ".session-date-header", "--sidebar"),
    (".session-select-toggle", ".session-select-toggle", "--sidebar"),
    (".session-empty-note", ".session-empty-note", "--sidebar"),
    (".session-action-meta", ".session-action-meta", "--sidebar"),
    (".log-line-debug", ".log-line-debug", "--sidebar"),
    (".session-source-chip", ".session-source-chip", "--sidebar"),
)


def _parse_block(css: str, selector: str) -> dict[str, str]:
    start = css.find(selector)
    while start != -1:
        open_brace = css.find("{", start)
        if open_brace != -1 and "{" not in css[start + len(selector) : open_brace]:
            close_brace = css.find("}", open_brace)
            if close_brace != -1:
                body = css[open_brace + 1 : close_brace]
                out: dict[str, str] = {}
                for decl in body.split(";"):
                    if ":" in decl:
                        key, _, value = decl.partition(":")
                        out[key.strip()] = value.strip()
                return out
        start = css.find(selector, start + 1)
    return {}


def gruvbox_blocks(css: str = None) -> tuple[dict[str, str], dict[str, str]]:
    css = css or CSS_PATH.read_text(encoding="utf-8")
    return (
        _parse_block(css, ':root[data-skin="gruvbox"]{'),
        _parse_block(css, ':root.dark[data-skin="gruvbox"]{'),
    )


def _rel_lum(rgb: tuple[float, float, float]) -> float:
    def chan(c: float) -> float:
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4

    r, g, b = (chan(x / 255) for x in rgb)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def _hex_rgb(hexcolor: str) -> tuple[float, float, float]:
    hexcolor = hexcolor.lstrip("#")
    if len(hexcolor) == 3:
        hexcolor = "".join(c * 2 for c in hexcolor)
    return tuple(int(hexcolor[i : i + 2], 16) for i in (0, 2, 4))  # type: ignore[return-value]


def contrast(fg: str, bg: str) -> float:
    l1, l2 = sorted([_rel_lum(_hex_rgb(fg)), _rel_lum(_hex_rgb(bg))], reverse=True)
    return (l1 + 0.05) / (l2 + 0.05)


def _blend_over(fg_rgba: tuple[float, float, float, float], bg: tuple[float, float, float]) -> tuple[float, float, float]:
    alpha = fg_rgba[3]
    return tuple(alpha * f + (1 - alpha) * b for f, b in zip(fg_rgba[:3], bg, strict=True))  # type: ignore[return-value]



_RULE_RE = re.compile(r"([^{}]+)\{([^{}]*)\}", re.S)


def _norm(text: str) -> str:
    return " ".join(text.split())


def _rule_decls(body: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for decl in body.split(";"):
        if ":" in decl:
            key, _, value = decl.partition(":")
            out[key.strip()] = _norm(value)
    return out


def _rule_entries(group_text: str) -> list[str]:
    group_text = re.sub(r"/\*.*?\*/", "", group_text, flags=re.S)
    return [_norm(part) for part in group_text.split(",")]


def _collect_groups(css: str) -> list[tuple[list[str], dict[str, str]]]:
    return [(_rule_entries(sel), _rule_decls(body)) for sel, body in _RULE_RE.findall(css)]


def _base_rule(css: str, selector: str) -> dict[str, str]:
    """Merged declarations of every rule whose selector list contains
    `selector` exactly (no skin prefix, no pseudo classes); later rules win."""
    merged: dict[str, str] = {}
    for entries, decls in _collect_groups(css):
        if selector in entries:
            merged.update(decls)
    return merged


def _gruvbox_override(css: str, element: str, prefixes: tuple[str, ...] = GRUVBOX_PREFIXES) -> dict[str, str]:
    """Merged gruvbox-scoped declarations applying to `element` in document
    order. Non-state pseudo variants (:not chains) count; :hover does not."""
    merged: dict[str, str] = {}
    for entries, decls in _collect_groups(css):
        hit = False
        for entry in entries:
            for prefix in prefixes:
                target = prefix + element
                if entry == target or entry.startswith(target + ":not("):
                    hit = True
                    break
        if hit:
            merged.update(decls)
    return merged


def _resolve_color(expr: str, pal: dict[str, str]):
    """Hex | var() | color-mix(in srgb, ...) | rgba() -> (r, g, b, a)."""
    e = _norm(expr).replace("!important", "")
    if e.startswith("#"):
        rgb = _hex_rgb(e.split()[0])
        return (float(rgb[0]), float(rgb[1]), float(rgb[2]), 1.0)
    m = re.fullmatch(r"var\((--[A-Za-z0-9-]+)\)", e)
    if m and pal.get(m.group(1), "").startswith("#"):
        rgb = _hex_rgb(pal[m.group(1)])
        return (float(rgb[0]), float(rgb[1]), float(rgb[2]), 1.0)
    m = re.fullmatch(r"color-mix\(\s*in srgb\s*,\s*(.+)\)", e)
    if m:
        parts = [_norm(p) for p in m.group(1).split(",")]
        if len(parts) < 2:
            return None

        def split_part(part: str):
            pm = re.fullmatch(r"(.+\S)\s+([\d.]+)%", part)
            if pm:
                return _resolve_color(pm.group(1), pal), float(pm.group(2)) / 100.0
            return _resolve_color(part, pal), None

        (c1, p1), (c2, p2) = split_part(parts[0]), split_part(parts[-1])
        if c1 is None or c2 is None:
            return None
        if p1 is None and p2 is None:
            p1 = p2 = 0.5
        elif p1 is None:
            p1 = max(0.0, 1.0 - p2)
        elif p2 is None:
            p2 = max(0.0, 1.0 - p1)
        total = p1 + p2 or 1.0
        w1, w2 = p1 / total, p2 / total
        return (
            w1 * c1[0] + w2 * c2[0],
            w1 * c1[1] + w2 * c2[1],
            w1 * c1[2] + w2 * c2[2],
            w1 * c1[3] + w2 * c2[3],
        )
    m = re.fullmatch(r"rgba?\(([^)]+)\)", e)
    if m:
        vals = [v.strip() for v in m.group(1).split(",")]
        nums = [float(v.rstrip("%")) / 100.0 if v.endswith("%") else float(v) for v in vals]
        return (nums[0], nums[1], nums[2], nums[3] if len(nums) > 3 else 1.0)
    return None


def _painted_ratio(css: str, pal: dict[str, str], base_selector: str, override_element: str, surface: str, prefixes: tuple[str, ...] = GRUVBOX_PREFIXES):
    """(ratio, error) for the browser-painted label on `surface`: gruvbox
    override declarations win over the base rule; unresolved colors and
    opacities are reported rather than silently assumed."""
    bg = pal.get(surface, "")
    if not bg.startswith("#"):
        return None, f"missing surface token {surface}"
    base = _base_rule(css, base_selector)
    override = _gruvbox_override(css, override_element, prefixes)
    color_expr = override.get("color") or base.get("color") or ""
    opacity_expr = override.get("opacity") or base.get("opacity") or "1"
    fg = _resolve_color(color_expr, pal)
    if fg is None:
        return None, f"unresolvable color {color_expr!r}"
    try:
        alpha = float(_norm(opacity_expr).replace("!important", ""))
    except ValueError as exc:
        return None, f"unparseable opacity {opacity_expr!r}: {exc}"
    rgba = (fg[0], fg[1], fg[2], fg[3] * alpha)
    painted = _blend_over(rgba, _hex_rgb(bg))
    painted_hex = "#{:02X}{:02X}{:02X}".format(*(round(c) for c in painted))
    return contrast(painted_hex, bg), None


def failing_pairs(css: str = None) -> list[tuple[str, str, str, float]]:
    """(mode, token, surface, ratio) for every text/surface pair under 4.5:1."""
    light, dark = gruvbox_blocks(css)
    failures: list[tuple[str, str, str, float]] = []
    for mode, pal in (("light", light), ("dark", dark)):
        if not pal:
            failures.append((mode, "<block-missing>", "<block-missing>", 0.0))
            continue
        backgrounds = {name: pal[f"--{name}"] for name in SURFACES}
        surface_rgb = _hex_rgb(pal["--surface"])
        for token in TEXT_TOKENS:
            fg = pal.get(f"--{token}", "")
            if not fg.startswith("#"):
                continue
            for bg_name, bg in backgrounds.items():
                ratio = contrast(fg, bg)
                if ratio < AA:
                    failures.append((mode, token, bg_name, ratio))
        # Toast context: .toast.<kind> renders `color: var(--token)` over a
        # 14% mix of the same token onto --surface.
        for kind in ("success", "warning", "error"):
            fg = pal[f"--{kind}"]
            mixed = _blend_over(_hex_rgb(fg) + (0.14,), surface_rgb)
            hex_mixed = "#{:02X}{:02X}{:02X}".format(*(round(c) for c in mixed))
            ratio = contrast(fg, hex_mixed)
            if ratio < AA:
                failures.append((mode, kind, "toast-14%", ratio))
    return failures


def failing_component_pairs(css: str = None) -> list[tuple[str, str, str, float]]:
    """(mode, component, over, ratio) for every component-paint pair under 4.5:1.

    All component values are read out of the stylesheet itself — the dark
    solid-fill ink group, the promoted accent-fill inks, the hard-coded
    tree/diff steps and the opacity/foreground repair rules (resolved to the
    winning value incl. color-mix/rgba and the effective opacity) — so a
    reverted or mutated repair fails here exactly as it paints in Chromium.
    Structural failures (a named control missing from the ink group, an
    unresolvable color) report ratio 0.0.
    """
    css = css or CSS_PATH.read_text(encoding="utf-8")
    light, dark = gruvbox_blocks(css)
    palettes = {"light": light, "dark": dark}
    failures: list[tuple[str, str, str, float]] = []
    if not light or not dark:
        return [("light", "<palette-block-missing>", "-", 0.0)]

    def check_ink(ink_rgba, pal, fill_tokens, label):
        if ink_rgba is None:
            failures.append(("dark", f"ink rule missing/invalid: {label}", "--accent", 0.0))
            return
        ink_hex = "#{:02X}{:02X}{:02X}".format(*(round(c) for c in ink_rgba[:3]))
        for fill in fill_tokens:
            ratio = contrast(ink_hex, pal[fill])
            if ratio < AA:
                failures.append(("dark", f"ink {ink_hex} on {label}", fill, ratio))

    # 1. dark solid-fill ink group: every gate-named control must be covered,
    #    and the parsed ink must clear AA on the aqua fills it paints on.
    ink_value: str | None = None
    covered_entries: list[str] = []
    for entries, decls in _collect_groups(css):
        if DARK_INK_RULE in entries and "color" in decls:
            ink_value, covered_entries = decls["color"], entries
    ink_rgba = _resolve_color(ink_value, dark) if ink_value else None
    if ink_rgba is None:
        failures.append(("dark", f"dark ink rule missing: {DARK_INK_RULE}", "--accent", 0.0))
    else:
        for entry in DARK_INK_COVERED:
            if entry not in covered_entries:
                failures.append(("dark", f"ink group missing {entry}", "-", 0.0))
        check_ink(ink_rgba, dark, ("--accent", "--accent-hover"), "dark ink group")

    # 2. promoted translucent-accent controls: their solid fill rule must
    #    carry ink that clears AA too (same mutation class as the ink group).
    for element in PROMOTED_INK_ELEMENTS:
        decls = _gruvbox_override(css, element, (DARK_PREFIX,))
        check_ink(_resolve_color(decls.get("color", ""), dark) if decls.get("color") else None,
                  dark, ("--accent", "--accent-hover"), f"{element} fill")

    # 3. hard-coded tree/diff steps painted on the code-block surface.
    for mode in ("light", "dark"):
        prefixes = GRUVBOX_PREFIXES if mode == "dark" else (LIGHT_PREFIX,)
        for element in TREE_COLOR_ELEMENTS:
            decls = _gruvbox_override(css, element, prefixes)
            fg = _resolve_color(decls.get("color", ""), palettes[mode]) if decls.get("color") else None
            if fg is None:
                failures.append((mode, f"{element} color missing", "--code-bg", 0.0))
                continue
            hex_fg = "#{:02X}{:02X}{:02X}".format(*(round(c) for c in fg[:3]))
            ratio = contrast(hex_fg, palettes[mode]["--code-bg"])
            if ratio < AA:
                failures.append((mode, element, "--code-bg", ratio))

    # 4. opacity/foreground repairs: paint the winning color at the effective
    #    opacity over the declared surface, per Chromium compositing.
    for mode in ("light", "dark"):
        pal = palettes[mode]
        prefixes = GRUVBOX_PREFIXES if mode == "dark" else (LIGHT_PREFIX,)
        for base_selector, override_element, surface in COMPONENT_CASES:
            ratio, error = _painted_ratio(css, pal, base_selector, override_element, surface, prefixes)
            if error is not None:
                failures.append((mode, f"{base_selector} ({error})", surface, 0.0))
            elif ratio < AA:
                failures.append((mode, base_selector, surface, ratio))
    return failures


if __name__ == "__main__":
    light, dark = gruvbox_blocks()
    for mode, pal in (("light", light), ("dark", dark)):
        print(f"== {mode} ==")
        for token in TEXT_TOKENS:
            fg = pal.get(f"--{token}", "")
            if not fg.startswith("#"):
                continue
            ratios = "  ".join(f"{n}={contrast(fg, pal[f'--{n}']):.2f}" for n in SURFACES)
            print(f"  {token:<12} {fg}  {ratios}")
    failures = failing_pairs()
    component_failures = failing_component_pairs()
    print(f"\nAA failures: {len(failures)}")
    for mode, token, surface, ratio in failures:
        print(f"  FAIL {mode} {token} on {surface}: {ratio:.2f}")
    print(f"Component AA failures: {len(component_failures)}")
    for mode, component, surface, ratio in component_failures:
        print(f"  FAIL {mode} {component} on {surface}: {ratio:.2f}")
    if "--fail" in sys.argv:
        sys.exit(1 if (failures or component_failures) else 0)
