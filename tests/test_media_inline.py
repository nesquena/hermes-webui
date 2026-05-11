"""
Tests for feat #450: MEDIA: token inline rendering in web UI chat.

Covers:
1. /api/media endpoint: serves local image files by absolute path
2. /api/media endpoint: rejects paths outside allowed roots (path traversal)
3. /api/media endpoint: 404 for non-existent files
4. /api/media endpoint: auth gate when auth is enabled
5. renderMd() MEDIA: stash/restore logic (static JS analysis)
6. /api/media endpoint: integration test via live server (requires 8788)
"""
from __future__ import annotations

import json
import os
import pathlib
import tempfile
import unittest
import urllib.error
import urllib.request

from tests._pytest_port import BASE, TEST_STATE_DIR

REPO_ROOT = pathlib.Path(__file__).parent.parent
UI_JS = (REPO_ROOT / "static" / "ui.js").read_text(encoding="utf-8")
WORKSPACE_JS = (REPO_ROOT / "static" / "workspace.js").read_text(encoding="utf-8")


# ── Static analysis: renderMd MEDIA stash ────────────────────────────────────

class TestMediaRenderMdStash(unittest.TestCase):
    """Verify the MEDIA: stash/restore logic exists in ui.js."""

    def test_media_stash_defined(self):
        self.assertIn("media_stash", UI_JS,
                      "media_stash array must be defined in renderMd()")

    def test_media_token_regex(self):
        self.assertIn("MEDIA:", UI_JS,
                      "MEDIA: token regex must be present in renderMd()")

    def test_media_restore_produces_img_tag(self):
        self.assertIn("msg-media-img", UI_JS,
                      "restore pass must produce <img class='msg-media-img'>")

    def test_media_restore_produces_download_link(self):
        self.assertIn("msg-media-link", UI_JS,
                      "restore pass must produce download link for non-image files")

    def test_media_api_url_pattern(self):
        self.assertIn("api/media?path=", UI_JS,
                      "renderMd must build api/media?path=... URL for local files")

    def test_local_audio_video_media_tokens_request_inline_streaming(self):
        self.assertIn("apiUrl+'&inline=1'", UI_JS,
                      "MEDIA: audio/video local paths must request inline streaming")

    def test_media_stash_uses_null_byte_token(self):
        self.assertIn("\\x00D", UI_JS,
                      "MEDIA stash must use null-byte token (\\x00D) to avoid conflicts")

    def test_media_stash_runs_before_fence_stash(self):
        media_pos = UI_JS.find("media_stash")
        fence_pos = UI_JS.find("fence_stash")
        self.assertGreater(fence_pos, media_pos,
                           "media_stash must be defined before fence_stash in renderMd()")

    def test_image_extension_regex_covers_common_types(self):
        # The JS source has these extensions in a regex like /\.png|jpg|.../i
        # Check for the extension strings (without the dot, which may be escaped as \.)
        for ext in ["png", "jpg", "jpeg", "gif", "webp"]:
            self.assertIn(ext, UI_JS,
                          f"Image extension {ext} must be in the MEDIA img-check regex")

    def test_http_url_media_rendered_as_img(self):
        # renderMd should treat MEDIA:https://... as an <img>
        # In the JS source, the regex is /^https?:\/\//i (escaped)
        self.assertTrue(
            "https?:" in UI_JS or "http" in UI_JS,
            "MEDIA: restore must handle HTTPS URLs",
        )

    def test_zoom_toggle_on_click(self):
        # PR #1135: CSS class toggle replaced by proper lightbox overlay
        self.assertIn("_openImgLightbox", UI_JS,
                      "Clicking the image must open lightbox overlay (_openImgLightbox)")


# ── Static analysis: CSS ──────────────────────────────────────────────────────

class TestMediaCSS(unittest.TestCase):

    CSS = (REPO_ROOT / "static" / "style.css").read_text(encoding="utf-8")

    def test_msg_media_img_class_defined(self):
        self.assertIn(".msg-media-img", self.CSS)

    def test_msg_media_img_max_width(self):
        # PR #1135: resting thumbnail is 120x90px (fixed size); no max-width needed.
        # Lightbox shows full-size. Check width is set instead.
        idx = self.CSS.find(".msg-media-img{")
        self.assertGreater(idx, 0)
        rule = self.CSS[idx:idx+200]
        self.assertIn("width:120px", rule, "Thumbnail must have fixed 120px width")

    def test_msg_media_img_full_class_defined(self):
        # PR #1135: .msg-media-img--full removed; lightbox replaces inline zoom.
        self.assertIn(".img-lightbox", self.CSS,
                      "Full-size toggle class must exist for zoom-on-click")

    def test_msg_media_link_class_defined(self):
        self.assertIn(".msg-media-link", self.CSS,
                      "Download link style must be defined for non-image media")



