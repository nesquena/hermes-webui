"""Slow request diagnostics for latency-sensitive browser API paths."""

from __future__ import annotations

import json
import logging
import os
import sys
import threading
import time
import traceback
import uuid
from typing import Any


DEFAULT_SLOW_REQUEST_SECONDS = 5.0
MAX_STACK_FRAMES_PER_THREAD = 40


def _slow_request_seconds() -> float:
    raw = os.getenv("HERMES_WEBUI_SLOW_REQUEST_SECONDS", "").strip()
    if not raw:
        return DEFAULT_SLOW_REQUEST_SECONDS
    try:
        value = float(raw)
    except ValueError:
        return DEFAULT_SLOW_REQUEST_SECONDS
    return max(0.0, value)


class RequestDiagnostics:
    """Track request stages and emit a watchdog record if a request wedges."""

    def __init__(
        self,
        method: str,
        path: str,
        *,
        logger: logging.Logger | None = None,
        timeout_seconds: float | None = None,
        auto_start: bool = True,
    ) -> None:
        self.request_id = uuid.uuid4().hex[:10]
        self.method = str(method or "-")
        self.path = str(path or "-").split("?", 1)[0]
        self.logger = logger or logging.getLogger(__name__)
        self.timeout_seconds = _slow_request_seconds() if timeout_seconds is None else max(0.0, float(timeout_seconds))
        self.started_monotonic = time.monotonic()
        self.started_wall = time.time()
        self._lock = threading.Lock()
        self._stages: list[dict[str, Any]] = []
        self._current_stage = "start"
        self._current_stage_started = self.started_monotonic
        self._finished = False
        self._watchdog_logged = False
        self._timer: threading.Timer | None = None
        if auto_start and self.timeout_seconds > 0:
            self._timer = threading.Timer(self.timeout_seconds, self._on_timeout)
            self._timer.daemon = True
            self._timer.start()

    @classmethod
    def maybe_start(
        cls,
        method: str,
        path: str,
        *,
        logger: logging.Logger | None = None,
    ) -> "RequestDiagnostics | None":
        clean_path = str(path or "").split("?", 1)[0]
        if (method.upper(), clean_path) not in {
            ("GET", "/api/sessions"),
            ("POST", "/api/chat/start"),
        }:
            return None
        return cls(method, clean_path, logger=logger)

    def stage(self, name: str) -> None:
        now = time.monotonic()
        clean = str(name or "unknown").strip() or "unknown"
        with self._lock:
            if self._finished:
                return
            self._stages.append(
                {
                    "name": self._current_stage,
                    "ms": round((now - self._current_stage_started) * 1000, 1),
                }
            )
            self._current_stage = clean
            self._current_stage_started = now

    def finish(self) -> None:
        timer = None
        record = None
        with self._lock:
            if self._finished:
                return
            self._finished = True
            timer = self._timer
            record = self._build_record_locked(include_stacks=False)
        if timer is not None:
            timer.cancel()
        if record and self.timeout_seconds > 0 and record["elapsed_ms"] >= self.timeout_seconds * 1000:
            self.logger.warning(
                "Slow WebUI request completed: %s",
                json.dumps(record, sort_keys=True),
            )

    def _on_timeout(self) -> None:
        with self._lock:
            if self._finished or self._watchdog_logged:
                return
            self._watchdog_logged = True
            record = self._build_record_locked(include_stacks=True)
        self.logger.warning(
            "Slow WebUI request still running: %s",
            json.dumps(record, sort_keys=True),
        )

    def _build_record_locked(self, *, include_stacks: bool) -> dict[str, Any]:
        now = time.monotonic()
        stages = list(self._stages)
        stages.append(
            {
                "name": self._current_stage,
                "ms": round((now - self._current_stage_started) * 1000, 1),
            }
        )
        record: dict[str, Any] = {
            "request_id": self.request_id,
            "method": self.method,
            "path": self.path,
            "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(self.started_wall)),
            "elapsed_ms": round((now - self.started_monotonic) * 1000, 1),
            "current_stage": self._current_stage,
            "stages": stages,
        }
        if include_stacks:
            record["thread_stacks"] = _thread_stack_snapshot()
        return record


def _thread_stack_snapshot() -> list[dict[str, Any]]:
    frames = sys._current_frames()
    threads = {thread.ident: thread for thread in threading.enumerate()}
    snapshot: list[dict[str, Any]] = []
    for ident, frame in frames.items():
        thread = threads.get(ident)
        stack = traceback.format_stack(frame, limit=MAX_STACK_FRAMES_PER_THREAD)
        snapshot.append(
            {
                "thread_id": ident,
                "thread_name": thread.name if thread else "",
                "daemon": bool(thread.daemon) if thread else None,
                "stack": [line.rstrip() for line in stack],
            }
        )
    snapshot.sort(key=lambda item: str(item.get("thread_name") or ""))
    return snapshot
