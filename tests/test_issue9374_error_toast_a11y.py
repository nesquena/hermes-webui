import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
UI_JS = (ROOT / "static" / "ui.js").read_text(encoding="utf-8")
STYLE_CSS = (ROOT / "static" / "style.css").read_text(encoding="utf-8")
I18N_JS = (ROOT / "static" / "i18n.js").read_text(encoding="utf-8")

# Every locale bundle currently shipped in static/i18n.js.
LOCALES = [
    "en", "it", "ja", "ru", "es", "de", "zh", "zh-Hant",
    "pt", "ko", "fr", "cs", "tr", "pl", "vi",
]

TOAST_KEYS = ("toast_copy", "toast_dismiss", "toast_dismiss_label")

ENGLISH_DEFAULTS = {
    "toast_copy": "Copy",
    "toast_dismiss": "Dismiss",
    "toast_dismiss_label": "Dismiss error toast",
}


def _show_toast_block() -> str:
    start = UI_JS.index("function showToast")
    end = UI_JS.index("// ── Shared app dialogs", start)
    return UI_JS[start:end]


def _locale_block(locale: str) -> str:
    head = re.compile(rf"^  (?:'{re.escape(locale)}'|{re.escape(locale)}):\s*\{{", re.M)
    match = head.search(I18N_JS)
    assert match, f"locale {locale!r} not found"
    body_start = match.end()
    depth = 1
    i = body_start
    while i < len(I18N_JS) and depth > 0:
        ch = I18N_JS[i]
        if ch == "/" and i + 1 < len(I18N_JS) and I18N_JS[i + 1] == "/":
            newline = I18N_JS.find("\n", i)
            i = len(I18N_JS) if newline < 0 else newline + 1
            continue
        if ch in ("'", '"'):
            quote = ch
            i += 1
            while i < len(I18N_JS) and I18N_JS[i] != quote:
                i += 2 if I18N_JS[i] == "\\" else 1
            i += 1
            continue
        if ch == "`":
            i += 1
            while i < len(I18N_JS) and I18N_JS[i] != "`":
                i += 2 if I18N_JS[i] == "\\" else 1
            i += 1
            continue
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return I18N_JS[body_start:i]
        i += 1
    raise AssertionError(f"locale {locale!r} block never closed")


def _pointer_coarse_blocks() -> list[str]:
    blocks = []
    idx = 0
    while True:
        needle_at = STYLE_CSS.find("@media (pointer:coarse)", idx)
        if needle_at < 0:
            return blocks
        brace = STYLE_CSS.index("{", needle_at)
        depth = 0
        j = brace
        while j < len(STYLE_CSS):
            if STYLE_CSS[j] == "{":
                depth += 1
            elif STYLE_CSS[j] == "}":
                depth -= 1
                if depth == 0:
                    blocks.append(STYLE_CSS[brace + 1:j])
                    break
            j += 1
        idx = j + 1


def _toast_touch_rule() -> str:
    blocks = [b for b in _pointer_coarse_blocks() if ".toast-copy" in b and ".toast-dismiss" in b]
    assert blocks, (
        "Error toast buttons need a 44x44px touch target inside an "
        "@media (pointer:coarse) block so phones get the larger hit area "
        "without changing the desktop toast (#9374)."
    )
    return blocks[0]


def test_error_toast_button_labels_use_locale_keys():
    show_toast = _show_toast_block()

    assert "t('toast_copy')" in show_toast, (
        "The Copy control must read its label from the locale bundle, not a "
        "hard-coded English literal (#9374)."
    )
    assert "t('toast_dismiss')" in show_toast, (
        "The Dismiss control must read its label from the locale bundle, not a "
        "hard-coded English literal (#9374)."
    )
    assert "t('toast_dismiss_label')" in show_toast, (
        "The Dismiss control's aria-label must be localized as well (#9374)."
    )

    assert ">Copy</button>" not in show_toast
    assert ">Dismiss</button>" not in show_toast
    assert 'aria-label="Dismiss error toast"' not in show_toast


def test_copy_toast_confirmation_uses_locale_key():
    start = UI_JS.index("function copyToastText")
    end = UI_JS.index("function showToast", start)
    copy_toast = UI_JS[start:end]

    assert "t('copied')" in copy_toast, (
        "The transient 'Copied' confirmation on the toast button is visible UI "
        "text and must follow the active locale (#9374)."
    )
    assert "'Copied'" not in copy_toast


def test_all_locales_define_the_toast_button_keys():
    for locale in LOCALES:
        block = _locale_block(locale)
        for key in TOAST_KEYS:
            match = re.search(rf"^\s+{key}:\s+'([^']*)'", block, re.M)
            assert match, f"{locale} is missing the {key} key (#9374)"
            value = match.group(1).strip()
            assert value, f"{locale} has an empty {key} value (#9374)"
            assert "TODO" not in value, f"{locale} leaves {key} untranslated (#9374)"
            if locale != "en":
                assert value != ENGLISH_DEFAULTS[key], (
                    f"{locale} still falls back to the English default for {key} (#9374)"
                )


def test_toast_buttons_meet_44px_touch_target_on_coarse_pointers():
    rule = _toast_touch_rule()

    assert ".toast-copy,.toast-dismiss" in rule
    assert "min-height:44px" in rule, (
        "Phone toasts must expose at least a 44px tall hit area for Copy and "
        "Dismiss (#9374)."
    )
    assert "min-width:44px" in rule, (
        "Phone toasts must expose at least a 44px wide hit area for Copy and "
        "Dismiss (#9374)."
    )


def test_mobile_long_error_messages_get_a_full_width_row():
    rule = _toast_touch_rule()

    assert ".toast{flex-wrap:wrap;}" in rule, (
        "On coarse pointers the error toast should wrap its controls onto their "
        "own row so a long localized message is not squeezed into a narrow "
        "column between the buttons (#9374)."
    )
    assert ".toast-message{flex:1 1 100%;}" in rule, (
        "The toast message should occupy the full first row on coarse pointers "
        "(#9374)."
    )


def test_desktop_toast_button_padding_is_unchanged():
    match = re.search(r"\.toast-copy,\.toast-dismiss\{([^}]*)\}", STYLE_CSS)
    assert match, "Base .toast-copy,.toast-dismiss rule missing from style.css"
    base_rule = match.group(1)
    assert "padding:4px 8px" in base_rule, (
        "The desktop toast buttons must keep their compact padding; the 44px "
        "target belongs only to the touch layout (#9374)."
    )
    assert "font-size:12px" in base_rule