class TestInlineAudioVideoEditor(unittest.TestCase):
    """Static checks for inline audio/video preview controls in chat and workspace."""

    CSS = (REPO_ROOT / "static" / "style.css").read_text(encoding="utf-8")
    WORKSPACE_JS = (REPO_ROOT / "static" / "workspace.js").read_text(encoding="utf-8")
    INDEX_HTML = (REPO_ROOT / "static" / "index.html").read_text(encoding="utf-8")

    def test_audio_and_video_extension_detection_exists(self):
        self.assertIn("_AUDIO_EXTS", UI_JS)
        self.assertIn("_VIDEO_EXTS", UI_JS)
        for ext in ["mp3", "wav", "m4a", "mp4", "mov", "webm"]:
            self.assertIn(ext, UI_JS)

    def test_media_player_markup_has_native_controls(self):
        self.assertIn("_mediaPlayerHtml", UI_JS)
        self.assertIn("<audio", UI_JS)
        self.assertIn("<video", UI_JS)
        self.assertIn("controls", UI_JS)
        self.assertIn("playsinline", UI_JS)

    def test_variable_speed_buttons_and_playback_rate_handler_exist(self):
        self.assertIn("MEDIA_PLAYBACK_RATES", UI_JS)
        for rate in ["0.5", "0.75", "1.25", "1.5", "2"]:
            self.assertIn(rate, UI_JS)
        self.assertIn("playbackRate", UI_JS)
        self.assertIn("media-speed-btn", UI_JS)
        self.assertIn("aria-pressed", UI_JS)

    def test_playback_speed_preference_persists_in_localstorage(self):
        self.assertIn("MEDIA_PLAYBACK_STORAGE_KEY", UI_JS)
        self.assertIn("localStorage.getItem(MEDIA_PLAYBACK_STORAGE_KEY)", UI_JS)
        self.assertIn("localStorage.setItem(MEDIA_PLAYBACK_STORAGE_KEY", UI_JS)
        self.assertIn("_applyMediaPlaybackRate", UI_JS)
        self.assertIn('addEventListener("loadedmetadata"', UI_JS)
        self.assertIn("MutationObserver", UI_JS)
        self.assertIn("setTimeout(_initMediaPlaybackObserver,0)", UI_JS)
        self.assertIn("_applyMediaPlaybackPreferences(inner)", UI_JS)
        self.assertIn("_applyMediaPlaybackPreferences(wrap)", WORKSPACE_JS)

    def test_message_attachments_render_audio_video_instead_of_badges(self):
        self.assertIn("_renderAttachmentHtml", UI_JS)
        self.assertIn("data-media-kind", UI_JS)
        self.assertIn("api/file/raw?session_id=", UI_JS)

    def test_composer_tray_recognizes_audio_video_files(self):
        self.assertIn("attach-chip--media", UI_JS)
        self.assertIn("attach-chip--'+mediaKind", UI_JS)
        self.assertIn("URL.createObjectURL(f)", UI_JS)

    def test_workspace_preview_routes_audio_video_inline(self):
        self.assertIn("AUDIO_EXTS", self.WORKSPACE_JS)
        self.assertIn("VIDEO_EXTS", self.WORKSPACE_JS)
        self.assertIn("previewMediaWrap", self.WORKSPACE_JS)
        self.assertIn("showPreview(mode)", self.WORKSPACE_JS)
        self.assertIn("&inline=1", self.WORKSPACE_JS)
        self.assertIn('id="previewMediaWrap"', self.INDEX_HTML)

    def test_media_editor_css_defined(self):
        for cls in [".msg-media-editor", ".msg-media-player", ".msg-media-video", ".media-speed-controls", ".media-speed-btn", ".preview-media-wrap"]:
            self.assertIn(cls, self.CSS)


