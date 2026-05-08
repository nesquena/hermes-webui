"""Regression tests for the Japanese (`ja`) locale added by PR #1439.

Mirrors `test_chinese_locale.py` and `test_korean_locale.py` — confirms the
locale block exists, has the required identifier triple (`_lang/_label/_speech`),
covers the same key set as English, and contains representative translations.

Per PR #1439, `ja` is inserted between `en` and `ru` in the LOCALES object.
"""

from collections import Counter
from pathlib import Path
import re


REPO = Path(__file__).resolve().parent.parent


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def extract_locale_block(src: str, locale_key: str) -> str:
    start_match = re.search(rf"\b{re.escape(locale_key)}\s*:\s*\{{", src)
    assert start_match, f"{locale_key} locale block not found"

    start = start_match.end() - 1  # "{"
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


def test_japanese_locale_block_exists():
    src = read(REPO / "static" / "i18n.js")
    assert "\n  ja: {" in src
    assert "_lang: 'ja'" in src
    assert "_label: '日本語'" in src
    assert "_speech: 'ja-JP'" in src


def test_japanese_locale_includes_representative_translations():
    """Spot-check a handful of high-traffic UI strings to make sure they were
    actually translated (not left in English or replaced with a placeholder).
    """
    src = read(REPO / "static" / "i18n.js")
    expected = [
        "settings_title: '設定'",
        "login_title: 'サインイン'",
        "approval_heading: '承認が必要'",
        "tab_tasks: 'タスク'",
        "tab_profiles: 'プロファイル'",
        "session_time_bucket_today: '今日'",
        "onboarding_title: 'Hermes Web UI へようこそ'",
        "mcp_servers_title: 'MCPサーバー'",
        "tree_view: 'ツリー'",
    ]
    for entry in expected:
        assert entry in src, f"Missing expected translation: {entry}"


def test_japanese_locale_covers_english_keys():
    """The ja locale must define every translation key that en defines.

    JS object semantics: missing keys at runtime fall through to LOCALES.en[key]
    via the i18n.js fallback path, but parity is the contract — a missing key
    means a half-translated UI surface for ja users.
    """
    src = read(REPO / "static" / "i18n.js")
    key_pattern = re.compile(r"^\s{4}([a-zA-Z0-9_]+):", re.MULTILINE)
    en_keys = set(key_pattern.findall(extract_locale_block(src, "en")))
    ja_keys = set(key_pattern.findall(extract_locale_block(src, "ja")))

    missing = sorted(en_keys - ja_keys)
    assert not missing, f"Japanese locale missing keys: {missing}"


def test_japanese_locale_has_no_keys_outside_english():
    """ja should not invent keys that en doesn't have — those would only ever
    fire on the ja branch and silently regress every other locale.
    """
    src = read(REPO / "static" / "i18n.js")
    key_pattern = re.compile(r"^\s{4}([a-zA-Z0-9_]+):", re.MULTILINE)
    en_keys = set(key_pattern.findall(extract_locale_block(src, "en")))
    ja_keys = set(key_pattern.findall(extract_locale_block(src, "ja")))

    extra = sorted(ja_keys - en_keys)
    assert not extra, f"Japanese locale has keys not in English: {extra}"


def test_japanese_locale_duplicates_match_english():
    """JS object literal duplicates use last-wins semantics. en has 8 known
    duplicates (untitled, dialog_*, discard, clear, create, remove,
    project_name_prompt) where the second occurrence is the intended value
    for a different UI surface. ja must mirror exactly the same duplicate
    set so the JS resolution order is consistent.
    """
    src = read(REPO / "static" / "i18n.js")
    key_pattern = re.compile(r"^\s{4}([a-zA-Z0-9_]+):", re.MULTILINE)
    en_dupes = sorted(
        k for k, c in Counter(key_pattern.findall(extract_locale_block(src, "en"))).items() if c > 1
    )
    ja_dupes = sorted(
        k for k, c in Counter(key_pattern.findall(extract_locale_block(src, "ja"))).items() if c > 1
    )
    assert en_dupes == ja_dupes, (
        f"Japanese duplicates must mirror English exactly. "
        f"en_dupes={en_dupes}, ja_dupes={ja_dupes}"
    )


