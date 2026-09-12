from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
I18N = ROOT / "static" / "i18n.js"


def source() -> str:
    return I18N.read_text(encoding="utf-8")


def test_set_locale_applies_direction_metadata():
    text = source()
    assert "document.documentElement.dir = _locale._dir || 'ltr';" in text
    assert "document.documentElement.dataset.locale = resolved;" in text


def test_non_rtl_locales_explicitly_restore_ltr():
    text = source()
    assert "_locale._dir || 'ltr'" in text


def test_direction_update_stays_in_set_locale():
    text = source()
    start = text.index("function setLocale(lang)")
    end = text.index("function loadLocale()", start)
    block = text[start:end]
    assert "document.documentElement.dir" in block
    assert "document.documentElement.dataset.locale" in block
