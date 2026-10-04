"""Regression tests for the two objective residuals in #7237's settlement
boundary (nesquena-hermes review, 2026-09-27T16:38:33Z on HEAD 73a5d94f).

Residual 1 — the compression-marker exception bypasses projection authority:

``_dedupe_replayed_context_messages()`` used to compute
``_has_compression_marker`` with ``any(...)`` over the ENTIRE returned
conversation BEFORE the projection-authority gate. Because
``result_messages`` is the sent projection plus the current turn, an OLD
marker already inside ``projected_history`` disabled the exact-prefix gate,
and on a prefix mismatch the settle reached the wholesale
``return result_messages`` path, discarding authoritative raw history.
``is_context_compression_marker()`` also classifies any non-tool row whose
text begins ``[CONTEXT COMPACTION``, so a LITERAL user prompt that begins
with that string granted the same wholesale authority.

The fix ties wholesale replacement to proof of a NEW current-turn
compression rotation: the marker must be new in this return (not already
part of the exact supplied projection), must not be the current user's own
literal prompt text, and must lead the returned conversation (a rotation
replaces history from the top). Otherwise the projection gate owns the
settle (exact prefix → raw context + proven current-turn suffix;
non-exact → raw context alone, fail closed).

Residual 2 — sync /api/chat empty projection composition:

``result.get("messages") or _previous_context_messages`` substitutes the
raw pre-turn context for an empty Agent result. With the exact sent
projection ``[]``, ``_proven_current_turn_suffix([], substituted_raw)``
treats the entire substituted raw history as current-turn output and the
settle returns ``previous_context + previous_context``. The synchronous
path must keep the distinction: an empty Agent result is a no-op for model
context (the streaming settle's contract), never a substitution that an
empty projection then re-appends as a fake current-turn suffix.

Each regression composes the REAL sanitizer projection (and the REAL sync
handler for residual 2), so the fixtures genuinely exercise the production
settlement/dedupe entry points.
"""
import copy
import sys
from types import SimpleNamespace

from api.streaming import (
    _dedupe_replayed_context_messages,
    _is_context_compression_marker,
    _proven_current_turn_suffix,
    _sanitize_messages_for_agent,
    _settle_result_messages,
)


def _call(cid):
    return {"id": cid, "type": "function", "function": {"name": "t", "arguments": "{}"}}


def _no_id(rows):
    return [{k: v for k, v in m.items() if k != "id"} for m in rows]


def _role_content(rows):
    return [(m.get("role"), m.get("content")) for m in rows]


class _Fixture:
    """Raw history carrying a compression marker (from an earlier legitimate
    rotation) plus rows the OUTBOUND SANITIZER rewrites: a reasoning-only
    assistant row is dropped and two consecutive assistant rows are merged,
    so the projection is shorter than the raw context and the raw rows do not
    survive the projection byte-for-byte.
    """

    MARKER = {
        "role": "assistant",
        "content": "[CONTEXT COMPACTION — REFERENCE ONLY] earlier turns were compacted",
        "timestamp": 1.0,
    }
    CURRENT_TURN = [
        {"role": "user", "content": "third question"},
        {"role": "assistant", "content": "third answer"},
    ]

    @classmethod
    def raw_history(cls):
        return [
            copy.deepcopy(cls.MARKER),
            {
                "role": "assistant",
                "content": "",
                "reasoning_content": "hidden thought",
                "timestamp": 1.5,
            },
            {"role": "user", "content": "second question", "timestamp": 2.0},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [_call("k1")],
                "timestamp": 3.0,
            },
            {"role": "assistant", "content": "progress", "timestamp": 3.5},
            {"role": "tool", "tool_call_id": "k1", "content": "tool output", "timestamp": 4.0},
            {"role": "assistant", "content": "second answer", "timestamp": 5.0},
        ]

    @classmethod
    def projection(cls):
        return _sanitize_messages_for_agent([copy.deepcopy(m) for m in cls.raw_history()])

    @classmethod
    def assert_fixture_premises(cls):
        raw = cls.raw_history()
        projected = cls.projection()
        assert len(projected) < len(raw), (
            "fixture premise: the sanitizer must rewrite the raw context so the "
            "projection diverges; otherwise the historical-marker bypass is vacuous"
        )
        assert any(_is_context_compression_marker(m) for m in projected), (
            "fixture premise: the sent projection must itself carry the historical marker"
        )
        return raw, projected


