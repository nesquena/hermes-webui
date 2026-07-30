"""PR #2520: archive extraction respects HERMES_WEBUI_ATTACHMENT_DIR.

Verifies that extract_archive() lands files in the per-session attachment
inbox when HERMES_WEBUI_ATTACHMENT_DIR is set, matching the single-file
upload path and ensuring session cleanup covers extracted archives.
"""
import io
import shutil
import zipfile
from pathlib import Path

import pytest

from api.upload import extract_archive, _session_attachment_dir


def _make_zip(members: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, data in members.items():
            zf.writestr(name, data)
    return buf.getvalue()


class TestExtractArchiveAttachmentDir:

    def test_extraction_lands_in_session_dir(self, tmp_path, monkeypatch):
        inbox = tmp_path / "att-inbox"
        monkeypatch.setenv("HERMES_WEBUI_ATTACHMENT_DIR", str(inbox))

        session_id = "sess-42"
        session_dir = _session_attachment_dir(session_id)
        session_dir.mkdir(parents=True, exist_ok=True)

        zip_bytes = _make_zip({
            "hello.txt": b"Hello, world!",
            "sub/nested.txt": b"Nested file",
        })

        result = extract_archive(zip_bytes, "demo.zip", session_dir)

        assert result["extracted"] == 2
        dest = Path(result["dest"])
        assert dest.is_relative_to(session_dir)
        assert dest.name == "demo"
        assert (dest / "hello.txt").read_text() == "Hello, world!"
        assert (dest / "sub" / "nested.txt").read_text() == "Nested file"

    def test_session_cleanup_covers_extracted_archives(self, tmp_path, monkeypatch):
        inbox = tmp_path / "att-inbox"
        monkeypatch.setenv("HERMES_WEBUI_ATTACHMENT_DIR", str(inbox))

        session_id = "sess-cleanup"
        session_dir = _session_attachment_dir(session_id)
        session_dir.mkdir(parents=True, exist_ok=True)

        zip_bytes = _make_zip({"a.txt": b"data"})
        result = extract_archive(zip_bytes, "pkg.zip", session_dir)
        dest = Path(result["dest"])
        assert dest.exists()

        shutil.rmtree(session_dir, ignore_errors=True)
        assert not dest.exists()
        assert not session_dir.exists()

    def test_extraction_not_at_bare_attachment_root(self, tmp_path, monkeypatch):
        inbox = tmp_path / "att-inbox"
        monkeypatch.setenv("HERMES_WEBUI_ATTACHMENT_DIR", str(inbox))

        session_id = "sess-scoped"
        session_dir = _session_attachment_dir(session_id)
        session_dir.mkdir(parents=True, exist_ok=True)

        zip_bytes = _make_zip({"file.txt": b"content"})
        result = extract_archive(zip_bytes, "archive.zip", session_dir)
        dest = Path(result["dest"])

        assert dest.parent == session_dir
        assert dest.parent != inbox.resolve()

    def test_relative_files_are_relative_to_session_dir(self, tmp_path, monkeypatch):
        inbox = tmp_path / "att-inbox"
        monkeypatch.setenv("HERMES_WEBUI_ATTACHMENT_DIR", str(inbox))

        session_dir = _session_attachment_dir("sess-rel")
        session_dir.mkdir(parents=True, exist_ok=True)

        zip_bytes = _make_zip({"doc.md": b"# Title"})
        result = extract_archive(zip_bytes, "docs.zip", session_dir)

        assert len(result["files"]) == 1
        rel = result["files"][0]
        assert rel == "docs/doc.md"
        assert (session_dir / rel).exists()
