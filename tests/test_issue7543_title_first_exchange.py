"""Regression coverage for nesquena/hermes-webui#7543.

Bug: manual "Regenerate title" failed with missing_exchange for sessions
whose transcript opens with consecutive user rows (no assistant text before
the second user turn) — _first_exchange_snippets() aborted at the second
user message, the aux call was skipped, and the deterministic local fallback
was persisted (200 + identical wrong title on every retry).
"""

from unittest.mock import MagicMock

import api.profiles as profiles_api
import api.streaming as streaming


class _ProfileEnv:
    def __enter__(self):
        return None

    def __exit__(self, exc_type, exc, tb):
        return False


def _capturing_llm_result(captured, title="LLM Title", status="llm_aux"):
    # Guard-faithful stand-in for the aux route: mirrors the documented
    # generate_title_raw_via_aux contract — empty assistant_text is rejected
    # with missing_exchange (the provider edge), never the selection logic.
    def _fake(user_text, assistant_text, **kwargs):
        captured["user_text"] = user_text
        captured["assistant_text"] = assistant_text
        if not user_text or not assistant_text:
            return None, "missing_exchange", ""
        return title, status, ""

    return _fake


def _run_generation(monkeypatch, messages, captured, prefer_latest=False):
    monkeypatch.setattr(profiles_api, "profile_env_for_background_worker", lambda *a, **k: _ProfileEnv())
    monkeypatch.setattr(streaming, "_aux_title_generation_enabled", lambda: True)
    monkeypatch.setattr(streaming, "_generate_llm_session_title_via_aux", _capturing_llm_result(captured))
    session = MagicMock()
    session.messages = messages
    session.session_id = "issue7543"
    return streaming.generate_session_title_for_session(session, prefer_latest=prefer_latest)


# --- Fix A: _first_exchange_snippets scans past consecutive user rows ---


def test_first_exchange_snippets_scan_past_consecutive_user_rows():
    # Channel-backed import/projection shape: opening run of user rows,
    # first assistant answer only much later (#7543 repro shape).
    messages = [
        {"role": "user", "content": "Opening question about certificates"},
        {"role": "user", "content": "Follow-up that used to trigger the break"},
        {"role": "user", "content": "Another queued user turn"},
        {"role": "assistant", "content": "## Real first answer with substance"},
    ]
    user_text, asst_text = streaming._first_exchange_snippets(messages)
    assert user_text == "Opening question about certificates"
    assert asst_text == "## Real first answer with substance"


def test_first_exchange_snippets_normal_pair_unchanged():
    # Classic [user, assistant] opening must keep its exact behavior.
    messages = [
        {"role": "user", "content": "Please fix the stale sidebar title controls"},
        {"role": "assistant", "content": "I will add a regenerate-title action."},
        {"role": "user", "content": "Second question"},
    ]
    user_text, asst_text = streaming._first_exchange_snippets(messages)
    assert user_text == "Please fix the stale sidebar title controls"
    assert asst_text == "I will add a regenerate-title action."


def test_first_exchange_snippets_without_any_assistant_text_still_empty():
    # No assistant text anywhere -> still unusable for the LLM path; the
    # missing_exchange rejection in generate_title_raw_via_aux stays intact.
    messages = [
        {"role": "user", "content": "Question one"},
        {"role": "user", "content": "Question two"},
    ]
    user_text, asst_text = streaming._first_exchange_snippets(messages)
    assert user_text == "Question one"
    assert asst_text == ""


def test_issue7543_real_transcript_shape_reaches_llm_path(monkeypatch):
    captured = {}
    messages = [
        {"role": "user", "content": "Wie kann ich auf einem windows server 2025 ein zertifikat erstellen"} ,
        {"role": "user", "content": "Wächst das Log so nicht in eine unendliche Schleife"},
        {"role": "assistant", "content": "## Zertifikat für Windows Admin Center mit AD-CS"},
    ]
    title, status, _raw = _run_generation(monkeypatch, messages, captured)
    assert status == "llm_aux"
    assert title == "LLM Title"
    assert captured["user_text"].startswith("Wie kann ich auf einem windows server 2025")
    assert captured["assistant_text"].startswith("## Zertifikat")


def test_regenerate_helper_errors_with_real_walkers_when_no_user_text_exists(monkeypatch):
    """Observable error path through the real walkers: no user text anywhere
    (orphan assistant rows only) -> empty_user_message, aux never called."""
    captured = {}
    messages = [{"role": "assistant", "content": "orphan answer without any user turn"}]
    title, status, _raw = _run_generation(monkeypatch, messages, captured)
    assert title is None
    assert status == "empty_user_message"
    assert "user_text" not in captured  # aux path never reached


def test_regenerate_helper_prefer_latest_path_unchanged(monkeypatch):
    captured = {}
    messages = [
        {"role": "user", "content": "First question"},
        {"role": "assistant", "content": "First answer"},
        {"role": "user", "content": "Latest question"},
        {"role": "assistant", "content": "Latest answer"},
    ]
    title, status, _raw = _run_generation(monkeypatch, messages, captured, prefer_latest=True)
    assert status == "llm_aux"
    assert captured["user_text"] == "Latest question"
    assert captured["assistant_text"] == "Latest answer"


def test_regenerate_helper_prefer_latest_with_empty_last_user_message_errors(monkeypatch):
    captured = {}
    messages = [
        {"role": "user", "content": "First question"},
        {"role": "assistant", "content": "First answer"},
        {"role": "user", "content": "   "},
    ]
    title, status, _raw = _run_generation(monkeypatch, messages, captured, prefer_latest=True)
    assert title is None
    assert status == "empty_user_message"
    assert "user_text" not in captured
