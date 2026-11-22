"""#8940: a memory recall belongs to one stream, and stays with it however the turn ends.

Recovery must not hand one stream's recall to another turn. Stop, a successful
authentication retry and dead-worker repair must save it on the turn it was
relayed for, and never in the history a model receives.

The worker, the cancel path, the journal and the repair are the real ones; only
the Agent and the credential refresh are fakes. The browser statements run in
node, extracted from the shipped sources.
"""
import gzip
import json
import pathlib
import queue
import shutil
import subprocess
import sys
import types
from unittest import mock

import pytest

import api.config as config
import api.models as models
import api.streaming as streaming
from api import run_journal
from api.models import Session
from api.process_event_utils import build_active_turn_token
from tests.test_inflight_stream_reuse import _function_body, _function_decl

REPO = pathlib.Path(__file__).resolve().parents[1]
NODE = shutil.which("node")
RECALL = "🧠 Honcho — recalled 3 memories"
OLD_RECALL = "🧠 Honcho — recalled 9 memories"
SID, STREAM, PROMPT = "recall-session", "recall-stream", "Plan the release"
STARTED = 1234567890.0
HEAL_RUNTIME = {"provider": "test-provider", "api_key": "fresh-key", "base_url": None}


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    monkeypatch.setattr(models, "SESSION_DIR", sessions)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", sessions / "_index.json")
    monkeypatch.setattr(run_journal, "_default_session_dir", lambda: sessions)

    runtime = types.ModuleType("hermes_cli.runtime_provider")
    runtime.resolve_runtime_provider = lambda requested=None, **_kw: {
        "provider": requested or "test-provider", "api_key": "synthetic-key", "base_url": None,
    }
    cli = types.ModuleType("hermes_cli")
    cli.runtime_provider = runtime
    state = types.ModuleType("hermes_state")
    state.SessionDB = mock.Mock(return_value=None)
    for name, module in (("hermes_cli", cli), ("hermes_cli.runtime_provider", runtime), ("hermes_state", state)):
        monkeypatch.setitem(sys.modules, name, module)

    shared = (
        models.SESSIONS, config.STREAMS, config.CANCEL_FLAGS, config.AGENT_INSTANCES,
        config.STREAM_PARTIAL_TEXT, config.STREAM_REASONING_TEXT, config.STREAM_LIVE_TOOL_CALLS,
        config.ACTIVE_RUNS, config.SESSION_AGENT_LOCKS,
    )
    for registry in shared:
        registry.clear()
    yield tmp_path
    for registry in shared:
        registry.clear()


def _pending_session(*, history=(), session_id=SID, parent=None):
    session = Session(session_id=session_id, title="Recall", messages=[dict(row) for row in history])
    session.context_messages = [{"role": r["role"], "content": r["content"]} for r in history]
    session.parent_session_id = parent
    session.pending_user_message = PROMPT
    session.pending_attachments = []
    session.pending_started_at = STARTED
    session.pending_user_source = "webui"
    session.active_stream_id = STREAM
    session.save()
    models.SESSIONS[session_id] = session
    return session


def _history():
    return [
        {"role": "user", "content": "Earlier question", "timestamp": 1},
        {"role": "assistant", "content": "Earlier answer", "timestamp": 2},
    ]


def _journal(*events, session_id=SID, stream_id=STREAM):
    writer = run_journal.RunJournalWriter(session_id, stream_id)
    for name, payload in events:
        writer.append_sse_event(name, payload)


def _recall_event(message=RECALL, session_id=SID):
    return ("memory_recall", {"session_id": session_id, "message": message})


def _cold_load(session_id=SID):
    models.SESSIONS.clear()
    loaded = Session.load(session_id)
    assert loaded is not None
    return loaded


def _recalled(messages):
    return [row for row in messages if row.get("_memoryRecall")]


def _assert_out_of_model_history(session):
    # The sanitizer alone would hide a leak into the stored context.
    assert "_memoryRecall" not in json.dumps(session.context_messages)
    sent = streaming._sanitize_messages_for_agent(
        session.context_messages or session.messages,
        cfg={}, effective_model="test-model", effective_provider="test-provider",
        effective_base_url=None, requested_provider="",
    )
    assert "_memoryRecall" not in json.dumps(sent)
    assert RECALL not in json.dumps(sent, ensure_ascii=False)


