"""Minimal in-memory runner backend skeleton for the runner-local seam.

The class here models the runner-side contract without wiring the default WebUI
request process to a new execution owner. It is deliberately process-local to the
runner backend object only; callers that need restart survival should run it in a
supervised backend and later replace the storage with durable runner-owned state.
"""
from __future__ import annotations

from dataclasses import asdict
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import itertools
import threading
import time
from typing import Any
from urllib.parse import parse_qs, urlparse


def _request_payload(value: Any) -> dict[str, Any]:
    if hasattr(value, "__dataclass_fields__") and not isinstance(value, type):
        return asdict(value)
    if isinstance(value, dict):
        return dict(value)
    return {}


class InMemoryRunnerBackend:
    """Tiny runner-side backend implementing the HTTP client contract methods."""

    def __init__(self):
        self._lock = threading.Lock()
        self._counter = itertools.count(1)
        self._runs: dict[str, dict[str, Any]] = {}
        self._events: dict[str, list[dict[str, Any]]] = {}
        self._session_runs: dict[str, str] = {}

    def start_run(self, request: Any) -> dict[str, Any]:
        payload = _request_payload(request)
        session_id = str(payload.get("session_id") or "")
        run_id = f"runner-local-{next(self._counter)}"
        now = time.time()
        event = {
            "event_id": f"{run_id}:1",
            "seq": 1,
            "run_id": run_id,
            "session_id": session_id,
            "type": "run.started",
            "created_at": now,
            "terminal": False,
            "payload": {
                "status": "running",
                "workspace": payload.get("workspace"),
                "profile": payload.get("profile"),
                "model": payload.get("model"),
                "provider": payload.get("provider"),
            },
        }
        status = {
            "run_id": run_id,
            "session_id": session_id,
            "stream_id": run_id,
            "status": "running",
            "started_at": now,
            "last_event_id": event["event_id"],
            "terminal_state": None,
            "active_controls": ["cancel"],
            "pending_approval_id": None,
            "pending_clarify_id": None,
        }
        with self._lock:
            self._runs[run_id] = status
            self._events[run_id] = [event]
            if session_id:
                self._session_runs[session_id] = run_id
        return dict(status)

    def observe_run(self, run_id: str, *, cursor: str | None = None) -> dict[str, Any]:
        after_seq = _cursor_to_seq(cursor)
        with self._lock:
            events = [dict(event) for event in self._events.get(run_id, []) if int(event.get("seq") or 0) > after_seq]
            status = self._runs.get(run_id, {})
        cursor_value = str(events[-1]["seq"]) if events else (cursor or "")
        return {
            "run_id": run_id,
            "events": events,
            "cursor": cursor_value,
            "last_event_id": events[-1]["event_id"] if events else status.get("last_event_id"),
        }

    def get_run(self, run_id: str) -> dict[str, Any]:
        with self._lock:
            return dict(self._runs.get(run_id, {"run_id": run_id, "status": "unknown", "active_controls": []}))

    def latest_run_for_session(self, session_id: str) -> dict[str, Any] | None:
        with self._lock:
            run_id = self._session_runs.get(session_id)
            if not run_id:
                return None
            return dict(self._runs.get(run_id, {}))

    def cancel_run(self, run_id: str) -> dict[str, Any]:
        now = time.time()
        with self._lock:
            status = self._runs.get(run_id)
            if not status or status.get("terminal_state"):
                return {"ok": False, "status": "not-active", "message": "Run is not active."}
            next_seq = len(self._events.get(run_id, [])) + 1
            event = {
                "event_id": f"{run_id}:{next_seq}",
                "seq": next_seq,
                "run_id": run_id,
                "session_id": status.get("session_id"),
                "type": "cancelled",
                "created_at": now,
                "terminal": True,
                "payload": {"ok": True},
            }
            self._events.setdefault(run_id, []).append(event)
            status.update(
                {
                    "status": "cancelled",
                    "terminal_state": "cancelled",
                    "last_event_id": event["event_id"],
                    "active_controls": [],
                }
            )
        return {"ok": True, "status": "cancelled", "event_id": event["event_id"]}

    def respond_approval(self, run_id: str, approval_id: str, choice: str) -> dict[str, Any]:
        return {"ok": False, "status": "unsupported", "message": "Approval is not supported by this runner backend."}

    def respond_clarify(self, run_id: str, clarify_id: str, response: str) -> dict[str, Any]:
        return {"ok": False, "status": "unsupported", "message": "Clarify is not supported by this runner backend."}

    def queue_message(self, run_id: str, message: str, *, mode: str = "queue") -> dict[str, Any]:
        return {"ok": False, "status": "unsupported", "message": "Queue is not supported by this runner backend."}

    def update_goal(self, session_id: str, action: str, text: str = "") -> dict[str, Any]:
        return {"ok": False, "status": "unsupported", "message": "Goal is not supported by this runner backend."}


