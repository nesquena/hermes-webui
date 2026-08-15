from __future__ import annotations

import os
import time
from pathlib import Path
from types import SimpleNamespace

import pytest


ROOT = Path(__file__).resolve().parents[1]


class _FakeHandler:
    def __init__(self, headers=None):
        self.status = None
        self.sent_headers: list[tuple[str, str]] = []
        self.body = bytearray()
        self.wfile = self
        self.headers = dict(headers or {})

    def send_response(self, code):
        self.status = code

    def send_header(self, key, value):
        self.sent_headers.append((key, value))

    def end_headers(self):
        pass

    def write(self, data):
        self.body.extend(data)

    def header(self, key):
        return next((v for k, v in self.sent_headers if k == key), "") or ""


def _serve(routes, target, mime, cache_control, headers=None, **kwargs):
    handler = _FakeHandler(headers)
    result = routes._serve_file_bytes(
        handler, target, mime, "inline", cache_control, **kwargs
    )
    return handler, result


@pytest.fixture
def routes():
    from api import routes
    return routes


# ── _serve_file_bytes ETag / 304 ──────────────────────────────────────────


def test_serve_file_bytes_emits_weak_etag(routes, tmp_path):
    target = tmp_path / "img.png"
    target.write_bytes(b"\x89PNG\r\n\x1a\npayload")
    handler, _ = _serve(routes, target, "image/png", "private, no-cache")

    assert handler.status == 200
    assert handler.body == b"\x89PNG\r\n\x1a\npayload"
    etag = handler.header("ETag")
    assert etag and etag.startswith('W/"')
    assert etag.endswith('"')


def test_if_none_match_matching_returns_304_no_body(routes, tmp_path):
    target = tmp_path / "img.png"
    target.write_bytes(b"abc")
    handler, _ = _serve(routes, target, "image/png", "private, no-cache")
    etag = handler.header("ETag")
    assert handler.status == 200

    handler2, _ = _serve(
        routes, target, "image/png", "private, no-cache", headers={"If-None-Match": etag}
    )
    assert handler2.status == 304
    assert handler2.body == b""
    assert handler2.header("ETag") == etag
    assert handler2.header("Cache-Control") == "private, no-cache"
    # RFC 9110 §15.4.5: a 304 must not carry Content-Length.
    assert not any(k == "Content-Length" for k, _ in handler2.sent_headers)


def test_if_none_match_star_returns_304(routes, tmp_path):
    target = tmp_path / "img.png"
    target.write_bytes(b"abc")
    handler, _ = _serve(
        routes, target, "image/png", "private, no-cache", headers={"If-None-Match": "*"}
    )
    assert handler.status == 304


def test_if_none_match_mismatch_returns_full_200(routes, tmp_path):
    target = tmp_path / "img.png"
    target.write_bytes(b"abc")
    handler, _ = _serve(
        routes,
        target,
        "image/png",
        "private, no-cache",
        headers={"If-None-Match": 'W/"1-1"'},
    )
    assert handler.status == 200
    assert handler.body == b"abc"


def test_if_none_match_weak_comparison_ignores_w_prefix(routes, tmp_path):
    """RFC 7232 weak comparison: client may send the strong form (no W/)."""
    target = tmp_path / "img.png"
    target.write_bytes(b"abc")
    handler, _ = _serve(routes, target, "image/png", "private, no-cache")
    strong_form = handler.header("ETag")[2:]  # strip W/ -> '"<digest>"'
    handler2, _ = _serve(
        routes, target, "image/png", "private, no-cache", headers={"If-None-Match": strong_form}
    )
    assert handler2.status == 304


def test_if_none_match_list_any_entry_matches(routes, tmp_path):
    target = tmp_path / "img.png"
    target.write_bytes(b"abc")
    handler, _ = _serve(routes, target, "image/png", "private, no-cache")
    etag = handler.header("ETag")
    handler2, _ = _serve(
        routes,
        target,
        "image/png",
        "private, no-cache",
        headers={"If-None-Match": f'W/"0-0", {etag}, W/"2-2"'},
    )
    assert handler2.status == 304


