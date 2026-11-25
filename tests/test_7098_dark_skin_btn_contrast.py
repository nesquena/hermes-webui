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


# ---------------------------------------------------------------------------
# hover state (re-gate 2026-10-01T18:38:05Z, item 1)
# ---------------------------------------------------------------------------
#
# Every solid-accent primary surface switches its fill to ``--accent-hover`` on
# hover while the text keeps ``--btn-primary-text``.  Covering only ``--accent``
# left three skins below AA (ares 3.68, sisyphus 4.46, slate 3.97).  The token
# and the hover fill therefore have to be verified *together*, per skin.


def _label(skin: str | None) -> str:
    return f"data-skin={skin}" if skin else "base :root.dark (no skin)"


@pytest.mark.parametrize("skin", DARK_SKINS + [None])
def test_dark_hover_fill_resolves_aa_with_primary_text(skin: str | None):
    """The primary foreground must pass AA on the hover fill too."""
    token = _effective(skin, "--btn-primary-text")
    accent = _effective(skin, "--accent")
    hover = _effective(skin, "--accent-hover")
    assert token and _is_hex(token), f"{_label(skin)}: --btn-primary-text unresolved ({token!r})"
    assert accent and _is_hex(accent), f"{_label(skin)}: --accent unresolved ({accent!r})"
    assert hover and _is_hex(hover), f"{_label(skin)}: --accent-hover unresolved ({hover!r})"
    ratio = contrast_ratio(token, hover)
    assert ratio >= AA_RATIO, (
        f"{_label(skin)}: primary text {token} on hover fill {hover} -> contrast "
        f"{ratio:.2f} (need >= {AA_RATIO}); the normal fill {accent} is "
        f"{contrast_ratio(token, accent):.2f}"
    )


# ---------------------------------------------------------------------------
# surface inventory (re-gate 2026-10-01T18:38:05Z, items 2 and 3)
# ---------------------------------------------------------------------------
#
# Item 2: ``static/panels.js`` painted its "Save options" primary button with a
# literal ``#1a1a1a``, which is invisible to a stylesheet-only scan.  Item 3:
# the inventory has to assert that each solid-accent textual surface *resolves*
# through the primary token instead of only rejecting co-located literals.

#: Names that mark a selector as a textual primary control.
_PRIMARY_CONTROL_RE = re.compile(
    r"(?:btn|button|send|submit|save|primary|new-chat|apply)", re.IGNORECASE
)
#: A fill that is the bare accent value (a solid primary fill), not a wash.
_SOLID_FILL_RE = re.compile(r"^var\(\s*(--accent(?:-hover)?)\s*\)$")
#: ``var(--token)`` / ``var(--token, fallback)`` -> token name.
_VAR_REF_RE = re.compile(r"var\(\s*(--[A-Za-z0-9_-]+)")
_PSEUDO_RE = re.compile(
    r"::?(?:not|is|where|has)\([^()]*\)|::?[a-z-]+(?:\([^()]*\))?", re.IGNORECASE
)
_INLINE_STYLE_LITERAL_RE = re.compile(
    r"color\s*:\s*(#[0-9a-fA-F]{3,8}|white|black)\b"
)
_INLINE_SOLID_FILL_RE = re.compile(r"background(?:-color)?\s*:\s*var\(\s*--accent(?:-hover)?\s*\)")
_INLINE_STYLE_FILES = tuple(sorted((REPO / "static").glob("*.js"))) + tuple(
    sorted((REPO / "static").glob("*.html"))
)


def _normalise_selector(selector: str) -> str:
    """Strip pseudo-classes/``:not()`` so ``x:hover`` and ``x`` compare equal."""
    return " ".join(_PSEUDO_RE.sub("", selector).split())


def _selector_scope(selector: str):
    """``(skin_or_None)`` when the selector applies in dark mode, else ``None``.

    A light-only selector (``:not(.dark)``) is skipped: its tokens come from the
    light block and are covered by the normal-state tests.
    """
    if ":not(.dark)" in selector:
        return None
    match = re.search(r'\[data-skin="([^"]+)"\]', selector)
    return match.group(1) if match else ""


