"""#7855: the goal-continuation record is admitted by its continuation ID.

#6885's distinction still holds — a genuine user turn must never inherit
goal-continuation semantics — but the mechanism changed. The previous
mechanism matched the incoming text against the recorded prompt, and the
browser's own queue broke it twice (maintainer review 2026-09-29):

1. A continuation sent after the TTL expired was dropped and became an
   ordinary turn, silently ending the goal loop. The browser deliberately
   keeps a queued continuation — it survives a refresh and is restored into
   the composer for a later send.
2. Editing or combining a queued entry changed its text, so the exact
   comparison rejected it and the goal loop ended there.

Admission is now by a server-issued continuation ID
(``register_pending_goal_continuation`` returns it; the goal_continue SSE
event carries it; the browser keeps it on the queued entry through inline
edits and combines; /api/chat/start validates it). A genuine user message
never carries an ID, which is the #6885 distinction.

There is no wall-clock expiry: a record lives until it is consumed or
explicitly retired (goal clear / pause / session retirement).
"""

import re
import uuid
from pathlib import Path

import pytest

from api.config import (
    PENDING_GOAL_CONTINUATION,
    PENDING_GOAL_CONTINUATION_PROMPTS,
)
from api.goals import (
    _goal_continuation_normalize_wire_text,
    clear_pending_goal_continuation,
    consume_pending_goal_continuation,
    peek_pending_goal_continuation_id,
    register_pending_goal_continuation,
    restore_pending_goal_continuation,
    sweep_expired_goal_continuations,
)
from api.routes import _consume_pending_goal_continuation, _normalize_goal_continuation_id


@pytest.fixture(autouse=True)
def _clean_markers():
    PENDING_GOAL_CONTINUATION.clear()
    PENDING_GOAL_CONTINUATION_PROMPTS.clear()
    yield
    PENDING_GOAL_CONTINUATION.clear()
    PENDING_GOAL_CONTINUATION_PROMPTS.clear()


def _register(session_id: str, prompt: str) -> str:
    """Register a record and return its continuation ID."""
    cont_id = register_pending_goal_continuation(session_id, prompt)
    assert cont_id, "register must return a continuation ID for a non-empty prompt"
    return cont_id


