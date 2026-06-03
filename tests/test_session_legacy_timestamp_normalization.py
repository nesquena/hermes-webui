import json
import pathlib
import sys
import time
import urllib.error
import urllib.request
import uuid

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))

from tests._pytest_port import BASE
from tests.conftest import TEST_STATE_DIR, TEST_WORKSPACE

_needs_server = pytest.mark.usefixtures("test_server")


def _get(path):
    req = urllib.request.Request(BASE + path, headers={"Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read()), r.status
    except urllib.error.HTTPError as e:
        return json.loads(e.read()), e.code


@_needs_server
def test_api_sessions_survives_legacy_string_timestamps(cleanup_test_sessions):
    from api.models import Session

    sid = "legacyts_" + uuid.uuid4().hex[:8]
    cleanup_test_sessions.append(sid)
    now = time.time()
    session = Session(
        session_id=sid,
        title="Legacy string timestamp session",
        workspace=str(TEST_WORKSPACE),
        model="test",
        created_at=now,
        updated_at=now,
        profile="default",
        messages=[{"role": "user", "content": "hello"}],
        tool_calls=[],
    )
    session.save(touch_updated_at=False)

    sidecar = TEST_STATE_DIR / "sessions" / f"{sid}.json"
    payload = json.loads(sidecar.read_text(encoding="utf-8"))
    payload["created_at"] = "2026-05-02T17:31:45.925125"
    payload["updated_at"] = "2026-05-02T18:36:25.175590"
    sidecar.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    index_path = TEST_STATE_DIR / "sessions" / "_index.json"
    index_path.unlink(missing_ok=True)

    data, status = _get("/api/sessions")
    assert status == 200, data
    sessions = data.get("sessions") or []
    row = next((s for s in sessions if s.get("session_id") == sid), None)
    assert row is not None, data
    assert isinstance(row.get("created_at"), (int, float))
    assert isinstance(row.get("updated_at"), (int, float))
    assert isinstance(row.get("last_message_at"), (int, float))


def test_full_index_rebuild_survives_legacy_string_updated_at(cleanup_test_sessions):
    from api.models import Session, _write_session_index

    sid = "legacyidx_" + uuid.uuid4().hex[:8]
    cleanup_test_sessions.append(sid)
    now = time.time()
    session = Session(
        session_id=sid,
        title="Legacy rebuild timestamp session",
        workspace=str(TEST_WORKSPACE),
        model="test",
        created_at=now,
        updated_at=now,
        profile="default",
        messages=[{"role": "user", "content": "hello rebuild"}],
        tool_calls=[],
    )
    session.save(touch_updated_at=False)

    sidecar = TEST_STATE_DIR / "sessions" / f"{sid}.json"
    payload = json.loads(sidecar.read_text(encoding="utf-8"))
    payload["created_at"] = "2026-05-02T17:31:45.925125"
    payload["updated_at"] = "2026-05-02T18:36:25.175590"
    sidecar.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    index_path = TEST_STATE_DIR / "sessions" / "_index.json"
    index_path.unlink(missing_ok=True)

    _write_session_index(updates=None)

    rows = json.loads(index_path.read_text(encoding="utf-8"))
    row = next((s for s in rows if s.get("session_id") == sid), None)
    assert row is not None, rows
    assert isinstance(row.get("created_at"), (int, float))
    assert isinstance(row.get("updated_at"), (int, float))
