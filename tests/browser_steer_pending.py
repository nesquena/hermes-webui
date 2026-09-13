#!/usr/bin/env python3
"""Public browser gate for the pending-steer indicator lifecycle.

Boots the real WebUI server in gateway mode with isolated state, opens
headless Chromium, starts a chat turn, intercepts /api/chat/steer so it
always returns accepted, and then verifies the full pending-steer count
cycle through the real frontend code:

  1. Steer submit -> count = 1, then 2 (visible composer status)
  2. Tool boundary -> count clears to 0
  3. Turn completion (done) -> no stale count

Requires: playwright + chromium.
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


PROMPT = "Exercise the steer pending visibility browser gate."
REASONING_TEXT = "Checking steer pending indicator."
FINAL_TEXT = "Steer pending gate final answer."
FINAL_PREFIX = "Steer pending gate "
FINAL_SUFFIX = "final answer."
TOOL_NAME = "read_file"
TOOL_ID_1 = "steer-gate-tool-1"
STEER_TEXT_1 = "browser steer one"
STEER_TEXT_2 = "browser steer two"
ACTIVITY_TIMEOUT = 60.0


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _wait_for_health(base_url: str, timeout: float = 30.0, proc=None) -> bool:
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


def _terminate_process(proc):
    if proc is None or proc.poll() is not None:
        return
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()


def _start_webui_server(repo_root: Path, env: dict, artifact_dir: Path):
    port = _free_port()
    base_url = f"http://127.0.0.1:{port}"
    log_path = artifact_dir / "server.log"
    log = log_path.open("w", encoding="utf-8")
    run_env = dict(env)
    run_env["HERMES_WEBUI_PORT"] = str(port)
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
    tail = ""
    if log_path.exists():
        tail = log_path.read_text(encoding="utf-8", errors="replace")[-2000:]
    raise RuntimeError(f"WebUI server did not become healthy on port {port}; log tail:\n{tail}")


class SteerPendingGateway:
    """Gateway that emits tool.started, then waits for tool_completed_release
    before emitting tool.completed. This creates a window for steer submits."""

    def __init__(self) -> None:
        self.activity_ready = threading.Event()
        self.tool_completed_release = threading.Event()
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
                    self._json({"features": {"approval_events": True, "run_approval_response": True}})
                    return
                if request_path != "/v1/runs/steer-gate-run-1/events":
                    self._json({"error": "not found"}, status=404)
                    return
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-cache")
                self.send_header("Connection", "close")
                self.end_headers()
                try:
                    self._event("reasoning.available", {"event": "reasoning.available", "text": REASONING_TEXT})
                    self._event("tool.started", {
                        "event": "tool.started",
                        "tool": TOOL_NAME,
                        "tool_call_id": TOOL_ID_1,
                        "status": "running",
                        "args": {"path": "README.md"},
                    })
                    owner.activity_ready.set()
                    if not owner.tool_completed_release.wait(timeout=60):
                        return
                    self._event("tool.completed", {
                        "event": "tool.completed",
                        "tool": TOOL_NAME,
                        "tool_call_id": TOOL_ID_1,
                        "status": "completed",
                        "preview": "steer gate tool done",
                    })
                    if not owner.release_settle.wait(timeout=30):
                        return
                    self._event("message.delta", {"event": "message.delta", "delta": FINAL_PREFIX})
                    owner.final_prefix_ready.set()
                    if not owner.release_terminal.wait(timeout=30):
                        return
                    self._event("message.delta", {"event": "message.delta", "delta": FINAL_SUFFIX})
                    self._event("run.completed", {"event": "run.completed", "usage": {"input_tokens": 12, "output_tokens": 5}})
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
                self._json({"run_id": "steer-gate-run-1"})

        return Handler

    def start(self) -> None:
        self._thread.start()

    def close(self) -> None:
        self.tool_completed_release.set()
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


def main() -> int:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("SETUP FAIL: playwright is not installed", file=sys.stderr)
        return 2

    repo_root = Path(__file__).resolve().parent.parent
    state_tmp = tempfile.TemporaryDirectory(prefix="hermes-steer-gate-")
    state_dir = Path(state_tmp.name)
    artifact_dir = Path(tempfile.mkdtemp(prefix="hermes-steer-gate-artifacts-"))
    artifact_dir.mkdir(parents=True, exist_ok=True)

    gateway = SteerPendingGateway()
    gateway.start()

    agent_dir = state_dir / "no-agent"
    agent_dir.mkdir(parents=True)
    workspace_dir = state_dir / "workspace"
    workspace_dir.mkdir()
    (agent_dir / "run_agent.py").write_text(
        '"""Empty agent stub for the Gateway-backed steer browser gate."""\n',
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
    steer_calls = []

    try:
        proc, log, log_path, base_url = _start_webui_server(repo_root, env, artifact_dir)
        playwright = sync_playwright().start()
        browser = playwright.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage"],
        )
        context = browser.new_context(base_url=base_url)
        page = context.new_page()
        errors = _capture_page_errors(page)

        # Intercept /api/chat/steer so it always returns accepted.
        # This avoids needing a real local agent in gateway mode.
        def _route_steer(route):
            raw = route.request.post_data or ""
            try:
                body = json.loads(raw)
            except (json.JSONDecodeError, TypeError):
                body = {}
            steer_calls.append(body)
            route.fulfill(
                status=200,
                content_type="application/json",
                body=json.dumps({
                    "accepted": True,
                    "fallback": None,
                    "stream_id": page.evaluate("S.activeStreamId || (S.session && S.session.active_stream_id) || null"),
                }),
            )
        page.route("**/api/chat/steer", _route_steer)

        page.goto("/", wait_until="domcontentloaded")
        page.wait_for_selector("#msg", state="visible", timeout=15000)
        page.locator("#msg").fill(PROMPT)
        page.locator("#btnSend").click()

        if not gateway.activity_ready.wait(timeout=ACTIVITY_TIMEOUT):
            raise AssertionError(
                "mock Gateway did not reach the tool.started checkpoint; "
                f"events: {gateway.emitted_events!r}"
            )

        # Wait for the live assistant turn to appear in the browser.
        page.wait_for_function(
            f"""() => {{
              const turn = document.querySelector('#liveAssistantTurn');
              return Boolean(turn) && (turn.innerText || '').includes({json.dumps(REASONING_TEXT)});
            }}""",
            timeout=10000,
        )
        print("OK  live turn is active after tool.started")

        # Get the current session ID.
        session_id = page.evaluate("S.session && S.session.session_id")
        assert session_id, "session_id missing"

        # Submit two steers via the real _trySteer code path.
        page.evaluate(f"() => _trySteer({json.dumps(STEER_TEXT_1)}, true)")
        count_1 = page.evaluate(f"getSteerPendingCount({json.dumps(session_id)})")
        assert count_1 == 1, f"after first steer, count should be 1 but got {count_1}"
        status_1 = page.evaluate("document.querySelector('.composer-status') ? document.querySelector('.composer-status').innerText : ''")
        print(f"OK  first steer: count={count_1}, status={status_1!r}")

        page.evaluate(f"() => _trySteer({json.dumps(STEER_TEXT_2)}, true)")
        count_2 = page.evaluate(f"getSteerPendingCount({json.dumps(session_id)})")
        assert count_2 == 2, f"after second steer, count should be 2 but got {count_2}"
        print(f"OK  second steer: count={count_2}")

        assert len(steer_calls) == 2, f"expected 2 steer API calls, got {len(steer_calls)}"
        assert steer_calls[0].get("session_id") == session_id
        assert steer_calls[1].get("session_id") == session_id
        print("OK  both steer POSTs reached /api/chat/steer with correct session_id")

        # Release tool.completed - the tool-batch boundary that should
        # consume the armed steer and clear the pending count.
        gateway.tool_completed_release.set()
        page.wait_for_function(
            "sid => getSteerPendingCount(sid) === 0",
            arg=session_id,
            timeout=10000,
        )
        count_3 = page.evaluate(f"getSteerPendingCount({json.dumps(session_id)})")
        assert count_3 == 0, f"after tool boundary, count should be 0 but got {count_3}"
        print("OK  tool boundary: pending count cleared to 0")

        # Let the turn settle.
        gateway.release_settle.set()
        if not gateway.final_prefix_ready.wait(timeout=10):
            raise AssertionError("mock Gateway did not emit the final-answer prefix")
        gateway.release_terminal.set()
        page.wait_for_function(
            "text => typeof S !== 'undefined' && S.busy === false && !S.activeStreamId && "
            "((document.querySelector('#msgInner') || {}).innerText || '').includes(text)",
            arg=FINAL_TEXT,
            timeout=15000,
        )
        count_final = page.evaluate(f"getSteerPendingCount({json.dumps(session_id)})")
        assert count_final == 0, f"after turn completion, count should be 0 but got {count_final}"
        print("OK  turn completion: no stale pending count")

        if errors:
            raise AssertionError(f"unexpected browser errors: {errors!r}")

        context.close()
        browser.close()
        browser = None
        print("\nSTEER PENDING BROWSER GATE PASSED")
        exit_code = 0
        return 0

    except Exception as error:
        print(f"\nSTEER PENDING BROWSER GATE FAILED: {error}", file=sys.stderr)
        try:
            if page is not None:
                page.screenshot(path=str(artifact_dir / "failure.png"), full_page=True)
                (artifact_dir / "snapshot.json").write_text(
                    json.dumps({
                        "browser_errors": errors,
                        "steer_calls": steer_calls,
                        "gateway_events": gateway.emitted_events,
                    }, indent=2),
                    encoding="utf-8",
                )
        except Exception as artifact_error:
            print(f"Could not capture browser artifacts: {artifact_error}", file=sys.stderr)
        print(f"Artifacts: {artifact_dir}", file=sys.stderr)
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
        shutil.rmtree(artifact_dir, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