class _RecallAgent:
    """Emits the recall the way hermes-agent does, then runs ``behave``."""

    behave = None
    runs = 0

    def __init__(self, status_callback=None, stream_delta_callback=None, **kwargs):
        self.session_id = kwargs.get("session_id")
        self.status_callback = status_callback
        self.stream_delta_callback = stream_delta_callback
        self._memory_manager = types.SimpleNamespace(describe_recall=lambda: RECALL)
        self.session_prompt_tokens = self.session_completion_tokens = 0
        self.session_estimated_cost_usd = 0.0
        self.context_compressor = self._last_error = self.ephemeral_system_prompt = None

    def interrupt(self, _message):
        pass

    def run_conversation(self, **kwargs):
        cls = type(self)
        cls.runs += 1
        if cls.runs == 1:
            self.status_callback("lifecycle", RECALL)
        return cls.behave(self, list(kwargs.get("conversation_history") or []))


def _run_worker(session, behave, workspace, *, heal=None):
    agent_cls = type("Agent", (_RecallAgent,), {"behave": staticmethod(behave), "runs": 0})
    events = queue.Queue()
    config.STREAMS[STREAM] = events
    config.STREAM_PARTIAL_TEXT[STREAM] = ""
    with mock.patch.object(streaming, "get_session", return_value=session), \
         mock.patch.object(streaming, "_get_ai_agent", return_value=agent_cls), \
         mock.patch.object(streaming, "resolve_model_provider", return_value=("test-model", "test-provider", None)), \
         mock.patch("api.config.get_config", return_value={}), \
         mock.patch("api.config._resolve_cli_toolsets", return_value=[]), \
         mock.patch.object(streaming, "_attempt_credential_self_heal", return_value=heal),          mock.patch.object(streaming, "construct_profile_scoped_agent", lambda cls, kwargs, **_kw: (cls(**kwargs), True)):
        streaming._run_agent_streaming(session.session_id, PROMPT, "test-model", str(workspace), STREAM)
    emitted = [(item[0], item[1]) for item in list(events.queue)]
    assert "memory_recall" in [name for name, _ in emitted], "fixture: the Agent's recall was not relayed"
    return emitted


# ── Stop ─────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("partial", ["", "Looking at the checklist"], ids=["before-output", "after-partial"])
def test_stop_keeps_the_recall_on_the_cancelled_turn(isolated, partial):
    session = _pending_session(history=_history())

    def behave(agent, history):
        if partial:
            agent.stream_delta_callback(partial)
        assert streaming.cancel_stream(STREAM) is True
        return {"messages": history, "interrupted": True}

    events = _run_worker(session, behave, isolated)
    saved = _cold_load()

    rows = _recalled(saved.messages)
    assert [(row.get("_error"), row["_memoryRecall"]) for row in rows] == [(True, RECALL)]
    assert saved.messages.index(rows[0]) > 1, "never stamped on the previous turn"
    if partial:
        kept = next(row for row in saved.messages if row.get("_partial"))
        assert kept["content"] == partial
        assert "_memoryRecall" not in kept, "the partial is sent to the model"
    cancel = next(data for name, data in events if name == "cancel")
    assert _recalled(cancel["session"]["messages"]), "the cancel payload shows it without a reload"
    _assert_out_of_model_history(saved)


def test_stop_without_a_recall_stays_empty(isolated):
    session = _pending_session(history=_history())
    config.STREAMS[STREAM] = queue.Queue()
    config.AGENT_INSTANCES[STREAM] = types.SimpleNamespace(session_id=SID, interrupt=lambda _reason: None)
    _journal(("token", {"text": "partial"}))
    _journal(_recall_event(OLD_RECALL), stream_id="another-stream")

    assert streaming.cancel_stream(STREAM) is True
    assert session.active_stream_id is None
    assert not _recalled(_cold_load().messages)


def test_stop_after_compression_reads_the_parent_session_journal(isolated):
    _journal(_recall_event())
    _pending_session(history=_history(), session_id="recall-child", parent=SID)
    config.STREAMS[STREAM] = queue.Queue()
    config.AGENT_INSTANCES[STREAM] = types.SimpleNamespace(session_id="recall-child", interrupt=lambda _reason: None)

    assert streaming.cancel_stream(STREAM) is True
    assert [row["_memoryRecall"] for row in _recalled(_cold_load("recall-child").messages)] == [RECALL]


