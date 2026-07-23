"""Agent-delegated TTS locale and accessibility parity."""

from __future__ import annotations

import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
I18N = (ROOT / "static" / "i18n.js").read_text(encoding="utf-8")
INDEX = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
PANELS = (ROOT / "static" / "panels.js").read_text(encoding="utf-8")
WORKER = (ROOT / "api" / "agent_tts_worker.py").read_text(encoding="utf-8")

LOCALES = (
    "en",
    "it",
    "ja",
    "ru",
    "es",
    "de",
    "zh",
    "zh-Hant",
    "pt",
    "ko",
    "fr",
    "cs",
    "tr",
    "pl",
    "vi",
)
PROVIDER_IDS = (
    "edge",
    "openai",
    "xai",
    "elevenlabs",
    "mistral",
    "gemini",
    "kittentts",
    "piper",
    "deepinfra",
    "neutts",
)
STATE_KEYS = (
    "settings_desc_tts_engine",
    "settings_label_tts_provider",
    "tts_engine_browser",
    "tts_engine_agent",
    "tts_browser_default_voice",
    "tts_effective_browser_fallback",
    "tts_agent_unavailable_fallback",
    "tts_migration_required",
    "tts_saved_engine_repair",
    "tts_browser_unavailable",
    "tts_provider_none",
    "tts_provider_unavailable",
    "tts_provider_guidance",
    "tts_provider_refresh",
    "tts_capability_loading",
    "tts_agent_unavailable",
    "tts_migration_saving",
    "tts_migration_complete",
    "tts_migration_failed",
    "tts_provider_conflict",
    "tts_provider_saving",
    "tts_provider_saved",
    "tts_provider_failed",
    "tts_saved_engine_unavailable",
    "tts_timeout",
    "tts_invalid_audio",
    "tts_read_only",
)
REQUIRED_KEYS = STATE_KEYS + tuple(f"tts_provider_{item}" for item in PROVIDER_IDS)


def _locale_blocks() -> dict[str, str]:
    starts = []
    pattern = re.compile(r"^  (?:(\w+)|['\"]([^'\"]+)['\"]): \{", re.M)
    for match in pattern.finditer(I18N):
        locale = match.group(1) or match.group(2)
        if locale in LOCALES:
            starts.append((match.start(), locale))
    assert tuple(locale for _, locale in starts) == LOCALES
    blocks = {}
    for index, (start, locale) in enumerate(starts):
        end = starts[index + 1][0] if index + 1 < len(starts) else I18N.index("\n};", start)
        blocks[locale] = I18N[start:end]
    return blocks


def test_every_tts_key_is_owned_by_every_shipped_locale():
    blocks = _locale_blocks()
    for locale, block in blocks.items():
        for key in REQUIRED_KEYS:
            matches = re.findall(rf"^    {re.escape(key)}:\s*([^\n]+)", block, re.M)
            assert len(matches) == 1, f"{locale}: expected one {key}, got {len(matches)}"
            value = matches[0].strip().rstrip(",")
            assert value not in {f"'{key}'", f'"{key}"'}, f"{locale}: raw key {key}"


def test_non_english_locales_do_not_fall_back_to_english_for_target_states():
    blocks = _locale_blocks()
    english = blocks["en"]
    keys = ("tts_capability_loading", "tts_migration_failed", "tts_provider_conflict")
    for locale in LOCALES[1:]:
        for key in keys:
            en_value = re.search(rf"^    {key}:\s*(.+),$", english, re.M).group(1)
            value = re.search(rf"^    {key}:\s*(.+),$", blocks[locale], re.M).group(1)
            assert value != en_value, f"{locale}: {key} still uses English copy"


def test_provider_i18n_keys_are_a_fixed_lowercase_allowlist():
    expected = {f"tts_provider_{item}" for item in PROVIDER_IDS}
    worker_keys = set(re.findall(r'"(tts_provider_[a-z0-9_]+)"', WORKER))
    # The implementation constructs only allowlisted keys and uses no display-label derivation.
    assert 'f"tts_provider_{provider_id}"' in WORKER
    for provider_id in PROVIDER_IDS:
        assert f'"{provider_id}"' in WORKER
    assert "label.lower" not in WORKER
    assert "name.lower" not in WORKER
    assert not worker_keys - expected


def test_tts_settings_status_and_busy_state_are_accessible():
    assert 'id="settingsTtsStatus" role="status" aria-live="polite"' in INDEX
    assert 'for="settingsTtsProvider"' in INDEX
    assert 'id="settingsTtsProviderRefresh"' in INDEX
    assert "setAttribute('aria-busy','true')" in PANELS
    assert "removeAttribute('aria-busy')" in PANELS
    assert "option.disabled=!row.selectable" in PANELS


def test_tts_engine_surface_has_only_canonical_selectable_values():
    start = INDEX.index('id="settingsTtsEngine"')
    end = INDEX.index("</select>", start)
    options = set(re.findall(r'<option value="([^"]+)"', INDEX[start:end]))
    assert options == {"browser", "agent"}
