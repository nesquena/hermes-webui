#!/usr/bin/env python3
"""Deterministic browser harness for active-session switch-back paint ordering."""

import json
import os
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

PORT = int(os.getenv("BENCH_PORT", "8789"))
BASE = f"http://127.0.0.1:{PORT}"

ACTIVE_SESSION_ID = "bench-session-active-switch"
IDLE_SESSION_ID = "bench-session-active-switch-idle"
ACTIVE_STREAM_ID = "bench-stream-active-switch"
VISIBLE_MESSAGES = 30
TOTAL_MESSAGES = 4410
TOTAL_TOOL_CALLS = 2882
RUNTIME_ACTIVITY_ROWS = 424
EXPECTED_FINAL_SCENE_ROWS = 129
TARGET_INFLIGHT_LAST_SEQ = RUNTIME_ACTIVITY_ROWS + 25
REPO_ROOT = Path(__file__).resolve().parent.parent
WORK_ROOT = Path(os.getenv("BENCH_WORK_ROOT", str(REPO_ROOT / ".benchmarks" / "session-switch-active-render")))
REPORT_ROOT = Path(os.getenv("BENCH_REPORT_ROOT", str(WORK_ROOT / "final")))


def _fixture_timestamp(i: int) -> int:
    return 1_760_000_000_000 + (i * 60_000)


def _build_messages() -> list[dict]:
    return [
        {
            "role": "user" if (i % 2 == 0) else "assistant",
            "content": f"Synthetic message row {i + 1:04d}",
            "timestamp": _fixture_timestamp(i),
            "msg_idx": i,
            "msg_id": f"bench-msg-{i:05d}",
        }
        for i in range(TOTAL_MESSAGES)
    ]


_MESSAGES = _build_messages()


def _build_tool_calls() -> list[dict]:
    out: list[dict] = []
    for i in range(TOTAL_TOOL_CALLS):
        out.append(
            {
                "tool_call_id": f"bench-tool-call-{i:05d}",
                "id": f"bench-tool-call-{i:05d}",
                "name": "synth_tool",
                "type": "tool_call",
                "args": {"value": i},
                "status": "running" if i < 75 else "ok",
                "order": i,
            }
        )
    return out


_TOOL_CALLS = _build_tool_calls()


def _build_runtime_activity_rows() -> list[dict]:
    out: list[dict] = []
    for i in range(RUNTIME_ACTIVITY_ROWS):
        if i < 75:
            source_event_type = "tool_started"
            role = "tool"
            status = "running"
            text = f"tool-start {i:04d}"
        elif i < 149:
            source_event_type = "tool_completed"
            role = "tool"
            status = "ok"
            text = f"tool-complete {i:04d}"
        elif i < 203:
            source_event_type = "reasoning"
            role = "assistant"
            status = ""
            text = f"reasoning text {i:04d}"
        elif i < 214:
            source_event_type = "prose"
            role = "assistant"
            status = ""
            text = f"interim prose {i:04d}"
        elif i < 218:
            source_event_type = "compressing"
            role = "lifecycle"
            status = "running"
            text = "compressing"
        else:
            source_event_type = "lifecycle"
            role = "lifecycle"
            status = "done"
            text = f"activity {i:04d}"
        row = {
            "row_id": f"bench-runtime-row-{i:04d}",
            "local_id": f"bench-runtime-row-{i:04d}",
            "source_event_type": source_event_type,
            "role": role,
            "text": text,
            "status": status,
            "ts": _fixture_timestamp(TOTAL_MESSAGES + i),
            "anchor_event_id": f"bench-runtime-event-{i:04d}",
        }
        if role == "tool":
            row["tool"] = {
                "id": f"tool-{i:04d}",
                "name": "synth_tool",
                "tool_call_id": f"bench-tool-call-{i % TOTAL_TOOL_CALLS:05d}",
            }
        out.append(row)
    return out


_RUNTIME_ACTIVITY_ROWS = _build_runtime_activity_rows()


def _build_anchor_activity_rows() -> list[dict]:
    out: list[dict] = []
    for i in range(EXPECTED_FINAL_SCENE_ROWS):
        tool_call_id = f"bench-tool-call-{i:05d}"
        row = {
            "role": "tool",
            "source_event_type": "tool_started",
            "row_id": f"bench-anchor-row-{i:04d}",
            "local_id": f"bench-anchor-row-{i:04d}",
            "text": f"tool call {i}",
            "status": "running" if i < 75 else "ok",
            "tool": {
                "id": f"bench-anchor-tool-{i:04d}",
                "name": "synth_tool",
                "tool_call_id": tool_call_id,
                "status": "running" if i < 75 else "ok",
            },
        }
        out.append(row)
    return out


_ANCHOR_ACTIVITY_ROWS = _build_anchor_activity_rows()


def _runtime_snapshot() -> dict:
    return {
        "stream_id": ACTIVE_STREAM_ID,
        "session_id": ACTIVE_SESSION_ID,
        "active": True,
        "identity": {
            "stream_id": ACTIVE_STREAM_ID,
            "session_id": ACTIVE_SESSION_ID,
        },
        "activity_rows": _RUNTIME_ACTIVITY_ROWS,
        "last_seq": RUNTIME_ACTIVITY_ROWS,
        "last_event_id": f"bench-runtime-event-{RUNTIME_ACTIVITY_ROWS-1:04d}",
        "tool_count": TOTAL_TOOL_CALLS,
        "tool_calls": _TOOL_CALLS,
        "activity_burst_anchors": [
            {"id": i, "textEnd": (i + 1) * 120}
            for i in range(1, 11)
        ],
        "anchor_activity_scene": {
            "version": "activity_scene_v1",
            "stream_id": ACTIVE_STREAM_ID,
            "session_id": ACTIVE_SESSION_ID,
            "activity_rows": _ANCHOR_ACTIVITY_ROWS,
            "tool_calls": [],
            "activity_burst_id": 0,
            "last_seq": EXPECTED_FINAL_SCENE_ROWS,
            "identity": {
                "stream_id": ACTIVE_STREAM_ID,
                "session_id": ACTIVE_SESSION_ID,
            },
        },
    }


def _clamp_int(value, fallback: int = 0) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return fallback


def _session_payload(
    active: bool = True,
    messages_requested: int = 0,
    messages_limit: int = 0,
    messages_before: int = 0,
) -> dict:
    if active:
        limit = max(0, int(messages_requested or 0))
        limit = max(0, int(messages_limit or 0)) or limit

        messages = []
        truncated = False
        offset = 0

        if limit <= 0:
            messages = []
        elif messages_before > 0:
            end = min(messages_before, len(_MESSAGES))
            start = max(0, end - limit)
            messages = _MESSAGES[start:end]
            offset = start
            truncated = start > 0
        else:
            limit = min(limit, len(_MESSAGES))
            messages = _MESSAGES[-limit:]
            offset = len(_MESSAGES) - limit
            truncated = offset > 0

        payload = {
            "session_id": ACTIVE_SESSION_ID,
            "title": "Benchmark active session",
            "message_count": TOTAL_MESSAGES,
            "tool_call_count": TOTAL_TOOL_CALLS,
            "active_stream_id": ACTIVE_STREAM_ID,
            "stream_id": ACTIVE_STREAM_ID,
            "last_message_at": _fixture_timestamp(TOTAL_MESSAGES),
            "updated_at": _fixture_timestamp(TOTAL_MESSAGES + 1),
            "model": "benchmark-model",
            "model_provider": "benchmark-provider",
            "context_length": 180000,
            "threshold_tokens": 120000,
            "runtime_journal_snapshot": _runtime_snapshot(),
            "tool_calls": _TOOL_CALLS,
            "pending_attachments": [],
            "messages": messages,
        }
        if limit and limit < TOTAL_MESSAGES:
            payload["_messages_truncated"] = truncated
            payload["_messages_offset"] = offset
        return payload

    return {
        "session_id": IDLE_SESSION_ID,
        "title": "Benchmark idle session",
        "message_count": 2,
        "tool_call_count": 0,
        "active_stream_id": None,
        "stream_id": None,
        "last_message_at": _fixture_timestamp(3),
        "updated_at": _fixture_timestamp(4),
        "model": "benchmark-model",
        "model_provider": "benchmark-provider",
        "context_length": 4096,
        "threshold_tokens": 12288,
        "pending_attachments": [],
        "tool_calls": [
            {
                "tool_call_id": "idle-tool-001",
                "id": "idle-tool-001",
                "name": "idle_tool",
                "status": "ok",
            }
        ],
        "messages": [
            {
                "role": "user",
                "content": "Idle benchmark start",
                "timestamp": _fixture_timestamp(1),
                "msg_idx": 0,
                "msg_id": "idle-msg-1",
            },
            {
                "role": "assistant",
                "content": "Idle benchmark assistant",
                "timestamp": _fixture_timestamp(2),
                "msg_idx": 1,
                "msg_id": "idle-msg-2",
            },
        ],
    }


