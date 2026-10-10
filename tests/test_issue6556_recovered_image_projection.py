"""Regression tests for #6556.

A recovered user turn was re-projected into the model context carrying its full
inline base64 image payload. The duplicated multi-MB ``data:`` URIs pushed a
single request content string past the provider's ~10 MiB per-string limit, so
every subsequent send failed with HTTP 400 (``string too long``) and the session
became permanently unusable (the oversized message is persisted in the session's
``context_messages``).

Fix: ``_recovered_model_context_projection`` strips inline ``data:`` image parts
to a placeholder ONLY when the message's total inline image payload is
pathologically large (> ``_RECOVERED_PROJECTION_INLINE_IMAGE_SOFT_CAP``). Below
the cap content is returned byte-identical, so normal single images are untouched
and the only behavior change is turning an already-bricked session into a
recoverable one. The structured ``attachments`` reference on the message is
preserved, so the original image stays reachable via ``/api/file/raw``.
"""
from __future__ import annotations

import json

from api.models import (
    _recovered_model_context_projection,
    _strip_oversized_inline_images,
    _RECOVERED_PROJECTION_INLINE_IMAGE_SOFT_CAP,
)

PROVIDER_PER_STRING_CAP = 10 * 1024 * 1024
PLACEHOLDER_MARK = "image omitted from recovered context"


def _data_url(size: int) -> str:
    return "data:image/jpeg;base64," + ("A" * size)


def _has_placeholder(content) -> bool:
    return any(
        isinstance(p, dict) and p.get("type") == "text"
        and PLACEHOLDER_MARK in (p.get("text") or "")
        for p in content
    )


def _has_inline_data_image(content) -> bool:
    for p in content:
        if not isinstance(p, dict):
            continue
        ref = p.get("image_url")
        url = ref.get("url") if isinstance(ref, dict) else (ref if isinstance(ref, str) else p.get("url"))
        if isinstance(url, str) and url.startswith("data:"):
            return True
    return False


def test_oversized_inline_image_stripped_below_provider_cap():
    """The exact #6556 shape: ~19 MB of inline base64 (four ~4.9 MB images).
    The projection must drop the inline payload for a placeholder, land well
    under the provider cap, and leave the structured attachment reference."""
    big = _data_url(19_000_000)
    msg = {
        "role": "user", "timestamp": 3000,
        "content": [
            {"type": "text", "text": "here are my screenshots"},
            {"type": "image_url", "image_url": {"url": big}},
        ],
        "attachments": [{"id": "att1", "path": "/x/att1.jpg"}],
    }
    proj = _recovered_model_context_projection(dict(msg))
    assert len(json.dumps(proj["content"])) < PROVIDER_PER_STRING_CAP
    assert _has_placeholder(proj["content"])
    assert not _has_inline_data_image(proj["content"])
    # text parts survive, and the source attachment reference is untouched.
    assert any(p.get("text") == "here are my screenshots" for p in proj["content"])
    assert msg["attachments"] == [{"id": "att1", "path": "/x/att1.jpg"}]


def test_multiple_images_over_total_cap_are_stripped():
    """Several individually-moderate images whose TOTAL exceeds the cap are
    stripped — the gate is on total inline payload, not a single part. Assert
    on the placeholder (the fix behavior), since two 5 MB URLs also sit under
    the 10 MiB provider cap on master."""
    half = _data_url(5_000_000)
    msg = {
        "role": "user", "timestamp": 3100,
        "content": [
            {"type": "image_url", "image_url": {"url": half}},
            {"type": "image_url", "image_url": {"url": half}},
        ],
    }
    proj = _recovered_model_context_projection(dict(msg))
    assert _has_placeholder(proj["content"])
    assert not _has_inline_data_image(proj["content"])


def test_normal_small_image_left_untouched():
    """Isolation cell: a normal small inline image (well under the cap) must
    pass through byte-identical — the fix must not change the common case."""
    small = _data_url(300_000)  # ~300 KB
    content = [
        {"type": "text", "text": "one small icon"},
        {"type": "image_url", "image_url": {"url": small}},
    ]
    msg = {"role": "user", "timestamp": 3200, "content": [dict(p) for p in content]}
    proj = _recovered_model_context_projection(dict(msg))
    assert proj["content"] == content


def test_single_large_image_just_under_cap_untouched():
    """A single legitimate image just under the soft cap is preserved."""
    url = _data_url(_RECOVERED_PROJECTION_INLINE_IMAGE_SOFT_CAP - 1024)
    content = [{"type": "image_url", "image_url": {"url": url}}]
    msg = {"role": "user", "timestamp": 3300, "content": [dict(p) for p in content]}
    proj = _recovered_model_context_projection(dict(msg))
    assert proj["content"] == content


def test_string_content_unaffected():
    """Plain-string content (no image parts) is returned unchanged."""
    msg = {"role": "user", "timestamp": 3400, "content": "just text"}
    proj = _recovered_model_context_projection(dict(msg))
    assert proj["content"] == "just text"


def test_bare_string_image_url_also_stripped():
    """An image part whose ``image_url`` is a bare oversized data: string
    (not a dict) is also bounded, and replaced by the placeholder."""
    msg = {
        "role": "user", "timestamp": 3500,
        "content": [{"type": "image_url", "image_url": _data_url(19_000_000)}],
    }
    proj = _recovered_model_context_projection(dict(msg))
    assert len(json.dumps(proj["content"])) < PROVIDER_PER_STRING_CAP
    assert _has_placeholder(proj["content"])


def test_strip_helper_reports_changed_only_when_over_cap():
    """Direct helper contract: ``changed`` is False below the cap and True
    above it, and the returned list is a new object only when changed."""
    under = [{"type": "image_url", "image_url": {"url": _data_url(1_000_000)}}]
    out, changed = _strip_oversized_inline_images(under)
    assert changed is False and out is under

    over = [{"type": "image_url", "image_url": {"url": _data_url(19_000_000)}}]
    out2, changed2 = _strip_oversized_inline_images(over)
    assert changed2 is True and out2 is not over
    assert _has_placeholder(out2)


def test_non_list_content_is_passthrough_in_helper():
    """The helper is a no-op on non-list content."""
    assert _strip_oversized_inline_images("hi") == ("hi", False)
    assert _strip_oversized_inline_images(None) == (None, False)
