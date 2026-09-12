"""Regression: after a compaction, the agent's alternation repair merges two adjacent
user rows at the FRONT of the history in place. The Agent-reported
``current_turn_user_idx`` (recorded before the merge) then no longer addresses the
current user row, ``result_messages`` no longer carries ``previous_context`` as a
prefix, and ``_settle_current_turn_boundary`` fell back to
``insert_at = current_turn_user_idx - len(previous_context) == 0`` — writing the
new user turn to the front of the context. Every later turn then merged it into
the first user message (one message per turn, newest first) and the prompt's
second message changed on every turn (0% prefix-cache hit at 200K+ tokens).
"""
from api import streaming as st


def _prev_context():
    return [
        {'role': 'assistant', 'content': '**Context snapshot** (compaction)', 'timestamp': 1788641031.5},
        {'role': 'user', 'content': 'Here is a summary of the conversation so far …'},   # compaction summary, role=user
        {'role': 'user', 'content': 'first protected user message'},                     # adjacent user row
        {'role': 'assistant', 'content': 'ok', 'tool_calls': [{'id': 'c1', 'type': 'function', 'function': {'name': 't', 'arguments': '{}'}}]},
        {'role': 'tool', 'tool_call_id': 'c1', 'content': 'tool output'},
    ]


def _agent_result_after_repair(prev, new_text):
    # hermes-agent: history + new user, then repair_message_sequence merges the two
    # adjacent user rows in place (list shrinks by one), then the turn produces rows.
    merged = {'role': 'user', 'content': prev[1]['content'] + '\n\n' + prev[2]['content']}
    return [prev[0], merged, prev[3], prev[4],
            {'role': 'user', 'content': new_text},
            {'role': 'assistant', 'content': 'answer'}]


def _identity(prev, new_text):
    return {
        'token': 'tok-1', 'turn_id': 'turn-1', 'text': new_text, 'timestamp': 1788800000.0,
        'agent_turn_boundary_resolved': True,
        # recorded by the agent BEFORE the in-place merge: len(history) at that time
        'current_turn_user_idx': len(prev),
    }


def test_new_user_turn_is_never_settled_at_the_front():
    prev = _prev_context()
    new_text = 'NEW TURN question'
    result = _agent_result_after_repair(prev, new_text)
    settled = st._settle_current_turn_boundary(prev, result, _identity(prev, new_text), new_text, 'webui')
    users = [i for i, m in enumerate(settled) if m.get('role') == 'user' and m.get('content') == new_text]
    assert users, 'the current user turn must be present'
    assert len(users) == 1, f'current user turn duplicated at {users}'
    assert users[0] > 0, f'current user turn settled at index {users[0]} (front) — must follow the prior history'
    assert settled[users[0] - 1].get('role') in ('assistant', 'tool'), 'must be preceded by the prior history tail'
    assert settled[-1].get('role') == 'assistant', 'turn output must stay after the user turn'


def test_first_history_message_is_stable_across_turns():
    """Turn N+1 must see the same leading rows as turn N (prefix-cache safety)."""
    prev = _prev_context()
    ctx = list(prev)
    heads = []
    for n in range(3):
        new_text = f'question {n}'
        result = _agent_result_after_repair(ctx, new_text) if n == 0 else (
            list(ctx) + [{'role': 'user', 'content': new_text}, {'role': 'assistant', 'content': 'answer'}]
        )
        identity = _identity(ctx, new_text)
        ctx = st._settle_current_turn_boundary(ctx, result, identity, new_text, 'webui')
        heads.append(ctx[0].get('content'))
        assert ctx[0].get('role') != 'user' or ctx[0].get('content') != new_text, 'new turn at front'
    assert len(set(heads)) == 1, f'leading row changed across turns: {heads}'


def test_delta_only_result_keeps_front_insertion():
    """When the Agent returns only this turn's rows (no user row, no history), the
    current user turn is still inserted before those rows (legacy delta shape)."""
    prev = _prev_context()
    new_text = 'delta question'
    result = [{'role': 'assistant', 'content': 'answer'}, {'role': 'tool', 'tool_call_id': 'c9', 'content': 'x'}]
    identity = _identity(prev, new_text)
    identity['current_turn_user_idx'] = 99  # unresolvable
    identity['messages_projection'] = 'delta'  # declared by a bound contract-v2 envelope
    settled = st._settle_current_turn_boundary(prev, result, identity, new_text, 'webui')
    assert settled[0].get('role') == 'user' and settled[0].get('content') == new_text
    assert [m.get('role') for m in settled[1:]] == ['assistant', 'tool']


def test_delta_only_result_with_in_range_index_still_inserts_at_front():
    """A delta-only result long enough for the recorded full-history index to be
    numerically in range must not have the user turn placed after its own output:
    the index addresses the full Agent history, not the delta."""
    prev = _prev_context()  # five prior messages
    new_text = 'delta question'
    result = [{'role': 'assistant', 'content': f'step {i}', 'tool_calls': [{'id': f'c{i}'}]} if i % 2 == 0
              else {'role': 'tool', 'tool_call_id': f'c{i - 1}', 'content': 'x'} for i in range(8)]
    identity = _identity(prev, new_text)
    identity['current_turn_user_idx'] = len(prev)  # 5: in range of the 8-row delta
    identity['messages_projection'] = 'delta'  # declared by a bound contract-v2 envelope
    settled = st._settle_current_turn_boundary(prev, result, identity, new_text, 'webui')
    assert settled[0].get('role') == 'user' and settled[0].get('content') == new_text
    assert settled[1:] == result
    assert sum(1 for m in settled if m.get('role') == 'user') == 1
