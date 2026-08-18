"""Persian (fa) locale + locale-aware RTL direction.

#6664: add a Persian locale (`fa`, speech `fa-IR`) and make the application
shell follow locale direction metadata so Persian and future right-to-left
locales render correctly. The locale declares `_dir: 'rtl'`; setLocale()
applies it to <html dir> and restores `ltr` for non-RTL locales.
"""
from collections import Counter
from pathlib import Path
import os
import re
from tests.test_issue2147_profile_concept_help import PROFILE_CONCEPT_KEYS


REPO = Path(__file__).resolve().parent.parent
PROFILE_CONCEPT_FALLBACK_KEYS = {
    *PROFILE_CONCEPT_KEYS,
    "workspace_artifact_source_session",
}


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def extract_locale_block(src: str, locale_key: str) -> str:
    start_match = re.search(rf"\b{re.escape(locale_key)}\s*:\s*\{{", src)
    assert start_match, f"{locale_key} locale block not found"

    start = start_match.end() - 1
    depth = 0
    in_single = False
    in_double = False
    in_backtick = False
    escape = False

    for i in range(start, len(src)):
        ch = src[i]

        if escape:
            escape = False
            continue

        if in_single:
            if ch == "\\":
                escape = True
            elif ch == "'":
                in_single = False
            continue

        if in_double:
            if ch == "\\":
                escape = True
            elif ch == '"':
                in_double = False
            continue

        if in_backtick:
            if ch == "\\":
                escape = True
            elif ch == "`":
                in_backtick = False
            continue

        if ch == "'":
            in_single = True
            continue
        if ch == '"':
            in_double = True
            continue
        if ch == "`":
            in_backtick = True
            continue

        if ch == "{":
            depth += 1
            continue
        if ch == "}":
            depth -= 1
            if depth == 0:
                return src[start + 1 : i]

    raise AssertionError(f"{locale_key} locale block braces are not balanced")


def locale_keys(src: str, locale_key: str) -> list[str]:
    key_pattern = re.compile(r"^\s*([a-zA-Z0-9_]+)\s*:", re.MULTILINE)
    return key_pattern.findall(extract_locale_block(src, locale_key))


def test_persian_locale_block_exists():
    src = read(REPO / "static" / "i18n.js")
    fa_block = extract_locale_block(src, "fa")
    assert fa_block
    assert "_lang: 'fa'" in fa_block
    assert "_label: 'فارسی'" in fa_block
    assert "_speech: 'fa-IR'" in fa_block
    assert "_dir: 'rtl'" in fa_block


def test_persian_locale_includes_representative_translations():
    src = read(REPO / "static" / "i18n.js")
    fa_block = extract_locale_block(src, "fa")
    expected = [
        "settings_title: 'تنظیمات'",
        "settings_label_language: 'زبان'",
        "login_title: 'ورود'",
        "approval_heading: 'تأیید لازم است'",
        "tab_chat: 'گفتگو'",
        "tab_tasks: 'وظایف'",
        "empty_title: 'در چه کاری می‌توانم کمک کنم؟'",
        "onboarding_title: 'به رابط کاربری وب هرمس خوش آمدید'",
    ]
    for entry in expected:
        assert entry in fa_block


def test_persian_locale_matches_english_key_coverage():
    src = read(REPO / "static" / "i18n.js")
    en_keys = set(locale_keys(src, "en"))
    fa_keys = set(locale_keys(src, "fa"))
    assert sorted((en_keys - fa_keys) - PROFILE_CONCEPT_FALLBACK_KEYS) == []
    assert sorted(fa_keys - en_keys - {"_dir"}) == []


def test_persian_locale_has_no_new_duplicate_keys():
    """fa may only duplicate keys that en itself duplicates (shared by design)."""
    src = read(REPO / "static" / "i18n.js")
    en_dups = {k for k, c in Counter(locale_keys(src, "en")).items() if c > 1}
    fa_dups = {k for k, c in Counter(locale_keys(src, "fa")).items() if c > 1}
    assert not (fa_dups - en_dups), f"Persian locale has new duplicate keys: {fa_dups - en_dups}"


