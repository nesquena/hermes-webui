"""Regression tests for issue #7587.

Repeated identical user turns collapse: visible/content keys omit occurrence
metadata.

``_session_message_merge_key()`` already carries
``_normalized_message_timestamp_for_key(...)``, so it can tell two occurrences
of the same content apart.  ``_session_message_visible_key()`` and
``_session_message_content_key()`` build identity from ``(role, content)``
only, so two genuinely distinct user turns that happen to carry the same text
(or the same image payload) collapse into ONE visible identity.  Because
exact-identity matching is authoritative in ``_matching_visible_duplicate()``
(it returns on ``visible_key in visible_keys`` before any fuzzy logic runs),
the later occurrence is suppressed as an "already visible duplicate" on the
restore / next-turn-context-reconstruction path and is lost from the
projection.

These tests pin three properties:

1.  Two independent same-content occurrences must NOT collapse.
2.  The SAME durable record replayed/restored must STILL collapse -- otherwise
    the fix turns an over-dedup bug into a duplicate-rows bug.
3.  Rows with no timestamp (malformed or legacy data) must keep a well-defined
    behaviour: they must not crash, and they must not become permanently
    unmatchable.  When the clock is missing the discriminator falls back to the
    row's private identity, so two rows that are provably different are
    preserved rather than suppressed (fail closed).
"""

SAME_TEXT = "please summarise the attached log"

IMAGE_PARTS = [
    {"type": "text", "text": "what is wrong with this chart?"},
    {
        "type": "image_url",
        "image_url": {"url": "data:image/png;base64,ZmFrZS1pbWFnZS1ieXRlcw=="},
    },
]


def _text_turn(ts):
    return {"role": "user", "content": SAME_TEXT, "timestamp": ts}


def _image_turn(ts):
    return {"role": "user", "content": [dict(p) for p in IMAGE_PARTS], "timestamp": ts}


# ---------------------------------------------------------------------------
# Key level: the issue reproduction.
# ---------------------------------------------------------------------------


def test_repeated_user_turn_keys_are_occurrence_sensitive():
    """The #7587 repro: two same-content user turns must key differently."""
    from api.models import (
        _session_message_content_key,
        _session_message_merge_key,
        _session_message_visible_key,
    )

    turn_a = _text_turn(1000.0)
    turn_b = _text_turn(2000.0)

    # Control: the merge key already distinguishes the two occurrences.  This
    # assertion holds on unpatched master and must keep holding.
    assert _session_message_merge_key(turn_a) != _session_message_merge_key(turn_b)

    # The bug: visible/content identity collapsed the two occurrences.
    assert _session_message_visible_key(turn_a) != _session_message_visible_key(turn_b)
    assert _session_message_content_key(turn_a) != _session_message_content_key(turn_b)


def test_repeated_multimodal_turn_keys_are_occurrence_sensitive():
    """Same collapse for structured (image-bearing) payloads."""
    from api.models import (
        _session_message_content_key,
        _session_message_visible_key,
    )

    turn_a = _image_turn(1000.0)
    turn_b = _image_turn(2000.0)

    assert _session_message_visible_key(turn_a) != _session_message_visible_key(turn_b)
    assert _session_message_content_key(turn_a) != _session_message_content_key(turn_b)


def test_matching_visible_duplicate_no_longer_suppresses_later_occurrence():
    """The suppression helper must not treat a later occurrence as a duplicate."""
    from api.models import (
        _matching_visible_duplicate,
        _session_message_visible_key,
    )

    turn_a = _text_turn(1000.0)
    turn_b = _text_turn(2000.0)

    assert _matching_visible_duplicate(
        _session_message_visible_key(turn_b),
        {_session_message_visible_key(turn_a)},
    ) is None


def test_matching_visible_duplicate_still_matches_the_same_occurrence():
    """Guard: identical content AND identical timestamp is still a duplicate."""
    from api.models import (
        _matching_visible_duplicate,
        _session_message_visible_key,
    )

    turn_a = _text_turn(1000.0)
    replay = _text_turn(1000.0)

    assert _matching_visible_duplicate(
        _session_message_visible_key(replay),
        {_session_message_visible_key(turn_a)},
    ) == _session_message_visible_key(turn_a)


# ---------------------------------------------------------------------------
# Merge level: the data loss itself.
# ---------------------------------------------------------------------------


