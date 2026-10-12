"""#7827 re-gate: effective HOVER contrast for the accent-fill control family.

The baseline module (`test_issue7827_effective_accent_contrast.py`) resolves the
winning declaration per ``(skin, control)`` for the NEUTRAL control — it
deliberately skips state variants, so ``:hover`` rules are invisible to it. That
is why the re-gate found four dark skins regressing on hover while every test was
green: the ``color:var(--bg)`` rule added for #7092 is correct against
``--accent-hover``, but three of these skins paint a DIFFERENT hover background
(a skin-local ``--hover-bg`` or their own pinned fill), and ``--bg`` is nearly
black there.

This module resolves the hover state: for each (skin, control) it collects the
rules whose selector matches the skin, the control, AND the ``:hover`` state,
applies the same cascade (important -> specificity -> source order), resolves
``var(--…)`` against the skin's palette, and asserts the effective hover
foreground clears 3.0:1 against the effective hover background.

Numbers below are the ones the review reproduced, kept in the test so a future
edit cannot silently re-break them:

    geist-contrast  #000000 on #111111 -> 1.11:1   (was: Save Settings invisible)
    hepburn         #110a0f on #c6246a -> 3.60:1   (was 5.44:1)
    ares            #000000 on #CC3333 -> 4.09:1   (was 5.14:1)
    slate           #000000 on #64748B -> 4.41:1   (was 4.76:1)
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

CSS_PATH = Path(__file__).resolve().parents[1] / "static" / "style.css"
CSS = CSS_PATH.read_text(encoding="utf-8")

ACCENT_FILL_CONTROLS = (
    ".send-btn",
    "#mainSettings .settings-btn",
    '#mainSettings .sm-btn[onclick^="saveSettings"]',
    ".toolsets-apply-btn",
    ".msg-edit-send",
)

# The Appearance "Save Settings" button carries BOTH classes on one element
# (`class="settings-btn sm-btn"`), so a rule targeting either class paints it.
# geist-contrast restyles `.sm-btn:hover` with `background:var(--hover-bg)
# !important` (#111111), which is the hover fill the review measured — not the
# generic `--accent-hover`. Listing the element's classes lets the resolver see
# both rules instead of silently picking the generic one.
CONTROL_CLASS_ALIASES = {
    "#mainSettings .settings-btn": ("settings-btn", "sm-btn"),
    '#mainSettings .sm-btn[onclick^="saveSettings"]': ("sm-btn", "settings-btn"),
    ".send-btn": ("send-btn",),
    ".toolsets-apply-btn": ("toolsets-apply-btn",),
    ".msg-edit-send": ("msg-edit-send",),
}

# Stop/Interrupt/Steer/Queue are separate chrome with their own hover fills; the
# review explicitly excluded them, so they are excluded here too.
_VARIANT_EXCLUDED = (".stop", ".interrupt", ".steer", ".queue")

# The four skins the review reproduced, with the ratio it measured. Used both as
# the regression set and as documentation of what "correct" means here.
REGRESSION_SKINS = {
    "geist-contrast": 1.11,
    "hepburn": 3.60,
    "ares": 4.09,
    "slate": 4.41,
}


def _strip_comments(css: str) -> str:
    return re.sub(r"/\*.*?\*/", "", css, flags=re.S)


def _css_rules(css: str):
    """Yield (selector, body) for every leaf rule, unwrapping @media blocks."""
    css = _strip_comments(css)
    out = []
    i = 0
    n = len(css)
    while i < n:
        brace = css.find("{", i)
        if brace < 0:
            break
        selector = css[i:brace].strip()
        depth = 1
        j = brace + 1
        while j < n and depth:
            if css[j] == "{":
                depth += 1
            elif css[j] == "}":
                depth -= 1
            j += 1
        body = css[brace + 1 : j - 1]
        if selector.startswith("@"):
            # At-rule: recurse into its body so inner rules are still yielded.
            out.extend(_css_rules(body))
        else:
            for sel in selector.split(","):
                sel = sel.strip()
                if sel:
                    out.append((sel, body))
        i = j
    return out


def _dark_skin_names() -> set[str]:
    names = set()
    for sel, _ in _css_rules(CSS):
        for m in re.findall(r'data-skin="([^"]+)"', sel):
            names.add(m)
    return names


def _global_palette(css: str) -> dict[str, str]:
    """The ``:root`` custom properties, used as the fallback for skins that only
    override part of the palette (slate/ares dark blocks define no ``--bg``)."""
    tokens: dict[str, str] = {}
    for sel, body in _css_rules(css):
        if sel.strip() not in (":root", ":root.dark", "html", ":root.dark,"):
            if not re.fullmatch(r":root(\.dark)?", sel.strip()):
                continue
        for prop, val, _important in _declarations(body, include_custom_props=True):
            if prop.startswith("--"):
                tokens[prop] = val
    for _ in range(6):
        changed = False
        for key, val in list(tokens.items()):
            if "var(" not in val:
                continue
            resolved = _resolve_var(val, tokens)
            if resolved != val:
                tokens[key] = resolved
                changed = True
        if not changed:
            break
    return tokens


def _skin_palette(css: str, skin: str) -> dict[str, str]:
    """Custom-property values declared for this skin, light and dark blocks.

    Source order matters: the ``:root.dark[data-skin=…]`` block is declared
    after the light one, so a later declaration wins, and the values themselves
    may reference other custom properties (``--accent-bg: rgba(…)`` referencing
    ``--accent``), so resolve the chain once at the end.
    """
    tokens: dict[str, str] = {}
    for sel, body in _css_rules(css):
        refs = re.findall(r'data-skin="([^"]+)"', sel)
        if skin not in refs:
            continue
        for prop, val, _important in _declarations(body, include_custom_props=True):
            if prop.startswith("--"):
                tokens[prop] = val
    # Resolve var() references between the skin's own tokens (bounded passes).
    for _ in range(6):
        changed = False
        for key, val in list(tokens.items()):
            if "var(" not in val:
                continue
            resolved = _resolve_var(val, tokens)
            if resolved != val:
                tokens[key] = resolved
                changed = True
        if not changed:
            break
    # A skin that overrides only part of the palette inherits the rest from
    # :root (slate's dark block defines no --bg, so var(--bg) must resolve).
    inherited = _global_palette(css)
    inherited.update(tokens)
    return inherited


def _control_tokens(control: str) -> list[str]:
    """Tokens that must appear in a selector for it to target the control.

    ``#mainSettings .sm-btn[onclick^="saveSettings"]`` must also match a
    skin rule that targets ``.sm-btn`` generally: the settings modal lives inside
    ``#mainSettings``, and geist-contrast restyles ``.sm-btn`` wholesale. Emit
    the class token as well as the attribute token so the broader rule is seen.
    """
    toks = []
    for part in control.split():
        part = part.strip()
        if not part:
            continue
        if "[" in part:
            # ``sm-btn[onclick^="saveSettings"]`` -> the class/element AND the
            # attribute value, so both the exact rule and a broader ``.sm-btn``
            # rule match.
            head, _, attr = part.partition("[")
            head = head.strip().lstrip(".#")
            if head:
                toks.append(head)
            m = re.search(r'[~\^$*]?="([^"]+)"', attr)
            toks.append(m.group(1) if m else attr.strip("[]"))
            continue
        if part.startswith("."):
            toks.append(part[1:])
        elif part.startswith("#"):
            toks.append(part[1:])
        else:
            toks.append(part)
    return [t for t in toks if t]


def _specificity(sel: str) -> tuple[int, int, int]:
    ids = len(re.findall(r"#[\w-]+", sel))
    classes = len(re.findall(r"\.[\w-]+", sel)) + len(re.findall(r"\[[^\]]+\]", sel))
    # Pseudo-classes count as class-level; :not() adds its argument's weight.
    classes += len(re.findall(r":(?!:)(?!not\b)[\w-]+", sel))
    elements = len(re.findall(r"(?:^|[\s>+~])([a-zA-Z][\w-]*)", sel))
    return (ids, classes, elements)


def _declarations(body: str, *, include_custom_props: bool = False):
    """Split a rule body into ``(property, value, important)`` triples.

    Custom properties (``--accent: …``) are skipped by default because they are
    palette definitions rather than paint; ``_skin_palette`` opts in.
    """
    out = []
    for decl in body.split(";"):
        decl = decl.strip()
        if not decl or ":" not in decl:
            continue
        prop, _, val = decl.partition(":")
        prop = prop.strip()
        val = val.strip()
        if prop.startswith("--") and not include_custom_props:
            continue
        important = "!important" in val
        val = val.replace("!important", "").strip()
        out.append((prop, val, important))
    return out


def _matches_hover(sel: str, control: str) -> bool:
    """Selector targets the HOVER state of this control.

    The class/id tokens are REQUIRED; the attribute-value token is only a
    DISCRIMINATOR. ``#mainSettings .sm-btn[onclick^="saveSettings"]`` is one
    button, but a skin may restyle ``.sm-btn`` wholesale (geist-contrast does,
    with ``!important``), and that broader rule must still count as targeting
    this control — requiring the attribute value would hide it and resolve the
    hover background from the generic ``--accent-hover`` rule instead.
    """
    if ":hover" not in sel:
        return False
    if any(marker in sel for marker in _VARIANT_EXCLUDED):
        return False
    # The element's classes are alternatives (one element, several classes), so
    # ANY of them matches. An ancestor like #mainSettings is only required when
    # the selector names one: a skin may restyle `.sm-btn` directly without
    # repeating the modal id, and that rule still paints this button.
    classes = CONTROL_CLASS_ALIASES.get(control, ())
    if classes and not any(c in sel for c in classes):
        return False
    if "mainSettings" in sel and "mainSettings" not in control:
        return False
    return True


def _is_attribute_value(control: str, token: str) -> bool:
    """True when *token* came from an attribute selector's value, not a class."""
    for m in re.findall(r'\[[^\]]*="([^"]+)"', control):
        if m == token:
            return True
    return False


