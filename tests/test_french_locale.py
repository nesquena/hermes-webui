"""Regression guards for the French (fr) locale in static/i18n.js.

The fr locale historically lagged en: recently-added keys
(profile-concept help, workspace artifact source) and a long tail of
labels were never translated and silently fell back to English at
render time. These tests pin the fr locale to full key parity with en
and forbid new English leftovers that are not legitimately identical
words in both languages.
"""
from collections import Counter
from pathlib import Path
import re


REPO = Path(__file__).resolve().parent.parent


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


def locale_string_values(block: str) -> dict:
    """key -> plain single-quoted string value (unescaped), for fr-block lines only."""
    out = {}
    for line in block.splitlines():
        m = re.match(r"^    ([a-zA-Z0-9_]+):\s*'((?:[^'\\]|\\.)*)',?$", line)
        if m:
            out[m.group(1)] = m.group(2)
    return out


def test_french_locale_block_exists():
    src = read(REPO / "static" / "i18n.js")
    fr_block = extract_locale_block(src, "fr")
    assert fr_block
    assert "_lang: 'fr'" in fr_block
    assert "_label: 'Français'" in fr_block
    assert "_speech: 'fr-FR'" in fr_block


def test_french_locale_includes_representative_translations():
    src = read(REPO / "static" / "i18n.js")
    fr_block = extract_locale_block(src, "fr")
    expected = [
        "settings_title: 'Paramètres'",
        "login_title: 'Connexion'",
        "approval_heading: 'Approbation requise'",
        "tab_tasks: 'Tâches'",
        "tab_profiles: 'Profils'",
        "empty_title: 'En quoi puis-je vous aider ?'",
        "onboarding_title: 'Bienvenue dans l\\'interface Web Hermes'",
    ]
    for entry in expected:
        assert entry in fr_block, f"missing expected French translation: {entry}"


def test_french_locale_includes_profile_concept_help():
    """The profile-concept help keys once fell back to English in fr; pin them."""
    src = read(REPO / "static" / "i18n.js")
    fr_block = extract_locale_block(src, "fr")
    expected = [
        "profile_concept_title: 'Profils vs espaces de travail'",
        "profile_concept_subtitle: 'Utilisez les profils pour définir comment l’agent travaille ; utilisez les espaces de travail pour définir sur quels fichiers il travaille.'",
        "profile_concept_label_together: 'Ensemble'",
        "profile_concept_label_example: 'Exemple'",
        "workspace_artifact_source_session: 'session'",
    ]
    for entry in expected:
        assert entry in fr_block, f"missing expected French translation: {entry}"


def test_french_locale_matches_english_key_coverage():
    src = read(REPO / "static" / "i18n.js")
    en_keys = set(locale_keys(src, "en"))
    fr_keys = set(locale_keys(src, "fr"))
    # Full parity: the fr locale must not rely on per-key fallback to English.
    assert sorted(en_keys - fr_keys) == [], (
        f"French locale missing keys: {sorted(en_keys - fr_keys)}"
    )
    assert sorted(fr_keys - en_keys) == [], (
        f"French locale has keys unknown to English: {sorted(fr_keys - en_keys)}"
    )


def test_french_locale_has_no_duplicate_keys():
    src = read(REPO / "static" / "i18n.js")
    keys = locale_keys(src, "fr")

    duplicates = sorted(k for k, count in Counter(keys).items() if count > 1)
    assert not duplicates, f"French locale has duplicate keys: {duplicates}"


def test_french_locale_keys_use_standard_indentation():
    src = read(REPO / "static" / "i18n.js")
    fr_block = extract_locale_block(src, "fr")

    badly_indented = []
    for line in fr_block.splitlines():
        m = re.match(r"^(\s*)[a-zA-Z0-9_]+\s*:", line)
        if m and len(m.group(1)) != 4:
            badly_indented.append(f"{len(m.group(1))} spaces: {line.strip()}")
    assert badly_indented == []


