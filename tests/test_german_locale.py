from pathlib import Path
import re

from tests.test_polish_locale import extract_locale_block, read

REPO = Path(__file__).resolve().parent.parent
I18N = REPO / "static" / "i18n.js"

# Begriffe, die im Deutschen bewusst identisch zum Englischen bleiben
ALLOWED_IDENTICAL = {
    "mcp_field_url", "session_worktree_badge", "theme_set", "status_tokens",
    "terminal_title", "workspace_sort_name_asc", "workspace_sort_name_desc",
    "symlink_link_to", "settings_section_system_title", "composer_control_workspace",
    "composer_control_yolo", "composer_control_toolsets", "composer_control_status",
    "settings_dropdown_system", "settings_tab_plugins", "ext_assets_styles",
    "ext_sidecar_proxy", "ext_gallery_version", "settings_plugins_title",
    "settings_tab_system", "settings_aux_task_mcp", "settings_label_theme",
    "settings_label_skin", "tab_chat", "tab_skills", "tab_kanban", "kanban_board",
    "kanban_status", "kanban_workspace_worktree", "kanban_skills", "insights_tokens",
    "insights_model_tokens", "insights_model_cache", "insights_model_health_provider",
    "insights_skill_usage_col_skill", "insights_skill_usage_col_patches",
    "export_session_json", "export_session_html", "providers_status_oauth",
    "providers_key_placeholder_new", "provider_quota_metric_limit", "cron_mode_agent",
    "skill_name", "cron_name_label", "cron_name_placeholder", "cron_schedule_minute_label",
    "cron_prompt_label", "workspace_name_label", "profile_name_label", "yolo_pill_label",
    "media_audio_label", "media_video_label", "checkpoint_title",
}

# Schlüssel mit Platzhaltern, die in diesem Beitrag übersetzt wurden
PR_KEYS = {
    "mcp_tool_count", "pdf_truncated", "show_earlier_steps", "workspace_switched_new_chat",
    "kanban_visible_tasks", "kanban_comments_count", "kanban_events_count",
    "kanban_runs_count", "kanban_status_original_hint", "provider_cost_budget_pct",
}

VALUE_RE = re.compile(r"^\s{4}([a-zA-Z0-9_]+):\s*'((?:[^'\\]|\\.)*)',\s*$", re.M)


def _values(block: str) -> dict[str, str]:
    return dict(VALUE_RE.findall(block))


def test_german_locale_block_exists():
    de = extract_locale_block(read(I18N), "de")
    assert "_lang: 'de'" in de


def test_german_locale_representative_translations():
    de = extract_locale_block(read(I18N), "de")
    for entry in [
        "settings_heading_title: 'Kontrollzentrum'",
        "settings_tab_appearance: 'Darstellung'",
        "session_archive: 'Konversation archivieren'",
        "kanban_status_ready: 'Bereit'",
        "composer_control_model: 'Modell'",
        "providers_save: 'Speichern'",
        "cron_weekday_mon: 'Montag'",
    ]:
        assert entry in de


def test_german_locale_has_few_untranslated_strings():
    src = read(I18N)
    en = _values(extract_locale_block(src, "en"))
    de = _values(extract_locale_block(src, "de"))
    untranslated = sorted(
        k for k, v in de.items()
        if k in en and v == en[k] and len(v) > 3 and k not in ALLOWED_IDENTICAL
    )
    # Neue Schlüssel dürfen kurzzeitig englisch nachgezogen werden,
    # aber nicht wieder in dreistelliger Zahl.
    assert len(untranslated) <= 25, untranslated


def test_german_locale_preserves_placeholders():
    src = read(I18N)
    en = _values(extract_locale_block(src, "en"))
    de = _values(extract_locale_block(src, "de"))
    ph = re.compile(r"\$?\{[a-zA-Z0-9_]+\}")
    mismatched = sorted(
        k for k in ALLOWED_IDENTICAL | PR_KEYS
        if k in de and k in en and sorted(ph.findall(de[k])) != sorted(ph.findall(en[k]))
    )
    assert mismatched == []
