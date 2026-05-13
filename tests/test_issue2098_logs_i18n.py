import re
from pathlib import Path


I18N_PATH = Path(__file__).resolve().parent.parent / "static" / "i18n.js"


LOGS_FILTER_KEYS = {
    "ja": {
        "logs_severity": "重大度",
        "logs_severity_all": "すべて",
        "logs_severity_errors": "エラー",
        "logs_severity_warnings": "警告+",
        "logs_filter_active": "表示中（フィルター有効）",
    },
    "ru": {
        "logs_severity": "Уровень",
        "logs_severity_all": "Все",
        "logs_severity_errors": "Ошибки",
        "logs_severity_warnings": "Предупреждения+",
        "logs_filter_active": "показано (фильтр активен)",
    },
    "es": {
        "logs_severity": "Severidad",
        "logs_severity_all": "Todo",
        "logs_severity_errors": "Errores",
        "logs_severity_warnings": "Advertencias+",
        "logs_filter_active": "mostrados (filtro activo)",
    },
    "de": {
        "logs_severity": "Schweregrad",
        "logs_severity_all": "Alle",
        "logs_severity_errors": "Fehler",
        "logs_severity_warnings": "Warnungen+",
        "logs_filter_active": "angezeigt (Filter aktiv)",
    },
    "zh": {
        "logs_severity": "严重性",
        "logs_severity_all": "全部",
        "logs_severity_errors": "错误",
        "logs_severity_warnings": "警告+",
        "logs_filter_active": "已显示（筛选器已启用）",
    },
    "zh-Hant": {
        "logs_severity": "嚴重性",
        "logs_severity_all": "全部",
        "logs_severity_errors": "錯誤",
        "logs_severity_warnings": "警告+",
        "logs_filter_active": "已顯示（篩選器已啟用）",
    },
    "pt": {
        "logs_severity": "Severidade",
        "logs_severity_all": "Todos",
        "logs_severity_errors": "Erros",
        "logs_severity_warnings": "Avisos+",
        "logs_filter_active": "exibidos (filtro ativo)",
    },
    "ko": {
        "logs_severity": "심각도",
        "logs_severity_all": "전체",
        "logs_severity_errors": "오류",
        "logs_severity_warnings": "경고+",
        "logs_filter_active": "표시됨(필터 활성)",
    },
}


def _i18n_locale_block(locale: str) -> str:
    src = I18N_PATH.read_text(encoding="utf-8")
    if "-" in locale:
        head = re.compile(rf"^  '{re.escape(locale)}':\s*\{{", re.M)
    else:
        head = re.compile(rf"^  {re.escape(locale)}:\s*\{{", re.M)
    match = head.search(src)
    assert match, f"locale {locale!r} not found"
    body_start = match.end()
    depth = 1
    i = body_start
    while i < len(src) and depth > 0:
        ch = src[i]
        if ch == "/" and i + 1 < len(src) and src[i + 1] == "/":
            newline = src.find("\n", i)
            i = len(src) if newline < 0 else newline + 1
            continue
        if ch in ("'", '"'):
            quote = ch
            i += 1
            while i < len(src) and src[i] != quote:
                i += 2 if src[i] == "\\" else 1
            i += 1
            continue
        if ch == "`":
            i += 1
            while i < len(src) and src[i] != "`":
                i += 2 if src[i] == "\\" else 1
            i += 1
            continue
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return src[body_start:i]
        i += 1
    raise AssertionError(f"locale {locale!r} block never closed")


def _string_value(block: str, key: str) -> str:
    match = re.search(rf"^\s+{re.escape(key)}:\s+'([^']*)',(?P<tail>[^\n]*)$", block, re.M)
    assert match, f"{key} missing"
    assert "TODO: translate" not in match.group("tail")
    return match.group(1)


def test_logs_severity_filter_keys_are_translated_for_non_english_locales():
    for locale, expected in LOGS_FILTER_KEYS.items():
        block = _i18n_locale_block(locale)
        for key, value in expected.items():
            assert _string_value(block, key) == value
