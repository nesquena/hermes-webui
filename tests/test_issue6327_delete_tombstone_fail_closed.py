"""#6327 — ``/api/session/delete`` must not report success when the durable
delete tombstone could not be persisted.

Review-round context (maintainer, 2026-08-12 blocker 2, second half): "The
deletion backstop is also not fail-closed. ... ``_save_webui_deleted_session_
tombstone()`` swallows write errors and ``/api/session/delete`` still reports
success after tombstone failure."

The same handler is fail-closed for every neighbouring revocation — a failed
share revoke answers ``share_revoke_failed`` (500), a failed state.db delete
answers ``state_db_cleanup_failed`` (500) and a failed draft delete answers
500 — but the durable "this WebUI session is gone" marker is the one write
that is allowed to fail silently, and the response still carries ``ok: True``.

The tombstone is what makes a deleted WebUI session 404 (``was_webui``) instead
of being materialised again as a recoverable foreign session, and what keeps it
from reappearing as an "Agent" ghost in the sidebar (#5498).  Reporting success
over a lost tombstone therefore claims a deletion that a later read can undo.

Contract asserted here:

1. A tombstone persistence failure makes the delete answer **500** with an
   error payload, never ``ok: True`` (the defect).
2. The happy path is unchanged: 200, ``ok: True``, the id is in the tombstone.
3. A messaging session does not need a tombstone, so a tombstone write failure
   must not fail its delete (control against over-refusal).
4. The failure is recoverable, not terminal: a retry that can persist the
   tombstone answers 200 and records the id (control against a permanent
   refusal that would strand the session un-tombstoned).
5. The model contract is the one that has to change: a direct
   ``_record_webui_deleted_session_tombstone`` raises on write failure instead
   of swallowing it, and leaves no temporary file behind (the cleanup path
   stays best-effort and is guarded by its own callers).

These are unit tests over the real ``routes.handle_post`` delete branch and the
real ``api.models`` tombstone helpers, with an isolated SESSION_DIR; no HTTP, no
browser, no live Agent.
"""
import json
import os
import shutil
import tempfile
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from api import models

# What the process inherited, so teardown can be checked to restore it exactly.
_ATTACHMENT_ROOT_AT_IMPORT = os.environ.get("HERMES_WEBUI_ATTACHMENT_DIR")


@pytest.fixture
def isolated_session_env():
    """Redirect the SESSIONS cache, the session dir and the tombstone file.

    ``api.models`` and ``api.routes`` bind ``SESSION_DIR`` /
    ``SESSION_INDEX_FILE`` at module load, so both module copies must be
    redirected.  The tombstone file lives under ``SESSION_DIR``
    (``_deleted_webui_sessions.json``), so this also isolates tombstone
    reads/writes.  The delete branch also removes the session's attachment
    inbox, whose root comes from ``HERMES_WEBUI_ATTACHMENT_DIR`` independently
    of ``SESSION_DIR``, so that root is redirected into the same temporary
    dir before any delete can run.  Everything is restored on teardown.
    """
    import collections

    import api.routes as routes
    from api import config as _cfg

    tmpdir = tempfile.mkdtemp()
    sessions_dir = Path(tmpdir) / "sessions"
    sessions_dir.mkdir(parents=True, exist_ok=True)
    attachments_dir = Path(tmpdir) / "attachments"
    attachments_dir.mkdir(parents=True, exist_ok=True)
    old_attachment_root = os.environ.get("HERMES_WEBUI_ATTACHMENT_DIR")

    old = {
        "cfg_SESSION_DIR": _cfg.SESSION_DIR,
        "models_SESSION_DIR": getattr(models, "SESSION_DIR", None),
        "routes_SESSION_DIR": getattr(routes, "SESSION_DIR", None),
        "cfg_SESSION_INDEX_FILE": _cfg.SESSION_INDEX_FILE,
        "models_SESSION_INDEX_FILE": getattr(models, "SESSION_INDEX_FILE", None),
        "routes_SESSION_INDEX_FILE": getattr(routes, "SESSION_INDEX_FILE", None),
        "cfg_SESSIONS": _cfg.SESSIONS,
        "models_SESSIONS": getattr(models, "SESSIONS", None),
        "routes_SESSIONS": getattr(routes, "SESSIONS", None),
        "cfg_LOCK": _cfg.LOCK,
        "models_LOCK": getattr(models, "LOCK", None),
        "routes_LOCK": getattr(routes, "LOCK", None),
    }

    index_file = sessions_dir / "_index.json"
    _cfg.SESSION_DIR = sessions_dir
    models.SESSION_DIR = sessions_dir
    routes.SESSION_DIR = sessions_dir
    _cfg.SESSION_INDEX_FILE = index_file
    models.SESSION_INDEX_FILE = index_file
    routes.SESSION_INDEX_FILE = index_file
    _cfg.LOCK = threading.Lock()
    models.LOCK = _cfg.LOCK
    routes.LOCK = _cfg.LOCK
    _cfg.SESSIONS = collections.OrderedDict()
    models.SESSIONS = _cfg.SESSIONS
    routes.SESSIONS = _cfg.SESSIONS
    os.environ["HERMES_WEBUI_ATTACHMENT_DIR"] = str(attachments_dir)

    try:
        yield sessions_dir
    finally:
        if old_attachment_root is None:
            os.environ.pop("HERMES_WEBUI_ATTACHMENT_DIR", None)
        else:
            os.environ["HERMES_WEBUI_ATTACHMENT_DIR"] = old_attachment_root
        _cfg.SESSION_DIR = old["cfg_SESSION_DIR"]
        if old["models_SESSION_DIR"] is not None:
            models.SESSION_DIR = old["models_SESSION_DIR"]
        if old["routes_SESSION_DIR"] is not None:
            routes.SESSION_DIR = old["routes_SESSION_DIR"]
        _cfg.SESSION_INDEX_FILE = old["cfg_SESSION_INDEX_FILE"]
        if old["models_SESSION_INDEX_FILE"] is not None:
            models.SESSION_INDEX_FILE = old["models_SESSION_INDEX_FILE"]
        if old["routes_SESSION_INDEX_FILE"] is not None:
            routes.SESSION_INDEX_FILE = old["routes_SESSION_INDEX_FILE"]
        _cfg.SESSIONS = old["cfg_SESSIONS"]
        if old["models_SESSIONS"] is not None:
            models.SESSIONS = old["models_SESSIONS"]
        if old["routes_SESSIONS"] is not None:
            routes.SESSIONS = old["routes_SESSIONS"]
        _cfg.LOCK = old["cfg_LOCK"]
        if old["models_LOCK"] is not None:
            models.LOCK = old["models_LOCK"]
        if old["routes_LOCK"] is not None:
            routes.LOCK = old["routes_LOCK"]
        shutil.rmtree(tmpdir, ignore_errors=True)


