"""#7826 re-gate CORE: archiving an old read-only Gateway session must not
make it writable.

The owner-only lookup (``_lookup_cli_session_owner_readonly``) opens each
profile's state.db read-only and never loads a sidecar. Its return dict
originally omitted ``read_only``, so the archive fallback's guard never
fired: a Gateway row older than the CLI window detail-loads with
``read_only: true``, but after a cache eviction the fallback matched it,
materialized a sidecar with ``read_only: false`` and null source identity,
and returned 200 where master returns 404 without creating a file.

The fix tracks owner-fallback provenance: the fallback exists ONLY to turn
a foreign owner into a 409, so a same-profile match through it returns 404
(after the foreign-owner 409 branch, before anything is materialised) — the
row was also invisible to the active-profile CLI lookup, which is exactly
why the fallback ran, so master's behaviour for it is a bare 404.

These tests drive the REAL ``_lookup_cli_session_owner_readonly`` against a
REAL state.db row (``source='gateway'``) and the REAL ``handle_post``
dispatcher. No source-text assertions and no metadata mocks for the lookup:
the row is a genuine read-only Gateway row.
"""
from __future__ import annotations

import io
import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.test_issue7549_archive_cross_profile import _FakePostHandler

ROUTES_PY = Path(__file__).resolve().parents[1] / "api" / "routes.py"


def _post_archive(routes_module, body: dict):
    handler = _FakePostHandler(body, path="/api/session/archive")
    parsed = SimpleNamespace(path="/api/session/archive", query="")
    routes_module.handle_post(handler, parsed)
    payload = None
    try:
        payload = json.loads(handler.wfile.getvalue().decode("utf-8"))
    except (ValueError, AttributeError):
        payload = None
    return handler.status, payload


def _make_gateway_state_db(path: Path, sid: str, *, source: str = "gateway",
                           message_count: int = 2) -> None:
    """Write a REAL agent-style state.db with one foreign-source row.

    The schema mirrors what ``_lookup_cli_session_owner_readonly`` selects
    (sessions.id/source/started_at/ended_at/message_count) and what the
    messages reader needs (role/content/timestamp). The row carries
    messages so a regression that reaches the materialization path would
    actually import a writable sidecar (200) instead of dying on an empty
    transcript.
    """
    conn = sqlite3.connect(str(path))
    try:
        conn.executescript(
            """
            CREATE TABLE sessions (
                id TEXT PRIMARY KEY,
                source TEXT,
                started_at REAL,
                ended_at REAL,
                message_count INTEGER
            );
            CREATE TABLE messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT,
                role TEXT,
                content TEXT,
                timestamp REAL
            );
            """
        )
        conn.execute(
            "INSERT INTO sessions (id, source, started_at, ended_at, message_count)"
            " VALUES (?, ?, ?, ?, ?)",
            (sid, source, 1000.0, 2000.0, message_count),
        )
        conn.executemany(
            "INSERT INTO messages (session_id, role, content, timestamp)"
            " VALUES (?, ?, ?, ?)",
            [
                (sid, "user", "hello", 1000.0),
                (sid, "assistant", "hi there", 1500.0),
            ],
        )
        conn.commit()
    finally:
        conn.close()


@pytest.fixture
def routes_module():
    return pytest.importorskip("api.routes")


