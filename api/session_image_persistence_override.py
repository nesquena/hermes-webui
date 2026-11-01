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

1.1.0 adds the bounded ``_image_payload_digest`` discriminator. The replay guard
fingerprints whole rows, so a save -> cold load -> save cycle would otherwise see
two rows that differ only by image bytes as exact replays and delete one. The
digest is stamped on the display transcript only; provider-replay rows are left
without it so strict provider schemas never gain an unknown key.
"""

from __future__ import annotations

import hashlib

_IMAGE_TYPES = {"image", "image_url", "input_image"}
# Pinned independently of upstream WebUI releases. Bump only with a regression
# test and an explicit compatibility review.
PERSISTENCE_OVERRIDE_VERSION = "1.1.0"
_MARKER = {"type": "text", "text": "[image attachment]"}
# Bounded provenance for the bytes the marker replaced. The replay guard
# fingerprints the WHOLE row, so two rows that differ only by image bytes must
# stay distinguishable once their content is projected -- otherwise the next
# save after a cold load reads them as exact replays and deletes one. 16 hex
# chars is collision-safe at session scale and costs 16 bytes per image row.
_DIGEST_FIELD = "_image_payload_digest"
_DIGEST_CHARS = 16


def _url_value(part: dict):
    """Return the part's image URL whether it is nested or a bare string."""
    image_url = part.get("image_url")
    if isinstance(image_url, dict):
        return image_url.get("url")
    return image_url


def _inline_image_payload(part):
    """Return the inline payload string for a part, or None when it is not inline.

    Accepted shapes: an OpenAI/Responses ``image_url``/``input_image`` part whose
    URL is a ``data:image/...`` URL (nested object or bare string), and an
    Anthropic ``image`` part with ``source.type == "base64"``. Remote references,
    placeholder URLs, and malformed parts are deliberately out of contract and
    are left byte-identical.
    """
    if not isinstance(part, dict):
        return None
    part_type = part.get("type")
    if not isinstance(part_type, str) or part_type not in _IMAGE_TYPES:
        return None
    url = _url_value(part)
    if isinstance(url, str) and url.lower().startswith("data:image/"):
        return url
    source = part.get("source")
    if isinstance(source, dict) and source.get("type") == "base64":
        data = source.get("data")
        if isinstance(data, str) and data:
            media_type = source.get("media_type")
            if isinstance(media_type, str) and media_type:
                return f"{media_type}:{data}"
            return data
    return None


def _is_inline_image_part(part) -> bool:
    """True only for parts that carry inline base64 image bytes.

    Accepted shapes: an OpenAI/Responses ``image_url``/``input_image`` part whose
    URL is a ``data:image/...`` URL (nested object or bare string), and an
    Anthropic ``image`` part with ``source.type == "base64"``. Remote references,
    placeholder URLs, and malformed parts are deliberately out of contract and
    are left byte-identical.
    """
    return _inline_image_payload(part) is not None


def compact_persisted_image_parts(messages, *, stamp_digest: bool = True) -> int:
    """Replace inline image bytes in a persisted projection with markers.

    Callers pass a projection they own (a save-time snapshot or a deep copy),
    never the live provider payload.

    ``stamp_digest`` adds ``_image_payload_digest`` to a projected row so the
    lossless dedup guard can still tell two originally-different rows apart on a
    later save. Keep it OFF for provider-replay rows: those are serialized and
    later sent to strict provider schemas that must not gain unknown keys. Only
    the display transcript is fingerprinted, and only it is deduplicated.
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
        digest = hashlib.sha256()
        for part in content:
            payload = _inline_image_payload(part)
            if payload is not None:
                digest.update(payload.encode("utf-8", "surrogatepass"))
                digest.update(b"\x00")
                projected.append(dict(_MARKER))
                changed += 1
                message_changed = True
            else:
                projected.append(part)
        if message_changed:
            message["content"] = projected
            if stamp_digest:
                # Content-equality discriminator that survives the projection.
                # The marker alone is identical for every image, so without this
                # the next save's lossless dedup would see two originally
                # distinct rows as exact replays and drop one.
                message[_DIGEST_FIELD] = digest.hexdigest()[:_DIGEST_CHARS]
    return changed