def test_worker_side_cancel_finalizer_keeps_the_recall():
    session = _pending_session(history=_history())
    _journal(_recall_event())

    with mock.patch.object(streaming, "session_writeback_owner", return_value=STREAM), \
         mock.patch.object(streaming, "get_session", return_value=session):
        streaming._finalize_cancelled_turn(session, stream_id=STREAM)

    saved = _cold_load()
    assert saved.messages[-1]["_error"] is True
    assert saved.messages[-1]["_memoryRecall"] == RECALL


# ── Authentication retry ─────────────────────────────────────────────────────


@pytest.mark.parametrize("failure", ["raised", "returned"])
def test_successful_auth_retry_saves_the_recall(isolated, failure):
    session = _pending_session(history=_history())

    def behave(agent, history):
        if type(agent).runs == 1:
            if failure == "raised":
                raise RuntimeError("401 unauthorized")
            return {"messages": history, "error": {"error": {
                "type": "authentication_error", "status_code": 401, "code": "auth_unavailable",
                "message": "Your authentication token has been invalidated. Please try signing in again.",
            }}}
        return {"messages": history + [{"role": "assistant", "content": "Recovered answer"}]}

    events = _run_worker(session, behave, isolated, heal=HEAL_RUNTIME)
    saved = _cold_load()

    assert [(row["content"], row["_memoryRecall"]) for row in _recalled(saved.messages)] == [
        ("Recovered answer", RECALL),
    ]
    done = next(data for name, data in events if name == "done")
    assert _recalled(done["session"]["messages"]), "saved before the payload, not patched by the browser"
    _assert_out_of_model_history(saved)


def test_settled_payload_carries_the_recall_the_browser_shows(isolated):
    # The browser does not stamp rows, so the line after settlement is this one.
    session = _pending_session(history=_history())

    events = _run_worker(
        session, lambda _agent, history: {"messages": history + [{"role": "assistant", "content": "Answer"}]}, isolated,
    )

    done = next(data for name, data in events if name == "done")
    assert [(row["content"], row["_memoryRecall"]) for row in _recalled(done["session"]["messages"])] == [("Answer", RECALL)]


def test_retry_stamp_copies_the_shared_row_and_stays_in_its_turn():
    answer = {"role": "assistant", "content": "Recovered answer"}
    messages, context = [{"role": "user", "content": PROMPT}, answer], [answer]
    streaming._stamp_settled_turn_memory_recall(messages, None, RECALL)
    assert messages[1]["_memoryRecall"] == RECALL
    assert "_memoryRecall" not in context[0], "the row is shared with context_messages"

    unanswered = [dict(answer), {"role": "user", "content": PROMPT}]
    streaming._stamp_settled_turn_memory_recall(unanswered, None, RECALL)
    assert not _recalled(unanswered)


# ── Dead-worker repair ───────────────────────────────────────────────────────


@pytest.mark.parametrize("history", [[], _history()], ids=["first-turn", "later-turn"])
@pytest.mark.parametrize("output", ["none", "text", "tool"])
def test_dead_worker_repair_keeps_the_recall(isolated, history, output):
    session = _pending_session(history=history)
    events = [_recall_event()]
    if output == "text":
        events.append(("token", {"text": "Looking at the checklist"}))
    elif output == "tool":
        events.append(("tool", {"name": "terminal", "preview": "ls", "tid": "call-1"}))
    _journal(*events)

    assert models._apply_core_sync_or_error_marker(session, isolated / "absent.json", STREAM) is True
    saved = _cold_load()

    rows = _recalled(saved.messages)
    assert [(row.get("type"), row["_memoryRecall"]) for row in rows] == [("interrupted", RECALL)]
    assert "_memoryRecall" not in json.dumps(saved.context_messages)
    _assert_out_of_model_history(saved)


def test_ordinary_session_repair_keeps_the_recall(isolated, monkeypatch):
    session = _pending_session(history=_history())
    _journal(_recall_event(), ("token", {"text": "Looking at the checklist"}))
    monkeypatch.setattr(models, "_get_profile_home", lambda _profile: isolated)

    assert models._repair_stale_pending(session) is True
    assert [row["_memoryRecall"] for row in _recalled(_cold_load().messages)] == [RECALL]