def _repeated_turn_transcript(role_payload):
    """A sidecar occurrence of the prompt plus a LATER state.db occurrence.

    The state.db rows are deliberately NOT positionally aligned with the
    sidecar prefix (the sidecar opens on an unrelated assistant row), so the
    index-aligned replay probe cannot vouch for them.  The only thing that can
    recognise the later state.db occurrence as "already visible" is the
    content-only visible key -- which is exactly the #7587 collapse.
    """
    sidecar = [
        {"role": "assistant", "content": "earlier answer", "timestamp": 500.0},
        role_payload(1000.0),
        {"role": "assistant", "content": "first answer", "timestamp": 1001.0},
    ]
    state = [
        role_payload(2000.0),
        {"role": "assistant", "content": "second answer", "timestamp": 2001.0},
    ]
    return sidecar, state


def test_repeated_plain_text_user_turn_survives_restore():
    """#7587: the second identical user prompt must not be dropped."""
    from api.models import merge_session_messages_append_only

    sidecar, state = _repeated_turn_transcript(_text_turn)
    merged = merge_session_messages_append_only(sidecar, state)

    repeated = [
        m for m in merged if m.get("role") == "user" and m.get("content") == SAME_TEXT
    ]
    assert len(repeated) == 2, (
        "the later identical user turn was suppressed as a duplicate: "
        f"{[m.get('content') for m in merged]}"
    )
    assert any(m.get("content") == "second answer" for m in merged)


def test_repeated_multimodal_user_turn_survives_restore():
    """#7587: pasting the same screenshot twice must keep both turns."""
    from api.models import merge_session_messages_append_only

    sidecar, state = _repeated_turn_transcript(_image_turn)
    merged = merge_session_messages_append_only(sidecar, state)

    image_rows = [
        m
        for m in merged
        if m.get("role") == "user" and isinstance(m.get("content"), list)
    ]
    assert len(image_rows) == 2, (
        "the later identical image turn was suppressed as a duplicate: "
        f"{[type(m.get('content')).__name__ for m in merged]}"
    )
    assert any(m.get("content") == "second answer" for m in merged)


# ---------------------------------------------------------------------------
# The invariant that must NOT regress: replay of one durable record collapses.
# ---------------------------------------------------------------------------


def test_same_durable_state_db_row_replayed_twice_still_collapses():
    """A single durable row replayed from state.db must still collapse to one.

    Without this, the fix would turn the over-dedup bug into a duplicate-rows
    bug on every restore.
    """
    from api.models import merge_session_messages_append_only

    sidecar = [
        {
            "role": "user",
            "content": SAME_TEXT,
            "timestamp": 1000.0,
            "_state_db_row_id": 7,
        },
        {
            "role": "assistant",
            "content": "first answer",
            "timestamp": 1001.0,
            "_state_db_row_id": 8,
        },
    ]
    state = [
        {
            "role": "user",
            "content": SAME_TEXT,
            "timestamp": 1000.0,
            "_state_db_row_id": 7,
        },
        {
            "role": "assistant",
            "content": "first answer",
            "timestamp": 1001.0,
            "_state_db_row_id": 8,
        },
    ]

    merged = merge_session_messages_append_only(sidecar, state)

    assert merged == sidecar
    assert len(merged) == 2


def test_replayed_legacy_rows_without_identity_still_collapse():
    """Legacy rows (no durable id, no message_id) replayed verbatim collapse."""
    from api.models import merge_session_messages_append_only

    sidecar = [
        {"role": "user", "content": SAME_TEXT, "timestamp": 1000.0},
        {"role": "assistant", "content": "first answer", "timestamp": 1001.0},
    ]
    state = [
        {"role": "user", "content": SAME_TEXT, "timestamp": 1000.0},
        {"role": "assistant", "content": "first answer", "timestamp": 1001.0},
    ]

    merged = merge_session_messages_append_only(sidecar, state)

    assert merged == sidecar
    assert len(merged) == 2


