"""Inline edit of a user message keeps a visible caret and text selection.

The cascade checks resolve authored colors from the stylesheet. The Chromium
regression selects the inline editor and reads the computed ::selection color
from headless Chromium. Playwright is not installed for the project test
interpreter; the test drives the headless shell directly.
"""
import html
import json
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

CSS = (Path(__file__).resolve().parents[1] / "static" / "style.css").read_text(encoding="utf-8")
_CSS_NO_COMMENTS = re.sub(r"/\*.*?\*/", "", CSS, flags=re.S)

_AREA = '.msg-row[data-role="user"][data-editing="1"] .msg-edit-area'


def _rule(selector: str) -> str:
    idx = CSS.find(selector)
    assert idx >= 0, f"missing {selector}"
    end = CSS.find("}", idx)
    assert end > idx
    return CSS[idx:end]


def test_edit_area_caret_uses_bubble_text():
    rule = _rule(_AREA + " {")
    assert "caret-color: var(--user-bubble-text)" in rule


def test_edit_area_selection_is_split_and_inverts_the_bubble():
    for pseudo in ("::selection {", "::-moz-selection {"):
        rule = _rule(_AREA + pseudo)
        assert "background: var(--user-bubble-text)" in rule
        assert "background-color: var(--user-bubble-text)" in rule
        assert "color: var(--user-bubble-bg)" in rule


def test_dark_edit_selection_uses_page_background():
    for pseudo in ("::selection {", "::-moz-selection {"):
        rule = _rule(":root.dark " + _AREA + pseudo)
        assert "background: var(--user-bubble-text)" in rule
        assert "background-color: var(--user-bubble-text)" in rule
        assert "color: var(--bg)" in rule


_GEIST_LIGHT = (
    ':root[data-skin="geist-contrast"]:not(.dark) ' + _AREA
)


def _specificity(selector: str) -> tuple[int, int, int]:
    """Subset used by the edit-selection rules: :not(), classes, attributes, :root."""
    selector = selector.replace("::selection", " __PE__ ").replace("::-moz-selection", " __PE__ ")
    selector = re.sub(r":not\(([^)]*)\)", r" \1 ", selector)
    ids = len(re.findall(r"#[\w-]+", selector))
    attrs = len(re.findall(r"\[[^\]]+\]", selector))
    without_attr = re.sub(r"\[[^\]]+\]", " ", selector)
    classes = len(re.findall(r"\.[\w-]+", without_attr))
    pseudos = len(re.findall(r":(?!:)[\w-]+", without_attr))
    pe = selector.count("__PE__")
    tmp = re.sub(r"#[\w-]+|\.[\w-]+|::?[\w-]+", " ", without_attr)
    tmp = tmp.replace("__PE__", " ")
    elements = len(re.findall(r"[\w-]+", tmp))
    return (ids, classes + attrs + pseudos, elements + pe)


def _declarations(body: str) -> dict[str, str]:
    found = {}
    for part in body.split(";"):
        if ":" not in part:
            continue
        name, value = part.split(":", 1)
        found[name.strip().lower()] = value.strip()
    return found


def _rules():
    if not hasattr(_rules, "cached"):
        _rules.cached = [
            (match.group(1).strip(), _declarations(match.group(2)))
            for match in re.finditer(r"([^{}]+)\{([^{}]*)\}", _CSS_NO_COMMENTS, flags=re.S)
        ]
    return _rules.cached


def _root_matches(subject: str, skin: str | None, dark: bool) -> bool:
    if ":root" not in subject:
        return False
    if re.search(r":not\(\.dark\)", subject) and dark:
        return False
    without_not = re.sub(r":not\([^)]*\)", "", subject)
    if ".dark" in without_not and not dark:
        return False
    skin_match = re.search(r'data-skin="([^"]+)"', subject)
    if skin_match and skin_match.group(1) != skin:
        return False
    return True


def _custom_properties(skin: str | None, dark: bool) -> dict[str, str]:
    winners: dict[str, tuple[tuple[int, int, int], int, str]] = {}
    for order, (selector, decls) in enumerate(_rules()):
        props = {name: value for name, value in decls.items() if name.startswith("--")}
        if not props:
            continue
        matched = []
        for part in selector.split(","):
            subject = re.split(r"\s*[>+~]\s*|\s+", part.strip())[-1]
            if _root_matches(subject, skin, dark):
                matched.append(_specificity(part))
        if not matched:
            continue
        spec = max(matched)
        for name, value in props.items():
            current = winners.get(name)
            if current is None or (spec, order) >= (current[0], current[1]):
                winners[name] = (spec, order, value)
    return {name: item[2] for name, item in winners.items()}


