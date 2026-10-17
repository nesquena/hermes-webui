"""#6885 slice 1: admission correction for automatic ``/goal`` continuations.

Background
----------
``api/routes.py`` consumed ``PENDING_GOAL_CONTINUATION`` by bare session id::

    if not goal_related and s.session_id in PENDING_GOAL_CONTINUATION:
        goal_related = True
        PENDING_GOAL_CONTINUATION.discard(s.session_id)

The marker therefore classified the session's *next* turn as goal-related no
matter what that turn actually was. A genuine user follow-up typed while the
marker was pending was:

* marked ``goal_related``, so the streaming hook ran
  ``evaluate_goal_after_turn`` and burned goal budget on an unrelated message
  (the exact regression #1932 was filed for), and
* consumed the marker, so the authentic continuation the server still owed had
  nothing left to claim and arrived unjudged.

The maintainer's bounded slice 1 correction: a pending goal marker may only be
claimed by the turn that actually carries the continuation prompt the server
emitted. Everything else keeps the marker for the real continuation.

These tests are behaviour-level and drive the real ``_start_chat_stream_for_session``
admission path (the repo's #7302 re-gate prefers behaviour over source greps);
the only stubbed collaborators are the ones unrelated to admission.
"""

import pytest

import api.config as config
import api.models as models
import api.routes as routes
from api.models import new_session


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _record_pending(session_id, prompt):
    """Record a pending continuation the way api/streaming.py must record it."""
    record = getattr(config, "record_pending_goal_continuation", None)
    assert record is not None, (
        "api.config.record_pending_goal_continuation() is required by #6885: the "
        "continuation prompt must be persisted next to the pending marker so the "
        "admission path can tell an authentic continuation from a user follow-up"
    )
    record(session_id, prompt)


@pytest.fixture
def issue6885_env(tmp_path, monkeypatch):
    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    index_file = session_dir / "_index.json"
    monkeypatch.setattr(models, "SESSION_DIR", session_dir)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", index_file)
    monkeypatch.setattr(config, "SESSION_INDEX_FILE", index_file, raising=False)
    monkeypatch.setattr(config, "cfg", {"webui": {"session_save_mode": "eager"}})
    monkeypatch.setattr(routes, "set_last_workspace", lambda *_a, **_k: None)
    monkeypatch.setattr(routes, "publish_session_list_changed", lambda *_a, **_k: None)
    monkeypatch.setattr(routes, "_agent_runtime_barrier_response", lambda **_k: None)
    monkeypatch.setattr(routes, "_active_run_stream_for_session", lambda _sid: None)
    # The worker is irrelevant to admission; stubbing it keeps
    # STREAM_GOAL_RELATED (set synchronously before the thread starts) observable.
    monkeypatch.setattr(routes, "_run_agent_streaming", lambda *_a, **_k: None)
    monkeypatch.setattr(routes, "_run_gateway_chat_streaming", lambda *_a, **_k: None)
    _reset(monkeypatch)
    yield tmp_path
    _reset(monkeypatch)


def _reset(monkeypatch=None):
    models.SESSIONS.clear()
    config.STREAMS.clear()
    config.STREAM_GOAL_RELATED.clear()
    config.STREAM_SESSION_OWNERS.clear()
    config.SESSION_WRITEBACK_OWNERS.clear()
    routes.PENDING_GOAL_CONTINUATION.clear()
    routes.PENDING_BG_TASK_COMPLETIONS.clear()
    prompts = getattr(config, "PENDING_GOAL_CONTINUATION_PROMPTS", None)
    if isinstance(prompts, dict):
        prompts.clear()


def _start(session, *, workspace, **overrides):
    values = {
        "msg": "unrelated user question",
        "attachments": [],
        "workspace": str(workspace),
        "model": session.model,
        "model_provider": session.model_provider,
        "external_runtime_owned": False,
    }
    values.update(overrides)
    return routes._start_chat_stream_for_session(session, **values)


