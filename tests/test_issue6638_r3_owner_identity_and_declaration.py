"""#6638 re-review (2026-11-23): saved-owner identity and declaration recovery.

Both findings were reproduced through the production ``GET /api/session``
handler before this spec was written (3 failures / 3 controls on the reviewed
head ``292729ac3``):

1. A surviving shared assistant loses its tool card.  ``_merged_session_messages_for_display``
   dedupes a shared row by merge key and keeps the CLI copy, so the saved
   sidecar row that owns the card is not present by object identity in the
   merged list.  ``_display_exact_owner_positions`` therefore returns a partial
   map, it is treated as complete, and ``_display_rebased_tool_calls`` filters
   the card out.  A nonempty identity map is not proof that every surviving
   saved owner is represented.
2. Recovery admits a tool result without its declaration.  In
   ``_merge_session_messages_append_only_impl`` a state-only assistant row that
   carries ``tool_calls`` reaches the legacy nonempty-``tool_calls`` discard
   branch before the ``keep_state_rows_missing_from_sidecar`` opt-in, so the
   declaration is dropped while its result and final answer are recovered.
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


# The saved sidecar holds the shared user/assistant pair plus a WebUI-only
# assistant and it OWNS the cards.  state.db has the same shared pair plus two
# newer rows, so the display merge takes the CLI-dominant dedupe path and the
# CLI copies of the shared rows win.
SHARED_SIDECAR = [
    {"role": "user", "content": "q1", "timestamp": 1.0},
    {"role": "assistant", "content": "a1", "timestamp": 2.0},
    {"role": "assistant", "content": "webui only", "timestamp": 4.0},
]

SHARED_STATE = [
    {"role": "user", "content": "q1", "timestamp": 1.0},
    {"role": "assistant", "content": "a1", "timestamp": 2.0},
    {"role": "user", "content": "q5", "timestamp": 5.0},
    {"role": "assistant", "content": "a6", "timestamp": 6.0},
]

SHARED_CONTENTS = ["q1", "a1", "webui only", "q5", "a6"]


def _shared_cards():
    return [
        {"id": "tc-shared", "name": "read_file", "assistant_msg_idx": 1},
        {"id": "tc-webui", "name": "write_file", "assistant_msg_idx": 2},
    ]


def _assert_cards_follow_their_owners(messages, cards):
    owners = {card["id"]: card["assistant_msg_idx"] for card in cards}
    assert set(owners) == {"tc-shared", "tc-webui"}, cards
    assert messages[owners["tc-shared"]]["content"] == "a1"
    assert messages[owners["tc-webui"]]["content"] == "webui only"


def test_shared_assistant_keeps_its_card_in_the_full_response(monkeypatch):
    """Finding 1: the saved card of a duplicated shared assistant must survive."""
    session = _make_session(SHARED_SIDECAR, tool_calls=_shared_cards())
    payload = _drive_session_get(monkeypatch, session, SHARED_STATE)
    messages = payload["session"]["messages"]
    assert [m.get("content") for m in messages] == SHARED_CONTENTS
    _assert_cards_follow_their_owners(messages, payload["session"]["tool_calls"])


def test_shared_assistant_keeps_its_card_in_a_paged_response(monkeypatch):
    """Finding 1 also holds for the windowed (msg_limit) payload."""
    session = _make_session(SHARED_SIDECAR, tool_calls=_shared_cards())
    payload = _drive_session_get(
        monkeypatch, session, SHARED_STATE, query="session_id=tip&msg_limit=30"
    )
    messages = payload["session"]["messages"]
    assert [m.get("content") for m in messages] == SHARED_CONTENTS
    _assert_cards_follow_their_owners(messages, payload["session"]["tool_calls"])


def test_card_without_any_surviving_owner_is_still_dropped(monkeypatch):
    """Control: genuine missing-owner dropping must not become a blind union."""
    session = _make_session(
        SHARED_SIDECAR,
        tool_calls=[
            {"id": "tc-webui", "name": "write_file", "assistant_msg_idx": 2},
            {"id": "tc-ghost", "name": "grep", "assistant_msg_idx": 99},
        ],
    )
    payload = _drive_session_get(monkeypatch, session, SHARED_STATE)
    cards = {card["id"] for card in payload["session"]["tool_calls"]}
    assert cards == {"tc-webui"}, cards


# state.db carries a tool-call declaration, its result and the final answer that
# the sidecar never observed; the sidecar still has rows after them, so the merge
# takes the sidecar-dominant append-only path.
ORDINAL_SIDECAR = [
    {"role": "user", "content": "q1", "timestamp": 1.0},
    {"role": "tool", "content": "tool output", "timestamp": 2.5},
    {"role": "assistant", "content": "a2", "timestamp": 3.0},
    {"role": "user", "content": "q3", "timestamp": 5.0},
    {"role": "assistant", "content": "a3", "timestamp": 6.0},
]

ORDINAL_STATE = [
    {"role": "user", "content": "q1", "timestamp": 1.0},
    {
        "role": "assistant",
        "content": "checking",
        "timestamp": 2.0,
        "tool_calls": [{"id": "call-1", "name": "read_file"}],
    },
    {"role": "tool", "content": "tool output", "timestamp": 2.5},
    {"role": "assistant", "content": "final answer", "timestamp": 2.9},
    {"role": "assistant", "content": "a2", "timestamp": 3.0},
]


def test_state_only_declaration_is_recovered_with_result_and_final(monkeypatch):
    """Finding 2: the recovered result must not arrive without its declaration."""
    session = _make_session(ORDINAL_SIDECAR)
    payload = _drive_session_get(monkeypatch, session, ORDINAL_STATE)
    contents = [m.get("content") for m in payload["session"]["messages"]]
    assert "checking" in contents, contents
    assert "tool output" in contents, contents
    assert "final answer" in contents, contents
    assert contents.index("checking") < contents.index("tool output")
    assert contents.index("tool output") < contents.index("final answer")


def test_declaration_is_not_recovered_when_a_watermark_is_active(monkeypatch):
    """Control: the truncation watermark keeps the deleted suffix dead."""
    session = _make_session(
        ORDINAL_SIDECAR, truncation_watermark=2.0, truncation_boundary=2.0
    )
    payload = _drive_session_get(monkeypatch, session, ORDINAL_STATE)
    contents = [m.get("content") for m in payload["session"]["messages"]]
    assert "checking" not in contents, contents


CANCELLED_DECL_SIDECAR = [
    {"role": "user", "content": "q1", "timestamp": 1.0},
    {"role": "assistant", "content": "a1", "timestamp": 2.0},
    {"role": "user", "content": "q2", "timestamp": 3.0},
    {"role": "assistant", "content": "", "timestamp": 3.5, "_partial": True},
    {"role": "assistant", "content": "stopped attempt", "timestamp": 4.0,
     "_error": True, "type": "cancelled"},
    {"role": "user", "content": "q3", "timestamp": 5.0},
    {"role": "assistant", "content": "a3", "timestamp": 6.0},
]

CANCELLED_DECL_STATE = [
    {"role": "user", "content": "q1", "timestamp": 1.0},
    {"role": "assistant", "content": "a1", "timestamp": 2.0},
    {"role": "user", "content": "q2", "timestamp": 3.0},
    {
        "role": "assistant",
        "content": "CANCELLED_DECL",
        "timestamp": 3.6,
        "tool_calls": [{"id": "call-x", "name": "read_file"}],
    },
    {"role": "tool", "content": "CANCELLED_TOOL", "timestamp": 3.7},
    {"role": "user", "content": "q3", "timestamp": 5.0},
    {"role": "assistant", "content": "a3", "timestamp": 6.0},
]


def test_cancelled_journal_declaration_stays_dead(monkeypatch):
    """Control: the new recovery must not resurrect a cancelled turn."""
    session = _make_session(CANCELLED_DECL_SIDECAR)
    payload = _drive_session_get(monkeypatch, session, CANCELLED_DECL_STATE)
    contents = [m.get("content") for m in payload["session"]["messages"]]
    assert "CANCELLED_DECL" not in contents, contents
    assert "CANCELLED_TOOL" not in contents, contents
