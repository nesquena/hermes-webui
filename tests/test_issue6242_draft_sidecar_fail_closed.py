"""Regression tests for #6242: draft clear must fail closed on sidecar unlink failure.

The authoritative clear path (``POST /api/session/draft`` with an emptied
composer) removes the per-session draft sidecar via
``delete_composer_draft_sidecar()``.  If that unlink raises ``OSError`` the
route returns a server error (never ``{"ok": true}``) and leaves the
recoverable draft untouched both in the sidecar and in the session JSON, so the
client can retain/recover it instead of a false clear.  Success and
absent-sidecar clears stay idempotent, and a retry after a failed unlink still
converges.

The clear decision is taken on the MERGED draft (supplied fields applied
first), not on the raw optional request fields: ``text: ''`` with ``files``
omitted must not delete a sidecar while stored attachments survive, and a
request that supplies only ``files: []`` — or omits both fields on an
unchanged empty draft — must still clean up a stale sidecar.  The 500 for a
failed unlink is serialized only after the per-session lock is released.
"""

from __future__ import annotations

import json
from collections import OrderedDict
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace

import pytest

pytestmark = pytest.mark.requires_agent_modules


# ── isolated session environment (mirrors test_issue5532 harness) ──────────


def _install_isolated_session_env(monkeypatch, tmp_path):
    import api.config as config
    import api.models as models
    import api.profiles as profiles
    import api.routes as routes

    monkeypatch.setattr(config, "STATE_DIR", tmp_path, raising=False)
    session_dir = tmp_path / "sessions"
    monkeypatch.setattr(config, "SESSION_DIR", session_dir, raising=False)
    monkeypatch.setattr(
        config, "SESSION_INDEX_FILE", session_dir / "_index.json", raising=False
    )
    monkeypatch.setattr(models, "SESSION_DIR", session_dir, raising=False)
    monkeypatch.setattr(
        models, "SESSION_INDEX_FILE", session_dir / "_index.json", raising=False
    )
    monkeypatch.setattr(models, "SESSIONS", OrderedDict(), raising=False)
    monkeypatch.setattr(
        profiles, "get_active_hermes_home", lambda: tmp_path, raising=False
    )
    monkeypatch.setattr(
        models, "_active_state_db_path", lambda: tmp_path / "state.db", raising=False
    )
    monkeypatch.setattr(
        routes, "_active_state_db_path", lambda: tmp_path / "state.db", raising=False
    )
    monkeypatch.setattr(config, "_evict_session_agent", lambda _sid: None, raising=False)
    session_dir.mkdir(parents=True, exist_ok=True)
    return session_dir


def _seed_session(tmp_path, sid, draft):
    from api.models import Session

    session = Session(
        session_id=sid,
        title="Draft fail-closed",
        workspace=str(tmp_path),
        model="test-model",
        messages=[],
        created_at=1000.0,
        updated_at=1001.0,
        composer_draft=draft,
    )
    session.save(touch_updated_at=False)
    return session


def _seed_sidecar(session_dir, sid, draft):
    """Create the authoritative per-session draft sidecar on disk."""
    sidecar_dir = session_dir / "_drafts"
    sidecar_dir.mkdir(parents=True, exist_ok=True)
    sidecar = sidecar_dir / f"{sid}.json"
    sidecar.write_text(json.dumps(draft), encoding="utf-8")
    return sidecar


