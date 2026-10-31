"""Non-invasive persistence override for native image content.

The WebUI sends full data URLs to the active model, but persisted session JSON
must retain only a small marker. This adapter is deliberately separate from
the upstream streaming implementation so it can be pinned and removed without
conflicting with upstream updates.

IMPORTANT UPDATE NOTE
---------------------
This module is a local compatibility override, not an upstream replacement.
Keep it separate from ``api/streaming.py`` and preserve the call in
``api/models.py:Session.save`` when updating or rebasing WebUI. Before changing
or removing it, run ``tests/test_session_image_persistence_override.py`` and
reproduce the oversized-session history-load request. The upstream
``_compact_image_parts_for_persistence`` guard only covers completed tool
results; this save-boundary override also protects persisted user image parts.
The model-facing payload must continue to receive native image bytes; only the
serialized session copy is compacted. Bump ``PERSISTENCE_OVERRIDE_VERSION``
only with a regression test and an explicit compatibility review.
"""

from __future__ import annotations

_IMAGE_TYPES = {"image", "image_url", "input_image"}
# Pinned independently of upstream WebUI releases. Bump only with a regression
# test and an explicit compatibility review.
PERSISTENCE_OVERRIDE_VERSION = "1.0.0"
_MARKER = {"type": "text", "text": "[image attachment]"}


def _url_value(part: dict):
    """Return the part's image URL whether it is nested or a bare string."""
    image_url = part.get("image_url")
    if isinstance(image_url, dict):
        return image_url.get("url")
    return image_url


def _is_inline_image_part(part) -> bool:
    """True only for parts that carry inline base64 image bytes.

    Accepted shapes: an OpenAI/Responses ``image_url``/``input_image`` part whose
    URL is a ``data:image/...`` URL (nested object or bare string), and an
    Anthropic ``image`` part with ``source.type == "base64"``. Remote references,
    placeholder URLs, and malformed parts are deliberately out of contract and
    are left byte-identical.
    """
    if not isinstance(part, dict):
        return False
    part_type = part.get("type")
    if not isinstance(part_type, str) or part_type not in _IMAGE_TYPES:
        return False
    url = _url_value(part)
    if isinstance(url, str) and url.lower().startswith("data:image/"):
        return True
    source = part.get("source")
    if isinstance(source, dict) and source.get("type") == "base64":
        data = source.get("data")
        return isinstance(data, str) and bool(data)
    return False


def compact_persisted_image_parts(messages) -> int:
    """Replace inline image bytes in a persisted projection with markers.

    Callers pass a projection they own (a save-time snapshot or a deep copy),
    never the live provider payload.
    """
    changed = 0
    for message in messages or ():
        if not isinstance(message, dict):
            continue
        content = message.get("content")
        if not isinstance(content, list):
            continue
        projected = []
        message_changed = False
        for part in content:
            if _is_inline_image_part(part):
                projected.append(dict(_MARKER))
                changed += 1
                message_changed = True
            else:
                projected.append(part)
        if message_changed:
            message["content"] = projected
    return changed