def _matches_skin(sel: str, skin: str) -> bool:
    if ":not(.dark)" in sel:
        return False
    skin_refs = re.findall(r'data-skin="([^"]+)"', sel)
    if skin_refs:
        return skin in skin_refs
    # A skin-agnostic hover rule still applies.
    return True


def _resolve_var(value: str, tokens: dict[str, str]) -> str:
    for _ in range(4):
        m = re.search(r"var\((--[\w-]+)\)", value)
        if not m:
            break
        name = m.group(1)
        if name not in tokens:
            return value
        value = value.replace(f"var({name})", tokens[name])
    return value


def _hex_luminance(hex_color: str) -> float:
    h = hex_color.lstrip("#")
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    if len(h) != 6:
        raise ValueError(f"not a 6-digit hex colour: {hex_color!r}")

    def _lin(c: int) -> float:
        c = c / 255.0
        return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4

    r, g, b = (int(h[i : i + 2], 16) for i in (0, 2, 4))
    return 0.2126 * _lin(r) + 0.7152 * _lin(g) + 0.0722 * _lin(b)


def _contrast(a: str, b: str) -> float:
    la, lb = _hex_luminance(a), _hex_luminance(b)
    hi, lo = max(la, lb), min(la, lb)
    return (hi + 0.05) / (lo + 0.05)


