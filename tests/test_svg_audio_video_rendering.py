"""Test: SVG, audio, video inline rendering (#481)"""

import re


def test_media_extension_regexes_exist():
    """Verify SVG/audio/video extension regexes are defined."""
    with open("static/ui.js") as f:
        src = f.read()
    assert "_SVG_EXTS" in src, "Missing _SVG_EXTS regex"
    assert "_AUDIO_EXTS" in src, "Missing _AUDIO_EXTS regex"
    assert "_VIDEO_EXTS" in src, "Missing _VIDEO_EXTS regex"
    # Verify they test correct extensions
    assert "svg" in src, "SVG regex should match .svg"
    assert "mp3" in src, "AUDIO regex should match .mp3"
    assert "ogg" in src, "AUDIO regex should match .ogg"
    assert "mp4" in src, "VIDEO regex should match .mp4"
    assert "webm" in src, "VIDEO regex should match .webm"


def test_svg_rendered_before_image_catch_all():
    """Verify SVG handler for URLs runs before the catch-all image handler."""
    with open("static/ui.js") as f:
        src = f.read()
    # Find positions of SVG vs image catch-all in the URL section
    svg_url_match = src.find("SVG URLs")
    # Comment can say either variant of the catch-all description
    image_catch_all = src.find("Render all https:// URLs as <img>")
    assert svg_url_match > 0, "SVG URL handler not found"
    assert image_catch_all > 0, "Image catch-all handler not found"
    assert svg_url_match < image_catch_all, (
        "SVG handler must come before image catch-all to avoid being shadowed"
    )


def test_local_svg_inline_rendering():
    """Verify local SVG files render as inline image."""
    with open("static/ui.js") as f:
        src = f.read()
    assert "msg-media-svg" in src, "Missing msg-media-svg CSS class for SVG rendering"
    # Should have at least 2 SVG handlers (URL + local)
    count = src.count("msg-media-svg")
    assert count >= 2, f"Expected >=2 msg-media-svg references, got {count}"


def test_local_audio_inline_rendering():
    """Verify local audio files render as inline player."""
    with open("static/ui.js") as f:
        src = f.read()
    assert "msg-media-audio" in src, "Missing msg-media-audio CSS class"
    assert "<audio controls" in src, "Should render <audio> element with controls"
    count = src.count("msg-media-audio")
    assert count >= 2, f"Expected >=2 msg-media-audio references, got {count}"


def test_local_video_inline_rendering():
    """Verify local video files render as inline player."""
    with open("static/ui.js") as f:
        src = f.read()
    assert "msg-media-video" in src, "Missing msg-media-video CSS class"
    assert "<video controls" in src, "Should render <video> element with controls"
    count = src.count("msg-media-video")
    assert count >= 2, f"Expected >=2 msg-media-video references, got {count}"


def test_url_svg_audio_video_handlers():
    """Verify HTTPS URLs for SVG/audio/video get inline rendering."""
    with open("static/ui.js") as f:
        src = f.read()
    # SVG URLs should be handled via _SVG_EXTS test on urlPath
    url_svg = "_SVG_EXTS.test(urlPath)" in src or (
        "_SVG_EXTS.test" in src and "urlPath" in src
    )
    # Audio/video via mediaKindForName or explicit _AUDIO/_VIDEO tests
    url_audio = (
        src.count("_AUDIO_EXTS.test(src.split")
        + src.count("_AUDIO_EXTS.test(urlPath")
        + src.count("mediaKindForName")
    )
    url_video = (
        src.count("_VIDEO_EXTS.test(src.split")
        + src.count("_VIDEO_EXTS.test(urlPath")
        + src.count("mediaKindForName")
    )
    assert url_svg, "URL SVG handler should test extension on src"
    assert url_audio >= 1, "URL audio handler should test extension on src"
    assert url_video >= 1, "URL video handler should test extension on src"


def test_attachment_svg_audio_video():
    """Verify file attachments for SVG/audio/video get inline previews."""
    with open("static/ui.js") as f:
        src = f.read()
    assert "attach-thumb--svg" in src, "Missing attach-thumb--svg for SVG thumbnails"
    assert "attach-chip--audio" in src, "Missing attach-chip--audio"
    assert "attach-chip--video" in src, "Missing attach-chip--video"
    assert "attach-chip-media" in src, "Missing attach-chip-media label"


def test_attachment_blob_url_cleanup():
    """Verify audio/video attachment chips create blob URLs."""
    with open("static/ui.js") as f:
        src = f.read()
    # SVG and media attachments should use createObjectURL
    assert "URL.createObjectURL(f)" in src, "Should create blob URLs for attachments"


def test_preload_metadata():
    """Verify audio/video elements use preload='metadata' for performance."""
    with open("static/ui.js") as f:
        src = f.read()
    assert 'preload="metadata"' in src, "Audio/video should use preload='metadata'"


def test_media_label_class():
    """Verify media label class exists for type identification."""
    with open("static/ui.js") as f:
        src = f.read()
    assert "msg-media-label" in src, "Missing msg-media-label class"


def test_i18n_keys():
    """Verify media rendering i18n keys exist in all locales."""
    with open("static/i18n.js") as f:
        src = f.read()
    required_keys = [
        "media_audio_label",
        "media_svg_label",
        "media_video_label",
    ]
    for key in required_keys:
        count = src.count(f"{key}:")
        assert count >= 8, (
            f"Key '{key}' found {count} times, expected >= 8 (one per locale)"
        )


def test_css_classes_exist():
    """Verify all media CSS classes are defined."""
    with open("static/style.css") as f:
        src = f.read()
    required_classes = [
        "msg-media-svg",
        "msg-media-label",
        "msg-media-audio",
        "msg-media-video",
        "attach-thumb--svg",
        "attach-chip--audio",
        "attach-chip--video",
        "attach-chip-media",
    ]
    for cls in required_classes:
        assert cls in src, f"Missing CSS class: .{cls}"


def test_svg_not_matched_by_image_exts():
    """Verify .svg is NOT in _IMAGE_EXTS (SVG has its own handler)."""
    with open("static/ui.js") as f:
        src = f.read()
    # Extract the _IMAGE_EXTS regex
    match = re.search(r"const _IMAGE_EXTS=/([^/]+)/i", src)
    assert match, "Could not find _IMAGE_EXTS regex"
    exts = match.group(1)
    assert "svg" not in exts.lower(), ".svg should NOT be in _IMAGE_EXTS"


def test_audio_video_not_matched_by_image_exts():
    """Verify audio/video extensions are NOT in _IMAGE_EXTS."""
    with open("static/ui.js") as f:
        src = f.read()
    match = re.search(r"const _IMAGE_EXTS=/([^/]+)/i", src)
    assert match
    exts = match.group(1)
    for ext in ["mp3", "mp4", "wav", "ogg", "webm", "mov", "m4a"]:
        assert ext not in exts.lower(), f".{ext} should NOT be in _IMAGE_EXTS"