class TestHistoricalMarkerCannotBypassProjectionAuthority:
    """Residual 1 (a): a marker already in the supplied projected history
    describes a PREVIOUS rotation. It must not disable the exact-prefix gate,
    on either side of the gate's outcome."""

    def test_historical_marker_exact_projection_appends_proven_suffix_only(self):
        """Even when the returned list starts EXACTLY with the sent projection
        (containing the historical marker), the settle must keep the raw
        pre-turn context and append only the proven current-turn suffix, not
        wholesale-replace the raw history with the sanitized projection.
        """
        raw, projected = _Fixture.assert_fixture_premises()
        result_messages = list(projected) + [
            copy.deepcopy(m) for m in _Fixture.CURRENT_TURN
        ]
        # Premise: the supplied marker is historical material and the
        # projection is a strict exact prefix, so the strict gate resolves a
        # proven suffix.
        assert _proven_current_turn_suffix(projected, result_messages) == (
            _Fixture.CURRENT_TURN
        )
        settled, _protected = _dedupe_replayed_context_messages(
            list(raw), list(result_messages), "third question", None,
            projected_history=list(projected),
        )
        assert settled == raw + _Fixture.CURRENT_TURN, (
            "an old marker inside the exact projection must not force wholesale "
            "replacement; the settle must be raw context + the proven current "
            "turn (#7237 review residual 1a)"
        )

    def test_historical_marker_non_exact_projection_fails_closed(self):
        """When the returned list does NOT start with the sent projection (a
        rewritten/drifted row), the historical marker must not allow wholesale
        replacement: the raw pre-turn turns to be exact-prefixed verbatim and
        nothing unproven is appended."""
        raw, projected = _Fixture.assert_fixture_premises()
        drifted = copy.deepcopy(projected)
        drifted[-1] = copy.deepcopy(drifted[-1])
        drifted[-1]["content"] = str(drifted[-1].get("content") or "") + " DRIFTED"
        result_messages = drifted + [
            copy.deepcopy(m) for m in _Fixture.CURRENT_TURN
        ]
        assert _proven_current_turn_suffix(projected, result_messages) is None, (
            "fixture premise: the drift must defeat the strict exact-prefix proof"
        )
        settled, _protected = _dedupe_replayed_context_messages(
            list(raw), list(result_messages), "third question", None,
            projected_history=list(projected),
        )
        assert settled == raw, (
            "a historical marker inside a non-exact supplied projection must "
            "fail closed to the raw pre-turn context; wholesale replacement "
            "would discard the sanitizer-rewritten raw rows (residual 1)"
        )
        # Belt and braces: none of the sanitized historical rows survive.
        assert not any(
            m.get("role") == "assistant"
            and any(tc.get("id") == "k1" for tc in (m.get("tool_calls") or []))
            and m.get("content") == "progress"
            for m in settled
        )

    def test_historical_marker_full_settle_preserves_raw_history(self):
        """Full ``_settle_result_messages`` regression: with the marker inside
        the exact supplied projection, the persisted context is raw history +
        the proven current turn — the sanitized projection rows (dropped
        reasoning row, merged assistant rows) must NOT replace the raw."""
        raw, projected = _Fixture.assert_fixture_premises()
        result_messages = list(projected) + [
            copy.deepcopy(m) for m in _Fixture.CURRENT_TURN
        ]
        session = SimpleNamespace(
            messages=[copy.deepcopy(m) for m in raw],
            context_messages=[copy.deepcopy(m) for m in raw],
            truncation_watermark=None,
        )
        _settle_result_messages(
            session,
            [copy.deepcopy(m) for m in raw],
            [copy.deepcopy(m) for m in raw],
            result_messages,
            "third question",
            "webui",
            None,
            list(projected),
        )
        persisted = session.context_messages
        expected = raw + _Fixture.CURRENT_TURN
        assert _role_content(persisted) == _role_content(expected), (
            "full settle must persist raw + proven current-turn suffix; a "
            "sanitized historical residual is the residual-1 defect"
        )
        # The raw call/result pair survived.
        assert any(
            tc.get("id") == "k1"
            for m in persisted
            if m.get("role") == "assistant"
            for tc in (m.get("tool_calls") or [])
        )
        assert any(
            m.get("role") == "tool" and m.get("tool_call_id") == "k1" for m in persisted
        )


