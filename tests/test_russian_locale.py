from collections import Counter
from pathlib import Path
import re


REPO = Path(__file__).resolve().parent.parent


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_russian_locale_block_exists():
    src = read(REPO / "static" / "i18n.js")
    assert "\n  ru: {" in src
    assert "_label: 'Русский'" in src
    assert "_speech: 'ru-RU'" in src


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


def test_russian_locale_includes_representative_translations():
    src = read(REPO / "static" / "i18n.js")
    expected = [
        "settings_title: '\u041d\u0430\u0441\u0442\u0440\u043e\u0439\u043a\u0438'",
        "login_title: '\u0412\u043e\u0439\u0442\u0438'",
        "approval_heading: '\u0422\u0440\u0435\u0431\u0443\u0435\u0442\u0441\u044f \u043f\u043e\u0434\u0442\u0432\u0435\u0440\u0436\u0434\u0435\u043d\u0438\u0435'",
        "tab_tasks: '\u0417\u0430\u0434\u0430\u0447\u0438'",
        "tab_profiles: '\u041f\u0440\u043e\u0444\u0438\u043b\u0438'",
        "session_time_just_now: '\u0442\u043e\u043b\u044c\u043a\u043e \u0447\u0442\u043e'",
        "onboarding_title: '\u0414\u043e\u0431\u0440\u043e \u043f\u043e\u0436\u0430\u043b\u043e\u0432\u0430\u0442\u044c \u0432 Hermes Web UI'",
        "onboarding_complete: '\u041f\u0435\u0440\u0432\u0438\u0447\u043d\u0430\u044f \u043d\u0430\u0441\u0442\u0440\u043e\u0439\u043a\u0430 \u0437\u0430\u0432\u0435\u0440\u0448\u0435\u043d\u0430'",
    ]
    for entry in expected:
        assert entry in src


def test_russian_locale_covers_english_keys():
    src = read(REPO / "static" / "i18n.js")
    key_pattern = re.compile(r"^\s{4}([a-zA-Z0-9_]+):", re.MULTILINE)
    en_keys = set(key_pattern.findall(extract_locale_block(src, "en")))
    ru_keys = set(key_pattern.findall(extract_locale_block(src, "ru")))

    missing = sorted(en_keys - ru_keys)
    assert not missing, f"Russian locale missing keys: {missing}"


def test_russian_locale_has_no_duplicate_keys():
    src = read(REPO / "static" / "i18n.js")
    key_pattern = re.compile(r"^\s{4}([a-zA-Z0-9_]+):", re.MULTILINE)
    keys = key_pattern.findall(extract_locale_block(src, "ru"))
    duplicates = sorted(k for k, count in Counter(keys).items() if count > 1)
    assert not duplicates, f"Russian locale has duplicate keys: {duplicates}"
