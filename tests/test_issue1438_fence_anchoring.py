"""Regression tests for #1438 — triple backticks mid-line should not terminate fences.

Bug shape (issue #1438): the fence regex `/```([\\s\\S]*?)```/g` had no line
anchoring. A literal triple backtick inside a code-block content (e.g. a regex
pattern with ``` in a lookbehind) terminated the outer fence at the wrong place.
The leaked tail then went through bold/italic/inline-code passes, eating `*`
characters and leaking literal `</strong>` tags into the rendered output.

Reported by Cygnus (Discord, May 1 2026), relayed by @AvidFuturist.

Fix: anchor the fence regex per CommonMark §4.5 — opening fence must start a
line (with up to 3 spaces of indent), closing fence must also start a line.

Sites patched:
  static/ui.js:1557  — renderMd() fenced-block stash
  static/ui.js:74    — _renderUserFencedBlocks() (user message renderer)
  static/ui.js:2597  — _stripForTTS() (TTS pre-strip)
  tests/test_sprint16.py — Python mirror (two regexes to keep in sync)

Note on the Python mirror: it does not implement `_ob_stash` (which protects
`<code>` from outer bold/italic) or `_pre_stash` (which protects `<pre>` from
paragraph-wrap). So mirror output may show extra `<em>` inside `<pre>` blocks
or `<pre>` wrapped in `<p>`. These are mirror-only artifacts; the actual JS
pipeline produces clean output. Tests below focus on the structural property
that the fence regex captures the correct extent — the part that's identical
in both implementations.
"""

from __future__ import annotations

import re
from pathlib import Path


from tests.test_sprint16 import render_md  # Python mirror of renderMd()


UI_JS = (Path(__file__).resolve().parent.parent / "static" / "ui.js").read_text()


def _strip_pre_blocks(out: str) -> str:
    """Remove all <pre>...</pre> blocks for assertions about the surrounding text."""
    return re.sub(r"<pre[\s\S]*?</pre>", "", out)


# ── 1. THE BUG: Cygnus's exact repro ──────────────────────────────────────────


def test_inner_triple_backtick_inside_regex_does_not_terminate_outer_fence():
    """Cygnus's exact input — a regex literal with ``` in a lookbehind."""
    text = (
        "Here's the regex:\n\n"
        "```regex\n"
        "(?<!\\n)(?<!(?:^|\\n)[ \\t]*(?:```[^\\n]*|%%[ \\t]*(?:\\n|$)))\n"
        "```\n\n"
        "uses **bold** for emphasis."
    )
    out = render_md(text)

    # ── Structural property: exactly one fence captured the right extent.
    assert out.count("<pre>") == 1, f"expected 1 <pre>, got {out.count('<pre>')}"
    assert out.count("</pre>") == 1

    # ── The inner ``` is preserved as literal text inside the code block.
    #    Before the fix, the `</code></pre>` would appear MID-content and the
    #    inner ``` would NOT survive (the regex ate it as a delimiter).
    pre_block = re.search(r"<pre[\s\S]*?</pre>", out)
    assert pre_block, "no <pre> block found"
    assert "```[^" in pre_block.group(0), (
        "inner triple backtick should be preserved as literal inside the code block"
    )
    assert "%%" in pre_block.group(0), "tail of regex must survive inside code block"

    # ── No orphaned ``` outside any <pre> block.
    #    Before the fix, the trailing ``` (which used to be a closing fence)
    #    leaked into the surrounding markdown stream as literal text.
    outside = _strip_pre_blocks(out)
    assert "```" not in outside, f"orphaned ``` leaked outside <pre>: {outside!r}"

    # ── Bold renders correctly AFTER the fence.
    assert "<strong>bold</strong>" in out, "bold must survive intact"
    # Strong tags balanced.
    assert out.count("<strong>") == out.count("</strong>")


# ── 2. Inline triple-backtick in running text must NOT open a fence ────────────


