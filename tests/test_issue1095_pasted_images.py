"""Tests for #1095 — full fix covering both bugs:

Bug 1: Composer tray shows paperclip chip for images instead of thumbnail preview.
Bug 2: Chat history renders uploaded images as broken <img> (wrong endpoint / dead URL).
"""
import os
import re
import pytest


def _read_js(name):
    with open(os.path.join('static', name)) as f:
        return f.read()


def _read_css():
    with open(os.path.join('static', 'style.css')) as f:
        return f.read()


# ── Bug 1: Composer tray thumbnail previews ────────────────────────────────

class TestComposerTrayThumbnails:
    """renderTray() must show thumbnail previews for image files, not paperclip chips."""

    def test_rendertray_checks_image_extension(self):
        """renderTray must branch on _IMAGE_EXTS for the file object in S.pendingFiles."""
        ui = _read_js('ui.js')
        # Find renderTray function body
        idx = ui.find('function renderTray()')
        assert idx >= 0, 'renderTray() not found in ui.js'
        body = ui[idx:idx + 800]
        assert '_IMAGE_EXTS.test(' in body, 'renderTray must check _IMAGE_EXTS for thumbnail vs chip'

    def test_rendertray_uses_createobjecturl_for_images(self):
        """Image files must use URL.createObjectURL(f) to generate a blob URL for the thumbnail."""
        ui = _read_js('ui.js')
        idx = ui.find('function renderTray()')
        body = ui[idx:idx + 800]
        assert 'URL.createObjectURL(' in body, 'renderTray must use URL.createObjectURL for image thumbnails'

    def test_rendertray_revokes_blob_url_on_remove(self):
        """Blob URLs must be revoked when a file is removed to prevent memory leaks."""
        ui = _read_js('ui.js')
        idx = ui.find('function renderTray()')
        body = ui[idx:idx + 1200]
        assert 'URL.revokeObjectURL(' in body, 'renderTray must revoke blob URL when chip is removed'

    def test_rendertray_uses_attach_thumb_class(self):
        """Image chips must use attach-thumb class for the thumbnail <img> element."""
        ui = _read_js('ui.js')
        idx = ui.find('function renderTray()')
        body = ui[idx:idx + 800]
        assert 'attach-thumb' in body, 'renderTray image chip must use attach-thumb class'

    def test_rendertray_non_image_still_uses_paperclip(self):
        """Non-image files must still get the paperclip chip (not thumbnail)."""
        ui = _read_js('ui.js')
        idx = ui.find('function renderTray()')
        body = ui[idx:idx + 800]
        assert 'paperclip' in body, 'non-image files must still use paperclip chip in renderTray'

    def test_attach_thumb_css_present(self):
        """CSS must define .attach-thumb with width/height/object-fit for the thumbnail."""
        css = _read_css()
        assert '.attach-thumb' in css, '.attach-thumb CSS class must be defined'
        # Find the rule
        idx = css.find('.attach-thumb')
        rule = css[idx:idx + 200]
        assert 'object-fit' in rule, '.attach-thumb must set object-fit to crop image to square'

    def test_attach_chip_image_variant_css(self):
        """CSS must define .attach-chip--image for the image chip variant."""
        css = _read_css()
        assert '.attach-chip--image' in css, '.attach-chip--image CSS variant must be defined'

    def test_adfiles_function_still_present(self):
        """addFiles() must still exist after renderTray refactor."""
        ui = _read_js('ui.js')
        assert 'function addFiles(' in ui, 'addFiles() must not be removed from ui.js'


# ── Bug 2: Chat history image rendering ───────────────────────────────────

class TestChatHistoryImageRendering:
    """Uploaded images in chat history must render via a working HTTP endpoint, not a dead path."""

    def test_attachment_render_uses_file_raw_not_media(self):
        """Image attachments in chat history must use api/file/raw, not api/media.

        api/media expects a full absolute filesystem path (e.g. /home/hermes/.hermes/...).
        We only store the filename in m.attachments — feeding just a filename to api/media
        results in a broken image (path not in allowed roots → 404).

        api/file/raw resolves the filename relative to the session's workspace, which is
        exactly where the upload endpoint stores the file.
        """
        ui = _read_js('ui.js')
        m = re.search(r'm\.attachments&&m\.attachments\.length', ui)
        assert m, 'attachments rendering block not found in ui.js'
        body = ui[m.start():m.start() + 1200]
        assert 'api/file/raw' in body, (
            'Image attachments in chat history must use api/file/raw endpoint '
            '(resolves filename relative to session workspace). '
            'api/media requires a full absolute path which is not stored on the client.'
        )
        assert 'api/media?path=' not in body, (
            'api/media?path= must not be used for user-uploaded image attachments — '
            'it expects a full absolute path, but only filenames are stored in m.attachments.'
        )

    def test_attachment_render_includes_session_id(self):
        """api/file/raw URL must include session_id parameter for workspace resolution."""
        ui = _read_js('ui.js')
        m = re.search(r'm\.attachments&&m\.attachments\.length', ui)
        body = ui[m.start():m.start() + 1200]
        assert 'session_id' in body, (
            'api/file/raw URL in attachment rendering must include session_id '
            'so the server can resolve the filename against the correct workspace.'
        )

    def test_attachment_render_image_uses_msg_media_img(self):
        """Image attachments must still render with msg-media-img class for consistent styling."""
        ui = _read_js('ui.js')
        m = re.search(r'm\.attachments&&m\.attachments\.length', ui)
        body = ui[m.start():m.start() + 1200]
        assert 'msg-media-img' in body, 'Image attachment <img> must use msg-media-img class'

    def test_attachment_render_click_to_fullscreen(self):
        """Click-to-fullscreen must still work on chat history images."""
        ui = _read_js('ui.js')
        m = re.search(r'm\.attachments&&m\.attachments\.length', ui)
        body = ui[m.start():m.start() + 1200]
        assert 'msg-media-img--full' in body, 'Chat history images must toggle fullscreen on click'

    def test_attachment_render_non_image_keeps_paperclip(self):
        """Non-image attachments in chat history must still show paperclip badge."""
        ui = _read_js('ui.js')
        m = re.search(r'm\.attachments&&m\.attachments\.length', ui)
        body = ui[m.start():m.start() + 1200]
        assert 'msg-file-badge' in body, 'Non-image attachments must still use msg-file-badge in chat history'

    def test_attachment_render_extracts_filename(self):
        """Filename extraction (.split('/').pop()) must still be present for display."""
        ui = _read_js('ui.js')
        m = re.search(r'm\.attachments&&m\.attachments\.length', ui)
        body = ui[m.start():m.start() + 1200]
        assert ".split('/').pop()" in body, 'Must extract filename from path for display'
