"""
PR #3104: workspace upload endpoint tests.

Covers the POST /api/workspace/upload handler:
  - happy-path upload into workspace
  - filename dedup (-1/-2 suffixes)
  - path-traversal blocking (../ filename → 403)
  - oversized body rejection (413)
  - archive extraction containment (no member escapes workspace)
  - zip-bomb cap (extraction rejects when total extracted > limit)
"""

import io
import json
import sys
import uuid
import urllib.request
import urllib.error
import pathlib
import zipfile
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).parent.parent.parent))

from tests._pytest_port import BASE


# ── HTTP helpers (mirrored from test_sprint1.py) ──────────────────────────

def get(path):
    url = BASE + path
    with urllib.request.urlopen(url, timeout=10) as r:
        return json.loads(r.read())


def post(path, body=None):
    url = BASE + path
    data = json.dumps(body or {}).encode()
    req = urllib.request.Request(url, data=data,
          headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read()), r.status
    except urllib.error.HTTPError as e:
        return json.loads(e.read()), e.code


def post_multipart(path, fields, files):
    """Post a multipart/form-data request. files: {name: (filename, bytes)}"""
    boundary = uuid.uuid4().hex.encode()
    body = b""
    for name, value in fields.items():
        body += b"--" + boundary + b"\r\n"
        body += f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode()
        body += value.encode() + b"\r\n"
    for name, (filename, data) in files.items():
        body += b"--" + boundary + b"\r\n"
        body += f'Content-Disposition: form-data; name="{name}"; filename="{filename}"\r\n'.encode()
        body += b"Content-Type: application/octet-stream\r\n\r\n"
        body += data + b"\r\n"
    body += b"--" + boundary + b"--\r\n"
    req = urllib.request.Request(BASE + path, data=body,
          headers={"Content-Type": f"multipart/form-data; boundary={boundary.decode()}"})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read()), r.status
    except urllib.error.HTTPError as e:
        return json.loads(e.read()), e.code


def make_session_tracked(created_list, ws=None):
    """Create a session and register it with the cleanup fixture."""
    body = {}
    if ws:
        body["workspace"] = str(ws)
    d, _ = post("/api/session/new", body)
    sid = d["session"]["session_id"]
    created_list.append(sid)
    return sid, pathlib.Path(d["session"]["workspace"])


