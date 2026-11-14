"""Re-gate 2026-10-21 at 35ec7c5f: fresh-response extraction guards (#7539).

Round-7 re-gate found two must-fix extraction defects at head 35ec7c5f. Both
reproductions below fail before the fix and pass after it:

1. A heading-prefixed reasoning sentence ("Analysis: I should not use …")
   reached the embedded-``"title"`` substring regex because the persisted-title
   predicate is no longer responsible for candidate shapes. Fresh responses
   now validate with the candidate-only predicates after thinking-markup
   cleanup, and the response status classifies invalid model output instead of
   empty content.
2. Thinking cleanup bypassed complete-object validation: a completed wrapper
   around an extra-key JSON object was saved. Complete containers in the
   visible payload (top-level, fenced, or in supported prose envelopes) are
   now validated under the schema contract (exactly ``{"title"}``), malformed
   containers and arrays fail closed, while bracket-label prose
   (``[2026] Login Repair``), valid prose, JSON-string titles and one-call
   wrapper recovery are preserved.
"""
import json
import threading
from contextlib import nullcontext
from unittest.mock import patch

import pytest
import api.models as models
import api.streaming as streaming
from api.models import Session
from tests._aux_client_helpers import auxiliary_client_modules, patch_tg_config
from tests.test_title_reasoning_trace_guard import _fixture_agent, _response

TRACE_SENTENCE = 'I should not use {"title": "Rejected Candidate"} for this conversation.'
HEADING_SENTENCES = (
    "Analysis: " + TRACE_SENTENCE,
    "Thinking: " + TRACE_SENTENCE,
    "Reasoning: " + TRACE_SENTENCE,
    "Thought: " + TRACE_SENTENCE,
)
WRAPPER_EXTRA_KEY = '<think>Choose a concise label.</think>\n{"title": "Rejected Candidate", "extra": "forbidden"}'
WRAPPED_VALID_CONTAINER = '<think>Choose a concise label.</think>\n{"title": "Mobile Login Repair"}'
DIGIT_LABEL_TITLES = ("[2026] Login Repair", "[3D] Printer Extruder Repair", "[v2.1] Release Notes Cleanup")
BROKEN_ARRAYS = ('[1, 2', '[-1, "x"', '["Login Flow"', '[{"title": "x"')


@pytest.fixture
def scene(tmp_path, monkeypatch):
    directory = tmp_path / "sessions"
    directory.mkdir()
    monkeypatch.setattr(models, "SESSION_DIR", directory)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", directory / "_index.json")
    session = Session(session_id="extraction-guards", title="Untitled", messages=[
        {"role": "user", "content": "Fix login."},
        {"role": "assistant", "content": "Inspect login."},
    ], llm_title_generated=False, manual_title=False)
    monkeypatch.setattr(streaming, "get_session", lambda sid: session)
    monkeypatch.setattr(streaming, "SESSIONS", {session.session_id: session})
    monkeypatch.setattr(streaming, "LOCK", threading.Lock())
    monkeypatch.setattr(streaming, "_aux_title_generation_enabled", lambda: True)
    monkeypatch.setattr("api.profiles.profile_env_for_background_worker", lambda *a, **k: nullcontext())
    monkeypatch.setattr("api.state_sync.sync_session_title", lambda *a, **k: None)
    return session, directory / (session.session_id + ".json")


# ── Finding 1: heading-prefixed reasoning sentences cannot supply the title ──

@pytest.mark.parametrize("content", HEADING_SENTENCES)
def test_extraction_rejects_heading_prefixed_trace_sentence(content):
    """The candidate-only heading rule applies to fresh responses before any
    embedded-title extraction; the caller falls through to the compatibility
    request instead of saving the embedded candidate."""
    assert streaming._extract_title_text(content, mode="compatibility") == ""


@pytest.mark.parametrize("aux", (True, False))
@pytest.mark.parametrize("content", HEADING_SENTENCES)
def test_extract_response_classifies_heading_traces_as_invalid_output(aux, content):
    """Invalid model output is surfaced as the llm_invalid_trace family, not
    merely empty content, so persistent traces keep the provisional title and
    the adaptive-refresh alternate route still triggers."""
    resp = _response(content)
    content_out, status = streaming._extract_title_response(resp, aux=aux, mode="compatibility")
    assert (content_out, status) == ("", "llm_invalid_trace_aux" if aux else "llm_invalid_trace")


