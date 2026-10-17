"""#7827 re-gate: effective HOVER contrast for the accent-fill control family.

The baseline module (`test_issue7827_effective_accent_contrast.py`) resolves the
winning declaration per ``(skin, control)`` for the NEUTRAL control — it
deliberately skips state variants, so ``:hover`` rules are invisible to it. That
is why the re-gate found four dark skins regressing on hover while every test was
green: the ``color:var(--bg)`` rule added for #7092 is correct against
``--accent-hover``, but three of these skins paint a DIFFERENT hover background
(a skin-local ``--hover-bg`` or their own pinned fill), and ``--bg`` is nearly
black there.

This module resolves the hover state: for each (skin, control) it collects
EVERY rule whose selector matches the control while it is hovered — the neutral
rules AND the ``:hover`` rules together — applies the same cascade (important ->
specificity -> source order), resolves ``var(--…)`` against the skin's palette,
and asserts the effective hover foreground clears the pre-regression ratio
against the effective hover background.

A neutral selector still matches a hovered element: the generic
``:root.dark #mainSettings …`` foreground rules carry the ``#mainSettings`` ID,
so they beat any ID-free skin hover rule in the browser. Resolving only the
``:hover`` rules (as the first version of this module did) silently omits the
winning production rule and lets the regression pass — which is exactly what the
re-gate reproduced. The resolver therefore considers both kinds together.

Numbers below are the ones the review reproduced, kept in the test so a future
edit cannot silently re-break them:

    geist-contrast  Save Settings hovered  #000000 on #111111 -> 1.11:1  (was 18.88:1)
    ares            Settings hovered       #0D0D1A on #CC3333 -> 3.75:1  (was 5.14:1)
    slate           Settings hovered       #0D0D1A on #64748B -> 4.05:1  (was 4.76:1)
    ares            msg-edit/toolsets      #fff    on #FF4444 -> 3.41:1  (was 5.66:1)
    hepburn         Send/Queue             #fff    on #c6246a -> 5.44:1
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

CSS_PATH = Path(__file__).resolve().parents[1] / "static" / "style.css"
CSS = CSS_PATH.read_text(encoding="utf-8")

ACCENT_FILL_CONTROLS = (
    ".send-btn",
    ".send-btn.queue",
    "#mainSettings .settings-btn",
    '#mainSettings .sm-btn[onclick^="saveSettings"]',
    ".toolsets-apply-btn",
    ".msg-edit-send",
)

# Real markup (static/index.html): the auxiliary buttons (Reset all to auto /
# Apply changes) carry ONLY the `settings-btn` class, and the Appearance /
# Preferences "Save Settings" button is `sm-btn` with onclick="saveSettings()".
# No element carries both classes. The alias list for each control is therefore
# the class(es) that element actually has — a rule targeting `.sm-btn` must NOT
# be counted as painting the auxiliary-only `.settings-btn`, and vice versa.
CONTROL_CLASS_ALIASES = {
    "#mainSettings .settings-btn": ("settings-btn",),
    '#mainSettings .sm-btn[onclick^="saveSettings"]': ("sm-btn",),
    ".send-btn": ("send-btn",),
    ".send-btn.queue": ("send-btn",),
    ".toolsets-apply-btn": ("toolsets-apply-btn",),
    ".msg-edit-send": ("msg-edit-send",),
}

# Stop/Interrupt/Steer are separate chrome with their own hover fills (error /
# purple); the review explicitly excluded them. Queue shares the Send hover
# fill and IS a variant of the accent-fill control, so it is resolved here.
_VARIANT_EXCLUDED = (".stop", ".interrupt", ".steer")

# State pseudo-classes that contradict a hovered, enabled element: rules that
# match :disabled/:active/:focus… do not paint it, so they must not participate
# even though they are "neutral" (non-:hover) selectors. :not(:disabled) is
# fine — the element IS enabled — which is why :not(...) wrappers are stripped
# before this probe runs.
_STATE_PSEUDOS = re.compile(
    r":(?:disabled|checked|active|focus|focus-within|focus-visible|"
    r"placeholder-shown|read-only|indeterminate)\b"
)

# The four skins the review reproduced, with the ratio it measured as correct
# on master. Used both as the regression set and as documentation.
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
    """Selector applies to the control while it is hovered.

    A hovered element still matches its neutral rules: the ID-bearing
    ``:root.dark #mainSettings …`` foreground beats a later, lower-specificity
    skin hover rule in the browser — the exact production bug this suite exists
    for. So ``:hover`` is NOT required here. The class/id tokens are REQUIRED;
    the attribute-value token is only a DISCRIMINATOR (``#mainSettings
    .sm-btn[onclick^="saveSettings"]`` is one button, but a skin may restyle
    ``.sm-btn`` wholesale and that broader rule still paints it).

    State pseudo-classes that contradict a hovered-enabled element, and the
    stop/interrupt/steer chrome variants, are excluded.
    """
    if any(marker in sel for marker in _VARIANT_EXCLUDED):
        return False
    if "::" in sel or re.fullmatch(r"\d+%", sel.strip()):
        return False  # pseudo-element / keyframe block
    state_probe = re.sub(r":not\([^)]*\)", "", sel)
    if _STATE_PSEUDOS.search(state_probe):
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


def _matches_skin(sel: str, skin: str) -> bool:
    if ":not(.dark)" in sel:
        return False
    skin_refs = re.findall(r'data-skin="([^"]+)"', sel)
    if skin_refs:
        return skin in skin_refs
    # A skin-agnostic rule still applies.
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

    Neutral AND ``:hover`` declarations are resolved TOGETHER in one pass with
    the full cascade (importance, then specificity, then source order): a
    neutral foreground with an ID ancestor outranks a later ID-free hover rule,
    and an ``!important`` neutral background outranks a hover background. When
    no rule sets a background at all, the neutral fill is inherited — but that
    rule itself participates here, so nothing extra is needed.
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
    return fg, bg


# The minimum hover contrast per (skin, control). These are the ratios the
# review measured as CORRECT on master — NOT a generic AA floor, which would
# pass the regressed values (4.09 / 4.41) and let the fix be reverted without a
# single test going red.
REGRESSION_MIN_RATIO = {
    "geist-contrast": {
        ".send-btn": 4.5,
        ".send-btn.queue": 4.5,
        "#mainSettings .settings-btn": 4.5,
        '#mainSettings .sm-btn[onclick^="saveSettings"]': 4.5,
    },
    "hepburn": {
        ".send-btn": 4.5,
        ".send-btn.queue": 4.5,
        "#mainSettings .settings-btn": 4.5,
        '#mainSettings .sm-btn[onclick^="saveSettings"]': 4.5,
        ".toolsets-apply-btn": 4.5,
        ".msg-edit-send": 4.5,
    },
    "ares": {
        ".send-btn": 5.14,
        ".send-btn.queue": 5.14,
        "#mainSettings .settings-btn": 5.14,
        '#mainSettings .sm-btn[onclick^="saveSettings"]': 5.14,
        # No hover background of their own: they keep the #FF4444 accent fill,
        # where the dark foreground gives 5.66:1 and white drops to 3.41:1.
        ".toolsets-apply-btn": 4.5,
        ".msg-edit-send": 4.5,
    },
    "slate": {
        ".send-btn": 4.76,
        ".send-btn.queue": 4.76,
        "#mainSettings .settings-btn": 4.76,
        '#mainSettings .sm-btn[onclick^="saveSettings"]': 4.76,
        # No hover background rule of their own: these keep the skin's --accent
        # (#94A3B8), where the generic dark foreground gives 7.52:1 and #fff
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
            failures[control] = "no rule resolved"
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


def test_geist_contrast_save_hover_uses_the_skin_text_not_bg():
    """Finding 1 specifically: geist-contrast's Save hover fill is ``--hover-bg``
    (#111111), and its ``--bg`` is #000000 — 1.11:1. The Save button's hover
    foreground must be ``--text`` (#ededed -> 16.13:1), and the auxiliary-only
    ``.settings-btn`` must NOT be flipped to light text on its own light hover
    fill (that would be light-on-light)."""
    tokens = _skin_palette(CSS, "geist-contrast")
    assert tokens.get("--hover-bg", "").lower() == "#111111"
    save_fg, save_bg = _effective_hover_fg_bg(
        CSS, "geist-contrast", '#mainSettings .sm-btn[onclick^="saveSettings"]'
    )
    assert save_fg is not None and save_bg is not None, (
        "no hover rule resolved for geist-contrast Save"
    )
    assert _resolve_var(save_fg, tokens).lower() == tokens.get("--text", "").lower(), (
        f"geist-contrast Save hover foreground must resolve to --text "
        f"({tokens.get('--text')}), got {save_fg!r} -> {_resolve_var(save_fg, tokens)!r}"
    )
    assert _contrast(_resolve_var(save_fg, tokens), _resolve_var(save_bg, tokens)) >= 4.5, (
        "geist-contrast Save hover contrast must clear AA for normal text"
    )
    # The auxiliary-only control carries no `sm-btn` class, so the ID-bearing
    # Save override must not paint it: it keeps the dark neutral foreground on
    # its light hover fill.
    aux_fg, aux_bg = _effective_hover_fg_bg(CSS, "geist-contrast", "#mainSettings .settings-btn")
    assert aux_fg is not None and aux_bg is not None, (
        "no hover rule resolved for geist-contrast auxiliary settings"
    )
    resolved_aux_fg = _resolve_var(aux_fg, tokens).lower()
    assert resolved_aux_fg == tokens.get("--bg", "").lower(), (
        f"geist-contrast auxiliary settings must keep the dark neutral foreground "
        f"(--bg {tokens.get('--bg')}), got {resolved_aux_fg}"
    )
    assert _contrast(resolved_aux_fg, _resolve_var(aux_bg, tokens)) >= 4.5


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


def test_queue_variant_resolves_like_send():
    """Queue is a variant of the accent-fill Send control (the same element with
    a `.queue` class toggled) and shares its hover fill: it must resolve the
    same hover foreground/background as `.send-btn` in every reviewed skin."""
    for skin in sorted(REGRESSION_SKINS):
        send = _effective_hover_fg_bg(CSS, skin, ".send-btn")
        queue = _effective_hover_fg_bg(CSS, skin, ".send-btn.queue")
        assert queue[0] is not None and queue[1] is not None, (
            f"{skin}: queue hover did not resolve"
        )
        assert queue == send, (
            f"{skin}: .send-btn.queue hover ({queue}) differs from .send-btn ({send})"
        )


def test_id_bearing_neutral_foreground_beats_lower_specificity_hover_rule():
    """Real-markup shape of the production bug: the ID-bearing NEUTRAL
    foreground (`:root.dark #mainSettings .settings-btn`, an ID ancestor) still
    matches the hovered element and beats a later, lower-specificity skin hover
    rule. The resolver must pick the neutral value — and must therefore flag
    the dark-on-dark regression instead of reporting the pretty hover rule
    that loses in the browser."""
    fixture = """
:root{--bg:#0D0D1A;--accent-hover:#CC3333;}
:root.dark #mainSettings .settings-btn{color:var(--bg);}
#mainSettings .settings-btn{background:var(--accent-hover);}
:root.dark[data-skin="ares"] .settings-btn:hover{color:#ffffff;}
"""
    fg, bg = _effective_hover_fg_bg(fixture, "ares", "#mainSettings .settings-btn")
    assert fg is not None and bg is not None, "fixture rules must resolve"
    resolved_fg = _resolve_var(fg, {"--bg": "#0D0D1A"}).lower()
    resolved_bg = _resolve_var(bg, {"--accent-hover": "#CC3333"}).lower()
    assert resolved_fg == "#0d0d1a", (
        f"ID-bearing neutral foreground must win over the later lower-specificity "
        f"hover rule: expected #0D0D1A (var(--bg)), got {resolved_fg!r}"
    )
    assert resolved_fg != "#ffffff", "the ID-free hover rule must NOT win"
    # Dark on the #CC3333 hover fill — 3.75:1, the regression the resolver
    # must flag (it cannot, if neutral rules are dropped from the resolution).
    assert _contrast(resolved_fg, resolved_bg) < 4.5, (
        f"the ID-neutral-wins shape must resolve below AA, got "
        f"{resolved_fg} on {resolved_bg} = {_contrast(resolved_fg, resolved_bg):.2f}:1"
    )


def test_important_neutral_background_beats_hover_background():
    """Cascade control: an ``!important`` NEUTRAL background outranks a later
    non-important hover background, and the resolver must honor that (a
    hover-only resolver could not see the neutral rule at all)."""
    injected = "#mainSettings .settings-btn{background:#0a0a0a!important;}"
    modified = _strip_comments(CSS) + "\n" + injected
    fg, bg = _effective_hover_fg_bg(modified, "mono", "#mainSettings .settings-btn")
    assert fg is not None and bg is not None, "injected rules must resolve"
    assert _resolve_var(bg, {}).lower() == "#0a0a0a", (
        f"!important neutral background must beat the later non-important hover "
        f"background, got {bg!r}"
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
