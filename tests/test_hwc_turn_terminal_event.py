"""hwc: a rejected error writeback emits apperror and does not save.

``active_stream_id is None`` on the worker-held session is not proof that
object is still canonical. Cancel clears the field, eviction reloads a
different instance, and a rename after Stop must survive the old worker.
"""

import json
import queue
import sys
import threading
import time
import types
from pathlib import Path

import api.config as config
import api.models as models
import api.streaming as streaming
from api.models import Session
from api.session_ops import apply_session_title_rename

STREAMING = Path(__file__).resolve().parents[1] / "api" / "streaming.py"
MARKER = "Skipping stale stream error writeback"
NEW_TITLE = "New user title after Stop"
OLD_TITLE = "Old title before Stop"


def _stale_error_branch(src: str) -> str:
    start = src.find(MARKER)
    assert start >= 0, "stale error writeback marker missing"
    lines = []
    for line in src[start:].splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        lines.append(line)
        if stripped == "return":
            return "\n".join(lines)
    raise AssertionError("stale error writeback branch has no return")


def test_stale_error_writeback_emits_apperror_without_saving_snapshot():
    branch = _stale_error_branch(STREAMING.read_text(encoding="utf-8"))
    assert "put('apperror'" in branch
    assert "stale_writeback_skipped" in branch
    assert ".save(" not in branch
    assert "pending_user_message = None" not in branch
    assert "if getattr(s, 'active_stream_id', None) is None:" not in branch


