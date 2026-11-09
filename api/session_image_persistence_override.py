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

import copy
import hashlib
import json
import re

_IMAGE_TYPES = {"image", "image_url", "input_image"}
# Pinned independently of upstream WebUI releases. Bump only with a regression
# test and an explicit compatibility review.
PERSISTENCE_OVERRIDE_VERSION = "1.2.0"
_MARKER = {"type": "text", "text": "[image attachment]"}
# Bounded provenance for the bytes the marker replaced. The replay guard
# fingerprints the WHOLE row, so two rows that differ only by image bytes must
# stay distinguishable once their content is projected -- otherwise the next
# save after a cold load reads them as exact replays and deletes one. 16 hex
# chars is collision-safe at session scale and costs 16 bytes per image row.
_DIGEST_FIELD = "_image_payload_digest"
_DIGEST_CHARS = 16
# Payload keys that hold durable message arrays in WebUI session/journal shapes.
# Default is the display transcript only: `context_messages` is the provider
# replay prefix, and an existing contract (tests/test_cancel_restart_journal_recovery.py)
# requires those rows to survive a save/load round trip byte-identically.
# Bounding that prefix needs load-time rehydration, which is a separate change.
_DISPLAY_MESSAGE_KEYS = frozenset({"messages"})


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


_MASK = "<inline-image>"

# Agent-written rows store structured content as this literal prefix followed by
# a JSON array (``_STATE_DB_CONTENT_JSON_PREFIX`` in api/models.py). Kept as a
# local literal so this module stays importable on its own.
_STATE_DB_CONTENT_PREFIX = "\x00json:"

# A run long enough to be a stored image payload rather than prose that merely
# mentions the scheme. Real snippets carry ~4,000-char previews; the minimum
# keeps ordinary discussion of data URLs untouched.
_MIN_INLINE_B64 = 256

_INLINE_DATA_URL_RE = re.compile(
    r"data:image/[A-Za-z0-9.+-]+;base64,[A-Za-z0-9+/=]{%d,}" % _MIN_INLINE_B64
)


def _mask_inline_string(value):
    """Replace base64 runs inside a bare string, keeping all other text verbatim.

    Returns the masked string, or None when there is nothing to replace.

    Only the matched data URL is substituted; the surrounding characters are
    preserved exactly. A captured tool-call ``snippet`` can hold a Python
    ``repr`` of the provider payload -- single-quoted, not valid JSON -- so it
    must never be reparsed and reserialized here: doing so would rewrite the
    captured record and its escaping. A targeted substitution is both safer and
    sufficient, because the bytes are what must be bounded.
    """
    if "data:image/" not in value.lower():
        return None
    # Replace EVERY qualifying run in one pass. Masking one run per save let a
    # later save mask the next run and re-stamp ``_image_payload_digest`` from a
    # string that had already lost the distinction the digest exists to capture:
    # two partial rows whose snippets held ``[image A, image C]`` and
    # ``[image B, image C]`` collapsed after cold reload (2 -> 2 -> 1 -> 1 rows),
    # because the second save digested an identical, fully-projected remainder.
    # Masking all runs here erases every payload on the FIRST save, while the
    # digest is taken from the COMPLETE original string, so the distinction
    # survives on the row itself.
    masked, count = _INLINE_DATA_URL_RE.subn(_MASK, value)
    if not count:
        # The substring is present but no run is long enough to be a payload.
        return None
    return masked


def _mask_inline_payload(part):
    """Return the part with its inline payload masked, preserving every other key.

    The projection replaces a whole part with one shared marker, so any structural
    or value distinction the part carried -- ``detail: low`` vs ``high``, the part
    type, a container key, an extra field -- is erased along with the bytes. The
    persisted discriminator must therefore cover what was erased, not only the
    payload string, or two legitimately distinct rows collapse on the next save.
    """
    masked = copy.deepcopy(part)
    url_value = masked.get("image_url")
    if isinstance(url_value, dict):
        url_value["url"] = _MASK
    elif isinstance(url_value, str):
        masked["image_url"] = _MASK
    source = masked.get("source")
    if isinstance(source, dict) and source.get("type") == "base64":
        source["data"] = _MASK
    return masked


