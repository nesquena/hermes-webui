#!/usr/bin/env python3
"""Public browser gate for the normal + terminal-error lifecycle.

This test boots the real WebUI server with isolated state, drives the real chat
composer in Chromium, and supplies deterministic runtime events through the
existing Hermes Gateway Runs API. It proves that one assistant turn keeps the
same semantic activity across live streaming, settlement, and a hard reload.
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
from urllib.parse import urlsplit


PROMPT = "Exercise the public conversation lifecycle gate."
REASONING_TEXT = "Checking the persistent assistant turn."
FINAL_TEXT = "Lifecycle gate final answer."
FINAL_ACK_TEXT = "Lifecycle"
FINAL_PREFIX = "Lifecycle gate "
FINAL_SUFFIX = "final answer."
TERMINAL_PROCESS_TEXT = "Lifecycle terminal process check"
TERMINAL_ERROR_TEXT = "Lifecycle gate encountered a terminal-side error."
SCENARIO = os.environ.get("LIFECYCLE_SCENARIO", "normal").strip() or "normal"
TOOL_NAME = "read_file"
TOOL_ID = "lifecycle-tool-1"
TEST_BITE = os.environ.get("LIFECYCLE_TEST_BITE", "").strip()
GATEWAY_ACTIVITY_TIMEOUT = 60.0
ANCHOR_SCENE_PERSIST_TIMEOUT = 60.0
ANCHOR_SCENE_PROJECTION_TIMEOUT = 10_000


def _latest_anchor_scene_from_disk(state_root: Path, session_id: str) -> dict | None:
    session_file_candidates = [
        state_root / "webui-state" / "sessions" / f"{session_id}.json",
        state_root / "sessions" / f"{session_id}.json",
    ]
    for session_file in session_file_candidates:
        if not session_file.exists():
            continue
        try:
            raw = json.loads(session_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        records = raw.get("anchor_activity_scenes")
        if not isinstance(records, dict):
            continue
        candidates = []
        for record in records.values():
            if not isinstance(record, dict):
                continue
            scene = record.get("scene")
            if not isinstance(scene, dict):
                continue
            idx = record.get("message_index")
            try:
                message_index = int(idx)
            except (TypeError, ValueError):
                message_index = -1
            candidates.append((message_index, scene))
        if not candidates:
            continue
        _, latest = max(candidates, key=lambda item: item[0])
        return latest
    return None


def _safe_request_post_data(request_or_route_request) -> str:
    raw = getattr(request_or_route_request, "post_data", None)
    if callable(raw):
        raw = raw()
    if raw is None:
        return ""
    if isinstance(raw, bytes):
        return raw.decode("utf-8")
    return str(raw)


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

    def __init__(self, scenario: str) -> None:
        self.scenario = scenario
        self.activity_ready = threading.Event()
        self.release_settle = threading.Event()
        self.final_prefix_ready = threading.Event()
        self.release_terminal = threading.Event()
        self.request_body = None
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
                    if owner.scenario == "terminal-error":
                        self._event("message.delta", {
                            "event": "message.delta",
                            "delta": TERMINAL_PROCESS_TEXT,
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
                    owner.activity_ready.set()
                    if not owner.release_settle.wait(timeout=30):
                        return
                    if owner.scenario == "terminal-error":
                        owner.final_prefix_ready.set()
                        if not owner.release_terminal.wait(timeout=30):
                            return
                        self._event("run.failed", {
                            "event": "run.failed",
                            "error": TERMINAL_ERROR_TEXT,
                        })
                    else:
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
                owner.request_body = json.loads(self.rfile.read(length) or b"{}")
                self._json({"run_id": "lifecycle-run-1"})

        return Handler

    def start(self) -> None:
        self._thread.start()

    def close(self) -> None:
        self.release_settle.set()
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
          const messages = (typeof S !== 'undefined' && Array.isArray(S.messages)) ? S.messages : [];
          const assistants = messages.filter((message) => message && message.role === 'assistant');
          const lastAssistant = assistants.length ? assistants[assistants.length - 1] : null;
          const turn = document.querySelector('#liveAssistantTurn') ||
            Array.from(document.querySelectorAll('.assistant-turn')).pop() || null;
          const groups = turn ? Array.from(turn.querySelectorAll('[data-anchor-scene-owner="1"]')) : [];
          const rows = turn ? Array.from(turn.querySelectorAll('[data-anchor-scene-row="1"]')) : [];
          const visibleFinal = turn ? Array.from(turn.querySelectorAll('.assistant-segment .msg-body'))
            .filter(el => {
              const segment = el.closest('.assistant-segment');
              const role = segment && segment.getAttribute('data-anchor-row-role');
              return segment && !segment.hidden &&
                role !== 'prose' &&
                !segment.classList.contains('assistant-segment-worklog-source') &&
                getComputedStyle(segment).display !== 'none';
            })
            .map(el => el.innerText.trim()).filter(Boolean) : [];
          return {
            live: Boolean(document.querySelector('#liveAssistantTurn')),
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
              expanded: (group.querySelector('.tool-worklog-summary,.tool-call-group-summary') || {})
                .getAttribute?.('aria-expanded') || '',
            })),
            rows: rows.map(row => ({
              role: row.getAttribute('data-anchor-row-role'),
              source: row.getAttribute('data-anchor-source-event-type'),
              status: row.getAttribute('data-anchor-row-status'),
              tool: row.getAttribute('data-tool-name'),
              text: row.innerText.trim(),
              classes: row.className,
            })),
            visibleFinal,
            assistantMessage: lastAssistant ? {
              turnDuration: lastAssistant._turnDuration,
              hasError: Boolean(lastAssistant._error),
              anchorTerminalState: lastAssistant._anchor_activity_scene
                && (lastAssistant._anchor_activity_scene.terminal_state
                  || (lastAssistant._anchor_activity_scene.lifecycle
                    && lastAssistant._anchor_activity_scene.lifecycle.terminal_state)) || null,
            } : null,
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


def _terminal_rows(snapshot: dict) -> list[dict]:
    return [row for row in snapshot["rows"] if row["role"] == "terminal"]


def _process_rows(snapshot: dict) -> list[dict]:
    return [
        row for row in snapshot["rows"]
        if row["role"] == "prose" and _is_terminal_process_row_text(row.get("text") or "")
    ]


def _is_terminal_process_row_text(text: str) -> bool:
    text = " ".join(text.split())
    if not text:
        return False
    return (
        TERMINAL_PROCESS_TEXT.startswith(text)
        or text.startswith(TERMINAL_PROCESS_TEXT)
    )


def _tool_rows(snapshot: dict) -> list[dict]:
    return [row for row in snapshot["rows"] if row["role"] == "tool"]


def _is_terminal_row_error(row: dict) -> bool:
    status = (row.get("status") or "").strip()
    if status in {"error", "failed"}:
        return True
    if status and status not in {"", "done"}:
        return False
    row_text = (row.get("text") or "").lower()
    row_classes = (row.get("classes") or "")
    return (
        "error" in row_text
        or "warning" in row_classes
        or "agent-activity-status-error" in row_classes
    )


def _assert_no_running_tool_rows(rows: list[dict]) -> None:
    running = [
        row
        for row in rows
        if (
            row.get("status") == "running"
            or ("tool-card-running" in row.get("classes", ""))
        )
    ]
    assert not running, rows


def _assert_live_activity(snapshot: dict) -> None:
    assert snapshot["live"], snapshot
    assert snapshot["groupCount"] == 1, snapshot
    roles = [row["role"] for row in snapshot["rows"]]
    assert roles.count("thinking") == 1, snapshot
    assert roles.count("tool") == 1, snapshot
    tool_rows = _tool_rows(snapshot)
    assert len(tool_rows) == 1 and tool_rows[0]["tool"] == TOOL_NAME, snapshot
    _assert_no_running_tool_rows(tool_rows)
    assert any(REASONING_TEXT in row["text"] for row in snapshot["rows"]), snapshot
    assert all(FINAL_TEXT not in text for text in snapshot["visibleFinal"]), snapshot
    assert any(character.isdigit() for character in snapshot["summary"][0]["label"]), snapshot


def _assert_settled(snapshot: dict, scenario: str) -> None:
    assert not snapshot["live"], snapshot
    assert snapshot["groupCount"] == 1, snapshot
    roles = [row["role"] for row in snapshot["rows"]]
    assert "thinking" in roles and "tool" in roles, snapshot
    tool_rows = _tool_rows(snapshot)
    assert len(tool_rows) == 1 and tool_rows[0]["tool"] == TOOL_NAME, snapshot
    _assert_no_running_tool_rows(tool_rows)
    assert any(REASONING_TEXT in row["text"] for row in snapshot["rows"]), snapshot
    if scenario == "terminal-error":
        assert "terminal" in roles, snapshot
        terminal_rows = _terminal_rows(snapshot)
        assert terminal_rows, snapshot
        assert all(_is_terminal_row_error(row) for row in terminal_rows), snapshot
        assert snapshot["assistantMessage"] is not None, snapshot
        turn_duration = snapshot["assistantMessage"].get("turnDuration")
        assert isinstance(turn_duration, (int, float)) and turn_duration > 0, snapshot
        assert snapshot["assistantMessage"]["hasError"] is True, snapshot
        assert snapshot["assistantMessage"]["anchorTerminalState"] in {"error", "failed"}, snapshot
        assert all(FINAL_TEXT not in text for text in snapshot["visibleFinal"]), snapshot
        assert sum(TERMINAL_ERROR_TEXT in text for text in snapshot["visibleFinal"]) == 1, snapshot
        assert TERMINAL_ERROR_TEXT in snapshot["transcript"], snapshot
    else:
        assert sum(FINAL_TEXT in text for text in snapshot["visibleFinal"]) == 1, snapshot
        assert snapshot["transcript"].count(FINAL_TEXT) == 1, snapshot


def _parse_json_payload(raw: str | bytes | None) -> dict:
    if raw is None:
        return {}
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8")
    if not raw:
        return {}
    try:
        payload = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return {}
    if not isinstance(payload, dict):
        return {}
    return payload


def _assert_process_row_present(snapshot: dict) -> list[dict]:
    rows = _process_rows(snapshot)
    assert len(rows) == 1, snapshot
    assert all(_is_terminal_process_row_text(row["text"]) for row in rows), snapshot
    assert all(TERMINAL_PROCESS_TEXT not in text for text in snapshot["visibleFinal"]), snapshot
    return rows


def _semantic_activity(snapshot: dict) -> list[dict]:
    """Canonical user-visible activity, independent of renderer row ordering."""
    semantic = []
    for row in snapshot["rows"]:
        if row["role"] == "thinking":
            text = " ".join(row["text"].split())
            if text.startswith("Thinking "):
                text = text[len("Thinking ") :]
            semantic.append({"role": "thinking", "text": text})
        elif row["role"] == "tool":
            semantic.append({"role": "tool", "tool": row["tool"]})
    return sorted(semantic, key=lambda item: json.dumps(item, sort_keys=True))


def main() -> int:
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
    scenario = SCENARIO
    if scenario not in {"normal", "terminal-error"}:
        raise ValueError(
            f"Unsupported LIFECYCLE_SCENARIO {scenario!r}; "
            "expected 'normal' or 'terminal-error'"
        )
    if TEST_BITE not in {"", "drop-anchor-persistence", "drop-terminal-anchor-row"}:
        raise ValueError(
            f"Unsupported LIFECYCLE_TEST_BITE {TEST_BITE!r}; "
            "expected one of '', 'drop-anchor-persistence', 'drop-terminal-anchor-row'"
        )
    if TEST_BITE == "drop-terminal-anchor-row" and scenario != "terminal-error":
        raise ValueError(
            "drop-terminal-anchor-row is only valid for "
            "LIFECYCLE_SCENARIO=terminal-error"
        )

    gateway = DeterministicGateway(scenario)
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
        if TEST_BITE:
            def _route_anchor_scene(route):
                if TEST_BITE == "drop-anchor-persistence":
                    route.fulfill(
                        status=200,
                        content_type="application/json",
                        body='{"ok":true}',
                    )
                    return
                if TEST_BITE == "drop-terminal-anchor-row":
                    raw_payload = _safe_request_post_data(route.request)
                    payload = _parse_json_payload(raw_payload)
                    if isinstance(payload, dict):
                        scene = payload.get("scene")
                        if isinstance(scene, dict):
                            rows = scene.get("activity_rows")
                            if isinstance(rows, list):
                                kept = [
                                    row for row in rows
                                    if not (isinstance(row, dict) and row.get("role") == "terminal")
                                ]
                                if len(kept) != len(rows):
                                    mutated = dict(payload)
                                    updated_scene = dict(scene)
                                    updated_scene["activity_rows"] = kept
                                    mutated["scene"] = updated_scene
                                    response = route.fetch(post_data=json.dumps(mutated))
                                    route.fulfill(response=response)
                                    return
                    response = route.fetch()
                    route.fulfill(response=response)
                    return
                response = route.fetch()
                route.fulfill(response=response)
                return

            page.route("**/api/session/anchor-scene", _route_anchor_scene)
        errors = _capture_page_errors(page)
        page.goto("/", wait_until="domcontentloaded")
        page.wait_for_selector("#msg", state="visible", timeout=15000)
        page.locator("#msg").fill(PROMPT)
        page.locator("#btnSend").click()

        if not gateway.activity_ready.wait(timeout=GATEWAY_ACTIVITY_TIMEOUT):
            raise AssertionError(
                "mock Gateway did not reach the live activity checkpoint; "
                f"request body: {gateway.request_body!r}; events: {gateway.emitted_events!r}"
            )
        page.wait_for_function(
            """({reasoning, tool}) => {
              const turn = document.querySelector('#liveAssistantTurn');
              if (!turn) return false;
              const text = turn.innerText || '';
              return text.includes(reasoning) &&
                Boolean(turn.querySelector(`[data-anchor-row-role="tool"][data-tool-name="${tool}"]`));
            }""",
            arg={"reasoning": REASONING_TEXT, "tool": TOOL_NAME},
            timeout=10000,
        )
        live_snapshot = _activity_snapshot(page)
        _assert_live_activity(live_snapshot)
        if scenario == "terminal-error":
            _assert_process_row_present(live_snapshot)
            print("OK  live activity: terminal-error run keeps reasoning + completed tool")
        else:
            print("OK  live activity: one Anchor worklog with reasoning + completed tool")
        _wait_for_live_anchor_projection(page)

        gateway.release_settle.set()
        if not gateway.final_prefix_ready.wait(timeout=10):
            raise AssertionError("mock Gateway did not emit the final-answer prefix")
        if scenario == "normal":
            page.wait_for_function(
                """text => {
                  const turn = document.querySelector('#liveAssistantTurn');
                  return Boolean(turn) && turn.innerText.includes(text);
                }""",
                arg=FINAL_ACK_TEXT,
                timeout=10000,
            )
            gateway.release_terminal.set()
            page.wait_for_function(
                """text => typeof S !== 'undefined' && S.busy === false && !S.activeStreamId &&
                  ((document.querySelector('#msgInner') || {}).innerText || '').includes(text)""",
                arg=FINAL_TEXT,
                timeout=15000,
            )
        else:
            gateway.release_terminal.set()
            page.wait_for_function(
                """text => typeof S !== 'undefined' && S.busy === false && !S.activeStreamId &&
                  !document.querySelector('#liveAssistantTurn') &&
                  ((document.querySelector('#msgInner') || {}).innerText||'').includes(text)""",
                arg=TERMINAL_ERROR_TEXT,
                timeout=15000,
            )
        session_id = page.evaluate("S.session && S.session.session_id")
        assert session_id, "active session id missing after settlement"
        if TEST_BITE == "drop-terminal-anchor-row":
            scene = _wait_for_persisted_scene(
                base_url,
                session_id,
                anchor_scene_requests=anchor_scene_requests,
            )
            assert scene.get("version") == "activity_scene_v1", scene
            scene_rows = scene.get("activity_rows") or []
            scene_roles = [
                row.get("role") for row in scene_rows
                if isinstance(row, dict)
            ]
            assert any(
                isinstance(row, dict) and row.get("role") == "prose"
                for row in scene_rows
            ), scene
            assert any(
                isinstance(row, dict) and row.get("role") == "thinking"
                for row in scene_rows
            ), scene
            assert any(
                isinstance(row, dict) and row.get("role") == "tool"
                for row in scene_rows
            ), scene
            assert all(
                not (
                    isinstance(row, dict)
                    and row.get("role") == "terminal"
                )
                for row in scene_rows
            ), scene
            print(
                "OK  persisted scene via API: roles=%s terminal_present=%s"
                % (sorted(set(scene_roles)), any(role == "terminal" for role in scene_roles))
            )
            persisted_scene = _latest_anchor_scene_from_disk(state_dir, session_id)
            assert persisted_scene is not None, {
                "session_id": session_id,
                "state_dir": str(state_dir / "webui-state"),
            }
            persisted_rows = persisted_scene.get("activity_rows") or []
            persisted_roles = [
                row.get("role") for row in persisted_rows
                if isinstance(row, dict)
            ]
            assert any(
                isinstance(row, dict) and row.get("role") == "thinking"
                for row in persisted_rows
            ), {
                "persisted_rows": persisted_rows,
            }
            assert any(
                isinstance(row, dict) and row.get("role") == "prose"
                for row in persisted_rows
            ), {
                "persisted_rows": persisted_rows,
            }
            assert any(
                isinstance(row, dict) and row.get("role") == "tool"
                for row in persisted_rows
            ), {
                "persisted_rows": persisted_rows,
            }
            assert all(
                not (
                    isinstance(row, dict)
                    and row.get("role") == "terminal"
                )
                for row in persisted_rows
            ), {
                "persisted_rows": persisted_rows,
            }
            print(
                "OK  persisted scene on disk: roles=%s terminal_present=%s"
                % (sorted(set(persisted_roles)), "terminal" in persisted_roles)
            )
        elif not TEST_BITE:
            scene = _wait_for_persisted_scene(
                base_url,
                session_id,
                anchor_scene_requests=anchor_scene_requests,
            )
            assert scene.get("version") == "activity_scene_v1", scene
            if scenario == "terminal-error":
                scene_rows = scene.get("activity_rows") or []
                assert any(
                    isinstance(row, dict) and row.get("role") == "terminal" for row in scene_rows
                ), scene
        _expand_settled_worklog(page)
        page.wait_for_selector(
            '.assistant-turn [data-anchor-settled-scene-owner="1"] [data-anchor-scene-row="1"]',
            timeout=10000,
        )
        settled_snapshot = _activity_snapshot(page)
        _assert_settled(settled_snapshot, scenario)
        if scenario == "terminal-error":
            _assert_process_row_present(settled_snapshot)
        assert _semantic_activity(settled_snapshot) == _semantic_activity(live_snapshot), {
            "live": _semantic_activity(live_snapshot),
            "settled": _semantic_activity(settled_snapshot),
        }
        if scenario == "terminal-error":
            print("OK  settled: terminal row and same activity survived terminal settlement")
        else:
            print("OK  settled: final prose and the same semantic activity coexist without duplication")

        page.reload(wait_until="domcontentloaded")
        page.wait_for_function(
            "text => (document.querySelector('#msgInner') || {}).innerText?.includes(text)",
            arg=TERMINAL_ERROR_TEXT if scenario == "terminal-error" else FINAL_TEXT,
            timeout=15000,
        )
        _expand_settled_worklog(page)
        page.wait_for_selector(
            '.assistant-turn [data-anchor-settled-scene-owner="1"] [data-anchor-scene-row="1"]',
            timeout=2000 if TEST_BITE else 10000,
        )
        reloaded_snapshot = _activity_snapshot(page)
        _assert_settled(reloaded_snapshot, scenario)
        if scenario == "terminal-error":
            _assert_process_row_present(reloaded_snapshot)
        assert _semantic_activity(reloaded_snapshot) == _semantic_activity(settled_snapshot), {
            "settled": _semantic_activity(settled_snapshot),
            "reloaded": _semantic_activity(reloaded_snapshot),
        }
        if scenario == "terminal-error":
            settled_terminal = _terminal_rows(settled_snapshot)
            reloaded_terminal = _terminal_rows(reloaded_snapshot)
            settled_process = _process_rows(settled_snapshot)
            reloaded_process = _process_rows(reloaded_snapshot)
            assert len(settled_process) == len(reloaded_process) == 1, {
                "settled_process": settled_process,
                "reloaded_process": reloaded_process,
            }
            assert settled_process[0]["text"] == reloaded_process[0]["text"], {
                "settled_process": settled_process,
                "reloaded_process": reloaded_process,
            }
            assert len(settled_terminal) == len(reloaded_terminal) == 1, {
                "settled_terminal": settled_terminal,
                "reloaded_terminal": reloaded_terminal,
            }
            assert settled_terminal[0]["text"] == reloaded_terminal[0]["text"], {
                "settled_terminal": settled_terminal[0],
                "reloaded_terminal": reloaded_terminal[0],
            }
        print("OK  hard reload: transcript-backed Anchor scene preserves settled parity")

        assert gateway.request_body and gateway.request_body.get("input") == PROMPT, gateway.request_body
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
                        "scenario": scenario,
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