class TestLiteralUserMarkerCannotBypassProjectionAuthority:
    """Residual 1 / case (b): a user that literally begins their current
    prompt with ``[CONTEXT COMPLAC...`` marker text must never grant
    wholesale replacement. The marker row is the current user's own turn."""

    LITERAL = "[CONTEXT COMPACTION — REFERENCE ONLY] what does this marker mean?"

    def test_literal_user_marker_exact_projection_appended_as_current_turn(self):
        """Exact projection: the literal user row IS the current turn and is
        appended through the gate — the raw context is preserved, nothing is
        wholesale-replaced."""
        raw, projected = _Fixture.assert_fixture_premises()
        result_messages = list(projected) + [
            {"role": "user", "content": self.LITERAL},
            {"role": "assistant", "content": "it is a synthetic marker"},
        ]
        # Premise: today's marker-any() makes the wholesale path reachable.
        assert any(_is_context_compression_marker(m) for m in result_messages)
        settled, _protected = _dedupe_replayed_context_messages(
            list(raw), list(result_messages), self.LITERAL, None,
            projected_history=list(projected),
        )
        assert settled == raw + [
            {"role": "user", "content": self.LITERAL},
            {"role": "assistant", "content": "it is a synthetic marker"},
        ], (
            "a literal marker typed by the user must not let wholesale "
            "replacement discard the raw history; it settles as the current "
            "turn (residual 1/b)"
        )

    def test_literal_user_marker_non_exact_projection_fails_closed(self):
        """With a drifted (non-exact) supplied projection the literal marker
        fails closed to the raw context — no wholesale, no appended unproven
        rows."""
        raw, projected = _Fixture.assert_fixture_premises()
        drifted = copy.deepcopy(projected)
        drifted[-1] = copy.deepcopy(drifted[-1])
        drifted[-1]["content"] = str(drifted[-1].get("content") or "") + " DRIFTED"
        result_messages = drifted + [
            {"role": "user", "content": self.LITERAL},
            {"role": "assistant", "content": "literal answer"},
        ]
        assert _proven_current_turn_suffix(projected, result_messages) is None
        settled, _protected = _dedupe_replayed_context_messages(
            list(raw), list(result_messages), self.LITERAL, None,
            projected_history=list(projected),
        )
        assert settled == raw, (
            "a literal user marker with a non-exact projection must fail "
            "closed to the raw context (residual 1/b)"
        )

    def test_literal_user_marker_without_projection_preserves_raw_and_turn(self):
        """Gateway/agent path (no projection): the literal user prompt is the
        current turn; raw history must survive AND the current turn must be
        appended by the ownership scan — not replaced wholesale."""
        raw = [
            {"role": "user", "content": "earlier question", "timestamp": 1.0},
            {"role": "assistant", "content": "earlier answer", "timestamp": 2.0},
        ]
        result_messages = [
            {"role": "user", "content": self.LITERAL},
            {"role": "assistant", "content": "literal answer"},
        ]
        settled, _protected = _dedupe_replayed_context_messages(
            list(raw), list(result_messages), self.LITERAL, None,
        )
        assert settled == raw + result_messages, (
            "a literal current-user marker must not replace the raw history "
            "wholesale; the ownership scan must keep the raw context and "
            "append the literal current turn (residual 1/b, projection-less)"
        )


class TestGenuineRotationStillWholesale:
    """Positive controls: a marker that IS a new current-turn compression
    rotation keeps the wholesale-replacement contract."""

    def test_no_projection_rotation_wholesale_stands(self):
        raw = [
            {"role": "user", "content": "first question", "timestamp": 1.0},
            {"role": "assistant", "content": "first answer", "timestamp": 2.0},
            {"role": "user", "content": "second question", "timestamp": 3.0},
        ]
        marker = {
            "role": "assistant",
            "content": "[CONTEXT COMPACTION] summary of prior turns",
        }
        result_messages = [
            copy.deepcopy(marker),
            {"role": "user", "content": "third question"},
            {"role": "assistant", "content": "third answer"},
        ]
        settled, _protected = _dedupe_replayed_context_messages(
            list(raw), list(result_messages), "third question", None,
        )
        assert settled == result_messages, (
            "a genuine no-projection rotation (new marker at the head of the "
            "return) must keep the wholesale replacement contract"
        )

    def test_new_rotation_with_supplied_non_exact_projection_still_wholesales(self):
        """A projection is supplied but the returned conversation is a NEW
        rotated context: the marker is not part of the sent history, it heads
        the return, and it is not the user's literal prompt — the wholesale
        replacement stands."""
        raw = [
            {"role": "user", "content": "first question", "timestamp": 1.0},
            {"role": "assistant", "content": "first answer", "timestamp": 2.0},
        ]
        projected = _sanitize_messages_for_agent([copy.deepcopy(m) for m in raw])
        marker = {
            "role": "assistant",
            "content": "[CONTEXT COMPACTION] fresh rotation summary",
        }
        result_messages = [
            copy.deepcopy(marker),
            {"role": "user", "content": "third question"},
            {"role": "assistant", "content": "third answer"},
        ]
        # Premises: the marker is NOT part of what we sent, and the return is
        # a non-exact continuation of the projection.
        from api.streaming import _model_row_exact_equal

        assert not any(
            _model_row_exact_equal(marker, p) for p in projected
        ), "fixture premise: the rotation marker is new (not in the projection)"
        assert _proven_current_turn_suffix(projected, result_messages) is None, (
            "fixture premise: the return is not the projection + turn"
        )
        settled, _protected = _dedupe_replayed_context_messages(
            list(raw), list(result_messages), "third question", None,
            projected_history=list(projected),
        )
        assert settled == result_messages, (
            "a fresh rotation marker (not in the supplied projection, heading "
            "the return, not the user's prompt) must keep wholesale authority "
            "even when the projection is threaded"
        )


