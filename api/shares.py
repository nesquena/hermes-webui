"""
Hermes Web UI -- public read-only share snapshots.

Stores a sanitized, immutable snapshot of a conversation under STATE_DIR/shares.
The snapshot is intentionally narrower than a full session export so public
links do not leak local workspace paths, profile details, or raw tool payloads.
"""

from __future__ import annotations

import base64
import binascii
import html
import io
import json
import logging
import mimetypes
import os
import posixpath
import re
import secrets
import tempfile
import threading
import time
import xml.etree.ElementTree as ET
from pathlib import Path
from urllib.parse import unquote, urlsplit

from api.config import STATE_DIR
from api.helpers import redact_session_data, split_media_token_ref
# _redact_fn_cached is the ALWAYS-ON credential redactor (agent redactor with
# force=True + local fallback regex). Unlike redact_session_data it does NOT
# consult the user-toggleable api_redact_enabled setting — a public share is a
# hard safety boundary that must redact credentials even if the operator turned
# API-response redaction off.
from api.helpers import _redact_fn_cached as _force_redact_credentials

logger = logging.getLogger(__name__)

SHARES_DIR = STATE_DIR / "shares"
_SHARE_LOCK = threading.Lock()


def _ensure_share_dir() -> None:
    SHARES_DIR.mkdir(parents=True, exist_ok=True)


def _share_path(token: str) -> Path:
    token = str(token or "").strip()
    if not token:
        raise ValueError("share token is required")
    if not token.replace("-", "").replace("_", "").isalnum():
        raise ValueError("invalid share token")
    return SHARES_DIR / f"{token}.json"


def _write_json_atomic(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        dir=str(path.parent),
        prefix=f"{path.stem}.",
        suffix=".tmp",
        text=True,
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_name, path)
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def _share_message_text(message: dict) -> str:
    content = message.get("content") if isinstance(message, dict) else ""
    if isinstance(content, list):
        parts = []
        for item in content:
            if not isinstance(item, dict):
                # Non-dict list items (e.g. nested structures) are NOT plain text —
                # never stringify them into the public snapshot.
                continue
            if item.get("type") == "text":
                # Only append genuine string text — a dict-valued "text" (possible
                # via /api/session/import) must NOT be str()'d into the public
                # snapshot (that would publish structured/tool payload verbatim).
                _t = item.get("text")
                if isinstance(_t, str):
                    parts.append(_t)
        return "".join(parts).strip()
    if isinstance(content, str):
        return content.strip()
    # A dict/other structured content (e.g. a tool-result object) is NOT shareable
    # text — do NOT str() it (that would publish raw structured/tool payload).
    return ""


def _redact_share_paths(text: str, extra_paths) -> str:
    """Strip known local session/workspace/home paths out of public-share text.

    A workspace path or Hermes home can be embedded inside message prose (an
    agent quoting a file path, a traceback, etc.). Redact the concrete local
    paths so a public share never discloses the operator's filesystem layout.
    """
    if not isinstance(text, str) or not text:
        return text
    for p in extra_paths:
        if not p:
            continue
        p = str(p).strip()
        if len(p) >= 4 and p in text:
            text = text.replace(p, "[redacted-path]")
    return text


# Regex matching local MEDIA:<path> references — same pattern that
# _inlineMediaHtmlForRef() in ui.js handles when rendering messages.
# Excludes MEDIA: followed by http/https URLs so external images pass
# through unchanged.  file:// references are NOT matched here — they are
# always rejected at the public-share boundary (absolute, un-scoped).
# `data:` URIs DO match: _replace_ref() routes them to _embed_share_data_uri(),
# which applies the same public-share policy as a local file (raster MIME
# allow-list, byte cap, magic-byte check) and never touches the filesystem.
# Feeding a multi-KB base64 blob to Path(...).resolve()/stat() raised OSError
# ENAMETOOLONG (errno 36) and 500'd share creation (#7949); passing every data:
# URI through unchanged would instead republish arbitrary bytes (text/plain,
# octet-stream, SVG) that credential redaction cannot see.
_SHARE_MEDIA_RE = re.compile(
    r"MEDIA:(?!https?://)([^\s\)\]>]+)"
)