class _FakeSession:
    """Minimal Session stand-in carrying the attrs the delete path reads."""

    def __init__(self, session_id, *, messages=None):
        self.session_id = session_id
        self.title = "Delete tombstone fail-closed"
        self.workspace = "/workspace"
        self.model = "test-model"
        self.model_provider = None
        self.profile = None
        self.personality = None
        self.messages = list(messages or [])
        self.context_messages = []
        self.tool_calls = []
        self.input_tokens = 0
        self.output_tokens = 0
        self.estimated_cost = None
        self.context_length = 0
        self.threshold_tokens = 0
        self.last_prompt_tokens = 0
        self.active_stream_id = None
        self.pending_user_message = None
        self.pending_attachments = []
        self.pending_started_at = None
        self.llm_title_generated = True
        self.composer_draft = None
        self._loaded_metadata_only = False
        self.created_at = time.time()

    def save(self, *args, **kwargs):  # sidecar persistence is not under test here
        return True

    def compact(self):
        return {"session_id": self.session_id, "title": self.title}


def _install_failing_tombstone_write(monkeypatch):
    """Make the durable tombstone write behave like a full disk.

    Returns a one-key dict so a test can flip the failure off and prove the
    retry is recoverable.
    """
    real_save = models._save_webui_deleted_session_tombstone
    state = {"fail": True}

    def _save(ids):
        if state["fail"]:
            raise OSError("tombstone disk full")
        return real_save(ids)

    monkeypatch.setattr(models, "_save_webui_deleted_session_tombstone", _save)
    return state


def _drive_delete(monkeypatch, sid, *, messaging=False):
    """Drive the real ``/api/session/delete`` branch and capture the response."""
    import api.routes as routes

    session = _FakeSession(sid)
    with models.LOCK:
        models.SESSIONS[sid] = session

    captured = {}

    def _j(handler, payload, status=200, extra_headers=None):
        captured.update(payload=payload, status=status)
        return True

    def _bad(handler, msg, status=400):
        captured.update(payload={"error": msg}, status=status)
        return True

    monkeypatch.setattr(routes, "_check_csrf", lambda handler: True)
    monkeypatch.setattr(routes, "read_body", lambda handler: {"session_id": sid})
    monkeypatch.setattr(routes, "j", _j)
    monkeypatch.setattr(routes, "bad", _bad)
    monkeypatch.setattr(routes, "_lookup_cli_session_metadata", lambda sid: {})
    monkeypatch.setattr(routes, "_is_messaging_session_id", lambda sid: messaging)
    monkeypatch.setattr(
        routes, "_worktree_retained_payload_for_session_id", lambda sid: {}
    )
    monkeypatch.setattr(routes, "_session_is_subagent_view_only", lambda sid: False)
    monkeypatch.setattr(models, "delete_cli_session", lambda sid: True)

    assert routes.handle_post(object(), SimpleNamespace(path="/api/session/delete")) is True
    return captured


