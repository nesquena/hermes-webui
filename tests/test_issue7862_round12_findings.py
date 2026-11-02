"""#7862 re-gate round 3 (Oct 14): the five open findings at a37c6f2257d8.

The maintainer re-gated at exact head ``a37c6f2257d8`` with five findings:

1. **Pause -> resume -> ``continue`` stops the goal loop.** The route's
   active-goal check reads ``goal_state_snapshot`` and requires a dict with
   ``status == "active"``; the native snapshot is a ``GoalState`` object (not
   a dict) and a beta-profile goal reads as ``None`` without ``profile_home``.
   ``/goal resume`` also returns no continuation token, so the literal
   ``continue`` the resume message tells the user to type carries no identity
   and the store's token-gated admission refuses it. Fix: profile-scoped
   ``has_active_goal()`` in the route, the pending token on the resume
   payload, and a frontend binding of that token to the session's next
   literal ``continue`` send.

2. **Failed persistence can resurrect an already-launched continuation.**
   The launch path drops the return value of ``record_goal_continuation_stream``,
   so an ENOSPC on the stream-identity write still launches the worker with a
   200, and a restart restores the continuation (turn runs twice). The
   regeneration path never binds a stream id at all. Fix: check the binding
   result and refuse/compensate the launch on failure; pass the attempt
   identity into regeneration and bind it durably before releasing its worker.

3. **A retried continuation can be lost or come back as a phantom.** The
   registry writer suppresses a session-wide ``_ADOPTED_HANDOFF_SESSIONS``
   set, which also hides the RETRY's fresh handoff (a crash after the retry
   consume restores nothing); and a discharge clears the session from the set
   while the original adopted token is still in memory, so an unrelated write
   republishes that token as ``{}`` (inner field filter) and a cold restore
   adopts a phantom with an empty prompt and token. Fix: remove the
   session-wide suppression and the inner field filter; filter whole handoff
   entries by their adoption marker instead.

4. **Editing or combining a queued continuation ends the goal loop.** The
   store admission still requires the recorded prompt text even when the
   request carries this continuation's validated token, and the queue
   Combine action drops the token from the merged entry. Fix: admit a
   validated current-generation token independently of prompt text (tokenless
   clients keep the text match), and carry the token through Combine.

5. **A crash after recording the stream id but before ``thr.start()`` loses
   the continuation.** Recovery treats an allocated stream id as proof of
   execution and drops the handoff, although no worker ever ran. Fix:
   reconcile the recorded attempt against durable run/turn state (the
   journals the WORKER writes), and keep recovery for an attempt that never
   launched.

RED control: each case fails on the pre-fix store/routes/frontend.
"""

from __future__ import annotations

import re as _re

import pytest

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")

PROMPT = "keep refining the release checklist"
TOKEN = "tok-r12-1"


@pytest.fixture
def clean_registry():
    """Isolate the registry between cases (same shape as round 7/11)."""
    from api import goal_continuation_store as store
    from api.config import PENDING_GOAL_CONTINUATION, PENDING_GOAL_CONTINUATION_RECORDS

    def _reset():
        with store._LOCK:
            PENDING_GOAL_CONTINUATION.clear()
            PENDING_GOAL_CONTINUATION_RECORDS.clear()
            store._LAST_LOAD_ERROR = None
            store._LAST_WRITE_ERROR = None
            store._RETIRED_LOG.clear()
            store._ROLLBACK_RECEIPTS.clear()
            store._RETIRED_GENERATIONS.clear()
            store._TOMBSTONES.clear()
            store._CONTINUATION_HANDOFF_TOKENS.clear()
            store._ADOPTED_HANDOFF_SESSIONS.clear()
            store._GENERATION = 0
        store._PENDING_GOAL_FILE.unlink(missing_ok=True)

    _reset()
    yield store
    _reset()


def _cold_restore(store):
    """Simulate the cold-process half of a restart (round-11 pattern)."""
    with store._LOCK:
        store._CONTINUATION_HANDOFF_TOKENS.clear()
        store._ADOPTED_HANDOFF_SESSIONS.clear()
        store._ROLLBACK_RECEIPTS.clear()
        store._GENERATION = 0
    return store.restore_goal_continuations()


