"""Regression tests for the public-share local-media embedding boundary (#6174).

These assert the hard security boundary of ``_embed_share_media`` /
``build_share_snapshot``: only image files that resolve INSIDE an allowed root
may be embedded as inline base64; every other reference (path traversal,
symlink escape, absolute-path-outside, ``file://``, non-image, SVG,
extension-spoofed, oversize, empty-allowed-roots) must degrade to a static
placeholder so no local file bytes leak into a public share.

Added as the committed regression net for PR #6319 (the reviewer's own
adversarial reproduction of the boundary).
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from api import shares


def _write_png(path: Path) -> None:
    # Minimal valid PNG magic header + filler.
    path.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 64)


@pytest.fixture()
def sandbox(tmp_path: Path):
    """A workspace (allowed root) plus a secret dir OUTSIDE it."""
    ws = tmp_path / "workspace"
    ws.mkdir()
    outside = tmp_path / "secret"
    outside.mkdir()
    (outside / "creds.txt").write_text("SECRET-API-KEY-DO-NOT-LEAK")
    _write_png(ws / "ok.png")
    # A .png that is actually a script (extension spoof).
    (ws / "evil.png").write_bytes(b"#!/bin/sh\necho pwned\n")
    # An SVG inside the workspace (text-bearing, must NOT embed).
    (ws / "pic.svg").write_bytes(
        b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>'
    )
    return {"ws": ws, "outside": outside}


def _embed(text: str, roots):
    return shares._embed_share_media(text, allowed_roots=tuple(roots))


def test_valid_workspace_image_is_embedded(sandbox):
    out = _embed("MEDIA:ok.png", [sandbox["ws"]])
    assert "base64," in out
    assert "data:image/png" in out
    assert shares._PLACEHOLDER not in out


def test_relative_path_traversal_is_blocked(sandbox):
    out = _embed("MEDIA:../secret/creds.txt", [sandbox["ws"]])
    assert out == shares._PLACEHOLDER
    assert "SECRET-API-KEY" not in out


def test_absolute_path_outside_allowed_roots_is_blocked(sandbox):
    creds = sandbox["outside"] / "creds.txt"
    out = _embed(f"MEDIA:{creds}", [sandbox["ws"]])
    assert out == shares._PLACEHOLDER
    assert "SECRET-API-KEY" not in out


def test_absolute_path_to_valid_image_inside_root_embeds(sandbox):
    ok = sandbox["ws"] / "ok.png"
    out = _embed(f"MEDIA:{ok}", [sandbox["ws"]])
    assert "base64," in out


def test_file_uri_is_always_rejected(sandbox):
    creds = sandbox["outside"] / "creds.txt"
    out = _embed(f"MEDIA:file://{creds}", [sandbox["ws"]])
    assert out == shares._PLACEHOLDER
    assert "SECRET-API-KEY" not in out


@pytest.mark.skipif(os.name == "nt", reason="symlink creation is restricted on Windows CI")
def test_symlink_escaping_allowed_root_is_blocked(sandbox):
    link = sandbox["ws"] / "link.png"
    os.symlink(sandbox["outside"] / "creds.txt", link)
    out = _embed("MEDIA:link.png", [sandbox["ws"]])
    assert out == shares._PLACEHOLDER
    assert "SECRET-API-KEY" not in out


def test_extension_spoofed_non_image_is_blocked(sandbox):
    # evil.png is a shell script; magic-byte validation must reject it.
    out = _embed("MEDIA:evil.png", [sandbox["ws"]])
    assert out == shares._PLACEHOLDER


def test_svg_is_never_embedded(sandbox):
    out = _embed("MEDIA:pic.svg", [sandbox["ws"]])
    assert out == shares._PLACEHOLDER


def test_oversize_image_is_blocked(sandbox):
    big = sandbox["ws"] / "big.png"
    big.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * (shares._SHARE_EMBED_MAX_BYTES + 1))
    out = _embed("MEDIA:big.png", [sandbox["ws"]])
    assert out == shares._PLACEHOLDER


def test_empty_allowed_roots_fails_closed(sandbox):
    ok = sandbox["ws"] / "ok.png"
    assert _embed("MEDIA:ok.png", []) == shares._PLACEHOLDER
    assert _embed(f"MEDIA:{ok}", []) == shares._PLACEHOLDER


def test_public_https_url_passes_through_unchanged(sandbox):
    text = "MEDIA:https://example.com/a.png"
    assert _embed(text, [sandbox["ws"]]) == text


def test_sanitize_message_embeds_inside_allowed_root(sandbox):
    msg = {"role": "assistant", "content": "see MEDIA:ok.png"}
    out = shares._sanitize_message(msg, allowed_roots=(sandbox["ws"],))
    assert out is not None
    assert "base64," in out["content"]


def test_sanitize_message_blocks_traversal_and_never_leaks(sandbox):
    msg = {"role": "assistant", "content": "see MEDIA:../secret/creds.txt"}
    out = shares._sanitize_message(msg, allowed_roots=(sandbox["ws"],))
    assert out is not None
    assert "SECRET-API-KEY" not in out["content"]
    assert shares._PLACEHOLDER in out["content"]