def test_inline_triple_backtick_in_paragraph_does_not_open_fence():
    """A ``` in the middle of a sentence must not be treated as a fence opener."""
    text = "Plain text with ``` in the middle of a sentence."
    out = render_md(text)
    assert "<pre>" not in out
    # The literal ``` survives somewhere in the rendered output.
    assert "```" in out


def test_three_backticks_at_end_of_sentence_no_fence():
    """``` immediately after text on the same line — not a fence."""
    text = "End with``` not a fence"
    out = render_md(text)
    assert "<pre>" not in out


def test_unmatched_partial_fence_does_not_eat_message():
    """Partial/streaming input with no closing fence — must not match anything.

    Before the fix, an inner ``` could pair with itself and produce a bogus <pre>.
    With anchoring, no match occurs and content stays as plain text.
    """
    text = "```python\nincomplete code, no close yet"
    out = render_md(text)
    assert "<pre>" not in out
    assert "incomplete code" in out


# ── 3. Existing happy paths must keep working ─────────────────────────────────


def test_simple_python_fence_renders():
    text = "```python\nprint('hi')\n```"
    out = render_md(text)
    assert "<pre>" in out
    # esc() may HTML-encode quotes; either form is fine.
    assert "print(&#x27;hi&#x27;)" in out or "print('hi')" in out
    # Language tag preserved.
    assert 'class="pre-header">python' in out


def test_fence_after_paragraph_no_blank_line_required():
    """CommonMark allows a fence directly after text — `\\n` is enough."""
    text = "Some text\n```\nx = 1\n```"
    out = render_md(text)
    assert "<pre>" in out
    assert "x = 1" in out


def test_fence_at_end_of_input_no_trailing_newline():
    """Closing fence at the very end of input (no newline after)."""
    text = "Intro\n\n```\nfoo\n```"
    out = render_md(text)
    assert out.count("<pre>") == 1
    assert "foo" in out


def test_two_adjacent_fenced_blocks_render_independently():
    text = "```\nA\n```\n\n```\nB\n```"
    out = render_md(text)
    assert out.count("<pre>") == 2
    assert "A" in out and "B" in out


def test_three_space_indented_fence_still_recognised():
    """CommonMark allows up to 3 spaces of indent on fence lines."""
    text = "   ```\nfoo\n   ```"
    out = render_md(text)
    assert "<pre>" in out
    assert "foo" in out


def test_four_space_indent_is_not_a_fence():
    """4+ spaces is an indented code block in CommonMark, not a fence.

    With the line-anchored regex, this no longer matches the fence regex, so
    no <pre> with bogus content. We don't implement strict CommonMark
    indented-code-block behaviour — just verify we don't false-positive.
    """
    text = "    ```py\n    foo\n    ```"
    out = render_md(text)
    assert "<pre>" not in out


# ── 4. Bold/italic/inline-code outside a fence still work after the fix ───────


def test_bold_after_fence_renders_correctly():
    text = "```\ncode\n```\n\nThen **bold** text."
    out = render_md(text)
    assert "<pre>" in out
    assert "<strong>bold</strong>" in out


def test_italic_after_fence_renders_correctly():
    text = "```\ncode\n```\n\nThen *italic* text."
    out = render_md(text)
    assert "<pre>" in out
    assert "<em>italic</em>" in out


def test_inline_code_after_fence():
    text = "```\nblock\n```\n\nThen `inline` code."
    out = render_md(text)
    assert "<pre>" in out
    assert "<code>inline</code>" in out


# ── 5. SOURCE-LEVEL guards (catch regression of any of the 3 patched sites) ───


def test_renderMd_fence_regex_is_line_anchored():
    """The fence regex in renderMd must keep line anchoring and fence-length matching."""
    pattern = r"s=s.replace(/(^|\n)[ ]{0,3}(`{3,})([^\n`]*)\n(?:([\s\S]*?)\n)?[ ]{0,3}\2`*[ \t]*(?=\n|$)/g"
    assert pattern in UI_JS, (
        "renderMd fence regex lost line anchoring or #1696 fence-length matching"
    )