def _handoffs_on_disk(store):
    _records, _tombstones, handoffs = store._load_file_raw()
    return handoffs


def _arm_and_consume(store, sid, attempt_id, token=TOKEN, prompt=PROMPT):
    store.arm_pending_goal_continuation(sid, prompt, continuation_id=token)
    assert (
        store.consume_pending_goal_continuation(sid, prompt, token, attempt_id)
        == store.CONSUME_COMMITTED
    )


def _age_handoffs(store, sid):
    """Age all of *sid*'s durable handoffs past the owner-liveness window.

    #7862 re-gate round 15 (CORE finding 2): adoption is gated on PROOF that
    the owning process is gone — a freshly minted handoff is left alone on a
    cold restore (its process may be mid-admission). These crash scenarios
    first age the durable liveness clock the way a crashed owner that stopped
    reclaiming would, so the restart's adoption/reconciliation still runs.
    """
    from api.config import PENDING_GOAL_CONTINUATION_RECORDS

    with store._LOCK:
        for key in [k for k in store._CONTINUATION_HANDOFF_TOKENS if k[0] == sid]:
            store._CONTINUATION_HANDOFF_TOKENS[key]["_handoff_minted_at"] = (
                store.time.time() - store._ROLLBACK_RECEIPT_TTL_SECONDS - 60
            )
    with store._LOCK:
        store._write_registry_unlocked(PENDING_GOAL_CONTINUATION_RECORDS, context="aging")


def _routes_scope(start=0, length=70000):
    import api.routes as routes

    src = _re.sub(r"\s+", " ", open(routes.__file__, encoding="utf-8").read())
    body = _re.search(r"def _start_chat_stream_for_session\(", src)
    assert body is not None
    return src[body.start() + start: body.start() + start + length]


def _first_match(text, pattern):
    return _re.search(pattern, text, _re.DOTALL)


# ---------------------------------------------------------------------------
# Finding 3: the writer must not suppress the retry's fresh handoff, and must
# never republish an adopted handoff as a phantom.
# ---------------------------------------------------------------------------


class TestRetriedContinuationDurability:
    """The session-wide suppression hides the RETRY's own handoff."""

    def test_the_retrys_handoff_is_persisted_after_an_adoption(self, clean_registry):
        store = clean_registry
        sid = "sess-r12-retry-handoff"

        _arm_and_consume(store, sid, "att-1")
        # The crashed owner's liveness clock goes stale (round 15, CORE
        # finding 2) before restart 1 adopts the handoff.
        _age_handoffs(store, sid)
        assert _cold_restore(store) == 1, "restart 1 must adopt the handoff"

        # The retry consumes the restored record under a NEW attempt.
        assert (
            store.consume_pending_goal_continuation(sid, PROMPT, TOKEN, "att-2")
            == store.CONSUME_COMMITTED
        )
        # Its handoff must be durable: a second crash before the launch would
        # otherwise restore nothing and the goal loop silently dies.
        handoffs = _handoffs_on_disk(store)
        assert (sid, "att-2") in handoffs, (
            "the retry's handoff was suppressed from the durable snapshot by "
            "the session-wide adoption marker (finding 3: retried continuation "
            "is lost)"
        )
        # ...and a second cold restore still recovers it (the retry's OWN
        # crash owner is also proven gone first).
        _age_handoffs(store, sid)
        assert _cold_restore(store) == 1
        from api.config import PENDING_GOAL_CONTINUATION_RECORDS

        assert sid in PENDING_GOAL_CONTINUATION_RECORDS

    def test_an_adopted_handoff_is_never_republished_as_a_phantom(
        self, clean_registry
    ):
        store = clean_registry
        sid = "sess-r12-phantom"

        _arm_and_consume(store, sid, "att-1")
        # The crashed owner's liveness clock goes stale (round 15, CORE
        # finding 2) before restart 1 adopts the handoff.
        _age_handoffs(store, sid)
        assert _cold_restore(store) == 1, "restart 1 must adopt the handoff"

        # The retry consumes and its launch discharge lands. The discharge
        # clears the session from the adopted set while the ORIGINAL adopted
        # token is still in memory (it anchors the receipt liveness clock).
        assert (
            store.consume_pending_goal_continuation(sid, PROMPT, TOKEN, "att-2")
            == store.CONSUME_COMMITTED
        )
        assert store.discard_goal_continuation_handoff(sid, "att-2") is True

        # An UNRELATED registry write snapshots every in-memory token.
        assert store.arm_pending_goal_continuation("sess-r12-other", "other goal")
        handoffs = _handoffs_on_disk(store)
        assert not any(k.startswith(sid + "\u0000") for k in handoffs), (
            "the adopted handoff was republished to disk by an unrelated write "
            "(finding 3: cold restore adopts a phantom with an empty prompt "
            "and token)"
        )
        # A cold restore must therefore restore nothing.
        assert _cold_restore(store) == 0
        from api.config import PENDING_GOAL_CONTINUATION_RECORDS

        assert sid not in PENDING_GOAL_CONTINUATION_RECORDS


