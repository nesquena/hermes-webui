from collections import Counter
from pathlib import Path
import re


REPO = Path(__file__).resolve().parent.parent


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


def test_korean_locale_block_exists():
    src = read(REPO / "static" / "i18n.js")
    assert "\n  ko: {" in src
    assert "_lang: 'ko'" in src
    assert "_label: 'Korean (한국어)'" in src
    assert "_speech: 'ko-KR'" in src


def test_korean_locale_includes_representative_translations():
    src = read(REPO / "static" / "i18n.js")
    expected = [
        "settings_title: '설정'",
        "settings_label_language: '언어'",
        "login_title: '로그인'",
        "approval_heading: '승인 필요'",
        "tab_chat: '채팅'",
        "tab_tasks: '작업'",
        "tab_profiles: 'Agent 프로필'",
        "empty_title: '무엇을 도와드릴까요?'",
        "onboarding_title: 'Hermes Web UI에 오신 것을 환영합니다'",
    ]
    for entry in expected:
        assert entry in src


def test_korean_locale_has_no_duplicate_keys():
    src = read(REPO / "static" / "i18n.js")
    key_pattern = re.compile(r"^\s{4}([a-zA-Z0-9_]+):", re.MULTILINE)
    keys = key_pattern.findall(extract_locale_block(src, "ko"))
    duplicates = sorted(k for k, count in Counter(keys).items() if count > 1)
    assert not duplicates, f"Korean locale has duplicate keys: {duplicates}"