class TestDriftedHistoricalMarkerCannotBypassProjectionAuthority:
    """Round-N follow-up (nesquena-hermes review, 2026-09-28T04:34:04Z on
    HEAD ``021c37a7``): residual 1 is only half fixed.

    ``_current_turn_compression_rotation`` recognizes a historical marker by
    MODEL-FIELD-EXACT equality against a row in the supplied
    ``projected_history``. A marker can DRIFT between the projection this
    process sent and the conversation the Agent returned (a re-rendered card,
    an updated summary, a re-timestamped row). The exact-equality filter then
    no longer recognizes it as historical, the marker heads the return, the
    rotation predicate flips True — and the settle reaches the wholesale
    ``return result_messages`` path, discarding the authoritative raw history.

    Maintainer reproduction: a prior historical marker present in BOTH raw and
    projected history, with only that marker changed in the returned list.
    Wholesale replacement must never be authorized by a drifted marker.
    """

    def test_drifted_historical_marker_non_exact_projection_fails_closed(self):
        """A historical marker whose text drifted in the return must not
        authorize wholesale replacement: the raw pre-turn context is kept
        verbatim and nothing unproven is appended."""
        raw, projected = _Fixture.assert_fixture_premises()
        # The projection's own marker, drifted, heads the returned list.
        drifted_projected = copy.deepcopy(projected)
        drifted_projected[0] = copy.deepcopy(drifted_projected[0])
        drifted_projected[0]["content"] = (
            "[CONTEXT COMPACTION — REFERENCE ONLY] earlier turns were compacted "
            "(restated with a newer revision)"
        )
        # ...and a later non-marker row drifts too, so the supplied projection
        # is NOT a strict exact prefix of the return.
        drifted_projected[-1] = copy.deepcopy(drifted_projected[-1])
        drifted_projected[-1]["content"] = str(
            drifted_projected[-1].get("content") or ""
        ) + " DRIFTED"
        result_messages = drifted_projected + [
            copy.deepcopy(m) for m in _Fixture.CURRENT_TURN
        ]
        # Premises: the return heads with a marker, that marker is NOT
        # model-field-exact to any supplied projection row, and the strict
        # exact-prefix proof is defeated. The drifted marker is still the
        # SAME historical marker, so it must not authorize a new rotation
        # (round-N residual 1).
        from api.streaming import (
            _current_turn_compression_rotation,
            _model_row_exact_equal,
        )

        assert _is_context_compression_marker(result_messages[0]), (
            "fixture premise: the drifted marker still looks like a marker"
        )
        assert not any(
            _model_row_exact_equal(result_messages[0], p) for p in projected
        ), (
            "fixture premise: the drift defeats model-field-exact "
            "recognition of the historical marker"
        )
        assert _proven_current_turn_suffix(projected, result_messages) is None, (
            "fixture premise: a non-exact return defeats the strict suffix proof"
        )
        assert not _current_turn_compression_rotation(
            result_messages, projected, "third question"
        ), (
            "a drifted historical marker must NOT be treated as a new "
            "current-turn rotation (round-N residual 1)"
        )
        settled, _protected = _dedupe_replayed_context_messages(
            list(raw), list(result_messages), "third question", None,
            projected_history=list(projected),
        )
        assert settled == raw, (
            "a DRIFTED historical marker must not authorize wholesale "
            "replacement: the settle must fail closed to the raw pre-turn "
            "context (round-N residual 1, drifted-marker case)"
        )

    def test_drifted_historical_marker_full_settle_preserves_raw_history(self):
        """Full ``_settle_result_messages`` regression for the drifted-marker
        case: the persisted model context is the raw history alone — the
        sanitizer-dropped reasoning row, the merged assistant rows and the
        tool call/result pair all survive, and no returned historical row
        replaces them."""
        raw, projected = _Fixture.assert_fixture_premises()
        drifted_projected = copy.deepcopy(projected)
        drifted_projected[0] = copy.deepcopy(drifted_projected[0])
        drifted_projected[0]["content"] = (
            "[CONTEXT COMPACTION — REFERENCE ONLY] earlier turns were compacted "
            "(restated with a newer revision)"
        )
        result_messages = drifted_projected + [
            copy.deepcopy(m) for m in _Fixture.CURRENT_TURN
        ]
        session = SimpleNamespace(
            messages=[copy.deepcopy(m) for m in raw],
            context_messages=[copy.deepcopy(m) for m in raw],
            truncation_watermark=None,
        )
        _settle_result_messages(
            session,
            [copy.deepcopy(m) for m in raw],
            [copy.deepcopy(m) for m in raw],
            result_messages,
            "third question",
            "webui",
            None,
            list(projected),
        )
        persisted = session.context_messages
        assert _role_content(persisted) == _role_content(raw), (
            "a drifted historical marker must not wholesale-replace the raw "
            "history: the settled context is the raw pre-turn context verbatim "
            "(round-N residual 1, drifted-marker case, full settle)"
        )
        # The sanitizer-dropped raw rows survived.
        assert any(
            m.get("role") == "assistant" and m.get("reasoning_content")
            for m in persisted
        ), "the reasoning-only assistant row must survive"
        assert any(
            m.get("role") == "assistant"
            and any(tc.get("id") == "k1" for tc in (m.get("tool_calls") or []))
            for m in persisted
        ), "the assistant row carrying tool_calls[k1] must survive"
        assert any(
            m.get("role") == "tool" and m.get("tool_call_id") == "k1"
            for m in persisted
        ), "the tool result for k1 must survive"