def _declared_token(value: str | None) -> str | None:
    """The token a colour declaration resolves through, or ``None``."""
    if not value:
        return None
    match = _VAR_REF_RE.search(value)
    return match.group(1) if match else None


def _base_token_for(selectors: list, rule) -> str | None:
    """The colour token an inheriting surface gets from its base rule."""
    targets = {_normalise_selector(sel) for sel in selectors}
    for other in RULES:
        if other is rule:
            continue
        token = _declared_token(other.decls.get("color"))
        if not token:
            continue
        for candidate in other.selectors:
            base = _normalise_selector(candidate)
            for target in targets:
                if target == base or (
                    target.startswith(base) and target[len(base):][:1] in ".[:#"
                ):
                    return token
    return None


#: The token this contract is about; other tokens are separate design decisions
#: (an inverted button painting ``var(--bg)``, for instance) and are only
#: required to resolve *some* token, not this one.
_PRIMARY_TEXT_TOKEN = "--btn-primary-text"


def _solid_primary_surfaces() -> list:
    """Every (selector, skin_scope, fill_token, colour_token) primary surface."""
    surfaces = []
    for rule in RULES:
        fill = _SOLID_FILL_RE.match(
            (rule.decls.get("background") or rule.decls.get("background-color") or "")
            .replace("!important", "")
            .strip()
        )
        if not fill:
            continue
        colour_token = _declared_token(rule.decls.get("color"))
        for selector in rule.selectors:
            if not _PRIMARY_CONTROL_RE.search(selector):
                continue
            if "::before" in selector or "::after" in selector:
                # Decorative bar/dot painted in the accent: not a text surface.
                continue
            scope = _selector_scope(selector)
            if scope is None:
                continue
            token = colour_token or _base_token_for([selector], rule)
            surfaces.append((selector.strip(), scope, fill.group(1), token))
    return surfaces


def test_surface_inventory_finds_the_shared_primary_controls():
    """Guard: an empty/one-sided inventory would make the next test vacuous."""
    surfaces = _solid_primary_surfaces()
    fills = {item[2] for item in surfaces}
    assert {"--accent", "--accent-hover"} <= fills, f"fills discovered: {sorted(fills)}"
    selector_text = " | ".join(item[0] for item in surfaces)
    for expected in (".send-btn", ".send-btn:hover", ".settings-btn:hover",
                     ".provider-card-btn-primary:hover"):
        assert expected in selector_text, f"{expected} missing from the surface inventory"
    primary = [item for item in surfaces if item[3] == _PRIMARY_TEXT_TOKEN]
    assert len(primary) >= 20, (
        f"only {len(primary)} surface(s) resolve {_PRIMARY_TEXT_TOKEN}; the inventory "
        f"is not seeing the shared primary controls"
    )


def test_primary_text_surfaces_resolve_through_the_token():
    """Each solid-accent textual surface must resolve a token that passes AA.

    Resolution is per skin: a skin-scoped selector is measured only against its
    own skin, so a hover rule that keeps the normal-state token is caught by the
    numbers rather than by a style convention.
    """
    missing, offenders = [], []
    for selector, scope, fill_token, colour_token in _solid_primary_surfaces():
        if not colour_token:
            missing.append(f"{selector} (fill {fill_token}): no colour token resolved")
            continue
        if colour_token != _PRIMARY_TEXT_TOKEN:
            # Inverted surfaces own a different token and a different contract.
            continue
        skins = [scope] if scope else DARK_SKINS + [None]
        for skin in skins:
            foreground = _effective(skin, colour_token)
            background = _effective(skin, fill_token)
            if not (foreground and _is_hex(foreground)):
                missing.append(f"{selector}: {colour_token} unresolved for {_label(skin)}")
                continue
            if not (background and _is_hex(background)):
                missing.append(f"{selector}: {fill_token} unresolved for {_label(skin)}")
                continue
            ratio = contrast_ratio(foreground, background)
            if ratio < AA_RATIO:
                offenders.append(
                    f"{selector} [{_label(skin)}]: {colour_token}={foreground} on "
                    f"{fill_token}={background} -> {ratio:.2f}"
                )
    assert not missing, "primary surfaces with no resolvable token:\n  " + "\n  ".join(missing)
    assert not offenders, (
        f"{len(offenders)} primary surface(s) below AA:\n  " + "\n  ".join(offenders)
    )


