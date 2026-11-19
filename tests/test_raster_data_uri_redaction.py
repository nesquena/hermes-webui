"""Regression coverage for large native image redaction overhead.

Native multimodal messages store raster images as base64 data URIs.  Those
opaque image bytes must not be sent through the text credential redactor: a
large image can randomly contain one of the cheap prefilter's markers and then
pay for every regex pass in the agent redactor.
"""
import base64
import json
import struct
import zlib

import pytest

from api import helpers
from api.session_export_html import render_session_html


_FAKE_AWS_KEY = "AKIATESTFAKEKEY12345"
_ONE_PIXEL_JPEG = base64.b64decode(
    "/9j/4AAQSkZJRgABAQAAAQABAAD/2wBDAP//////////////////////////////////////////////////////////////////////////////////////2wBDAf//////////////////////////////////////////////////////////////////////////////////////wAARCAABAAEDASIAAhEBAxEB/8QAFQABAQAAAAAAAAAAAAAAAAAAAAf/xAAUEAEAAAAAAAAAAAAAAAAAAAAA/9oADAMBAAIQAxAAAAF//8QAFBABAAAAAAAAAAAAAAAAAAAAAP/aAAgBAQABBQJ//8QAFBEBAAAAAAAAAAAAAAAAAAAAAP/aAAgBAwEBPwF//8QAFBEBAAAAAAAAAAAAAAAAAAAAAP/aAAgBAgEBPwF//8QAFBABAAAAAAAAAAAAAAAAAAAAAP/aAAgBAQAGPwJ//8QAFBABAAAAAAAAAAAAAAAAAAAAAP/aAAgBAQABPyF//9oADAMBAAIAAwAAABAf/8QAFBEBAAAAAAAAAAAAAAAAAAAAAP/aAAgBAwEBPxB//8QAFBEBAAAAAAAAAAAAAAAAAAAAAP/aAAgBAgEBPxB//8QAFBABAAAAAAAAAAAAAAAAAAAAAP/aAAgBAQABPxB//9k="
)
_ONE_PIXEL_GIF = base64.b64decode(
    "R0lGODlhAQABAIAAAAAAAP///ywAAAAAAQABAAACAUwAOw=="
)
_ONE_PIXEL_WEBP = base64.b64decode(
    "UklGRiIAAABXRUJQVlA4IBYAAAAwAQCdASoBAAEAAUAmJaQAA3AA/vuU"
)


def _one_pixel_bmp() -> bytes:
    pixel_data = b"\x00\x00\x00\x00"  # BGR black + row padding
    file_size = 54 + len(pixel_data)
    file_header = b"BM" + struct.pack("<IHHI", file_size, 0, 0, 54)
    dib_header = struct.pack(
        "<IiiHHIIiiII",
        40,
        1,
        1,
        1,
        24,
        0,
        len(pixel_data),
        2835,
        2835,
        0,
        0,
    )
    return file_header + dib_header + pixel_data


def _png_chunk(kind: bytes, data: bytes) -> bytes:
    crc = zlib.crc32(kind + data) & 0xFFFFFFFF
    return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", crc)


def _valid_png_bytes_with_sensitive_marker(*, extra_bytes: int = 0) -> bytes:
    """Build a real 1x1 RGBA PNG whose base64 contains an ``AKIA`` quartet."""
    marker_bytes = base64.b64decode("AKIA")
    ihdr = struct.pack(">IIBBBBB", 1, 1, 8, 6, 0, 0, 0)
    # The first data byte starts at absolute offset 41. One byte of padding
    # aligns marker_bytes to a base64 quantum, preserving the literal AKIA
    # quartet that exercises the redaction prefilter.
    marker_chunk = _png_chunk(
        b"raNd",
        b"\x00" + marker_bytes + (b"image-bytes" * 32) + (b"x" * extra_bytes),
    )
    idat = _png_chunk(b"IDAT", zlib.compress(b"\x00\x00\x00\x00\x00"))
    prefix = b"\x89PNG\r\n\x1a\n" + _png_chunk(b"IHDR", ihdr) + marker_chunk + idat
    # Make the complete PNG length a multiple of three. This lets the
    # maintainer-supplied post-IEND attack append decoded bytes whose standard
    # base64 representation remains the literal fake credential.
    for pad_len in range(3):
        raw = prefix + _png_chunk(b"paDd", b"x" * pad_len) + _png_chunk(b"IEND", b"")
        if len(raw) % 3 == 0:
            encoded = base64.b64encode(raw).decode("ascii")
            assert "AKIA" in encoded
            return raw
    raise AssertionError("could not align synthetic PNG")


def _png_data_uri_with_sensitive_marker() -> str:
    raw = _valid_png_bytes_with_sensitive_marker()
    encoded = base64.b64encode(raw).decode("ascii")
    assert "AKIA" in encoded
    return f"data:image/png;base64,{encoded}"