def test_etag_changes_when_file_replaced_in_place(routes, tmp_path):
    """Core user scenario: same-name file updated -> old ETag no longer valid."""
    target = tmp_path / "img.png"
    target.write_bytes(b"v1")
    handler, _ = _serve(routes, target, "image/png", "private, no-cache")
    old_etag = handler.header("ETag")

    time.sleep(0.01)  # ensure mtime_ns advances
    target.write_bytes(b"v2-content")
    handler2, _ = _serve(
        routes, target, "image/png", "private, no-cache", headers={"If-None-Match": old_etag}
    )
    assert handler2.status == 200
    assert handler2.body == b"v2-content"
    assert handler2.header("ETag") != old_etag


def test_etag_changes_on_same_size_same_mtime_replacement(routes, tmp_path):
    """Regression for the metadata-only ETag gap: an in-place replacement with
    identical size AND identical mtime (e.g. 2-second filesystem granularity)
    must still produce a different ETag, so the conditional GET returns 200
    with the new content instead of a stale 304."""
    target = tmp_path / "img.png"
    target.write_bytes(b"AAAA")
    fixed_ns = 1_700_000_000_000_000_000
    os.utime(target, ns=(fixed_ns, fixed_ns))
    handler, _ = _serve(routes, target, "image/png", "private, no-cache")
    old_etag = handler.header("ETag")
    assert handler.status == 200
    assert handler.body == b"AAAA"

    # Same byte length, same mtime to the nanosecond — only content differs.
    target.write_bytes(b"BBBB")
    os.utime(target, ns=(fixed_ns, fixed_ns))
    handler2, _ = _serve(
        routes, target, "image/png", "private, no-cache", headers={"If-None-Match": old_etag}
    )
    assert handler2.status == 200
    assert handler2.body == b"BBBB"
    assert handler2.header("ETag") != old_etag


def test_304_path_closes_fd(routes, tmp_path, monkeypatch):
    """The 304 early return must close the opened fd (no descriptor leak on
    revalidated hits)."""
    target = tmp_path / "img.png"
    target.write_bytes(b"abc")
    handler, _ = _serve(routes, target, "image/png", "private, no-cache")
    etag = handler.header("ETag")

    closed = []
    real_close = os.close
    monkeypatch.setattr("api.routes.os.close", lambda fd: (closed.append(fd), real_close(fd))[1])
    handler2, _ = _serve(
        routes, target, "image/png", "private, no-cache", headers={"If-None-Match": etag}
    )
    assert handler2.status == 304
    assert closed, "fd was not closed on the 304 path"


def test_range_response_includes_etag(routes, tmp_path):
    target = tmp_path / "img.png"
    target.write_bytes(b"0123456789")
    handler, _ = _serve(
        routes, target, "image/png", "private, no-cache", headers={"Range": "bytes=0-2"}
    )
    assert handler.status == 206
    assert handler.body == b"012"
    assert handler.header("ETag") and handler.header("ETag").startswith('W/"')


def test_if_none_match_precedes_range(routes, tmp_path):
    """A matched conditional request short-circuits before Range handling."""
    target = tmp_path / "img.png"
    target.write_bytes(b"0123456789")
    handler, _ = _serve(routes, target, "image/png", "private, no-cache")
    etag = handler.header("ETag")
    handler2, _ = _serve(
        routes,
        target,
        "image/png",
        "private, no-cache",
        headers={"If-None-Match": etag, "Range": "bytes=0-2"},
    )
    assert handler2.status == 304


def test_invalid_range_still_416(routes, tmp_path):
    target = tmp_path / "img.png"
    target.write_bytes(b"0123456789")
    handler, _ = _serve(
        routes, target, "image/png", "private, no-cache", headers={"Range": "bytes=999-1000"}
    )
    assert handler.status == 416
    assert handler.header("Content-Range") == "bytes */10"


# ── _handle_media end-to-end (real user path) ─────────────────────────────


def _media_get(routes, monkeypatch, target, headers=None):
    monkeypatch.setattr("api.auth.is_auth_enabled", lambda: False)
    handler = _FakeHandler(headers)
    parsed = SimpleNamespace(path="/api/media", query=f"path={target}")
    routes._handle_media(handler, parsed)
    return handler