class TestHistoricalUserLiteralMarkerCannotBypassProjectionAuthority:
    """Round-N follow-up (2026-09-28T04:34:04Z): a HISTORICAL user row that
    literally begins with ``[CONTEXT COMPACTION`` (typed in an earlier turn,
    persisting inside the projected history) must never grant wholesale
    replacement when it drifts in the return.

    The current head only rejects a marker that matches the CURRENT turn's
    ``msg_text``. A historical literal marker from an earlier turn is not that
    prompt, so it survives the check and authorizes a wholesale swap.
    """

    LITERAL = "[CONTEXT COMPACTION — REFERENCE ONLY] what does this marker mean?"

    def test_historical_user_literal_marker_drifted_fails_closed(self):
        raw = [
            {"role": "user", "content": "first question", "timestamp": 1.0},
            {"role": "assistant", "content": "first answer", "timestamp": 2.0},
            {
                "role": "user",
                "content": self.LITERAL + " (earlier turn, restated)",
                "timestamp": 3.0,
            },
            {"role": "assistant", "content": "literal explanation", "timestamp": 4.0},
        ]
        projected = _sanitize_messages_for_agent([copy.deepcopy(m) for m in raw])
        # Premise: the historical literal marker row IS inside the projection.
        assert any(
            _is_context_compression_marker(m)
            and m.get("role") == "user"
            and str(m.get("content") or "").startswith("[CONTEXT COMPACTION")
            for m in projected
        ), "fixture premise: a historical user row literal marker is projected"
        # The return drifts that row and appends the current turn, so the
        # projection is not a strict exact prefix.
        drifted_projected = copy.deepcopy(projected)
        for idx, m in enumerate(drifted_projected):
            if (
                _is_context_compression_marker(m)
                and m.get("role") == "user"
                and str(m.get("content") or "").startswith("[CONTEXT COMPACTION")
            ):
                drifted_projected[idx] = copy.deepcopy(m)
                drifted_projected[idx]["content"] = str(m.get("content") or "") + " DRIFTED"
                break
        result_messages = drifted_projected + [
            {"role": "user", "content": "current question"},
            {"role": "assistant", "content": "current answer"},
        ]
        from api.streaming import _current_turn_compression_rotation

        assert _proven_current_turn_suffix(projected, result_messages) is None, (
            "fixture premise: the drift defeats the strict suffix proof"
        )
        assert not _current_turn_compression_rotation(
            result_messages, projected, "current question"
        ), (
            "a drifted HISTORICAL user literal marker must NOT be treated as "
            "a new current-turn rotation (round-N residual 1)"
        )
        settled, _protected = _dedupe_replayed_context_messages(
            list(raw), list(result_messages), "current question", None,
            projected_history=list(projected),
        )
        assert settled == raw, (
            "a drifted historical user-literal marker must not wholesale-"
            "replace the raw history (round-N residual 1, historical-literal case)"
        )


