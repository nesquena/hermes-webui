"""#6638 re-gate: cancelled-journal rows must not return, tool cards must follow their owner.

The #6637 relaxation (``keep_state_rows_missing_from_sidecar``) re-attaches
state.db rows the stale sidecar never observed.  The 2026-10-14 review found two
blockers when that relaxation is exercised through the production
``GET /api/session`` handler:

1. [CORE] A session with cancelled-journal state resurrects the cancelled turn
   (``CANCELLED_REPLAY`` / ``CANCELLED_TOOL``) after a later successful turn,
   because the messaging display caller passes neither ``incoming_provenance``
   nor the cancelled-journal owner/source messages, so the journal proof never
   runs.
2. [SILENT] Inserting a recovered assistant row shifts the saved owner of a
   legacy tool card (index 3 -> 4) while ``assistant_msg_idx`` stays at 3, so a
   paginated load shows the owning assistant and filters the card out.  The
   index rebase currently runs only for cancelled sessions.

Both cases drive ``routes.handle_get`` (the real handler) with the same seams
the file's existing tests use.
"""

from api import routes

def _make_session(messages, tool_calls=None, **attrs):
    class DummySession:
        def __init__(self):
            self.messages = [dict(m) for m in messages]
            self.tool_calls = [dict(c) for c in (tool_calls or [])]
            self.active_stream_id = None
            self.pending_user_message = None
            self.pending_attachments = []
            self.pending_started_at = None
            self.context_length = 0
            self.threshold_tokens = 0
            self.last_prompt_tokens = 0
            self.model = "openai/gpt-5"
            self.session_id = "tip"
            self.truncation_watermark = None
            self.truncation_boundary = None
            for name, value in attrs.items():
                setattr(self, name, value)

        def compact(self):
            return {"session_id": "tip", "title": "Tip", "model": "openai/gpt-5"}

    return DummySession()


def _drive_session_get(monkeypatch, session, cli_messages, query="session_id=tip"):
    """Run the production GET handler for a messaging session and return the payload."""
    captured = {}

    monkeypatch.setattr(routes, "get_session", lambda sid, metadata_only=False: session)
    monkeypatch.setattr(routes, "_clear_stale_stream_state", lambda s: None)
    monkeypatch.setattr(
        routes, "_lookup_cli_session_metadata", lambda sid: {"session_source": "messaging"}
    )
    monkeypatch.setattr(routes, "_is_messaging_session_record", lambda s: True)
    monkeypatch.setattr(routes, "get_cli_session_messages", lambda sid: list(cli_messages))
    monkeypatch.setattr(
        routes,
        "_resolve_effective_session_model_for_display",
        lambda s: getattr(s, "model", None),
    )
    monkeypatch.setattr(
        routes, "_resolve_effective_session_model_provider_for_display", lambda s: None
    )
    monkeypatch.setattr(routes, "_merge_cli_sidebar_metadata", lambda raw, meta: raw)
    monkeypatch.setattr(routes, "redact_session_data", lambda raw: raw)
    monkeypatch.setattr(
        routes,
        "j",
        lambda handler, payload, status=200: captured.setdefault("payload", payload),
    )

    class Handler:
        pass

    class Parsed:
        path = "/api/session"
        pass

    parsed = Parsed()
    parsed.query = query
    routes.handle_get(Handler(), parsed)
    return captured["payload"]


CANCELLED_SIDECAR = [
    {"role": "user", "content": "q1", "timestamp": 1.0},
    {"role": "assistant", "content": "a1", "timestamp": 2.0},
    {"role": "user", "content": "q2", "timestamp": 3.0},
    {"role": "assistant", "content": "", "timestamp": 3.5, "_partial": True},
    {"role": "assistant", "content": "stopped attempt", "timestamp": 4.0,
     "_error": True, "type": "cancelled"},
    {"role": "user", "content": "q3", "timestamp": 5.0},
    {"role": "assistant", "content": "a3", "timestamp": 6.0},
]