# Public-share hardening for renderer-active references that bypass
# _SHARE_MEDIA_RE because they already look like HTTP(S), plus bare file://
# references that renderMd() routes through the authenticated /api/media path.
# The public snapshot is the trust boundary: it must be safe without knowing
# reverse-proxy origin configuration.
_SHARE_ANY_MEDIA_RE = re.compile(r"MEDIA:([^\s\)\]]+)")
_SHARE_WRAPPED_MEDIA_RE = re.compile(r"`MEDIA:([^`\s]+)`")
# Titles classify the complete wrapper before the bare-token alternative.
_SHARE_TITLE_MEDIA_RE = re.compile(
    _SHARE_WRAPPED_MEDIA_RE.pattern + "|" + _SHARE_ANY_MEDIA_RE.pattern
)
_SHARE_FILE_MARKDOWN_RE = re.compile(
    r"!?\[[^\]\r\n]*\]\(\s*file://[^)\s]+\s*\)",
    re.IGNORECASE,
)
_SHARE_FILE_CODE_RE = re.compile(r"`file://[^`\r\n]+`", re.IGNORECASE)
_SHARE_FILE_URI_RE = re.compile(
    r"(?<![a-z0-9+.-])file://[^`\s<>\"')\]]*", re.IGNORECASE,
)
# Only rejected/shadowed raw data-image attributes need a boundary-free scrub:
# payload bytes can run directly into file://, unlike a standalone URI scheme.
_SHARE_DATA_FILE_URI_RE = re.compile(r"file://[^`\s<>\"')\]]*", re.IGNORECASE)
# Attribute shapes accepted by renderMd's raw-tag sanitizer. Only complete
# data-image src values are protected; other attributes and neighbors are scrubbed.
_SHARE_RAW_IMG_RE = re.compile(r"<img(?=[\s/>])[^>]*>", re.IGNORECASE)
_SHARE_RAW_ATTR_RE = re.compile(
    r'''([a-zA-Z0-9:_-]+)(?:\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s"'>`]+)))?'''
)
_SHARE_IMAGE_LABEL_ENTITY_RE = re.compile(
    r"&(?:#[0-9]{1,8}|#[xX][0-9a-fA-F]{1,6}|[a-zA-Z][a-zA-Z0-9]{0,31});|[<>&]"
)
# Unknown malformed destinations must not consume a later image marker. A
# renderer-supported outer scheme still consumes its full reference so a private
# outer URL cannot evade classification by nesting a public image inside it.
# The renderer's outer Markdown scheme gate is case-sensitive.
_SHARE_MARKDOWN_IMAGE_DESTINATION_GUARD = (
    r"(?:(?=(?-i:https?://|file://|data:image/))|(?![^)\r\n]*!\[))"
)
_SHARE_MARKDOWN_IMAGE_RE = re.compile(
    r"!\[[^\]\r\n]*\]\(\s*(?:"
    rf"<({_SHARE_MARKDOWN_IMAGE_DESTINATION_GUARD}[^>\r\n]+)>|"
    rf"({_SHARE_MARKDOWN_IMAGE_DESTINATION_GUARD}[^)\r\n]+))\s*\)",
    re.IGNORECASE,
)
_SHARE_HTTP_SCHEME_RE = re.compile(r"https?://", re.IGNORECASE)
_SHARE_MEDIA_SAFETY_MAX_CHARS = 16 * 1024
_SHARE_MEDIA_SAFETY_DECODE_ROUNDS = 4
# Match self-contained renderer image forms and its 2 MiB URI budget.
# A complete self-contained payload cannot route to authenticated local media.
# Keep SVG base64 strict; escaped raster base64 must validate after one decode.
_SHARE_BASE64_IMAGE_RE = re.compile(
    r"data:image/(?:png|jpe?g|gif|webp|avif|svg\+xml);base64,[a-z0-9+/=]+",
    re.IGNORECASE,
)
_SHARE_RASTER_DATA_IMAGE_RE = re.compile(
    r"data:image/(?:png|jpe?g|gif|webp|avif),[a-z0-9+/=%._~:@!$&'()*+,;-]*",
    re.IGNORECASE,
)
# Recognize the raster header even if the escaped payload is malformed.
# Validation below must reject it instead of falling through to URL handling.
_SHARE_ESCAPED_BASE64_RASTER_RE = re.compile(
    r"data:image/(?:png|jpe?g|gif|webp|avif);base64,(.*)",
    re.IGNORECASE | re.DOTALL,
)
_SHARE_DATA_IMAGE_MAX_CHARS = 2 * 1024 * 1024


def _bounded_decode_share_media_ref(raw: str) -> str | None:
    """Decode one public MEDIA reference with a small fail-closed budget."""
    if not isinstance(raw, str) or len(raw) > _SHARE_MEDIA_SAFETY_MAX_CHARS:
        return None
    value = html.unescape(raw)
    total = len(value)
    for _ in range(_SHARE_MEDIA_SAFETY_DECODE_ROUNDS):
        decoded = html.unescape(unquote(value))
        total += len(decoded)
        if total > _SHARE_MEDIA_SAFETY_MAX_CHARS * (_SHARE_MEDIA_SAFETY_DECODE_ROUNDS + 1):
            return None
        if decoded == value:
            return value
        value = decoded
    # More decoding would still change the value: classification is uncertain,
    # so the public boundary rejects it instead of publishing a partial view.
    return value if html.unescape(unquote(value)) == value else None


def _share_query_has_path_param(query: str) -> bool:
    """Return True only for a real path= query field, never a fragment."""
    for field in str(query or "").split("&"):
        key, sep, _value = field.partition("=")
        if sep and key.strip().lower() == "path":
            return True
    return False


def _canonical_share_url_path(path: str) -> str:
    """Apply browser-style slash/dot-segment normalization to a URL path."""
    value = str(path or "").replace("\\", "/")
    value = re.sub(r"/+", "/", value)
    if not value.startswith("/"):
        value = "/" + value
    normalized = posixpath.normpath(value)
    if not normalized.startswith("/"):
        normalized = "/" + normalized
    if normalized != "/":
        normalized = normalized.rstrip("/")
    return normalized.lower()


def _share_url_candidate_is_private(candidate: str) -> bool:
    """Classify one decoded URL/path candidate against the private media route."""
    try:
        text = str(candidate or "").strip().replace("\\", "/")
        # WHATWG folds excess authority slashes (including backslashes) before
        # parsing the host; urlsplit alone leaves that host in the path.
        text = re.sub(r"^(https?:)/{2,}", r"\1//", text, flags=re.IGNORECASE)
        parsed = urlsplit(text)
    except ValueError:
        # An unparseable renderer-active candidate cannot be proven public.
        return True
    return (
        _canonical_share_url_path(parsed.path) == "/api/media"
        and _share_query_has_path_param(parsed.query)
    )