def _is_goal_related(response):
    """Was the admitted turn classified as goal-related?"""
    return response.get("stream_id") in config.STREAM_GOAL_RELATED


def _pending_prompt(session_id):
    prompts = getattr(config, "PENDING_GOAL_CONTINUATION_PROMPTS", None)
    return prompts.get(session_id) if isinstance(prompts, dict) else None


# ---------------------------------------------------------------------------
# The defect: a genuine user turn was treated as the continuation
# ---------------------------------------------------------------------------

def test_user_followup_does_not_claim_pending_goal_marker(issue6885_env):
    """A user follow-up must not consume the pending continuation marker."""
    session = new_session(workspace=str(issue6885_env / "ws"), profile="profile-a")
    routes.PENDING_GOAL_CONTINUATION.add(session.session_id)

    response = _start(session, workspace=issue6885_env / "ws", msg="what time is it?")

    assert response.get("_status", 200) < 400, "a user turn must not be refused"
    assert session.session_id in routes.PENDING_GOAL_CONTINUATION, (
        "REGRESSION: a genuine user follow-up consumed PENDING_GOAL_CONTINUATION. "
        "The server still owes a continuation, so the marker must survive for the "
        "turn that actually carries it"
    )


def test_user_followup_does_not_mark_stream_goal_related(issue6885_env):
    """The goal evaluation hook must not run for an unrelated user turn (#1932)."""
    session = new_session(workspace=str(issue6885_env / "ws"), profile="profile-a")
    routes.PENDING_GOAL_CONTINUATION.add(session.session_id)

    response = _start(session, workspace=issue6885_env / "ws", msg="what time is it?")

    assert not _is_goal_related(response), (
        "REGRESSION: a genuine user follow-up was classified goal-related because a "
        "pending marker existed. That burns goal budget on an unrelated message"
    )


def test_user_followup_does_not_claim_marker_for_the_recorded_prompt(issue6885_env):
    """Same guarantee once the continuation prompt is persisted."""
    session = new_session(workspace=str(issue6885_env / "ws"), profile="profile-a")
    _record_pending(session.session_id, "Continue: finish the migration")

    response = _start(session, workspace=issue6885_env / "ws", msg="actually, stop")

    assert not _is_goal_related(response), (
        "an unrelated turn must not be classified as the recorded continuation"
    )
    assert session.session_id in routes.PENDING_GOAL_CONTINUATION
    assert _pending_prompt(session.session_id) == "Continue: finish the migration", (
        "the owed continuation prompt must survive an unrelated turn untouched"
    )


# ---------------------------------------------------------------------------
# The legacy behaviour that must be retained
# ---------------------------------------------------------------------------

def test_authentic_continuation_is_claimed_and_marked_goal_related(issue6885_env):
    """The turn carrying the emitted prompt keeps the original behaviour."""
    session = new_session(workspace=str(issue6885_env / "ws"), profile="profile-a")
    _record_pending(session.session_id, "Continue: finish the migration")

    response = _start(
        session,
        workspace=issue6885_env / "ws",
        msg="Continue: finish the migration",
    )

    assert response.get("_status", 200) < 400
    assert _is_goal_related(response), (
        "the authentic continuation must still be admitted as a goal-related turn"
    )
    assert session.session_id not in routes.PENDING_GOAL_CONTINUATION, (
        "the continuation is single-use: the marker must be consumed by its own turn"
    )


def test_continuation_prompt_is_compared_after_trimming(issue6885_env):
    """The browser ships the prompt verbatim; surrounding whitespace is not identity."""
    session = new_session(workspace=str(issue6885_env / "ws"), profile="profile-a")
    _record_pending(session.session_id, "Continue: finish the migration")

    response = _start(
        session,
        workspace=issue6885_env / "ws",
        msg="  Continue: finish the migration\n",
    )

    assert _is_goal_related(response)
    assert session.session_id not in routes.PENDING_GOAL_CONTINUATION


