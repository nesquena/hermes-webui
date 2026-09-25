"""Current-turn ownership is proven by the producer contract or not claimed at all.

Contract v2 (declared by the Agent callable as ``TURN_BOUNDARY_CONTRACT``): every
result envelope carries ``turn_boundary_contract``, ``turn_id`` (this invocation's),
``messages_projection`` ("full": the exact final ``messages`` list) and a nullable
``current_turn_user_idx`` that addresses the row the Agent stamped with the same
``_turn_id`` marker at append time. The WebUI reads the capability from the callable
before each invocation (replacement Agents included), binds the envelope to the live
turn id, validates the index by marker — never by prompt text — and otherwise leaves
the turn capable-but-unproven: no historical row receives the live token, no prior
answer is persisted or classified as this turn's, no ``done``.

Legacy callables (no contract — every released hermes-agent today) keep master's
behaviour for ordinary prefix/append results. On a REWRITTEN result their index pair is
text-derived, so ownership needs an invocation-bound coordinate: the WebUI token, or
the user row stamped with this turn's ``pending_started_at`` (the Agent stamps its own
row with ``persist_user_timestamp``). A valid rewritten reply therefore still completes;
an identical historical prompt never binds, and the live prompt is never written at
index 0 from a stale pre-repair index.
"""

import copy
import json
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


TURN_STAMP = 1.9  # pending_started_at of the live turn in these tests


def _rewritten(current=True, answer=NEW_ANSWER, stamp=None):
    """Compaction dropped the tail; the identical historical prompt survives (unmarked).

    ``stamp``: the timestamp a released legacy Agent puts on the row it appends for
    this invocation (``persist_user_timestamp`` = the WebUI's ``pending_started_at``).
    """
    rows = [{"role": "user", "content": PROMPT}, {"role": "assistant", "content": OLD_ANSWER}]
    if current:
        rows.append({"role": "user", "content": PROMPT})
        if stamp is not None:
            rows[-1]["timestamp"] = stamp
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
    assert streaming._result_is_delta_projection(legacy, rows) is True  # legacy keeps master's role-derived rule


def test_legacy_callable_keeps_master_resolution_and_lookup():
    history = _history()
    appended = history + [{"role": "user", "content": PROMPT}, {"role": "assistant", "content": NEW_ANSWER}]
    identity = _resolve({"messages": appended}, _agent(idx=len(history), contract=None))
    assert identity["producer_capability"] == "legacy" and identity["agent_turn_boundary_source"] == "agent"
    assert streaming._find_active_turn_checkpoint_index(appended, history, identity, PROMPT) == len(history)
    # On a rewritten list the released Agent's text-derived pair is not proof; the row
    # stamped with this turn's timestamp is. No token is stamped at resolution.
    rewritten = _rewritten(stamp=TURN_STAMP)
    identity = _resolve({"messages": rewritten, "turn_id": TURN_ID, "current_turn_user_idx": 2}, _agent(idx=0, contract=None))
    assert identity["agent_turn_boundary_source"] == "result" and identity["current_turn_user_idx"] == 2
    assert streaming._find_active_turn_checkpoint_index(rewritten, history, identity, PROMPT) == 2
    assert streaming._assistant_reply_added_after_current_turn(rewritten, history, PROMPT, active_turn_identity=identity) is True
    assert _tokened(rewritten) == []
    unstamped = _rewritten()
    identity = _resolve({"messages": unstamped, "turn_id": TURN_ID, "current_turn_user_idx": 2}, _agent(idx=2, contract=None))
    assert streaming._find_active_turn_checkpoint_index(unstamped, history, identity, PROMPT) is None

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


def test_streaming_reviewer_blocker_shape_text_derived_pair_is_rejected_under_v2(tmp_path, monkeypatch):
    # A v2 callable whose envelope points the index at the identical historical prompt
    # (unmarked row, i.e. a text-derived coordinate) is malformed and never accepted.
    # Legacy callables keep master's behaviour for this shape (see the legacy tests).
    result = _envelope(_rewritten(current=False), completed=True, final_response=OLD_ANSWER)
    result["current_turn_user_idx"] = 0
    kinds, payload, session = _run(tmp_path, monkeypatch, result, agent_contract=2)
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