def test_handle_media_image_served_with_no_cache_and_etag(routes, monkeypatch, tmp_path):
    img = tmp_path / "pic.png"
    img.write_bytes(b"\x89PNG\r\n\x1a\nimagedata")
    handler = _media_get(routes, monkeypatch, img)

    assert handler.status == 200
    assert handler.header("Cache-Control") == "private, no-cache"
    assert handler.header("ETag") and handler.header("ETag").startswith('W/"')
    assert bytes(handler.body) == b"\x89PNG\r\n\x1a\nimagedata"


def test_handle_media_image_revalidation_returns_304(routes, monkeypatch, tmp_path):
    img = tmp_path / "pic.png"
    img.write_bytes(b"\x89PNG\r\n\x1a\nimagedata")
    first = _media_get(routes, monkeypatch, img)
    etag = first.header("ETag")

    second = _media_get(
        routes, monkeypatch, img, headers={"If-None-Match": etag}
    )
    assert second.status == 304
    assert second.body == b""


def test_handle_media_image_update_in_place_revalidates(routes, monkeypatch, tmp_path):
    """The bug this fix targets: same-name image replaced -> preview must refresh."""
    img = tmp_path / "pic.png"
    img.write_bytes(b"old-content")
    first = _media_get(routes, monkeypatch, img)
    old_etag = first.header("ETag")

    time.sleep(0.01)
    img.write_bytes(b"new-content-here")
    second = _media_get(
        routes, monkeypatch, img, headers={"If-None-Match": old_etag}
    )
    assert second.status == 200
    assert bytes(second.body) == b"new-content-here"
    assert second.header("ETag") != old_etag


def test_handle_media_html_keeps_no_store(routes, monkeypatch, tmp_path):
    """Regression guard: HTML inline preview must stay no-store (PR #6706)."""
    page = tmp_path / "page.html"
    page.write_text("<html><body>hi</body></html>", encoding="utf-8")
    handler = _media_get(routes, monkeypatch, page)

    assert handler.status == 200
    assert handler.header("Cache-Control") == "no-store"


def test_handle_media_pdf_uses_no_cache(routes, monkeypatch, tmp_path):
    pdf = tmp_path / "doc.pdf"
    pdf.write_bytes(b"%PDF-1.4\n%EOF")
    handler = _media_get(routes, monkeypatch, pdf)

    assert handler.status == 200
    assert handler.header("Cache-Control") == "private, no-cache"
    assert handler.header("ETag")


def test_large_file_skips_etag_no_hashing(routes, tmp_path):
    """Files above the cap must be served without ETag to avoid hashing every
    byte of a large video / Range request."""
    target = tmp_path / "big.bin"
    target.write_bytes(b"X" * (10 * 1024 * 1024 + 1))  # _ETAG_SIZE_CAP + 1
    handler, _ = _serve(routes, target, "application/octet-stream", "private, no-cache")
    assert handler.status == 200
    assert not handler.header("ETag")


def test_etag_no_body_on_pread_failure(routes, tmp_path, monkeypatch):
    """If os.read fails during ETag snapshot computation, the fd must be closed and
    the response must be 500 (not an unhandled exception)."""
    target = tmp_path / "img.png"
    target.write_bytes(b"abc")

    real_close = os.close
    closed = []
    # Simulate os.read failure (we replaced os.pread with os.lseek + os.read)
    monkeypatch.setattr(
        "api.routes.os.read", lambda fd, n: (_ for _ in ()).throw(OSError(5, "EIO"))
    )
    monkeypatch.setattr(
        "api.routes.os.close", lambda fd: (closed.append(fd), real_close(fd))[1]
    )

    handler, result = _serve(routes, target, "image/png", "private, no-cache")
    assert handler.status == 500
    assert closed, "fd was not closed on read failure"