def _fixture_description() -> dict:
    return {
        "active_session_id": ACTIVE_SESSION_ID,
        "active_stream_id": ACTIVE_STREAM_ID,
        "message_count": TOTAL_MESSAGES,
        "visible_messages": VISIBLE_MESSAGES,
        "tool_call_count": TOTAL_TOOL_CALLS,
        "runtime_activity_rows": RUNTIME_ACTIVITY_ROWS,
        "expected_final_scene_rows": EXPECTED_FINAL_SCENE_ROWS,
        "mobile_emulation": "Pixel 5",
    }


def _snapshot_session_seed(page, sid: str) -> dict:
    return page.evaluate(
        """
        (sid) => {
          const targetSid = String(sid || '');
          const session = (typeof S !== 'undefined' && S && S.session && String(S.session.session_id || '') === targetSid)
            ? S.session
            : null;
          const runtimeJournalSnapshot = session && session.runtime_journal_snapshot ? session.runtime_journal_snapshot : null;
          const toolCalls = Array.isArray(session && session.tool_calls)
            ? session.tool_calls
            : (Array.isArray(runtimeJournalSnapshot && runtimeJournalSnapshot.tool_calls)
                ? runtimeJournalSnapshot.tool_calls
                : []);
          const messages = Array.isArray(S && S.messages) ? S.messages : [];
          if (!session) {
            return {
              sid: targetSid,
              session: null,
              runtime_journal_snapshot: null,
              tool_calls: [],
              messages: [],
            };
          }
          return {
            sid: targetSid,
            session: JSON.parse(JSON.stringify(session)),
            runtime_journal_snapshot: runtimeJournalSnapshot ? JSON.parse(JSON.stringify(runtimeJournalSnapshot)) : null,
            tool_calls: JSON.parse(JSON.stringify(toolCalls)),
            messages: JSON.parse(JSON.stringify(messages)),
          };
        }
        """,
        sid,
    )


def _query_inflight_state(page, sid: str) -> dict:
    return page.evaluate(
        """
        (sid) => {
          const inflight = (typeof INFLIGHT !== 'undefined' && INFLIGHT && INFLIGHT[sid]) ? INFLIGHT[sid] : null;
          const scene = inflight && inflight.anchorActivityScene ? inflight.anchorActivityScene : null;
          const liveTurn = typeof document !== 'undefined' ? document.getElementById('liveAssistantTurn') : null;
          const liveRowCount = liveTurn
            ? liveTurn.querySelectorAll('[data-anchor-scene-row=\"1\"]').length
            : 0;
          return {
            exists: !!inflight,
            hasMessageState: !!(inflight && Array.isArray(inflight.messages) && inflight.messages.length),
            reattach: !!(inflight && inflight.reattach),
            journalReplayFromStart: !!(inflight && inflight.journalReplayFromStart),
            streamId: inflight ? String(inflight.streamId || '') : '',
            lastRunJournalSeq: inflight ? Number(inflight.lastRunJournalSeq || 0) : 0,
            hasAnchorActivityScene:
              !!(inflight && inflight.anchorActivityScene && inflight.anchorActivityScene.version === 'activity_scene_v1'),
            anchorRows: !!scene && Array.isArray(scene.activity_rows)
              ? scene.activity_rows.length
              : 0,
            liveSceneRows: liveRowCount,
            toolCallCount: inflight && Array.isArray(inflight.toolCalls) ? inflight.toolCalls.length : 0,
            activeStreamId: (() => {
              try { return String(S && S.activeStreamId || ''); } catch (_) { return ''; }
            })(),
            sessionStreamId: (() => {
              try { return String(S && S.session && S.session.active_stream_id || ''); } catch (_) { return ''; }
            })(),
            sessionId: (() => {
              try { return String(S && S.session && S.session.session_id || ''); } catch (_) { return ''; }
            })(),
            liveStreams: (typeof LIVE_STREAMS !== 'undefined' && LIVE_STREAMS) ? Object.keys(LIVE_STREAMS) : [],
            hasActiveStream: !!(typeof S !== 'undefined' && S && S.activeStreamId),
          };
        }
        """,
        sid,
    )


def _inject_synthetic_inflight(page, sid: str, stream_id: str, base_seq: int, seed_state: dict | None = None) -> dict:
    return page.evaluate(
        """
        (args) => {
          const sid = String((args && args.sid) || '');
          const streamId = String((args && args.streamId) || '');
          const baseSeq = Number(args && args.baseSeq);
          const seedState = args && args.seedState ? args.seedState : null;
          const targetSid = sid;
          const normalizedSeed = (seedState && typeof seedState === 'object') ? seedState : {};
          const sessionFromSeed = normalizedSeed.session && typeof normalizedSeed.session === 'object'
            ? normalizedSeed.session
            : null;
          const messagesFromSeed = Array.isArray(normalizedSeed.messages) ? normalizedSeed.messages : [];
          const toolCallsFromSeed = Array.isArray(normalizedSeed.tool_calls) ? normalizedSeed.tool_calls : [];
          const session = (typeof S !== 'undefined' && S && S.session && String(S.session.session_id || '') === targetSid)
            ? S.session
            : sessionFromSeed;
          if (!session) {
            return {ok: false, reason: 'session missing for sid'};
          }
          const snapshot = session.runtime_journal_snapshot
            || (typeof normalizedSeed.runtime_journal_snapshot === 'object' ? normalizedSeed.runtime_journal_snapshot : null);
          const scene = snapshot && snapshot.anchor_activity_scene && snapshot.anchor_activity_scene.version === 'activity_scene_v1'
            ? snapshot.anchor_activity_scene
            : null;
          const anchorScene = scene
            ? JSON.parse(JSON.stringify(scene))
            : null;
          const activityAnchors = Array.isArray(snapshot && snapshot.activity_burst_anchors)
            ? JSON.parse(JSON.stringify(snapshot.activity_burst_anchors))
            : [];
          const toolCalls = Array.isArray(toolCallsFromSeed)
            ? JSON.parse(JSON.stringify(toolCallsFromSeed))
            : (Array.isArray(snapshot && snapshot.tool_calls)
              ? JSON.parse(JSON.stringify(snapshot.tool_calls))
              : []);
          const msgs = Array.isArray(messagesFromSeed)
            ? JSON.parse(JSON.stringify(messagesFromSeed))
            : (Array.isArray(S && S.messages) ? JSON.parse(JSON.stringify(S.messages)) : []);
          const sessionAttachments = Array.isArray(session && session.pending_attachments)
            ? session.pending_attachments
            : [];
          const attachments = Array.isArray(sessionAttachments)
            ? JSON.parse(JSON.stringify(session.pending_attachments))
            : [];
          if (typeof INFLIGHT !== 'object' || !INFLIGHT) {
            window.INFLIGHT = {};
          }
          const seq = Number.isFinite(Number(baseSeq)) ? Number(baseSeq) : 0;
          INFLIGHT[sid] = {
            streamId,
            messages: msgs,
            uploaded: attachments,
            toolCalls,
            reattach: true,
            lastAssistantText: String(snapshot && (snapshot.last_assistant_text || snapshot.lastAssistantText || '') || ''),
            lastReasoningText: String(snapshot && (snapshot.last_reasoning_text || snapshot.lastReasoningText || '') || ''),
            lastRunJournalSeq: Math.max(0, seq),
            lastRunJournalEventId: String(snapshot && (snapshot.last_event_id || snapshot.lastEventId || '') || ''),
            journalReplayFromStart: false,
            anchorActivityScene: anchorScene,
            currentActivityBurstId: Number(snapshot && (snapshot.current_activity_burst_id || snapshot.currentActivityBurstId || 0)) || 0,
            currentLiveSegmentSeq: Number(snapshot && (snapshot.current_live_segment_seq || snapshot.currentLiveSegmentSeq || 0)) || 0,
            activityBurstAnchors: activityAnchors,
          };
          if (typeof window.__benchSetCaptureMetric === 'function') {
            window.__benchSetCaptureMetric('synth-inflight=' + sid);
          }
          return {
            ok: true,
            reattach: true,
            streamId,
            lastRunJournalSeq: INFLIGHT[sid].lastRunJournalSeq,
            hasAnchorActivityScene: !!anchorScene,
            toolCallCount: toolCalls.length,
          };
        }
        """,
        {"sid": sid, "streamId": stream_id, "baseSeq": base_seq, "seedState": seed_state},
    )