class TestRecordAdmission:
    def test_record_consumed_by_its_id(self):
        """The browser dispatch carrying the issued ID consumes the record
        and the turn becomes goal-related."""
        cont_id = _register("s1", "continue step 2")
        receipt = consume_pending_goal_continuation("s1", cont_id)
        # #7855: a successful admission returns the popped record as the
        # rollback receipt, not a bare True.
        assert receipt and receipt.get("continuation_id") == cont_id
        assert "s1" not in PENDING_GOAL_CONTINUATION
        assert "s1" not in PENDING_GOAL_CONTINUATION_PROMPTS

    def test_record_is_single_use(self):
        cont_id = _register("s1", "continue step 2")
        receipt = consume_pending_goal_continuation("s1", cont_id)
        # #7855: a successful admission returns the popped record as the
        # rollback receipt, not a bare True.
        assert receipt and receipt.get("continuation_id") == cont_id
        assert consume_pending_goal_continuation("s1", cont_id) is False

    def test_prompt_text_does_not_admit(self):
        """The #7855 mechanism: text is not the admission token, so even the
        verbatim prompt no longer consumes the record. Only the ID does."""
        _register("s1", "continue step 2")
        assert consume_pending_goal_continuation("s1", "continue step 2") is False
        assert "s1" in PENDING_GOAL_CONTINUATION

    def test_wrong_id_does_not_admit_and_keeps_record(self):
        cont_id = _register("s1", "continue step 2")
        other = uuid.uuid4().hex
        assert other != cont_id
        assert consume_pending_goal_continuation("s1", other) is False
        assert "s1" in PENDING_GOAL_CONTINUATION
        assert PENDING_GOAL_CONTINUATION_PROMPTS["s1"]["prompt"] == "continue step 2"

    def test_genuine_user_message_does_not_admit(self):
        """#6885: a genuine user turn (no ID) keeps user priority and leaves
        the record for the browser's real continuation dispatch."""
        _register("s1", "continue step 2")
        assert consume_pending_goal_continuation("s1", "帮我总结一下当前进度") is False
        assert "s1" in PENDING_GOAL_CONTINUATION

    def test_empty_id_does_not_admit(self):
        _register("s1", "continue step 2")
        assert consume_pending_goal_continuation("s1", "") is False
        assert consume_pending_goal_continuation("s1", "   ") is False
        assert "s1" in PENDING_GOAL_CONTINUATION

    def test_no_record_returns_false(self):
        assert consume_pending_goal_continuation("s9", uuid.uuid4().hex) is False

    def test_marker_without_record_is_cleared_fail_closed(self):
        """A marker present without a record (legacy/abnormal state) must fail
        closed — not consumed — and the broken pair must not get stuck."""
        PENDING_GOAL_CONTINUATION.add("s1")
        assert consume_pending_goal_continuation("s1", uuid.uuid4().hex) is False
        assert "s1" not in PENDING_GOAL_CONTINUATION

    def test_register_rejects_empty_prompt(self):
        """A blank continuation prompt must not create a half-record; the SSE
        event is gated on this return value so the frontend queue and the
        server record cannot disagree."""
        assert register_pending_goal_continuation("s1", "   ") is None
        assert "s1" not in PENDING_GOAL_CONTINUATION
        assert "s1" not in PENDING_GOAL_CONTINUATION_PROMPTS

    def test_records_get_distinct_ids(self):
        a = _register("s1", "a")
        b = _register("s2", "b")
        assert a != b
        assert peek_pending_goal_continuation_id("s1") == a
        assert peek_pending_goal_continuation_id("s2") == b
        assert peek_pending_goal_continuation_id("s-missing") is None

    def test_record_shape_carries_prompt_and_id(self):
        cont_id = _register("s1", "continue step 2")
        record = PENDING_GOAL_CONTINUATION_PROMPTS["s1"]
        assert record["prompt"] == "continue step 2"
        assert record["continuation_id"] == cont_id
        assert len(cont_id) == 32


class TestLateSendIsStillAdmitted:
    """Maintainer blocker 1: a continuation sent after 30 minutes must keep
    its goal semantics. The browser deliberately keeps a queued
    continuation (it survives a refresh and is restored into the composer),
    so there is no wall-clock expiry."""

    def test_late_send_still_admitted(self):
        """No clock manipulation needed: the contract has no expiry, so a
        send at any later time is admitted as long as it carries the ID."""
        cont_id = _register("s1", "continue step 2")
        # Any amount of simulated wall-clock time later — the record has no
        # expires_at to compare against.
        receipt = consume_pending_goal_continuation("s1", cont_id)
        # #7855: a successful admission returns the popped record as the
        # rollback receipt, not a bare True.
        assert receipt and receipt.get("continuation_id") == cont_id

    def test_record_has_no_expiry_field(self):
        _register("s1", "continue step 2")
        assert "expires_at" not in PENDING_GOAL_CONTINUATION_PROMPTS["s1"]

    def test_restored_continuation_from_refresh_is_admitted(self):
        """The refresh-restore path (loadSession) re-queues the entry with its
        ID intact; that later send must still admit."""
        cont_id = _register("s1", "continue step 2")
        # Simulate the browser restoring the queue from storage after a
        # refresh: same text, same ID.
        receipt = consume_pending_goal_continuation("s1", cont_id)
        # #7855: a successful admission returns the popped record as the
        # rollback receipt, not a bare True.
        assert receipt and receipt.get("continuation_id") == cont_id