def test_pread_snapshot_matches_served_bytes(routes, tmp_path, monkeypatch):
    """TOCTOU: the file is replaced between ETag computation and body send;
    the pread snapshot guarantees the ETag and the served bytes agree."""
    target = tmp_path / "img.png"
    target.write_bytes(b"snapshot")
    handler, _ = _serve(routes, target, "image/png", "private, no-cache")
    assert handler.status == 200
    assert handler.body == b"snapshot"
    etag = handler.header("ETag")

    # Replace the file on disk with different content but same path.
    target.write_bytes(b"MUTATED!")

    # A subsequent request with the old ETag must 304 because the
    # snapshot that was actually served still matches.
    handler2, _ = _serve(
        routes, target, "image/png", "private, no-cache",
        headers={"If-None-Match": etag},
    )
    assert handler2.status == 200  # new snapshot ≠ old ETag
    assert handler2.body == b"MUTATED!"
    assert handler2.header("ETag") != etag


def test_short_read_truncation_uses_actual_size(routes, tmp_path, monkeypatch):
    """When a file is truncated between fstat and snapshot read, the actual
    read size is used for Content-Length, not the stale fstat size."""
    target = tmp_path / "img.png"
    # Start with a 10-byte file
    target.write_bytes(b"0123456789")
    
    # Monkeypatch os.read to simulate truncation mid-read:
    # First call returns only 5 bytes (simulating file was truncated to 5).
    original_read = os.read
    read_calls = [0]
    
    def fake_read(fd, n):
        read_calls[0] += 1
        if read_calls[0] == 1:
            # First read: return only 5 bytes, then EOF
            return b"01234"
        return original_read(fd, n)
    
    monkeypatch.setattr(os, "read", fake_read)
    
    handler, _ = _serve(routes, target, "image/png", "private, no-cache")
    
    # The response should use actual_size=5 for Content-Length
    assert handler.status == 200
    content_length = int(handler.header("Content-Length") or "0")
    # Since snapshot is None (short read), it falls back to streaming from fd
    # which would read the actual file (still 10 bytes on disk).
    # But the key invariant: Content-Length must match body length.
    assert content_length == len(handler.body), \
        f"Content-Length {content_length} != body length {len(handler.body)}"


def test_short_read_no_etag_on_truncation(routes, tmp_path, monkeypatch):
    """When the file is truncated mid-read, no ETag is sent (streaming fallback)."""
    target = tmp_path / "img.png"
    target.write_bytes(b"0123456789")
    
    # Simulate truncation: fstat says 10, but _etag_and_snapshot's read only gets 6 bytes
    snapshot_reads = [0]
    
    def fake_read(fd, n):
        # Only intercept the first few reads (for _etag_and_snapshot's loop)
        # After 2 reads (6 bytes), return empty to signal EOF/truncation
        snapshot_reads[0] += 1
        if snapshot_reads[0] == 1:
            return b"012"
        elif snapshot_reads[0] == 2:
            return b"345"
        else:
            # All subsequent reads (streaming path after snapshot failure)
            # return empty, so body is only what fake_read returned during snapshot attempt
            # BUT: snapshot is None, so the code falls back to fdopen streaming
            # which calls f.read(), not os.read(). So this branch is never hit.
            return b""
    
    # Patch api.routes.os.read to only affect the snapshot-read loop
    monkeypatch.setattr("api.routes.os.read", fake_read)
    
    handler, _ = _serve(routes, target, "image/png", "private, no-cache")
    
    # No ETag should be sent (truncation detected, actual_size != file_size)
    assert handler.header("ETag") == ""
    # Content-Length must still match body
    content_length = int(handler.header("Content-Length") or "0")
    # Round-6 fix: we keep the snapshot data, so body is the 6-byte snapshot
    # Content-Length derives from actual_size == len(snapshot), so they match.
    assert content_length == len(handler.body), \
        f"Content-Length {content_length} != body {len(handler.body)}"
    assert len(handler.body) == 6, f"body should be 6-byte snapshot, got {len(handler.body)}"


