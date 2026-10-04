"""Gateway-backend async-delegation completions must wake the WebUI session.

The Gateway persists them as ``async_delegation_complete`` delivery rows in the
profile state.db and starts no turn; the WebUI poller claims and wakes.
"""
import json
import time

import pytest

hermes_state = pytest.importorskip("hermes_state")

import api.background_process as bp
import api.gateway_delegation_wakeup as gdw


def _deliver(db, sid, deleg_id, **meta):
    db.append_delegation_delivery(sid, f"[ASYNC DELEGATION COMPLETE — {deleg_id}]\nresult",
                                  {"delegation_id": deleg_id, **meta})


@pytest.fixture
def env(tmp_path, monkeypatch):
    if not gdw._claim_api_available():
        pytest.skip("Hermes Agent without the delivery claim/release API")
    home = tmp_path / "home"
    sessions = tmp_path / "sessions"
    home.mkdir(); sessions.mkdir()
    db_path = home / "state.db"
    db = hermes_state.SessionDB(db_path)
    db.create_session("sid1", source="api_server")
    (sessions / "sid1.json").write_text(json.dumps({"session_id": "sid1"}))
    monkeypatch.setattr(gdw, "_profile_state_dbs", lambda: [("default", db_path)])
    monkeypatch.setattr("api.config.SESSION_DIR", sessions)
    monkeypatch.setattr(bp, "_session_has_active_turn", lambda _sid: False)
    calls = []
    status = {"v": 200}

    def fake_start(sid, prompt, source="process_wakeup"):
        calls.append((sid, prompt, source))
        return {"_status": status["v"], "stream_id": "s"}

    monkeypatch.setattr("api.routes.start_session_turn", fake_start)
    yield db, calls, status
    db.close()


def test_delivery_row_starts_wakeup_turn_once(env):
    db, calls, _ = env
    _deliver(db, "sid1", "deleg_a")
    assert gdw.poll_once(time.time() - 60) == 1
    assert calls and calls[0][0] == "sid1" and "ASYNC DELEGATION COMPLETE — deleg_a" in calls[0][1]
    assert calls[0][2] == "process_wakeup"
    # Claimed exactly once: the next poll (and the Gateway's next-run fold) see nothing.
    assert gdw.poll_once(time.time() - 60) == 0
    assert db.claim_caller_history_deliveries("sid1") == []


def test_hidden_delivery_row_wakes_once_and_is_claimed_once(env, tmp_path):
    import sqlite3
    db, calls, _ = env
    _deliver(db, "sid1", "deleg_h", presentation_suppressed=True, delivery_notice="task_failure:0")
    with sqlite3.connect(tmp_path / "home" / "state.db") as conn:
        kinds = [r[0] for r in conn.execute("SELECT display_kind FROM messages WHERE session_id = 'sid1'")]
    assert kinds == ["hidden"]
    assert gdw.poll_once(time.time() - 60) == 1
    assert len(calls) == 1 and "deleg_h" in calls[0][1]
    assert gdw.poll_once(time.time() - 60) == 0
    assert len(calls) == 1
    assert db.claim_caller_history_deliveries("sid1") == []


def test_busy_session_leaves_row_unclaimed(env, monkeypatch):
    db, calls, _ = env
    _deliver(db, "sid1", "deleg_b")
    monkeypatch.setattr(bp, "_session_has_active_turn", lambda _sid: True)
    assert gdw.poll_once(time.time() - 60) == 0
    assert calls == []
    monkeypatch.setattr(bp, "_session_has_active_turn", lambda _sid: False)
    assert gdw.poll_once(time.time() - 60) == 1


def test_start_race_409_releases_rows_for_the_next_poll(env):
    db, calls, status = env
    _deliver(db, "sid1", "deleg_c")
    status["v"] = 409
    assert gdw.poll_once(time.time() - 60) == 0
    status["v"] = 200
    assert gdw.poll_once(time.time() - 60) == 1
    assert "deleg_c" in calls[-1][1]


def test_failed_start_leaves_row_unconsumed(env, monkeypatch):
    db, calls, _ = env
    _deliver(db, "sid1", "deleg_e")

    def boom(*_a, **_k):
        raise RuntimeError("gateway unreachable")

    monkeypatch.setattr("api.routes.start_session_turn", boom)
    assert gdw.poll_once(time.time() - 60) == 0
    # A restart now loses nothing: the row is still claimable by the Gateway's next-run fold.
    rows = db.claim_caller_history_deliveries("sid1")
    assert [r["display_metadata"]["delegation_id"] for r in rows] == ["deleg_e"]


def test_sidecar_of_another_profile_is_not_woken(env, tmp_path):
    db, calls, _ = env
    (tmp_path / "sessions" / "sid1.json").write_text(json.dumps({"session_id": "sid1", "profile": "work"}))
    _deliver(db, "sid1", "deleg_f")
    assert gdw.poll_once(time.time() - 60) == 0
    assert calls == []
    assert len(db.claim_caller_history_deliveries("sid1")) == 1


def test_sidecar_of_same_named_profile_is_woken(env, tmp_path, monkeypatch):
    db, calls, _ = env
    db_path = tmp_path / "home" / "state.db"
    monkeypatch.setattr(gdw, "_profile_state_dbs", lambda: [("work", db_path)])
    (tmp_path / "sessions" / "sid1.json").write_text(json.dumps({"session_id": "sid1", "profile": "work"}))
    _deliver(db, "sid1", "deleg_g")
    assert gdw.poll_once(time.time() - 60) == 1
    assert calls[0][0] == "sid1" and "deleg_g" in calls[0][1]
    assert db.claim_caller_history_deliveries("sid1") == []


def test_poller_inert_without_agent_claim_api(monkeypatch):
    monkeypatch.setattr("api.gateway_chat.webui_gateway_chat_enabled", lambda _cfg: True)
    monkeypatch.setattr(hermes_state.SessionDB, "release_caller_history_deliveries", None, raising=False)
    assert gdw.start_gateway_delegation_poller() is False


def test_non_webui_session_is_ignored(env):
    db, calls, _ = env
    db.create_session("other", source="api_server")
    _deliver(db, "other", "deleg_d")
    assert gdw.poll_once(time.time() - 60) == 0
    assert calls == []
    # Left for its own client's next run to fold.
    assert len(db.claim_caller_history_deliveries("other")) == 1
