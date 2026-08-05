"""MEDIA grammar consistency across every live consumer (PR #6607 re-review).

Re-review item 2: the shared grammar was not actually shared by all consumers.

- ``static/ui.js::_stripForTTS()`` used ``/MEDIA:[^\\s]+/g``, so a dotted/spaced
  or quoted path was only partly removed and the local-path tail was spoken
  aloud.
- ``media_token_pattern(exclude_urls=True)`` applied a case-SENSITIVE
  ``https?://`` guard OUTSIDE the capture, so a quoted URL
  (``MEDIA:"https://…"``) and an uppercase scheme (``MEDIA:HTTPS://…``) both
  matched as local share paths and were replaced with the missing-media
  placeholder, while the frontend rendered them as remote images.

One fixture table drives classification for both languages plus the real
``_embed_share_media()``.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).parent.parent.resolve()
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from api.helpers import (  # noqa: E402
    is_external_media_url,
    media_token_pattern,
    unquote_media_ref,
)

UI_JS = (REPO_ROOT / "static" / "ui.js").read_text(encoding="utf-8")
MESSAGES_JS = (REPO_ROOT / "static" / "messages.js").read_text(encoding="utf-8")

# A 1x1 PNG that satisfies the share inliner's magic-byte + MIME checks.
# Written as a commented, chunk-by-chunk bytes literal rather than a single
# bytes.fromhex(...) blob so each PNG chunk is readable in place and the fixture
# needs no decoding step at import time.
_TINY_PNG = (
    b"\x89PNG\r\n\x1a\n"                      # signature
    b"\x00\x00\x00\rIHDR"                     # IHDR chunk header
    b"\x00\x00\x00\x01\x00\x00\x00\x01"       # 1x1
    b"\x08\x06\x00\x00\x00"                   # 8-bit RGBA
    b"\x1f\x15\xc4\x89"                       # IHDR CRC
    b"\x00\x00\x00\nIDAT"                     # IDAT chunk header
    b"x\x9cc\x00\x01\x00\x00\x05\x00\x01"     # zlib-compressed single pixel
    b"\r\n-\xb4"                              # IDAT CRC
    b"\x00\x00\x00\x00IEND\xaeB`\x82"         # IEND
)

# (ref_as_written, is_external)
URL_CASES = [
    ("https://example.test/a.png", True),
    ('"https://example.test/a.png"', True),
    ("'https://example.test/a.png'", True),
    ("HTTPS://example.test/a.png", True),
    ("Https://example.test/a.png", True),
    ("http://example.test/a.png", True),
    ("HTTP://example.test/a.png", True),
    ('"HTTPS://example.test/a.png"', True),
    ("file:///tmp/a.png", False),
    ("data:image/png;base64,AAAA", False),
    ("/tmp/local.png", False),
    ('"/tmp/My Files/x.png"', False),
    ("/tmp/v1.2 Reports/chart.png", False),
    ("/tmp/no-ext-file", False),
    # A colon inside a local filename is not a scheme.
    ("/tmp/notdata:x.png", False),
]


@pytest.mark.parametrize("ref,external", URL_CASES)
def test_python_classifies_external_urls_case_insensitively(ref, external):
    assert is_external_media_url(ref) is external


@pytest.mark.parametrize("ref", [
    c[0] for c in URL_CASES
    if c[1] and re.match(r"(?i)^[\"']?https?://", c[0])
])
def test_share_matcher_never_captures_an_external_http_url(ref):
    """The share pattern must skip HTTP(S) URLs in every spelling.

    A captured URL is resolved as a local path and placeholdered, so this is the
    difference between a share showing a remote image and showing "media
    unavailable".

    Scoped to HTTP(S) deliberately. ``file://`` and ``data:`` refs ARE matched on
    purpose — the share boundary must see them so it can reject ``file://`` as
    absolute/un-scoped (api/shares.py:274) rather than let it through unexamined.
    That predates this PR and is asserted separately below.
    """
    share_re = re.compile(media_token_pattern(extra_exclude=">", exclude_urls=True))
    m = share_re.search(f"MEDIA:{ref}")
    assert m is None, (
        f"share matcher captured external URL {ref!r} as "
        f"{unquote_media_ref(m.group(1))!r} — it will be placeholdered"
    )


def test_share_matcher_still_sees_file_uris_so_they_can_be_rejected():
    """``file://`` must remain visible to the share boundary.

    api/shares.py rejects it explicitly as absolute/un-scoped. If the URL guard
    skipped it, the token would pass through a public share unexamined.
    """
    share_re = re.compile(media_token_pattern(extra_exclude=">", exclude_urls=True))
    assert share_re.search("MEDIA:file:///tmp/a.png") is not None


@pytest.mark.parametrize("ref", [
    c[0] for c in URL_CASES
    if c[1] and re.match(r"(?i)^[\"']?https?://", c[0])
])
def test_share_inliner_leaves_external_http_urls_untouched(ref, tmp_path):
    from api import shares

    text = f"see MEDIA:{ref} ok"
    out = shares._embed_share_media(text, allowed_roots=(tmp_path,))
    assert out == text, f"external URL {ref!r} was rewritten: {out!r}"


def test_share_inliner_placeholders_a_file_uri(tmp_path):
    """Pre-existing security posture, pinned: file:// never resolves in a share."""
    from api import shares

    target = tmp_path / "secret.png"
    target.write_bytes(_TINY_PNG)
    out = shares._embed_share_media(
        f"see MEDIA:file://{target} ok", allowed_roots=(tmp_path,)
    )
    assert "data:image/png;base64," not in out