def _png_data_uri_with_post_iend_credential() -> str:
    raw = _valid_png_bytes_with_sensitive_marker()
    suffix = base64.b64decode(_FAKE_AWS_KEY, validate=True)
    encoded = base64.b64encode(raw + suffix).decode("ascii")
    assert encoded.endswith(_FAKE_AWS_KEY)
    return f"data:image/png;base64,{encoded}"


def test_native_raster_data_uri_bypasses_text_redactor(monkeypatch):
    uri = _png_data_uri_with_sensitive_marker()
    calls = []
    monkeypatch.setattr(
        helpers,
        "_redact_fn_cached",
        lambda text: calls.append(text) or "unexpected-redaction",
    )

    content_part = {
        "type": "image_url",
        "image_url": {"url": uri, "detail": "auto"},
    }

    redacted = helpers.redact_session_data(
        {"messages": [{"role": "user", "content": [content_part]}]}
    )
    assert redacted["messages"][0]["content"][0] == content_part
    assert calls == []


def test_multimegabyte_native_raster_still_bypasses_text_redactor(monkeypatch):
    raw = _valid_png_bytes_with_sensitive_marker(extra_bytes=1_000_000)
    uri = f"data:image/png;base64,{base64.b64encode(raw).decode('ascii')}"
    calls = []
    monkeypatch.setattr(
        helpers,
        "_redact_fn_cached",
        lambda text: calls.append(text) or "unexpected-redaction",
    )

    result = helpers.redact_session_data(
        {
            "messages": [
                {
                    "role": "user",
                    "content": [{"type": "image_url", "image_url": {"url": uri}}],
                }
            ]
        }
    )

    assert result["messages"][0]["content"][0]["image_url"]["url"] == uri
    assert calls == []


def test_native_raster_data_uri_accepts_uppercase_mime(monkeypatch):
    uri = _png_data_uri_with_sensitive_marker().replace(
        "data:image/png",
        "data:image/PNG",
        1,
    )
    calls = []
    monkeypatch.setattr(
        helpers,
        "_redact_fn_cached",
        lambda text: calls.append(text) or "unexpected-redaction",
    )

    content_part = {"type": "image_url", "image_url": {"url": uri}}

    redacted = helpers.redact_session_data(
        {"messages": [{"role": "user", "content": [content_part]}]}
    )
    assert redacted["messages"][0]["content"][0] == content_part
    assert calls == []


def test_raster_data_uri_outside_image_part_keeps_security_boundary(monkeypatch):
    uri = _png_data_uri_with_sensitive_marker()
    calls = []
    monkeypatch.setattr(
        helpers,
        "_redact_fn_cached",
        lambda text: calls.append(text) or "redacted",
    )

    assert helpers._redact_value({"content": uri}, _enabled=True) == {
        "content": "redacted",
    }
    assert calls == [uri]


def test_non_raster_image_part_keeps_security_boundary(monkeypatch):
    uri = "data:image/svg+xml;base64," + base64.b64encode(
        b"<svg> " + base64.b64decode("AKIA") + b" sensitive text</svg>"
    ).decode("ascii")
    assert "AKIA" in uri
    calls = []
    monkeypatch.setattr(
        helpers,
        "_redact_fn_cached",
        lambda text: calls.append(text) or "redacted",
    )

    result = helpers._redact_value(
        {"type": "image_url", "image_url": {"url": uri}},
        _enabled=True,
    )

    assert result["image_url"]["url"] == "redacted"
    assert calls == [uri]


def test_declared_raster_with_wrong_magic_keeps_security_boundary(monkeypatch):
    encoded = base64.b64encode(b"not-a-png" + base64.b64decode("AKIA")).decode("ascii")
    uri = f"data:image/png;base64,{encoded}"
    calls = []
    monkeypatch.setattr(
        helpers,
        "_redact_fn_cached",
        lambda text: calls.append(text) or "redacted",
    )

    result = helpers._redact_value(
        {"type": "image_url", "image_url": {"url": uri}},
        _enabled=True,
    )

    assert result["image_url"]["url"] == "redacted"
    assert calls == [uri]


def test_post_iend_credential_falls_through_to_text_redaction(monkeypatch):
    uri = _png_data_uri_with_post_iend_credential()
    calls = []

    def redact(text):
        calls.append(text)
        return text.replace(_FAKE_AWS_KEY, "[REDACTED]")

    monkeypatch.setattr(helpers, "_redact_fn_cached", redact)
    content_part = {"type": "image_url", "image_url": {"url": uri}}

    result = helpers.redact_session_data(
        {"messages": [{"role": "user", "content": [content_part]}]}
    )

    assert _FAKE_AWS_KEY not in result["messages"][0]["content"][0]["image_url"]["url"]
    assert calls == [uri]
    assert helpers._is_native_raster_data_uri(uri) is False


