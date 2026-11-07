"""Repeated terminal heartbeats must not consume the process's final result.

Events match ProcessRegistry._emit_heartbeat: session_id is the process id,
seq is a positive per-process counter, and output is the new output delta.
The host boundaries (Agent formatter and run launch) are stubbed; both WebUI
queue consumers, routing, dedupe, deferred storage and retry paths run for real.
The live-view regression also runs the native turn-start and SSE publisher.
"""
from __future__ import annotations

import json
from pathlib import Path
import shutil
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from tests._wakeup_helpers import FakeProcessRegistry, install_fake_registry


def _heartbeat(seq=1, *, owner="owner", process_id="proc-heartbeat"):
    return {
        "type": "heartbeat",
        "session_id": process_id,
        "session_key": "session-key",
        "origin_ui_session_id": owner,
        "event_id": f"source-event-{seq}",
        "command": "example-progress-command",
        "seq": seq,
        "interval": 60,
        "elapsed": 60 * seq if type(seq) is int else 60,
        "output": f"progress-{seq}",
        "omitted": 0,
        "started_at": 1.0,
    }


def _completion():
    return {
        "type": "completion",
        "session_id": "proc-heartbeat",
        "session_key": "session-key",
        "origin_ui_session_id": "owner",
        "command": "example-progress-command",
        "exit_code": 0,
        "output": "finished",
    }


@pytest.fixture
def host(monkeypatch):
    from api import background_process as bp, config as cfg, process_event_utils as peu
    from api import streaming as st

    registry = FakeProcessRegistry()
    registry.register("proc-heartbeat", "session-key")
    install_fake_registry(monkeypatch, registry)
    formatted = []

    def agent_format(evt):
        formatted.append(evt)
        return f"heartbeat #{evt['seq']}: {evt['output']}"

    monkeypatch.setattr(
        sys.modules["tools.process_registry"], "format_process_notification",
        agent_format, raising=False,
    )
    for name in (
        "PROCESS_SESSION_INDEX", "BG_TASK_COMPLETE_EVENTS_SEEN",
        "DEFERRED_PROCESS_WAKEUPS", "ACTIVE_RUNS", "STREAMS", "STREAM_SESSION_OWNERS",
    ):
        monkeypatch.setattr(cfg, name, {})
    monkeypatch.setattr(cfg, "PENDING_BG_TASK_COMPLETIONS", set())
    monkeypatch.setattr(st, "PROCESS_SESSION_INDEX", cfg.PROCESS_SESSION_INDEX)
    monkeypatch.setattr(peu, "_HEARTBEAT_DELIVERIES", {})
    bp.register_process_session("session-key", "owner")
    calls, emits = [], []

    def start(session_id, prompt, **kwargs):
        calls.append((session_id, prompt, kwargs))

    def emit(session_id, event, payload):
        emits.append((session_id, event, payload))
        return 1

    real_start = bp._start_server_side_wakeup_turn
    monkeypatch.setattr(bp, "_start_server_side_wakeup_turn", start)
    monkeypatch.setattr(bp, "_emit_to_session_streams", emit)
    monkeypatch.setattr(bp, "_EMIT_COALESCE_WINDOW_SECS", 0.0)
    monkeypatch.setattr(bp, "_LAST_EMIT_TS", {})
    return SimpleNamespace(
        bp=bp, cfg=cfg, st=st, peu=peu, registry=registry, calls=calls,
        emits=emits, formatted=formatted, real_start=real_start,
    )


def _deliver(host, lane, evt):
    if lane == "background":
        before = len(host.calls)
        host.bp._process_one(evt)
        return [prompt for _, prompt, _ in host.calls[before:]]
    host.registry.completion_queue.put(evt)
    return host.st._drain_webui_process_notifications("owner")


@pytest.mark.parametrize("lane", ["background", "next-turn"])
def test_heartbeat_uses_agent_formatter(host, lane):
    evt = _heartbeat()
    formatter = (
        host.bp.format_wakeup_prompt if lane == "background"
        else host.st._format_process_notification
    )
    assert formatter(evt) == "heartbeat #1: progress-1"
    assert host.formatted == [evt]