@pytest.mark.parametrize("route", ("aux", "agent"))
@pytest.mark.parametrize("content", HEADING_SENTENCES + (WRAPPER_EXTRA_KEY,))
def test_caller_falls_through_to_compatibility_recovering_good_title(route, content):
    """Schema attempt on the trace/extra-key payload fails closed; the
    compatibility request (whose canned answer recovers) wins, like the
    predecessor head."""
    calls = []

    def respond(**kwargs):
        calls.append(kwargs)
        return _response(content if len(calls) == 1 else "Valid Recovery Title")

    if route == "aux":
        with auxiliary_client_modules(), patch_tg_config({"provider": "custom", "model": "fixture"}):
            with patch("agent.auxiliary_client.call_llm", side_effect=respond, create=True):
                result, status = streaming.generate_title_raw_via_aux("Fix login.", "Inspect login.")
        assert status == "llm_aux_retry"
    else:
        result, status = streaming.generate_title_raw_via_agent(
            _fixture_agent(respond), "Fix login.", "Inspect login.")
        assert status == "llm_retry"
    assert result == "Valid Recovery Title"
    assert len(calls) == 2


@pytest.mark.parametrize("route", ("aux", "agent"))
@pytest.mark.parametrize("content", HEADING_SENTENCES + (WRAPPER_EXTRA_KEY,))
def test_caller_persistent_trace_output_ends_invalid(route, content):
    """When every response is the trace/extra-key output, no title is returned
    and the status names invalid model output (no local fallback)."""
    calls = []

    def respond(**kwargs):
        calls.append(kwargs)
        return _response(content)

    if route == "aux":
        with auxiliary_client_modules(), patch_tg_config({"provider": "custom", "model": "fixture"}):
            with patch("agent.auxiliary_client.call_llm", side_effect=respond, create=True):
                result, status = streaming.generate_title_raw_via_aux("Fix login.", "Inspect login.")
        assert (result, status) == (None, "llm_invalid_trace_aux")
    else:
        result, status = streaming.generate_title_raw_via_agent(
            _fixture_agent(respond), "Fix login.", "Inspect login.")
        assert (result, status) == (None, "llm_invalid_trace")
    # prompt 1: schema + compatibility resend; prompt 2: compatibility only
    assert len(calls) == 3


# ── Finding 2: complete-container validation on the cleaned visible payload ──

def test_wrapped_extra_key_container_is_rejected_not_regex_extracted():
    """A completed thinking wrapper must not bypass container validation around
    an additionalProperties-violating object."""
    assert streaming._extract_title_text(WRAPPER_EXTRA_KEY, mode="compatibility") == ""


@pytest.mark.parametrize("content", (
    'Sure. {"title": "Unexpected Title", "extra": "forbidden"}',
    'Here is the JSON: {"title": "Unexpected Title", "extra": "forbidden"}',
    'Sure. {"title": "Login Flow',
    'Here is the JSON: [{"title": "Surprise One"}]',
    'Sure. [1, 2]',
))
def test_envelope_containers_fail_closed(content):
    """Supported prose envelopes validate their complete container under the
    schema contract; malformed containers and arrays never lose the wrapper
    around their extraction."""
    assert streaming._extract_title_text(content, mode="compatibility") == ""


@pytest.mark.parametrize("content", (
    'Here is the JSON: {"title": "Unexpected Title", "extra": "forbidden"}',
    'Sure. {"title": "Login Flow',
    'Here is the JSON: [{"title": "Surprise One"}]',
))
def test_schema_mode_still_fails_closed_on_wrapped_prose(content):
    assert streaming._extract_title_text(content, mode="schema") == ""


@pytest.mark.parametrize("content", DIGIT_LABEL_TITLES)
def test_digit_bracket_label_prose_is_a_title(content):
    """A closed bracket label followed by prose is not malformed-JSON evidence
    (maintainer fold 2026-10-21)."""
    assert streaming._extract_title_text(content, mode="compatibility") == content


@pytest.mark.parametrize("content", BROKEN_ARRAYS)
def test_broken_json_arrays_still_fail_closed(content):
    assert streaming._extract_title_text(content, mode="compatibility") == ""


def test_wrapped_valid_container_saves_title_in_one_call():
    """One-call recovery after genuine completed thinking metadata is
    preserved."""
    assert streaming._extract_title_text(WRAPPED_VALID_CONTAINER, mode="compatibility") == "Mobile Login Repair"


def test_compatibility_extract_keeps_bracket_prose():
    assert streaming._extract_title_text("[Bug] Login Fails On Submit", mode="compatibility") == (
        "[Bug] Login Fails On Submit"
    )


def test_plain_valid_container_still_validates_exactly_once():
    assert streaming._extract_title_text('{"title": "Mobile Login Repair"}', mode="compatibility") == (
        "Mobile Login Repair"
    )


