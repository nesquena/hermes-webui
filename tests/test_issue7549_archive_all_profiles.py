"""Regression tests for #7549 — archiving a row from the all-profiles sidebar.

With "all profiles" enabled the sidebar lists sessions owned by profiles other
than the active one. The archive endpoint resolved the session id against the
process-wide ACTIVE profile only, so hiding/archiving such a row answered 404
even though the row was rendered right there in the sidebar.

The fix threads the row's own profile through the request (``profile`` +
``all_profiles``, mirroring the CLI import path) and resolves every lookup on
the archive path against it: the CLI metadata lookup, the CLI message read, the
sidecar that gets materialized, and the sidebar cache invalidation.
"""
import json
from pathlib import Path
from types import SimpleNamespace

import api.models as models
import api.routes as routes
from api.models import SESSIONS, Session

ACTIVE_PROFILE = "root-profile"
FOREIGN_PROFILE = "worker-one"

SESSIONS_JS = (
    Path(__file__).resolve().parent.parent / "static" / "sessions.js"
).read_text(encoding="utf-8")


def _capture_post(monkeypatch, body):
    captured = {}
    monkeypatch.setattr(routes, "_check_csrf", lambda handler: True)
    monkeypatch.setattr(routes, "read_body", lambda handler: body)
    monkeypatch.setattr(
        routes,
        "j",
        lambda handler, payload, status=200, extra_headers=None: captured.update(
            payload=payload,
            status=status,
        )
        or True,
    )
    monkeypatch.setattr(
        routes,
        "bad",
        lambda handler, message, status=400, extra_headers=None: captured.update(
            error=message,
            status=status,
        )
        or True,
    )
    return captured


def _isolate_session_store(tmp_path, monkeypatch):
    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    monkeypatch.setattr(models, "SESSION_DIR", session_dir)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", session_dir / "_index.json")
    monkeypatch.setattr(routes, "SESSION_DIR", session_dir)
    monkeypatch.setattr(routes, "SESSION_INDEX_FILE", session_dir / "_index.json")
    SESSIONS.clear()
    return session_dir


def _missing_session(session_id, metadata_only=False):
    """No sidecar exists in the ACTIVE profile for this row."""
    raise KeyError(session_id)


def _foreign_cli_row(session_id):
    """CLI-store metadata row as the foreign profile's store reports it."""
    return {
        "session_id": session_id,
        "title": "Foreign profile CLI session",
        "profile": FOREIGN_PROFILE,
        "source_tag": "cli",
        "raw_source": "cli",
        "model": "cli-test-model",
        "created_at": 1700000000.0,
        "updated_at": 1700000100.0,
        "message_count": 2,
    }


def _install_foreign_row(monkeypatch, session_id, lookups, reads):
    """Row is invisible to the active profile and only found with all_profiles."""

    def fake_lookup(session_id_, *, all_profiles=False):
        lookups.append((session_id_, all_profiles))
        return _foreign_cli_row(session_id) if all_profiles else {}

    monkeypatch.setattr(routes, "_lookup_cli_session_metadata", fake_lookup)

    def fake_messages(session_id_, profile=None):
        reads.append((session_id_, profile))
        return [
            {"role": "user", "content": "hello from the CLI"},
            {"role": "assistant", "content": "hi"},
        ]

    monkeypatch.setattr(routes, "get_cli_session_messages", fake_messages)
    monkeypatch.setattr(routes, "get_session", _missing_session)
    monkeypatch.setattr(routes, "_is_subagent_child_session_id", lambda sid: False)
    monkeypatch.setattr(routes, "_is_messaging_session_record", lambda meta: False)


def _archive(monkeypatch, body, *, isolated=False):
    captured = _capture_post(monkeypatch, body)
    monkeypatch.setattr(routes, "_is_isolated_profile_mode", lambda: isolated)
    published = []
    monkeypatch.setattr(
        routes,
        "publish_session_list_changed",
        lambda *args, **kwargs: published.append((args, kwargs)),
    )
    handled = routes.handle_post(
        object(), SimpleNamespace(path="/api/session/archive")
    )
    return handled, captured, published


def _sidecar(session_dir, session_id):
    path = session_dir / f"{session_id}.json"
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def test_archive_all_profiles_row_is_materialized_into_its_own_profile(
    tmp_path, monkeypatch
):
    """#7549: a foreign-profile row archives (200) and keeps its own profile."""
    session_dir = _isolate_session_store(tmp_path, monkeypatch)
    session_id = "foreign_archive_7549"
    lookups, reads = [], []
    _install_foreign_row(monkeypatch, session_id, lookups, reads)

    handled, captured, published = _archive(
        monkeypatch,
        {
            "session_id": session_id,
            "archived": True,
            "profile": FOREIGN_PROFILE,
            "all_profiles": 1,
        },
    )

    assert handled is True
    assert captured.get("status") == 200, captured
    # active-profile lookup first (miss), then the all-profiles lookup (hit)
    assert lookups == [(session_id, False), (session_id, True)]
    # the CLI message read is scoped to the row's profile, not the active one
    assert reads == [(session_id, FOREIGN_PROFILE)]

    sidecar = _sidecar(session_dir, session_id)
    assert sidecar is not None, "the row was not materialized into a sidecar"
    assert sidecar["profile"] == FOREIGN_PROFILE
    assert sidecar["archived"] is True
    assert sidecar["session_id"] == session_id
    # sidebar cache invalidation must target the foreign profile's cache entry
    assert published, "publish_session_list_changed was not called"
    assert published[0][1].get("profile") == FOREIGN_PROFILE


