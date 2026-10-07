from __future__ import annotations

from pathlib import Path
import base64
import struct
import zlib
import shutil
import subprocess
from urllib.parse import quote_from_bytes

import pytest

from api import shares
from api.models import Session

from tests.test_data_uri_images import _DRIVER_SRC

REPO_ROOT = Path(__file__).resolve().parents[1]
NODE = shutil.which("node")


@pytest.fixture()
def workspace(tmp_path: Path) -> Path:
    root = tmp_path / "workspace"
    root.mkdir()
    (root / "safe.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 64)
    return root


def _sanitize(text: str, *, workspace: Path | None = None) -> str:
    roots = (workspace,) if workspace is not None else ()
    out = shares._sanitize_message(
        {"role": "assistant", "content": text},
        allowed_roots=roots,
    )
    assert out is not None
    return out["content"]


@pytest.mark.parametrize(
    "text",
    [
        "file:///tmp/private.png",
        "[open](file:///tmp/private.png)",
        "`file:///tmp/private.png`",
        "MEDIA:https://webui.example/api/media?path=/tmp/private.png",
        "MEDIA:https://webui.example/API/MEDIA?PATH=/tmp/private.png",
        "MEDIA:https://cdn.example/render?next=https%3A%2F%2Fwebui.example%2Fapi%2Fmedia%3Fpath%3D%2Ftmp%2Fprivate.png",
        "MEDIA:https://cdn.example/render?next=https%253A%252F%252Fwebui.example%252Fapi%252Fmedia%253Fpath%253D%252Ftmp%252Fprivate.png",
        "MEDIA:https://cdn.example/render?next=file%3A%2F%2F%2Ftmp%2Fprivate.png",
        "`MEDIA:https://webui.example/api/media?path=/tmp/private.png`",
        "![a](https:///webui.example/api/media?path=/tmp/private.png)",
        "![a](https:////webui.example/api/media?path=/tmp/private.png)",
        "MEDIA:https:///webui.example/api/media?path=/tmp/private.png",
        "MEDIA:https://\\webui.example/api/media?path=/tmp/private.png",
    ],
)
def test_public_share_snapshot_omits_private_renderer_media_references(text):
    content = _sanitize(text)

    expected = shares._PLACEHOLDER
    if text.startswith("[open]"):
        expected = f"[open]({shares._PLACEHOLDER})"
    elif text.startswith("`file:"):
        expected = f"`{shares._PLACEHOLDER}`"
    elif text.startswith("![a]"):
        expected = f"![a]({shares._PLACEHOLDER})"
    assert content == expected
    assert "file://" not in content.lower()
    assert "/api/media" not in content.lower()


def test_public_https_media_without_private_endpoint_is_preserved():
    text = "MEDIA:https://cdn.example/images/public.png?size=large"

    assert _sanitize(text) == text


def test_lowercase_wrapped_media_token_stays_inert_code():
    text = "`media:https://webui.example/api/media?path=/tmp/private.png`"

    assert _sanitize(text) == text


def test_public_link_before_file_link_on_same_line_is_preserved():
    text = "see [public](https://cdn.example/a.png) and [x](file:///etc/passwd)"

    content = _sanitize(text)

    assert content == f"see [public](https://cdn.example/a.png) and [x]({shares._PLACEHOLDER})"


def test_external_api_media_like_path_without_path_parameter_is_preserved():
    text = "MEDIA:https://cdn.example/api/media/public-image.png"

    assert _sanitize(text) == text


def test_deep_dot_segments_cannot_evade_private_media_route():
    text = (
        "MEDIA:https://webui.example/api/1/2/3/4/5/6/7/8/9/"
        "../../../../../../../../../media?path=/tmp/private.png"
    )

    assert _sanitize(text) == shares._PLACEHOLDER


def test_direct_markdown_image_to_private_media_is_omitted():
    text = "![private](https://webui.example/api/media?path=/tmp/private.png)"

    assert _sanitize(text) == f"![private]({shares._PLACEHOLDER})"


@pytest.mark.parametrize("mime", ["png", "jpeg", "gif", "webp", "avif", "svg+xml"])
def test_large_self_contained_base64_image_survives_snapshot(mime):
    ref = f"data:image/{mime};base64," + base64.b64encode(b"image" * 4000).decode()
    text = f"![chart]({ref})"
    assert len(ref) > shares._SHARE_MEDIA_SAFETY_MAX_CHARS
    assert _sanitize(text) == text


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
@pytest.mark.parametrize("encoding", ["base64", "percent", "percent-private-text", "escaped-base64"])
def test_large_valid_png_survives_snapshot_and_production_renderer(tmp_path, encoding):
    # A complete PNG with deterministic, poorly compressible RGB pixels.
    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))

    width, height = (120, 100) if encoding == "escaped-base64" else (128, 128)
    stride = width * 3
    pixels = bytes((i * 73 + i // 256) % 256 for i in range(width * height * 3))
    scanlines = b"".join(b"\0" + pixels[i:i + stride] for i in range(0, len(pixels), stride))
    png = b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
    if encoding == "percent-private-text":
        # Text inside image bytes is inert data, not a renderer-active URL.
        png += chunk(b"tEXt", b"Comment\0https://webui.example/api/media?path=/tmp/private.png file:///tmp/private.png")
    png += chunk(b"IDAT", zlib.compress(scanlines, level=0)) + chunk(b"IEND", b"")
    if encoding in ("base64", "escaped-base64"):
        payload = base64.b64encode(png).decode()
        if encoding == "escaped-base64":
            assert "/" in payload and "+" in payload
            payload = payload.replace("/", "%2F").replace("+", "%2B")
        ref = "data:image/png;base64," + payload
    else:
        ref = "data:image/png," + quote_from_bytes(png, safe="")
    text = f"![chart]({ref})"
    assert len(ref) > shares._SHARE_MEDIA_SAFETY_MAX_CHARS
    session = Session(session_id="share-large-png", messages=[{"role": "assistant", "content": text}])
    driver = tmp_path / "large-png-render.js"
    driver.write_text(_DRIVER_SRC, encoding="utf-8")
    # Establish renderer support before exercising the public snapshot boundary.
    original_rendered = subprocess.run(
        [NODE, str(driver), str(REPO_ROOT / "static" / "ui.js")],
        input=text, capture_output=True, text=True, timeout=30, check=True,
    ).stdout
    assert f'src="{ref}"' in original_rendered
    content = shares.build_share_snapshot(session)["messages"][0]["content"]
    assert content == text
    rendered = subprocess.run(
        [NODE, str(driver), str(REPO_ROOT / "static" / "ui.js")],
        input=content, capture_output=True, text=True, timeout=30, check=True,
    ).stdout
    assert f'src="{ref}"' in rendered



@pytest.mark.skipif(NODE is None, reason="node not on PATH")
@pytest.mark.parametrize("mime", ["png", "jpg", "jpeg", "gif", "webp", "avif", "PNG"])
def test_large_percent_raster_forms_survive_snapshot_and_renderer(tmp_path, mime):
    ref = f"data:image/{mime}," + "%89" * 6000
    text = f"![image]({ref})"
    assert len(ref) > shares._SHARE_MEDIA_SAFETY_MAX_CHARS
    assert _sanitize(text) == text
    driver = tmp_path / "percent-raster-render.js"
    driver.write_text(_DRIVER_SRC, encoding="utf-8")
    rendered = subprocess.run(
        [NODE, str(driver), str(REPO_ROOT / "static" / "ui.js")],
        input=text, capture_output=True, text=True, timeout=30, check=True,
    ).stdout
    assert f'src="{ref}"' in rendered


@pytest.mark.parametrize("encoding", ["base64", "percent"])
@pytest.mark.parametrize("offset", [-1, 0, 1], ids=["below-limit", "at-limit", "over-limit"])
def test_self_contained_image_uri_size_boundary(encoding, offset):
    prefix = "data:image/png;base64," if encoding == "base64" else "data:image/png,"
    size = shares._SHARE_DATA_IMAGE_MAX_CHARS + offset
    payload = "A" * (size - len(prefix))
    if encoding == "percent":
        payload = "%89" + payload[3:]
    text = f"![image]({prefix}{payload})"
    assert _sanitize(text) == (text if offset <= 0 else f"![image]({shares._PLACEHOLDER})")


@pytest.mark.parametrize("ref", [
    "data:image/svg+xml," + "%3Csvg%3E" * 3000,
    "data:image/png;charset=utf-8," + "%89" * 6000,
    "data:image/bmp," + "%89" * 6000,
    "data:text/html," + "%3Cscript%3E" * 2000,
    "data:image/png," + "%89" * 6000 + "?next=https://webui.example/api/media?path=private.png",
    "data:image/png," + "%89" * 6000 + "#fragment",
    "data:image/png," + "%89" * 6000 + "\\private.png",
    "data:image/png," + "%89" * 6000 + '<script>',
], ids=["percent-svg", "charset-parameter", "unsupported-raster", "html-scheme",
        "private-url-suffix", "fragment", "backslash", "html-payload"])
def test_large_non_renderer_percent_image_fails_closed(ref):
    assert _sanitize(f"![unsafe]({ref})") == f"![unsafe]({shares._PLACEHOLDER})"


def test_percent_image_does_not_exempt_neighboring_private_references():
    image = "![image](data:image/png," + "%89" * 6000 + ")"
    text = (
        f"before {image} "
        "![private](https://webui.example/api/media?path=/tmp/private.png) "
        "file:///tmp/private.png after"
    )
    assert _sanitize(text) == f"before {image} ![private]({shares._PLACEHOLDER}) {shares._PLACEHOLDER} after"


@pytest.mark.parametrize("ref", [
    "data:image/png;base64," + "A" * (2 * 1024 * 1024),
    "data:image/png;base64," + "A" * 17000 + "%2Fapi%2Fmedia%3Fpath%3Dprivate.png",
    "data:image/png;base64," + "A" * 17000 + "file:///tmp/private.png",
    "data:text/html;base64," + "A" * 17000,
], ids=["oversized", "encoded-private-path", "literal-file-path", "html-scheme"])
def test_large_non_renderer_base64_image_does_not_bypass_private_boundary(ref):
    assert _sanitize(f"![unsafe]({ref})") == f"![unsafe]({shares._PLACEHOLDER})"


def test_public_media_path_with_fragment_path_text_is_preserved():
    text = (
        "MEDIA:https://cdn.example/albums/api/media/photos/2024.jpg"
        "#path=screenshot.png"
    )

    assert _sanitize(text) == text


def test_public_markdown_image_with_api_media_path_segment_is_preserved():
    text = (
        "![public](https://cdn.example/albums/api/media/photos/2024.jpg"
        "#path=screenshot.png)"
    )

    assert _sanitize(text) == text


def test_existing_safe_local_image_embedding_remains_self_contained(workspace):
    content = _sanitize("MEDIA:safe.png", workspace=workspace)

    assert content.startswith('<img src="data:image/png;base64,')
    assert "api/media" not in content
    assert shares._PLACEHOLDER not in content

@pytest.mark.parametrize(
    "text",
    [
        "MEDIA:https://webui.example/api//media?path=/tmp/private.png",
        "MEDIA:https://webui.example/api/./media?path=/tmp/private.png",
        "MEDIA:https://webui.example/api/private/../media?path=/tmp/private.png",
        "MEDIA:https://webui.example/api/%70rivate/%2e%2e/media?%70ath=/tmp/private.png",
        "MEDIA:https://webui.example/api/media?path&#61;/tmp/private.png",
        "MEDIA:https://cdn.example/render?next=file%253A%252F%252F%252Ftmp%252Fprivate.png",
    ],
)
def test_public_share_private_media_normalization_fails_closed(text):
    assert _sanitize(text) == shares._PLACEHOLDER


def test_public_share_private_media_decode_depth_fails_closed():
    nested = "file:///tmp/private.png"
    for _ in range(shares._SHARE_MEDIA_SAFETY_DECODE_ROUNDS + 1):
        nested = nested.replace("%", "%25").replace(":", "%3A").replace("/", "%2F")
    text = f"MEDIA:https://cdn.example/render?next={nested}"

    assert _sanitize(text) == shares._PLACEHOLDER


def test_public_share_oversized_media_token_fails_closed():
    text = "MEDIA:https://cdn.example/" + ("a" * (shares._SHARE_MEDIA_SAFETY_MAX_CHARS + 1))

    assert _sanitize(text) == shares._PLACEHOLDER


def test_private_media_replacement_preserves_surrounding_public_text():
    content = _sanitize(
        "before MEDIA:https://webui.example/api/media?path=/tmp/private.png after"
    )

    assert content == f"before {shares._PLACEHOLDER} after"


def test_public_share_title_preserves_ordinary_public_media(tmp_path):
    public = "MEDIA:https://cdn.example/images/title.png"
    session = Session(
        session_id="share-public-media-title",
        title=public,
        messages=[{"role": "user", "content": "hello"}],
        workspace=str(tmp_path),
    )

    assert shares.build_share_snapshot(session)["title"] == public




@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_public_snapshot_stays_private_through_production_renderer(tmp_path):
    session = Session(
        session_id="share-renderer-private-media",
        title="Renderer closure",
        messages=[
            {
                "role": "assistant",
                "content": (
                    "before "
                    "MEDIA:https://webui.example/api/media?path=/tmp/private.png "
                    "and file:///tmp/private.pdf after"
                ),
            }
        ],
        workspace=str(tmp_path),
    )
    snapshot = shares.build_share_snapshot(session)
    content = snapshot["messages"][0]["content"]
    driver = tmp_path / "share-render-driver.js"
    driver.write_text(_DRIVER_SRC, encoding="utf-8")

    result = subprocess.run(
        [NODE, str(driver), str(REPO_ROOT / "static" / "ui.js")],
        input=content,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    rendered = result.stdout.lower()
    assert "api/media?path=" not in rendered
    assert "file://" not in rendered
    assert shares._PLACEHOLDER.lower().strip("[]*") in rendered


def test_public_share_title_uses_same_private_media_boundary(tmp_path):
    session = Session(
        session_id="share-private-media-title",
        title="MEDIA:https://webui.example/api/media?path=/tmp/title.png",
        messages=[{"role": "user", "content": "hello"}],
        workspace=str(tmp_path),
    )

    snapshot = shares.build_share_snapshot(session)

    assert snapshot["title"] == shares._PLACEHOLDER
    assert "/api/media" not in snapshot["title"].lower()


def test_public_share_title_omits_file_uri(tmp_path):
    session = Session(
        session_id="share-private-file-title",
        title="file:///tmp/private-title.txt",
        messages=[{"role": "user", "content": "hello"}],
        workspace=str(tmp_path),
    )

    snapshot = shares.build_share_snapshot(session)

    assert snapshot["title"] == shares._PLACEHOLDER
    assert "file://" not in snapshot["title"].lower()


@pytest.mark.parametrize("wrapped", [False, True], ids=["bare", "wrapped"])
@pytest.mark.parametrize("ref,private", [
    ("https://cdn.example/icon.png", False),
    ("https://cdn.example/albums/api/media/photos.png#path=public.png", False),
    ("/home/me/secret.png", True),
    ("relative-secret.png", True),
    ("file:///home/me/secret.png", True),
    ("https://webui.example/api/media?path=/home/me/secret.png", True),
    ("https://cdn.example/render?next=https%253A%252F%252Fwebui.example%252Fapi%252Fmedia%253Fpath%253Dsecret.png", True),
], ids=["public", "public-path-lookalike", "local-absolute", "local-relative",
        "file-uri", "private-endpoint", "encoded-private-endpoint"])
def test_public_share_title_wrapped_media_matrix(tmp_path, wrapped, ref, private):
    token = f"MEDIA:{ref}"
    if wrapped:
        token = f"`{token}`"
    text = f"See {token} here"
    session = Session(
        session_id="share-title-wrapped-media",
        title=text,
        messages=[{"role": "assistant", "content": text}],
        workspace=str(tmp_path),
    )
    snapshot = shares.build_share_snapshot(session)
    expected_title = f"See {shares._PLACEHOLDER} here" if private else text
    assert snapshot["title"] == expected_title
    # Bodies retain the production renderer's activation of wrapped MEDIA.
    if not private:
        assert snapshot["messages"][0]["content"] == f"See MEDIA:{ref} here"
    else:
        assert ref not in snapshot["messages"][0]["content"]


def test_public_share_title_wrapped_media_keeps_neighbors_and_lowercase(tmp_path):
    public = "`MEDIA:https://cdn.example/icon.png`"
    private = "`MEDIA:/home/me/secret.png`"
    lowercase = "`media:https://cdn.example/inert.png`"
    text = f"Reference {public} and {private} then {lowercase}"
    session = Session(
        session_id="share-title-wrapped-neighbors",
        title=text,
        messages=[{"role": "user", "content": "hello"}],
        workspace=str(tmp_path),
    )
    snapshot = shares.build_share_snapshot(session)
    assert snapshot["title"] == f"Reference {public} and {shares._PLACEHOLDER} then {lowercase}"


def test_public_share_title_preserves_exact_review_public_wrapper(tmp_path):
    text = "Reference `MEDIA:https://cdn.example/icon.png`"
    session = Session(
        session_id="share-title-exact-review",
        title=text,
        messages=[{"role": "user", "content": "hello"}],
        workspace=str(tmp_path),
    )
    assert shares.build_share_snapshot(session)["title"] == text


@pytest.mark.parametrize("wrapped", [False, True], ids=["bare", "wrapped"])
@pytest.mark.parametrize("escaped", [False, True], ids=["literal-base64", "escaped-base64"])
def test_public_share_title_keeps_review_gif(tmp_path, wrapped, escaped):
    # Complete 1x1 GIF89a, matching the reviewer-pinned title shape.
    gif_uri = "data:image/gif;base64,R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7"
    gif = base64.b64decode(gif_uri.split(",", 1)[1], validate=True)
    assert gif.startswith(b"GIF89a\x01\x00\x01\x00") and gif.endswith(b";")
    if escaped:
        prefix, payload = gif_uri.split(",", 1)
        gif_uri = prefix + "," + payload.replace("/", "%2F").replace("+", "%2B")
    token = f"MEDIA:{gif_uri}"
    if wrapped:
        token = f"`{token}`"
    title = f"Logo {token}"
    session = Session(
        session_id="share-title-review-gif",
        title=title,
        messages=[{"role": "user", "content": "hello"}],
        workspace=str(tmp_path),
    )
    assert shares.build_share_snapshot(session)["title"] == title
    assert session.title == title


@pytest.mark.parametrize("wrapped", [False, True], ids=["bare", "wrapped"])
@pytest.mark.parametrize("mime,encoding", [
    *((mime, "base64") for mime in ["png", "jpg", "jpeg", "gif", "webp", "avif", "svg+xml"]),
    *((mime, "percent") for mime in ["png", "jpg", "jpeg", "gif", "webp", "avif"]),
])
def test_public_share_title_keeps_supported_data_image_forms(tmp_path, wrapped, mime, encoding):
    # This matrix checks URI policy; the review GIF above checks a real image.
    suffix = ";base64," + base64.b64encode(b"image" * 4000).decode()
    if encoding == "percent":
        suffix = "," + "%89" * 6000
    ref = f"data:image/{mime}{suffix}"
    assert len(ref) > shares._SHARE_MEDIA_SAFETY_MAX_CHARS
    token = f"MEDIA:{ref}"
    if wrapped:
        token = f"`{token}`"
    title = f"Logo {token} here"
    session = Session(
        session_id="share-title-data-image-forms",
        title=title,
        messages=[{"role": "user", "content": "hello"}],
        workspace=str(tmp_path),
    )
    assert shares.build_share_snapshot(session)["title"] == title


@pytest.mark.parametrize("wrapped", [False, True], ids=["bare", "wrapped"])
@pytest.mark.parametrize("encoding", ["base64", "percent"])
@pytest.mark.parametrize("offset", [-1, 0, 1], ids=["below-limit", "at-limit", "over-limit"])
def test_public_share_title_data_image_size_boundary(tmp_path, wrapped, encoding, offset):
    prefix = "data:image/png;base64," if encoding == "base64" else "data:image/png,"
    payload = "A" * (shares._SHARE_DATA_IMAGE_MAX_CHARS + offset - len(prefix))
    if encoding == "percent":
        payload = "%89" + payload[3:]
    token = f"MEDIA:{prefix}{payload}"
    if wrapped:
        token = f"`{token}`"
    title = f"Logo {token} here"
    session = Session(
        session_id="share-title-image-size-boundary",
        title=title,
        messages=[{"role": "user", "content": "hello"}],
        workspace=str(tmp_path),
    )
    expected = title if offset <= 0 else f"Logo {shares._PLACEHOLDER} here"
    assert shares.build_share_snapshot(session)["title"] == expected


@pytest.mark.parametrize("wrapped", [False, True], ids=["bare", "wrapped"])
@pytest.mark.parametrize("ref", [
    "data:image/svg+xml,%3Csvg%3E",
    "data:image/png;charset=utf-8,%89PNG",
    "data:image/bmp;base64,AAAA",
    "data:text/html;base64,AAAA",
    "data:image/png;base64,AAAA%2Fapi%2Fmedia%3Fpath%3Dprivate.png",
    "data:image/png;base64,AAAA" + "file:///tmp/private.png",
    "data:image/png;base64," + "A" * 17000 + "%2Fapi%2Fmedia%3Fpath%3Dprivate.png",
    "data:image/png," + "%89" * 6000 + "?next=https://webui.example/api/media?path=private.png",
], ids=["percent-svg", "charset-parameter", "unsupported-raster", "html-scheme",
        "encoded-private-base64-suffix", "literal-file-base64-suffix",
        "large-malformed-base64", "private-url-percent-suffix"])
def test_public_share_title_rejects_unsupported_data_image_forms(tmp_path, wrapped, ref):
    token = f"MEDIA:{ref}"
    if wrapped:
        token = f"`{token}`"
    session = Session(
        session_id="share-title-data-image-negative",
        title=f"Logo {token} here",
        messages=[{"role": "user", "content": "hello"}],
        workspace=str(tmp_path),
    )
    assert shares.build_share_snapshot(session)["title"] == f"Logo {shares._PLACEHOLDER} here"


def test_public_share_title_image_exemption_does_not_exempt_private_neighbors(tmp_path):
    ref = "data:image/gif;base64,R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7"
    image = f"`MEDIA:{ref}`"
    title = (
        f"Logo {image} and `MEDIA:/home/me/secret.png` then "
        "MEDIA:https://cdn.example/render?next=https%253A%252F%252Fwebui.example%252Fapi%252Fmedia%253Fpath%253Dsecret.png"
    )
    session = Session(
        session_id="share-title-data-image-neighbors",
        title=title,
        messages=[{"role": "user", "content": "hello"}],
        workspace=str(tmp_path),
    )
    expected = f"Logo {image} and {shares._PLACEHOLDER} then {shares._PLACEHOLDER}"
    assert shares.build_share_snapshot(session)["title"] == expected



def _snapshot_escaped_image(tmp_path, ref, location):
    # Long body MEDIA data URI filename handling (#7949) is a separate issue.
    body = f"![image]({ref})"
    title = f"MEDIA:{ref}"
    if location == "wrapped-title":
        title = f"`{title}`"
    session = Session(
        session_id="share-escaped-image",
        title=f"before {title} after" if location != "body" else "Image",
        messages=[{"role": "assistant", "content": f"before {body} after" if location == "body" else "hello"}],
        workspace=str(tmp_path),
    )
    snapshot = shares.build_share_snapshot(session)
    return (snapshot["messages"][0]["content"], session.messages[0]["content"]) if location == "body" else (snapshot["title"], session.title)


@pytest.mark.parametrize("location", ["body", "bare-title", "wrapped-title"])
@pytest.mark.parametrize("mime", ["png", "jpg", "jpeg", "gif", "webp", "avif", "PNG"])
def test_escaped_base64_raster_mimes_keep_original_snapshot_text(tmp_path, location, mime):
    # Both literal and escaped + survive; slash escape is case-insensitive.
    payload = base64.b64encode(b"\xfb\xef\xff" * 5000).decode()
    ref = f"data:image/{mime};base64," + payload.replace("/", "%2f").replace("++", "+%2B")
    assert len(ref) > shares._SHARE_MEDIA_SAFETY_MAX_CHARS
    actual, original = _snapshot_escaped_image(tmp_path, ref, location)
    assert actual == original


@pytest.mark.parametrize("location", ["body", "bare-title", "wrapped-title"])
@pytest.mark.parametrize("payload", [
    "+%2B//", "%2B+//", "%2b%2B%2f%2F", "aQ%3D%3d", "%61%51==",
], ids=["literal-plus-first", "literal-plus-second", "escape-case", "padding", "alphabet"])
def test_once_escaped_base64_formats_survive_snapshot(tmp_path, location, payload):
    ref = "DATA:IMAGE/GIF;BASE64," + payload
    actual, original = _snapshot_escaped_image(tmp_path, ref, location)
    assert actual == original


@pytest.mark.parametrize("location", ["body", "bare-title", "wrapped-title"])
@pytest.mark.parametrize("payload", [
    "AAAA%", "AAAA%2", "AAAA%ZZ", "AAAA%252F",
    "AAAA%00", "AAAA%FF", "AAAA%3D", "AAAA%3D===", "AA%3DA", "AAAA%2Fapi%2Fmedia%3Fpath%3Dprivate.png",
    "AAAA%2Ffile%3A%2F%2F%2Ftmp%2Fprivate.png",
    "AAAA%2F?junk", "AAAA%2F#junk", "AAAA%2F\\junk", "AAAA%2F<script>",
    "AAAA%2F?next=https://webui.example/api/media?path=private.png",
], ids=["bare-percent", "short-escape", "nonhex-escape", "double-escape",
        "nul", "nonascii", "excess-padding", "four-padding", "middle-padding", "private-route", "file-uri",
        "raw-query", "raw-fragment", "raw-backslash", "raw-markup", "raw-private-url"])
def test_malformed_escaped_base64_fails_closed_in_snapshot(tmp_path, location, payload):
    ref = "data:image/png;base64," + payload
    actual, _ = _snapshot_escaped_image(tmp_path, ref, location)
    expected = f"![image]({shares._PLACEHOLDER})" if location == "body" else shares._PLACEHOLDER
    assert actual == f"before {expected} after"


@pytest.mark.parametrize("location", ["body", "bare-title", "wrapped-title"])
@pytest.mark.parametrize("offset", [-1, 0, 1], ids=["below-limit", "at-limit", "over-limit"])
def test_escaped_base64_original_uri_size_boundary(tmp_path, location, offset):
    # Valid base64 has even escaped length; MIME aliases permit each exact size.
    prefix = "data:image/png;base64," if offset == 0 else "data:image/jpeg;base64,"
    size = shares._SHARE_DATA_IMAGE_MAX_CHARS + offset
    length = size - len(prefix)
    escapes = 1 if length % 4 == 2 else 2
    payload = "%41" * escapes + "A" * (length - 3 * escapes)
    ref = prefix + payload
    assert len(ref) == size
    actual, original = _snapshot_escaped_image(tmp_path, ref, location)
    expected = f"![image]({shares._PLACEHOLDER})" if location == "body" else shares._PLACEHOLDER
    assert actual == (original if offset <= 0 else f"before {expected} after")


@pytest.mark.parametrize("wrapped", [False, True], ids=["bare", "wrapped"])
def test_escaped_base64_svg_title_stays_outside_raster_exemption(tmp_path, wrapped):
    ref = "data:image/svg+xml;base64," + "%41" * 6000
    location = "wrapped-title" if wrapped else "bare-title"
    actual, _ = _snapshot_escaped_image(tmp_path, ref, location)
    assert actual == f"before {shares._PLACEHOLDER} after"


@pytest.mark.parametrize("location", ["body", "bare-title", "wrapped-title"])
def test_escaped_base64_does_not_exempt_private_snapshot_neighbors(tmp_path, location):
    ref = "data:image/gif;base64,+%2B%2F/"
    image = f"![image]({ref})" if location == "body" else f"MEDIA:{ref}"
    if location == "wrapped-title":
        image = f"`{image}`"
    private = "MEDIA:https://cdn.example/render?next=https%253A%252F%252Fwebui.example%252Fapi%252Fmedia%253Fpath%253Dprivate.png"
    file_ref = "file:///tmp/private.png"
    text = f"before {image} and {private} then {file_ref} after"
    session = Session(
        session_id="share-escaped-image-neighbors",
        title=text if location != "body" else "Image",
        messages=[{"role": "assistant", "content": text if location == "body" else "hello"}],
        workspace=str(tmp_path),
    )
    snapshot = shares.build_share_snapshot(session)
    actual = snapshot["messages"][0]["content"] if location == "body" else snapshot["title"]
    assert actual == f"before {image} and {shares._PLACEHOLDER} then {shares._PLACEHOLDER} after"


@pytest.mark.parametrize("location", ["body", "bare-title", "wrapped-title"])
@pytest.mark.parametrize("payload", ["AAAA%20", "AAAA%0A", "A%2F", "AA%09%0C%0D%20"])
def test_forgiving_escaped_base64_is_not_malformed(tmp_path, location, payload):
    actual, original = _snapshot_escaped_image(tmp_path, "data:image/png;base64," + payload, location)
    assert actual == original


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
@pytest.mark.parametrize("location", ["body", "bare-title", "wrapped-title"])
@pytest.mark.parametrize("form", ["unpadded", "plus-slash-unpadded", "lf", "crlf", "mime-wrapped"])
def test_real_png_forgiving_base64_snapshot_and_renderer(tmp_path, location, form):
    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))

    png = (b"\x89PNG\r\n\x1a\n"
           + chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0))
           + chunk(b"tEXt", b"k\0vv")
           + chunk(b"IDAT", zlib.compress(b"\0\xfb\xef\xff", level=0))
           + chunk(b"IEND", b""))
    payload = base64.b64encode(png).decode()
    assert payload.endswith("=") and ("+" in payload or "/" in payload)
    if form == "unpadded":
        escaped = quote_from_bytes(payload.rstrip("=").encode(), safe="")
    elif form == "plus-slash-unpadded":
        escaped = payload.rstrip("=").replace("+", "%2B").replace("/", "%2F")
    else:
        separator = "\r\n" if form != "lf" else "\n"
        width = 76 if form == "mime-wrapped" else 12
        escaped = quote_from_bytes(separator.join(payload[i:i + width] for i in range(0, len(payload), width)).encode(), safe="")
    ref = "data:image/png;base64," + escaped
    # Browser-equivalent data decoding is checked independently of our validator.
    decoded = subprocess.run([NODE, "-e", "fetch(process.argv[1]).then(r=>r.arrayBuffer()).then(b=>process.stdout.write(Buffer.from(b)))", ref], capture_output=True, timeout=30, check=True).stdout
    assert decoded == png
    driver = tmp_path / "forgiving-png-render.js"
    driver.write_text(_DRIVER_SRC, encoding="utf-8")
    body = f"![image]({ref})"
    before = subprocess.run([NODE, str(driver), str(REPO_ROOT / "static" / "ui.js")], input=body, capture_output=True, text=True, timeout=30, check=True).stdout
    assert f'src="{ref}"' in before
    actual, original = _snapshot_escaped_image(tmp_path, ref, location)
    assert actual == original
    if location == "body":
        after = subprocess.run([NODE, str(driver), str(REPO_ROOT / "static" / "ui.js")], input=actual, capture_output=True, text=True, timeout=30, check=True).stdout
        assert f'src="{ref}"' in after


