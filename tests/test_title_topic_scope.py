"""Title scope: a session is named by its opening exchange, never its last turn.

Regression shape this guards: after the opening title attempt failed (or its
output was rejected), the automatic path re-titled the session from the LATEST
exchange on the next turn, so long sessions ended up named after whatever the
last request happened to be ("search for similar issues", "answer in Korean").
Forked and compaction-continuation sessions hit that on their very first
automatic attempt, because they start with the copied history and the exchange
count was already above one.

Contract after the fix:
  * ``_title_exchange_for_unresolved_title`` always returns the opening
    user+assistant pair, scanning past consecutive opening user rows.
  * Rows the WebUI injects with ``role='user'`` never become the title source
    and never appear as context — recognised by the structured ``_source`` /
    ``display_kind`` stamps first, with the rendered text prefixes as fallback.
  * Recent real user requests are given to the model as labelled CONTEXT
    (``_title_context_samples``) so a warm-up opener still gets anchored by what
    the work became, without letting the last request become the title.
"""
from unittest.mock import MagicMock

import api.profiles as profiles_api
import api.streaming as streaming


# --- exchange selection -----------------------------------------------------


def test_selector_uses_opening_exchange_for_fork_shaped_transcripts():
    # A forked session copies the parent's history: several exchanges exist
    # before the user types anything new. The opener must still win.
    messages = [
        {'role': 'user', 'content': '카카오 AI top 100 대회 준비를 도와줘'},
        {'role': 'assistant', 'content': '준비 절차를 정리했습니다.'},
        {'role': 'user', 'content': '계속 진행해줘'},
        {'role': 'assistant', 'content': '이어서 진행합니다.'},
        {'role': 'user', 'content': '비슷한 이슈 찾아줘'},
        {'role': 'assistant', 'content': '유사 이슈 3건을 찾았습니다.'},
    ]
    assert streaming._title_exchange_for_unresolved_title(messages) == (
        '카카오 AI top 100 대회 준비를 도와줘',
        '준비 절차를 정리했습니다.',
    )


def test_selector_scans_past_consecutive_opening_user_rows():
    # Queued opening turns must not yield an empty assistant snippet — an empty
    # pair used to leave the title unsettled until a later turn took over.
    messages = [
        {'role': 'user', 'content': 'Opening question about certificates'},
        {'role': 'user', 'content': 'Queued follow-up'},
        {'role': 'assistant', 'content': '## Real first answer'},
        {'role': 'user', 'content': 'Latest request'},
        {'role': 'assistant', 'content': 'Latest answer'},
    ]
    user_text, assistant_text = streaming._title_exchange_for_unresolved_title(messages)
    assert user_text == 'Opening question about certificates'
    assert assistant_text == '## Real first answer'


def test_selector_never_borrows_a_later_assistant_reply():
    # An opener with no assistant text anywhere must stay empty rather than pair
    # with some later turn's answer.
    messages = [
        {'role': 'user', 'content': 'Only question'},
        {'role': 'assistant', 'content': ''},
    ]
    assert streaming._title_exchange_for_unresolved_title(messages) == ('Only question', '')


def test_compaction_continuation_names_the_copied_opener_not_the_rotation_point():
    # A rotated transcript starts mid-conversation: the copied history holds the
    # original task, and the row right before the rotation is a bare refinement.
    messages = [
        {'role': 'user', 'content': '임베딩 모델 서빙 스택 성능을 최적화해줘'},
        {'role': 'assistant', 'content': '프로파일링부터 시작하겠습니다.'},
        {'role': 'user', 'content': '계속'},
        {'role': 'assistant', 'content': '이어서 진행합니다.'},
    ]
    assert streaming._title_exchange_for_unresolved_title(messages) == (
        '임베딩 모델 서빙 스택 성능을 최적화해줘',
        '프로파일링부터 시작하겠습니다.',
    )


# --- injected rows: the opening request must be a human request -------------

