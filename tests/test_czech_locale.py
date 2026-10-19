from collections import Counter
from pathlib import Path
import json
import re
import shutil
import subprocess
import tempfile

import pytest

from tests.test_issue2147_profile_concept_help import PROFILE_CONCEPT_KEYS


REPO = Path(__file__).resolve().parent.parent
I18N_JS = REPO / "static" / "i18n.js"
NODE = shutil.which("node")
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


def test_czech_locale_block_exists():
    src = read(REPO / "static" / "i18n.js")
    cs_block = extract_locale_block(src, "cs")
    assert cs_block
    assert "_lang: 'cs'" in cs_block
    assert "_label: 'Čeština'" in cs_block
    assert "_speech: 'cs-CZ'" in cs_block


def test_czech_locale_includes_representative_translations():
    src = read(REPO / "static" / "i18n.js")
    cs_block = extract_locale_block(src, "cs")
    expected = [
        "settings_title: 'Nastavení'",
        "settings_label_language: 'Jazyk'",
        "login_title: 'Přihlášení'",
        "approval_heading: 'Požadováno schválení'",
        "tab_tasks: 'Úkoly'",
        "tab_profiles: 'Profily'",
        "empty_title: 'Jak vám mohu pomoci?'",
        "onboarding_title: 'Vítejte v Hermes Web UI'",
    ]
    for entry in expected:
        assert entry in cs_block, f"missing expected Czech translation: {entry}"


def test_czech_locale_matches_english_key_coverage():
    src = read(REPO / "static" / "i18n.js")
    en_keys = set(locale_keys(src, "en"))
    cs_keys = set(locale_keys(src, "cs"))
    assert sorted((en_keys - cs_keys) - PROFILE_CONCEPT_FALLBACK_KEYS) == []
    assert sorted(cs_keys - en_keys) == []


def test_czech_locale_has_no_duplicate_keys():
    src = read(REPO / "static" / "i18n.js")
    keys = locale_keys(src, "cs")

    duplicates = sorted(k for k, count in Counter(keys).items() if count > 1)
    assert not duplicates, f"Czech locale has duplicate keys: {duplicates}"


def test_czech_locale_keys_use_standard_indentation():
    src = read(REPO / "static" / "i18n.js")
    cs_block = extract_locale_block(src, "cs")

    # Enforce strict 4-space indentation for keys.
    badly_indented = []
    for line in cs_block.splitlines():
        m = re.match(r"^(\s*)[a-zA-Z0-9_]+\s*:", line)
        if m and len(m.group(1)) != 4:
            badly_indented.append(f"{len(m.group(1))} spaces: {line.strip()}")
    assert badly_indented == []


def test_czech_locale_arrow_function_values_mirror_english():
    src = read(REPO / "static" / "i18n.js")
    en_block = extract_locale_block(src, "en")
    cs_block = extract_locale_block(src, "cs")

    value_re = re.compile(r"^\s+([a-zA-Z0-9_]+):\s*(.+?)(?:,\s*$|\s*$)", re.MULTILINE)
    arrow_re = re.compile(r"^\s*\(?[a-zA-Z_,\s]*\)?\s*=>")
    helper_re = re.compile(r"^_i18n[A-Za-z]+$")

    def callable_values(block):
        out = set()
        for k, v in value_re.findall(block):
            v = v.strip()
            if arrow_re.match(v) or helper_re.match(v):
                out.add(k)
        return out

    # Every key whose English value is a function must also be a function in cs
    # (either an arrow or a named _i18n* helper reference).
    assert callable_values(en_block) == callable_values(cs_block)


def test_czech_locale_preserves_placeholder_patterns():
    src = read(REPO / "static" / "i18n.js")
    en_block = extract_locale_block(src, "en")
    cs_block = extract_locale_block(src, "cs")

    value_re = re.compile(r"^\s+([a-zA-Z0-9_]+):\s*(.+?)(?:,\s*$|\s*$)", re.MULTILINE)
    placeholder_re = re.compile(r"\{[0-9]+\}|\$\{[a-zA-Z_][a-zA-Z0-9_]*\}")

    def kv(block):
        out = {}
        for k, v in value_re.findall(block):
            out[k] = v
        return out

    en_kv = kv(en_block)
    cs_kv = kv(cs_block)

    for key, en_val in en_kv.items():
        if key not in cs_kv:
            continue
        en_vars = sorted(placeholder_re.findall(en_val))
        cs_vars = sorted(placeholder_re.findall(cs_kv[key]))
        # Skip arrow functions which might contain conditional logic and thus more template vars.
        if "=>" not in cs_kv[key]:
            if en_vars or cs_vars:
                assert cs_vars == en_vars, f"Key '{key}' placeholder mismatch in cs locale"


