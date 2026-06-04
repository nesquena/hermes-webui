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
    assert "dataset.rawText=String(displayContent).trim()" in ui_src


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