INJECTED_TEXTS = (
    # api/background_process.format_wakeup_prompt — completion
    '[IMPORTANT: Background process proc_1 completed (exit_code=0).\nCommand: x\nOutput:\ny]',
    # ... same formatter, the agent-side spelling used by streaming
    '[IMPORTANT: Background process proc_1 completed (exit code 0).\nCommand: x\nOutput:\ny]',
    # ... watch_match
    '[IMPORTANT: Background process proc_1 matched watch pattern "boom".\nCommand: x\nMatched output:\ny]',
    # ... watch_disabled / watch_overflow_*
    '[IMPORTANT: Watch for proc_1 was disabled: too many matches.]',
    # process_registry_notifications.ProcessNotificationBatch.render header
    '[IMPORTANT: 3 background processes completed. Treat these results as one batch]',
    # ... heartbeat
    '[Background process proc_1 heartbeat #3 — still running after 5m\nCommand: x]',
    # ... async delegation, single / batch / task-failed
    '[ASYNC DELEGATION COMPLETE — deleg_1]\nStatus: completed',
    '[ASYNC DELEGATION BATCH COMPLETE — deleg_1]\n2 subagent(s)',
    '[ASYNC DELEGATION TASK FAILED — deleg_1, task 1/3]',
    # the /steer control frame
    '[OUT-OF-BAND USER MESSAGE — steer]\nuse the local branch',
)


def test_every_real_injected_shape_is_recognised_as_text():
    # Guards the prefix table against drift: each entry here is a shape a real
    # formatter emits, so a missing prefix is a live bug, not a nit.
    for row in INJECTED_TEXTS:
        assert streaming._title_is_internal_user_text(row) is True, row


def test_injected_rows_never_become_the_opening_request():
    for row in INJECTED_TEXTS:
        assert streaming._title_exchange_for_unresolved_title(
            [{'role': 'user', 'content': row}, {'role': 'assistant', 'content': 'ok'}]
        ) == ('', ''), row


def test_injected_rows_are_skipped_by_the_structured_stamp_too():
    # The durable row carries _source / display_kind. That stamp is the
    # authoritative signal: it holds even when the rendered text is edited or
    # trimmed, which a prefix match cannot survive.
    for row in (
        {'_source': 'process_wakeup', 'content': 'a completion whose text was trimmed'},
        {'_source': 'delegation_wakeup', 'content': 'wakeup turn'},
        {'display_kind': 'steer', 'content': 'mid-turn steer'},
        {'display_kind': 'async_delegation_complete', 'content': 'batch done'},
    ):
        msg = {'role': 'user', **row}
        assert streaming._title_row_is_internal_user(msg) is True, row
        assert streaming._title_user_request_text(msg) == '', row


def test_a_real_request_that_quotes_a_marker_is_kept():
    # The filter must not swallow a human question merely because it mentions
    # one of these markers in prose.
    messages = [
        {'role': 'user', 'content': 'Why does the log show "[IMPORTANT: Background process" twice?'},
        {'role': 'assistant', 'content': 'Because two completions were drained.'},
    ]
    assert streaming._title_exchange_for_unresolved_title(messages) == (
        'Why does the log show "[IMPORTANT: Background process" twice?',
        'Because two completions were drained.',
    )


def test_a_glued_control_block_keeps_the_request_that_follows_it():
    # The drain path persists ``notifications + "\n\n" + msg_text`` as ONE row.
    # Dropping the whole row would lose the human's request, so only the control
    # head is stripped.
    messages = [
        {
            'role': 'user',
            'content': (
                '[IMPORTANT: Background process proc_1 completed (exit code 0).\n'
                'Command: make build\nOutput:\nok]\n\n'
                'Now fix the sidebar title controls'
            ),
        },
        {'role': 'assistant', 'content': 'Tracing the title path.'},
    ]
    assert streaming._title_exchange_for_unresolved_title(messages) == (
        'Now fix the sidebar title controls',
        'Tracing the title path.',
    )


def test_a_stacked_batch_header_and_blocks_are_all_stripped():
    messages = [
        {
            'role': 'user',
            'content': (
                '[IMPORTANT: 2 background processes completed. Treat these results as one batch]\n\n'
                '[IMPORTANT: Background process proc_1 completed (exit code 0).\nOutput:\na]\n\n'
                '[IMPORTANT: Background process proc_2 completed (exit code 1).\nOutput:\nb]\n\n'
                'Ship the release'
            ),
        },
        {'role': 'assistant', 'content': 'Shipping.'},
    ]
    assert streaming._title_exchange_for_unresolved_title(messages) == (
        'Ship the release',
        'Shipping.',
    )