def test_stale_worker_after_stop_rename_keeps_title_and_emits_apperror(tmp_path, monkeypatch):
    """Real worker, cancel, eviction, cold reload, rename, then the old worker.

    The rejected snapshot still has ``active_stream_id is None``. Saving it
    would put the pre-Stop title back on the sidecar and the session index
    while the resident object keeps the rename.
    """
    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    index_file = session_dir / "_index.json"
    monkeypatch.setattr(models, "SESSION_DIR", session_dir)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", index_file)
    monkeypatch.setattr(streaming, "SESSION_DIR", session_dir)
    monkeypatch.setattr(config, "SESSION_DIR", session_dir)
    monkeypatch.setattr(config, "SESSION_INDEX_FILE", index_file)
    monkeypatch.setattr(models, "_sync_sidecar_from_state_db_if_newer", lambda *_a, **_k: False)
    monkeypatch.setattr(
        "api.state_sync.sync_session_cwd_background",
        lambda *_a, **_k: None,
    )

    models.SESSIONS.clear()
    config.STREAMS.clear()
    config.CANCEL_FLAGS.clear()
    config.AGENT_INSTANCES.clear()
    config.ACTIVE_RUNS.clear()
    config.SESSION_AGENT_LOCKS.clear()
    config.SESSION_WRITEBACK_OWNERS.clear()
    try:
        config.SESSION_AGENT_CACHE.clear()
    except Exception:
        pass

    sid = "stale_title_after_stop"
    stream_id = "stream-stale-title"
    msg_text = "hello before stop"
    captured = Session(
        session_id=sid,
        title=OLD_TITLE,
        workspace=str(tmp_path),
        model="gpt-4o",
        messages=[{"role": "user", "content": "earlier turn", "timestamp": 1.0}],
    )
    captured.active_stream_id = stream_id
    captured.pending_user_message = msg_text
    captured.pending_started_at = time.time()
    captured.pending_user_source = "webui"
    captured.manual_title = False
    captured.save()
    models.SESSIONS[sid] = captured
    event_queue = queue.Queue()
    config.STREAMS[stream_id] = event_queue
    config.register_stream_owner(stream_id, sid)

    reached = threading.Event()
    release = threading.Event()
    original_payload = streaming._provider_error_payload
    paused_once = {"done": False}

    def _pause_after_cancel_check(*args, **kwargs):
        # First call on this path is after ``if cancel_event.is_set()`` and
        # before the session lock / writeback decision.
        if not paused_once["done"]:
            paused_once["done"] = True
            reached.set()
            assert release.wait(20), "stale worker was not released"
        return original_payload(*args, **kwargs)

    class FakeAgent:
        def __init__(self, **kwargs):
            self.session_id = kwargs.get("session_id") or sid
            self.stream_delta_callback = kwargs.get("stream_delta_callback")
            self.context_compressor = None
            self.session_prompt_tokens = 0
            self.session_completion_tokens = 0
            self.session_estimated_cost_usd = None
            self.session_cache_read_tokens = 0
            self.session_cache_write_tokens = 0
            self.reasoning_config = None
            self.ephemeral_system_prompt = None

        def run_conversation(self, **_kwargs):
            raise RuntimeError("synthetic agent failure")

        def interrupt(self, _message=None):
            return None

    fake_hermes_state = types.ModuleType("hermes_state")
    fake_hermes_state.SessionDB = lambda *_a, **_k: object()

    monkeypatch.setattr(streaming, "_provider_error_payload", _pause_after_cancel_check)
    monkeypatch.setattr(streaming, "_get_ai_agent", lambda: FakeAgent)
    monkeypatch.setattr(
        streaming,
        "resolve_model_provider",
        lambda *_a, **_k: ("gpt-4o", "openai", None),
    )
    monkeypatch.setattr("api.config.get_config", lambda *_a, **_k: {})
    monkeypatch.setattr("api.config._resolve_cli_toolsets", lambda *_a, **_k: [])
    monkeypatch.setitem(sys.modules, "hermes_state", fake_hermes_state)

    errors = []

    def _run():
        try:
            streaming._run_agent_streaming(
                session_id=sid,
                msg_text=msg_text,
                model="gpt-4o",
                workspace=str(tmp_path),
                stream_id=stream_id,
            )
        except Exception as exc:  # pragma: no cover - worker swallows its own errors
            errors.append(exc)

    worker = threading.Thread(target=_run, name="stale-title-worker", daemon=True)
    worker.start()
    try:
        assert reached.wait(30), "worker did not reach the post-cancel-check pause"
        assert streaming.cancel_stream(stream_id) is True
        assert captured.active_stream_id is None

        filler = Session(
            session_id="filler_keep_resident",
            title="Filler",
            workspace=str(tmp_path),
            messages=[{"role": "user", "content": "keep", "timestamp": 1.0}],
        )
        filler.save()
        with models.LOCK:
            models.SESSIONS[filler.session_id] = filler
            models.SESSIONS.move_to_end(filler.session_id)
            evicted = models._evict_sessions_over_cap(1)
        assert evicted >= 1
        assert sid not in models.SESSIONS

        cold = models.get_session(sid)
        assert cold is not captured
        apply_session_title_rename(cold, NEW_TITLE)
        cold.save()
        assert models.SESSIONS.get(sid) is cold
        sidecar_after_rename = (session_dir / f"{sid}.json").read_text(encoding="utf-8")
        assert json.loads(sidecar_after_rename)["title"] == NEW_TITLE

        release.set()
        worker.join(30)
    finally:
        release.set()
        worker.join(5)
        models.SESSIONS.clear()
        config.STREAMS.clear()
        config.CANCEL_FLAGS.clear()
        config.AGENT_INSTANCES.clear()
        config.ACTIVE_RUNS.clear()
        config.SESSION_AGENT_LOCKS.clear()
        config.SESSION_WRITEBACK_OWNERS.clear()
        try:
            config.SESSION_AGENT_CACHE.clear()
        except Exception:
            pass

    assert not worker.is_alive()
    assert errors == []
    assert captured.title == OLD_TITLE
    assert cold.title == NEW_TITLE

    sidecar = json.loads((session_dir / f"{sid}.json").read_text(encoding="utf-8"))
    assert sidecar["title"] == NEW_TITLE
    assert sidecar["manual_title"] is True
    assert sidecar_after_rename == (session_dir / f"{sid}.json").read_text(encoding="utf-8")

    index = json.loads(index_file.read_text(encoding="utf-8"))
    row = next(entry for entry in index if entry.get("session_id") == sid)
    assert row["title"] == NEW_TITLE
    assert row["manual_title"] is True

    events = []
    while not event_queue.empty():
        events.append(event_queue.get_nowait())
    apperrors = [item[1] for item in events if item[0] == "apperror"]
    assert apperrors, "terminal apperror was not emitted"
    assert apperrors[-1].get("stale_writeback_skipped") is True
    assert apperrors[-1].get("session_id") == sid