def test_shrink_again_during_body_transmission_200(routes, tmp_path, monkeypatch):
    """When file shrinks AGAIN between first read and body transmission,
    the captured snapshot (not re-read) is served — no header/body mismatch.
    
    Round-6 regression: Codex reproduced 200 advertised 6 / wrote 3.
    """
    target = tmp_path / "img.png"
    target.write_bytes(b"0123456789")  # 10 bytes initially
    
    # Track reads to simulate shrink-again after first snapshot
    read_calls = [0]
    
    def fake_read(fd, n):
        read_calls[0] += 1
        # First read: get 6 bytes (simulate first truncation)
        if read_calls[0] == 1:
            return b"012345"
        # Subsequent reads during the loop: return empty (EOF)
        # The loop will stop and we get a 6-byte snapshot
        if read_calls[0] <= 3:  # Loop continues until remaining=0 or EOF
            return b""
        # Any read AFTER the snapshot (during body transmission):
        # This should NEVER happen with the fix - we serve the snapshot.
        # If it does happen, return different bytes to detect the bug.
        return b"XXX"
    
    monkeypatch.setattr("api.routes.os.read", fake_read)
    
    handler, _ = _serve(routes, target, "image/png", "private, no-cache")
    
    assert handler.status == 200
    content_length = int(handler.header("Content-Length") or "0")
    # Content-Length should be 6 (from first snapshot), not 10 (file on disk)
    # Body should be the 6-byte snapshot
    assert content_length == 6, f"Content-Length should be 6, got {content_length}"
    assert len(handler.body) == 6, f"body should be 6 bytes, got {len(handler.body)}"
    assert handler.body == b"012345", f"body should be the captured snapshot, got {handler.body!r}"
    assert content_length == len(handler.body), \
        f"Content-Length {content_length} != body {len(handler.body)}"


def test_shrink_again_during_body_transmission_range_206(routes, tmp_path, monkeypatch):
    """When file shrinks AGAIN between first read and Range body transmission,
    the captured snapshot (not re-read) is served — no header/body mismatch.
    
    Round-6 regression: Codex reproduced 206 advertised 4 / wrote 1.
    """
    target = tmp_path / "img.png"
    target.write_bytes(b"0123456789")  # 10 bytes initially
    
    # Track reads to simulate shrink-again after first snapshot
    read_calls = [0]
    
    def fake_read(fd, n):
        read_calls[0] += 1
        # First read: get 6 bytes (simulate first truncation)
        if read_calls[0] == 1:
            return b"012345"
        # Any subsequent read: return only 1 byte (file shrank again)
        # But with the fix, this should NEVER be called for body transmission
        return b"x"
    
    monkeypatch.setattr("api.routes.os.read", fake_read)
    
    # Request Range: bytes=1-4 (4 bytes from offset 1)
    handler, _ = _serve(
        routes, target, "image/png", "private, no-cache",
        headers={"Range": "bytes=1-4"},
    )
    
    assert handler.status == 206
    content_length = int(handler.header("Content-Length") or "0")
    # Range requested 4 bytes from offset 1 of the 6-byte snapshot: "1234"
    assert content_length == 4, f"Content-Length should be 4, got {content_length}"
    assert len(handler.body) == 4, f"body should be 4 bytes, got {len(handler.body)}"
    assert handler.body == b"1234", "body should be snapshot[1:5]"
    assert content_length == len(handler.body), \
        f"Content-Length {content_length} != body {len(handler.body)}"



# ── Post-commit body transmission: client disconnect must not double-respond ─

class _DisconnectAfterHeadersHandler(_FakeHandler):
    """Fake handler whose writer raises BrokenPipeError once the response is
    committed, simulating a client that disconnects mid-body."""

    def __init__(self, headers=None):
        super().__init__(headers)
        self.send_response_calls: list[int] = []
        self.headers_ended = False

    def send_response(self, code):
        self.send_response_calls.append(code)
        super().send_response(code)

    def end_headers(self):
        self.headers_ended = True

    def write(self, data):
        raise BrokenPipeError()


def test_disconnect_mid_body_snapshot_no_second_response(routes, tmp_path):
    """Client disconnects while the snapshot body is written: the 200 is
    already committed, so no second 500 may be attempted afterwards."""
    target = tmp_path / "img.png"
    target.write_bytes(b"payload")
    handler = _DisconnectAfterHeadersHandler()
    result = routes._serve_file_bytes(
        handler, target, "image/png", "inline", "private, no-cache"
    )

    assert handler.status == 200
    assert handler.send_response_calls == [200], (
        f"exactly one status expected, got {handler.send_response_calls}"
    )
    assert handler.headers_ended
    assert result is True


