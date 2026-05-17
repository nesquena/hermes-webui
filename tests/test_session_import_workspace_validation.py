import io
import json
from pathlib import Path
from urllib.parse import urlparse

from api.config import DEFAULT_WORKSPACE, SESSION_DIR
from api.models import get_session
from api.routes import _handle_file_read, _handle_session_import
from api.workspace import resolve_trusted_workspace


class _DummyHandler:
    def __init__(self):
        self.status = None
        self.response_headers = []
        self.headers = {}
        self.wfile = io.BytesIO()
        self.command = "GET"
        self.path = "/"

    def send_response(self, status):
        self.status = status

    def send_header(self, key, value):
        self.response_headers.append((key, value))

    def end_headers(self):
        pass

    def json_body(self):
        return json.loads(self.wfile.getvalue().decode("utf-8"))


def test_session_import_rejects_blocked_root_workspace():
    handler = _DummyHandler()

    _handle_session_import(
        handler,
        {
            "title": "blocked import",
            "workspace": "/",
            "model": "test",
            "messages": [],
        },
    )

    assert handler.status == 400
    assert "system directory" in handler.json_body()["error"]


def test_session_import_rejects_non_path_workspace_value():
    handler = _DummyHandler()

    _handle_session_import(
        handler,
        {
            "title": "invalid import",
            "workspace": {"not": "a path"},
            "model": "test",
            "messages": [],
        },
    )

    assert handler.status == 400
    assert handler.json_body()["error"]


def test_imported_session_file_read_stays_under_validated_workspace():
    SESSION_DIR.mkdir(parents=True, exist_ok=True)
    workspace = Path(DEFAULT_WORKSPACE)
    workspace.mkdir(parents=True, exist_ok=True)
    (workspace / "allowed.txt").write_text("allowed", encoding="utf-8")

    import_handler = _DummyHandler()
    _handle_session_import(
        import_handler,
        {
            "title": "valid import",
            "workspace": str(workspace),
            "model": "test",
            "messages": [],
        },
    )

    assert import_handler.status == 200
    sid = import_handler.json_body()["session"]["session_id"]
    assert get_session(sid).workspace == str(resolve_trusted_workspace(workspace))

    read_handler = _DummyHandler()
    _handle_file_read(read_handler, urlparse(f"/api/file?session_id={sid}&path=allowed.txt"))

    assert read_handler.status == 200
    assert read_handler.json_body()["content"] == "allowed"


def test_resolver_would_reject_imported_root_before_file_read():
    # Regression guard for the original issue shape: '/' must be rejected at
    # import time rather than becoming a session workspace that makes
    # Path('/')-relative reads like etc/hosts reachable through /api/file.
    try:
        resolve_trusted_workspace(Path("/"))
    except ValueError as exc:
        assert "system directory" in str(exc)
    else:  # pragma: no cover - this would weaken the security invariant
        raise AssertionError("root workspace unexpectedly accepted")
