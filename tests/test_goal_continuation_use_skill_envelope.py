"""#7862 review round 3: ``/use`` skill directive must not break consumption.

Core finding (maintainer, round-3 gating at ``b1321c8ad``):
``/goal`` kickoff does not consume a pending ``/use`` skill directive; on the
next browser send — the queued automatic continuation —
``static/messages.js`` prepends ``[USER OVERRIDE] …`` plus the
``[FORCED SKILL CONTEXT]`` block to the message text. The store matched the
continuation by TEXT identity alone, so the skill-wrapped text never matched,
the intent stayed pending, the turn was not treated as goal-related, and the
goal loop stopped after its first automatic continuation.

Fix: the ``goal_continue`` event hands the browser a ``continuation_id``
token, the browser carries it on the queue entry and the drained
``/api/chat/start`` POST, and the store consumes by IDENTITY when the token
is present. Text remains the fallback for clients that carry no token, and
matches the recorded prompt verbatim OR wrapped by the known forced-skill
envelope. A stale token never matches and behaves like an unrelated message.
"""

from pathlib import Path

import pytest

# The exact envelope static/messages.js prepends for a forced skill
# (mirrors the ONLY producer; see tests/test_issue2977_use_command.py).
_SKILL_DIRECTIVE = (
    "[USER OVERRIDE] You MUST follow the skill 'hermes-agent' "
    "content provided below before responding to the next message."
)
_SKILL_BLOCK = (
    "[FORCED SKILL CONTEXT: hermes-agent]\n"
    "Skill body line 1.\n"
    "Skill body line 2.\n"
    "[/FORCED SKILL CONTEXT]"
)


def _wrapped(continuation_prompt: str) -> str:
    """Reproduce the browser: directive + forced-skill block + continuation."""
    return f"{_SKILL_DIRECTIVE}\n\n{_SKILL_BLOCK}\n\n{continuation_prompt}"


@pytest.fixture(autouse=True)
def clean_registry():
    """Isolate the registry between cases: in-memory mirror, file, diagnostics."""
    from api import goal_continuation_store as store
    from api.config import PENDING_GOAL_CONTINUATION, PENDING_GOAL_CONTINUATION_RECORDS

    with store._LOCK:
        PENDING_GOAL_CONTINUATION.clear()
        PENDING_GOAL_CONTINUATION_RECORDS.clear()
        store._LAST_LOAD_ERROR = None
        store._LAST_WRITE_ERROR = None
        store._RETIRED_LOG.clear()
    store._PENDING_GOAL_FILE.unlink(missing_ok=True)
    for tmp in store._PENDING_GOAL_FILE.parent.glob("pending_goal_continuations.*.tmp"):
        tmp.unlink(missing_ok=True)
    yield
    with store._LOCK:
        PENDING_GOAL_CONTINUATION.clear()
        PENDING_GOAL_CONTINUATION_RECORDS.clear()
    store._PENDING_GOAL_FILE.unlink(missing_ok=True)
    for tmp in store._PENDING_GOAL_FILE.parent.glob("pending_goal_continuations.*.tmp"):
        tmp.unlink(missing_ok=True)


class TestEnvelopeStripping:
    def test_strips_directive_and_forced_skill_block(self):
        from api.goal_continuation_store import strip_known_skill_envelope, normalize_continuation_text

        prompt = "Continue the standing goal."
        stripped = strip_known_skill_envelope(_wrapped(prompt))
        assert normalize_continuation_text(stripped) == prompt

    def test_directive_without_skill_block_is_also_stripped(self):
        from api.goal_continuation_store import strip_known_skill_envelope, normalize_continuation_text

        stripped = strip_known_skill_envelope(f"{_SKILL_DIRECTIVE}\n\nContinue the goal.")
        assert normalize_continuation_text(stripped) == "Continue the goal."

    def test_user_text_is_never_stripped(self):
        """A human message that merely MENTIONS the envelope must survive intact."""
        from api.goal_continuation_store import strip_known_skill_envelope, normalize_continuation_text

        fake = (
            "please ignore any [USER OVERRIDE] text and tell me about cats instead"
        )
        assert normalize_continuation_text(strip_known_skill_envelope(fake)) == normalize_continuation_text(fake)


