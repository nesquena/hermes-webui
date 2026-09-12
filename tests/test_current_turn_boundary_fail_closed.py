"""Current-turn ownership is proven by the producer contract or not claimed at all.

Contract v2 (declared by the Agent callable as ``TURN_BOUNDARY_CONTRACT``): every
result envelope carries ``turn_boundary_contract``, ``turn_id`` (this invocation's),
``messages_projection`` ("full": the exact final ``messages`` list) and a nullable
``current_turn_user_idx`` that addresses the row the Agent stamped with the same
``_turn_id`` marker at append time. The WebUI reads the capability from the callable
before each invocation (replacement Agents included), binds the envelope to the live
turn id, validates the index by marker — never by prompt text — and otherwise leaves
the turn capable-but-unproven: no historical row receives the live token, no prior
answer is persisted or classified as this turn's, no ``done``. Legacy callables
(no contract) keep only a prefix-intact instance index; rewritten results fail closed.
"""

import copy
import types

import pytest

from api import streaming
from tests.test_tool_limit_terminal_state import _run_streaming_with_fake_agent

PROMPT = "Fix the failing test."
OLD_ANSWER = "The earlier attempt is complete."
NEW_ANSWER = "Done: parser fixed, tests green."
TURN_ID = "turn-current"


def _history():
    return [
        {"role": "user", "content": PROMPT, "timestamp": 1.0},
        {"role": "assistant", "content": OLD_ANSWER},
        {"role": "user", "content": "Also update the docs.", "timestamp": 1.2},
        {"role": "assistant", "content": "Docs updated."},
    ]


def _rewritten(current=True, answer=NEW_ANSWER):
    """Compaction dropped the tail; the identical historical prompt survives (unmarked)."""
    rows = [{"role": "user", "content": PROMPT}, {"role": "assistant", "content": OLD_ANSWER}]
    if current:
        rows.append({"role": "user", "content": PROMPT})
        if answer is not None:
            rows.append({"role": "assistant", "content": answer})
    return rows


def _envelope(messages, *, turn_id=TURN_ID, marked_idx=None, projection="full", contract=2, **extra):
    """A producer-conformant envelope: the marked row (if any) carries ``_turn_id``."""
    messages = copy.deepcopy(messages)
    if marked_idx is not None:
        messages[marked_idx]["_turn_id"] = turn_id
    envelope = {"messages": messages, "turn_boundary_contract": contract, "turn_id": turn_id,
                "messages_projection": projection, "current_turn_user_idx": marked_idx}
    envelope.update(extra)
    return envelope


def _identity(token="tok-current"):
    return {"session_id": "s", "token": token, "text": PROMPT, "timestamp": 1.9, "source": "webui",
            "attachments": [], "checkpoint": None, "current_turn_user_idx": None, "turn_id": ""}


def _agent(idx=0, turn_id=TURN_ID, contract=2):
    agent = types.SimpleNamespace(_persist_user_message_idx=idx, _current_turn_id=turn_id)
    if contract is not None:
        agent.TURN_BOUNDARY_CONTRACT = contract
    return agent


def _resolve(result, agent):
    return streaming._resolve_active_turn_authority(_identity(), result=result, agent=agent)


def _tokened(messages):
    return [i for i, m in enumerate(messages) if isinstance(m, dict) and m.get("_active_turn_token")]


def _assistant_texts(messages):
    return [m.get("content") for m in messages if m.get("role") == "assistant"]


# ── producer-conformance: what the resolver accepts and rejects ────────────


def test_conformant_envelope_binds_by_marker_only():
    result = _envelope(_rewritten(), marked_idx=2, completed=True, final_response=NEW_ANSWER)
    identity = _resolve(result, _agent(idx=0))  # stale instance index is never consulted
    assert identity["producer_contract"] == 2 and identity["messages_projection"] == "full"
    assert identity["agent_turn_boundary_source"] == "result" and identity["current_turn_user_idx"] == 2
    assert _tokened(result["messages"]) == [2]
    history = _history()
    assert streaming._find_active_turn_checkpoint_index(result["messages"], history, identity, PROMPT) == 2
    assert streaming._assistant_reply_added_after_current_turn(result["messages"], history, PROMPT, active_turn_identity=identity) is True
    assert streaming._self_heal_result_succeeded(result, history, identity, PROMPT) is True
    partial = streaming._append_result_partial_on_error(
        types.SimpleNamespace(messages=list(history)), {**result, "partial": True}, history, PROMPT, active_turn_identity=identity,
    )
    assert partial is not None and partial["content"] == NEW_ANSWER


