"""Public-share sanitizer stash integrity — review of PR #6285 (2026-11-23).

The re-review at ``83eb7143f42f`` accepted the October boundary work and raised
four new problems in ``api/shares.py::_strip_media_references``. Three of them
were already present at ``40ccd38`` and are measured here on the merged head:

1. FORGERY — input normalisation only handles CR/LF, so user text can contain
   NUL bytes that spell the internal stash markers (``\\x00I<n>\\x00`` style).
   The restore phase then substitutes stashed content into user text, and a
   stashed span carrying a later family's marker is expanded again by that
   family's restore pass, so a short message grows without bound. Reachable
   through share creation AND through a legacy stored snapshot served by the
   public GET payload.
2. QUADRATIC RESTORE — every restore is a whole-message ``str.replace`` per
   stashed item, so a message with many stashed spans costs O(spans x size).
3. INLINE MATH — the renderer stashes ``\\(...\\)`` and ``$...$`` as math, so
   ``file://`` inside them is inert on the public page; the sanitizer treats
   that text as prose and replaces it with the attachment placeholder.

Controls pin the behaviour that must not change: renderer-active bare
``file://`` prose is still stripped, display math stays preserved, a ``$...$``
span the renderer does NOT treat as math (contains `` | ``) is still stripped,
and plain prose without references is returned untouched.

Defect 4 of the review (the existing master linearity test fails on this head)
is measured by ``tests/test_share_inline_data_image_placeholder.py`` itself,
not re-implemented here.
"""

from __future__ import annotations

import time
from types import SimpleNamespace

from api import shares
from api.shares import _PLACEHOLDER

FENCE_BODY = "SECRETFENCEBODY"


def _share(text: str) -> str:
    """Sanitize through the real snapshot builder (the share-creation path)."""
    session = SimpleNamespace(
        workspace=None,
        messages=[{"role": "assistant", "content": text}],
        title="t",
    )
    return shares.build_share_snapshot(session)["messages"][0]["content"]


def _public_get(content: str) -> str:
    """Sanitize through the anonymous public-read payload (legacy snapshots)."""
    payload = {"title": "t", "messages": [{"role": "assistant", "content": content}]}
    public = shares._public_share_payload(payload)
    return public["messages"][0]["content"]


# ── 1. Forged stash markers ──────────────────────────────────────────────────


def test_forged_marker_cannot_duplicate_stashed_content_on_create():
    """A literal ``\\x00I0\\x00`` in user text must not become the stashed span."""
    text = "`x` \x00I0\x00"
    out = _share(text)
    assert "\x00" not in out
    assert out.count("`x`") == 1, out


def test_forged_marker_cannot_duplicate_stashed_content_on_public_get():
    text = "`x` \x00I0\x00"
    out = _public_get(text)
    assert "\x00" not in out
    assert out.count("`x`") == 1, out


def test_stashed_span_carrying_a_later_family_marker_is_not_expanded():
    """An inline span whose text spells the fence marker must not get the fence."""
    text = "```\n%s\n```\ntext `\x00F0\x00` tail" % FENCE_BODY
    out = _share(text)
    assert out.count(FENCE_BODY) == 1, out
    assert "\x00" not in out


def test_forged_markers_do_not_amplify_a_short_message():
    """511 forged markers must not turn a short message into thousands of chars."""
    text = "```\n%s\n```\n" % FENCE_BODY + "`\x00F0\x00`" * 511
    out = _public_get(text)
    assert len(out) <= len(text) + 8 * len(FENCE_BODY), (len(text), len(out))
    assert out.count(FENCE_BODY) == 1, out.count(FENCE_BODY)


# ── 2. Restore cost ──────────────────────────────────────────────────────────


def test_restore_cost_stays_linear_in_stashed_spans():
    def _elapsed(spans: int) -> float:
        text = "".join("`c%d` " % i for i in range(spans))
        start = time.perf_counter()
        out = _share(text)
        elapsed = time.perf_counter() - start
        assert out.count("`c0`") == 1
        return elapsed

    _elapsed(500)  # warm up
    small = _elapsed(2_000)
    large = _elapsed(8_000)
    # 4x the spans must stay far below the ~16x a whole-message replace per
    # span would cost.
    assert large < max(small * 10, 0.5), (small, large)


# ── 3. Inline math ───────────────────────────────────────────────────────────


def test_inline_math_latex_delimiter_keeps_file_url():
    text = "\\(a = file:///home/alice/secret.txt\\)"
    out = _share(text)
    assert "file:///home/alice/secret.txt" in out
    assert _PLACEHOLDER not in out


def test_inline_math_dollar_delimiter_keeps_file_url():
    text = "$a = file:///home/alice/secret.txt b$"
    out = _share(text)
    assert "file:///home/alice/secret.txt" in out
    assert _PLACEHOLDER not in out


def test_inline_math_preserved_on_public_get_too():
    text = "\\(a = file:///home/alice/secret.txt\\)"
    out = _public_get(text)
    assert "file:///home/alice/secret.txt" in out
    assert _PLACEHOLDER not in out


# ── Controls: what must NOT change ───────────────────────────────────────────


def test_bare_file_url_prose_is_still_stripped():
    out = _share("see file:///tmp/pic.png here")
    assert "file:///tmp/pic.png" not in out
    assert _PLACEHOLDER in out


def test_dollar_span_the_renderer_does_not_treat_as_math_is_still_stripped():
    """``$...$`` containing `` | `` is literal text to the renderer, not math."""
    out = _share("$a = file:///tmp/pic.png | b$")
    assert "file:///tmp/pic.png" not in out
    assert _PLACEHOLDER in out


def test_display_math_stays_preserved():
    for text in ("$$a = file:///tmp/pic.png$$", "\\[a = file:///tmp/pic.png\\]"):
        out = _share(text)
        assert "file:///tmp/pic.png" in out, text
        assert _PLACEHOLDER not in out, text


def test_plain_prose_without_references_is_unchanged():
    text = "plain prose without references"
    assert _share(text) == text