def test_completed_journal_repair_stamps_the_recovered_answer(isolated):
    session = _pending_session(history=_history())
    _journal(_recall_event(), ("token", {"text": "Done."}), ("done", {"status": "completed"}))

    assert models._apply_core_sync_or_error_marker(session, isolated / "absent.json", STREAM) is True
    saved = _cold_load()

    assert [(row["content"], row["_memoryRecall"]) for row in _recalled(saved.messages)] == [("Done.", RECALL)]
    assert "_memoryRecall" not in json.dumps(saved.context_messages)
    _assert_out_of_model_history(saved)


def test_core_transcript_repair_stamps_the_current_answer(isolated):
    session = _pending_session()
    _journal(_recall_event())
    core = isolated / "core.json"
    core.write_text(json.dumps({"messages": _history() + [
        {"role": "user", "content": PROMPT, "timestamp": STARTED},
        {"role": "assistant", "content": "Answer from core", "timestamp": STARTED + 1},
    ]}), encoding="utf-8")

    assert models._apply_core_sync_or_error_marker(session, core, STREAM) is True
    assert [row["content"] for row in _recalled(_cold_load().messages)] == ["Answer from core"]


def test_already_advanced_transcript_stamps_its_own_answer(isolated, monkeypatch):
    token = build_active_turn_token(STREAM, STARTED)
    session = _pending_session(history=_history() + [
        {"role": "user", "content": PROMPT, "timestamp": STARTED, "_active_turn_token": token},
        {"role": "assistant", "content": "Settled answer", "timestamp": STARTED + 1},
    ])
    _journal(_recall_event())
    monkeypatch.setattr(models, "_turn_journal_records_completion", lambda *_a: True)

    assert models._apply_core_sync_or_error_marker(session, isolated / "absent.json", STREAM) is True
    assert [row["content"] for row in _recalled(_cold_load().messages)] == ["Settled answer"]


def test_state_db_repair_stamps_the_current_answer_only(monkeypatch):
    session = _pending_session(history=_history())
    _journal(_recall_event())
    state_rows = _history() + [
        {"role": "user", "content": PROMPT, "timestamp": STARTED},
        {"role": "assistant", "content": "Answer from state.db", "timestamp": STARTED + 1},
    ]
    monkeypatch.setattr(models, "get_state_db_session_summary", lambda *_a, **_k: {
        "message_count": len(state_rows), "last_message_at": STARTED + 1,
    })
    monkeypatch.setattr(models, "get_state_db_session_messages", lambda *_a, **_k: state_rows)

    assert models._sync_sidecar_from_state_db_if_newer(session) is True
    saved = _cold_load()

    assert [row["content"] for row in _recalled(saved.messages)] == ["Answer from state.db"]
    assert "_memoryRecall" not in json.dumps(saved.context_messages)


def test_late_journal_reaches_the_interrupted_marker(isolated):
    session = _pending_session(history=_history())
    assert models._apply_core_sync_or_error_marker(session, isolated / "absent.json", STREAM) is True
    assert not _recalled(session.messages)

    _journal(_recall_event(), ("token", {"text": "Late tail"}))
    assert models._retry_journal_recovery_in_place(session) is True

    saved = _cold_load()
    assert [(row.get("type"), row["_memoryRecall"]) for row in _recalled(saved.messages)] == [("interrupted", RECALL)]
    assert "_memoryRecall" not in json.dumps(saved.context_messages)


@pytest.mark.parametrize("owner", [{"stream_id": "another-stream"}, {"session_id": "another-session"}])
def test_repair_never_borrows_another_owner_recall(isolated, owner):
    session = _pending_session(history=_history())
    _journal(("token", {"text": "mine"}))
    _journal(_recall_event(OLD_RECALL), **owner)

    assert models._apply_core_sync_or_error_marker(session, isolated / "absent.json", STREAM) is True
    assert not _recalled(_cold_load().messages)


def test_completed_repair_never_stamps_an_older_turn_with_the_same_prompt(isolated):
    session = _pending_session(history=[
        # Same prompt, same second: only the token tells the turns apart.
        {"role": "user", "content": PROMPT, "timestamp": STARTED, "_active_turn_token": "older-stream:1"},
        {"role": "assistant", "content": "Older answer", "timestamp": 2},
    ])
    _journal(_recall_event(), ("done", {"status": "completed"}))

    assert models._apply_core_sync_or_error_marker(session, isolated / "absent.json", STREAM) is True
    assert not _recalled(_cold_load().messages)