def _resolve(value: str, props: dict[str, str], seen: tuple[str, ...] = ()) -> str:
    value = value.strip()
    match = re.fullmatch(r"var\(\s*(--[\w-]+)\s*(?:,\s*([^)]+))?\)", value)
    if not match:
        return value
    name = match.group(1)
    if name in seen or name not in props:
        fallback = match.group(2)
        if fallback is None:
            raise AssertionError(f"unresolved {value}")
        return _resolve(fallback.strip(), props, seen + (name,))
    return _resolve(props[name], props, seen + (name,))


def _parse_color(value: str) -> tuple[int, int, int]:
    value = value.strip().lower()
    match = re.fullmatch(r"#([0-9a-f]{3}|[0-9a-f]{6})", value)
    if not match:
        raise AssertionError(f"expected an opaque hex color, got {value!r}")
    hex_digits = match.group(1)
    if len(hex_digits) == 3:
        hex_digits = "".join(ch * 2 for ch in hex_digits)
    return tuple(int(hex_digits[i : i + 2], 16) for i in (0, 2, 4))


def _normalize_color(value: str) -> str:
    red, green, blue = _parse_color(value)
    return f"#{red:02x}{green:02x}{blue:02x}"


def _channel(value: int) -> float:
    srgb = value / 255
    if srgb <= 0.04045:
        return srgb / 12.92
    return ((srgb + 0.055) / 1.055) ** 2.4


def _contrast(fg: str, bg: str) -> float:
    def luminance(hex_color: str) -> float:
        red, green, blue = _parse_color(hex_color)
        return 0.2126 * _channel(red) + 0.7152 * _channel(green) + 0.0722 * _channel(blue)

    lighter = max(luminance(fg), luminance(bg))
    darker = min(luminance(fg), luminance(bg))
    return (lighter + 0.05) / (darker + 0.05)


def _selection_winner(skin: str | None, dark: bool, pseudo: str):
    winner = None
    for order, (selector, decls) in enumerate(_rules()):
        matched = []
        for part in selector.split(","):
            if ".msg-edit-area" not in part or pseudo not in part:
                continue
            if pseudo == "::selection" and "::-moz-selection" in part:
                continue
            if re.search(r":not\(\.dark\)", part) and dark:
                continue
            without_not = re.sub(r":not\([^)]*\)", "", part)
            if ".dark" in without_not and not dark:
                continue
            skin_match = re.search(r'data-skin="([^"]+)"', part)
            if skin_match and skin_match.group(1) != skin:
                continue
            matched.append(_specificity(part))
        if not matched:
            continue
        spec = max(matched)
        rank = (spec, order)
        if winner is None or rank >= winner[0]:
            winner = (rank, selector, decls)
    assert winner is not None, f"no {pseudo} rule for skin={skin!r} dark={dark}"
    return winner[1], winner[2]


def _resolved_pair(skin: str | None, dark: bool, pseudo: str) -> tuple[str, str, str]:
    selector, decls = _selection_winner(skin, dark, pseudo)
    props = _custom_properties(skin, dark)
    background = decls.get("background-color") or decls.get("background")
    assert background, selector
    assert "color" in decls, selector
    fg = _normalize_color(_resolve(decls["color"], props))
    bg = _normalize_color(_resolve(background, props))
    return selector, fg, bg


def test_geist_contrast_light_edit_selection_meets_contrast():
    """Selected text in the Geist Contrast light inline edit clears 4.5:1.

    Contrast is the WCAG sRGB ratio of the authored colors after the real
    stylesheet cascade. The known failing pair, #0070f3 on #111111, is 4.147
    and must not win. No Chromium process is started.
    """
    assert abs(_contrast("#0070f3", "#111111") - 4.147) < 0.001
    assert _contrast("#000000", "#ffffff") == 21

    for pseudo in ("::selection", "::-moz-selection"):
        selector, fg, bg = _resolved_pair("geist-contrast", False, pseudo)
        assert pseudo in selector
        assert "," not in selector
        if pseudo == "::selection":
            assert "::-moz-selection" not in selector
        assert fg == "#ffffff"
        assert bg == "#000000"
        assert {fg, bg} != {"#0070f3", "#111111"}
        assert _contrast(fg, bg) >= 4.5