class TestEditedAndCombinedAreAdmitted:
    """Maintainer blocker 2: editing or combining a queued continuation must
    not silently end the goal. The ID survives the edit, so the turn is
    admitted regardless of the resulting text."""

    def test_edited_continuation_is_admitted(self):
        """The user rewrites the queued text; the ID is untouched, so the
        turn still consumes the record."""
        cont_id = _register("s-edit", "original prompt")
        # The browser sends the EDITED text, but carries the ID.
        assert consume_pending_goal_continuation("s-edit", cont_id)
        assert "s-edit" not in PENDING_GOAL_CONTINUATION

    def test_combined_continuation_is_admitted(self):
        """Combining a continuation with another entry keeps the first ID."""
        cont_id = _register("s-combine", "step 2")
        # The combined entry keeps the first continuation ID (ui.js _doMerge).
        assert consume_pending_goal_continuation("s-combine", cont_id)
        assert "s-combine" not in PENDING_GOAL_CONTINUATION

    def test_combined_pure_user_messages_stay_ordinary(self):
        """A combine of entries that carry no continuation ID must NOT be
        admitted: the #6885 distinction survives the queue path."""
        _register("s-combine2", "step 2")
        # The user combines two entries that are not the continuation — the
        # merged entry has no ID (ui.js keeps the FIRST id, which is absent).
        assert consume_pending_goal_continuation("s-combine2", "hello\n\nworld") is False
        assert "s-combine2" in PENDING_GOAL_CONTINUATION


class TestWireTextNormalization:
    """Kept from #6885 round 2: the normalizer still exists and is still used
    for canonical-semantic display/normalization of the continuation text,
    but it is no longer the admission predicate."""

    def test_forced_skill_envelope_is_stripped(self):
        wire = (
            "[USER OVERRIDE] You MUST follow the skill 'writing' content provided below "
            "before responding to the next message.\n\n"
            "[FORCED SKILL CONTEXT: writing]\nskill body here\n[/FORCED SKILL CONTEXT]\n\n"
            "continue step 2"
        )
        assert _goal_continuation_normalize_wire_text(wire) == "continue step 2"

    def test_attached_files_tail_is_stripped(self):
        wire = "continue step 2\n\n[Attached files: /tmp/a.txt, /tmp/b.py]"
        assert _goal_continuation_normalize_wire_text(wire) == "continue step 2"

    def test_user_authored_lookalike_envelope_in_body_is_not_stripped(self):
        wire = (
            "please explain\n[FORCED SKILL CONTEXT: fake]\nx\n[/FORCED SKILL CONTEXT]\nthanks"
        )
        assert _goal_continuation_normalize_wire_text(wire) == wire


class TestRecordLifecycle:
    def test_no_record_survives_a_goal_clear(self):
        """Explicit retirement (goal clear / pause) still drops the record."""
        _register("s1", "continue step 2")
        clear_pending_goal_continuation("s1")
        assert "s1" not in PENDING_GOAL_CONTINUATION
        assert "s1" not in PENDING_GOAL_CONTINUATION_PROMPTS

    def test_sweep_clears_orphaned_marker(self):
        PENDING_GOAL_CONTINUATION.add("s-orphan")
        swept = sweep_expired_goal_continuations()
        assert swept == 1
        assert "s-orphan" not in PENDING_GOAL_CONTINUATION

    def test_sweep_keeps_valid_records(self):
        _register("s1", "a")
        _register("s2", "b")
        swept = sweep_expired_goal_continuations()
        assert swept == 0
        assert "s1" in PENDING_GOAL_CONTINUATION
        assert "s2" in PENDING_GOAL_CONTINUATION

    def test_sweep_clears_record_without_id(self):
        """A legacy pre-#7855 record (no continuation_id) is unmatchable and
        must be cleared rather than left to block the session."""
        _register("s1", "a")
        del PENDING_GOAL_CONTINUATION_PROMPTS["s1"]["continuation_id"]
        swept = sweep_expired_goal_continuations()
        assert swept == 1
        assert "s1" not in PENDING_GOAL_CONTINUATION


