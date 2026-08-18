"""Regression: Agent ``_row_id`` must reconcile with WebUI state provenance."""


def test_agent_and_webui_row_id_aliases_resolve_to_the_same_sqlite_row():
    from api.models import _state_db_row_identity_details

    agent_identity, agent_valid = _state_db_row_identity_details({"_row_id": 780107})
    webui_identity, webui_valid = _state_db_row_identity_details(
        {"_state_db_row_id": 780107}
    )

    assert agent_valid is True
    assert webui_valid is True
    assert agent_identity == webui_identity
    assert agent_identity is not None


def test_agent_row_id_alias_dedupes_restamped_prompt_before_final_answer():
    from api.models import merge_session_messages_append_only

    sidecar_messages = [
        {
            "role": "assistant",
            "content": "earlier activity",
            "timestamp": 100.0,
            "_state_db_row_id": 700,
            "api_content": "earlier activity wire",
        },
        {
            "role": "user",
            "content": "original maintenance question",
            "timestamp": 110.0,
            "_row_id": 780107,
            "api_content": "original maintenance question wire",
        },
        {
            "role": "assistant",
            "content": "settled final answer",
            "timestamp": 130.0,
        },
    ]
    state_messages = [
        {
            "role": "user",
            "content": "original maintenance question",
            "timestamp": 120.0,
            "_state_db_row_id": 780107,
            "api_content": "original maintenance question wire",
        }
    ]

    merged = merge_session_messages_append_only(sidecar_messages, state_messages)

    assert [message["content"] for message in merged] == [
        "earlier activity",
        "original maintenance question",
        "settled final answer",
    ]


def test_imported_row_id_is_not_trusted_as_state_identity():
    """JSON import must not keep a caller-supplied ``_row_id`` as provenance.

    Replay merge now treats ``_row_id`` as an Agent/WebUI identity alias. Import
    must strip that field the same way it strips ``_state_db_row_id``, or a
    crafted import can collide with a later genuine state.db turn.
    """
    from api.helpers import strip_public_internal_fields
    from api.models import _state_db_row_identity_details

    imported = strip_public_internal_fields(
        {
            "messages": [
                {
                    "role": "user",
                    "content": "same prompt",
                    "timestamp": 10.0,
                    "_row_id": 780107,
                    "_state_db_row_id": 780107,
                }
            ]
        }
    )
    imported_msg = imported["messages"][0]
    assert isinstance(imported_msg, dict)
    assert "_row_id" not in imported_msg
    assert "_state_db_row_id" not in imported_msg
    identity, valid = _state_db_row_identity_details(imported_msg)
    assert valid is True
    assert identity is None