@pytest.mark.parametrize("case", [
    "capable_omission",        # producer could not identify the row: index None
    "index_on_unmarked_row",   # text-matching row without the marker (the old text-derived export)
    "index_on_assistant_row",
    "foreign_turn_id",         # envelope from another invocation
    "live_turn_id_unavailable",
    "missing_projection",
    "unknown_projection",
    "contract_version_missing",
])
def test_unproven_or_malformed_envelopes_fail_closed(case):
    history = _history()
    messages = _rewritten(current=False)  # only the identical historical prompt survives
    agent = _agent(idx=0)
    if case == "capable_omission":
        result = _envelope(messages, marked_idx=None)
    elif case == "index_on_unmarked_row":
        result = _envelope(messages, marked_idx=None); result["current_turn_user_idx"] = 0
    elif case == "index_on_assistant_row":
        result = _envelope(messages, marked_idx=None); result["current_turn_user_idx"] = 1
    elif case == "foreign_turn_id":
        result = _envelope(messages, marked_idx=0, turn_id="turn-historical")
    elif case == "live_turn_id_unavailable":
        result = _envelope(messages, marked_idx=0); agent = _agent(idx=0, turn_id="")
    elif case == "missing_projection":
        result = _envelope(messages, marked_idx=0); del result["messages_projection"]
    elif case == "unknown_projection":
        result = _envelope(messages, marked_idx=0, projection="partial")
    else:
        result = _envelope(messages, marked_idx=0); del result["turn_boundary_contract"]
    result.update({"completed": True, "final_response": OLD_ANSWER})
    identity = _resolve(result, agent)
    assert identity["producer_capability"] == "turn_boundary"
    assert identity.get("agent_turn_boundary_source") is None  # never the instance index either
    assert _tokened(result["messages"]) == []
    assert streaming._find_active_turn_checkpoint_index(result["messages"], history, identity, PROMPT) is None
    assert streaming._settle_current_turn_boundary(history, list(result["messages"]), identity, PROMPT, "webui") == result["messages"]
    assert streaming._assistant_reply_added_after_current_turn(result["messages"], history, PROMPT, active_turn_identity=identity) is False
    assert streaming._self_heal_result_succeeded(result, history, identity, PROMPT) is False
    assert streaming._append_result_partial_on_error(
        types.SimpleNamespace(messages=list(history)), {**result, "partial": True}, history, PROMPT, active_turn_identity=identity,
    ) is None
    assert streaming._merged_transcript_lacks_final_assistant_answer(history, history, result["messages"], PROMPT, active_turn_identity=identity) is True


def test_full_projection_of_historical_output_rows_is_never_a_delta():
    rows = [{"role": "assistant", "content": "Docs updated."}, {"role": "tool", "tool_call_id": "c1", "content": "x"}]
    identity = _resolve(_envelope(rows), _agent())
    assert streaming._result_is_delta_projection(identity, rows) is False
    assert streaming._settle_current_turn_boundary(_history(), list(rows), identity, PROMPT, "webui") == rows
    declared = _resolve(_envelope(rows, projection="delta"), _agent())
    assert streaming._result_is_delta_projection(declared, rows) is True
    legacy = _resolve({"messages": rows}, _agent(contract=None))
    assert streaming._result_is_delta_projection(legacy, rows) is False  # no role inference with an identity


def test_legacy_callable_uses_only_a_prefix_intact_instance_index():
    history = _history()
    appended = history + [{"role": "user", "content": PROMPT}, {"role": "assistant", "content": NEW_ANSWER}]
    identity = _resolve({"messages": appended}, _agent(idx=len(history), contract=None))
    assert identity["producer_capability"] == "unproven" and identity["agent_turn_boundary_source"] == "agent"
    assert streaming._find_active_turn_checkpoint_index(appended, history, identity, PROMPT) == len(history)
    # result keys from a legacy callable are ignored (their index is text-derived) ...
    rewritten = _rewritten(); rewritten[0]["_turn_id"] = TURN_ID
    identity = _resolve({"messages": rewritten, "turn_id": TURN_ID, "current_turn_user_idx": 0}, _agent(idx=0, contract=None))
    assert identity["agent_turn_boundary_source"] == "agent"
    # ... and the instance index is not trusted after a rewrite
    assert streaming._find_active_turn_checkpoint_index(rewritten, history, identity, PROMPT) is None
    assert _tokened(rewritten) == []