def test_context_seed_drops_the_display_only_recall():
    session = types.SimpleNamespace(context_messages=[], messages=[
        {"role": "user", "content": "Earlier question"},
        {"role": "assistant", "content": "Earlier answer", "_memoryRecall": OLD_RECALL},
    ])
    models._append_recovered_turn_to_context(session, {"role": "user", "content": PROMPT})

    assert [row["content"] for row in session.context_messages] == ["Earlier question", "Earlier answer", PROMPT]
    assert "_memoryRecall" not in json.dumps(session.context_messages)


def test_repair_without_an_answer_or_marker_invents_no_row(isolated):
    session = _pending_session()
    _journal(_recall_event())
    core = isolated / "core.json"
    prompt_only = [{"role": "user", "content": PROMPT, "timestamp": STARTED}]
    core.write_text(json.dumps({"messages": prompt_only}), encoding="utf-8")

    assert models._apply_core_sync_or_error_marker(session, core, STREAM) is True
    assert [row["role"] for row in _cold_load().messages] == ["user"]


@pytest.mark.parametrize("recall", [False, True], ids=["none", "recall"])
def test_reading_the_recall_does_not_validate_the_whole_live_journal(monkeypatch, recall):
    # Stop runs this under the session lock, also for users without a memory provider.
    _journal(*([_recall_event()] if recall else []), ("token", {"text": 'mentions "memory_recall" in prose'}))

    def full_read(*_args, **_kwargs):
        raise AssertionError("full validated read")

    monkeypatch.setattr(run_journal, "_read_validated_recovery_events", full_read)
    monkeypatch.setattr(run_journal, "read_run_events", full_read)
    with mock.patch.object(run_journal.logger, "debug") as failed:
        assert run_journal.memory_recall_for_run(STREAM, SID) == (RECALL if recall else "")
    failed.assert_not_called()


@pytest.mark.parametrize("live_tail", [False, True], ids=["archived", "archived-with-live-tail"])
def test_recall_is_read_from_an_archived_journal(live_tail):
    # A tool of the same name puts the token in a row that is not the recall.
    _journal(_recall_event(), ("tool", {"name": "memory_recall"}))
    live = run_journal._run_path(SID, STREAM)
    first, second = live.read_bytes().splitlines(keepends=True)
    archive = run_journal._archive_path_for(live)
    archive.parent.mkdir(parents=True)
    archive.write_bytes(gzip.compress(first if live_tail else first + second))
    if live_tail:
        live.write_bytes(second)
    else:
        live.unlink()

    assert run_journal.memory_recall_for_run(STREAM, SID) == RECALL


def test_recall_in_a_last_row_without_newline_is_read():
    _journal(_recall_event())
    live = run_journal._run_path(SID, STREAM)
    live.write_bytes(live.read_bytes().rstrip(b"\n"))

    assert run_journal.memory_recall_for_run(STREAM, SID) == RECALL


# ── Browser ownership ────────────────────────────────────────────────────────


def _source(name):
    return (REPO / "static" / name).read_text(encoding="utf-8")


def _between(source, start, end):
    begin = source.index(start)
    return source[begin:source.index(end, begin)]


def _node(*parts):
    completed = subprocess.run(
        [NODE, "-e", "\n".join(parts)], capture_output=True, text=True, encoding="utf-8"
    )
    assert completed.returncode == 0, completed.stderr
    return json.loads(completed.stdout) if completed.stdout.strip() else None