def test_inline_primary_surfaces_use_the_token():
    """Inline styles in ``static/*.js``/``*.html`` cannot hide a literal either.

    The stylesheet-only scan stayed green while ``static/panels.js`` painted a
    solid accent button with ``color:#1a1a1a``.
    """
    offenders = []
    for path in _INLINE_STYLE_FILES:
        text = path.read_text(encoding="utf-8", errors="replace")
        for number, line in enumerate(text.splitlines(), 1):
            if not _INLINE_SOLID_FILL_RE.search(line):
                continue
            for literal in _INLINE_STYLE_LITERAL_RE.findall(line):
                if literal.lower() in {"white", "black"} or not literal.startswith("var("):
                    offenders.append(f"{path.name}:{number} -> color:{literal}")
    assert not offenders, (
        f"{len(offenders)} inline solid-accent surface(s) with a literal foreground:\n  "
        + "\n  ".join(sorted(offenders))
    )


# ---------------------------------------------------------------------------
# control cascade (re-gate 2026-11-23T04:53:02Z)
# ---------------------------------------------------------------------------
#
# The token tests above resolve custom properties.  The re-gate asked for the
# *control* cascade instead, because three defects live where a property is not
# the whole story:
#
# * Hepburn paints its New Chat hover with a fixed dark pink (#c6246a) while the
#   ink keeps ``--btn-primary-text`` (dark pin #111) -> 3.47:1;
# * Geist Contrast's ``:root[data-skin="geist-contrast"] .sm-btn:hover`` sets the
#   neutral ``--hover-bg`` with ``!important``, so it beats the higher-specificity
#   Settings-save hover fill and leaves near-black text on #111 -> 1.08:1;
# * ``_ensureAuxAdvancedModal`` paints its Save button inline with the shared
#   token, which resolves to #fff in light mode -> #fff on the gold accent
#   (#B8860B) -> 3.25:1.
#
# So: match real selectors against a modelled control, let importance,
# specificity and source order pick the winning declaration, then measure the
# pair the browser would paint.

_MODE_ROOT_RE = re.compile(
    r'^(?:html|:root)(?P<dark>\.dark)?(?P<light>:not\(\.dark\))?'
    r'(?:\[data-skin="(?P<skin>[^"]+)"\])?$'
)


def _root_parts(selector: str):
    """``(specificity, requires_dark, skin)`` for a root selector, else ``None``."""
    match = _MODE_ROOT_RE.match(selector.strip())
    if not match:
        return None
    requires_dark = bool(match.group("dark"))
    light_only = bool(match.group("light"))
    if requires_dark and light_only:
        return None
    skin = match.group("skin")
    specificity = 1 + (1 if requires_dark else 0) + (1 if light_only else 0) + (1 if skin else 0)
    return specificity, requires_dark, skin


def _effective_in(mode: str, skin: str | None, prop: str) -> str | None:
    """Resolve ``prop`` for a light or dark root, optionally scoped to a skin."""
    best = None
    for rule in RULES:
        if prop not in rule.decls:
            continue
        for selector in rule.selectors:
            parts = _root_parts(selector)
            if parts is None:
                continue
            specificity, requires_dark, selector_skin = parts
            if mode == "dark" and not requires_dark:
                continue
            if mode == "light" and requires_dark:
                continue
            if selector_skin is not None and selector_skin != skin:
                continue
            candidate = (specificity, rule.order, rule.decls[prop])
            if best is None or candidate[:2] > best[:2]:
                best = candidate
    if best is None:
        return None
    return _resolve_value_in(mode, skin, best[2])


