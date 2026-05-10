import queue
import threading
from pathlib import Path

import api.config as config
import api.routes as routes

REPO = Path(__file__).resolve().parents[1]
ROUTES_SRC = (REPO / "api" / "routes.py").read_text(encoding="utf-8")
SESSIONS_SRC = (REPO / "static" / "sessions.js").read_text(encoding="utf-8")
SW_SRC = (REPO / "static" / "sw.js").read_text(encoding="utf-8")


class _GateLock:
    def __init__(self):
        self._lock = threading.Lock()
        self.lookup_finished = threading.Event()
        self.writer_finished = threading.Event()

    def __enter__(self):
        self._lock.acquire()
        return self

    def __exit__(self, exc_type, exc, tb):
        self._lock.release()
        if not self.lookup_finished.is_set():
            self.lookup_finished.set()
            assert self.writer_finished.wait(2), "writer did not finish race setup"
        return False


class _FakeSession:
    session_id = "issue1533-session"

    def __init__(self):
        self.active_stream_id = "stale-stream"
        self.pending_user_message = "old prompt"
        self.pending_attachments = ["old.txt"]
        self.pending_started_at = 123
        self.saved_stream_ids = []

    def save(self):
        self.saved_stream_ids.append(self.active_stream_id)


def test_stale_stream_cleanup_helper_exists():
    assert "def _clear_stale_stream_state(session)" in ROUTES_SRC
    assert "stream_id in STREAMS" in ROUTES_SRC
    assert "session.active_stream_id = None" in ROUTES_SRC
    assert "session.pending_user_message = None" in ROUTES_SRC
    assert "session.pending_attachments = []" in ROUTES_SRC
    assert "session.pending_started_at = None" in ROUTES_SRC
    assert "session.save()" in ROUTES_SRC


def test_session_load_clears_stale_stream_before_response():
    load_pos = ROUTES_SRC.index("s = get_session(sid, metadata_only=(not load_messages))")
    cleanup_pos = ROUTES_SRC.index("_clear_stale_stream_state(s)", load_pos)
    response_pos = ROUTES_SRC.index('"active_stream_id": getattr(s, "active_stream_id", None)', cleanup_pos)
    assert load_pos < cleanup_pos < response_pos


def test_chat_start_clears_stale_pending_state_not_only_active_id():
    stale_comment_pos = ROUTES_SRC.index("# Stale stream id from a previous run; clear and continue.")
    cleanup_pos = ROUTES_SRC.index("_clear_stale_stream_state(s)", stale_comment_pos)
    stream_id_pos = ROUTES_SRC.index("stream_id = uuid.uuid4().hex", cleanup_pos)
    assert stale_comment_pos < cleanup_pos < stream_id_pos


def test_stale_stream_cleanup_does_not_clobber_concurrent_chat_start(monkeypatch):
    """Regression for #1533: stale cleanup must not erase a new stream id.

    The gate lock pauses the cleanup thread after it has decided that the old
    stream id is stale, then lets a chat_start-like writer register and persist
    a new active_stream_id for the same session.
    """
    config.STREAMS.clear()
    config.SESSION_AGENT_LOCKS.clear()
    gate_lock = _GateLock()
    session = _FakeSession()
    new_stream_id = "new-stream"
    result = {}

    monkeypatch.setattr(routes, "STREAMS_LOCK", gate_lock)

    def cleanup_stale_stream():
        result["cleared"] = routes._clear_stale_stream_state(session)

    def start_new_stream():
        assert gate_lock.lookup_finished.wait(2), "cleanup did not reach race point"
        with routes.STREAMS_LOCK:
            routes.STREAMS[new_stream_id] = queue.Queue()
        with routes._get_session_agent_lock(session.session_id):
            session.active_stream_id = new_stream_id
            session.pending_user_message = "new prompt"
            session.pending_attachments = ["new.txt"]
            session.pending_started_at = 456
            session.save()
        gate_lock.writer_finished.set()

    cleanup_thread = threading.Thread(target=cleanup_stale_stream)
    writer_thread = threading.Thread(target=start_new_stream)
    cleanup_thread.start()
    writer_thread.start()
    cleanup_thread.join(2)
    writer_thread.join(2)

    assert not cleanup_thread.is_alive()
    assert not writer_thread.is_alive()
    assert result["cleared"] is False
    assert session.active_stream_id == new_stream_id
    assert session.pending_user_message == "new prompt"
    assert session.pending_attachments == ["new.txt"]
    assert session.pending_started_at == 456


def test_frontend_drops_inflight_cache_when_server_session_is_idle():
    marker = "If the server says the session is idle, discard any browser-side inflight"
    marker_pos = SESSIONS_SRC.index(marker)
    window = SESSIONS_SRC[marker_pos:marker_pos + 500]
    assert "if(!activeStreamId&&INFLIGHT[sid])" in window
    assert "delete INFLIGHT[sid]" in window
    assert "clearInflightState" in window
    assert "S.busy=false" in window


def test_service_worker_cache_bumped_for_frontend_fix_delivery():
    """The SW CACHE_NAME must be keyed on the WEBUI_VERSION placeholder so
    every release naturally invalidates the previous shell cache and delivers
    the frontend half of the stale-stream cleanup fix to existing browsers.

    Originally pinned a manual `-stale-stream-cleanup1` suffix on
    `CACHE_NAME` (PR #1525 author shipped that to force-bump existing
    SWs). During the v0.50.279 stage build that suffix collided with the
    independent #1517 placeholder rename (`__CACHE_VERSION__` →
    `__WEBUI_VERSION__`), so the maintainer dropped the manual suffix in
    favor of the canonical version-token path. The natural bump still
    invalidates the old cache via `keys.filter((k) => k !== CACHE_NAME)`
    in the activate handler — same delivery guarantee, less churn.
    """
    # CACHE_NAME must include the WEBUI_VERSION placeholder so each release
    # produces a different cache name. The activate handler then deletes any
    # cache whose key != current CACHE_NAME, so the old shell is reaped on
    # every upgrade and the new sessions.js (with the INFLIGHT[sid] clear)
    # ships to existing browsers.
    assert "CACHE_NAME = 'hermes-shell-__WEBUI_VERSION__'" in SW_SRC, (
        "SW CACHE_NAME must include __WEBUI_VERSION__ so each release "
        "invalidates the previous cache and delivers frontend changes."
    )