class TestContinuationIdNormalization:
    def test_valid_hex_token_survives(self):
        token = uuid.uuid4().hex
        assert _normalize_goal_continuation_id(token) == token

    def test_uppercase_is_lowered(self):
        token = uuid.uuid4().hex.upper()
        assert _normalize_goal_continuation_id(token) == token.lower()

    def test_none_and_blank_become_empty(self):
        assert _normalize_goal_continuation_id(None) == ""
        assert _normalize_goal_continuation_id("") == ""
        assert _normalize_goal_continuation_id("   ") == ""

    @pytest.mark.parametrize(
        "bad",
        [
            "not-hex",
            "xyz",
            "a" * 31,
            "a" * 33,
            "../../etc/passwd",
            "'; DROP TABLE sessions;--",
            ("a" * 64),
            42,
        ],
    )
    def test_malformed_values_rejected_as_no_id(self, bad):
        """A hostile or malformed value must degrade to "no ID" (the genuine
        user-turn signal) rather than reach the matcher."""
        assert _normalize_goal_continuation_id(bad) == ""


class TestWriterWiring:
    def test_streaming_registers_one_record(self):
        src = Path(__file__).parents[1].joinpath("api", "streaming.py").read_text(encoding="utf-8")
        assert "register_pending_goal_continuation(session_id, continuation_prompt)" in src, (
            "streaming.py must register the goal-continuation record through "
            "api.goals.register_pending_goal_continuation"
        )
        assert "PENDING_GOAL_CONTINUATION_PROMPTS[session_id]" not in src, (
            "streaming.py must not write the prompt map directly — the record "
            "is one object maintained by api.goals"
        )
        assert "'continuation_id': continuation_id" in src, (
            "streaming.py must carry the continuation ID on the goal_continue event"
        )

    def test_gateway_registers_one_record(self):
        src = Path(__file__).parents[1].joinpath("api", "gateway_chat.py").read_text(encoding="utf-8")
        assert "register_pending_goal_continuation(session_id, continuation_prompt)" in src, (
            "gateway_chat.py must register the goal-continuation record through "
            "api.goals.register_pending_goal_continuation"
        )
        assert "PENDING_GOAL_CONTINUATION_PROMPTS[session_id]" not in src, (
            "gateway_chat.py must not write the prompt map directly — the record "
            "is one object maintained by api.goals"
        )
        assert '"continuation_id": continuation_id' in src, (
            "gateway_chat.py must carry the continuation ID on the goal_continue event"
        )

    def test_routes_consumer_admits_by_id(self):
        """routes.py must admit via the helper using the continuation ID, not
        the message text."""
        src = Path(__file__).parents[1].joinpath("api", "routes.py").read_text(encoding="utf-8")
        # #7855 rebase compat (#7249): admission moved INSIDE the session lock
        # (consume_continuation_markers), replacing the bare marker check, and
        # keeps the popped record as a rollback receipt.
        m = re.search(
            r"def consume_continuation_markers\(\).*?"
            r"if not goal_related and goal_continuation_id:\s*\n\s*"
            r"receipt = _consume_pending_goal_continuation\(",
            src,
            re.S,
        )
        assert m is not None, (
            "routes.py admission must use the continuation ID inside "
            "consume_continuation_markers(): "
            "if not goal_related and goal_continuation_id: "
            "receipt = _consume_pending_goal_continuation(...)"
        )
        assert "consumed_goal_continuation_receipt = receipt" in src, (
            "consume_continuation_markers() must keep the popped record as a "
            "rollback receipt so a rejected start can restore both halves"
        )
        assert "restore_pending_goal_continuation(s.session_id," in src, (
            "restore_consumed_continuation_markers() must restore the record "
            "as well as the marker (#7862: marker-only rollback breaks the retry)"
        )
        direct = re.findall(r"PENDING_GOAL_CONTINUATION\.discard", src)
        # master's consume_continuation_markers() closure still consumes the
        # legacy #1932 SET marker for the plain no-id path; that is a different
        # object from the id-keyed record api.goals owns. Everything else must
        # delegate to the helper.
        allowed = re.findall(
            r"def consume_continuation_markers\(\)[^}]*?PENDING_GOAL_CONTINUATION\.discard", src, re.S
        )
        assert len(direct) == len(allowed), (
            f"PENDING_GOAL_CONTINUATION.discard must not appear in routes.py "
            f"(record removal is owned by api.goals); found {len(direct)}, of "
            f"which {len(allowed)} sit inside the legacy "
            f"consume_continuation_markers() closure"
        )

    def test_routes_normalizes_the_client_id(self):
        """The client-supplied ID must be normalized (shape-checked) before it
        reaches the matcher."""
        src = Path(__file__).parents[1].joinpath("api", "routes.py").read_text(encoding="utf-8")
        assert "goal_continuation_id" in src and "_normalize_goal_continuation_id(" in src

    def test_goal_command_hooks_clear_and_sweep(self):
        src = Path(__file__).parents[1].joinpath("api", "routes.py").read_text(encoding="utf-8")
        assert "clear_pending_goal_continuation(s.session_id)" in src, (
            "the /goal command handler must clear a pending continuation on "
            "goal clear/pause"
        )
        assert "sweep_expired_goal_continuations()" in src, (
            "the /goal command handler must sweep orphaned records"
        )

    def test_session_delete_hooks_clear(self):
        src = Path(__file__).parents[1].joinpath("api", "routes.py").read_text(encoding="utf-8")
        assert "clear_pending_goal_continuation(sid)" in src, (
            "session deletion must retire the session's continuation record"
        )

    def test_no_wall_clock_ttl_in_goals_module(self):
        """The record must not carry an expiry any more (#7855 blocker 1)."""
        src = Path(__file__).parents[1].joinpath("api", "goals.py").read_text(encoding="utf-8")
        assert "expires_at" not in src, (
            "api/goals.py must not use a wall-clock expiry for the "
            "goal-continuation record"
        )