def _resolve_value_in(mode: str, skin: str | None, value: str | None, depth: int = 0):
    """Resolve a declaration value (``var()`` chains included) for a mode/skin."""
    if value is None or depth > 5:
        return None
    text = value.replace("!important", "").strip()
    match = re.fullmatch(r"var\(\s*(--[A-Za-z0-9_-]+)\s*(?:,\s*(.+))?\)", text)
    if not match:
        return text
    token, fallback = match.group(1), match.group(2)
    resolved = _effective_in(mode, skin, token)
    if resolved is None and fallback is not None:
        return _resolve_value_in(mode, skin, fallback, depth + 1)
    return resolved


class _Node:
    """One element in the modelled chain (root -> body -> ancestors -> control)."""

    __slots__ = ("tag", "node_id", "classes", "attrs", "is_root", "hover", "focus")

    def __init__(self, tag, node_id=None, classes=(), attrs=None, is_root=False,
                 hover=False, focus=False):
        self.tag = tag
        self.node_id = node_id
        self.classes = set(classes)
        self.attrs = dict(attrs or {})
        self.is_root = is_root
        self.hover = hover
        self.focus = focus


_COMPOUND_RE = re.compile(
    r"""
    (?P<tag>[A-Za-z][A-Za-z0-9]*)|
    \#(?P<node_id>[A-Za-z0-9_-]+)|
    \.(?P<cls>[A-Za-z0-9_-]+)|
    \[(?P<attr>[A-Za-z0-9_-]+)(?:(?P<op>[\^$*~|]?=)"(?P<val>[^"]*)")?\]|
    ::(?P<pseudo_el>[A-Za-z-]+)|
    (?P<negation>:not\([^()]*\))|
    :(?P<pseudo>[A-Za-z-]+)
    """,
    re.VERBOSE,
)


def _split_compounds(selector: str) -> list:
    return [part for part in re.split(r"\s*>\s*|\s+", selector.strip()) if part]


def _compound_matches(compound: str, node: _Node) -> bool:
    matches = list(_COMPOUND_RE.finditer(compound))
    leftover = _COMPOUND_RE.sub("", compound).strip()
    if not matches or leftover:
        # An unparsed fragment means the model cannot vouch for this selector.
        return False
    for match in matches:
        if match.group("tag"):
            if node.is_root:
                if match.group("tag").lower() != "html":
                    return False
            elif node.tag != match.group("tag").lower():
                return False
        elif match.group("node_id"):
            if node.node_id != match.group("node_id"):
                return False
        elif match.group("cls"):
            if match.group("cls") not in node.classes:
                return False
        elif match.group("attr"):
            name = match.group("attr")
            if name not in node.attrs:
                return False
            actual = node.attrs[name]
            op, value = match.group("op"), match.group("val")
            if op == "^=" and not actual.startswith(value):
                return False
            if op == "=" and actual != value:
                return False
            if op in ("$=", "*=", "~=", "|="):
                return False
        elif match.group("pseudo_el"):
            return False
        elif match.group("negation"):
            inner = match.group("negation")[5:-1].strip()
            if inner == ".dark":
                if "dark" in node.classes:
                    return False
            elif inner != ":disabled":
                return False
        elif match.group("pseudo"):
            name = match.group("pseudo").lower()
            if name == "root":
                if not node.is_root:
                    return False
            elif name == "hover":
                if not node.hover:
                    return False
            elif name in ("focus", "focus-visible"):
                if not node.focus:
                    return False
            elif name == "disabled":
                return False
            else:
                return False
    return True


def _specificity(selector: str) -> tuple:
    ids = classes = elements = 0
    for compound in _split_compounds(selector):
        for match in _COMPOUND_RE.finditer(compound):
            if match.group("node_id"):
                ids += 1
            elif match.group("tag") or match.group("pseudo_el"):
                elements += 1
            else:
                classes += 1
    return ids, classes, elements


