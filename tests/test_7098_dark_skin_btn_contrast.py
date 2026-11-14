"""WCAG AA (4.5:1) contrast for primary-button text on every dark skin.

Companion to #7098 (CR on #7092). ``--btn-primary-text`` is the single token
that decides the foreground of anything painted with the skin's solid
``--accent`` (new-chat button, send button, clarify submit, settings save, ...).

These tests parse ``static/style.css`` and assert *computed* contrast ratios —
never substrings — so a skin that drops the token, or pins it back to ``#fff``,
fails with the real numbers:

* every ``:root.dark[data-skin=...]`` token block must pin its own
  ``--btn-primary-text`` whose ratio against that block's ``--accent`` passes;
* the value a browser actually resolves (mini-cascade: ``:root`` ->
  ``:root.dark`` -> skin blocks) must pass for every skin, including the
  base ``:root.dark`` default;
* no rule may pair a solid ``var(--accent)``/``var(--accent-hover)``
  background with a literal foreground colour — that is the defect class the
  CR asked to close.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
CSS = (REPO / "static" / "style.css").read_text(encoding="utf-8")

#: WCAG 2.1 SC 1.4.3 — normal text (AA).
AA_RATIO = 4.5

# ---------------------------------------------------------------------------
# colour maths
# ---------------------------------------------------------------------------


def _srgb_to_linear(channel: float) -> float:
    return channel / 12.92 if channel <= 0.03928 else ((channel + 0.055) / 1.055) ** 2.4


def relative_luminance(colour: str) -> float:
    """WCAG relative luminance of an ``#rgb``/``#rrggbb`` colour."""
    hex_part = colour.strip().lstrip("#")
    if len(hex_part) == 3:
        hex_part = "".join(ch * 2 for ch in hex_part)
    if len(hex_part) != 6:
        raise ValueError(f"not a hex colour: {colour!r}")
    red, green, blue = (int(hex_part[i : i + 2], 16) / 255 for i in (0, 2, 4))
    return (
        0.2126 * _srgb_to_linear(red)
        + 0.7152 * _srgb_to_linear(green)
        + 0.0722 * _srgb_to_linear(blue)
    )


def contrast_ratio(foreground: str, background: str) -> float:
    """WCAG contrast ratio between two hex colours (1.0 .. 21.0)."""
    lighter = relative_luminance(foreground)
    darker = relative_luminance(background)
    high, low = max(lighter, darker), min(lighter, darker)
    return (high + 0.05) / (low + 0.05)


_HEX_RE = re.compile(r"^#(?:[0-9a-f]{3}|[0-9a-f]{6})$", re.IGNORECASE)


def _is_hex(value: str) -> bool:
    return bool(_HEX_RE.match(value.strip()))


# ---------------------------------------------------------------------------
# stylesheet parsing
# ---------------------------------------------------------------------------


class Rule:
    """A single CSS rule: selector list + resolved declaration map."""

    __slots__ = ("selectors", "decls", "order")

    def __init__(self, selectors, decls, order):
        self.selectors = selectors
        self.decls = decls
        self.order = order

    @property
    def text(self) -> str:
        return ", ".join(self.selectors)


def _parse_declarations(body: str) -> dict:
    decls = {}
    for chunk in body.split(";"):
        if ":" not in chunk:
            continue
        name, _, value = chunk.partition(":")
        name = name.strip()
        if name.startswith("--"):
            decls[name] = value.strip()
        elif name and not name.startswith("@"):
            decls[name] = value.strip()
    return decls


def _parse_rules(text: str, counter=None) -> list:
    if counter is None:
        counter = [0]
    rules = []
    index = 0
    selector_start = 0
    length = len(text)
    while index < length:
        if text[index] == "{":
            selector = text[selector_start:index].strip()
            depth = 1
            cursor = index + 1
            while cursor < length and depth:
                if text[cursor] == "{":
                    depth += 1
                elif text[cursor] == "}":
                    depth -= 1
                cursor += 1
            body = text[index + 1 : cursor - 1]
            counter[0] += 1
            if selector.startswith("@"):
                rules.extend(_parse_rules(body, counter))
            else:
                rules.append(
                    Rule(
                        [part.strip() for part in selector.split(",") if part.strip()],
                        _parse_declarations(body),
                        counter[0],
                    )
                )
            selector_start = cursor
            index = cursor
            continue
        index += 1
    return rules


_COMMENT_RE = re.compile(r"/\*.*?\*/", re.DOTALL)
RULES = _parse_rules(_COMMENT_RE.sub(" ", CSS))

#: ``:root`` / ``:root.dark`` optionally carrying a ``[data-skin="..."]``.
_ROOT_SELECTOR_RE = re.compile(
    r'^(?:html|:root)(?P<dark>\.dark)?(?:\[data-skin="(?P<skin>[^"]+)"\])?$'
)


def _root_info(selector: str):
    """Return ``(specificity, requires_dark, skin_or_None)`` or ``None``."""
    match = _ROOT_SELECTOR_RE.match(selector)
    if not match:
        return None
    specificity = 1 + (1 if match.group("dark") else 0) + (1 if match.group("skin") else 0)
    return specificity, bool(match.group("dark")), match.group("skin")