# ---------------------------------------------------------------------------
# Finding 4: a validated current-generation token admits independently of
# prompt text; Combine carries the token.
# ---------------------------------------------------------------------------


class TestTokenAdmissionIsTextIndependent:
    """Editing a queued continuation must not stop the goal loop."""

    def test_a_valid_token_consumes_an_edited_continuation(self, clean_registry):
        from api import goal_continuation_store as store
        from api.config import PENDING_GOAL_CONTINUATION_RECORDS

        sid = "sess-r12-edited"
        store.arm_pending_goal_continuation(sid, PROMPT, continuation_id=TOKEN)
        edited = "keep refining and add release notes"
        assert (
            store.consume_pending_goal_continuation(sid, edited, TOKEN)
            == store.CONSUME_COMMITTED
        ), "a validated token must admit even though the text was edited"
        assert sid not in PENDING_GOAL_CONTINUATION_RECORDS

    def test_a_tokenless_edited_text_is_still_rejected(self, clean_registry):
        """Tokenless clients keep the text contract (finding 4)."""
        from api import goal_continuation_store as store
        from api.config import PENDING_GOAL_CONTINUATION_RECORDS

        sid = "sess-r12-edited-tokenless"
        store.arm_pending_goal_continuation(sid, PROMPT, continuation_id=TOKEN)
        edited = "keep refining and add release notes"
        assert (
            store.consume_pending_goal_continuation(sid, edited, "")
            == store.CONSUME_NOT_MATCHING
        )
        assert sid in PENDING_GOAL_CONTINUATION_RECORDS

    def test_a_foreign_generation_token_does_not_admit(self, clean_registry):
        from api import goal_continuation_store as store
        from api.config import PENDING_GOAL_CONTINUATION_RECORDS

        sid = "sess-r12-edited-wrong-token"
        store.arm_pending_goal_continuation(sid, PROMPT, continuation_id=TOKEN)
        assert (
            store.consume_pending_goal_continuation(
                sid, "keep refining and add release notes", "gc-OTHER000000"
            )
            == store.CONSUME_NOT_MATCHING
        )
        assert sid in PENDING_GOAL_CONTINUATION_RECORDS

    def test_the_combine_action_carries_the_continuation_token(self):
        """ui.js Combine must keep the token on the merged queue entry."""
        from pathlib import Path

        src = (Path(__file__).resolve().parents[1] / "static" / "ui.js").read_text(
            encoding="utf-8"
        )
        # Locate the Combine handler by its toast string, then inspect the
        # merge closure body up to the persist call.
        merge_start = src.find("Combine all into one message")
        assert merge_start > 0
        window = src[merge_start: merge_start + 2200]
        assert "goal_continuation_id" in window, (
            "Combine drops the continuation token from the merged queue entry, "
            "so a combined queued continuation sends without identity and the "
            "goal loop stops (finding 4)"
        )
        assert "snapshot" in window, (
            "the merged token must come from the combined entries, not a literal"
        )


