from pathlib import Path

from api.streaming import _compact_for_echo_compare


def test_streaming_initializes_one_run_journal_writer_per_stream():
    src = Path("api/streaming.py").read_text(encoding="utf-8")
    register_idx = src.index("register_active_run(")
    writer_idx = src.index("RunJournalWriter(session_id, stream_id)", register_idx)
    cancel_idx = src.index("cancel_event = threading.Event()", writer_idx)

    assert "from api.run_journal import RunJournalWriter" in src
    assert register_idx < writer_idx < cancel_idx


def test_streaming_uses_one_transaction_for_journal_and_queue_delivery():
    src = Path("api/streaming.py").read_text(encoding="utf-8")
    put_idx = src.index("def put(event, data):")
    block = src[put_idx:src.index("# #5940", put_idx)]

    assert "def _publish_journaled(journaled):" in block
    assert "run_journal.append_and_publish_sse_event(event, data, _publish_journaled)" in block
    assert "q.put_nowait(queue_item)" in block
    assert "Failed to append run journal event" in block
    assert "queue_item = (event, data, event_id) if event_id and hasattr(q, \"subscribe_with_snapshot\") else (event, data)" in block


def test_gateway_uses_same_transaction_for_journal_and_queue_delivery():
    src = Path("api/gateway_chat.py").read_text(encoding="utf-8")
    put_idx = src.index("def put_gateway_event(event, data):")
    block = src[put_idx:src.index("\n    s = None", put_idx)]

    assert "def _publish_journaled(journaled):" in block
    assert "run_journal.append_and_publish_sse_event(event, data, _publish_journaled)" in block
    assert "q.put_nowait(queue_item)" in block
    assert "Failed to append gateway event" in block



def test_streaming_compacts_all_successful_agent_result_writebacks():
    src = Path("api/streaming.py").read_text(encoding="utf-8")
    run_src = src[src.index("def _run_agent_streaming("):]
    settle_src = src[src.index("def _settle_result_messages("):]
    settle_src = settle_src[: settle_src.index("\n\ndef _current_turn_already_has_visible_assistant_answer(")]

    # Normal completion plus both credential self-heal retry-success paths now
    # route through one shared settlement helper, which owns the compaction.
    assert "def _settle_result_messages(" in src
    assert "_compact_session_image_parts_for_persistence(session)" in settle_src
    assert run_src.count("_settle_result_messages(") == 3


def test_visible_process_echo_compare_ignores_all_whitespace():
    token_text = "先把 issue 4249 拉下来\n\n先看正文和评论"
    interim_text = "先把 issue 4249 拉下来先看正文和评论"

    assert _compact_for_echo_compare(token_text) == _compact_for_echo_compare(interim_text)