def test_french_locale_no_untranslated_english_leftovers():
    """Every fr string value must differ from its en value, except words that
    are genuinely identical in French (acronyms, technical terms, and words
    like 'Conversation'/'Total' that are the same in both languages)."""
    src = read(REPO / "static" / "i18n.js")
    en_vals = locale_string_values(extract_locale_block(src, "en"))
    fr_vals = locale_string_values(extract_locale_block(src, "fr"))

    IDENTICAL_IN_FRENCH = {
        "URL", "MCP", "YOLO", "JSON", "HTML", "OAuth", "Worktree", "Kanban",
        "auto", "session", "sessions",
        "Conversation", "Plugins", "Extensions", "Diagnostics",
        "Permissions", "Version", "Documentation", "Vision", "Compression",
        "Triage", "Parents", "Description", "Messages", "Sessions",
        "Total", "Cache", "Compact", "Agent", "Script", "Mode", "Minute",
        "Quota", "Audio", "Date", "Message", "Terminal",
    }

    leftovers = sorted(
        k for k, fr in fr_vals.items()
        if k in en_vals and fr == en_vals[k] and fr not in IDENTICAL_IN_FRENCH
    )
    assert leftovers == [], (
        f"French locale still carries English values: {leftovers}"
    )


def test_french_locale_arrow_function_values_mirror_english():
    src = read(REPO / "static" / "i18n.js")
    en_block = extract_locale_block(src, "en")
    fr_block = extract_locale_block(src, "fr")

    value_re = re.compile(r"^\s+([a-zA-Z0-9_]+):\s*(.+?)(?:,\s*$|\s*$)", re.MULTILINE)
    arrow_re = re.compile(r"^\s*\(?[a-zA-Z_,\s]*\)?\s*=>")

    def arrows(block):
        return {k for k, v in value_re.findall(block) if arrow_re.match(v)}

    assert arrows(fr_block) == arrows(en_block)


def test_french_locale_preserves_placeholder_patterns():
    src = read(REPO / "static" / "i18n.js")
    en_block = extract_locale_block(src, "en")
    fr_block = extract_locale_block(src, "fr")

    value_re = re.compile(r"^\s+([a-zA-Z0-9_]+):\s*(.+?)(?:,\s*$|\s*$)", re.MULTILINE)
    placeholder_re = re.compile(r"\{[0-9]+\}|\$\{[a-zA-Z_][a-zA-Z0-9_]*\}")

    def kv(block):
        out = {}
        for k, v in value_re.findall(block):
            out[k] = v
        return out

    en_kv = kv(en_block)
    fr_kv = kv(fr_block)

    for key, en_val in en_kv.items():
        if key not in fr_kv:
            continue
        en_vars = sorted(placeholder_re.findall(en_val))
        fr_vars = sorted(placeholder_re.findall(fr_kv[key]))
        if "=>" not in fr_kv[key]:
            if en_vars or fr_vars:
                assert fr_vars == en_vars, f"Key '{key}' placeholder mismatch in fr locale"


def test_french_locale_has_no_double_escaped_unicode_sequences():
    """JSON-style double escapes (\\\\u2026) render literal backslash-u in the UI."""
    src = read(REPO / "static" / "i18n.js")
    fr_block = extract_locale_block(src, "fr")
    for bad in ("\\\\u2026", "\\\\u2192", "\\\\u2713"):
        assert bad not in fr_block, f"French locale must not contain {bad!r}"


def test_french_locale_uses_real_utf8_accents():
    """French uses accents (é è à ç …) — confirm the block carries real UTF-8
    accents, not ASCII-only text (which would mean nothing was translated)."""
    src = read(REPO / "static" / "i18n.js")
    fr_block = extract_locale_block(src, "fr")
    accents = "éèêëàâçœûùÜ"
    assert any(ch in fr_block for ch in accents), "French locale has no accents"
