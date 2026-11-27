"""#6580 round 2 (maintainer review 2026-11-23T04:13:21Z): the two narrow defects
that remain on ``fix/filename-spaces``.

1. Single-quoted and parenthesised Markdown titles become part of the URL. The
   four link/image patterns in ``static/ui.js`` only accept the double-quoted
   title arm, so ``[docs](https://example.com/a 'the title')`` links to
   ``https://example.com/a%20'the%20title'`` and loses the title text (same for
   ``(the title)``). ``renderMd()`` (paragraph path), ``inlineMd()`` (list and
   blockquote paths) and the table path must all keep ``href``/``src`` equal to
   the bare URL and retain the title text.

2. Quoted ``MEDIA:`` restoration changes code-block source. The MEDIA stash
   keeps the decoded reference but not the original matched token, so the
   ``data-raw`` reconstruction of a json/yaml fence drops the quotes and the
   encoding: ``{"a":"MEDIA:","b":"x"}`` comes back as invalid JSON and
   ``p: MEDIA:"/tmp/my screenshot.png"`` loses its quotes.

Coverage is behavioural: every case drives the real ``renderMd()`` (and the real
``_inlineMediaHtmlForRef``) out of ``static/ui.js`` through node.
"""
from __future__ import annotations

import html as html_lib
import json
import re
import unittest

from tests.test_issue6580_quoted_media_port import _render


def _attr(tag_html: str, name: str) -> str:
    match = re.search(rf'{name}="([^"]*)"', tag_html)
    return html_lib.unescape(match.group(1)) if match else ""


def _first_tag(out: str, tag: str) -> str:
    match = re.search(rf"<{tag}\b[^>]*>", out)
    return match.group(0) if match else ""


def _data_raw(out: str) -> str:
    match = re.search(r'data-raw="(.*?)" data-lang=', out, re.S)
    if not match:
        raise AssertionError(f"no data-raw attribute in {out!r}")
    raw = match.group(1).replace("&#10;", "\n")
    return html_lib.unescape(raw)


class TestTitledLinkAndImageForms(unittest.TestCase):
    """All three title forms keep the bare destination and the title text."""

    URL = "https://example.com/a"

    def _assert_link(self, markdown: str, url: str = URL) -> str:
        out = _render(markdown)
        anchor = _first_tag(out, "a")
        self.assertTrue(anchor, f"no anchor rendered for {markdown!r}: {out!r}")
        self.assertEqual(
            _attr(anchor, "href"),
            url,
            f"href must be the bare URL for {markdown!r}: {out!r}",
        )
        self.assertIn("the title", out, f"title text must be retained for {markdown!r}: {out!r}")
        return out

    def _assert_image(self, markdown: str, url: str) -> str:
        out = _render(markdown)
        img = _first_tag(out, "img")
        self.assertTrue(img, f"no image rendered for {markdown!r}: {out!r}")
        self.assertEqual(
            _attr(img, "src"),
            url,
            f"src must be the bare URL for {markdown!r}: {out!r}",
        )
        self.assertIn("the title", out, f"title text must be retained for {markdown!r}: {out!r}")
        return out

    # -- control: the double-quoted arm already works -----------------------
    def test_double_quoted_title_control(self):
        out = self._assert_link('[docs](https://example.com/a "the title") tail')
        self.assertIn("tail", out)

    # -- defect 1: single-quoted title --------------------------------------
    def test_single_quoted_title_link(self):
        self._assert_link("[docs](https://example.com/a 'the title') tail")

    def test_single_quoted_title_image(self):
        self._assert_image("![shot](https://example.com/i.png 'the title') tail", "https://example.com/i.png")

    def test_single_quoted_title_in_list_item(self):
        out = self._assert_link("- [docs](https://example.com/a 'the title')\n- second")
        self.assertIn("second", out)

    def test_single_quoted_title_in_table_cell(self):
        out = self._assert_link("| a | b |\n|---|---|\n| [docs](https://example.com/a 'the title') | x |")
        self.assertIn("x", out)

    # -- defect 1: parenthesised title --------------------------------------
    def test_parenthesised_title_link(self):
        self._assert_link("[docs](https://example.com/a (the title)) tail")

    def test_parenthesised_title_image(self):
        self._assert_image("![shot](https://example.com/i.png (the title)) tail", "https://example.com/i.png")

    def test_parenthesised_title_in_list_item(self):
        self._assert_link("- [docs](https://example.com/a (the title))\n- second")

    # -- control: title-like text AFTER the link is not consumed as a title --
    def test_title_like_text_after_the_link_is_not_consumed(self):
        out = _render("[docs](https://example.com/a) (the title) tail")
        self.assertEqual(_attr(_first_tag(out, "a"), "href"), "https://example.com/a")
        self.assertIn("(the title)", out, f"trailing prose must stay text: {out!r}")
        self.assertIn("tail", out)

    def test_plain_link_without_title_control(self):
        out = _render("[docs](https://example.com/a) tail")
        self.assertEqual(_attr(_first_tag(out, "a"), "href"), "https://example.com/a")
        self.assertIn("tail", out)


class TestJsonFenceDataRawKeepsSourceBytes(unittest.TestCase):
    """``data-raw`` restores the fence source, MEDIA token included verbatim."""

    def test_json_fence_with_a_media_key_is_valid_json_again(self):
        source = '{"a":"MEDIA:","b":"x"}'
        out = _render("```json\n" + source + "\n```")
        self.assertEqual(_data_raw(out), source)
        self.assertEqual(json.loads(_data_raw(out)), {"a": "MEDIA:", "b": "x"})

    def test_yaml_fence_with_a_quoted_media_ref_keeps_quotes_and_space(self):
        source = 'p: MEDIA:"/tmp/my screenshot.png"'
        out = _render("```yaml\n" + source + "\n```")
        self.assertEqual(_data_raw(out), source)

    def test_json_fence_with_a_quoted_media_ref_keeps_the_token(self):
        source = '{"p":"MEDIA:\\"/tmp/my screenshot.png\\""}'
        out = _render("```json\n" + source + "\n```")
        self.assertEqual(_data_raw(out), source)

    def test_bare_media_ref_in_json_fence_control(self):
        source = '{"p":"MEDIA:/tmp/a.png"}'
        out = _render("```json\n" + source + "\n```")
        self.assertEqual(_data_raw(out), source)


if __name__ == "__main__":
    unittest.main()
