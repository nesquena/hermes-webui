"""Regression coverage for large native image redaction overhead.

Native multimodal messages store raster images as base64 data URIs.  Those
opaque image bytes must not be sent through the text credential redactor: a
large image can randomly contain one of the cheap prefilter's markers and then
pay for every regex pass in the agent redactor.
"""
import base64

from api import helpers


def _png_data_uri_with_sensitive_marker() -> str:
    # Align a decoded ``AKIA`` quartet after the 8-byte PNG signature so the
    # encoded payload contains a marker that routes through the hard redactor.
    marker_bytes = base64.b64decode("AKIA")
    raw = b"\x89PNG\r\n\x1a\n" + b"\x00" + marker_bytes + (b"image-bytes" * 32)
    encoded = base64.b64encode(raw).decode("ascii")
    assert "AKIA" in encoded
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

    assert helpers._redact_value(content_part, _enabled=True) == content_part
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
