"""Regression tests for issue #5334.

The Hermes Agent runs an internal verify-before-finish loop. When it fires, it
appends a synthetic assistant "premature done" answer plus a synthetic ``user``
nudge whose text looks like::

    [System: You edited code in this turn, but the workspace does not have fresh
     passing verification evidence yet...]

Those turns exist only to drive the loop for one more API round-trip. The agent
flags each with a structured marker (``_verification_stop_synthetic`` for the
verify-on-stop path, ``_pre_verify_synthetic`` for the plugin ``pre_verify``
path) and keeps them out of its own durable store. WebUI never honored those
markers, so the nudge leaked into the visible transcript — the same class of
internal-control-message leak as #3320/#3821/#4373/#4875.

These tests pin the contract that a marker-flagged synthetic turn is dropped
from the merged display transcript while a normal user message is preserved.
"""

from api import streaming


VERIFICATION_STOP_NUDGE = (
    "[System: You edited code in this turn, but the workspace does not have "
    "fresh passing verification evidence yet.\n\n"
    "Verification status: unverified\n\n"
    "Changed paths:\n- `app/foo.py`\n\n"
    "Run the relevant verification command now.]"
)


# --------------------------------------------------------------------------- #
# Marker classifier + drop helper
# --------------------------------------------------------------------------- #
def test_verification_stop_synthetic_message_is_classified_as_control():
    msg = {
        "role": "user",
        "content": VERIFICATION_STOP_NUDGE,
        "_verification_stop_synthetic": True,
    }
    assert streaming._is_synthetic_control_message(msg) is True


def test_pre_verify_synthetic_message_is_classified_as_control():
    msg = {
        "role": "user",
        "content": "[System: run tests]",
        "_pre_verify_synthetic": True,
    }
    assert streaming._is_synthetic_control_message(msg) is True


def test_normal_user_message_is_not_classified_as_control():
    assert streaming._is_synthetic_control_message(
        {"role": "user", "content": "Please refactor the parser."}
    ) is False
    # A message that merely quotes the nudge text but lacks the marker is a real
    # message and must NOT be dropped (marker-based, not string-based, filter).
    assert streaming._is_synthetic_control_message(
        {"role": "user", "content": VERIFICATION_STOP_NUDGE}
    ) is False
    assert streaming._is_synthetic_control_message("not a dict") is False


def test_drop_synthetic_control_messages_filters_only_flagged_rows():
    messages = [
        {"role": "user", "content": "Fix the bug."},
        {"role": "assistant", "content": "premature done", "_verification_stop_synthetic": True},
        {"role": "user", "content": VERIFICATION_STOP_NUDGE, "_verification_stop_synthetic": True},
        {"role": "assistant", "content": "Fixed and verified."},
    ]
    cleaned = streaming._drop_synthetic_control_messages(messages)
    contents = [m.get("content") for m in cleaned]
    assert contents == ["Fix the bug.", "Fixed and verified."]
    assert all(
        not m.get("_verification_stop_synthetic") and not m.get("_pre_verify_synthetic")
        for m in cleaned
    )


# --------------------------------------------------------------------------- #
# End-to-end: the display-merge chokepoint that builds the visible transcript
# --------------------------------------------------------------------------- #
def test_verification_stop_nudge_never_appears_in_rendered_transcript():
    """The synthetic verify-stop turns must not survive display settlement."""
    previous_display = [{"role": "user", "content": "Fix the bug."}]
    previous_context = [{"role": "user", "content": "Fix the bug."}]
    result_messages = [
        {"role": "user", "content": "Fix the bug."},
        {"role": "assistant", "content": "premature done", "_verification_stop_synthetic": True},
        {"role": "user", "content": VERIFICATION_STOP_NUDGE, "_verification_stop_synthetic": True},
        {"role": "assistant", "content": "Ran tests; all green. Fixed."},
    ]

    merged = streaming._merge_display_messages_after_agent_result(
        previous_display,
        previous_context,
        result_messages,
        "Fix the bug.",
    )

    # The internal nudge text must never reach the rendered transcript.
    assert all(VERIFICATION_STOP_NUDGE not in str(m.get("content") or "") for m in merged)
    # No leftover synthetic scaffolding rows.
    assert all(not streaming._is_synthetic_control_message(m) for m in merged)
    # The real turn is preserved end to end.
    contents = [m.get("content") for m in merged]
    assert contents == ["Fix the bug.", "Ran tests; all green. Fixed."]


def test_pre_verify_nudge_never_appears_in_rendered_transcript():
    previous_display = [{"role": "user", "content": "Ship the feature."}]
    previous_context = [{"role": "user", "content": "Ship the feature."}]
    result_messages = [
        {"role": "user", "content": "Ship the feature."},
        {"role": "assistant", "content": "premature done", "_pre_verify_synthetic": True},
        {"role": "user", "content": "[System: run tests]", "_pre_verify_synthetic": True},
        {"role": "assistant", "content": "Verified and clean."},
    ]

    merged = streaming._merge_display_messages_after_agent_result(
        previous_display,
        previous_context,
        result_messages,
        "Ship the feature.",
    )

    assert all(not streaming._is_synthetic_control_message(m) for m in merged)
    contents = [m.get("content") for m in merged]
    assert contents == ["Ship the feature.", "Verified and clean."]


def test_normal_user_message_is_not_excluded_from_transcript():
    """A regular user turn (no marker) must still render, even mid-transcript."""
    previous_display = [
        {"role": "user", "content": "First question."},
        {"role": "assistant", "content": "First answer."},
    ]
    previous_context = [
        {"role": "user", "content": "First question."},
        {"role": "assistant", "content": "First answer."},
    ]
    result_messages = [
        {"role": "user", "content": "First question."},
        {"role": "assistant", "content": "First answer."},
        {"role": "user", "content": "Second question."},
        {"role": "assistant", "content": "Second answer."},
    ]

    merged = streaming._merge_display_messages_after_agent_result(
        previous_display,
        previous_context,
        result_messages,
        "Second question.",
    )

    contents = [m.get("content") for m in merged]
    assert contents == [
        "First question.",
        "First answer.",
        "Second question.",
        "Second answer.",
    ]


def test_leaked_nudge_in_prior_display_is_scrubbed_on_next_settlement():
    """A previously-leaked synthetic row in prior display is dropped on re-merge."""
    previous_display = [
        {"role": "user", "content": "Fix the bug."},
        # Simulates a nudge that leaked in a prior build before the fix landed.
        {"role": "user", "content": VERIFICATION_STOP_NUDGE, "_verification_stop_synthetic": True},
        {"role": "assistant", "content": "Fixed and verified."},
    ]
    previous_context = [
        {"role": "user", "content": "Fix the bug."},
        {"role": "assistant", "content": "Fixed and verified."},
    ]
    result_messages = [
        {"role": "user", "content": "Fix the bug."},
        {"role": "assistant", "content": "Fixed and verified."},
        {"role": "user", "content": "Now add a test."},
        {"role": "assistant", "content": "Test added."},
    ]

    merged = streaming._merge_display_messages_after_agent_result(
        previous_display,
        previous_context,
        result_messages,
        "Now add a test.",
    )

    assert all(VERIFICATION_STOP_NUDGE not in str(m.get("content") or "") for m in merged)
    assert all(not streaming._is_synthetic_control_message(m) for m in merged)