# state.db: the cancelled turn was replayed (different content, plus its tool
# row) and a later successful turn exists.  Same length as the sidecar so the
# display merge takes the sidecar-dominant append-only branch.
CANCELLED_STATE = [
    {"role": "user", "content": "q1", "timestamp": 1.0},
    {"role": "assistant", "content": "a1", "timestamp": 2.0},
    {"role": "user", "content": "q2", "timestamp": 3.0},
    {"role": "assistant", "content": "CANCELLED_REPLAY", "timestamp": 4.0},
    {"role": "tool", "content": "CANCELLED_TOOL", "timestamp": 4.5},
    {"role": "user", "content": "q3", "timestamp": 5.0},
    {"role": "assistant", "content": "a3", "timestamp": 6.0},
]


def test_cancelled_journal_rows_do_not_return_through_the_session_endpoint(monkeypatch):
    """[CORE] A cancelled turn stays dead even after a later successful turn."""
    session = _make_session(CANCELLED_SIDECAR)
    payload = _drive_session_get(monkeypatch, session, CANCELLED_STATE)
    contents = [m.get("content") for m in payload["session"]["messages"]]
    assert "CANCELLED_REPLAY" not in contents
    assert "CANCELLED_TOOL" not in contents
    assert contents == ["q1", "a1", "q2", "", "stopped attempt", "q3", "a3"]


def test_state_only_assistant_row_still_survives_without_cancelled_journal(monkeypatch):
    """Control: the #6637 relaxation itself must keep working."""
    sidecar = [
        {"role": "user", "content": "q1", "timestamp": 1.0},
        {"role": "assistant", "content": "a1", "timestamp": 2.0},
        {"role": "user", "content": "q2", "timestamp": 3.0},
        {"role": "assistant", "content": "a2", "timestamp": 6.0},
    ]
    state = [
        {"role": "user", "content": "q1", "timestamp": 1.0},
        {"role": "assistant", "content": "state only reply", "timestamp": 2.5},
        {"role": "assistant", "content": "a1", "timestamp": 2.0},
        {"role": "user", "content": "q2", "timestamp": 3.0},
    ]
    session = _make_session(sidecar)
    payload = _drive_session_get(monkeypatch, session, state)
    contents = [m.get("content") for m in payload["session"]["messages"]]
    assert "state only reply" in contents
    assert contents == ["q1", "a1", "state only reply", "q2", "a2"]


TOOL_CARD_SIDECAR = [
    {"role": "user", "content": "q1", "timestamp": 1.0},
    {"role": "assistant", "content": "a1", "timestamp": 2.0},
    {"role": "user", "content": "q2", "timestamp": 3.0},
    {"role": "assistant", "content": "a2", "timestamp": 6.0},
]

TOOL_CARD_STATE_SHIFTED = [
    {"role": "user", "content": "q1", "timestamp": 1.0},
    {"role": "assistant", "content": "recovered reply", "timestamp": 1.5},
    {"role": "assistant", "content": "a1", "timestamp": 2.0},
    {"role": "user", "content": "q2", "timestamp": 3.0},
]


def test_legacy_tool_card_follows_its_owner_when_a_row_is_recovered(monkeypatch):
    """[SILENT] ``assistant_msg_idx`` must be rebased onto the merged display list."""
    session = _make_session(
        TOOL_CARD_SIDECAR,
        tool_calls=[{"id": "tc1", "name": "read_file", "assistant_msg_idx": 1}],
    )
    payload = _drive_session_get(
        monkeypatch, session, TOOL_CARD_STATE_SHIFTED, query="session_id=tip&msg_limit=30"
    )
    messages = payload["session"]["messages"]
    cards = payload["session"]["tool_calls"]
    assert [m.get("content") for m in messages] == [
        "q1",
        "recovered reply",
        "a1",
        "q2",
        "a2",
    ]
    assert len(cards) == 1
    owner = cards[0]["assistant_msg_idx"]
    assert owner == 2
    assert messages[owner]["content"] == "a1"


def test_legacy_tool_card_keeps_its_index_when_nothing_is_recovered(monkeypatch):
    """Control: no insertion, no rebase."""
    session = _make_session(
        TOOL_CARD_SIDECAR,
        tool_calls=[{"id": "tc1", "name": "read_file", "assistant_msg_idx": 1}],
    )
    payload = _drive_session_get(
        monkeypatch, session, list(TOOL_CARD_SIDECAR), query="session_id=tip&msg_limit=30"
    )
    messages = payload["session"]["messages"]
    cards = payload["session"]["tool_calls"]
    assert len(cards) == 1
    owner = cards[0]["assistant_msg_idx"]
    assert owner == 1
    assert messages[owner]["content"] == "a1"
