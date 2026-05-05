"""Tests for #1697 — pasting multiple images at once only attaches one.

Root cause: the composer paste handler in `static/boot.js` synthesizes filenames
via `screenshot-${Date.now()}.${ext}` inside a `.map()` callback. `Date.now()`
returns the same millisecond timestamp for every iteration of a synchronous
loop within the same event tick, so all N pasted images end up with the same
filename. `addFiles()` then dedupes by name and silently drops images 2..N.

The fix: capture `Date.now()` once outside the `.map()` and append a 1-based
index suffix when there are 2+ images:

    const ts = Date.now();
    const multi = imageItems.length > 1;
    const files = imageItems.map((i, idx) => {
      const blob = i.getAsFile();
      const ext = i.type.split('/')[1] || 'png';
      const suffix = multi ? `-${idx + 1}` : '';
      return new File([blob], `screenshot-${ts}${suffix}.${ext}`, { type: i.type });
    });

These tests guard the handler shape against regression by static-analyzing
`static/boot.js`. They follow the same pattern as `test_1620_paste_text_with_image.py`
and `test_issue1095_pasted_images.py`.
"""
import os
import re


def _read_boot_js() -> str:
    with open(os.path.join('static', 'boot.js')) as f:
        return f.read()


def _paste_handler_body() -> str:
    """Extract the body of the #msg paste handler for assertions."""
    src = _read_boot_js()
    m = re.search(r"\$\('msg'\)\.addEventListener\('paste',\s*e\s*=>\s*\{", src)
    assert m, "#msg paste handler not found in static/boot.js"
    start = m.end() - 1
    depth = 0
    for i in range(start, len(src)):
        c = src[i]
        if c == '{':
            depth += 1
        elif c == '}':
            depth -= 1
            if depth == 0:
                return src[start:i + 1]
    raise AssertionError("Unbalanced braces in #msg paste handler")


class TestPasteMultipleImages:
    """Regression suite for #1697 — Date.now() filename collision."""

    def test_handler_does_not_call_dot_now_inside_map(self):
        """Date.now() must NOT be called inside the imageItems.map() callback —
        that's exactly the bug. All callbacks see the same timestamp because
        they run in the same synchronous tick.
        """
        body = _paste_handler_body()
        # Find the imageItems.map(...) section.
        m = re.search(r"imageItems\.map\(", body)
        assert m, "imageItems.map() callback not found"
        # Find the matching close paren of .map(...).
        start = m.end() - 1
        depth = 0
        for i in range(start, len(body)):
            c = body[i]
            if c == '(':
                depth += 1
            elif c == ')':
                depth -= 1
                if depth == 0:
                    map_body = body[start:i + 1]
                    break
        else:
            raise AssertionError("Unbalanced parens in imageItems.map()")

        # Inside the map callback, Date.now() must NOT appear — that's the bug shape.
        assert 'Date.now()' not in map_body, (
            "Date.now() inside imageItems.map() collides for multi-image pastes (#1697); "
            "capture the timestamp once outside the map and use an index suffix instead"
        )

    def test_handler_captures_timestamp_outside_map(self):
        """The fix captures Date.now() into a variable (or equivalent) BEFORE the map,
        and the map callback uses that captured value plus a per-iteration suffix.
        """
        body = _paste_handler_body()
        # Look for a `const ts = Date.now()` (or similar) before the imageItems.map line.
        # Allow either `const ts=Date.now()` or `let ts=Date.now()` style.
        pre_map = body.split('imageItems.map(')[0]
        assert re.search(r"\b(?:const|let)\s+\w+\s*=\s*Date\.now\(\)", pre_map), (
            "expected Date.now() to be captured into a variable before imageItems.map() so "
            "all pasted images share a single timestamp + are differentiated by index"
        )

    def test_handler_uses_index_in_filename(self):
        """The map callback must take an index parameter and use it in the synthesized
        filename so two simultaneously-pasted images get distinct names.
        """
        body = _paste_handler_body()
        # The map callback must accept (i, idx) or similar two-arg form.
        m = re.search(r"imageItems\.map\(\s*\(\s*\w+\s*,\s*\w+\s*\)\s*=>", body)
        assert m, (
            "imageItems.map() callback must accept a second (index) parameter so each "
            "synthesized filename can be disambiguated within the same paste event (#1697)"
        )

    def test_handler_filename_template_includes_distinguisher(self):
        """The screenshot-${ts} template must include a distinguisher (suffix or index)
        when more than one image is being attached, so addFiles() dedup-by-name doesn't
        drop images 2..N.
        """
        body = _paste_handler_body()
        # Either a `-${idx+1}` suffix or any other expression that varies per-iteration
        # is acceptable. Reject the bare `screenshot-${Date.now()}.${ext}` shape.
        bad = re.search(
            r"screenshot-\$\{Date\.now\(\)\}\.\$\{ext\}",
            body,
        )
        assert not bad, (
            "filename uses bare `screenshot-${Date.now()}.${ext}` template — collides for "
            "multi-image pastes (#1697); use a captured timestamp + index suffix"
        )
        # And confirm SOME differentiator is present in the filename construction.
        # The filename template `screenshot-${ts}${suffix}.${ext}` (or any equivalent
        # that varies per-iteration) is acceptable. Reject only the bare
        # `screenshot-${Date.now()}.${ext}` shape.
        # Look for any template-literal expansion in the screenshot filename beyond
        # just the timestamp and extension — that's the differentiator.
        screenshot_template = re.search(
            r"`screenshot-([^`]*)`",
            body,
        )
        assert screenshot_template, (
            "expected a `screenshot-...` template literal for the synthesized filename"
        )
        template_body = screenshot_template.group(1)
        # Count the ${...} placeholders. The bug shape is exactly two: ${Date.now()}
        # and ${ext}. Any fix needs at least 3 placeholders OR a per-iteration index
        # baked into one of them.
        placeholders = re.findall(r"\$\{[^}]+\}", template_body)
        assert len(placeholders) >= 3 or any(
            tok in p for p in placeholders
            for tok in ('idx', 'index', 'suffix', 'i+', 'i +', '+1', 'count')
        ), (
            "filename template must incorporate a per-image distinguisher (suffix or "
            "index) so simultaneously-pasted images get distinct names. Found "
            f"placeholders: {placeholders}"
        )

    def test_handler_single_image_path_unchanged(self):
        """For a SINGLE pasted image, the filename must remain a clean
        `screenshot-<ts>.<ext>` (no `-1` suffix that would change existing behavior or
        break tests that assume the bare filename shape).
        """
        body = _paste_handler_body()
        # Look for a length-check / multi guard that conditionally applies the suffix.
        # Acceptable shapes:
        #   const multi = imageItems.length > 1;
        #   const suffix = multi ? `-${idx+1}` : '';
        # OR any equivalent expression that suppresses the suffix when length is 1.
        assert re.search(
            r"imageItems\.length\s*>\s*1",
            body,
        ) or re.search(
            r"length\s*>\s*1\s*\?",
            body,
        ), (
            "single-image paste must keep the bare `screenshot-<ts>.<ext>` filename — "
            "expected an `imageItems.length > 1` guard before applying the index suffix"
        )

    def test_handler_still_intercepts_screenshot_paste(self):
        """The screenshot-attach code path must still preventDefault and call addFiles
        — the fix is only inside the filename construction, not the surrounding logic.
        """
        body = _paste_handler_body()
        assert 'e.preventDefault()' in body
        assert 'addFiles(files)' in body
        assert "setStatus(t('image_pasted')" in body
