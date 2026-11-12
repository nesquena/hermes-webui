"""Composer box metrics derived from the REAL stylesheet.

``autoResize()`` in ``static/messages.js`` decides between skipping the height
round trip and doing a full remeasure from *computed* styles: the composer's
natural one-row border box is ``line-height + vertical padding + borders`` and it
is compared against the CSS ``min-height`` (see the guard discussed in
``docs/UIUX-GUIDE.md``, "Composer sizing").

Hardcoding those numbers in a test fixture is how the first #7604 fixture went
wrong: it used the 18px ``data-font-size="large"`` composer (29.7px line-height,
47.7px natural row) as if it were the stylesheet's 16px default. Parse
``static/style.css`` here instead and derive every dimension from it, so the
fixtures follow the stylesheet and cannot quietly encode the wrong font size.

Usage::

    from tests._composer_metrics import CONFIG_DEFAULT, CONFIG_LARGE, composer_metrics

    m = composer_metrics(CONFIG_LARGE)
    m.natural_row        # 47.7: what one row of text wants (border box)
    m.offset_height      # 48: what a browser reports (integer, min-height floored)
    m.old_ceiling        # ceil(min-height) + 1: the pre-#7604 skip ceiling
    m.new_ceiling        # ceil(max(min-height, natural row)) + 1: with the fix
    m.computed           # {"lineHeight": "29.7px", ...} for a getComputedStyle stub
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass
from pathlib import Path

STYLE_CSS = (Path(__file__).parents[1] / "static" / "style.css").read_text(encoding="utf-8")


def _declarations(rules: list[str]) -> dict[str, str]:
    out: dict[str, str] = {}
    for rule in rules:
        for decl in rule.split(";"):
            if ":" not in decl:
                continue
            prop, _, value = decl.partition(":")
            out[prop.strip().lower()] = value.strip()
    return out


def _rule(pattern: str, *, flags: int = re.M) -> str | None:
    m = re.search(pattern, STYLE_CSS, flags)
    return m.group(1) if m else None


def _px(value: str) -> float:
    return float(value.replace("px", "").strip())


def _padding_shorthand(value: str) -> tuple[float, float]:
    """(padding-top, padding-bottom) from a CSS padding shorthand."""
    parts = value.split()
    if len(parts) == 1:  # all sides
        return _px(parts[0]), _px(parts[0])
    if len(parts) == 2:  # vertical horizontal
        return _px(parts[0]), _px(parts[0])
    if len(parts) == 3:  # top horizontal bottom
        return _px(parts[0]), _px(parts[2])
    return _px(parts[0]), _px(parts[2])


# The base composer rule: `textarea#msg{...}` (skin rules are prefixed with
# `:root[data-skin=...]`, so anchor the match at the start of a line).
_BASE = _declarations([_rule(r"^\s*textarea#msg\{([^}]*)\}") or ""])
_BASE_FONT_SIZE = _px(_BASE["font-size"])
_BASE_LINE_RATIO = float(_BASE["line-height"])
_BASE_PAD_TOP, _BASE_PAD_BOTTOM = _padding_shorthand(_BASE["padding"])
_MIN_HEIGHT = _px(_BASE["min-height"])
# `border:none` / unset borders contribute nothing to the computed border box.
_BORDER = _px(_BASE.get("border-width", "0px")) if _BASE.get("border-width") else 0.0

# `:root[data-font-size="large"] #msg { font-size: 18px; }` overrides, i.e. the
# appearance setting a real user can switch on.
_FONT_SIZE_OVERRIDES = {
    name: _px(size)
    for name, size in re.findall(
        r':root\[data-font-size="(\w+)"\]\s*#msg\s*\{\s*font-size:\s*([\d.]+px)', STYLE_CSS
    )
}

# Skins that restyle the composer's font metrics, e.g.
# `:root[data-skin="graphite"] textarea#msg{...font-size:14px;...line-height:1.45;}`.
_SKIN_OVERRIDES = {
    name: _declarations([body])
    for name, body in re.findall(
        r':root\[data-skin="([\w-]+)"\]\s*textarea#msg\{([^}]*)\}', STYLE_CSS
    )
}


@dataclass(frozen=True)
class ComposerMetrics:
    """One composer configuration, all values derived from ``static/style.css``."""

    name: str
    font_size: float
    line_height: float
    padding_top: float
    padding_bottom: float
    border_top: float
    border_bottom: float
    min_height: float

    @property
    def natural_row(self) -> float:
        """The border box one row of text wants (line-height + padding + borders)."""
        return (
            self.line_height
            + self.padding_top
            + self.padding_bottom
            + self.border_top
            + self.border_bottom
        )

    @property
    def offset_height(self) -> int:
        """What a browser reports for a one-row composer: an integer, min-height floored."""
        return round(max(self.natural_row, self.min_height))

    @property
    def old_ceiling(self) -> int:
        """The pre-#7604 skip ceiling: ``ceil(min-height) + 1`` (min-height only)."""
        return math.ceil(self.min_height) + 1

    @property
    def new_ceiling(self) -> int:
        """The skip ceiling with the natural one-row height considered."""
        return math.ceil(max(self.min_height, self.natural_row)) + 1

    @property
    def skip_was_reachable(self) -> bool:
        return self.offset_height <= self.old_ceiling

    @property
    def skip_is_reachable(self) -> bool:
        return self.offset_height <= self.new_ceiling

    @property
    def content_two_rows(self) -> int:
        """scrollHeight for a two-row value in the same box."""
        return round(2 * self.line_height + self.padding_top + self.padding_bottom)

    @property
    def oversized_box(self) -> int:
        """A composer several rows tall that must still remeasure back down."""
        return round(self.natural_row + 3 * self.line_height)

    @property
    def computed(self) -> dict[str, str]:
        """The ``getComputedStyle`` payload the JS guard reads."""

        def px(value: float) -> str:
            return f"{value:g}px"

        return {
            "minHeight": px(self.min_height),
            "lineHeight": px(self.line_height),
            "paddingTop": px(self.padding_top),
            "paddingBottom": px(self.padding_bottom),
            "borderTopWidth": px(self.border_top),
            "borderBottomWidth": px(self.border_bottom),
            "fontSize": px(self.font_size),
        }


