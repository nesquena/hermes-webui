"""#7827/#7092: effective (winning) foreground declaration per (skin, control).

The generic ``:root.dark`` rule added for #7092 paints the accent-fill
button family with ``var(--bg)``. But skins may override that with a
more-specific selector of their own (graphite/codex/terracotta/github pin
an explicit dark foreground; hepburn pins its own background + #fff). The
parser-level test in ``test_issue7092_accent_contrast.py`` reads only the
generic ``:root.dark`` rules, so a later, more-specific skin override that
regresses an effective button color stays green.

This module resolves the ACTUAL winning declaration per ``(skin, control)``:
it parses the stylesheet rules, keeps only those whose selector matches the
skin and the control, applies CSS cascade order (``!important``, then
specificity, then source order), resolves ``var(--…)`` references against
the skin's palette, and asserts the *effective* foreground clears contrast
against the *effective* background the element actually gets.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

CSS_PATH = Path(__file__).resolve().parents[1] / "static" / "style.css"
CSS = CSS_PATH.read_text(encoding="utf-8")

# Same family the #7092 fix claims to cover; a more-specific skin override
# must still end in a readable foreground.
ACCENT_FILL_CONTROLS = (
    ".send-btn",
    "#mainSettings .settings-btn",
    '#mainSettings .sm-btn[onclick^="saveSettings"]',
    ".toolsets-apply-btn",
    ".msg-edit-send",
)


def _strip_comments(css: str) -> str:
    return re.sub(r"/\*.*?\*/", "", css, flags=re.S)


# The tested viewport — the CI headless Chromium default. Media length features
# are evaluated against it; every other media feature (hover / pointer /
# prefers-* / display-mode…) is conservatively skipped, never blindly descended.
_VIEWPORT_WIDTH = 1280
_VIEWPORT_HEIGHT = 720


def _media_branch_applies(branch: str) -> bool | None:
    """Whether one comma-separated @media branch holds at the tested viewport.

    Returns True (certainly applies), False (certainly not), or None
    (uncertain — contains a feature this stylesheet-only oracle cannot decide,
    e.g. ``hover:hover``, ``pointer:coarse``, ``prefers-reduced-motion``). An
    uncertain branch is treated as NOT applying: descending every inactive
    media branch would not establish general CSS correctness, so the oracle
    only follows branches it can prove active at the tested viewport.
    """
    seen_feature = False
    for feature in branch.split("and"):
        feature = feature.strip().strip("()").strip()
        if not feature:
            continue
        seen_feature = True
        # Bare media types: screen/all apply in the tested (headless desktop)
        # environment; print never does. `not`/`only` prefixes are undecidable.
        if feature in ("screen", "all"):
            continue
        if feature == "print":
            return False
        if re.match(r"^(not|only)\b", feature):
            return None
        m = re.match(r"^(min|max)-(width|height)\s*:\s*([0-9.]+)(?:px)?$", feature)
        if m:
            op, prop, value = m.group(1), m.group(2), float(m.group(3))
            viewport = _VIEWPORT_WIDTH if prop == "width" else _VIEWPORT_HEIGHT
            if op == "min" and viewport < value:
                return False
            if op == "max" and viewport > value:
                return False
            continue
        return None
    return True if seen_feature else None


def _media_applies(sel: str) -> bool:
    """True when an ``@media`` rule applies at the tested viewport.

    A comma-separated media list is an OR: if any branch certainly applies,
    the block applies. Otherwise (all branches certainly-inactive or
    uncertain) the block is skipped — the oracle never descends a branch it
    cannot prove active. ``@supports`` / ``@keyframes`` / ``@font-face`` etc.
    are not ``@media`` and return False.
    """
    if not re.match(r"@media\b", sel):
        return False
    cond = sel[len("@media"):].strip()
    if not cond:
        return True
    for branch in cond.split(","):
        if _media_branch_applies(branch) is True:
            return True
    return False


def _css_rules(css: str):
    """Yield (selector, body) for every leaf rule that applies at the tested
    viewport, unwrapping applicable @media blocks.

    Braces are balanced-scanned so a rule inside ``@media`` still yields its
    own (selector, body) pair. A comma-separated selector list is split: each
    selector in the list carries its OWN specificity and only the individual
    matching selector participates in the cascade — the browser never merges
    a list's specificity, so the oracle must not either. A nested ``@media``
    block is descended only when its condition holds at the tested viewport
    (1280×720, the CI headless default); inapplicable or undecidable branches
    are skipped exactly as the browser skips them.
    """
    css = _strip_comments(css)
    rules = []
    pos = 0
    n = len(css)
    while pos < n:
        opener = css.find("{", pos)
        if opener == -1:
            break
        sel = css[pos:opener].strip()
        d = 1
        j = opener + 1
        while j < n and d:
            if css[j] == "{":
                d += 1
            elif css[j] == "}":
                d -= 1
            j += 1
        body = css[opener + 1:j - 1]
        if sel.startswith("@"):
            # At-rule: recurse only into an @media block that applies at the
            # tested viewport. Other at-rules (@supports/@keyframes/…) do not
            # participate in the plain cascade and are skipped.
            if _media_applies(sel):
                rules.extend(_css_rules(body))
        elif sel:
            for single in sel.split(","):
                single = single.strip()
                if single:
                    rules.append((single, body))
        pos = j
    return rules


def _dark_skin_names() -> set[str]:
    return set(re.findall(r':root\.dark\[data-skin="([^"]+)"\]\s*\{', CSS))


def _skin_palette(css: str, skin: str | None) -> dict[str, str]:
    tokens: dict[str, str] = {}
    root_m = re.search(r":root\.dark\s*\{([^{}]*)\}", css)
    if root_m:
        for m in re.finditer(r"--([a-z0-9-]+):\s*(#[0-9A-Fa-f]{6})", root_m.group(1)):
            tokens[m.group(1)] = m.group(2)
    if skin:
        m = re.search(
            rf':root\.dark\[data-skin="{re.escape(skin)}"\]\{{([^}}]*)\}}', css)
        if m:
            for tm in re.finditer(r"--([a-z0-9-]+):\s*(#[0-9A-Fa-f]{6})", m.group(1)):
                tokens[tm.group(1)] = tm.group(2)
    return tokens


def _specificity(sel: str) -> tuple[int, int, int]:
    """CSS specificity (ids, classes/attrs/pseudo-classes, elements).

    ``:not(…)`` contributes its inner selector's specificity; ``:root``
    counts as a pseudo-class; attribute selectors count class-level.
    """
    s = re.sub(r":not\(([^)]*)\)", r"\1", sel)
    ids = len(re.findall(r"#[\w-]+", s))
    classes = len(re.findall(r"\.[\w-]+|\[[^\]]*\]|:(?!:)[\w-]+", s))
    base = re.sub(r"#[\w-]+|\.[\w-]+|\[[^\]]*\]|:(?!:)[\w-]+", "", s)
    elements = len(re.findall(r"[a-zA-Z][\w-]*", base))
    return (ids, classes, elements)


def _control_tokens(control: str) -> list[str]:
    """The tokens that must appear in a selector for it to target the control."""
    toks = []
    for part in control.split():
        part = part.strip()
        if not part:
            continue
        if part.startswith("."):
            toks.append(part[1:])
        elif part.startswith("#"):
            toks.append(part[1:])
        elif part.startswith("["):
            m = re.search(r'[~\^$*]?="([^"]+)"', part)
            toks.append(m.group(1) if m else part.strip("[]"))
        else:
            toks.append(part)
    return [t for t in toks if t]


# Class-name suffixes / pseudo-classes that denote a *state variant* of the
# control (.send-btn.stop, :hover, :disabled...). The baseline accent-fill
# foreground contract covers the neutral control, not its states.
_VARIANT_MARKERS = (".stop", ".interrupt", ".steer", ".queue",
                    ":hover", ":active", ":focus", ":focus-within",
                    ":disabled", ":checked")


def _matches_selector(sel: str, control: str) -> bool:
    # :not(...) wraps its token — :not(:disabled) is still the BASE rule,
    # so strip that wrapper before scanning for state variants.
    no_variant = sel.replace(":not(:disabled)", "")
    if any(marker in no_variant for marker in _VARIANT_MARKERS):
        return False
    toks = _control_tokens(control)
    return all(t in sel for t in toks)


def _matches_skin(sel: str, skin: str) -> bool:
    """Rule applies to dark mode of the given skin."""
    # :not(.dark) marks a light-mode-only variant — never applies in dark.
    if ":not(.dark)" in sel:
        return False
    skin_refs = re.findall(r'data-skin="([^"]+)"', sel)
    if skin_refs:
        return skin in skin_refs
    if ":root" in sel:
        return ":root.dark" in sel or ":root" in sel
    # Plain class/element rules (.send-btn, #mainSettings .settings-btn)
    # are theme-neutral base styles — they apply in dark mode too.
    return True


def _declarations(body: str) -> list[tuple[str, str, bool]]:
    out = []
    for decl in body.split(";"):
        decl = decl.strip()
        if not decl or ":" not in decl:
            continue
        prop, _, val = decl.partition(":")
        prop = prop.strip()
        val = val.strip()
        important = "!important" in val
        val = val.replace("!important", "").strip()
        out.append((prop, val, important))
    return out


def _effective_property(css: str, skin: str, control: str, prop: str):
    """Resolve the winning declaration for (skin, control, prop)."""
    best = None
    for sel, body in _css_rules(css):
        if not _matches_selector(sel, control):
            continue
        if not _matches_skin(sel, skin):
            continue
        for p, v, important in _declarations(body):
            if p != prop:
                continue
            # (importance, ids, classes, elements, value) — higher wins;
            # equal weights keep the LAST (source order = later wins)
            key = (1 if important else 0, *_specificity(sel), v)
            if best is None or key[:-1] > best[:-1]:
                best = key
            elif best is not None and key[:-1] == best[:-1]:
                best = key
    return best[-1] if best else None


def _resolve_var(value: str, tokens: dict[str, str]) -> str:
    value = value.strip()
    if not value.startswith("var("):
        return value
    m = re.match(r"var\(\s*--([a-z0-9-]+)", value)
    if not m:
        return value
    return tokens.get(m.group(1), value)


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


def _effective_fg_bg(css: str, skin: str, control: str):
    tokens = _skin_palette(css, skin)
    fg = _effective_property(css, skin, control, "color")
    bg = _effective_property(css, skin, control, "background")
    if fg:
        fg = _resolve_var(fg, tokens)
    if bg:
        bg = _resolve_var(bg, tokens)
    return fg, bg


@pytest.mark.parametrize("skin", sorted(_dark_skin_names()))
def test_effective_accent_fill_foreground_clears_aa_for_every_skin(skin):
    """Every dark skin's effective accent-fill foreground must clear 3.0:1
    against its effective background — the generic-rule test stays green
    when a skin pin regresses, this one does not."""
    failures = {}
    for control in ACCENT_FILL_CONTROLS:
        fg, bg = _effective_fg_bg(CSS, skin, control)
        if not fg or not bg:
            continue  # not styled for this skin; generic rule covers it
        if fg.startswith("#") and bg.startswith("#"):
            ratio = _contrast(fg, bg)
            if ratio < 3.0:
                failures[control] = f"{fg} on {bg} = {ratio:.2f}"
    assert not failures, (
        f"skin '{skin}' effective accent-fill foregrounds below 3.0:1: {failures}")


def test_resolver_catches_a_more_specific_bad_override():
    """A later, more-specific skin override with a known bad foreground must
    fail the effective-contrast check — proving the resolution does cascade
    work rather than just reading the generic rule."""
    injected = (
        ':root.dark[data-skin="mono"] button.send-btn:not(:disabled)'
        "{color:#ffffff!important;}"
    )
    modified = _strip_comments(CSS) + "\n" + injected
    fg, bg = _effective_fg_bg(modified, "mono", ".send-btn")
    assert fg == "#ffffff", f"resolver must honor the more-specific override: {fg}"
    assert bg and bg.startswith("#"), f"background must resolve: {bg}"
    assert _contrast(fg, bg) < 3.0, (
        "mono 'passes' after a bad #fff override was injected?!")


def test_every_dark_skin_send_btn_resolves_a_foreground():
    """Skins must actually resolve a foreground for the primary control —
    guards against the generic-rule selector family drifting out of sync."""
    missing = []
    for skin in sorted(_dark_skin_names()):
        fg, _ = _effective_fg_bg(CSS, skin, ".send-btn")
        if not fg:
            missing.append(skin)
    assert not missing, f"skins with no resolved send-btn foreground: {missing}"


# ── Neutral-oracle fidelity controls ─────────────────────────────────────────
# A rule appended AFTER the real stylesheet wins on specificity + source order
# in the browser, so the oracle must report the appended value. The mono
# palette makes the failure visible: white on #CCCCCC is ~1.2:1.

def test_resolver_catches_a_mono_white_override_on_the_settings_button():
    """Negative control 1: `:root.dark[data-skin="mono"] #mainSettings
    .settings-btn{color:#ffffff}` (specificity 1-4-0) must beat the generic
    `:root.dark …` group in the browser.

    The group is written as a COMMA-SEPARATED selector list. A parser that
    treats the whole group as one selector sums its specificity (2 ids / many
    classes) and keeps reporting the old dark foreground — the exact fidelity
    hole this control pins. Individual selectors in a list each carry their
    own specificity; only the one that matches participates.
    """
    injected = (
        ':root.dark[data-skin="mono"] #mainSettings .settings-btn'
        "{color:#ffffff;}"
    )
    modified = _strip_comments(CSS) + "\n" + injected
    fg, bg = _effective_fg_bg(modified, "mono", "#mainSettings .settings-btn")
    assert fg == "#ffffff", (
        "resolver must honor the appended white override (the individual "
        f"matching selector's specificity), got fg={fg!r} bg={bg!r}")
    assert bg and bg.startswith("#"), f"background must resolve: {bg!r}"
    assert _contrast(fg, bg) < 3.0, (
        "white on the mono accent fill must measure below 3.0:1 — if it does "
        f"not, the resolver is not reading the override (got {fg} on {bg})")


def test_resolver_descends_an_applicable_nested_media_rule():
    """Negative control 2: an `!important` white override inside an APPLICABLE
    `@media(min-width:1px)` block must be visible to the oracle at the tested
    viewport.

    A parser that skips every nested `@media` body keeps the old dark
    foreground, while Chromium (viewport width 1280px) paints white. Media
    applicability must be evaluated at the tested viewport — not blindly
    descended (see the inapplicable-branch control below).
    """
    injected = (
        '@media(min-width:1px){:root.dark[data-skin="mono"] '
        "#mainSettings .settings-btn{color:#ffffff!important;}}"
    )
    modified = _strip_comments(CSS) + "\n" + injected
    fg, bg = _effective_fg_bg(modified, "mono", "#mainSettings .settings-btn")
    assert fg == "#ffffff", (
        "resolver must descend an applicable @media block at the tested "
        f"viewport, got fg={fg!r} bg={bg!r}")
    assert bg and bg.startswith("#"), f"background must resolve: {bg!r}"
    assert _contrast(fg, bg) < 3.0, (
        f"white on the mono accent fill must measure below 3.0:1, got {fg} on {bg}")


def test_resolver_ignores_an_inapplicable_media_branch():
    """Guard: a rule inside `@media(max-width:1px)` does NOT apply at the
    1280px test viewport, so it must not change the resolved foreground.

    Descending every media branch regardless of applicability would also be
    wrong — this control keeps the viewport evaluation honest.
    """
    injected = (
        '@media(max-width:1px){:root.dark[data-skin="mono"] '
        "#mainSettings .settings-btn{color:#ffffff!important;}}"
    )
    modified = _strip_comments(CSS) + "\n" + injected
    fg, bg = _effective_fg_bg(modified, "mono", "#mainSettings .settings-btn")
    baseline_fg, _ = _effective_fg_bg(CSS, "mono", "#mainSettings .settings-btn")
    assert fg == baseline_fg, (
        "an inapplicable media branch must not change the resolved foreground; "
        f"expected the unmodified baseline {baseline_fg!r}, got {fg!r} on {bg!r}")
    assert fg != "#ffffff", "the inapplicable branch's white must not win"
    assert bg and bg.startswith("#"), f"background must resolve: {bg!r}"
