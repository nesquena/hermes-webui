"""Regression tests for #2863 missing session-index background rebuild."""
from __future__ import annotations

import json
import time


def test_missing_index_starts_background_rebuild_while_preserving_first_scan(monkeypatch, tmp_path):
    import api.models as models
    from api.models import all_sessions

    # Hermetic isolation: a prior test in the same worker may have left the
    # background rebuild thread bookkeeping populated. Since #3884 the start
    # helper short-circuits when a rebuild thread for the same target is still
    # alive, so stale globals from another test could suppress the fresh thread
    # this test asserts on. Join any leftover thread and clear both globals so
    # this test only observes the thread IT triggers, regardless of run order.
    _stale = getattr(models, "_SESSION_INDEX_REBUILD_THREAD", None)
    if _stale is not None:
        try:
            _stale.join(timeout=5)
        except Exception:
            pass
    models._SESSION_INDEX_REBUILD_THREAD = None
    if hasattr(models, "_SESSION_INDEX_REBUILD_THREAD_TARGET"):
        models._SESSION_INDEX_REBUILD_THREAD_TARGET = None

    session_dir = tmp_path / "sessions"
    session_dir.mkdir(parents=True)
    monkeypatch.setattr(models, "SESSION_DIR", session_dir)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", session_dir / "_index.json")
    models.SESSIONS.clear()

    for idx in range(3):
        payload = {
            "session_id": f"issue2863{idx}",
            "title": f"Session {idx}",
            "workspace": str(tmp_path),
            "model": "test-model",
            "messages": [{"role": "user", "content": f"hello {idx}", "timestamp": time.time() + idx}],
            "created_at": time.time() + idx,
            "updated_at": time.time() + idx,
        }
        (session_dir / f"issue2863{idx}.json").write_text(json.dumps(payload), encoding="utf-8")

    rows = all_sessions()

    assert {row["session_id"] for row in rows} == {"issue28630", "issue28631", "issue28632"}

    thread = models._SESSION_INDEX_REBUILD_THREAD
    assert thread is not None
    thread.join(timeout=5)
    assert not thread.is_alive()

    index = json.loads(models.SESSION_INDEX_FILE.read_text(encoding="utf-8"))
    assert {row["session_id"] for row in index} == {"issue28630", "issue28631", "issue28632"}
    assert {row["session_id"] for row in all_sessions()} == {"issue28630", "issue28631", "issue28632"}
