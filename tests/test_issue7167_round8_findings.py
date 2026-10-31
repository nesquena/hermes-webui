"""Round-8 review finding on #7167 (nesquena-hermes 2026-10-27).

F1 [CORE] — ``_pending_turn_marker_present`` accepts historical text
equality even when the pending turn carries a token/timestamp identity.
With transcript ``[old user "run the check", old answer, armed stream-A
marker]``, pending ``"run the check"`` at 900.5 WITH an attachment and a
nonterminal journal token ``new answer``, the text-equal OLD row at/before
the marker makes the gate report "this turn is already recovered". The
repair then skips ``_append_recovered_pending_turn`` (the only path that
writes the current prompt + attachment into ``session.messages``); the
fallback touches only the ephemeral in-memory ``context_messages``, which
is not persisted. After two cold ``get_session()`` / ``Session.load()``
cycles the sidecar holds neither the current user row nor its attachment
and pending state has been cleared — the prompt is lost outright. Master
keeps both because it lacks the marker gate.

Fix: when the pending turn has a token/timestamp identity, the marker gate
must require ``_message_owns_current_turn`` (token equality first, then the
full pending-checkpoint identity: text + timestamp + source + attachments)
instead of pure text equality. The identity-less legacy shape (dead-stream
sidecars that never persisted ``pending_started_at``) keeps the text
fallback so old behaviour is preserved.
"""

from __future__ import annotations

import pytest

import api.profiles as profiles
from api.models import Session, _apply_core_sync_or_error_marker
from api.run_journal import append_run_event
from api.streaming import _context_messages_for_new_turn, _session_context_messages


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
    # conftest tree (same pattern as the round-7 findings file).
    import api.models as models

    monkeypatch.setattr(models, "SESSION_DIR", home / "sessions")
    monkeypatch.setattr(
        models, "SESSION_INDEX_FILE", home / "sessions" / "_index.json"
    )
    return home


def _marker(stream_id, pending_started_at=None):
    """Seed the armed marker through the production builder.

    The real marker carries ``type='interrupted'`` and ``_error`` alongside
    the retry-hook fields, so after a resolved pass strips the retry meta
    (``_strip_journal_retry_meta``) the marker is STILL recognised by
    ``_marker_reuse_index`` and a re-armed cycle reuses it instead of
    stacking a second marker. The previous hand-written fake lacked both
    fields and silently vanished from recognition after promotion (#7167
    review 2026-10-27, should-fix).
    """
    from api.models import _build_recovery_marker_with_retry_hook

    return _build_recovery_marker_with_retry_hook(
        recovered_output=False,
        stream_id=stream_id,
        pending_started_at=pending_started_at,
    )


def _current_user_rows(session):
    """User rows whose timestamp matches the pending turn's identity."""
    return [
        m for m in session.messages
        if isinstance(m, dict)
        and m.get("role") == "user"
        and m.get("timestamp") == 900.5
    ]