@pytest.mark.parametrize("busy", [False, True])
def test_heartbeat_bypasses_completion_publisher_but_starts_native_live_view(
    host, monkeypatch, tmp_path, busy,
):
    from api import routes

    class InlineThread:
        def __init__(self, *, target, **kwargs):
            self.target = target

        def start(self):
            self.target()

    launched = []

    def start_run(session, **kwargs):
        launched.append(kwargs)
        return {"_status": 200, "stream_id": f"stream-{len(launched)}"}

    monkeypatch.setattr(host.bp.threading, "Thread", InlineThread)
    monkeypatch.setattr(host.bp, "_start_server_side_wakeup_turn", host.real_start)
    monkeypatch.setattr(host.bp, "SESSION_CHANNELS", {})
    monkeypatch.setattr(routes, "run_admission_closed_response", lambda: None)
    monkeypatch.setattr(routes, "_agent_runtime_barrier_response", lambda **kw: None)
    monkeypatch.setattr(routes, "get_session", lambda sid: SimpleNamespace(
        session_id=sid, model="test-model", model_provider=None,
    ))
    monkeypatch.setattr(routes, "_resolve_chat_workspace_with_recovery", lambda s, w: str(tmp_path))
    monkeypatch.setattr(routes, "_read_profile_model_config", lambda *args: (None, None, {}))
    monkeypatch.setattr(routes, "_resolve_compatible_session_model_state", lambda *a, **kw: (
        "test-model", None, False,
    ))
    monkeypatch.setattr(routes, "_start_run", start_run)
    publisher = Mock(wraps=host.bp._emit_bg_task_complete_events_coalesced)
    monkeypatch.setattr(host.bp, "_emit_bg_task_complete_events_coalesced", publisher)
    channel = host.bp.get_or_create_session_channel("owner")
    subscriber = channel.subscribe()
    try:
        if busy:
            host.cfg.ACTIVE_RUNS["active"] = {"session_id": "owner", "stream_id": "active"}
        for evt in (_heartbeat(), _heartbeat(), _heartbeat(2)):
            host.bp._process_one(evt)
        # This is the FIRST completion-SSE boundary, before coalescing or either
        # alias: publishing progress here makes messages.js toast "Task … done".
        publisher.assert_not_called()
        assert host.emits == []
        assert host.cfg.PENDING_BG_TASK_COMPLETIONS == {"owner"}
        assert not host.registry.is_completion_consumed("proc-heartbeat")
        if busy:
            assert launched == [] and subscriber.empty()
            assert [entry["process_id"] for entry in host.cfg.DEFERRED_PROCESS_WAKEUPS["owner"]] == [
                "proc-heartbeat:heartbeat:1", "proc-heartbeat:heartbeat:2",
            ]
            host.cfg.ACTIVE_RUNS.clear()
            assert host.bp.drain_deferred_wakeups_for_session("owner") == 1
            assert host.bp.drain_deferred_wakeups_for_session("owner") == 1
        assert [call["msg"] for call in launched] == [
            "heartbeat #1: progress-1", "heartbeat #2: progress-2",
        ]
        assert [call["process_id"] for call in launched] == [
            "proc-heartbeat:heartbeat:1", "proc-heartbeat:heartbeat:2",
        ]
        # Keep the real start_session_turn -> SessionChannel publication: an
        # open tab attaches to native turns, without a completion toast/ACK.
        for stream_id in ("stream-1", "stream-2"):
            event, payload = subscriber.get_nowait()
            assert event == "server_turn_started"
            assert payload["session_id"] == "owner" and payload["stream_id"] == stream_id
            assert payload["source"] == "process_wakeup"
        assert subscriber.empty()
        publisher.assert_not_called()

        host.bp._process_one(_completion())
        publisher.assert_called_once()
        sid, payload = publisher.call_args.args
        assert sid == "owner" and payload["task_id"] == "proc-heartbeat"
        assert "completed (exit_code=0)" in payload["summary"]
        assert "finished" in launched[-1]["msg"]
        assert launched[-1]["process_id"] == "proc-heartbeat"
        assert [(sid, event) for sid, event, _ in host.emits] == [
            ("owner", "bg_task_complete"), ("owner", "process_complete"),
        ]
        assert all(data == payload for _, _, data in host.emits)
        assert host.registry.is_completion_consumed("proc-heartbeat")
        host.bp._process_one(_completion())
        publisher.assert_called_once()
    finally:
        channel.unsubscribe(subscriber)