def test_compression_marker_and_unknown_role_history_fail_closed_without_marker():
    history = _history()
    rows = [
        {"role": "assistant", "content": "[Context compressed]", "_compressed_summary": True},
        {"role": "system", "content": "unknown-role scaffolding"},
        {"role": "user", "content": PROMPT},
        {"role": "assistant", "content": OLD_ANSWER},
    ]
    identity = _resolve(_envelope(rows, marked_idx=None), _agent(idx=2))
    assert streaming._find_active_turn_checkpoint_index(rows, history, identity, PROMPT) is None
    assert streaming._settle_current_turn_boundary(history, list(rows), identity, PROMPT, "webui") == rows
    proven_env = _envelope(rows, marked_idx=2)
    proven = _resolve(proven_env, _agent(idx=2))
    assert streaming._find_active_turn_checkpoint_index(proven_env["messages"], history, proven, PROMPT) == 2


# ── production-composed streaming route ───────────────────────────────────


def _run(tmp_path, monkeypatch, result, **kw):
    kw.setdefault("prior_messages", _history())
    kw.setdefault("prior_context_messages", _history())
    kw.setdefault("msg_text", PROMPT)
    kw.setdefault("pending_started_at", 1.9)
    kw.setdefault("current_turn_user_idx", 0)  # stale instance index; live turn id "turn-current"
    kw.setdefault("agent_contract", 2)
    events, payload = _run_streaming_with_fake_agent(tmp_path, monkeypatch, result, **kw)
    kinds = [event for event, _payload in events]
    session = streaming.SESSIONS["tool_limit_session"]
    session._sse_events = events  # raw terminal payloads for public-projection assertions
    return kinds, payload, session


def _prompt_rows(messages):
    return [i for i, m in enumerate(messages) if m.get("role") == "user" and m.get("content") == PROMPT]


def _assert_history_intact_and_unclaimed(kinds, payload, session):
    assert "done" not in kinds
    assert _assistant_texts(payload["messages"]).count(OLD_ANSWER) == 1
    last_old = max(i for i, m in enumerate(payload["messages"]) if m.get("content") == OLD_ANSWER)
    assert _prompt_rows(payload["messages"])[-1] > last_old  # pending prompt follows history, unanswered
    tokened = _tokened(session.context_messages)
    assert all(session.context_messages[i].get("content") == PROMPT for i in tokened)
    assert not (session.context_messages and session.context_messages[0].get("_active_turn_token"))


def test_streaming_repeated_prompt_rewrite_with_capable_omission_fails_closed(tmp_path, monkeypatch):
    kinds, payload, session = _run(tmp_path, monkeypatch, _envelope(_rewritten(current=False), completed=True, final_response=OLD_ANSWER))
    _assert_history_intact_and_unclaimed(kinds, payload, session)
    assert "apperror" in kinds


@pytest.mark.parametrize("callable_contract", [2, 1, None])
def test_streaming_reviewer_blocker_shape_text_derived_pair_is_rejected(tmp_path, monkeypatch, callable_contract):
    # The installed pair-only producer's export: live turn id + index 0 pointing at the
    # identical historical prompt (text-derived, no marker). Never accepted — whether the
    # callable declares v2 (index on an unmarked row is malformed), pair-only, or nothing.
    if callable_contract == 2:
        result = _envelope(_rewritten(current=False), completed=True, final_response=OLD_ANSWER)
        result["current_turn_user_idx"] = 0
    else:
        result = {"messages": _rewritten(current=False), "turn_id": TURN_ID, "current_turn_user_idx": 0,
                  "completed": True, "final_response": OLD_ANSWER}
    kinds, payload, session = _run(tmp_path, monkeypatch, result, agent_contract=callable_contract)
    _assert_history_intact_and_unclaimed(kinds, payload, session)


def test_streaming_foreign_turn_id_envelope_fails_closed(tmp_path, monkeypatch):
    kinds, payload, session = _run(tmp_path, monkeypatch, _envelope(_rewritten(current=False), marked_idx=0, turn_id="turn-historical", completed=True, final_response=OLD_ANSWER))
    _assert_history_intact_and_unclaimed(kinds, payload, session)


def test_streaming_full_projection_of_historical_output_rows_is_not_replayed_or_tool_limit_closed(tmp_path, monkeypatch):
    rows = [{"role": "assistant", "content": "Docs updated."}, {"role": "tool", "tool_call_id": "c1", "content": "x"}]
    kinds, payload, session = _run(tmp_path, monkeypatch, _envelope(rows, turn_exit_reason="max_iterations_reached(30/30)"))
    assert "done" not in kinds
    assert _assistant_texts(payload["messages"]).count("Docs updated.") == 1
    assert _prompt_rows(payload["messages"])[-1] > 3