@pytest.fixture
def gateway_env(routes_module, monkeypatch, tmp_path):
    """Isolate the archive handler's external reads for the fallback path.

    ``get_session`` raises KeyError (no sidecar in the active profile's
    store) and the active profile is pinned to ``default``. The REAL
    ``_lookup_cli_session_owner_readonly`` runs against a real state.db the
    test writes into ``tmp_path``; the test points the profile context
    enumeration at that directory.
    """
    import api.models as models

    home = tmp_path / "hermes-home"
    home.mkdir()
    monkeypatch.setattr(routes_module, "get_session",
                        lambda _sid, *a, **k: (_ for _ in ()).throw(KeyError(_sid)))
    monkeypatch.setattr(routes_module, "_get_active_profile_name", lambda: "default")
    monkeypatch.setattr(routes_module, "_session_is_subagent_view_only", lambda _sid: False)
    monkeypatch.setattr(routes_module, "_is_subagent_child_session_id", lambda _sid: False)
    monkeypatch.setattr(routes_module, "_is_messaging_session_record", lambda _m: False)
    # Point the owner lookup's profile-context enumeration at the test home.
    monkeypatch.setattr(
        models,
        "_all_profiles_cli_contexts",
        lambda: ([(home, home / "state.db", "default")], ()),
    )
    # The lookup internally resolves the active profile through api.profiles;
    # pin it to 'default' so the enumeration above stays authoritative.
    try:
        import api.profiles as profiles

        monkeypatch.setattr(profiles, "get_active_profile_name", lambda: "default")
        monkeypatch.setattr(profiles, "get_hermes_home_for_profile",
                            lambda _p=None: str(home))
    except Exception:
        pass
    routes_module._gateway_home = home
    return routes_module


# ── 1. CORE: the real lookup must expose read_only for a real Gateway row ──


def test_owner_lookup_returns_read_only_for_a_real_gateway_row(
    routes_module, gateway_env, monkeypatch
):
    """The owner lookup's dict carries ``read_only`` for a real read-only
    Gateway row (state.db has no read_only column; the flag is derived from
    the source family). Pre-fix the dict omitted it, so the archive
    fallback's guard never fired."""
    sid = "20261017_gateway_ro"
    _make_gateway_state_db(gateway_env._gateway_home / "state.db", sid)

    meta = routes_module._lookup_cli_session_owner_readonly(sid)

    assert meta, "the real lookup must find the real Gateway row"
    assert meta["profile"] == "default"
    assert meta["source"] == "gateway"
    assert meta["read_only"] is True, (
        "a Gateway row is a read-only view of an external store — the lookup "
        f"must say so. Got: {meta!r}"
    )


def test_owner_lookup_marks_a_writable_cli_row_not_read_only(
    routes_module, gateway_env, monkeypatch
):
    """Counterpart: a genuine CLI row stays writable (read_only False) so the
    derivation cannot blanket-refuse every fallback match."""
    sid = "20261017_cli_writable"
    _make_gateway_state_db(gateway_env._gateway_home / "state.db", sid,
                           source="cli")

    meta = routes_module._lookup_cli_session_owner_readonly(sid)

    assert meta and meta["read_only"] is False, (
        f"a cli row is claimable/writable: {meta!r}"
    )


# ── 2. CORE: the archive endpoint must 404 a same-profile fallback match ──


def test_archive_of_readonly_gateway_row_returns_404_without_sidecar(
    routes_module, gateway_env, monkeypatch, tmp_path
):
    """The exact re-gate CORE repro: an old read-only Gateway row matched by
    the owner-only fallback must return 404 (master's behaviour) and must
    NOT create a writable sidecar. Pre-fix the fallback materialized a
    sidecar with read_only False and null source identity and returned 200.

    The REAL owner lookup runs against the REAL state.db row; only the
    active-profile CLI lookup is stubbed to {} — that IS the out-of-window
    condition (a Gateway row older than the CLI window), which is what makes
    the fallback run in production.
    """
    import api.models as models

    sid = "20261017_gateway_ro"
    _make_gateway_state_db(gateway_env._gateway_home / "state.db", sid)
    monkeypatch.setattr(routes_module, "_lookup_cli_session_metadata",
                        lambda _sid, *, all_profiles=False: {})
    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    monkeypatch.setattr(models, "SESSION_DIR", session_dir)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", session_dir / "_index.json")
    published = []
    monkeypatch.setattr(routes_module, "publish_session_list_changed",
                        lambda *a, **k: published.append(a))

    status, payload = _post_archive(routes_module, {
        "session_id": sid, "archived": True})

    assert status == 404, (
        f"a same-profile read-only Gateway fallback match must 404, got "
        f"{status}: {payload}"
    )
    assert not (session_dir / f"{sid}.json").exists(), (
        "the fallback must not materialize a writable sidecar for a read-only "
        "Gateway row"
    )
    assert published == [], f"a 404 must not publish: {published}"


