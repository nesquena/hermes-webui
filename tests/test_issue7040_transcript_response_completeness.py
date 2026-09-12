"""#7040: the public session response must stay byte-complete.

The bounded-transcript design (docs/superpowers/specs/2026-08-14-wkwebview-
transcript-layout-design.md) makes DOM/display projection the *only* place a
transcript may be abbreviated. Its Goal and Scope are explicit: "The WebUI must
retain the complete API response and canonical ``S.messages`` values" and "No
... API response mutation, server transcript mutation ... is part of this
change." The implementation plan repeats it as a Global Constraint: "Do not
mutate API responses, ``S.messages``, persisted recovery state, or server
transcript data."

A previous revision of this PR added a ``_PUBLIC_TRANSCRIPT_TEXT_LIMIT`` head/
tail excerpt to ``_redact_text()``. Because ``redact_session_data()`` routes
``messages``, ``context_messages`` and tool calls through that function, the
browser stopped receiving the canonical value, so Copy/edit/regenerate/fork/
export and later UI recovery could not recover the omitted middle.

These tests pin the invariant at the server boundary so the bound can only ever
live at the display seam.
"""

from __future__ import annotations

import pytest

from api.helpers import redact_session_data


# Comfortably past the old 65_536 limit *and* its 2x (131_072) branch point, so
# a reintroduced head/tail excerpt cannot slip through on a boundary case.
_HUGE = 400_000


def _benign_blob(size: int) -> str:
    # Deliberately free of every marker in _SENSITIVE_CASE_MARKERS /
    # _SENSITIVE_LOWER_MARKERS so nothing here is legitimately maskable.
    unit = "The quick brown fox jumps over the lazy dog. "
    return (unit * (size // len(unit) + 1))[:size]


@pytest.fixture(autouse=True)
def _force_redaction_on(monkeypatch):
    """Pin api_redact_enabled=True — the defect only reproduces when on."""
    import api.config

    monkeypatch.setattr(
        api.config, "load_settings", lambda: {"api_redact_enabled": True}
    )


@pytest.mark.parametrize(
    "size",
    [
        65_537,   # just past the old limit
        100_000,  # inside the old "omitted <= 0" carve-out
        131_072,  # exactly 2x the old limit
        131_073,  # first size the old code actually truncated
        _HUGE,
    ],
)
def test_large_message_content_survives_redaction_byte_complete(size):
    blob = _benign_blob(size)
    out = redact_session_data(
        {"session_id": "s1", "messages": [{"role": "assistant", "content": blob}]}
    )
    got = out["messages"][0]["content"]
    assert got == blob, (
        f"size={size}: content must be byte-complete "
        f"(got {len(got)} chars, expected {len(blob)})"
    )
    assert "characters omitted" not in got


def test_large_context_message_and_tool_call_survive_byte_complete():
    blob = _benign_blob(_HUGE)
    out = redact_session_data(
        {
            "session_id": "s1",
            "messages": [{"role": "assistant", "content": blob}],
            "context_messages": [{"role": "user", "content": blob}],
            "tool_calls": [
                {
                    "id": "t1",
                    "function": {"name": "read", "arguments": blob},
                }
            ],
        }
    )
    assert out["messages"][0]["content"] == blob
    assert out["context_messages"][0]["content"] == blob
    assert out["tool_calls"][0]["function"]["arguments"] == blob


def test_redaction_stays_semantically_complete_in_a_huge_message():
    """Removing the length bound must not weaken masking.

    The secret is planted *past* the old head excerpt and *before* the old tail
    excerpt — i.e. exactly in the middle the old code dropped — so this fails
    both if redaction regresses and if the excerpting is reintroduced.
    """
    secret = "ghp_" + ("a" * 36)
    half = _benign_blob(_HUGE // 2)
    # Space-delimit the secret: the redactor's patterns are word-boundary
    # anchored, so a blob whose split happens to land mid-word would leave it
    # unmasked for reasons unrelated to this test's subject.
    blob = half + " " + secret + " " + half
    out = redact_session_data(
        {"session_id": "s1", "messages": [{"role": "assistant", "content": blob}]}
    )
    got = out["messages"][0]["content"]
    assert secret not in got, "credential in the middle must still be masked"
    assert "characters omitted" not in got, "must not be excerpted"
    # Everything except the credential is preserved.
    assert got.startswith(half)
    assert got.endswith(half)
    assert len(got) >= len(half) * 2
