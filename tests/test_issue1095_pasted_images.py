"""Tests for #1095 — pasted images render as inline previews, not paperclip badges."""
import os
import re
import pytest


def _read_js(name):
    with open(os.path.join('static', name)) as f:
        return f.read()


class TestAttachmentImageRendering:
    """User message attachments with image extensions should render as <img>, not paperclip badges."""

    def test_attachments_block_uses_image_check(self):
        ui = _read_js('ui.js')
        # Find the attachments rendering block
        assert 'm.attachments' in ui
        # Must check file extension before rendering
        assert '_IMAGE_EXTS.test(' in ui, '_IMAGE_EXTS not used in attachment rendering'

    def test_image_attachments_use_img_tag(self):
        """Image attachments should produce <img> with api/media?path=, not paperclip badge."""
        ui = _read_js('ui.js')
        # Find the attachments section
        m = re.search(r"m\.attachments&&m\.attachments\.length", ui)
        assert m, 'attachments rendering block not found'
        body = ui[m.start():m.start() + 1000]
        # Should have img tag with api/media
        assert 'msg-media-img' in body, 'attachments must render images with msg-media-img class'
        assert 'api/media?path=' in body, 'image attachments must use api/media endpoint'

    def test_non_image_attachments_keep_paperclip(self):
        """Non-image attachments must still show paperclip badge."""
        ui = _read_js('ui.js')
        m = re.search(r"m\.attachments&&m\.attachments\.length", ui)
        body = ui[m.start():m.start() + 1000]
        assert "msg-file-badge" in body, 'non-image attachments must still use paperclip badge'

    def test_image_click_to_full(self):
        """Inline image attachments should support click-to-fullscreen (toggle class)."""
        ui = _read_js('ui.js')
        m = re.search(r"m\.attachments&&m\.attachments\.length", ui)
        body = ui[m.start():m.start() + 1000]
        assert "msg-media-img--full" in body, 'image attachments should toggle full-screen on click'

    def test_uses_filename_not_full_path(self):
        """Non-image badge should display filename, not full path."""
        ui = _read_js('ui.js')
        m = re.search(r"m\.attachments&&m\.attachments\.length", ui)
        body = ui[m.start():m.start() + 1000]
        assert ".split('/').pop()" in body, 'should extract filename from path for display'
