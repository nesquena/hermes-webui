from collections import Counter
from pathlib import Path
import re
from tests.test_issue2147_profile_concept_help import PROFILE_CONCEPT_KEYS


REPO = Path(__file__).resolve().parent.parent
PROFILE_CONCEPT_FALLBACK_KEYS = set(PROFILE_CONCEPT_KEYS)


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


def locale_keys(src: str, locale_key: str) -> list[str]:
    key_pattern = re.compile(r"^\s*([a-zA-Z0-9_]+)\s*:", re.MULTILINE)
    return key_pattern.findall(extract_locale_block(src, locale_key))


def test_turkish_locale_block_exists():
    src = read(REPO / "static" / "i18n.js")
    tr_block = extract_locale_block(src, "tr")
    assert tr_block
    assert "_lang: 'tr'" in tr_block
    assert "_label: 'Türkçe'" in tr_block
    assert "_speech: 'tr-TR'" in tr_block


def test_turkish_locale_includes_representative_translations():
    src = read(REPO / "static" / "i18n.js")
    tr_block = extract_locale_block(src, "tr")
    expected = [
        "settings_title: 'Ayarlar'",
        "settings_label_language: 'Dil'",
        "login_title: 'Oturum aç'",
        "approval_heading: 'Onay gerekli'",
        "tab_chat: 'Sohbet'",
        "tab_tasks: 'Görevler'",
        "tab_profiles: 'Agent profilleri'",
        "empty_title: 'Hangi konuda yardımcı olabilirim?'",
        "onboarding_title: 'Hermes Web Kullanıcı Arayüzüne Hoş Geldiniz'",
    ]
    for entry in expected:
        assert entry in tr_block


def test_turkish_settings_detail_descriptions_are_translated():
    src = read(REPO / "static" / "i18n.js")
    tr_block = extract_locale_block(src, "tr")
    expected = [
        "settings_desc_workspace_panel_open: 'Etkinleştirildiğinde, çalışma alanı / dosya tarayıcı paneli her yeni oturumda otomatik olarak açılır. Yine de istediğiniz zaman manuel olarak kapatabilirsiniz.'",
        "settings_desc_notifications: 'Uygulama arka plandayken bir yanıt tamamlandığında bir sistem bildirimi gösterin.'",
        "settings_desc_token_usage: 'Her Asistan yanıtının altında giriş/çıkış jeton sayılarını gösterir. /usage ile de değiştirilebilir.'",
        "settings_desc_sidebar_density: 'Oturum listesinin sol kenar çubuğunda ne kadar meta veri göstereceğini kontrol eder.'",
        "settings_desc_auto_title_refresh: 'Oturum başlıklarını en son konuşmaya göre otomatik olarak yeniden oluşturarak konuşma ilerledikçe başlıkların alakalı kalmasını sağlar. LLM başlık oluşturma modeli yapılandırması gerektirir.'",
        "settings_desc_external_sessions: 'Oturum listesinde CLI, Telegram, Discord, Slack ve diğer kanallardan gelen konuşmaları gösterin. İçe aktarmak ve devam etmek için tıklayın.'",
        "settings_desc_sync_insights: 'WebUI belirteci kullanımını state.db\\'ye yansıtır, böylece hermes /insights tarayıcı oturum verilerini içerir. Varsayılan olarak kapalıdır.'",
        "settings_desc_check_updates: 'WebUI veya Agent\\'ın daha yeni sürümleri mevcut olduğunda bir banner gösterin. Periyodik olarak bir arka plan git getirme işlemi çalıştırır.'",
        "settings_desc_bot_name: 'Yalnızca varsayılan profil için kullanılır. Diğer profiller kendi profil adlarını kullanır.'",
        "settings_desc_password: 'Ayarlamak veya değiştirmek için yeni bir şifre girin. Geçerli ayarı korumak için boş bırakın.'",
    ]
    for entry in expected:
        assert entry in tr_block


def test_turkish_locale_matches_english_key_coverage():
    src = read(REPO / "static" / "i18n.js")
    en_keys = set(locale_keys(src, "en"))
    tr_keys = set(locale_keys(src, "tr"))
    assert sorted((en_keys - tr_keys) - PROFILE_CONCEPT_FALLBACK_KEYS) == []
    assert sorted(tr_keys - en_keys) == []


def test_turkish_locale_has_no_duplicate_keys():
    src = read(REPO / "static" / "i18n.js")
    keys = locale_keys(src, "tr")
    duplicates = sorted(k for k, count in Counter(keys).items() if count > 1)
    assert not duplicates, f"Turkish locale has duplicate keys: {duplicates}"


def test_turkish_locale_keys_use_standard_indentation():
    src = read(REPO / "static" / "i18n.js")
    tr_block = extract_locale_block(src, "tr")
    badly_indented = [
        line.strip()
        for line in tr_block.splitlines()
        if re.match(r"^\s{1,3}[a-zA-Z0-9_]+\s*:", line)
    ]
    assert badly_indented == []


def test_turkish_locale_has_no_double_escaped_unicode_sequences():
    """JSON-style double escapes (\\\\u2026) render literal backslash-u in the UI."""
    src = read(REPO / "static" / "i18n.js")
    tr_block = extract_locale_block(src, "tr")
    for bad in ("\\\\u2026", "\\\\u2192", "\\\\u2713"):
        assert bad not in tr_block, f"Turkish locale must not contain {bad!r}"
