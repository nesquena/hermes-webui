"""#7738 stale-window protections, checked on the fast draft path.

tests/test_issue7738_stale_session_overwrite.py injects each race inside the session agent lock, which
a reloading draft save takes. The ordinary fast draft save takes only the draft lifecycle lock (it never
loads, publishes or saves the session), so those three draft cases cannot inject their race there:
- test_draft_autosave_does_not_undo_effort_pick_saved_after_eviction
- test_draft_save_publishes_over_copy_cached_by_get_in_window
- test_empty_session_deleted_before_lock_is_not_resurrected[draft]
These tests run the same races inside the lock the fast path does take and check the same outcomes.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

import api.models as models
import api.routes as routes
from api.models import SESSIONS, Session
from tests.test_issue7738_stale_session_overwrite import (
    _FakeHandler, _capture_post, _isolate_session_store, _seed_stale_session,
    _stub_reasoning_profile_side_effects,
)


class _InjectBeforeDraftLock:
    """The draft lifecycle lock, running ``action`` once just before the draft path acquires it."""

    def __init__(self, inner, action, fired):
        self._inner, self._action, self._fired = inner, action, fired

    def __enter__(self):
        if not self._fired:
            self._fired.append(True)
            self._action()
        self._inner.acquire()
        return self

    def __exit__(self, *exc):
        self._inner.release()


def _inject(monkeypatch, action):
    real = routes._draft_lifecycle_lock_for_session_id
    fired = []
    monkeypatch.setattr(routes, "_draft_lifecycle_lock_for_session_id",
                        lambda value: _InjectBeforeDraftLock(real(value), action, fired))
    return fired


def _post_draft():
    handler = _FakeHandler(b"{}")
    handler.command = "POST"
    return routes.handle_post(handler, SimpleNamespace(path="/api/session/draft"))


@pytest.mark.parametrize("evict_after_pick", [False, True], ids=["pick-resident", "fully-evicted"])
def test_draft_save_does_not_undo_effort_pick_and_turn_in_its_window(tmp_path, monkeypatch, evict_after_pick):
    session_dir = _isolate_session_store(tmp_path, monkeypatch)
    sid = "stale-draft-effort-8402"
    _stub_reasoning_profile_side_effects(monkeypatch)
    stale = _seed_stale_session(session_dir, sid, title="t", draft={"text": "old draft"})
    stale.reasoning_effort = "low"
    stale.save()
    bodies = {"body": {"session_id": sid, "text": "new draft"}}
    captured = _capture_post(monkeypatch, None)
    monkeypatch.setattr(routes, "read_body", lambda handler: bodies["body"])

    def pick_and_turn():
        SESSIONS.pop(sid, None)  # count-cap eviction, then a turn saved by another writer
        turn = Session.load(sid)
        turn.messages = list(turn.messages) + [
            {"role": "assistant", "content": "hello"}, {"role": "user", "content": "newer turn"}]
        turn.save()
        draft_body = bodies["body"]
        bodies["body"] = {"effort": "xhigh", "session_id": sid}
        routes.handle_post(_FakeHandler(b"{}"), SimpleNamespace(path="/api/reasoning"))
        assert captured["status"] == 200, f"effort pick failed: {captured}"
        bodies["body"] = draft_body
        if evict_after_pick:
            SESSIONS.pop(sid, None)

    fired = _inject(monkeypatch, pick_and_turn)
    assert _post_draft() is True
    assert fired, "the effort pick must run inside the draft's window"
    assert captured["status"] == 200, f"expected 200, got {captured}"
    SESSIONS.clear()
    cold = Session.load(sid)
    assert cold.reasoning_effort == "xhigh", f"the draft save undid the effort pick; got {cold.reasoning_effort!r}"
    assert cold.composer_draft == {"text": "new draft"}
    assert len(cold.messages) == 3, f"got {len(cold.messages)} messages"


def test_get_caching_the_old_copy_in_the_draft_window_cannot_save_the_old_draft_back(tmp_path, monkeypatch):
    session_dir = _isolate_session_store(tmp_path, monkeypatch)
    sid = "draft-get-window-8402"
    _stub_reasoning_profile_side_effects(monkeypatch)
    _seed_stale_session(session_dir, sid, title="t", draft={"text": "old draft"})
    SESSIONS.pop(sid, None)
    bodies = {"body": {"session_id": sid, "text": "new draft"}}
    captured = _capture_post(monkeypatch, None)
    monkeypatch.setattr(routes, "read_body", lambda handler: bodies["body"])

    fired = _inject(monkeypatch, lambda: routes.get_session(sid))  # a concurrent GET caches the old draft
    assert _post_draft() is True
    assert fired and captured["status"] == 200, f"expected 200, got {captured}"

    bodies["body"] = {"effort": "high", "session_id": sid}
    routes.handle_post(_FakeHandler(b"{}"), SimpleNamespace(path="/api/reasoning"))
    assert captured["status"] == 200, f"effort pick failed: {captured}"
    SESSIONS.clear()
    cold = Session.load(sid)
    assert cold.reasoning_effort == "high"
    assert cold.composer_draft == {"text": "new draft"}, (
        f"a later effort pick saved the cached old draft back; got {cold.composer_draft!r}")


def test_session_deleted_in_the_draft_window_is_not_resurrected(tmp_path, monkeypatch):
    session_dir = _isolate_session_store(tmp_path, monkeypatch)
    sid = "deleted-in-window-8402"
    _stub_reasoning_profile_side_effects(monkeypatch)
    monkeypatch.setattr(routes, "_lookup_cli_session_metadata", lambda value: {})
    outside = Session(session_id=sid, title="Untitled", workspace=str(tmp_path),
                      composer_draft={"text": "draft"})
    outside.save()
    SESSIONS[sid] = outside
    monkeypatch.setattr(routes, "_DELETED_SESSION_IDS", set())
    monkeypatch.setattr(models, "_record_webui_deleted_session_tombstone", lambda value: None)
    monkeypatch.setattr(routes, "_record_webui_deleted_session_tombstone", lambda value: None)
    body = {"session_id": sid, "text": "late keystroke"}
    captured = _capture_post(monkeypatch, body)
    deleted = {}

    def delete_in_window():
        monkeypatch.setattr(routes, "read_body", lambda handler: {"session_id": sid})
        monkeypatch.setattr(routes, "delete_cli_session", lambda value: True, raising=False)
        monkeypatch.setattr(models, "delete_cli_session", lambda value: True)
        monkeypatch.setattr(routes, "_publish_session_list_changed", lambda *a, **kw: None)
        routes.handle_post(_FakeHandler(b"{}"), SimpleNamespace(path="/api/session/delete"))
        deleted.update(captured)
        monkeypatch.setattr(routes, "read_body", lambda handler: body)

    fired = _inject(monkeypatch, delete_in_window)
    _post_draft()
    assert fired and deleted.get("status") == 200, f"delete failed: {deleted}"
    assert captured["status"] == 409, f"expected 409, got {captured}"
    assert captured["payload"].get("error") == "Session no longer active"
    assert not (session_dir / ".drafts" / f"{sid}.json").exists()
    assert sid not in SESSIONS
    assert not (session_dir / f"{sid}.json").exists()