def _iter_share_url_candidates(value: str):
    """Yield the whole value plus every nested HTTP(S) URL start.

    Bounded decoding happens before this step, so a percent-encoded nested URL
    becomes visible here. Starting a candidate at every scheme occurrence lets
    us classify an inner private URL without mistaking an outer CDN path that
    merely contains the text "/api/media".
    """
    text = str(value or "").strip()
    if text:
        yield text
    for match in _SHARE_HTTP_SCHEME_RE.finditer(text):
        start = match.start()
        if start == 0:
            continue
        tail = text[start:]
        candidate = re.split(r"[\s<>\"'\x60\]\)]", tail, maxsplit=1)[0]
        if candidate:
            yield candidate


def _share_media_ref_is_self_contained_image(raw: str) -> bool:
    """Recognize a complete supported image URI within the renderer's budget."""
    if not isinstance(raw, str) or len(raw) > _SHARE_DATA_IMAGE_MAX_CHARS:
        return False
    if _SHARE_BASE64_IMAGE_RE.fullmatch(raw) or _SHARE_RASTER_DATA_IMAGE_RE.fullmatch(raw):
        return True
    match = _SHARE_ESCAPED_BASE64_RASTER_RE.fullmatch(raw)
    if not match or "%" not in match.group(1):
        return False
    try:
        # unquote preserves literal +; a second decode is never permitted.
        payload = re.sub(r"[ \t\n\f\r]", "", unquote(match.group(1)))
        # Match the browser's forgiving-base64 after exactly one URI decode.
        if len(payload) % 4 == 0:
            if payload.endswith("=="):
                payload = payload[:-2]
            elif payload.endswith("="):
                payload = payload[:-1]
        if len(payload) % 4 == 1 or not re.fullmatch(r"[A-Za-z0-9+/]*", payload):
            return False
        base64.b64decode(payload + "=" * (-len(payload) % 4), validate=True)
    except ValueError:
        return False
    return True


def _share_media_ref_is_private(raw: str) -> bool:
    """Return True when a renderer-active ref can route to private local media."""
    if _share_media_ref_is_self_contained_image(raw):
        return False
    # Malformed escaped raster payloads fail closed even below the URL budget.
    if "%" in raw and _SHARE_ESCAPED_BASE64_RASTER_RE.fullmatch(raw):
        return True
    decoded = _bounded_decode_share_media_ref(raw)
    if decoded is None:
        return True
    normalized = decoded.strip().replace("\\", "/")
    if re.search(r"(?<![a-z0-9+.-])file:", normalized, re.IGNORECASE):
        return True
    return any(
        _share_url_candidate_is_private(candidate)
        for candidate in _iter_share_url_candidates(normalized)
    )


class _BoundedShareMarkdownPattern:
    """Preserve existing matches while avoiding repeated malformed-tail scans.

    The sanitizer uses only finditer/sub. Structural bounds retain the original
    Match objects and groups, including nested alt brackets and angle references.
    """

    def __init__(self, pattern, *, image=False):
        self.original = pattern
        self.opener = re.compile(r"!\[" if image else r"!?\[")
        self.angle = image
        guard = _SHARE_MARKDOWN_IMAGE_DESTINATION_GUARD
        self.guard = re.compile(guard, re.I) if guard in pattern.pattern else None

    def __getattr__(self, name):
        return getattr(self.original, name)

    def finditer(self, text):
        cursor = 0
        cached = {}
        spaces = {}
        size = len(text)

        def next_at(token, start, purpose=""):
            key = (token, purpose)
            position = cached.get(key, -1)
            if position < start:
                position = text.find(token, start)
                if position < 0:
                    position = size
                cached[key] = position
            return position

        def skip_space(start, purpose):
            first, last = spaces.get(purpose, (-1, -1))
            if first <= start <= last:
                return last
            end = start
            while end < size and text[end].isspace():
                end += 1
            spaces[purpose] = (start, end)
            return end

        def guard_ok(start, purpose):
            if self.guard is None:
                return True
            boundary = min(
                next_at(")", start, purpose),
                next_at("\r", start, purpose),
                next_at("\n", start, purpose),
            )
            nested = next_at("![", start, purpose)
            end = min(boundary, nested + 2) if nested < boundary else boundary
            return self.guard.match(text, start, end) is not None

        while cursor < size:
            opened = self.opener.search(text, cursor)
            if not opened:
                return
            # One failed destination invalidates every opener sharing this alt close.
            close = next_at("]", opened.end(), "label")
            line = min(
                next_at("\r", opened.end(), "label"),
                next_at("\n", opened.end(), "label"),
            )
            if line < close:
                cursor = line + 1
                continue
            if close == size:
                return
            if text[close + 1 : close + 2] != "(":
                cursor = close + 1
                continue
            # No later closing paren proves every remaining match impossible.
            if next_at(")", close + 2, "global") == size:
                return
            raw_start = close + 2
            dest = skip_space(raw_start, "leading")
            paren = next_at(")", dest, "bare")
            newline = min(next_at("\r", dest, "bare"), next_at("\n", dest, "bare"))
            boundary = min(paren, newline)
            tail = (
                skip_space(boundary, "bare-tail") if boundary == newline else boundary
            )
            bare_end = (
                tail + 1
                if tail < size
                and text[tail] == ")"
                and (boundary > dest or dest > raw_start)
                and guard_ok(dest, "bare-guard")
                else None
            )
            angle_end = None
            if self.angle and text[dest : dest + 1] == "<":
                angle_close = next_at(">", dest + 1, "angle")
                angle_line = min(
                    next_at("\r", dest + 1, "angle"), next_at("\n", dest + 1, "angle")
                )
                if dest + 1 < angle_close < angle_line:
                    angle_tail = skip_space(angle_close + 1, "angle-tail")
                    if (
                        angle_tail < size
                        and text[angle_tail] == ")"
                        and guard_ok(dest + 1, "angle-guard")
                    ):
                        angle_end = angle_tail + 1
            # Bound the regex to a viable terminator, rather than every later tail.
            end = angle_end if angle_end is not None else bare_end
            found = (
                self.original.match(text, opened.start(), end)
                if end is not None
                else None
            )
            if found:
                yield found
                cursor = found.end()
            else:
                cursor = close + 1

    def sub(self, replacement, text, count=0):
        out = []
        cursor = 0
        number = 0
        for match in self.finditer(text):
            out.append(text[cursor : match.start()])
            out.append(
                replacement(match)
                if callable(replacement)
                else match.expand(replacement)
            )
            cursor = match.end()
            number += 1
            if count and number >= count:
                break
        out.append(text[cursor:])
        return "".join(out)