@pytest.mark.parametrize("export", ["released_pair", "instance_only"])
def test_streaming_legacy_agent_non_prefix_rewritten_reply_reaches_done_with_the_reply_persisted(tmp_path, monkeypatch, export):
    # Required regression (2026-09-24 review): a legacy Agent (no TURN_BOUNDARY_CONTRACT)
    # returns a rewritten, non-prefix result holding a valid reply. The turn must settle
    # as master does — ``done`` with the reply persisted — never an application error.
    result = {"messages": _rewritten(stamp=TURN_STAMP), "completed": True, "final_response": NEW_ANSWER}
    if export == "released_pair":  # current releases export a text-derived pair (#106312)
        result.update({"turn_id": TURN_ID, "current_turn_user_idx": 2})
    kinds, payload, session = _run(tmp_path, monkeypatch, result, current_turn_user_idx=2, agent_contract=None)
    assert "done" in kinds and "apperror" not in kinds
    assert payload["messages"][-1]["role"] == "assistant" and payload["messages"][-1]["content"] == NEW_ANSWER
    assert _assistant_texts(payload["messages"]).count(NEW_ANSWER) == 1
    assert not any(m.get("_error") for m in payload["messages"])
    assert any(m.get("role") == "assistant" and m.get("content") == NEW_ANSWER for m in session.context_messages)


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
@pytest.mark.parametrize("variant", ["bound_to_failed_agent", "capable_omission"])
def test_streaming_self_heal_replacement_agent_fails_closed(tmp_path, monkeypatch, lane, variant):
    first = RuntimeError("401 unauthorized") if lane == "exception" else _auth_failure()
    if variant == "bound_to_failed_agent":
        heal = _envelope(_rewritten(current=False), marked_idx=0, turn_id=TURN_ID, completed=True, final_response=OLD_ANSWER)
        profiles = [{"turn_id": TURN_ID, "current_turn_user_idx": 0, "contract": 2}, {"turn_id": "turn-heal", "current_turn_user_idx": 0, "contract": 2}]
    elif variant == "capable_omission":
        heal = _envelope(_rewritten(current=False), turn_id="turn-heal", completed=True, final_response=OLD_ANSWER)
        profiles = [{"turn_id": TURN_ID, "current_turn_user_idx": 0, "contract": 2}, {"turn_id": "turn-heal", "current_turn_user_idx": 0, "contract": 2}]
    else:
        raise AssertionError(variant)
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


@pytest.mark.parametrize("lane", _lanes())
def test_streaming_self_heal_legacy_replacement_agent_reply_reaches_done(tmp_path, monkeypatch, lane):
    # The replacement Agent built by the self-heal lane is a legacy callable: its
    # rewritten result settles with master's behaviour instead of erroring.
    first = RuntimeError("401 unauthorized") if lane == "exception" else _auth_failure()
    heal = {"messages": _rewritten(stamp=TURN_STAMP), "turn_id": "turn-heal", "current_turn_user_idx": 2,
            "completed": True, "final_response": NEW_ANSWER}
    kinds, payload, session = _run(
        tmp_path, monkeypatch, heal, agent_results=[first, heal], enable_auth_retry=True,
        agent_profiles=[{"turn_id": TURN_ID, "current_turn_user_idx": 0, "contract": 2},
                        {"turn_id": "turn-heal", "current_turn_user_idx": 2, "contract": None}],
    )
    assert "done" in kinds and "apperror" not in kinds
    assert payload["messages"][-1]["content"] == NEW_ANSWER
    assert _assistant_texts(payload["messages"]).count(NEW_ANSWER) == 1


# ── legacy regressions required by the 2026-09-24 08:17 review ─────────────


@pytest.mark.parametrize("historical_stamp", [None, 1.0])
def test_streaming_legacy_text_derived_pair_on_identical_historical_prompt_is_not_accepted(tmp_path, monkeypatch, historical_stamp):
    # Contract-less Agent; the rewrite kept only the identical historical prompt and its
    # old answer, and the exported (text-derived) pair points at that historical row.
    rows = _rewritten(current=False)
    if historical_stamp is not None:
        rows[0]["timestamp"] = historical_stamp  # its own, older timestamp
    result = {"messages": rows, "turn_id": TURN_ID, "current_turn_user_idx": 0,
              "completed": True, "final_response": OLD_ANSWER}
    kinds, payload, session = _run(tmp_path, monkeypatch, result, current_turn_user_idx=0, agent_contract=None)
    _assert_history_intact_and_unclaimed(kinds, payload, session)
    assert "apperror" in kinds