# ---------------------------------------------------------------------------
# Finding 1: profile-scoped has_active_goal() + resume returns the token +
# the frontend binds it to the literal continue send.
# ---------------------------------------------------------------------------


class TestResumeReturnsAndBindsTheContinuationToken:
    """``/goal resume`` must hand the pending token to the browser."""

    def test_resume_payload_carries_the_pending_continuation_token(
        self, clean_registry, monkeypatch
    ):
        from api import goal_continuation_store as store
        from api import goals

        sid = "sess-r12-resume"
        store.arm_pending_goal_continuation(sid, PROMPT, continuation_id=TOKEN)

        class _FakeState:
            goal = PROMPT

        class _FakeMgr:
            def resume(self):
                return _FakeState()

            def has_goal(self):
                return True

        monkeypatch.setattr(goals, "_manager", lambda _sid, **kw: _FakeMgr())
        payload = goals.goal_command_payload(sid, "resume")
        assert payload.get("action") == "resume"
        assert payload.get("continuation_id") == TOKEN, (
            "resume must return the pending continuation token so the browser "
            "can bind it to the literal `continue` send (finding 1)"
        )

    def test_resume_without_a_pending_continuation_carries_no_token(
        self, clean_registry, monkeypatch
    ):
        from api import goals

        class _FakeState:
            goal = PROMPT

        class _FakeMgr:
            def resume(self):
                return _FakeState()

        monkeypatch.setattr(goals, "_manager", lambda _sid, **kw: _FakeMgr())
        payload = goals.goal_command_payload("sess-r12-resume-none", "resume")
        assert payload.get("action") == "resume"
        assert not payload.get("continuation_id")

    def test_messages_js_binds_the_resume_token_to_a_continue_send(self):
        from pathlib import Path

        src = (Path(__file__).resolve().parents[1] / "static" / "messages.js").read_text(
            encoding="utf-8"
        )
        ui_src = (Path(__file__).resolve().parents[1] / "static" / "ui.js").read_text(
            encoding="utf-8"
        )
        assert "_RESUME_GOAL_CONTINUATION_TOKENS" in ui_src, (
            "the frontend keeps no per-session resume token holder (finding 1)"
        )
        assert "_claimResumeGoalContinuationToken" in ui_src, (
            "the resume token holder is not claimable, so a stale identity "
            "would ride on an unrelated later turn (finding 1)"
        )
        assert "_claimResumeGoalContinuationToken(activeSid)" in src, (
            "send() never claims the resume-bound continuation token (finding 1)"
        )
        # The bound token must ride on the /api/chat/start payload.
        post = _first_match(
            src,
            r"goal_continuation_id:\s*\(options&&options\.goalContinuationId\)"
            r"\|\|_resumeContinuationId\|\|_goalContinuationId\|\|''",
        )
        assert post is not None, (
            "the chat-start payload does not fall back to the resume-bound "
            "continuation token (finding 1)"
        )

    def test_commands_js_stores_the_resume_token_per_session(self):
        from pathlib import Path

        src = (Path(__file__).resolve().parents[1] / "static" / "commands.js").read_text(
            encoding="utf-8"
        )
        # #7862 re-gate (2026-10-27, SILENT): the resume token must SURVIVE a
        # ``/goal status`` between the resume and the continue. The old pin
        # bound the setter to the raw ``r.continuation_id`` expression, which
        # every /goal response used to re-set (a status payload has none, so
        # it blanked the token). The binding now routes through
        # ``_resumeToken`` and is conditional: update when the response
        # returns an id, clear only for a retiring action.
        assert _re.search(
            r"_resumeToken\s*=\s*r\s*&&\s*r\.continuation_id\s*\?"
            r"\s*String\(\s*r\.continuation_id\s*\)\s*:\s*''",
            src,
        ), (
            "cmdGoal does not read the resume-returned continuation token "
            "into _resumeToken (finding 1)"
        )
        assert _re.search(
            r"_setResumeGoalContinuationToken\(\s*activeSid\s*,\s*_resumeToken\s*\)",
            src,
        ), (
            "cmdGoal does not bind the resume-returned continuation token "
            "per session (finding 1)"
        )
        # The token is cleared ONLY for a retiring action.
        assert _re.search(
            r"String\(\s*\(?\s*r\s*&&\s*r\.action\s*\)?\s*\|\|\s*''\s*\)\s*===\s*'clear'",
            src,
        ), (
            "the resume token is not cleared exclusively for the retiring "
            "clear action; /goal status would blank it again (2026-10-27)"
        )
        # The setter is shared (defined in ui.js); the call above is the binding.
        assert "_setResumeGoalContinuationToken" in src


