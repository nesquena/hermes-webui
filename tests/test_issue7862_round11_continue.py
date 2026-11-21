"""#7862 re-gate CORE: an explicit ``continue`` after ``/goal resume``.

The Oct 10 re-gate found the last CORE in the durable-continuation store:
``/goal pause`` → ``/goal resume`` → ``continue`` stops the goal loop,
because ``continue`` is a brand-new human turn whose text is not the
recorded continuation prompt. The admission then runs with
``goal_related=false`` and the goal is never evaluated again — while the
same sequence on master (whose marker was spent by session id alone)
launched goal-related.

The fix must NOT widen the text match (an unrelated human message still
has to leave the intent pending). Instead the store grows an explicit
admission for a resumed goal's ``continue``:

* the request must carry this continuation's token (identity first, same
  as every other path — a replayed token from another generation is
  rejected), and
* the turn's text must BE the literal ``continue`` control (normalised),
  and
* the recorded goal must still be active and resumed, which the caller
  asserts by passing ``goal_active=True``.

Everything else about the consume is unchanged: the removal and the
handoff still land in one ``os.replace``, a commit failure still refuses
the admission, and an unrelated message still leaves the intent pending.

RED control: each case below fails on the pre-fix store (``continue``
falls through as a non-matching turn).
"""

from __future__ import annotations

import re as _re

import pytest

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")


def _routes_source() -> str:
    """Return the real ``api/routes.py`` source (same pattern as round 7)."""
    import api.routes as routes

    return _re.sub(r"\s+", " ", open(routes.__file__, encoding="utf-8").read())


def _first_match(text: str, pattern: str):
    return _re.search(pattern, text, _re.DOTALL)

PROMPT = "keep refining the release checklist"
TOKEN = "tok-continue-1"
CONTINUE_TEXT = "continue"


@pytest.fixture
def clean_registry(monkeypatch):
    """Isolate the registry between cases (same shape as round 7's)."""
    from api import goal_continuation_store as store
    from api.config import PENDING_GOAL_CONTINUATION, PENDING_GOAL_CONTINUATION_RECORDS

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
        store._GENERATION = 0
    store._PENDING_GOAL_FILE.unlink(missing_ok=True)
    yield
    with store._LOCK:
        PENDING_GOAL_CONTINUATION.clear()
        PENDING_GOAL_CONTINUATION_RECORDS.clear()
        store._ROLLBACK_RECEIPTS.clear()
        store._RETIRED_GENERATIONS.clear()
        store._TOMBSTONES.clear()
        store._CONTINUATION_HANDOFF_TOKENS.clear()
    store._PENDING_GOAL_FILE.unlink(missing_ok=True)


def _arm(store, prompt=PROMPT, token=TOKEN):
    store.arm_pending_goal_continuation("sid-continue", prompt, continuation_id=token)