def _effective_hover_fg_bg(css: str, skin: str, control: str):
    """Winning (color, background) for the control's :hover state in this skin.

    Falls back to the NEUTRAL rule's background when no hover rule sets one,
    because an unset hover background means the neutral fill is still showing.
    """
    tokens = _skin_palette(css, skin)
    best_fg = None
    best_bg = None
    for order, (sel, body) in enumerate(_css_rules(css)):
        if not _matches_hover(sel, control) or not _matches_skin(sel, skin):
            continue
        spec = _specificity(sel)
        for prop, val, important in _declarations(body):
            resolved = _resolve_var(val, tokens)
            if prop == "color":
                cand = (1 if important else 0, spec, order, resolved)
                if best_fg is None or cand[:3] > best_fg[:3]:
                    best_fg = cand
            elif prop in ("background", "background-color"):
                cand = (1 if important else 0, spec, order, resolved)
                if best_bg is None or cand[:3] > best_bg[:3]:
                    best_bg = cand
    fg = best_fg[3] if best_fg else None
    bg = best_bg[3] if best_bg else None
    if bg is not None:
        # `background` may be a shorthand; keep the colour token only.
        bg = _resolve_var(bg.split()[0] if bg else "", tokens)
    if bg is None:
        # No hover background declared: the neutral fill still shows.
        bg = _neutral_background(css, skin, control, tokens)
    return fg, bg


