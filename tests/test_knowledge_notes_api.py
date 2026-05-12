import io
import json
from pathlib import Path
from urllib.parse import urlparse, quote


class _FakeHandler:
    def __init__(self, body_bytes: bytes = b""):
        self.status = None
        self.sent_headers = []
        self.body = bytearray()
        self.wfile = self
        self.rfile = io.BytesIO(body_bytes)
        self.headers = {"Content-Length": str(len(body_bytes))}
        self.request = None

    def send_response(self, status):
        self.status = status

    def send_header(self, name, value):
        self.sent_headers.append((name, value))

    def end_headers(self):
        pass

    def write(self, data):
        self.body.extend(data)

    def json_body(self):
        return json.loads(bytes(self.body).decode("utf-8"))


def _post(path: str, payload: dict):
    from api.routes import handle_post

    handler = _FakeHandler(json.dumps(payload).encode("utf-8"))
    handled = handle_post(handler, urlparse(f"http://example.com{path}"))
    return handled, handler


def _install_fake_knowledge_index(root: Path):
    root.mkdir(parents=True, exist_ok=True)
    (root / "knowledge_index.py").write_text(
        """
from pathlib import Path

def load_config(path=None):
    return {'database_path': str(Path(__file__).with_name('knowledge.sqlite3'))}

def status(cfg=None, config_path=None):
    return {
        'db_path': str(Path(__file__).with_name('knowledge.sqlite3')),
        'db_exists': True,
        'config_ok': True,
        'source_count': 2,
        'chunk_count': 7,
        'last_error_count': 0,
        'stale_source_count': 0,
        'last_run_status': 'ok',
        'last_successful_run': '2026-05-12T12:00:00+00:00',
        'embedding_enabled': False,
    }

def search(query, cfg=None, config_path=None, limit=10, source_types=None):
    return {'query': query, 'results': [
        {
            'path': str(Path.cwd() / 'Vault' / 'Note.md'),
            'source_type': 'obsidian',
            'title': 'Unsafe <script>Title</script>',
            'heading_path': 'Heading',
            'start_line': 1,
            'end_line': 4,
            'snippet': 'Relevant result with SECRET_VALUE_DO_NOT_LEAK and <script>alert(1)</script>',
            'rank': -1.0,
            'content_sha256': 'abc123',
        }
    ][:limit]}

def read_source(path, cfg=None, config_path=None, offset=1, limit=80):
    if 'outside' in str(path):
        raise PermissionError('not indexed: /private/secret.txt')
    return {
        'path': str(path),
        'offset': offset,
        'limit': limit,
        'total_lines': 2,
        'content': '1|# Safe Note\\n2|SECRET_VALUE_DO_NOT_LEAK should be redacted',
    }
""".lstrip(),
        encoding="utf-8",
    )


def test_knowledge_status_route_reports_local_index_without_raw_db_path(monkeypatch, tmp_path):
    knowledge_root = tmp_path / "local-knowledge"
    _install_fake_knowledge_index(knowledge_root)
    monkeypatch.setenv("HERMES_LOCAL_KNOWLEDGE_DIR", str(knowledge_root))

    from api.routes import handle_get

    handler = _FakeHandler()
    handled = handle_get(handler, urlparse("http://example.com/api/knowledge/status"))

    assert handled is True
    assert handler.status == 200
    payload = handler.json_body()
    assert payload["available"] is True
    assert payload["local_only"] is True
    assert payload["source_count"] == 2
    assert payload["chunk_count"] == 7
    assert "db_path" not in payload, "UI status must not expose local database paths"