class TestStrictProvenSuffixIsNotReplayStripped:
    """Round-N follow-up (2026-09-28T04:34:04Z): the strict-prefix branch
    deletes a legitimate repeated current turn.

    When the sent projection is a STRICT exact prefix of the returned
    conversation, ``_proven_current_turn_suffix`` states that the rows after
    it were produced by the current turn and must be appended verbatim — a
    current turn may legitimately repeat historical content. The head's new
    sub-branch discards that proof and runs ``_strip_replayed_prefix`` /
    ``_strip_replayed_context_items`` over the suffix whenever raw history is
    ALSO replay-key-prefix-equal, so a legitimate repeat is stripped away.

    Maintainer reproduction: raw/projected ``[user Q, assistant A]`` with a
    new turn repeating exactly ``[user Q, assistant A]``. The proven suffix is
    both rows; the strip removes both and only the old history survives.
    """

    Q = {"role": "user", "content": "question"}
    A = {"role": "assistant", "content": "answer"}

    def _rows(self, *rows):
        return [copy.deepcopy(r) for r in rows]

    def test_legitimate_repeated_current_turn_is_not_stripped(self):
        raw = [self.Q, self.A]
        projected = _sanitize_messages_for_agent([copy.deepcopy(m) for m in raw])
        # The current turn legitimately repeats the historical exchange.
        result_messages = list(projected) + self._rows(self.Q, self.A)
        assert _proven_current_turn_suffix(projected, result_messages) == (
            [self.Q, self.A]
        ), "fixture premise: strict prefix proves both new rows"
        settled, _protected = _dedupe_replayed_context_messages(
            list(raw), list(result_messages), "question", None,
            projected_history=list(projected),
        )
        assert settled == raw + [self.Q, self.A], (
            "a strict-proven current-turn suffix must be appended VERBATIM; "
            "stripping a legitimate repeat deletes the current turn "
            "(round-N residual 2)"
        )

    def test_legitimate_repeated_current_turn_survives_full_settle(self):
        """Same shape through the real ``_settle_result_messages`` entry:
        the persisted model context is raw + the repeated current turn."""
        raw = [self.Q, self.A]
        projected = _sanitize_messages_for_agent([copy.deepcopy(m) for m in raw])
        result_messages = list(projected) + self._rows(self.Q, self.A)
        session = SimpleNamespace(
            messages=[copy.deepcopy(m) for m in raw],
            context_messages=[copy.deepcopy(m) for m in raw],
            truncation_watermark=None,
        )
        _settle_result_messages(
            session,
            [copy.deepcopy(m) for m in raw],
            [copy.deepcopy(m) for m in raw],
            result_messages,
            "question",
            "webui",
            None,
            list(projected),
        )
        expected = raw + [self.Q, self.A]
        assert _role_content(session.context_messages) == _role_content(expected), (
            "the full settle must persist raw + the strict-proven repeated "
            "current turn (round-N residual 2, full settle)"
        )

    def test_control_replay_tail_still_deduplicated(self):
        """Positive control: a replay of historical rows that is NOT part of
        the strict-proven suffix must still be deduplicated.

        Here the supplied projection is NOT an exact prefix of the return
        (the replay is spliced into the middle), so no suffix is proven and
        the settle fails closed to the raw history — the duplicated
        historical rows never reach the model context.
        """
        raw = [
            {"role": "user", "content": "first question", "timestamp": 1.0},
            {"role": "assistant", "content": "first answer", "timestamp": 2.0},
        ]
        projected = _sanitize_messages_for_agent([copy.deepcopy(m) for m in raw])
        current_turn = [
            {"role": "user", "content": "second question", "timestamp": 3.0},
            {"role": "assistant", "content": "second answer", "timestamp": 4.0},
        ]
        # The projection, then a REPLAY of the projection, then the real turn.
        # The return still STARTS with the sent projection, so the strict
        # proof stands: the suffix is the replayed copy plus the current turn.
        # The replayed historical copy is duplicate material and must be
        # collapsed; the current turn's own rows survive verbatim.
        result_messages = list(projected) + list(projected) + current_turn
        _proven = _proven_current_turn_suffix(projected, result_messages)
        assert _proven is not None and len(_proven) == len(projected) + len(current_turn), (
            "fixture premise: the strict proof covers the replayed copy and "
            "the current turn"
        )
        settled, _protected = _dedupe_replayed_context_messages(
            list(raw), list(result_messages), "second question", None,
            projected_history=list(projected),
        )
        assert _role_content(settled) == _role_content(raw + current_turn), (
            "the replayed historical copy must still be deduped while the "
            "current turn's own rows survive verbatim"
        )

    def test_repeated_current_turn_rows_survive_identity_dedupe(self):
        """The rows the turn generated itself must collapse with nothing:
        even though they repeat historical content verbatim, the identity
        dedupe may not remove them (round-N residual 2 through the full
        settle)."""
        Q = {"role": "user", "content": "repeated question"}
        A = {"role": "assistant", "content": "repeated answer"}
        raw = [copy.deepcopy(Q), copy.deepcopy(A)]
        projected = _sanitize_messages_for_agent([copy.deepcopy(m) for m in raw])
        result_messages = list(projected) + self._rows(Q, A)
        session = SimpleNamespace(
            messages=[copy.deepcopy(m) for m in raw],
            context_messages=[copy.deepcopy(m) for m in raw],
            truncation_watermark=None,
        )
        _settle_result_messages(
            session,
            [copy.deepcopy(m) for m in raw],
            [copy.deepcopy(m) for m in raw],
            result_messages,
            "repeated question",
            "webui",
            None,
            list(projected),
        )
        expected = raw + [Q, A]
        assert _role_content(session.context_messages) == _role_content(expected), (
            "the full settle must persist raw + the turn's own repeated rows; "
            "the identity dedupe must not collapse the current turn"
        )


