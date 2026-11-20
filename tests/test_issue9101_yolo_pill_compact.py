"""#9101 design review: the compact ⚡ YOLO pill.

While YOLO is on the pill can't be hidden (it is the only sign that approvals
are skipped), so Settings -> "Situational footer controls" -> YOLO off now
shrinks it to its ⚡ icon (`yolo-pill-compact`, toggled by `_updateYoloPill`;
the behaviour is harness scenario r5c in
test_issue9101_yolo_pill_session_authority.py). The static contracts:

- the compact pill hides its label; so does every pill at the narrowest
  footer fit stage (`.composer-footer.cf-burger`, phones); the icon stage
  (`.cf-icons`, desktop) keeps it;
- the ⚡-only pill gets a 44px-tall hit area from an inset pseudo-element,
  never a wider box (a wider box pushes the mobile-config button off screen);
- the label stays in the DOM (tooltip and visible text intact) and the pill's
  accessible name comes from an explicit aria-label;
- the situational-controls description says so in every locale.
"""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "static"
KEY = "settings_desc_composer_situational_controls"
EN_SENTENCE = "YOLO can't be hidden while it's on; turning its chip off here shrinks it to ⚡."
LOCALES = {"en", "it", "ja", "ru", "es", "de", "zh", "zh-Hant", "pt", "ko", "fr", "cs", "tr", "pl", "vi"}


def _read(name):
    return (STATIC / name).read_text(encoding="utf-8")


def _css_rules():
    """(selectors, declarations, depth) for every innermost CSS rule; depth 0
    is top level (outside any @media/@container/@supports block)."""
    css = re.sub(r"/\*.*?\*/", "", _read("style.css"), flags=re.S)
    rules = []
    for match in re.finditer(r"([^{}]+)\{([^{}]*)\}", css):
        before = css[:match.start(1)]
        depth = before.count("{") - before.count("}")
        selectors = [" ".join(s.split()) for s in match.group(1).split(",")]
        decls = {}
        for decl in match.group(2).split(";"):
            if ":" in decl:
                name, value = decl.split(":", 1)
                decls[name.strip().lower()] = " ".join(value.split()).lower()
        rules.append((selectors, decls, depth))
    return rules


def _rules_for(selector):
    return [(decls, depth) for selectors, decls, depth in _css_rules() if selector in selectors]


def _hides(decls):
    return decls.get("display", "").startswith("none")


def test_compact_pill_hides_its_label_at_every_width():
    found = _rules_for(".yolo-pill.yolo-pill-compact .yolo-pill-label")
    assert any(_hides(d) and depth == 0 for d, depth in found), found


def test_burger_stage_hides_the_pill_label_for_everyone():
    found = _rules_for(".composer-footer.cf-burger .yolo-pill-label")
    assert any(_hides(d) and depth == 0 for d, depth in found), found


def test_only_the_compact_pill_and_the_burger_stage_hide_the_label():
    """`.cf-icons` (desktop has room there) and plain widths keep the label."""
    for selectors, decls, _depth in _css_rules():
        for sel in selectors:
            if sel.endswith(".yolo-pill-label") and _hides(decls):
                assert "yolo-pill-compact" in sel or ".cf-burger" in sel, sel


def _hit_area(owner):
    found = _rules_for(owner + "::after")
    assert found, f"no {owner}::after hit area"
    for decls, depth in found:
        assert depth == 0
        assert decls.get("content") in ("''", '""'), decls
        assert decls.get("position") == "absolute", decls
        assert decls.get("height") == "44px", decls
        # Exactly the pill's width: an inset box, never a wider one.
        assert decls.get("left") == "0" and decls.get("right") == "0", decls
        assert "width" not in decls and "min-width" not in decls, decls
    owner_rules = _rules_for(owner)
    assert any(d.get("position") == "relative" for d, _ in owner_rules), owner_rules
    for decls, _depth in owner_rules:
        assert "min-width" not in decls and "width" not in decls, decls


def test_compact_pill_has_a_44px_tall_inset_hit_area():
    _hit_area(".yolo-pill.yolo-pill-compact")


def test_burger_stage_pill_has_the_same_hit_area():
    _hit_area(".composer-footer.cf-burger .yolo-pill")


def test_label_stays_in_the_dom():
    pill = re.search(r'<button[^>]*id="yoloPill"[^>]*>(.*?)</button>', _read("index.html"), re.S)
    assert pill, "yoloPill missing from index.html"
    assert '<span class="yolo-pill-label" data-i18n="yolo_pill_label">YOLO</span>' in pill.group(1)
    src = _read("messages.js")
    start = src.index("function _updateYoloPill()")
    body = src[start:src.index("\n}\n", start)]
    for forbidden in ("innerHTML", "textContent", "innerText", ".remove()", "removeChild"):
        assert forbidden not in body, forbidden


def _locale_values():
    src = _read("i18n.js")
    langs = [(m.start(), m.group(1)) for m in re.finditer(r"_lang:\s*'([\w-]+)'", src)]
    values = {}
    for match in re.finditer(KEY + r":\s*'((?:[^'\\]|\\.)*)'", src):
        lang = [name for pos, name in langs if pos < match.start()][-1]
        assert lang not in values, f"{KEY} twice in {lang}"
        values[lang] = re.sub(r"\\(.)", r"\1", match.group(1))
    return values


def test_settings_copy_explains_the_compact_pill_in_every_locale():
    values = _locale_values()
    assert set(values) == LOCALES, sorted(values)
    assert values["en"].endswith(" " + EN_SENTENCE), values["en"]
    for lang, text in values.items():
        assert text.count("⚡") == 1 and "YOLO" in text, (lang, text)
        if lang != "en":
            # The new sentence is translated, not the English one.
            assert EN_SENTENCE not in text and "shrinks it" not in text, (lang, text)
    # The pre-i18n fallback text in index.html is the English copy.
    match = re.search(r'data-i18n="' + KEY + r'">([^<]*)<', _read("index.html"))
    assert match and match.group(1) == values["en"], match and match.group(1)
