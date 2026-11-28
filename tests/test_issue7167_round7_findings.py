"""Round-7 review finding on #7167 (nesquena-hermes 2026-10-14).

F1 [ordering] — an owned marker does not prove its user was materialized.
With transcript ``[old user, old answer, same-stream interruption marker]``,
pending ``new prompt`` at 900.5 and a real nonterminal journal token
``new answer``, the shared ``_apply_core_sync_or_error_marker`` appends the
current user AFTER the marker; promotion then hoists the recovered answer
above the marker without moving that newly appended user, producing:

    old user → old answer → new answer → marker → new prompt

The repair must resolve the marker's exact current-user ownership before
promotion and keep the owned user and recovered output ordered together.
When ownership cannot be established, the answer must NOT be hoisted ahead
of its user.
"""

from __future__ import annotations

import pytest

import api.profiles as profiles
from api.models import Session, _apply_core_sync_or_error_marker
from api.run_journal import append_run_event


@pytest.fixture
def hermes_home(tmp_path, monkeypatch):
    """Isolate HERMES_HOME + the WebUI state dir so journal writes sandbox."""
    home = tmp_path / "hermes_home"
    home.mkdir()
    (home / "sessions").mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_WEBUI_STATE_DIR", str(home))
    monkeypatch.setattr(profiles, "_DEFAULT_HERMES_HOME", home)
    # Session.load() resolves SESSION_DIR at import time; redirect the module
    # global so the fresh-file reload reads OUR sandbox, not the shared
    # conftest tree (same pattern as test_3230_preserve_manual_session_title).
    import api.models as models

    monkeypatch.setattr(models, "SESSION_DIR", home / "sessions")
    monkeypatch.setattr(
        models, "SESSION_INDEX_FILE", home / "sessions" / "_index.json"
    )
    return home


def _marker(stream_id):
    return {
        "role": "assistant",
        "content": "Response interrupted",
        "_pending_journal_recovery": True,
        "_journal_marker_stream_id": stream_id,
        "_journal_retry_stream_id": stream_id,
    }


def _ordering(messages):
    """Map the five named rows to their positions for ordering asserts."""
    pos = {}
    for i, m in enumerate(messages):
        content = str(m.get("content"))
        if content in ("old prompt", "old answer", "new answer", "new prompt"):
            pos.setdefault(content, i)
        elif "Response interrupted" in content:
            pos.setdefault("marker", i)
    return pos


def test_missing_user_through_shared_repair_keeps_order(hermes_home):
    """The reviewer's exact repro: user absent at repair time, appended after
    the marker by the shared repair, then promotion hoists the answer."""
    sid = "r7_missing_user_shared_repair"
    stream_id = "stream-r7-shared"
    started_at = 900.5

    append_run_event(sid, stream_id, "token", {"text": "new answer"})

    session = Session(
        session_id=sid,
        title="r7 missing user via shared repair",
        messages=[
            {"role": "user", "content": "old prompt", "timestamp": 100},
            {"role": "assistant", "content": "old answer", "timestamp": 200},
            _marker(stream_id),
        ],
        pending_user_message="new prompt",
        pending_started_at=started_at,
        pending_attachments=[],
        pending_user_source="webui",
        active_stream_id=stream_id,
    )
    session.save()

    assert _apply_core_sync_or_error_marker(
        session,
        hermes_home / "sessions" / f"session_{sid}.json",
        stream_id_for_recheck=stream_id,
    ) is True

    order = _ordering(session.messages)
    assert order["new prompt"] < order["new answer"], (
        "the recovered answer must stay AFTER its own user turn, got "
        f"{session.messages!r}"
    )
    assert order["new answer"] < order["marker"], (
        "the journaled output must still be hoisted above the reused marker"
    )

    # Persisted reload keeps the repaired order (reviewer: "surviving save and
    # a fresh file load").
    session.save()
    loaded = Session.load(sid)
    assert loaded is not None, (
        f"session {sid} must reload from {hermes_home / 'sessions' / f'session_{sid}.json'}"
    )
    order_loaded = _ordering(loaded.messages)
    assert order_loaded["new prompt"] < order_loaded["new answer"], (
        "order must survive save + fresh load, got "
        f"{[str(m.get('content'))[:30] for m in loaded.messages]!r}"
    )


