#!/usr/bin/env python3
"""Public browser gate for the normal conversation lifecycle.

This test boots the real WebUI server with isolated state, drives the real chat
composer in Chromium, and supplies deterministic runtime events through the
existing Hermes Gateway Runs API. It proves that one assistant turn keeps the
same semantic activity across live streaming, session reattachment, settlement,
and a hard reload.
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit


PROMPT = "Exercise the public conversation lifecycle gate."
SEED_PROMPT = "Create an idle session for the lifecycle switch gate."
REASONING_TEXT = "Checking the persistent assistant turn."
SECOND_REASONING_TEXT = "Verifying the tool result before continuing."
THIRD_REASONING_TEXT = "Confirming the no-prose tool boundary."
CONTINUATION_REASONING_TEXT = "Continuing after the session reattach boundary."
PROCESS_TEXT = "Inspecting the fixture before the tool call. "
FINAL_TEXT = "Lifecycle gate final answer."
FINAL_PREFIX = "Lifecycle gate "
FINAL_SUFFIX = "final answer."
FINAL_ACK_TEXT = "Lifecycle"
SEED_FINAL_TEXT = "Idle session ready."
TOOL_NAME = "read_file"
TOOL_ID = "lifecycle-tool-1"
SECOND_TOOL_NAME = "terminal"
SECOND_TOOL_ID = "lifecycle-tool-2"
SCENARIO = os.environ.get("LIFECYCLE_SCENARIO", "normal").strip() or "normal"
TEST_BITE = os.environ.get("LIFECYCLE_TEST_BITE", "").strip()
GATEWAY_ACTIVITY_TIMEOUT = 60.0
ANCHOR_SCENE_PERSIST_TIMEOUT = 60.0
ANCHOR_SCENE_PROJECTION_TIMEOUT = 10_000
BROKEN_REASONING_LOCAL_ID = "broken-runtime-reasoning-id"


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _wait_for_health(
    base_url: str,
    timeout: float = 30.0,
    proc: subprocess.Popen | None = None,
) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if proc is not None and proc.poll() is not None:
            return False
        try:
            with urllib.request.urlopen(base_url + "/health", timeout=2) as response:
                if response.status == 200:
                    return True
        except (urllib.error.URLError, OSError):
            pass
        time.sleep(0.25)
    return False


def _get_json(url: str) -> dict:
    with urllib.request.urlopen(url, timeout=3) as response:
        return json.loads(response.read(1024 * 1024))


def _wait_for_persisted_scene(
    base_url: str,
    session_id: str,
    timeout: float = ANCHOR_SCENE_PERSIST_TIMEOUT,
    anchor_scene_requests: list[dict] | None = None,
) -> dict:
    deadline = time.time() + timeout
    url = f"{base_url}/api/session?session_id={session_id}&messages=1"
    last_payload = None
    last_error = None
    while time.time() < deadline:
        try:
            last_payload = _get_json(url)
            last_error = None
        except (json.JSONDecodeError, TimeoutError, urllib.error.URLError, OSError) as exc:
            last_error = f"{type(exc).__name__}: {exc}"
            time.sleep(0.2)
            continue
        session = last_payload.get("session") if isinstance(last_payload, dict) else None
        messages = session.get("messages", []) if isinstance(session, dict) else []
        assistants = [message for message in messages if message.get("role") == "assistant"]
        if assistants and assistants[-1].get("_anchor_activity_scene"):
            return assistants[-1]["_anchor_activity_scene"]
        time.sleep(0.2)
    session = last_payload.get("session") if isinstance(last_payload, dict) else None
    messages = session.get("messages", []) if isinstance(session, dict) else []
    summary = [
        {
            "role": message.get("role"),
            "has_anchor_scene": bool(message.get("_anchor_activity_scene")),
        }
        for message in messages
        if isinstance(message, dict)
    ]
    error_note = f"; last read error: {last_error}" if last_error else ""
    request_note = ""
    if anchor_scene_requests is not None:
        request_note = f"; anchor scene requests: {anchor_scene_requests!r}"
    raise AssertionError(
        "anchor scene was not persisted before reload; "
        f"message summary: {summary!r}{error_note}{request_note}"
    )


def _anchor_projection_snapshot(page) -> dict:
    return page.evaluate(
        """() => {
          const streamId = (typeof S !== 'undefined' && S.activeStreamId) || '';
          const registries = window._liveAnchorRegistries;
          const registry = streamId && registries && typeof registries.get === 'function'
            ? registries.get(streamId)
            : null;
          const api = window.HermesAssistantTurnAnchors;
          const canProject = Boolean(
            registry && api && typeof api.projectAssistantTurnAnchorActivityScene === 'function'
          );
          let scene = null;
          if (canProject) {
            try {
              scene = api.projectAssistantTurnAnchorActivityScene(registry, {
                mode: 'compact_worklog',
              });
            } catch (error) {
              scene = { error: String(error) };
            }
          }
          const rows = Array.isArray(scene && scene.activity_rows) ? scene.activity_rows : [];
          return {
            streamId,
            hasRegistry: Boolean(registry),
            registryCount: registries && typeof registries.size === 'number' ? registries.size : null,
            canProject,
            mode: scene && scene.mode || null,
            rowCount: rows.length,
            rows: rows.map(row => ({
              role: row && row.role || null,
              source: row && row.source_event_type || null,
              status: row && row.status || null,
              tool: row && row.tool && row.tool.name || null,
              text: row && row.text || '',
            })),
          };
        }"""
    )


def _wait_for_live_anchor_projection(page) -> dict:
    try:
        page.wait_for_function(
            """({reasoning, tool}) => {
              const streamId = (typeof S !== 'undefined' && S.activeStreamId) || '';
              const registries = window._liveAnchorRegistries;
              const registry = streamId && registries && typeof registries.get === 'function'
                ? registries.get(streamId)
                : null;
              const api = window.HermesAssistantTurnAnchors;
              if (!registry || !api || typeof api.projectAssistantTurnAnchorActivityScene !== 'function') {
                return false;
              }
              const scene = api.projectAssistantTurnAnchorActivityScene(registry, {
                mode: 'compact_worklog',
              });
              const rows = Array.isArray(scene && scene.activity_rows) ? scene.activity_rows : [];
              const hasThinking = rows.some(row =>
                row && row.role === 'thinking' && String(row.text || '').includes(reasoning)
              );
              const hasTool = rows.some(row =>
                row && row.role === 'tool' && row.tool && row.tool.name === tool
              );
              return hasThinking && hasTool;
            }""",
            arg={"reasoning": REASONING_TEXT, "tool": TOOL_NAME},
            timeout=ANCHOR_SCENE_PROJECTION_TIMEOUT,
        )
    except Exception as exc:
        raise AssertionError(
            "live Anchor projection never included reasoning and tool rows before terminal release: "
            f"{_anchor_projection_snapshot(page)!r}"
        ) from exc
    return _anchor_projection_snapshot(page)


def _wait_for_runtime_scene(
    base_url: str,
    session_id: str,
    expected_reasoning_ids: list[str],
    timeout: float = 10.0,
) -> dict:
    deadline = time.time() + timeout
    url = f"{base_url}/api/session?session_id={session_id}&messages=0&resolve_model=0"
    last_scene = None
    while time.time() < deadline:
        try:
            payload = _get_json(url)
        except (json.JSONDecodeError, TimeoutError, urllib.error.URLError, OSError):
            time.sleep(0.2)
            continue
        session = payload.get("session") if isinstance(payload, dict) else None
        snapshot = session.get("runtime_journal_snapshot") if isinstance(session, dict) else None
        scene = snapshot.get("anchor_activity_scene") if isinstance(snapshot, dict) else None
        last_scene = scene
        rows = scene.get("activity_rows", []) if isinstance(scene, dict) else []
        reasoning_ids = [row.get("local_id") for row in rows if row.get("role") == "thinking"]
        if reasoning_ids == expected_reasoning_ids:
            return scene
        time.sleep(0.2)
    raise AssertionError(
        "runtime journal scene did not expose the expected reasoning identities; "
        f"expected={expected_reasoning_ids!r}, scene={last_scene!r}"
    )


def _terminate_process(proc: subprocess.Popen | None) -> None:
    if proc is None or proc.poll() is not None:
        return
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()


def _start_webui_server(repo_root: Path, env: dict, artifact_dir: Path):
    requested_port = str(os.environ.get("LIFECYCLE_PORT") or "").strip()
    attempts = 1 if requested_port else 5
    last_tail = ""
    last_port = None
    for attempt in range(attempts):
        port = int(requested_port) if requested_port else _free_port()
        last_port = port
        base_url = f"http://127.0.0.1:{port}"
        run_env = dict(env)
        run_env["HERMES_WEBUI_PORT"] = str(port)
        suffix = "" if attempts == 1 else f"-attempt-{attempt + 1}"
        log_path = artifact_dir / f"server{suffix}.log"
        log = log_path.open("w", encoding="utf-8")
        proc = subprocess.Popen(
            [sys.executable, str(repo_root / "server.py")],
            cwd=repo_root,
            env=run_env,
            stdout=log,
            stderr=subprocess.STDOUT,
        )
        if _wait_for_health(base_url, proc=proc):
            return proc, log, log_path, base_url
        _terminate_process(proc)
        log.close()
        if log_path.exists():
            last_tail = log_path.read_text(encoding="utf-8", errors="replace")[-2000:]
    detail = f" on port {last_port}" if last_port else ""
    if last_tail:
        detail += f"; last server log tail:\n{last_tail}"
    raise RuntimeError(f"WebUI server did not become healthy{detail}")


class DeterministicGateway:
    """A localhost-only Gateway Runs server with test-controlled phase gates."""

    def __init__(self) -> None:
        self.activity_ready = threading.Event()
        self.release_settle = threading.Event()
        self.continuation_ready = threading.Event()
        self.release_final_prefix = threading.Event()
        self.final_prefix_ready = threading.Event()
        self.release_terminal = threading.Event()
        self.request_body = None
        self.request_bodies = []
        self.emitted_events = []
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), self._handler())
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    @property
    def base_url(self) -> str:
        host, port = self._server.server_address
        return f"http://{host}:{port}"

    def _handler(self):
        owner = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, _format, *_args):
                return

            def _json(self, payload, status=200):
                body = json.dumps(payload).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _event(self, event_name, payload):
                owner.emitted_events.append({"event": event_name, "payload": payload})
                frame = (
                    f"event: {event_name}\n"
                    f"data: {json.dumps(payload, separators=(',', ':'))}\n\n"
                ).encode("utf-8")
                self.wfile.write(frame)
                self.wfile.flush()

            def do_GET(self):
                request_path = urlsplit(self.path).path
                if request_path == "/v1/capabilities":
                    self._json({
                        "features": {
                            "approval_events": True,
                            "run_approval_response": True,
                        }
                    })
                    return
                if request_path == "/v1/runs/lifecycle-seed-run/events":
                    self.send_response(200)
                    self.send_header("Content-Type", "text/event-stream")
                    self.send_header("Cache-Control", "no-cache")
                    self.send_header("Connection", "close")
                    self.end_headers()
                    self._event("message.delta", {
                        "event": "message.delta",
                        "delta": SEED_FINAL_TEXT,
                    })
                    self._event("run.completed", {
                        "event": "run.completed",
                        "usage": {"input_tokens": 8, "output_tokens": 4},
                    })
                    self.wfile.write(b"data: [DONE]\n\n")
                    self.wfile.flush()
                    return
                if request_path != "/v1/runs/lifecycle-run-1/events":
                    self._json({"error": "not found"}, status=404)
                    return
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-cache")
                self.send_header("Connection", "close")
                self.end_headers()
                try:
                    self._event("reasoning.available", {
                        "event": "reasoning.available",
                        "text": REASONING_TEXT,
                    })
                    self._event("message.delta", {
                        "event": "message.delta",
                        "delta": PROCESS_TEXT,
                    })
                    self._event("tool.started", {
                        "event": "tool.started",
                        "tool": TOOL_NAME,
                        "tool_call_id": TOOL_ID,
                        "status": "running",
                        "args": {"path": "README.md"},
                    })
                    self._event("tool.completed", {
                        "event": "tool.completed",
                        "tool": TOOL_NAME,
                        "tool_call_id": TOOL_ID,
                        "status": "completed",
                        "preview": "README fixture read",
                    })
                    self._event("reasoning.available", {
                        "event": "reasoning.available",
                        "text": SECOND_REASONING_TEXT,
                    })
                    self._event("tool.started", {
                        "event": "tool.started",
                        "tool": SECOND_TOOL_NAME,
                        "tool_call_id": SECOND_TOOL_ID,
                        "status": "running",
                        "args": {"command": "git status --short"},
                    })
                    self._event("tool.completed", {
                        "event": "tool.completed",
                        "tool": SECOND_TOOL_NAME,
                        "tool_call_id": SECOND_TOOL_ID,
                        "status": "completed",
                        "preview": "clean fixture",
                    })
                    self._event("reasoning.available", {
                        "event": "reasoning.available",
                        "text": THIRD_REASONING_TEXT,
                    })
                    owner.activity_ready.set()
                    if not owner.release_settle.wait(timeout=30):
                        return
                    if SCENARIO == "session-reattach":
                        self._event("reasoning.available", {
                            "event": "reasoning.available",
                            "text": CONTINUATION_REASONING_TEXT,
                        })
                        owner.continuation_ready.set()
                    if not owner.release_final_prefix.wait(timeout=30):
                        return
                    self._event("message.delta", {
                        "event": "message.delta",
                        "delta": FINAL_PREFIX,
                    })
                    owner.final_prefix_ready.set()
                    if not owner.release_terminal.wait(timeout=30):
                        return
                    self._event("message.delta", {
                        "event": "message.delta",
                        "delta": FINAL_SUFFIX,
                    })
                    self._event("run.completed", {
                        "event": "run.completed",
                        "usage": {"input_tokens": 12, "output_tokens": 5},
                    })
                    self.wfile.write(b"data: [DONE]\n\n")
                    self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError):
                    return

            def do_POST(self):
                if urlsplit(self.path).path != "/v1/runs":
                    self._json({"error": "not found"}, status=404)
                    return
                length = int(self.headers.get("Content-Length", "0"))
                request_body = json.loads(self.rfile.read(length) or b"{}")
                owner.request_body = request_body
                owner.request_bodies.append(request_body)
                prompt = request_body.get("input")
                if prompt == SEED_PROMPT:
                    self._json({"run_id": "lifecycle-seed-run"})
                elif prompt == PROMPT:
                    self._json({"run_id": "lifecycle-run-1"})
                else:
                    self._json({"error": "unexpected deterministic prompt"}, status=400)

        return Handler

    def start(self) -> None:
        self._thread.start()

    def close(self) -> None:
        self.release_settle.set()
        self.release_final_prefix.set()
        self.release_terminal.set()
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)


def _capture_page_errors(page):
    errors = []
    benign = ("favicon", "manifest.json", "serviceworker", "sw.js")

    def on_console(message):
        if message.type != "error":
            return
        text = message.text
        if not any(needle in text.lower() for needle in benign):
            errors.append(("console", text))

    page.on("console", on_console)
    page.on("pageerror", lambda error: errors.append(("pageerror", str(error))))
    return errors


def _capture_anchor_scene_requests(page):
    events = []

    def on_request(request):
        if "/api/session/anchor-scene" not in request.url:
            return
        events.append({
            "type": "request",
            "method": request.method,
            "url": request.url,
        })

    def on_response(response):
        if "/api/session/anchor-scene" not in response.url:
            return
        events.append({
            "type": "response",
            "status": response.status,
            "url": response.url,
        })

    def on_request_failed(request):
        if "/api/session/anchor-scene" not in request.url:
            return
        failure = request.failure or ""
        events.append({
            "type": "requestfailed",
            "method": request.method,
            "url": request.url,
            "error": str(failure),
        })

    page.on("request", on_request)
    page.on("response", on_response)
    page.on("requestfailed", on_request_failed)
    return events


def _activity_snapshot(page) -> dict:
    return page.evaluate(
        """() => {
          const turn = document.querySelector('#liveAssistantTurn') ||
            Array.from(document.querySelectorAll('.assistant-turn')).pop() || null;
          const groups = turn ? Array.from(turn.querySelectorAll('[data-anchor-scene-owner="1"]')) : [];
          const rows = turn ? Array.from(turn.querySelectorAll('[data-anchor-scene-row="1"]')) : [];
          const visibleFinal = turn ? Array.from(turn.querySelectorAll('.assistant-segment .msg-body'))
            .filter(el => {
              const segment = el.closest('.assistant-segment');
              return segment && !segment.hidden &&
                !segment.classList.contains('assistant-segment-worklog-source') &&
                getComputedStyle(segment).display !== 'none';
            })
            .map(el => el.innerText.trim()).filter(Boolean) : [];
          return {
            live: Boolean(document.querySelector('#liveAssistantTurn')),
            turnIdentity: {
              streamId: turn ? turn.getAttribute('data-anchor-stream-id') || '' : '',
            },
            clientState: {
              busy: Boolean(typeof S !== 'undefined' && S.busy),
              activeStreamId: (typeof S !== 'undefined' && S.activeStreamId) || null,
              sessionId: (typeof S !== 'undefined' && S.session && S.session.session_id) || null,
            },
            groupCount: groups.length,
            summary: groups.map(group => ({
              label: (group.querySelector('.tool-worklog-label,.tool-call-group-label') || {}).textContent || '',
              duration: (group.querySelector('.tool-call-group-duration') || {}).textContent || '',
              live: group.getAttribute('data-live-tool-call-group'),
              settled: group.getAttribute('data-anchor-settled-scene-owner'),
              classes: group.className,
              deferred: group.getAttribute('data-worklog-rows-deferred'),
              activityKey: group.getAttribute('data-tool-worklog-key') || '',
              streamId: group.getAttribute('data-anchor-stream-id') || '',
              turnStartedAt: group.getAttribute('data-turn-started-at') || '',
              expanded: (group.querySelector('.tool-worklog-summary,.tool-call-group-summary') || {})
                .getAttribute?.('aria-expanded') || '',
            })),
            rows: rows.map(row => ({
              role: row.getAttribute('data-anchor-row-role'),
              source: row.getAttribute('data-anchor-source-event-type'),
              rowId: row.getAttribute('data-anchor-row-id') || '',
              localId: row.getAttribute('data-anchor-local-id') || '',
              streamId: row.getAttribute('data-anchor-stream-id') || '',
              tool: row.getAttribute('data-tool-name'),
              text: row.innerText.trim(),
              classes: row.className,
            })),
            visibleFinal,
            transcript: (document.querySelector('#msgInner') || {}).innerText || '',
          };
        }"""
    )


def _expand_settled_worklog(page) -> None:
    page.wait_for_function(
        """() => {
          const group = Array.from(document.querySelectorAll(
            '.assistant-turn [data-anchor-settled-scene-owner="1"]'
          )).pop();
          if (!group) return false;
          const summary = group.querySelector('.tool-worklog-summary,.tool-call-group-summary');
          if (group.classList.contains('tool-call-group-collapsed') && summary) {
            if (typeof _toggleActivityGroup === 'function') _toggleActivityGroup(summary);
            else summary.click();
          }
          if (
            group.getAttribute('data-worklog-rows-deferred') === '1' &&
            typeof _materializeDeferredWorklogRows === 'function'
          ) {
            _materializeDeferredWorklogRows(group);
          }
          return Boolean(group.querySelector('[data-anchor-scene-row="1"]'));
        }""",
        timeout=10000,
    )


def _assert_live_activity(snapshot: dict, *, reasoning_count: int = 3) -> None:
    assert snapshot["live"], snapshot
    assert snapshot["groupCount"] == 1, snapshot
    roles = [row["role"] for row in snapshot["rows"]]
    assert roles.count("thinking") == reasoning_count, snapshot
    assert roles.count("tool") == 2, snapshot
    tool_rows = [row for row in snapshot["rows"] if row["role"] == "tool"]
    assert [row["tool"] for row in tool_rows] == [TOOL_NAME, SECOND_TOOL_NAME], snapshot
    assert all("tool-card-running" not in row["classes"] for row in tool_rows), snapshot
    assert any(REASONING_TEXT in row["text"] for row in snapshot["rows"]), snapshot
    assert any(SECOND_REASONING_TEXT in row["text"] for row in snapshot["rows"]), snapshot
    assert any(THIRD_REASONING_TEXT in row["text"] for row in snapshot["rows"]), snapshot
    expected_order = [
        "thinking",
        "tool",
        "thinking",
        "tool",
        "thinking",
    ]
    if reasoning_count == 4:
        assert any(CONTINUATION_REASONING_TEXT in row["text"] for row in snapshot["rows"]), snapshot
        expected_order.append("thinking")
    assert [row["role"] for row in snapshot["rows"] if row["role"] in {"thinking", "tool"}] == expected_order, snapshot
    assert all(FINAL_TEXT not in text for text in snapshot["visibleFinal"]), snapshot
    assert any(character.isdigit() for character in snapshot["summary"][0]["label"]), snapshot


def _assert_settled(snapshot: dict, *, reasoning_count: int) -> None:
    assert not snapshot["live"], snapshot
    assert snapshot["groupCount"] == 1, snapshot
    roles = [row["role"] for row in snapshot["rows"]]
    assert roles.count("thinking") == reasoning_count and roles.count("tool") == 2, snapshot
    tool_rows = [row for row in snapshot["rows"] if row["role"] == "tool"]
    assert [row["tool"] for row in tool_rows] == [TOOL_NAME, SECOND_TOOL_NAME], snapshot
    assert all("tool-card-running" not in row["classes"] for row in tool_rows), snapshot
    assert any(REASONING_TEXT in row["text"] for row in snapshot["rows"]), snapshot
    assert any(THIRD_REASONING_TEXT in row["text"] for row in snapshot["rows"]), snapshot
    if reasoning_count == 4:
        assert any(CONTINUATION_REASONING_TEXT in row["text"] for row in snapshot["rows"]), snapshot
    assert sum(FINAL_TEXT in text for text in snapshot["visibleFinal"]) == 1, snapshot
    assert snapshot["transcript"].count(FINAL_TEXT) == 1, snapshot


def _semantic_activity(snapshot: dict) -> list[dict]:
    """Canonical user-visible activity in renderer order."""
    semantic = []
    for row in snapshot["rows"]:
        if row["role"] == "thinking":
            text = " ".join(row["text"].split())
            if text.startswith("Thinking "):
                text = text[len("Thinking ") :]
            semantic.append({"role": "thinking", "text": text})
        elif row["role"] == "tool":
            semantic.append({"role": "tool", "tool": row["tool"]})
    return semantic


def _assert_same_live_turn(before: dict, after: dict) -> None:
    _assert_live_activity(after)
    assert _semantic_activity(after) == _semantic_activity(before), {
        "before": _semantic_activity(before),
        "after": _semantic_activity(after),
    }
    assert after["clientState"]["activeStreamId"] == before["clientState"]["activeStreamId"], {
        "before": before["clientState"],
        "after": after["clientState"],
    }
    assert after["turnIdentity"]["streamId"] == before["turnIdentity"]["streamId"], {
        "before": before["turnIdentity"],
        "after": after["turnIdentity"],
    }
    before_group = before["summary"][0]
    after_group = after["summary"][0]
    for key in ("activityKey", "streamId", "turnStartedAt"):
        assert after_group[key] == before_group[key], {"key": key, "before": before_group, "after": after_group}
    # ``rowId`` includes the row's current projection index. Reattachment may
    # legitimately rebuild that projection, while ``localId`` is the durable
    # source-event identity that must survive the rebuild.
    before_rows = [(row["role"], row["localId"]) for row in before["rows"]]
    after_rows = [(row["role"], row["localId"]) for row in after["rows"]]
    assert after_rows == before_rows, {"before": before_rows, "after": after_rows}


def _assert_live_continuation(before: dict, after: dict) -> None:
    _assert_live_activity(after, reasoning_count=4)
    before_semantic = _semantic_activity(before)
    after_semantic = _semantic_activity(after)
    assert after_semantic[:-1] == before_semantic, {
        "before": before_semantic,
        "after": after_semantic,
    }
    assert after_semantic[-1] == {
        "role": "thinking",
        "text": CONTINUATION_REASONING_TEXT,
    }, after_semantic
    stream_id = before["clientState"]["activeStreamId"]
    assert after["rows"][-1]["localId"] == f"live-reasoning:{stream_id}:4", after


def _replace_runtime_reasoning_identity(route, session_id: str, bite_hits: list[str]) -> None:
    parsed = urlsplit(route.request.url)
    query = parse_qs(parsed.query)
    is_target = (
        route.request.method == "GET"
        and parsed.path == "/api/session"
        and query.get("session_id") == [session_id]
        and query.get("messages") == ["0"]
        and query.get("resolve_model") == ["0"]
    )
    if not is_target:
        route.continue_()
        return
    response = route.fetch()
    payload = response.json()
    session = payload.get("session") if isinstance(payload, dict) else None
    snapshot = session.get("runtime_journal_snapshot") if isinstance(session, dict) else None
    scene = snapshot.get("anchor_activity_scene") if isinstance(snapshot, dict) else None
    rows = scene.get("activity_rows", []) if isinstance(scene, dict) else []
    reasoning = next((row for row in rows if row.get("role") == "thinking"), None)
    if isinstance(reasoning, dict):
        reasoning["local_id"] = BROKEN_REASONING_LOCAL_ID
        reasoning["row_id"] = BROKEN_REASONING_LOCAL_ID
        identity = reasoning.get("identity")
        if isinstance(identity, dict):
            identity["local_id"] = BROKEN_REASONING_LOCAL_ID
        bite_hits.append(route.request.url)
    route.fulfill(status=response.status, content_type="application/json", body=json.dumps(payload))


def main() -> int:
    if SCENARIO not in {"normal", "session-reattach"}:
        print(f"SETUP FAIL: unsupported LIFECYCLE_SCENARIO={SCENARIO!r}", file=sys.stderr)
        return 2
    if TEST_BITE == "replace-runtime-reasoning-id" and SCENARIO != "session-reattach":
        print("SETUP FAIL: replace-runtime-reasoning-id requires session-reattach", file=sys.stderr)
        return 2
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("SETUP FAIL: playwright is not installed", file=sys.stderr)
        return 2

    repo_root = Path(__file__).resolve().parent.parent
    state_tmp = tempfile.TemporaryDirectory(prefix="hermes-lifecycle-gate-")
    state_dir = Path(state_tmp.name)
    artifact_env = str(os.environ.get("LIFECYCLE_ARTIFACT_DIR") or "").strip()
    artifact_dir_owned = not bool(artifact_env)
    artifact_dir = Path(artifact_env) if artifact_env else Path(
        tempfile.mkdtemp(prefix="hermes-lifecycle-artifacts-")
    )
    artifact_dir.mkdir(parents=True, exist_ok=True)
    gateway = DeterministicGateway()
    gateway.start()

    agent_dir = state_dir / "no-agent"
    agent_dir.mkdir(parents=True)
    workspace_dir = state_dir / "workspace"
    workspace_dir.mkdir()
    (agent_dir / "run_agent.py").write_text(
        '"""Empty agent stub for the Gateway-backed browser gate."""\n',
        encoding="utf-8",
    )
    env = os.environ.copy()
    for key in list(env):
        if key.endswith("_API_KEY"):
            env.pop(key, None)
    for key in (
        "API_SERVER_KEY",
        "HERMES_WEBUI_PASSWORD",
        "HERMES_WEBUI_EXTENSION_DIR",
        "HERMES_WEBUI_EXTENSION_MANIFEST",
    ):
        env.pop(key, None)
    env.update({
        "HERMES_WEBUI_HOST": "127.0.0.1",
        "HERMES_WEBUI_STATE_DIR": str(state_dir / "webui-state"),
        "HERMES_HOME": str(state_dir / "hermes-home"),
        "HERMES_BASE_HOME": str(state_dir / "hermes-home"),
        "HERMES_CONFIG_PATH": str(state_dir / "hermes-home" / "config.yaml"),
        "HERMES_WEBUI_SKIP_ONBOARDING": "1",
        "HERMES_WEBUI_AGENT_DIR": str(agent_dir),
        "HERMES_WEBUI_DEFAULT_WORKSPACE": str(workspace_dir),
        "HERMES_WEBUI_CHAT_BACKEND": "gateway",
        "HERMES_WEBUI_GATEWAY_BASE_URL": gateway.base_url,
        "HERMES_WEBUI_GATEWAY_USE_RUNS_API": "1",
        "NO_PROXY": "127.0.0.1,localhost",
        "no_proxy": "127.0.0.1,localhost",
    })
    proc = None
    log = None
    log_path = None
    exit_code = 1
    playwright = None
    browser = None
    page = None
    errors = []
    anchor_scene_requests = []
    try:
        proc, log, log_path, base_url = _start_webui_server(repo_root, env, artifact_dir)
        playwright = sync_playwright().start()
        browser = playwright.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage"],
        )
        context = browser.new_context(base_url=base_url)
        page = context.new_page()
        anchor_scene_requests = _capture_anchor_scene_requests(page)
        bite_hits = []
        if TEST_BITE == "drop-anchor-persistence":
            page.route(
                "**/api/session/anchor-scene",
                lambda route: route.fulfill(
                    status=200,
                    content_type="application/json",
                    body='{"ok":true}',
                ),
            )
        errors = _capture_page_errors(page)
        page.goto("/", wait_until="domcontentloaded")
        page.wait_for_selector("#msg", state="visible", timeout=15000)
        seed_session_id = None
        if SCENARIO == "session-reattach":
            page.locator("#msg").fill(SEED_PROMPT)
            page.locator("#btnSend").click()
            page.wait_for_function(
                "text => typeof S !== 'undefined' && S.busy === false && !S.activeStreamId && "
                "(document.querySelector('#msgInner') || {}).innerText?.includes(text)",
                arg=SEED_FINAL_TEXT,
                timeout=15000,
            )
            seed_session_id = page.evaluate("S.session && S.session.session_id")
            assert seed_session_id, "seed session id missing after settlement"
            page.wait_for_selector(
                f'.session-item[data-sid="{seed_session_id}"]',
                state="visible",
                timeout=10000,
            )
            page.locator("#btnNewChat").click()
            page.wait_for_function(
                "sid => S.session && S.session.session_id && S.session.session_id !== sid && !S.busy",
                arg=seed_session_id,
                timeout=15000,
            )
        page.locator("#msg").fill(PROMPT)
        page.locator("#btnSend").click()

        if not gateway.activity_ready.wait(timeout=GATEWAY_ACTIVITY_TIMEOUT):
            raise AssertionError(
                "mock Gateway did not reach the live activity checkpoint; "
                f"request body: {gateway.request_body!r}; events: {gateway.emitted_events!r}"
            )
        page.wait_for_function(
            """({reasoning, secondReasoning, thirdReasoning, tool, secondTool}) => {
              const turn = document.querySelector('#liveAssistantTurn');
              if (!turn) return false;
              const text = turn.innerText || '';
              const thinkingRows = turn.querySelectorAll('[data-anchor-row-role="thinking"]');
              return text.includes(reasoning) && text.includes(secondReasoning) &&
                text.includes(thirdReasoning) && thinkingRows.length === 3 &&
                Boolean(turn.querySelector(`[data-anchor-row-role="tool"][data-tool-name="${tool}"]`)) &&
                Boolean(turn.querySelector(`[data-anchor-row-role="tool"][data-tool-name="${secondTool}"]`));
            }""",
            arg={
                "reasoning": REASONING_TEXT,
                "secondReasoning": SECOND_REASONING_TEXT,
                "thirdReasoning": THIRD_REASONING_TEXT,
                "tool": TOOL_NAME,
                "secondTool": SECOND_TOOL_NAME,
            },
            timeout=10000,
        )
        live_snapshot = _activity_snapshot(page)
        _assert_live_activity(live_snapshot)
        print("OK  live activity: one Anchor worklog with reasoning + completed tool activity")
        _wait_for_live_anchor_projection(page)

        if SCENARIO == "session-reattach":
            active_session_id = live_snapshot["clientState"]["sessionId"]
            active_stream_id = live_snapshot["clientState"]["activeStreamId"]
            assert active_session_id and active_stream_id, live_snapshot
            expected_reasoning_ids = [
                f"live-reasoning:{active_stream_id}:1",
                f"live-reasoning:{active_stream_id}:2",
                f"live-reasoning:{active_stream_id}:3",
            ]
            _wait_for_runtime_scene(base_url, active_session_id, expected_reasoning_ids)
            page.wait_for_selector(
                f'.session-item[data-sid="{active_session_id}"].streaming',
                state="visible",
                timeout=10000,
            )
            page.locator(f'.session-item[data-sid="{seed_session_id}"]').click()
            page.wait_for_function(
                "({sid, text}) => S.session && S.session.session_id === sid && !S.busy && "
                "!S.activeStreamId && !document.querySelector('#liveAssistantTurn') && "
                "(document.querySelector('#msgInner') || {}).innerText?.includes(text)",
                arg={"sid": seed_session_id, "text": SEED_FINAL_TEXT},
                timeout=15000,
            )
            print("OK  session switch: idle session replaces the active live turn")
            page.evaluate(
                """({sid, stream}) => {
                  if (typeof INFLIGHT === 'object' && INFLIGHT) delete INFLIGHT[sid];
                  if (typeof clearInflightState === 'function') clearInflightState(sid);
                  if (window._liveAnchorRegistries) window._liveAnchorRegistries.delete(stream);
                }""",
                {"sid": active_session_id, "stream": active_stream_id},
            )
            if TEST_BITE == "replace-runtime-reasoning-id":
                page.route(
                    "**/api/session*",
                    lambda route: _replace_runtime_reasoning_identity(route, active_session_id, bite_hits),
                )
            page.locator(f'.session-item[data-sid="{active_session_id}"]').click()
            page.wait_for_function(
                "({sid, stream, reasoning, secondReasoning, thirdReasoning, tool, secondTool}) => {"
                "  const turn = document.querySelector('#liveAssistantTurn');"
                "  return S.session && S.session.session_id === sid && S.busy === true && "
                "    S.activeStreamId === stream && turn && "
                "    turn.getAttribute('data-anchor-stream-id') === stream && "
                "    (turn.innerText || '').includes(reasoning) && "
                "    (turn.innerText || '').includes(secondReasoning) && "
                "    (turn.innerText || '').includes(thirdReasoning) && "
                "    turn.querySelectorAll('[data-anchor-row-role=\"thinking\"]').length === 3 && "
                "    Boolean(turn.querySelector(`[data-anchor-row-role=\"tool\"][data-tool-name=\"${tool}\"]`)) && "
                "    Boolean(turn.querySelector(`[data-anchor-row-role=\"tool\"][data-tool-name=\"${secondTool}\"]`));"
                "}",
                arg={
                    "sid": active_session_id,
                    "stream": active_stream_id,
                    "reasoning": REASONING_TEXT,
                    "secondReasoning": SECOND_REASONING_TEXT,
                    "thirdReasoning": THIRD_REASONING_TEXT,
                    "tool": TOOL_NAME,
                    "secondTool": SECOND_TOOL_NAME,
                },
                timeout=15000,
            )
            if TEST_BITE == "replace-runtime-reasoning-id":
                page.unroute("**/api/session*")
                assert len(bite_hits) == 1, bite_hits
            reattached_snapshot = _activity_snapshot(page)
            _assert_same_live_turn(live_snapshot, reattached_snapshot)
            live_snapshot = reattached_snapshot
            print("OK  session reattach: same stream and Anchor activity resume without duplication")

        gateway.release_settle.set()
        if SCENARIO == "session-reattach":
            if not gateway.continuation_ready.wait(timeout=10):
                raise AssertionError("mock Gateway did not emit the post-reattach reasoning continuation")
            page.wait_for_function(
                """text => {
                  const turn = document.querySelector('#liveAssistantTurn');
                  return turn && (turn.innerText || '').includes(text) &&
                    turn.querySelectorAll('[data-anchor-row-role="thinking"]').length === 4;
                }""",
                arg=CONTINUATION_REASONING_TEXT,
                timeout=10000,
            )
            continued_snapshot = _activity_snapshot(page)
            _assert_live_continuation(live_snapshot, continued_snapshot)
            live_snapshot = continued_snapshot
            print("OK  live continuation: post-reattach reasoning appends with a new Anchor identity")
        gateway.release_final_prefix.set()
        if not gateway.final_prefix_ready.wait(timeout=10):
            raise AssertionError("mock Gateway did not emit the final-answer prefix")
        page.wait_for_function(
            """text => {
              const turn = document.querySelector('#liveAssistantTurn');
              return turn && (turn.innerText || '').includes(text);
            }""",
            arg=FINAL_ACK_TEXT,
            timeout=10000,
        )
        gateway.release_terminal.set()
        page.wait_for_function(
            """text => typeof S !== 'undefined' && S.busy === false && !S.activeStreamId &&
              !document.querySelector('#liveAssistantTurn') &&
              (document.querySelector('#msgInner') || {}).innerText?.includes(text)""",
            arg=FINAL_TEXT,
            timeout=15000,
        )
        session_id = page.evaluate("S.session && S.session.session_id")
        assert session_id, "active session id missing after settlement"
        if not TEST_BITE:
            scene = _wait_for_persisted_scene(
                base_url,
                session_id,
                anchor_scene_requests=anchor_scene_requests,
            )
            assert scene.get("version") == "activity_scene_v1", scene
        _expand_settled_worklog(page)
        page.wait_for_selector(
            '.assistant-turn [data-anchor-settled-scene-owner="1"] [data-anchor-scene-row="1"]',
            timeout=10000,
        )
        settled_snapshot = _activity_snapshot(page)
        settled_reasoning_count = 4 if SCENARIO == "session-reattach" else 3
        _assert_settled(settled_snapshot, reasoning_count=settled_reasoning_count)
        assert _semantic_activity(settled_snapshot) == _semantic_activity(live_snapshot), {
            "live": _semantic_activity(live_snapshot),
            "settled": _semantic_activity(settled_snapshot),
        }
        print("OK  settled: final prose and the same semantic activity coexist without duplication")

        page.reload(wait_until="domcontentloaded")
        page.wait_for_function(
            "text => (document.querySelector('#msgInner') || {}).innerText?.includes(text)",
            arg=FINAL_TEXT,
            timeout=15000,
        )
        _expand_settled_worklog(page)
        page.wait_for_selector(
            '.assistant-turn [data-anchor-settled-scene-owner="1"] [data-anchor-scene-row="1"]',
            timeout=2000 if TEST_BITE else 10000,
        )
        reloaded_snapshot = _activity_snapshot(page)
        _assert_settled(reloaded_snapshot, reasoning_count=settled_reasoning_count)
        assert _semantic_activity(reloaded_snapshot) == _semantic_activity(settled_snapshot), {
            "settled": _semantic_activity(settled_snapshot),
            "reloaded": _semantic_activity(reloaded_snapshot),
        }
        print("OK  hard reload: transcript-backed Anchor scene preserves settled parity")

        expected_inputs = [PROMPT] if SCENARIO == "normal" else [SEED_PROMPT, PROMPT]
        actual_inputs = [request.get("input") for request in gateway.request_bodies]
        assert actual_inputs == expected_inputs, actual_inputs
        if errors:
            raise AssertionError(f"unexpected browser errors: {errors!r}")
        context.close()
        browser.close()
        browser = None
        print("\nCONVERSATION LIFECYCLE GATE PASSED")
        exit_code = 0
        return 0
    except Exception as error:
        print(f"\nCONVERSATION LIFECYCLE GATE FAILED: {error}", file=sys.stderr)
        try:
            if page is not None:
                page.screenshot(path=str(artifact_dir / "failure.png"), full_page=True)
                (artifact_dir / "snapshot.json").write_text(
                    json.dumps({
                        "scenario": SCENARIO,
                        "test_bite": TEST_BITE or None,
                        "browser_errors": errors,
                        "anchor_scene_requests": anchor_scene_requests,
                        "anchor_projection": _anchor_projection_snapshot(page),
                        "gateway_events": gateway.emitted_events,
                        "dom": _activity_snapshot(page),
                    }, indent=2),
                    encoding="utf-8",
                )
        except Exception as artifact_error:
            print(f"Could not capture browser artifacts: {artifact_error}", file=sys.stderr)
        print(f"Artifacts: {artifact_dir}", file=sys.stderr)
        exit_code = 1
        return 1
    finally:
        gateway.close()
        if browser is not None:
            browser.close()
        if playwright is not None:
            playwright.stop()
        _terminate_process(proc)
        if log is not None:
            log.close()
        if proc is not None and proc.returncode not in (None, 0, -15):
            print(f"WebUI server exit code: {proc.returncode}", file=sys.stderr)
        if log_path is not None and log_path.exists():
            tail = log_path.read_text(encoding="utf-8", errors="replace")[-4000:]
            if tail and proc is not None and proc.returncode not in (None, 0, -15):
                print(tail, file=sys.stderr)
        state_tmp.cleanup()
        if artifact_dir_owned and exit_code == 0:
            shutil.rmtree(artifact_dir, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