@pytest.mark.skipif(shutil.which("node") is None, reason="node not on PATH")
def test_browser_completion_handler_only_receives_final_completion(host):
    for evt in (_heartbeat(), _heartbeat(2), _completion()):
        host.bp._process_one(evt)
    # Execute the shipped handler using the existing quiet-delegation test
    # pattern, with actual publisher payloads and both canonical/legacy emits.
    script = r"""
const fs = require('fs');
const src = fs.readFileSync(process.argv[1], 'utf8');
const start = src.indexOf('function _handleBgTaskCompleteEvent(');
if (start < 0) throw new Error('completion handler missing');
const brace = src.indexOf('{', start);
let depth = 0, end = 0;
for (let i = brace; i < src.length; i++) {
  if (src[i] === '{') depth++;
  else if (src[i] === '}' && !--depth) { end = i + 1; break; }
}
const toast = [], ack = [], seen = new Set();
function _bgTaskCompleteRingBufferAdd(sid, id) {
  const key = sid + ':' + id;
  if (seen.has(key)) return true;
  seen.add(key); return false;
}
function _isSessionActivelyViewed() { return false; }
function _apiUrl(path) { return path; }
function showToast(...args) { toast.push(args); }
function fetch(path, opts) { ack.push(JSON.parse(opts.body)); return Promise.resolve(); }
eval(src.slice(start, end));
for (const [sid, event, payload] of JSON.parse(process.argv[2])) {
  _handleBgTaskCompleteEvent({data: JSON.stringify(payload)}, sid, {});
}
console.log(JSON.stringify({toast, ack}));
"""
    node = shutil.which("node")
    assert node is not None
    result = subprocess.run(
        [node, "-e", script,
         str(Path(__file__).resolve().parents[1] / "static" / "messages.js"),
         json.dumps(host.emits)],
        capture_output=True, text=True, timeout=30, check=False,
    )
    assert result.returncode == 0, result.stderr
    outcome = json.loads(result.stdout)
    assert len(outcome["toast"]) == 1, "progress must never become a 'Task … done' toast"
    assert outcome["toast"] == [[
        "Task proc-hea done: IMPORTANT: Background process proc-heartbeat completed (exit_code=0).",
        2600,
    ]]
    assert len(outcome["ack"]) == 1
    assert outcome["ack"][0]["task_id"] == "proc-heartbeat"


@pytest.mark.parametrize("first_lane", ["background", "next-turn"])
def test_repeated_sequences_and_completion_share_both_drains(host, first_lane):
    other_lane = "next-turn" if first_lane == "background" else "background"
    first = _heartbeat()
    assert _deliver(host, first_lane, first) == ["heartbeat #1: progress-1"]
    assert not host.registry.is_completion_consumed("proc-heartbeat")
    # Identity is process + seq, not a newly minted transport event_id.
    duplicate = dict(first, event_id="duplicate-transport-id")
    assert _deliver(host, other_lane, duplicate) == []
    assert _deliver(host, other_lane, _heartbeat(2)) == ["heartbeat #2: progress-2"]
    assert _deliver(host, first_lane, _heartbeat(2)) == []
    assert not host.registry.is_completion_consumed("proc-heartbeat")
    assert len(_deliver(host, other_lane, _completion())) == 1
    assert host.registry.is_completion_consumed("proc-heartbeat")
    assert _deliver(host, first_lane, _completion()) == []
    payloads = [payload for _, event, payload in host.emits if event == "bg_task_complete"]
    assert len(payloads) == (1 if other_lane == "background" else 0)
    assert all("completed (exit_code=0)" in payload["summary"] for payload in payloads)
    assert all(payload["task_id"] == "proc-heartbeat" for payload in payloads)
    assert len({payload["event_id"] for payload in payloads}) == len(payloads)


def test_next_turn_keeps_other_owner_queued_and_accepts_out_of_order_ticks(host):
    events = [_heartbeat(2), _heartbeat(), _heartbeat(2), _heartbeat(owner="other"), _completion()]
    for evt in events:
        host.registry.completion_queue.put(evt)
    notes = host.st._drain_webui_process_notifications("owner")
    assert notes[:2] == ["heartbeat #2: progress-2", "heartbeat #1: progress-1"]
    assert len(notes) == 3
    assert "finished" in notes[-1]
    assert host.registry.completion_queue.get_nowait() == events[-2]
    assert host.registry.completion_queue.empty()


