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
from types import SimpleNamespace
from unittest import mock
import urllib.error
import urllib.parse
import urllib.request

from tests._pytest_port import BASE, TEST_STATE_DIR

REPO_ROOT = pathlib.Path(__file__).parent.parent
UI_JS = (REPO_ROOT / "static" / "ui.js").read_text(encoding="utf-8")
I18N_JS = (REPO_ROOT / "static" / "i18n.js").read_text(encoding="utf-8")
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

    def test_bare_file_urls_are_stashed_as_media_artifacts(self):
        self.assertIn("file:// links for local artifacts", UI_JS)
        self.assertIn("file:\\/\\/[^\\s<>", UI_JS)

    def test_file_urls_are_rewritten_through_media_endpoint(self):
        self.assertIn("new URL(ref)", UI_JS)
        self.assertIn("u.pathname", UI_JS)
        self.assertIn("api/media?path=", UI_JS)

    def test_media_restore_produces_img_tag(self):
        self.assertIn("msg-media-img", UI_JS,
                      "restore pass must produce <img class='msg-media-img'>")

    def test_media_restore_produces_download_link(self):
        self.assertIn("msg-media-link", UI_JS,
                      "restore pass must produce download link for non-image files")

    def test_local_image_media_uses_clean_image_with_hover_download(self):
        # #3220 redesign: generated local images render as a clean inline image
        # (keeping the lightbox-on-click) with a hover/focus-revealed Download
        # overlay — matching the ChatGPT/Claude/Gemini pattern — instead of a
        # permanent bordered card with always-visible Open/Download buttons.
        self.assertIn("localArtifactCard", UI_JS)
        self.assertIn("msg-artifact-image", UI_JS)
        self.assertIn("msg-artifact-download", UI_JS)
        self.assertIn("msg-media-img", UI_JS)
        self.assertIn("t('media_download')", UI_JS)
        self.assertIn("media_download:", I18N_JS)
        # The clean-image redesign drops the permanent card chrome.
        self.assertNotIn("msg-artifact-card", UI_JS)
        self.assertNotIn("msg-artifact-actions", UI_JS)
        self.assertNotIn("downloadUrl=src+(String(src).includes('?')?'&':'?')+'download=1'", UI_JS)
        self.assertNotIn("openUrl=src+(String(src).includes('?')?'&':'?')+'inline=1'", UI_JS)

    def test_media_api_url_pattern(self):
        self.assertIn("api/media?path=", UI_JS,
                      "renderMd must build api/media?path=... URL for local files")

    def test_local_media_api_url_carries_session_id_when_available(self):
        self.assertIn("session_id='+encodeURIComponent(mediaSessionId)", UI_JS,
                      "local MEDIA: image URLs must include session_id so the server can authorize session-referenced artifacts")

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
        self.assertIn(
            ".msg-media-link",
            self.CSS,
            "Download link style must be defined for non-image media",
        )

    def test_generated_artifact_image_css_defined(self):
        # #3220 redesign: clean image + hover-revealed download overlay.
        for cls in [
            ".msg-artifact-image",
            ".msg-artifact-download",
        ]:
            self.assertIn(cls, self.CSS)
        # Hover/focus reveals the download button (hidden by default).
        self.assertIn(".msg-artifact-image:hover .msg-artifact-download", self.CSS)
        # The old permanent-card classes are gone.
        self.assertNotIn(".msg-artifact-card", self.CSS)
        self.assertNotIn(".msg-artifact-action", self.CSS)



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

    def test_media_allowed_roots_uses_os_pathsep(self):
        """MEDIA_ALLOWED_ROOTS must use the platform path separator."""
        routes_src = (REPO_ROOT / "api" / "routes.py").read_text(encoding="utf-8")
        start = routes_src.index("extra_roots =")
        block = routes_src[start:start + 900]
        self.assertIn(".split(_os.pathsep)", block)
        self.assertNotIn('.split(":")', block)

    def test_path_is_within_root_treats_commonpath_valueerror_as_not_within(self):
        """Windows cross-drive commonpath() errors must not crash /api/media."""
        from api import routes

        with mock.patch.object(
            routes.os.path,
            "commonpath",
            side_effect=ValueError("Paths don't have the same drive"),
        ):
            self.assertFalse(
                routes._path_is_within_root(
                    pathlib.Path("D:/outputs/card.png"),
                    pathlib.Path("C:/Users/agent/.hermes"),
                )
            )

    def test_path_is_within_root_accepts_child_path(self):
        from api import routes

        with tempfile.TemporaryDirectory() as tmpd:
            root = pathlib.Path(tmpd).resolve()
            child = root / "media" / "card.png"
            child.parent.mkdir()
            child.write_bytes(b"png")
            self.assertTrue(routes._path_is_within_root(child.resolve(), root))

    def test_active_workspace_carveout_gated_against_hermes_roots(self):
        """#3234: the active-workspace carve-out must NOT re-open the disclosure
        when the active workspace is pathologically set to a broad/internal root
        ($HOME, ~/.hermes, a profile root, etc.). A state.db sitting under such a
        workspace must still be denied (403), not served.
        """
        from api import routes

        class _Handler:
            def __init__(self):
                self.status = None
                self._buf = []
            def send_response(self, code):
                self.status = code
            def send_header(self, *a, **k):
                pass
            def end_headers(self):
                pass
            class _W:
                def write(self_inner, b):
                    pass
            wfile = _W()

        with tempfile.TemporaryDirectory() as home:
            hermes_home = pathlib.Path(home) / ".hermes"
            hermes_home.mkdir(parents=True)
            secret = hermes_home / "state.db"
            secret.write_bytes(b"secret-state")
            target = secret.resolve()

            handler = _Handler()
            parsed = SimpleNamespace(
                query=f"path={urllib.parse.quote(str(target))}", path="/api/media"
            )
            with mock.patch.dict(os.environ, {"HERMES_HOME": str(hermes_home)}), \
                 mock.patch.object(routes, "get_last_workspace", lambda: str(hermes_home)), \
                 mock.patch("api.auth.is_auth_enabled", lambda: False):
                routes._handle_media(handler, parsed)

            self.assertEqual(
                handler.status, 403,
                "state.db must stay denied even when the active workspace IS the "
                "Hermes home (carve-out must be gated against internal roots)",
            )

    def test_active_workspace_under_state_dir_serves_but_sessions_denied(self):
        """#3234: a workspace at STATE_DIR/workspace is legitimate user media —
        STATE_DIR/workspace/shot.png must serve (not 403), while a sibling
        STATE_DIR/sessions/<sid>.json (internal state) must stay denied (403).

        Regression for the over-block where STATE_DIR was denied wholesale.
        """
        from api import routes

        class _Handler:
            def __init__(self):
                self.status = None
                self.headers = {}
            def send_response(self, code):
                self.status = code
            def send_header(self, *a, **k):
                pass
            def end_headers(self):
                pass
            class _W:
                def write(self_inner, b):
                    pass
                def flush(self_inner):
                    pass
            wfile = _W()

        png_bytes = (
            b'\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01'
            b'\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\nIDATx\x9cc\x00'
            b'\x01\x00\x00\x05\x00\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82'
        )
        with tempfile.TemporaryDirectory() as home:
            hermes_home = pathlib.Path(home) / ".hermes"
            state_dir = hermes_home / "webui-state"
            ws = state_dir / "workspace"
            sessions = state_dir / "sessions"
            ws.mkdir(parents=True)
            sessions.mkdir(parents=True)
            shot = ws / "shot.png"
            shot.write_bytes(png_bytes)
            sess_file = sessions / "abc.json"
            sess_file.write_text('{"messages":[]}', encoding="utf-8")

            env = {
                "HERMES_HOME": str(hermes_home),
                "HERMES_WEBUI_STATE_DIR": str(state_dir),
            }
            with mock.patch.dict(os.environ, env), \
                 mock.patch.object(routes, "get_last_workspace", lambda: str(ws)), \
                 mock.patch("api.auth.is_auth_enabled", lambda: False), \
                 mock.patch("api.config.STATE_DIR", state_dir):
                # workspace media → not blocked by the #3234 deny
                h1 = _Handler()
                routes._handle_media(h1, SimpleNamespace(
                    query=f"path={urllib.parse.quote(str(shot.resolve()))}&inline=1",
                    path="/api/media"))
                self.assertNotEqual(
                    h1.status, 403,
                    "STATE_DIR/workspace/shot.png must NOT be blocked (legit media)")
                # sessions state → still denied
                h2 = _Handler()
                routes._handle_media(h2, SimpleNamespace(
                    query=f"path={urllib.parse.quote(str(sess_file.resolve()))}",
                    path="/api/media"))
                self.assertEqual(
                    h2.status, 403,
                    "STATE_DIR/sessions/abc.json must stay denied (internal state)")

    def test_named_profile_workspace_serves_but_profile_secrets_denied(self):
        """#3234: a named-profile workspace (<base>/profiles/p1/workspace) is
        legitimate media and must serve, while that profile's secrets
        (<base>/profiles/p1/auth.json) and a SIBLING profile's secrets
        (<base>/profiles/other/auth.json) must stay denied (403).

        Regression for the over-block where the whole `profiles` tree was denied.
        """
        from api import routes

        class _Handler:
            def __init__(self):
                self.status = None
                self.headers = {}
            def send_response(self, code):
                self.status = code
            def send_header(self, *a, **k):
                pass
            def end_headers(self):
                pass
            class _W:
                def write(self_inner, b):
                    pass
                def flush(self_inner):
                    pass
            wfile = _W()

        png_bytes = (
            b'\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01'
            b'\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\nIDATx\x9cc\x00'
            b'\x01\x00\x00\x05\x00\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82'
        )
        with tempfile.TemporaryDirectory() as home:
            base = pathlib.Path(home) / ".hermes"
            p1_ws = base / "profiles" / "p1" / "workspace"
            p1_ws.mkdir(parents=True)
            (p1_ws / "shot.png").write_bytes(png_bytes)
            p1_secret = base / "profiles" / "p1" / "auth.json"
            p1_secret.write_text("{}", encoding="utf-8")
            other_secret = base / "profiles" / "other" / "auth.json"
            other_secret.parent.mkdir(parents=True)
            other_secret.write_text("{}", encoding="utf-8")

            active = base / "profiles" / "p1"  # active profile HERMES_HOME
            with mock.patch.dict(os.environ, {"HERMES_HOME": str(active)}), \
                 mock.patch.object(routes, "get_last_workspace", lambda: str(p1_ws)), \
                 mock.patch("api.auth.is_auth_enabled", lambda: False), \
                 mock.patch("api.profiles._DEFAULT_HERMES_HOME", base):
                # named-profile workspace media → served
                h1 = _Handler()
                routes._handle_media(h1, SimpleNamespace(
                    query=f"path={urllib.parse.quote(str((p1_ws / 'shot.png').resolve()))}&inline=1",
                    path="/api/media"))
                self.assertNotEqual(
                    h1.status, 403,
                    "named-profile workspace media must NOT be blocked")
                # this profile's own secret → denied
                h2 = _Handler()
                routes._handle_media(h2, SimpleNamespace(
                    query=f"path={urllib.parse.quote(str(p1_secret.resolve()))}",
                    path="/api/media"))
                self.assertEqual(h2.status, 403, "profile auth.json must be denied")
                # sibling profile's secret → denied
                h3 = _Handler()
                routes._handle_media(h3, SimpleNamespace(
                    query=f"path={urllib.parse.quote(str(other_secret.resolve()))}",
                    path="/api/media"))
                self.assertEqual(h3.status, 403, "sibling profile auth.json must be denied")
                # per-profile webui_state/sessions → denied (not a direct child of root)
                ws_sess = active / "webui_state" / "sessions"
                ws_sess.mkdir(parents=True, exist_ok=True)
                ws_sess_file = ws_sess / "s1.json"
                ws_sess_file.write_text('{"messages":[]}', encoding="utf-8")
                h4 = _Handler()
                routes._handle_media(h4, SimpleNamespace(
                    query=f"path={urllib.parse.quote(str(ws_sess_file.resolve()))}",
                    path="/api/media"))
                self.assertEqual(
                    h4.status, 403,
                    "profile webui_state/sessions/*.json must be denied")

    def test_media_endpoints_advertise_byte_range_support(self):
        routes_src = (REPO_ROOT / "api" / "routes.py").read_text(encoding="utf-8")
        self.assertIn("Accept-Ranges", routes_src)
        self.assertIn("Content-Range", routes_src)
        self.assertIn("206", routes_src)

    def test_session_media_token_allows_exact_image_path(self):
        from api import routes

        with tempfile.TemporaryDirectory() as tmpd:
            image = pathlib.Path(tmpd) / "card.png"
            image.write_bytes(b"\x89PNG\r\n\x1a\n")
            session = SimpleNamespace(messages=[{"role": "assistant", "content": f"MEDIA:{image}"}])
            with mock.patch.object(routes, "get_session", return_value=session):
                self.assertTrue(
                    routes._session_media_token_allows_image_path(
                        "s-media", image, {"image/png"}
                    )
                )

    def test_session_media_token_rejects_unmentioned_image_path(self):
        from api import routes

        with tempfile.TemporaryDirectory() as tmpd:
            image = pathlib.Path(tmpd) / "card.png"
            image.write_bytes(b"\x89PNG\r\n\x1a\n")
            session = SimpleNamespace(messages=[{"role": "assistant", "content": "MEDIA:/tmp/other.png"}])
            with mock.patch.object(routes, "get_session", return_value=session):
                self.assertFalse(
                    routes._session_media_token_allows_image_path(
                        "s-media", image, {"image/png"}
                    )
                )

    def test_session_media_token_rejects_non_image_path(self):
        from api import routes

        with tempfile.TemporaryDirectory() as tmpd:
            text_file = pathlib.Path(tmpd) / "notes.txt"
            text_file.write_text("secret", encoding="utf-8")
            session = SimpleNamespace(messages=[{"role": "assistant", "content": f"MEDIA:{text_file}"}])
            with mock.patch.object(routes, "get_session", return_value=session):
                self.assertFalse(
                    routes._session_media_token_allows_image_path(
                        "s-media", text_file, {"image/png"}
                    )
                )


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

    def test_webui_state_secret_files_denied(self):
        """#3234: /api/media must hard-deny WebUI state/secret files even though
        they live under an allowed root (the whole Hermes home is allowed).

        An authenticated session rendering attacker-influenced agent output that
        emits a file://  or MEDIA: link to settings.json / state.db / auth.json
        must NOT be able to fetch it through /api/media.
        """
        state_dir = pathlib.Path(TEST_STATE_DIR)
        state_dir.mkdir(parents=True, exist_ok=True)
        # settings.json by name (deny-by-filename)
        settings = state_dir / "settings.json"
        settings.write_text('{"secret":"value"}', encoding="utf-8")
        try:
            _, status, _ = self._get(
                "/api/media?path=" + urllib.request.quote(str(settings.resolve()))
            )
            self.assertEqual(
                status, 403,
                f"settings.json under the state dir must be denied, got {status}",
            )
        finally:
            settings.unlink(missing_ok=True)

        # a file inside the sessions/ state subdir (deny-by-dir)
        sess_dir = state_dir / "sessions"
        sess_dir.mkdir(parents=True, exist_ok=True)
        sess_file = sess_dir / "abc123.json"
        sess_file.write_text('{"messages":[]}', encoding="utf-8")
        try:
            _, status, _ = self._get(
                "/api/media?path=" + urllib.request.quote(str(sess_file.resolve()))
            )
            self.assertEqual(
                status, 403,
                f"files under the sessions/ state subdir must be denied, got {status}",
            )
        finally:
            sess_file.unlink(missing_ok=True)

    def test_deny_list_does_not_overblock_legitimate_media(self):
        """#3234 follow-up: the state/secret deny-list must NOT block ordinary
        media that merely shares a sensitive basename but lives OUTSIDE any
        Hermes state root (e.g. a user artifact in /tmp named settings.json).

        The deny is scoped to files under a Hermes root; a /tmp PNG named
        settings.png — or even settings.json — is the user's own content and
        must still be served (200), not 403.
        """
        png_bytes = (
            b'\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01'
            b'\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\nIDATx\x9cc\x00'
            b'\x01\x00\x00\x05\x00\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82'
        )
        # A /tmp artifact whose stem collides with a denied basename — must serve
        # because /tmp is not a Hermes state root.
        with tempfile.NamedTemporaryFile(
            suffix=".png", prefix="settings_artifact_", dir="/tmp", delete=False
        ) as f:
            f.write(png_bytes)
            tmp_path = f.name
        try:
            body, status, headers = self._get(
                f"/api/media?path={urllib.request.quote(tmp_path)}"
            )
            self.assertEqual(
                status, 200,
                f"a /tmp PNG outside any Hermes root must serve, got {status}",
            )
            self.assertIn("image/png", headers.get("Content-Type", ""))
        finally:
            pathlib.Path(tmp_path).unlink(missing_ok=True)

    def test_health_check_still_works(self):
        """Sanity: server is up and /health works."""
        body, status, _ = self._get("/health")
        self.assertEqual(status, 200)
        d = json.loads(body)
        self.assertEqual(d["status"], "ok")
