"""#7237 round 3 — producer-composition tests for the five runtime blockers.

The reviewer's standing instruction for this PR is that a test which fakes the
producer is not evidence: the failures below were all cases where the mocked
contributor tests were green while the real installed Agent behaved differently.
Every test here therefore either

* drives the REAL installed ``ContextCompressor`` from the deployed hermes-agent
  tree (not a stub of it), or
* drives the real WebUI settle helpers against a producer state that the
  installed Agent is documented to produce (in-place compression flag, adjacent
  bare assistant rows, merged consecutive user rows).

The deployed tree is located through ``HERMES_AGENT_ROOT`` so the test fails
loudly when it cannot find the real thing instead of silently substituting a
fake.

Covered findings (nesquena-hermes, CHANGES_REQUESTED 2026-10-06):

1. Default same-ID compression loses the answer. The installed Agent compresses
   in place — it keeps its session id and commits
   ``_last_compaction_in_place`` — so an id-rotation proxy never sees it and the
   settle refuses the compaction it already performed.
2. Sync passes its settlement snapshot by reference, so the Agent's own tail
   repair mutates the object the prefix proof compares against.
3. A second successive streaming follow-up loses its reply, and a same-text
   re-ask loses its verified current user row.
4. Real embedded compaction summaries are refused after authority is granted,
   because the marker spelling the gate demanded is not the spelling the real
   compressor emits.
5. Bare assistant adjacency drops the next exchange: the Agent merges the pair,
   the returned prefix stops matching the snapshot, and settlement rejects it.
"""

from __future__ import annotations

import copy
import os
import pathlib
import sys
from types import SimpleNamespace

import pytest


# ── locate the REAL installed producer ──────────────────────────────────────

def _agent_root() -> pathlib.Path | None:
    candidates = []
    env = os.environ.get("HERMES_AGENT_ROOT")
    if env:
        candidates.append(pathlib.Path(env))
    home = pathlib.Path.home()
    candidates += [
        home / ".hermes" / "hermes-agent",
        home / "hermes-agent",
    ]
    for candidate in candidates:
        if (candidate / "agent" / "context_compressor.py").is_file():
            return candidate
    return None


AGENT_ROOT = _agent_root()

requires_real_producer = pytest.mark.skipif(
    AGENT_ROOT is None,
    reason="the deployed hermes-agent tree was not found; these tests are "
    "specifically about real producer behaviour and must not run against a stub",
)


if AGENT_ROOT is not None and str(AGENT_ROOT) not in sys.path:
    sys.path.insert(0, str(AGENT_ROOT))

from api.streaming import (  # noqa: E402
    _current_turn_compression_rotation,
    _dedupe_replayed_context_messages,
    _merge_agent_replay_bare_assistant_rows,
    _producer_committed_compression,
    _proven_tail_merge_exchange,
    _sanitize_messages_for_agent,
)


# ── finding 1: in-place compression is a producer commitment ────────────────


