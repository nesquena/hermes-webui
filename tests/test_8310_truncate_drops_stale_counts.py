"""#8310 maintainer fold: truncation drops the load-time sidecar counts.

compact() prefers the load-time ``_metadata_message_count`` over the live array. The clear route (truncate to zero,
save, return ``s.compact()``) therefore answered with the pre-clear total, and #8310's /status card, which shows a
"Total messages" row whenever the raw and visible counts differ, rendered a phantom "Messages 0, Total 6" until the
next session load.

The fold drops the counts in ``truncate_session_at_keep`` (the helper the clear and truncate routes share), so
compact() counts the live array as it does for a never-loaded session. An earlier version of the fold refreshed the
counts inside ``Session.save()``; Codex reproduced that a delayed older save could then write a stale count back over
a newer one, and that state.db recovery copies messages without counts. The last test pins that save() assigns
neither count.
"""
from __future__ import annotations

import json
from io import BytesIO
from types import SimpleNamespace


def _seed_session_dir(monkeypatch, tmp_path):
    import api.models as models

    session_dir = tmp_path / "sessions"
    session_dir.mkdir(parents=True)
    monkeypatch.setattr(models, "SESSION_DIR", session_dir)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", session_dir / "_index.json")
    models.SESSIONS.clear()
    return models


def _rows():
    """Six rows: four chat rows, one tool row, one hidden delegation wakeup (raw 6, visible 5)."""
    return [
        {"id": "u1", "role": "user", "content": "first", "timestamp": 1.0},
        {"id": "a1", "role": "assistant", "content": "reply first", "timestamp": 2.0},
        {"id": "t1", "role": "tool", "content": "{}", "tool_call_id": "c1", "timestamp": 2.5},
        {"id": "w1", "role": "user", "content": "[wakeup]", "timestamp": 2.7, "_source": "delegation_wakeup"},
        {"id": "u2", "role": "user", "content": "second", "timestamp": 3.0},
        {"id": "a2", "role": "assistant", "content": "reply second", "timestamp": 4.0},
    ]


def _call_clear(monkeypatch, session_id):
    import api.routes as routes

    body_bytes = json.dumps({"session_id": session_id}).encode()
    monkeypatch.setattr(routes, "_check_csrf", lambda handler: True)
    monkeypatch.setattr("api.config._evict_session_agent", lambda _sid: None)
    captured = {}

    def fake_j(handler, payload, status=200, extra_headers=None):
        captured["payload"] = payload
        captured["status"] = status

    monkeypatch.setattr(routes, "j", fake_j)
    handler = SimpleNamespace(headers={"Content-Length": str(len(body_bytes))}, rfile=BytesIO(body_bytes))
    routes.handle_post(handler, SimpleNamespace(path="/api/session/clear"))
    return captured


def test_clear_response_reports_zero_totals_for_a_loaded_session(monkeypatch, tmp_path):
    models = _seed_session_dir(monkeypatch, tmp_path)
    models.Session(session_id="f8310clear", messages=_rows()).save()
    models.SESSIONS.clear()  # the route loads from disk, caching the load-time counts (6 raw / 5 visible)

    loaded = models.Session.load("f8310clear")
    assert loaded is not None
    assert loaded.compact()["message_count"] == 6
    assert loaded.compact()["visible_message_count"] == 5
    models.SESSIONS.clear()

    captured = _call_clear(monkeypatch, "f8310clear")
    assert captured["payload"].get("ok") is True
    session = captured["payload"]["session"]
    assert session["message_count"] == 0
    assert session["visible_message_count"] == 0


def test_truncate_helper_drops_the_load_time_counts(monkeypatch, tmp_path):
    from api.session_ops import truncate_session_at_keep

    models = _seed_session_dir(monkeypatch, tmp_path)
    models.Session(session_id="f8310trunc", messages=_rows()).save()
    loaded = models.Session.load("f8310trunc")
    assert loaded is not None
    assert loaded._metadata_message_count == 6

    truncate_session_at_keep(loaded, 4)
    assert loaded._metadata_message_count is None
    assert loaded._metadata_visible_message_count is None
    compact = loaded.compact()
    assert compact["message_count"] == 4
    assert compact["visible_message_count"] == 3  # the hidden wakeup row stays out of the visible count

    loaded.save()
    reloaded = models.Session.load("f8310trunc")
    assert reloaded is not None
    assert reloaded.compact()["message_count"] == 4


def test_save_assigns_no_counts_to_the_object(monkeypatch, tmp_path):
    """save() must not stamp counts onto the live object: a delayed older save would race a stale value back."""
    models = _seed_session_dir(monkeypatch, tmp_path)
    fresh = models.Session(session_id="f8310fresh", messages=_rows()[:2])
    fresh.save()
    assert fresh._metadata_message_count is None
    fresh.messages.append({"id": "u9", "role": "user", "content": "more", "timestamp": 9.0})
    assert fresh.compact()["message_count"] == 3