def _build_report() -> dict:
    return {
        "fixture": _fixture_description(),
        "command": {
            "script": str(Path(__file__).name),
            "bench_port": PORT,
            "bench_server_root": str(REPO_ROOT),
            "samples": 0,
            "capability": {
                "is_mobile": True,
                "touch": True,
                "capture_enabled": False,
                "cpu_throttle": 1,
            },
        },
        "samples": [],
        "median": {},
        "p95": {},
        "errors": [],
        "invalidSamples": 0,
    }


def _build_routes_payload(path: str, qs: dict[str, list[str]], *, include_session_list: bool = True) -> str:
    if path == "/api/sessions":
        if not include_session_list:
            return json.dumps([])
        return json.dumps(
            [
                {
                    "session_id": ACTIVE_SESSION_ID,
                    "message_count": TOTAL_MESSAGES,
                    "active_stream_id": ACTIVE_STREAM_ID,
                    "is_streaming": True,
                },
                {
                    "session_id": IDLE_SESSION_ID,
                    "message_count": 2,
                    "active_stream_id": None,
                    "is_streaming": False,
                },
            ]
        )

    if path == "/api/session":
        sid = (qs.get("session_id") or [""])[0]
        if sid == ACTIVE_SESSION_ID:
            if qs.get("resolve_model", ["0"])[0] == "1":
                return json.dumps({"session": _session_payload(active=True, messages_requested=0)})
            msg_limit = _clamp_int((qs.get("msg_limit") or [0])[0], 0)
            msg_before = _clamp_int((qs.get("msg_before") or qs.get("messages_before") or [0])[0], 0)
            try:
                msg_requested = int(qs.get("messages", ["0"])[0] or 0)
            except ValueError:
                msg_requested = 0
            return json.dumps(
                {
                    "session": _session_payload(
                        active=True,
                        messages_requested=msg_requested,
                        messages_limit=msg_limit,
                        messages_before=msg_before,
                    )
                }
            )

        if sid == IDLE_SESSION_ID:
            if qs.get("resolve_model", ["0"])[0] == "1":
                return json.dumps({"session": _session_payload(active=False, messages_requested=0)})
            msg_limit = _clamp_int((qs.get("msg_limit") or [0])[0], 0)
            msg_before = _clamp_int((qs.get("msg_before") or qs.get("messages_before") or [0])[0], 0)
            try:
                msg_requested = int(qs.get("messages", ["0"])[0] or 0)
            except ValueError:
                msg_requested = 0
            return json.dumps(
                {
                    "session": _session_payload(
                        active=False,
                        messages_requested=msg_requested,
                        messages_limit=msg_limit,
                        messages_before=msg_before,
                    )
                }
            )

        return json.dumps({"session": {"session_id": sid, "message_count": 0, "messages": []}})

    if path == "/api/chat/stream/status":
        stream_id = (qs.get("stream_id") or [""])[0]
        return json.dumps(
            {
                "active": stream_id == ACTIVE_STREAM_ID,
                "stream_id": stream_id or ACTIVE_STREAM_ID,
                "session_id": ACTIVE_SESSION_ID,
                "state": "running" if stream_id == ACTIVE_STREAM_ID else "idle",
            }
        )

    if path == "/api/chat/stream" or path == "/api/session/stream":
        return "retry: 1000\n\n"

    if path == "/health":
        return "{}"

    if path.startswith("/api/"):
        return "{}"

    return None


def _start_server() -> tuple[subprocess.Popen | None, str | None]:
    repo_root = Path(os.getenv("BENCH_SERVER_ROOT", str(REPO_ROOT))).resolve()
    server_py = repo_root / "server.py"
    if not server_py.exists():
        print(f"SETUP FAIL: server.py not found at {server_py}", file=sys.stderr)
        return None, None

    state_dir = tempfile.mkdtemp(prefix="hermes-bench-session-switch-active-render-")
    env = os.environ.copy()
    env.update(
        {
            "HERMES_WEBUI_PORT": str(PORT),
            "HERMES_WEBUI_HOST": "127.0.0.1",
            "HERMES_WEBUI_STATE_DIR": state_dir,
            "HERMES_HOME": state_dir,
            "HERMES_BASE_HOME": state_dir,
            "HERMES_WEBUI_SKIP_ONBOARDING": "1",
            "HERMES_WEBUI_AGENT_DIR": os.path.join(state_dir, "agent"),
        }
    )
    for key in list(env.keys()):
        if key.endswith("_API_KEY"):
            env.pop(key, None)

    logfile = Path(state_dir) / "server.log"
    proc = subprocess.Popen(
        [sys.executable, str(server_py)],
        cwd=str(repo_root),
        env=env,
        stdout=open(logfile, "w", encoding="utf-8"),
        stderr=subprocess.STDOUT,
        **({"creationflags": subprocess.CREATE_NO_WINDOW} if sys.platform == "win32" else {}),
    )

    deadline = time.time() + 30
    while time.time() < deadline:
        try:
            import urllib.request

            with urllib.request.urlopen(f"{BASE}/health", timeout=1) as r:
                if getattr(r, "status", 200) == 200:
                    return proc, state_dir
        except Exception:
            time.sleep(0.25)

    print("SETUP FAIL: server did not become healthy", file=sys.stderr)
    proc.terminate()
    proc.wait(timeout=5)
    return None, None


def _stop_server(proc: subprocess.Popen | None) -> None:
    if not proc:
        return
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()


def _install_runtime_stubs(page):
    page.add_init_script(
        """
        (() => {
          if (typeof window.t !== 'function') {
            window.t = (key) => (key == null ? '' : String(key));
          }
          const noop = () => {};
          window._uploadPendingFilesProgressBySession =
            window._uploadPendingFilesProgressBySession || new Map();
          window._uploadPendingFilesShowProgressBar = window._uploadPendingFilesShowProgressBar || noop;
          window._uploadPendingFilesHideProgressBar = window._uploadPendingFilesHideProgressBar || noop;
          window._uploadPendingFilesSyncProgressForSession = window._uploadPendingFilesSyncProgressForSession || noop;
          window._uploadPendingFilesUpdateProgress = window._uploadPendingFilesUpdateProgress || noop;
        })();
        """
    )


