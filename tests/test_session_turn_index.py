"""Focused coverage for the lightweight GET /api/session/turns index."""

from collections import OrderedDict
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import urlparse

import time

from api import config, models, routes


def test_empty_session():
    assert routes._session_turn_index([]) == {"turns": [], "total_messages": 0}


def test_three_turns_have_absolute_indices_and_following_row_counts():
    messages = [
        {"role": "user", "content": "one", "timestamp": 1.25},
        {"role": "assistant", "content": "a"},
        {"role": "tool", "content": "t"},
        {"role": "user", "content": "two", "timestamp": 2},
        {"role": "assistant", "content": "b"},
        {"role": "user", "content": "three"},
        {"role": "assistant", "content": "c"},
        {"role": "tool", "content": "t"},
    ]
    result = routes._session_turn_index(messages)
    assert result["total_messages"] == 8
    assert result["turns"] == [
        {"k": 0, "msg_idx": 0, "ts": 1.25, "preview": "one", "n_msgs": 2},
        {"k": 1, "msg_idx": 3, "ts": 2.0, "preview": "two", "n_msgs": 1},
        {"k": 2, "msg_idx": 5, "ts": None, "preview": "three", "n_msgs": 2},
    ]


def test_hidden_user_rows_do_not_start_turns_but_keep_absolute_indices():
    hidden = {"role": "user", "content": "hidden", "_source": "delegation_wakeup"}
    messages = [
        {"role": "user", "content": "visible"},
        hidden,
        {"role": "assistant", "content": "answer"},
        {"role": "user", "content": "next"},
    ]
    result = routes._session_turn_index(messages)
    assert [(t["msg_idx"], t["n_msgs"]) for t in result["turns"]] == [(0, 2), (3, 0)]


def test_rows_before_first_visible_user_are_ignored():
    messages = [
        {"role": "assistant", "content": "leading"},
        {"role": "user", "content": "hidden", "_source": "delegation_wakeup"},
        {"role": "user", "content": "first"},
    ]
    assert routes._session_turn_index(messages)["turns"] == [
        {"k": 0, "msg_idx": 2, "ts": None, "preview": "first", "n_msgs": 0}
    ]


def test_preview_joins_text_parts_truncates_and_redacts(monkeypatch):
    monkeypatch.setattr("api.config.load_settings", lambda: {"api_redact_enabled": True})
    secret = "sk-" + "a" * 40
    messages = [
        {"role": "user", "content": [
            {"type": "text", "text": "  token\n" + secret},
            {"type": "image", "source": "ignored"},
            {"type": "text", "text": "tail"},
        ]},
        {"role": "user", "content": "b" * 100},
    ]
    turns = routes._session_turn_index(messages)["turns"]
    assert secret not in turns[0]["preview"]
    assert turns[0]["preview"].startswith("token sk-aaa...")
    assert turns[0]["preview"].endswith("tail")
    assert turns[1]["preview"] == "b" * 80


def _session_fixture(monkeypatch, tmp_path, messages):
    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    for module in (config, models):
        monkeypatch.setattr(module, "SESSION_DIR", session_dir)
        monkeypatch.setattr(module, "SESSION_INDEX_FILE", session_dir / "_index.json")
    monkeypatch.setattr(models, "SESSIONS", OrderedDict())
    monkeypatch.setattr(routes, "_get_active_profile_name", lambda: "turn-test")
    session = models.Session(
        session_id="turn-index-fixture",
        profile="turn-test",
        messages=messages,
        created_at=1.0,
        updated_at=2.0,
    )
    session.save(touch_updated_at=False)
    return session


def _get_payload(path):
    with patch.object(routes, "j") as response, patch("api.helpers.j", response):
        routes.handle_get(SimpleNamespace(_safe_webui_print=lambda *_: None), urlparse(path))
    return response.call_args.kwargs.get("status", 200), response.call_args.args[1]


def test_route_msg_idx_agrees_with_full_session_positions(monkeypatch, tmp_path):
    messages = [
        {"role": "assistant", "content": "ignored"},
        {"role": "user", "content": "first", "timestamp": 10},
        {"role": "user", "content": "hidden", "_source": "delegation_wakeup"},
        {"role": "assistant", "content": "answer"},
        {"role": "user", "content": "second", "timestamp": 20},
    ]
    _session_fixture(monkeypatch, tmp_path, messages)
    status, turns_payload = _get_payload("/api/session/turns?session_id=turn-index-fixture")
    full_status, full_payload = _get_payload(
        "/api/session?session_id=turn-index-fixture&msg_limit=all&resolve_model=0"
    )
    assert (status, full_status) == (200, 200)
    assert turns_payload["session_id"] == "turn-index-fixture"
    assert turns_payload["total_messages"] == len(full_payload["session"]["messages"])
    assert [t["msg_idx"] for t in turns_payload["turns"]] == [
        i for i, m in enumerate(full_payload["session"]["messages"])
        if m["role"] == "user" and m.get("_source") != "delegation_wakeup"
    ] == [1, 4]
    assert "messages" not in turns_payload


def test_unknown_session_uses_session_404(monkeypatch):
    monkeypatch.setattr(routes, "_refuse_cross_profile_before_load", lambda *_, **__: False)
    def missing_session(*_, **__):
        raise KeyError("unknown")

    monkeypatch.setattr(routes, "get_session", missing_session)
    status, payload = _get_payload("/api/session/turns?session_id=unknown")
    assert status == 404
    assert payload == {"error": "Session not found"}


def test_five_thousand_message_single_turn_is_fast():
    messages = [{"role": "user", "content": "only"}] + [
        {"role": "assistant", "content": str(i)} for i in range(4999)
    ]
    start = time.perf_counter()
    result = routes._session_turn_index(messages)
    elapsed = time.perf_counter() - start
    assert len(result["turns"]) == 1
    assert result["turns"][0]["n_msgs"] == 4999
    assert result["total_messages"] == 5000
    assert elapsed < 1.0