@pytest.mark.parametrize("quote", ['"', "'", "&quot;", "&#39;"])
@pytest.mark.parametrize("ref,private", [
    ("data:image/gif;base64,R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7", False),
    ("https://cdn.example/logo.png", False),
    ("/tmp/private.png", True),
    ("https://webui.example/api/media?path=/tmp/private.png", True),
])
def test_quoted_media_snapshot_keeps_public_ref_and_private_closer(tmp_path, quote, ref, private):
    text = f"Logo {quote}MEDIA:{ref}{quote} end"
    session = Session(session_id="quoted-share-media", title=text,
                      messages=[{"role": "assistant", "content": text}], workspace=str(tmp_path))
    snapshot = shares.build_share_snapshot(session)
    if private:
        # Shared suffix splitting normalizes entity closers to their quote byte.
        closer = '"' if quote == "&quot;" else "'" if quote == "&#39;" else quote
        expected = f"Logo {quote}{shares._PLACEHOLDER}{closer} end"
    else:
        expected = text
    assert snapshot["title"] == expected
    # Body MEDIA data-URI embedding is the separate existing #7949 path.
    # Real data-image body preservation is asserted above through Markdown.
    if not ref.startswith("data:"):
        assert snapshot["messages"][0]["content"] == expected
    assert session.title == text and session.messages[0]["content"] == text


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
@pytest.mark.parametrize("location", ["body", "bare-title", "wrapped-title"])
@pytest.mark.parametrize("encoding", ["raw-colon-slash", "fully-escaped", "base64"])
def test_png_file_uri_metadata_survives_snapshot_with_private_neighbors(tmp_path, location, encoding):
    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))

    png = (b"\x89PNG\r\n\x1a\n"
           + chunk(b"IHDR", struct.pack(">IIBBBBB", 3, 2, 8, 2, 0, 0, 0))
           + chunk(b"tEXt", b"Comment\0file:///etc/x")
           + chunk(b"IDAT", zlib.compress(b"\0" + b"\xfb\xef\xff" * 3 + b"\0" + b"\x12\x34\x56" * 3))
           + chunk(b"IEND", b""))
    if encoding == "base64":
        ref = "data:image/png;base64," + base64.b64encode(png).decode()
    else:
        ref = "data:image/png," + quote_from_bytes(png, safe=":/" if encoding == "raw-colon-slash" else "")
    if encoding == "raw-colon-slash":
        assert "file:///etc/x" in ref
    image = f"![chart]({ref})" if location == "body" else f"MEDIA:{ref}"
    if location == "wrapped-title":
        image = f"`{image}`"
    private = ["file:///etc/secret.txt", "![private](file:///etc/private.png)",
               "`file:///etc/private.pdf`", "MEDIA:file:///etc/private.gif"]
    text = f"before {image} after " + " and ".join(private)
    session = Session(session_id="png-file-metadata", title=text if location != "body" else "PNG metadata",
                      messages=[{"role": "assistant", "content": text if location == "body" else "hello"}], workspace=str(tmp_path))
    # Independent data decoder and original production renderer must accept it.
    decoded = subprocess.run([NODE, "-e", "fetch(process.argv[1]).then(r=>r.arrayBuffer()).then(b=>process.stdout.write(Buffer.from(b)))", ref], capture_output=True, timeout=30, check=True).stdout
    assert decoded == png
    driver = tmp_path / "metadata-png-render.js"
    driver.write_text(_DRIVER_SRC, encoding="utf-8")
    render_input = f"![chart]({ref})"
    before = subprocess.run([NODE, str(driver), str(REPO_ROOT / "static" / "ui.js")], input=render_input, capture_output=True, text=True, timeout=30, check=True).stdout
    assert f'src="{ref}"' in before
    snapshot = shares.build_share_snapshot(session)
    actual = snapshot["messages"][0]["content"] if location == "body" else snapshot["title"]
    assert actual == f"before {image} after " + " and ".join([shares._PLACEHOLDER, f"![private]({shares._PLACEHOLDER})", f"`{shares._PLACEHOLDER}`", shares._PLACEHOLDER])
    if location == "body":
        after = subprocess.run([NODE, str(driver), str(REPO_ROOT / "static" / "ui.js")], input=actual, capture_output=True, text=True, timeout=30, check=True).stdout
        assert f'src="{ref}"' in after
    assert session.title == text if location != "body" else session.title == "PNG metadata"
    assert session.messages[0]["content"] == (text if location == "body" else "hello")


