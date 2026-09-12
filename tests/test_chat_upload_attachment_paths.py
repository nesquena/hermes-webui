"""Regression coverage for WebUI chat upload path handoff."""
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MESSAGES_JS = ROOT / "static" / "messages.js"
UPLOAD_PY = ROOT / "api" / "upload.py"


def test_image_uploads_use_server_path_in_attached_files_context():
    """The agent text context must include real uploaded paths for images.

    /api/upload returns an absolute attachment path. The browser also sends the
    structured attachment payload to /api/chat/start, but text/tool-mode agents
    still rely on the literal ``[Attached files: ...]`` suffix. Images must not
    be downgraded to bare filenames there, otherwise tools like vision_analyze
    cannot open the uploaded file immediately.
    """
    src = MESSAGES_JS.read_text(encoding="utf-8")

    assert "uploadedPaths=uploaded.map(u=>u&&u.is_image?" not in src
    assert "uploadedPaths=uploaded.map(u=>u&&u.path?u.path" in src


def test_attached_files_context_is_hidden_from_user_message_display():
    """Persist full attachment paths for the agent without showing them in chat."""
    ui_src = (ROOT / "static" / "ui.js").read_text(encoding="utf-8")

    assert "function _stripAttachedFilesMarkerForDisplay" in ui_src
    assert "_stripAttachedFilesMarkerForDisplay(_stripWorkspaceDisplayPrefix(content))" in ui_src
    assert "const newRawText=String(displayContent).trim();" in ui_src
    assert "row.dataset.rawText=newRawText;" in ui_src


def test_attached_files_context_is_hidden_from_sidebar_titles():
    """Sidebar rows should not expose absolute uploaded image paths in titles."""
    sessions_src = (ROOT / "static" / "sessions.js").read_text(encoding="utf-8")

    assert "function _stripAttachedFilesMarker" in sessions_src
    assert "? _stripAttachedFilesMarker" in sessions_src
    assert "replace(/\\n\\n\\[Attached files: [^\\]]+\\]$/" in sessions_src


def test_server_provisional_titles_strip_attached_files_context():
    """Server-generated provisional titles must not include the path suffix."""
    from api.models import title_from

    title = title_from([
        {
            "role": "user",
            "content": "why is llm wiki not working?\n\n[Attached files: /tmp/private/Screenshot.png]",
        }
    ])

    assert title == "why is llm wiki not working?"
    assert "Attached files" not in title
    assert "/tmp/private" not in title


def test_duplicate_upload_response_reports_actual_stored_filename(tmp_path, monkeypatch):
    """Duplicate upload names should report the suffixed stored basename."""
    monkeypatch.setenv("HERMES_WEBUI_ATTACHMENT_DIR", str(tmp_path))

    from api.upload import _sanitize_upload_name, _upload_destination

    safe_name = _sanitize_upload_name("photo.png")
    first = _upload_destination("session-a", safe_name)
    first.write_bytes(b"first")
    second = _upload_destination("session-a", safe_name)

    assert first.name == "photo.png"
    assert second.name == "photo-1.png"

    src = UPLOAD_PY.read_text(encoding="utf-8")
    handle_body = src[src.index("def handle_upload"):src.index("def extract_archive", src.index("def handle_upload"))]
    assert "'filename': dest.name" in handle_body
    assert "'filename': safe_name" not in handle_body


def test_upload_response_includes_attachment_id_and_sha256(tmp_path, monkeypatch):
    """Upload response must expose a stable attachment_id plus a sha256 hash
    so capability-scoped agents (no terminal/file tools) can look up and
    verify an attachment without a bare filesystem path."""
    monkeypatch.setenv("HERMES_WEBUI_ATTACHMENT_DIR", str(tmp_path))

    import hashlib

    from api.upload import _record_attachment_manifest, _session_attachment_dir, _upload_destination

    safe_name = "notes.md"
    dest = _upload_destination("session-b", safe_name)
    file_bytes = b"# hello\nworld\n"
    dest.write_bytes(file_bytes)

    expected_sha256 = hashlib.sha256(file_bytes).hexdigest()
    expected_id = hashlib.sha256(f"session-b:{dest.name}".encode()).hexdigest()[:16]

    _record_attachment_manifest(
        session_id="session-b",
        attachment_id=expected_id,
        original_name=safe_name,
        stored_name=dest.name,
        mime="text/markdown",
        size=dest.stat().st_size,
        sha256=expected_sha256,
    )

    manifest_path = _session_attachment_dir("session-b") / ".manifest.json"
    assert manifest_path.exists()

    import json

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert len(manifest) == 1
    entry = manifest[0]
    assert entry["id"] == expected_id
    assert entry["sha256"] == expected_sha256
    assert entry["stored_name"] == dest.name
    assert entry["original_name"] == safe_name
    assert entry["mime"] == "text/markdown"


def test_upload_manifest_id_is_stable_across_two_writes(tmp_path, monkeypatch):
    """Re-recording the same attachment_id must update, not duplicate, the
    manifest entry (idempotent by id)."""
    monkeypatch.setenv("HERMES_WEBUI_ATTACHMENT_DIR", str(tmp_path))

    from api.upload import _record_attachment_manifest, _session_attachment_dir

    for size in (10, 20):
        _record_attachment_manifest(
            session_id="session-c",
            attachment_id="fixedid1234567890",
            original_name="a.txt",
            stored_name="a.txt",
            mime="text/plain",
            size=size,
            sha256="deadbeef",
        )

    import json

    manifest_path = _session_attachment_dir("session-c") / ".manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert len(manifest) == 1
    assert manifest[0]["size"] == 20


def test_messages_js_includes_attachment_id_in_composed_context():
    """Composed chat text for uploads-without-typed-text must surface
    attachment_id/mime/size/sha256, not just a bare filesystem path."""
    src = MESSAGES_JS.read_text(encoding="utf-8")

    assert "attachment_id: ${u.id}" in src
    assert "sha256: ${u.sha256}" in src


def test_ui_js_upload_response_propagates_id_and_sha256():
    """uploadPendingFiles() must forward the server's id/sha256 fields into
    the per-file object consumed by messages.js, not silently drop them."""
    ui_src = (ROOT / "static" / "ui.js").read_text(encoding="utf-8")

    assert "id: data.id, sha256: data.sha256" in ui_src