def _install_fake_event_source(page):
    page.add_init_script(
        """
        (() => {
          const state = {
            constructorCalls: 0,
            openCount: 0,
            closeCount: 0,
            openByInstance: {},
            closeByInstance: {},
            events: [],
          };

          function FakeEventSource(url) {
            const id = ++FakeEventSource._nextId;
            this.__id = id;
            this.url = url || '';
            this.readyState = FakeEventSource.OPEN;
            this.listeners = new Map();
            this.onerror = null;
            this.onopen = null;
            this.onclose = null;

            state.constructorCalls += 1;
            state.openCount += 1;
            state.openByInstance[id] = (state.openByInstance[id] || 0) + 1;
            state.events.push({
              type: 'open',
              id,
              url: String(this.url || ''),
              token: Number(window.__benchCurrentSampleToken || 0),
              t: performance.now(),
            });
            setTimeout(() => {
              if (typeof this.onopen === 'function') {
                try { this.onopen({type: 'open', target: this}); } catch (_) {}
              }
            }, 0);
          }
          FakeEventSource._nextId = FakeEventSource._nextId || 0;
          FakeEventSource.OPEN = 1;
          FakeEventSource.CONNECTING = 0;
          FakeEventSource.CLOSED = 2;
          FakeEventSource.prototype.OPEN = 1;
          FakeEventSource.prototype.CONNECTING = 0;
          FakeEventSource.prototype.CLOSED = 2;

          FakeEventSource.prototype.addEventListener = function(type, fn) {
            if (!type || typeof fn !== 'function') return;
            const list = this.listeners.get(type) || [];
            list.push(fn);
            this.listeners.set(type, list);
          };
          FakeEventSource.prototype.removeEventListener = function(type, fn) {
            const list = this.listeners.get(type) || [];
            this.listeners.set(
              type,
              list.filter((entry) => entry !== fn),
            );
          };
          FakeEventSource.prototype.close = function() {
            if (this.readyState === FakeEventSource.CLOSED) return;
            this.readyState = FakeEventSource.CLOSED;
            state.closeCount += 1;
            state.closeByInstance[this.__id] = (state.closeByInstance[this.__id] || 0) + 1;
            state.events.push({
              type: 'close',
              id: this.__id,
              url: String(this.url || ''),
              token: Number(window.__benchCurrentSampleToken || 0),
              t: performance.now(),
            });
            const listeners = this.listeners.get('close') || [];
            for (const fn of listeners) {
              try { fn({ type: 'close', target: this }); } catch (_) {}
            }
          };
          FakeEventSource.prototype.dispatchEvent = function(event) {
            const type = String(event && event.type || '');
            const listeners = this.listeners.get(type) || [];
            for (const fn of listeners) {
              try { fn.call(this, event); } catch (_) {}
            }
            const onType = this['on' + type];
            if (typeof onType === 'function') {
              try { onType(event); } catch (_) {}
            }
          };

          window.EventSource = FakeEventSource;
          window.__benchEventSourceState = state;
        })();
        """
    )


def _install_capture_overlay(page):
    page.add_init_script(
        """
        (() => {
          const ensure = () => {
            if (document.getElementById('__benchCaptureHud')) return;
            const host = document.createElement('div');
            host.id = '__benchCaptureHud';
            host.style.position = 'fixed';
            host.style.top = '8px';
            host.style.left = '8px';
            host.style.padding = '8px 10px';
            host.style.font = '12px/1.2 ui-monospace, monospace';
            host.style.color = '#dfe3f0';
            host.style.background = 'rgba(9, 15, 31, 0.86)';
            host.style.border = '1px solid rgba(255,255,255,.25)';
            host.style.borderRadius = '8px';
            host.style.zIndex = '2147483647';
            host.style.pointerEvents = 'none';
            host.style.maxWidth = '32rem';

            const stage = document.createElement('div');
            const timer = document.createElement('div');
            const metric = document.createElement('div');

            host.appendChild(stage);
            host.appendChild(timer);
            host.appendChild(metric);
            document.body.appendChild(host);

            window.__benchHud = {
              start: performance.now(),
              stage: 'init',
              timer,
              stageEl: stage,
              metricEl: metric,
            };
          };

          window.__benchSetCaptureStage = (value) => {
            ensure();
            if (!window.__benchHud) return;
            window.__benchHud.stage = String(value || 'init');
            window.__benchHud.stageEl.textContent = `stage: ${window.__benchHud.stage}`;
          };

          window.__benchSetCaptureMetric = (value) => {
            ensure();
            if (!window.__benchHud || !window.__benchHud.metricEl) return;
            const text = String(value == null ? '' : value);
            window.__benchHud.metricEl.textContent = text;
          };

          window.__benchCreateCaptureHud = () => {
            ensure();
            if (window.__benchHud._timer) return;
            const update = () => {
              const elapsed = Math.max(0, performance.now() - (window.__benchHud.start || 0));
              if (window.__benchHud.timer) {
                window.__benchHud.timer.textContent = `t=${elapsed.toFixed(1)}ms`;
              }
            };
            window.__benchHud._timer = setInterval(update, 120);
            update();
          };
        })();
        """
    )