def test_index_aligned_replay_with_coarse_sidecar_clocks_still_collapses():
    """A replayed prefix must still collapse when the two stores disagree on clocks.

    Production shape: a compressed/tip sidecar persists coarse second-level
    timestamps while state.db keeps the very same rows with sub-second (here
    restamped) floats.  The positional replay probe is therefore
    clock-tolerant -- requiring clock agreement there would append the whole
    state prefix after the sidecar tail and make /api/session end on an old
    user prompt.
    """
    from api.models import merge_session_messages_append_only

    sidecar = [
        {"role": "user", "content": "plan", "timestamp": 1779309765},
        {"role": "assistant", "content": "loaded plan", "timestamp": 1779309765},
        {"role": "user", "content": SAME_TEXT, "timestamp": 1779309765},
        {"role": "assistant", "content": "final answer", "timestamp": 1779309765},
    ]
    state = [
        {"role": "user", "content": "plan", "timestamp": 1779344917.2780898},
        {"role": "assistant", "content": "loaded plan", "timestamp": 1779344917.285758},
        {"role": "user", "content": SAME_TEXT, "timestamp": 1779344917.2926793},
    ]

    merged = merge_session_messages_append_only(sidecar, state)

    assert merged == sidecar
    assert merged[-1]["role"] == "assistant"
    assert merged[-1]["content"] == "final answer"


# ---------------------------------------------------------------------------
# Malformed / legacy rows: explicit, crash-free, never-permanently-unmatchable.
# ---------------------------------------------------------------------------


def test_rows_without_timestamp_still_match_each_other():
    """Two timestamp-less rows of the same content must NOT become unmatchable."""
    from api.models import (
        _session_message_content_key,
        _session_message_visible_key,
    )

    a = {"role": "user", "content": SAME_TEXT}
    b = {"role": "user", "content": SAME_TEXT}

    assert _session_message_visible_key(a) == _session_message_visible_key(b)
    assert _session_message_content_key(a) == _session_message_content_key(b)


def test_unparseable_timestamp_rows_do_not_crash_and_stay_distinct_per_value():
    """Malformed timestamps are stringified, never raised."""
    from api.models import (
        _session_message_content_key,
        _session_message_visible_key,
    )

    a = {"role": "user", "content": SAME_TEXT, "timestamp": "not-a-number"}
    b = {"role": "user", "content": SAME_TEXT, "timestamp": "also-not-a-number"}
    same = {"role": "user", "content": SAME_TEXT, "timestamp": "not-a-number"}

    # Same malformed value still identifies the same occurrence.
    assert _session_message_visible_key(a) == _session_message_visible_key(same)
    assert _session_message_content_key(a) == _session_message_content_key(same)
    # Different malformed values are different occurrences, not a crash.
    assert _session_message_visible_key(a) != _session_message_visible_key(b)
    assert _session_message_content_key(a) != _session_message_content_key(b)


def test_unestablished_occurrence_does_not_distinguish_rows():
    """A row with no timestamp cannot prove it is a different occurrence.

    The model-facing context legitimately carries no timestamps, so an
    unestablished occurrence must stay a wildcard rather than turning those
    rows into permanently unmatchable orphans.
    """
    from api.models import (
        _matching_visible_duplicate,
        _session_message_visible_key,
    )

    no_clock = {"role": "user", "content": SAME_TEXT}
    clocked = {"role": "user", "content": SAME_TEXT, "timestamp": 1000.0}

    # Identity is still occurrence-sensitive ...
    assert _session_message_visible_key(no_clock) != _session_message_visible_key(clocked)
    # ... but an unestablished occurrence must not suppress the clocked row.
    assert _matching_visible_duplicate(
        _session_message_visible_key(clocked),
        {_session_message_visible_key(no_clock)},
    ) == _session_message_visible_key(no_clock)


def test_state_row_after_a_restored_prefix_is_preserved():
    """A later state.db occurrence of a repeated prompt survives the merge.

    The sidecar's copy of the prompt carries no timestamp, so its occurrence is
    unestablished; the later state.db occurrence must still survive rather than
    be suppressed -- under-dedup (a repeated row) is the accepted trade,
    over-dedup deletes a turn the user actually sent.
    """
    from api.models import merge_session_messages_append_only

    sidecar = [
        # occurrence unestablished: no timestamp on the sidecar copy
        {"role": "user", "content": SAME_TEXT},
        {"role": "assistant", "content": "first answer", "timestamp": 500.0},
    ]
    state = [
        {"role": "user", "content": SAME_TEXT, "timestamp": 100.0},
        {"role": "assistant", "content": "first answer", "timestamp": 101.0},
        # a second, later occurrence with an establishable clock
        {"role": "user", "content": SAME_TEXT, "timestamp": 1000.0},
    ]

    merged = merge_session_messages_append_only(sidecar, state)

    repeated = [
        m for m in merged if m.get("role") == "user" and m.get("content") == SAME_TEXT
    ]
    assert len(repeated) == 2