def test_persian_locale_keys_use_standard_indentation():
    src = read(REPO / "static" / "i18n.js")
    fa_block = extract_locale_block(src, "fa")
    badly_indented = [
        line.strip()
        for line in fa_block.splitlines()
        if re.match(r"^\s{1,3}[a-zA-Z0-9_]+ *:", line)
    ]
    assert badly_indented == []


def test_persian_locale_has_no_double_escaped_unicode_sequences():
    """JSON-style double escapes (\\\\u2026) render literal backslash-u in the UI."""
    src = read(REPO / "static" / "i18n.js")
    fa_block = extract_locale_block(src, "fa")
    for bad in ("\\\\u2026", "\\\\u2192", "\\\\u2713"):
        assert bad not in fa_block, f"Persian locale must not contain {bad!r}"


def test_set_locale_applies_rtl_direction_for_persian():
    """setLocale('fa') must set dir=rtl on <html>; non-RTL locales restore ltr."""
    src = read(REPO / "static" / "i18n.js")
    assert "_dir === 'rtl'" in src, (
        "setLocale() must apply the locale's _dir metadata to <html dir>"
    )
    assert "document.documentElement.dir" in src, (
        "setLocale() must write document.documentElement.dir"
    )
    # Non-RTL locales must explicitly restore LTR so an RTL selection never
    # leaks into the next session or a locale switch.
    assert "'ltr'" in src


def test_rtl_shell_css_keeps_technical_surfaces_ltr():
    """RTL shell CSS must bidi-isolate code/terminal/path/numeric surfaces."""
    css = read(REPO / "static" / "style.css")
    assert 'html[dir="rtl"]' in css
    assert "unicode-bidi:isolate" in css
    assert "unicode-bidi:plaintext" in css
    # Technical surfaces stay LTR even in an RTL shell.
    rtl_block_start = css.index('html[dir="rtl"]')
    rtl_block = css[rtl_block_start:]
    assert "direction:ltr" in rtl_block
    assert ".composer-terminal-surface" in rtl_block
    assert 'input[type="number"]' in rtl_block
    # The sidebar border flips so the right-side rail keeps its divider.
    assert "border-left:1px solid var(--border)" in rtl_block


def test_rtl_shell_css_mirrors_manual_tool_surface_rules():
    """html[dir=rtl] must mirror the .chat-content-rtl plain-text tool rules.

    Review (CHANGES_REQUESTED): setLocale() applies html dir=rtl without the
    manual .chat-content-rtl class, so the plain-text tool surfaces that the
    manual rules force LTR (.tool-call-group-body, .tool-result + descendants)
    must be covered by the automatic shell rules too — otherwise Persian with
    the manual RTL toggle off lets commands, paths, and JSON inherit RTL.
    """
    css = read(REPO / "static" / "style.css")
    rtl_block_start = css.index('html[dir="rtl"]')
    rtl_block = css[rtl_block_start:]
    # Every selector the manual .chat-content-rtl tool rule uses must exist
    # in the automatic html[dir=rtl] block with the same LTR declarations.
    for sel in (
        'html[dir="rtl"] .tool-call-group-body,',
        'html[dir="rtl"] .tool-call-group-body *,',
        'html[dir="rtl"] .tool-result,',
        'html[dir="rtl"] .tool-result *,',
    ):
        assert sel in rtl_block, f"missing mirror selector {sel}"
    # The shared declaration block carries the LTR isolation contract.
    tool_rule_start = rtl_block.index('html[dir="rtl"] .tool-call-group-body')
    tool_rule_end = rtl_block.index("}", tool_rule_start)
    tool_rule = rtl_block[tool_rule_start:tool_rule_end]
    assert "direction:ltr" in tool_rule
    assert "text-align:left" in tool_rule
    assert "unicode-bidi:isolate" in tool_rule
    # Plain-text path/ID surfaces from the #6664 audit stay LTR as well:
    # command palette file paths and model IDs are technical identifiers.
    assert 'html[dir="rtl"] .cmd-item-path-value,' in rtl_block
    assert 'html[dir="rtl"] .model-opt-id,' in rtl_block


