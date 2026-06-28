"""Focused regression coverage for #5145 busy-input defaults."""

import re
from pathlib import Path

ROOT = Path(__file__).parent.parent
CONFIG_PY = (ROOT / "api" / "config.py").read_text(encoding="utf-8")
BOOT_JS = (ROOT / "static" / "boot.js").read_text(encoding="utf-8")
PANELS_JS = (ROOT / "static" / "panels.js").read_text(encoding="utf-8")
INDEX_HTML = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
I18N_JS = (ROOT / "static" / "i18n.js").read_text(encoding="utf-8")
LOCALE_KEYS = (
    "en",
    "it",
    "ja",
    "ru",
    "es",
    "de",
    "zh",
    "'zh-Hant'",
    "pt",
    "ko",
    "fr",
    "tr",
    "pl",
    "vi",
)


def _locale_block(locale_key):
    escaped_keys = [re.escape(key) for key in LOCALE_KEYS]
    next_keys = "|".join(key for key in escaped_keys if key != re.escape(locale_key))
    pattern = rf"^  {re.escape(locale_key)}: \{{(?P<body>.*?)(?=^  (?:{next_keys}): \{{|\n\}};)"
    match = re.search(pattern, I18N_JS, re.MULTILINE | re.DOTALL)
    assert match, f"missing {locale_key} locale block"
    return match.group("body")


def _busy_input_label(locale_key):
    block = _locale_block(locale_key)
    match = re.search(r"settings_label_busy_input_mode: '([^']+)'", block)
    assert match, f"missing busy input label for {locale_key}"
    return match.group(1)


def test_backend_default_resolves_to_steer():
    assert '"busy_input_mode": "steer"' in CONFIG_PY


def test_boot_defaults_resolve_to_steer():
    assert "window._busyInputMode=(s.busy_input_mode||'steer')" in BOOT_JS
    assert "window._busyInputMode='steer'" in BOOT_JS


def test_settings_panel_fallbacks_resolve_to_steer():
    assert "String(settings.busy_input_mode||'steer')" in PANELS_JS
    assert "['queue','interrupt','steer'].includes(val)?val:'steer'" in PANELS_JS
    assert "window._busyInputMode=body.busy_input_mode||'steer'" in PANELS_JS
    assert "const busyInputMode=($('settingsBusyInputMode')||{}).value||'steer'" in PANELS_JS


def test_busy_input_label_changes_without_key_or_id_drift():
    assert 'id="settingsBusyInputMode"' in INDEX_HTML
    assert 'data-i18n="settings_label_busy_input_mode">While agent is running' in INDEX_HTML
    assert _busy_input_label("en") == "While agent is running"
    assert I18N_JS.count("settings_label_busy_input_mode: 'While agent is running'") == 1


def test_busy_input_labels_stay_in_their_locale_blocks():
    assert _busy_input_label("it") == "Modalità input occupato"
    assert _busy_input_label("ja") == "ビジー時の入力モード"
    assert _busy_input_label("ru") == "Режим ввода при занятости"
    assert _busy_input_label("es") == "Modo de entrada ocupada"
    assert _busy_input_label("de") == "Eingabemodus bei Beschäftigung"
    assert _busy_input_label("zh") == "忙碌输入模式"
    assert _busy_input_label("'zh-Hant'") == "忙碌輸入模式"
    assert _busy_input_label("pt") == "Modo de input ocupado"
    assert _busy_input_label("ko") == "작업 중 입력 방식"
    assert _busy_input_label("fr") == "Mode de saisie occupé"
    assert _busy_input_label("tr") == "Meşgul giriş modu"
    assert _busy_input_label("pl") == "Tryb wprowadzania przy zajętości"
    assert _busy_input_label("vi") == "Chế độ nhập khi bận"