def test_foreign_owner_gateway_row_still_409s_through_real_lookup(
    routes_module, gateway_env, monkeypatch
):
    """The foreign-owner 409 branch must survive the provenance change: a
    Gateway row living under a KNOWN other profile (the fallback's whole
    reason to exist) still emits the structured 409, before anything is
    materialised."""
    import api.models as models

    sid = "20261017_gateway_work"
    work_home = gateway_env._gateway_home / "work-profile"
    work_home.mkdir()
    _make_gateway_state_db(work_home / "state.db", sid)
    monkeypatch.setattr(
        models, "_all_profiles_cli_contexts",
        lambda: ([(work_home, work_home / "state.db", "work")], ()),
    )
    monkeypatch.setattr(routes_module, "_lookup_cli_session_metadata",
                        lambda _sid, *, all_profiles=False: {})
    published = []
    monkeypatch.setattr(routes_module, "publish_session_list_changed",
                        lambda *a, **k: published.append(a))

    status, payload = _post_archive(routes_module, {
        "session_id": sid, "archived": True})

    assert status == 409, f"a foreign owner must 409, got {status}: {payload}"
    assert payload["code"] == "session_profile_mismatch"
    assert payload["profile"] == "work"
    assert payload["session_id"] == sid
    assert published == [], f"a 409 must not publish: {published}"


# ── 3. Counterpart: the writable same-profile CLI archive still works ──


def test_writable_same_profile_cli_row_still_archives_via_normal_lookup(
    routes_module, gateway_env, monkeypatch
):
    """The provenance guard must not break the legitimate path: a writable
    same-profile CLI row that the ACTIVE-PROFILE lookup finds (inside the
    CLI window — so the fallback never runs) still materializes and
    archives, exactly as master behaves."""
    cli_meta = {
        "session_id": "20261017_cli_writable",
        "title": "CLI session",
        "profile": "default",
        "model": "MiniMax-M3",
        "source_tag": "cli",
        "raw_source": "cli",
        "session_source": "cli",
        "source_label": "CLI",
        "read_only": False,
    }
    monkeypatch.setattr(routes_module, "_lookup_cli_session_metadata",
                        lambda _sid, *, all_profiles=False: cli_meta)
    messages = [{"role": "user", "content": "hi"},
                {"role": "assistant", "content": "hello"}]
    monkeypatch.setattr(routes_module, "get_cli_session_messages",
                        lambda _sid: messages)
    fake_session = SimpleNamespace(
        session_id="20261017_cli_writable", archived=False,
        profile="default", messages=[], title="CLI session",
        compact=lambda: {"session_id": "20261017_cli_writable",
                         "archived": True, "profile": "default"},
        save=lambda **kw: saved.append(kw))
    saved = []

    def fake_import(*args, **kwargs):
        fake_session.is_cli_session = True
        with routes_module.LOCK:
            routes_module.SESSIONS[fake_session.session_id] = fake_session
            routes_module.SESSIONS.move_to_end(fake_session.session_id)
        return fake_session

    monkeypatch.setattr(routes_module, "import_cli_session", fake_import)
    monkeypatch.setattr(routes_module, "is_cli_session_row", lambda _m: True)
    monkeypatch.setattr(routes_module, "publish_session_list_changed",
                        lambda *a, **k: None)

    status, payload = _post_archive(routes_module, {
        "session_id": "20261017_cli_writable", "archived": True})

    assert status == 200, f"a writable same-profile CLI archive must succeed: {payload}"
    assert fake_session.archived is True
    assert saved and saved[-1].get("touch_updated_at") is False
