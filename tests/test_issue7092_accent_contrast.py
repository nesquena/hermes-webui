"""Accent-fill button contrast in dark mode (#7092).

Dark-mode skins whose ``--accent`` is light (mono, catppuccin, hepburn,
sienna, geist-contrast, verdigris, codex, …) painted hardcoded ``#fff``
foregrounds on the accent fill. White on mono's ``#CCCCCC`` is ~1.2:1 --
unreadable, far below WCAG AA. #3810 already documented 29 theme variants
falling below 3.0 and recommended ``color: var(--bg)`` for text on an
accent fill; this issue asks for the generalization instead of yet another
per-skin override.

The fix adds one dark-mode rule in ``static/style.css`` painting the
solid-accent button family (send button, settings buttons, toolsets apply,
edit-send) with ``var(--bg)`` -- ``--accent`` is by design contrasted
against ``--bg``, so the rule is correct for every palette.

Rather than pinning the new selector text (a grep-style test stays green
through a re-broken rule), these tests parse the stylesheet, resolve each
dark skin's own ``--accent``/``--bg`` pair, and assert the *computed* WCAG
contrast of the solid-accent button family actually clears AA for every
dark palette -- the property the issue is about, not the exact syntax that
delivers it.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest


REPO = Path(__file__).resolve().parents[1]
CSS = (REPO / "static" / "style.css").read_text(encoding="utf-8")


# Buttons whose background is a solid var(--accent) fill. A dark-mode
# foreground override must cover exactly this family.
ACCENT_FILL_BUTTONS = (
    ".send-btn",
    "#mainSettings .settings-btn",
    '#mainSettings .sm-btn[onclick^="saveSettings"]',
    ".toolsets-apply-btn",
    ".msg-edit-send",
)


def _hex_luminance(hex_color: str) -> float:
    r = int(hex_color[1:3], 16) / 255
    g = int(hex_color[3:5], 16) / 255
    b = int(hex_color[5:7], 16) / 255

    def lin(c: float) -> float:
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4

    return 0.2126 * lin(r) + 0.7152 * lin(g) + 0.0722 * lin(b)


def _contrast(a: str, b: str) -> float:
    la, lb = _hex_luminance(a), _hex_luminance(b)
    hi, lo = max(la, lb), min(la, lb)
    return (hi + 0.05) / (lo + 0.05)


def _dark_skin_palettes() -> dict[str, dict[str, str]]:
    """Resolve every dark skin's palette tokens.

    The root ``:root.dark`` block provides the fallback ``--bg`` for
    partial overrides (e.g. mono only overrides the accent family); each
    ``:root.dark[data-skin="x"]`` block merges on top of that root.
    """
    root_m = re.search(r":root\.dark\s*\{([^{}]*)\}", CSS)
    root_tokens: dict[str, str] = {}
    if root_m:
        for m in re.finditer(r"--([a-z0-9-]+):(#[0-9A-Fa-f]{6})", root_m.group(1)):
            root_tokens[m.group(1)] = m.group(2)

    palettes: dict[str, dict[str, str]] = {}
    for m in re.finditer(r':root\.dark\[data-skin="([^"]+)"\]\{([^{}]*)\}', CSS):
        skin, body = m.group(1), m.group(2)
        if skin in palettes:
            continue  # first (palette-defining) block wins
        tokens = dict(root_tokens)
        for tm in re.finditer(r"--([a-z0-9-]+):(#[0-9A-Fa-f]{6})", body):
            tokens[tm.group(1)] = tm.group(2)
        palettes[skin] = tokens
    return palettes


def _accent_fill_rule() -> dict[str, str]:
    """Return the computed ``color`` for each accent-fill button selector
    under ``:root.dark`` from the real stylesheet."""
    colors: dict[str, str] = {}
    for m in re.finditer(
        r"([^{}]*?)\{([^{}]*color:[^{}]*)\}", CSS
    ):
        selector_blob, body = m.group(1), m.group(2)
        if ":root.dark" not in selector_blob:
            continue
        cm = re.search(r"color:\s*(#[0-9A-Fa-f]{3,8}|var\([^)]*\))", body)
        if not cm:
            continue
        color = cm.group(1)
        for part in selector_blob.split(","):
            sel = part.strip()
            if not sel.startswith(":root.dark"):
                continue
            bare = sel[len(":root.dark"):].strip()
            if bare:
                # normalize "button.send-btn:not(:disabled)" -> ".send-btn"
                # so the family membership check stays selector-form agnostic
                bare = re.sub(r"\bbutton\.", ".", bare)
                bare = re.sub(r":not\([^)]*\)", "", bare).strip()
                colors[bare] = color
    return colors


def test_every_dark_skin_accent_clears_wcag_aa_against_bg():
    """The property the issue asks for: each dark palette's --accent used
    as a button fill must clear 3.0 contrast against its own --bg.

    This is the invariant ``color: var(--bg)`` guarantees by construction
    (--accent is designed to contrast with --bg); asserting it on the
    resolved palettes catches any future skin that defines a light accent
    without a dark-enough bg pair.
    """
    palettes = _dark_skin_palettes()
    assert palettes, "no dark skin palettes parsed -- CSS structure changed?"
    offenders = {}
    for skin, tokens in palettes.items():
        accent, bg = tokens.get("accent"), tokens.get("bg")
        if not accent or not bg:
            continue  # dark-only accent-less skins (zeus) not in scope
        ratio = _contrast(accent, bg)
        if ratio < 3.0:
            offenders[skin] = f"{accent} vs {bg} = {ratio:.2f}"
    assert not offenders, (
        f"dark skins whose --accent cannot carry text against --bg "
        f"(a var(--bg) foreground would inherit the problem): {offenders}"
    )


def test_dark_accent_fill_buttons_use_bg_foreground():
    """The actual fix: under :root.dark, every solid-accent button in the
    family must paint its foreground from --bg (not hardcoded #fff)."""
    rule = _accent_fill_rule()
    missing = [b for b in ACCENT_FILL_BUTTONS if b not in rule]
    assert not missing, (
        f"no :root.dark foreground rule for accent-fill buttons: {missing} "
        f"(dark skins with light --accent render white-on-light text)"
    )
    for button in ACCENT_FILL_BUTTONS:
        color = rule[button]
        assert color == "var(--bg)", (
            f"{button} must paint its dark-mode foreground with var(--bg), "
            f"got {color!r}"
        )


def test_light_mode_buttons_keep_white_foreground():
    """Guard the other half of the contract: the base (light-mode) rules
    keep #fff on the accent fill -- the dark override must not leak."""
    base = re.search(r"(?<!:root\.dark )\.send-btn\{([^}]*)\}", CSS)
    assert base and "color:#fff" in base.group(1), (
        "base .send-btn must keep its light-mode white foreground"
    )
    settings = re.search(
        r"(?<!:root\.dark )#mainSettings \.settings-btn[^{]*\{([^}]*)\}", CSS)
    assert settings and "color:#fff" in settings.group(1)


def test_mono_dark_palette_contrast_pairs_are_sane():
    """The issue's reporter skin: mono dark --accent=#CCCCCC must clear 3.0
    against the dark root --bg it inherits."""
    palettes = _dark_skin_palettes()
    assert "mono" in palettes, "mono skin palette not found"
    mono = palettes["mono"]
    assert mono["accent"] == "#CCCCCC", "mono dark accent changed unexpectedly"
    ratio = _contrast(mono["accent"], mono["bg"])
    assert ratio >= 3.0, (
        f"mono dark accent {mono['accent']} vs bg {mono['bg']} = {ratio:.2f}"
    )


@pytest.mark.parametrize("skin", ["catppuccin", "geist-contrast", "graphite",
                                  "hepburn", "sienna", "verdigris", "codex"])
def test_previously_broken_skins_now_clear_aa(skin):
    """The skins measured broken by the issue (#3810 audit + local WCAG
    math): each must clear 4.5:1 (AA for normal text) with the fix."""
    palettes = _dark_skin_palettes()
    assert skin in palettes, f"{skin} dark palette not found"
    ratio = _contrast(palettes[skin]["accent"], palettes[skin]["bg"])
    assert ratio >= 4.5, (
        f"{skin} dark accent fill vs bg = {ratio:.2f} (< 4.5 AA)"
    )