def _install_benchmark_hooks(page):
    page.evaluate(
        """
        () => {
          if (typeof window.__benchHookState === 'undefined') {
            const state = {
              token: 0,
              events: [],
              selectRecoveryCalls: [],
              deferCallbacks: [],
              attachCalls: [],
              watchCalls: [],
              sceneRenderCalls: [],
              appendLiveToolCardCount: 0,
              firstAppendLiveToolCardStack: '',
            };

            const push = (type, payload) => {
              state.events.push({
                type,
                token: state.token,
                t: typeof performance !== 'undefined' && performance.now ? performance.now() : 0,
                ...(payload || {}),
              });
            };

            const wrap = (name, makeWrapper) => {
              const original = window[name];
              if (typeof original !== 'function' || original.__benchWrapped) return;
              const wrapped = makeWrapper(original);
              wrapped.__benchWrapped = true;
              window[name] = wrapped;
            };

            wrap('_selectLiveRecoveryInflight', (orig) => function(localInflight, serverSnapshot, activeStreamId) {
              const token = state.token;
              const selected = orig.call(this, localInflight, serverSnapshot, activeStreamId);
              state.selectRecoveryCalls.push({
                token,
                hasLocal: !!localInflight,
                hasServer: !!serverSnapshot,
                activeStreamId: String(activeStreamId || ''),
                selected: selected === localInflight ? 'inflight' : (selected === serverSnapshot ? 'discover' : 'other'),
              });
              push('selectLiveRecovery', {
                hasLocal: !!localInflight,
                hasServer: !!serverSnapshot,
                localStreamId: localInflight && localInflight.streamId ? String(localInflight.streamId) : '',
                hasActive: !!(typeof INFLIGHT === 'object' && INFLIGHT),
                inflightKeys: (typeof INFLIGHT === 'object' && INFLIGHT) ? Object.keys(INFLIGHT) : [],
                selected: state.selectRecoveryCalls[state.selectRecoveryCalls.length - 1].selected,
                activeStreamId: String(activeStreamId || '')
              });
              return selected;
            });

            wrap('_deferActiveSessionSceneRestore', (orig) => function(sid, activeStreamId, loadGeneration, restoreFn) {
              const token = state.token;
              const sidValue = String(sid || '');
              const streamValue = String(activeStreamId || '');
              push('deferScheduled', {sid: sidValue, activeStreamId: streamValue, loadGeneration});

              const wrappedRestore = function() {
                const beforeInflight = (typeof INFLIGHT === 'object' && INFLIGHT) ? INFLIGHT[sidValue] : null;
                push('deferCallbackStart', {
                  sid: sidValue,
                  activeStreamId: streamValue,
                  inflightExists: !!beforeInflight,
                  inflightReattach: !!(beforeInflight && beforeInflight.reattach),
                  inflightStreamId: String(beforeInflight && beforeInflight.streamId || ''),
                  ownerSessionId: String(typeof S !== 'undefined' && S && S.session && S.session.session_id || ''),
                  ownerActiveStreamId: String(typeof S !== 'undefined' && S && S.activeStreamId || ''),
                });
                state.deferCallbacks.push({
                  token,
                  sid: sidValue,
                  activeStreamId: streamValue,
                  loadGeneration,
                  started: true,
                });
                try {
                  const result = restoreFn && restoreFn();
                  if (result && typeof result.then === 'function') {
                    return result.then(
                      (value) => {
                        push('deferCallbackDone', {sid: sidValue, activeStreamId: streamValue, ok: true});
                        return value;
                      },
                      () => {
                        push('deferCallbackDone', {sid: sidValue, activeStreamId: streamValue, ok: false});
                        return undefined;
                      }
                    );
                  }
                  const afterInflight = (typeof INFLIGHT === 'object' && INFLIGHT) ? INFLIGHT[sidValue] : null;
                  push('deferCallbackDone', {
                    sid: sidValue,
                    activeStreamId: streamValue,
                    ok: true,
                    inflightExists: !!afterInflight,
                    inflightReattach: !!(afterInflight && afterInflight.reattach),
                    inflightStreamId: String(afterInflight && afterInflight.streamId || ''),
                  });
                  return result;
                } catch (_) {
                  push('deferCallbackDone', {sid: sidValue, activeStreamId: streamValue, ok: false});
                  return undefined;
                }
              };

              return orig.call(this, sid, activeStreamId, loadGeneration, wrappedRestore);
            });

            wrap('_renderLiveAnchorActivitySceneForStream', (orig) => function(streamId, sid) {
              const token = state.token;
              push('sceneRenderLive', {streamId: String(streamId || ''), sid: String(sid || ''), token});
              state.sceneRenderCalls.push({
                token,
                type: 'live',
                sid: String(sid || ''),
                streamId: String(streamId || ''),
              });
              const result = orig.call(this, streamId, sid);
              if (!state.sceneRenderCalls.some((entry) => entry.type === 'live-result' && entry.token === token)) {
                state.sceneRenderCalls.push({
                  token,
                  type: 'live-result',
                  sid: String(sid || ''),
                  streamId: String(streamId || ''),
                  returned: !!result,
                  liveTurnExists: !!document.getElementById('liveAssistantTurn'),
                  totalSceneRows: document.querySelectorAll('[data-anchor-scene-row="1"]').length,
                });
              }
              return result;
            });

            wrap('_renderRuntimeJournalAnchorActivityScene', (orig) => function(streamId, sid) {
              const token = state.token;
              push('sceneRenderRuntime', {streamId: String(streamId || ''), sid: String(sid || ''), token});
              state.sceneRenderCalls.push({
                token,
                type: 'runtime',
                sid: String(sid || ''),
                streamId: String(streamId || ''),
              });
              const result = orig.call(this, streamId, sid);
              if (!state.sceneRenderCalls.some((entry) => entry.type === 'runtime-result' && entry.token === token)) {
                state.sceneRenderCalls.push({
                  token,
                  type: 'runtime-result',
                  sid: String(sid || ''),
                  streamId: String(streamId || ''),
                  returned: !!result,
                  liveTurnExists: !!document.getElementById('liveAssistantTurn'),
                  totalSceneRows: document.querySelectorAll('[data-anchor-scene-row="1"]').length,
                });
              }
              return result;
            });

            wrap('attachLiveStream', (orig) => function(sid, streamId, pendingAttachments, opts) {
              const reconnecting = !!(opts && opts.reconnecting);
              push('attachLiveStream', {sid: String(sid || ''), streamId: String(streamId || ''), reconnecting});
              state.attachCalls.push({
                token: state.token,
                sid: String(sid || ''),
                streamId: String(streamId || ''),
                reconnecting,
              });
              return orig.call(this, sid, streamId, pendingAttachments, opts);
            });

            wrap('appendLiveToolCard', (orig) => function() {
              state.appendLiveToolCardCount += 1;
              if (!state.firstAppendLiveToolCardStack) {
                state.firstAppendLiveToolCardStack = String(new Error('bench appendLiveToolCard').stack || '');
              }
              return orig.apply(this, arguments);
            });

            if (typeof window.watchInflightSession === 'function') {
              wrap('watchInflightSession', (orig) => function(sid, streamId) {
                push('watchInflightSession', {sid: String(sid || ''), streamId: String(streamId || '')});
                state.watchCalls.push({
                  token: state.token,
                  sid: String(sid || ''),
                  streamId: String(streamId || ''),
                });
                return orig.call(this, sid, streamId);
              });
            }

            window.__benchBeginSample = () => {
              state.token += 1;
              return state.token;
            };
            window.__benchResetHookState = () => {
              state.events.length = 0;
              state.selectRecoveryCalls.length = 0;
              state.deferCallbacks.length = 0;
              state.attachCalls.length = 0;
              state.watchCalls.length = 0;
              state.sceneRenderCalls.length = 0;
              state.appendLiveToolCardCount = 0;
              state.firstAppendLiveToolCardStack = '';
            };
            window.__benchGetHookState = () => ({
              token: state.token,
              events: [...state.events],
              selectRecoveryCalls: [...state.selectRecoveryCalls],
              deferCallbacks: [...state.deferCallbacks],
              attachCalls: [...state.attachCalls],
              watchCalls: [...state.watchCalls],
              sceneRenderCalls: [...state.sceneRenderCalls],
              appendLiveToolCardCount: state.appendLiveToolCardCount,
              firstAppendLiveToolCardStack: state.firstAppendLiveToolCardStack,
            });
            window.__benchHookState = state;
          }
        }
        """
    )


def _install_routes(page):
    route_state = {"session_list_seen": False, "first_session_list_omitted": False}

    def handler(route):
        parsed = urlsplit(route.request.url)
        path = parsed.path
        qs = parse_qs((parsed.query or ""))
        include_session_list = True
        if path == "/api/sessions":
            include_session_list = route_state.get("session_list_seen", False)
            if not route_state.get("session_list_seen", False):
                route_state["first_session_list_omitted"] = not include_session_list
            route_state["session_list_seen"] = True
        payload = _build_routes_payload(path, qs, include_session_list=include_session_list)
        if payload is None:
            route.continue_()
            return

        headers = {"content-type": "application/json"}
        if path in ("/api/chat/stream", "/api/session/stream"):
            headers = {"content-type": "text/event-stream"}

        route.fulfill(status=200, headers=headers, body=payload)

    page.route("**/*", handler)
    return route_state


def _median(values):
    return statistics.median(values) if values else None


def _p95(values):
    if not values:
        return None
    ordered = sorted(values)
    idx = int(round(0.95 * (len(ordered) - 1)))
    return ordered[max(0, min(len(ordered) - 1, idx))]