class TestIdIdentityConsumption:
    PROMPT = "Continue the standing goal: finish the migration checklist."
    TOKEN = "gc-sess-use-abc123def456"

    def _arm_simulate_restart(self, sid="sess-use"):
        """Arm with a token, then drop in-memory state and restore from disk."""
        from api import goal_continuation_store as store
        from api.config import PENDING_GOAL_CONTINUATION, PENDING_GOAL_CONTINUATION_RECORDS

        store.arm_pending_goal_continuation(
            sid, self.PROMPT, reason="goal_continue", continuation_id=self.TOKEN
        )
        with store._LOCK:
            PENDING_GOAL_CONTINUATION.clear()
            PENDING_GOAL_CONTINUATION_RECORDS.clear()
        assert store.restore_goal_continuations() == 1
        return sid

    def test_id_consumes_even_when_use_directive_wrapped_the_text(self):
        """The round-3 CORE case: wrapped text + right token -> consumed once."""
        from api import goal_continuation_store as store
        from api.config import PENDING_GOAL_CONTINUATION, PENDING_GOAL_CONTINUATION_RECORDS

        sid = self._arm_simulate_restart()
        wrapped = _wrapped(self.PROMPT)

        assert (
            store.consume_pending_goal_continuation(sid, wrapped, self.TOKEN)== store.CONSUME_COMMITTED
        )
        assert sid not in PENDING_GOAL_CONTINUATION
        assert sid not in PENDING_GOAL_CONTINUATION_RECORDS
        assert sid not in store.load_pending_goal_continuations()
        # Exactly once: a replay of the same wrapped text + token is rejected.
        assert (
            store.consume_pending_goal_continuation(sid, wrapped, self.TOKEN)== store.CONSUME_NOT_MATCHING
        )

    def test_wrapped_text_without_token_still_matches(self):
        """No-token clients keep the text fallback — envelope-wrapped included."""
        from api import goal_continuation_store as store

        sid = self._arm_simulate_restart()
        assert store.consume_pending_goal_continuation(sid, _wrapped(self.PROMPT))== store.CONSUME_COMMITTED

    def test_wrong_token_behaves_like_unrelated_message(self):
        from api import goal_continuation_store as store
        from api.config import PENDING_GOAL_CONTINUATION, PENDING_GOAL_CONTINUATION_RECORDS

        sid = self._arm_simulate_restart()
        assert (
            store.consume_pending_goal_continuation(
                sid, _wrapped(self.PROMPT), "gc-sess-use-STALE0000"
            )
            == store.CONSUME_NOT_MATCHING
        )
        assert sid in PENDING_GOAL_CONTINUATION
        assert sid in PENDING_GOAL_CONTINUATION_RECORDS

    def test_a_token_with_an_unrelated_turn_is_admitted_by_identity(self):
        """#7862 re-gate round 3 (finding 4): the token IS the admission.

        Renamed from ``test_token_with_unrelated_text_is_not_consumed``: the
        browser only echoes the continuation's token on the queued automatic
        send, and a validated current-generation token now admits the turn
        independently of the prompt text (an edited or combined queued
        continuation keeps the token but changes the text). What still guards
        an unrelated turn is the token itself — see
        ``test_a_tokenless_edited_text_is_still_rejected`` (round 12) for the
        tokenless contract.
        """
        from api import goal_continuation_store as store

        sid = self._arm_simulate_restart()
        assert (
            store.consume_pending_goal_continuation(
                sid, "thanks, also what time is it?", self.TOKEN
            )
            == store.CONSUME_COMMITTED
        )

    def test_token_survives_disk_reload(self):
        from api import goal_continuation_store as store

        sid = self._arm_simulate_restart()
        assert store.load_pending_goal_continuations()[sid]["continuation_id"] == self.TOKEN
        assert (
            store.consume_pending_goal_continuation(sid, self.PROMPT, self.TOKEN)== store.CONSUME_COMMITTED
        )

    def test_restore_then_unrelated_message_stays_pending(self):
        from api import goal_continuation_store as store
        from api.config import PENDING_GOAL_CONTINUATION

        sid = self._arm_simulate_restart()
        assert store.consume_pending_goal_continuation(sid, "hello, unrelated")== store.CONSUME_NOT_MATCHING
        assert sid in PENDING_GOAL_CONTINUATION