def test_delete_reports_failure_when_the_tombstone_cannot_be_persisted(
    isolated_session_env, monkeypatch
):
    """Defect: a lost durable deletion marker must not answer ``ok: True``."""
    sid = "sess-delete-tombstone-lost"
    _install_failing_tombstone_write(monkeypatch)

    captured = _drive_delete(monkeypatch, sid)

    assert captured["status"] == 500, captured
    assert captured["payload"].get("ok") is not True, captured
    assert captured["payload"].get("error"), captured
    # No phantom durability: the id must not be recorded as deleted.
    assert sid not in models._load_webui_deleted_session_tombstone()


def test_delete_succeeds_and_records_the_tombstone_when_persistence_works(
    isolated_session_env, monkeypatch
):
    """Control: the happy path is unchanged."""
    sid = "sess-delete-tombstone-ok"

    captured = _drive_delete(monkeypatch, sid)

    assert captured["status"] == 200, captured
    assert captured["payload"].get("ok") is True, captured
    assert sid in models._load_webui_deleted_session_tombstone()


@pytest.fixture
def inherited_attachment_root(monkeypatch):
    """An attachment inbox configured before the session fixture is entered.

    Stands in for a ``HERMES_WEBUI_ATTACHMENT_DIR`` the test process inherits;
    the delete tests must never clean anything under it.
    """
    parent = Path(tempfile.mkdtemp())
    root = parent / "inherited-attachments"
    root.mkdir()
    monkeypatch.setenv("HERMES_WEBUI_ATTACHMENT_DIR", str(root))
    try:
        yield root
    finally:
        shutil.rmtree(parent, ignore_errors=True)


def test_delete_leaves_an_inherited_attachment_inbox_alone(
    inherited_attachment_root, isolated_session_env, monkeypatch
):
    """The fixture owns attachment cleanup: an inherited inbox is untouched."""
    from api.upload import _attachment_root

    sid = "sess-delete-tombstone-ok"
    inherited = inherited_attachment_root / sid / "keep.txt"
    inherited.parent.mkdir(parents=True)
    inherited.write_text("inherited attachment", encoding="utf-8")

    fixture_root = _attachment_root()
    owned = fixture_root / sid / "owned.txt"
    owned.parent.mkdir(parents=True, exist_ok=True)
    owned.write_text("fixture attachment", encoding="utf-8")

    captured = _drive_delete(monkeypatch, sid)

    assert captured["status"] == 200, captured
    assert inherited.exists(), "the delete removed an inbox outside the fixture"
    assert inherited.read_text(encoding="utf-8") == "inherited attachment"
    assert not owned.parent.exists(), "the delete did not clean the session's inbox"
    assert fixture_root.is_relative_to(isolated_session_env.parent.resolve())


def test_messaging_session_delete_does_not_require_a_tombstone(
    isolated_session_env, monkeypatch
):
    """Control: a session that is never tombstoned must not fail on that write."""
    sid = "sess-delete-messaging"
    _install_failing_tombstone_write(monkeypatch)

    captured = _drive_delete(monkeypatch, sid, messaging=True)

    assert captured["status"] == 200, captured
    assert captured["payload"].get("ok") is True, captured


def test_a_retry_after_a_failed_tombstone_write_completes_the_delete(
    isolated_session_env, monkeypatch
):
    """Control: the refusal is recoverable — the retry records the tombstone."""
    sid = "sess-delete-tombstone-retry"
    state = _install_failing_tombstone_write(monkeypatch)

    first = _drive_delete(monkeypatch, sid)
    assert first["status"] == 500, first
    assert sid not in models._load_webui_deleted_session_tombstone()

    state["fail"] = False
    second = _drive_delete(monkeypatch, sid)

    assert second["status"] == 200, second
    assert second["payload"].get("ok") is True, second
    assert sid in models._load_webui_deleted_session_tombstone()