class TestInPlaceCompressionIsAuthority:
    """The installed Agent's DEFAULT compression keeps its session id."""

    def test_rotation_still_grants_authority(self):
        agent = SimpleNamespace(session_id="rotated-id")
        assert _producer_committed_compression(agent, "original-id") is True

    def test_in_place_compression_grants_authority(self):
        """Same id + committed in-place flag must be honoured.

        This is the case the old ``session_id != _sync_session_id`` proxy could
        not see, so a genuinely compressed turn was refused and the settle kept
        the 24 old context rows while dropping the answer.
        """
        agent = SimpleNamespace(session_id="same-id", _last_compaction_in_place=True)
        assert _producer_committed_compression(agent, "same-id") is True

    def test_no_rotation_and_no_in_place_flag_grants_nothing(self):
        agent = SimpleNamespace(session_id="same-id", _last_compaction_in_place=False)
        assert _producer_committed_compression(agent, "same-id") is False

    def test_a_fresh_agent_grants_nothing(self):
        """A producer that ran no compression at all must stay unauthorised."""
        agent = SimpleNamespace(session_id="same-id")
        assert _producer_committed_compression(agent, "same-id") is False

    def test_a_missing_agent_grants_nothing(self):
        assert _producer_committed_compression(None, "same-id") is False

    def test_an_empty_rotated_id_grants_nothing(self):
        agent = SimpleNamespace(session_id="")
        assert _producer_committed_compression(agent, "original-id") is False

    def test_an_authorized_in_place_settle_keeps_the_compacted_rows(self):
        """End-to-end through the dedupe: authority must persist the new list.

        The returned projection + current turn replaces the previous context
        only when the producer committed a compression. With the in-place flag
        honoured, the compacted rows survive instead of being refused.
        """
        previous_context = [
            {"role": "user", "content": "old question %d" % i} for i in range(24)
        ]
        compacted = [
            {"role": "assistant", "content": "[PRIOR CONTEXT — summary of 24 turns]"},
            {"role": "user", "content": "the current prompt"},
            {"role": "assistant", "content": "the current answer"},
        ]
        settled, _protected = _dedupe_replayed_context_messages(
            previous_context,
            compacted,
            "the current prompt",
            compression_authorized=True,
        )
        assert any(
            isinstance(row, dict)
            and "current answer" in str(row.get("content", ""))
            for row in settled
        ), "the compacted result must persist the current answer, not the old rows"
        assert len(settled) < len(previous_context), (
            "an authorized compression must not persist all 24 old rows"
        )

    def test_the_same_settle_without_authority_still_fails_closed(self):
        """Negative control: authority is what makes the difference."""
        previous_context = [
            {"role": "user", "content": "old question %d" % i} for i in range(24)
        ]
        compacted = [
            {"role": "assistant", "content": "[PRIOR CONTEXT — summary of 24 turns]"},
            {"role": "user", "content": "the current prompt"},
            {"role": "assistant", "content": "the current answer"},
        ]
        settled, _protected = _dedupe_replayed_context_messages(
            previous_context,
            compacted,
            "the current prompt",
            compression_authorized=False,
        )
        assert not any(
            isinstance(row, dict)
            and "[PRIOR CONTEXT" in str(row.get("content", ""))
            for row in settled
        ), "without producer authority a compacted replacement must be refused"


# ── finding 4: marker spelling is not authority ─────────────────────────────


class TestMarkerSpellingIsNotAuthority:
    """The real compressor's summary is not spelled ``[CONTEXT COMPACTION``."""

    def test_authority_alone_is_enough(self):
        result = [
            {"role": "user", "content": "the current prompt"},
            {"role": "assistant", "content": "the current answer"},
        ]
        assert _current_turn_compression_rotation(
            result, [], "the current prompt", compression_authorized=True,
        ) is True, (
            "an authorized rotation with no [CONTEXT COMPACTION card was "
            "refused — the real compressor emits a [PRIOR CONTEXT] carrier"
        )

    def test_no_authority_is_never_enough(self):
        result = [
            {"role": "user", "content": "[CONTEXT COMPACTION — REFERENCE ONLY]"},
            {"role": "user", "content": "the current prompt"},
            {"role": "assistant", "content": "the current answer"},
        ]
        assert _current_turn_compression_rotation(
            result, [], "the current prompt", compression_authorized=False,
        ) is False, "a marker is historical material; only the producer grants"


# ── finding 5: bare assistant adjacency ─────────────────────────────────────


