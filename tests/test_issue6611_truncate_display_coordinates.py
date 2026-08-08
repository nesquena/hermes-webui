"""Display-to-child truncation contract for regeneration (#6611)."""
from __future__ import annotations

import copy
import json
from io import BytesIO
from types import SimpleNamespace

import pytest

import api.models as models
import api.routes as routes
from api.models import Session


def _msg(mid, role, content, timestamp):
    return {"id": mid, "role": role, "content": content, "timestamp": timestamp}


@pytest.fixture(autouse=True)
def isolated_sessions(monkeypatch, tmp_path):
    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    monkeypatch.setattr(models, "SESSION_DIR", session_dir)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", session_dir / "_index.json")
    monkeypatch.setattr(routes, "_check_csrf", lambda _handler: True)
    monkeypatch.setattr("api.config._evict_session_agent", lambda _sid: None)
    models.SESSIONS.clear()
    yield
    models.SESSIONS.clear()


def _save(session):
    session.save()
    models.SESSIONS[session.session_id] = session
    return session


def _post(monkeypatch, body):
    raw = json.dumps(body).encode()
    captured = {}

    def fake_j(_handler, payload, status=200, extra_headers=None):
        captured.update(payload=payload, status=status)
        return True

    monkeypatch.setattr(routes, "j", fake_j)
    monkeypatch.setattr(
        routes,
        "bad",
        lambda handler, message, status=400: fake_j(
            handler, {"error": message}, status=status
        ),
    )
    handler = SimpleNamespace(
        headers={"Content-Length": str(len(raw))},
        rfile=BytesIO(raw),
    )
    routes.handle_post(handler, SimpleNamespace(path="/api/session/truncate"))
    return captured


def _lineage(*, fork=False):
    parent = _save(
        Session(
            session_id="parent",
            messages=[
                _msg("p-u", "user", "parent prompt", 10.125),
                _msg("p-a", "assistant", "parent answer", 11.125),
            ],
            pre_compression_snapshot=True,
            session_source="fork" if fork else "webui",
        )
    )
    child_rows = [
        _msg("c-u", "user", "child prompt", 20.125),
        _msg("c-a", "assistant", "child error", 21.125),
    ]
    child = _save(
        Session(
            session_id="child",
            parent_session_id=parent.session_id,
            session_source="fork" if fork else "webui",
            messages=copy.deepcopy(child_rows),
            context_messages=copy.deepcopy(child_rows),
        )
    )
    return parent, child


def _target(session_id, row, display_index, display_keep_count):
    return {
        "session_id": session_id,
        "message_id": row["id"],
        "timestamp": row["timestamp"],
        "display_index": display_index,
        "display_keep_count": display_keep_count,
    }


@pytest.mark.parametrize("fork", [False, True])
def test_compressed_child_display_keep_maps_to_child_local_keep(monkeypatch, fork):
    _parent, child = _lineage(fork=fork)
    display = routes._merged_session_messages_for_display(child)
    assert [row["id"] for row in display] == ["p-u", "p-a", "c-u", "c-a"]
    target = _target("child", display[2], 2, 3)

    response = _post(
        monkeypatch,
        {
            "session_id": "child",
            "keep_count": 3,
            "keep_count_space": "display",
            "regenerate_target": target,
        },
    )

    assert response["status"] == 200
    assert [row["id"] for row in Session.load("child").messages] == ["c-u"]
    assert [row["id"] for row in Session.load("parent").messages] == ["p-u", "p-a"]


def test_parent_only_target_returns_typed_409_and_mutates_neither_store(monkeypatch):
    parent, child = _lineage()
    display = routes._merged_session_messages_for_display(child)
    target = _target("child", display[0], 0, 1)
    parent_before = parent.path.read_bytes()
    child_before = child.path.read_bytes()

    response = _post(
        monkeypatch,
        {
            "session_id": "child",
            "keep_count": 1,
            "keep_count_space": "display",
            "regenerate_target": target,
        },
    )

    assert response == {
        "payload": {
            "error": "The selected turn belongs to an archived parent session.",
            "code": "parent_only_target",
        },
        "status": 409,
    }
    assert parent.path.read_bytes() == parent_before
    assert child.path.read_bytes() == child_before


def test_direct_session_display_space_is_identity_mapping(monkeypatch):
    session = _save(
        Session(
            session_id="direct",
            messages=[
                _msg("u1", "user", "one", 1.125),
                _msg("a1", "assistant", "answer", 2.125),
                _msg("u2", "user", "two", 3.125),
            ],
        )
    )
    response = _post(
        monkeypatch,
        {
            "session_id": "direct",
            "keep_count": 1,
            "keep_count_space": "display",
            "regenerate_target": _target("direct", session.messages[0], 0, 1),
        },
    )

    assert response["status"] == 200
    assert [row["id"] for row in Session.load("direct").messages] == ["u1"]


def test_pure_fork_child_keeps_its_independent_local_coordinate(monkeypatch):
    parent = _save(
        Session(
            session_id="fork-parent",
            messages=[_msg("p-u", "user", "parent", 1.125)],
            pre_compression_snapshot=True,
            session_source="webui",
        )
    )
    child = _save(
        Session(
            session_id="fork-child",
            parent_session_id=parent.session_id,
            session_source="fork",
            messages=[
                _msg("f-u", "user", "fork prompt", 2.125),
                _msg("f-a", "assistant", "fork error", 3.125),
            ],
        )
    )
    display = routes._merged_session_messages_for_display(child)
    assert [row["id"] for row in display] == ["f-u", "f-a"]

    response = _post(
        monkeypatch,
        {
            "session_id": child.session_id,
            "keep_count": 1,
            "keep_count_space": "display",
            "regenerate_target": _target(child.session_id, display[0], 0, 1),
        },
    )

    assert response["status"] == 200
    assert [row["id"] for row in Session.load(child.session_id).messages] == ["f-u"]


@pytest.mark.parametrize("space", [None, "session"])
def test_generic_truncate_preserves_session_local_coordinate(monkeypatch, space):
    _parent, child = _lineage()
    body = {"session_id": "child", "keep_count": 1}
    if space is not None:
        body["keep_count_space"] = space

    response = _post(monkeypatch, body)

    assert response["status"] == 200
    assert [row["id"] for row in Session.load("child").messages] == ["c-u"]


def test_stale_display_target_returns_409_before_mutation(monkeypatch):
    _parent, child = _lineage()
    before = child.path.read_bytes()
    response = _post(
        monkeypatch,
        {
            "session_id": "child",
            "keep_count": 3,
            "keep_count_space": "display",
            "regenerate_target": _target(
                "child",
                {"id": "missing", "timestamp": 20.125},
                2,
                3,
            ),
        },
    )

    assert response["status"] == 409
    assert response["payload"]["code"] == "stale_regeneration_target"
    assert child.path.read_bytes() == before