def _selector_matches(selector: str, chain: list) -> bool:
    compounds = _split_compounds(selector)
    if not compounds:
        return False
    index = len(chain) - 1
    for compound in reversed(compounds):
        matched = False
        while index >= 0:
            if _compound_matches(compound, chain[index]):
                matched = True
                index -= 1
                break
            index -= 1
        if not matched:
            return False
    return True


def _cascade_declarations(chain: list, inline: dict | None = None) -> dict:
    """Winning declaration per property, honouring importance/specificity/order."""
    winners = {}

    def consider(prop, raw, important, specificity, order):
        key = (1 if important else 0, specificity, order)
        if prop not in winners or key > winners[prop][0]:
            winners[prop] = (key, raw)

    for rule in RULES:
        for selector in rule.selectors:
            if not _selector_matches(selector, chain):
                continue
            specificity = _specificity(selector)
            important = False
            for prop, raw in rule.decls.items():
                important = raw.strip().endswith("!important")
                consider(prop, raw, important, specificity, rule.order)
    for prop, raw in (inline or {}).items():
        # Inline styles beat every normal declaration and lose to !important.
        consider(prop, raw, False, (9, 9, 9), 1 << 30)
    return {prop: raw for prop, (_key, raw) in winners.items()}


def _control_chain(*, tag="button", classes="", node_id=None, attrs=None, ancestors=(),
                   hover=False, focus=False, mode="dark", skin=None) -> list:
    root = _Node(
        "html",
        classes=({"dark"} if mode == "dark" else ()),
        attrs=({"data-skin": skin} if skin else {}),
        is_root=True,
    )
    chain = [root, _Node("body")]
    for ancestor in ancestors:
        chain.append(_Node("div", node_id=ancestor))
    chain.append(
        _Node(
            tag,
            node_id=node_id,
            classes=classes.split() if classes else (),
            attrs=attrs,
            hover=hover,
            focus=focus,
        )
    )
    return chain


def _control_pair(chain, mode, skin, inline=None):
    """``(foreground, fill, declarations)`` the browser would settle on."""
    declarations = _cascade_declarations(chain, inline)
    background = declarations.get("background") or declarations.get("background-color")
    foreground = _resolve_value_in(mode, skin, declarations.get("color"))
    fill = _resolve_value_in(mode, skin, background)
    return foreground, fill, declarations


def _assert_pair(foreground, fill, label: str) -> None:
    assert _is_hex(foreground or "") and _is_hex(fill or ""), (
        f"{label}: pair did not resolve to hex colours (fg={foreground!r} bg={fill!r})"
    )
    ratio = contrast_ratio(foreground, fill)
    assert ratio >= AA_RATIO, (
        f"{label}: {foreground} on {fill} -> {ratio:.3f} (need >= {AA_RATIO})"
    )


@pytest.mark.parametrize("mode,hover", [("dark", True), ("dark", False), ("light", True)])
def test_hepburn_new_chat_states_reach_aa(mode: str, hover: bool):
    """Hepburn's hover fill is the fixed dark pink, not ``--accent-hover``.

    Dark mode kept ``--btn-primary-text`` (#111) on #c6246a -> 3.47:1, so the
    fill/ink pair has to be readable together.  The fill is deliberately not
    pinned: either side may move.

    ``dark+hover`` is the re-gate defect.  ``dark`` normal and ``light`` hover
    are the controls the fix must keep.  The light normal state is left out on
    purpose: it is the production pair (#fff on #ec5597, 3.33:1) that no round
    of this CR asked to redesign, so tightening it here would turn a pre-existing
    palette choice into an unrequested product change.
    """
    state = ":hover" if hover else ""
    chain = _control_chain(classes="new-chat-btn", hover=hover, mode=mode, skin="hepburn")
    foreground, fill, _ = _control_pair(chain, mode, "hepburn")
    _assert_pair(foreground, fill, f"hepburn {mode} .new-chat-btn{state}")