def test_renderUserFencedBlocks_fence_regex_is_line_anchored():
    """The fence regex in _renderUserFencedBlocks must also be line-anchored."""
    pattern = r"s=s.replace(/(^|\n)[ ]{0,3}(`{3,})([^\n`]*)\n(?:([\s\S]*?)\n)?[ ]{0,3}\2`*[ \t]*(?=\n|$)/g"
    assert UI_JS.count(pattern) >= 2, (
        "render/user fence regexes lost line anchoring or #1696 fence-length matching"
    )


def test_stripForTTS_fence_regex_is_line_anchored():
    """_stripForTTS must use the line-anchored fence regex too."""
    assert re.search(
        r"text=text\.replace\(/\(\^\|\\n\)\[ \]\{0,3\}```\(\?:\[\\s\\S\]\*\?\\n\)\?\[ \]\{0,3\}```\(\?=\\n\|\$\)/g",
        UI_JS,
    ), "_stripForTTS fence regex is not line-anchored — regression of #1438"


def test_renderMd_callback_prefixes_lead():
    """Without `lead+`, the leading newline gets stripped and paragraphs above
    bleed into the <pre> after fence stash restore.
    """
    assert "return lead+'\\x00P'+(_preBlock_stash.length-1)+'\\x00';" in UI_JS, (
        "renderMd fence callback must return lead+stashtoken to preserve newlines"
    )


def test_renderUserFencedBlocks_callback_prefixes_lead():
    assert "return lead+'\\x00UF'+(stash.length-1)+'\\x00';" in UI_JS, (
        "_renderUserFencedBlocks callback must return lead+stashtoken to preserve newlines"
    )


def test_no_unanchored_fence_regex_remains_in_render_paths():
    """Belt-and-suspenders: assert the OLD vulnerable patterns are gone from
    the three render/strip paths. (Documenting the regex literal is fragile;
    we just verify the bare unanchored form isn't present in the patched
    sites as a literal substring.)
    """
    # The exact OLD vulnerable forms that this PR replaces.
    old_render_md = "s=s.replace(/```([\\s\\S]*?)```/g,(_,raw)=>{"
    old_user_fenced = (
        "s=s.replace(/```([a-zA-Z0-9_+-]*)\\n([\\s\\S]*?)```/g,(_,lang,code)=>{"
    )
    old_tts_strip = "text=text.replace(/```[\\s\\S]*?```/g,' ');"
    assert old_render_md not in UI_JS, (
        "old unanchored renderMd fence regex still present"
    )
    assert old_user_fenced not in UI_JS, (
        "old unanchored _renderUserFencedBlocks regex still present"
    )
    assert old_tts_strip not in UI_JS, "old unanchored _stripForTTS regex still present"


# ── 6. Diff/patch fence with inner ``` in content ─────────────────────────────


def test_diff_fence_with_inner_backticks_in_content():
    """Diff/patch blocks where a content line contains ``` mid-line — the inner
    ``` must not be treated as a close fence.

    Verified at the source level: the JS regex requires the close fence to be
    on its own line (preceded by `\\n`), so a mid-line ``` cannot match.
    The Python mirror doesn't implement diff/patch styling or _preBlock_stash
    so we don't assert against its output here.
    """
    # Source-level: the renderMd fence regex requires `\n[ ]{0,3}\`\`\`(?=\n|$)`
    # for the close fence — a mid-line ``` cannot match.
    # Three sites must all use this pattern.
    # Pattern explanation: ui.js source contains literal backslash-n in regex literals
    # (ONE backslash + 'n'). In a Python raw string, r"\\n" compiles to a regex pattern
    # matching ONE literal backslash followed by 'n'.
    new_matches = UI_JS.count(r"[ ]{0,3}\2`*[ \t]*(?=\n|$)")
    old_tts_matches = re.findall(r"```\(\?=\\n\|\$\)", UI_JS)
    assert new_matches >= 2 and len(old_tts_matches) >= 1, (
        f"renderMd/_renderUserFencedBlocks must have fence-length-aware line-anchored "
        f"closers and _stripForTTS must keep a line-anchored closer; found "
        f"new={new_matches}, tts={len(old_tts_matches)}"
    )