# ---------------------------------------------------------------------------
# Both orderings the maintainer asked for
# ---------------------------------------------------------------------------

def test_ordering_user_turn_then_authentic_continuation(issue6885_env):
    """A user turn arriving first must not starve the owed continuation."""
    session = new_session(workspace=str(issue6885_env / "ws"), profile="profile-a")
    _record_pending(session.session_id, "Continue: finish the migration")

    first = _start(session, workspace=issue6885_env / "ws", msg="quick question")
    assert not _is_goal_related(first)
    assert session.session_id in routes.PENDING_GOAL_CONTINUATION

    # The browser dispatches the real continuation afterwards.
    models.SESSIONS.pop(session.session_id, None)
    reloaded = models.get_session(session.session_id)
    reloaded.active_stream_id = None
    second = _start(reloaded, workspace=issue6885_env / "ws", msg="Continue: finish the migration")

    assert _is_goal_related(second), (
        "after a user turn the owed continuation must still be admissible"
    )
    assert session.session_id not in routes.PENDING_GOAL_CONTINUATION


def test_ordering_continuation_then_user_turn(issue6885_env):
    """Once the continuation is claimed, a later user turn stays unrelated."""
    session = new_session(workspace=str(issue6885_env / "ws"), profile="profile-a")
    _record_pending(session.session_id, "Continue: finish the migration")

    first = _start(
        session, workspace=issue6885_env / "ws", msg="Continue: finish the migration"
    )
    assert _is_goal_related(first)

    models.SESSIONS.pop(session.session_id, None)
    reloaded = models.get_session(session.session_id)
    reloaded.active_stream_id = None
    second = _start(reloaded, workspace=issue6885_env / "ws", msg="thanks, one more thing")

    assert not _is_goal_related(second), (
        "a user turn after a consumed continuation must not be goal-related"
    )


# ---------------------------------------------------------------------------
# Rejected admission must not lose the continuation (paired with #7193)
# ---------------------------------------------------------------------------

def test_rejected_start_preserves_recorded_prompt(issue6885_env, monkeypatch):
    """A refused start restores the marker *and* the prompt it still owes."""
    session = new_session(workspace=str(issue6885_env / "ws"), profile="profile-a")
    _record_pending(session.session_id, "Continue: finish the migration")
    monkeypatch.setattr(
        routes,
        "create_stream_channel",
        lambda: (_ for _ in ()).throw(RuntimeError("stream registration rejected")),
    )

    with pytest.raises(RuntimeError, match="stream registration rejected"):
        _start(
            session,
            workspace=issue6885_env / "ws",
            msg="Continue: finish the migration",
        )

    assert session.session_id in routes.PENDING_GOAL_CONTINUATION
    assert _pending_prompt(session.session_id) == "Continue: finish the migration"


def test_rejected_start_409_preserves_recorded_prompt(issue6885_env, monkeypatch):
    """A 409 on the continuation turn must not drop the owed prompt."""
    session = new_session(workspace=str(issue6885_env / "ws"), profile="profile-a")
    _record_pending(session.session_id, "Continue: finish the migration")
    monkeypatch.setattr(
        routes, "_active_run_stream_for_session", lambda _sid: "active-run"
    )

    response = _start(
        session, workspace=issue6885_env / "ws", msg="Continue: finish the migration"
    )

    assert response["_status"] == 409
    assert session.session_id in routes.PENDING_GOAL_CONTINUATION
    assert _pending_prompt(session.session_id) == "Continue: finish the migration"


def test_empty_continuation_prompt_is_not_recorded(issue6885_env):
    """An empty prompt cannot identify a continuation, so nothing is owed."""
    session = new_session(workspace=str(issue6885_env / "ws"), profile="profile-a")

    _record_pending(session.session_id, "   ")

    assert session.session_id not in routes.PENDING_GOAL_CONTINUATION
    assert _pending_prompt(session.session_id) is None