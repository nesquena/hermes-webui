"""Reconciliation of the residual #7579 surfaces.

The umbrella lists a Project Context label/empty state (Memory panel) and the
sidebar project filter chips (All / Unassigned + tooltip) as hardcoded English.
Everything else in the A/B/C list is either merged (#7602, #7619, #7650) or
owned by the open #7698 / #8001 lanes; these two surfaces were not.
"""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
I18N_JS = (ROOT / "static" / "i18n.js").read_text(encoding="utf-8")
PANELS_JS = (ROOT / "static" / "panels.js").read_text(encoding="utf-8")
SESSIONS_JS = (ROOT / "static" / "sessions.js").read_text(encoding="utf-8")

PROJECT_CONTEXT_KEYS = ("memory_project_context_title", "memory_project_context_empty")
PROJECT_CHIP_KEYS = ("project_chip_all", "project_chip_unassigned", "project_chip_unassigned_title")

# Keys from the #7579 list A that are wired through t()/data-i18n but still hold
# English text in the ru bundle, so the UI silently falls back to English.
RU_VALUE_FIX_KEYS = (
    "settings_send_key_enter",
    "settings_send_key_ctrl_enter",
    "settings_send_key_shift_enter",
    "settings_tts_voice_default_system",
    "show_earlier_step_one",
    "show_earlier_steps",
)


def _extract_locale_block(src: str, locale_key: str) -> str:
    match = re.search(rf"\n  {re.escape(locale_key)}: \{{\n", src)
    assert match, f"{locale_key} locale block not found"
    start = match.end() - 1
    end = src.index("\n  },", start)
    return src[start:end]


EN_BLOCK = _extract_locale_block(I18N_JS, "en")
RU_BLOCK = _extract_locale_block(I18N_JS, "ru")


def _value(block: str, key: str) -> str | None:
    match = re.search(rf"^\s+{re.escape(key)}: '((?:[^'\\]|\\.)*)'", block, re.MULTILINE)
    return match.group(1) if match else None


def test_en_locale_defines_the_new_keys():
    for key in PROJECT_CONTEXT_KEYS + PROJECT_CHIP_KEYS:
        assert _value(EN_BLOCK, key), f"missing key {key!r} in the en locale block"


def test_memory_project_context_section_uses_i18n_keys():
    row = re.search(r"\{ key: 'project_context'.*?\},", PANELS_JS, re.DOTALL)
    assert row, "project_context memory section row not found"
    body = row.group(0)
    assert "labelKey: 'memory_project_context_title'" in body
    assert "emptyKey: 'memory_project_context_empty'" in body
    assert "label:" not in body and " empty:" not in body
    assert "No project context file found for this workspace." not in PANELS_JS


def test_sidebar_project_chips_use_t():
    assert "t('project_chip_all')" in SESSIONS_JS
    assert "t('project_chip_unassigned')" in SESSIONS_JS
    assert "t('project_chip_unassigned_title')" in SESSIONS_JS
    assert "textContent='All'" not in SESSIONS_JS
    assert "textContent='Unassigned'" not in SESSIONS_JS
    assert "title='Show conversations not yet assigned to a project'" not in SESSIONS_JS


def test_ru_translates_the_new_keys():
    for key in PROJECT_CONTEXT_KEYS + PROJECT_CHIP_KEYS:
        value = _value(RU_BLOCK, key)
        assert value, f"missing key {key!r} in the ru locale block"
        assert re.search(r"[А-Яа-я]", value), f"ru value for {key!r} is not Russian: {value!r}"


def test_ru_no_longer_falls_back_to_english_for_wired_keys():
    for key in RU_VALUE_FIX_KEYS:
        en_value = _value(EN_BLOCK, key)
        ru_value = _value(RU_BLOCK, key)
        assert ru_value, f"missing key {key!r} in the ru locale block"
        assert ru_value != en_value, f"ru value for {key!r} still English: {en_value!r}"
    # {0} interpolation must survive the translation.
    steps = _value(RU_BLOCK, "show_earlier_steps")
    assert steps and "{0}" in steps
