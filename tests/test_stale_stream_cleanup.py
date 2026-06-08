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
        self.messages = []
        self.saved_stream_ids = []
        self.saved_touch_updated_at = []

    def save(self, *, touch_updated_at=True):
        self.saved_stream_ids.append(self.active_stream_id)
        self.saved_touch_updated_at.append(touch_updated_at)


def test_stale_stream_cleanup_helper_exists():
    assert "def _clear_stale_stream_state(session)" in ROUTES_SRC
    assert "stream_id in STREAMS" in ROUTES_SRC
    assert "session.active_stream_id = None" in ROUTES_SRC
    assert "session.pending_user_message = None" in ROUTES_SRC
    assert "session.pending_attachments = []" in ROUTES_SRC
    assert "session.pending_started_at = None" in ROUTES_SRC
    assert "session.save(touch_updated_at=False)" in ROUTES_SRC


def test_stale_stream_cleanup_does_not_refresh_sidebar_timestamp():
    config.STREAMS.clear()
    config.SESSION_AGENT_LOCKS.clear()
    session = _FakeSession()

    assert routes._clear_stale_stream_state(session) is True

    assert session.active_stream_id is None
    assert session.saved_touch_updated_at == [False]


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


def test_chat_start_rechecks_active_stream_under_session_lock(monkeypatch, tmp_path):
    """A concurrent chat_start must not overwrite stream ownership.

    The first request can pass the pre-lock active_stream_id check while another
    request is waiting/running. Once this request enters the session lock, it
    must re-read active_stream_id and reject instead of creating a ghost stream.
    """
    config.STREAMS.clear()
    config.SESSION_AGENT_LOCKS.clear()
    existing_stream_id = "already-running-stream"

    class ChatStartSession:
        session_id = "duplicate-start-session"

        def __init__(self):
            self.active_stream_id = None
            self.pending_user_message = None
            self.pending_attachments = []
            self.pending_started_at = None
            self.messages = []
            self.title = "Untitled"
            self.worktree_path = None
            self.workspace = None
            self.model = None
            self.model_provider = None

        def save(self, *args, **kwargs):
            return None

    session = ChatStartSession()

    class MutatingSessionLock:
        def __enter__(self):
            session.active_stream_id = existing_stream_id
            session.pending_user_message = "prompt already claimed by another start"
            session.pending_started_at = 123.0
            routes.STREAMS[existing_stream_id] = queue.Queue()
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

    class NoopThread:
        def __init__(self, *args, **kwargs):
            self.args = args
            self.kwargs = kwargs

        def start(self):
            return None

    monkeypatch.setattr(routes, "_get_session_agent_lock", lambda sid: MutatingSessionLock())
    monkeypatch.setattr(routes.uuid, "uuid4", lambda: type("FakeUuid", (), {"hex": "new-stream"})())
    monkeypatch.setattr(routes, "set_last_workspace", lambda workspace: None)
    monkeypatch.setattr(routes, "create_stream_channel", lambda: queue.Queue())
    monkeypatch.setattr(routes.threading, "Thread", NoopThread)

    try:
        response = routes._start_chat_stream_for_session(
            session,
            msg="please start once",
            attachments=[],
            workspace=str(tmp_path),
            model="test-model",
            model_provider=None,
        )

        assert response["_status"] == 409
        assert response["active_stream_id"] == existing_stream_id
        assert session.active_stream_id == existing_stream_id
        assert "new-stream" not in routes.STREAMS
    finally:
        routes.STREAMS.pop(existing_stream_id, None)


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