class TestExplicitContinueAfterResume:
    """The Oct 10 finding: ``continue`` must be admissible, safely."""

    def test_a_literal_continue_consumes_the_recorded_intent(self, clean_registry):
        from api import goal_continuation_store as store
        from api.goal_continuation_store import CONSUME_COMMITTED

        _arm(store)
        outcome = store.consume_pending_goal_continuation(
            "sid-continue",
            CONTINUE_TEXT,
            TOKEN,
            "att-continue-1",
            explicit_continue=True,
            goal_active=True,
        )
        assert outcome == CONSUME_COMMITTED
        from api.config import PENDING_GOAL_CONTINUATION, PENDING_GOAL_CONTINUATION_RECORDS

        assert "sid-continue" not in PENDING_GOAL_CONTINUATION_RECORDS
        assert "sid-continue" not in PENDING_GOAL_CONTINUATION
        # The consume still leaves the durable handoff the retry reconciles on.
        assert ("sid-continue", "att-continue-1") in store._CONTINUATION_HANDOFF_TOKENS

    def test_continue_without_the_token_is_not_swallowed(self, clean_registry):
        """Identity first: a tokenless ``continue`` stays an ordinary turn."""
        from api import goal_continuation_store as store
        from api.goal_continuation_store import CONSUME_NOT_MATCHING

        _arm(store)
        outcome = store.consume_pending_goal_continuation(
            "sid-continue",
            CONTINUE_TEXT,
            "",  # no token carried
            "att-continue-2",
            explicit_continue=True,
            goal_active=True,
        )
        assert outcome == CONSUME_NOT_MATCHING
        from api.config import PENDING_GOAL_CONTINUATION_RECORDS

        assert "sid-continue" in PENDING_GOAL_CONTINUATION_RECORDS

    def test_continue_with_another_generations_token_is_rejected(self, clean_registry):
        """A token from a different continuation must not spend this one."""
        from api import goal_continuation_store as store
        from api.goal_continuation_store import CONSUME_NOT_MATCHING

        _arm(store)
        outcome = store.consume_pending_goal_continuation(
            "sid-continue",
            CONTINUE_TEXT,
            "tok-from-another-generation",
            "att-continue-3",
            explicit_continue=True,
            goal_active=True,
        )
        assert outcome == CONSUME_NOT_MATCHING
        from api.config import PENDING_GOAL_CONTINUATION_RECORDS

        assert "sid-continue" in PENDING_GOAL_CONTINUATION_RECORDS

    def test_continue_is_refused_when_the_goal_is_not_active(self, clean_registry):
        """``continue`` for a paused/cleared goal must not spend the intent.

        The caller passes ``goal_active=False`` when the goal manager has no
        active goal for the session (cleared, or still paused), so a stray
        ``continue`` cannot resurrect it through the continuation path.
        """
        from api import goal_continuation_store as store
        from api.goal_continuation_store import CONSUME_NOT_MATCHING

        _arm(store)
        outcome = store.consume_pending_goal_continuation(
            "sid-continue",
            CONTINUE_TEXT,
            "",
            "att-continue-4",
            explicit_continue=True,
            goal_active=False,
        )
        assert outcome == CONSUME_NOT_MATCHING
        from api.config import PENDING_GOAL_CONTINUATION_RECORDS

        assert "sid-continue" in PENDING_GOAL_CONTINUATION_RECORDS

    def test_other_text_with_the_flag_set_is_not_swallowed(self, clean_registry):
        """The flag is not a bypass: only the literal control is admitted.

        ``Continue please!!`` and a paraphrase are ordinary turns; only the
        normalised literal ``continue`` (what the resume message tells the
        user to type) takes the explicit path.

        #7862 re-gate round 3 (finding 4): these cases send a TOKEN, because
        the browser always carries the continuation's identity on the queued
        automatic send. A validated token now admits the turn independently
        of its text (an edited or combined queued continuation must not stop
        the goal loop), so the token is dropped here to keep this test about
        the TEXT contract alone. ``test_an_unrelated_message_is_still_left_
        pending`` below pins the same question with no token at all.
        """
        from api import goal_continuation_store as store
        from api.goal_continuation_store import CONSUME_NOT_MATCHING

        _arm(store)
        for text in ("Continue please!!", "keep going", "continue working on it"):
            outcome = store.consume_pending_goal_continuation(
                "sid-continue",
                text,
                "",
                f"att-continue-{text[:4]}",
                explicit_continue=True,
                goal_active=True,
            )
            assert outcome == CONSUME_NOT_MATCHING, text
        from api.config import PENDING_GOAL_CONTINUATION_RECORDS

        assert "sid-continue" in PENDING_GOAL_CONTINUATION_RECORDS

    def test_an_unrelated_message_is_still_left_pending(self, clean_registry):
        """The core contract is unchanged without the flag."""
        from api import goal_continuation_store as store
        from api.goal_continuation_store import CONSUME_NOT_MATCHING

        _arm(store)
        outcome = store.consume_pending_goal_continuation(
            "sid-continue", "what is the weather", "", "att-continue-6"
        )
        assert outcome == CONSUME_NOT_MATCHING
        from api.config import PENDING_GOAL_CONTINUATION_RECORDS

        assert "sid-continue" in PENDING_GOAL_CONTINUATION_RECORDS

    def test_a_failed_commit_refuses_the_continue_admission(self, clean_registry, monkeypatch):
        """The durable outcome still gates the acknowledgement."""
        from api import goal_continuation_store as store
        from api.goal_continuation_store import CONSUME_COMMIT_FAILED

        _arm(store)

        def _boom(src, dst, *args, **kwargs):
            raise OSError("simulated failed registry replacement")

        monkeypatch.setattr(store.os, "replace", _boom)
        outcome = store.consume_pending_goal_continuation(
            "sid-continue",
            CONTINUE_TEXT,
            TOKEN,
            "att-continue-7",
            explicit_continue=True,
            goal_active=True,
        )
        assert outcome == CONSUME_COMMIT_FAILED
        from api.config import PENDING_GOAL_CONTINUATION_RECORDS

        assert "sid-continue" in PENDING_GOAL_CONTINUATION_RECORDS