_SHARE_MARKDOWN_IMAGE_RE = _BoundedShareMarkdownPattern(_SHARE_MARKDOWN_IMAGE_RE, image=True)
_SHARE_FILE_MARKDOWN_RE = _BoundedShareMarkdownPattern(_SHARE_FILE_MARKDOWN_RE)

def _omit_private_share_media_references(text: str, *, plain_text: bool = False) -> str:
    """Remove renderer-active private media references from a public snapshot.

    This intentionally does not infer the WebUI's public origin. Any MEDIA URL
    that decodes to the authenticated /api/media?path= shape is private,
    regardless of host. Ordinary public HTTP(S) media remain unchanged.
    """
    if not isinstance(text, str) or not text:
        return text

    # Bodies mirror renderMd()'s wrapped-token activation. Plain-text titles
    # keep public wrappers intact and omit a private wrapper as one unit.
    if not plain_text:
        text = _SHARE_WRAPPED_MEDIA_RE.sub(lambda m: f"MEDIA:{m.group(1)}", text)

    def _replace_media(match: re.Match) -> str:
        # The title alternation puts bare references in group 2. Give the
        # shared splitter the original bare match so quoted prose stays outside
        # classification, just as in the local-image embedding path.
        token_match = (
            _SHARE_ANY_MEDIA_RE.match(text, match.start())
            if plain_text and match.group(1) is None else match
        )
        parts = split_media_token_ref(text, token_match)
        if not parts:
            return match.group(0)
        raw, suffix = parts
        if _share_media_ref_is_private(raw):
            return _PLACEHOLDER + suffix
        if plain_text:
            if re.match(r"https?://", raw, re.IGNORECASE):
                return match.group(0)
            if _share_media_ref_is_self_contained_image(raw):
                return match.group(0)
            # Titles have no file-reading context: reuse the no-root embedding
            # decision for local paths, before any wrapper can be consumed.
            token = f"MEDIA:{raw}"
            if _embed_share_media(token, allowed_roots=()) != token:
                return _PLACEHOLDER + suffix
        return match.group(0)

    media_re = _SHARE_TITLE_MEDIA_RE if plain_text else _SHARE_ANY_MEDIA_RE
    text = media_re.sub(_replace_media, text)

    def _escape_label(label: str) -> str:
        if plain_text:
            return label

        def _label_entity(entity):
            return "".join(f"&#{ord(char)};" for char in html.unescape(entity.group(0)))

        return _SHARE_IMAGE_LABEL_ENTITY_RE.sub(_label_entity, label)

    # Markdown images are renderer-active even without the MEDIA: prefix.
    # Run their URL through the same classifier so direct private media links
    # cannot survive into the anonymous share page.
    def _replace_markdown_image(match: re.Match) -> str:
        group = 1 if match.group(1) is not None else 2
        raw = str(match.group(group) or "")
        if not _share_media_ref_is_private(raw):
            return match.group(0)
        # Labels can contain the opener of a code span which ends outside the
        # image. Removing the whole match would activate previously inert HTML.
        start, end = match.span(group)
        destination = re.sub(r"[^`]+", _PLACEHOLDER, raw)
        # The omitted destination no longer enters _mdImageHtml's alt escaping.
        # Keep label text/code delimiters, but do not expose its inert HTML to
        # the renderer's raw-tag pass when the image syntax becomes plain text.
        # renderMd decodes named entities repeatedly (also in blockquotes).
        # Numeric references remain inert until DOM text parsing, so they cannot
        # become tags even when the renderer recurses through nested quotes.
        label_start = match.start() + 2
        label_end = text.index("](", label_start, start)
        # Numeric references cannot materialize tags or Markdown delimiters
        # during renderer entity decoding, even inside nested blockquotes.
        label = _escape_label(text[label_start:label_end])
        prefix = (
            text[match.start():label_start]
            + label
            + text[label_end:start]
        )
        return prefix + destination + text[end:match.end()]

    text = _SHARE_MARKDOWN_IMAGE_RE.sub(_replace_markdown_image, text)

    def _replace_file_markdown(match: re.Match) -> str:
        source = match.group(0)
        label_start = 2 if source.startswith("![") else 1
        label_end = source.index("](", label_start)
        destination_start = label_end + 2
        while source[destination_start].isspace():
            destination_start += 1
        destination_end = len(source) - 1
        while source[destination_end - 1].isspace():
            destination_end -= 1
        destination = re.sub(r"[^`]+", _PLACEHOLDER,
                             source[destination_start:destination_end])
        return (source[:label_start] + _escape_label(source[label_start:label_end])
                + source[label_end:destination_start] + destination
                + source[destination_end:])

    # Rewrite the complete link before splitting protected data-image spans;
    # otherwise an image-looking label can be exposed to the raw HTML pass.
    text = _SHARE_FILE_MARKDOWN_RE.sub(_replace_file_markdown, text)

    def _scrub_rejected_data_attributes(tag: re.Match) -> str:
        source = tag.group(0)
        attrs = list(_SHARE_RAW_ATTR_RE.finditer(source[4:-1]))
        active_src = next((attr for attr in reversed(attrs)
                           if attr.group(1).lower() == "src"), None)
        replacements = []
        for attr in attrs:
            group = next((i for i in (2, 3, 4) if attr.group(i) is not None), None)
            if group is None:
                continue
            value = attr.group(group)
            decoded = html.unescape(value)
            if (re.match(r"data:image/", decoded, re.IGNORECASE)
                    and (attr is not active_src
                         or not _share_media_ref_is_self_contained_image(decoded))):
                start, end = attr.span(group)
                replacements.append((4 + start, 4 + end,
                                     _SHARE_DATA_FILE_URI_RE.sub(_PLACEHOLDER, value)))
        for start, end, replacement in reversed(replacements):
            source = source[:start] + replacement + source[end:]
        return source

    text = _SHARE_RAW_IMG_RE.sub(_scrub_rejected_data_attributes, text)

    # Public JSON must not expose filesystem URIs even when markdown would have
    # treated the literal as inert code. Preserve syntax delimiters: they may
    # keep adjacent HTML inert. Only filesystem destinations are replaced.
    for pattern, replacement in (
        (_SHARE_FILE_CODE_RE, lambda match: "`" + _PLACEHOLDER + "`"),
        (_SHARE_FILE_URI_RE, _PLACEHOLDER),
    ):
        # Recompute after every scrub: replacing a private neighbor shifts the
        # image offsets. URI metadata is inert within a complete accepted image.
        protected = []
        for tag in _SHARE_RAW_IMG_RE.finditer(text):
            attrs = {}
            for attr in _SHARE_RAW_ATTR_RE.finditer(tag.group(0)[4:-1]):
                attrs[attr.group(1).lower()] = attr
            src = attrs.get("src")
            if src is not None:
                group = next((i for i in (2, 3, 4) if src.group(i) is not None), None)
                if group is not None and _share_media_ref_is_self_contained_image(
                    html.unescape(src.group(group))
                ):
                    start, end = src.span(group)
                    protected.append((tag.start() + 4 + start, tag.start() + 4 + end))
        for image in _SHARE_MARKDOWN_IMAGE_RE.finditer(text):
            group = 1 if image.group(1) is not None else 2
            if _share_media_ref_is_self_contained_image(image.group(group)):
                protected.append(image.span(group))
        for media in media_re.finditer(text):
            token = (
                _SHARE_ANY_MEDIA_RE.match(text, media.start())
                if plain_text and media.group(1) is None else media
            )
            parts = split_media_token_ref(text, token)
            if parts and _share_media_ref_is_self_contained_image(parts[0]):
                start = token.start(1)
                protected.append((start, start + len(parts[0])))
        parts = []
        cursor = 0
        for start, end in sorted(protected):
            if end <= cursor:
                continue
            # Scrub gaps rather than whole matches: an internal file:// match
            # can cross a closing backtick into an outside private neighbor.
            start = max(start, cursor)
            parts.append(pattern.sub(replacement, text[cursor:start]))
            parts.append(text[start:end])
            cursor = end
        parts.append(pattern.sub(replacement, text[cursor:]))
        text = "".join(parts)
    return text