@pytest.mark.parametrize("lane", ["background", "next-turn"])
def test_exact_origin_overrides_stale_session_key_index(host, lane):
    host.bp.register_process_session("session-key", "wrong-owner")
    evt = _heartbeat()
    if lane == "next-turn":
        host.registry.completion_queue.put(evt)
        assert host.st._drain_webui_process_notifications("wrong-owner") == []
        assert host.st._drain_webui_process_notifications("owner") == ["heartbeat #1: progress-1"]
    else:
        assert _deliver(host, lane, evt) == ["heartbeat #1: progress-1"]
        assert host.calls[0][0] == "owner"
    assert not host.registry.is_completion_consumed("proc-heartbeat")


def test_busy_turn_defers_each_tick_and_completion_without_overlap(host):
    host.cfg.ACTIVE_RUNS["active"] = {"session_id": "owner", "stream_id": "active"}
    for evt in [_heartbeat(), _heartbeat(), _heartbeat(2), _completion()]:
        host.bp._process_one(evt)
    assert host.calls == []
    entries = host.cfg.DEFERRED_PROCESS_WAKEUPS["owner"]
    assert len(entries) == 3
    assert len({entry["process_id"] for entry in entries}) == 3
    assert not host.registry.is_completion_consumed("proc-heartbeat")
    assert host.bp.drain_deferred_wakeups_for_session("owner") == 0
    host.cfg.ACTIVE_RUNS.clear()
    for expected in ["heartbeat #1: progress-1", "heartbeat #2: progress-2", "finished"]:
        before = len(host.calls)
        assert host.bp.drain_deferred_wakeups_for_session("owner") == 1
        assert len(host.calls) == before + 1
        assert expected in host.calls[-1][1]
    assert host.bp.drain_deferred_wakeups_for_session("owner") == 0
    assert "owner" not in host.cfg.DEFERRED_PROCESS_WAKEUPS
    assert "owner" not in host.cfg.PENDING_BG_TASK_COMPLETIONS
    # Deferred deliveries and a duplicate in either queue lane cannot re-fire.
    assert _deliver(host, "next-turn", _heartbeat()) == []


def test_reading_final_result_does_not_drop_deferred_output_ticks(host):
    host.cfg.ACTIVE_RUNS["active"] = {"session_id": "owner", "stream_id": "active"}
    for evt in [_heartbeat(), _heartbeat(2), _completion()]:
        host.bp._process_one(evt)
    host.registry._completion_consumed.add("proc-heartbeat")
    host.cfg.ACTIVE_RUNS.clear()
    assert host.bp.drain_deferred_wakeups_for_session("owner") == 1
    assert host.bp.drain_deferred_wakeups_for_session("owner") == 1
    assert host.bp.drain_deferred_wakeups_for_session("owner") == 0
    assert [prompt for _, prompt, _ in host.calls] == [
        "heartbeat #1: progress-1", "heartbeat #2: progress-2",
    ]


@pytest.mark.parametrize("seq", [None, True, 0, -1, "1", 1.0])
@pytest.mark.parametrize("lane", ["background", "next-turn"])
def test_invalid_heartbeat_identity_never_consumes_completion(host, seq, lane):
    assert _deliver(host, lane, _heartbeat(seq)) == []
    assert not host.registry.is_completion_consumed("proc-heartbeat")
    assert len(_deliver(host, lane, _completion())) == 1


@pytest.mark.parametrize("first_lane", ["background", "next-turn"])
def test_heartbeat_duplicate_survives_1024_unrelated_owner_ticks(host, first_lane):
    other_lane = "next-turn" if first_lane == "background" else "background"
    first = _heartbeat()
    assert _deliver(host, first_lane, first) == ["heartbeat #1: progress-1"]
    assert _deliver(host, other_lane, first) == []
    for seq in range(1, 1025):
        evt = _heartbeat(seq, owner="other", process_id="other-process")
        assert host.peu.claim_heartbeat_delivery(evt, "other")
    # Unrelated traffic must not expire the original active process's claim.
    assert _deliver(host, other_lane, first) == []
    assert _deliver(host, other_lane, _heartbeat(2)) == ["heartbeat #2: progress-2"]
    assert not host.registry.is_completion_consumed("proc-heartbeat")
    assert len(_deliver(host, first_lane, _completion())) == 1