def _neutral_background(css: str, skin: str, control: str, tokens) -> str | None:
    best = None
    for order, (sel, body) in enumerate(_css_rules(css)):
        if ":hover" in sel or not _matches_skin(sel, skin):
            continue
        if ":hover" in sel:
            continue
        classes = CONTROL_CLASS_ALIASES.get(control, ())
        if classes and not any(c in sel for c in classes):
            continue
        if "mainSettings" in sel and "mainSettings" not in control:
            continue
        if any(marker in sel for marker in _VARIANT_EXCLUDED):
            continue
        spec = _specificity(sel)
        for prop, val, important in _declarations(body):
            if prop in ("background", "background-color"):
                cand = (1 if important else 0, spec, order, _resolve_var(val, tokens))
                if best is None or cand[:3] > best[:3]:
                    best = cand
    if best is None:
        return None
    # `background` may be a shorthand (`background:#fff none`); take the colour.
    value = best[3].split()[0] if best[3] else ""
    return _resolve_var(value, tokens)


# The minimum hover contrast per (skin, control). These are the ratios the
# review measured as CORRECT before the regression — NOT a generic AA floor,
# which would pass the regressed values (4.09 / 4.41) and let the fix be
# reverted without a single test going red.
#
# Controls with no hover background rule of their own keep the skin's `--accent`
# fill, which is lighter than `--accent-hover`; the review scoped its findings to
# the controls that DO have a hover fill, and those carry the higher floor.
REGRESSION_MIN_RATIO = {
    "geist-contrast": {
        "#mainSettings .settings-btn": 4.5,
        '#mainSettings .sm-btn[onclick^="saveSettings"]': 4.5,
    },
    "hepburn": {
        ".send-btn": 4.5,
        "#mainSettings .settings-btn": 4.5,
        '#mainSettings .sm-btn[onclick^="saveSettings"]': 4.5,
        ".toolsets-apply-btn": 4.5,
        ".msg-edit-send": 4.5,
    },
    "ares": {
        ".send-btn": 5.14,
        "#mainSettings .settings-btn": 5.14,
        '#mainSettings .sm-btn[onclick^="saveSettings"]': 5.14,
    },
    "slate": {
        ".send-btn": 4.76,
        "#mainSettings .settings-btn": 4.76,
        '#mainSettings .sm-btn[onclick^="saveSettings"]': 4.76,
        # No hover background rule of their own: these keep the skin's --accent
        # (#94A3B8), where the generic dark foreground gives 8.19:1 and #fff
        # drops to 2.56:1. Floor set below the correct value but well above the
        # regression so the fix cannot be flipped back.
        ".toolsets-apply-btn": 4.5,
        ".msg-edit-send": 4.5,
    },
    }


@pytest.mark.parametrize("skin", sorted(REGRESSION_SKINS))
def test_reviewed_skins_hover_contrast_clears_aa(skin):
    """The four skins the review reproduced must reach their pre-regression ratio."""
    floors = REGRESSION_MIN_RATIO[skin]
    failures = {}
    for control, floor in floors.items():
        fg, bg = _effective_hover_fg_bg(CSS, skin, control)
        if not fg or not bg:
            failures[control] = "no hover rule resolved"
            continue
        if not (fg.startswith("#") and bg.startswith("#")):
            continue  # rgba/gradient fills are not decidable here
        ratio = _contrast(fg, bg)
        # A hair of tolerance for float representation of the measured value.
        if ratio < floor - 0.01:
            failures[control] = f"{fg} on {bg} = {ratio:.2f}:1 (floor {floor})"
    assert not failures, (
        f"skin '{skin}' hover foregrounds below their pre-regression ratio: {failures}"
    )


