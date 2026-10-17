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
    print(f"\nAA failures: {len(failures)}")
    for mode, token, surface, ratio in failures:
        print(f"  FAIL {mode} {token} on {surface}: {ratio:.2f}")
    if "--fail" in sys.argv:
        sys.exit(1 if failures else 0)
