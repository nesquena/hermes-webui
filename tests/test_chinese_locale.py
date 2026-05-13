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


def test_chinese_locale_block_exists():
    src = read(REPO / "static" / "i18n.js")
    assert "\n  zh: {" in src
    assert "_lang: 'zh'" in src
    assert "_speech: 'zh-CN'" in src


def test_chinese_locale_includes_representative_translations():
    src = read(REPO / "static" / "i18n.js")
    # Each tuple is a list of acceptable source forms for the same translation —
    # either escape-encoded `\uXXXX` form or literal CJK characters. They produce
    # the same runtime string; do not pin source encoding.
    expected_alternatives = [
        [r"settings_title: '\u8bbe\u7f6e'", "settings_title: '设置'"],
        [r"login_title: '\u767b\u5f55'", "login_title: '登录'"],
        ["approval_heading: '需要审批'"],
        ["tab_tasks: '任务'"],
        ["tab_profiles: '配置'"],
        ["session_time_bucket_today: '今天'"],
        ["onboarding_title: '欢迎使用 Hermes Web UI'"],
        ["onboarding_complete: '引导完成'"],
    ]
    for alts in expected_alternatives:
        assert any(alt in src for alt in alts), (
            f"None of the expected forms found in i18n.js: {alts!r}"
        )


def test_chinese_locale_covers_english_keys():
    src = read(REPO / "static" / "i18n.js")
    key_pattern = re.compile(r"^\s{4}([a-zA-Z0-9_]+):", re.MULTILINE)
    en_keys = set(key_pattern.findall(extract_locale_block(src, "en")))
    zh_keys = set(key_pattern.findall(extract_locale_block(src, "zh")))

    missing = sorted(en_keys - zh_keys)
    assert not missing, f"Chinese locale missing keys: {missing}"


def test_chinese_locale_has_no_duplicate_keys():
    src = read(REPO / "static" / "i18n.js")
    key_pattern = re.compile(r"^\s{4}([a-zA-Z0-9_]+):", re.MULTILINE)
    keys = key_pattern.findall(extract_locale_block(src, "zh"))
    duplicates = sorted(k for k, count in Counter(keys).items() if count > 1)
    assert not duplicates, f"Chinese locale has duplicate keys: {duplicates}"


def test_traditional_chinese_mcp_and_tree_labels_are_not_cyrillic():
    """Regression for PR #1254/#1274 locale cross-paste fallout.

    zh-Hant inherited Russian MCP/tree-view labels such as "MCP Серверы",
    "Дерево", and "Исходный".  Those labels show up under JSON/YAML code
    block tree toggles and Settings → System → MCP Servers for zh-TW users.
    """
    src = read(REPO / "static" / "i18n.js")
    start = src.index("  'zh-Hant': {")
    end = src.index("\n  pt:", start)
    block = src[start:end]

    expected = [
        "tree_view: '樹狀'",
        "raw_view: '原始'",
        "parse_failed_note: '解析失敗'",
        "mcp_servers_title: 'MCP 伺服器'",
        "mcp_no_servers: '未設定 MCP 伺服器。'",
        "mcp_add_server: '+ 新增伺服器'",
    ]
    for entry in expected:
        assert entry in block

    assert not re.search(r"[\u0400-\u04FF]", block), "zh-Hant locale contains Cyrillic text"
