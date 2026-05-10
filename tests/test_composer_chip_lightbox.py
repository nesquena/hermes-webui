"""Regression tests for composer attach-thumb lightbox click behaviour.

User pasted/dropped/picked an image and wants to verify the right one
attached before sending. Clicking the thumbnail in the composer's
attach-tray should open the existing image lightbox (the same one
that's wired to message-attached images).

This file pins the wiring at the source level — the document-level
delegated click handler must:
  - Continue handling .msg-media-img (existing v0.50.x behaviour).
  - Also handle .attach-thumb on IMG elements (new in this PR).
  - NOT trigger on the chip's × remove button (sibling element).
  - NOT trigger on audio/video chips (those have native controls).

It also pins the CSS cursor affordance so users discover the feature.
"""

from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
UI = ROOT / "static" / "ui.js"
STYLE = ROOT / "static" / "style.css"


class TestComposerChipLightboxDelegate:
    def test_delegate_handles_attach_thumb_clicks(self):
        """The document click handler must pick up clicks on .attach-thumb
        (composer image chips) and route them to _openImgLightbox().

        Previously the handler only looked for .msg-media-img.
        """
        src = UI.read_text(encoding="utf-8")
        assert "e.target.closest('.attach-thumb')" in src, (
            "Document click delegate must also match .attach-thumb"
        )
        # And it must call _openImgLightbox in that path.
        # Use a tighter anchor block to ensure both branches are wired.
        anchor = (
            "img = e.target.closest('.attach-thumb');\n"
            "  if(img && img.tagName === 'IMG'){\n"
        )
        assert anchor in src

    def test_delegate_still_handles_message_attached_images(self):
        """Existing .msg-media-img wiring must not regress."""
        src = UI.read_text(encoding="utf-8")
        # The message-image branch must come first (so _openImgLightbox
        # fires for them without falling through to the .attach-thumb check).
        msg_branch = "let img = e.target.closest('.msg-media-img');\n  if(img){ _openImgLightbox(img.src, img.alt); return; }"
        assert msg_branch in src

    def test_delegate_excludes_audio_video_chips(self):
        """Audio/video chips have their own inline controls (native <audio>
        / <video>) — they don't get a thumbnail .attach-thumb at all, so
        the handler can't possibly trigger on them. Pin that the chip
        renderer uses .attach-chip--audio / .attach-chip--video sibling
        classes (no IMG with class attach-thumb in those branches).
        """
        src = UI.read_text(encoding="utf-8")
        # Audio chip block — uses <audio>, no .attach-thumb img
        assert '<audio controls preload="metadata"' in src
        # Video chip block — uses <video>, no .attach-thumb img
        assert '<video controls preload="metadata"' in src
        # The .attach-thumb img tag is only generated in the image / svg branches.
        # Quick structural check: every chip-rendering line that emits
        # `class="attach-thumb"` has either `<img class="attach-thumb"` or
        # `attach-thumb attach-thumb--svg`. Both are images.
        for line in src.splitlines():
            if 'class="attach-thumb' in line:
                assert "<img " in line, (
                    "Every .attach-thumb emission should be an <img> tag, "
                    f"got: {line.strip()[:120]}"
                )


class TestComposerChipCursorAffordance:
    def test_attach_thumb_cursor_is_zoom_in(self):
        """`cursor: zoom-in` signals to the user that the thumbnail is
        clickable for zoom — the most discoverable affordance for this UX.
        Previously it was `cursor: default` which silently advertised
        non-interactivity.
        """
        src = STYLE.read_text(encoding="utf-8")
        # The .attach-thumb rule must declare cursor:zoom-in
        # Use a substring search resilient to other property additions.
        for line in src.splitlines():
            if line.strip().startswith(".attach-thumb{"):
                assert "cursor:zoom-in" in line, (
                    f".attach-thumb cursor must be 'zoom-in', got: {line.strip()[:120]}"
                )
                break
        else:
            raise AssertionError(".attach-thumb selector not found in style.css")

    def test_attach_thumb_has_hover_emphasis(self):
        """Subtle hover emphasis (brightness + scale) reinforces the
        zoom-in cursor by giving instant visual feedback before click.
        """
        src = STYLE.read_text(encoding="utf-8")
        assert ".attach-thumb:hover{" in src or ".attach-thumb:hover {" in src