def test_injected_row_before_the_first_human_turn_is_skipped():
    # A background completion can arrive before the human's first turn (a
    # wakeup turn). It must not become the session's opening request.
    messages = [
        {
            'role': 'user',
            'content': '[IMPORTANT: Background process proc_1 completed (exit code 0).]',
        },
        {'role': 'assistant', 'content': 'Job done.'},
        {'role': 'user', 'content': '실제로 부탁하고 싶은 작업은 이거야'},
        {'role': 'assistant', 'content': '알겠습니다.'},
    ]
    assert streaming._title_exchange_for_unresolved_title(messages) == (
        '실제로 부탁하고 싶은 작업은 이거야',
        '알겠습니다.',
    )


# --- context samples --------------------------------------------------------


def test_context_samples_return_recent_real_requests_without_the_opener():
    messages = [
        {'role': 'user', 'content': '이거 좀 봐줘'},
        {'role': 'assistant', 'content': '무엇을 도와드릴까요?'},
        {'role': 'user', 'content': 'WebUI 제목 생성 버그를 고쳐줘'},
        {'role': 'assistant', 'content': '코드를 확인하겠습니다.'},
        {'role': 'user', 'content': '계속'},
        {'role': 'assistant', 'content': '진행 중입니다.'},
        {'role': 'user', 'content': '비슷한 이슈도 찾아줘'},
        {'role': 'assistant', 'content': '찾았습니다.'},
    ]
    samples = streaming._title_context_samples(messages)
    assert samples == ['계속', '비슷한 이슈도 찾아줘']
    assert '이거 좀 봐줘' not in samples


def test_context_samples_skip_every_injected_shape():
    messages = [{'role': 'user', 'content': 'First real request'}]
    for row in INJECTED_TEXTS:
        messages.append({'role': 'user', 'content': row})
    messages.append({'role': 'user', 'content': 'Second real request'})
    assert streaming._title_context_samples(messages) == ['Second real request']


def test_context_samples_never_repeat_the_title_source():
    # On the manual ``prefer_latest`` path the newest request IS the source;
    # listing it again as context would feed the model the same text twice.
    messages = [
        {'role': 'user', 'content': 'First request'},
        {'role': 'assistant', 'content': 'First answer'},
        {'role': 'user', 'content': 'The latest request'},
        {'role': 'assistant', 'content': 'Latest answer'},
    ]
    samples = streaming._title_context_samples(messages, exclude_text='The latest request')
    assert samples == []


def test_context_samples_are_capped_and_truncated():
    long_request = '요' * 400
    messages = [
        {'role': 'user', 'content': 'First request'},
        {'role': 'user', 'content': 'middle request'},
        {'role': 'user', 'content': long_request},
    ]
    samples = streaming._title_context_samples(messages)
    assert len(samples) == 2
    assert samples[-1] == '요' * 200
    assert streaming._title_context_samples(messages, limit=0) == []


def test_context_samples_empty_for_single_exchange():
    messages = [
        {'role': 'user', 'content': 'Only request'},
        {'role': 'assistant', 'content': 'Only answer'},
    ]
    assert streaming._title_context_samples(messages) == []


# --- prompt wiring ----------------------------------------------------------


def test_prompt_carries_samples_as_labelled_context():
    qa, prompts = streaming._title_prompts(
        'Fix the sidebar title controls', 'Done.', None, ['계속', '비슷한 이슈도 찾아줘']
    )
    assert 'Later requests in this same session (context only' in qa
    assert '- 계속' in qa
    assert '- 비슷한 이슈도 찾아줘' in qa
    # The scope rule is load-bearing: without it the labelled block reads as a
    # second title source and the model titles from the newest instruction.
    assert "Name the session's overall topic from the opening exchange" in prompts[0]
    assert 'title the work, not the last instruction' in prompts[1]


def test_prompt_without_samples_keeps_the_original_shape():
    qa, prompts = streaming._title_prompts('Fix the sidebar title controls', 'Done.')
    assert qa == (
        'User question:\nFix the sidebar title controls\n\n'
        'Assistant answer:\nDone.'
    )
    assert 'Later requests' not in qa
    # No samples means no scope rule either: the periodic refresh path titles
    # from the latest exchange on purpose, so a standing "never title from a
    # later request" line would contradict its own inputs.
    assert 'later' not in prompts[0].lower()
    assert 'later requests' not in prompts[1].lower()