def _encode_nested_path(path):
    """Canonical, length-delimited, TYPE-TAGGED encoding of a provenance path.

    Concatenating raw keys with punctuation aliases distinct traversals. Both of
    these render as the text ``a.b``:

        {"a.b": <image>}        -> literal key "a.b"
        {"a": {"b": <image>}}   -> traversal a then b

    and both of these render as ``a[0]``:

        {"a[0]": <image>}       -> literal key "a[0]"
        {"a": [<image>]}        -> list a at index 0

    Two partial rows that differ only in such placement therefore produced
    identical hash input. That is not a hash collision -- the bytes fed to the
    digest were literally equal -- so the loader's duplicate guard dropped one
    row on the next save (measured 2 -> 1 -> 1).

    Each segment carries a type tag (``k`` for a dict key, ``i`` for a list
    index) and a byte length, so no key content can shift a boundary and no key
    can impersonate a traversal.
    """
    out = bytearray(b"\x02nested")
    for kind, value in path:
        if kind == "k":
            segment = value.encode("utf-8", "surrogatepass")
            tag = b"k"
        else:
            segment = str(value).encode("ascii")
            tag = b"i"
        out += b"\x00" + tag + b"\x00" + str(len(segment)).encode("ascii")
        out += b"\x00" + segment
    return bytes(out)


def _update_part_digest(digest, part, payload, position):
    """Feed the position, masked structure AND payload bytes into the digest.

    ``position`` is either the part's index in the ORIGINAL array (flat ``content``
    lane) or a typed tuple path (nested ``_partial_tool_calls`` lane); both are
    encoded unambiguously. The projection emits
    the same marker regardless of where the image sat, so
    ``[image, text("[image attachment]")]`` and
    ``[text("[image attachment]"), image]`` would otherwise produce identical
    markers and an identical digest -- two legitimately distinct rows collapsing
    on the next save.
    """
    if isinstance(position, tuple):
        digest.update(_encode_nested_path(position))
    else:
        digest.update(f"pos={position}".encode("utf-8", "surrogatepass"))
    digest.update(b"\x00")
    try:
        masked = json.dumps(
            _mask_inline_payload(part),
            sort_keys=True,
            ensure_ascii=False,
            default=str,
        )
    except (TypeError, ValueError):
        masked = repr(part)
    digest.update(masked.encode("utf-8", "surrogatepass"))
    digest.update(b"\x00")
    digest.update(payload.encode("utf-8", "surrogatepass"))
    digest.update(b"\x00")


def _project_parts(content, digest):
    """Return (projected_parts, removed_payloads) for one content array.

    Every part is compacted in ONE pass: an image-typed part becomes the marker,
    and any other part is walked for nested strings carrying a raw data URL. That
    second half matters because a ``{"type": "text", "text": "...data:image/..."
    }`` part holds bytes too, and leaving them for a later save makes the digest
    unstable: the next save finds nothing left in the sentinel form, falls back to
    the plain-string lane, and re-stamps ``_image_payload_digest`` from a string
    that no longer distinguishes two originally-different rows -- after which the
    replay guard deletes one (reproduced: row count 2 -> 2 -> 1 -> 1).
    """
    projected = []
    removed = []
    for position, part in enumerate(content):
        payload = _inline_image_payload(part)
        if payload is None:
            # Not an image part, but it may still CONTAIN inline bytes in a nested
            # string. Project it in this same pass so the digest is stamped from
            # the complete original exactly once.
            nested, nested_changed = _project_nested(part, digest, (("i", position),))
            projected.append(nested if nested_changed else part)
            continue
        _update_part_digest(digest, part, payload, position)
        projected.append(dict(_MARKER))
        removed.append(payload)
    return projected, removed