def _run_one_sample(browser, device, sample_index: int, config: dict, capture: bool = False, capture_dir: Path | None = None):
    capture_dir = capture_dir or Path(REPORT_ROOT / "captures")
    sample_capture = None
    context_opts = dict(device)
    if capture:
        sample_capture = capture_dir / f"sample_{sample_index:02d}"
        sample_capture.mkdir(parents=True, exist_ok=True)
        context_opts["record_video_dir"] = str(sample_capture)
        if "viewport" in context_opts:
            context_opts["record_video_size"] = context_opts["viewport"]

    context_opts.setdefault("base_url", BASE)

    sample: dict = {
        "sample": sample_index,
        "errors": [],
        "pageErrorCount": 0,
        "pageErrors": [],
        "firstSessionListOmitted": False,
    }

    with browser.new_context(**context_opts) as ctx:
        page = ctx.new_page()
        page.set_default_timeout(30_000)

        page_errors: list[str] = []
        page.on("pageerror", lambda e: page_errors.append(str(e)))
        page.on("requestfailed", lambda req: page_errors.append(f"requestfailed: {req.url}"))

        _install_runtime_stubs(page)
        _install_fake_event_source(page)
        _install_capture_overlay(page)
        route_state = _install_routes(page)

        throttle = float(config.get("cpuThrottle", 1))
        if throttle != 1:
            try:
                cdp = ctx.new_cdp_session(page)
                cdp.send("Emulation.setCPUThrottlingRate", {"rate": throttle})
            except Exception:
                pass

        page.goto(f"{BASE}/", wait_until="domcontentloaded")
        if capture:
            page.evaluate(
                "if (typeof window.__benchCreateCaptureHud === 'function') { window.__benchCreateCaptureHud(); }"
            )

        if isinstance(page, object):
            page.evaluate("window.localStorage.removeItem('hermes-webui-session')")
        page.wait_for_function("typeof window.loadSession === 'function'", timeout=15_000)

        _install_benchmark_hooks(page)

        # Start from a tiny idle conversation. The measured action is a real
        # switch to an already-running background conversation represented by
        # its persisted local INFLIGHT snapshot.
        idle_state = page.evaluate(
            f"""
            async () => {{
              const out = {{ ok: true }};
              try {{
                await window.loadSession({json.dumps(IDLE_SESSION_ID)});
                return out;
              }} catch (err) {{
                out.ok = false;
                out.error = String(err && (err.message || err));
                return out;
              }}
            }}
            """
        )
        if not idle_state.get("ok"):
            sample["errors"].append(f"idle setup failed: {idle_state.get('error')}")
            sample["pageErrorCount"] = len(page_errors)
            sample["pageErrors"] = page_errors[:20]
            return sample

        seeded_session = _session_payload(
            active=True,
            messages_requested=1,
            messages_limit=VISIBLE_MESSAGES,
            messages_before=0,
        )
        prime_seed = {
            "sid": ACTIVE_SESSION_ID,
            "session": seeded_session,
            "runtime_journal_snapshot": seeded_session.get("runtime_journal_snapshot"),
            "tool_calls": seeded_session.get("tool_calls") or [],
            "messages": seeded_session.get("messages") or [],
        }

        sample["primeInflightSynthetic"] = False
        sample["primeInflightState"] = {}
        # This is the deterministic equivalent of the browser's persisted
        # switch-away snapshot. No product method is stubbed: loadSession still
        # selects, renders, and reattaches this state through its real path.
        synthetic = _inject_synthetic_inflight(
            page,
            ACTIVE_SESSION_ID,
            ACTIVE_STREAM_ID,
            TARGET_INFLIGHT_LAST_SEQ,
            prime_seed,
        )
        sample["primeInflightSynthetic"] = bool(synthetic.get("ok"))
        if not synthetic.get("ok"):
            sample["errors"].append(f"prime INFLIGHT unavailable: {synthetic.get('reason', 'unknown')}")

        pre_return_state = _query_inflight_state(page, ACTIVE_SESSION_ID)
        sample["primeInflightState"] = pre_return_state
        sample["primeInflightPostSwitch"] = pre_return_state
        sample["primeRetainedInflightSnapshot"] = bool(pre_return_state.get("exists"))
        sample["retainedInflightSnapshot"] = bool(pre_return_state.get("exists"))
        sample["replayedInflightRestored"] = bool(
            pre_return_state.get("exists")
            and pre_return_state.get("streamId") == ACTIVE_STREAM_ID
            and pre_return_state.get("reattach")
            and not pre_return_state.get("journalReplayFromStart")
            and pre_return_state.get("toolCallCount") == TOTAL_TOOL_CALLS
            and pre_return_state.get("hasAnchorActivityScene")
        )
        sample["preReturnState"] = pre_return_state

        if capture:
            page.evaluate(
                """
                () => {
                  if (typeof window.__benchSetCaptureStage === 'function') window.__benchSetCaptureStage('ready');
                  if (typeof window.__benchSetCaptureMetric === 'function') window.__benchSetCaptureMetric('next: switch to active fixture');
                }
                """
            )
            page.wait_for_timeout(1000)

        measurement = page.evaluate(
            f"""
            async () => {{
              const out = {{
                branchObserved: 'non_inflight',
                tLoadSessionCallMs: null,
                tTranscriptReadyMs: null,
                tTranscriptPaintOpportunityMs: null,
                tAfterTranscriptPaintMs: null,
                tFullSceneReadyMs: null,
                sceneRowFirstSeenMs: null,
                transcriptRowCount: 0,
                transcriptRowCountAtTranscriptPaintOpportunity: null,
                sceneRowCountAtTranscriptPaintOpportunity: null,
                sceneRowCount: 0,
                sceneRowIds: [],
                sceneRowIdSetSize: 0,
                eventSourceOpenCount: 0,
                eventSourceCloseCount: 0,
                longTaskTotalMs: 0,
                longTaskMaxMs: 0,
                longTaskCount: 0,
                errors: [],
                t0: performance.now(),
                captureSwitchElapsedMs: null,
                attachAfterSceneRestore: false,
              }};

              const targetStream = {json.dumps(ACTIVE_STREAM_ID)};
              const targetSid = {json.dumps(ACTIVE_SESSION_ID)};
              const expectPatched = {json.dumps(bool(config.get('expectPatch', True)))};
              if (typeof window.__benchBeginSample === 'function') {{
                window.__benchCurrentSampleToken = window.__benchBeginSample();
              }} else {{
                out.errors.push('benchmark hooks not installed');
                return out;
              }}

              const token = window.__benchCurrentSampleToken;
              const sampleToken = (typeof window.__benchCurrentSampleToken === 'number')
                ? window.__benchCurrentSampleToken
                : 0;
              if (window.__benchHud) {{
                out.captureSwitchElapsedMs = performance.now() - Number(window.__benchHud.start || 0);
              }}
              if (typeof window.__benchSetCaptureStage === 'function') {{
                window.__benchSetCaptureStage('switch-start');
              }}

              const countRows = () => {{
                const inner = document.getElementById('msgInner');
                const liveTurn = document.getElementById('liveAssistantTurn');
                const transcriptRows = inner ? inner.querySelectorAll('[data-msg-idx], .message').length : 0;
                const loading = inner ? String(inner.textContent || '').includes('Loading conversation...') : false;
                const sceneRows = liveTurn ? liveTurn.querySelectorAll('[data-anchor-scene-row="1"]').length : 0;
                if (out.sceneRowFirstSeenMs === null && sceneRows > 0) {{
                  out.sceneRowFirstSeenMs = performance.now() - out.t0;
                }}
                return {{ transcriptRows, loading, sceneRows }};
              }};

              let longTaskTotal = 0;
              let longTaskMax = 0;
              let longTaskCount = 0;
              let observer = null;
              try {{
                observer = new PerformanceObserver((list) => {{
                  for (const entry of list.getEntries()) {{
                    const duration = Number(entry.duration || 0);
                    if (duration > 0) {{
                      longTaskCount += 1;
                      longTaskTotal += duration;
                      if (duration > longTaskMax) longTaskMax = duration;
                    }}
                  }}
                }});
                observer.observe({{ type: 'longtask', buffered: true }});
              }} catch (_) {{}}

              const makeProbe = (predicate) => new Promise((resolve) => {{
                const timeoutMs = 30000;
                const started = performance.now();
                const finish = (result) => {{
                  if (result && result.timeout) return resolve({{timeout: true}});
                  resolve({{timeout: false, ...result}});
                }};
                const state = {{ done: false }};
                const timer = setTimeout(() => {{
                  if (state.done) return;
                  state.done = true;
                  finish({{ timeout: true }});
                }}, timeoutMs);
                const evaluate = () => {{
                  if (state.done) return;
                  const current = countRows();
                  if (predicate(current)) {{
                    state.done = true;
                    clearTimeout(timer);
                    finish({{
                      timeout: false,
                      tMs: performance.now() - out.t0,
                      current,
                    }});
                  }}
                }};
                const mo = new MutationObserver(evaluate);
                mo.observe(document.body || document.documentElement, {{ childList: true, subtree: true, characterData: true }});
                evaluate();
              }});

              const transcriptPromise = makeProbe((current) => !current.loading && current.transcriptRows >= {VISIBLE_MESSAGES});
              const scenePromise = makeProbe((current) => current.sceneRows >= {EXPECTED_FINAL_SCENE_ROWS});

              const tLoadStart = performance.now();
              const loadPromise = window.loadSession(targetSid)
                .then(() => {{
                  out.tLoadSessionCallMs = performance.now() - tLoadStart;
                  return true;
                }})
                .catch((err) => {{
                  out.errors.push('loadSession failed: ' + String(err && (err.message || err)));
                  return false;
                }});

              const transcriptResult = await transcriptPromise;
              if (transcriptResult && transcriptResult.timeout) {{
                out.errors.push('transcript-ready timeout');
              }} else if (transcriptResult) {{
                out.tTranscriptReadyMs = Number(transcriptResult.tMs || 0);
                if (typeof window.__benchSetCaptureStage === 'function') {{ window.__benchSetCaptureStage('frame-1'); }}
                await new Promise((resolve) => requestAnimationFrame(resolve));
                const frame1 = countRows();
                out.transcriptRowCountAtTranscriptPaintOpportunity = Number(frame1.transcriptRows || 0);
                out.sceneRowCountAtTranscriptPaintOpportunity = Number(frame1.sceneRows || 0);
                out.tTranscriptPaintOpportunityMs = performance.now() - out.t0;
                if (typeof window.__benchSetCaptureMetric === 'function') {{
                  window.__benchSetCaptureMetric('paint-op=' + Math.round(out.tTranscriptPaintOpportunityMs) + 'ms scene=' + out.sceneRowCountAtTranscriptPaintOpportunity);
                }}
                await new Promise((resolve) => requestAnimationFrame(resolve));
                out.tAfterTranscriptPaintMs = performance.now() - out.t0;
                if (typeof window.__benchSetCaptureStage === 'function') {{ window.__benchSetCaptureStage('frame-2'); }}
              }}

              if (expectPatched) {{
                const fullSceneResult = await scenePromise;
                if (fullSceneResult && fullSceneResult.timeout) {{
                  out.errors.push('full scene timeout');
                }} else if (fullSceneResult) {{
                  out.tFullSceneReadyMs = Number(fullSceneResult.tMs || 0);
                }}
              }}

              await loadPromise;

              // attachLiveStream performs an async status preflight before it
              // constructs the real EventSource. Wait briefly so transport
              // ownership is part of the verified outcome rather than a race
              // against report collection.
              await Promise.race([
                new Promise((resolve) => {{
                  const check = () => {{
                    const es = window.__benchEventSourceState || {{}};
                    const targetOpen = (es.events || []).some(
                      (entry) =>
                        entry &&
                        entry.type === 'open' &&
                        entry.token === sampleToken &&
                        String(entry.url || '').includes('stream_id=' + encodeURIComponent(targetStream))
                    );
                    if (targetOpen) return resolve();
                    setTimeout(check, 10);
                  }};
                  check();
                }}),
                new Promise((resolve) => setTimeout(resolve, 2000)),
              ]);

              const final = countRows();
              out.transcriptRowCount = final.transcriptRows;
              out.sceneRowCount = final.sceneRows;

              const liveTurn = document.getElementById('liveAssistantTurn');
              if (liveTurn) {{
                const ids = [];
                liveTurn.querySelectorAll('[data-anchor-scene-row="1"]').forEach((row) => {{
                  const rid =
                    row.getAttribute('data-anchor-row-id') ||
                    row.getAttribute('data-anchor-local-id') ||
                    '';
                  if (rid) {{
                    ids.push(String(rid));
                  }}
                }});
                const setSize = (new Set(ids)).size;
                out.sceneRowIds = ids;
                out.sceneRowIdSetSize = setSize;
              }}

              const esState = window.__benchEventSourceState || {{}};
              out.eventSourceOpenCount = Number(esState.openCount || 0);
              out.eventSourceCloseCount = Number(esState.closeCount || 0);
              const targetEventSourceOpen = (esState.events || []).find(
                (entry) =>
                  entry &&
                  entry.type === 'open' &&
                  entry.token === sampleToken &&
                  String(entry.url || '').includes('stream_id=' + encodeURIComponent(targetStream))
              );
              out.targetEventSourceOpenMs = targetEventSourceOpen
                ? Number(targetEventSourceOpen.t || 0) - out.t0
                : null;

              if (observer) {{
                observer.disconnect();
              }}
              out.longTaskTotalMs = Number(longTaskTotal || 0);
              out.longTaskMaxMs = Number(longTaskMax || 0);
              out.longTaskCount = Number(longTaskCount || 0);
              const hook = (typeof window.__benchGetHookState === 'function')
                ? window.__benchGetHookState()
                : null;
              out.hookToken = sampleToken;
              if (!hook) {{
                out.errors.push('benchmark hook state missing');
                return out;
              }}

              const matchingSelection = (hook.selectRecoveryCalls || []).filter(
                (entry) => entry && entry.token === sampleToken && String(entry.activeStreamId || '') === targetStream
              );
              const matchingDefers = (hook.deferCallbacks || []).filter(
                (entry) => entry && entry.token === sampleToken && String(entry.activeStreamId || '') === targetStream
              );
              if (matchingSelection.length) {{
                const last = matchingSelection[matchingSelection.length - 1];
                if (last.selected === 'inflight') {{
                  out.branchObserved = 'local_inflight';
                }} else if (last.selected === 'discover' && matchingDefers.length) {{
                  out.branchObserved = 'server_snapshot_inflight';
                }} else {{
                  out.branchObserved = last.selected || 'non_inflight';
                }}
              }} else if (matchingDefers.length) {{
                out.branchObserved = 'active_recovery';
              }} else {{
                out.errors.push('branch-selection hook never recorded');
              }}
              out.deferCallbackCount = matchingDefers.length;

              const matchingEvents = (hook.events || []).filter(
                (entry) =>
                  entry &&
                  entry.token === sampleToken &&
                  (
                    String(entry.sid || '') === targetSid ||
                    String(entry.streamId || '') === targetStream ||
                    (!entry.sid && !entry.streamId)
                  ),
              );
              const firstSceneIdx = matchingEvents.findIndex((entry) => String(entry.type || '').startsWith('sceneRender'));
              const firstAttachIdx = matchingEvents.findIndex((entry) =>
                String(entry.type || '') === 'attachLiveStream' || String(entry.type || '') === 'watchInflightSession'
              );
              const firstSceneEvent = firstSceneIdx >= 0 ? matchingEvents[firstSceneIdx] : null;
              if (matchingDefers.length > 0 && firstSceneEvent && targetEventSourceOpen) {{
                out.attachAfterSceneRestore = Number(targetEventSourceOpen.t || 0) >= Number(firstSceneEvent.t || 0);
                if (!out.attachAfterSceneRestore) out.errors.push('target EventSource opened before scene restore event');
              }} else if (matchingDefers.length > 0) {{
                out.attachAfterSceneRestore = false;
                out.errors.push('missing scene or target EventSource ordering evidence');
              }} else if (firstAttachIdx !== -1 && firstSceneIdx !== -1 && firstAttachIdx < firstSceneIdx) {{
                out.attachAfterSceneRestore = false;
              }} else {{
                out.attachAfterSceneRestore = true;
              }}

              return out;
            }}
            """
        )

        sample.update(measurement)
        sample["firstSessionListOmitted"] = bool(route_state.get("first_session_list_omitted"))
        sample.pop("t0", None)
        sample["hookState"] = page.evaluate(
            "() => (typeof window.__benchGetHookState === 'function' ? window.__benchGetHookState() : null);"
        )
        hook_state = sample.get("hookState") or {}
        sample["hookSummary"] = {
            "sceneRenderCalls": len(hook_state.get("sceneRenderCalls") or []),
            "deferCallbacks": len(hook_state.get("deferCallbacks") or []),
            "attachCalls": len(hook_state.get("attachCalls") or []),
            "watchCalls": len(hook_state.get("watchCalls") or []),
            "events": len(hook_state.get("events") or []),
        }

        if sample.get("sceneRowIdSetSize") and sample.get("sceneRowIdSetSize") != sample.get("sceneRowCount"):
            sample.setdefault("errors", []).append("duplicate final scene ids")

        if capture and sample_capture:
            page.screenshot(path=str(sample_capture / "final.png"), full_page=True)

    sample["pageErrors"] = page_errors[:20]
    sample["pageErrorCount"] = len(page_errors)
    if sample.get("pageErrorCount"):
        sample.setdefault("errors", []).append(f"page errors: {sample['pageErrorCount']}")
    return sample