def _snap(stream, recall="", seq=1, visible=True):
    row = {"streamId": stream, "memoryRecall": recall, "lastRunJournalSeq": seq, "messages": []}
    if visible:
        row["lastAssistantText"] = "streamed text"
    return row


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
@pytest.mark.parametrize(
    "local,server,expected",
    [
        # The gate's mixed A/B inputs and its two controls.
        pytest.param(_snap("B"), _snap("A", "RA"), "", id="rejected-A-is-not-borrowed"),
        pytest.param(_snap("B", "RB"), _snap("A", "RA"), "RB", id="current-value-preserved"),
        pytest.param(_snap("B"), _snap("B"), "", id="no-recall-stays-empty"),
        pytest.param(_snap("B", seq=5), _snap("", "RX"), "", id="ownerless-snapshot-is-not-borrowed"),
        pytest.param(None, _snap("A", "RA"), "", id="selected-foreign-snapshot"),
        pytest.param(None, _snap("", "RX"), "", id="selected-ownerless-snapshot"),
        pytest.param(_snap("A", "RA"), None, "", id="selected-foreign-local"),
        pytest.param(_snap("A", "RA"), _snap("B"), "", id="stale-local"),
        # Same-stream borrowing in both directions keeps working.
        pytest.param(_snap("B", seq=5), _snap("B", "RB"), "RB", id="newer-local-borrows-from-server"),
        pytest.param(_snap("B", "RB"), _snap("B", seq=5), "RB", id="newer-server-borrows-from-local"),
        pytest.param(None, _snap("B", "RB"), "RB", id="reload-from-server"),
        pytest.param(_snap("B", "RB"), None, "RB", id="reload-from-local"),
    ],
)
def test_recovery_borrows_recall_only_from_the_active_stream(local, server, expected):
    source = _source("sessions.js")
    block = _between(
        source,
        "  const hadLiveRecoveryInflight=!!INFLIGHT[sid];",
        "  if(INFLIGHT[sid]){\n    _ensureInflightLiveAssistantMessage",
    )
    got = _node(
        _function_decl(source, "_selectLiveRecoveryInflight"),
        _function_decl(source, "_inflightHasVisibleLiveState"),
        "const _notifyRunJournalSideEffectRecoveryTruncation=()=>{},clearInflightState=()=>{};",
        f"const sid='s',activeStreamId='B',serverLiveSnapshot={json.dumps(server)},local={json.dumps(local)};",
        "const INFLIGHT=local?{s:local}:{};",
        block,
        "process.stdout.write(JSON.stringify((INFLIGHT[sid]&&INFLIGHT[sid].memoryRecall)||''));",
    )
    assert got == expected


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
@pytest.mark.parametrize("prior,expected", [("A", ""), ("", ""), ("B", "kept")])
def test_attach_keeps_recall_only_for_the_same_stream(prior, expected):
    block = _between(
        _function_body(_source("messages.js"), "attachLiveStream"),
        "  const _priorInflightStreamId=",
        "  if(!Array.isArray(INFLIGHT[activeSid].activityBurstAnchors))",
    )
    got = _node(
        "const activeSid='s',streamId='B',noted=[];",
        f"const INFLIGHT={{s:{{streamId:{json.dumps(prior)},memoryRecall:'kept'}}}};",
        "const _noteLiveMemoryRecall=(sid,stream,text)=>noted.push(text);",
        block,
        "process.stdout.write(JSON.stringify([INFLIGHT.s.memoryRecall||'',noted]));",
    )
    assert got == [expected, [expected] if expected else []]


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
@pytest.mark.parametrize("owner,accepted", [("B", False), ("A", True)])
def test_late_event_from_an_older_stream_does_not_take_over(owner, accepted):
    listener = _between(
        _source("messages.js"),
        "source.addEventListener('memory_recall'",
        "source.addEventListener('warning'",
    )
    got = _node(
        "const activeSid='s',streamId='A',noted=[];let handler,persisted=0;",
        f"const INFLIGHT={{s:{{streamId:{json.dumps(owner)}}}}},S={{session:{{session_id:'s'}}}};",
        "const _noteLiveMemoryRecall=(sid,stream,text)=>noted.push(text);",
        "const persistInflightState=()=>{persisted++;},_syncLiveAssistantMemoryRecall=()=>{};",
        "const source={addEventListener:(name,fn)=>{handler=fn;}};",
        listener,
        "handler({data:JSON.stringify({session_id:'s',message:'RA'})});",
        "process.stdout.write(JSON.stringify([INFLIGHT.s.memoryRecall||'',noted,persisted]));",
    )
    assert got == (["RA", ["RA"], 1] if accepted else ["", [], 0])


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
@pytest.mark.parametrize("active,shown", [("B", ""), ("A", "RA"), ("", "RA")])
def test_reused_live_turn_drops_another_stream_line(active, shown):
    got = _node(
        f"const S={{session:{{session_id:'s'}},activeStreamId:{json.dumps(active)}}};",
        "let line='RA';const turn={dataset:{sessionId:'s'}},$=()=>turn;",
        "const _liveMemoryRecallText=(sid,stream)=>stream==='A'?'RA':'';",
        "const _setAssistantTurnMemoryRecall=(el,text)=>{line=text;};",
        _function_decl(_source("ui.js"), "_syncLiveAssistantMemoryRecall"),
        "_syncLiveAssistantMemoryRecall();process.stdout.write(JSON.stringify(line));",
    )
    assert got == shown


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
@pytest.mark.parametrize("previous", ["RA", None], ids=["A-has-recall", "A-has-none"])
@pytest.mark.parametrize("ending", ["done", "apperror", "cancel"])
def test_browser_never_assigns_a_recall_to_a_settled_row(ending, previous):
    # A server-started turn B pushes no user row, so the transcript still ends
    # with A's answer. Its tail says nothing about who owns B's recall: only the
    # server writes _memoryRecall on a row.
    source = _source("messages.js")
    if ending == "done":
        block = _between(
            source,
            "          if(lastAsst&&!lastAsst._ts&&!lastAsst.timestamp)",
            "          if(d.usage){",
        )
    elif ending == "apperror":
        block = _between(
            source,
            "            S.messages.push({role:'assistant',content:`**${label}:**",
            "            _attachProjectedAnchorSceneToLastAssistant(S.messages);",
        )
    else:
        block = _between(
            source,
            "            S.messages.push({role:'assistant',content:`**Task cancelled:** ${_cancelDetailText}",
            "            _attachProjectedAnchorSceneToLastAssistant(S.messages);",
        )
    rows = [{"role": "user"}, {"role": "assistant", "_memoryRecall": previous}]
    got = _node(
        "const activeSid='s',streamId='B',label='Error',d={message:'boom'},hint='',details='',detailsLabel='',recovery=null;",
        "const _cancelDetailText='x.',_cancelHint='h',_cancelDetail='x';",
        f"const S={{messages:{json.dumps(rows)}}},lastAsst=S.messages[1];",
        "const _lastUserRow=()=>S.messages[0],_cancelledUserRow=S.messages[0];",
        "const _liveMemoryRecallText=()=>'RB';",
        "const _stampLiveMemoryRecall=(row)=>{row._memoryRecall='RB';};",
        block,
        "process.stdout.write(JSON.stringify(S.messages.map(m=>m._memoryRecall||null)));",
    )
    assert got[:2] == [None, previous]
    assert "RB" not in got


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
@pytest.mark.parametrize("duplicate", [False, True], ids=["unique-row", "same-identity-twice"])
def test_carry_forward_does_not_copy_recall_between_identical_rows(duplicate):
    source = _source("messages.js")
    fields = _between(source, "  const _EPHEMERAL_TURN_FIELDS=", "  function _isHistoricalAnchorActivityScene")
    marker = {"role": "assistant", "content": "Task cancelled.", "timestamp": 7}
    following = [dict(marker), dict(marker)] if duplicate else [dict(marker)]
    got = _node(
        _function_decl(source, "_messageIdentityKey"),
        fields,
        _function_decl(source, "_isHistoricalAnchorActivityScene"),
        _function_decl(source, "_carryForwardEphemeralTurnFields"),
        f"const next={json.dumps(following)},prev={json.dumps([{**marker, '_memoryRecall': 'RA'}])};",
        "_carryForwardEphemeralTurnFields(prev,next);",
        "process.stdout.write(JSON.stringify(next.map(m=>m._memoryRecall||null)));",
    )
    assert got == ([None, None] if duplicate else ["RA"])


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
@pytest.mark.parametrize("active,shown", [("B", ""), ("A", "RA")], ids=["other-stream", "own-stream"])
def test_restored_live_html_shows_only_the_active_stream_line(active, shown):
    source = _source("ui.js")
    got = _node(
        f"const S={{session:{{session_id:'s'}},activeStreamId:{json.dumps(active)}}},INFLIGHT={{s:{{liveTurnHtml:'<div>'}}}};",
        "let restored=null;const el={line:'RA',dataset:{},querySelector:()=>null};",
        "const document={createElement:()=>({content:{firstElementChild:el}})};",
        "const $=(id)=>id==='msgInner'?{appendChild:(n)=>{restored=n;}}:restored;",
        "const _mergeRestoredLiveAssistantSegment=()=>{},_postProcessWithAnchorSuppression=()=>{};",
        "const requestAnimationFrame=()=>{},_liveMemoryRecallText=(sid,stream)=>stream==='A'?'RA':'';",
        "const _setAssistantTurnMemoryRecall=(turn,text)=>{turn.line=text;};",
        _function_decl(source, "_syncLiveAssistantMemoryRecall"),
        _function_decl(source, "restoreLiveTurnHtmlForSession"),
        "restoreLiveTurnHtmlForSession('s');process.stdout.write(JSON.stringify(restored.line));",
    )
    assert got == shown
