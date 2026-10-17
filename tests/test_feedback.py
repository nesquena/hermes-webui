"""Tests for POST /api/feedback."""

from __future__ import annotations

import json
from unittest.mock import MagicMock
from urllib.parse import urlparse

import api.feedback as feedback
import api.routes as routes


def _post(path: str, body: dict, monkeypatch):
    cap = {}

    def _j(_handler, payload, *_, status=200, **__):
        cap["ok"] = payload
        cap["status"] = status
        return True

    def _bad(_handler, msg, status=400, **__):
        cap["bad"] = (msg, status)
        return True

    handler = MagicMock()
    handler.command = "POST"
    handler.headers = {}

    monkeypatch.setattr(routes, "read_body", lambda _h: body)
    monkeypatch.setattr(routes, "_check_csrf", lambda _h: True)
    monkeypatch.setattr(routes, "_csrf_exempt_path", lambda _p: False)
    monkeypatch.setattr(routes, "_guard_request_session_visibility", lambda *_a, **_k: True)
    monkeypatch.setattr(routes, "j", _j)
    monkeypatch.setattr(routes, "bad", _bad)

    routes.handle_post(handler, urlparse(path))
    return cap


def test_normalize_feedback_requires_session_and_target():
    try:
        feedback.normalize_feedback_payload({"rating": "up"}, validate_session=False)
        assert False, "expected FeedbackValidationError"
    except feedback.FeedbackValidationError as exc:
        assert "session_id" in str(exc)

    try:
        feedback.normalize_feedback_payload(
            {"session_id": "abc", "rating": "up"}, validate_session=False
        )
        assert False, "expected FeedbackValidationError"
    except feedback.FeedbackValidationError as exc:
        assert "message_id or index" in str(exc)


def test_normalize_feedback_rejects_bad_rating_and_reason():
    try:
        feedback.normalize_feedback_payload(
            {"session_id": "abc", "index": 1, "rating": "meh"},
            validate_session=False,
        )
        assert False, "expected FeedbackValidationError"
    except feedback.FeedbackValidationError:
        pass

    try:
        feedback.normalize_feedback_payload(
            {
                "session_id": "abc",
                "index": 1,
                "rating": "down",
                "reason": "spam",
            },
            validate_session=False,
        )
        assert False, "expected FeedbackValidationError"
    except feedback.FeedbackValidationError:
        pass


def test_normalize_feedback_accepts_valid_down_with_reason():
    record = feedback.normalize_feedback_payload(
        {
            "session_id": "sess1",
            "index": 3,
            "rating": "down",
            "reason": "Not helpful",
            "model": "gpt-test",
            "mode": "ask",
        },
        validate_session=False,
    )
    assert record["session_id"] == "sess1"
    assert record["index"] == 3
    assert record["rating"] == "down"
    assert record["reason"] == "not_helpful"
    assert record["model"] == "gpt-test"
    assert record["mode"] == "ask"
    assert record["profile"]
    assert "ts" in record


def test_append_feedback_writes_jsonl(tmp_path, monkeypatch):
    monkeypatch.setattr(feedback, "_profile_state_dir", lambda: tmp_path)
    record = feedback.normalize_feedback_payload(
        {"session_id": "s1", "message_id": "m1", "rating": "up"},
        validate_session=False,
    )
    feedback.append_feedback(record)
    target = tmp_path / "feedback.jsonl"
    lines = target.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    parsed = json.loads(lines[0])
    assert parsed["rating"] == "up"
    assert parsed["message_id"] == "m1"
    assert "profile" in parsed