def test_repeated_prompt_with_attachment_survives_two_cold_cycles(hermes_home):
    """CORE #7167 review 2026-10-27: a pending turn that repeats an OLD
    prompt's text must not be swallowed by the marker gate.

    Repro input (the reviewer's exact start state): transcript
    ``[old user "run the check", old answer, armed stream-A marker]``,
    pending ``"run the check"`` at 900.5 with ``['/tmp/upload.png']``, and
    one nonterminal journal token ``new answer``. Two cold
    get_session()/Session.load() cycles later the current user row and its
    attachment must still be in the display transcript, in the model-facing
    context, and in the next-send input.
    """
    sid = "r8_repeated_prompt_attachment"
    stream_id = "stream-r8-repeat"
    started_at = 900.5
    token = f"{stream_id}:{started_at:.17g}"

    append_run_event(sid, stream_id, "token", {"text": "new answer"})

    session = Session(
        session_id=sid,
        title="r8 repeated prompt keeps current row + attachment",
        messages=[
            {"role": "user", "content": "run the check", "timestamp": 100},
            {"role": "assistant", "content": "old answer", "timestamp": 200},
            _marker(stream_id, started_at),
        ],
        pending_user_message="run the check",
        pending_started_at=started_at,
        pending_attachments=["/tmp/upload.png"],
        pending_user_source="cli",
        active_stream_id=stream_id,
    )
    session.save()

    # Cold cycle 1: cache-miss repair against the persisted sidecar.
    assert _apply_core_sync_or_error_marker(
        session,
        hermes_home / "sessions" / f"session_{sid}.json",
        stream_id_for_recheck=stream_id,
    ) is True

    # The CURRENT turn's row must have been materialized into the display
    # transcript (the ephemeral context copy is not persisted, so this is
    # the only copy that survives a reload).
    first_pass_rows = _current_user_rows(session)
    assert len(first_pass_rows) == 1, (
        "the first repair pass must materialize the current user row in "
        f"session.messages, got {len(first_pass_rows)} rows; transcript="
        f"{[str(m.get('content'))[:24] for m in session.messages]!r}"
    )
    assert first_pass_rows[0].get("attachments") == ["/tmp/upload.png"], (
        "the materialized current row keeps the pending attachment"
    )
    assert first_pass_rows[0].get("_active_turn_token") == token, (
        "the materialized current row is stamped with the pending turn token"
    )
    session.save()

    # Cold cycle 2: a fresh file load, exactly like the browser's next
    # cache-miss get_session() after the repair.
    loaded = Session.load(sid)
    assert loaded is not None, (
        f"session {sid} must reload from {hermes_home / 'sessions' / f'session_{sid}.json'}"
    )

    # ── ① display transcript: current user row + attachment survive ──
    current_rows = _current_user_rows(loaded)
    assert len(current_rows) == 1, (
        "after two cold cycles the display transcript must keep the current "
        f"user row, got {len(current_rows)} rows; transcript="
        f"{[str(m.get('content'))[:24] for m in loaded.messages]!r}"
    )
    current_row = current_rows[0]
    assert current_row.get("content") == "run the check", (
        "the surviving row is the current prompt, not the historical one"
    )
    assert current_row.get("attachments") == ["/tmp/upload.png"], (
        "the current row's attachment survives the cold reload, got "
        f"{current_row.get('attachments')!r}"
    )
    assert current_row.get("_active_turn_token") == token, (
        "the current row keeps its turn token across the cold reload"
    )
    old_rows = [
        m for m in loaded.messages
        if isinstance(m, dict) and m.get("role") == "user"
        and m.get("timestamp") == 100
    ]
    assert len(old_rows) == 1, "the historical turn stays intact"
    assert not old_rows[0].get("attachments"), (
        "the historical same-text row must never receive the pending "
        "turn's attachment"
    )
    assert not old_rows[0].get("_active_turn_token"), (
        "the historical same-text row must never receive the pending turn token"
    )

    # ── ② model-facing context: effective history keeps the turn ──
    context = _session_context_messages(loaded)
    context_text = "\n".join(str(m.get("content") or "") for m in context)
    assert "run the check" in context_text, (
        "the recovered current prompt must be in the model-facing context, "
        f"got {context_text!r}"
    )
    context_rows = [
        m for m in context
        if isinstance(m, dict) and m.get("role") == "user"
        and m.get("timestamp") == started_at
    ]
    assert len(context_rows) == 1, (
        "the context must carry the current user row, got "
        f"{len(context_rows)} rows"
    )
    assert context_rows[0].get("attachments") == ["/tmp/upload.png"], (
        "the context copy keeps the attachment"
    )

    # ── ③ next-send input: the next user turn sees the turn + attachment ──
    next_input = _context_messages_for_new_turn(loaded, "check again please")
    next_text = "\n".join(str(m.get("content") or "") for m in next_input)
    assert "run the check" in next_text, (
        "the next-send input must still contain the recovered prompt, got "
        f"{next_text!r}"
    )
    next_rows = [
        m for m in next_input
        if isinstance(m, dict) and m.get("role") == "user"
        and m.get("timestamp") == started_at
    ]
    assert len(next_rows) == 1, (
        "the next-send input must carry the current user row, got "
        f"{len(next_rows)} rows"
    )
    assert next_rows[0].get("attachments") == ["/tmp/upload.png"], (
        "the next-send input keeps the attachment"
    )
    # The recovered turn stays ordered before its journaled answer in the
    # history the streaming path feeds to the model.
    user_idx = next(i for i, m in enumerate(next_input) if m is next_rows[0])
    answer_idx = next(
        i for i, m in enumerate(next_input)
        if isinstance(m, dict) and m.get("role") == "assistant"
        and "new answer" in str(m.get("content") or "")
    )
    assert user_idx < answer_idx, (
        "the next-send input keeps user-before-answer ordering, got "
        f"user at {user_idx}, answer at {answer_idx}"
    )


def test_identity_less_legacy_gate_keeps_text_fallback(hermes_home):
    """The identity-less legacy shape (no pending_started_at, no token)
    keeps the text-equality gate: a same-text row at/before the marker
    still proves the turn was already recovered there.
    """
    sid = "r8_legacy_text_fallback"
    stream_id = "stream-r8-legacy"
    append_run_event(sid, stream_id, "token", {"text": "new answer"})

    session = Session(
        session_id=sid,
        title="r8 legacy text fallback",
        messages=[
            {"role": "user", "content": "run the check", "timestamp": 100},
            {"role": "assistant", "content": "old answer", "timestamp": 200},
            _marker(stream_id),
        ],
        pending_user_message="run the check",
        pending_started_at=None,
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

    # Same text at/before the marker + no pending identity: the gate treats
    # the turn as already recovered and must NOT materialize a duplicate
    # same-text user row.
    assert len(_current_user_rows(session)) == 0, (
        "identity-less legacy gate keeps the text-equality suppression, "
        "got a duplicate 900.5 row"
    )
    user_rows = [m for m in session.messages if m.get("role") == "user"]
    assert len(user_rows) == 1, (
        "the historical same-text row is the only user row in the legacy shape"
    )


def test_malformed_pending_started_at_is_not_turn_identity(hermes_home):
    """A hand-edited ``pending_started_at: 0`` is malformed authority, not
    turn identity (#7167 review 2026-10-27, should-fix): the gate must fall
    back to the legacy text-equality rule instead of demanding an ownership
    no row can ever prove. Pre-fix, the 0 was honoured as identity, the
    gate refused to suppress, and the repair appended the prompt AGAIN
    after the marker with the answer below the notice.
    """
    sid = "r8_malformed_started_at"
    stream_id = "stream-r8-malformed"
    append_run_event(sid, stream_id, "token", {"text": "new answer"})

    session = Session(
        session_id=sid,
        title="r8 malformed pending_started_at falls back to text gate",
        messages=[
            {"role": "user", "content": "run the check", "timestamp": 100},
            {"role": "assistant", "content": "old answer", "timestamp": 200},
            _marker(stream_id),
        ],
        pending_user_message="run the check",
        pending_started_at=0,
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

    # The malformed 0 must not be treated as identity: the same-text row at
    # or before the marker still proves the turn was already recovered
    # there, so no duplicate same-text user row is materialized.
    user_rows = [m for m in session.messages if m.get("role") == "user"]
    assert len(user_rows) == 1, (
        "pending_started_at=0 is malformed authority — the text-equality "
        "gate must apply, got a duplicate prompt row: "
        f"{[str(m.get('content'))[:24] for m in session.messages]!r}"
    )
