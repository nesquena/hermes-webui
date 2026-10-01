"""Persistence capabilities are process-local, never part of exported session data."""
import copy
import json

import pytest

from api.models import Session
from api.session_persistence import SessionPersistenceRevoked
from tests.test_session_delete_writeback_revocation import local_store as local_store


def test_session_data_dict_remains_json_serializable(local_store):
    session = local_store.new("serialization-owner")
    payload = json.loads(json.dumps(vars(session)))
    assert "_persistence_handles" not in payload
    assert payload["session_id"] == session.session_id
    assert payload["messages"] == session.messages
    reloaded = Session(**payload)
    assert reloaded.session_id == session.session_id
    reloaded.save()


@pytest.mark.parametrize("copy_session", [copy.copy, copy.deepcopy], ids=["shallow", "deep"])
def test_copied_session_keeps_authority_without_serializing_it(local_store, copy_session):
    session = local_store.new("serialization-copy")
    copied = copy_session(session)
    assert copied is not session
    assert copied._persistence_handle() is session._persistence_handle()
    assert json.loads(json.dumps(vars(copied)))["messages"] == session.messages
    status, response = local_store.delete(session.session_id)
    assert status == 200 and response["ok"]
    for stale in (session, copied):
        with pytest.raises(SessionPersistenceRevoked):
            stale.save()
    assert not session.path.exists()


@pytest.mark.parametrize("fmt", ["json", "html"])
def test_real_export_handler_excludes_process_local_handle(local_store, monkeypatch, fmt):
    from io import BytesIO
    from unittest.mock import MagicMock
    from urllib.parse import urlparse
    from api import routes

    session = local_store.new("serialization-export")
    monkeypatch.setattr(routes, "get_active_profile_name", lambda: session.profile or "default")
    handler = MagicMock()
    handler.wfile = BytesIO()
    parsed = urlparse(f"/api/session/export?session_id={session.session_id}&format={fmt}")
    assert routes._handle_session_export(handler, parsed) is True
    handler.send_response.assert_called_once_with(200)
    exported = handler.wfile.getvalue().decode("utf-8")
    assert "_persistence_handles" not in exported
    assert "SessionPersistenceHandle" not in exported
    assert "keep until delete" in exported
    if fmt == "json":
        assert json.loads(exported)["session_id"] == session.session_id