def _project_nested(value, digest, path):
    """Recursively replace inline image parts inside an arbitrary JSON value.

    ``_partial_tool_calls`` stores captured tool arguments for interrupted turns;
    a screenshot-style tool holds image parts inside ``args``, which the content
    and api_content lanes never visit. Those bytes reached disk unbounded.

    ``path`` (not a bare index) is fed to the digest so a part's position inside
    the nested structure participates in the erased-provenance fingerprint, for
    the same reason ``_project_parts`` tracks the array index.

    Returns (new_value, changed) and is copy-on-write: a value with no inline
    image bytes is returned as the SAME object.
    """
    if isinstance(value, dict):
        payload = _inline_image_payload(value)
        if payload is not None:
            _update_part_digest(digest, value, payload, path)
            return dict(_MARKER), True
        out = None
        for key, child in value.items():
            new_child, changed = _project_nested(child, digest, path + (("k", key),))
            if changed:
                if out is None:
                    out = dict(value)
                out[key] = new_child
        return (value if out is None else out), out is not None
    if isinstance(value, list):
        out = None
        for index, item in enumerate(value):
            new_item, changed = _project_nested(item, digest, path + (("i", index),))
            if changed:
                if out is None:
                    out = list(value)
                out[index] = new_item
        return (value if out is None else out), out is not None
    if isinstance(value, str):
        # A bare string that itself carries inline image bytes. This is the
        # captured tool-call ``snippet`` lane: a vision tool records a preview
        # holding the data URL directly while its ``args`` point at a disk path,
        # so the dict/list branches above never reach it. Real session
        # 24748c2f63ab.json held 17 such snippets = 57,303 base64 chars that
        # survived migration untouched.
        #
        # Only the base64 RUN is replaced; every surrounding character is kept
        # verbatim. That matters because a snippet may hold a Python ``repr`` of
        # the provider payload (single quotes, not JSON), which must not be
        # reparsed or reserialized -- reformatting it would silently rewrite the
        # captured record.
        masked = _mask_inline_string(value)
        if masked is not None:
            _update_part_digest(digest, {"type": "text", "text": _MASK}, value, path)
            return masked, True
    return value, False


def _project_sentinelled_content(raw, digest):
    """Compact a ``\\x00json:``-prefixed structured-content string.

    Agent-written rows store their content parts as the literal prefix
    ``\x00json:`` followed by a JSON array (see
    ``_STATE_DB_CONTENT_JSON_PREFIX`` in ``api/models.py``). The list branch in
    ``compact_persisted_image_parts`` never sees those, so the recovery and
    discoverability writers -- which pass ``_state_db_row_to_sidecar`` output
    through unchanged -- persisted the full inline image bytes.

    Returns the re-encoded sentinelled string, or None when the value is not in
    that form or holds no inline bytes (so an untouched row stays byte-identical).
    """
    if not raw.startswith(_STATE_DB_CONTENT_PREFIX):
        return None
    body = raw[len(_STATE_DB_CONTENT_PREFIX):]
    try:
        parsed = json.loads(body)
    except (ValueError, TypeError):
        return None
    if not isinstance(parsed, list):
        return None
    projected, removed = _project_parts(parsed, digest)
    if not removed:
        return None
    encoded = json.dumps(projected, ensure_ascii=False, separators=(",", ":"))
    return _STATE_DB_CONTENT_PREFIX + encoded


def _project_api_content(raw, digest):
    """Project the per-message ``api_content`` sidecar, which can duplicate the bytes.

    ``api_content`` is the provider-shaped mirror of ``content`` kept on agent
    rows. Compacting only ``content`` left a full base64 copy of every image in
    the persisted JSON, so the save boundary must project both. Returns the new
    JSON string, or None when the value is not a JSON part list or holds no
    inline bytes (an untouched sidecar must stay byte-identical).
    """
    if not isinstance(raw, str) or not raw:
        return None
    stripped = raw.lstrip()
    if not stripped.startswith("["):
        return None
    try:
        parsed = json.loads(raw)
    except (ValueError, TypeError):
        return None
    if not isinstance(parsed, list):
        return None
    projected, removed = _project_parts(parsed, digest)
    if not removed:
        return None
    projected_json = json.dumps(projected, ensure_ascii=False, separators=(",", ":"))
    # The mirror is reserialized to one canonical layout, so the ORIGINAL string's
    # arrangement (compact vs indented, key order, escaping) is erased too. Feed
    # both the original bytes and the reserialized result into the discriminator:
    # otherwise two rows whose mirrors differ only in layout collapse to one.
    digest.update(b"\x01mirror\x00")
    digest.update(raw.encode("utf-8", "surrogatepass"))
    digest.update(b"\x00")
    digest.update(projected_json.encode("utf-8", "surrogatepass"))
    digest.update(b"\x00")
    return projected_json