def test_default_edit_selection_controls_stay_inverted():
    """Default light stays a visible inversion; default dark stays on the page background.

    Default light is gold (#b8860b) on white, 3.254:1. That pair is the
    preserved default skin, not the Geist Contrast regression.
    """
    light_selector, light_fg, light_bg = _resolved_pair(None, False, "::selection")
    assert "data-skin" not in light_selector
    assert ".dark" not in light_selector
    assert light_bg == "#ffffff"
    assert light_fg == "#b8860b"
    assert light_fg != light_bg

    dark_selector, dark_fg, dark_bg = _resolved_pair(None, True, "::selection")
    assert dark_selector.startswith(":root.dark")
    assert dark_fg == "#0d0d1a"
    assert dark_bg == "#fff8dc"
    assert _contrast(dark_fg, dark_bg) >= 4.5

    geist_dark_selector, geist_fg, geist_bg = _resolved_pair("geist-contrast", True, "::selection")
    assert geist_dark_selector.startswith(":root.dark")
    assert 'data-skin="geist-contrast"' not in geist_dark_selector
    assert _contrast(geist_fg, geist_bg) >= 4.5


def test_touch_width_uses_the_same_edit_selection():
    """390px does not retint the textarea selection. The edit area stays 14px/400.

    No @media block contains the inline-edit selection rules, so the phone
    width and the 1100px width share the resolved pairs above.
    """
    assert re.search(r"\.msg-edit-area\{[^}]*font-size:14px", CSS)
    for _selector, decls in _rules():
        if ".msg-edit-area" in _selector:
            assert "font-weight" not in decls

    start = 0
    while True:
        media_at = _CSS_NO_COMMENTS.find("@media", start)
        if media_at < 0:
            break
        brace = _CSS_NO_COMMENTS.find("{", media_at)
        depth = 0
        for index in range(brace, len(_CSS_NO_COMMENTS)):
            char = _CSS_NO_COMMENTS[index]
            if char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0:
                    body = _CSS_NO_COMMENTS[brace + 1 : index]
                    assert "msg-edit-area" not in body or "::selection" not in body
                    start = index + 1
                    break
        else:
            raise AssertionError("unbalanced @media")


_CHROME_GLOB = "chromium_headless_shell-*"
_CHROME_RELATIVE = Path("chrome-headless-shell-linux64") / "chrome-headless-shell"
_SELECTION_FIXTURE = """<!doctype html>
<html>
<head>
<meta charset="utf-8">
<link rel="stylesheet" href="__CSS_URI__">
</head>
<body>
<section id="geist-contrast-light">
  <div class="msg-row" data-role="user" data-editing="1">
    <textarea class="msg-edit-area">Geist Contrast light</textarea>
  </div>
</section>
<section id="default-light">
  <div class="msg-row" data-role="user" data-editing="1">
    <textarea class="msg-edit-area">Default light</textarea>
  </div>
</section>
<section id="default-dark">
  <div class="msg-row" data-role="user" data-editing="1">
    <textarea class="msg-edit-area">Default dark</textarea>
  </div>
</section>
<pre id="probe">UNSET</pre>
<script>
function readSelection(id, skin, dark) {
  const root = document.documentElement;
  root.classList.toggle("dark", dark);
  if (skin) root.setAttribute("data-skin", skin);
  else root.removeAttribute("data-skin");
  const area = document.getElementById(id).querySelector("textarea.msg-edit-area");
  area.focus();
  area.setSelectionRange(0, area.value.length);
  const selectedChars = area.selectionEnd - area.selectionStart;
  const pseudo = getComputedStyle(area, "::selection");
  const own = getComputedStyle(area);
  const tokens = getComputedStyle(root);
  return {
    id: id,
    innerWidth: window.innerWidth,
    selectedChars: selectedChars,
    valueLength: area.value.length,
    active: document.activeElement === area,
    tag: area.tagName,
    className: area.className,
    role: area.parentElement.getAttribute("data-role"),
    editing: area.parentElement.getAttribute("data-editing"),
    color: pseudo.color,
    backgroundColor: pseudo.backgroundColor,
    fontSize: own.fontSize,
    fontWeight: own.fontWeight,
    strong: tokens.getPropertyValue("--strong").trim(),
    bg: tokens.getPropertyValue("--bg").trim()
  };
}
const rows = [
  readSelection("geist-contrast-light", "geist-contrast", false),
  readSelection("default-light", null, false),
  readSelection("default-dark", null, true)
];
document.getElementById("probe").textContent = JSON.stringify(rows);
</script>
</body>
</html>
"""