class TestSyncChatEmptyResultNoDuplicate:
    """Residual 2: sync /api/chat with non-empty raw history, an explicit []
    projected history, and an Agent ``messages=[]`` must NOT re-append the raw
    context as a fake current-turn suffix — one raw-history copy, no loss."""

    def test_sync_chat_empty_agent_result_keeps_single_raw_copy(self, tmp_path, monkeypatch):
        import api.config as config
        import api.models as models
        import api.routes as routes

        state_dir = tmp_path / "state"
        session_dir = state_dir / "sessions"
        session_dir.mkdir(parents=True)
        monkeypatch.setattr(models, "SESSION_DIR", session_dir)
        monkeypatch.setattr(models, "SESSION_INDEX_FILE", state_dir / "session_index.json")
        monkeypatch.setattr(routes, "SESSION_INDEX_FILE", state_dir / "session_index.json")
        monkeypatch.setattr(routes, "get_session", models.get_session)
        monkeypatch.setattr(routes, "title_from", models.title_from)
        monkeypatch.setattr(
            config, "get_config", lambda: {"model": "test-model", "provider": "test-provider"}
        )
        monkeypatch.setattr(routes, "get_config", lambda: {"model": "test-model", "provider": "test-provider"})
        monkeypatch.setattr(routes, "resolve_trusted_workspace", lambda value, **_kw: tmp_path)
        monkeypatch.setattr(routes, "load_settings", lambda: {})
        monkeypatch.setattr(routes, "_resolve_cli_toolsets", lambda: [])

        # Non-empty raw history whose EVERY row the outbound sanitizer drops
        # (reasoning-only assistant + orphan tool row) — the exact sent
        # projection is therefore [].
        raw = [
            {
                "role": "assistant",
                "content": "",
                "reasoning_content": "hidden thought",
                "timestamp": 1.0,
            },
            {"role": "tool", "tool_call_id": "ghost", "content": "orphan", "timestamp": 2.0},
        ]
        assert _sanitize_messages_for_agent([copy.deepcopy(m) for m in raw]) == [], (
            "fixture premise: raw history sanitizes to the explicit empty projection"
        )

        session = models.Session(
            session_id="sync_empty_projection",
            workspace=str(tmp_path),
            messages=list(raw),
            context_messages=list(raw),
            model="test-model",
            model_provider="test-provider",
        )
        session.save(touch_updated_at=False)

        class FakeAgent:
            def __init__(self, **_kwargs):
                pass

            def run_conversation(self, **_kwargs):
                # Empty result: the Agent produced NO rows for this turn.
                return {"messages": [], "final_response": "", "completed": False}

        monkeypatch.setitem(sys.modules, "run_agent", SimpleNamespace(AIAgent=FakeAgent))

        class _Handler:
            status = None
            headers = {}
            body = bytearray()
            wfile = None

            def __init__(self):
                self.wfile = self

            def send_response(self, status):
                self.status = status

            def send_header(self, name, value):
                self.headers[name] = value

            def end_headers(self):
                pass

            def write(self, data):
                self.body.extend(data)

        handler = _Handler()
        routes._handle_chat_sync(
            handler,
            {
                "session_id": session.session_id,
                "message": "continue please",
                "workspace": str(tmp_path),
            },
        )
        assert handler.status == 200, handler.status

        reloaded = models.Session.load(session.session_id)
        assert reloaded is not None
        persisted = reloaded.context_messages
        # Exactly ONE copy of the raw context — nothing appended, nothing dropped.
        assert _role_content(_no_id(persisted)) == _role_content(_no_id(raw)), (
            f"an empty Agent result must keep one raw-history copy, not a "
            f"duplicate; got {_role_content(persisted)}"
        )
        assert _role_content(_no_id(persisted)).count(
            _role_content(_no_id(raw))[0]
        ) == 1, "the first raw row must appear exactly once"
        # The user prompt must not have been stamped into the context either
        # (there were no current-turn rows to add).
        assert all(
            "continue please" not in str(m.get("content") or "")
            for m in persisted
        ), "no synthetic current turn may be invented for an empty result"

