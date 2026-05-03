"""
Live integration test for WAL crash recovery.

Tests the WAL subsystem end-to-end against the running hermes-webui service:
  1. WAL file is created and written during streaming
  2. WAL is deleted after clean stream completion
  3. WAL replay recovers assistant text from a simulated crash

Run:
  cd /home/hermes/hermes-webui
  /home/hermes/.hermes/hermes-agent/venv/bin/python -m pytest tests/test_wal_live_recovery.py -v
"""

import json
import sys
import time
import uuid
from pathlib import Path

import pytest

import api.models as _models
import api.config as _config
from api import wal as _wal
from api.config import LOCK

BASE_URL = "http://localhost:8787"
REAL_SESSION_DIR = Path("/home/hermes/.hermes/webui-mvp/sessions")


def wait_for(url, timeout=10):
    start = time.time()
    while time.time() - start < timeout:
        try:
            import requests
            r = requests.get(url, timeout=2)
            if r.status_code == 200:
                return True
        except Exception:
            pass
        time.sleep(0.3)
    return False


def api_post(path, json=None, timeout=30):
    import requests
    return requests.post(f"{BASE_URL}{path}", json=json, timeout=timeout)


def api_get(path, timeout=10):
    import requests
    return requests.get(f"{BASE_URL}{path}", timeout=timeout)


# ─── Fixtures ────────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def _isolate_models_session_dir(tmp_path, monkeypatch):
    """Redirect in-process models SESSION_DIR to a temp directory.

    This is needed for test 3 which directly calls get_session() with a
    hand-crafted session JSON.  The running webui service still uses the
    real session dir — tests 1 & 2 verify WAL behaviour by checking the
    real dir that the service writes to.
    """
    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    index_file = session_dir / "_index.json"

    monkeypatch.setattr(_models, "SESSION_DIR", session_dir)
    monkeypatch.setattr(_models, "SESSION_INDEX_FILE", index_file)

    _models.SESSIONS.clear()
    yield session_dir, index_file
    _models.SESSIONS.clear()


# ─── Tests ───────────────────────────────────────────────────────────────────

