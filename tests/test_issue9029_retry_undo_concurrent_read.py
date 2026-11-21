"""Regression tests for #9029: a concurrent full GET between an intentional
shrink (retry/undo/truncate/clear) and its save reloads the OLD sidecar over
the shrunk cached object, putting the removed reply back.

The window is hit deterministically: Session.save is patched so the
concurrent full read runs at the exact moment the in-memory object has been
truncated but the sidecar has not been rewritten yet.
"""
from __future__ import annotations


def _msg(role: str, content: str, ts: float, mid: str) -> dict:
    return {"id": mid, "role": role, "content": content, "timestamp": ts}


def _five_message_sidecar(session_id: str):
    """The issue's shape: q1, a1, [partial], q2, OLD-A2."""
    from api.models import Session

    return Session(
        session_id=session_id,
        messages=[
            _msg("user", "q1", 1.0, "u1"),
            _msg("assistant", "a1", 2.0, "a1"),
            _msg("assistant", "partial", 2.5, "p1"),
            _msg("user", "q2", 3.0, "u2"),
            _msg("assistant", "OLD-A2", 4.0, "a2"),
        ],
    )


def _prepare_session_dir(monkeypatch, tmp_path, session_id: str):
    import api.models as models

    session_dir = tmp_path / "sessions"
    session_dir.mkdir(parents=True)
    monkeypatch.setattr(models, "SESSION_DIR", session_dir)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", session_dir / "_index.json")
    models.SESSIONS.clear()
    return models


def _patch_save_with_concurrent_full_read(monkeypatch, models, session_id: str):
    """Run one full get_session() inside save(), i.e. after the in-memory
    truncation but before the sidecar is rewritten — the issue's race window,
    without relying on timing."""
    original_save = models.Session.save
    seen = []
    in_window = False

    def save_with_concurrent_read(self, *args, **kwargs):
        nonlocal in_window
        if str(getattr(self, "session_id", "")) == session_id and not in_window:
            in_window = True
            try:
                seen.append(models.get_session(session_id))
            finally:
                in_window = False
        return original_save(self, *args, **kwargs)

    monkeypatch.setattr(models.Session, "save", save_with_concurrent_read)
    return seen


def test_retry_concurrent_full_read_in_window_does_not_restore_removed_reply(
    monkeypatch, tmp_path
):
    """#9029 core repro: retry_last truncates the cached object, then saves.
    A full GET landing in between must not reload the old sidecar back into
    the cache — the removed reply OLD-A2 must not come back."""
    import api.session_ops as session_ops

    models = _prepare_session_dir(monkeypatch, tmp_path, "retryrace")
    session = _five_message_sidecar("retryrace")
    session.save()
    models.SESSIONS["retryrace"] = session

    _patch_save_with_concurrent_full_read(monkeypatch, models, "retryrace")

    session_ops.retry_last("retryrace")

    # Disk must hold the truncated transcript (OLD-A2 gone).
    on_disk = models.Session.load("retryrace")
    assert on_disk is not None
    disk_contents = [m["content"] for m in on_disk.messages]
    assert "OLD-A2" not in disk_contents, (
        f"retry did not persist the truncation: {disk_contents}"
    )
    assert disk_contents == ["q1", "a1", "partial"]

    # The NEXT full read must still see the truncated transcript — the
    # concurrent read inside the window must not have reloaded the old file.
    after = models.get_session("retryrace")
    after_contents = [m["content"] for m in after.messages]
    assert "OLD-A2" not in after_contents, (
        "the concurrent full GET reloaded the old sidecar over the shrunk "
        f"cache: {after_contents}"
    )
    assert after_contents == ["q1", "a1", "partial"]


