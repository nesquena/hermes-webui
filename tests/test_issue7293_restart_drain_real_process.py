"""#7293 restart/stop drain, exercised against a real ``server.py`` process.

Each test boots ``tests/issue7293_drain_server.py`` (the real ``server.main()``
with a deterministic in-process fake agent; see that file for the seams) on a
free port with an isolated state dir under the system temp dir, then drives it
over HTTP and with real signals:

* a WebUI update refuses while a run is active (count + ``force_available``),
  and the forced update settles the run durably before the re-exec point;
* the first SIGTERM closes run admission (503 ``server_draining``), lets an
  active run finish inside the budget, or settles it as interrupted at the
  deadline, then exits;
* a second SIGTERM escalates to an immediate settled stop;
* with no active run, SIGTERM exits immediately.

No Hermes Agent checkout, provider or network is needed, so CI runs these.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request

import pytest

from api.run_drain import RESTART_SETTLE_REASON, SHUTDOWN_SETTLE_REASON

ROOT = Path(__file__).resolve().parents[1]
LAUNCHER = ROOT / "tests" / "issue7293_drain_server.py"
PRODUCTION_PORT = 8787

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(sys.platform == "win32", reason="POSIX stop signals"),
]


def _free_port() -> int:
    for _ in range(20):
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        if port != PRODUCTION_PORT:
            return port
    raise AssertionError("could not find a free port")


class DrainServer:
    """One isolated real server process. Always ``close()`` it (see fixture)."""

    def __init__(self, tmp_path: Path, drain_seconds: str):
        self.root = tmp_path
        self.record_dir = tmp_path / "records"
        self.state_dir = tmp_path / "state"
        self.workspace = tmp_path / "workspace"
        self.log_path = tmp_path / "server.log"
        self.drain_seconds = drain_seconds
        self.proc: subprocess.Popen | None = None
        self.port = 0

    # ── lifecycle ────────────────────────────────────────────────────────
    def _env(self) -> dict:
        # From scratch: never inherit the suite's state dir, credentials,
        # profiles or the real Hermes home.
        env = {"PATH": os.pathsep.join([str(Path(sys.executable).parent), "/usr/bin", "/bin"])}
        for key in ("HOME", "HERMES_HOME", "TMPDIR"):
            directory = self.root / key.lower()
            directory.mkdir(exist_ok=True)
            env[key] = str(directory)
        self.state_dir.mkdir(exist_ok=True)
        self.workspace.mkdir(exist_ok=True)
        config_path = Path(env["HERMES_HOME"]) / "config.yaml"
        config_path.write_text("model:\n  default: fake-model\n  provider: openrouter\n", encoding="utf-8")
        env.update(
            HERMES_BASE_HOME=env["HERMES_HOME"],
            HERMES_CONFIG_PATH=str(config_path),
            HERMES_WEBUI_STATE_DIR=str(self.state_dir),
            HERMES_WEBUI_DEFAULT_WORKSPACE=str(self.workspace),
            HERMES_WEBUI_DEFAULT_MODEL="fake-model",
            HERMES_WEBUI_HOST="127.0.0.1",
            HERMES_WEBUI_PORT=str(self.port),
            HERMES_WEBUI_PASSWORD="",
            HERMES_WEBUI_SKIP_ONBOARDING="1",
            HERMES_WEBUI_TEST_NETWORK_BLOCK="1",
            HERMES_DISABLE_LAZY_INSTALLS="1",
            # Deterministic everywhere: no Agent checkout, the fake agent runs.
            HERMES_WEBUI_AGENT_DIR=str(self.root / "no-agent"),
            OPENROUTER_API_KEY="sk-test-not-a-real-key",
            HERMES_WEBUI_SHUTDOWN_DRAIN_SECONDS=self.drain_seconds,
            ISSUE7293_TEST_RESTART_DELAY="1.0",
            PYTHONUNBUFFERED="1",
        )
        return env

    def start(self) -> "DrainServer":
        last = ""
        for _attempt in range(2):  # one retry covers a lost free-port race
            self.port = _free_port()
            with self.log_path.open("w", encoding="utf-8") as log:
                self.proc = subprocess.Popen(
                    [sys.executable, str(LAUNCHER), str(ROOT), str(self.record_dir)],
                    cwd=self.root,
                    env=self._env(),
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    start_new_session=True,
                )
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                if self.proc.poll() is not None:
                    break
                try:
                    status, body = self.request("GET", "/health", timeout=0.5)
                    if status == 200 and body.get("status") == "ok":
                        return self
                except OSError:
                    pass
                time.sleep(0.05)
            last = self.log_tail()
            self.close()
        raise AssertionError(f"drain test server did not start:\n{last}")

    def close(self) -> None:
        proc = self.proc
        if proc is None:
            return
        if proc.poll() is None:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                proc.kill()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)

    def signal(self, signum: int = signal.SIGTERM) -> float:
        assert self.proc is not None and self.proc.poll() is None, self.log_tail()
        self.proc.send_signal(signum)
        return time.monotonic()

    def wait_exit(self, timeout: float) -> int:
        assert self.proc is not None
        try:
            return self.proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired as exc:
            raise AssertionError(
                f"server still running {timeout}s after the stop signal:\n{self.log_tail()}"
            ) from exc

    def log_tail(self, chars: int = 4000) -> str:
        try:
            return self.log_path.read_text(encoding="utf-8", errors="replace")[-chars:]
        except OSError:
            return "<no server log>"

    # ── HTTP ─────────────────────────────────────────────────────────────
    def request(self, method: str, path: str, body: dict | None = None, timeout: float = 10):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}", data=data, method=method,
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.status, json.loads(resp.read() or b"{}")
        except urllib.error.HTTPError as exc:
            raw = exc.read() or b"{}"
            try:
                return exc.code, json.loads(raw)
            except ValueError:
                return exc.code, {"raw": raw.decode("utf-8", "replace")}

    def new_session(self) -> str:
        status, body = self.request("POST", "/api/session/new", {"workspace": str(self.workspace)})
        assert status == 200, (status, body)
        sid = (body.get("session") or {}).get("session_id") or body.get("session_id")
        assert sid, body
        return sid

    def start_chat(self, sid: str, seconds: float):
        return self.request("POST", "/api/chat/start", {
            "session_id": sid,
            "message": f"drain test sleep:{seconds}",
            "workspace": str(self.workspace),
            "model": "fake-model",
        })

    def start_run(self, seconds: float) -> tuple[str, str]:
        sid = self.new_session()
        status, body = self.start_chat(sid, seconds)
        assert status == 200 and body.get("stream_id"), (status, body, self.log_tail())
        return sid, body["stream_id"]

    def health(self) -> dict:
        status, body = self.request("GET", "/health")
        assert status == 200, (status, body)
        return body

    def wait_active_runs(self, count: int, timeout: float = 60) -> dict:
        deadline = time.monotonic() + timeout
        body = {}
        while time.monotonic() < deadline:
            body = self.health()
            if int(body.get("active_runs") or 0) == count:
                return body
            time.sleep(0.05)
        raise AssertionError(f"expected {count} active runs, last /health: {body}\n{self.log_tail()}")

    def session_file(self, sid: str) -> dict:
        return json.loads((self.state_dir / "sessions" / f"{sid}.json").read_text(encoding="utf-8"))

    def wait_first_output(self, sid: str, timeout: float = 90) -> None:
        """Block until the fake agent of ``sid`` has streamed its first delta.

        Settling a run preserves exactly the output it had streamed so far. A
        run stopped while it is still launching (registered stream, agent not
        yet producing) legitimately has no partial to keep and is marked for
        journal recovery instead, so tests that assert partial preservation
        must first wait for real output. The bound is generous on purpose: a
        loaded CI host or sandbox can take well over 15s to construct a second
        agent while the first one streams, and a premature failure here is a
        fixture flake, not a drain defect.
        """
        path = self.record_dir / f"first-output-{sid}"
        deadline = time.monotonic() + timeout
        while not path.exists():
            assert time.monotonic() < deadline, f"{sid} never streamed output\n{self.log_tail()}"
            time.sleep(0.02)

    def wait_record(self, name: str, timeout: float = 20) -> dict:
        path = self.record_dir / name
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if path.exists():
                return json.loads(path.read_text(encoding="utf-8"))
            time.sleep(0.05)
        raise AssertionError(f"{name} was never written\n{self.log_tail()}")


@pytest.fixture
def drain_server(tmp_path):
    servers: list[DrainServer] = []

    def _make(drain_seconds: str) -> DrainServer:
        server = DrainServer(tmp_path / f"server{len(servers)}", drain_seconds)
        server.root.mkdir()
        servers.append(server)
        return server.start()

    try:
        yield _make
    finally:
        for server in servers:
            server.close()


def _settle_markers(session: dict, reason: str) -> list[dict]:
    return [
        m for m in session.get("messages") or []
        if m.get("role") == "assistant" and m.get("_error") and m.get("provider_details") == reason
    ]


def _assert_settled(session: dict, reason: str) -> None:
    dump = json.dumps(session.get("messages"), indent=1)[:4000]
    markers = _settle_markers(session, reason)
    assert len(markers) == 1, dump
    marker = markers[0]
    # Server-initiated: the marker names the server and never the user.
    assert marker.get("cancel_initiator") == "server", dump
    assert "cancelled by the user" not in str(marker.get("content", "")).lower(), dump
    # No turn is left looking live or pending after the settle.
    assert not session.get("active_stream_id"), session.get("active_stream_id")
    assert not session.get("pending_user_message"), session.get("pending_user_message")
    assert any(m.get("role") == "user" for m in session["messages"]), session["messages"]
    # The output streamed before the settle is preserved durably, ahead of the
    # marker, exactly as a user Stop preserves it (callers wait for output).
    messages = session["messages"]
    partials = [
        i for i, m in enumerate(messages)
        if m.get("role") == "assistant" and m.get("_partial") and str(m.get("content", "")).startswith("tick0")
    ]
    assert len(partials) == 1, dump
    assert partials[0] < messages.index(marker), dump


def _assert_admission_closed(server: DrainServer, phase: str) -> None:
    sid = server.new_session()
    status, body = server.start_chat(sid, 1)
    assert status == 503, (status, body)
    assert body.get("code") == "server_draining", body
    assert body.get("admission_phase") == phase, body
    assert body.get("retryable") is True, body


# ── (a) WebUI update/restart path ──────────────────────────────────────────


def test_update_refuses_while_run_active_and_force_settles_before_execv(drain_server):
    server = drain_server("3")
    sid_a, stream_a = server.start_run(600)
    server.wait_active_runs(1)

    for path in ("/api/updates/apply", "/api/updates/force"):
        status, refused = server.request("POST", path, {"target": "webui"})
        assert status == 200, (path, status, refused)
        assert refused["ok"] is False, refused
        assert refused["restart_blocked"] is True and refused["force_available"] is True, refused
        assert refused["active_runs"] == 1, refused
        assert stream_a in refused["blocking_run_ids"], refused

    # A refusal changes nothing: no restart, the run keeps going, and run
    # admission stays open (a second run can still start).
    sid_b, stream_b = server.start_run(600)
    server.wait_active_runs(2)
    assert not list(server.record_dir.glob("execv-*.json"))
    # Both runs have streamed output, so the settle has partials to preserve
    # (a run settled before its first delta legitimately has none and is
    # marked for journal recovery instead; see wait_first_output).
    server.wait_first_output(sid_a)
    server.wait_first_output(sid_b)

    status, forced = server.request(
        "POST", "/api/updates/apply", {"target": "webui", "force_active_runs": True}
    )
    assert status == 200 and forced["ok"] is True and forced["restart_scheduled"] is True, forced
    # From the forced update until the process image is replaced, nothing new
    # may start that the re-exec would drop.
    _assert_admission_closed(server, "restart_pending")

    record = server.wait_record("execv-1.json")
    assert record["admission"]["phase"] == "restart_pending", record["admission"]
    assert record["admission"]["restart_forced"] is True, record["admission"]
    # Settled BEFORE execv: the durable interrupted marker is already on disk
    # and no run is still registered as a live stream (worker threads may
    # still be unwinding in phase "cancelling"; execv ends them).
    assert not [r for r in record["active_runs"] if r.get("registered_stream")], record["active_runs"]
    assert {r["stream_id"] for r in record["active_runs"]} <= {stream_a, stream_b}, record["active_runs"]
    for sid in (sid_a, sid_b):
        _assert_settled(record["sessions"][sid], RESTART_SETTLE_REASON)

    # The stubbed execv returned ("not replaced"), so admission must reopen
    # rather than leave the server permanently closed.
    deadline = time.monotonic() + 10
    while True:
        status, body = server.start_chat(server.new_session(), 0.2)
        if status == 200:
            break
        assert time.monotonic() < deadline, (status, body, server.log_tail())
        time.sleep(0.2)
    assert server.proc.poll() is None


# ── (b) first SIGTERM: bounded drain ───────────────────────────────────────


def test_sigterm_closes_admission_and_settles_run_at_deadline(drain_server):
    budget = 3.0
    server = drain_server(str(budget))
    sid, _stream = server.start_run(600)
    server.wait_active_runs(1)
    server.wait_first_output(sid)

    sent = server.signal(signal.SIGTERM)
    _assert_admission_closed(server, "draining")
    # The run is waited on, not killed: still active and the server still
    # answers while the budget runs.
    time.sleep(1.0)
    assert int(server.health().get("active_runs") or 0) == 1

    code = server.wait_exit(timeout=budget + 20)
    elapsed = time.monotonic() - sent
    assert code == 0, server.log_tail()
    assert budget - 0.5 <= elapsed <= budget + 10, (elapsed, server.log_tail())
    _assert_settled(server.session_file(sid), SHUTDOWN_SETTLE_REASON)


def test_sigterm_lets_a_run_finish_inside_the_budget(drain_server):
    server = drain_server("20")
    sid, _stream = server.start_run(2)
    server.wait_active_runs(1)

    sent = server.signal(signal.SIGTERM)
    _assert_admission_closed(server, "draining")
    code = server.wait_exit(timeout=30)
    elapsed = time.monotonic() - sent
    assert code == 0, server.log_tail()
    # Exited when the run finished, long before the 20s budget.
    assert elapsed < 12, (elapsed, server.log_tail())
    session = server.session_file(sid)
    assert not session.get("active_stream_id")
    assert not [m for m in session["messages"] if m.get("_error")], session["messages"]
    assert any(
        m.get("role") == "assistant" and str(m.get("content", "")).endswith("DONE")
        for m in session["messages"]
    ), session["messages"]


# ── (c) second SIGTERM escalates ──────────────────────────────────────────


def test_second_sigterm_escalates_to_immediate_settled_stop(drain_server):
    server = drain_server("40")
    sid, _stream = server.start_run(600)
    server.wait_active_runs(1)
    server.wait_first_output(sid)

    first = server.signal(signal.SIGTERM)
    _assert_admission_closed(server, "draining")
    time.sleep(0.5)
    assert server.proc.poll() is None, "first SIGTERM must drain, not stop at once"

    second = server.signal(signal.SIGTERM)
    code = server.wait_exit(timeout=20)
    assert code == 0, server.log_tail()
    # Far below the 40s budget: escalation skipped the rest of the drain.
    assert time.monotonic() - second < 12, server.log_tail()
    assert time.monotonic() - first < 20, server.log_tail()
    _assert_settled(server.session_file(sid), SHUTDOWN_SETTLE_REASON)


# ── (d) nothing active ────────────────────────────────────────────────────


def test_sigterm_without_active_runs_exits_immediately(drain_server):
    server = drain_server("40")
    # A run that already finished must not hold the stop up.
    sid, _stream = server.start_run(0.3)
    deadline = time.monotonic() + 15
    while not any(
        str(m.get("content", "")).endswith("DONE") for m in server.session_file(sid).get("messages") or []
    ):
        assert time.monotonic() < deadline, server.log_tail()
        time.sleep(0.05)
    server.wait_active_runs(0)

    sent = server.signal(signal.SIGTERM)
    code = server.wait_exit(timeout=15)
    assert code == 0, server.log_tail()
    assert time.monotonic() - sent < 8, server.log_tail()
    session = server.session_file(sid)
    assert not [m for m in session["messages"] if m.get("_error")], session["messages"]