# ── Fold-equivalent guards: newline title menus and thinking-strip modes ────

@pytest.mark.parametrize("content", (
    "Title options:\nMobile Login Repair\nLogin Button Fix",
    "Here are some title suggestions:\nMobile Login Repair\nLogin Flow Fix",
))
def test_extraction_rejects_newline_title_menus(content):
    """Thinking-markup cleanup keeps line breaks so the option-menu detector
    still sees the candidate boundaries (maintainer fold 2026-10-21)."""
    assert streaming._extract_title_text(content, mode="compatibility") == ""


def test_strip_thinking_markup_collapses_whitespace_by_default():
    assert streaming._strip_thinking_markup("<think>x</think>\nA\n\nB") == "A B"
    assert streaming._strip_thinking_markup("<think>x</think>\nA\nB", collapse_whitespace=False) == "A\nB"


# ── Worker-level regressions for the same matrix ────────────────────────────

@pytest.mark.parametrize("route", ("aux", "agent"))
@pytest.mark.parametrize("content", HEADING_SENTENCES + (WRAPPER_EXTRA_KEY,))
def test_worker_all_trace_output_keeps_provisional_title_and_no_event(scene, monkeypatch, route, content):
    session, path = scene
    monkeypatch.setattr(streaming, "_aux_title_configured", lambda: route == "aux")
    calls = []

    def respond(**kwargs):
        calls.append(kwargs)
        return _response(content)

    agent = _fixture_agent(respond) if route == "agent" else None
    events = []
    with auxiliary_client_modules(), patch_tg_config({"provider": "custom", "model": "fixture"}):
        with patch("agent.auxiliary_client.call_llm", side_effect=respond, create=True):
            streaming._run_background_title_update(
                session.session_id, "Fix login.", "Inspect login.", "Untitled",
                lambda name, data: events.append((name, data)), agent=agent)
    assert not any(name == "title" for name, _ in events)
    assert session.title == "Untitled"
    assert not getattr(session, "llm_title_generated", False)
    if path.exists():
        saved = json.loads(path.read_text(encoding="utf-8"))
        assert saved["title"] == "Untitled"
        assert not saved.get("llm_title_generated")
    reasons = [str(data.get("reason") or "") for name, data in events if name == "title_status"]
    assert any(r.startswith("llm_invalid_trace") for r in reasons), reasons


@pytest.mark.parametrize("route", ("aux", "agent"))
@pytest.mark.parametrize("content", HEADING_SENTENCES + (WRAPPER_EXTRA_KEY,))
def test_worker_recovers_good_title_after_trace_schema_attempt(scene, monkeypatch, route, content):
    """Durable title/flag/publication for the compatibility recovery, on both
    routes, for the heading-trace and wrapper extra-key payloads."""
    session, path = scene
    monkeypatch.setattr(streaming, "_aux_title_configured", lambda: route == "aux")
    calls = []

    def respond(**kwargs):
        calls.append(kwargs)
        return _response(content if len(calls) == 1 else "Valid Recovery Title")

    agent = _fixture_agent(respond) if route == "agent" else None
    events = []
    with auxiliary_client_modules(), patch_tg_config({"provider": "custom", "model": "fixture"}):
        with patch("agent.auxiliary_client.call_llm", side_effect=respond, create=True):
            streaming._run_background_title_update(
                session.session_id, "Fix login.", "Inspect login.", "Untitled",
                lambda name, data: events.append((name, data)), agent=agent)
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert saved["title"] == "Valid Recovery Title"
    assert saved["llm_title_generated"] is True
    assert len(calls) == 2
    assert [data["title"] for name, data in events if name == "title"] == ["Valid Recovery Title"]


@pytest.mark.parametrize("primary", ("aux", "agent"))
@pytest.mark.parametrize("content", HEADING_SENTENCES + (WRAPPER_EXTRA_KEY,))
def test_refresh_persistent_trace_reaches_alternate_route(scene, monkeypatch, primary, content):
    session, path = scene
    session.title = "Existing Title"
    session.llm_title_generated = True
    session.save()
    monkeypatch.setattr(streaming, "_aux_title_configured", lambda: primary == "aux")
    calls = []

    def respond(route, **kwargs):
        calls.append(route)
        return _response(content)

    agent = _fixture_agent(lambda **kw: respond("agent", **kw))
    events = []
    with auxiliary_client_modules(), patch_tg_config({"provider": "custom", "model": "fixture"}):
        with patch("agent.auxiliary_client.call_llm", side_effect=lambda **kw: respond("aux", **kw), create=True):
            streaming._run_background_title_refresh(
                session.session_id, "Fix login.", "Inspect login.", "Existing Title",
                lambda name, data: events.append((name, data)), agent=agent)
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert saved["title"] == "Existing Title"
    assert not any(name == "title" for name, _ in events)
    statuses = [str(data.get("status") or "") for name, data in events if name == "title_status"]
    assert "refresh_skipped" in statuses, statuses