def test_post_iend_credential_is_removed_by_real_redactor():
    uri = _png_data_uri_with_post_iend_credential()
    result = helpers.redact_session_data(
        {
            "messages": [
                {
                    "role": "user",
                    "content": [{"type": "image_url", "image_url": {"url": uri}}],
                }
            ]
        }
    )

    assert _FAKE_AWS_KEY not in json.dumps(result)


def test_image_exemption_is_scoped_to_direct_message_content_parts(monkeypatch):
    valid_uri = _png_data_uri_with_sensitive_marker()
    calls = []
    monkeypatch.setattr(
        helpers,
        "_redact_fn_cached",
        lambda text: calls.append(text) or text.replace("AKIA", "[REDACTED]"),
    )
    image_shaped = {"type": "image_url", "image_url": {"url": valid_uri}}
    session = {
        "messages": [
            {
                "role": "user",
                "content": [
                    image_shaped,
                    {"type": "text", "text": f"sibling={_FAKE_AWS_KEY}"},
                ],
                "metadata": image_shaped,
            },
            {"role": "assistant", "content": [image_shaped]},
        ],
        "tool_calls": [image_shaped],
        "todo_state": {"image": image_shaped},
        "runtime_journal_snapshot": {"messages": [image_shaped]},
    }

    result = helpers.redact_session_data(session)

    # Only the canonical messages[*].content[*] image URL stays opaque.
    assert result["messages"][0]["content"][0]["image_url"]["url"] == valid_uri
    assert _FAKE_AWS_KEY not in result["messages"][0]["content"][1]["text"]
    assert "AKIA" not in result["messages"][0]["metadata"]["image_url"]["url"]
    assert "AKIA" not in result["messages"][1]["content"][0]["image_url"]["url"]
    assert "AKIA" not in result["tool_calls"][0]["image_url"]["url"]
    assert "AKIA" not in result["todo_state"]["image"]["image_url"]["url"]
    assert "AKIA" not in result["runtime_journal_snapshot"]["messages"][0]["image_url"]["url"]
    assert calls


def test_crafted_image_is_redacted_from_json_html_and_journal(monkeypatch):
    uri = _png_data_uri_with_post_iend_credential()
    monkeypatch.setattr(
        helpers,
        "_redact_fn_cached",
        lambda text: text.replace(_FAKE_AWS_KEY, "[REDACTED]"),
    )
    crafted_part = {"type": "image_url", "image_url": {"url": uri}}
    safe = helpers.redact_session_data(
        {
            "title": "security regression",
            "messages": [{"role": "user", "content": [crafted_part]}],
            "runtime_journal_snapshot": {
                "messages": [crafted_part],
                "persisted_payload": uri,
            },
        }
    )

    json_export = json.dumps(safe)
    html_export = render_session_html(safe)
    assert _FAKE_AWS_KEY not in json_export
    assert _FAKE_AWS_KEY not in html_export
    assert _FAKE_AWS_KEY not in json.dumps(safe["runtime_journal_snapshot"])


def test_uncertain_base64_forms_fail_closed():
    valid_uri = _png_data_uri_with_sensitive_marker()
    header, payload = valid_uri.split(",", 1)
    hostile = [
        f"{header},{payload[:16]}\n{payload[16:]}",
        f"{valid_uri}?token={_FAKE_AWS_KEY}",
        f"{valid_uri} {_FAKE_AWS_KEY}",
        f"{header},{payload[:-1]}",
        f"{header},{payload}=A",
    ]

    assert all(not helpers._is_native_raster_data_uri(value) for value in hostile)


@pytest.mark.parametrize(
    "mime,raw",
    [
        ("png", _valid_png_bytes_with_sensitive_marker()),
        ("jpeg", _ONE_PIXEL_JPEG),
        ("gif", _ONE_PIXEL_GIF),
        ("webp", _ONE_PIXEL_WEBP),
        ("bmp", _one_pixel_bmp()),
    ],
)
def test_supported_raster_requires_exact_terminal_boundary(mime, raw):
    exact = f"data:image/{mime};base64,{base64.b64encode(raw).decode('ascii')}"
    trailing = (
        f"data:image/{mime};base64,"
        f"{base64.b64encode(raw + b'trailing-bytes').decode('ascii')}"
    )

    assert helpers._is_native_raster_data_uri(exact) is True
    assert helpers._is_native_raster_data_uri(trailing) is False


def test_webp_animation_frame_requires_real_nested_image_chunk():
    fake_frame = b"\x00" * 16
    anmf = b"ANMF" + struct.pack("<I", len(fake_frame)) + fake_frame
    raw = b"RIFF" + struct.pack("<I", len(anmf) + 4) + b"WEBP" + anmf
    uri = f"data:image/webp;base64,{base64.b64encode(raw).decode('ascii')}"

    assert helpers._is_native_raster_data_uri(uri) is False