def _fail_sidecar_unlink(monkeypatch, sid):
    """Make unlink of the *sid* sidecar raise OSError; all other unlinks pass.

    Returns a mutable state dict; set ``state["fail"] = False`` to restore
    normal unlink behaviour (used by the retry/idempotency test).
    """
    real_unlink = Path.unlink
    state = {"fail": True}

    def raising_unlink(self, *args, **kwargs):
        if state["fail"] and "_drafts" in self.parts and self.name == f"{sid}.json":
            raise OSError(13, "Permission denied", str(self))
        return real_unlink(self, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", raising_unlink)
    return state


def _post_draft(monkeypatch, sid, text="", files=None, on_response=None):
    """POST /api/session/draft and capture the route's JSON response.

    ``on_response(payload, status)`` — when given — runs inside the response
    sink itself (i.e. at the exact moment the route serializes its reply), so a
    test can observe state the response write must not be holding.
    """
    import api.helpers as helpers
    import api.routes as routes

    payload = {"session_id": sid}
    if text is not None:
        payload["text"] = text
    if files is not None:
        payload["files"] = files
    body = json.dumps(payload).encode("utf-8")

    monkeypatch.setattr(routes, "_check_csrf", lambda handler: True)
    monkeypatch.setattr(
        routes, "_guard_request_session_visibility", lambda *a, **k: True
    )

    captured = {}

    def fake_j(_handler, payload, status=200, extra_headers=None):
        if on_response is not None:
            on_response(payload, status)
        captured["payload"] = payload
        captured["status"] = status

    # The route calls `j` directly AND indirectly through `bad()` (which
    # resolves `j` inside api.helpers) — patch both bindings so the 500
    # fail-closed response is captured instead of hitting a real socket.
    monkeypatch.setattr(routes, "j", fake_j)
    monkeypatch.setattr(helpers, "j", fake_j)

    handler = SimpleNamespace(
        command="POST",
        headers={"Content-Length": str(len(body))},
        rfile=BytesIO(body),
    )
    routes.handle_post(handler, SimpleNamespace(path="/api/session/draft"))
    return captured


def _session_agent_lock(sid):
    """The very lock the route takes for *sid* (non-reentrant threading.Lock)."""
    import api.config as config

    return config._get_session_agent_lock(sid)


def _lock_state_at_response(sid):
    """Probe that records ``lock.locked()`` every time the route writes a reply."""
    lock = _session_agent_lock(sid)
    observed = []

    def on_response(_payload, _status):
        observed.append(lock.locked())

    return lock, observed, on_response


def _stored_draft(sid):
    """Durable (text, files) of the loaded session, normalized for assertions."""
    from api.models import Session

    loaded = Session.load(sid)
    draft = dict(getattr(loaded, "composer_draft", {}) or {})
    text = draft.get("text")
    return ("" if text is None else str(text)), list(draft.get("files") or [])


# ── helper-level tests ──────────────────────────────────────────────────────


def test_helper_removes_existing_sidecar(monkeypatch, tmp_path):
    import api.models as models

    session_dir = _install_isolated_session_env(monkeypatch, tmp_path)
    sidecar = _seed_sidecar(session_dir, "helper_existing", {"text": "hello", "files": []})
    assert sidecar.exists()

    models.delete_composer_draft_sidecar("helper_existing")

    assert not sidecar.exists()


def test_helper_absent_sidecar_is_noop(monkeypatch, tmp_path):
    """Deleting an absent sidecar must succeed silently (idempotent no-op)."""
    import api.models as models

    _install_isolated_session_env(monkeypatch, tmp_path)

    models.delete_composer_draft_sidecar("helper_absent")  # must not raise


def test_helper_propagates_unlink_oserror(monkeypatch, tmp_path):
    """A forced unlink failure must propagate as OSError, not be swallowed."""
    import api.models as models

    session_dir = _install_isolated_session_env(monkeypatch, tmp_path)
    _seed_sidecar(session_dir, "helper_oserror", {"text": "recoverable", "files": []})
    _fail_sidecar_unlink(monkeypatch, "helper_oserror")

    with pytest.raises(OSError):
        models.delete_composer_draft_sidecar("helper_oserror")

    # the sidecar must remain on disk (recoverable)
    assert (session_dir / "_drafts" / "helper_oserror.json").exists()


def test_helper_rejects_unsafe_session_id(monkeypatch, tmp_path):
    """Traversal-style session ids must be ignored, never acted on."""
    import api.models as models

    session_dir = _install_isolated_session_env(monkeypatch, tmp_path)
    outside = tmp_path / "escape.json"
    # "..%2F..%2Fescape" fails is_safe_session_id → early return, nothing created
    models.delete_composer_draft_sidecar("..%2F..%2Fescape")
    assert not outside.exists()
    assert not (session_dir / "_drafts").exists()


# ── route-level tests ───────────────────────────────────────────────────────


def test_route_clear_fails_closed_when_sidecar_unlink_fails(monkeypatch, tmp_path):
    """Clear must return a server error (never {"ok": true}) on unlink failure
    and leave the recoverable draft untouched both in the sidecar and in the
    session JSON."""
    from api.models import Session

    session_dir = _install_isolated_session_env(monkeypatch, tmp_path)
    sid = "issue6242_fail_closed"
    draft = {"text": "recoverable draft", "files": []}
    _seed_session(tmp_path, sid, draft)
    sidecar = _seed_sidecar(session_dir, sid, draft)
    _fail_sidecar_unlink(monkeypatch, sid)

    captured = _post_draft(monkeypatch, sid, text="")

    # fail closed: server error, never a false success
    assert captured["status"] == 500
    assert "ok" not in captured["payload"]
    assert "error" in captured["payload"]

    # the authoritative sidecar remains on disk and recoverable
    assert sidecar.exists()
    assert json.loads(sidecar.read_text(encoding="utf-8")) == draft

    # the session draft is still intact — the client can retain/recover it
    loaded = Session.load(sid)
    assert (loaded.composer_draft or {}).get("text") == "recoverable draft"


def test_route_clear_success_removes_sidecar_and_persists_empty(monkeypatch, tmp_path):
    """A successful clear removes the sidecar and persists the emptied draft."""
    from api.models import Session

    session_dir = _install_isolated_session_env(monkeypatch, tmp_path)
    sid = "issue6242_clear_ok"
    draft = {"text": "clear me", "files": []}
    _seed_session(tmp_path, sid, draft)
    sidecar = _seed_sidecar(session_dir, sid, draft)

    captured = _post_draft(monkeypatch, sid, text="")

    assert captured["status"] == 200
    assert captured["payload"]["ok"] is True
    assert not sidecar.exists()
    loaded = Session.load(sid)
    assert (loaded.composer_draft or {}).get("text") == ""


def test_route_clear_absent_sidecar_is_idempotent_success(monkeypatch, tmp_path):
    """Clearing when no sidecar exists is still a success (idempotent no-op)."""
    from api.models import Session

    _install_isolated_session_env(monkeypatch, tmp_path)
    sid = "issue6242_absent_sidecar"
    _seed_session(tmp_path, sid, {"text": "old", "files": []})
    # no sidecar seeded

    captured = _post_draft(monkeypatch, sid, text="")

    assert captured["status"] == 200
    assert captured["payload"]["ok"] is True
    loaded = Session.load(sid)
    assert (loaded.composer_draft or {}).get("text") == ""


def test_route_clear_retry_after_failed_unlink_converges(monkeypatch, tmp_path):
    """A retry after a failed unlink must still remove the leftover sidecar and
    report success — retry/idempotency stays defined after a failure."""
    from api.models import Session

    session_dir = _install_isolated_session_env(monkeypatch, tmp_path)
    sid = "issue6242_retry"
    draft = {"text": "recoverable draft", "files": []}
    _seed_session(tmp_path, sid, draft)
    sidecar = _seed_sidecar(session_dir, sid, draft)
    state = _fail_sidecar_unlink(monkeypatch, sid)

    # first attempt: unlink fails → 500, sidecar still recoverable
    captured_fail = _post_draft(monkeypatch, sid, text="")
    assert captured_fail["status"] == 500
    assert sidecar.exists()

    # retry with unlink healthy → sidecar removed, ok:true
    state["fail"] = False
    captured_ok = _post_draft(monkeypatch, sid, text="")
    assert captured_ok["status"] == 200
    assert captured_ok["payload"]["ok"] is True
    assert not sidecar.exists()
    loaded = Session.load(sid)
    assert (loaded.composer_draft or {}).get("text") == ""


# ── merged-draft clear classification (real handle_post() request shapes) ───


def test_route_clear_explicit_text_empty_files_empty(monkeypatch, tmp_path):
    """The explicit frontend clear shape (``text: ''`` + ``files: []``) removes
    the sidecar and persists the emptied draft."""
    session_dir = _install_isolated_session_env(monkeypatch, tmp_path)
    sid = "issue6242_explicit_shape"
    draft = {"text": "clear me", "files": ["a.txt"]}
    _seed_session(tmp_path, sid, draft)
    sidecar = _seed_sidecar(session_dir, sid, draft)
    lock, observed, probe = _lock_state_at_response(sid)

    captured = _post_draft(monkeypatch, sid, text="", files=[], on_response=probe)

    assert captured["status"] == 200
    assert captured["payload"]["ok"] is True
    assert captured["payload"]["draft"] == {"text": "", "files": []}
    assert not sidecar.exists()
    assert _stored_draft(sid) == ("", [])
    assert observed == [False]


def test_route_clear_files_only_request_cleans_stale_sidecar(monkeypatch, tmp_path):
    """``files: []`` with ``text`` omitted is still a clear when the MERGED draft
    is empty: a stale sidecar may not survive it."""
    session_dir = _install_isolated_session_env(monkeypatch, tmp_path)
    sid = "issue6242_files_only"
    empty = {"text": "", "files": []}
    _seed_session(tmp_path, sid, empty)
    sidecar = _seed_sidecar(session_dir, sid, {"text": "stale", "files": ["a.txt"]})

    captured = _post_draft(monkeypatch, sid, text=None, files=[])

    assert captured["status"] == 200
    assert captured["payload"]["ok"] is True
    assert captured["payload"].get("unchanged") is True
    assert not sidecar.exists()
    assert _stored_draft(sid) == ("", [])


@pytest.mark.parametrize(
    "stored",
    [{}, {"text": "", "files": []}],
    ids=["legacy_no_draft_keys", "legacy_empty_fields"],
)
def test_route_clear_both_omitted_cleans_stale_sidecar(monkeypatch, tmp_path, stored):
    """Both optional fields omitted on an already-empty legacy draft must still
    remove a stale sidecar (idempotent cleanup, no false error)."""
    session_dir = _install_isolated_session_env(monkeypatch, tmp_path)
    sid = "issue6242_both_omitted"
    _seed_session(tmp_path, sid, dict(stored))
    sidecar = _seed_sidecar(session_dir, sid, {"text": "stale", "files": []})

    captured = _post_draft(monkeypatch, sid, text=None, files=None)

    assert captured["status"] == 200
    assert captured["payload"]["ok"] is True
    assert captured["payload"].get("unchanged") is True
    assert not sidecar.exists()
    assert _stored_draft(sid) == ("", [])


def test_route_clear_text_only_keeps_sidecar_while_attachments_remain(
    monkeypatch, tmp_path
):
    """``text: ''`` with ``files`` omitted is NOT a clear when stored attachments
    remain: the merged draft is still non-empty, so the sidecar must survive."""
    session_dir = _install_isolated_session_env(monkeypatch, tmp_path)
    sid = "issue6242_text_only_attachments"
    draft = {"text": "hi", "files": ["a.txt"]}
    _seed_session(tmp_path, sid, draft)
    sidecar = _seed_sidecar(session_dir, sid, draft)
    lock, observed, probe = _lock_state_at_response(sid)

    captured = _post_draft(monkeypatch, sid, text="", on_response=probe)

    assert captured["status"] == 200
    assert captured["payload"]["ok"] is True
    assert captured["payload"]["draft"] == {"text": "", "files": ["a.txt"]}
    assert sidecar.exists()
    assert json.loads(sidecar.read_text(encoding="utf-8")) == draft
    assert _stored_draft(sid) == ("", ["a.txt"])
    assert observed == [False]


def test_route_clear_files_only_keeps_sidecar_while_text_remains(monkeypatch, tmp_path):
    """Mirror case: ``files: []`` with ``text`` omitted is NOT a clear while the
    stored text remains, so the sidecar must survive."""
    session_dir = _install_isolated_session_env(monkeypatch, tmp_path)
    sid = "issue6242_files_only_text"
    draft = {"text": "hi", "files": []}
    _seed_session(tmp_path, sid, draft)
    sidecar = _seed_sidecar(session_dir, sid, draft)

    captured = _post_draft(monkeypatch, sid, text=None, files=[])

    assert captured["status"] == 200
    assert captured["payload"]["ok"] is True
    assert captured["payload"].get("unchanged") is True
    assert sidecar.exists()
    assert json.loads(sidecar.read_text(encoding="utf-8")) == draft
    assert _stored_draft(sid) == ("hi", [])


# ── fail-closed for every logically-empty request form ─────────────────────


@pytest.mark.parametrize(
    "text, files",
    [
        pytest.param("", [], id="text_empty_files_empty"),
        pytest.param(None, [], id="text_omitted_files_empty"),
        pytest.param(None, None, id="both_omitted"),
    ],
)
def test_route_clear_unlink_failure_fails_closed_for_empty_form(
    monkeypatch, tmp_path, text, files
):
    """Every request shape whose MERGED draft is empty must fail closed when the
    unlink fails: 500 (never ``ok: true``), sidecar kept, stored session state
    untouched, and the reply written only after the per-session lock is free."""
    from api.models import Session

    session_dir = _install_isolated_session_env(monkeypatch, tmp_path)
    sid = "issue6242_fail_closed_form"
    empty = {"text": "", "files": []}
    _seed_session(tmp_path, sid, empty)
    session_file = session_dir / f"{sid}.json"
    seeded_session_bytes = session_file.read_bytes()
    stale = {"text": "stale recoverable", "files": ["a.txt"]}
    sidecar = _seed_sidecar(session_dir, sid, stale)
    _fail_sidecar_unlink(monkeypatch, sid)
    lock, observed, probe = _lock_state_at_response(sid)

    captured = _post_draft(monkeypatch, sid, text=text, files=files, on_response=probe)

    # fail closed: server error, never a false success
    assert captured["status"] == 500
    assert "ok" not in captured["payload"]
    assert captured["payload"]["error"]

    # authoritative sidecar still on disk, byte-identical
    assert sidecar.exists()
    assert json.loads(sidecar.read_text(encoding="utf-8")) == stale

    # stored session state untouched (no partial write of the clear)
    assert session_file.read_bytes() == seeded_session_bytes
    assert _stored_draft(sid) == ("", [])
    assert Session.load(sid).composer_draft.get("files") == []

    # and the 500 was not serialized while holding the per-session lock
    assert observed == [False]
    assert not lock.locked()


def test_route_response_sink_observes_session_lock_released(monkeypatch, tmp_path):
    """The response sink must never run while the per-session agent lock is held:
    both the clear success and the fail-closed 500 are written after release."""
    session_dir = _install_isolated_session_env(monkeypatch, tmp_path)

    # ── success path ───────────────────────────────────────────────────────
    sid_ok = "issue6242_lock_release_ok"
    draft_ok = {"text": "clear me", "files": []}
    _seed_session(tmp_path, sid_ok, draft_ok)
    _seed_sidecar(session_dir, sid_ok, draft_ok)
    lock_ok, observed_ok, probe_ok = _lock_state_at_response(sid_ok)

    captured_ok = _post_draft(
        monkeypatch, sid_ok, text="", files=[], on_response=probe_ok
    )

    assert captured_ok["status"] == 200
    assert captured_ok["payload"]["ok"] is True
    assert observed_ok == [False]
    assert not lock_ok.locked()

    # ── fail-closed path ───────────────────────────────────────────────────
    sid_fail = "issue6242_lock_release_500"
    draft_fail = {"text": "recoverable draft", "files": []}
    _seed_session(tmp_path, sid_fail, draft_fail)
    _seed_sidecar(session_dir, sid_fail, draft_fail)
    _fail_sidecar_unlink(monkeypatch, sid_fail)
    lock_fail, observed_fail, probe_fail = _lock_state_at_response(sid_fail)

    captured_fail = _post_draft(
        monkeypatch, sid_fail, text="", files=[], on_response=probe_fail
    )

    assert captured_fail["status"] == 500
    assert "ok" not in captured_fail["payload"]
    assert captured_fail["payload"]["error"]
    assert observed_fail == [False]
    assert not lock_fail.locked()