def test_rtl_four_case_behavior_matrix():
    """Four-case behavior matrix for locale-driven vs manual RTL.

    Case 1 — fa / manual-off: setLocale('fa') sets html dir=rtl; the
      automatic shell rules (not .chat-content-rtl) must isolate tool
      surfaces, since the manual class is absent.
    Case 2 — fa / manual-on: html dir=rtl AND .chat-content-rtl are both
      active; tool surfaces stay LTR under either rule set.
    Case 3 — LTR / manual-on: html dir=ltr with the manual class; the legacy
      .chat-content-rtl rules still isolate tool surfaces.
    Case 4 — fa -> LTR restoration: switching fa -> en restores dir=ltr so an
      RTL selection never leaks into the next locale or session.
    """
    css = read(REPO / "static" / "style.css")
    src = read(REPO / "static" / "i18n.js")

    # Case 1 + 2: automatic html[dir=rtl] rules must isolate tool surfaces
    # even when the manual class is not applied.
    rtl_block = css[css.index('html[dir="rtl"]'):]
    assert 'html[dir="rtl"] .tool-call-group-body,' in rtl_block
    assert 'html[dir="rtl"] .tool-result,' in rtl_block
    assert 'html[dir="rtl"] .tool-call-group-body *,' in rtl_block
    assert 'html[dir="rtl"] .tool-result *,' in rtl_block

    # Case 2 + 3: the manual .chat-content-rtl rules must remain intact.
    assert ".chat-content-rtl .tool-call-group-body," in css
    assert ".chat-content-rtl .tool-call-group-body *," in css
    assert ".chat-content-rtl .tool-result," in css
    assert ".chat-content-rtl .tool-result *{" in css
    tool_manual = css[css.index(".chat-content-rtl .tool-call-group-body,"):]
    tool_manual = tool_manual[: tool_manual.index("}")]
    assert "direction:ltr" in tool_manual
    assert "text-align:left" in tool_manual
    assert "unicode-bidi:isolate" in tool_manual

    # Case 4: setLocale() must restore dir=ltr for non-RTL locales.
    assert "document.documentElement.dir = _locale._dir === 'rtl' ? 'rtl' : 'ltr'" in src


def test_set_locale_rtl_to_ltr_restoration_behavior():
    """Behavioral (node + DOM shims): fa sets dir=rtl; switching to en restores ltr.

    Drives the REAL setLocale() from static/i18n.js (case 4 of the behavior
    matrix: fa -> LTR restoration) and asserts the data-locale stamp.
    """
    import shutil
    import subprocess
    import tempfile

    node = shutil.which("node")
    if node is None:
        import pytest

        pytest.skip("node not on PATH")
    js = r"""
const docEl = { lang:'', dir:'', attrs:{}, setAttribute(k,v){this.attrs[k]=v;},
  getAttribute(k){return this.attrs[k]??null;} };
global.document = { documentElement: docEl, querySelectorAll: () => [] };
global.localStorage = { getItem: () => null, setItem: () => {}, removeItem: () => {} };
global.window = global;
const fs = require('fs');
eval(fs.readFileSync(process.argv[2], 'utf8'));
setLocale('fa');
const faDir = docEl.dir; const faLoc = docEl.attrs['data-locale'];
setLocale('en');
const enDir = docEl.dir; const enLoc = docEl.attrs['data-locale'];
setLocale('fa'); setLocale('en');  // restoration path after an RTL session
const restoredDir = docEl.dir;
process.stdout.write(JSON.stringify({faDir, faLoc, enDir, enLoc, restoredDir}));
"""
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".js", delete=False, encoding="utf-8"
    ) as tf:
        tf.write(js)
        script = tf.name
    try:
        result = subprocess.run(
            [node, script, str(REPO / "static" / "i18n.js")],
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert result.returncode == 0, f"node error: {result.stderr}"
        import json

        out = json.loads(result.stdout)
        assert out["faDir"] == "rtl", out
        assert out["faLoc"] == "fa", out
        assert out["enDir"] == "ltr", out
        assert out["enLoc"] == "en", out
        assert out["restoredDir"] == "ltr", out
    finally:
        os.unlink(script)