def test_heartbeat_claims_are_compact_for_large_contiguous_stream(host):
    count = 100_000
    for seq in range(1, count + 1):
        assert host.peu.claim_heartbeat_delivery(_heartbeat(seq), "owner")
    assert host.peu._HEARTBEAT_DELIVERIES == {
        "owner": {"proc-heartbeat": [(1, count)]},
    }
    for seq in (1, count // 2, count):
        assert host.peu.claim_heartbeat_delivery(_heartbeat(seq), "owner") == ""
    # A later arrival can leave a real hole; filling it must still be delivered
    # once, then merge the adjacent ranges instead of retaining per-tick state.
    assert host.peu.claim_heartbeat_delivery(_heartbeat(count + 2), "owner")
    assert host.peu._HEARTBEAT_DELIVERIES["owner"]["proc-heartbeat"] == [
        (1, count), (count + 2, count + 2),
    ]
    assert host.peu.claim_heartbeat_delivery(_heartbeat(count + 1), "owner")
    assert host.peu.claim_heartbeat_delivery(_heartbeat(count + 1), "owner") == ""
    assert host.peu._HEARTBEAT_DELIVERIES["owner"]["proc-heartbeat"] == [(1, count + 2)]
    assert host.registry._completion_consumed == set()
    assert host.cfg.BG_TASK_COMPLETE_EVENTS_SEEN == {}


def test_out_of_order_heartbeat_ranges_accept_each_distinct_tick_once(host):
    for index, seq in enumerate([10, 4, 8, 6, 2, 5, 3, 9, 7, 1, 12, 11]):
        lane, other_lane = ("background", "next-turn") if index % 2 else ("next-turn", "background")
        assert _deliver(host, lane, _heartbeat(seq)) == [f"heartbeat #{seq}: progress-{seq}"]
        assert _deliver(host, other_lane, _heartbeat(seq)) == []
    assert host.peu._HEARTBEAT_DELIVERIES["owner"]["proc-heartbeat"] == [(1, 12)]
    assert not host.registry.is_completion_consumed("proc-heartbeat")
    assert len(_deliver(host, "next-turn", _completion())) == 1
    # Completion must not forget claimed ticks or discard queued distinct ones.
    assert _deliver(host, "background", _heartbeat(1)) == []
    assert _deliver(host, "background", _heartbeat(13)) == ["heartbeat #13: progress-13"]


def test_session_deletion_releases_only_its_exact_owner_heartbeat_records(host):
    for owner in ("owner", "owner-other"):
        for process_id in ("proc-heartbeat", "other-process"):
            evt = _heartbeat(owner=owner, process_id=process_id)
            assert host.peu.claim_heartbeat_delivery(evt, owner)
            assert host.peu.claim_heartbeat_delivery(evt, owner) == ""
    host.bp.forget_bg_task_completion_dedup("owner")
    assert host.peu._HEARTBEAT_DELIVERIES == {
        "owner-other": {"proc-heartbeat": [(1, 1)], "other-process": [(1, 1)]},
    }
    for process_id in ("proc-heartbeat", "other-process"):
        evt = _heartbeat(owner="owner-other", process_id=process_id)
        assert host.peu.claim_heartbeat_delivery(evt, "owner-other") == ""
        assert host.peu.claim_heartbeat_delivery(_heartbeat(process_id=process_id), "owner")
    host.bp.forget_bg_task_completion_dedup("owner")
    host.bp.forget_bg_task_completion_dedup("owner-other")
    assert host.peu._HEARTBEAT_DELIVERIES == {}


def test_missing_agent_formatter_does_not_poison_completion(host, monkeypatch):
    monkeypatch.delattr(sys.modules["tools.process_registry"], "format_process_notification")
    assert _deliver(host, "background", _heartbeat()) == []
    assert not host.registry.is_completion_consumed("proc-heartbeat")
    assert len(_deliver(host, "next-turn", _completion())) == 1


@pytest.mark.parametrize("failure", [409, 500, "exception"])
def test_rejected_heartbeat_dispatch_retries_only_its_own_identity(host, monkeypatch, failure):
    from api import routes

    class InlineThread:
        def __init__(self, *, target, **kwargs):
            self.target = target

        def start(self):
            self.target()

    attempts = []

    def start(session_id, prompt, **kwargs):
        attempts.append((session_id, prompt, kwargs))
        if len(attempts) == 1:
            if failure == "exception":
                raise RuntimeError("test launch rejection")
            return {"_status": failure, "error": "test launch rejection"}
        return {"_status": 200, "stream_id": "accepted"}

    monkeypatch.setattr(host.bp.threading, "Thread", InlineThread)
    monkeypatch.setattr(host.bp, "_start_server_side_wakeup_turn", host.real_start)
    monkeypatch.setattr(routes, "start_session_turn", start)
    host.bp._process_one(_heartbeat())
    deferred = host.cfg.DEFERRED_PROCESS_WAKEUPS["owner"]
    assert len(deferred) == 1
    first_id = deferred[0]["process_id"]
    assert first_id != "proc-heartbeat"
    assert attempts[0][2]["process_id"] == first_id
    assert not host.registry.is_completion_consumed("proc-heartbeat")
    # The launch-abort re-arm and the dispatcher's rejection handler may both
    # queue this same delivery. The threaded id must preserve seq in that lane
    # too; retry_attempt=1 keeps the verification free of real retry timers.
    routes._rearm_process_wakeup_after_launch_failure(
        "owner", "failed-launch", wakeup_prompt=attempts[0][1],
        process_id=first_id, retry_attempt=1,
    )
    assert len(host.cfg.DEFERRED_PROCESS_WAKEUPS["owner"]) == 1

    host.cfg.ACTIVE_RUNS["active"] = {"session_id": "owner", "stream_id": "active"}
    for evt in [_heartbeat(), _heartbeat(2), _completion()]:
        host.bp._process_one(evt)
    assert len(host.cfg.DEFERRED_PROCESS_WAKEUPS["owner"]) == 3
    assert len(attempts) == 1
    host.cfg.ACTIVE_RUNS.clear()
    for _ in range(3):
        assert host.bp.drain_deferred_wakeups_for_session("owner") == 1
    assert attempts[1][2]["process_id"] == first_id
    assert [prompt for _, prompt, _ in attempts[1:3]] == [
        "heartbeat #1: progress-1", "heartbeat #2: progress-2",
    ]
    assert "finished" in attempts[3][1]
    assert host.bp.drain_deferred_wakeups_for_session("owner") == 0
    assert host.cfg.DEFERRED_PROCESS_WAKEUPS == {}


def test_concurrent_drains_claim_same_heartbeat_once(host):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    barrier = Barrier(2)

    def deliver(lane):
        barrier.wait(timeout=5)
        return _deliver(host, lane, _heartbeat())

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(deliver, ["background", "next-turn"]))
    assert [note for batch in results for note in batch] == ["heartbeat #1: progress-1"]
    assert not host.registry.is_completion_consumed("proc-heartbeat")
    assert len(_deliver(host, "next-turn", _completion())) == 1