class TestTheRouteWiresTheContinueAdmission:
    """The route must certify the goal before passing the flags.

    Store-level coverage alone would let the admission be unreachable (or
    reachable with ``goal_active=True`` hard-coded), so these pin the real
    ``_start_chat_stream_for_session`` source: the flags are computed from
    the session's actual goal state and forwarded to the consume.
    """

    @staticmethod
    def _routes_scope():
        src = _routes_source()
        body = _first_match(src, r"def _start_chat_stream_for_session\(")
        assert body is not None
        return src[body.start():body.start() + 40000]

    def test_the_route_reads_the_goal_state_before_admitting(self):
        scope = self._routes_scope()
        # The consume call must forward BOTH flags, so a hard-coded True (or a
        # dropped flag) fails here rather than in production.
        consume_m = _first_match(
            scope,
            r"consume_pending_goal_continuation\(\s*s\.session_id,\s*msg,\s*"
            r"goal_continuation_id\s*,\s*goal_continuation_attempt_id\s*,"
            r"\s*explicit_continue\s*=\s*\w+\s*,\s*goal_active\s*=\s*\w+\s*,?\s*\)",
        )
        assert consume_m is not None, (
            "the continue admission must forward explicit_continue and goal_active"
        )
        # ...and the flags must be DERIVED from the session's goal state, not
        # literals: the admission is only legitimate for an active goal.
        before = scope[: consume_m.start()]
        # #7862 re-gate round 3 (finding 1): the certification reads the goal
        # through the profile-scoped ``has_active_goal()``, not the
        # ``goal_state_snapshot`` dict-status check this test originally
        # pinned. The snapshot is a ``GoalState`` object on the native manager
        # (``isinstance(snapshot, dict)`` is False) and resolves to ``None``
        # for a beta profile without ``profile_home``, so the dict check never
        # certified and the goal loop stopped after every resume.
        assert "has_active_goal(" in before, (
            "the route must certify the session's goal via has_active_goal() "
            "before admitting a continue"
        )
        assert "get_hermes_home_for_profile(" in before, (
            "the route must scope the goal certification to the session's profile"
        )
        assert "goal_state_snapshot(" not in before, (
            "the dict-status snapshot check is still live; it never certifies "
            "a native GoalState or a beta-profile goal (#7862 re-gate finding 1)"
        )

    def test_a_non_continue_message_never_sets_the_flag(self):
        """Only the literal control may take the explicit path."""
        scope = self._routes_scope()
        # The flag computation is gated on the normalised text being the
        # literal continue control, so an unrelated message keeps the old
        # text-match contract.
        assert _re.search(
            r"normalize_continuation_text\(msg\)\s*==\s*_CONTINUE_CONTROL", scope
        ), "the explicit path must be gated on the literal continue control"

