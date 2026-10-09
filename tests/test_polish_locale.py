from collections import Counter
from pathlib import Path
import re
from tests.test_issue2147_profile_concept_help import PROFILE_CONCEPT_KEYS


REPO = Path(__file__).resolve().parent.parent
PROFILE_CONCEPT_FALLBACK_KEYS = {
    *PROFILE_CONCEPT_KEYS,
    "workspace_artifact_source_session",
}


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


def test_polish_locale_block_exists():
    src = read(REPO / "static" / "i18n.js")
    pl_block = extract_locale_block(src, "pl")
    assert pl_block
    assert "_lang: 'pl'" in pl_block
    assert "_label: 'Polski'" in pl_block
    assert "_speech: 'pl-PL'" in pl_block


def test_polish_locale_includes_representative_translations():
    src = read(REPO / "static" / "i18n.js")
    pl_block = extract_locale_block(src, "pl")
    expected = [
        "settings_title: 'Ustawienia'",
        "settings_label_language: 'Język'",
        "login_title: 'Zaloguj się'",
        "approval_heading: 'Wymagana aprobata'",
        "tab_chat: 'Czat'",
        "tab_tasks: 'Zadania'",
        "tab_profiles: 'Profile'",
        "empty_title: 'W czym mogę pomóc?'",
        "onboarding_title: 'Witaj w Hermes Web UI'",
    ]
    for entry in expected:
        assert entry in pl_block


def test_polish_settings_detail_descriptions_are_translated():
    src = read(REPO / "static" / "i18n.js")
    pl_block = extract_locale_block(src, "pl")
    expected = [
        "settings_desc_workspace_panel_open: 'Gdy ta opcja jest włączona, panel obszaru roboczego / przeglądarki plików otwiera się automatycznie przy każdej nowej sesji. Nadal możesz go zamknąć ręcznie w dowolnym momencie.'",
        "settings_desc_notifications: 'Pokaż powiadomienie systemowe, gdy odpowiedź zostanie ukończona, podczas gdy aplikacja działa w tle.'",
        "settings_desc_token_usage: 'Wyświetla liczbę tokenów wejściowych/wyjściowych pod każdą odpowiedzią asystenta. Można też przełączyć za pomocą /usage.'",
        "settings_desc_sidebar_density: 'Kontroluje, ile metadanych wyświetla lista sesji na lewym pasku bocznym.'",
        "settings_desc_auto_title_refresh: 'Automatycznie generuje na nowo tytuł konwersacji na podstawie najnowszej wymiany, utrzymując go adekwatnym w miarę rozwoju rozmowy. Wymaga skonfigurowanego modelu LLM do generowania tytułów.'",
        "settings_desc_external_sessions: 'Pokaż konwersacje z CLI, Telegrama, Discorda, Slacka i innych kanałów na liście sesji. Kliknij, aby zaimportować i kontynuować.'",
        "settings_desc_cron_sessions: 'Wyświetlaj wyjście zadań cron jako konwersacje na pasku bocznym. Aktywne tylko wtedy, gdy włączone są sesje spoza WebUI. Domyślnie wyłączone; zadania o wysokiej częstotliwości mogą zalać pasek boczny.'",
        "settings_desc_sync_insights: 'Odzwierciedla zużycie tokenów WebUI w state.db, dzięki czemu hermes /insights uwzględnia dane sesji przeglądarki. Domyślnie wyłączone.'",
        "settings_desc_check_updates: 'Pokaż baner, gdy dostępne są nowsze wersje WebUI lub Agenta. Okresowo uruchamia pobieranie git fetch w tle.'",
        "settings_desc_bot_name: 'Używane tylko dla profilu domyślnego. Inne profile używają własnych nazw profilu.'",
        "settings_desc_password: 'Wpisz nowe hasło, aby je ustawić lub zmienić. Pozostaw puste, aby zachować obecne ustawienie.'",
    ]
    for entry in expected:
        assert entry in pl_block


def test_polish_locale_matches_english_key_coverage():
    src = read(REPO / "static" / "i18n.js")
    en_keys = set(locale_keys(src, "en"))
    pl_keys = set(locale_keys(src, "pl"))
    assert sorted((en_keys - pl_keys) - PROFILE_CONCEPT_FALLBACK_KEYS) == []
    assert sorted(pl_keys - en_keys) == []


def test_polish_locale_has_no_duplicate_keys():
    src = read(REPO / "static" / "i18n.js")
    keys = locale_keys(src, "pl")

    duplicates = sorted(k for k, count in Counter(keys).items() if count > 1)
    assert not duplicates, f"Polish locale has duplicate keys: {duplicates}"


def test_polish_locale_keys_use_standard_indentation():
    src = read(REPO / "static" / "i18n.js")
    pl_block = extract_locale_block(src, "pl")

    # Enforce strict 4-space indentation for keys.
    badly_indented = []
    for line in pl_block.splitlines():
        m = re.match(r"^(\s*)[a-zA-Z0-9_]+\s*:", line)
        if m and len(m.group(1)) != 4:
            badly_indented.append(f"{len(m.group(1))} spaces: {line.strip()}")
    assert badly_indented == []


def test_polish_locale_arrow_function_values_mirror_english():
    src = read(REPO / "static" / "i18n.js")
    en_block = extract_locale_block(src, "en")
    pl_block = extract_locale_block(src, "pl")

    value_re = re.compile(r"^\s+([a-zA-Z0-9_]+):\s*(.+?)(?:,\s*$|\s*$)", re.MULTILINE)
    arrow_re = re.compile(r"^\s*\(?[a-zA-Z_,\s]*\)?\s*=>")

    def arrows(block):
        return {k for k, v in value_re.findall(block) if arrow_re.match(v)}

    assert arrows(pl_block) == arrows(en_block)


def test_polish_locale_preserves_placeholder_patterns():
    src = read(REPO / "static" / "i18n.js")
    en_block = extract_locale_block(src, "en")
    pl_block = extract_locale_block(src, "pl")

    value_re = re.compile(r"^\s+([a-zA-Z0-9_]+):\s*(.+?)(?:,\s*$|\s*$)", re.MULTILINE)
    placeholder_re = re.compile(r"\{[0-9]+\}|\$\{[a-zA-Z_][a-zA-Z0-9_]*\}")

    def kv(block):
        out = {}
        for k, v in value_re.findall(block):
            out[k] = v
        return out

    en_kv = kv(en_block)
    pl_kv = kv(pl_block)

    for key, en_val in en_kv.items():
        if key not in pl_kv:
            continue
        en_vars = sorted(placeholder_re.findall(en_val))
        pl_vars = sorted(placeholder_re.findall(pl_kv[key]))
        # Skip arrow functions which might contain duplicate conditional logic and thus more template vars
        if "=>" not in pl_kv[key]:
            if en_vars or pl_vars:
                assert pl_vars == en_vars, f"Key '{key}' missing placeholders in pl locale"


def test_polish_locale_has_no_double_escaped_unicode_sequences():
    """JSON-style double escapes (\\\\u2026) render literal backslash-u in the UI."""
    src = read(REPO / "static" / "i18n.js")
    pl_block = extract_locale_block(src, "pl")
    for bad in ("\\\\u2026", "\\\\u2192", "\\\\u2713"):
        assert bad not in pl_block, f"Polish locale must not contain {bad!r}"