def _chrome_shell() -> Path | None:
    env = os.environ.get("CHROME_HEADLESS_SHELL")
    if env and Path(env).is_file():
        return Path(env)
    found = shutil.which("chrome-headless-shell")
    if found:
        return Path(found)
    roots = []
    browsers = os.environ.get("PLAYWRIGHT_BROWSERS_PATH")
    if browsers:
        roots.append(Path(browsers))
    roots.append(Path.home() / ".cache" / "ms-playwright")
    for root in roots:
        matches = sorted(root.glob(_CHROME_GLOB + "/" + str(_CHROME_RELATIVE)))
        if matches:
            return matches[-1]
    return None


def _rgb_hex(value: str) -> str:
    match = re.fullmatch(r"rgb\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)\s*\)", value.strip())
    assert match, f"expected an opaque rgb() color, got {value!r}"
    channels = tuple(int(channel) for channel in match.groups())
    assert all(0 <= channel <= 255 for channel in channels)
    return "#{:02x}{:02x}{:02x}".format(*channels)


def _chromium_selection(width: int) -> dict[str, dict]:
    chrome = _chrome_shell()
    if chrome is None:
        pytest.skip("chrome-headless-shell is not installed")
    css_uri = (Path(__file__).resolve().parents[1] / "static" / "style.css").as_uri()
    with tempfile.TemporaryDirectory() as tmp:
        page = Path(tmp) / "edit-selection.html"
        page.write_text(_SELECTION_FIXTURE.replace("__CSS_URI__", css_uri), encoding="utf-8")
        proc = subprocess.run(
            [
                str(chrome),
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--headless",
                f"--window-size={width},800",
                "--dump-dom",
                page.as_uri(),
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )
    assert proc.returncode == 0, proc.stderr[-500:]
    match = re.search(r'<pre id="probe">([^<]*)</pre>', proc.stdout)
    assert match, "Chromium did not return the selection probe"
    payload = html.unescape(match.group(1)).strip()
    assert payload != "UNSET"
    rows = json.loads(payload)
    return {row["id"]: row for row in rows}


def _assert_real_edit_selection(row: dict, width: int) -> tuple[str, str]:
    assert row["innerWidth"] == width
    assert row["tag"] == "TEXTAREA"
    assert row["className"] == "msg-edit-area"
    assert row["role"] == "user"
    assert row["editing"] == "1"
    assert row["active"] is True
    assert row["valueLength"] >= 1
    assert row["selectedChars"] == row["valueLength"]
    assert row["fontSize"] == "14px"
    assert row["fontWeight"] == "400"
    return _rgb_hex(row["color"]), _rgb_hex(row["backgroundColor"])


def test_chromium_inline_edit_selection_computed_colors():
    """Chromium selects the inline editor and returns the used ::selection colors.

    Geist Contrast light must be the strong pair, white on black. Default light
    keeps gold on white, default dark keeps the page background, and a 390px
    width on the default skin does not take the Geist pair.
    """
    desktop = _chromium_selection(1100)
    touch = _chromium_selection(390)

    geist_fg, geist_bg = _assert_real_edit_selection(desktop["geist-contrast-light"], 1100)
    assert _normalize_color(desktop["geist-contrast-light"]["strong"]) == "#000000"
    assert _normalize_color(desktop["geist-contrast-light"]["bg"]) == "#ffffff"
    assert geist_bg == "#000000"
    assert geist_fg == "#ffffff"
    assert geist_bg == _normalize_color(desktop["geist-contrast-light"]["strong"])
    assert geist_fg == _normalize_color(desktop["geist-contrast-light"]["bg"])
    assert {geist_fg, geist_bg} != {"#0070f3", "#111111"}
    assert _contrast(geist_fg, geist_bg) >= 4.5

    light_fg, light_bg = _assert_real_edit_selection(desktop["default-light"], 1100)
    assert light_bg == "#ffffff"
    assert light_fg == "#b8860b"
    assert light_fg != light_bg

    dark_fg, dark_bg = _assert_real_edit_selection(desktop["default-dark"], 1100)
    assert dark_fg == "#0d0d1a"
    assert dark_bg == "#fff8dc"
    assert _contrast(dark_fg, dark_bg) >= 4.5

    touch_fg, touch_bg = _assert_real_edit_selection(touch["default-light"], 390)
    assert touch_bg == "#ffffff"
    assert touch_fg == "#b8860b"
    assert (touch_fg, touch_bg) == (light_fg, light_bg)
    assert (touch_fg, touch_bg) != ("#ffffff", "#000000")