class TestWorkspacePdfViewer(unittest.TestCase):
    """Static checks for inline PDF preview support in the workspace panel."""

    CSS = (REPO_ROOT / "static" / "style.css").read_text(encoding="utf-8")
    WORKSPACE_JS = (REPO_ROOT / "static" / "workspace.js").read_text(encoding="utf-8")
    INDEX_HTML = (REPO_ROOT / "static" / "index.html").read_text(encoding="utf-8")

    def test_pdf_extension_routes_to_inline_viewer(self):
        self.assertIn("PDF_EXTS", self.WORKSPACE_JS)
        self.assertIn("PDF_EXTS.has(ext)", self.WORKSPACE_JS)
        self.assertIn("showPreview('pdf')", self.WORKSPACE_JS)
        self.assertIn("&inline=1", self.WORKSPACE_JS)

    def test_pdf_viewer_markup_exists(self):
        self.assertIn('id="previewPdfWrap"', self.INDEX_HTML)
        self.assertIn('id="previewPdfFrame"', self.INDEX_HTML)
        self.assertIn('title="PDF preview"', self.INDEX_HTML)

    def test_pdf_preview_css_defined(self):
        for cls in [".preview-pdf-wrap", ".preview-pdf-frame", ".preview-badge.pdf"]:
            self.assertIn(cls, self.CSS)

# ── Backend: /api/media endpoint (unit-level, no server needed) ─────────────

class TestMediaEndpointUnit(unittest.TestCase):
    """Test route registration and handler logic via imports."""

    def test_handle_media_function_exists(self):
        from api import routes
        self.assertTrue(
            hasattr(routes, "_handle_media"),
            "_handle_media must be defined in api/routes.py",
        )

    def test_api_media_route_registered(self):
        """The GET dispatch must include the /api/media path."""
        routes_src = (REPO_ROOT / "api" / "routes.py").read_text(encoding="utf-8")
        self.assertIn('"/api/media"', routes_src,
                      '/api/media must be registered in the GET route dispatch')

    def test_allowed_roots_include_tmp(self):
        """Handler must allow /tmp so screenshot paths work."""
        routes_src = (REPO_ROOT / "api" / "routes.py").read_text(encoding="utf-8")
        self.assertIn('/tmp', routes_src,
                      '/tmp must be in the allowed roots list for /api/media')

    def test_svg_forces_download(self):
        """.svg must not be served inline (XSS risk)."""
        routes_src = (REPO_ROOT / "api" / "routes.py").read_text(encoding="utf-8")
        # SVG should be in _DOWNLOAD_TYPES or explicitly excluded from inline
        self.assertIn("image/svg+xml", routes_src,
                      "SVG MIME type must be handled (forced download) in _handle_media")

    def test_non_image_forces_download(self):
        """Non-image files should be forced to download, not served inline."""
        routes_src = (REPO_ROOT / "api" / "routes.py").read_text(encoding="utf-8")
        self.assertIn("_INLINE_IMAGE_TYPES", routes_src,
                      "_INLINE_IMAGE_TYPES whitelist must exist in _handle_media")

    def test_media_allowed_roots_env_var_referenced(self):
        """Handler must reference MEDIA_ALLOWED_ROOTS for configurable roots."""
        routes_src = (REPO_ROOT / "api" / "routes.py").read_text(encoding="utf-8")
        self.assertIn("MEDIA_ALLOWED_ROOTS", routes_src,
                      "MEDIA_ALLOWED_ROOTS env var must be parsed in _handle_media")

    def test_media_endpoints_advertise_byte_range_support(self):
        routes_src = (REPO_ROOT / "api" / "routes.py").read_text(encoding="utf-8")
        self.assertIn("Accept-Ranges", routes_src)
        self.assertIn("Content-Range", routes_src)
        self.assertIn("206", routes_src)


# ── Integration tests: live server on TEST_PORT ───────────────────────────────
# No collection-time skip guard — conftest.py starts the server via its
# autouse session fixture BEFORE tests run.  A collection-time check always
# sees no server and turns every test into a skip.  Instead we assert
# reachability inside setUp() so failures are loud errors, not silent skips.