def composer_metrics(
    font_size: float | None = None,
    line_height_ratio: float | None = None,
    *,
    name: str = "custom",
) -> ComposerMetrics:
    """Metrics for the stylesheet's composer at ``font_size`` (default: 16px)."""
    size = _BASE_FONT_SIZE if font_size is None else font_size
    ratio = _BASE_LINE_RATIO if line_height_ratio is None else line_height_ratio
    return ComposerMetrics(
        name=name,
        font_size=size,
        line_height=size * ratio,
        padding_top=_BASE_PAD_TOP,
        padding_bottom=_BASE_PAD_BOTTOM,
        border_top=_BORDER,
        border_bottom=_BORDER,
        min_height=_MIN_HEIGHT,
    )


#: The stylesheet default (16px), i.e. `textarea#msg` with no appearance override.
CONFIG_DEFAULT = composer_metrics(name="default-16px")

#: The `data-font-size` appearance overrides.
CONFIG_SMALL = composer_metrics(_FONT_SIZE_OVERRIDES.get("small"), name="small-14px")
CONFIG_LARGE = composer_metrics(_FONT_SIZE_OVERRIDES.get("large"), name="large-18px")
CONFIG_XLARGE = composer_metrics(_FONT_SIZE_OVERRIDES.get("xlarge"), name="xlarge-20px")

#: A skin that restyles the composer metrics (14px / 1.45).
_SKIN = _SKIN_OVERRIDES.get("graphite", {})
CONFIG_SKIN = composer_metrics(
    _px(_SKIN["font-size"]) if "font-size" in _SKIN else None,
    float(_SKIN["line-height"]) if "line-height" in _SKIN else None,
    name="skin-graphite-14px",
)

#: Every composer configuration the stylesheet can produce.
COMPOSER_CONFIGS = (CONFIG_DEFAULT, CONFIG_SMALL, CONFIG_LARGE, CONFIG_XLARGE, CONFIG_SKIN)
