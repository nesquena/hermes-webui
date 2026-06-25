import json
import pathlib
import sys
import time
import urllib.parse
import urllib.request
import uuid

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).parent.parent.parent))

_needs_server = pytest.mark.usefixtures("test_server")
from tests._pytest_port import BASE
_FULL_SECRET = "sk-" + ("B" * 24)


def _get(path):
    with urllib.request.urlopen(BASE + path, timeout=10) as r:
        return json.loads(r.read())


def _write_session_with_secret_title():
    from api.models import Session
    from tests.conftest import TEST_WORKSPACE

    sid = "sec_summary_" + uuid.uuid4().hex[:8]
    now = time.time()
    session = Session(
        session_id=sid,
        title=f"session with {_FULL_SECRET}",
        workspace=str(TEST_WORKSPACE),
        model="test",
        created_at=now,
        updated_at=now,
        profile="default",
        messages=[{"role": "user", "content": "visible row", "timestamp": now}],
        tool_calls=[],
    )
    # Save through the model layer so the sidebar index is updated just like a
    # real session write. Direct sidecar writes are intentionally not visible to
    # /api/sessions while an index exists.
    session.save(touch_updated_at=False)
    return sid


@_needs_server
def test_api_sessions_search_redacts_titles(test_server):
    sid = _write_session_with_secret_title()
    data = _get("/api/sessions/search?q=" + urllib.parse.quote("B" * 24))
    dump = json.dumps(data)
    assert sid in dump
    assert _FULL_SECRET not in dump


@_needs_server
def test_api_sessions_list_redacts_secret_titles(test_server):
    sid = _write_session_with_secret_title()
    data = _get("/api/sessions")
    dump = json.dumps(data)
    assert sid in dump
    assert _FULL_SECRET not in dump