def make_runner_handler(backend: InMemoryRunnerBackend | None = None) -> type[BaseHTTPRequestHandler]:
    """Return a small HTTP handler exposing the runner client endpoint shape."""
    runner = backend or InMemoryRunnerBackend()

    class RunnerBackendHandler(BaseHTTPRequestHandler):
        def log_message(self, format, *args):  # pragma: no cover - embedder owns logging
            return

        def _read_json(self) -> dict[str, Any]:
            length = int(self.headers.get("Content-Length") or "0")
            if length <= 0:
                return {}
            try:
                parsed = json.loads(self.rfile.read(length).decode("utf-8"))
            except json.JSONDecodeError:
                return {}
            return parsed if isinstance(parsed, dict) else {}

        def _send_json(self, payload: dict[str, Any], status: int = 200) -> None:
            body = json.dumps(payload).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self) -> None:
            body = self._read_json()
            parsed = urlparse(self.path)
            parts = [part for part in parsed.path.split("/") if part]
            if parts == ["v1", "runs"]:
                return self._send_json(runner.start_run(body))
            if len(parts) == 4 and parts[:2] == ["v1", "runs"] and parts[3] == "cancel":
                return self._send_json(runner.cancel_run(parts[2]))
            if len(parts) == 5 and parts[:2] == ["v1", "runs"] and parts[3] == "approval":
                return self._send_json(runner.respond_approval(parts[2], parts[4], str(body.get("choice") or "")))
            if len(parts) == 5 and parts[:2] == ["v1", "runs"] and parts[3] == "clarify":
                return self._send_json(runner.respond_clarify(parts[2], parts[4], str(body.get("response") or "")))
            if len(parts) == 4 and parts[:2] == ["v1", "runs"] and parts[3] == "queue":
                return self._send_json(runner.queue_message(parts[2], str(body.get("message") or ""), mode=str(body.get("mode") or "queue")))
            if len(parts) == 4 and parts[:2] == ["v1", "sessions"] and parts[3] == "goal":
                return self._send_json(runner.update_goal(parts[2], str(body.get("action") or "status"), str(body.get("text") or "")))
            return self._send_json({"error": "not found"}, status=404)

        def do_GET(self) -> None:
            parsed = urlparse(self.path)
            parts = [part for part in parsed.path.split("/") if part]
            if len(parts) == 4 and parts[:2] == ["v1", "runs"] and parts[3] == "events":
                cursor = (parse_qs(parsed.query).get("cursor") or [None])[0]
                return self._send_json(runner.observe_run(parts[2], cursor=cursor))
            if len(parts) == 3 and parts[:2] == ["v1", "runs"]:
                return self._send_json(runner.get_run(parts[2]))
            if len(parts) == 4 and parts[:2] == ["v1", "sessions"] and parts[3] == "latest-run":
                return self._send_json(runner.latest_run_for_session(parts[2]) or {"status": "unknown"})
            return self._send_json({"error": "not found"}, status=404)

    return RunnerBackendHandler


def make_runner_server(host: str = "127.0.0.1", port: int = 0, *, backend: InMemoryRunnerBackend | None = None) -> ThreadingHTTPServer:
    """Create, but do not start, an embeddable runner HTTP server."""
    return ThreadingHTTPServer((host, port), make_runner_handler(backend))


def _cursor_to_seq(cursor: str | None) -> int:
    if cursor in (None, ""):
        return 0
    try:
        text = str(cursor)
        if ":" in text:
            text = text.rsplit(":", 1)[-1]
        return max(0, int(text))
    except (TypeError, ValueError):
        return 0