class TestFrontendWiring:
    """The browser must carry the ID through the queue and the POST body."""

    def _ui(self) -> str:
        return Path(__file__).parents[1].joinpath("static", "ui.js").read_text(encoding="utf-8")

    def _messages(self) -> str:
        return Path(__file__).parents[1].joinpath("static", "messages.js").read_text(encoding="utf-8")

    def _sessions(self) -> str:
        return Path(__file__).parents[1].joinpath("static", "sessions.js").read_text(encoding="utf-8")

    def test_sse_handler_captures_the_id(self):
        src = self._messages()
        assert "goal_continuation_id:String(d.continuation_id||'').trim()" in src, (
            "the goal_continue SSE handler must capture the continuation ID"
        )

    def test_queue_entry_carries_the_id(self):
        src = self._messages()
        assert "goal_continuation_id:_goalNext.goal_continuation_id||''" in src, (
            "the queued entry must carry the continuation ID"
        )

    def test_drain_hands_the_id_to_that_send_invocation(self):
        # Round 5 (CORE): the ID must reach send() as an ARGUMENT. Publishing it
        # to a module slot let any concurrent send read it, which is how a
        # genuine user turn consumed the pending goal.
        assert "send({goalContinuationId:next.goal_continuation_id||''})" in self._ui(), (
            "the queue drain must pass the entry's continuation ID into send()"
        )

    def test_send_binds_the_id_from_its_argument(self):
        src = self._messages()
        assert "let _goalContinuationId=_normalizeGoalContinuationId(_sendOptions.goalContinuationId)" in src, (
            "send() must bind the continuation ID from its own argument"
        )
        assert "goal_continuation_id:_goalContinuationId||undefined" in src, (
            "send() must post the invocation-bound ID in the /api/chat/start body"
        )

    def test_no_shared_drain_slot_remains(self):
        for name, read in (("static/ui.js", self._ui()), ("static/messages.js", self._messages())):
            for symbol in (
                "_drainingGoalContinuationId",
                "_setDrainingGoalContinuationId",
                "_readDrainingGoalContinuationId",
            ):
                assert symbol not in read, (
                    f"{name} still references the shared drain slot {symbol!r} — "
                    "the round-5 CORE defect"
                )

    def test_rejected_continuation_requeue_keeps_the_id(self):
        # Round 5 item 2: a continuation rejected with "session already has an
        # active stream" used to be re-queued WITHOUT its ID, so the retry became
        # an ordinary turn and the goal loop ended silently.
        src = self._messages()
        assert "if(_retryContId) _retryEntry.goal_continuation_id=_retryContId;" in src, (
            "the active-stream requeue must carry the continuation ID onto the entry"
        )

    def test_restored_continuation_is_a_text_bound_draft(self):
        src = self._messages()
        restore = self._sessions()
        assert "_setRestoredGoalContinuationDraft" in restore, (
            "a refresh-restored continuation must be recorded as a draft"
        )
        assert "String(text).trim()!==_draftText" in src, (
            "a restored draft must be dropped when the user replaces the text"
        )
        assert "delete _msg.dataset.goalContinuationId" in src, (
            "a restored draft must be one-shot and leave nothing behind"
        )

    def test_edit_path_preserves_the_id(self):
        assert "liveQ[idx]={...liveQ[idx],text:newText}" in self._ui(), (
            "the inline-edit path must preserve the entry's continuation ID"
        )

    def test_combine_path_keeps_the_first_id(self):
        src = self._ui()
        assert "goal_continuation_id:_contId" in src, (
            "the combine path must keep the first continuation ID"
        )
        assert "snapshot.find(e=>e&&String(e.goal_continuation_id||'').trim())" in src, (
            "the combine path must search the merged entries for an ID"
        )


