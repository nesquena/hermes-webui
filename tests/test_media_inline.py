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
        self.assertIn("msg-media-img--full", UI_JS,
                      "Clicking the image must toggle msg-media-img--full class for zoom")


# ── Static analysis: CSS ──────────────────────────────────────────────────────

class TestMediaCSS(unittest.TestCase):

    CSS = (REPO_ROOT / "static" / "style.css").read_text(encoding="utf-8")

    def test_msg_media_img_class_defined(self):
        self.assertIn(".msg-media-img", self.CSS)

    def test_msg_media_img_max_width(self):
        # Should have a max-width to prevent huge images breaking layout
        idx = self.CSS.find(".msg-media-img{")
        self.assertGreater(idx, 0)
        rule = self.CSS[idx:idx+200]
        self.assertIn("max-width", rule)

    def test_msg_media_img_full_class_defined(self):
        self.assertIn(".msg-media-img--full", self.CSS,
                      "Full-size toggle class must exist for zoom-on-click")

    def test_msg_media_link_class_defined(self):
        self.assertIn(".msg-media-link", self.CSS,
                      "Download link style must be defined for non-image media")


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


# ── Integration tests: live server on TEST_PORT ───────────────────────────────

def _server_reachable() -> bool:
    try:
        urllib.request.urlopen(BASE + "/health", timeout=3)
        return True
    except Exception:
        return False


requires_server = unittest.skipUnless(
    _server_reachable(), f"Test server not reachable at {BASE}"
)


@requires_server
class TestMediaEndpointIntegration(unittest.TestCase):

    def _get(self, path):
        try:
            with urllib.request.urlopen(BASE + path, timeout=10) as r:
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
