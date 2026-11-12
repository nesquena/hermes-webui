"""Ownership preflight must not repair a refused session's legacy sidecar."""
from __future__ import annotations

import collections
import http.client
import json
import threading

import pytest

import api.auth as auth
import api.config as config
import api.models as models
import api.profiles as profiles
import api.routes as routes


@pytest.fixture
def isolated_sessions(tmp_path, monkeypatch):
    directory = tmp_path / "sessions"
    directory.mkdir()
    cache = collections.OrderedDict()
    for module in (config, models, routes):
        monkeypatch.setattr(module, "SESSION_DIR", directory)
        monkeypatch.setattr(module, "SESSIONS", cache)
        if hasattr(module, "SESSION_INDEX_FILE"):
            monkeypatch.setattr(module, "SESSION_INDEX_FILE", directory / "_index.json")
    monkeypatch.setattr(routes, "_lookup_cli_session_metadata", lambda *_a, **_k: {})
    return directory, cache


def _legacy_sidecar(directory, *, profile="work", layout="messages_first"):
    partial = {"role": "assistant", "content": "interrupted", "_partial": True}
    messages = [{"role": "user", "content": "hello"}, partial, dict(partial)]
    metadata = {
        "session_id": "legacy-8094", "title": "Legacy", "profile": profile,
        "created_at": 1.0, "updated_at": 2.0,
    }
    if layout == "messages_first":
        data = {"messages": messages, **metadata}
    else:
        # Pre-modern layout: scenes precede the count and there is no scene index.
        data = {**metadata, "anchor_activity_scenes": [], "message_count": 3,
                "messages": messages}
    path = directory / "legacy-8094.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


@pytest.fixture
def authenticated_http(tmp_path, monkeypatch):
    """Use the real HTTP handler, password login, auth cookie and CSRF check."""
    import server

    monkeypatch.setenv("HERMES_WEBUI_PASSWORD", "dummy-8094-password")
    auth._invalidate_password_hash_cache()
    monkeypatch.setattr(routes, "_get_active_profile_name", profiles.get_active_profile_name)
    httpd = server.QuietHTTPServer(("127.0.0.1", 0), server.Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()

    def request(path, payload, *, cookie=None, csrf=None):
        connection = http.client.HTTPConnection("127.0.0.1", httpd.server_port, timeout=5)
        headers = {"Content-Type": "application/json", "Connection": "close",
                   "Origin": f"http://127.0.0.1:{httpd.server_port}"}
        if cookie:
            headers["Cookie"] = cookie
        if csrf:
            headers[auth.CSRF_HEADER_NAME] = csrf
        try:
            connection.request("POST", path, json.dumps(payload), headers)
            response = connection.getresponse()
            result = (response.status, json.loads(response.read()), response.getheaders())
            return result
        finally:
            connection.close()

    try:
        status, _, headers = request("/api/auth/login", {"password": "dummy-8094-password"})
        assert status == 200
        cookie = next(value.split(";", 1)[0] for name, value in headers
                      if name.lower() == "set-cookie" and value.startswith(auth._resolve_cookie_name() + "="))
        token = cookie.split("=", 1)[1]
        csrf = auth.csrf_token_for_session(token)
        assert csrf
        yield request, cookie, csrf
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)
        auth._invalidate_password_hash_cache()


@pytest.mark.parametrize("layout", ["messages_first", "scenes_first"])
def test_refused_archive_leaves_legacy_bytes_backup_and_cache_untouched(
    isolated_sessions, authenticated_http, layout,
):
    directory, cache = isolated_sessions
    path = _legacy_sidecar(directory, layout=layout)
    before = path.read_bytes()
    request, cookie, csrf = authenticated_http
    status, body, _ = request("/api/session/archive", {"session_id": "legacy-8094"},
                              cookie=cookie + "; hermes_profile=default", csrf=csrf)
    assert (status, body.get("code")) == (409, "session_profile_mismatch")
    print("refused archive", layout, "rows", len(json.loads(path.read_bytes())["messages"]),
          "byte_identical", path.read_bytes() == before, "backup", path.with_suffix(".json.bak").exists(),
          "cached", "legacy-8094" in cache)
    assert path.read_bytes() == before
    assert not path.with_suffix(".json.bak").exists()
    assert "legacy-8094" not in cache


def test_authorized_archive_still_repairs_and_persists(isolated_sessions, authenticated_http):
    directory, cache = isolated_sessions
    path = _legacy_sidecar(directory, profile="default")
    request, cookie, csrf = authenticated_http
    status, body, _ = request("/api/session/archive", {"session_id": "legacy-8094"},
                              cookie=cookie + "; hermes_profile=default", csrf=csrf)
    assert status == 200, body
    data = json.loads(path.read_bytes())
    assert data["archived"] is True
    assert len(data["messages"]) == 2
    assert path.with_suffix(".json.bak").exists()
    assert cache["legacy-8094"].archived is True