def test_archive_without_profile_identity_still_404s_for_foreign_row(
    tmp_path, monkeypatch
):
    """Boundary: the fix is the request identity — unqualified requests keep the
    old behaviour, so a foreign row is still reported as not found."""
    session_dir = _isolate_session_store(tmp_path, monkeypatch)
    session_id = "foreign_archive_unqualified"
    lookups, reads = [], []
    _install_foreign_row(monkeypatch, session_id, lookups, reads)

    handled, captured, published = _archive(
        monkeypatch, {"session_id": session_id, "archived": True}
    )

    assert handled is True
    assert captured.get("status") == 404, captured
    assert reads == []
    # no all-profiles escalation without an explicit profile in the request
    assert lookups == [(session_id, False)]
    assert _sidecar(session_dir, session_id) is None
    assert published == []


def test_archive_refuses_profile_that_does_not_own_the_row(tmp_path, monkeypatch):
    """A request may not name a profile the CLI store does not confirm."""
    session_dir = _isolate_session_store(tmp_path, monkeypatch)
    session_id = "foreign_archive_spoofed"
    lookups, reads = [], []
    _install_foreign_row(monkeypatch, session_id, lookups, reads)

    handled, captured, published = _archive(
        monkeypatch,
        {
            "session_id": session_id,
            "archived": True,
            "profile": "some-other-profile",
            "all_profiles": 1,
        },
    )

    assert handled is True
    assert captured.get("status") == 404, captured
    assert reads == []
    assert _sidecar(session_dir, session_id) is None
    assert published == []


def test_archive_refuses_qualified_profile_mismatch_on_stored_sidecar(
    tmp_path, monkeypatch
):
    """A stored sidecar owned by another profile is not archivable by name."""
    session_dir = _isolate_session_store(tmp_path, monkeypatch)
    session_id = "stored_foreign_sidecar"
    Session(
        session_id=session_id,
        title="Stored foreign session",
        messages=[{"role": "user", "content": "stored"}],
        profile=FOREIGN_PROFILE,
    ).save()
    SESSIONS.clear()

    handled, captured, published = _archive(
        monkeypatch,
        {
            "session_id": session_id,
            "archived": True,
            "profile": "some-other-profile",
            "all_profiles": 1,
        },
    )

    assert handled is True
    assert captured.get("status") == 404, captured
    sidecar = _sidecar(session_dir, session_id)
    assert sidecar["profile"] == FOREIGN_PROFILE
    assert not sidecar.get("archived")


def test_archive_rejects_all_profiles_without_profile(tmp_path, monkeypatch):
    session_dir = _isolate_session_store(tmp_path, monkeypatch)
    session_id = "archive_all_profiles_no_profile"
    lookups, reads = [], []
    _install_foreign_row(monkeypatch, session_id, lookups, reads)

    handled, captured, published = _archive(
        monkeypatch,
        {"session_id": session_id, "archived": True, "all_profiles": 1},
    )

    assert handled is True
    assert captured.get("status") == 400, captured
    assert lookups == []
    assert _sidecar(session_dir, session_id) is None


def test_archive_rejects_all_profiles_in_isolated_profile_mode(
    tmp_path, monkeypatch
):
    session_dir = _isolate_session_store(tmp_path, monkeypatch)
    session_id = "archive_all_profiles_isolated"
    lookups, reads = [], []
    _install_foreign_row(monkeypatch, session_id, lookups, reads)

    handled, captured, published = _archive(
        monkeypatch,
        {
            "session_id": session_id,
            "archived": True,
            "profile": FOREIGN_PROFILE,
            "all_profiles": 1,
        },
        isolated=True,
    )

    assert handled is True
    assert captured.get("status") == 403, captured
    assert lookups == []
    assert _sidecar(session_dir, session_id) is None


def test_archive_rejects_malformed_profile_value(tmp_path, monkeypatch):
    session_dir = _isolate_session_store(tmp_path, monkeypatch)
    session_id = "archive_bad_profile"
    lookups, reads = [], []
    _install_foreign_row(monkeypatch, session_id, lookups, reads)

    handled, captured, published = _archive(
        monkeypatch,
        {"session_id": session_id, "archived": True, "profile": "not a profile!"},
    )

    assert handled is True
    assert captured.get("status") == 400, captured
    assert lookups == []
    assert _sidecar(session_dir, session_id) is None


def test_sidebar_archive_requests_carry_the_row_profile():
    """Every archive POST in the sidebar carries the row's profile when the
    all-profiles view is on (the request must not lose the identity)."""
    assert "function _archivePayload(session, archived=true, sessionId=null)" in SESSIONS_JS
    assert "payload.all_profiles = true;" in SESSIONS_JS
    assert "payload.profile = session.profile;" in SESSIONS_JS
    # helper definition + the batch bar, _archiveSession() and the row action menu
    assert SESSIONS_JS.count("_archivePayload(") == 4
    # no archive call site may build the payload by hand again
    assert "JSON.stringify({session_id:session.session_id,archived" not in SESSIONS_JS
    assert "JSON.stringify({session_id:sid,archived:true})" not in SESSIONS_JS
