"""#3455 — _splitThinkFromContent persist-path regression tests.

The think-block extraction runs at PERSIST time (inflight state + SSE `done`
finalization), moving inline <think>…</think> reasoning out of m.content into
m.reasoning. Because it rewrites persisted assistant content, the critical
invariant is that it NEVER loses real content: content before/after a think
block survives, partial/unclosed blocks are left intact for the live renderer,
and lookalike tags in code are not falsely extracted.

Drives the live JS via Node (same harness style as the #3368/#1188 suites) so
the test exercises the shipped function, not a Python re-implementation.
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
MESSAGES_JS = (REPO / "static" / "messages.js").read_text(encoding="utf-8")


def _extract_block(src: str, marker: str) -> str:
    """Extract a brace-balanced JS block starting at `marker` (a `const x=[` or
    `function name(`)."""
    start = src.index(marker)
    # find first opening bracket of the block ( '[' for the array, '{' for the fn )
    i = start
    while src[i] not in "[{":
        i += 1
    opener = src[i]
    closer = "]" if opener == "[" else "}"
    depth = 0
    j = i
    while j < len(src):
        if src[j] == opener:
            depth += 1
        elif src[j] == closer:
            depth -= 1
            if depth == 0:
                return src[start:j + 1]
        j += 1
    raise AssertionError(f"unbalanced block for {marker!r}")


_DRIVER = """
%s
%s
const args = JSON.parse(process.argv[2]);
process.stdout.write(JSON.stringify(_splitThinkFromContent(args.raw, args.existing || '')));
"""


@pytest.fixture(scope="module")
def driver(tmp_path_factory):
    if shutil.which("node") is None:
        pytest.skip("node not available")
    pairs = _extract_block(MESSAGES_JS, "const _thinkPairs=")
    fn = _extract_block(MESSAGES_JS, "function _splitThinkFromContent(")
    p = tmp_path_factory.mktemp("think3455") / "driver.js"
    p.write_text(_DRIVER % (pairs, fn), encoding="utf-8")
    return str(p)


def _split(driver, raw, existing=""):
    out = subprocess.run(
        ["node", driver, json.dumps({"raw": raw, "existing": existing})],
        capture_output=True, text=True, check=True,
    )
    return json.loads(out.stdout)


def test_plain_content_untouched(driver):
    r = _split(driver, "Hello world, no thinking here.")
    assert r["content"] == "Hello world, no thinking here."
    assert r["reasoning"] == ""


def test_think_at_start_extracted(driver):
    r = _split(driver, "<think>my reasoning</think>The visible answer")
    assert r["content"] == "The visible answer"
    assert r["reasoning"] == "my reasoning"


def test_content_before_think_is_not_extracted(driver):
    """Renderer-matching: a think block is only extracted at the LEADING position.
    A <think> that appears after real prose is, by the renderer's definition,
    visible content and must be left in m.content (not moved to reasoning)."""
    r = _split(driver, "Real prefix <think>mid</think> tail")
    # Not leading -> nothing extracted, content fully preserved.
    assert r["content"] == "Real prefix <think>mid</think> tail"
    assert r["reasoning"] == ""


def test_closed_literal_think_in_code_block_preserved(driver):
    """#3455 review (Codex data-loss): a closed literal <think>...</think> inside
    a fenced code block (visible content, not leading) must NOT be extracted into
    reasoning — the whole-body scan that did this is removed."""
    raw = "```html\n<think>visible literal</think>\n```"
    r = _split(driver, raw)
    assert r["content"] == raw, "fenced-code closed think tag must stay in content"
    assert r["reasoning"] == ""
    assert "visible literal" in r["content"]


def test_unclosed_think_left_intact(driver):
    """Streaming-safe: a partial/unclosed block is not extracted (the live
    renderer hides it); content must not be dropped."""
    r = _split(driver, "<think>still thinking...")
    assert r["content"] == "<think>still thinking..."
    assert r["reasoning"] == ""


def test_existing_reasoning_is_merged_not_overwritten(driver):
    r = _split(driver, "<think>extra</think>answer", existing="from on_reasoning stream")
    assert r["content"] == "answer"
    assert r["reasoning"] == "from on_reasoning stream\n\nextra"


def test_single_leading_block_extracted_matches_renderer(driver):
    """Only ONE leading think block is extracted — matching _streamDisplay/
    _parseStreamState which strip a single leading block. A second consecutive
    block stays in content so persisted state never diverges from the live stream."""
    r = _split(driver, "<think>a</think><think>b</think>the answer")
    assert r["content"] == "<think>b</think>the answer"
    assert r["reasoning"] == "a"


def test_block_after_content_not_extracted(driver):
    """A think block that follows visible content stays in content (renderer only
    strips leading blocks)."""
    r = _split(driver, "<think>lead</think>answer <think>trailing</think> more")
    assert r["content"] == "answer <think>trailing</think> more"
    assert r["reasoning"] == "lead"


def test_lookalike_tag_in_code_not_extracted(driver):
    r = _split(driver, "use <think> as a literal token, never closed")
    assert r["content"] == "use <think> as a literal token, never closed"


def test_empty_content(driver):
    r = _split(driver, "")
    assert r["content"] == ""
    assert r["reasoning"] == ""


def test_think_only_message(driver):
    r = _split(driver, "<think>only thinking</think>")
    assert r["content"] == ""
    assert r["reasoning"] == "only thinking"


# ── Backend parity: api/streaming._split_thinking_from_content ──────────────
# #3455 review (Codex): the split must also run server-side before s.save() so
# the PERSISTED session file is compacted (the client-only split left the saved
# file bloated). The backend helper must match the JS semantics exactly.

class TestBackendThinkSplitParity:
    def _sp(self, raw, existing=""):
        from api.streaming import _split_thinking_from_content
        return _split_thinking_from_content(raw, existing)

    def test_plain_untouched(self):
        assert self._sp("Hello world") == ("Hello world", "")

    def test_leading_extracted(self):
        assert self._sp("<think>r</think>The answer") == ("The answer", "r")

    def test_mid_body_code_block_preserved(self):
        raw = "```html\n<think>visible literal</think>\n```"
        content, reasoning = self._sp(raw)
        assert content == raw
        assert reasoning == ""

    def test_unclosed_left_intact(self):
        assert self._sp("<think>still...") == ("<think>still...", "")

    def test_existing_reasoning_merged(self):
        assert self._sp("<think>new</think>ans", "prior") == ("ans", "prior\n\nnew")

    def test_single_leading_block_only(self):
        assert self._sp("<think>a</think><think>b</think>end") == ("<think>b</think>end", "a")

    def test_empty(self):
        assert self._sp("") == ("", "")

    def test_none_content(self):
        # Defensive: non-string content must not crash.
        content, reasoning = self._sp(None)
        assert content in (None, "")
        assert reasoning == ""