def test_unprovable_user_blocks_hoist(hermes_home):
    """A tokenless, journal-less user row after the marker cannot be proven to
    belong to an older turn — the answer must not be hoisted above it."""
    sid = "r7_unprovable_user"
    stream_id = "stream-r7-unprovable"

    session = Session(
        session_id=sid,
        title="r7 unprovable user",
        messages=[
            {"role": "user", "content": "old prompt", "timestamp": 100},
            {"role": "assistant", "content": "old answer", "timestamp": 200},
            _marker(stream_id),
            # A recovered-but-tokenless user row BELOW the marker: this is
            # the exact shape the shared repair appends when the current
            # turn's prompt could not be proven already materialized (see
            # ``_append_recovered_pending_turn`` — ``_recovered`` set, no
            # token when ``pending_started_at`` was never persisted on this
            # path). Ownership is undecidable.
            {
                "role": "user",
                "content": "ambiguous prompt",
                "timestamp": 300,
                "_recovered": True,
            },
            {
                "role": "assistant",
                "content": "new answer",
                "timestamp": 400,
                "_recovered_from_run_journal": True,
                "_recovered_stream_id": stream_id,
            },
        ],
        pending_user_message="new prompt",
        pending_started_at=900.5,
        pending_attachments=[],
        pending_user_source="webui",
        active_stream_id=stream_id,
    )
    session.save()

    # Drive the shared repair: the journal carries a nonterminal answer.
    append_run_event(sid, stream_id, "token", {"text": "new answer"})

    assert _apply_core_sync_or_error_marker(
        session,
        hermes_home / "sessions" / f"session_{sid}.json",
        stream_id_for_recheck=stream_id,
    ) is True

    order = _ordering(session.messages)
    ambiguous_idx = next(
        i for i, m in enumerate(session.messages)
        if m.get("content") == "ambiguous prompt"
    )
    assert order["new answer"] > ambiguous_idx, (
        "when the user row's ownership cannot be established, the recovered "
        "answer must NOT be hoisted above it"
    )


def test_foreign_token_user_stays_after_marker(hermes_home):
    """A user row carrying a FOREIGN stream's token is provably another
    turn's — the rehome proceeds and never moves it above the marker."""
    sid = "r7_foreign_token_user"
    stream_id = "stream-r7-foreign"
    other_stream = "stream-other"

    session = Session(
        session_id=sid,
        title="r7 foreign token user",
        messages=[
            {"role": "user", "content": "old prompt", "timestamp": 100},
            {"role": "assistant", "content": "old answer", "timestamp": 200},
            _marker(stream_id),
            {
                "role": "user",
                "content": "other turn prompt",
                "timestamp": 300,
                "_active_turn_token": f"{other_stream}:350",
            },
            {
                "role": "assistant",
                "content": "new answer",
                "timestamp": 400,
                "_recovered_from_run_journal": True,
                "_recovered_stream_id": stream_id,
            },
        ],
        pending_user_message="new prompt",
        pending_started_at=900.5,
        pending_attachments=[],
        pending_user_source="webui",
        active_stream_id=stream_id,
    )
    session.save()

    append_run_event(sid, stream_id, "token", {"text": "new answer"})

    assert _apply_core_sync_or_error_marker(
        session,
        hermes_home / "sessions" / f"session_{sid}.json",
        stream_id_for_recheck=stream_id,
    ) is True

    order = _ordering(session.messages)
    foreign_idx = next(
        i for i, m in enumerate(session.messages)
        if m.get("content") == "other turn prompt"
    )
    # The foreign user row keeps its place after the marker …
    assert foreign_idx > order["marker"], (
        "a foreign turn's user row must stay below the marker"
    )
    # … and this stream's recovered answer is hoisted above the marker.
    assert order["new answer"] < order["marker"], (
        "this stream's journaled answer must still be hoisted above the marker"
    )


def test_owned_user_moves_with_recovered_answer(hermes_home):
    """When the user row after the marker carries THIS stream's token, it is
    the current turn's user — promotion must move it up together with the
    recovered answer, user first."""
    sid = "r7_owned_user_moves"
    stream_id = "stream-r7-owned"
    started_at = 900.5
    token = f"{stream_id}:{started_at:.17g}"

    append_run_event(sid, stream_id, "token", {"text": "new answer"})

    session = Session(
        session_id=sid,
        title="r7 owned user moves",
        messages=[
            {"role": "user", "content": "old prompt", "timestamp": 100},
            {"role": "assistant", "content": "old answer", "timestamp": 200},
            _marker(stream_id),
            # The current turn's user, already materialized below the marker
            # by an earlier repair pass that stamped the token.
            {
                "role": "user",
                "content": "new prompt",
                "timestamp": started_at,
                "_active_turn_token": token,
            },
        ],
        pending_user_message="new prompt",
        pending_started_at=started_at,
        pending_attachments=[],
        pending_user_source="webui",
        active_stream_id=stream_id,
    )
    session.save()

    assert _apply_core_sync_or_error_marker(
        session,
        hermes_home / "sessions" / f"session_{sid}.json",
        stream_id_for_recheck=stream_id,
    ) is True

    order = _ordering(session.messages)
    assert order["new prompt"] < order["new answer"], (
        "the owned user must rise WITH the recovered answer, user first"
    )
    assert order["new answer"] < order["marker"], (
        "the journaled output must be hoisted above the reused marker"
    )