# Strict shape for an inline data URI: data:<type/subtype>[;param...];base64,<payload>.
# Parameters (e.g. charset, name) are tolerated before the mandatory ;base64.
_DATA_URI_RE = re.compile(
    r"^data:(?P<mime>[a-z0-9.+-]+/[a-z0-9.+-]+)(?P<params>(?:;[^;,]*)*?);base64,(?P<payload>[A-Za-z0-9+/]*={0,2})$",
    re.IGNORECASE,
)
# Normalise common aliases to the canonical names used by _SHARE_ALLOWED_MIME_TYPES.
_DATA_URI_MIME_ALIASES = {"image/jpg": "image/jpeg", "image/pjpeg": "image/jpeg"}
# Upper bound on the ENCODED payload, checked before decoding so an oversized
# blob is refused without allocating its decoded bytes.
_SHARE_EMBED_MAX_B64_CHARS = ((512 * 1024 + 2) // 3) * 4

# Max size (in bytes) for files we'll embed as base64 in a share snapshot.
_SHARE_EMBED_MAX_BYTES = 512 * 1024  # 512 KiB

# Only these image MIME types may be embedded in public shares.
# Non-image files and SVG are NEVER embedded — embedding arbitrary file
# bytes circumvents the credential-redaction boundary that protects
# message prose, and a public share is not a file-transfer service.
# SVG is excluded because it is the only text-bearing type in this set;
# agent-authored SVGs can carry credentials in their text content which
# _redact_share_paths (which only touches message prose, not embedded
# bytes) cannot reach.
_SHARE_ALLOWED_MIME_TYPES: frozenset[str] = frozenset({
    "image/png",
    "image/jpeg",
    "image/gif",
    "image/webp",
})

# SVG namespace URI used during sanitisation.
_SVG_NS = "http://www.w3.org/2000/svg"

# Pattern matching on* event-handler attributes.
_ON_ATTR_RE = re.compile(r"^on\w+$", re.IGNORECASE)

# Dangerous href/xlink:href schemes.
_DANGEROUS_HREF_RE = re.compile(r"^\s*javascript\s*:", re.IGNORECASE)

# Static placeholder emitted when a media reference cannot be embedded.
_PLACEHOLDER = "[*Local attachment omitted from public share*]"

# Magic byte signatures for allowed image formats — content-based validation
# that catches mismatched extensions (e.g. a .png that is actually a script).
# SVG is excluded here because it is validated by XML parsing in
# _sanitize_svg_bytes.
_IMAGE_MAGIC: dict[str, bytes] = {
    "image/png": b"\x89PNG\r\n\x1a\n",
    "image/jpeg": b"\xff\xd8\xff",
    "image/gif": b"GIF8",
    "image/webp": b"RIFF",
}
# Offset for WebP magic: "RIFF" at 0, file size at 4, "WEBP" at 8.
_WEBP_MAGIC_OFFSET = 8
_WEBP_MAGIC = b"WEBP"


def _check_image_magic(data: bytes, mime_type: str) -> bool:
    """Verify *data* header bytes match the expected magic for *mime_type*.

    Returns ``True`` if the content is consistent with the claimed type.
    SVG is exempt because it is validated structurally by
    :func:`_sanitize_svg_bytes`.
    """
    if mime_type == "image/svg+xml":
        return True
    magic = _IMAGE_MAGIC.get(mime_type)
    if magic is None:
        return False
    if not data.startswith(magic):
        return False
    # Extra check for WebP: "WEBP" at offset 8.
    if mime_type == "image/webp":
        if len(data) < 12 or data[_WEBP_MAGIC_OFFSET:_WEBP_MAGIC_OFFSET + 4] != _WEBP_MAGIC:
            return False
    return True


def _sanitize_svg_bytes(data: bytes) -> bytes:
    """Strip script elements, on* handlers, and javascript: hrefs from SVG.

    SVG images served via ``<img src="data:image/svg+xml;base64,…">`` are
    sandboxed by modern browsers and script execution is blocked.  However,
    a sufficiently determined adversary with an older or exotic client may
    still extract credentials embedded in the SVG, so we strip the unsafe
    content at the server before it ever reaches a share page.

    Returns sanitised SVG bytes on success, or the original *data* unchanged
    if the content cannot be parsed as XML (fail-closed).
    """
    try:
        ET.register_namespace("", _SVG_NS)
        root = ET.fromstring(data.decode("utf-8", errors="replace"))
    except ET.ParseError:
        # Not valid XML — cannot sanitise safely.  Return a minimal empty SVG
        # so the <img> renders nothing rather than embedding un-sanitised bytes.
        return b'<svg xmlns="http://www.w3.org/2000/svg"/>'

    # Walk the tree depth-first, stripping on* attrs, dangerous hrefs,
    # and removing <script> children.
    def _walk(elem: ET.Element) -> None:
        for attr_name in list(elem.attrib):
            if _ON_ATTR_RE.match(attr_name):
                del elem.attrib[attr_name]
            elif attr_name in ("href", "xlink:href", "{http://www.w3.org/1999/xlink}href"):
                val = elem.attrib[attr_name]
                if _DANGEROUS_HREF_RE.match(val):
                    del elem.attrib[attr_name]

        for child in list(elem):
            tag = child.tag.split("}", 1)[-1] if "}" in child.tag else child.tag
            if tag == "script":
                elem.remove(child)
            else:
                _walk(child)

    _walk(root)

    buf = io.BytesIO()
    tree = ET.ElementTree(root)
    tree.write(buf, encoding="utf-8", xml_declaration=False)
    return buf.getvalue()


def _embed_share_data_uri(raw: str) -> str:
    """Validate an inline ``data:`` media reference for a public share.

    Applies the same policy as an embedded local file: only the raster types in
    ``_SHARE_ALLOWED_MIME_TYPES``, strictly valid base64, at most
    ``_SHARE_EMBED_MAX_BYTES`` decoded, and magic bytes that match the declared
    type. A valid image is re-emitted as a canonical ``<img>`` built from the
    decoded bytes; anything else (non-image, SVG, malformed, oversized,
    mismatched magic, non-base64) becomes ``_PLACEHOLDER`` so none of its bytes
    reach the public snapshot. Never touches the filesystem (#7949).
    """
    if len(raw) > _SHARE_EMBED_MAX_B64_CHARS + 256:
        return _PLACEHOLDER
    m = _DATA_URI_RE.match(raw)
    if not m:
        return _PLACEHOLDER
    mime_type = m.group("mime").lower()
    mime_type = _DATA_URI_MIME_ALIASES.get(mime_type, mime_type)
    if mime_type not in _SHARE_ALLOWED_MIME_TYPES:
        return _PLACEHOLDER
    payload = m.group("payload")
    if not payload or len(payload) > _SHARE_EMBED_MAX_B64_CHARS:
        return _PLACEHOLDER
    try:
        data = base64.b64decode(payload, validate=True)
    except (binascii.Error, ValueError):
        return _PLACEHOLDER
    if not data or len(data) > _SHARE_EMBED_MAX_BYTES:
        return _PLACEHOLDER
    if not _check_image_magic(data, mime_type):
        return _PLACEHOLDER
    b64 = base64.b64encode(data).decode("ascii")
    return (
        f'<img src="data:{mime_type};base64,{b64}"'
        f' class="msg-media-img" alt="image" loading="lazy">'
    )


def _embed_share_media(text: str, *, allowed_roots: tuple[Path, ...] = ()) -> str:
    """Find local MEDIA: references and replace them with inline <img> tags.

    Only relative paths that resolve inside at least one of *allowed_roots*
    are honoured.  Absolute paths, ``file://`` URIs, paths that traverse
    outside the allowed directories via ``..`` or symlinks, non-image MIME
    types, and files larger than ``_SHARE_EMBED_MAX_BYTES`` are all replaced
    with a static placeholder — no file content leaves the server.

    This runs BEFORE :func:`_redact_share_paths` so the concrete file path
    is still available for the allowed-roots check.
    """
    if not isinstance(text, str) or not text:
        return text

    allowed = tuple(Path(r).resolve() for r in allowed_roots if r)

    def _resolve_against_roots(raw: str) -> Path | None:
        """Resolve *raw* against each allowed root, returning the first valid
        absolute Path that lives inside one of them, or ``None``.

        - ``file://`` is always rejected (absolute, un-scoped).
        - Absolute paths (``/…``, ``~…``) are resolved as-is and checked
          against the allowed-roots allow-list via ``is_relative_to()``.
        - Relative paths are joined with each allowed root in turn so they
          don't silently anchor to the server's process CWD.
        """
        if raw.startswith("file://"):
            return None

        # Defensive length / newline guard (#7949). A real local attachment
        # path is short; an over-long or newline-bearing token (e.g. a
        # data:...;base64,<blob> URI that slipped past the caller) is never a
        # valid file and must never be handed to the filesystem — a stat() on
        # an over-length path raises OSError ENAMETOOLONG (errno 36), and that
        # stat happens inside .is_file() below, OUTSIDE the resolve()
        # try/except, so the error would otherwise escape and 500 the caller.
        # 4096 comfortably exceeds any real PATH_MAX-bounded attachment path.
        if len(raw) > 4096 or "\n" in raw or "\x00" in raw:
            return None

        # --- Absolute paths: resolve as-is, then allow-list check ------------
        if raw.startswith("/") or raw.startswith("~"):
            try:
                p = Path(raw).expanduser().resolve(strict=False)
                if not allowed or not any(p.is_relative_to(r) for r in allowed):
                    return None
                return p if p.is_file() else None
            except (OSError, ValueError, RuntimeError):
                return None

        # --- Relative paths: try each allowed root as the anchor -------------
        for root in allowed:
            try:
                candidate = (root / raw).resolve(strict=False)
                # Path traversal guard: resolved path must still be under root.
                if not candidate.is_relative_to(root):
                    continue
                if candidate.is_file():
                    return candidate
            except (OSError, ValueError, RuntimeError):
                continue
        return None

    def _replace_ref(m: re.Match) -> str:
        parts = split_media_token_ref(text, m)
        if not parts:
            return m.group(0)
        raw, suffix = parts
        raw = raw.strip()
        if not raw:
            return m.group(0)

        # --- Inline data: URI — validate in memory, never resolve as a path ---
        if raw[:5].lower() == "data:":
            return _embed_share_data_uri(raw) + suffix

        # --- Resolve and validate against allowed roots -----------------------
        p = _resolve_against_roots(raw)
        if p is None:
            return _PLACEHOLDER + suffix

        # --- Size guard -------------------------------------------------------
        try:
            size = p.stat().st_size
        except OSError:
            return _PLACEHOLDER + suffix

        if size > _SHARE_EMBED_MAX_BYTES:
            return _PLACEHOLDER + suffix

        # --- MIME allow-list (images only) ------------------------------------
        mime_type, _ = mimetypes.guess_type(str(p))
        if not mime_type or mime_type not in _SHARE_ALLOWED_MIME_TYPES:
            return _PLACEHOLDER + suffix

        # --- Embed as base64 <img> -------------------------------------------
        try:
            data = p.read_bytes()
            # Content-based MIME validation: verify the actual file header
            # matches the claimed MIME type — catches extension-spoofed files
            # (e.g. a script renamed to .png).
            if not _check_image_magic(data, mime_type):
                return _PLACEHOLDER + suffix
            # Sanitise SVG content before embedding — SVG can carry
            # <script> elements and on* event handlers that could leak
            # credentials in the context of a public share page.
            if mime_type == "image/svg+xml":
                data = _sanitize_svg_bytes(data)
            b64 = base64.b64encode(data).decode("ascii")
            # HTML-escape the filename so a crafted name like
            # '"><script>alert(1)</script>' cannot break out of the
            # attribute and inject script into the share page.
            safe_name = html.escape(p.name, quote=True)
            return (
                f'<img src="data:{mime_type};base64,{b64}"'
                f' class="msg-media-img" alt="{safe_name}"'
                f' loading="lazy">'
            ) + suffix
        except (OSError, MemoryError):
            return _PLACEHOLDER + suffix

    return _SHARE_MEDIA_RE.sub(_replace_ref, text)


def _sanitize_message(message: dict, *, redact_paths=(), allowed_roots: tuple[Path, ...] = ()) -> dict | None:
    if not isinstance(message, dict):
        return None
    role = str(message.get("role") or "").strip().lower()
    if role not in {"user", "assistant"}:
        return None
    text = _share_message_text(message)
    if not text:
        return None
    # ALWAYS-ON hardening for the public boundary, independent of any setting:
    # (1) force credential redaction, (2) embed allowed local media,
    # (3) remove residual renderer-active private media references,
    # (4) strip known local paths.
    text = _force_redact_credentials(text)
    # Embed local media BEFORE path redaction so the concrete path is still
    # available for file reads.  MEDIA: references become self-contained data
    # URIs — or a static placeholder if the path is outside the allowed roots.
    text = _embed_share_media(text, allowed_roots=allowed_roots)
    text = _omit_private_share_media_references(text)
    text = _redact_share_paths(text, redact_paths)
    if not text.strip():
        return None
    sanitized = {
        "role": role,
        "content": text,
    }
    ts = message.get("timestamp")
    if isinstance(ts, (int, float)):
        sanitized["timestamp"] = ts
    return sanitized


def _public_share_payload(payload: dict) -> dict:
    messages = payload.get("messages")
    if not isinstance(messages, list):
        messages = []
    public = {
        "title": str(payload.get("title") or "Untitled"),
        "messages": messages,
        "message_count": int(payload.get("message_count") or len(messages)),
    }
    created_at = payload.get("created_at")
    updated_at = payload.get("updated_at")
    if isinstance(created_at, (int, float)):
        public["created_at"] = created_at
    if isinstance(updated_at, (int, float)):
        public["updated_at"] = updated_at
    return public


def build_share_snapshot(session) -> dict:
    raw_dict = getattr(session, "__dict__", {}) or {}
    # redact_session_data respects the api_redact_enabled setting; keep it as a
    # first pass, but the per-message sanitizer below applies ALWAYS-ON credential
    # + path redaction that does NOT depend on that setting (the public boundary
    # must hold even if the operator disabled api_redact_enabled).
    safe_session = redact_session_data(raw_dict)
    # Concrete local paths to scrub from any message prose / title.
    redact_paths = []
    for key in ("workspace", "worktree_path", "worktree_repo_root"):
        val = raw_dict.get(key)
        if val:
            redact_paths.append(str(val))
    try:
        from api.profiles import get_active_hermes_home
        redact_paths.append(str(get_active_hermes_home()))
    except Exception:
        pass
    try:
        redact_paths.append(str(Path.home()))
    except Exception:
        pass
    # Collect allowed roots for _embed_share_media: only files inside the
    # session workspace or the attachments root may be embedded.  This is
    # the hard security boundary that prevents arbitrary file reads through
    # crafted MEDIA: references in message text.
    _allowed_roots: list[Path] = []
    _ws = raw_dict.get("workspace")
    if _ws and isinstance(_ws, str) and _ws.strip():
        _allowed_roots.append(Path(_ws.strip()))
    try:
        from api.upload import _attachment_root
        _allowed_roots.append(_attachment_root())
    except Exception:
        pass
    _allowed_roots_tuple: tuple[Path, ...] = tuple(_allowed_roots)
    safe_messages = []
    for raw in safe_session.get("messages") or []:
        sanitized = _sanitize_message(
            raw, redact_paths=redact_paths, allowed_roots=_allowed_roots_tuple,
        )
        if sanitized:
            safe_messages.append(sanitized)
    if not safe_messages:
        raise ValueError("This conversation has no shareable messages yet.")
    # Only accept a genuine string title — a dict-valued title (possible via
    # /api/session/import) must not be str()'d into the public snapshot.
    _raw_title = safe_session.get("title")
    _raw_title = _raw_title if isinstance(_raw_title, str) else "Untitled"
    title = _force_redact_credentials(_raw_title or "Untitled")
    # Titles share the same public trust boundary but have no file-reading
    # context. Local MEDIA refs fail closed; ordinary public HTTP(S) refs may
    # remain, while residual file:// and authenticated /api/media refs do not.
    title = _omit_private_share_media_references(title, plain_text=True)
    title = _redact_share_paths(title, redact_paths) or "Untitled"
    return {
        "title": title,
        "messages": safe_messages,
        "message_count": len(safe_messages),
    }


def create_or_refresh_share(session) -> dict:
    snapshot = build_share_snapshot(session)
    with _SHARE_LOCK:
        _ensure_share_dir()
        existing_token = str(getattr(session, "share_token", "") or "").strip()
        token = existing_token or secrets.token_urlsafe(18)
        now = time.time()
        payload = {
            "token": token,
            "source_session_id": str(getattr(session, "session_id", "") or ""),
            "title": snapshot["title"],
            "messages": snapshot["messages"],
            "message_count": snapshot["message_count"],
            "created_at": now,
            "updated_at": now,
            "revoked_at": None,
        }
        path = _share_path(token)
        if path.exists():
            try:
                existing = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(existing, dict):
                    payload["created_at"] = existing.get("created_at") or now
            except Exception:
                logger.debug("Ignoring malformed share snapshot at %s", path, exc_info=True)
        _write_json_atomic(path, payload)
    return {
        "share_token": token,
        "share_title": payload["title"],
        "share_message_count": payload["message_count"],
        "share_created_at": payload["created_at"],
        "share_updated_at": payload["updated_at"],
    }


def load_share(token: str) -> dict | None:
    try:
        path = _share_path(token)
    except ValueError:
        return None
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        logger.warning("Failed to read share snapshot %s", path, exc_info=True)
        return None
    if not isinstance(payload, dict):
        return None
    if payload.get("revoked_at"):
        return None
    return _public_share_payload(payload)


def revoke_share(session) -> bool:
    token = str(getattr(session, "share_token", "") or "").strip()
    if not token:
        return False
    with _SHARE_LOCK:
        try:
            path = _share_path(token)
        except ValueError:
            return False
        if path.exists():
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                payload = {}
            if not isinstance(payload, dict):
                payload = {}
            payload["revoked_at"] = time.time()
            _write_json_atomic(path, payload)
    return True