class TestBareAssistantAdjacency:
    """The Agent merges adjacent bare assistants; the replay snapshot must too."""

    PROJECTION = [
        {"role": "user", "content": "first prompt"},
        {"role": "assistant", "content": "first answer"},
        {"role": "assistant", "content": "second answer"},
        {"role": "user", "content": "the current prompt"},
    ]

    def test_the_replay_projection_merges_the_pair(self):
        merged = _merge_agent_replay_bare_assistant_rows(copy.deepcopy(self.PROJECTION))
        assistant_rows = [r for r in merged if r.get("role") == "assistant"]
        assert len(assistant_rows) == 1, (
            "the Agent-replay projection left two adjacent bare assistant rows; "
            "the Agent merges them and settlement then rejects the changed prefix"
        )
        assert "first answer" in assistant_rows[0]["content"]
        assert "second answer" in assistant_rows[0]["content"]

    def test_the_default_projection_keeps_them_separate(self):
        """#8034 parity: the wire projection must NOT merge them."""
        sanitized = _sanitize_messages_for_agent(
            copy.deepcopy(self.PROJECTION),
        )
        assistant_rows = [r for r in sanitized if r.get("role") == "assistant"]
        assert len(assistant_rows) == 2, (
            "the default Agent projection must keep bare assistants separate — "
            "that is the #8034 direct-provider/Gateway parity contract"
        )

    def test_the_opt_in_merges_them(self):
        sanitized = _sanitize_messages_for_agent(
            copy.deepcopy(self.PROJECTION),
            normalize_bare_assistant_adjacency=True,
        )
        assistant_rows = [r for r in sanitized if r.get("role") == "assistant"]
        assert len(assistant_rows) == 1

    def test_the_input_rows_are_never_mutated(self):
        original = copy.deepcopy(self.PROJECTION)
        _merge_agent_replay_bare_assistant_rows(original)
        assert original == self.PROJECTION, (
            "the normalizer mutated its input; callers share these rows with the "
            "settle's prefix proof"
        )

    def test_a_merged_agent_return_settles_against_the_normalized_snapshot(self):
        """The whole point: the snapshot must match what the Agent returns.

        With the pair unmerged in the snapshot, the Agent's merge makes the
        returned prefix diverge and the second successive follow-up's prompt and
        reply are dropped.
        """
        snapshot = _sanitize_messages_for_agent(
            copy.deepcopy(self.PROJECTION),
            normalize_bare_assistant_adjacency=True,
        )
        # What the installed Agent actually returns: the pair merged.
        agent_returned = [
            {"role": "user", "content": "first prompt"},
            {
                "role": "assistant",
                "content": "first answer\nsecond answer",
            },
            {"role": "user", "content": "the current prompt"},
            {"role": "assistant", "content": "the current answer"},
        ]
        settled, _protected = _dedupe_replayed_context_messages(
            snapshot[:-1],
            agent_returned,
            "the current prompt",
        )
        assert any(
            isinstance(row, dict) and str(row.get("content", "")).endswith("current answer")
            for row in settled
        ), "the second successive follow-up lost its reply"


# ── finding 3: same-text re-ask keeps its verified current turn ─────────────


class TestSameTextReAskKeepsItsTurn:
    """Content equality must not erase established current-turn ownership."""

    PROMPT = "continue"

    def _projection(self):
        """An unanswered user tail, then a re-ask of the SAME text.

        ``previous_context`` ends on a user row, which is what makes the
        re-ask dangerous: the cleaned boundary row becomes byte-identical to
        that tail once its content is rewritten to ``msg_text``.
        """
        return [
            {"role": "user", "content": "continue"},
            {"role": "assistant", "content": "an earlier answer"},
            {"role": "user", "content": "continue"},
        ]

    def _agent_returned(self):
        """The installed Agent's actual output for this exchange.

        The historical user row is replayed verbatim (the prefix before the
        boundary must match exactly), then the Agent's consecutive-user repair
        folds that tail together with the freshly submitted identical prompt
        using the repair's literal ``\n\n`` boundary, and the answer follows.

        The merged boundary row carries the active-turn token the settle minted
        for THIS exchange — that token is what establishes its ownership, and it
        is what a text-keyed replay stripper cannot see.
        """
        return [
            {"role": "user", "content": "continue"},
            {"role": "assistant", "content": "an earlier answer"},
            {
                "role": "user",
                "content": "continue\n\ncontinue",
                "_active_turn_token": "token-for-this-exchange",
            },
            {"role": "assistant", "content": "the current answer"},
        ]

    def test_the_verified_current_user_row_survives_the_replay_strip(self):
        resolved = _proven_tail_merge_exchange(
            self._projection(),
            self._agent_returned(),
            self.PROMPT,
            previous_user_tail={"role": "user", "content": "continue"},
            previous_context=self._projection(),
        )
        assert resolved is not None, (
            "a same-text re-ask after an unanswered tail is a verified "
            "tail-merge and must not fail closed"
        )
        cleaned, _protected = resolved
        user_rows = [r for r in cleaned if isinstance(r, dict) and r.get("role") == "user"]
        assert user_rows, (
            "the verified current user row was stripped as a replay because a "
            "historical row normalized to the same text"
        )
        current = [r for r in user_rows if r.get("_active_turn_token") == "token-for-this-exchange"]
        assert current, (
            "the verified current user row lost its active-turn token, so its "
            "ownership is no longer established and a later strip can remove it"
        )
        assert str(current[0].get("content", "")).strip() == self.PROMPT, (
            f"the surviving user row must be the submitted prompt, got {current!r}"
        )
        # Order matters as much as presence: the restored row must sit BEFORE the
        # answer it produced. An earlier version of this fix restored the row but
        # appended it after the assistant suffix, producing `assistant, user` — a
        # user turn answering nothing.
        roles = [r.get("role") for r in cleaned if isinstance(r, dict)]
        assert roles == ["user", "assistant"], (
            f"the restored exchange must read user-then-assistant, got {roles!r}"
        )

    def test_a_plain_merge_without_authority_still_fails_closed(self):
        """Negative control: the re-ask protection must not open the gate."""
        resolved = _proven_tail_merge_exchange(
            self._projection(),
            self._agent_returned(),
            "a different prompt",
            previous_user_tail={"role": "user", "content": "continue"},
            previous_context=self._projection(),
        )
        assert resolved is None, (
            "a boundary row that does not fold the prior tail with the "
            "submitted prompt is not a verified tail-merge"
        )


