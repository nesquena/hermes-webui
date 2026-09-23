"""Canonical keys intentionally owned by English and inherited by other locales."""

from tests.test_issue2147_profile_concept_help import PROFILE_CONCEPT_KEYS


SESSION_RESUME_FALLBACK_KEYS = {
    "session_resume_in_webui",
    "session_resume_in_webui_desc",
    "session_resume_in_webui_confirm_title",
    "session_resume_in_webui_confirm_message",
    "session_resume_in_webui_confirm_btn",
    "session_resume_in_webui_resumed",
    "session_resume_in_webui_failed",
    # D6: localized, identifier-free failure categories. English owns them and
    # every other locale inherits via the fallback chain.
    "session_resume_in_webui_confirm_required",
    "session_resume_in_webui_not_allowed",
    "session_resume_in_webui_source_changed",
    "session_resume_in_webui_conflict",
    "session_resume_in_webui_lineage_unavailable",
    "session_resume_in_webui_profile_mismatch",
    "session_resume_in_webui_required",
}

INTENTIONAL_ENGLISH_FALLBACK_KEYS = {
    *PROFILE_CONCEPT_KEYS,
    "workspace_artifact_source_session",
    *SESSION_RESUME_FALLBACK_KEYS,
}
