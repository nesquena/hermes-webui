from collections import Counter
from pathlib import Path
import re
import pytest
from tests.test_issue2147_profile_concept_help import PROFILE_CONCEPT_KEYS


REPO = Path(__file__).resolve().parent.parent
PROFILE_CONCEPT_FALLBACK_KEYS = {
    *PROFILE_CONCEPT_KEYS,
    "workspace_artifact_source_session",
}


@pytest.fixture(scope="session", autouse=True)
def test_server():
    """Localization unit tests do not require an HTTP server."""


@pytest.fixture(autouse=True)
def cleanup_test_sessions():
    """No server sessions are created by this module."""


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def get_catalan_source() -> str:
    """Return the static/i18n.js source that contains the Catalan locale."""
    return read(REPO / "static" / "i18n.js")


def extract_locale_block(src: str, locale_key: str) -> str:
    start_match = re.search(rf"^\s*{re.escape(locale_key)}\s*:\s*\{{", src, re.MULTILINE)
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


def test_catalan_locale_block_exists():
    src = get_catalan_source()
    ca_block = extract_locale_block(src, "ca")
    assert ca_block
    assert "_lang: 'ca'" in ca_block
    assert "_label: 'Català'" in ca_block
    assert "_speech: 'ca-ES'" in ca_block


def test_catalan_locale_includes_representative_translations():
    src = get_catalan_source()
    ca_block = extract_locale_block(src, "ca")
    expected = [
        "settings_title: 'Configuració'",
        "settings_label_language: 'Llengua'",
        "login_title: 'Inicia la sessió'",
        "login_placeholder: 'Contrasenya'",
        "approval_heading: 'Cal aprovació'",
        "tab_tasks: 'Tasques'",
        "tab_skills: 'Habilitats'",
        "tab_memory: 'Memòria'",
        "tab_profiles: 'Perfils'",
        "empty_title: 'Com et puc ajudar?'",
        "onboarding_title: 'Benvingut a Hermes Web UI'",
    ]
    for entry in expected:
        assert entry in ca_block, f"missing expected Catalan translation: {entry}"


def test_catalan_locale_matches_english_key_coverage():
    i18n_src = read(REPO / "static" / "i18n.js")
    ca_src = get_catalan_source()
    en_keys = set(locale_keys(i18n_src, "en"))
    ca_keys = set(locale_keys(ca_src, "ca"))
    assert sorted((en_keys - ca_keys) - PROFILE_CONCEPT_FALLBACK_KEYS) == []
    assert sorted(ca_keys - en_keys) == []


def test_catalan_locale_has_no_duplicate_keys():
    src = get_catalan_source()
    keys = locale_keys(src, "ca")
    duplicates = sorted(k for k, count in Counter(keys).items() if count > 1)
    assert not duplicates, f"Catalan locale has duplicate keys: {duplicates}"


def test_catalan_locale_keys_use_standard_indentation():
    src = get_catalan_source()
    ca_block = extract_locale_block(src, "ca")
    badly_indented = []
    for line in ca_block.splitlines():
        m = re.match(r"^(\s*)[a-zA-Z0-9_]+\s*:", line)
        if m and len(m.group(1)) != 4:
            badly_indented.append(f"{len(m.group(1))} spaces: {line.strip()}")
    assert badly_indented == []


def test_catalan_locale_arrow_function_values_mirror_english():
    i18n_src = read(REPO / "static" / "i18n.js")
    ca_src = get_catalan_source()
    en_block = extract_locale_block(i18n_src, "en")
    ca_block = extract_locale_block(ca_src, "ca")

    value_re = re.compile(r"^\s+([a-zA-Z0-9_]+):\s*(.+?)(?:,\s*$|\s*$)", re.MULTILINE)
    arrow_re = re.compile(r"^\s*\(?[a-zA-Z_,\s]*\)?\s*=>")
    helper_re = re.compile(r"^_i18n[A-Za-z]+$")

    def callable_values(block):
        out = set()
        for k, v in value_re.findall(block):
            v = v.strip()
            if arrow_re.match(v) or helper_re.match(v):
                out.add(k)
        return out

    assert callable_values(en_block) == callable_values(ca_block)


def test_catalan_locale_preserves_placeholder_patterns():
    i18n_src = read(REPO / "static" / "i18n.js")
    ca_src = get_catalan_source()
    en_block = extract_locale_block(i18n_src, "en")
    ca_block = extract_locale_block(ca_src, "ca")

    value_re = re.compile(r"^\s+([a-zA-Z0-9_]+):\s*(.+?)(?:,\s*$|\s*$)", re.MULTILINE)
    placeholder_re = re.compile(r"\{[0-9]+\}|\$\{[a-zA-Z_][a-zA-Z0-9_]*\}")

    def kv(block):
        out = {}
        for k, v in value_re.findall(block):
            out[k] = v
        return out

    en_kv = kv(en_block)
    ca_kv = kv(ca_block)

    for key, en_val in en_kv.items():
        if key not in ca_kv:
            continue
        en_vars = sorted(placeholder_re.findall(en_val))
        ca_vars = sorted(placeholder_re.findall(ca_kv[key]))
        if "=>" not in ca_kv[key]:
            if en_vars or ca_vars:
                assert ca_vars == en_vars, f"Key '{key}' placeholder mismatch in ca locale"


def test_catalan_locale_has_no_double_escaped_unicode_sequences():
    src = get_catalan_source()
    ca_block = extract_locale_block(src, "ca")
    for bad in ("\\\\u2026", "\\\\u2192", "\\\\u2713"):
        assert bad not in ca_block, f"Catalan locale must not contain {bad!r}"


def test_catalan_locale_uses_real_utf8_diacritics():
    """Català uses à è é í ï ò ó ú ü ç l·l — confirm the block carries real
    UTF-8 diacritics, not ASCII-only text."""
    src = get_catalan_source()
    ca_block = extract_locale_block(src, "ca")
    diacritics = "àèéíïòóúüçÀÈÉÍÏÒÓÚÜÇ·"
    assert any(ch in ca_block for ch in diacritics), "Catalan locale has no diacritics"