def compact_persisted_image_parts(messages, *, stamp_digest: bool = True) -> int:
    """Replace inline image bytes in a persisted projection with markers.

    Callers pass a projection they own (a save-time snapshot or a deep copy),
    never the live provider payload.

    Only the display transcript is projected. ``context_messages`` is the
    provider replay prefix and is preserved exactly; bounding it requires
    load-time rehydration (see the update note above).

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
        digest = hashlib.sha256()
        message_changed = False
        content = message.get("content")
        if isinstance(content, list):
            projected, removed = _project_parts(content, digest)
            if removed:
                message["content"] = projected
                changed += len(removed)
                message_changed = True
        elif isinstance(content, str):
            # Agent-written rows store structured content in a sentinel form:
            # the literal prefix ``\x00json:`` followed by the JSON part list.
            # ``_state_db_row_to_sidecar`` passes that string through unchanged,
            # so the recovery and discoverability writers persisted the full
            # image bytes even though this projector was wired into them -- the
            # list branch above never matched. Compact the decoded list and
            # re-encode in the SAME storage form, leaving the text intact.
            reencoded = _project_sentinelled_content(content, digest)
            if reencoded is not None:
                message["content"] = reencoded
                changed += 1
                message_changed = True
            else:
                # A PLAIN string carrying a raw data URL. `_has_inline_image`
                # reports these as holding bytes, but only the sentinel form was
                # handled, so they fell through uncompacted. Not present in the
                # live store today (0 across 108 files) -- closed because it is
                # the same unwired-lane class as the other findings, and because
                # a lane the preflight flags but the projector ignores is exactly
                # the inconsistency that produced them. Only the base64 RUN is
                # replaced; surrounding prose is kept verbatim.
                masked = _mask_inline_string(content)
                if masked is not None:
                    _update_part_digest(digest, {"type": "text", "text": _MASK}, content, ())
                    message["content"] = masked
                    changed += 1
                    message_changed = True
        # The provider-shaped mirror duplicates the same bytes; leaving it alone
        # would persist a full base64 copy per image row.
        projected_api_content = _project_api_content(message.get("api_content"), digest)
        if projected_api_content is not None:
            message["api_content"] = projected_api_content
            changed += 1
            message_changed = True
        # `_partial_tool_calls` is the interrupted-turn capture lane. It is
        # stripped before provider replay, but it IS persisted, and a captured
        # tool call can carry image parts inside its `args`.
        partials = message.get("_partial_tool_calls")
        if isinstance(partials, list):
            projected_partials, partials_changed = _project_nested(
                partials, digest, (("k", "_partial_tool_calls"),)
            )
            if partials_changed:
                message["_partial_tool_calls"] = projected_partials
                changed += 1
                message_changed = True
        if message_changed and stamp_digest:
            # Content-equality discriminator that survives the projection. The
            # marker alone is identical for every image, so without this the
            # next save's lossless dedup would see two originally distinct rows
            # as exact replays and drop one.
            message[_DIGEST_FIELD] = digest.hexdigest()[:_DIGEST_CHARS]
    return changed


def compact_persisted_anchor_scenes(records):
    """Bound inline image bytes inside ``anchor_activity_scenes`` snippets.

    ``Session.save()`` serializes this field verbatim, and a vision tool records
    its preview inside ``scene.activity_rows[].tool.snippet`` (and the mirrored
    ``.payload.snippet``). Those lanes are not ``messages`` or
    ``context_messages``, so :func:`compact_persisted_image_parts` never visited
    them: after migration the live store still carried ~122 KB of >=256-char
    base64 in this one field across four sessions.

    Returns ``(records, changed)``. Copy-on-write: the caller's live object is
    never mutated, because the render path and the #5854 scene-key fingerprint
    both read the original bytes. Identity fields (``message_ref``,
    ``message_index``, ``stream_id``, ``updated_at``, ``version``) and the key
    map are preserved verbatim so ref/index matching cannot drift.
    """
    if not isinstance(records, dict) or not records:
        return records, False
    digest = hashlib.sha256()
    projected, changed = _project_nested(records, digest, (("k", "anchor_activity_scenes"),))
    if not changed:
        return records, False
    return projected, True


def _has_inline_image(value) -> bool:
    """Cheap early-out scan: does this payload hold any inline image bytes?

    Must recognise every representation the projector downstream can compact,
    including the STRING ``api_content`` mirror: a message whose display content
    is plain text can still carry an inline image only in that mirror, and
    missing it here would skip projection entirely.
    """
    if isinstance(value, list):
        return any(_has_inline_image(item) for item in value)
    if isinstance(value, dict):
        if _inline_image_payload(value) is not None:
            return True
        return any(_has_inline_image(child) for child in value.values())
    if isinstance(value, str):
        # A string is either an ``api_content`` mirror (JSON part list) or a bare
        # payload. Parse first so the SAME shape recognition the projector uses
        # decides: a literal substring scan misses Anthropic base64 mirrors
        # (no ``data:image/`` at all), case variants (``DATA:IMAGE/``) and
        # JSON-escaped URLs (``data:image\/jpeg``).
        stripped = value.lstrip()
        # Agent-written rows prefix the part list with a sentinel, so the first
        # character is the NUL of "\x00json:", not "[". Without this the
        # preflight fell through to the substring scan and returned False for an
        # Anthropic base64 mirror (which carries no "data:image/" text), so the
        # projector was never invoked and the bytes stayed on disk.
        if stripped.startswith(_STATE_DB_CONTENT_PREFIX):
            stripped = stripped[len(_STATE_DB_CONTENT_PREFIX):]
        if stripped[:1] in ("[", "{"):
            try:
                parsed = json.loads(stripped)
            except (ValueError, TypeError):
                parsed = None
            if parsed is not None and parsed is not value:
                if _has_inline_image(parsed):
                    return True
                # A well-formed mirror with no inline part is authoritative.
                return False
        return "data:image/" in value.lower()
    return False


def project_message_payloads(value, *, stamp_digest=None, keys=None):
    """Return a bounded copy of a JSON-shaped payload, or the SAME object if clean.

    Every embedded ``messages`` / ``context_messages`` array is projected through
    :func:`compact_persisted_image_parts`. Copy-on-write: a payload with no inline
    image bytes is returned unchanged, so callers on hot paths pay a scan, not a
    deep copy. Used by the other durable writers (recovery materialization, the
    workspace-binding repair, the shrink backup, the offline migration) that
    bypass ``Session.save()``.

    ``stamp_digest`` defaults to "auto": the display transcript gets the
    discriminator (it is deduplicated), provider-replay arrays never do (they are
    replayed to strict provider schemas and must not gain unknown keys).
    """
    message_keys = _DISPLAY_MESSAGE_KEYS if keys is None else frozenset(keys)
    if isinstance(value, dict):
        out = None
        for key, child in value.items():
            if key in message_keys and isinstance(child, list):
                if not _has_inline_image(child):
                    continue
                projected = copy.deepcopy(child)
                stamp = (
                    (key in _DISPLAY_MESSAGE_KEYS) if stamp_digest is None else bool(stamp_digest)
                )
                if compact_persisted_image_parts(projected, stamp_digest=stamp):
                    if out is None:
                        out = dict(value)
                    out[key] = projected
                continue
            if key == "anchor_activity_scenes" and isinstance(child, dict):
                # A vision tool records its preview inside this field's
                # `scene.activity_rows[].tool.snippet`, which no message lane
                # covers. Every writer that funnels through here -- recovery,
                # discoverability, workspace binding, shrink backup, migration --
                # therefore left those bytes on disk.
                if not _has_inline_image(child):
                    continue
                projected_scenes, changed = compact_persisted_anchor_scenes(child)
                if changed:
                    if out is None:
                        out = dict(value)
                    out[key] = projected_scenes
                continue
            projected_child = project_message_payloads(child, stamp_digest=stamp_digest, keys=message_keys)
            if projected_child is not child:
                if out is None:
                    out = dict(value)
                out[key] = projected_child
        return value if out is None else out
    if isinstance(value, list):
        out = None
        for index, item in enumerate(value):
            projected_item = project_message_payloads(item, stamp_digest=stamp_digest, keys=message_keys)
            if projected_item is not item:
                if out is None:
                    out = list(value)
                out[index] = projected_item
        return value if out is None else out
    return value
