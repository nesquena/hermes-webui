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


def _default_message_mode_label(locale_key):
    block = _locale_block(locale_key)
    match = re.search(r"settings_label_default_message_mode: '([^']+)'", block)
    assert match, f"missing default message mode label for {locale_key}"
    return match.group(1)

def _default_message_mode_steer_option(locale_key):
    block = _locale_block(locale_key)
    match = re.search(r"settings_default_message_mode_steer: '([^']+)'", block)
    assert match, f"missing default message mode steer option for {locale_key}"
    return match.group(1)

def _default_message_mode_description(locale_key):
    block = _locale_block(locale_key)
    match = re.search(r"settings_desc_default_message_mode: ([\"'])((?:\\.|(?!\1).)*)\1", block)
    assert match, f"missing default message mode description for {locale_key}"
    return match.group(2)


def test_backend_default_resolves_to_steer():
    assert '"default_message_mode": "steer"' in CONFIG_PY


def test_boot_defaults_resolve_to_steer():
    assert "window._defaultMessageMode=(s.default_message_mode||s.busy_input_mode||'steer')" in BOOT_JS
    assert "window._defaultMessageMode='steer'" in BOOT_JS


def test_settings_panel_fallbacks_resolve_to_steer():
    assert "String(settings.default_message_mode||settings.busy_input_mode||'steer')" in PANELS_JS
    assert "['queue','interrupt','steer'].includes(val)?val:'steer'" in PANELS_JS
    assert "window._defaultMessageMode=body.default_message_mode||body.busy_input_mode||'steer'" in PANELS_JS
    assert "const defaultMessageMode=($('settingsDefaultMessageMode')||{}).value||'steer'" in PANELS_JS


def test_busy_input_label_changes_without_key_or_id_drift():
    assert 'id="settingsDefaultMessageMode"' in INDEX_HTML
    assert 'data-i18n="settings_label_default_message_mode">Default message mode' in INDEX_HTML
    assert _default_message_mode_label("en") == "Default message mode"
    assert I18N_JS.count("settings_label_default_message_mode: 'Default message mode'") == 1


def test_busy_input_labels_stay_in_their_locale_blocks():
    assert _default_message_mode_label("it") == "Modalità messaggio predefinita"
    assert _default_message_mode_label("ja") == "既定のメッセージモード"
    assert _default_message_mode_label("ru") == "Режим сообщений по умолчанию"
    assert _default_message_mode_label("es") == "Modo de mensaje predeterminado"
    assert _default_message_mode_label("de") == "Standard-Nachrichtenmodus"
    assert _default_message_mode_label("zh") == "默认消息模式"
    assert _default_message_mode_label("'zh-Hant'") == "預設訊息模式"
    assert _default_message_mode_label("pt") == "Modo de mensagem padrão"
    assert _default_message_mode_label("ko") == "기본 메시지 모드"
    assert _default_message_mode_label("fr") == "Mode de message par défaut"
    assert _default_message_mode_label("tr") == "Varsayılan mesaj modu"
    assert _default_message_mode_label("pl") == "Domyślny tryb wiadomości"
    assert _default_message_mode_label("vi") == "Chế độ tin nhắn mặc định"

def test_russian_steer_option_keeps_mid_turn_meaning():
    assert _default_message_mode_steer_option("ru") == "Steer (коррекция в середине хода)"

def test_localized_descriptions_keep_draft_restore_meaning():
    expected_restore_phrases = {
        "it": "bozza viene ripristinata",
        "ja": "下書きが復元",
        "ru": "черновик восстанавливается",
        "es": "borrador se restaura",
        "de": "Entwurf wiederhergestellt",
        "zh": "恢复草稿",
        "'zh-Hant'": "恢復草稿",
        "pt": "rascunho é restaurado",
        "ko": "초안이 복원",
        "fr": "brouillon est restauré",
        "tr": "taslak geri yüklenir",
        "pl": "szkic zostaje przywrócony",
        "vi": "bản nháp sẽ được khôi phục",
    }
    for locale_key, restore_phrase in expected_restore_phrases.items():
        assert restore_phrase in _default_message_mode_description(locale_key), (
            f"{locale_key} description must describe restoring the draft when Steer is unavailable"
        )