class TestRoutesHelperDelegation:
    def test_helper_delegates_to_goals_module(self):
        cont_id = _register("s1", "continue step 2")
        assert _consume_pending_goal_continuation("s1", cont_id)
        assert "s1" not in PENDING_GOAL_CONTINUATION

    def test_helper_returns_false_for_genuine_user_message(self):
        _register("s1", "continue step 2")
        assert _consume_pending_goal_continuation("s1", "hi there") is False
        assert "s1" in PENDING_GOAL_CONTINUATION


class TestProductionShapedDispatch:
    """Maintainer's requested shape: begin from the goal_continue payload,
    carry the ID through the queue (edit / combine / late send), then pass it
    through the routes helper — admitted exactly once."""

    def _goal_continue_payload(self, session_id: str, prompt: str) -> dict:
        cont_id = register_pending_goal_continuation(session_id, prompt)
        assert cont_id
        return {
            "session_id": session_id,
            "continuation_prompt": prompt,
            "continuation_id": cont_id,
        }

    def test_plain_dispatch_is_admitted_once(self):
        payload = self._goal_continue_payload("s1", "Continue refining the parser module.")
        assert _consume_pending_goal_continuation("s1", payload["continuation_id"])
        assert "s1" not in PENDING_GOAL_CONTINUATION
        # a second identical dispatch does not re-consume (single-use record)
        assert _consume_pending_goal_continuation("s1", payload["continuation_id"]) is False

    def test_genuine_message_while_continuation_pending_stays_ordinary(self):
        """The requested case: a real user message sent while a continuation
        is pending must NOT inherit goal semantics."""
        payload = self._goal_continue_payload("s1", "Continue refining the parser module.")
        assert _consume_pending_goal_continuation("s1", "what is the status?") is False
        assert "s1" in PENDING_GOAL_CONTINUATION
        # the record is still there for the browser's real dispatch
        assert _consume_pending_goal_continuation("s1", payload["continuation_id"])

    def test_edited_queue_entry_still_admitted(self):
        payload = self._goal_continue_payload("s1", "Continue step 2")
        edited_text = "Continue step 2, and also fix the typos I noticed"
        # The queue rewrites the text; the ID is untouched.
        assert _consume_pending_goal_continuation("s1", payload["continuation_id"])
        assert edited_text  # text is irrelevant to admission now

    def test_combined_queue_entry_still_admitted(self):
        payload = self._goal_continue_payload("s1", "Continue step 2")
        assert _consume_pending_goal_continuation("s1", payload["continuation_id"])

    def test_late_dispatch_after_refresh_still_admitted(self):
        payload = self._goal_continue_payload("s1", "Continue step 2")
        # A refresh-restored queue entry re-drains with the same ID.
        assert _consume_pending_goal_continuation("s1", payload["continuation_id"])