@pytest.mark.parametrize("quoted", [True, False])
def test_share_inliner_still_embeds_local_spaced_paths(tmp_path, quoted):
    """The URL guard must not become a blanket skip."""
    from api import shares

    target = tmp_path / "My Files" / "chart.png"
    target.parent.mkdir(parents=True)
    target.write_bytes(_TINY_PNG)
    ref = f'"{target}"' if quoted else str(target)
    out = shares._embed_share_media(f"see MEDIA:{ref} ok", allowed_roots=(tmp_path,))
    assert "data:image/png;base64," in out


# ── TTS ─────────────────────────────────────────────────────────────────────


def _js_strip_for_tts(texts: list[str]) -> list[str]:
    """Run the real _stripForTTS MEDIA replacement under node."""
    node = shutil.which("node")
    if not node:
        pytest.skip("node not available")

    def extract(src: str, name: str) -> str:
        start = src.index(f"function {name}(")
        depth = 0
        started = False
        for i in range(start, len(src)):
            if src[i] == "{":
                depth += 1
                started = True
            elif src[i] == "}":
                depth -= 1
                if started and depth == 0:
                    return src[start:i + 1]
        raise AssertionError(f"unbalanced braces extracting {name}")

    script = "\n".join([
        extract(UI_JS, "_mediaPathSrc"),
        extract(UI_JS, "_mediaTokenRe"),
        "const texts = JSON.parse(process.argv[1]);",
        "console.log(JSON.stringify(texts.map(t => t.replace(_mediaTokenRe(), 'a file'))));",
    ])
    proc = subprocess.run(
        [node, "--input-type=module", "-e", script, json.dumps(texts)],
        capture_output=True, text=True, timeout=30, check=True,
    )
    return json.loads(proc.stdout)


TTS_CASES = [
    ("See MEDIA:/tmp/a.png now", "See a file now"),
    # Dotted/spaced: the old /MEDIA:[^\s]+/ left "Reports/chart.png" to be spoken.
    ("See MEDIA:/tmp/v1.2 Reports/chart.png now", "See a file now"),
    # Quoted: the old pattern left the closing quote behind.
    ('See MEDIA:"/tmp/My Files/report (final).png" now', "See a file now"),
    ("See MEDIA:'/tmp/My Files/single.png' now", "See a file now"),
    ("Two MEDIA:/tmp/a.png and MEDIA:/tmp/b.png here",
     "Two a file and a file here"),
    ("No media here at all", "No media here at all"),
]


@pytest.mark.parametrize("text,expected", TTS_CASES)
def test_tts_strips_whole_media_ref(text, expected):
    assert _js_strip_for_tts([text]) == [expected]


def test_tts_uses_the_shared_grammar_not_a_private_regex():
    """Guard against a private MEDIA regex reappearing in the TTS path."""
    start = UI_JS.index("function _stripForTTS")
    # Bound the scan to the function body.
    depth = 0
    started = False
    end = start
    for i in range(start, len(UI_JS)):
        if UI_JS[i] == "{":
            depth += 1
            started = True
        elif UI_JS[i] == "}":
            depth -= 1
            if started and depth == 0:
                end = i + 1
                break
    body = UI_JS[start:end]
    assert "_mediaTokenRe()" in body, (
        "_stripForTTS must route MEDIA refs through the shared token grammar"
    )
    # Strip line comments before scanning: the explanatory comment legitimately
    # quotes the old pattern, and greping raw source would match it.
    code = "\n".join(
        line.split("//", 1)[0] for line in body.splitlines()
    )
    assert "MEDIA:[^\\s]" not in code, (
        "_stripForTTS still carries the whitespace-only MEDIA regex, which "
        "leaves the local-path tail of a spaced ref to be spoken aloud"
    )


# ── MEDIA inside code fences ────────────────────────────────────────────────


def test_media_in_code_is_active_in_settled_and_streaming():
    """Document and pin the code-fence policy: ACTIVE media, both paths.

    Reviewer question: do safe streaming, fade streaming, and settled
    ``renderMd()`` apply the same MEDIA policy inside inline/fenced code?

    They do, and the chosen policy is **active media**:

    - Settled: ``renderMd()`` stashes MEDIA tokens FIRST, before the fence pass
      ("must run first, before any other processing"), so a token inside ``` is
      already replaced by a stash placeholder by the time fenced code is
      HTML-escaped.
    - Streaming: the smd parser delivers fenced-code content through
      ``add_text``, which is the method the MEDIA interceptor wraps (verified
      against the real vendored parser: text inside ``` arrives at ``add_text``
      while the code-block token is open). Both streaming renderers — safe and
      fade — wrap the same ``add_text``, so neither can diverge from the other.

    This test pins the ordering that makes settled behave that way, and pins that
    the interceptor is installed on ``add_text`` rather than on a text sink that
    skips code. Changing either without a deliberate policy decision breaks it.
    """
    body_start = UI_JS.index("function renderMd(")
    body = UI_JS[body_start:body_start + 40000]
    media_stash = body.find("MEDIA: token stash")
    fence_stash = body.find("Fence stash:")
    assert media_stash != -1 and fence_stash != -1, (
        "renderMd no longer has the expected MEDIA/fence stash passes"
    )
    assert media_stash < fence_stash, (
        "MEDIA must be stashed BEFORE fence protection, or settled rendering "
        "silently switches to literal-code semantics while streaming keeps "
        "rendering cards"
    )
    # Streaming: the interceptor wraps add_text, which is what receives fenced
    # content — so both stream renderers inherit one policy.
    assert "renderer.add_text=" in MESSAGES_JS
    assert "_smdMediaAwareAddText(baseAddText" in MESSAGES_JS
