"""Round-4 blocker 4: heal settles derive compression authority from the producer."""

from types import SimpleNamespace

from api.streaming import (
    _producer_committed_compression,
    _settle_result_messages,
)


class TestHealSettleCompressionAuthority:
    """bl4: the heal settles must ask the agent that PRODUCED the result.

    The old wiring checked a rotation-only proxy (``session_id != session_id``)
    against the ORIGINAL failed ``agent`` even when the healed result was
    produced by a different instance. The installed Agent's default compaction
    is IN-PLACE (same session id + ``_last_compaction_in_place``), so the proxy
    could never see it: the settle refused a compression the producer had
    already committed and dropped the healed answer.
    """

    def test_rotation_only_proxy_on_the_stale_agent_cannot_see_the_heal(self):
        session_id = "session-1"
        # The original (failed) agent: no rotation, no in-place commitment.
        stale_agent = SimpleNamespace(session_id=session_id)
        old_proxy = bool(
            getattr(stale_agent, "session_id", None)
            and getattr(stale_agent, "session_id", None) != session_id
        )
        assert old_proxy is False
        # The terminal heal agent committed an in-place compaction: the shared
        # predicate on the ACTUAL producer must honour it.
        heal_agent = SimpleNamespace(session_id=session_id, _last_compaction_in_place=True)
        assert _producer_committed_compression(heal_agent, session_id) is True

    def test_first_heal_retry_rotation_grants_authority(self):
        # The rebuilt retry agent rotating its session id is a producer
        # commitment the predicate must see (the old proxy saw this one).
        healed = SimpleNamespace(session_id="rotated-session")
        assert _producer_committed_compression(healed, "session-1") is True

    def test_fresh_heal_agent_without_compression_grants_nothing(self):
        fresh = SimpleNamespace(session_id="session-1")
        assert _producer_committed_compression(fresh, "session-1") is False

    def test_missing_producer_grants_nothing(self):
        assert _producer_committed_compression(None, "session-1") is False

    def test_heal_settle_with_in_place_commitment_keeps_the_compacted_answer(self):
        """End-to-end through the real settle with the heal producer's authority.

        The first heal retry re-enters the settle with the retry agent; the
        terminal heal with ``_heal_agent``. Both derive
        ``compression_authorized`` from the shared predicate on that producer.
        With the in-place commitment honoured, the compacted result (summary +
        current exchange) survives; the rotation-only proxy on the stale agent
        would have refused it and kept the old rows without the answer.
        """
        previous = [{"role": "user", "content": "old question %d" % i} for i in range(24)]
        compacted = [
            {"role": "assistant", "content": "[PRIOR CONTEXT — summary of 24 turns]"},
            {"role": "user", "content": "the current prompt"},
            {"role": "assistant", "content": "the current answer"},
        ]
        session = SimpleNamespace(
            session_id="heal-session",
            messages=list(previous),
            context_messages=list(previous),
        )
        _settle_result_messages(
            session,
            previous,
            previous,
            compacted,
            "the current prompt",
            "webui",
            {
                "token": None,
                "text": "the current prompt",
                "current_turn_user_idx": None,
                "turn_id": "",
            },
            compression_authorized=_producer_committed_compression(
                SimpleNamespace(
                    session_id="heal-session", _last_compaction_in_place=True
                ),
                "heal-session",
            ),
        )
        text = [row.get("content") for row in (session.context_messages or [])]
        assert any("current answer" in str(row or "") for row in text), text
        assert len(text) < len(previous), text
