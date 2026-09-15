#!/usr/bin/env python3
"""Public browser gate for gateway terminal steer-leftover delivery (#7440).

Boots the real WebUI server with isolated state, drives the real chat
composer in Chromium, and supplies deterministic runtime events through a
fake Hermes Gateway Runs API — the same harness pattern as
browser_conversation_lifecycle.py.

Proves the greptile P1/P2 chain AND the #7440 re-gate's reproduced blockers
as observable browser behavior, through the application's real stream and
session-switch lifecycle (no source slicing, no stubbed collaborators):

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
  4. Re-gate blocker 1: switching to an EXISTING session (a completed turn
     of its own) closes the owning stream's live consumer — no live queue
     write happens on that shape — and the terminal leftover must STILL be
     durably recoverable: it is persisted server-side on the owning session
     (observable via /api/session's pending_steer_leftover_* fields),
     surviving the switch.
  5. Closed-tab restoration: a brand-new browser context (no client queue,
     no sessionStorage — also the cross-device/cleared-storage shape)
     re-offers the unconsumed leftover as a restore-for-review composer
     prefill from the durable slot, and sending that prefill retires the
     slot transactionally (matched by run id) — exactly-once delivery.
  6. Re-gate blocker 2: queue application is idempotent by STABLE run id —
     the same id replayed queues once, while two intentional identical
     steers with distinct ids both queue (never deduped by text or a time
     window).
  7. Explicit dismissal: clearing the restored prefill (a real user input
     event) retires the durable slot so it stops being re-offered.
  8. No foreign-session copies after all scenarios.
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
LEFTOVER_RESTORE = "reopen via the durable slot"
LEFTOVER_DISMISS = "dismiss me after review"
GATEWAY_ACTIVITY_TIMEOUT = 60.0
QUEUE_WRITE_TIMEOUT = 20.0
DRAIN_DELIVERY_TIMEOUT = 45.0
SLOT_POLL_TIMEOUT = 30.0


class SteerLeftoverGateway:
    """ localhost-only Gateway Runs server with a test-controlled completion gate.

    Run 1 streams one delta, then blocks until the test releases completion —
    at which point it sends ``run.completed`` carrying ``pending_steer`` (the
    agent's terminal unconsumed-guidance field). Every later run completes
    immediately, so the queue drain's auto-send is observable as a captured
    request body.
    """

    def __init__(self) -> None:
        self.request_bodies: list[dict] = []
        self._lock = threading.Lock()
        self._run_counter = 0
        self._gates: dict[int, dict] = {}
        # The original scenario pre-arms gate 1 (the initial blocked run).
        gate1 = self._new_gate(LEFTOVER_TEXT, 1)
        self._gates[1] = gate1
        # Backward-compatible aliases used by the original flow.
        self.release_completion = gate1["release"]
        self.first_delta_sent = gate1["first_delta"]
        self.completion_sent = gate1["sent"]
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), self._handler())
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    def _new_gate(self, pending_steer: str, run_no: int) -> dict:
        return {
            "run_no": run_no,
            "release": threading.Event(),
            "first_delta": threading.Event(),
            "sent": threading.Event(),
            "pending_steer": pending_steer,
        }

    def arm_gated_completion(self, pending_steer: str) -> dict:
        """Reserve the NEXT run number for a blocked completion carrying
        ``pending_steer``. The caller must send the turn that creates that
        run immediately after arming (the scenarios do)."""
        with self._lock:
            run_no = self._run_counter + 1
            gate = self._new_gate(pending_steer, run_no)
            self._gates[run_no] = gate
            return gate

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
                run_no = 0
                if run_id.startswith("leftover-run-"):
                    try:
                        run_no = int(run_id[len("leftover-run-"):])
                    except ValueError:
                        run_no = 0
                gate = owner._gates.get(run_no)
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-cache")
                self.send_header("Connection", "close")
                self.end_headers()
                try:
                    if gate is not None:
                        self._event("message.delta", {
                            "event": "message.delta",
                            "delta": "working through the turn",
                        })
                        gate["first_delta"].set()
                        if not gate["release"].wait(timeout=60):
                            return
                        completed_payload = {
                            "event": "run.completed",
                            "output": "first turn done",
                            "pending_steer": gate["pending_steer"],
                            "usage": {"input_tokens": 7, "output_tokens": 3},
                        }
                        self._event("run.completed", completed_payload)
                        gate["sent"].set()
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


def _webui_session_slot(base_url: str, sid: str) -> tuple[str, str]:
    """Read the durable leftover slot straight from the WebUI session API."""
    import urllib.request

    url = f"{base_url}/api/session?session_id={sid}&messages=0"
    with urllib.request.urlopen(url, timeout=10) as resp:
        data = json.loads(resp.read())
    session = (data or {}).get("session") or {}
    return (
        str(session.get("pending_steer_leftover_run_id") or ""),
        str(session.get("pending_steer_leftover_text") or ""),
    )


def _wait_for_slot(base_url: str, sid: str, want_run_id: str, timeout: float = SLOT_POLL_TIMEOUT) -> None:
    deadline = time.monotonic() + timeout
    last = ("", "")
    while time.monotonic() < deadline:
        last = _webui_session_slot(base_url, sid)
        if last[0] == want_run_id:
            return
        time.sleep(0.25)
    raise AssertionError(
        f"durable leftover slot never held run {want_run_id!r} for {sid} (last: {last!r})"
    )


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

        # ── Scenario 4 (#7440 re-gate blocker 1): existing-session switch +
        # closed-tab restoration through the SERVER-DURABLE slot. Switching to
        # an EXISTING session closes the owning stream's live consumer — the
        # exact reproduced loss (the queue write above only happens on the
        # new-chat shape that leaves the owner stream open) — so recovery must
        # come from the session sidecar, not from SSE delivery.
        # Create a real EXISTING session C with a completed turn (empty
        # new-chat drafts are pruned from the sidebar on navigation).
        page.locator("#btnNewChat").click()
        page.wait_for_function(
            "(sidA) => {"
            "  const rows = document.querySelectorAll('.session-item[data-sid]');"
            "  const sids = new Set(Array.from(rows).map(r => r.dataset.sid));"
            "  return sids.size >= 2 && sids.has(sidA) &&"
            "         typeof S !== 'undefined' && S.session && S.session.session_id !== sidA;"
            "}",
            arg=sid_a,
            timeout=10000,
        )
        sid_c = page.evaluate(
            "(sidA) => {"
            "  const rows = document.querySelectorAll('.session-item[data-sid]');"
            "  for (const row of rows) {"
            "    if (row.dataset.sid !== sidA) return row.dataset.sid;"
            "  }"
            "  return null;"
            "}",
            arg=sid_a,
        )
        if not sid_c:
            raise AssertionError("new-chat C did not appear for the existing-session switch")
        _fill_and_send(page, "Seed turn for the existing session C")
        deadline = time.monotonic() + DRAIN_DELIVERY_TIMEOUT
        c_seed_seen = False
        while time.monotonic() < deadline:
            with gateway._lock:
                bodies = list(gateway.request_bodies)
            if any(body.get("session_id") == sid_c for body in bodies):
                c_seed_seen = True
                break
            time.sleep(0.25)
        if not c_seed_seen:
            raise AssertionError(
                "session C's seed turn never reached the fake Gateway "
                f"(bodies: {gateway.request_bodies!r})"
            )
        print("OK  session C created as a real existing session")

        # Back to A, start the gated run that will complete with a leftover.
        page.locator(f".session-item[data-sid={json.dumps(sid_a)}]").click()
        page.wait_for_function(
            "(sidA) => typeof S !== 'undefined' && S.session && S.session.session_id === sidA",
            arg=sid_a,
            timeout=10000,
        )
        gate2 = gateway.arm_gated_completion(LEFTOVER_RESTORE)  # reserves the next run
        _fill_and_send(page, "Second durable-slot check")
        if not gate2["first_delta"].wait(timeout=GATEWAY_ACTIVITY_TIMEOUT):
            raise AssertionError("fake Gateway never streamed the gated second run")
        # Switch to the EXISTING session C while A's run is still in flight —
        # this closes the owning stream's live consumer.
        page.locator(f".session-item[data-sid={json.dumps(sid_c)}]").click()
        page.wait_for_function(
            "(sidC) => typeof S !== 'undefined' && S.session && S.session.session_id === sidC",
            arg=sid_c,
            timeout=10000,
        )
        gate2["release"].set()
        if not gate2["sent"].wait(timeout=10):
            raise AssertionError("fake Gateway never completed the gated second run")
        _wait_for_slot(base_url, sid_a, f"leftover-run-{gate2['run_no']}")
        print("OK  terminal leftover persisted server-side across an existing-session switch")
        # The live queue write does NOT happen on this shape (the owning
        # stream's consumer is closed) — the durable slot is the recovery.
        time.sleep(1.0)
        if any(LEFTOVER_RESTORE in text for text in _queue_entry_texts(page, sid_a)):
            raise AssertionError(
                "unexpected live queue write after an existing-session switch "
                "(the owning stream consumer should be closed on this shape)"
            )
        if any(LEFTOVER_RESTORE in text for text in _queue_entry_texts(page, sid_c)):
            raise AssertionError("leftover guidance leaked into session C's queue")
        print("OK  existing-session switch left no live queue copy (slot is the recovery)")

        # Closed-tab restoration: a brand-new browser context has NO client
        # queue and NO sessionStorage — the durable slot is the only recovery
        # source (also the cross-device / cleared-storage shape).
        context2 = browser.new_context(base_url=base_url)
        page2 = context2.new_page()
        try:
            page2.goto("/", wait_until="domcontentloaded")
            page2.wait_for_selector("#msg", state="visible", timeout=15000)
            page2.locator(f".session-item[data-sid={json.dumps(sid_a)}]").click()
            page2.wait_for_function(
                "(promptText) => {"
                "  const pane = document.querySelector('#messages');"
                "  return pane && (pane.innerText || '').includes(promptText);"
                "}",
                arg=PROMPT_A1,
                timeout=15000,
            )
            page2.wait_for_function(
                "(leftover) => document.getElementById('msg').value.trim() === leftover",
                arg=LEFTOVER_RESTORE,
                timeout=15000,
            )
            queue_raw = page2.evaluate(
                "(sid) => localStorage.getItem('hermes-queue-' + sid)", sid_a
            )
            if queue_raw not in (None, "", "[]"):
                raise AssertionError(
                    "recovery must restore-for-review (prefill), not re-queue: "
                    f"storage held {queue_raw!r}"
                )
            print("OK  closed-tab restore prefilled the composer from the durable slot")

            # Sending the restored prefill must retire the slot transactionally.
            page2.locator("#btnSend").click()
            delivered2 = None
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
                    if LEFTOVER_RESTORE in input_text and body.get("session_id") == sid_a:
                        delivered2 = body
                        break
                if delivered2:
                    break
                time.sleep(0.25)
            if delivered2 is None:
                raise AssertionError(
                    "restored leftover was never delivered as session A's next run "
                    f"(bodies: {gateway.request_bodies!r})"
                )
            deadline = time.monotonic() + SLOT_POLL_TIMEOUT
            while time.monotonic() < deadline:
                run_id, _text = _webui_session_slot(base_url, sid_a)
                if run_id == "":
                    break
                time.sleep(0.25)
            else:
                raise AssertionError("sending the restored prefill never retired the durable slot")
            print("OK  sending the restored prefill retired the slot transactionally")
        finally:
            context2.close()

        # ── Scenario 5 (#7440 re-gate blocker 2): application-level
        # idempotency by STABLE IDENTITY — the same event id replayed must not
        # duplicate, while two intentional identical steers (distinct ids)
        # must BOTH queue. Never dedupe by text or a time window.
        id_counts = None
        context3 = browser.new_context(base_url=base_url)
        page3 = context3.new_page()
        try:
            page3.goto("/", wait_until="domcontentloaded")
            page3.wait_for_selector("#msg", state="visible", timeout=15000)
            page3.locator(f".session-item[data-sid={json.dumps(sid_a)}]").click()
            page3.wait_for_function(
                "() => typeof queueSessionMessage === 'function'",
                timeout=15000,
            )
            page3.wait_for_function(
                "(sidA) => typeof S !== 'undefined' && S.session && S.session.session_id === sidA",
                arg=sid_a,
                timeout=15000,
            )
            id_counts = page3.evaluate(
                """(sid) => {
                    const key = 'hermes-queue-' + sid;
                    localStorage.removeItem(key);
                    queueSessionMessage(sid, {text: 'identical guidance'}, 'run-id-x');
                    queueSessionMessage(sid, {text: 'identical guidance'}, 'run-id-x');
                    queueSessionMessage(sid, {text: 'identical guidance'}, 'run-id-y');
                    const entries = JSON.parse(localStorage.getItem(key) || '[]');
                    return entries.map(e => e._leftover_id || null);
                }""",
                arg=sid_a,
            )
        finally:
            context3.close()
        if id_counts != ["run-id-x", "run-id-y"]:
            raise AssertionError(
                "queue idempotency by stable identity failed: "
                f"expected one entry per distinct run id, got {id_counts!r}"
            )
        print("OK  queue application is idempotent by run id (replay once, distinct ids twice)")

        # ── Scenario 6 (#7440 re-gate): explicit dismissal — clearing a
        # restored prefill retires the durable slot so it stops being
        # re-offered on every load.
        gate3 = gateway.arm_gated_completion(LEFTOVER_DISMISS)
        context4 = browser.new_context(base_url=base_url)
        page4 = context4.new_page()
        try:
            page4.goto("/", wait_until="domcontentloaded")
            page4.wait_for_selector("#msg", state="visible", timeout=15000)
            page4.locator(f".session-item[data-sid={json.dumps(sid_a)}]").click()
            page4.wait_for_function(
                "(promptText) => {"
                "  const pane = document.querySelector('#messages');"
                "  return pane && (pane.innerText || '').includes(promptText);"
                "}",
                arg=PROMPT_A1,
                timeout=15000,
            )
            _fill_and_send(page4, "Third durable-slot check")
            if not gate3["first_delta"].wait(timeout=GATEWAY_ACTIVITY_TIMEOUT):
                raise AssertionError("fake Gateway never streamed the gated third run")
            gate3["release"].set()
            if not gate3["sent"].wait(timeout=10):
                raise AssertionError("fake Gateway never completed the gated third run")
            _wait_for_slot(base_url, sid_a, f"leftover-run-{gate3['run_no']}")
            # Reload: recovery prefills the unconsumed leftover for review.
            page4.reload(wait_until="domcontentloaded")
            page4.wait_for_selector("#msg", state="visible", timeout=15000)
            page4.wait_for_function(
                "(leftover) => document.getElementById('msg').value.trim() === leftover",
                arg=LEFTOVER_DISMISS,
                timeout=15000,
            )
            # The user reviews and discards: select-all + delete (a real
            # user input event, unlike programmatic .value writes).
            page4.locator("#msg").fill("")
            deadline = time.monotonic() + SLOT_POLL_TIMEOUT
            dismissed = False
            while time.monotonic() < deadline:
                run_id, _text = _webui_session_slot(base_url, sid_a)
                if run_id == "":
                    dismissed = True
                    break
                time.sleep(0.25)
            if not dismissed:
                raise AssertionError("clearing the restored prefill never dismissed the slot")
            print("OK  explicit dismissal retired the durable slot")
        finally:
            context4.close()

        # Cross-session zero-copy re-check after all scenarios.
        queued_for_b_final = _queue_entry_texts(page, sid_b)
        for leftover in (LEFTOVER_TEXT, LEFTOVER_RESTORE, LEFTOVER_DISMISS):
            if any(leftover in text for text in queued_for_b_final):
                raise AssertionError(
                    f"leftover guidance leaked into session B's queue ({queued_for_b_final!r})"
                )
        print("OK  no foreign-session copies after all scenarios")

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
