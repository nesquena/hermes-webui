from api.helpers import redact_session_data
from api.models import Session
from api.session_ops import (
    RegenerationUnavailable,
    regeneration_authority,
    regeneration_revision_for,
    regeneration_state,
    resolve_regeneration_turn,
)
from api.streaming import _session_payload_with_full_messages


def _session():
    return Session(
        session_id="authority6611",
        messages=[{"role": "user", "content": "p", "_source": "webui"}, {"role": "assistant", "content": "a"}],
        context_messages=[{"role": "user", "content": "p"}, {"role": "assistant", "content": "a"}],
    )


def test_all_terminal_payloads_carry_fresh_revision():
    session = _session()
    payload = _session_payload_with_full_messages(session)
    assert payload["regeneration_revision"] == regeneration_authority(session, rows=session.messages)
    assert payload["regeneration_revision"] == regeneration_revision_for(session.messages, session=session, context=session.context_messages)


def test_private_active_turn_token_never_public():
    session = _session()
    session.messages[-2]["_active_turn_token"] = "private"
    public = redact_session_data(session.compact() | {"messages": session.messages})
    assert "_active_turn_token" not in str(public)


def test_private_active_turn_token_is_redacted_in_nested_context_and_journal():
    session = _session()
    session_dict = session.compact() | {
        "messages": session.messages,
        "context_messages": [
            {"role": "user", "content": "p", "_active_turn_token": "secret"}
        ],
        "runtime_journal_snapshot": {
            "context_messages": [
                {"role": "user", "content": "p", "_active_turn_token": "secret"}
            ]
        },
    }
    public = redact_session_data(session_dict)
    assert public["context_messages"][0].get("_active_turn_token") is None
    assert public["runtime_journal_snapshot"]["context_messages"][0].get("_active_turn_token") is None


def test_public_active_turn_marker_matches_only_the_active_user_row():
    from api.process_event_utils import build_active_turn_token

    session = _session()
    session.active_stream_id = "stream-active"
    session.pending_started_at = 123.0
    token = build_active_turn_token(session.active_stream_id, session.pending_started_at)
    session.messages[0]["_active_turn_token"] = token
    session.messages.insert(0, {"role": "user", "content": "older", "_active_turn_token": "other"})
    public = redact_session_data(
        session.compact()
        | {
            "active_stream_id": session.active_stream_id,
            "pending_started_at": session.pending_started_at,
            "messages": session.messages,
            "context_messages": [
                {"role": "user", "content": "older", "_active_turn_token": "other"},
                {"role": "user", "content": "p", "_active_turn_token": token},
            ],
        }
    )
    assert public["messages"][1]["_active_turn_user"] is True
    assert "_active_turn_user" not in public["messages"][0]
    assert public["context_messages"][1]["_active_turn_user"] is True
    assert "_active_turn_token" not in str(public)


def test_parent_or_foreign_source_refuses_authority():
    session = _session()
    session.messages[-2]["_source"] = "cron"
    assert regeneration_authority(session, rows=session.messages) is None
    try:
        resolve_regeneration_turn(session)
    except RegenerationUnavailable as exc:
        assert exc.code == "regeneration_read_only"
    else:
        raise AssertionError("foreign source was accepted")


def test_fork_child_has_regeneration_authority():
    session = _session()
    session.session_source = "fork"
    session.parent_session_id = "parent-6611"
    session.messages[-2]["_fork_child_turn"] = True
    revision = regeneration_authority(session)
    assert revision
    assert resolve_regeneration_turn(session, expected_revision=revision).source == "webui"


def test_fork_without_child_lineage_refuses_authority():
    session = _session()
    session.session_source = "fork"
    session.parent_session_id = None
    assert regeneration_authority(session) is None
    try:
        resolve_regeneration_turn(session)
    except RegenerationUnavailable as exc:
        assert exc.code == "regeneration_read_only"
    else:
        raise AssertionError("parent-only fork state was accepted")


def test_authority_withholds_a_noncanonical_display_projection():
    session = _session()
    canonical_rows, canonical_context = regeneration_state(session)
    projected_rows = canonical_rows + [{"role": "tool", "content": "stitched parent"}]
    assert regeneration_authority(
        session,
        rows=projected_rows,
        context=canonical_context,
    ) is None


def test_terminal_payload_embeds_the_rows_it_hashes(monkeypatch):
    session = _session()
    canonical_rows = [
        {"role": "user", "content": "recovered", "_source": "webui"},
        {"role": "assistant", "content": "answer"},
    ]
    canonical_context = list(canonical_rows)
    monkeypatch.setattr(
        "api.session_ops.regeneration_state",
        lambda _session: (canonical_rows, canonical_context),
    )
    payload = _session_payload_with_full_messages(session)
    assert payload["messages"] == canonical_rows
    assert payload["message_count"] == len(canonical_rows)
    assert payload["regeneration_revision"] == regeneration_authority(
        session,
        rows=canonical_rows,
        context=canonical_context,
    )


def test_recovered_display_context_pair_survives_local_and_gateway_apply(monkeypatch):
    session = _session()
    canonical_rows = [
        {"role": "user", "content": "recovered", "id": "u-recovered", "_source": "webui"},
        {"role": "assistant", "content": "failed"},
    ]
    canonical_context = [
        {"role": "system", "content": "recovered context only"},
        *canonical_rows,
    ]
    monkeypatch.setattr(
        "api.session_ops.regeneration_state",
        lambda _session: (canonical_rows, canonical_context),
    )
    from api.session_ops import apply_regeneration_plan, plan_regeneration

    plan = plan_regeneration(session)
    assert apply_regeneration_plan(session, plan)
    assert session.messages == canonical_rows[:1]
    assert any(row.get("content") == "recovered" for row in session.context_messages)
    payload = _session_payload_with_full_messages(session)
    assert payload["messages"] == canonical_rows
    assert payload["message_count"] == len(canonical_rows)