@pytest.mark.parametrize("projection", ["missing", "partial", 42])
def test_streaming_malformed_projection_fails_closed(tmp_path, monkeypatch, projection):
    result = _envelope(_rewritten(), marked_idx=2, completed=True, final_response=NEW_ANSWER)
    if projection == "missing":
        del result["messages_projection"]
    else:
        result["messages_projection"] = projection
    kinds, payload, session = _run(tmp_path, monkeypatch, result)
    assert "done" not in kinds
    assert _tokened(session.context_messages) in ([], [len(session.context_messages) - 1])


def _contains_key(value, key):
    if isinstance(value, dict):
        return key in value or any(_contains_key(v, key) for v in value.values())
    if isinstance(value, (list, tuple)):
        return any(_contains_key(v, key) for v in value)
    return False


def test_streaming_conformant_envelope_completes_the_turn(tmp_path, monkeypatch):
    result = _envelope(_rewritten(), marked_idx=2, completed=True, final_response=NEW_ANSWER)
    assert result["messages"][2]["_turn_id"] == TURN_ID  # marker binding, before cleanup
    kinds, payload, session = _run(tmp_path, monkeypatch, result)
    assert "done" in kinds and "apperror" not in kinds
    assert [(m["role"], m.get("content")) for m in payload["messages"][-2:]] == [("user", PROMPT), ("assistant", NEW_ANSWER)]
    assert _assistant_texts(payload["messages"]).count(OLD_ANSWER) == 1
    assert _tokened(session.context_messages) == [2]
    # the producer marker converted into the WebUI token and stripped from every
    # projection: durable session state, saved sidecar, and public SSE payloads
    for projection in (session.messages, session.context_messages, payload["messages"], payload.get("context_messages") or []):
        assert not any(isinstance(m, dict) and "_turn_id" in m for m in projection)
    for event, event_payload in session._sse_events:
        assert not _contains_key(event_payload, "_turn_id"), event
        assert not _contains_key(event_payload, "_active_turn_token"), event
    assert result["messages"][2]["_turn_id"] == TURN_ID  # result-envelope rows are copied, not mutated


def test_redact_session_data_strips_the_producer_marker_from_both_projections():
    from api.helpers import redact_session_data

    row = {"role": "user", "content": PROMPT, "_turn_id": TURN_ID, "_active_turn_token": "tok"}
    public = redact_session_data({
        "session_id": "s", "messages": [dict(row), {"role": "assistant", "content": NEW_ANSWER}],
        "context_messages": [dict(row)],
    })
    for projection in ("messages", "context_messages"):
        assert public[projection][0]["content"] == PROMPT
        assert "_turn_id" not in public[projection][0] and "_active_turn_token" not in public[projection][0]


def test_streaming_legacy_callable_with_intact_prefix_completes(tmp_path, monkeypatch):
    history = _history()
    appended = history + [{"role": "user", "content": PROMPT}, {"role": "assistant", "content": NEW_ANSWER}]
    kinds, payload, session = _run(tmp_path, monkeypatch, {"messages": appended, "completed": True, "final_response": NEW_ANSWER},
                                   current_turn_user_idx=len(history), agent_contract=None)
    assert "done" in kinds and "apperror" not in kinds
    assert payload["messages"][-1]["content"] == NEW_ANSWER


def test_streaming_legacy_callable_with_rewritten_result_fails_closed(tmp_path, monkeypatch):
    kinds, payload, session = _run(tmp_path, monkeypatch, {"messages": _rewritten(), "completed": True, "final_response": NEW_ANSWER},
                                   current_turn_user_idx=2, agent_contract=None)
    assert "done" not in kinds
    assert _assistant_texts(payload["messages"]).count(OLD_ANSWER) == 1


def test_streaming_partial_error_persists_the_current_partial_once(tmp_path, monkeypatch):
    kinds, payload, session = _run(tmp_path, monkeypatch, _envelope(
        _rewritten(), marked_idx=2, completed=False, partial=True, failed=False, error="provider hiccup", final_response=NEW_ANSWER,
    ))
    assert "done" not in kinds and "apperror" in kinds
    assert _assistant_texts(payload["messages"]).count(NEW_ANSWER) == 1
    assert _assistant_texts(payload["messages"]).count(OLD_ANSWER) == 1
    assert payload["messages"][-1].get("_error") is True
    assert any(m.get("_partial") and m.get("content") == NEW_ANSWER for m in payload["messages"])


def test_streaming_absent_output_keeps_the_marked_prompt_once_without_done(tmp_path, monkeypatch):
    kinds, payload, session = _run(tmp_path, monkeypatch, _envelope(_rewritten(answer=None), marked_idx=2, completed=False, final_response=""))
    assert "done" not in kinds
    assert len(_prompt_rows(payload["messages"])) == 2  # historical copy + this turn, no extra materialization
    assert _tokened(session.context_messages) == [2]