def test_tombstone_helper_raises_and_leaves_no_temp_file_on_a_real_write_failure(
    isolated_session_env, monkeypatch
):
    """The model contract: durability failures surface instead of being swallowed.

    The failure is produced by the filesystem, not by patching the function
    under test: the tombstone path is occupied by a directory, so the atomic
    ``os.replace`` of the temp file onto it cannot succeed.  On the defective
    head this raises nothing (the write error is swallowed) and the assertion
    below fails; the id must never be reported as recorded.
    """
    sid = "sess-delete-tombstone-contract"
    sessions_dir = isolated_session_env
    tombstone_path = sessions_dir / "_deleted_webui_sessions.json"
    tombstone_path.mkdir()

    with pytest.raises(OSError):
        models._record_webui_deleted_session_tombstone(sid)

    assert sid not in models._load_webui_deleted_session_tombstone()
    residue = [p.name for p in sessions_dir.iterdir() if ".tmp." in p.name]
    assert residue == [], residue

    # With the obstruction removed the same call records the id, proving the
    # failure above was the write and not a broken code path.
    tombstone_path.rmdir()
    models._record_webui_deleted_session_tombstone(sid)
    assert tombstone_path.exists()
    payload = json.loads(tombstone_path.read_text(encoding="utf-8"))
    assert sid in payload["ids"]
    residue = [p.name for p in sessions_dir.iterdir() if ".tmp." in p.name]
    assert residue == [], residue


# --- Fixture isolation (review 2026-11-18T04:29:33Z on #9148) -----------------
#
# ``api.routes`` imports ``SESSION_DIR`` / ``SESSIONS`` / ``LOCK`` from
# ``api.config`` and ``SESSION_INDEX_FILE`` from ``api.models``, so it holds its
# own module-level copies.  The fixture has to redirect and restore those copies
# too, whichever order the suite imports the module in.

_ROUTE_ALIAS_NAMES = ("SESSION_DIR", "SESSION_INDEX_FILE", "SESSIONS", "LOCK")


def _route_aliases():
    import api.routes as routes

    return {name: getattr(routes, name) for name in _ROUTE_ALIAS_NAMES}


def test_fixture_redirects_the_route_module_aliases_on_a_cold_import(
    isolated_session_env,
):
    """Cold order: the fixture imports routes and points it at the temp dir."""
    import api.routes as routes

    sessions_dir = isolated_session_env
    aliases = _route_aliases()

    assert aliases["SESSION_DIR"] == sessions_dir, aliases["SESSION_DIR"]
    assert aliases["SESSION_INDEX_FILE"] == sessions_dir / "_index.json"
    assert aliases["SESSIONS"] is models.SESSIONS
    assert aliases["LOCK"] is models.LOCK
    assert routes.SESSION_DIR == sessions_dir


def test_fixture_redirects_the_route_module_aliases_when_already_imported(
    isolated_session_env,
):
    """Already-imported order: the stale copies must be redirected too."""
    _route_aliases()  # routes is imported by now, from the previous test
    sessions_dir = isolated_session_env
    aliases = _route_aliases()

    assert aliases["SESSION_DIR"] == sessions_dir, aliases["SESSION_DIR"]
    assert aliases["SESSION_INDEX_FILE"] == sessions_dir / "_index.json"
    assert aliases["SESSIONS"] is models.SESSIONS
    assert aliases["LOCK"] is models.LOCK


def test_delete_physically_removes_the_sidecar_and_evicts_the_cache(
    isolated_session_env, monkeypatch
):
    """The handler must act on the isolated dir: sidecar gone, cache evicted."""
    sessions_dir = isolated_session_env
    sid = "sess-delete-physical-sidecar"
    sidecar = sessions_dir / f"{sid}.json"
    sidecar.write_text(
        json.dumps({"session_id": sid, "title": "physical", "messages": []}),
        encoding="utf-8",
    )
    assert sidecar.exists()

    captured = _drive_delete(monkeypatch, sid)

    assert captured["status"] == 200, captured
    assert captured["payload"].get("ok") is True, captured
    assert not sidecar.exists(), "the delete handler wrote outside the fixture dir"
    assert sid not in models.SESSIONS
    assert sid in models._load_webui_deleted_session_tombstone()


def test_fixture_restores_every_module_alias_after_teardown():
    """Teardown must restore routes/config/models to the same shared objects."""
    from api import config as _cfg

    aliases = _route_aliases()

    assert aliases["SESSION_DIR"] is models.SESSION_DIR is _cfg.SESSION_DIR
    assert aliases["SESSION_INDEX_FILE"] is models.SESSION_INDEX_FILE is (
        _cfg.SESSION_INDEX_FILE
    )
    assert aliases["SESSIONS"] is models.SESSIONS is _cfg.SESSIONS
    assert aliases["LOCK"] is models.LOCK is _cfg.LOCK
    assert os.environ.get("HERMES_WEBUI_ATTACHMENT_DIR") == _ATTACHMENT_ROOT_AT_IMPORT