def _resolve_var(value: str, depth: int = 0) -> str | None:
    """Resolve a bare ``var(--x)``/``var(--x, fallback)`` declaration value."""
    if value is None or depth > 5:
        return None
    value = value.strip()
    if not value.startswith("var("):
        return value
    inner = value[value.index("(") + 1 : value.rindex(")")]
    if "," in inner:
        name, _, fallback = inner.partition(",")
        return _resolve_var(fallback, depth + 1)
    return _resolve_var(_effective(None, inner.strip()), depth + 1)


def _effective(skin: str | None, prop: str) -> str | None:
    """Resolve ``prop`` for ``<html class="dark" data-skin=...>`` (dark mode)."""
    best = None  # (specificity, order, value)
    for rule in RULES:
        if prop not in rule.decls:
            continue
        for selector in rule.selectors:
            info = _root_info(selector)
            if info is None:
                continue
            specificity, requires_dark, selector_skin = info
            if not requires_dark:
                continue
            if selector_skin is not None and selector_skin != skin:
                continue
            candidate = (specificity, rule.order, rule.decls[prop])
            if best is None or candidate[:2] > best[:2]:
                best = candidate
    if best is None:
        return None
    return _resolve_var(best[2])


def _dark_skin_blocks() -> dict:
    """Every ``:root.dark[data-skin=...]`` token block, keyed by skin name."""
    blocks = {}
    for rule in RULES:
        if not any(key.startswith("--") for key in rule.decls):
            continue
        for selector in rule.selectors:
            info = _root_info(selector)
            if info is None:
                continue
            _specificity, requires_dark, skin = info
            if requires_dark and skin:
                blocks.setdefault(skin, {}).update(rule.decls)
    return blocks


DARK_SKIN_BLOCKS = _dark_skin_blocks()
DARK_SKINS = sorted(DARK_SKIN_BLOCKS)


def _accent_for(skin: str) -> str:
    """The solid accent that skin's dark primary buttons are painted with."""
    accent = DARK_SKIN_BLOCKS[skin].get("--accent") or _effective(skin, "--accent")
    assert accent and _is_hex(accent), f"{skin}: no hex --accent resolved (got {accent!r})"
    return accent


# ---------------------------------------------------------------------------
# tests
# ---------------------------------------------------------------------------


def test_stylesheet_parsed_into_a_meaningful_rule_set():
    """Guard: a broken parser would silently turn every other test green."""
    assert len(RULES) > 500
    root_dark = _effective(None, "--btn-primary-text")
    assert root_dark and _is_hex(root_dark), f"base :root.dark token unresolved: {root_dark!r}"
    assert DARK_SKINS, "no :root.dark[data-skin=...] token blocks discovered"
    for skin in ("charizard", "hepburn", "neon", "nous", "poseidon", "sienna", "verdigris"):
        assert skin in DARK_SKINS, f"{skin} dark block missing from parse"


@pytest.mark.parametrize("skin", DARK_SKINS)
def test_dark_skin_block_pins_aa_btn_primary_text(skin: str):
    """Each dark skin block must pin its own token, and it must pass AA."""
    block = DARK_SKIN_BLOCKS[skin]
    accent = _accent_for(skin)
    token = block.get("--btn-primary-text")
    ratio = contrast_ratio(token, accent) if token and _is_hex(token) else None
    assert ratio is not None and ratio >= AA_RATIO, (
        f"{skin}: :root.dark[data-skin={skin}] declares --btn-primary-text="
        f"{token or 'MISSING'} on --accent:{accent} -> contrast "
        f"{f'{ratio:.2f}' if ratio is not None else 'n/a'} "
        f"(need >= {AA_RATIO})"
    )


@pytest.mark.parametrize("skin", DARK_SKINS + [None])
def test_dark_cascade_resolves_aa_primary_text(skin: str | None):
    """What the browser actually computes in dark mode must pass AA."""
    label = f"data-skin={skin}" if skin else "base :root.dark (no skin)"
    token = _effective(skin, "--btn-primary-text")
    accent = _effective(skin, "--accent")
    assert token and _is_hex(token), f"{label}: --btn-primary-text unresolved ({token!r})"
    assert accent and _is_hex(accent), f"{label}: --accent unresolved ({accent!r})"
    ratio = contrast_ratio(token, accent)
    assert ratio >= AA_RATIO, (
        f"{label}: {token} on {accent} -> contrast {ratio:.2f} (need >= {AA_RATIO})"
    )


_SOLID_ACCENT_BG_RE = re.compile(r"var\(--accent(?:-hover)?[,)]")
_LITERAL_COLOUR_RE = re.compile(
    r"^(?:#[0-9a-f]{3}|#[0-9a-f]{6}|white|black)$", re.IGNORECASE
)


def _offenders_on_solid_accent_background() -> list:
    found = []
    for rule in RULES:
        background = rule.decls.get("background") or rule.decls.get("background-color") or ""
        colour = rule.decls.get("color") or ""
        colour = colour.replace("!important", "").strip()
        if _SOLID_ACCENT_BG_RE.search(background) and _LITERAL_COLOUR_RE.match(colour):
            found.append(f"{rule.text} -> color:{colour}")
    return sorted(found)


def test_no_hardcoded_foreground_on_solid_accent_background():
    """The defect class: literal text colour on a solid accent fill.

    A literal can only be right for one mode/skin; ``--btn-primary-text`` is
    resolved per skin, so any literal left here re-opens the failure.
    """
    offenders = _offenders_on_solid_accent_background()
    assert not offenders, (
        f"{len(offenders)} rule(s) paint a solid accent background with a literal "
        f"colour instead of var(--btn-primary-text):\n  " + "\n  ".join(offenders)
    )
