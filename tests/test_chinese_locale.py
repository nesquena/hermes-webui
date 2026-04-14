from collections import Counter
from pathlib import Path
import re


REPO = Path(__file__).resolve().parent.parent


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_chinese_locale_block_exists():
    src = read(REPO / "static" / "i18n.js")
    assert "\n  zh: {" in src
    assert "_lang: 'zh'" in src
    assert "_speech: 'zh-CN'" in src


def test_chinese_locale_includes_representative_translations():
    src = read(REPO / "static" / "i18n.js")
    expected = [
        "settings_title: '\\u8bbe\\u7f6e'",
        "login_title: '\\u767b\\u5f55'",
        "approval_heading: '需要审批'",
        "tab_tasks: '任务'",
        "tab_profiles: '配置'",
        "session_time_just_now: '刚刚'",
        "onboarding_title: '欢迎使用 Hermes Web UI'",
        "onboarding_complete: '引导完成'",
    ]
    for entry in expected:
        assert entry in src


def test_chinese_locale_covers_english_keys():
    src = read(REPO / "static" / "i18n.js")
    en_match = re.search(r"\n  en: \{([\s\S]*?)\n  \},\n\n  es: \{", src)
    zh_match = re.search(
        r"\n  zh: \{([\s\S]*?)\n  \},\n\n  // Traditional Chinese \(zh-Hant\)",
        src,
    )
    assert en_match, "English locale block not found"
    assert zh_match, "Chinese locale block not found"

    key_pattern = re.compile(r"^\s{4}([a-zA-Z0-9_]+):", re.MULTILINE)
    en_keys = set(key_pattern.findall(en_match.group(1)))
    zh_keys = set(key_pattern.findall(zh_match.group(1)))

    missing = sorted(en_keys - zh_keys)
    assert not missing, f"Chinese locale missing keys: {missing}"


def test_chinese_locale_has_no_duplicate_keys():
    src = read(REPO / "static" / "i18n.js")
    zh_match = re.search(
        r"\n  zh: \{([\s\S]*?)\n  \},\n\n  // Traditional Chinese \(zh-Hant\)",
        src,
    )
    assert zh_match, "Chinese locale block not found"

    key_pattern = re.compile(r"^\s{4}([a-zA-Z0-9_]+):", re.MULTILINE)
    keys = key_pattern.findall(zh_match.group(1))
    duplicates = sorted(k for k, count in Counter(keys).items() if count > 1)
    assert not duplicates, f"Chinese locale has duplicate keys: {duplicates}"