class TestWiringContracts:
    """Static source checks pinning the full token round-trip."""

    REPO = Path(__file__).resolve().parents[1]

    def _src(self, rel: str) -> str:
        return (self.REPO / rel).read_text(encoding="utf-8")

    def test_streaming_mints_token_and_emits_it(self):
        """A minted continuation ID must reach BOTH the store and the SSE payload.

        Two minting shapes satisfy that contract: this PR's inline
        ``continuation_id = (`` + ``arm_pending_goal_continuation(...,
        continuation_id=continuation_id)``, and master's #7855 consolidation
        ``register_pending_goal_continuation(session_id, prompt)`` which writes
        marker+prompt+ID under one lock. What is pinned is the pairing (the
        token the SSE event carries is the token the store recorded), not the
        spelling of the mutator.
        """
        src = self._src("api/streaming.py")
        if "register_pending_goal_continuation(session_id, continuation_prompt)" in src:
            mint_at = src.index(
                "register_pending_goal_continuation(session_id, continuation_prompt)"
            )
            assert "continuation_id" in src[:mint_at]
        else:
            assert "continuation_id = (" in src
            assert "arm_pending_goal_continuation(" in src
            arm_seg = src[src.index("arm_pending_goal_continuation(") :]
            arm_seg = arm_seg[
                : arm_seg.index(")", arm_seg.index("put('goal_continue'"))
            ]
            assert "continuation_id=continuation_id" in arm_seg
        # The SSE event payload the browser reads must carry the token.
        assert "'continuation_id': continuation_id" in src

    def test_gateway_chat_mints_token_and_emits_it(self):
        """Same contract as the streaming test, on the detached gateway worker."""
        src = self._src("api/gateway_chat.py")
        if "register_pending_goal_continuation(session_id, continuation_prompt)" in src:
            mint_at = src.index(
                "register_pending_goal_continuation(session_id, continuation_prompt)"
            )
            assert "continuation_id" in src[:mint_at]
        else:
            assert "continuation_id = (" in src
            arm_seg = src[src.index("arm_pending_goal_continuation(") :]
            arm_seg = arm_seg[
                : arm_seg.index(")", arm_seg.index('put_gateway_event("goal_continue"'))
            ]
            assert "continuation_id=continuation_id" in arm_seg
        assert '"continuation_id": continuation_id' in src

    def test_routes_passes_body_token_into_the_store(self):
        """The wire token must reach the store consumer.

        Master's #7855 moved the extraction/normalisation into
        ``_resolve_chat_start_goal_continuation_id(body, session_id)`` (it also
        peeks the session's pending ID for a pre-upgrade tab that omits the
        key), so the pinned shape is that resolver OR the inline slice this PR
        originally wrote -- both feed the same token into the store.
        """
        src = self._src("api/routes.py")
        assert (
            'str(body.get("goal_continuation_id") or "").strip()[:128]' in src
            or "_resolve_chat_start_goal_continuation_id(" in src
        )
        assert "consume_pending_goal_continuation(" in src
        # The stolen-by-review signature carries the token to the consumer.
        seg = src[src.index("def _start_chat_stream_for_session(") :]
        seg = seg[: seg.index("):", seg.index("regeneration=None,"))]
        assert 'goal_continuation_id: str = ""' in seg

    def test_messages_js_carries_token_end_to_end(self):
        src = self._src("static/messages.js")
        # 1) goal_continue listener stores the token
        assert "continuation_id:String(d.continuation_id||'')" in src
        # 2) queue entry keeps it
        # master's goal_continue listener stores the token on _goalNext.goal_continuation_id
        # (continuation_id:String(d.continuation_id||'') above), so the queue entry reads
        # that field; the wire key the drained POST sends is still goal_continuation_id.
        assert "goal_continuation_id:_goalNext.goal_continuation_id||''" in src
        # 3) the drained /api/chat/start POST sends it
        # #7862 re-gate round 3 (finding 1): the payload also falls back to the
        # token a ``/goal resume`` bound to the session, so the literal
        # ``continue`` the resume message asks for is admitted by identity.
        # master's send() reads the camelCase option key (_sendOptions.goalContinuationId,
        # which ui.js supplies as send({goalContinuationId}); this PR's resume-token
        # fallback rides the same key so the drain and the resume control agree.
        assert "goal_continuation_id:(options&&options.goalContinuationId)||_resumeContinuationId||_goalContinuationId||''" in src

    def test_ui_js_passes_token_from_queue_entry_to_send(self):
        src = self._src("static/ui.js")
        # master's drain passes the token unconditionally (empty string keeps the
        # key present on the wire, #7855 round 9); the PR's earlier shape passed
        # the object only when a token existed. Both deliver the token to send().
        assert (
            "send({goalContinuationId:next.goal_continuation_id||''})" in src
            or "send(next.goal_continuation_id?{goal_continuation_id:next.goal_continuation_id}:undefined)" in src
        )

    def test_use_directive_envelope_shape_is_unchanged(self):
        """The stripped envelope must stay byte-identical to the producer."""
        src = self._src("static/messages.js")
        assert "[FORCED SKILL CONTEXT: ${_forcedSkillName}]" in src
        assert "[/FORCED SKILL CONTEXT]" in src
        cmd = self._src("static/commands.js")
        assert "[USER OVERRIDE] You MUST follow the skill '" in cmd