def test_undo_concurrent_full_read_in_window_does_not_restore_removed_reply(
    monkeypatch, tmp_path
):
    """Same shape for undo_last (#9029)."""
    import api.session_ops as session_ops

    models = _prepare_session_dir(monkeypatch, tmp_path, "undorace")
    session = _five_message_sidecar("undorace")
    session.save()
    models.SESSIONS["undorace"] = session

    _patch_save_with_concurrent_full_read(monkeypatch, models, "undorace")

    session_ops.undo_last("undorace")

    on_disk = models.Session.load("undorace")
    assert on_disk is not None
    disk_contents = [m["content"] for m in on_disk.messages]
    assert disk_contents == ["q1", "a1", "partial"], (
        f"undo did not persist the truncation: {disk_contents}"
    )

    after = models.get_session("undorace")
    after_contents = [m["content"] for m in after.messages]
    assert "OLD-A2" not in after_contents, (
        "the concurrent full GET reloaded the old sidecar over the shrunk "
        f"cache after undo: {after_contents}"
    )
    assert after_contents == ["q1", "a1", "partial"]


def test_truncate_endpoint_concurrent_read_in_window_does_not_restore_messages(
    monkeypatch, tmp_path
):
    """Same window on the real /api/session/truncate route (#9029).

    The truncate route already republishes the saved object after its save,
    so this passes on unpatched master as well — it guards that the
    shrink-window handling keeps truncate correct, it is not a RED repro.
    """
    import json
    from io import BytesIO
    from types import SimpleNamespace

    import api.routes as routes

    models = _prepare_session_dir(monkeypatch, tmp_path, "truncrace")
    session = _five_message_sidecar("truncrace")
    session.save()
    models.SESSIONS["truncrace"] = session

    _patch_save_with_concurrent_full_read(monkeypatch, models, "truncrace")

    body = {"session_id": "truncrace", "keep_count": 3}
    body_bytes = json.dumps(body).encode()
    monkeypatch.setattr(routes, "_check_csrf", lambda handler: True)
    captured = {}

    def fake_j(handler, payload, status=200, extra_headers=None):
        captured.update(payload)

    monkeypatch.setattr(routes, "j", fake_j)
    monkeypatch.setattr("api.config._evict_session_agent", lambda _sid: None)

    handler = SimpleNamespace(
        headers={"Content-Length": str(len(body_bytes))},
        rfile=BytesIO(body_bytes),
    )
    routes.handle_post(handler, SimpleNamespace(path="/api/session/truncate"))

    assert captured.get("ok") is True
    on_disk = models.Session.load("truncrace")
    assert on_disk is not None
    disk_contents = [m["content"] for m in on_disk.messages]
    assert disk_contents == ["q1", "a1", "partial"], (
        f"truncate did not persist: {disk_contents}"
    )
    after = models.get_session("truncrace")
    after_contents = [m["content"] for m in after.messages]
    assert "OLD-A2" not in after_contents, (
        "the concurrent full GET reloaded the old sidecar over the shrunk "
        f"cache after truncate: {after_contents}"
    )
    assert after_contents == ["q1", "a1", "partial"]


def test_genuine_stale_cache_still_reloads_from_disk(monkeypatch, tmp_path):
    """Control group: the freshness check must keep doing its actual job.

    When the sidecar is legitimately AHEAD of the cache (another writer
    persisted new messages while the LRU still holds the older object),
    get_session() must reload from disk. The #9029 fix must not turn the
    shrink-window exemption into a blanket 'cache is always fresh'."""
    models = _prepare_session_dir(monkeypatch, tmp_path, "staleref")

    cached = _five_message_sidecar("staleref")
    cached.save()
    models.SESSIONS["staleref"] = cached

    # Another writer persists two NEW messages through a different object,
    # exactly like the active/reconnect path this check exists for.
    other = models.Session.load("staleref")
    assert other is not None
    other.messages = other.messages + [
        _msg("user", "q3", 5.0, "u3"),
        _msg("assistant", "A3", 6.0, "a3"),
    ]
    other.save()

    resolved = models.get_session("staleref")
    contents = [m["content"] for m in resolved.messages]
    assert "q3" in contents and "A3" in contents, (
        "a genuinely stale cache (disk ahead) must still be reloaded; "
        f"got {contents}"
    )
    assert len(contents) == 7