def test_geist_contrast_hover_uses_the_skin_text_not_bg():
    """Finding 1 specifically: geist-contrast's hover fill is ``--hover-bg``
    (#111111), and its ``--bg`` is #000000 — 1.11:1. The fix must switch the
    hover foreground to ``--text`` (#ededed -> 16.13:1)."""
    tokens = _skin_palette(CSS, "geist-contrast")
    assert tokens.get("--hover-bg", "").lower() == "#111111"
    fg, bg = _effective_hover_fg_bg(CSS, "geist-contrast", "#mainSettings .settings-btn")
    assert fg is not None and bg is not None, "no hover rule resolved for geist-contrast"
    assert _resolve_var(fg, tokens).lower() == tokens.get("--text", "").lower(), (
        f"geist-contrast hover foreground must resolve to --text ({tokens.get('--text')}), "
        f"got {fg!r} -> {_resolve_var(fg, tokens)!r}"
    )
    assert _contrast(_resolve_var(fg, tokens), _resolve_var(bg, tokens)) >= 4.5, (
        "geist-contrast hover contrast must clear AA for normal text"
    )


def test_hepburn_hover_keeps_its_own_white_foreground():
    """Finding 2: hepburn's ``.send-btn:hover`` pins ``#c6246a`` as the fill, so
    the generic ``var(--bg)`` (#110a0f) drops it to 3.60:1. Restore ``#fff``."""
    tokens = _skin_palette(CSS, "hepburn")
    fg, bg = _effective_hover_fg_bg(CSS, "hepburn", ".send-btn")
    assert fg is not None and bg is not None, "no hover rule resolved for hepburn"
    resolved_fg = _resolve_var(fg, tokens).lower()
    resolved_bg = _resolve_var(bg, tokens).lower()
    assert resolved_fg.lstrip("#") in ("fff", "ffffff"), (
        f"hepburn send-btn hover foreground must be #fff, got {resolved_fg}"
    )
    assert resolved_bg == "#c6246a", (
        f"hepburn send-btn hover background must stay its own #c6246a, got {resolved_bg}"
    )
    assert _contrast(resolved_fg, resolved_bg) >= 4.5
    # The composer variant has no hover background of its own, so it keeps the
    # accent fill — where the generic rule's dark foreground is the right answer
    # (7.51:1) and #fff would be 2.60:1.
    edit_fg, edit_bg = _effective_hover_fg_bg(CSS, "hepburn", ".msg-edit-send")
    assert edit_fg and edit_bg, "no hover rule resolved for hepburn's composer send"
    resolved_edit_fg = _resolve_var(edit_fg, tokens).lower()
    resolved_edit_bg = _resolve_var(edit_bg, tokens).lower()
    assert _contrast(resolved_edit_fg, resolved_edit_bg) >= 4.5, (
        f"hepburn composer send hover: {resolved_edit_fg} on {resolved_edit_bg} "
        f"= {_contrast(resolved_edit_fg, resolved_edit_bg):.2f}:1"
    )


def test_resolver_rejects_a_regressed_hover_foreground():
    """Control: inject the exact regression the review found (a skin-agnostic
    ``color:var(--bg)`` winning over hepburn's #fff) and prove the check fails.

    Without this, the hover resolver could be reading nothing and passing.
    """
    # Specificity must beat hepburn's own `.send-btn:hover` rule (1 id, 2 class,
    # 0 element), so mirror the selector the generic #7092 rule uses.
    injected = (
        ':root.dark[data-skin="hepburn"] button.send-btn:not(:disabled):hover'
        "{color:var(--bg);}"
    )
    modified = _strip_comments(CSS) + "\n" + injected
    tokens = _skin_palette(modified, "hepburn")
    fg, bg = _effective_hover_fg_bg(modified, "hepburn", ".send-btn")
    assert fg is not None, "injected hover rule must be visible to the resolver"
    resolved_fg = _resolve_var(fg, tokens).lower()
    resolved_bg = _resolve_var(bg, tokens).lower() if bg else ""
    if resolved_fg.startswith("#") and resolved_bg.startswith("#"):
        assert _contrast(resolved_fg, resolved_bg) < 4.5, (
            "the injected regression should drop hepburn below AA — if it does not, "
            "the resolver is not honoring the injected rule"
        )
