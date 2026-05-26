"""Regression coverage for WebUI chat upload path handoff."""
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MESSAGES_JS = ROOT / "static" / "messages.js"


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