@pytest.mark.parametrize("prefix", ["data:image/png,", "data:image/png;base64,", "data:image/svg+xml,"])
def test_image_file_scrub_exemption_does_not_cover_invalid_or_oversized_refs(prefix):
    ref = prefix + "A" * shares._SHARE_DATA_IMAGE_MAX_CHARS + "file:///etc/private.png"
    content = _sanitize(f"![unsafe]({ref}) file:///etc/neighbor.txt")
    assert "file://" not in content and "/etc/neighbor.txt" not in content
    assert shares._PLACEHOLDER in content


@pytest.mark.parametrize("neighbor", ["bare", "code", "both-sides"])
def test_image_metadata_protection_stops_at_wrapped_title_boundary(tmp_path, neighbor):
    ref = "data:image/png,%89PNGfile:///etc/metadata"
    image = f"`MEDIA:{ref}`"
    if neighbor == "bare":
        text, expected = image + "file:///etc/secret.txt", image + shares._PLACEHOLDER
    elif neighbor == "code":
        text, expected = image + "`file:///etc/secret.txt`", image + f"`{shares._PLACEHOLDER}`"
    else:
        text = "file:///etc/before.txt " + image + "file:///etc/after.txt"
        expected = shares._PLACEHOLDER + " " + image + shares._PLACEHOLDER
    session = Session(session_id="metadata-span-boundary", title=text, messages=[{"role": "user", "content": "hello"}], workspace=str(tmp_path))
    assert shares.build_share_snapshot(session)["title"] == expected


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
@pytest.mark.parametrize("prefix", ["![a](x ", "![a](chart.png ", "![a](x\n", "![a](x\r\n", "![a](x\t"])
def test_malformed_markdown_prefix_cannot_hide_following_private_image(prefix,tmp_path):
    private="https://webui.example/api/media?path=/tmp/private.png"
    body=prefix+f"![b]({private})"
    session=Session(session_id="malformed-private-image",messages=[{"role":"assistant","content":body}])
    content=shares.build_share_snapshot(session)["messages"][0]["content"]
    driver=tmp_path/"malformed-private.js";driver.write_text(_DRIVER_SRC)
    rendered=subprocess.run([NODE,str(driver),str(REPO_ROOT/"static/ui.js")],input=content,text=True,capture_output=True,timeout=30,check=True).stdout
    assert "/api/media?path=" not in rendered
    assert private not in content
    assert session.messages[0]["content"]==body


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
@pytest.mark.parametrize("body", [
    '![a](https://webui.example/api/media?path=![b](https://cdn.example/public.png))',
    '![a](https://webui.example/api/media?path=/tmp/private.png "![b](https://cdn.example/public.png)")',
])
def test_supported_private_outer_image_cannot_skip_to_public_nested_image(body,tmp_path):
    session=Session(session_id="private-outer-image",messages=[{"role":"assistant","content":body}])
    content=shares.build_share_snapshot(session)["messages"][0]["content"]
    driver=tmp_path/"private-outer.js";driver.write_text(_DRIVER_SRC)
    rendered=subprocess.run([NODE,str(driver),str(REPO_ROOT/"static/ui.js")],input=content,text=True,capture_output=True,timeout=30,check=True).stdout
    assert "/api/media?path=" not in content
    assert "/api/media?path=" not in rendered


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
@pytest.mark.parametrize("prefix,suffix", [
    ("![a](x ", ""), ("![a](chart.png ", ""), ("![a](x\n", ""),
    ("![a](HTTPS://cdn.example/a.png ", ""), ("![a](<x> ", ""),
])
@pytest.mark.parametrize("form", ["large-base64", "raw-percent-metadata"])
def test_malformed_unknown_prefix_preserves_supported_real_png(prefix,suffix,form,tmp_path):
    def chunk(kind,data):
        return struct.pack(">I",len(data))+kind+data+struct.pack(">I",zlib.crc32(kind+data))
    width,height=(120,100) if form=="large-base64" else (3,2)
    pixels=bytes((i*73+i//256)%256 for i in range(width*height*3))
    stride=width*3
    raw=b"".join(b"\0"+pixels[i:i+stride] for i in range(0,len(pixels),stride))
    png=b"\x89PNG\r\n\x1a\n"+chunk(b"IHDR",struct.pack(">IIBBBBB",width,height,8,2,0,0,0))
    if form=="raw-percent-metadata":png+=chunk(b"tEXt",b"Comment\0file:///etc/x")
    png+=chunk(b"IDAT",zlib.compress(raw,level=0))+chunk(b"IEND",b"")
    ref=("data:image/png;base64,"+base64.b64encode(png).decode() if form=="large-base64" else "data:image/png,"+quote_from_bytes(png,safe=":/"))
    if form=="large-base64":assert len(ref)>shares._SHARE_MEDIA_SAFETY_MAX_CHARS
    decoded=subprocess.run([NODE,"-e","fetch(process.argv[1]).then(r=>r.arrayBuffer()).then(b=>process.stdout.write(Buffer.from(b)))",ref],capture_output=True,timeout=30,check=True).stdout
    assert decoded==png
    body=prefix+f"![b]({ref})"+suffix
    driver=tmp_path/"nested-real-png.js";driver.write_text(_DRIVER_SRC)
    before=subprocess.run([NODE,str(driver),str(REPO_ROOT/"static/ui.js")],input=body,text=True,capture_output=True,timeout=30,check=True).stdout
    assert f'src="{ref}"' in before
    session=Session(session_id="nested-real-png",messages=[{"role":"assistant","content":body}])
    content=shares.build_share_snapshot(session)["messages"][0]["content"]
    assert content==body
    after=subprocess.run([NODE,str(driver),str(REPO_ROOT/"static/ui.js")],input=content,text=True,capture_output=True,timeout=30,check=True).stdout
    assert f'src="{ref}"' in after
    assert session.messages[0]["content"]==body


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
@pytest.mark.parametrize("body", [
    '![public](https://cdn.example/a.png)',
    '![public](https://cdn.example/a.png "caption")',
    '![left](https://cdn.example/a.png) ![private](https://webui.example/api/media?path=/tmp/private.png) ![right](https://cdn.example/b.png)',
])
def test_strict_image_boundary_preserves_public_neighbors_and_caption(body,tmp_path):
    session=Session(session_id="strict-public-images",messages=[{"role":"assistant","content":body}])
    content=shares.build_share_snapshot(session)["messages"][0]["content"]
    driver=tmp_path/"strict-public.js";driver.write_text(_DRIVER_SRC)
    rendered=subprocess.run([NODE,str(driver),str(REPO_ROOT/"static/ui.js")],input=content,text=True,capture_output=True,timeout=30,check=True).stdout
    assert "https://cdn.example/a.png" in rendered
    assert "/api/media?path=" not in rendered
    if '![right]' in body:assert "https://cdn.example/b.png" in rendered
    else:assert content==body