# --- readiness gate ---------------------------------------------------------


def test_background_inputs_stay_none_when_the_opener_has_no_answer():
    # The gate must keep refusing an unanswered opening turn. This is not new
    # behaviour — it is the invariant that must survive the selector now
    # scanning past queued user rows (#7543), so it is pinned here explicitly.
    session = MagicMock()
    session.title = 'Untitled'
    session.llm_title_generated = False
    session.messages = [
        {'role': 'user', 'content': 'Only question'},
        {'role': 'assistant', 'content': '', 'tool_calls': [{'id': 'x'}]},
    ]
    assert streaming._background_title_generation_inputs(session) is None


def test_background_inputs_return_the_opening_pair_once_answered():
    session = MagicMock()
    session.title = 'Untitled'
    session.llm_title_generated = False
    session.messages = [
        {'role': 'user', 'content': 'Opening request'},
        {'role': 'assistant', 'content': 'Opening answer'},
        {'role': 'user', 'content': 'Later refinement'},
        {'role': 'assistant', 'content': 'Later answer'},
    ]
    assert streaming._background_title_generation_inputs(session) == (
        'Opening request',
        'Opening answer',
    )


# --- manual regeneration wiring ---------------------------------------------


class _ProfileEnv:
    def __enter__(self):
        return None

    def __exit__(self, exc_type, exc, tb):
        return False


def test_manual_regeneration_passes_context_samples(monkeypatch):
    captured = {}

    def _fake_aux(user_text, assistant_text, **kwargs):
        captured['user_text'] = user_text
        captured['assistant_text'] = assistant_text
        captured['recent_user_texts'] = kwargs.get('recent_user_texts')
        return 'Generated Title', 'llm_aux', ''

    monkeypatch.setattr(profiles_api, 'profile_env_for_background_worker', lambda *a, **k: _ProfileEnv())
    monkeypatch.setattr(streaming, '_aux_title_generation_enabled', lambda: True)
    monkeypatch.setattr(streaming, '_generate_llm_session_title_via_aux', _fake_aux)

    session = MagicMock()
    session.session_id = 'title-scope'
    session.messages = [
        {'role': 'user', 'content': '이거 좀 봐줘'},
        {'role': 'assistant', 'content': '무엇을 도와드릴까요?'},
        {'role': 'user', 'content': 'WebUI 제목 생성 버그를 고쳐줘'},
        {'role': 'assistant', 'content': '확인하겠습니다.'},
    ]

    title, status, _raw = streaming.generate_session_title_for_session(session)
    assert (title, status) == ('Generated Title', 'llm_aux')
    assert captured['user_text'] == '이거 좀 봐줘'
    assert captured['assistant_text'] == '무엇을 도와드릴까요?'
    assert captured['recent_user_texts'] == ['WebUI 제목 생성 버그를 고쳐줘']


def test_manual_regeneration_on_prefer_latest_does_not_repeat_its_source(monkeypatch):
    captured = {}

    def _fake_aux(user_text, assistant_text, **kwargs):
        captured['user_text'] = user_text
        captured['recent_user_texts'] = kwargs.get('recent_user_texts')
        return 'Generated Title', 'llm_aux', ''

    monkeypatch.setattr(profiles_api, 'profile_env_for_background_worker', lambda *a, **k: _ProfileEnv())
    monkeypatch.setattr(streaming, '_aux_title_generation_enabled', lambda: True)
    monkeypatch.setattr(streaming, '_generate_llm_session_title_via_aux', _fake_aux)

    session = MagicMock()
    session.session_id = 'title-latest'
    session.messages = [
        {'role': 'user', 'content': 'Opening request'},
        {'role': 'assistant', 'content': 'Opening answer'},
        {'role': 'user', 'content': 'Latest request'},
        {'role': 'assistant', 'content': 'Latest answer'},
    ]

    title, status, _raw = streaming.generate_session_title_for_session(session, prefer_latest=True)
    assert (title, status) == ('Generated Title', 'llm_aux')
    assert captured['user_text'] == 'Latest request'
    assert 'Latest request' not in (captured['recent_user_texts'] or [])