def test_post_feedback_persists_and_returns_ok(tmp_path, monkeypatch):
    monkeypatch.setattr(feedback, "_profile_state_dir", lambda: tmp_path)
    monkeypatch.setattr(
        feedback,
        "_assert_session_exists",
        lambda _sid: None,
    )
    monkeypatch.setattr(feedback, "_assert_message_target", lambda *_a, **_k: None)
    monkeypatch.setattr(feedback, "_RATE_HITS", {})

    cap = _post(
        "/api/feedback",
        {
            "session_id": "sess-a",
            "index": 2,
            "rating": "down",
            "reason": "too_long",
            "model": "m1",
            "mode": "build",
        },
        monkeypatch,
    )
    assert "bad" not in cap
    assert cap.get("ok", {}).get("ok") is True
    target = tmp_path / "feedback.jsonl"
    assert target.exists()
    row = json.loads(target.read_text(encoding="utf-8").strip())
    assert row["session_id"] == "sess-a"
    assert row["reason"] == "too_long"
    assert row["profile"]


def test_post_feedback_fails_closed_on_bad_input(monkeypatch):
    monkeypatch.setattr(feedback, "_assert_session_exists", lambda _sid: None)
    cap = _post(
        "/api/feedback",
        {"session_id": "x", "rating": "up"},
        monkeypatch,
    )
    assert "ok" not in cap
    assert cap["bad"][1] == 400


def test_feedback_rejects_missing_session(monkeypatch):
    def _missing(_sid):
        raise feedback.FeedbackValidationError("session not found")

    monkeypatch.setattr(feedback, "_assert_session_exists", _missing)
    try:
        feedback.normalize_feedback_payload(
            {"session_id": "nope", "index": 0, "rating": "up"}
        )
        assert False, "expected FeedbackValidationError"
    except feedback.FeedbackValidationError as exc:
        assert "session not found" in str(exc)


def test_feedback_rejects_profile_mismatch(monkeypatch):
    monkeypatch.setattr(feedback, "_active_profile_name", lambda: "default")
    monkeypatch.setattr(feedback, "_assert_session_exists", lambda _sid: None)
    monkeypatch.setattr(feedback, "_assert_message_target", lambda *_a, **_k: None)
    try:
        feedback.normalize_feedback_payload(
            {
                "session_id": "s1",
                "index": 0,
                "rating": "up",
                "profile": "other",
            }
        )
        assert False, "expected FeedbackValidationError"
    except feedback.FeedbackValidationError as exc:
        assert "profile" in str(exc).lower()


def test_feedback_rejects_invalid_message_target(monkeypatch):
    class _Session:
        messages = [{"role": "user", "content": "hi", "id": "msg-1"}]

    monkeypatch.setattr(feedback, "_assert_session_exists", lambda _sid: None)
    monkeypatch.setattr(
        "api.models.get_session",
        lambda _sid, metadata_only=False: _Session(),
    )
    try:
        feedback.normalize_feedback_payload(
            {"session_id": "s1", "message_id": "missing", "rating": "up"}
        )
        assert False, "expected FeedbackValidationError"
    except feedback.FeedbackValidationError as exc:
        assert "message_id" in str(exc).lower()


def test_feedback_rate_limit_and_size_cap(tmp_path, monkeypatch):
    monkeypatch.setattr(feedback, "_profile_state_dir", lambda: tmp_path)
    monkeypatch.setattr(feedback, "_RATE_HITS", {})
    monkeypatch.setattr(feedback, "_RATE_LIMIT_MAX", 2)
    monkeypatch.setattr(feedback, "_MAX_FEEDBACK_FILE_BYTES", 80)

    assert feedback.feedback_rate_limited("s1") is False
    feedback.feedback_record_rate_hit("s1")
    assert feedback.feedback_rate_limited("s1") is False
    feedback.feedback_record_rate_hit("s1")
    assert feedback.feedback_rate_limited("s1") is True

    record = feedback.normalize_feedback_payload(
        {"session_id": "s1", "message_id": "m1", "rating": "up", "profile": "default"},
        validate_session=False,
    )
    # Fill past the size cap with repeated writes.
    try:
        for _ in range(20):
            feedback.append_feedback(record)
        assert False, "expected size-cap FeedbackValidationError"
    except feedback.FeedbackValidationError as exc:
        assert "full" in str(exc).lower()