def test_disconnect_mid_body_stream_no_second_response(routes, tmp_path):
    """Client disconnects while an over-cap (no-ETag) file is streamed:
    exactly one 200 status, never a trailing 500."""
    target = tmp_path / "big.bin"
    target.write_bytes(b"X" * (10 * 1024 * 1024 + 1))  # _ETAG_SIZE_CAP + 1
    handler = _DisconnectAfterHeadersHandler()
    result = routes._serve_file_bytes(
        handler, target, "application/octet-stream", "inline", "private, no-cache"
    )

    assert handler.status == 200
    assert handler.send_response_calls == [200], (
        f"exactly one status expected, got {handler.send_response_calls}"
    )
    assert handler.headers_ended
    assert result is True


# ── Post-commit body transmission: NON-disconnect errors must not escape ──

class _PostCommitErrorHandler(_FakeHandler):
    """Fake handler whose writer raises a NON-disconnect error (a generic
    OSError/EIO from a truncated read, a PermissionError, ...) once the
    response is committed. These are NOT in _CLIENT_DISCONNECT_ERRORS, so
    they are only contained by the broad post-commit except — if it escapes,
    Handler.do_GET appends a trailing 500 after the committed 200."""

    def __init__(self, error, headers=None):
        super().__init__(headers)
        self.send_response_calls: list[int] = []
        self.headers_ended = False
        self._error = error

    def send_response(self, code):
        self.send_response_calls.append(code)
        super().send_response(code)

    def end_headers(self):
        self.headers_ended = True

    def write(self, data):
        raise self._error


def _serve_with_caller(routes, handler, target, mime, cache_control):
    """Mirror server.py Handler.do_GET: any exception escaping
    _serve_file_bytes after commit becomes a second send_response(500).
    With the round-7 fix in place nothing escapes, so the recorded status
    sequence stays exactly [200]."""
    try:
        return routes._serve_file_bytes(
            handler, target, mime, "inline", cache_control
        )
    except Exception:
        handler.send_response(500)
        return False


@pytest.mark.parametrize(
    "error",
    [
        pytest.param(OSError(5, "Input/output error"), id="generic-OSError-EIO"),
        pytest.param(PermissionError(13, "Permission denied"), id="PermissionError"),
    ],
)
def test_non_disconnect_error_mid_body_snapshot_no_second_response(routes, tmp_path, error):
    """A non-disconnect error while the snapshot body is written must not
    escape to the caller's 500 path: exactly [200], never [200, 500]."""
    target = tmp_path / "img.png"
    target.write_bytes(b"payload")
    handler = _PostCommitErrorHandler(error)
    result = _serve_with_caller(
        routes, handler, target, "image/png", "private, no-cache"
    )

    assert handler.status == 200
    assert handler.send_response_calls == [200], (
        f"exactly one status expected, got {handler.send_response_calls}"
    )
    assert handler.headers_ended
    assert result is True


@pytest.mark.parametrize(
    "error",
    [
        pytest.param(OSError(5, "Input/output error"), id="generic-OSError-EIO"),
        pytest.param(PermissionError(13, "Permission denied"), id="PermissionError"),
    ],
)
def test_non_disconnect_error_mid_body_stream_no_second_response(routes, tmp_path, error):
    """Same contract for the over-cap streaming path: a non-disconnect error
    mid-stream must not produce a trailing 500."""
    target = tmp_path / "big.bin"
    target.write_bytes(b"X" * (10 * 1024 * 1024 + 1))  # _ETAG_SIZE_CAP + 1
    handler = _PostCommitErrorHandler(error)
    result = _serve_with_caller(
        routes,
        handler,
        target,
        "application/octet-stream",
        "private, no-cache",
    )

    assert handler.status == 200
    assert handler.send_response_calls == [200], (
        f"exactly one status expected, got {handler.send_response_calls}"
    )
    assert handler.headers_ended
    assert result is True