@pytest.mark.parametrize("primary", ("aux", "agent"))
@pytest.mark.parametrize("content", HEADING_SENTENCES + (WRAPPER_EXTRA_KEY,))
def test_refresh_recovers_alternate_route_title_after_trace(scene, monkeypatch, primary, content):
    session, path = scene
    session.title = "Existing Title"
    session.llm_title_generated = True
    session.save()
    monkeypatch.setattr(streaming, "_aux_title_configured", lambda: primary == "aux")
    calls = []

    def respond(route, **kwargs):
        calls.append(route)
        return _response(content if route == primary else "Recovered Refresh Title")

    agent = _fixture_agent(lambda **kw: respond("agent", **kw))
    events = []
    with auxiliary_client_modules(), patch_tg_config({"provider": "custom", "model": "fixture"}):
        with patch("agent.auxiliary_client.call_llm", side_effect=lambda **kw: respond("aux", **kw), create=True):
            streaming._run_background_title_refresh(
                session.session_id, "Fix login.", "Inspect login.", "Existing Title",
                lambda name, data: events.append((name, data)), agent=agent)
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert saved["title"] == "Recovered Refresh Title"
    assert saved["llm_title_generated"] is True
    assert [data["title"] for name, data in events if name == "title"] == ["Recovered Refresh Title"]
    assert any(
        data.get("status") == "refreshed"
        for name, data in events if name == "title_status"
    )


@pytest.mark.parametrize("content", (
    "Fix [1, 2] Parsing Bug", "Handle {} In Config Parser",
    'Parse {"a": 1} Payload Example', "Login Repair [2026]",
    "Fix array[0], Index Error", "Debug [] Empty List Handling",
    "Compare [1,2,3] vs (1,2,3)", 'Explain {"ok": true} Responses',
    "Maybe Monad Error Handling", "Options: Portfolio Optimization",
    "Topic Label Accessibility", "Options: Stocks, Bonds, ETFs",
    "Maybe Later Feature Flags",
))
def test_ordinary_subject_and_json_literal_prose_survive(content):
    assert streaming._extract_title_text(content, mode="compatibility") == content


@pytest.mark.parametrize("content", (
    'Here is the JSON: {"result": {"title": "Nested Pick"}',
    'Here is the JSON: [{"title": "Nested Pick"',
    'Here is the JSON: {"title": "Rejected Candidate"',
))
def test_malformed_outer_envelope_fails_closed(content):
    assert streaming._extract_title_text(content, mode="compatibility") == ""


@pytest.mark.parametrize("content", (
    'Title: {"title": "Rejected Candidate"}',
    '{"title": "Rejected Candidate"} Done',
    '{"title": "Rejected", "extra": "forbidden"} Parser Notes',
    '["Option A", "Option B"] Title Ideas',
    '```json\n{"title": "Rejected Candidate"}\n```\nDone',
    "Here's the JSON: {\"title\": \"Rejected Candidate\"",
    'Sure thing! {"result": {"title": "Nested Pick"}',
    'Title: ["Rejected Candidate", "Other Option"]',
    '**Title:** {"name": "Rejected Candidate"}',
    "Title: {'title': 'Rejected Candidate'}",
    'Title: {‘title’: ‘Rejected Candidate’}',
    '"{\\"title\\": \\"Rejected Candidate\\"}"',
    '{"title": "{\\"title\\": \\"Rejected Candidate\\"}"}',
    '{"title": "[\\"Option A\\", \\"Option B\\"]"}',
))
def test_prose_carrying_title_payload_fails_closed(content):
    """Prose returned verbatim may not smuggle a payload that boundary
    validation did not accept (title key, unclosed opener, leading
    string-bearing container followed by prose)."""
    assert streaming._extract_title_text(content, mode="compatibility") == ""


@pytest.mark.parametrize("content", (
    "Support {name} Placeholders", "Python list[str] Typing Help",
    'Use ["a", "b"] As Default List', "{} Config Parser Guide",
    "[1, 2] Parsing Tutorial", "[Bug] Login Fails On Submit",
    "Rename 'title' Field In Django", "Title Case In APA Style",
))
def test_literal_containers_are_not_payload_evidence(content):
    assert not streaming._has_title_payload_evidence(content)
    assert streaming._extract_title_text(content, mode="compatibility") == content
