"""Regression tests for issue #9217: list items containing U+2028/U+2029.

Bug: a Markdown list item whose text contains U+2028 (LINE SEPARATOR) or
U+2029 (PARAGRAPH SEPARATOR) is dropped by renderMd(). When it is the first
item of a list the whole list renders as nothing; a later item merges into
the previous one. This happens in the chat transcript and on the share
page, which share the same renderer.

Root cause: _renderListBlock in static/ui.js matched the item text with
`(.*)`:

    const om=line.match(/^(\s*)(\d+)\.\s+(.*)$/);
    const um=line.match(/^(\s*)([-*+])\s+(.*)$/);

In JavaScript `.` does not match U+2028/U+2029, and `$` without the `m`
flag anchors only at the end of the string, so a line containing either
character matches neither pattern. _renderLists has already selected the
line as a list line (its topRe only checks the marker prefix), so the line
falls to the continuation branch, which runs only when an item is already
open; with no item open the line is discarded.

Fix: match the item text with `([^\n]*)` — the lines are already split on
\n by _renderLists, so `[^\n]*` is exactly "the rest of this line",
including any U+2028/U+2029.

These tests drive the ACTUAL renderMd() in static/ui.js via node — the
same harness as test_fullwidth_autolink_behaviour.py — so they exercise
the real production renderer instead of a test-owned copy.
"""
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).parent.parent.resolve()
UI_JS_PATH = REPO_ROOT / "static" / "ui.js"

NODE = shutil.which("node")

pytestmark = pytest.mark.skipif(NODE is None, reason="node not on PATH")


_DRIVER_SRC = r"""
const fs = require('fs');
const src = fs.readFileSync(process.argv[2], 'utf8');
global.window = {};
global.document = { createElement: () => ({ innerHTML: '', textContent: '' }), baseURI: 'http://localhost/app/' };
function _sessionUrlForSid(sid) { return '/app/session/' + encodeURIComponent(String(sid || '')); }
const esc = s => String(s ?? '').replace(/[&<>"']/g, c => (
  {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const _IMAGE_EXTS=/\.(png|jpg|jpeg|gif|webp|bmp|ico|avif)$/i;
const _SVG_EXTS=/\.svg$/i;
const _AUDIO_EXTS=/\.(mp3|ogg|wav|m4a|aac|flac|wma|opus|webm)$/i;
const _VIDEO_EXTS=/\.(mp4|webm|mkv|mov|avi|ogv|m4v)$/i;
function _inlineMediaHtmlForRef(ref){
  const r = String(ref || '');
  if (/^https?:\/\//.test(r)) return `<img class="msg-media-img" src="${esc(r)}" alt="image" loading="lazy">`;
  if (/^file:\/\//i.test(r)){
    const m = r.replace(/^file:\/\//i, '');
    return `<img class="msg-media-img" src="api/media?path=${encodeURIComponent(m)}" alt="image" loading="lazy">`;
  }
  return `<img class="msg-media-img" src="api/media?path=${encodeURIComponent(r)}" alt="image" loading="lazy">`;
}

function extractFunc(name) {
  const re = new RegExp('function\\s+' + name + '\\s*\\(');
  const start = src.search(re);
  if (start < 0) throw new Error(name + ' not found');
  let i = src.indexOf('{', start);
  let depth = 1; i++;
  while (depth > 0 && i < src.length) {
    if (src[i] === '{') depth++;
    else if (src[i] === '}') depth--;
    i++;
  }
  return src.slice(start, i);
}
eval(extractFunc('_matchBacktickFenceLine'));
eval(extractFunc('_isBacktickFenceClose'));
eval(extractFunc('renderMd'));

let buf = '';
process.stdin.setEncoding('utf8');
process.stdin.on('data', c => { buf += c; });
process.stdin.on('end', () => {
  process.stdout.write(renderMd(buf));
});

"""


