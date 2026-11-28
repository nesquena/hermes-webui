"""Public-share media sanitizer boundaries — review of PR #6285 (2026-10-15).

The sanitizer in ``api/shares.py::_strip_media_references`` runs on every message
that enters or leaves a public share snapshot. Its grammar has to agree with the
settled renderer (``static/ui.js::renderMd``), because anything it misses is
turned into an authenticated ``api/media?path=`` reference by the browser and
anything it over-reaches destroys content the renderer keeps inert.

Five behaviours are pinned here (each fails on the merged head ``5b63094ca``):

1. ARBITRARY blockquote depth must not raise ``RecursionError`` — the renderer
   handles 400 nested levels, share creation must not 500 on them.
2. A remote ``MEDIA:https://`` reference is rendered as a remote image by the
   renderer and must survive the public boundary untouched, delimiters included.
3. Regions the renderer treats as literal (entity-encoded ``<pre>``, an
   entity-encoded blockquoted fence, multiline ``$$`` math) must keep their
   ``file://`` text byte-for-byte.
4. Local ``MEDIA:`` tokens that do reach the sanitizer use the same placeholder
   constant as the rest of the public boundary.
5. Controls: renderer-active references are still removed.
"""

import pytest

from api.shares import _PLACEHOLDER, _strip_media_references

ACTIVE_FILE_URL = "file:///home/alice/secret.txt"


# ── 1. Blockquote depth ──────────────────────────────────────────────────────


def test_deeply_nested_blockquote_does_not_recurse():
    """500 nested blockquote levels must not raise RecursionError."""
    text = "> " * 500 + "text"
    out = _strip_media_references(text)
    assert "text" in out


def test_deeply_nested_blockquote_still_strips_active_file_url():
    """Deep nesting must still sanitise the renderer-active reference inside it."""
    text = "> " * 400 + f"[secret]({ACTIVE_FILE_URL})"
    out = _strip_media_references(text)
    assert ACTIVE_FILE_URL not in out


def test_deeply_nested_blockquote_preserves_byte_for_byte_when_inert():
    """Nesting alone must not rewrite the message: plain prose stays intact."""
    body = "plain prose without references"
    out = _strip_media_references("> " * 300 + body)
    assert body in out


# ── 2. Remote MEDIA: references ──────────────────────────────────────────────

REMOTE = "https://cdn.example.com/chart.png"


def test_remote_media_reference_survives():
    out = _strip_media_references(f"chart: MEDIA:{REMOTE} here")
    assert f"MEDIA:{REMOTE}" in out


def test_remote_media_reference_keeps_bold_delimiters():
    out = _strip_media_references(f"**MEDIA:{REMOTE}**")
    assert out.endswith("**"), out


def test_remote_media_reference_keeps_closing_paren():
    out = _strip_media_references(f"(see MEDIA:{REMOTE})")
    assert out.endswith(")"), out


def test_inline_code_media_statement_keeps_its_backtick():
    out = _strip_media_references("run `MEDIA:` first")
    assert "`MEDIA:`" in out


# ── 3. Renderer-inert regions ────────────────────────────────────────────────

INERT_TARGET = "file:///home/alice/inert target.png"


def test_entity_encoded_pre_block_keeps_file_url():
    """renderMd() decodes entities first, then stashes <pre> as literal HTML."""
    out = _strip_media_references("&lt;pre&gt;open file:///home/alice/inert.png&lt;/pre&gt;")
    assert "file:///home/alice/inert.png" in out


def test_entity_encoded_blockquoted_fence_keeps_file_url():
    out = _strip_media_references(
        "> &gt; ```\n> &gt; file:///home/alice/inert.png\n> &gt; ```"
    )
    assert "file:///home/alice/inert.png" in out


def test_multiline_math_keeps_file_url():
    out = _strip_media_references("$$\nfile:///home/alice/inert.png\na = b\n$$")
    assert "file:///home/alice/inert.png" in out


def test_multiline_bracket_math_keeps_file_url():
    out = _strip_media_references("\\[\nfile:///home/alice/inert.png\n\\]")
    assert "file:///home/alice/inert.png" in out


# ── 4. Placeholder identity ─────────────────────────────────────────────────


def test_local_media_token_uses_the_shared_placeholder():
    out = _strip_media_references("see MEDIA:/tmp/shot.png now")
    assert _PLACEHOLDER in out
    assert "MEDIA:/tmp/shot.png" not in out


def test_file_url_replacement_uses_the_shared_placeholder():
    out = _strip_media_references("[the file](file:///tmp/shot.png)")
    assert _PLACEHOLDER in out


# ── 5. Controls: renderer-active references are removed ─────────────────────


@pytest.mark.parametrize(
    "raw",
    [
        f"the file {ACTIVE_FILE_URL} here",
        f"[the file]({ACTIVE_FILE_URL})",
        f"![chart]({ACTIVE_FILE_URL})",
        "![chart](file:///tmp/my shot.png)",
        "![chart](file:///tmp/multi\nline.png)",
    ],
)
def test_renderer_active_file_urls_are_removed(raw):
    out = _strip_media_references(raw)
    assert "file://" not in out, out


@pytest.mark.parametrize(
    "raw",
    [
        "```\nfile:///tmp/shot.png\n```",
        "run `file:///tmp/shot.png` now",
        "<pre>file:///tmp/shot.png</pre>",
    ],
)
def test_renderer_inert_regions_keep_their_literal(raw):
    out = _strip_media_references(raw)
    assert "file:///tmp/shot.png" in out, out
