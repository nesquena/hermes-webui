"""Regression tests for issue #3548: closed SessionDB handles are not reused on self-heal retry."""

from __future__ import annotations

from pathlib import Path
import sqlite3
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

    with (
        mock.patch.dict(sys.modules, {"hermes_state": fake_state}),
        mock.patch.object(streaming.time, "sleep") as sleep,
    ):
        state_db_path = Path("/tmp/profile") / "state.db"
        db = streaming._build_session_db_for_stream(state_db_path)

    assert db is not None
    assert calls["db_path"] == state_db_path
    assert isinstance(db, FakeSessionDB)
    sleep.assert_not_called()


def test_session_db_helper_retries_transient_constructor_failure():
    import api.streaming as streaming

    state_db_path = Path("/tmp/profile/state.db")
    created = mock.Mock(name="session_db")
    fake_state = types.ModuleType("hermes_state")
    fake_random = mock.Mock()
    fake_random.uniform.return_value = 0.0
    fake_state.SessionDB = mock.Mock(
        side_effect=[
            sqlite3.OperationalError("database is locked"),
            sqlite3.OperationalError("database is busy"),
            created,
        ]
    )

    with (
        mock.patch.dict(sys.modules, {"hermes_state": fake_state}),
        mock.patch.object(streaming, "random", fake_random, create=True),
        mock.patch.object(streaming.time, "sleep") as sleep,
    ):
        db = streaming._build_session_db_for_stream(state_db_path)

    assert db is created
    assert fake_state.SessionDB.call_args_list == [
        mock.call(db_path=state_db_path),
        mock.call(db_path=state_db_path),
        mock.call(db_path=state_db_path),
    ]
    assert sleep.call_args_list == [mock.call(0.05), mock.call(0.1)]


def test_session_db_helper_returns_none_after_exhausted_retries():
    import api.streaming as streaming

    state_db_path = Path("/tmp/profile/state.db")
    fake_state = types.ModuleType("hermes_state")
    fake_random = mock.Mock()
    fake_random.uniform.return_value = 0.0
    fake_state.SessionDB = mock.Mock(
        side_effect=sqlite3.OperationalError("database is locked")
    )

    with (
        mock.patch.dict(sys.modules, {"hermes_state": fake_state}),
        mock.patch.object(streaming, "random", fake_random, create=True),
        mock.patch.object(streaming.time, "sleep") as sleep,
    ):
        db = streaming._build_session_db_for_stream(state_db_path)

    assert db is None
    assert fake_state.SessionDB.call_args_list == [
        mock.call(db_path=state_db_path),
        mock.call(db_path=state_db_path),
        mock.call(db_path=state_db_path),
    ]
    assert sleep.call_args_list == [mock.call(0.05), mock.call(0.1)]


def test_session_db_helper_does_not_retry_permanent_constructor_failure():
    import api.streaming as streaming

    state_db_path = Path("/tmp/profile/state.db")
    permanent_error = TypeError("unsupported SessionDB argument")
    fake_state = types.ModuleType("hermes_state")
    fake_state.SessionDB = mock.Mock(side_effect=permanent_error)

    with (
        mock.patch.dict(sys.modules, {"hermes_state": fake_state}),
        mock.patch.object(streaming, "random", mock.Mock(), create=True) as random,
        mock.patch.object(streaming.time, "sleep") as sleep,
    ):
        db = streaming._build_session_db_for_stream(state_db_path)

    assert db is None
    fake_state.SessionDB.assert_called_once_with(db_path=state_db_path)
    random.uniform.assert_not_called()
    sleep.assert_not_called()


def test_session_db_helper_does_not_retry_noncontention_operational_error():
    import api.streaming as streaming

    state_db_path = Path("/tmp/profile/state.db")
    permanent_error = sqlite3.OperationalError("locking protocol")
    fake_state = types.ModuleType("hermes_state")
    fake_state.SessionDB = mock.Mock(side_effect=permanent_error)

    with (
        mock.patch.dict(sys.modules, {"hermes_state": fake_state}),
        mock.patch.object(streaming, "random", mock.Mock(), create=True) as random,
        mock.patch.object(streaming.time, "sleep") as sleep,
    ):
        db = streaming._build_session_db_for_stream(state_db_path)

    assert db is None
    fake_state.SessionDB.assert_called_once_with(db_path=state_db_path)
    random.uniform.assert_not_called()
    sleep.assert_not_called()


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
