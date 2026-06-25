from collections import Counter
from pathlib import Path
import re


REPO = Path(__file__).resolve().parent.parent


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_vietnamese_locale_block_exists():
    src = read(REPO / "static" / "i18n.js")
    assert "\n    _lang: 'vi'," in src
    assert "_label: 'Tiếng Việt'" in src
    assert "_speech: 'vi-VN'" in src


def extract_locale_block(src: str, locale_key: str) -> str:
    start_match = re.search(rf"\b{re.escape(locale_key)}\s*:\s*\{{", src)
    assert start_match, f"{locale_key} locale block not found"

    brace_start = start_match.end() - 1
    depth = 0
    in_single = False
    in_double = False
    in_backtick = False
    escape = False

    for i in range(brace_start, len(src)):
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
                return src[brace_start + 1 : i]

    raise AssertionError(f"{locale_key} locale block braces are not balanced")


def test_vietnamese_locale_includes_representative_translations():
    src = read(REPO / "static" / "i18n.js")
    vi_block = extract_locale_block(src, "vi")
    expected = [
        "settings_heading_title: 'Trung tâm điều khiển'",
        "settings_heading_subtitle: 'Tùy chọn, công cụ hội thoại và điều khiển hệ thống.'",
        "approval_skip_all: '⚡ Bỏ qua tất cả trong phiên này'",
        "checkpoint_title: 'Checkpoint'",
        "composer_send: 'Gửi tin nhắn'",
        "gateway_restart: 'Khởi động lại'",
        "wiki_browse: 'Duyệt wiki'",
        "yolo_pill_title_active: 'Chế độ YOLO đang bật — bấm để tắt'",
    ]
    for entry in expected:
        assert entry in vi_block


def test_vietnamese_locale_covers_english_keys():
    src = read(REPO / "static" / "i18n.js")
    key_pattern = re.compile(r"^\s{4}([a-zA-Z0-9_]+):", re.MULTILINE)
    en_keys = set(key_pattern.findall(extract_locale_block(src, "en")))
    vi_keys = set(key_pattern.findall(extract_locale_block(src, "vi")))

    missing = sorted(en_keys - vi_keys)
    assert not missing, f"Vietnamese locale missing keys: {missing}"


def test_vietnamese_locale_has_no_duplicate_keys():
    src = read(REPO / "static" / "i18n.js")
    key_pattern = re.compile(r"^\s{4}([a-zA-Z0-9_]+):", re.MULTILINE)
    keys = key_pattern.findall(extract_locale_block(src, "vi"))
    duplicates = sorted(k for k, count in Counter(keys).items() if count > 1)
    assert not duplicates, f"Vietnamese locale has duplicate keys: {duplicates}"
