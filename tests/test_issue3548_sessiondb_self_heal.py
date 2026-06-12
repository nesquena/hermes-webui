"""Regression tests for issue #3548: closed SessionDB handles are not reused on self-heal retry."""

from __future__ import annotations

from pathlib import Path
import sys
import types
from unittest import mock

def test_session_db_helper_uses_request_state_db_path():
    import api.streaming as streaming

    calls = {}

    class FakeSessionDB:
        def __init__(self, db_path=None):
            calls["db_path"] = db_path

        def close(self):
            calls["closed"] = True

    fake_state = types.ModuleType("hermes_state")
    fake_state.SessionDB = FakeSessionDB

    with mock.patch.dict(sys.modules, {"hermes_state": fake_state}):
        state_db_path = Path("/tmp/profile") / "state.db"
        db = streaming._build_session_db_for_stream(state_db_path)

    assert db is not None
    assert calls["db_path"] == state_db_path
    assert isinstance(db, FakeSessionDB)


def test_session_db_helper_returns_none_when_constructor_fails():
    import api.streaming as streaming

    def failing_session_db(db_path=None):
        raise RuntimeError("SessionDB unavailable")

    fake_state = types.ModuleType("hermes_state")
    fake_state.SessionDB = mock.Mock(side_effect=failing_session_db)

    with mock.patch.dict(sys.modules, {"hermes_state": fake_state}):
        db = streaming._build_session_db_for_stream(Path("/tmp/profile/state.db"))

    assert db is None


def test_self_heal_session_db_handle_is_replaced_safely():
    import api.streaming as streaming

    class FakeDb:
        def __init__(self, label):
            self.label = label

        def close(self):
            self.closed = True

    old_db = FakeDb("old")
    new_db = FakeDb("new")
    with mock.patch.object(
        streaming, "_build_session_db_for_stream", return_value=new_db
    ) as build_db:
        kwargs = {"session_db": old_db}
        assigned_db = streaming._replace_session_db_in_kwargs(kwargs, Path("/tmp/profile/state.db"))

    assert assigned_db is new_db
    assert kwargs["session_db"] is new_db
    build_db.assert_called_once_with(Path("/tmp/profile/state.db"))
    assert getattr(old_db, "closed", False) is True
    assert hasattr(new_db, "closed") is False


def test_session_db_handle_not_double_closed_when_rebuilt_to_same_instance():
    import api.streaming as streaming

    db = mock.Mock(name="session_db")

    with mock.patch.object(streaming, "_build_session_db_for_stream", return_value=db):
        kwargs = {"session_db": db}
        returned_db = streaming._replace_session_db_in_kwargs(kwargs, Path("/tmp/profile/state.db"))

    assert returned_db is db
    assert kwargs["session_db"] is db
    db.close.assert_not_called()