def _auth_failure():
    return {"completed": False, "messages": [],
            "error": {"error": {"type": "authentication_error", "status_code": 401, "code": "auth_unavailable",
                                "message": "Your authentication token has been invalidated."}}}


def _lanes():
    return [pytest.param(lane, id=lane) for lane in ("result", "exception")]


@pytest.mark.parametrize("lane", _lanes())
def test_streaming_self_heal_replacement_agent_with_conformant_envelope_succeeds(tmp_path, monkeypatch, lane):
    first = RuntimeError("401 unauthorized") if lane == "exception" else _auth_failure()
    heal = _envelope(_rewritten(), marked_idx=2, turn_id="turn-heal", completed=True, final_response=NEW_ANSWER)
    kinds, payload, session = _run(
        tmp_path, monkeypatch, heal, agent_results=[first, heal], enable_auth_retry=True,
        agent_profiles=[{"turn_id": TURN_ID, "current_turn_user_idx": 0, "contract": 2},      # failed Agent
                        {"turn_id": "turn-heal", "current_turn_user_idx": 7, "contract": 2}],  # replacement: new id, stale index
    )
    assert "done" in kinds and "apperror" not in kinds
    assert [(m["role"], m.get("content")) for m in payload["messages"][-2:]] == [("user", PROMPT), ("assistant", NEW_ANSWER)]
    assert _assistant_texts(payload["messages"]).count(OLD_ANSWER) == 1


@pytest.mark.parametrize("lane", _lanes())
@pytest.mark.parametrize("variant", ["bound_to_failed_agent", "capable_omission", "legacy_replacement"])
def test_streaming_self_heal_replacement_agent_fails_closed(tmp_path, monkeypatch, lane, variant):
    first = RuntimeError("401 unauthorized") if lane == "exception" else _auth_failure()
    if variant == "bound_to_failed_agent":
        heal = _envelope(_rewritten(current=False), marked_idx=0, turn_id=TURN_ID, completed=True, final_response=OLD_ANSWER)
        profiles = [{"turn_id": TURN_ID, "current_turn_user_idx": 0, "contract": 2}, {"turn_id": "turn-heal", "current_turn_user_idx": 0, "contract": 2}]
    elif variant == "capable_omission":
        heal = _envelope(_rewritten(current=False), turn_id="turn-heal", completed=True, final_response=OLD_ANSWER)
        profiles = [{"turn_id": TURN_ID, "current_turn_user_idx": 0, "contract": 2}, {"turn_id": "turn-heal", "current_turn_user_idx": 0, "contract": 2}]
    else:  # the replacement Agent is a legacy callable; its rewritten result cannot be proven
        heal = {"messages": _rewritten(current=False), "turn_id": "turn-heal", "current_turn_user_idx": 0, "completed": True, "final_response": OLD_ANSWER}
        profiles = [{"turn_id": TURN_ID, "current_turn_user_idx": 0, "contract": 2}, {"turn_id": "turn-heal", "current_turn_user_idx": 0, "contract": None}]
    kinds, payload, session = _run(tmp_path, monkeypatch, heal, agent_results=[first, heal], enable_auth_retry=True, agent_profiles=profiles)
    assert "done" not in kinds
    assert _assistant_texts(payload["messages"]).count(OLD_ANSWER) == 1
    assert payload["messages"][-1].get("_error") or payload["messages"][-1]["role"] == "user"


@pytest.mark.parametrize("marker", ["_verification_stop_synthetic", "_pre_verify_synthetic"])
def test_streaming_tool_limit_settlement_with_conformant_envelope(tmp_path, monkeypatch, marker):
    old_turn = [{"role": "user", "content": PROMPT, "timestamp": 1.1}, {"role": "assistant", "content": OLD_ANSWER}]
    corrective = "Verification failed. I fixed the parser and reran the tests."
    rows = old_turn + [{"role": "user", "content": PROMPT},
                       {"role": "user", "content": "[System: verify the workspace]", marker: True},
                       {"role": "assistant", "content": corrective}]
    kinds, payload, session = _run(tmp_path, monkeypatch, _envelope(rows, marked_idx=2, turn_exit_reason="max_iterations_reached(30/30)"),
                                   prior_messages=old_turn, prior_context_messages=old_turn, current_turn_user_idx=len(old_turn))
    assert "done" in kinds
    assert [(m["role"], m.get("content")) for m in payload["messages"]] == [
        ("user", PROMPT), ("assistant", OLD_ANSWER), ("user", PROMPT), ("assistant", corrective),
    ]
