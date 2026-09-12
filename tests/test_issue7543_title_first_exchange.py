"""Regression coverage for local hotfix of nesquena/hermes-webui#7543.

Bug: manual "Regenerate title" failed with missing_exchange for sessions
whose transcript opens with consecutive user rows (no assistant text before
the second user turn) — _first_exchange_snippets() aborted at the second
user message, the aux call was skipped, and the deterministic local fallback
was persisted (200 + identical wrong title on every retry).

LOCAL UNCOMMITTED HOTFIX — intentionally not upstreamed. The working-tree
changes are captured in /root/workspace/local-7543.patch; originals in
/root/workspace/7543-localpatch-backup/.
"""

import sys
from pathlib import Path
from unittest.mock import MagicMock

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import api.profiles as profiles_api  # noqa: E402
import api.streaming as streaming  # noqa: E402


class _ProfileEnv:
    def __enter__(self):
        return None

    def __exit__(self, exc_type, exc, tb):
        return False


def _capturing_llm_result(captured, title="LLM Title", status="llm_aux"):
    def _fake(user_text, assistant_text, **kwargs):
        captured["user_text"] = user_text
        captured["assistant_text"] = assistant_text
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


# --- Fix B: fallback to last complete exchange when first exchange unusable ---
#
# NOTE: both walkers share text extraction, so a "scrubbed message" transcript
# is handled identically by pre- and post-fix code (verified against the
# pre-fix module). To actually pin Fix B's discriminator — first walker yields
# no user text, latest walker yields a complete pair — the walkers are mocked.
# The transcript-level scrubber case stays covered by the mocked-walker test's
# transcript comment and by Fix A tests at snippet level.


def test_regenerate_helper_falls_back_when_first_exchange_yields_no_user_text(monkeypatch):
    """Fix B discriminator: first exchange unusable, latest exchange usable."""
    captured = {}
    monkeypatch.setattr(streaming, "_first_exchange_snippets", lambda msgs: ("", ""))
    monkeypatch.setattr(
        streaming, "_latest_exchange_snippets", lambda msgs: ("Latest question", "Latest answer")
    )
    title, status, _raw = _run_generation(monkeypatch, [{"role": "assistant", "content": "x"}], captured)
    assert status == "llm_aux"
    assert title == "LLM Title"
    assert captured["user_text"] == "Latest question"
    assert captured["assistant_text"] == "Latest answer"


def test_regenerate_helper_still_errors_when_neither_walker_yields_exchange(monkeypatch):
    """Original error path: both walkers unusable -> empty_user_message."""
    captured = {}
    monkeypatch.setattr(streaming, "_first_exchange_snippets", lambda msgs: ("", ""))
    monkeypatch.setattr(streaming, "_latest_exchange_snippets", lambda msgs: ("", ""))
    title, status, _raw = _run_generation(monkeypatch, [{"role": "assistant", "content": "x"}], captured)
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