def _compaction_history():
    return [
        {"role": "assistant", "content": "**Context snapshot** (compaction)"},
        {"role": "user", "content": "Here is a summary of the conversation so far", "timestamp": 1.0},
        {"role": "user", "content": "first protected user message", "timestamp": 1.1},
        {"role": "assistant", "content": "", "tool_calls": [{"id": "c1", "type": "function", "function": {"name": "t", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "c1", "content": "tool output"},
    ]


def _repair_merged(history, current_row):
    # The Agent's alternation repair merged the two adjacent user rows in place: the
    # list shrank by one, so the pre-repair index len(history) is now stale.
    merged = {"role": "user", "content": history[1]["content"] + "\n\n" + history[2]["content"], "timestamp": 1.0}
    return [history[0], merged, history[3], history[4], current_row, {"role": "assistant", "content": NEW_ANSWER}]


@pytest.mark.parametrize("stamped", [True, False], ids=["released_agent_stamps_row", "agent_without_stamp"])
def test_streaming_legacy_repair_merge_with_stale_pre_repair_index_never_writes_prompt_at_front(tmp_path, monkeypatch, stamped):
    history = _compaction_history()
    current = {"role": "user", "content": PROMPT}
    if stamped:
        current["timestamp"] = TURN_STAMP
    result = {"messages": _repair_merged(history, current), "completed": True, "final_response": NEW_ANSWER}
    kinds, payload, session = _run(
        tmp_path, monkeypatch, result, prior_messages=history, prior_context_messages=history,
        current_turn_user_idx=len(history),  # stale: recorded before the in-place merge
        agent_contract=None,
    )
    def prompt_rows(rows):
        return [i for i, m in enumerate(rows) if m.get("role") == "user" and m.get("content") == PROMPT]

    for rows in (session.context_messages, payload["messages"]):
        assert prompt_rows(rows) and all(i > 0 for i in prompt_rows(rows)), "live prompt written at index 0"
    assert len(prompt_rows(payload["messages"])) == 1
    if stamped:
        assert "done" in kinds and "apperror" not in kinds
        assert payload["messages"][-1]["content"] == NEW_ANSWER
        assert len(prompt_rows(session.context_messages)) == 1
    else:
        # Unprovable rewrite from an Agent that does not stamp its row: fail closed.
        # The Agent's rows are kept verbatim and the pending prompt goes to the tail.
        assert "done" not in kinds and "apperror" in kinds
        assert payload["messages"][-1].get("_error") is True
        assert session.context_messages[-1].get("content") == PROMPT
        assert session.context_messages[-1].get("_active_turn_token")


# ── adjacent-user merge after an unanswered prompt (2026-09-24 10:02 review) ─


UNANSWERED = "Is the backup job still running?"


def _unanswered_history():
    return [
        {"role": "user", "content": "first question", "timestamp": 1.0},
        {"role": "assistant", "content": "first answer"},
        {"role": "user", "content": UNANSWERED, "timestamp": 1.5},  # stopped / failed send
    ]


def _agent_merging_into_unanswered_tail(mode):
    """A legacy Agent: its alternation repair merges this invocation's user message into
    the unanswered trailing history row IN PLACE (the older row's dict and timestamp
    survive), exactly as ``_merge_consecutive_users`` does.

    ``finalizer_restamped``: a completed turn on released Agents, whose finalizer then
    applies the persist override to the re-anchored row (clean text + this turn's
    ``persist_user_timestamp``). ``merged_unstamped``: an exit that skips the finalizer,
    or an Agent without the re-anchor — the merged row keeps the older timestamp.
    ``foreign_merge``: the tail was merged with a message that is NOT this invocation's.
    """
    def run(**kwargs):
        history = [dict(m) for m in kwargs["conversation_history"]]
        tail = history[-1]
        if mode == "foreign_merge":
            merged = dict(tail, content=tail["content"] + "\n\n" + "a different message")
        else:
            merged = dict(tail, content=tail["content"] + "\n\n" + kwargs["user_message"])
            if mode == "finalizer_restamped":
                merged["content"] = kwargs.get("persist_user_message") or kwargs["user_message"]
                merged["timestamp"] = kwargs.get("persist_user_timestamp")
        messages = history[:-1] + [merged, {"role": "assistant", "content": NEW_ANSWER}]
        return {"messages": messages, "completed": True, "final_response": NEW_ANSWER,
                "turn_id": TURN_ID, "current_turn_user_idx": len(history) - 1}
    return run


@pytest.mark.parametrize("mode", ["merged_unstamped", "finalizer_restamped"])
def test_streaming_legacy_merge_into_unanswered_prompt_reaches_done_once(tmp_path, monkeypatch, mode):
    history = _unanswered_history()
    kinds, payload, session = _run(
        tmp_path, monkeypatch, _agent_merging_into_unanswered_tail(mode),
        prior_messages=history, prior_context_messages=history,
        current_turn_user_idx=len(history),  # recorded before the in-place merge
        agent_contract=None,
    )
    assert "done" in kinds and "apperror" not in kinds
    assert payload["messages"][-1]["role"] == "assistant" and payload["messages"][-1]["content"] == NEW_ANSWER
    for rows in (payload["messages"], session.context_messages):
        assert _assistant_texts(rows).count(NEW_ANSWER) == 1
        assert not any(m.get("_error") for m in rows)
        # prior unanswered prompt kept as its own row, then a clean current row, then
        # the answer: no merged blob, the new prompt exactly once, never at index 0
        assert [(m["role"], m.get("content")) for m in rows[-3:]] == [
            ("user", UNANSWERED), ("user", PROMPT), ("assistant", NEW_ANSWER),
        ]
        assert [m.get("content") for m in rows].count(PROMPT) == 1
        assert not any("\n\n" in str(m.get("content")) and UNANSWERED in str(m.get("content")) for m in rows)


def test_streaming_legacy_unanswered_tail_merged_with_a_foreign_message_fails_closed(tmp_path, monkeypatch):
    # Rewritten tail that is not this invocation's merge: nothing proves this turn.
    history = _unanswered_history()
    kinds, payload, session = _run(
        tmp_path, monkeypatch, _agent_merging_into_unanswered_tail("foreign_merge"),
        prior_messages=history, prior_context_messages=history,
        current_turn_user_idx=len(history), agent_contract=None,
    )
    assert "done" not in kinds and "apperror" in kinds
    assert _assistant_texts(payload["messages"]).count(NEW_ANSWER) == 0


def test_merge_signature_binds_the_merged_row_by_content_and_position():
    sent_history = _unanswered_history()
    sent = {"conversation_history": sent_history, "user_message": "[Workspace::v1: /w]\n" + PROMPT}
    signature = streaming._adjacent_user_merge_signature(sent)
    join = UNANSWERED + "\n\n[Workspace::v1: /w]\n" + PROMPT
    assert signature["content"] == join and len(signature["prefix"]) == len(sent_history) - 1
    identity = {"token": "tok", "text": PROMPT, "timestamp": TURN_STAMP, "user_merge_signature": signature}
    merged = [dict(m) for m in sent_history[:-1]] + [{"role": "user", "content": join}, {"role": "assistant", "content": NEW_ANSWER}]
    assert streaming._adjacent_user_merge_row_index(merged, identity) == 2
    # a later user row means the join is not the current row
    assert streaming._adjacent_user_merge_row_index(merged + [{"role": "user", "content": PROMPT}], identity) is None
    # a rewritten prefix (compaction dropped/changed earlier rows) is not proof either
    assert streaming._adjacent_user_merge_row_index(merged[1:], identity) is None
    # no merge possible: history does not end with a user row, or either side empty
    assert streaming._adjacent_user_merge_signature({"conversation_history": _history(), "user_message": PROMPT}) is None
    assert streaming._adjacent_user_merge_signature({"conversation_history": _unanswered_history(), "user_message": ""}) is None


def test_streaming_historical_row_equal_to_the_merge_text_is_never_bound(tmp_path, monkeypatch):
    # Greptile P1 (2026-09-24): the same unanswered prompt + submission happened before,
    # so history holds a row whose text equals this turn's merge. A legacy rewrite drops
    # the live row and keeps that historical row as the last user row: not the current turn.
    earlier_merge = UNANSWERED + "\n\n" + PROMPT
    history = [
        {"role": "user", "content": earlier_merge, "timestamp": 0.5},
        {"role": "assistant", "content": OLD_ANSWER},
        {"role": "user", "content": UNANSWERED, "timestamp": 1.5},
    ]

    def agent(**kwargs):
        # the sent tail was merged with exactly this invocation's message, then the rewrite
        # (e.g. compaction) dropped it and kept only the earlier rows
        rows = [dict(m) for m in kwargs["conversation_history"][:-1]]
        return {"messages": rows, "completed": True, "final_response": OLD_ANSWER,
                "turn_id": TURN_ID, "current_turn_user_idx": 0}

    def exact_join_agent(**kwargs):
        rows = [dict(m) for m in kwargs["conversation_history"][:-1]]
        rows[0]["content"] = kwargs["conversation_history"][-1]["content"] + "\n\n" + kwargs["user_message"]
        return {"messages": rows, "completed": True, "final_response": OLD_ANSWER,
                "turn_id": TURN_ID, "current_turn_user_idx": 0}

    for fake in (agent, exact_join_agent):
        kinds, payload, session = _run(tmp_path, monkeypatch, fake, prior_messages=[dict(m) for m in history],
                                       prior_context_messages=[dict(m) for m in history],
                                       current_turn_user_idx=len(history), agent_contract=None)
        assert "done" not in kinds and "apperror" in kinds
        assert _assistant_texts(payload["messages"]).count(OLD_ANSWER) == 1
        tmp_path = tmp_path / "again"; tmp_path.mkdir()


# ── exact continuity for legacy prefix decisions (2026-09-24 re-gate at 2e02b52ff) ─


LONG = "x" * 500


def test_exact_message_key_is_untruncated_where_the_display_identity_is_not():
    old = {"role": "user", "content": LONG + "OLD"}
    new = {"role": "user", "content": LONG + "NEW"}
    assert streaming._message_identity(old) == streaming._message_identity(new)  # display/dedupe key
    assert streaming._exact_message_key(old) != streaming._exact_message_key(new)
    assert streaming._messages_have_prefix([new], [old]) is True
    assert streaming._messages_have_exact_prefix([new], [old]) is False
    assert streaming._messages_have_exact_prefix([dict(old), new], [old]) is True


def test_streaming_legacy_row_differing_after_char_500_never_takes_the_live_turn(tmp_path, monkeypatch):
    # Equal-length prior/result lists; the rewrite changed the historical user row only
    # after character 500, so it now carries the repeated prompt text; the current row is
    # absent and the legacy index is the stale 0. Truncated identity calls this an intact
    # prefix; exact continuity does not.
    prompt = LONG + "NEW"
    history = [{"role": "user", "content": LONG + "OLD", "timestamp": 1.0},
               {"role": "assistant", "content": OLD_ANSWER}]
    result = {"messages": [{"role": "user", "content": prompt, "timestamp": 1.0},
                           {"role": "assistant", "content": OLD_ANSWER}],
              "turn_id": TURN_ID, "current_turn_user_idx": 0,
              "completed": True, "final_response": OLD_ANSWER}
    bound = []
    original_lookup = streaming._find_active_turn_checkpoint_index

    def recording_lookup(result_messages, previous_context, identity, msg_text):
        idx = original_lookup(result_messages, previous_context, identity, msg_text)
        bound.append(idx)
        return idx

    monkeypatch.setattr(streaming, "_find_active_turn_checkpoint_index", recording_lookup)
    kinds, payload, session = _run(tmp_path, monkeypatch, result, msg_text=prompt,
                                   prior_messages=[dict(m) for m in history],
                                   prior_context_messages=[dict(m) for m in history],
                                   current_turn_user_idx=0, agent_contract=None)
    assert "done" not in kinds
    assert 0 not in bound, f"a consumer bound the historical row as the current turn: {bound}"
    assert not result["messages"][0].get("_active_turn_token"), "live token stamped on the Agent's historical row"
    for rows in (session.context_messages, payload["messages"]):
        assert not rows[0].get("_active_turn_token"), "historical row received the live token"
        assert _assistant_texts(rows).count(OLD_ANSWER) == 1
        last_old = max(i for i, m in enumerate(rows) if m.get("content") == OLD_ANSWER)
        pending = [i for i, m in enumerate(rows) if m.get("role") == "user" and m.get("content") == prompt]
        assert pending and pending[-1] > last_old, "pending prompt must be materialized after history"


# ── Agents that cannot stamp their row (2026-09-24 12:52 review) ─────────────


def test_run_kwargs_carry_turn_timestamp_only_when_actually_sent():
    assert streaming._run_kwargs_carry_turn_timestamp({"persist_user_timestamp": 1.9}) is True
    assert streaming._run_kwargs_carry_turn_timestamp({}) is False
    assert streaming._run_kwargs_carry_turn_timestamp({"persist_user_timestamp": None}) is False
    assert streaming._run_kwargs_carry_turn_timestamp({"persist_user_timestamp": True}) is False


def test_streaming_strict_legacy_callable_rewrite_fails_closed_with_an_update_error(tmp_path, monkeypatch):
    # A pre-#6935 hermes-agent: run_conversation() does not accept persist_user_timestamp,
    # so the shim omits it and the Agent can never stamp this turn's row. After a history
    # rewrite nothing proves which row is this turn's: fail closed (2026-09-25 review), and
    # say why instead of "No response from provider".
    seen = {}

    def older_agent(**kwargs):
        seen.update(kwargs)
        rows = _rewritten()
        rows[2]["timestamp"] = 12345.0  # its own wall clock, never pending_started_at
        return {"messages": rows, "completed": True, "final_response": NEW_ANSWER}

    events, payload = _run_streaming_with_fake_agent(
        tmp_path, monkeypatch, older_agent, prior_messages=_history(), prior_context_messages=_history(),
        msg_text=PROMPT, pending_started_at=TURN_STAMP, current_turn_user_idx=2,
        agent_contract=None, agent_run_signature="strict",
    )
    kinds = [event for event, _payload in events]
    assert "persist_user_timestamp" not in seen  # the shim omitted it
    assert "done" not in kinds and "apperror" in kinds
    apperror = [p for event, p in events if event == "apperror"][-1]
    assert apperror.get("type") == "agent_update_required"
    assert "too old to prove turn ownership" in str(apperror.get("message"))
    assert "No response from provider" not in json.dumps(apperror)
    assert _assistant_texts(payload["messages"]).count(OLD_ANSWER) == 1
    assert payload["messages"][-1].get("_error") is True


@pytest.mark.parametrize("lane", _lanes())
def test_streaming_self_heal_strict_legacy_callable_rewrite_fails_closed(tmp_path, monkeypatch, lane):
    first = RuntimeError("401 unauthorized") if lane == "exception" else _auth_failure()
    seen = []

    def healed(**kwargs):
        seen.append("persist_user_timestamp" in kwargs)
        rows = _rewritten()
        rows[2]["timestamp"] = 12345.0
        return {"messages": rows, "completed": True, "final_response": NEW_ANSWER}

    kinds, payload, session = _run(tmp_path, monkeypatch, healed, agent_results=[first, healed],
                                   enable_auth_retry=True, current_turn_user_idx=2,
                                   agent_contract=None, agent_run_signature="strict")
    assert seen == [False]  # the replacement Agent never received the timestamp either
    assert "done" not in kinds and "apperror" in kinds
    assert _assistant_texts(payload["messages"]).count(OLD_ANSWER) == 1


def test_streaming_kwargs_wrapper_is_treated_as_accepting_the_timestamp(tmp_path, monkeypatch):
    # A **kwargs wrapper receives persist_user_timestamp (the shim treats **kwargs as
    # accepting it); the wrapped Agent stamps its row with it, so the rewritten reply is
    # proven by the timestamp and completes.
    seen = {}

    def wrapper(**kwargs):
        seen.update(kwargs)
        return {"messages": _rewritten(stamp=kwargs.get("persist_user_timestamp")),
                "completed": True, "final_response": NEW_ANSWER}

    kinds, payload, session = _run(tmp_path, monkeypatch, wrapper, current_turn_user_idx=2, agent_contract=None)
    assert seen.get("persist_user_timestamp") == TURN_STAMP
    assert "done" in kinds and "apperror" not in kinds
    for rows in (payload["messages"], session.context_messages):
        assert _assistant_texts(rows).count(NEW_ANSWER) == 1


# ── exact continuity in every turn-sensitive consumer (2026-09-25 review) ────


def _divergent_run(tmp_path, monkeypatch, shape, **kw):
    """Display history [LONG+OLD, OLD_ANSWER], shorter owner context [LONG+OLD], and an
    equal-length rewritten result [LONG+NEW, OLD_ANSWER] whose user row differs from the
    prior one only after character 500 and carries the repeated prompt text."""
    prompt = LONG + "NEW"
    display = [{"role": "user", "content": LONG + "OLD", "timestamp": 1.0},
               {"role": "assistant", "content": OLD_ANSWER}]
    owner = [{"role": "user", "content": LONG + "OLD", "timestamp": 1.0}]
    rows = [{"role": "user", "content": prompt}, {"role": "assistant", "content": OLD_ANSWER}]
    result = {"messages": rows, "turn_id": TURN_ID, "current_turn_user_idx": 0}
    if shape == "normal":
        result.update({"completed": True, "final_response": OLD_ANSWER})
    elif shape == "partial_error":
        result.update({"completed": False, "partial": True, "failed": False, "error": "provider hiccup",
                       "final_response": OLD_ANSWER})
    elif shape == "tool_limit":
        result.update({"turn_exit_reason": "max_iterations_reached(30/30)", "final_response": OLD_ANSWER})
    else:
        raise AssertionError(shape)
    return prompt, _run(tmp_path, monkeypatch, result, msg_text=prompt, prior_messages=display,
                        prior_context_messages=owner, current_turn_user_idx=0, agent_contract=None, **kw)


def _assert_no_false_done(kinds, payload, session, prompt):
    assert "done" not in kinds and "apperror" in kinds
    display = payload["messages"]
    assert _assistant_texts(display).count(OLD_ANSWER) == 1, "historical answer duplicated or claimed"
    last_old = max(i for i, m in enumerate(display) if m.get("content") == OLD_ANSWER)
    pending = [i for i, m in enumerate(display) if m.get("role") == "user" and m.get("content") == prompt]
    assert pending and pending[-1] > last_old, "pending prompt must follow the history"
    assert display[-1].get("_error") is True
    assert not any(m.get("_active_turn_token") and m.get("content") == LONG + "OLD" for m in session.context_messages)
    # Durable model context: never "historical user -> live pending prompt -> historical
    # answer". The pending prompt follows every historical row, and the old answer
    # appears once and is never after the live prompt.
    context = session.context_messages
    assert [m.get("content") for m in context].count(OLD_ANSWER) <= 1
    tokened = [i for i, m in enumerate(context) if m.get("_active_turn_token")]
    old_rows = [i for i, m in enumerate(context) if m.get("content") == OLD_ANSWER]
    assert all(t > o for t in tokened for o in old_rows), [
        (m.get("role"), str(m.get("content"))[-6:], bool(m.get("_active_turn_token"))) for m in context
    ]
    # the pending prompt is materialized at the tail, after every historical row
    assert context and context[-1].get("role") == "user" and context[-1].get("content") == prompt


@pytest.mark.parametrize("shape", ["normal", "partial_error", "tool_limit"])
def test_streaming_divergent_context_char_500_rewrite_never_completes_with_history(tmp_path, monkeypatch, shape):
    prompt, (kinds, payload, session) = _divergent_run(tmp_path, monkeypatch, shape)
    _assert_no_false_done(kinds, payload, session, prompt)


@pytest.mark.parametrize("lane", _lanes())
def test_streaming_divergent_context_char_500_rewrite_fails_closed_on_self_heal(tmp_path, monkeypatch, lane):
    first = RuntimeError("401 unauthorized") if lane == "exception" else _auth_failure()
    prompt = LONG + "NEW"
    heal = {"messages": [{"role": "user", "content": prompt}, {"role": "assistant", "content": OLD_ANSWER}],
            "turn_id": TURN_ID, "current_turn_user_idx": 0, "completed": True, "final_response": OLD_ANSWER}
    display = [{"role": "user", "content": LONG + "OLD", "timestamp": 1.0}, {"role": "assistant", "content": OLD_ANSWER}]
    owner = [{"role": "user", "content": LONG + "OLD", "timestamp": 1.0}]
    kinds, payload, session = _run(tmp_path, monkeypatch, heal, agent_results=[first, heal], enable_auth_retry=True,
                                   msg_text=prompt, prior_messages=display, prior_context_messages=owner,
                                   current_turn_user_idx=0, agent_contract=None)
    _assert_no_false_done(kinds, payload, session, prompt)


def test_eager_checkpoint_drop_compares_untruncated():
    prompt = LONG + "NEW"
    historical = [{"role": "user", "content": LONG + "OLD"}]
    assert streaming._drop_checkpointed_current_user_from_context(historical, prompt) == historical
    checkpoint = [{"role": "assistant", "content": "a"}, {"role": "user", "content": "[Workspace::v1: /w]\n" + prompt}]
    assert streaming._drop_checkpointed_current_user_from_context(checkpoint, prompt) == checkpoint[:1]


@pytest.mark.parametrize("failure, expected_type", [
    ({"error": "Error code: 429 - rate limit exceeded"}, "rate_limit"),
    ({"error": "Error code: 404 - model 'x' not found"}, "model_not_found"),
    ({"error": "compression_snapshot_stale", "compression_snapshot_stale": True, "partial": True}, "compression_snapshot_stale"),
    ({"failed": True, "compression_exhausted": True, "error": "context_compression_timeout"}, None),
])
def test_update_required_never_masks_a_real_provider_error(tmp_path, monkeypatch, failure, expected_type):
    # Greptile (2026-09-25): the update-required message replaces only the silent
    # no-response fallback; a real failure keeps its own classification.
    def older_agent(**kwargs):
        rows = _rewritten()
        rows[2]["timestamp"] = 12345.0
        return {"messages": rows, "completed": False, "final_response": NEW_ANSWER, **failure}

    events, payload = _run_streaming_with_fake_agent(
        tmp_path, monkeypatch, older_agent, prior_messages=_history(), prior_context_messages=_history(),
        msg_text=PROMPT, pending_started_at=TURN_STAMP, current_turn_user_idx=2,
        agent_contract=None, agent_run_signature="strict",
    )
    apperrors = [p for event, p in events if event == "apperror"]
    assert apperrors
    assert apperrors[-1].get("type") != "agent_update_required"
    if expected_type is not None:
        assert apperrors[-1].get("type") == expected_type


@pytest.mark.parametrize("shape", [{"completed": True}, {"completed": False, "partial": True}])
def test_update_required_covers_completed_and_bare_partial_results(tmp_path, monkeypatch, shape):
    # Greptile (2026-09-25): a partial result with no error of its own (e.g. stopped on
    # invalid tool calls) must get the update guidance, not "No response from provider".
    def older_agent(**kwargs):
        rows = _rewritten()
        rows[2]["timestamp"] = 12345.0
        return {"messages": rows, "final_response": NEW_ANSWER, **shape}

    events, payload = _run_streaming_with_fake_agent(
        tmp_path, monkeypatch, older_agent, prior_messages=_history(), prior_context_messages=_history(),
        msg_text=PROMPT, pending_started_at=TURN_STAMP, current_turn_user_idx=2,
        agent_contract=None, agent_run_signature="strict",
    )
    apperrors = [p for event, p in events if event == "apperror"]
    assert apperrors and apperrors[-1].get("type") == "agent_update_required"
    assert not any(event == "done" for event, _p in events)


# ── 2026-09-25 re-gate at f7e2b89bc: replay/context ops, merge prefix, scoping ─


def test_streaming_adjacent_merge_with_prefix_collision_after_char_500_is_not_bound(tmp_path, monkeypatch):
    # The sent prefix's first user row is rewritten after character 500 while the tail
    # is merged with exactly this invocation's message: the merge proof requires the
    # prefix to be unchanged, exactly, so it must not authenticate the joined row.
    history = [
        {"role": "user", "content": LONG + "A", "timestamp": 1.0},
        {"role": "assistant", "content": OLD_ANSWER},
        {"role": "user", "content": UNANSWERED, "timestamp": 1.5},
    ]

    def agent(**kwargs):
        sent = [dict(m) for m in kwargs["conversation_history"]]
        rewritten_head = dict(sent[0], content=LONG + "B")  # same length, differs after char 500
        joined = dict(sent[-1], content=sent[-1]["content"] + "\n\n" + kwargs["user_message"])
        return {"messages": [rewritten_head, sent[1], joined, {"role": "assistant", "content": NEW_ANSWER}],
                "completed": True, "final_response": NEW_ANSWER}

    kinds, payload, session = _run(tmp_path, monkeypatch, agent, prior_messages=[dict(m) for m in history],
                                   prior_context_messages=[dict(m) for m in history],
                                   current_turn_user_idx=len(history), agent_contract=None)
    assert "done" not in kinds
    assert _assistant_texts(payload["messages"]).count(NEW_ANSWER) == 0
    signature = streaming._adjacent_user_merge_signature(
        {"conversation_history": [{k: v for k, v in m.items() if k != "timestamp"} for m in history], "user_message": PROMPT})
    rows = [dict(history[0], content=LONG + "B"), history[1], {"role": "user", "content": signature["content"]}]
    assert streaming._adjacent_user_merge_row_index(rows, {"user_merge_signature": signature}) is None


def test_streaming_historical_only_rewrite_is_not_reported_as_update_required(tmp_path, monkeypatch):
    # A pre-#6935 Agent that produced no new output: its rewritten envelope only
    # preserves historical prose. That is not a reply to attribute, so the error is the
    # ordinary no-response one, not "update required".
    def older_agent(**kwargs):
        rows = [{"role": "user", "content": PROMPT + " (compacted)"}, {"role": "assistant", "content": OLD_ANSWER}]
        return {"messages": rows, "completed": True, "final_response": ""}

    events, payload = _run_streaming_with_fake_agent(
        tmp_path, monkeypatch, older_agent, prior_messages=_history(), prior_context_messages=_history(),
        msg_text=PROMPT, pending_started_at=TURN_STAMP, current_turn_user_idx=2,
        agent_contract=None, agent_run_signature="strict",
    )
    apperrors = [p for event, p in events if event == "apperror"]
    assert apperrors and apperrors[-1].get("type") != "agent_update_required"
    assert not any(event == "done" for event, _p in events)


@pytest.mark.parametrize("lane", _lanes())
def test_streaming_self_heal_lanes_report_update_required(tmp_path, monkeypatch, lane):
    first = RuntimeError("401 unauthorized") if lane == "exception" else _auth_failure()

    def healed(**kwargs):
        rows = _rewritten()
        rows[2]["timestamp"] = 12345.0  # the replacement Agent cannot stamp this turn's row
        return {"messages": rows, "completed": True, "final_response": NEW_ANSWER}

    events, payload = _run_streaming_with_fake_agent(
        tmp_path, monkeypatch, healed, agent_results=[first, healed], enable_auth_retry=True,
        prior_messages=_history(), prior_context_messages=_history(), msg_text=PROMPT,
        pending_started_at=TURN_STAMP, current_turn_user_idx=2, agent_contract=None, agent_run_signature="strict",
    )
    apperrors = [p for event, p in events if event == "apperror"]
    assert apperrors and apperrors[-1].get("type") == "agent_update_required", apperrors[-1] if apperrors else None
    assert "too old to prove turn ownership" in str(apperrors[-1].get("message"))
    assert not any(event == "done" for event, _p in events)


@pytest.mark.parametrize("lane", _lanes())
@pytest.mark.parametrize("retry_failure", [
    {"error": "Error code: 429 - rate limit exceeded"},
    {"failed": True, "compression_exhausted": True, "error": "context_compression_timeout"},
])
def test_self_heal_update_required_never_masks_the_retrys_own_error(tmp_path, monkeypatch, lane, retry_failure):
    # Greptile (2026-09-25): the retry rewrote history and kept new assistant text but
    # stopped on its own error; the self-heal lanes must not report update-required.
    first = RuntimeError("401 unauthorized") if lane == "exception" else _auth_failure()

    def healed(**kwargs):
        rows = _rewritten()
        rows[2]["timestamp"] = 12345.0
        return {"messages": rows, "completed": False, "final_response": NEW_ANSWER, **retry_failure}

    events, payload = _run_streaming_with_fake_agent(
        tmp_path, monkeypatch, healed, agent_results=[first, healed], enable_auth_retry=True,
        prior_messages=_history(), prior_context_messages=_history(), msg_text=PROMPT,
        pending_started_at=TURN_STAMP, current_turn_user_idx=2, agent_contract=None, agent_run_signature="strict",
    )
    apperrors = [p for event, p in events if event == "apperror"]
    assert apperrors and apperrors[-1].get("type") != "agent_update_required"
    assert not any(event == "done" for event, _p in events)