@pytest.mark.integration
class TestWALLiveRecovery:
    """End-to-end WAL crash-recovery tests."""

    def test_wal_file_created_during_streaming(self):
        """Verify WAL file is created and populated while agent is streaming.

        The running webui service writes WAL to REAL_SESSION_DIR.  We start a
        streaming session and check that a WAL file appears there.
        """
        # Create a new session via API
        r = api_post("/api/session/new")
        assert r.status_code == 200, f"new session failed: {r.text}"
        body = r.json()
        session_id = body.get("session_id") or (body.get("session") or {}).get("session_id")
        assert session_id, f"no session_id: {r.text}"
        assert all(c.isalnum() for c in session_id), f"unexpected chars: {session_id}"

        # Send a long message to generate many tokens (exceeds 100-token WAL flush)
        r = api_post("/api/chat/start", json={
            "session_id": session_id,
            "message": (
                "Write a detailed story about a robot who discovers an ancient library "
                "buried under the ocean. Include dialogue, describe the robot's thoughts "
                "and feelings, and explain how it shares this knowledge with humanity. "
                "Make it at least 400 words long."
            ),
            "activeProfile": "default",
        }, timeout=5)
        assert r.status_code == 200, f"chat/start failed: {r.text}"
        start_data = r.json()
        stream_id = start_data.get("stream_id")
        assert stream_id, f"no stream_id: {r.text}"

        # Consume SSE stream; WAL file should appear mid-stream
        wal_path = REAL_SESSION_DIR / f"{session_id}_wal.jsonl"
        wal_seen = False
        token_events = 0
        try:
            r = api_get(f"/api/chat/stream?stream_id={stream_id}", timeout=90)
            r.raise_for_status()
            for line in r.iter_lines(decode_unicode=True):
                if "stream_end" in line:
                    break
                if line.startswith("data:") and len(line) > 10:
                    token_events += 1
                    # Check for WAL file after collecting some tokens
                    if token_events >= 80 and not wal_seen and wal_path.exists():
                        # Verify it has content
                        try:
                            content = wal_path.read_text(encoding="utf-8").strip()
                            if content:
                                wal_seen = True
                                break
                        except Exception:
                            pass
        except Exception as e:
            print(f"Stream error (expected): {e}")

        # WAL file must exist (service creates it when streaming begins)
        assert wal_path.exists(), (
            f"WAL file not found at {wal_path}. "
            f"SERVICE SESSION_DIR={REAL_SESSION_DIR}; "
            f"dir contents: {list(REAL_SESSION_DIR.glob('*_wal.jsonl')[:5])}"
        )

        # WAL must contain events (at minimum a 'start' event)
        wal_text = wal_path.read_text(encoding="utf-8").strip()
        assert wal_text, "WAL file is empty"
        wal_lines = [l for l in wal_text.split("\n") if l.strip()]
        assert len(wal_lines) >= 1, f"Expected WAL events, got {len(wal_lines)} lines"

        # Verify event structure
        event_types = []
        for line in wal_lines:
            ev = json.loads(line)
            assert "type" in ev, f"WAL event missing 'type': {line}"
            event_types.append(ev["type"])

        # Should contain start + token events (end may or may not be present)
        assert "start" in event_types, f"WAL missing 'start' event: {event_types}"
        print(f"[PASS] WAL file exists with {len(wal_lines)} events: {event_types}")

    def test_wal_deleted_on_clean_completion(self):
        """Verify WAL is deleted when a stream completes normally (finally block)."""
        r = api_post("/api/session/new")
        assert r.status_code == 200
        body = r.json()
        session_id = body.get("session_id") or (body.get("session") or {}).get("session_id")
        assert session_id

        r = api_post("/api/chat/start", json={
            "session_id": session_id,
            "message": "Hello, how are you?",
            "activeProfile": "default",
        }, timeout=5)

        # Wait for stream to fully complete
        time.sleep(5)

        wal_path = REAL_SESSION_DIR / f"{session_id}_wal.jsonl"
        assert not wal_path.exists(), (
            f"WAL should be deleted after clean completion, found at {wal_path}"
        )
        print("[PASS] WAL deleted after clean stream completion")

    def test_crash_recovery_on_session_reload(self, _isolate_models_session_dir):
        """Simulate crash: session JSON has user msg + pending, WAL has tokens.
        Verify WAL replay recovers the assistant text on session load."""
        session_dir, _ = _isolate_models_session_dir

        # Use only alphanumeric chars for session_id (matches real session IDs)
        sid = "wl" + uuid.uuid4().hex[:12]

        # Build session JSON as it would look mid-stream after checkpoint:
        # user message present, active_stream_id set, NO assistant reply yet.
        session_data = {
            "session_id": sid,
            "title": "WAL Live Test",
            "workspace": str(session_dir),
            "model": "test-model",
            "messages": [
                {"role": "user", "content": "Tell me a story about a robot"},
            ],
            "tool_calls": [],
            "created_at": time.time(),
            "updated_at": time.time(),
            "active_stream_id": "dead_stream_123",
            "pending_user_message": "Tell me a story about a robot",
            "pending_attachments": [],
            "pending_started_at": time.time(),
        }
        session_path = session_dir / f"{sid}.json"
        session_path.write_text(json.dumps(session_data), encoding="utf-8")

        # Write WAL events as if the agent was mid-stream when killed
        _wal.write_wal_start(sid, "dead_stream_123")
        _wal.write_wal_token(sid, "Once ")
        _wal.write_wal_token(sid, "upon ")
        _wal.write_wal_token(sid, "a ")
        _wal.write_wal_token(sid, "time, ")
        _wal.write_wal_token(sid, "in a ")
        _wal.write_wal_token(sid, "factory ")
        _wal.write_wal_token(sid, "far ")
        _wal.write_wal_token(sid, "away...")
        # Simulate crash — no 'end' event

        # Clear in-memory cache to force disk load
        with LOCK:
            _models.SESSIONS.clear()

        # Patch STREAMS to simulate dead stream (not in STREAMS)
        orig_streams = _config.STREAMS.copy()
        _config.STREAMS.clear()

        try:
            s = _models.get_session(sid)
        finally:
            _config.STREAMS.update(orig_streams)
            with LOCK:
                _models.SESSIONS.pop(sid, None)

        # Verify WAL replay appended the assistant message
        assert len(s.messages) == 2, (
            f"Expected 2 messages after WAL replay, got {len(s.messages)}: "
            f"{[m.get('content', '')[:30] for m in s.messages]}"
        )
        assistant_msg = s.messages[1]
        assert assistant_msg["role"] == "assistant"
        assert "Once" in assistant_msg["content"], (
            f"Expected 'Once' in recovered content, got: {assistant_msg['content']}"
        )
        assert s.active_stream_id is None, "active_stream_id should be cleared after WAL recovery"

        # WAL file should be deleted after successful recovery
        assert not _wal.wal_path(sid).exists(), "WAL should be deleted after recovery"

        # Clean up
        if session_path.exists():
            session_path.unlink()
        _wal.delete_wal(sid)

        print("[PASS] WAL replay recovered: " + assistant_msg["content"][:50])


if __name__ == "__main__":
    import requests

    print("WAL Live Recovery Tests")
    print("=" * 50)

    if not wait_for(f"{BASE_URL}/"):
        print(f"[FATAL] hermes-webui not reachable at {BASE_URL}")
        sys.exit(1)
    print(f"[INFO] hermes-webui is up at {BASE_URL}")

    pytest.main([__file__, "-v"])