def _make_zip(members: dict[str, bytes]) -> bytes:
    """Create a zip archive in memory."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in members.items():
            zf.writestr(name, data)
    return buf.getvalue()


def _make_tar(members: dict[str, bytes], mode: str = "w") -> bytes:
    """Create a tar archive in memory (mode 'w' = uncompressed .tar)."""
    import tarfile
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode=mode) as tf:
        for name, data in members.items():
            info = tarfile.TarInfo(name=name)
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
    return buf.getvalue()


class _FakeUploadHandler:
    def __init__(self, content_length: int = 0):
        self.status = None
        self.sent_headers = []
        self.rfile = io.BytesIO()
        self.wfile = io.BytesIO()
        self.headers = {
            "Content-Type": "multipart/form-data; boundary=test",
            "Content-Length": str(content_length),
        }

    def send_response(self, status):
        self.status = status

    def send_header(self, name, value):
        self.sent_headers.append((name, value))

    def end_headers(self):
        pass

    def json_body(self):
        return json.loads(self.wfile.getvalue().decode("utf-8"))


def _configure_direct_office_upload(monkeypatch, tmp_path):
    import api.office_documents as office_documents
    import api.upload as upload

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    session = SimpleNamespace(workspace=workspace, profile="default")

    monkeypatch.setattr(upload, "get_session", lambda _sid: session)
    monkeypatch.setattr(upload, "_reject_invisible_session", lambda *_args: False)
    monkeypatch.setattr(upload, "resolve_trusted_workspace", lambda path: path)
    return upload, office_documents, workspace


def _set_office_upload_payload(monkeypatch, upload, filename, file_bytes):
    monkeypatch.setattr(
        upload,
        "parse_multipart",
        lambda *_args, **_kwargs: (
            {"session_id": "session-1", "path": ""},
            {"file": (filename, file_bytes)},
        ),
    )


def _set_office_preview(monkeypatch, office_documents, *, content, preview_kind, office_format):
    monkeypatch.setattr(
        office_documents,
        "preview_office_document",
        lambda path, raw: {
            "path": path,
            "content": content,
            "preview_kind": preview_kind,
            "office_format": office_format,
            "size": len(raw),
        },
    )


class _FailingSidecarWriter:
    def __init__(self, inner):
        self._inner = inner

    def write(self, _data):
        raise OSError("sidecar write failed")

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self._inner.close()
        return False


# ── Health check ──────────────────────────────────────────────────────────

def test_health():
    """Server must be running and healthy."""
    data = get("/health")
    assert data["status"] == "ok", f"health not ok: {data}"


# ── Workspace upload tests ────────────────────────────────────────────────

class TestWorkspaceUploadHappyPath:

    def test_upload_single_file(self, cleanup_test_sessions):
        """Happy path: upload a file into the workspace root."""
        sid, ws = make_session_tracked(cleanup_test_sessions)

        content = b"hello workspace"
        result, status = post_multipart(
            "/api/workspace/upload",
            {"session_id": sid, "path": ""},
            {"file": ("hello.txt", content)},
        )

        assert status == 200, f"Upload failed {status}: {result}"
        assert result["filename"] == "hello.txt"
        assert result["size"] == len(content)
        assert result["extracted"] is False
        assert "sidecar" not in result
        assert "sidecar_error" not in result

        # Verify file actually exists in the workspace
        uploaded = ws / "hello.txt"
        assert uploaded.exists(), f"File not found at {uploaded}"
        assert uploaded.read_bytes() == content

    def test_upload_docx_writes_sidecar_and_metadata(self, monkeypatch, tmp_path):
        """Office uploads should write a Markdown sidecar and surface it."""
        docx_bytes = b"fake-docx-bytes"

        upload, office_documents, workspace = _configure_direct_office_upload(monkeypatch, tmp_path)
        _set_office_upload_payload(monkeypatch, upload, "report.docx", docx_bytes)
        _set_office_preview(
            monkeypatch,
            office_documents,
            content="# Preview\n\nOffice upload",
            preview_kind="office-preview",
            office_format="docx",
        )

        handler = _FakeUploadHandler(content_length=len(docx_bytes))
        upload.handle_workspace_upload(handler)

        assert handler.status == 200
        result = handler.json_body()
        assert result["filename"] == "report.docx"
        assert result["sidecar"]["filename"] == "report.docx.md"
        assert result["sidecar"]["preview_kind"] == "office-preview"
        assert result["sidecar"]["office_format"] == "docx"

        uploaded = workspace / "report.docx"
        sidecar = workspace / "report.docx.md"
        assert uploaded.exists()
        assert uploaded.read_bytes() == docx_bytes
        assert sidecar.exists()
        assert sidecar.read_text(encoding="utf-8") == "# Preview\n\nOffice upload"

    def test_upload_docx_preview_failure_returns_generic_sidecar_error(self, monkeypatch, tmp_path):
        """Preview failures should not leak raw errors or paths."""
        docx_bytes = b"fake-docx-bytes"

        upload, office_documents, workspace = _configure_direct_office_upload(monkeypatch, tmp_path)
        _set_office_upload_payload(monkeypatch, upload, "report.docx", docx_bytes)

        def _raise_preview(_path, _raw):
            raise ValueError(f"preview failed for {workspace}")

        monkeypatch.setattr(office_documents, "preview_office_document", _raise_preview)

        handler = _FakeUploadHandler(content_length=len(docx_bytes))
        upload.handle_workspace_upload(handler)

        assert handler.status == 200
        result = handler.json_body()
        assert result["sidecar_error"] == "Office sidecar extraction failed"
        assert str(workspace) not in result["sidecar_error"]
        assert "preview failed" not in result["sidecar_error"]

        uploaded = workspace / "report.docx"
        sidecar = workspace / "report.docx.md"
        assert uploaded.exists()
        assert uploaded.read_bytes() == docx_bytes
        assert not sidecar.exists()

    @pytest.mark.parametrize(
        "filename, file_bytes, content, office_format",
        [
            ("report.xlsx", b"fake-xlsx-bytes", "# Preview\n\nWorkbook", "xlsx"),
            ("slides.pptx", b"fake-pptx-bytes", "# Preview\n\nDeck", "pptx"),
        ],
    )
    def test_upload_office_xlsx_and_pptx_write_sidecars(
        self,
        monkeypatch,
        tmp_path,
        filename,
        file_bytes,
        content,
        office_format,
    ):
        upload, office_documents, workspace = _configure_direct_office_upload(monkeypatch, tmp_path)
        _set_office_upload_payload(monkeypatch, upload, filename, file_bytes)
        _set_office_preview(
            monkeypatch,
            office_documents,
            content=content,
            preview_kind="office-preview",
            office_format=office_format,
        )

        handler = _FakeUploadHandler(content_length=len(file_bytes))
        upload.handle_workspace_upload(handler)

        assert handler.status == 200
        result = handler.json_body()
        assert result["filename"] == filename
        assert result["sidecar"]["filename"] == f"{filename}.md"
        assert result["sidecar"]["preview_kind"] == "office-preview"
        assert result["sidecar"]["office_format"] == office_format

        uploaded = workspace / filename
        sidecar = workspace / f"{filename}.md"
        assert uploaded.exists()
        assert uploaded.read_bytes() == file_bytes
        assert sidecar.exists()
        assert sidecar.read_text(encoding="utf-8") == content

    def test_upload_docx_sidecar_write_failure_cleans_up_and_keeps_upload_success(self, monkeypatch, tmp_path):
        """Sidecar write failures must not fail the original upload."""
        docx_bytes = b"fake-docx-bytes"

        upload, office_documents, workspace = _configure_direct_office_upload(monkeypatch, tmp_path)
        _set_office_upload_payload(monkeypatch, upload, "report.docx", docx_bytes)
        _set_office_preview(
            monkeypatch,
            office_documents,
            content="# Preview\n\nOffice upload",
            preview_kind="office-preview",
            office_format="docx",
        )

        real_fdopen = upload.os.fdopen
        fdopen_calls = {"count": 0}

        def _failing_fdopen(fd, *args, **kwargs):
            fdopen_calls["count"] += 1
            writer = real_fdopen(fd, *args, **kwargs)
            if fdopen_calls["count"] == 2:
                return _FailingSidecarWriter(writer)
            return writer

        monkeypatch.setattr(upload.os, "fdopen", _failing_fdopen)

        handler = _FakeUploadHandler(content_length=len(docx_bytes))
        upload.handle_workspace_upload(handler)

        assert handler.status == 200
        result = handler.json_body()
        assert result["filename"] == "report.docx"
        assert result["sidecar_error"] == "Office sidecar extraction failed"
        assert "sidecar write failed" not in result["sidecar_error"]
        assert "sidecar" not in result

        uploaded = workspace / "report.docx"
        sidecar = workspace / "report.docx.md"
        assert uploaded.exists()
        assert uploaded.read_bytes() == docx_bytes
        assert not sidecar.exists()

    def test_upload_docx_sidecar_collision_preserves_existing_sidecar(self, monkeypatch, tmp_path):
        """A pre-existing Markdown sidecar must survive a create collision."""
        docx_bytes = b"fake-docx-bytes"

        upload, office_documents, workspace = _configure_direct_office_upload(monkeypatch, tmp_path)
        _set_office_upload_payload(monkeypatch, upload, "report.docx", docx_bytes)
        _set_office_preview(
            monkeypatch,
            office_documents,
            content="# Preview\n\nOffice upload",
            preview_kind="office-preview",
            office_format="docx",
        )

        sidecar = workspace / "report.docx.md"
        sidecar.write_text("keep this", encoding="utf-8")

        handler = _FakeUploadHandler(content_length=len(docx_bytes))
        upload.handle_workspace_upload(handler)

        assert handler.status == 200
        result = handler.json_body()
        assert result["filename"] == "report.docx"
        assert "sidecar" not in result
        assert result["sidecar_error"] == "Office sidecar extraction failed"
        assert "report.docx.md" not in result["sidecar_error"]

        uploaded = workspace / "report.docx"
        assert uploaded.exists()
        assert uploaded.read_bytes() == docx_bytes
        assert sidecar.read_text(encoding="utf-8") == "keep this"

    def test_upload_docx_duplicate_names_get_deduped_sidecars(self, monkeypatch, tmp_path):
        """Repeated Office uploads should dedupe both the file and its sidecar."""
        upload, office_documents, workspace = _configure_direct_office_upload(monkeypatch, tmp_path)
        office_uploads = iter([b"first office payload", b"second office payload"])
        preview_paths = []

        def _next_upload(*_args, **_kwargs):
            payload = next(office_uploads)
            return (
                {"session_id": "session-1", "path": ""},
                {"file": ("report.docx", payload)},
            )

        def _preview(path, raw):
            preview_paths.append(path)
            return {
                "path": path,
                "content": f"# {path}\n\n{raw.decode('utf-8')}",
                "preview_kind": "office-preview",
                "office_format": "docx",
                "size": len(raw),
            }

        monkeypatch.setattr(upload, "parse_multipart", _next_upload)
        monkeypatch.setattr(office_documents, "preview_office_document", _preview)

        handler1 = _FakeUploadHandler(content_length=len(b"first office payload"))
        upload.handle_workspace_upload(handler1)
        handler2 = _FakeUploadHandler(content_length=len(b"second office payload"))
        upload.handle_workspace_upload(handler2)

        assert handler1.status == 200
        assert handler2.status == 200
        result1 = handler1.json_body()
        result2 = handler2.json_body()
        assert result1["filename"] == "report.docx"
        assert result1["sidecar"]["filename"] == "report.docx.md"
        assert result2["filename"] == "report-1.docx"
        assert result2["sidecar"]["filename"] == "report-1.docx.md"
        assert preview_paths == ["report.docx", "report-1.docx"]

        assert (workspace / "report.docx").read_bytes() == b"first office payload"
        assert (workspace / "report.docx.md").read_text(encoding="utf-8") == "# report.docx\n\nfirst office payload"
        assert (workspace / "report-1.docx").read_bytes() == b"second office payload"
        assert (workspace / "report-1.docx.md").read_text(encoding="utf-8") == "# report-1.docx\n\nsecond office payload"

    def test_upload_into_subdirectory(self, cleanup_test_sessions):
        """Upload a file into a subdirectory within the workspace."""
        sid, ws = make_session_tracked(cleanup_test_sessions)

        content = b"nested file"
        result, status = post_multipart(
            "/api/workspace/upload",
            {"session_id": sid, "path": "sub/dir"},
            {"file": ("nested.txt", content)},
        )

        assert status == 200, f"Upload failed {status}: {result}"
        assert result["filename"] == "nested.txt"

        uploaded = ws / "sub" / "dir" / "nested.txt"
        assert uploaded.exists(), f"File not found at {uploaded}"
        assert uploaded.read_bytes() == content

    def test_upload_image_mime_is_flagged(self, cleanup_test_sessions):
        """Image uploads should have is_image=True."""
        sid, ws = make_session_tracked(cleanup_test_sessions)

        # Minimal valid PNG bytes
        png = (
            b"\x89PNG\r\n\x1a\n"  # PNG signature
            b"\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
            b"\x08\x02\x00\x00\x00\x90wS\xde"
            b"\x00\x00\x00\x0cIDATx\x9cc\xf8\x0f\x00\x00\x01\x01\x00\x05\x18\xd8N"
            b"\x00\x00\x00\x00IEND\xaeB`\x82"
        )

        result, status = post_multipart(
            "/api/workspace/upload",
            {"session_id": sid, "path": ""},
            {"file": ("icon.png", png)},
        )

        assert status == 200, f"Upload failed {status}: {result}"
        assert result["is_image"] is True
        assert result["mime"] == "image/png"


class TestWorkspaceUploadDedup:

    def test_same_filename_produces_suffix(self, cleanup_test_sessions):
        """Uploading the same filename twice produces -1 suffix on the second."""
        sid, ws = make_session_tracked(cleanup_test_sessions)

        # First upload
        result1, status1 = post_multipart(
            "/api/workspace/upload",
            {"session_id": sid, "path": ""},
            {"file": ("report.pdf", b"first")},
        )
        assert status1 == 200

        # Second upload — same filename
        result2, status2 = post_multipart(
            "/api/workspace/upload",
            {"session_id": sid, "path": ""},
            {"file": ("report.pdf", b"second")},
        )
        assert status2 == 200

        # Second file should have -1 suffix
        assert result2["filename"] == "report-1.pdf"

        # Both files should exist with correct content
        assert (ws / "report.pdf").read_bytes() == b"first"
        assert (ws / "report-1.pdf").read_bytes() == b"second"

    def test_multiple_duplicates_increment(self, cleanup_test_sessions):
        """Three uploads of same name produce -1 and -2 suffixes."""
        sid, ws = make_session_tracked(cleanup_test_sessions)

        for i, expected_name in enumerate(["file.txt", "file-1.txt", "file-2.txt"]):
            result, status = post_multipart(
                "/api/workspace/upload",
                {"session_id": sid, "path": ""},
                {"file": ("file.txt", f"content {i}".encode())},
            )
            assert status == 200, f"Upload {i} failed {status}: {result}"
            assert result["filename"] == expected_name
            assert (ws / expected_name).exists()


class TestWorkspaceUploadPathTraversal:

    def test_dotdot_filename_blocked(self, cleanup_test_sessions):
        """Filename containing ../ should be sanitized, not traverse."""
        sid, ws = make_session_tracked(cleanup_test_sessions)

        result, status = post_multipart(
            "/api/workspace/upload",
            {"session_id": sid, "path": ""},
            {"file": ("../outside.txt", b"escape attempt")},
        )

        # The sanitizer converts ../ to ___ so this should succeed but with
        # sanitized name. The real traversal test is the subpath parameter.
        assert status == 200, f"Unexpected status {status}: {result}"
        assert ".." not in result["filename"]
        # File should be inside workspace
        uploaded = ws / result["filename"]
        assert uploaded.exists()
        assert uploaded.is_relative_to(ws.resolve())

    def test_traversal_via_subpath_blocked(self, cleanup_test_sessions):
        """Subpath with ../../etc should be blocked with 400."""
        sid, ws = make_session_tracked(cleanup_test_sessions)

        result, status = post_multipart(
            "/api/workspace/upload",
            {"session_id": sid, "path": "../../etc"},
            {"file": ("safe.txt", b"safe")},
        )

        # safe_resolve_ws raises ValueError on traversal → caught as 400
        assert status == 400, f"Expected 400, got {status}: {result}"
        assert "error" in result

    def test_traversal_via_subpath_deep(self, cleanup_test_sessions):
        """Subpath with .. buried inside should also be blocked."""
        sid, ws = make_session_tracked(cleanup_test_sessions)

        result, status = post_multipart(
            "/api/workspace/upload",
            {"session_id": sid, "path": "projects/../../etc/passwd"},
            {"file": ("safe.txt", b"safe")},
        )

        assert status == 400, f"Expected 400, got {status}: {result}"
        assert "error" in result


class TestWorkspaceUploadOversized:

    def test_oversized_file_gets_413(self, cleanup_test_sessions):
        """File over MAX_UPLOAD_BYTES should be rejected with 413."""
        from api.config import MAX_UPLOAD_BYTES

        sid, ws = make_session_tracked(cleanup_test_sessions)

        big = b"x" * (MAX_UPLOAD_BYTES + 1024)  # slightly over limit
        try:
            result, status = post_multipart(
                "/api/workspace/upload",
                {"session_id": sid, "path": ""},
                {"file": ("big.bin", big)},
            )
            assert status == 413, f"Expected 413, got {status}: {result}"
        except (urllib.error.URLError, ConnectionResetError, ConnectionAbortedError, BrokenPipeError):
            # Server may close connection after reading Content-Length > limit
            pass


class TestWorkspaceUploadArchive:

    def test_zip_extracts_into_subdirectory(self, cleanup_test_sessions):
        """Zip dropped into subdir/ should extract under subdir/, not workspace root."""
        sid, ws = make_session_tracked(cleanup_test_sessions)

        zip_data = _make_zip({
            "readme.md": b"# Project",
            "src/main.py": b"print('hello')",
        })

        result, status = post_multipart(
            "/api/workspace/upload",
            {"session_id": sid, "path": "projects"},
            {"file": ("vendor.zip", zip_data)},
        )

        assert status == 200, f"Upload failed {status}: {result}"
        assert result["extracted"] is True
        assert result["extracted_count"] == 2

        # Extraction should land under projects/vendor/
        extract_dir = ws / "projects" / "vendor"
        assert extract_dir.is_dir(), f"Extraction dir not found at {extract_dir}"
        assert (extract_dir / "readme.md").read_text() == "# Project"
        assert (extract_dir / "src" / "main.py").read_text() == "print('hello')"

        # Archive file itself should be removed after extraction
        assert not (ws / "projects" / "vendor.zip").exists()
        assert "sidecar" not in result
        assert "sidecar_error" not in result

    def test_zip_extracts_to_workspace_root_when_no_subpath(self, cleanup_test_sessions):
        """Zip uploaded without subpath extracts to workspace root."""
        sid, ws = make_session_tracked(cleanup_test_sessions)

        zip_data = _make_zip({"notes.txt": b"workspace notes"})

        result, status = post_multipart(
            "/api/workspace/upload",
            {"session_id": sid, "path": ""},
            {"file": ("notes.zip", zip_data)},
        )

        assert status == 200, f"Upload failed {status}: {result}"
        assert result["extracted"] is True

        extract_dir = ws / "notes"
        assert extract_dir.is_dir()
        assert (extract_dir / "notes.txt").read_text() == "workspace notes"

    def test_zip_slip_blocked(self, cleanup_test_sessions):
        """Zip member with ../ path should be blocked (zip-slip protection)."""
        sid, ws = make_session_tracked(cleanup_test_sessions)

        # Create a zip with a malicious member path
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            # Add a member that tries to escape the extraction directory
            info = zipfile.ZipInfo("../escape.txt")
            zf.writestr(info, b"escaped")

        result, status = post_multipart(
            "/api/workspace/upload",
            {"session_id": sid, "path": ""},
            {"file": ("evil.zip", buf.getvalue())},
        )

        # Extraction should fail (zip-slip blocked)
        assert status == 200, f"Upload failed {status}: {result}"
        assert result["extracted"] is False
        assert "extract_error" in result

        # No file should have escaped the workspace
        assert not (ws.parent / "escape.txt").exists()

    def test_corrupt_zip_surfaces_error(self, cleanup_test_sessions):
        """A corrupt zip should be rejected with an error surfaced to the frontend."""
        sid, ws = make_session_tracked(cleanup_test_sessions)

        result, status = post_multipart(
            "/api/workspace/upload",
            {"session_id": sid, "path": ""},
            {"file": ("corrupt.zip", b"this is not a zip file at all")},
        )

        assert status == 200, f"Upload failed {status}: {result}"
        assert result["extracted"] is False
        assert "extract_error" in result

        # Corrupt archive file should be removed
        assert not (ws / "corrupt.zip").exists()

    def test_zip_bomb_cap_trips(self, cleanup_test_sessions):
        """When extraction exceeds the cap, it should be rejected and cleaned up.

        The test server runs with HERMES_WEBUI_MAX_EXTRACTED_MB=5 (set in
        conftest), so a highly-compressible archive that extracts to >5MB trips
        the byte-tracking zip-bomb guard. (Monkeypatching the cap in the pytest
        process does nothing — extraction runs in the out-of-process server.)
        """
        sid, ws = make_session_tracked(cleanup_test_sessions)

        from api.config import MAX_UPLOAD_BYTES

        # ~6.4MB of zeros across two members — compresses to a tiny zip but
        # exceeds the 5MB extraction cap during the chunked write.
        zip_data = _make_zip({
            "a.bin": b"\0" * (4 * 1024 * 1024),  # 4MB — under cap
            "b.bin": b"\0" * (4 * 1024 * 1024),  # +4MB = 8MB — exceeds 5MB cap mid-extraction
        })
        # Sanity: the compressed archive itself must stay under the upload cap.
        assert len(zip_data) < MAX_UPLOAD_BYTES, f"test zip too big to upload: {len(zip_data)}"

        result, status = post_multipart(
            "/api/workspace/upload",
            {"session_id": sid, "path": ""},
            {"file": ("bomb.zip", zip_data)},
        )

        assert status == 200, f"Upload failed {status}: {result}"
        assert result["extracted"] is False
        assert "extract_error" in result

        # Archive should be removed on failure
        assert not (ws / "bomb.zip").exists()
        # No partial extraction directory left behind
        assert not (ws / "bomb").exists()

    def test_archive_member_count_cap_trips(self, cleanup_test_sessions):
        """An archive with too many members is rejected (inode-exhaustion guard).

        The member cap (_MAX_ARCHIVE_MEMBERS = 10000) trips before the byte cap
        when an archive packs a huge number of tiny files. Verifies the archive
        and any partial extraction are cleaned up.
        """
        sid, ws = make_session_tracked(cleanup_test_sessions)

        # 10001 one-byte members — under the 5MB byte cap, over the 10k member cap.
        members = {f"f{i}.txt": b"x" for i in range(10001)}
        zip_data = _make_zip(members)

        result, status = post_multipart(
            "/api/workspace/upload",
            {"session_id": sid, "path": ""},
            {"file": ("many.zip", zip_data)},
        )

        assert status == 200, f"Upload failed {status}: {result}"
        assert result["extracted"] is False
        assert "extract_error" in result
        assert not (ws / "many.zip").exists()
        assert not (ws / "many").exists()


# ── Hardening regression tests (v0.51.208 hotfix) ──────────────────────────

def test_parse_multipart_rejects_negative_content_length():
    """Negative Content-Length must not reach rfile.read(<0) (unbounded read).

    The per-handler `content_length > MAX_UPLOAD_BYTES` gate is False for a
    negative value, so the guard has to live in parse_multipart itself (the
    shared chokepoint for every upload handler).
    """
    from api.upload import parse_multipart
    big = b"x" * (2 * 1024 * 1024)
    rfile = io.BytesIO(
        b"--b\r\nContent-Disposition: form-data; name=\"f\"\r\n\r\n" + big + b"\r\n--b--\r\n"
    )
    try:
        parse_multipart(rfile, "multipart/form-data; boundary=b", -1)
        assert False, "negative Content-Length should have been rejected"
    except ValueError as e:
        assert "Content-Length" in str(e)
    # The stream must not have been drained by an unbounded read.
    assert rfile.tell() == 0


def test_parse_multipart_rejects_oversize_content_length():
    from api.config import MAX_UPLOAD_BYTES
    from api.upload import parse_multipart
    rfile = io.BytesIO(b"ignored")
    try:
        parse_multipart(rfile, "multipart/form-data; boundary=b", MAX_UPLOAD_BYTES + 1)
        assert False, "oversize Content-Length should have been rejected"
    except ValueError as e:
        assert "too large" in str(e).lower()


class TestWorkspaceUploadArchiveSuffixes:
    def test_plain_tar_is_extracted(self, cleanup_test_sessions):
        """A `.tar` (and .tbz2/.txz) upload must extract, matching extract_archive's
        supported set — previously `is_archive` omitted them so they landed raw."""
        sid, ws = make_session_tracked(cleanup_test_sessions)
        tar_data = _make_tar({"docs/readme.txt": b"hello from tar"})
        result, status = post_multipart(
            "/api/workspace/upload",
            {"session_id": sid, "path": ""},
            {"file": ("bundle.tar", tar_data)},
        )
        assert status == 200, f"Upload failed {status}: {result}"
        assert result["extracted"] is True
        assert result.get("extracted_count", 0) >= 1
        # The raw .tar should have been removed after successful extraction.
        assert not (ws / "bundle.tar").exists()


class TestWorkspaceUploadSymlinkTarget:
    def test_symlink_subpath_target_is_rejected(self, cleanup_test_sessions):
        """An in-workspace symlink subdir pointing outside the workspace must not
        let the upload target (mkdir + writes) escape the workspace root."""
        import os
        sid, ws = make_session_tracked(cleanup_test_sessions)
        escape = ws.parent / f"escape-{uuid.uuid4().hex[:8]}"
        escape.mkdir(parents=True, exist_ok=True)
        link = ws / "outlink"
        try:
            try:
                os.symlink(str(escape), str(link))
            except (OSError, NotImplementedError):
                import pytest
                pytest.skip("symlinks not supported in this environment")
            result, status = post_multipart(
                "/api/workspace/upload",
                {"session_id": sid, "path": "outlink"},
                {"file": ("pwned.txt", b"should not land outside")},
            )
            # The escaping target must be rejected outright, and nothing may land
            # outside the workspace. Either a 403 (upload-handler symlink-target
            # rejection) or a 400 ("Path traversal blocked" from safe_resolve_ws,
            # which #3398 made the workspace boundary enforce consistently for all
            # symlink escapes) is an acceptable rejection — the invariant is that
            # the upload does NOT land outside the workspace.
            assert status in (400, 403), f"expected 400/403, got status={status} result={result}"
            assert not (escape / "pwned.txt").exists()
        finally:
            import shutil
            shutil.rmtree(escape, ignore_errors=True)
