import collections
import json
import threading


class RecordingLock:
    def __init__(self):
        self._lock = threading.RLock()
        self.depth = 0

    def __enter__(self):
        self._lock.acquire()
        self.depth += 1
        return self

    def __exit__(self, exc_type, exc, tb):
        self.depth -= 1
        self._lock.release()

    @property
    def held(self):
        return self.depth > 0


def test_session_index_fast_path_keeps_json_work_outside_global_lock(monkeypatch, tmp_path):
    import api.models as models

    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    index_file = session_dir / "_index.json"
    monkeypatch.setattr(models, "SESSION_DIR", session_dir)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", index_file)

    old = models.Session(session_id="idx_old", title="Old", updated_at=1.0)
    updated = models.Session(session_id="idx_updated", title="Updated", updated_at=20.0)
    (session_dir / "idx_old.json").write_text("{}", encoding="utf-8")
    (session_dir / "idx_updated.json").write_text("{}", encoding="utf-8")
    index_file.write_text(
        json.dumps(
            [
                old.compact(),
                models.Session(session_id="idx_updated", title="Stale", updated_at=2.0).compact(),
            ]
        ),
        encoding="utf-8",
    )

    lock = RecordingLock()
    monkeypatch.setattr(models, "LOCK", lock)
    monkeypatch.setattr(models, "SESSIONS", collections.OrderedDict())

    original_loads = models.json.loads
    original_dumps = models.json.dumps

    def loads_outside_lock(*args, **kwargs):
        assert not lock.held
        return original_loads(*args, **kwargs)

    def dumps_outside_lock(*args, **kwargs):
        assert not lock.held
        return original_dumps(*args, **kwargs)

    monkeypatch.setattr(models.json, "loads", loads_outside_lock)
    monkeypatch.setattr(models.json, "dumps", dumps_outside_lock)

    models._write_session_index(updates=[updated])

    rows = json.loads(index_file.read_text(encoding="utf-8"))
    assert [row["session_id"] for row in rows] == ["idx_updated", "idx_old"]
    assert rows[0]["title"] == "Updated"
    assert rows[0]["updated_at"] == 20.0
