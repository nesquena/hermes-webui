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
from tests.conftest import TEST_WORKSPACE

REPO_ROOT = pathlib.Path(__file__).parent.parent
UI_JS = (REPO_ROOT / "static" / "ui.js").read_text(encoding="utf-8")
I18N_JS = (REPO_ROOT / "static" / "i18n.js").read_text(encoding="utf-8")
WORKSPACE_JS = (REPO_ROOT / "static" / "workspace.js").read_text(encoding="utf-8")


def _media_fixture_dir() -> pathlib.Path:
    # Dot-prefixed so the persistent fixture dir stays OUT of
    # /api/workspaces/suggest's default (non-hidden) results — otherwise it
    # pollutes the shared TEST_WORKSPACE and breaks sibling tests that assert an
    # exact workspace-suggestion set (e.g. test_sprint5
    # test_workspace_suggest_hidden_dirs_only_when_requested). Kept under
    # TEST_WORKSPACE (a MEDIA_ALLOWED_ROOT) and Windows-portable (no /tmp).
    fixture_dir = TEST_WORKSPACE / ".media-fixtures"
    fixture_dir.mkdir(parents=True, exist_ok=True)
    return fixture_dir


def _media_token_fixture_dir() -> pathlib.Path:
    """A writable directory that is NOT an /api/media allowed root.

    ``_handle_media`` hardcodes /tmp as an allowed root (screenshots land
    there), and the review sandbox sets HOME=/tmp, so an exact-token fixture
    under the OS temp dir would be authorized by the ALLOWED-ROOT branch and
    the request would never exercise the token grant (it would only reach it
    by precedence). The OS temp dir is used only when it lives OUTSIDE the
    /tmp root — macOS (/var/folders) and Windows (%TEMP%; ``Path('/tmp')``
    does not exist there). On Linux the /dev/shm tmpfs is used instead, which
    is outside every allowed root. Falls back to the OS temp dir when no
    outside-/tmp location is available.
    """
    tmp_root = pathlib.Path("/tmp").resolve()
    for cand in (
        pathlib.Path(tempfile.gettempdir()).resolve(),
        pathlib.Path("/dev/shm"),
    ):
        if cand == tmp_root or tmp_root in cand.parents:
            continue
        try:
            probe = pathlib.Path(
                tempfile.mkdtemp(prefix="hermes_media_token_probe_", dir=str(cand))
            )
            probe.rmdir()
            return cand
        except OSError:
            continue
    return pathlib.Path(tempfile.gettempdir())


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

    def test_inline_preview_mime_whitelist_exists(self):
        """Only the explicit safe preview whitelist should be eligible for inline display."""
        routes_src = (REPO_ROOT / "api" / "routes.py").read_text(encoding="utf-8")
        self.assertIn("_INLINE_IMAGE_TYPES", routes_src,
                      "_INLINE_IMAGE_TYPES whitelist must exist in _handle_media")
        self.assertIn("_AUDIO_VIDEO_PDF_TYPES", routes_src,
                      "shared audio/video/PDF preview MIME whitelist must exist in _handle_media")
        self.assertIn('{"text/html"}', routes_src,
                      "HTML must be added only to the session-token whitelist")

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

    def test_sibling_base_profile_webui_state_dir_denied_when_named_profile_active(self):
        """#6982: /api/media must NOT serve a base/sibling profile's WebUI state
        dir (<root>/webui — the default STATE_DIR) when a NAMED profile is
        active. Only the active STATE_DIR was previously denied (as a root), so
        base `~/.hermes/webui/sessions/...` and sibling
        `<root>/profiles/<other>/webui/sessions/...` were served with a 200.
        Fail-closed: deny every enumerated root/profile-root's `<root>/webui`
        state subtree, while `<root>/webui/workspace` media stays servable.
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
            # Base profile's WebUI state dir (~/.hermes/webui) and its workspace.
            base_webui = base / "webui"
            base_sessions = base_webui / "sessions"
            base_ws = base_webui / "workspace"
            base_sessions.mkdir(parents=True)
            base_ws.mkdir(parents=True)
            base_index = base_sessions / "_index.json"
            base_index.write_text('{"sessions":[]}', encoding="utf-8")
            base_shot = base_ws / "shot.png"
            base_shot.write_bytes(png_bytes)
            # Sibling named profile's WebUI state dir.
            sibling = base / "profiles" / "other"
            sibling_webui = sibling / "webui"
            sibling_sessions = sibling_webui / "sessions"
            sibling_sessions.mkdir(parents=True)
            sibling_sess = sibling_sessions / "s1.json"
            sibling_sess.write_text('{"messages":[]}', encoding="utf-8")
            # Active named profile's own state dir + workspace.
            active = base / "profiles" / "webui"
            active_webui = active / "webui"
            active_sessions = active_webui / "sessions"
            active_ws = active_webui / "workspace"
            active_sessions.mkdir(parents=True)
            active_ws.mkdir(parents=True)
            active_sess = active_sessions / "s2.json"
            active_sess.write_text('{"messages":[]}', encoding="utf-8")
            active_shot = active_ws / "shot.png"
            active_shot.write_bytes(png_bytes)

            env = {
                "HERMES_HOME": str(active),
                "HERMES_WEBUI_STATE_DIR": str(active_webui),
            }
            with mock.patch.dict(os.environ, env), \
                 mock.patch.object(routes, "get_last_workspace", lambda: str(active_ws)), \
                 mock.patch("api.auth.is_auth_enabled", lambda: False), \
                 mock.patch("api.config.STATE_DIR", active_webui), \
                 mock.patch("api.profiles._DEFAULT_HERMES_HOME", base):
                # Base profile's WebUI session index → DENIED (was 200, #6982)
                h1 = _Handler()
                routes._handle_media(h1, SimpleNamespace(
                    query=f"path={urllib.parse.quote(str(base_index.resolve()))}",
                    path="/api/media"))
                self.assertEqual(
                    h1.status, 403,
                    "base profile <root>/webui/sessions/_index.json must be denied "
                    "when a named profile is active (#6982)")
                # Sibling named profile's WebUI session file → DENIED
                h2 = _Handler()
                routes._handle_media(h2, SimpleNamespace(
                    query=f"path={urllib.parse.quote(str(sibling_sess.resolve()))}",
                    path="/api/media"))
                self.assertEqual(
                    h2.status, 403,
                    "sibling profile <root>/profiles/other/webui/sessions must be denied")
                # Active named profile's own state → still denied
                h3 = _Handler()
                routes._handle_media(h3, SimpleNamespace(
                    query=f"path={urllib.parse.quote(str(active_sess.resolve()))}",
                    path="/api/media"))
                self.assertEqual(
                    h3.status, 403,
                    "active named profile's own webui/sessions must stay denied")
                # Base profile's <root>/webui/workspace media → still servable
                h4 = _Handler()
                routes._handle_media(h4, SimpleNamespace(
                    query=f"path={urllib.parse.quote(str(base_shot.resolve()))}&inline=1",
                    path="/api/media"))
                self.assertNotEqual(
                    h4.status, 403,
                    "base <root>/webui/workspace/shot.png must NOT be blocked (legit media)")
                # Active profile's workspace media → still servable
                h5 = _Handler()
                routes._handle_media(h5, SimpleNamespace(
                    query=f"path={urllib.parse.quote(str(active_shot.resolve()))}&inline=1",
                    path="/api/media"))
                self.assertNotEqual(
                    h5.status, 403,
                    "active profile's webui/workspace/shot.png must NOT be blocked")

    def test_media_serve_binds_authorization_to_opened_object_swap_toctou(self):
        """#6988 review: authorization must be bound to the object actually
        opened. /api/media resolves + allow/deny-checks `target` first, then
        serves it; if an ancestor is swapped for a symlink to a denied webui
        state dir between the check and the final open, the SAME
        already-authorized pathname must NOT open the denied object.

        (a) A plain file under an allowed tree serves normally (200 + body).
        (b) After swapping an ancestor (`alias`) for a symlink to the denied
        active-profile <root>/webui/sessions dir, the same pathname must be
        refused at open time (component-anchored openat + O_NOFOLLOW) — the
        denied state content must never reach the response, even though the
        path was authorized before the swap.
        """
        import shutil
        from api import routes

        class _CaptureHandler:
            def __init__(self):
                self.status = None
                self.headers = {}
                self.body = bytearray()
                self.wfile = self
            def send_response(self, code):
                self.status = code
            def send_header(self, *a, **k):
                pass
            def end_headers(self):
                pass
            def write(self, b):
                self.body.extend(b)
            def flush(self):
                pass

        with tempfile.TemporaryDirectory() as home:
            base = pathlib.Path(home) / ".hermes"
            # Active named profile (HERMES_HOME) with its WebUI state dir and
            # the default workspace under it (STATE_DIR/workspace).
            active = base / "profiles" / "webui"
            active_webui = active / "webui"
            denied_sessions = active_webui / "sessions"
            ws = active_webui / "workspace"
            denied_sessions.mkdir(parents=True)
            ws.mkdir(parents=True)
            # Denied state object with the SAME relative name the request uses.
            denied_state = denied_sessions / "payload.json"
            denied_state.write_text(
                '{"top-secret-session":"do-not-leak","messages":[]}',
                encoding="utf-8")
            # Allowed, attacker-mutable tree inside the active workspace.
            alias_dir = ws / "media_swap" / "alias"
            alias_dir.mkdir(parents=True)
            innocent = alias_dir / "payload.json"
            innocent.write_text('{"ok":true,"payload":"innocent"}', encoding="utf-8")

            env = {
                "HERMES_HOME": str(active),
                "HERMES_WEBUI_STATE_DIR": str(active_webui),
            }
            with mock.patch.dict(os.environ, env), \
                 mock.patch.object(routes, "get_last_workspace", lambda: str(ws)), \
                 mock.patch("api.auth.is_auth_enabled", lambda: False), \
                 mock.patch("api.config.STATE_DIR", active_webui), \
                 mock.patch("api.profiles._DEFAULT_HERMES_HOME", base):
                request_path = (
                    "path="
                    + urllib.parse.quote(str((alias_dir / "payload.json").resolve()))
                )

                # (a) Pre-swap: the same pathname serves the innocent file.
                h1 = _CaptureHandler()
                routes._handle_media(h1, SimpleNamespace(
                    query=request_path, path="/api/media"))
                self.assertEqual(
                    h1.status, 200,
                    "innocent file under the allowed workspace must serve (200)")
                self.assertIn(
                    b"innocent", h1.body,
                    "pre-swap body must be the innocent file content")

                # (b) Swap the ancestor for a symlink to the denied webui
                # state dir between check and open (simulated inside the first
                # os.open call, i.e. after allow/deny evaluation). With the
                # anchor_root binding the open is component-anchored and the
                # swapped component is refused; without it, the same pathname
                # re-traversed by name would open the denied state object.
                real_open = os.open
                swapped = {"done": False}

                def _swap_on_first_open(path, *args, _real_open=real_open, _swapped=swapped, _alias_dir=alias_dir, _denied_sessions=denied_sessions, **kwargs):
                    if not _swapped["done"]:
                        _swapped["done"] = True
                        shutil.rmtree(str(_alias_dir))
                        os.symlink(str(_denied_sessions), str(_alias_dir))
                    return _real_open(path, *args, **kwargs)

                h2 = _CaptureHandler()
                with mock.patch.object(os, "open", autospec=True,
                                       side_effect=_swap_on_first_open):
                    routes._handle_media(h2, SimpleNamespace(
                        query=request_path, path="/api/media"))
                self.assertNotEqual(
                    h2.status, 200,
                    "swapped-ancestor open must not serve the denied state object")
                self.assertNotIn(
                    b"top-secret-session", h2.body,
                    "denied webui state content must never reach the response "
                    "(authorization must be bound to the opened object)")

    def test_media_serve_swapped_allowed_root_pathname_fails_closed(self):
        """#6988 round 2: replacing the SELECTED ALLOWED-ROOT pathname between
        authorization and open must never serve replacement state bytes.

        Round 1 anchored the open with ``anchor_root``, but that anchor was
        itself a PATHNAME re-resolved at open time (open_anchored_fd calls
        workspace.resolve() again), so replacing the allowed-root pathname for
        a symlink to a denied webui state dir made root and target rebound
        together into the replacement tree. Round 2 retains the root DIRECTORY
        FD at authorization time (O_DIRECTORY|O_NOFOLLOW) and walks
        pre-computed relative components from that fd:

        - swap BEFORE the retention open (inside the first os.open call, after
          all stat-based checks): the O_NOFOLLOW root open refuses the swapped
          pathname (fail closed);
        - swap AFTER the retention open (inside the second os.open call, i.e.
          at the first serve-walk component): the walk from the RETAINED fd
          stays in the original tree and serves the original object — a
          pathname re-open would have rebound into the replacement tree.

        Neither schedule can return denied bytes, for full or range requests.
        """
        import shutil
        from api import routes

        class _CaptureHandler:
            def __init__(self, headers=None):
                self.status = None
                self.headers = headers or {}
                self.body = bytearray()
                self.wfile = self
            def send_response(self, code):
                self.status = code
            def send_header(self, *a, **k):
                pass
            def end_headers(self):
                pass
            def write(self, b):
                self.body.extend(b)
            def flush(self):
                pass

        with tempfile.TemporaryDirectory() as home:
            base = pathlib.Path(home) / ".hermes"
            active = base / "profiles" / "webui"
            active_webui = active / "webui"
            denied_sessions = active_webui / "sessions"
            ws = active_webui / "workspace"
            denied_sessions.mkdir(parents=True)
            ws.mkdir(parents=True)
            # Denied state object with the SAME relative name the request uses.
            denied_state = denied_sessions / "payload.json"
            denied_state.write_text(
                '{"top-secret-session":"do-not-leak","messages":[]}',
                encoding="utf-8")
            # Innocent file served directly from the workspace root (the
            # SELECTED allowed root for this request).
            innocent = ws / "payload.json"
            innocent.write_text('{"ok":true,"payload":"innocent"}', encoding="utf-8")
            moved = ws.parent / (ws.name + "_moved")

            env = {
                "HERMES_HOME": str(active),
                "HERMES_WEBUI_STATE_DIR": str(active_webui),
            }
            with mock.patch.dict(os.environ, env), \
                 mock.patch.object(routes, "get_last_workspace", lambda: str(ws)), \
                 mock.patch("api.workspace.get_last_workspace", lambda: str(ws)), \
                 mock.patch("api.auth.is_auth_enabled", lambda: False), \
                 mock.patch("api.config.STATE_DIR", active_webui), \
                 mock.patch("api.profiles._DEFAULT_HERMES_HOME", base):
                request_path = (
                    "path=" + urllib.parse.quote(str((ws / "payload.json").resolve()))
                )

                # Baseline: the same pathname serves the innocent file.
                h1 = _CaptureHandler()
                routes._handle_media(h1, SimpleNamespace(
                    query=request_path, path="/api/media"))
                self.assertEqual(
                    h1.status, 200,
                    "innocent file at the selected root must serve (200)")
                self.assertIn(b"innocent", h1.body)

                def _restore():
                    if ws.is_symlink():
                        ws.unlink()
                        if moved.exists():
                            os.rename(str(moved), str(ws))
                        else:
                            ws.mkdir(parents=True)
                            innocent.write_text(
                                '{"ok":true,"payload":"innocent"}', encoding="utf-8")

                for trigger in ("before_retention", "after_retention"):
                    for range_header in (None, {"Range": "bytes=0-100"}):
                        _restore()
                        real_open = os.open
                        swapped = {"done": False}
                        calls = {"n": 0}

                        def _swap_root(path, *args, _real_open=real_open, _swapped=swapped, _moved=moved, _ws=ws, _denied_sessions=denied_sessions, **kwargs):
                            # Rename the real root dir aside, then put a
                            # symlink to the denied tree at the root pathname.
                            if not _swapped["done"]:
                                _swapped["done"] = True
                                shutil.rmtree(str(_moved), ignore_errors=True)
                                os.rename(str(_ws), str(_moved))
                                os.symlink(str(_denied_sessions), str(_ws))
                            return _real_open(path, *args, **kwargs)

                        def _swap_root_after_retention(path, *args, _real_open=real_open, _swapped=swapped, _calls=calls, _moved=moved, _ws=ws, _denied_sessions=denied_sessions, **kwargs):
                            _calls["n"] += 1
                            if _calls["n"] == 2 and not _swapped["done"]:
                                _swapped["done"] = True
                                shutil.rmtree(str(_moved), ignore_errors=True)
                                os.rename(str(_ws), str(_moved))
                                os.symlink(str(_denied_sessions), str(_ws))
                            return _real_open(path, *args, **kwargs)

                        side_effect = (
                            _swap_root if trigger == "before_retention"
                            else _swap_root_after_retention
                        )
                        h2 = _CaptureHandler(range_header)
                        with mock.patch.object(os, "open", autospec=True,
                                               side_effect=side_effect):
                            routes._handle_media(h2, SimpleNamespace(
                                query=request_path, path="/api/media"))
                        self.assertNotIn(
                            b"top-secret-session", h2.body,
                            "denied webui state bytes must never reach the "
                            "response (swapped allowed-root pathname) "
                            f"[trigger={trigger}, range={bool(range_header)}]")
                        if trigger == "before_retention":
                            self.assertNotEqual(
                                h2.status, 200,
                                "swapped allowed-root open must be refused "
                                "(O_NOFOLLOW on the retained root open)")
                        else:
                            self.assertIn(
                                h2.status, (200, 206),
                                "walk from the RETAINED root fd must keep "
                                "serving the original tree")
                            self.assertIn(
                                b"innocent", h2.body,
                                "retained-fd walk must serve the ORIGINAL "
                                "object, not the replacement tree")

    def test_media_serve_swapped_exact_token_parent_fails_closed(self):
        """#6988 round 2: the exact-token MEDIA grant must not use mutable
        target.parent as its anchor authority.

        The grant retains an already-verified LEAF fd (walked with O_NOFOLLOW
        from the filesystem anchor), so replacing the token target's parent
        with a symlink to a denied webui state dir — before or during the
        retention walk — is refused, and neither full nor range responses can
        return the replacement state bytes.
        """
        import shutil
        from api import routes

        class _CaptureHandler:
            def __init__(self, headers=None):
                self.status = None
                self.headers = headers or {}
                self.body = bytearray()
                self.wfile = self
            def send_response(self, code):
                self.status = code
            def send_header(self, *a, **k):
                pass
            def end_headers(self):
                pass
            def write(self, b):
                self.body.extend(b)
            def flush(self):
                pass

        token_dir = None
        try:
            # Token-granted file OUTSIDE every allowed root (the OS temp dir
            # is itself an allowed /api/media root — /tmp is hardcoded, and
            # the review sandbox sets HOME=/tmp — so a fixture there would be
            # authorized by the allowed-root branch and only reach the token
            # branch by precedence). The fixture root is chosen outside /tmp
            # so the exact-token grant is the ONLY thing that can serve it.
            token_dir = pathlib.Path(tempfile.mkdtemp(
                prefix="hermes_media_token_", dir=str(_media_token_fixture_dir())))
            moved = pathlib.Path(str(token_dir) + "_moved")
            with tempfile.TemporaryDirectory() as home:
                base = pathlib.Path(home) / ".hermes"
                active = base / "profiles" / "webui"
                active_webui = active / "webui"
                denied_sessions = active_webui / "sessions"
                ws = active_webui / "workspace"
                denied_sessions.mkdir(parents=True)
                ws.mkdir(parents=True)
                # Denied state object with the SAME relative name the request
                # uses, so a redirected open would return these bytes.
                denied_state = denied_sessions / "report.html"
                denied_state.write_text(
                    "<!doctype html><title>top-secret-session</title>",
                    encoding="utf-8")
                innocent = token_dir / "report.html"
                innocent.write_text(
                    "<!doctype html><title>innocent</title>", encoding="utf-8")
                session = SimpleNamespace(messages=[
                    {"role": "assistant", "content": f"MEDIA:{innocent}"}])

                env = {
                    "HERMES_HOME": str(active),
                    "HERMES_WEBUI_STATE_DIR": str(active_webui),
                }
                with mock.patch.dict(os.environ, env), \
                     mock.patch.object(routes, "get_last_workspace", lambda: str(ws)), \
                     mock.patch.object(routes, "get_session", return_value=session), \
                     mock.patch("api.auth.is_auth_enabled", lambda: False), \
                     mock.patch("api.config.STATE_DIR", active_webui), \
                     mock.patch("api.profiles._DEFAULT_HERMES_HOME", base):
                    request_path = (
                        "path=" + urllib.parse.quote(str(innocent.resolve()))
                        + "&session_id=s-media&inline=1"
                    )

                    # Baseline: the token-granted file serves normally.
                    h1 = _CaptureHandler()
                    routes._handle_media(h1, SimpleNamespace(
                        query=request_path, path="/api/media"))
                    self.assertEqual(
                        h1.status, 200,
                        "token-granted file must serve (200)")
                    self.assertIn(b"innocent", h1.body)

                    def _restore():
                        if token_dir.is_symlink():
                            token_dir.unlink()
                            if moved.exists():
                                os.rename(str(moved), str(token_dir))
                            else:
                                token_dir.mkdir(parents=True)
                                innocent.write_text(
                                    "<!doctype html><title>innocent</title>",
                                    encoding="utf-8")

                    for trigger in ("before_retention", "after_retention"):
                        for range_header in (None, {"Range": "bytes=0-100"}):
                            _restore()
                            real_open = os.open
                            swapped = {"done": False}
                            calls = {"n": 0}

                            def _swap_parent(path, *args, _real_open=real_open, _swapped=swapped, _moved=moved, _token_dir=token_dir, _denied_sessions=denied_sessions, **kwargs):
                                # Rename the token parent dir aside, then put a
                                # symlink to the denied webui state dir at the
                                # parent pathname.
                                if not _swapped["done"]:
                                    _swapped["done"] = True
                                    shutil.rmtree(str(_moved), ignore_errors=True)
                                    os.rename(str(_token_dir), str(_moved))
                                    os.symlink(str(_denied_sessions), str(_token_dir))
                                return _real_open(path, *args, **kwargs)

                            def _swap_parent_midwalk(path, *args, _real_open=real_open, _swapped=swapped, _calls=calls, _moved=moved, _token_dir=token_dir, _denied_sessions=denied_sessions, **kwargs):
                                _calls["n"] += 1
                                if _calls["n"] == 2 and not _swapped["done"]:
                                    _swapped["done"] = True
                                    shutil.rmtree(str(_moved), ignore_errors=True)
                                    os.rename(str(_token_dir), str(_moved))
                                    os.symlink(str(_denied_sessions), str(_token_dir))
                                return _real_open(path, *args, **kwargs)

                            side_effect = (
                                _swap_parent if trigger == "before_retention"
                                else _swap_parent_midwalk
                            )
                            h2 = _CaptureHandler(range_header)
                            with mock.patch.object(os, "open", autospec=True,
                                                   side_effect=side_effect):
                                routes._handle_media(h2, SimpleNamespace(
                                    query=request_path, path="/api/media"))
                            self.assertNotEqual(
                                h2.status, 200,
                                "swapped exact-token parent must not serve "
                                "(the grant authority is the retained leaf fd, "
                                "not target.parent) "
                                f"[trigger={trigger}, range={bool(range_header)}]")
                            self.assertNotIn(
                                b"top-secret-session", h2.body,
                                "denied webui state bytes must never reach the "
                                "response (swapped exact-token target parent) "
                                f"[trigger={trigger}, range={bool(range_header)}]")
        finally:
            if token_dir is not None:
                if token_dir.is_symlink():
                    token_dir.unlink()
                shutil.rmtree(str(token_dir), ignore_errors=True)
                shutil.rmtree(str(pathlib.Path(str(token_dir) + "_moved")),
                              ignore_errors=True)

    def test_media_fails_closed_without_dir_fd_allowed_root_grant(self):
        """#6988 round 3: on platforms without dir_fd (no secure anchored-open
        capability — Windows), /api/media must FAIL CLOSED (403) for
        allowed-root grants instead of silently re-resolving/opening the
        authorizing root/target pathnames (the known-vulnerable path). Covers
        full and Range responses, and proves replacement state bytes cannot be
        returned even when the allowed-root pathname is swapped to a secret
        location BEFORE the request — a pathname re-open would have served it.
        """
        import shutil
        from api import routes

        class _CaptureHandler:
            def __init__(self, headers=None):
                self.status = None
                self.headers = headers or {}
                self.body = bytearray()
                self.wfile = self
            def send_response(self, code):
                self.status = code
            def send_header(self, *a, **k):
                pass
            def end_headers(self):
                pass
            def write(self, b):
                self.body.extend(b)
            def flush(self):
                pass

        with tempfile.TemporaryDirectory() as home:
            base = pathlib.Path(home) / ".hermes"
            active = base / "profiles" / "webui"
            active_webui = active / "webui"
            ws = active_webui / "workspace"
            ws.mkdir(parents=True)
            innocent = ws / "payload.json"
            innocent.write_text('{"ok":true,"payload":"innocent"}', encoding="utf-8")
            # Secret location that is NOT deny-listed (so the deny gate cannot
            # mask the fail-closed): a plain pathname open through the swapped
            # symlink would serve these bytes on the pre-round-3 fallback.
            secret_stash = active_webui / "workspace_secret_stash"
            secret_stash.mkdir(parents=True)
            secret = secret_stash / "payload.json"
            secret.write_text(
                '{"top-secret-session":"do-not-leak","messages":[]}',
                encoding="utf-8")
            moved = ws.parent / (ws.name + "_moved")

            env = {
                "HERMES_HOME": str(active),
                "HERMES_WEBUI_STATE_DIR": str(active_webui),
            }
            with mock.patch.dict(os.environ, env), \
                 mock.patch.object(routes, "get_last_workspace", lambda: str(ws)), \
                 mock.patch("api.workspace.get_last_workspace", lambda: str(ws)), \
                 mock.patch("api.auth.is_auth_enabled", lambda: False), \
                 mock.patch("api.config.STATE_DIR", active_webui), \
                 mock.patch("api.profiles._DEFAULT_HERMES_HOME", base):
                request_path = (
                    "path=" + urllib.parse.quote(str((ws / "payload.json").resolve()))
                )

                # Control: with dir_fd the same request serves the innocent file.
                h1 = _CaptureHandler()
                routes._handle_media(h1, SimpleNamespace(
                    query=request_path, path="/api/media"))
                self.assertEqual(
                    h1.status, 200,
                    "control: allowed-root request must serve with dir_fd")
                self.assertIn(b"innocent", h1.body)

                def _restore():
                    if ws.is_symlink():
                        ws.unlink()
                        if moved.exists():
                            os.rename(str(moved), str(ws))
                        else:
                            ws.mkdir(parents=True)
                            innocent.write_text(
                                '{"ok":true,"payload":"innocent"}', encoding="utf-8")

                # Fail-closed on the no-dir_fd platform: full and Range.
                for range_header in (None, {"Range": "bytes=0-100"}):
                    _restore()
                    with mock.patch("api.routes._DIR_FD_OK", False):
                        h2 = _CaptureHandler(range_header)
                        routes._handle_media(h2, SimpleNamespace(
                            query=request_path, path="/api/media"))
                    self.assertEqual(
                        h2.status, 403,
                        "no-dir_fd platform must fail closed for allowed-root "
                        f"grants [range={bool(range_header)}]")
                    self.assertNotIn(
                        b"top-secret-session", h2.body,
                        "denied bytes must never reach the response")
                    self.assertNotIn(
                        b"innocent", h2.body,
                        "fail-closed must not serve the file either")

                # Replacement-bytes proof: swap the allowed-root pathname for a
                # symlink to the secret location BEFORE the request. Without
                # dir_fd the authority fd cannot be retained, so the route must
                # fail closed — never open the swapped pathname by name.
                for range_header in (None, {"Range": "bytes=0-100"}):
                    _restore()
                    os.rename(str(ws), str(moved))
                    os.symlink(str(secret_stash), str(ws))
                    with mock.patch("api.routes._DIR_FD_OK", False):
                        h3 = _CaptureHandler(range_header)
                        routes._handle_media(h3, SimpleNamespace(
                            query=request_path, path="/api/media"))
                    self.assertEqual(
                        h3.status, 403,
                        "swapped allowed-root pathname must fail closed without "
                        f"dir_fd [range={bool(range_header)}]")
                    self.assertNotIn(
                        b"top-secret-session", h3.body,
                        "replacement state bytes must never be returned "
                        f"[range={bool(range_header)}]")
                _restore()

    def test_media_fails_closed_without_dir_fd_exact_token_grant(self):
        """#6988 round 3: without dir_fd the exact-token MEDIA grant must also
        FAIL CLOSED (403) — the verified leaf fd cannot be retained without
        openat, and a plain pathname open of the resolved leaf would re-expose
        the authorization-to-open race. The token fixture lives OUTSIDE every
        allowed root, so the token is the ONLY grantor (not /tmp precedence).
        Covers full and Range responses, and proves replacement state bytes
        cannot be returned even when the token target's parent is swapped to a
        secret location BEFORE the request.
        """
        import shutil
        from api import routes

        class _CaptureHandler:
            def __init__(self, headers=None):
                self.status = None
                self.headers = headers or {}
                self.body = bytearray()
                self.wfile = self
            def send_response(self, code):
                self.status = code
            def send_header(self, *a, **k):
                pass
            def end_headers(self):
                pass
            def write(self, b):
                self.body.extend(b)
            def flush(self):
                pass

        token_dir = None
        try:
            token_dir = pathlib.Path(tempfile.mkdtemp(
                prefix="hermes_media_token_", dir=str(_media_token_fixture_dir())))
            moved = pathlib.Path(str(token_dir) + "_moved")
            with tempfile.TemporaryDirectory() as home:
                base = pathlib.Path(home) / ".hermes"
                active = base / "profiles" / "webui"
                active_webui = active / "webui"
                ws = active_webui / "workspace"
                ws.mkdir(parents=True)
                # Non-deny-listed secret location under an allowed root (the
                # deny gate cannot mask the fail-closed for the swapped case).
                secret_stash = active_webui / "workspace_secret_stash"
                secret_stash.mkdir(parents=True)
                secret = secret_stash / "report.html"
                secret.write_text(
                    "<!doctype html><title>top-secret-session</title>",
                    encoding="utf-8")
                innocent = token_dir / "report.html"
                innocent.write_text(
                    "<!doctype html><title>innocent</title>", encoding="utf-8")
                session = SimpleNamespace(messages=[
                    {"role": "assistant", "content": f"MEDIA:{innocent}"}])

                env = {
                    "HERMES_HOME": str(active),
                    "HERMES_WEBUI_STATE_DIR": str(active_webui),
                }
                with mock.patch.dict(os.environ, env), \
                     mock.patch.object(routes, "get_last_workspace", lambda: str(ws)), \
                     mock.patch("api.workspace.get_last_workspace", lambda: str(ws)), \
                     mock.patch.object(routes, "get_session", return_value=session), \
                     mock.patch("api.auth.is_auth_enabled", lambda: False), \
                     mock.patch("api.config.STATE_DIR", active_webui), \
                     mock.patch("api.profiles._DEFAULT_HERMES_HOME", base):
                    request_path = (
                        "path=" + urllib.parse.quote(str(innocent.resolve()))
                        + "&session_id=s-media&inline=1"
                    )

                    # Control: with dir_fd the token-granted file serves.
                    h1 = _CaptureHandler()
                    routes._handle_media(h1, SimpleNamespace(
                        query=request_path, path="/api/media"))
                    self.assertEqual(
                        h1.status, 200,
                        "control: token-granted file must serve with dir_fd")
                    self.assertIn(b"innocent", h1.body)

                    # Fail-closed on the no-dir_fd platform: full and Range.
                    for range_header in (None, {"Range": "bytes=0-100"}):
                        with mock.patch("api.routes._DIR_FD_OK", False):
                            h2 = _CaptureHandler(range_header)
                            routes._handle_media(h2, SimpleNamespace(
                                query=request_path, path="/api/media"))
                        self.assertEqual(
                            h2.status, 403,
                            "no-dir_fd platform must fail closed for exact-token "
                            f"grants [range={bool(range_header)}]")
                        self.assertNotIn(
                            b"top-secret-session", h2.body,
                            "denied bytes must never reach the response")
                        self.assertNotIn(
                            b"innocent", h2.body,
                            "fail-closed must not serve the file either")

                    # Replacement-bytes proof: swap the token target's parent
                    # for a symlink to the secret location BEFORE the request.
                    # The token still resolves (pathname-based), but without
                    # dir_fd the leaf fd cannot be retained, so the route must
                    # fail closed — never open the swapped pathname by name.
                    for range_header in (None, {"Range": "bytes=0-100"}):
                        # Restore the real dir at token_dir first (a previous
                        # iteration left it renamed aside at `moved`).
                        if token_dir.is_symlink():
                            token_dir.unlink()
                        if moved.exists():
                            os.rename(str(moved), str(token_dir))
                        os.rename(str(token_dir), str(moved))
                        os.symlink(str(secret_stash), str(token_dir))
                        with mock.patch("api.routes._DIR_FD_OK", False):
                            h3 = _CaptureHandler(range_header)
                            routes._handle_media(h3, SimpleNamespace(
                                query=request_path, path="/api/media"))
                        self.assertEqual(
                            h3.status, 403,
                            "swapped exact-token parent must fail closed without "
                            f"dir_fd [range={bool(range_header)}]")
                        self.assertNotIn(
                            b"top-secret-session", h3.body,
                            "replacement state bytes must never be returned "
                            f"[range={bool(range_header)}]")
        finally:
            if token_dir is not None:
                if token_dir.is_symlink():
                    token_dir.unlink()
                shutil.rmtree(str(token_dir), ignore_errors=True)
                shutil.rmtree(str(pathlib.Path(str(token_dir) + "_moved")),
                              ignore_errors=True)

    def test_media_allowed_roots_env_var_serves_outside_hermes_root(self):
        """MEDIA_ALLOWED_ROOTS must still allow legitimate outside-root media."""
        from api import routes

        class _Handler:
            def __init__(self):
                self.status = None
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
            headers = {}

        png_bytes = (
            b'\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01'
            b'\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\nIDATx\x9cc\x00'
            b'\x01\x00\x00\x05\x00\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82'
        )
        with tempfile.TemporaryDirectory() as home, tempfile.TemporaryDirectory() as extra:
            hermes_home = pathlib.Path(home) / ".hermes"
            hermes_home.mkdir(parents=True)
            outside_root = pathlib.Path(extra).resolve()
            image = outside_root / "settings_artifact.png"
            image.write_bytes(png_bytes)

            with mock.patch.dict(
                os.environ,
                {
                    "HERMES_HOME": str(hermes_home),
                    "MEDIA_ALLOWED_ROOTS": str(outside_root),
                },
            ), mock.patch.object(
                routes, "get_last_workspace", lambda: str(hermes_home / "workspace")
            ), mock.patch(
                "api.auth.is_auth_enabled", lambda: False
            ):
                handler = _Handler()
                routes._handle_media(
                    handler,
                    SimpleNamespace(
                        query=f"path={urllib.parse.quote(str(image))}&inline=1",
                        path="/api/media",
                    ),
                )

            self.assertEqual(
                handler.status, 200,
                "MEDIA_ALLOWED_ROOTS media outside Hermes roots must still serve",
            )

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

    def test_session_media_token_allows_exact_html_path_when_mime_is_safe(self):
        from api import routes

        with tempfile.TemporaryDirectory() as tmpd:
            html = pathlib.Path(tmpd) / "report.html"
            html.write_text("<h1>Report</h1>", encoding="utf-8")
            session = SimpleNamespace(messages=[{"role": "assistant", "content": f"MEDIA:{html}"}])
            with mock.patch.object(routes, "get_session", return_value=session):
                self.assertTrue(
                    routes._session_media_token_allows_path(
                        "s-media", html, {"text/html"}
                    )
                )

    def test_session_media_token_rejects_mentioned_html_when_mime_not_allowed(self):
        from api import routes

        with tempfile.TemporaryDirectory() as tmpd:
            html = pathlib.Path(tmpd) / "report.html"
            html.write_text("<h1>Report</h1>", encoding="utf-8")
            session = SimpleNamespace(messages=[{"role": "assistant", "content": f"MEDIA:{html}"}])
            with mock.patch.object(routes, "get_session", return_value=session):
                self.assertFalse(
                    routes._session_media_token_allows_path(
                        "s-media", html, {"image/png"}
                    )
                )

    def test_session_media_token_rejects_user_authored_html_path(self):
        from api import routes

        with tempfile.TemporaryDirectory() as tmpd:
            html = pathlib.Path(tmpd) / "report.html"
            html.write_text("<h1>Report</h1>", encoding="utf-8")
            session = SimpleNamespace(messages=[{"role": "user", "content": f"MEDIA:{html}"}])
            with mock.patch.object(routes, "get_session", return_value=session):
                self.assertFalse(
                    routes._session_media_token_allows_path(
                        "s-media", html, {"text/html"}
                    )
                )

    def test_handle_media_session_authorizes_html_artifact_outside_roots(self):
        from api import routes

        class _Handler:
            def __init__(self):
                self.status = None
                self.headers = {}
                self.body = b""
            def send_response(self, code):
                self.status = code
            def send_header(self, k, v):
                self.headers[k.lower()] = v
            def end_headers(self):
                pass
            class _W:
                def __init__(self, owner):
                    self.owner = owner
                def write(self, b):
                    self.owner.body += b
                def flush(self):
                    pass
            @property
            def wfile(self):
                return self._W(self)

        with tempfile.TemporaryDirectory() as home, tempfile.TemporaryDirectory() as outside:
            hermes_home = pathlib.Path(home) / ".hermes"
            hermes_home.mkdir(parents=True)
            ws = hermes_home / "workspace"
            ws.mkdir()
            html = pathlib.Path(outside) / "report.html"
            html.write_text("<h1>Report</h1>", encoding="utf-8")
            session = SimpleNamespace(messages=[{"role": "assistant", "content": f"MEDIA:{html}"}])
            with mock.patch.dict(os.environ, {"HERMES_HOME": str(hermes_home), "MEDIA_ALLOWED_ROOTS": ""}), \
                 mock.patch.object(routes, "get_last_workspace", lambda: str(ws)), \
                 mock.patch.object(routes, "get_session", return_value=session), \
                 mock.patch("api.auth.is_auth_enabled", lambda: False):
                handler = _Handler()
                routes._handle_media(
                    handler,
                    SimpleNamespace(
                        query=(
                            f"path={urllib.parse.quote(str(html.resolve()))}"
                            "&session_id=s-media&inline=1"
                        ),
                        path="/api/media",
                    ),
                )

            self.assertEqual(handler.status, 200)
            self.assertIn("text/html", handler.headers.get("content-type", ""))
            self.assertIn("sandbox", handler.headers.get("content-security-policy", ""))
            self.assertIn(b"Report", handler.body)


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
        missing = _media_fixture_dir() / "__hermes_nonexistent_12345.png"
        _, status, _ = self._get(
            "/api/media?path=" + urllib.parse.quote(str(missing))
        )
        self.assertEqual(status, 404)

    def test_path_outside_allowed_root_rejected(self):
        # /etc/passwd is outside allowed roots
        _, status, _ = self._get("/api/media?path=/etc/passwd")
        self.assertIn(status, {403, 404})

    def test_valid_png_served_with_image_mime(self):
        """Create a 1-pixel PNG in the isolated test workspace and verify it serves."""
        # Minimal valid 1x1 transparent PNG (67 bytes)
        png_bytes = (
            b'\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01'
            b'\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\nIDATx\x9cc\x00'
            b'\x01\x00\x00\x05\x00\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82'
        )
        with tempfile.NamedTemporaryFile(
            suffix=".png", prefix="hermes_test_", dir=_media_fixture_dir(), delete=False
        ) as f:
            f.write(png_bytes)
            tmp_path = f.name
        try:
            body, status, headers = self._get(
                f"/api/media?path={urllib.parse.quote(tmp_path)}"
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
            suffix=".wav", prefix="hermes_test_", dir=_media_fixture_dir(), delete=False
        ) as f:
            f.write(audio_bytes)
            tmp_path = f.name
        try:
            encoded = urllib.parse.quote(tmp_path)
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
            suffix=".html", prefix="hermes_test_", dir=_media_fixture_dir(), delete=False
        ) as f:
            f.write(html_bytes)
            tmp_path = f.name
        try:
            encoded = urllib.parse.quote(tmp_path)

            body, status, headers = self._get(f"/api/media?path={encoded}")
            self.assertEqual(status, 200)
            self.assertIn("text/html", headers.get("Content-Type", ""))
            self.assertIn("attachment", headers.get("Content-Disposition", ""))
            self.assertIn("DENY", headers.get_all("X-Frame-Options", []))
            self.assertFalse(
                any("sandbox allow-scripts" == h for h in headers.get_all("Content-Security-Policy", []))
            )
            self.assertEqual(body, html_bytes)
            # HTML responses must use no-store to prevent stale preview on
            # re-send of the same MEDIA: link (attachment branch).
            self.assertEqual(
                headers.get("Cache-Control"), "no-store",
                "HTML attachment must use Cache-Control: no-store"
            )

            body, status, headers = self._get(f"/api/media?path={encoded}&inline=1")
            self.assertEqual(status, 200)
            self.assertIn("text/html", headers.get("Content-Type", ""))
            self.assertIn("inline", headers.get("Content-Disposition", ""))
            self.assertEqual(headers.get_all("X-Frame-Options", []), [])
            self.assertTrue(
                any("sandbox allow-scripts" == h for h in headers.get_all("Content-Security-Policy", []))
            )
            self.assertEqual(body, html_bytes)
            # Inline HTML preview must also use no-store (inline branch).
            self.assertEqual(
                headers.get("Cache-Control"), "no-store",
                "Inline HTML preview must use Cache-Control: no-store"
            )
        finally:
            pathlib.Path(tmp_path).unlink(missing_ok=True)

    def test_path_traversal_rejected(self):
        _, status, _ = self._get(
            "/api/media?path=" + urllib.parse.quote("/tmp/../../etc/passwd")
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
                "/api/media?path=" + urllib.parse.quote(str(settings.resolve()))
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
                "/api/media?path=" + urllib.parse.quote(str(sess_file.resolve()))
            )
            self.assertEqual(
                status, 403,
                f"files under the sessions/ state subdir must be denied, got {status}",
            )
        finally:
            sess_file.unlink(missing_ok=True)

    def test_deny_list_does_not_overblock_active_workspace_media(self):
        """#3234 follow-up: workspace media with a sensitive-looking basename must
        still serve when it lives under the active workspace carve-out.
        """
        png_bytes = (
            b'\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01'
            b'\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\nIDATx\x9cc\x00'
            b'\x01\x00\x00\x05\x00\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82'
        )
        # A test-workspace artifact whose stem collides with a denied basename
        # must still serve because the active workspace carve-out allows
        # legitimate user media even when the test workspace lives under
        # TEST_STATE_DIR.
        with tempfile.NamedTemporaryFile(
            suffix=".png",
            prefix="settings_artifact_",
            dir=_media_fixture_dir(),
            delete=False,
        ) as f:
            f.write(png_bytes)
            tmp_path = f.name
        try:
            body, status, headers = self._get(
                f"/api/media?path={urllib.parse.quote(tmp_path)}"
            )
            self.assertEqual(
                status, 200,
                f"a test-workspace PNG under the active workspace must serve, got {status}",
            )
            self.assertIn("image/png", headers.get("Content-Type", ""))
        finally:
            pathlib.Path(tmp_path).unlink(missing_ok=True)

    def test_ts_artifact_served_as_text_plain_with_attachment(self):
        """.ts file via /api/media must have text/plain Content-Type,
        Content-Disposition: attachment, and X-Content-Type-Options: nosniff.
        Regression for PR #6372 — ensures narrow MIME_MAP fix is live."""
        ts_bytes = b"const x: number = 42;\nconsole.log(x);\n"
        with tempfile.NamedTemporaryFile(
            suffix=".ts", prefix="hermes_test_", dir=_media_fixture_dir(), delete=False
        ) as f:
            f.write(ts_bytes)
            tmp_path = f.name
        try:
            body, status, headers = self._get(
                f"/api/media?path={urllib.parse.quote(tmp_path)}"
            )
            self.assertEqual(status, 200)
            ct = headers.get("Content-Type", "")
            self.assertIn(
                "text/plain", ct,
                f"Expected text/plain Content-Type for .ts, got {ct}",
            )
            self.assertNotIn(
                "text/javascript", ct,
                f".ts must NOT be served as text/javascript, got {ct}",
            )
            disp = headers.get("Content-Disposition", "")
            self.assertIn(
                "attachment", disp,
                f"Expected attachment Content-Disposition for .ts, got {disp}",
            )
            self.assertEqual(
                headers.get("X-Content-Type-Options"),
                "nosniff",
                "X-Content-Type-Options: nosniff must be set on media responses",
            )
            self.assertEqual(body, ts_bytes)
        finally:
            pathlib.Path(tmp_path).unlink(missing_ok=True)

    def test_tsx_artifact_served_as_text_plain_with_attachment(self):
        """.tsx file via /api/media must also have text/plain Content-Type
        and attachment disposition. Regression for PR #6372."""
        tsx_bytes = b"const App: React.FC = () => <div>Hello</div>;\n"
        with tempfile.NamedTemporaryFile(
            suffix=".tsx", prefix="hermes_test_", dir=_media_fixture_dir(), delete=False
        ) as f:
            f.write(tsx_bytes)
            tmp_path = f.name
        try:
            body, status, headers = self._get(
                f"/api/media?path={urllib.parse.quote(tmp_path)}"
            )
            self.assertEqual(status, 200)
            ct = headers.get("Content-Type", "")
            self.assertIn(
                "text/plain", ct,
                f"Expected text/plain Content-Type for .tsx, got {ct}",
            )
            self.assertNotIn(
                "text/javascript", ct,
                f".tsx must NOT be served as text/javascript, got {ct}",
            )
            disp = headers.get("Content-Disposition", "")
            self.assertIn(
                "attachment", disp,
                f"Expected attachment Content-Disposition for .tsx, got {disp}",
            )
            self.assertEqual(
                headers.get("X-Content-Type-Options"),
                "nosniff",
            )
            self.assertEqual(body, tsx_bytes)
        finally:
            pathlib.Path(tmp_path).unlink(missing_ok=True)

    def test_file_raw_js_not_served_as_inline_executable(self):
        """.js files served via /api/file/raw must NOT get text/javascript
        Content-Type — they should fall through to application/octet-stream
        since MIME_MAP intentionally omits .js. Regression for PR #6372."""
        # Create a session so we can use /api/file/raw
        try:
            req = urllib.request.Request(
                BASE + "/api/session/new",
                data=b"{}",
                headers={"Content-Type": "application/json"},
            )
            with urllib.request.urlopen(req, timeout=10) as r:
                sess_data = json.loads(r.read())
        except urllib.error.HTTPError as e:
            sess_data = json.loads(e.read())
            self.fail(f"Cannot create test session: {sess_data}")
        sid = sess_data.get("session_id") or sess_data.get("session", {}).get("session_id", "")
        self.assertTrue(sid, f"No session_id in response: {sess_data}")
        ws = pathlib.Path(
            sess_data.get("workspace") or sess_data.get("session", {}).get("workspace", "")
        )
        self.assertTrue(str(ws), f"No workspace in response: {sess_data}")

        js_bytes = b"const x = 1;\n"
        js_file = ws / "exploit_test.js"
        try:
            js_file.write_bytes(js_bytes)

            encoded = urllib.parse.quote("exploit_test.js")
            body, status, headers = self._get(
                f"/api/file/raw?session_id={sid}&path={encoded}"
            )
            self.assertEqual(status, 200)
            ct = headers.get("Content-Type", "")
            self.assertNotIn(
                "text/javascript", ct,
                f".js via /api/file/raw must NOT be text/javascript, got {ct}",
            )
            # Without a .js MIME_MAP entry, it falls back to application/octet-stream
            self.assertEqual(
                ct, "application/octet-stream",
                f"Expected application/octet-stream for unmapped .js, got {ct}",
            )
            self.assertEqual(body, js_bytes)
        finally:
            js_file.unlink(missing_ok=True)

    def test_health_check_still_works(self):
        """Sanity: server is up and /health works."""
        body, status, _ = self._get("/health")
        self.assertEqual(status, 200)
        d = json.loads(body)
        self.assertEqual(d["status"], "ok")
