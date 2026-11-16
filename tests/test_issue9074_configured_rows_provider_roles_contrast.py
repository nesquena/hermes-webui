"""Regression coverage for #9074, the follow-ups to #8739's Configured chip.

1. #8739 moved the provider out of the Configured chip into its ``title``, which
   touch and keyboard users can't reach. The row now shows it as the muted
   ``.model-opt-provider`` pill the group rows use, beside the role chip.
2. ``Primary`` / ``Fallback N`` come from the backend in English. All three badge
   renderers now translate the role through ``t()``.
3. The primary/fallback/selected chip colours had no light-theme override and
   measured ~1.1:1 on the light dropdown. The light rules mix theme tokens and
   must stay >= 4.5:1 (WCAG 1.4.3) on plain, hover and active rows.
"""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

import pytest

from tests.test_issue8733_configured_chip_role import _DRIVER, _PAYLOAD, NODE, UI_JS_PATH

REPO_ROOT = Path(__file__).resolve().parents[1]
STYLE_CSS = (REPO_ROOT / "static" / "style.css").read_text(encoding="utf-8")
I18N_JS = (REPO_ROOT / "static" / "i18n.js").read_text(encoding="utf-8")
UI_JS = UI_JS_PATH.read_text(encoding="utf-8")

# The #8733 driver's t() returns the key for everything; let a payload supply a
# locale so the translated path can be exercised.
_LOCALE_T = """function t(key, ...args) {
  const loc = payload.locale || {};
  if (Object.prototype.hasOwnProperty.call(loc, key)) {
    return String(loc[key]).replace(/\\{(\\d+)\\}/g, (m, i) => (i in args ? String(args[i]) : m));
  }
"""
assert _DRIVER.count("function t(key, ...args) {\n") == 1
_DRIVER_WITH_LOCALE = _DRIVER.replace("function t(key, ...args) {\n", _LOCALE_T, 1)


def _render(tmp_path, payload):
    if NODE is None:
        pytest.skip("node not on PATH")
    driver = tmp_path / "driver.js"
    driver.write_text(_DRIVER_WITH_LOCALE, encoding="utf-8")
    result = subprocess.run(
        [NODE, str(driver), str(UI_JS_PATH), json.dumps(payload)],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)["rows"]


def _configured_row(rows, name):
    matches = [row for row in rows if row["name"] == name and row["chipText"] is not None]
    assert len(matches) == 1, f"expected one Configured row named {name!r}, got {matches}"
    return matches[0]


_PROVIDER_PILL_RE = re.compile(
    r'<span class="model-opt-provider model-opt-provider--configured(?: model-opt-provider--fit)?" '
    r'title="([^"]*)">([^<]*)</span>'
)


# ── 1. provider visible without hover ─────────────────────────────────────


@pytest.mark.parametrize(
    "name,provider",
    [("gpt-4o", "OpenAI"), ("gpt-4o-mini", "OpenAI"), ("codex/gpt-6.1-sol", "Magpie")],
)
def test_configured_row_shows_its_provider_as_a_pill_beside_the_chip(tmp_path, name, provider):
    row = _configured_row(_render(tmp_path, _PAYLOAD), name)

    pill = _PROVIDER_PILL_RE.search(row["html"])
    assert pill, f"Configured row {name} shows no visible provider: {row['html']}"
    assert pill.group(2) == provider
    assert pill.group(1) == provider, "the pill's title must carry the full name it may ellipsize"
    # The pill sits beside the chip; the chip keeps only the role (#8739).
    assert provider not in row["chipText"]
    assert row["html"].index("model-opt-badge--") < pill.start()


def test_long_custom_provider_name_is_kept_whole_in_the_pill(tmp_path):
    long_name = "Magpie Research Inference Gateway (eu-west staging)"
    payload = json.loads(json.dumps(_PAYLOAD))
    payload["groups"][1]["provider"] = long_name
    row = _configured_row(_render(tmp_path, payload), "codex/gpt-6.1-sol")

    pill = _PROVIDER_PILL_RE.search(row["html"])
    assert pill and pill.group(2) == long_name and pill.group(1) == long_name