class TestRouteUsesProfileScopedActiveGoalCheck:
    """The continue admission must certify the goal the profile-aware way."""

    def test_the_route_uses_profile_scoped_has_active_goal(self):
        scope = _routes_scope()
        consume_m = _first_match(
            scope,
            r"consume_pending_goal_continuation\(\s*s\.session_id,\s*msg,\s*"
            r"goal_continuation_id\s*,\s*goal_continuation_attempt_id\s*,"
            r"\s*explicit_continue\s*=\s*\w+\s*,\s*goal_active\s*=\s*\w+\s*,?\s*\)",
        )
        assert consume_m is not None
        before = scope[: consume_m.start()]
        assert "has_active_goal(" in before, (
            "the route must certify the goal via has_active_goal(), not a dict "
            "snapshot check (finding 1: the native snapshot is a GoalState, not "
            "a dict)"
        )
        assert "get_hermes_home_for_profile(" in before, (
            "the goal check must be profile-scoped (finding 1: a beta-profile "
            "goal reads as None without profile_home)"
        )
        assert "goal_state_snapshot(" not in before, (
            "the dict snapshot check is gone: isinstance(GoalState, dict) is "
            "False on the native manager, so a resumed goal never certified"
        )
        # The dict-status comparison must be gone as CODE (a comment may
        # legitimately name the old literal when explaining the defect).
        code_before = _re.sub(r'""".*?"""|#[^\n]*', "", before, flags=_re.DOTALL)
        assert "isinstance(" not in code_before and '"active"' not in code_before, (
            "the dict-status check is still live code"
        )


# ---------------------------------------------------------------------------
# Finding 2: both launch paths must refuse when the stream identity cannot
# be durably bound.
# ---------------------------------------------------------------------------


class TestLaunchPathsRefuseOnUnboundStreamIdentity:
    """The binding result gates the launch, on BOTH paths."""

    def test_the_worker_launch_path_refuses_when_binding_fails(self):
        scope = _routes_scope()
        binding = _first_match(scope, r"if not record_goal_continuation_stream\(")
        assert binding is not None, (
            "the worker launch path drops the binding result of "
            "record_goal_continuation_stream (finding 2)"
        )
        thr = scope.find("thr.start()")
        assert thr > 0
        assert binding.start() < thr, (
            "the binding check must happen before the worker starts"
        )
        window = scope[binding.start(): thr]
        assert "_GoalContinuationCommitFailed" in window, (
            "a failed binding must refuse the launch through the dedicated "
            "commit-failed exception so the start is compensated"
        )

    def test_the_regeneration_path_binds_before_releasing_its_worker(self):
        import api.routes as routes

        src = _re.sub(r"\s+", " ", open(routes.__file__, encoding="utf-8").read())
        regen = _re.search(r"def _start_regeneration_stream_locked\(", src)
        assert regen is not None
        regen_scope = src[regen.start(): regen.start() + 9000]
        # The attempt identity is threaded into the regeneration launch.
        assert _re.search(r"goal_continuation_attempt_id\s*[:=]", regen_scope), (
            "the regeneration path never receives the admission's attempt "
            "identity, so it cannot bind it to its stream (finding 2)"
        )
        binding = _first_match(
            regen_scope,
            r"record_goal_continuation_stream\(\s*s\.session_id\s*,\s*"
            r"goal_continuation_attempt_id\s*,\s*stream_id\s*,?\s*\)",
        )
        assert binding is not None, (
            "the regeneration path never binds the admission's stream identity "
            "despite the comment claiming it did (finding 2)"
        )
        release = regen_scope.find("release_worker.set()")
        assert release > 0
        assert binding.start() < release, (
            "the binding must land before the gated worker is released"
        )

    def test_the_worker_path_still_records_before_start(self):
        scope = _routes_scope()
        record_m = _first_match(
            scope,
            r"record_goal_continuation_stream\(\s*s\.session_id\s*,"
            r"\s*goal_continuation_attempt_id\s*,\s*stream_id\s*,?\s*\)",
        )
        assert record_m is not None
        thr = scope.find("thr.start()")
        assert record_m.start() < thr


