#!/usr/bin/env python3
"""Public browser gate for gateway terminal steer-leftover delivery (#7440).

Boots the real WebUI server with isolated state, drives the real chat
composer in Chromium, and supplies deterministic runtime events through a
fake Hermes Gateway Runs API — the same harness pattern as
browser_conversation_lifecycle.py.

Proves the greptile P1/P2 chain as observable browser behavior, through the
application's real stream and session-switch lifecycle (no source slicing,
no stubbed collaborators):

  1. A gateway run for session A completes with a terminal
     ``run.completed { output, pending_steer }`` while the user is viewing a
     DIFFERENT session B. The leftover guidance must be queued for the
     OWNING session A (observable: the browser's persisted per-session queue
     storage ``hermes-queue-<sid>``), not dropped and not landed on B.
  2. When the user returns to A and completes one more turn, the real
     setBusy(false) queue drain auto-sends the leftover as the owning
     session's next run (observable: the fake Gateway's captured
     ``POST /v1/runs`` request body carries the leftover text for A).
  3. Session B's queue stays empty (no cross-session leakage).
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parent))
from browser_conversation_lifecycle import (  # noqa: E402
    _capture_page_errors,
    _start_webui_server,
    _terminate_process,
)

PROMPT_A1 = "Exercise the gateway steer-leftover browser gate."
FOLLOWUP_A2 = "Continue with the second turn."
LEFTOVER_TEXT = "use the safer path"
GATEWAY_ACTIVITY_TIMEOUT = 60.0
QUEUE_WRITE_TIMEOUT = 20.0
DRAIN_DELIVERY_TIMEOUT = 45.0


class SteerLeftoverGateway:
    """ localhost-only Gateway Runs server with a test-controlled completion gate.

    Run 1 streams one delta, then blocks until the test releases completion —
    at which point it sends ``run.completed`` carrying ``pending_steer`` (the
    agent's terminal unconsumed-guidance field). Every later run completes
    immediately, so the queue drain's auto-send is observable as a captured
    request body.
    """

    def __init__(self) -> None:
        self.release_completion = threading.Event()
        self.first_delta_sent = threading.Event()
        self.completion_sent = threading.Event()
        self.request_bodies: list[dict] = []
        self._lock = threading.Lock()
        self._run_counter = 0
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), self._handler())
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    @property
    def base_url(self) -> str:
        host, port = self._server.server_address
        return f"http://{host}:{port}"

    def _next_run_id(self) -> str:
        with self._lock:
            self._run_counter += 1
            return f"leftover-run-{self._run_counter}"

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
                if not request_path.startswith("/v1/runs/") or not request_path.endswith("/events"):
                    self._json({"error": "not found"}, status=404)
                    return
                run_id = request_path[len("/v1/runs/"):-len("/events")]
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-cache")
                self.send_header("Connection", "close")
                self.end_headers()
                try:
                    if run_id == "leftover-run-1":
                        self._event("message.delta", {
                            "event": "message.delta",
                            "delta": "working through the turn",
                        })
                        owner.first_delta_sent.set()
                        if not owner.release_completion.wait(timeout=60):
                            return
                        self._event("run.completed", {
                            "event": "run.completed",
                            "output": "first turn done",
                            "pending_steer": LEFTOVER_TEXT,
                            "usage": {"input_tokens": 7, "output_tokens": 3},
                        })
                        owner.completion_sent.set()
                    else:
                        self._event("message.delta", {
                            "event": "message.delta",
                            "delta": "ok",
                        })
                        self._event("run.completed", {
                            "event": "run.completed",
                            "output": "ok",
                            "usage": {"input_tokens": 2, "output_tokens": 1},
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
                try:
                    body = json.loads(self.rfile.read(length) or b"{}")
                except json.JSONDecodeError:
                    body = {}
                run_id = owner._next_run_id()
                with owner._lock:
                    owner.request_bodies.append({
                        "run_id": run_id,
                        "session_id": str(body.get("session_id") or ""),
                        "input": body.get("input"),
                    })
                self._json({"run_id": run_id})

        return Handler

    def start(self) -> None:
        self._thread.start()

    def close(self) -> None:
        self.release_completion.set()
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)


def _queue_entry_texts(page, sid: str) -> list[str]:
    raw = page.evaluate(
        "(sid) => localStorage.getItem('hermes-queue-' + sid)", sid
    )
    if not raw:
        return []
    try:
        entries = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return []
    if not isinstance(entries, list):
        return []
    return [str((entry or {}).get("text") or "") for entry in entries if isinstance(entry, dict)]


def _fill_and_send(page, text: str, timeout: float = 15.0) -> None:
    """Fill the composer and send, tolerating the async draft-restore clear.

    Switching sessions restores the target's server-side composer draft
    asynchronously; when the target has no draft the restore CLEARS the
    textarea (so a previous session's draft does not leak forward). A fill
    issued before that restore lands is wiped and the send button stays
    disabled. Wait for the composer to be observably empty across two
    consecutive polls (restore settled), then fill and send.
    """
    deadline = time.monotonic() + timeout
    empty_streak = 0
    while time.monotonic() < deadline:
        value = page.evaluate("() => document.getElementById('msg').value")
        if value == "":
            empty_streak += 1
            if empty_streak >= 2:
                page.locator("#msg").fill(text)
                page.locator("#btnSend").click()
                return
        else:
            empty_streak = 0
        time.sleep(0.2)
    raise AssertionError("composer never settled to empty before fill/send")


def main() -> int:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("SETUP FAIL: playwright is not installed", file=sys.stderr)
        return 2

    repo_root = Path(__file__).resolve().parent.parent
    state_tmp = tempfile.TemporaryDirectory(prefix="hermes-steer-leftover-gate-")
    state_dir = Path(state_tmp.name)
    artifact_dir = Path(
        os.environ.get("STEER_LEFTOVER_ARTIFACT_DIR")
        or tempfile.mkdtemp(prefix="hermes-steer-leftover-artifacts-")
    )
    artifact_dir.mkdir(parents=True, exist_ok=True)

    gateway = SteerLeftoverGateway()
    gateway.start()

    agent_dir = state_dir / "no-agent"
    agent_dir.mkdir(parents=True)
    workspace_dir = state_dir / "workspace"
    workspace_dir.mkdir()
    (agent_dir / "run_agent.py").write_text(
        '"""Empty agent stub for the steer-leftover browser gate."""\n',
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
    playwright = None
    browser = None
    page = None
    errors = []
    exit_code = 1
    try:
        proc, log, _log_path, base_url = _start_webui_server(repo_root, env, artifact_dir)
        playwright = sync_playwright().start()
        browser = playwright.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage"],
        )
        context = browser.new_context(base_url=base_url)
        page = context.new_page()
        errors = _capture_page_errors(page)
        page.goto("/", wait_until="domcontentloaded")
        page.wait_for_selector("#msg", state="visible", timeout=15000)

        # Turn 1 for session A: start the gateway run, then hold it open.
        _fill_and_send(page, PROMPT_A1)
        if not gateway.first_delta_sent.wait(timeout=GATEWAY_ACTIVITY_TIMEOUT):
            raise AssertionError(
                "fake Gateway never streamed the first delta; "
                f"request bodies: {gateway.request_bodies!r}"
            )
        sid_a = page.evaluate(
            "() => {"
            "  const row = document.querySelector('.session-item[data-sid]');"
            "  return row ? row.dataset.sid : null;"
            "}"
        )
        if not sid_a:
            raise AssertionError("no session row found for the owning session A")

        # Switch to a brand-new session B while A's run is still in flight.
        page.locator("#btnNewChat").click()
        page.wait_for_function(
            "(sidA) => {"
            "  const rows = document.querySelectorAll('.session-item[data-sid]');"
            "  const sids = new Set(Array.from(rows).map(r => r.dataset.sid));"
            "  return sids.size >= 2 && sids.has(sidA);"
            "}",
            arg=sid_a,
            timeout=10000,
        )
        sid_b = page.evaluate(
            "(sidA) => {"
            "  const rows = document.querySelectorAll('.session-item[data-sid]');"
            "  for (const row of rows) {"
            "    if (row.dataset.sid !== sidA) return row.dataset.sid;"
            "  }"
            "  return null;"
            "}",
            arg=sid_a,
        )
        if not sid_b or sid_b == sid_a:
            raise AssertionError(f"session switch did not create a second session ({sid_a!r})")

        # Complete A's run with unconsumed guidance while B is the active view.
        gateway.release_completion.set()
        if not gateway.completion_sent.wait(timeout=10):
            raise AssertionError("fake Gateway never sent run.completed")

        deadline = time.monotonic() + QUEUE_WRITE_TIMEOUT
        queued_for_a: list[str] = []
        while time.monotonic() < deadline:
            queued_for_a = _queue_entry_texts(page, sid_a)
            if any(LEFTOVER_TEXT in text for text in queued_for_a):
                break
            time.sleep(0.25)
        if not any(LEFTOVER_TEXT in text for text in queued_for_a):
            raise AssertionError(
                "leftover guidance was not queued for the owning session A "
                f"while session B was viewed (queue: {queued_for_a!r})"
            )
        print("OK  leftover queued for owning session A across the session switch")

        queued_for_b = _queue_entry_texts(page, sid_b)
        if any(LEFTOVER_TEXT in text for text in queued_for_b):
            raise AssertionError(
                f"leftover guidance leaked into session B's queue ({queued_for_b!r})"
            )
        print("OK  session B queue untouched")

        # Return to A and complete one more turn: the real queue drain must
        # auto-send the leftover as A's next gateway run.
        page.locator(f".session-item[data-sid={json.dumps(sid_a)}]").click()
        page.wait_for_function(
            "(promptText) => {"
            "  const pane = document.querySelector('#messages');"
            "  return pane && (pane.innerText || '').includes(promptText);"
            "}",
            arg=PROMPT_A1,
            timeout=15000,
        )
        _fill_and_send(page, FOLLOWUP_A2)

        delivered = None
        deadline = time.monotonic() + DRAIN_DELIVERY_TIMEOUT
        while time.monotonic() < deadline:
            with gateway._lock:
                bodies = list(gateway.request_bodies)
            for body in bodies:
                input_value = body.get("input")
                input_text = ""
                if isinstance(input_value, str):
                    input_text = input_value
                elif isinstance(input_value, list):
                    input_text = json.dumps(input_value)
                if LEFTOVER_TEXT in input_text and body.get("session_id") == sid_a:
                    delivered = body
                    break
            if delivered:
                break
            time.sleep(0.25)
        if delivered is None:
            raise AssertionError(
                "the queue drain never delivered the leftover guidance to the "
                f"owning session's next gateway run (bodies: {gateway.request_bodies!r})"
            )
        print("OK  drain delivered the leftover as session A's next gateway run")

        if errors:
            raise AssertionError(f"page errors during the gate: {errors!r}")

        exit_code = 0
    except Exception as exc:
        import traceback
        traceback.print_exc()
        try:
            if page is not None:
                page.screenshot(path=str(artifact_dir / "failure.png"))
                state = page.evaluate(
                    "() => ({"
                    "  btnDisabled: document.getElementById('btnSend') ? document.getElementById('btnSend').disabled : null,"
                    "  busy: (typeof S !== 'undefined' && S) ? !!S.busy : null,"
                    "  activeSid: (typeof S !== 'undefined' && S && S.session) ? S.session.session_id : null,"
                    "  msgLen: document.getElementById('msg') ? document.getElementById('msg').value.length : null,"
                    "})"
                )
                print(f"FAILURE STATE: {state!r}", file=sys.stderr)
        except Exception:
            pass
        print(f"GATE FAILED: {exc}", file=sys.stderr)
    finally:
        if page is not None:
            try:
                page.close()
            except Exception:
                pass
        if browser is not None:
            try:
                browser.close()
            except Exception:
                pass
        if playwright is not None:
            playwright.stop()
        gateway.close()
        _terminate_process(proc)
        if log is not None:
            log.close()
        state_tmp.cleanup()
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