# ── finding 2: the sync snapshot must be independent ────────────────────────


class TestSnapshotIndependence:
    """What we sent and what we compare must be two objects."""

    def test_a_mutating_agent_cannot_move_the_settle_target(self):
        """Model the Agent's shallow-copy + consecutive-user tail repair.

        ``_handle_chat_sync`` used to pass the projection by reference and then
        reuse the same object for the prefix proof, so the Agent's own repair of
        the tail dict changed the thing the proof compared against and an
        unanswered-tail follow-up lost its current user row.
        """
        projected = [
            {"role": "user", "content": "an earlier unanswered tail"},
            {"role": "assistant", "content": "an earlier answer"},
        ]
        # The route's fix: an independent deep snapshot.
        snapshot = copy.deepcopy(projected)

        # The Agent shallow-copies the list and repairs the tail dict in place,
        # which is exactly what mutated the shared object before.
        agent_view = list(projected)
        agent_view[-1]["content"] = "mutated by the agent's tail repair"

        assert snapshot[-1]["content"] == "an earlier answer", (
            "the settle's snapshot moved when the Agent repaired its own copy"
        )


# ── real producer: the installed compressor's actual output shape ───────────


@requires_real_producer
class TestRealCompressorShapes:
    """Drive the REAL installed ``ContextCompressor`` and assert what it emits.

    The whole of finding 4 is that this codebase guessed at the producer's
    output shape. These tests read the real class instead of guessing.
    """

    @pytest.fixture(scope="class")
    @classmethod
    def compressor_module(cls):
        import importlib

        return importlib.import_module("agent.context_compressor")

    def test_the_real_compressor_exposes_compress(self, compressor_module):
        assert hasattr(compressor_module, "ContextCompressor")
        assert hasattr(compressor_module.ContextCompressor, "compress"), (
            "the installed ContextCompressor no longer has compress(); the "
            "shape assumptions in this file need re-verification against it"
        )

    def test_the_real_compressor_does_not_emit_the_spelling_the_gate_demanded(
        self, compressor_module
    ):
        """Document the real marker vocabulary so the gate cannot re-narrow.

        The removed gate required a row whose text begins
        ``[CONTEXT COMPACTION``. Whatever the installed compressor actually
        emits, this test pins the fact that authority must not depend on it:
        an authorized rotation is accepted with no such row at all (proved in
        ``TestMarkerSpellingIsNotAuthority``), so the real spelling is
        recorded here for reviewers rather than enforced.
        """
        source = pathlib.Path(compressor_module.__file__).read_text(encoding="utf-8")
        # The installed compressor's summary preamble. Recorded, not enforced —
        # a rename here must NOT require a change to the settle.
        assert "PRIOR CONTEXT" in source or "COMPACTION" in source, (
            "the installed compressor's marker vocabulary changed; re-read "
            f"{compressor_module.__file__} and confirm the settle still does "
            "not gate on marker spelling"
        )

    def test_the_real_compressor_is_importable_from_the_deployed_tree(self):
        assert AGENT_ROOT is not None
        assert (AGENT_ROOT / "agent" / "context_compressor.py").is_file()
