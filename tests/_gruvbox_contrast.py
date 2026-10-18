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
DARK_INK = "#1D2021"  # gruvbox bg1 — the dark-ink foreground used on solid accent fills
LIGHT_INK = "#FBF1C7"  # gruvbox light bg0 — cream ink on the darkened aqua fills
BUTTON_INK_CASES = (
    # What the browser paints on the solid primary fills: the ink is a fixed
    # component colour (style.css overrides), not a palette token.
    ("dark", DARK_INK, "--accent"),
    ("dark", DARK_INK, "--accent-hover"),
    ("light", LIGHT_INK, "--accent"),
    ("light", LIGHT_INK, "--accent-hover"),
)
TREE_DIFF_CASES = (
    # The tree/diff viewer paints inside code-block wraps, i.e. over --code-bg.
    ("light", ".tree-num", "#8F3F71"),
    ("light", ".tree-bool", "#A23803"),
    ("dark", ".tree-num", "#D3869B"),
    ("dark", ".tree-bool", "#FE8019"),
)
# Elements whose base rules reduce opacity: the gruvbox overrides pin opacity
# back to 1, so the browser paints the token at full strength (the pre-override
# composites, e.g. muted@.82 on sidebar at 4.13:1, are what failed the gate).
COMPOSITED_CASES = (
    ("dark", ".session-item.active .session-meta", "--accent-text", "--sidebar"),
    ("dark", ".session-item .session-search-preview", "--muted", "--sidebar"),
    ("dark", ".session-item.active .session-search-preview", "--accent-text", "--sidebar"),
    ("dark", ".project-picker-create", "--blue", "--sidebar"),
    ("dark", ".project-create-btn", "--muted", "--sidebar"),
    ("dark", ".tool-card-more", "--blue", "--code-bg"),
    ("dark", ".tool-cards-toggle button", "--blue", "--code-bg"),
    ("dark", ".provider-card-meta", "--muted", "--sidebar"),
    ("dark", ".session-date-header", "--muted", "--sidebar"),
    ("dark", ".session-select-toggle", "--muted", "--sidebar"),
    ("dark", ".session-empty-note", "--muted", "--sidebar"),
    ("dark", ".session-action-meta", "--muted", "--sidebar"),
    ("dark", ".log-line-debug", "--muted", "--sidebar"),
    ("dark", ".session-source-chip[general]", "--strong", "--sidebar"),
    ("light", ".session-item.active .session-meta", "--accent-text", "--sidebar"),
    ("light", ".session-item .session-search-preview", "--muted", "--sidebar"),
    ("light", ".session-item.active .session-search-preview", "--accent-text", "--sidebar"),
    ("light", ".project-picker-create", "--blue", "--sidebar"),
    ("light", ".project-create-btn", "--muted", "--sidebar"),
    ("light", ".tool-card-more", "--blue", "--code-bg"),
    ("light", ".tool-cards-toggle button", "--blue", "--code-bg"),
    ("light", ".provider-card-meta", "--muted", "--sidebar"),
    ("light", ".session-date-header", "--muted", "--sidebar"),
    ("light", ".session-select-toggle", "--muted", "--sidebar"),
    ("light", ".session-empty-note", "--muted", "--sidebar"),
    ("light", ".session-action-meta", "--muted", "--sidebar"),
    ("light", ".log-line-debug", "--muted", "--sidebar"),
    ("light", ".session-source-chip[general]", "--strong", "--sidebar"),
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
    return tuple(int(hexcolor[i : i + 2], 16) for i in (0, 2, 4))  # type: ignore[return-value]


def contrast(fg: str, bg: str) -> float:
    l1, l2 = sorted([_rel_lum(_hex_rgb(fg)), _rel_lum(_hex_rgb(bg))], reverse=True)
    return (l1 + 0.05) / (l2 + 0.05)


def _blend_over(fg_rgba: tuple[float, float, float, float], bg: tuple[float, float, float]) -> tuple[float, float, float]:
    alpha = fg_rgba[3]
    return tuple(alpha * f + (1 - alpha) * b for f, b in zip(fg_rgba[:3], bg, strict=True))  # type: ignore[return-value]


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

    Covers what the browser paints beyond plain token pairs:
    - solid primary fills and their inks (BUTTON_INK_CASES),
    - the hard-coded tree/diff syntax steps on their code-block surface,
    - elements whose opacity the skin pins back to 1 (COMPOSITED_CASES).
    """
    light, dark = gruvbox_blocks(css)
    failures: list[tuple[str, str, str, float]] = []
    for mode, pal in (("light", light), ("dark", dark)):
        if not pal:
            continue
        for mode_, fg_hex, fill_token in BUTTON_INK_CASES:
            if mode_ != mode:
                continue
            if not pal.get(fill_token, "").startswith("#"):
                failures.append((mode, f"ink {fg_hex}", fill_token, 0.0))
                continue
            ratio = contrast(fg_hex, pal[fill_token])
            if ratio < AA:
                failures.append((mode, f"ink {fg_hex}", fill_token, ratio))
        for mode_, name, fg_hex in TREE_DIFF_CASES:
            if mode_ != mode:
                continue
            ratio = contrast(fg_hex, pal["--code-bg"])
            if ratio < AA:
                failures.append((mode, name, "--code-bg", ratio))
        for mode_, name, token, surface in COMPOSITED_CASES:
            if mode_ != mode:
                continue
            fg = pal.get(token, "")
            bg = pal.get(surface, "")
            if not fg.startswith("#") or not bg.startswith("#"):
                failures.append((mode, name, surface, 0.0))
                continue
            ratio = contrast(fg, bg)
            if ratio < AA:
                failures.append((mode, name, surface, ratio))
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