@pytest.mark.parametrize("mode", ["dark", "light"])
@pytest.mark.parametrize("focus", [False, True])
def test_geist_settings_save_hover_keeps_an_accent_pair(mode: str, focus: bool):
    """The Settings save must not inherit Geist's neutral ``.sm-btn:hover`` wash.

    ``:root[data-skin="geist-contrast"] .sm-btn:hover{background:var(--hover-bg)
    !important}`` wins over ``#mainSettings .sm-btn[onclick^="saveSettings"]:hover``
    by importance, leaving ``--btn-primary-text`` (#050505 dark / #ffffff light)
    on #111 / #f5f5f5 -> 1.08:1 and 1.09:1.
    """
    label = f"geist-contrast {mode} Settings save :hover{' + :focus-visible' if focus else ''}"
    chain = _control_chain(
        classes="sm-btn",
        attrs={"onclick": "saveSettings(true);"},
        ancestors=("mainSettings",),
        hover=True,
        focus=focus,
        mode=mode,
        skin="geist-contrast",
    )
    foreground, fill, _ = _control_pair(chain, mode, "geist-contrast")
    neutral = (_effective_in(mode, "geist-contrast", "--hover-bg") or "").lower()
    assert (fill or "").lower() != neutral, (
        f"{label}: fill resolves to the neutral .sm-btn:hover wash {neutral!r}"
    )
    _assert_pair(foreground, fill, label)


_AUX_PRIMARY_INLINE_RE = re.compile(r"const primaryBtn='([^']*)';")


def _aux_modal_primary_declarations() -> dict:
    """The inline declarations ``_ensureAuxAdvancedModal`` gives its Save button."""
    panels = (REPO / "static" / "panels.js").read_text(encoding="utf-8")
    match = _AUX_PRIMARY_INLINE_RE.search(panels)
    assert match, "static/panels.js: the primaryBtn inline style is missing"
    declarations = {}
    for chunk in match.group(1).split(";"):
        if ":" in chunk:
            name, _, value = chunk.partition(":")
            declarations[name.strip()] = value.strip()
    assert "color" in declarations and "background" in declarations, declarations
    return declarations


@pytest.mark.parametrize("mode", ["light", "dark"])
def test_aux_advanced_save_pair_reaches_aa(mode: str):
    """``#auxAdvancedSave`` is inline-styled with the solid accent fill.

    In dark mode the shared token earns the per-skin ink; in light mode it
    resolved to #fff on the gold accent (#B8860B) -> 3.25:1, where production
    kept the dark ink (5.35:1).  A stylesheet ``!important`` override is the
    only way to beat an inline declaration, so that is what this measures.
    """
    chain = _control_chain(node_id="auxAdvancedSave", mode=mode)
    foreground, fill, _ = _control_pair(
        chain, mode, None, _aux_modal_primary_declarations()
    )
    _assert_pair(foreground, fill, f"#auxAdvancedSave ({mode} mode)")


def test_control_cascade_sees_the_competing_hover_rules():
    """Guard: an empty or token-only model would make the tests above vacuous."""
    chain = _control_chain(
        classes="sm-btn",
        attrs={"onclick": "saveSettings(true);"},
        ancestors=("mainSettings",),
        hover=True,
        mode="dark",
        skin="geist-contrast",
    )
    matched = [
        rule for rule in RULES if any(_selector_matches(sel, chain) for sel in rule.selectors)
    ]
    assert len(matched) >= 5, f"only {len(matched)} rule(s) matched the Settings save"
    competing = [
        rule
        for rule in matched
        if any(
            sel.strip() == ':root[data-skin="geist-contrast"] .sm-btn:hover'
            for sel in rule.selectors
        )
    ]
    assert competing, "the !important neutral .sm-btn:hover rule was not matched"
    assert any(
        raw.strip().endswith("!important")
        for rule in competing
        for prop, raw in rule.decls.items()
        if prop in ("background", "background-color")
    ), "the competing hover rule is not modelled as !important"
    assert _specificity(
        '#mainSettings .sm-btn[onclick^="saveSettings"]:hover'
    ) > _specificity(':root[data-skin="geist-contrast"] .sm-btn:hover'), (
        "specificity model is wrong: the neutral rule only wins through !important"
    )
