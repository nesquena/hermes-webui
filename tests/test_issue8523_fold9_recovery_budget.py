"""#8523 release gate, fold 9: one ``POST /api/chat/start`` runs journal recovery once, as on master.

Fold 8 read the session's owner with ``get_session()`` before the route's own load. A full load re-runs lazy journal
recovery, and a journal with no useful output spends one unit of retry budget per load, so every request spent two. At
attempt 10 of 12 a single request then exhausted the budget: the marker was demoted and its recovery metadata removed,
and an answer that reached the journal afterwards was never recovered. The owner is now read with
``get_session_profile_readonly()``, which does not load or repair, so the request loads the session once.

These tests drive the real HTTP handler (password login, auth cookie, CSRF and the profile cookie), the real loader and
the real run journal, for another profile's session and for the owner's own refused request, with the session cold
(only on disk) and cached.
"""
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
from api.run_journal import append_run_event

SID = "fold9-retry-budget"
STREAM = "fold9-retry-stream"


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    home = tmp_path / "hermes_home"
    (home / "sessions").mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(profiles, "_DEFAULT_HERMES_HOME", home)
    directory = tmp_path / "sessions"
    directory.mkdir()
    cache = collections.OrderedDict()
    for module in (config, models, routes):
        monkeypatch.setattr(module, "SESSION_DIR", directory)
        monkeypatch.setattr(module, "SESSIONS", cache)
        if hasattr(module, "SESSION_INDEX_FILE"):
            monkeypatch.setattr(module, "SESSION_INDEX_FILE", directory / "_index.json")
    monkeypatch.setattr(routes, "_lookup_cli_session_metadata", lambda *_a, **_k: {})
    monkeypatch.setattr(routes, "_agent_runtime_barrier_response", lambda **kw: None)
    monkeypatch.setattr(routes, "_start_run", lambda *a, **k: pytest.fail("must not start a turn"))
    # A journal that is not still arriving: each full load that finds no useful output spends one attempt.
    monkeypatch.setattr(models, "_journal_is_still_arriving", lambda *a, **k: False)
    models._JOURNAL_RETRY_LOCKS.clear()
    yield cache
    models._JOURNAL_RETRY_LOCKS.clear()


@pytest.fixture
def http_post(tmp_path, monkeypatch):
    import server

    monkeypatch.setenv("HERMES_WEBUI_PASSWORD", "dummy-8523-fold9-password")
    auth._invalidate_password_hash_cache()
    monkeypatch.setattr(routes, "_get_active_profile_name", profiles.get_active_profile_name)
    httpd = server.QuietHTTPServer(("127.0.0.1", 0), server.Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()

    def request(path, payload, *, cookie=None, csrf=None):
        connection = http.client.HTTPConnection("127.0.0.1", httpd.server_port, timeout=10)
        headers = {"Content-Type": "application/json", "Connection": "close",
                   "Origin": f"http://127.0.0.1:{httpd.server_port}"}
        if cookie:
            headers["Cookie"] = cookie
        if csrf:
            headers[auth.CSRF_HEADER_NAME] = csrf
        try:
            connection.request("POST", path, json.dumps(payload), headers)
            response = connection.getresponse()
            return response.status, response.read(), response.getheaders()
        finally:
            connection.close()

    try:
        status, _, headers = request("/api/auth/login", {"password": "dummy-8523-fold9-password"})
        assert status == 200
        cookie = next(value.split(";", 1)[0] for name, value in headers
                      if name.lower() == "set-cookie" and value.startswith(auth._resolve_cookie_name() + "="))
        csrf = auth.csrf_token_for_session(cookie.split("=", 1)[1])
        assert csrf

        def chat_start(payload):
            status, raw, _ = request("/api/chat/start", payload,
                                     cookie=cookie + "; hermes_profile=default", csrf=csrf)
            return status, raw

        yield chat_start
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)
        auth._invalidate_password_hash_cache()


def _seed(owner, cache, *, cached):
    session = models.Session(session_id=SID, title="Interrupted turn", profile=owner, messages=[
        {"role": "user", "content": "q", "timestamp": 1},
        {
            "role": "assistant",
            "content": models._INTERRUPTED_PENDING_RETRY_WORDING,
            "timestamp": 2,
            "_error": True,
            "type": "interrupted",
            "_pending_journal_recovery": True,
            "_journal_retry_stream_id": STREAM,
            "_journal_retry_attempts": models._JOURNAL_RETRY_MAX_ATTEMPTS - 2,
            "_journal_retry_first_seen_ts": int(models.time.time()),
        },
    ])
    session.save()
    # The journal exists but holds no useful output yet.
    append_run_event(SID, STREAM, "stream_end", {})
    cache.clear()
    if cached:
        cache[SID] = session


def _marker():
    data = json.loads((models.SESSION_DIR / f"{SID}.json").read_text(encoding="utf-8"))
    return data["messages"][-1]


def _assert_attempts_spent_and_answer_still_recoverable(cache, spent):
    marker = _marker()
    assert marker.get("_pending_journal_recovery") is True
    assert marker.get("_journal_retry_attempts") == models._JOURNAL_RETRY_MAX_ATTEMPTS - 2 + spent
    # The answer reaches the journal afterwards: the owner's next load recovers it.
    append_run_event(SID, STREAM, "token", {"text": "The answer arrived late."})
    cache.clear()
    loaded = models.get_session(SID)
    assert any(m.get("_recovered_from_run_journal") and "The answer arrived late." in (m.get("content") or "")
               for m in loaded.messages)


@pytest.mark.parametrize("cached", [False, True], ids=["cold", "cached"])
def test_another_profiles_request_spends_no_attempt_like_master(isolated, http_post, cached):
    # Since #8853 (exp-v0.52.644) master refuses another profile's session before any load, so the request runs no
    # journal recovery and spends no retry budget.
    _seed("josue", isolated, cached=cached)
    status, _ = http_post({"session_id": SID, "message": "hello"})
    assert status in (404, 409)  # master's answer for a session of another profile
    _assert_attempts_spent_and_answer_still_recoverable(isolated, 0)


@pytest.mark.parametrize("cached", [False, True], ids=["cold", "cached"])
def test_owners_refused_request_spends_one_attempt_like_master(isolated, http_post, monkeypatch, cached):
    # The owner's own request, refused after the load (no message text and no attachment is a 400 on both versions).
    _seed(None, isolated, cached=cached)
    status, _ = http_post({"session_id": SID, "message": ""})
    assert 400 <= status < 500
    _assert_attempts_spent_and_answer_still_recoverable(isolated, 1)