class TestWorkspacePrefixedCurrentUserNotDuplicated:
    """Round-N+1 (2026-09-28): a workspace-prefixed current user row must not
    be persisted beside the raw checkpoint it is the same turn as.

    The WebUI hands the Agent a ``user_message`` that starts with the
    ``[Workspace::v1: <path>]`` sentinel and a real Agent echoes that exact row
    back inside its full-conversation return. ``_message_replay_key`` (via
    ``_message_identity``) strips the sentinel for ``role == "user"``, so the
    echoed row and the raw checkpoint have the SAME identity — that is the
    documented merge contract (see ``_message_identity``).

    Model-facing history therefore ends up with the same user turn twice: the
    raw checkpoint row and its sentinel-prefixed echo. The provider then sees a
    duplicated user turn whose text is not what the human submitted.
    """

    PROMPT = "Fix the failing test."
    ANSWER = "The earlier attempt is complete."
    CORRECTIVE = "Verification failed. I fixed the parser and reran the tests."

    def _raw(self):
        return [
            {"role": "user", "content": self.PROMPT, "timestamp": 1.0},
            {"role": "assistant", "content": self.ANSWER},
            {
                "role": "user",
                "content": self.PROMPT,
                "timestamp": 2.0,
                "_source": "webui",
                "attachments": [],
                "_active_turn_token": "stream-tool-limit:2",
            },
        ]

    def _prefixed_user(self):
        return {
            "role": "user",
            "content": "[Workspace::v1: /tmp/workspace]\n" + self.PROMPT,
        }

    def _settle(self):
        raw = self._raw()
        projected = _sanitize_messages_for_agent([copy.deepcopy(m) for m in raw])
        result_messages = list(projected) + [
            self._prefixed_user(),
            {"role": "assistant", "content": self.CORRECTIVE},
        ]
        session = SimpleNamespace(
            messages=[],
            context_messages=[],
            truncation_watermark=None,
        )
        _settle_result_messages(
            session,
            [copy.deepcopy(m) for m in raw],
            [copy.deepcopy(m) for m in raw],
            result_messages,
            self.PROMPT,
            "webui",
            None,
            list(projected),
        )
        return session

    def test_dedupe_keeps_a_single_current_user_turn(self):
        raw = self._raw()
        projected = _sanitize_messages_for_agent([copy.deepcopy(m) for m in raw])
        result_messages = list(projected) + [
            self._prefixed_user(),
            {"role": "assistant", "content": self.CORRECTIVE},
        ]
        settled, _protected = _dedupe_replayed_context_messages(
            list(raw), list(result_messages), self.PROMPT, None,
            projected_history=list(projected),
        )
        user_rows = [m for m in settled if m.get("role") == "user"]
        assert len(user_rows) == 2, (
            "the raw prior-turn user row and exactly ONE current-turn user "
            f"row must survive; got {_role_content(settled)}"
        )

    def test_full_settle_persists_one_current_user_turn(self):
        session = self._settle()
        user_rows = [m for m in session.context_messages if m.get("role") == "user"]
        assert len(user_rows) == 2, (
            "the raw prior-turn user row and exactly ONE current-turn user "
            f"row must be persisted; got {_role_content(session.context_messages)}"
        )

    def test_persisted_current_user_row_is_the_submitted_prompt(self):
        session = self._settle()
        current = [
            m for m in session.context_messages
            if m.get("role") == "user" and m.get("content") != self.PROMPT
        ]
        assert not current, (
            "no workspace-prefixed duplicate of the submitted prompt may "
            f"reach the model-facing context; got {_role_content(session.context_messages)}"
        )
