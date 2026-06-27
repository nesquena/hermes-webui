from pathlib import Path
from tempfile import TemporaryDirectory

from api.routes import _normalize_chat_attachments
from api.streaming import _build_native_multimodal_message


REPO_ROOT = Path(__file__).resolve().parent.parent


def test_frontend_does_not_append_internal_attachment_paths_to_user_text():
    """Uploaded files should travel as structured attachments, not leaked paths in text."""
    src = (REPO_ROOT / "static" / "messages.js").read_text(encoding="utf-8")

    assert "[Attached files:" not in src
    assert "attachments:uploaded.length?uploaded:undefined" in src


def test_backend_adds_agent_only_hint_for_non_image_file_attachments():
    """If frontend no longer injects paths, backend must still tell the agent about files."""
    with TemporaryDirectory() as d:
        root = Path(d)
        doc = root / "notes.txt"
        doc.write_text("hello", encoding="utf-8")
        attachments = _normalize_chat_attachments([
            {
                "name": "notes.txt",
                "path": str(doc),
                "mime": "text/plain",
                "size": doc.stat().st_size,
                "is_image": False,
            }
        ])

        result = _build_native_multimodal_message("[WS]\n", "please inspect", attachments, str(root))

    assert isinstance(result, str)
    assert "please inspect" in result
    assert f"[Attached file available at: {doc}]" in result
    assert "read_file" in result


def test_backend_allows_webui_attachment_inbox_images_outside_workspace(monkeypatch, tmp_path):
    """Chat uploads live outside the selected workspace but must still reach the agent.

    The UI no longer leaks ``[Attached files: ...]`` into user text, so the
    backend must explicitly allow the configured WebUI attachment inbox as a
    safe attachment root. Otherwise screenshots uploaded to
    ``/opt/data/webui/attachments/<session>/...`` are silently dropped.
    """
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    inbox = tmp_path / "webui-attachments"
    session_dir = inbox / "session-1"
    session_dir.mkdir(parents=True)
    image = session_dir / "shot.png"
    image.write_bytes(
        b"\x89PNG\r\n\x1a\n"
        b"\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x02\x00\x00\x00\x90wS\xde"
        b"\x00\x00\x00\x0bIDATx\x9cc\xf8\x0f\x00\x00\x01\x01\x00\x05\x18\xd8N"
        b"\x00\x00\x00\x00IEND\xaeB`\x82"
    )
    monkeypatch.setenv("HERMES_WEBUI_ATTACHMENT_DIR", str(inbox))

    attachments = _normalize_chat_attachments([
        {
            "name": "shot.png",
            "path": str(image),
            "mime": "image/png",
            "size": image.stat().st_size,
            "is_image": True,
        }
    ])

    result = _build_native_multimodal_message(
        "[WS]\n",
        "what is this?",
        attachments,
        str(workspace),
        cfg={"agent": {"image_input_mode": "text"}},
    )

    assert isinstance(result, str)
    assert "what is this?" in result
    assert f"[Image attached at: {image}]" in result
    assert f"vision_analyze with image_url: {image}" in result