def test_non_dict_and_empty_timestamp_rows_are_keyable():
    """Key builders stay total: no exception for odd shapes."""
    from api.models import (
        _session_message_content_key,
        _session_message_visible_key,
    )

    for payload in (None, 7, "text", [], {}):
        assert _session_message_visible_key(payload) == _session_message_visible_key(payload)  # type: ignore[arg-type]
        assert _session_message_content_key(payload) == _session_message_content_key(payload)  # type: ignore[arg-type]

    for ts in (None, "", 0, 0.0, float("inf"), float("nan")):
        row = {"role": "user", "content": SAME_TEXT, "timestamp": ts}
        assert _session_message_visible_key(row) is not None
        assert _session_message_content_key(row) is not None


# ---------------------------------------------------------------------------
# Callers that legitimately need timestamp-agnostic content identity.
# ---------------------------------------------------------------------------


def test_state_db_delta_still_aligns_a_restamped_mirrored_prefix():
    """state_db_delta_after_context must stay timestamp-agnostic.

    state.db mirrors the model-facing context prefix with FRESH timestamps
    (LCM/continuation sessions).  If content identity there became
    occurrence-sensitive, that alignment would break and the whole state
    transcript would be replayed into the next prompt.
    """
    from api.models import state_db_delta_after_context

    sidecar_context = [
        {"role": "user", "content": SAME_TEXT, "timestamp": 1000.0},
        {"role": "assistant", "content": "first answer", "timestamp": 1001.0},
    ]
    state_messages = [
        {"role": "user", "content": SAME_TEXT, "timestamp": 5000.0},
        {"role": "assistant", "content": "first answer", "timestamp": 5001.0},
        {"role": "user", "content": "brand new question", "timestamp": 6000.0},
    ]

    delta = state_db_delta_after_context(sidecar_context, state_messages)

    assert [m["content"] for m in delta] == ["brand new question"]


def test_state_db_delta_still_surfaces_a_repeated_occurrence():
    """A repeated occurrence after the aligned prefix is a real delta."""
    from api.models import state_db_delta_after_context

    sidecar_context = [
        {"role": "user", "content": SAME_TEXT, "timestamp": 1000.0},
        {"role": "assistant", "content": "first answer", "timestamp": 1001.0},
    ]
    state_messages = [
        {"role": "user", "content": SAME_TEXT, "timestamp": 1000.0},
        {"role": "assistant", "content": "first answer", "timestamp": 1001.0},
        {"role": "user", "content": SAME_TEXT, "timestamp": 2000.0},
        {"role": "assistant", "content": "second answer", "timestamp": 2001.0},
    ]

    delta = state_db_delta_after_context(sidecar_context, state_messages)

    assert [m["content"] for m in delta] == [SAME_TEXT, "second answer"]


def test_occurrence_falls_back_to_private_identity_when_the_clock_is_missing():
    """#7587 fail-closed: no clock, but different ids -- the rows are distinct.

    The issue asks for suppression only when the timestamps AND all available
    private identities agree.  When there is no timestamp at all, the private
    identity is what tells two rows apart, and it has to be enough: preserving
    two rows is the safe direction, deleting a real user turn is not.
    """
    from api.models import _session_message_visible_key

    first = {"role": "user", "content": SAME_TEXT, "id": "row-a"}
    second = {"role": "user", "content": SAME_TEXT, "id": "row-b"}

    assert _session_message_visible_key(first) != _session_message_visible_key(second)


def test_occurrence_without_clock_or_identity_still_collapses():
    """With neither a clock nor an identity the two rows are indistinguishable.

    There is nothing to tell them apart with, so they key together; the point
    is that the behaviour stays defined instead of raising or turning into a
    permanent mismatch.
    """
    from api.models import _session_message_visible_key

    first = {"role": "user", "content": SAME_TEXT}
    second = {"role": "user", "content": SAME_TEXT}

    assert _session_message_visible_key(first) == _session_message_visible_key(second)