def test_real_agent_heartbeat_schema_and_output_tail(monkeypatch, request):
    # Optional in WebUI-only CI; the local source-backed run exercises the real
    # producer and formatter, not a hand-reconstructed copy of their schema.
    core = pytest.importorskip("tools.process_registry")
    if not hasattr(core.ProcessRegistry, "_emit_heartbeat"):
        pytest.skip("Agent version predates terminal heartbeats")
    tools_package = sys.modules["tools"]
    host = request.getfixturevalue("host")
    # Keep the producer's internal imports real as well as its formatter.
    monkeypatch.setitem(sys.modules, "tools", tools_package)
    monkeypatch.setitem(sys.modules, "tools.process_registry", core)
    registry = core.ProcessRegistry()
    monkeypatch.setattr(core, "process_registry", registry)
    host.registry = registry
    process = core.ProcessSession(
        id="proc-heartbeat", command="example-progress-command",
        session_key="session-key", started_at=1.0, heartbeat_seconds=60,
    )
    registry._running[process.id] = process
    process.append_output("x" * 20000 + "\nlatest progress\n")
    registry._emit_heartbeat(process, 61.0)
    first = registry.completion_queue.get_nowait()
    assert first["seq"] == 1 and first["interval"] == 60 and first["elapsed"] == 60
    assert first["omitted"] > 0
    expected = core.format_process_notification(first)
    assert "earlier characters omitted" in expected
    assert "latest progress" in expected
    assert _deliver(host, "background", first) == [expected]
    process.append_output("second progress\n")
    registry._emit_heartbeat(process, 121.0)
    second = registry.completion_queue.get_nowait()
    assert second["seq"] == 2 and second["output"] == "second progress\n"
    assert _deliver(host, "next-turn", second) == [core.format_process_notification(second)]
    registry._emit_heartbeat(process, 181.0)
    assert registry.completion_queue.empty()  # Quiet ticks are the producer's policy.
    assert not host.registry.is_completion_consumed("proc-heartbeat")
    assert len(_deliver(host, "next-turn", _completion())) == 1