def test_auth_and_csrf_fail_before_ownership_read(isolated_sessions, authenticated_http):
    directory, cache = isolated_sessions
    path = _legacy_sidecar(directory)
    before = path.read_bytes()
    request, cookie, _ = authenticated_http
    assert request("/api/session/archive", {"session_id": "legacy-8094"})[0] == 401
    assert request("/api/session/archive", {"session_id": "legacy-8094"}, cookie=cookie)[0] == 403
    assert path.read_bytes() == before
    assert not path.with_suffix(".json.bak").exists()
    assert not cache


def test_guard_does_not_promote_resident_foreign_session(isolated_sessions, monkeypatch):
    _, cache = isolated_sessions
    cache["legacy-8094"] = models.Session(session_id="legacy-8094", profile="work")
    cache["newer"] = models.Session(session_id="newer", profile="default")
    before = list(cache)
    monkeypatch.setattr(routes, "_get_active_profile_name", lambda: "default")
    assert not routes._session_id_visible_to_request_profile(object(), "legacy-8094", emit_error=False)
    assert list(cache) == before


def test_missing_and_invalid_ids_do_not_materialize(isolated_sessions):
    directory, cache = isolated_sessions
    assert routes._session_id_visible_to_request_profile(object(), "missing-8094", emit_error=False)
    assert routes._session_id_visible_to_request_profile(object(), "../unsafe", emit_error=False)
    assert not cache
    assert list(directory.iterdir()) == []


def test_deleted_missing_session_stays_tombstoned(isolated_sessions, authenticated_http):
    directory, cache = isolated_sessions
    models._record_webui_deleted_session_tombstone("deleted-8094")
    tombstone = models._webui_deleted_session_tombstone_file()
    before = tombstone.read_bytes()
    request, cookie, csrf = authenticated_http
    status, _, _ = request("/api/session/archive", {"session_id": "deleted-8094"},
                            cookie=cookie + "; hermes_profile=default", csrf=csrf)
    assert status == 404
    assert tombstone.read_bytes() == before
    assert not (directory / "deleted-8094.json").exists()
    assert not cache


def test_profile_agnostic_route_does_not_repair_foreign_sidecar(isolated_sessions, monkeypatch):
    from urllib.parse import urlparse

    directory, cache = isolated_sessions
    path = _legacy_sidecar(directory)
    before = path.read_bytes()
    assert routes._guard_request_session_visibility(
        object(), urlparse("/api/session?session_id=legacy-8094"), method="GET"
    )
    assert path.read_bytes() == before
    assert not cache


@pytest.mark.parametrize("profile,active,visible", [(None, "default", True), (None, "work", False)])
def test_legacy_unknown_profile_keeps_existing_visibility(isolated_sessions, monkeypatch, profile, active, visible):
    directory, cache = isolated_sessions
    path = _legacy_sidecar(directory, profile=profile)
    before = path.read_bytes()
    monkeypatch.setattr(routes, "_get_active_profile_name", lambda: active)
    assert routes._session_id_visible_to_request_profile(object(), "legacy-8094", emit_error=False) is visible
    assert path.read_bytes() == before
    assert not cache


@pytest.mark.parametrize("payload", ['{"session_id":', '[1,2]', '{"session_id":"other","profile":"default"}',
                                     '{"session_id":"legacy-8094","profile":{"name":"default"}}'])
def test_unverifiable_ownership_fails_closed_without_repair(isolated_sessions, payload):
    directory, cache = isolated_sessions
    path = directory / "legacy-8094.json"
    path.write_text(payload)
    assert not routes._session_id_visible_to_request_profile(object(), "legacy-8094", emit_error=False)
    assert path.read_text() == payload
    assert not path.with_suffix(".json.bak").exists()
    assert not cache


def test_mismatched_cache_identity_is_ignored_without_eviction(isolated_sessions, monkeypatch):
    directory, cache = isolated_sessions
    path = _legacy_sidecar(directory)
    wrong = models.Session(session_id="another-session", profile="default")
    cache["legacy-8094"] = wrong
    before = path.read_bytes()
    monkeypatch.setattr(routes, "_get_active_profile_name", lambda: "default")
    assert not routes._session_id_visible_to_request_profile(object(), "legacy-8094", emit_error=False)
    assert cache["legacy-8094"] is wrong
    assert path.read_bytes() == before


def test_modern_prefix_does_not_full_parse_transcript(isolated_sessions, monkeypatch):
    directory, cache = isolated_sessions
    path = _legacy_sidecar(directory)
    data = json.loads(path.read_bytes())
    data = {"session_id": "legacy-8094", "profile": "work", "message_count": 3,
            "anchor_scene_index": {}, "messages": data["messages"]}
    path.write_text(json.dumps(data))
    monkeypatch.setattr(type(path), "read_bytes", lambda *_a: (_ for _ in ()).throw(AssertionError("full parse")))
    assert models.get_session_profile_readonly("legacy-8094") == "work"
    assert not cache
