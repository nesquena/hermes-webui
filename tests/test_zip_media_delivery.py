"""Tests for zip archive delivery through /api/media.

Archives previously resolved to application/octet-stream (``.zip`` was absent
from ``MIME_MAP``) and were therefore rejected by the session MEDIA: token
allow-list, so a ``MEDIA:/path/to/file.zip`` link always failed to open.

Covers:
1. ``.zip`` maps to ``application/zip`` in MIME_MAP
2. ``application/zip`` is accepted by the session MEDIA: token allow-list
3. ``application/zip`` is NOT an inline-preview type (must download, never render)
"""
from __future__ import annotations

import pathlib
import re
import unittest

REPO_ROOT = pathlib.Path(__file__).parent.parent
ROUTES_PY = (REPO_ROOT / "api" / "routes.py").read_text(encoding="utf-8")


class TestZipMimeMapping(unittest.TestCase):
    """.zip must resolve to a concrete archive type, not octet-stream."""

    def test_zip_in_mime_map(self):
        from api.config import MIME_MAP

        self.assertEqual(
            MIME_MAP.get(".zip"),
            "application/zip",
            ".zip must map to application/zip so /api/media can serve archives",
        )

    def test_zip_not_octet_stream(self):
        from api.config import MIME_MAP

        self.assertNotEqual(
            MIME_MAP.get(".zip", "application/octet-stream"),
            "application/octet-stream",
            "octet-stream is rejected by the MEDIA: token allow-list",
        )


class TestZipTokenAllowList(unittest.TestCase):
    """The session MEDIA: token gate must accept archives."""

    def test_archive_types_declared(self):
        self.assertIn(
            '_ARCHIVE_TYPES = {"application/zip"}',
            ROUTES_PY,
            "_ARCHIVE_TYPES must declare the servable archive types",
        )

    def test_archive_types_in_session_token_types(self):
        match = re.search(
            r"_SESSION_MEDIA_TOKEN_TYPES\s*=\s*\(?\s*(.+?)\)?\n\s*session_media_allowed",
            ROUTES_PY,
            re.S,
        )
        self.assertIsNotNone(
            match, "could not locate the _SESSION_MEDIA_TOKEN_TYPES assignment"
        )
        self.assertIn(
            "_ARCHIVE_TYPES",
            match.group(1),
            "_SESSION_MEDIA_TOKEN_TYPES must include _ARCHIVE_TYPES",
        )


class TestZipIsDownloadOnly(unittest.TestCase):
    """Archives must never be offered for inline rendering."""

    def test_archive_not_in_inline_preview_types(self):
        match = re.search(r"_INLINE_PREVIEW_TYPES\s*=\s*(.+)", ROUTES_PY)
        self.assertIsNotNone(match, "could not locate _INLINE_PREVIEW_TYPES")
        self.assertNotIn(
            "_ARCHIVE_TYPES",
            match.group(1),
            "archives must not be inline-previewable; they must download",
        )

    def test_archive_not_in_inline_image_types(self):
        match = re.search(r"_INLINE_IMAGE_TYPES\s*=\s*\{(.+?)\}", ROUTES_PY, re.S)
        self.assertIsNotNone(match, "could not locate _INLINE_IMAGE_TYPES")
        self.assertNotIn(
            "zip",
            match.group(1),
            "zip must not be treated as an inline image type",
        )


if __name__ == "__main__":
    unittest.main()