# The pill must name the provider the row's click selects. A row's group label
# can be the backend's injected "Default" group (a default missing from its
# provider's catalog, or a label that carries a "(N of M)" count), or empty for
# a configured id outside every group.
# renderModelDropdown's default onSelect calls selectModelFromDropdown(value, provider).
_SELECT_RECORDER = (
    "function selectFromDropdown() {}\n"
    "function selectModelFromDropdown(value, provider) { globalThis.__sel = { value, provider }; }"
)
assert _DRIVER_WITH_LOCALE.count("function selectFromDropdown() {}") == 1
assert _DRIVER_WITH_LOCALE.count("  rows.push({\n") == 1
_DRIVER_WITH_CLICKS = _DRIVER_WITH_LOCALE.replace(
    "function selectFromDropdown() {}", _SELECT_RECORDER, 1
).replace(
    "  rows.push({\n",
    "  globalThis.__sel = null;\n  node.onclick({ stopPropagation() {}, preventDefault() {} });\n"
    "  rows.push({\n    selected: globalThis.__sel,\n",
    1,
)
_CUSTOM_ID = "custom:magpie-research-inference-gateway-eu-west-staging"
_CUSTOM_LABEL = "Magpie Research Inference Gateway (eu-west staging)"


def _render_with_clicks(tmp_path, payload):
    if NODE is None:
        pytest.skip("node not on PATH")
    driver = tmp_path / "driver_clicks.js"
    driver.write_text(_DRIVER_WITH_CLICKS, encoding="utf-8")
    result = subprocess.run(
        [NODE, str(driver), str(UI_JS_PATH), json.dumps(payload)],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)["rows"]


@pytest.mark.parametrize(
    "groups,badges,name,provider,pill_text",
    [
        pytest.param(
            [
                {"provider": "Nous Portal (15 of 40)", "provider_id": "nous", "models": [{"id": "hermes-4-70b"}]},
                {"provider": "Default", "provider_id": "nous", "models": [{"id": "hermes-4-405b"}]},
            ],
            {"hermes-4-405b": {"provider": "nous", "role": "primary", "label": "Primary"}},
            "hermes-4-405b", "nous", "Nous Portal",
            id="count-label-default-group",
        ),
        pytest.param(
            [
                {"provider": _CUSTOM_LABEL, "provider_id": _CUSTOM_ID, "models": [{"id": "llama-4"}]},
                {"provider": "Default", "provider_id": _CUSTOM_ID, "models": [{"id": "gpt-4o-custom"}]},
            ],
            {"gpt-4o-custom": {"provider": _CUSTOM_ID, "role": "primary", "label": "Primary"}},
            "gpt-4o-custom", _CUSTOM_ID, _CUSTOM_LABEL,
            id="custom-catalog-miss",
        ),
        pytest.param(
            [
                {"provider": "OpenAI", "provider_id": "openai", "models": [{"id": "gpt-4o"}]},
                {"provider": "Default", "provider_id": "custom:solo-gw", "models": [{"id": "solo-1"}]},
            ],
            {"solo-1": {"provider": "custom:solo-gw", "role": "primary", "label": "Primary"}},
            "solo-1", "custom:solo-gw", "solo-gw",
            id="default-group-only-falls-back-to-the-id",
        ),
        pytest.param(
            [{"provider": "OpenAI", "provider_id": "openai", "models": [{"id": "gpt-4o"}]}],
            {"o9-preview": {"provider": "openai", "role": "fallback", "label": "Fallback 1"}},
            "o9-preview", "openai", "OpenAI",
            id="configured-id-outside-every-group",
        ),
    ],
)
def test_configured_provider_pill_names_the_provider_the_click_selects(
    tmp_path, groups, badges, name, provider, pill_text
):
    payload = {"groups": groups, "configuredBadges": badges, "selectedValue": ""}
    row = _configured_row(_render_with_clicks(tmp_path, payload), name)

    assert row["selected"] and row["selected"]["provider"] == provider, row
    pill = _PROVIDER_PILL_RE.search(row["html"])
    assert pill, f"Configured row {name} shows no provider pill: {row['html']}"
    assert pill.group(2) == pill_text and pill.group(1) == pill_text, (
        f"the pill reads {pill.group(2)!r} but the click selects {provider!r}"
    )