# ---------------------------------------------------------------------------
# Finding 5: cold restore reconciles the recorded stream against durable
# run/turn state; an attempt that never launched stays recoverable.
# ---------------------------------------------------------------------------


def _write_run_journal_row(sid, stream):
    """Write one durable run-journal row, as the worker's first SSE would."""
    from api.models import SESSION_DIR
    from api.run_journal import append_run_event

    append_run_event(sid, stream, "start", {"meta": "test"}, session_dir=SESSION_DIR)


def _clean_journals(sid):
    from api.models import SESSION_DIR

    for sub in ("_run_journal", "_turn_journal"):
        target = SESSION_DIR / sub / sid
        try:
            for p in target.rglob("*"):
                if p.is_file():
                    p.unlink()
            target.rmdir()
        except FileNotFoundError:
            pass
        except OSError:
            pass


class TestColdRestoreReconcilesTheRecordedStream:
    """An allocated stream id is not proof of execution."""

    def test_a_stream_that_never_launched_stays_recoverable(self, clean_registry):
        store = clean_registry
        sid = "sess-r12-never-launched"
        try:
            _arm_and_consume(store, sid, "att-1")
            # The route recorded the stream identity, then the process died
            # BEFORE thr.start(). No worker-side journal exists. Its liveness
            # clock goes stale first (round 15, CORE finding 2).
            assert store.record_goal_continuation_stream(sid, "att-1", "stream-abc")
            _age_handoffs(store, sid)

            assert _cold_restore(store) == 1, (
                "the handoff was dropped because the stream id was treated as "
                "proof of execution, but the worker never ran (finding 5)"
            )
            from api.config import PENDING_GOAL_CONTINUATION_RECORDS

            assert sid in PENDING_GOAL_CONTINUATION_RECORDS
            # The restored record must not carry the stale stream id: the
            # retry mints its own identity when it launches.
            assert not PENDING_GOAL_CONTINUATION_RECORDS[sid].get(
                "_handoff_stream_id"
            ), "the never-launched stream id leaked into the restored record"
        finally:
            _clean_journals(sid)

    def test_a_stream_that_executed_is_still_dropped(self, clean_registry):
        store = clean_registry
        sid = "sess-r12-executed"
        try:
            _arm_and_consume(store, sid, "att-1")
            assert store.record_goal_continuation_stream(sid, "att-1", "stream-ghi")
            # The worker DID run (its run journal has a row).
            _write_run_journal_row(sid, "stream-ghi")
            # The owning process is proven gone (the liveness clock is aged)
            # before the restart reconciles the executed admission.
            _age_handoffs(store, sid)

            assert _cold_restore(store) == 0, (
                "an admission whose turn actually ran must still be dropped "
                "(round-11 contract)"
            )
            from api.config import PENDING_GOAL_CONTINUATION_RECORDS

            assert sid not in PENDING_GOAL_CONTINUATION_RECORDS
            # The executed handoff must not linger as future claimable intent.
            _records, _tombstones, handoffs = store._load_file_raw()
            assert not any(k[0] == sid for k in handoffs), (
                "an executed admission's handoff was left on disk after the "
                "reconciliation dropped it"
            )
        finally:
            _clean_journals(sid)