def test_czech_locale_has_no_double_escaped_unicode_sequences():
    """JSON-style double escapes (\\\\u2026) render literal backslash-u in the UI."""
    src = read(REPO / "static" / "i18n.js")
    cs_block = extract_locale_block(src, "cs")
    for bad in ("\\\\u2026", "\\\\u2192", "\\\\u2713"):
        assert bad not in cs_block, f"Czech locale must not contain {bad!r}"


def test_czech_locale_uses_real_utf8_diacritics():
    """Czech uses á č ď é ě í ň ó ř š ť ú ů ý ž — confirm the block carries real
    UTF-8 diacritics, not ASCII-only text (which would mean nothing was translated)."""
    src = read(REPO / "static" / "i18n.js")
    cs_block = extract_locale_block(src, "cs")
    diacritics = "áčďéěíňóřšťúůýžÁČĎÉĚÍŇÓŘŠŤÚŮÝŽ"
    assert any(ch in cs_block for ch in diacritics), "Czech locale has no diacritics"


# ── Runtime (node) coverage ────────────────────────────────────────────────
# Source-text assertions cannot tell a JS escape (`\n`, one backslash) from a
# double-escaped value (`\\n`, which renders as literal backslash-n text).  The
# reviewed base was green while `showConfirmDialog` displayed the escape text,
# so these tests drive the real `setLocale()` / `t()` and assert on the string
# the UI would actually render.

def _i18n_runtime(js_body: str) -> dict:
    """Run `js_body` in a fresh node process after `static/i18n.js` is loaded."""
    src = read(I18N_JS)
    # Drop the boot-time loadLocale() call so the driver picks the locale.
    src = re.sub(r"\nloadLocale\(\);\s*$", "", src, count=1)
    driver = f"""
const src = {src!r};
global.localStorage = {{ getItem: () => null, setItem: () => {{}} }};
global.document = {{
  documentElement: {{ set lang(_) {{}} }},
  querySelectorAll: () => [],
  querySelector: () => null,
  addEventListener: () => {{}},
}};
eval(src);
{js_body}
"""
    with tempfile.NamedTemporaryFile(
        "w", suffix=".js", delete=False, encoding="utf-8"
    ) as f:
        f.write(driver)
        path = f.name
    try:
        result = subprocess.run(
            [NODE, path], capture_output=True, text=True, timeout=120
        )
    finally:
        Path(path).unlink(missing_ok=True)
    assert result.returncode == 0, (
        f"node driver failed: rc={result.returncode} stderr={result.stderr!r}"
    )
    return json.loads(result.stdout)


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_czech_share_confirm_renders_real_paragraph_break():
    """cs.share_session_existing_confirm must reach the dialog as a real
    paragraph break, not as literal `\\n` escape text.

    `showConfirmDialog` assigns the value to `textContent`, so the escape text
    is exactly what a Czech user saw: one run-on line with backslash-n in the
    middle.
    """
    values = _i18n_runtime(
        """
setLocale('cs');
const cs = t('share_session_existing_confirm');
setLocale('en');
const en = t('share_session_existing_confirm');
process.stdout.write(JSON.stringify({ cs, en }));
"""
    )

    cs = values["cs"]
    en = values["en"]
    assert "\\n" not in cs, f"Czech value leaks a literal escape sequence: {cs!r}"
    assert "\\" not in cs, f"Czech value contains a literal backslash: {cs!r}"
    assert cs.count("\n") == 2, f"expected two newlines, got {cs.count(chr(10))}"
    paragraphs = cs.split("\n\n")
    assert len(paragraphs) == 2 and all(p.strip() for p in paragraphs)
    # Same paragraph shape as English, so the dialog keeps its two paragraphs.
    assert cs.count("\n") == en.count("\n")


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_no_czech_value_leaks_escape_sequences_through_t():
    """Any cs value whose source accidentally double-escapes a newline (or a
    unicode escape) renders the backslash to the user.  `t()` is the only
    seam the UI uses, so scan every translated value through it.
    """
    src = read(I18N_JS)
    keys = sorted(locale_keys(src, "cs"))
    values = _i18n_runtime(
        f"""
setLocale('cs');
const keys = {json.dumps(keys)};
const out = {{}};
for (const k of keys) {{
  try {{
    const v = t(k);
    if (typeof v === 'string') out[k] = v;
  }} catch (_) {{}}
}}
process.stdout.write(JSON.stringify(out));
"""
    )

    leaked = sorted(
        key
        for key, value in values.items()
        if "\\n" in value or "\\t" in value or "\\u" in value
    )
    assert leaked == [], f"Czech values render escape sequences: {leaked}"