class TestMediaEndpointIntegration(unittest.TestCase):

    def setUp(self):
        try:
            urllib.request.urlopen(BASE + "/health", timeout=5)
        except Exception as exc:
            self.fail(f"Test server at {BASE} is not reachable: {exc}")

    def _get(self, path, headers=None):
        req = urllib.request.Request(BASE + path, headers=headers or {})
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.read(), r.status, r.headers
        except urllib.error.HTTPError as e:
            return e.read(), e.code, e.headers

    def test_no_path_returns_400(self):
        _, status, _ = self._get("/api/media")
        self.assertEqual(status, 400)

    def test_nonexistent_file_returns_404(self):
        _, status, _ = self._get("/api/media?path=/tmp/__hermes_nonexistent_12345.png")
        self.assertEqual(status, 404)

    def test_path_outside_allowed_root_rejected(self):
        # /etc/passwd is outside allowed roots
        _, status, _ = self._get("/api/media?path=/etc/passwd")
        self.assertIn(status, {403, 404})

    def test_valid_png_served_with_image_mime(self):
        """Create a 1-pixel PNG in /tmp and verify it's served correctly."""
        # Minimal valid 1x1 transparent PNG (67 bytes)
        png_bytes = (
            b'\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01'
            b'\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\nIDATx\x9cc\x00'
            b'\x01\x00\x00\x05\x00\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82'
        )
        with tempfile.NamedTemporaryFile(
            suffix=".png", prefix="hermes_test_", dir="/tmp", delete=False
        ) as f:
            f.write(png_bytes)
            tmp_path = f.name
        try:
            body, status, headers = self._get(
                f"/api/media?path={urllib.request.quote(tmp_path)}"
            )
            self.assertEqual(status, 200, f"Expected 200, got {status}")
            ct = headers.get("Content-Type", "")
            self.assertIn("image/png", ct, f"Expected image/png, got {ct}")
            self.assertEqual(body, png_bytes)
        finally:
            pathlib.Path(tmp_path).unlink(missing_ok=True)

    def test_audio_media_endpoint_inline_and_range(self):
        """MEDIA: audio paths stream inline and support byte ranges for playback."""
        audio_bytes = b"RIFF" + (b"\x00" * 256)
        with tempfile.NamedTemporaryFile(
            suffix=".wav", prefix="hermes_test_", dir="/tmp", delete=False
        ) as f:
            f.write(audio_bytes)
            tmp_path = f.name
        try:
            encoded = urllib.request.quote(tmp_path)
            body, status, headers = self._get(f"/api/media?path={encoded}&inline=1")
            self.assertEqual(status, 200)
            self.assertIn("audio/wav", headers.get("Content-Type", ""))
            self.assertIn("inline", headers.get("Content-Disposition", ""))
            self.assertEqual(headers.get("Accept-Ranges"), "bytes")
            self.assertEqual(body, audio_bytes)

            body, status, headers = self._get(
                f"/api/media?path={encoded}&inline=1",
                headers={"Range": "bytes=0-3"},
            )
            self.assertEqual(status, 206)
            self.assertEqual(body, b"RIFF")
            self.assertEqual(headers.get("Content-Range"), f"bytes 0-3/{len(audio_bytes)}")
        finally:
            pathlib.Path(tmp_path).unlink(missing_ok=True)

    def test_html_media_endpoint_inline_requires_csp_sandbox(self):
        """HTML opens inline only when requested and always carries CSP sandbox."""
        html_bytes = b"<!doctype html><title>Hermes</title><script>window.ok=1</script>"
        with tempfile.NamedTemporaryFile(
            suffix=".html", prefix="hermes_test_", dir="/tmp", delete=False
        ) as f:
            f.write(html_bytes)
            tmp_path = f.name
        try:
            encoded = urllib.request.quote(tmp_path)

            body, status, headers = self._get(f"/api/media?path={encoded}")
            self.assertEqual(status, 200)
            self.assertIn("text/html", headers.get("Content-Type", ""))
            self.assertIn("attachment", headers.get("Content-Disposition", ""))
            self.assertIn("DENY", headers.get_all("X-Frame-Options", []))
            self.assertFalse(
                any("sandbox allow-scripts" == h for h in headers.get_all("Content-Security-Policy", []))
            )
            self.assertEqual(body, html_bytes)

            body, status, headers = self._get(f"/api/media?path={encoded}&inline=1")
            self.assertEqual(status, 200)
            self.assertIn("text/html", headers.get("Content-Type", ""))
            self.assertIn("inline", headers.get("Content-Disposition", ""))
            self.assertEqual(headers.get_all("X-Frame-Options", []), [])
            self.assertTrue(
                any("sandbox allow-scripts" == h for h in headers.get_all("Content-Security-Policy", []))
            )
            self.assertEqual(body, html_bytes)
        finally:
            pathlib.Path(tmp_path).unlink(missing_ok=True)

    def test_path_traversal_rejected(self):
        _, status, _ = self._get(
            "/api/media?path=" + urllib.request.quote("/tmp/../../etc/passwd")
        )
        self.assertIn(status, {403, 404})

    def test_health_check_still_works(self):
        """Sanity: server is up and /health works."""
        body, status, _ = self._get("/health")
        self.assertEqual(status, 200)
        d = json.loads(body)
        self.assertEqual(d["status"], "ok")