def main() -> int:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("SKIP: playwright not installed", file=sys.stderr)
        return 2

    samples = max(1, int(os.getenv("BENCH_SAMPLES", "5")))
    cpu_throttle = float(os.getenv("BENCH_CDP_THROTTLE", "1"))
    expect_patch = os.getenv("BENCH_EXPECT_PATCH", "1") != "0"
    capture = os.getenv("BENCH_CAPTURE") == "1"
    capture_dir = Path(os.getenv("BENCH_CAPTURE_DIR", str(REPORT_ROOT / "captures")))

    report_root = REPORT_ROOT
    report_root.mkdir(parents=True, exist_ok=True)
    report = _build_report()
    report["command"]["samples"] = samples
    report["command"]["capability"]["capture_enabled"] = capture
    report["command"]["capability"]["cpu_throttle"] = cpu_throttle
    report["command"]["capability"]["expect_patch"] = expect_patch

    fixture_path = Path(os.getenv("BENCH_FIXTURE_OUT", str(report_root / "session-switch-active-render-fixture.json")))
    report_path = Path(os.getenv("BENCH_OUT", str(report_root / "session-switch-active-render-report.json")))

    fixture_path.parent.mkdir(parents=True, exist_ok=True)
    fixture_path.write_text(json.dumps({"fixture": _fixture_description()}, indent=2), encoding="utf-8")

    proc, state_dir = _start_server()
    if not proc:
        return 2

    results: list[dict] = []
    errors: list[str] = []
    expected_scene_ids: list[str] | None = None

    try:
        with sync_playwright() as pw:
            device = pw.devices["Pixel 5"]
            browser = pw.chromium.launch(
                headless=True,
                args=["--no-sandbox", "--disable-dev-shm-usage"],
            )
            for i in range(samples):
                sample = _run_one_sample(
                    browser,
                    device,
                    i + 1,
                    {"cpuThrottle": cpu_throttle, "expectPatch": expect_patch},
                    capture=capture,
                    capture_dir=capture_dir,
                )
                results.append(sample)

                if sample.get("branchObserved") != "local_inflight":
                    errors.append(f"sample {i + 1}: expected local INFLIGHT recovery branch, observed {sample.get('branchObserved')}")
                if not sample.get("retainedInflightSnapshot", False):
                    errors.append(f"sample {i + 1}: no retained INFLIGHT snapshot from prime before idle switch")
                if not sample.get("replayedInflightRestored", False):
                    errors.append(f"sample {i + 1}: seeded INFLIGHT state was not reattach-ready before measured switch")
                pre_return_state = sample.get("preReturnState") or {}
                if not pre_return_state.get("exists"):
                    errors.append(f"sample {i + 1}: no INFLIGHT entry retained before measured return")
                if not pre_return_state.get("reattach"):
                    errors.append(f"sample {i + 1}: INFLIGHT entry not marked for reattach before measured return")
                if pre_return_state.get("journalReplayFromStart"):
                    errors.append(f"sample {i + 1}: seeded INFLIGHT unexpectedly requested replay-from-start")
                if sample.get("transcriptRowCount", 0) != VISIBLE_MESSAGES:
                    errors.append(f"sample {i + 1}: transcriptRowCount {sample.get('transcriptRowCount')} != {VISIBLE_MESSAGES}")
                if expect_patch:
                    if sample.get("sceneRowCount", 0) != EXPECTED_FINAL_SCENE_ROWS:
                        errors.append(f"sample {i + 1}: final scene count {sample.get('sceneRowCount')} != {EXPECTED_FINAL_SCENE_ROWS}")
                    if not sample.get("firstSessionListOmitted", False):
                        errors.append(f"sample {i + 1}: first /api/sessions response was not omitted")
                    if sample.get("sceneRowCountAtTranscriptPaintOpportunity") != 0:
                        errors.append(f"sample {i + 1}: scene rows at transcript paint opportunity = {sample.get('sceneRowCountAtTranscriptPaintOpportunity')}")
                    if sample.get("sceneRowIdSetSize", 0) != sample.get("sceneRowCount", 0):
                        errors.append(f"sample {i + 1}: duplicate final scene row ids")
                    if sample.get("deferCallbackCount") != 1:
                        errors.append(f"sample {i + 1}: expected one deferred restore callback")
                    if not sample.get("attachAfterSceneRestore"):
                        errors.append(f"sample {i + 1}: target EventSource did not open after scene restore")
                else:
                    hook_summary = sample.get("hookSummary") or {}
                    if sample.get("deferCallbackCount") != 0:
                        errors.append(f"sample {i + 1}: pre-fix baseline unexpectedly used deferred restore")
                    if int(hook_summary.get("sceneRenderCalls") or 0) < TOTAL_TOOL_CALLS:
                        errors.append(f"sample {i + 1}: pre-fix baseline did not reproduce persisted-tool scene-render cascade")
                if sample.get("eventSourceOpenCount", 0) < 1:
                    errors.append(f"sample {i + 1}: no EventSource opens")
                if sample.get("eventSourceCloseCount", 0) > sample.get("eventSourceOpenCount", 0):
                    errors.append(f"sample {i + 1}: EventSource close count exceeded opens")
                if sample.get("pageErrorCount", 0):
                    errors.append(f"sample {i + 1}: page errors {sample.get('pageErrorCount')}")
                for err in sample.get("errors") or []:
                    errors.append(f"sample {i + 1}: {err}")

                ids = sample.get("sceneRowIds") or []
                if ids:
                    if expected_scene_ids is None:
                        expected_scene_ids = ids
                    elif sorted(ids) != sorted(expected_scene_ids):
                        errors.append(f"sample {i + 1}: final scene identities changed from baseline")

            browser.close()
    finally:
        _stop_server(proc)

    report["samples"] = results
    valid = [s for s in results if not s.get("errors")]
    report["invalidSamples"] = len(results) - len(valid)
    report["median"] = {
        "tTranscriptReadyMs": _median([s.get("tTranscriptReadyMs") for s in valid if s.get("tTranscriptReadyMs") is not None]),
        "tTranscriptPaintOpportunityMs": _median([
            s.get("tTranscriptPaintOpportunityMs") for s in valid if s.get("tTranscriptPaintOpportunityMs") is not None
        ]),
        "tAfterTranscriptPaintMs": _median([
            s.get("tAfterTranscriptPaintMs") for s in valid if s.get("tAfterTranscriptPaintMs") is not None
        ]),
        "tFullSceneReadyMs": _median([s.get("tFullSceneReadyMs") for s in valid if s.get("tFullSceneReadyMs") is not None]),
        "tLoadSessionCallMs": _median([s.get("tLoadSessionCallMs") for s in valid if s.get("tLoadSessionCallMs") is not None]),
        "longTaskTotalMs": _median([s.get("longTaskTotalMs") for s in valid if s.get("longTaskTotalMs") is not None]),
        "longTaskMaxMs": _median([s.get("longTaskMaxMs") for s in valid if s.get("longTaskMaxMs") is not None]),
        "longTaskCount": _median([s.get("longTaskCount") for s in valid if s.get("longTaskCount") is not None]),
    }
    report["p95"] = {
        "tTranscriptReadyMs": _p95([s.get("tTranscriptReadyMs") for s in valid if s.get("tTranscriptReadyMs") is not None]),
        "tTranscriptPaintOpportunityMs": _p95([s.get("tTranscriptPaintOpportunityMs") for s in valid if s.get("tTranscriptPaintOpportunityMs") is not None]),
        "tAfterTranscriptPaintMs": _p95([s.get("tAfterTranscriptPaintMs") for s in valid if s.get("tAfterTranscriptPaintMs") is not None]),
        "tFullSceneReadyMs": _p95([s.get("tFullSceneReadyMs") for s in valid if s.get("tFullSceneReadyMs") is not None]),
        "tLoadSessionCallMs": _p95([s.get("tLoadSessionCallMs") for s in valid if s.get("tLoadSessionCallMs") is not None]),
        "longTaskTotalMs": _p95([s.get("longTaskTotalMs") for s in valid if s.get("longTaskTotalMs") is not None]),
        "longTaskMaxMs": _p95([s.get("longTaskMaxMs") for s in valid if s.get("longTaskMaxMs") is not None]),
        "longTaskCount": _p95([s.get("longTaskCount") for s in valid if s.get("longTaskCount") is not None]),
    }
    report["errors"] = errors

    report["command"]["bench_server_root"] = os.getenv("BENCH_SERVER_ROOT", str(REPO_ROOT))
    report["command"]["base_dir"] = str(Path(state_dir).parent)

    report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    fixture_path.write_text(json.dumps({"fixture": _fixture_description()}, indent=2), encoding="utf-8")

    if errors:
        print("FAILURE: one or more samples failed:")
        for error in errors:
            print(f"- {error}")
        return 1

    if valid:
        print(
            "PASS: "
            f"{len(valid)} sample(s), "
            f"scene-opportunity-max={max((s.get('sceneRowCountAtTranscriptPaintOpportunity') or 0) for s in valid)} "
            f"final-scene={max((s.get('sceneRowCount') or 0) for s in valid)}"
        )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