def test_configured_row_name_is_escaped_exactly_once(tmp_path):
    """Picker rows carry names already escaped where they are built, so the
    Configured row renders ``m.name`` as is. Escaping it again would show
    ``&amp;`` text; not escaping at build time would inject markup."""
    label = '<img src=x onerror="alert(1)">R&D'
    payload = {
        "groups": [{"provider": "OpenAI", "provider_id": "openai", "models": [{"id": "rnd-1", "label": label}]}],
        "configuredBadges": {"rnd-1": {"provider": "openai", "role": "primary", "label": "Primary"}},
        "selectedValue": "",
    }
    rows = [row for row in _render_with_clicks(tmp_path, payload) if row["chipText"] is not None]
    assert len(rows) == 1, rows
    assert rows[0]["name"] == "&lt;img src=x onerror=&quot;alert(1)&quot;&gt;R&amp;D"
    assert "<img" not in rows[0]["html"]


def test_configured_provider_pill_ellipsizes_instead_of_wrapping():
    rule = re.search(r"\.model-opt-provider--configured\{([^}]*)\}", STYLE_CSS)
    assert rule, "no .model-opt-provider--configured rule"
    body = rule.group(1)
    # A zero flex-basis keeps the pill from forcing a wrap; it grows up to its own width.
    for decl in ("flex:1 1 0", "max-width:max-content", "overflow:hidden", "text-overflow:ellipsis"):
        assert decl in body, f"{decl} missing from {body}"


@pytest.mark.parametrize(
    "provider,fits",
    [
        ("OpenAI", True),
        ("Anthropic", True),
        ("OpenRouter", True),
        ("Nous Portal", True),
        ("Fourteen chars", True),
        ("Fifteen chars!!", False),
        ("Magpie Research Inference Gateway (eu-west staging)", False),
    ],
)
def test_short_provider_names_keep_their_width_instead_of_a_stub(tmp_path, provider, fits):
    """An ellipsis on a short name leaves a stub ("Ope…", "A…") that can't tell
    OpenAI from OpenRouter or OpenCode, so a name of up to 14 characters keeps
    its full width and wraps whole; longer names still shrink to an 8em floor."""
    payload = json.loads(json.dumps(_PAYLOAD))
    payload["groups"][0]["provider"] = provider
    row = _configured_row(_render(tmp_path, payload), "gpt-4o")
    pill = re.search(r'<span class="(model-opt-provider model-opt-provider--configured[^"]*)"', row["html"])
    assert pill, row["html"]
    assert ("model-opt-provider--fit" in pill.group(1).split()) is fits, pill.group(1)


def test_fit_pill_never_shrinks_and_long_pills_keep_an_8em_floor():
    fit = re.search(r"\.model-opt-provider--configured\.model-opt-provider--fit\{([^}]*)\}", STYLE_CSS)
    assert fit, "no .model-opt-provider--configured.model-opt-provider--fit rule"
    assert "flex:0 0 auto" in fit.group(1) and "min-width:0" in fit.group(1), fit.group(1)
    base = re.search(r"\.model-opt-provider--configured\{([^}]*)\}", STYLE_CSS)
    assert base and "min-width:8em" in base.group(1), base and base.group(1)
    # The row wraps, so a pill that won't shrink moves to its own line whole.
    top = re.search(r"\.model-opt-top\{([^}]*)\}", STYLE_CSS)
    assert top and "flex-wrap:wrap" in top.group(1), top and top.group(1)


# ── 2. translated roles ───────────────────────────────────────────────────


def test_configured_chips_use_the_translated_role(tmp_path):
    payload = dict(_PAYLOAD, locale={"model_badge_primary": "Principale", "model_badge_fallback": "Riserva {0}"})
    rows = _render(tmp_path, payload)

    assert _configured_row(rows, "gpt-4o")["chipText"] == "Principale"
    assert _configured_row(rows, "gpt-4o-mini")["chipText"] == "Riserva 1"
    assert _configured_row(rows, "codex/gpt-6.1-sol")["chipText"] == "Riserva 2"