@pytest.fixture(scope="module")
def driver_path(tmp_path_factory):
    """Write the node driver to a tmp file (works around `node -e` arg quirks)."""
    p = tmp_path_factory.mktemp("list_u2028_driver") / "driver.js"
    p.write_text(_DRIVER_SRC, encoding="utf-8")
    return str(p)


def _render(driver_path, markdown: str) -> str:
    """Run renderMd against the actual ui.js and return the rendered HTML."""
    result = subprocess.run(
        [NODE, driver_path, str(UI_JS_PATH)],
        input=markdown,
        capture_output=True,
        text=True,
        timeout=30,
    )
    if result.returncode != 0:
        raise RuntimeError(f"node driver failed: {result.stderr}")
    return result.stdout


# ── The reported bug shapes (each failed on master before the fix) ─────────

class TestListItemLineParagraphSeparator:
    """renderMd() must keep list items whose text contains U+2028/U+2029."""

    def test_unordered_first_item_with_u2028(self, driver_path):
        """'- see\\u2028more here' rendered as '' (whole list dropped)."""
        out = _render(driver_path, "- see\u2028more here")
        assert out == "<ul><li>see\u2028more here</li></ul>", (
            f"first item with U+2028 must render as a list item. Got: {out!r}"
        )

    def test_ordered_first_item_with_u2028(self, driver_path):
        """'1. see\\u2028more here' rendered as '' (whole list dropped)."""
        out = _render(driver_path, "1. see\u2028more here")
        assert out == '<ol><li value="1">see\u2028more here</li></ol>', (
            f"ordered first item with U+2028 must render. Got: {out!r}"
        )

    def test_item_with_u2029(self, driver_path):
        """'- see\\u2029more here' rendered as '' (whole list dropped)."""
        out = _render(driver_path, "- see\u2029more here")
        assert out == "<ul><li>see\u2029more here</li></ul>", (
            f"item with U+2029 must render. Got: {out!r}"
        )

    def test_later_item_with_u2028_not_merged_into_previous(self, driver_path):
        """'- a\\n- b\\u2028c' merged the second item into the first."""
        out = _render(driver_path, "- a\n- b\u2028c")
        assert out == "<ul><li>a</li><li>b\u2028c</li></ul>", (
            f"each item must stay its own <li>. Got: {out!r}"
        )

    def test_list_between_paragraphs_survives(self, driver_path):
        """'intro\\n\\n- see\\u2028more here\\n\\nafter' lost the whole list."""
        out = _render(driver_path, "intro\n\n- see\u2028more here\n\nafter")
        assert "<p>intro</p>" in out, f"intro paragraph lost. Got: {out!r}"
        assert "<p>after</p>" in out, f"after paragraph lost. Got: {out!r}"
        assert out.count("<ul>") == 1, f"list must survive. Got: {out!r}"
        assert "<li>see\u2028more here</li>" in out, (
            f"list item with U+2028 must render between paragraphs. Got: {out!r}"
        )


# ── Regression guards: the fix must not change ordinary list behaviour ────

class TestOrdinaryListBehaviourUnchanged:
    """Plain lists must render exactly as before the U+2028/U+2029 fix."""

    def test_plain_unordered_list(self, driver_path):
        out = _render(driver_path, "- a\n- b")
        assert out == "<ul><li>a</li><li>b</li></ul>"

    def test_plain_ordered_list(self, driver_path):
        out = _render(driver_path, "1. a\n2. b")
        assert out == '<ol><li value="1">a</li><li value="2">b</li></ol>'

    def test_plain_continuation_line_still_joins_item(self, driver_path):
        out = _render(driver_path, "- a\n  more")
        assert out == "<ul><li>a\nmore</li></ul>"

    def test_nested_list_still_nests(self, driver_path):
        out = _render(driver_path, "- a\n  - b")
        assert out == "<ul><li>a<ul><li>b</li></ul></li></ul>"

    def test_mixed_markers_still_build_nested_tree(self, driver_path):
        out = _render(driver_path, "- a\n  1. b")
        assert out == '<ul><li>a<ol><li value="1">b</li></ol></li></ul>'
