"""Regression coverage for #7081: settled `done` redaction scales with the delta.

The `done` SSE payload keeps the FULL redacted transcript (the guard suite in
test_streaming_done_payload_message_count.py locks the source contract), but
the redaction work must scale with the messages added since the previous
settled turn, not with the whole transcript — otherwise a long tool-heavy
session blocks unrelated WebUI requests for 10s-100s of seconds while the full
transcript is re-redacted on every turn (#7081).

These tests drive the real settled-payload path
(``_session_payload_with_full_messages`` -> ``_redact_settled_session_payload``)
with fake sessions and count how many messages each pass actually redacts.
"""

from types import SimpleNamespace

import pytest

from api.helpers import _public_message_projection, redact_session_data
from api.streaming import (
    _DONE_REDACTION_CACHE,
    _DONE_REDACTION_CACHE_MAX,
    _DONE_REDACTION_LOCK,
    _DONE_REDACTION_CACHE_ORDER,
    _redact_settled_session_payload,
    _session_payload_with_full_messages,
)


def _message(role, content, *, ts=0, extra=None):
    message = {"role": role, "content": content, "timestamp": ts}
    if extra:
        message.update(extra)
    return message


def _session(messages, *, session_id="child-session", profile="test", **compact_extra):
    """Fake Session with a compact() mirroring the real metadata-only view."""
    return SimpleNamespace(
        session_id=session_id,
        profile=profile,
        messages=messages,
        compact=lambda: {
            "session_id": session_id,
            "title": "stale compact metadata",
            "message_count": 999,  # deliberately stale; helper must override it
            "profile": profile,
            **compact_extra,
        },
    )


@pytest.fixture(autouse=True)
def _clear_done_redaction_cache():
    with _DONE_REDACTION_LOCK:
        _DONE_REDACTION_CACHE.clear()
        _DONE_REDACTION_CACHE_ORDER.clear()
    yield
    with _DONE_REDACTION_LOCK:
        _DONE_REDACTION_CACHE.clear()
        _DONE_REDACTION_CACHE_ORDER.clear()


def _counting_projection(monkeypatch):
    """Wrap _public_message_projection to count per-message redactions."""
    calls = {"n": 0}
    real = _public_message_projection

    def counting(*args, **kwargs):
        calls["n"] += 1
        return real(*args, **kwargs)

    monkeypatch.setattr("api.helpers._public_message_projection", counting)
    return calls


def test_done_payload_keeps_full_transcript_and_real_count():
    messages = [
        _message("user", "first", ts=1),
        _message("assistant", "reply", ts=2),
        _message("user", "second", ts=3),
    ]
    session = _session(messages)
    raw = _session_payload_with_full_messages(session, tool_calls=[])

    payload = _redact_settled_session_payload(raw, session)

    # The settled payload must embed the full transcript with a matching count,
    # NOT the stale metadata-only count from compact() (the original #7081 fix
    # regressed exactly this by switching to s.compact()).
    assert payload["message_count"] == 3
    assert len(payload["messages"]) == 3
    assert payload["messages"] == messages
    assert payload["title"] == "stale compact metadata"


def test_incremental_output_matches_full_redaction():
    messages = [_message("user", f"q{i}", ts=i) for i in range(20)]
    messages += [_message("assistant", "api key sk-1234567890abcdef", ts=100)]
    session = _session(messages)
    raw = _session_payload_with_full_messages(session, tool_calls=[])

    result = _redact_settled_session_payload(raw, session)

    assert result == redact_session_data(raw)
    # The secret must actually be masked in the settled payload.
    assert "sk-1234567890abcdef" not in str(result["messages"])


def test_second_done_redacts_only_the_delta(monkeypatch):
    """The #7081 blocking regression: redaction must not re-walk the transcript.

    First settled turn redacts every message; the second turn, which appends
    only a handful of messages, must redact only those — the payload still
    carries the full transcript (parity with a full pass).
    """
    calls = _counting_projection(monkeypatch)

    base = [_message("user", f"q{i}", ts=i) for i in range(50)]
    session = _session(base)
    raw = _session_payload_with_full_messages(session, tool_calls=[])
    first = _redact_settled_session_payload(raw, session)
    assert calls["n"] == 50  # cold pass: whole transcript
    assert first == redact_session_data(raw)

    # Second turn: 3 new messages appended to the same session.
    grown = base + [
        _message("assistant", "a1", ts=100),
        _message("tool", "t1", ts=101),
        _message("assistant", "a2", ts=102),
    ]
    session2 = _session(grown)
    raw2 = _session_payload_with_full_messages(session2, tool_calls=[])
    calls["n"] = 0
    second = _redact_settled_session_payload(raw2, session2)

    assert calls["n"] == 3  # only the delta was redacted
    assert second == redact_session_data(raw2)  # same output as a full pass
    assert len(second["messages"]) == 53
    assert second["message_count"] == 53