def test_knowledge_search_route_returns_safe_metadata_and_redacted_snippets(monkeypatch, tmp_path):
    knowledge_root = tmp_path / "local-knowledge"
    _install_fake_knowledge_index(knowledge_root)
    vault = tmp_path / "Vault"
    vault.mkdir()
    monkeypatch.setenv("HERMES_LOCAL_KNOWLEDGE_DIR", str(knowledge_root))
    monkeypatch.setenv("OBSIDIAN_VAULT_PATH", str(vault))

    from api.routes import handle_get

    handler = _FakeHandler()
    handled = handle_get(
        handler,
        urlparse("http://example.com/api/knowledge/search?q=capy%20notes&limit=3&source_type=obsidian"),
    )

    assert handled is True
    assert handler.status == 200
    payload = handler.json_body()
    assert payload["query"] == "capy notes"
    assert len(payload["results"]) == 1
    result = payload["results"][0]
    assert result["source_type"] == "obsidian"
    assert "SECRET_VALUE_DO_NOT_LEAK" not in json.dumps(result)
    assert "<script>" not in json.dumps(result)
    assert result["snippet"].count("[REDACTED]") >= 1
    assert result["obsidian_url"].startswith("obsidian://open?")


def test_knowledge_read_route_redacts_content_and_maps_permission_errors_to_403(monkeypatch, tmp_path):
    knowledge_root = tmp_path / "local-knowledge"
    _install_fake_knowledge_index(knowledge_root)
    monkeypatch.setenv("HERMES_LOCAL_KNOWLEDGE_DIR", str(knowledge_root))

    from api.routes import handle_get

    handler = _FakeHandler()
    handled = handle_get(handler, urlparse("http://example.com/api/knowledge/read?path=/tmp/note.md&offset=1&limit=20"))

    assert handled is True
    assert handler.status == 200
    payload = handler.json_body()
    assert "SECRET_VALUE_DO_NOT_LEAK" not in payload["content"]
    assert "[REDACTED]" in payload["content"]

    denied = _FakeHandler()
    handled = handle_get(denied, urlparse("http://example.com/api/knowledge/read?path=/tmp/outside-secret.md"))
    assert handled is True
    assert denied.status == 403
    assert "/private/secret" not in denied.json_body().get("error", "")


def test_note_capture_writes_markdown_inside_obsidian_vault_and_returns_metadata_only(monkeypatch, tmp_path):
    vault = tmp_path / "Obsidian Vault"
    monkeypatch.setenv("OBSIDIAN_VAULT_PATH", str(vault))

    handled, handler = _post(
        "/api/notes/capture",
        {
            "title": "Capy <script> Research / Plan",
            "content": "# Private note\nSECRET_VALUE_DO_NOT_LEAK should be stored but not echoed",
            "folder": "00_Inbox",
            "tags": ["capy", "knowledge"],
        },
    )

    assert handled is True
    assert handler.status == 200
    payload = handler.json_body()
    assert payload["ok"] is True
    assert payload["title"] == "Capy Research Plan"
    assert payload["path"].startswith(str(vault.resolve()))
    assert Path(payload["path"]).is_file()
    assert Path(payload["path"]).resolve().is_relative_to(vault.resolve())
    assert "SECRET_VALUE_DO_NOT_LEAK" not in json.dumps(payload)
    assert "<script>" not in json.dumps(payload)
    rel = Path(payload["path"]).resolve().relative_to(vault.resolve()).as_posix()
    assert payload["obsidian_url"] == "obsidian://open?vault=" + quote(vault.name) + "&file=" + quote(rel)
    written = Path(payload["path"]).read_text(encoding="utf-8")
    assert "SECRET_VALUE_DO_NOT_LEAK" in written, "note body should be saved locally even though response is metadata-only"


def test_note_capture_rejects_folder_traversal(monkeypatch, tmp_path):
    monkeypatch.setenv("OBSIDIAN_VAULT_PATH", str(tmp_path / "Vault"))

    handled, handler = _post(
        "/api/notes/capture",
        {"title": "Escape", "content": "nope", "folder": "../outside"},
    )

    assert handled is True
    assert handler.status == 400
    assert "outside" not in handler.json_body().get("path", "")
