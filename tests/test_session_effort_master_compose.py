"""#8253 x master: the per-session effort persist lock composed with master's
draft lifecycle (#8193 series) and cold-publication write generations (#8267).

1. Lock order. ``Session.save()`` holds ``_session_persist_lock(sid)`` and,
   for an indexed save, takes ``_INDEX_WRITE_LOCK`` and then ``LOCK`` in
   ``_write_session_index``. Master's first-draft owner-record save runs under
   ``LOCK``; it must already own the persist lock when it does, or it waits on
   that lock while holding ``LOCK`` (the reverse order: a deadlock with a
   concurrent same-id save).
2. The pick's field-only sidecar write is a sidecar replace like any other: it
   bumps the #8267 write generation, so a cold ``get_session()`` that read the
   pre-pick sidecar re-reads instead of publishing the old effort.
3. The draft's lock-held ``get_session()`` reload keeps the CLI source
   identity the outside-resolved object carried (the PR's #7776 carry).
"""

from __future__ import annotations

import io
import json
import threading
from types import SimpleNamespace

import api.models as models
import api.routes as routes
from api.models import SESSIONS, Session


class _FakeHandler:
    def __init__(self):
        self.headers = {"Content-Length": "2", "Content-Type": "application/json"}
        self.rfile = io.BytesIO(b"{}")
        self.wfile = io.BytesIO()
        self.client_address = ("127.0.0.1", 12345)
        self.command = "POST"

    def send_response(self, status):
        self.status = status

    def send_header(self, key, value):
        pass

    def end_headers(self):
        pass

    def _safe_webui_print(self, *args, **kwargs):  # the draft handler's [SLOW] log
        pass


def _isolate_session_store(tmp_path, monkeypatch):
    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    index_file = session_dir / "_index.json"
    monkeypatch.setattr(models, "SESSION_DIR", session_dir)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", index_file)
    monkeypatch.setattr(routes, "SESSION_DIR", session_dir)
    monkeypatch.setattr(routes, "SESSION_INDEX_FILE", index_file)
    SESSIONS.clear()
    return session_dir


def _post(monkeypatch, path, body):
    captured: dict = {}
    monkeypatch.setattr(routes, "_check_csrf", lambda handler: True)
    monkeypatch.setattr(routes, "read_body", lambda handler: body)

    def _j(handler, payload, status=200, extra_headers=None, **_kw):
        captured["status"], captured["payload"] = status, payload
        return True

    def _bad(handler, msg, status=400, **_kw):
        captured["status"], captured["payload"] = status, {"error": msg}
        return True

    monkeypatch.setattr(routes, "j", _j)
    monkeypatch.setattr(routes, "bad", _bad)
    routes.handle_post(_FakeHandler(), SimpleNamespace(path=path))
    return captured


def test_first_draft_owner_record_save_owns_the_persist_lock_before_LOCK(tmp_path, monkeypatch):
    session_dir = _isolate_session_store(tmp_path, monkeypatch)
    sid = "draft-lock-order"
    SESSIONS[sid] = Session(session_id=sid, title="Untitled", workspace=str(tmp_path))
    assert not (session_dir / f"{sid}.json").exists()

    real_lock_for = models._session_persist_lock
    waits_under_lock = []

    def checked_lock_for(value):
        lock = real_lock_for(value)
        # Acquiring a persist lock this thread does not own while LOCK is held
        # is the inverted order (single-threaded test: LOCK.locked() is ours).
        if models.LOCK.locked() and not lock._is_owned():
            waits_under_lock.append(value)
        return lock

    monkeypatch.setattr(models, "_session_persist_lock", checked_lock_for)
    monkeypatch.setattr(routes, "_session_persist_lock", checked_lock_for)

    captured = _post(monkeypatch, "/api/session/draft", {"session_id": sid, "text": "first words"})

    assert captured["status"] == 200, captured
    assert (session_dir / f"{sid}.json").exists(), "the owner record was not materialized"
    assert waits_under_lock == [], (
        "a save waited on the per-session persist lock while holding LOCK "
        f"(persist -> _INDEX_WRITE_LOCK -> LOCK is the save order): {waits_under_lock}"
    )
    SESSIONS.clear()
    assert Session.load(sid).composer_draft == {"text": "first words"}


def test_field_only_effort_write_is_counted_for_cold_publication(tmp_path, monkeypatch):
    _isolate_session_store(tmp_path, monkeypatch)
    sid = "cold-pick"
    Session(
        session_id=sid, title="t", workspace=str(tmp_path), reasoning_effort="low",
        messages=[{"role": "user", "content": "hi"}, {"role": "assistant", "content": "hello"}],
    ).save()
    SESSIONS.clear()

    real_load = Session.load.__func__
    fired = []

    def load_then_pick(cls, value):
        loaded = real_load(cls, value)
        if value == sid and not fired:
            fired.append(True)
            # Another request's pick lands after this cold read and before its
            # publication (a different thread: the loader discounts its own
            # writes). Nothing is cached yet, so it is the field-only write.
            picker = threading.Thread(target=models.set_session_reasoning_effort, args=(sid, "xhigh"))
            picker.start()
            picker.join(10)
            assert not picker.is_alive()
        return loaded

    monkeypatch.setattr(Session, "load", classmethod(load_then_pick))
    resolved = models.get_session(sid)

    assert fired, "the pick must land inside the cold read"
    assert json.loads((models.SESSION_DIR / f"{sid}.json").read_text())["reasoning_effort"] == "xhigh"
    assert SESSIONS[sid].reasoning_effort == "xhigh", (
        "a cold get_session() published the pre-pick read: the field-only write "
        "did not bump the #8267 sidecar write generation"
    )
    assert resolved is SESSIONS[sid]


def test_draft_lock_held_reload_carries_cli_source_identity(tmp_path, monkeypatch):
    _isolate_session_store(tmp_path, monkeypatch)
    sid = "draft-cli-identity"
    Session(
        session_id=sid, title="cli title", workspace=str(tmp_path),
        messages=[{"role": "user", "content": "hi"}, {"role": "assistant", "content": "hello"}],
    ).save()  # the import save: WebUI-default identity on disk
    resident = Session.load(sid)
    identity = {
        "is_cli_session": True, "source_tag": "hermes-cli", "raw_source": "hermes-cli",
        "session_source": "cli", "source_label": "Hermes CLI",
    }
    for key, value in identity.items():
        setattr(resident, key, value)  # _get_or_materialize_session's in-memory stamp
    SESSIONS[sid] = resident

    real_lock_factory = routes._get_session_agent_lock

    class _EvictBeforeLock:
        def __init__(self, inner):
            self._inner = inner

        def __enter__(self):
            SESSIONS.pop(sid, None)  # count-cap eviction between resolve and lock
            self._inner.acquire()
            return self

        def __exit__(self, *exc):
            self._inner.release()

    monkeypatch.setattr(
        routes, "_get_session_agent_lock", lambda value: _EvictBeforeLock(real_lock_factory(value)),
    )
    captured = _post(monkeypatch, "/api/session/draft", {"session_id": sid, "text": "new draft"})

    assert captured["status"] == 200, captured
    reloaded = SESSIONS[sid]
    assert reloaded is not resident, "the draft must have re-resolved under the lock"
    for key, value in identity.items():
        assert getattr(reloaded, key) == value, f"the lock-held reload dropped {key}"
    # The next whole-session save of the published object keeps the identity.
    reloaded.save(touch_updated_at=False)
    on_disk = json.loads((models.SESSION_DIR / f"{sid}.json").read_text())
    for key, value in identity.items():
        assert on_disk.get(key) == value, key