def test_japanese_locale_preserves_placeholder_patterns():
    """Translation values may not strip `${var}` template-literal placeholders
    or `{0}`-style positional placeholders — those are interpolated by JS at
    render time and missing them produces literal `${name}` in the UI.
    """
    src = read(REPO / "static" / "i18n.js")

    en_block = extract_locale_block(src, "en")
    ja_block = extract_locale_block(src, "ja")

    # value_re matches:  key: <whitespace> <value-up-to-comma-or-EOL>
    value_re = re.compile(
        r"^\s{4}([a-zA-Z0-9_]+):\s*(.+?)(?:,\s*$|\s*$)",
        re.MULTILINE,
    )
    placeholder_re = re.compile(r"\{[0-9]+\}|\$\{[a-zA-Z_][a-zA-Z0-9_]*\}")

    def kv(block):
        # last-wins to match JS semantics
        out = {}
        for k, v in value_re.findall(block):
            out[k] = v
        return out

    en_kv = kv(en_block)
    ja_kv = kv(ja_block)

    mismatches = []
    for k, en_v in en_kv.items():
        if k in {"_lang", "_label", "_speech"}:
            continue
        if k not in ja_kv:
            continue
        en_ph = sorted(placeholder_re.findall(en_v))
        ja_ph = sorted(placeholder_re.findall(ja_kv[k]))
        if en_ph != ja_ph:
            mismatches.append((k, en_ph, ja_ph))

    assert not mismatches, (
        f"Japanese translations must preserve every {{0}} and ${{var}} "
        f"placeholder from English. Mismatches: {mismatches[:5]}"
    )


def test_japanese_locale_arrow_function_values_mirror_english():
    """Function-valued translations (e.g. `n_messages: (n) => ...`) must remain
    function values in ja — turning one into a static string breaks the call
    site `t('n_messages')(5)` and produces `[object Function]` in the UI.
    """
    src = read(REPO / "static" / "i18n.js")
    en_block = extract_locale_block(src, "en")
    ja_block = extract_locale_block(src, "ja")

    value_re = re.compile(
        r"^\s{4}([a-zA-Z0-9_]+):\s*(.+?)(?:,\s*$|\s*$)",
        re.MULTILINE,
    )
    arrow_re = re.compile(r"^\s*\(?[a-zA-Z_,\s]*\)?\s*=>")

    def arrows(block):
        return {k for k, v in value_re.findall(block) if arrow_re.match(v)}

    en_arrows = arrows(en_block)
    ja_arrows = arrows(ja_block)

    diff = en_arrows.symmetric_difference(ja_arrows)
    assert not diff, (
        f"Japanese must mirror English arrow-function values exactly. "
        f"Mismatch (in one but not the other): {sorted(diff)}"
    )


def test_japanese_label_is_japanese_script():
    """The locale label in the language picker must actually be in Japanese
    script (kanji/hiragana/katakana), not transliterated 'Japanese'.
    """
    src = read(REPO / "static" / "i18n.js")
    # Find the ja locale's _label
    m = re.search(r"\bja\s*:\s*\{[^{}]*?_label:\s*['\"]([^'\"]+)['\"]", src, re.DOTALL)
    assert m, "ja locale _label not found"
    label = m.group(1)
    # CJK Unified Ideographs (kanji) U+4E00–U+9FFF
    # Hiragana U+3040–U+309F
    # Katakana U+30A0–U+30FF
    has_jp = bool(re.search(r"[\u3040-\u309F\u30A0-\u30FF\u4E00-\u9FFF]", label))
    assert has_jp, f"ja _label must contain Japanese script, got: {label!r}"