def test_boundary_change_falls_back_to_full_redaction(monkeypatch):
    calls = _counting_projection(monkeypatch)

    base = [_message("user", f"q{i}", ts=i) for i in range(20)]
    session = _session(base)
    raw = _session_payload_with_full_messages(session, tool_calls=[])
    _redact_settled_session_payload(raw, session)
    assert calls["n"] == 20

    # The boundary message of the cached prefix was edited (as compaction /
    # retry / external reload can do) -> the prefix is no longer provably
    # unchanged, so the next pass must re-redact everything.
    mutated = base[:]
    mutated[-1] = _message("user", "EDITED-BOUNDARY", ts=19)
    session2 = _session(mutated)
    raw2 = _session_payload_with_full_messages(session2, tool_calls=[])
    calls["n"] = 0
    result = _redact_settled_session_payload(raw2, session2)

    assert calls["n"] == 20  # full re-redaction, no stale unredacted prefix
    assert result == redact_session_data(raw2)
    assert result["messages"][-1]["content"] == "EDITED-BOUNDARY"


def test_retry_truncation_falls_back_to_full_redaction(monkeypatch):
    calls = _counting_projection(monkeypatch)

    base = [_message("user", f"q{i}", ts=i) for i in range(20)]
    session = _session(base)
    raw = _session_payload_with_full_messages(session, tool_calls=[])
    _redact_settled_session_payload(raw, session)
    assert calls["n"] == 20

    # /api/session/retry truncates the tail before re-running: the transcript
    # shrank, so the cached prefix no longer applies.
    truncated = base[:10]
    session2 = _session(truncated)
    raw2 = _session_payload_with_full_messages(session2, tool_calls=[])
    calls["n"] = 0
    result = _redact_settled_session_payload(raw2, session2)

    assert calls["n"] == 10
    assert result == redact_session_data(raw2)
    assert len(result["messages"]) == 10


def test_active_turn_token_change_falls_back_to_full_redaction(monkeypatch):
    calls = _counting_projection(monkeypatch)

    base = [_message("user", f"q{i}", ts=i) for i in range(10)]
    session = _session(base)
    raw = _session_payload_with_full_messages(session, tool_calls=[])
    _redact_settled_session_payload(raw, session)
    assert calls["n"] == 10

    # Same messages, but the payload now carries an active-stream token; the
    # redaction output depends on it, so the cache must not be reused.
    session2 = _session(
        base,
        active_stream_id="stream-2",
        pending_started_at=123.0,
    )
    raw2 = _session_payload_with_full_messages(session2, tool_calls=[])
    calls["n"] = 0
    result = _redact_settled_session_payload(raw2, session2)

    assert calls["n"] == 10  # token mismatch -> full pass
    assert result == redact_session_data(raw2)


def test_redaction_toggle_never_reuses_prefix_from_other_settings(monkeypatch):
    """A mid-session api_redact_enabled toggle must not reuse a cached prefix.

    The dangerous direction: the prefix is cached while redaction is DISABLED
    (raw content), then redaction is re-enabled. Reusing that raw prefix would
    leak unredacted credentials into the done payload, so the next pass must
    re-redact the whole transcript under the new setting.
    """
    calls = _counting_projection(monkeypatch)

    settings = {"api_redact_enabled": False}
    monkeypatch.setattr("api.config.load_settings", lambda: settings)

    base = [_message("user", f"q{i}", ts=i) for i in range(20)]
    base.append(_message("assistant", "secret sk-1234567890abcdef", ts=20))
    session = _session(base)
    raw = _session_payload_with_full_messages(session, tool_calls=[])
    first = _redact_settled_session_payload(raw, session)
    assert calls["n"] == 21  # cold pass; redaction disabled -> raw cached prefix
    assert "sk-1234567890abcdef" in str(first["messages"])

    # Redaction re-enabled mid-session: the cached prefix was produced under
    # api_redact_enabled=False, so it must NOT be reused.
    settings["api_redact_enabled"] = True
    grown = base + [_message("assistant", "next", ts=21)]
    session2 = _session(grown)
    raw2 = _session_payload_with_full_messages(session2, tool_calls=[])
    calls["n"] = 0
    second = _redact_settled_session_payload(raw2, session2)

    assert calls["n"] == 22  # full pass under the new setting, not a delta reuse
    assert second == redact_session_data(raw2)
    assert "sk-1234567890abcdef" not in str(second["messages"])  # no raw leak


def test_cache_stays_bounded_across_sessions():
    for idx in range(_DONE_REDACTION_CACHE_MAX + 4):
        session = _session(
            [_message("user", f"s{idx}-q{i}", ts=i) for i in range(5)],
            session_id=f"session-{idx}",
        )
        raw = _session_payload_with_full_messages(session, tool_calls=[])
        _redact_settled_session_payload(raw, session)

    with _DONE_REDACTION_LOCK:
        assert len(_DONE_REDACTION_CACHE) <= _DONE_REDACTION_CACHE_MAX
        assert len(_DONE_REDACTION_CACHE_ORDER) <= _DONE_REDACTION_CACHE_MAX
        assert len(_DONE_REDACTION_CACHE) == len(_DONE_REDACTION_CACHE_ORDER)