def test_untranslated_locale_falls_back_to_the_english_role(tmp_path):
    rows = _render(tmp_path, _PAYLOAD)  # t() returns the key itself

    assert _configured_row(rows, "gpt-4o")["chipText"] == "Primary"
    assert _configured_row(rows, "codex/gpt-6.1-sol")["chipText"] == "Fallback 2"


def test_all_three_badge_renderers_go_through_the_translated_label():
    assert UI_JS.count("${esc(_modelBadgeLabel(m.badge))}") == 2, "group-row renderers"
    assert "badgeLabel = _modelBadgeLabel(m.badge);" in UI_JS, "Configured renderer"
    assert "esc(m.badge.label||'Configured')" not in UI_JS


def _locale_blocks():
    starts = list(re.finditer(r"^  (?:([a-z]{2})|'([a-zA-Z-]+)'): \{", I18N_JS, re.M))
    for match, nxt in zip(starts, starts[1:] + [None], strict=True):
        yield match.group(1) or match.group(2), I18N_JS[match.end() : nxt.start() if nxt else len(I18N_JS)]


def test_every_locale_translates_both_roles():
    blocks = dict(_locale_blocks())
    assert len(blocks) >= 15
    for locale, block in blocks.items():
        primary = re.search(r"^\s+model_badge_primary: '([^']+)',", block, re.M)
        fallback = re.search(r"^\s+model_badge_fallback: '([^']+)',", block, re.M)
        assert primary, f"{locale}: model_badge_primary missing"
        assert fallback and "{0}" in fallback.group(1), f"{locale}: model_badge_fallback needs {{0}}"


# ── 3. light-theme contrast ───────────────────────────────────────────────


def _decls(body):
    return dict(re.findall(r"--([a-z0-9-]+)\s*:\s*([^;]+);", body))


def _light_palettes():
    """The default light palette, and each skin's light palette on top of it."""
    base = _decls(re.search(r"^  :root \{(.*?)\}", STYLE_CSS, re.M | re.S).group(1))
    overrides = {"default": {}}
    for pattern in (
        r':root\[data-skin="([a-z-]+)"\]\s*\{([^}]*)\}',
        r':root\[data-skin="([a-z-]+)"\]:not\(\.dark\)\s*\{([^}]*)\}',
    ):
        for match in re.finditer(pattern, STYLE_CSS):
            overrides.setdefault(match.group(1), {}).update(_decls(match.group(2)))
    return {name: dict(base, **values) for name, values in overrides.items()}


LIGHT_PALETTES = _light_palettes()


def _color(value):
    """(rgb, alpha) for #rgb, #rrggbb or rgba(...)."""
    value = value.strip()
    if value.startswith("#"):
        digits = value[1:]
        if len(digits) == 3:
            digits = "".join(c * 2 for c in digits)
        return tuple(int(digits[i : i + 2], 16) for i in (0, 2, 4)), 1.0
    r, g, b, a = (float(x) for x in re.match(r"rgba\(([^)]*)\)", value).group(1).split(","))
    return (r, g, b), a