class TestRollbackReceipt:
    """#7855 rebase compat (#7249): a rejected chat start rolls back the whole
    admission.

    Master's #7249 consumes the continuation marker inside the session lock
    and restores it when a start is rejected. Admission under #7855 pops the
    *record* as well as the marker, so restoring only the marker — the
    pre-#7855 shape — left the retry unmatchable: consume -> rollback -> retry
    consume returned False and the continuation was gone (the failure the
    maintainer reproduced on #7862 with the real store). The popped record
    therefore travels as a rollback receipt and both halves are restored
    together, unless a newer continuation was armed meanwhile.
    """

    def test_rollback_keeps_the_retry_matchable(self):
        cont_id = _register("s1", "continue step 2")
        receipt = consume_pending_goal_continuation("s1", cont_id)
        assert receipt, "admission must return a rollback receipt"
        # the start is rejected; #7249's rollback runs
        assert restore_pending_goal_continuation("s1", receipt) is True
        assert "s1" in PENDING_GOAL_CONTINUATION
        assert PENDING_GOAL_CONTINUATION_PROMPTS["s1"]["continuation_id"] == cont_id
        # the retry must still admit by the same ID (this returned False before
        # the receipt existed)
        retry = consume_pending_goal_continuation("s1", cont_id)
        assert retry and retry.get("continuation_id") == cont_id
        assert "s1" not in PENDING_GOAL_CONTINUATION
        assert "s1" not in PENDING_GOAL_CONTINUATION_PROMPTS

    def test_marker_only_rollback_would_break_the_retry(self):
        """Documents the pre-#7855 failure mode so the regression is pinned:
        re-adding the marker alone must NOT make the retry matchable."""
        cont_id = _register("s1", "continue step 2")
        receipt = consume_pending_goal_continuation("s1", cont_id)
        assert receipt
        # marker-only rollback (the shape #7249 had before the receipt)
        PENDING_GOAL_CONTINUATION.add("s1")
        assert "s1" not in PENDING_GOAL_CONTINUATION_PROMPTS
        # admission now fails closed and clears the broken half
        assert consume_pending_goal_continuation("s1", cont_id) is False

    def test_rollback_does_not_clobber_a_newer_continuation(self):
        cont_id = _register("s1", "continue step 2")
        receipt = consume_pending_goal_continuation("s1", cont_id)
        assert receipt
        # while the start was in flight, the goal advanced and armed a new
        # continuation for the same session
        newer_id = _register("s1", "continue step 3")
        assert newer_id != cont_id
        assert restore_pending_goal_continuation("s1", receipt) is False
        assert PENDING_GOAL_CONTINUATION_PROMPTS["s1"]["continuation_id"] == newer_id
        # the newer continuation still works; the stale receipt did not burn it
        assert consume_pending_goal_continuation("s1", newer_id)

    def test_rollback_without_receipt_is_a_noop(self):
        cont_id = _register("s1", "continue step 2")
        receipt = consume_pending_goal_continuation("s1", cont_id)
        assert receipt
        assert restore_pending_goal_continuation("s1", None) is False
        assert restore_pending_goal_continuation("s1", False) is False
        assert "s1" not in PENDING_GOAL_CONTINUATION
        assert "s1" not in PENDING_GOAL_CONTINUATION_PROMPTS
        _ = cont_id

    def test_rollback_is_idempotent_for_one_session(self):
        cont_id = _register("s1", "continue step 2")
        receipt = consume_pending_goal_continuation("s1", cont_id)
        assert receipt
        assert restore_pending_goal_continuation("s1", receipt) is True
        # a second rollback of the same receipt finds the record already armed
        assert restore_pending_goal_continuation("s1", receipt) is False
        assert PENDING_GOAL_CONTINUATION_PROMPTS["s1"]["continuation_id"] == cont_id
