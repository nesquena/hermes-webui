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

        # Verify file actually exists in the workspace
        uploaded = ws / "hello.txt"
        assert uploaded.exists(), f"File not found at {uploaded}"
        assert uploaded.read_bytes() == content

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
        except (urllib.error.URLError, ConnectionResetError, BrokenPipeError):
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