def _luminance(rgb):
    def channel(v):
        v /= 255
        return v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4

    r, g, b = map(channel, rgb)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def _contrast(a, b):
    hi, lo = sorted((_luminance(a), _luminance(b)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


def _over(rgb, alpha, base):
    return tuple(c * alpha + b * (1 - alpha) for c, b in zip(rgb, base, strict=True))


def test_light_palettes_cover_the_skins():
    assert len(LIGHT_PALETTES) >= 10, sorted(LIGHT_PALETTES)
    assert "catppuccin" in LIGHT_PALETTES


@pytest.mark.parametrize("skin", sorted(LIGHT_PALETTES))
@pytest.mark.parametrize("role", ["primary", "fallback", "selected"])
def test_light_theme_chip_text_meets_aa_contrast(role, skin):
    light = re.search(
        rf":root:not\(\.dark\) \.model-opt-badge--{role}\{{color:color-mix\(in srgb,var\(--([a-z]+)\) (\d+)%,var\(--text\)\);\}}",
        STYLE_CSS,
    )
    assert light, f"no light-theme colour for the {role} chip"
    palette = LIGHT_PALETTES[skin]
    token, _ = _color(palette[light.group(1)])
    percent = int(light.group(2)) / 100
    text_rgb, _ = _color(palette["text"])
    text = tuple(t * percent + x * (1 - percent) for t, x in zip(token, text_rgb, strict=True))

    tint, alpha = _color(re.search(rf"\.model-opt-badge--{role}\{{background:(rgba\([^)]*\))", STYLE_CSS).group(1))
    surface, _ = _color(palette["surface"])  # .model-dropdown background
    accent_bg, accent_alpha = _color(palette["accent-bg"])  # .model-opt.active
    rows = {
        "plain": surface,
        "hover": _over((255, 255, 255), 0.07, surface),  # .model-opt:hover
        "active": _over(accent_bg, accent_alpha, surface),
    }
    for state, row_bg in rows.items():
        ratio = _contrast(text, _over(tint, alpha, row_bg))
        assert ratio >= 4.5, f"{role} chip on a {state} row, {skin} light skin: {ratio:.2f}:1"


def _dark_palettes():
    """Each skin's dark palette. ``:root.dark`` ties ``:root[data-skin]`` on specificity and comes
    first, so a skin's light block applies in dark mode too, under its own dark block."""
    dark_root = re.search(r"^  :root\.dark \{(.*?)\}", STYLE_CSS, re.M | re.S).group(1)
    dark = dict(LIGHT_PALETTES["default"], **_decls(dark_root))
    overrides = {"default": {}}
    for prefix in (r":root", r":root\.dark"):
        pattern = prefix + r'\[data-skin="([a-z-]+)"\]\s*\{([^}]*)\}'
        for match in re.finditer(pattern, STYLE_CSS):
            overrides.setdefault(match.group(1), {}).update(_decls(match.group(2)))
    return {name: dict(dark, **values) for name, values in overrides.items()}


PALETTES = {("light", s): p for s, p in LIGHT_PALETTES.items()} | {("dark", s): p for s, p in _dark_palettes().items()}


@pytest.mark.parametrize("mode,skin", sorted(PALETTES))
def test_configured_provider_text_meets_aa_contrast(mode, skin):
    assert len(PALETTES) >= 40 and ("dark", "nous") in PALETTES and ("dark", "zeus") in PALETTES
    palette = PALETTES[mode, skin]
    # The pill is the row's only visible provider; the last colour either of its classes declares wins.
    value = re.findall(r"\.model-opt-provider(?:--configured)?\{(?:[^}]*;)?color:([^;}]+)", STYLE_CSS)[-1]
    mix = re.fullmatch(r"color-mix\(in srgb,var\(--([a-z]+)\) (\d+)%,var\(--([a-z]+)\)\)", value)
    a, pct, b = mix.groups() if mix else (re.fullmatch(r"var\(--([a-z]+)\)", value).group(1), "100", "text")
    text = _over(_color(palette[a])[0], int(pct) / 100, _color(palette[b])[0])
    pill, pill_alpha = _color(re.search(r"\.model-opt-provider\{[^}]*;background:(rgba\([^)]*\))", STYLE_CSS).group(1))
    surface, _ = _color(palette["surface"])  # .model-dropdown background, composer and Settings alike
    rows = {"plain": surface, "active": _over(*_color(palette["accent-bg"]), surface)}
    for state in ("hover", "is-highlighted"):  # .model-opt:hover, keyboard .model-opt.is-highlighted
        tint = re.search(rf"\.model-opt[.:]{state}\{{background:(rgba\([^)]*\))", STYLE_CSS).group(1)
        rows[state] = _over(*_color(tint), surface)
    ratios = {state: round(_contrast(text, _over(pill, pill_alpha, row_bg)), 3) for state, row_bg in rows.items()}
    assert min(ratios.values()) >= 4.5, f"Configured provider text, {skin} {mode}: {ratios}"
