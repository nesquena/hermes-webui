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


def compact_persisted_image_parts(messages) -> int:
    """Replace image content parts in owned persisted rows with markers."""
    changed = 0
    for message in messages or ():
        message_changed = False
        if not isinstance(message, dict):
            continue
        content = message.get("content")
        if not isinstance(content, list):
            continue
        projected = []
        for part in content:
            image_url = part.get("image_url") if isinstance(part, dict) else None
            image_value = image_url.get("url") if isinstance(image_url, dict) else image_url
            is_inline_image = (
                isinstance(part, dict)
                and isinstance(part.get("type"), str)
                and part.get("type") in _IMAGE_TYPES
                and isinstance(image_value, str)
                and image_value.lower().startswith("data:image/")
            )
            if is_inline_image:
                projected.append(dict(_MARKER))
                changed += 1
                message_changed = True
            else:
                projected.append(part)
        if message_changed:
            message["content"] = projected
    return changed
