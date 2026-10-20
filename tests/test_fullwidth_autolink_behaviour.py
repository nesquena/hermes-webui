"""Behavioural tests for the CJK full-width punctuation autolink fix (#6792).

These tests drive the ACTUAL renderMd() in static/ui.js via node — the same
harness used by test_renderer_js_behaviour.py — so they exercise the real
production autolink passes (inlineMd + outer paragraph pass) instead of a
test-owned copy of the regex.

Bug: the autolink regex only excluded ASCII ')' (U+0029) from URL matches.
When a URL appeared before a CJK full-width right parenthesis ）(U+FF09) —
extremely common in Chinese LLM output like "（或 https://lmarena.ai）" —
the ）was swallowed into the <a href="..."> link.

Fix: add \uFF09 to the regex exclusion set AND to the trailing-punctuation
strip. Also covers full-width comma/period/colon/semicolon/exclamation/
question mark/ideographic comma for completeness.

Every case asserts BOTH directions:
- the punctuation never enters href
- the punctuation remains visible after the closing </a>
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
// Minimal stand-in for ui.js' _inlineMediaHtmlForRef used when the driver
// extracts only renderMd(). Mirrors the live UI for the cases the existing
// tests assert against (https image, bare file:// image).
function _inlineMediaHtmlForRef(ref){
  const r = String(ref || '');
  if (/^https?:\/\//.test(r)) return `<img class="msg-media-img" src="${esc(r)}" alt="image" loading="lazy">`;
  if (/^file:\/\//.test(r)){
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
process.stdin.on('data', c => { buf += c; });
process.stdin.on('end', () => {
  const started = process.hrtime.bigint();
  const rendered = renderMd(buf);
  const elapsedMs = Number(process.hrtime.bigint() - started) / 1e6;
  process.stdout.write(rendered);
  if (process.env.RENDER_TIMING === '1') process.stderr.write(String(elapsedMs));
});
"""


@pytest.fixture(scope="module")
def driver_path(tmp_path_factory):
    """Write the node driver to a tmp file (works around `node -e` arg quirks)."""
    p = tmp_path_factory.mktemp("autolink_driver") / "driver.js"
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


def _render_timed(driver_path, markdown: str) -> tuple[str, float]:
    """Return rendered HTML and renderer-only time, excluding Node startup."""
    result = subprocess.run(
        [NODE, driver_path, str(UI_JS_PATH)],
        input=markdown,
        capture_output=True,
        text=True,
        timeout=30,
        env={"PATH": str(Path(NODE).parent), "RENDER_TIMING": "1"},
    )
    if result.returncode != 0:
        raise RuntimeError(f"node driver failed: {result.stderr}")
    return result.stdout, float(result.stderr)


# Every CJK full-width mark the fix adds to the trailing-punctuation strip:
# ）(U+FF09) ，(U+FF0C) 。(U+3002) ；(U+FF1B) ：(U+FF1A) ！(U+FF01) ？(U+FF1F) 、(U+3001)
CJK_TRAILING_MARKS = ["）", "，", "。", "；", "：", "！", "？", "、"]
# ASCII punctuation the existing autolink strip already handled (regression guard).
ASCII_TRAILING_MARKS = [")", ",", ".", ";", ":", "!", "?"]


class TestAutolinkOuterParagraphPass:
    """Plain-paragraph URLs go through the OUTER autolink pass in renderMd()."""

    @pytest.mark.parametrize("mark", CJK_TRAILING_MARKS)
    def test_cjk_trailing_mark_stripped_and_visible_after_anchor(self, driver_path, mark):
        out = _render(driver_path, f"See https://example.com{mark}")
        assert 'href="https://example.com"' in out, (
            f"CJK mark {mark!r} must not enter href. Got: {out!r}"
        )
        assert f'href="https://example.com{mark}"' not in out, (
            f"CJK mark {mark!r} leaked into href. Got: {out!r}"
        )
        assert f"</a>{mark}" in out, (
            f"CJK mark {mark!r} must stay visible after </a>. Got: {out!r}"
        )

    @pytest.mark.parametrize("mark", ASCII_TRAILING_MARKS)
    def test_ascii_trailing_mark_stripped_and_visible_after_anchor(self, driver_path, mark):
        out = _render(driver_path, f"See https://example.com{mark}")
        assert 'href="https://example.com"' in out, (
            f"ASCII mark {mark!r} must not enter href. Got: {out!r}"
        )
        assert f'href="https://example.com{mark}"' not in out
        assert f"</a>{mark}" in out

    def test_fullwidth_paren_wrapped_url(self, driver_path):
        """The original bug shape: （或 https://lmarena.ai）."""
        out = _render(driver_path, "（或 https://lmarena.ai）")
        assert 'href="https://lmarena.ai"' in out
        assert 'href="https://lmarena.ai）"' not in out
        assert "</a>）" in out

    def test_normal_url_with_query_params_untouched(self, driver_path):
        out = _render(driver_path, "https://example.com/path?q=1&x=2")
        # href keeps the raw & (esc() is NOT applied to href — that would
        # corrupt query strings)…
        assert 'href="https://example.com/path?q=1&x=2"' in out
        # …while the visible link text IS escaped (XSS safety).
        assert ">https://example.com/path?q=1&amp;x=2</a>" in out

    def test_markdown_link_not_double_autolinked(self, driver_path):
        """[label](url) must be stashed before autolink so its URL is not re-linked."""
        out = _render(driver_path, "[see docs](https://example.com/doc)")
        assert 'href="https://example.com/doc"' in out
        # The link text must be the label, not a second autolinked URL.
        assert "<a href=\"https://example.com/doc\" target=\"_blank\" rel=\"noopener\">see docs</a>" in out
        assert out.count("<a ") == 1, f"Markdown link must not be double-wrapped. Got: {out!r}"

    def test_preexisting_anchor_not_autolinked(self, driver_path):
        """<a href=...> blocks must be stashed so autolink never runs inside them."""
        out = _render(driver_path, '<a href="https://example.com/pre">pre</a>')
        assert 'href="https://example.com/pre"' in out
        assert out.count("<a ") == 1, f"Existing anchor must not be re-wrapped. Got: {out!r}"


class TestAutolinkInlinePass:
    """List items and blockquotes render through inlineMd() — the INLINE autolink pass."""

    @pytest.mark.parametrize("mark", CJK_TRAILING_MARKS)
    def test_list_item_cjk_mark_stripped(self, driver_path, mark):
        out = _render(driver_path, f"- See https://example.com{mark}")
        assert 'href="https://example.com"' in out, (
            f"Inline pass leaked CJK mark {mark!r} into href. Got: {out!r}"
        )
        assert f'href="https://example.com{mark}"' not in out
        assert f"</a>{mark}" in out

    @pytest.mark.parametrize("mark", CJK_TRAILING_MARKS)
    def test_blockquote_cjk_mark_stripped(self, driver_path, mark):
        out = _render(driver_path, f"> See https://example.com{mark}")
        assert 'href="https://example.com"' in out, (
            f"Inline pass leaked CJK mark {mark!r} into href. Got: {out!r}"
        )
        assert f'href="https://example.com{mark}"' not in out
        assert f"</a>{mark}" in out

    def test_ordered_list_item_cjk_mark_stripped(self, driver_path):
        out = _render(driver_path, "1. See https://example.com！")
        assert 'href="https://example.com"' in out
        assert 'href="https://example.com！"' not in out
        assert "</a>！" in out

    def test_inline_fullwidth_paren_wrapped_url(self, driver_path):
        out = _render(driver_path, "- （或 https://lmarena.ai）")
        assert 'href="https://lmarena.ai"' in out
        assert 'href="https://lmarena.ai）"' not in out
        assert "</a>）" in out

    def test_inline_markdown_link_not_double_autolinked(self, driver_path):
        out = _render(driver_path, "> [link](https://example.com/x)")
        assert 'href="https://example.com/x"' in out
        assert out.count("<a ") == 1, f"Inline markdown link double-wrapped. Got: {out!r}"


# ── Follow-up fix: URL + CJK punctuation + CONTINUED prose ────────────────
#
# The #6792 strip could only drop a single mark sitting at the very END of the
# matched run. When a URL was followed by a full-width mark and then MORE prose
# before the closing mark — e.g. 「（https://example.com/x/，節錄原文）」 — the
# whole 「，節錄原文」 was swallowed into href, because the match never stopped
# at the interior mark. Fix: the match now STOPS at the high-confidence CJK
# sentence marks and closing brackets needed by the report, so the prose stays
# outside the anchor without truncating other valid Unicode IRI characters.
# Reported shape: （https://opencode.ai/docs/go/，節錄原文） and
# （https://opencode.ai/auth；文件也寫用量在那裡看） both clicked through to a
# broken URL.

CJK_CONTINUATION_MARKS = ["，", "。", "；", "：", "！", "？", "、", "）", "】", "」", "》", "〕"]


class TestAutolinkCjkProseContinuation:
    """A CJK mark between a URL and continued prose must end the link."""

    @pytest.mark.parametrize("mark", CJK_CONTINUATION_MARKS)
    def test_outer_pass_mark_terminates_and_prose_stays_visible(self, driver_path, mark):
        out = _render(driver_path, f"See https://example.com/menu{mark}節錄原文）")
        assert 'href="https://example.com/menu"' in out, (
            f"CJK mark {mark!r} must end the URL match. Got: {out!r}"
        )
        assert f'href="https://example.com/menu{mark}' not in out, (
            f"CJK mark {mark!r} leaked into href with following prose. Got: {out!r}"
        )
        assert f"</a>{mark}節錄原文）" in out, (
            f"Prose after {mark!r} must stay visible outside the anchor. Got: {out!r}"
        )

    @pytest.mark.parametrize("mark", ["，", "；", "）", "」"])
    def test_inline_pass_mark_terminates_and_prose_stays_visible(self, driver_path, mark):
        out = _render(driver_path, f"- See https://example.com/menu{mark}後續文字")
        assert 'href="https://example.com/menu"' in out, (
            f"Inline pass: CJK mark {mark!r} must end the URL match. Got: {out!r}"
        )
        assert f'href="https://example.com/menu{mark}' not in out
        assert f"</a>{mark}後續文字" in out

    def test_reported_shape_fullwidth_parens_with_interior_marks(self, driver_path):
        """The reported message shape: （URL，說明） and URL；說明） in one line."""
        out = _render(
            driver_path,
            "來源（https://example.com/docs/go/，節錄原文）以及 https://example.com/auth；文件也寫用量在那裡看）",
        )
        assert 'href="https://example.com/docs/go/"' in out
        assert 'href="https://example.com/auth"' in out
        assert 'href="https://example.com/docs/go/，' not in out
        assert 'href="https://example.com/auth；' not in out
        assert "</a>，節錄原文）" in out
        assert "</a>；文件也寫用量在那裡看）" in out

    @pytest.mark.parametrize("prefix", ["", "- "])
    @pytest.mark.parametrize("dot", ["。", "．", "｡"])
    def test_uts46_dot_variant_remains_in_authority(self, driver_path, prefix, dot):
        url = f"https://example{dot}com/path"
        out = _render(driver_path, prefix + url)
        assert f'href="{url}"' in out
        assert f">{url}</a>" in out

    @pytest.mark.parametrize("prefix", ["", "- "])
    @pytest.mark.parametrize("mark", ["，", "。", "；"])
    def test_raw_cjk_path_preserves_interior_cjk_punctuation(
        self, driver_path, prefix, mark
    ):
        url = f"https://example.com/日本語{mark}続き"
        out = _render(driver_path, prefix + url)
        assert f'href="{url}"' in out
        assert f">{url}</a>" in out

    @pytest.mark.parametrize("prefix", ["", "- "])
    def test_adjacent_fullwidth_parenthesized_urls_stay_separate(
        self, driver_path, prefix
    ):
        first = "https://example.com/a"
        second = "https://example.org/b"
        out = _render(driver_path, f"{prefix}（{first}）（{second}）")
        assert f'href="{first}"' in out
        assert f'href="{second}"' in out
        assert out.count("<a ") == 2
        assert f'href="{first}）（https://' not in out

    @pytest.mark.parametrize("prefix", ["", "- "])
    def test_every_url_after_cjk_boundaries_is_autolinked(self, driver_path, prefix):
        urls = ["https://a.com/x", "https://b.com/y", "https://c.com/z"]
        out = _render(driver_path, prefix + "参见 " + "，".join(urls))
        for url in urls:
            assert f'href="{url}"' in out
        assert out.count("<a ") == 3

    @pytest.mark.parametrize("prefix", ["", "- "])
    @pytest.mark.parametrize(
        "url",
        [
            "https://example.com?q=你好，世界",
            "https://example.com/?q=你好，世界",
            "https://example.com/#section，two",
        ],
    )
    def test_query_and_fragment_keep_interior_cjk_punctuation(
        self, driver_path, prefix, url
    ):
        out = _render(driver_path, prefix + url)
        assert f'href="{url}"' in out
        assert f">{url}</a>" in out

    @pytest.mark.parametrize("prefix", ["", "- "])
    def test_query_mark_before_cjk_prose_ends_link(self, driver_path, prefix):
        out = _render(driver_path, prefix + "https://example.com?q=1，参见")
        assert 'href="https://example.com?q=1"' in out
        assert 'href="https://example.com?q=1，参见"' not in out
        assert "</a>，参见" in out

    @pytest.mark.parametrize("prefix", ["", "- "])
    def test_cjk_path_does_not_override_query_prose_boundary(
        self, driver_path, prefix
    ):
        out = _render(driver_path, prefix + "https://example.com/日本語?q=1，参见")
        assert 'href="https://example.com/日本語?q=1"' in out
        assert 'href="https://example.com/日本語?q=1，参见"' not in out
        assert "</a>，参见" in out

    @pytest.mark.parametrize("prefix", ["", "- "])
    def test_later_slash_does_not_reclassify_authority_full_stop(
        self, driver_path, prefix
    ):
        out = _render(driver_path, prefix + "https://example.com。参见docs/guide.md")
        assert 'href="https://example.com"' in out
        assert 'href="https://example.com。参见docs/guide.md"' not in out
        assert "</a>。参见docs/guide.md" in out

    @pytest.mark.parametrize("prefix", ["", "- "])
    @pytest.mark.parametrize(
        "url", ["https://例子。中国/路径", "https://例え。日本/パス"]
    )
    def test_unicode_idn_labels_remain_linked(self, driver_path, prefix, url):
        out = _render(driver_path, prefix + url)
        assert f'href="{url}"' in out
        assert f">{url}</a>" in out

    @pytest.mark.parametrize("prefix", ["", "- "])
    def test_bare_host_full_stop_before_prose_ends_link(self, driver_path, prefix):
        out = _render(driver_path, prefix + "请访问 https://example.com。然后登录")
        assert 'href="https://example.com"' in out
        assert 'href="https://example.com。然后登录"' not in out
        assert "</a>。然后登录" in out

    @pytest.mark.parametrize("prefix", ["", "- "])
    def test_cjk_ascii_host_full_stop_before_prose_ends_link(
        self, driver_path, prefix
    ):
        out = _render(driver_path, prefix + "https://例子.com。然后登录")
        assert 'href="https://例子.com"' in out
        assert 'href="https://例子.com。然后登录"' not in out
        assert "</a>。然后登录" in out

    def test_long_boundary_run_completes_without_quadratic_scan(self, driver_path):
        def render_size(size):
            markdown = "https://example.com/" + ("a" * size) + "日" + ("，" * size)
            return _render_timed(driver_path, markdown)

        small_out, small_ms = render_size(30000)
        large_out, large_ms = render_size(60000)
        assert 'href="https://example.com/' in small_out
        assert 'href="https://example.com/' in large_out
        assert large_ms <= small_ms * 3 + 25, (
            f"doubling input grew render time from {small_ms:.2f} to {large_ms:.2f} ms"
        )

    def test_thousands_of_urls_do_not_recurse(self, driver_path):
        small_urls = [f"https://example.com/{i}" for i in range(2500)]
        large_urls = [f"https://example.com/{i}" for i in range(5000)]
        _, small_ms = _render_timed(driver_path, "，".join(small_urls))
        out, large_ms = _render_timed(driver_path, "，".join(large_urls))
        assert out.count("<a ") == len(large_urls)
        assert 'href="https://example.com/4999"' in out
        assert large_ms <= small_ms * 3 + 25, (
            f"doubling URL count grew render time from {small_ms:.2f} "
            f"to {large_ms:.2f} ms"
        )

    @pytest.mark.parametrize("iri_char", ["—", "’"])
    def test_outer_pass_preserves_unicode_iri_path_characters(self, driver_path, iri_char):
        url = f"https://example.com/owner{iri_char}s-guide"
        out = _render(driver_path, f"See {url}")
        assert f'href="{url}"' in out, (
            f"Unicode IRI character {iri_char!r} must remain inside a bare URL. Got: {out!r}"
        )
        assert f">{url}</a>" in out

    @pytest.mark.parametrize("iri_char", ["—", "’"])
    def test_inline_pass_preserves_unicode_iri_path_characters(self, driver_path, iri_char):
        url = f"https://example.com/owner{iri_char}s-guide"
        out = _render(driver_path, f"- See {url}")
        assert f'href="{url}"' in out, (
            f"Inline pass must preserve {iri_char!r} in a bare Unicode IRI. Got: {out!r}"
        )
        assert f">{url}</a>" in out

    @pytest.mark.parametrize("iri_char", ["—", "’"])
    def test_explicit_markdown_link_preserves_unicode_iri_path_characters(
        self, driver_path, iri_char
    ):
        url = f"https://example.com/owner{iri_char}s-guide"
        out = _render(driver_path, f"[guide]({url})")
        assert f'href="{url}"' in out
        assert ">guide</a>" in out
        assert out.count("<a ") == 1


# ── Follow-up fix: URL directly followed by a full-width OPENING mark ─────
#
# The #7979 boundary scan stopped only at closing marks and sentence
# punctuation. When a URL was immediately followed by a full-width opening
# mark and an ASCII label — e.g. `…/pull/8040（OPEN、非草稿、5 檔案…` — the
# next stop was the first interior mark, so `（OPEN` stayed glued to href and
# the link clicked through to a broken URL. Fix: the opening marks
# （ (U+FF08), 【 (U+3010), 「 (U+300C), 『 (U+300E) also end the URL when no
# raw CJK path content precedes them; raw-CJK IRI paths keep interior marks
# exactly as before (same guard the closing marks use).
# Reported shape: https://github.com/nesquena/hermes-webui/pull/8040（OPEN、非草稿、5 檔案 +210/-14、MERGEABLE；內文與你核准的稿逐字相同）

CJK_OPENING_MARKS = ["（", "【", "「", "『"]


class TestAutolinkCjkOpeningMarks:
    """A full-width opening mark right after a URL must end the link."""

    @pytest.mark.parametrize("mark", CJK_OPENING_MARKS)
    def test_outer_pass_opening_mark_terminates_and_prose_stays_visible(self, driver_path, mark):
        out = _render(driver_path, f"See https://example.com/x{mark}OPEN、後續說明）")
        assert 'href="https://example.com/x"' in out, (
            f"Opening mark {mark!r} must end the URL match. Got: {out!r}"
        )
        assert f'href="https://example.com/x{mark}' not in out, (
            f"Opening mark {mark!r} leaked into href. Got: {out!r}"
        )
        assert f"</a>{mark}OPEN、後續說明）" in out, (
            f"Prose after {mark!r} must stay visible outside the anchor. Got: {out!r}"
        )

    @pytest.mark.parametrize("mark", ["（", "「"])
    def test_inline_pass_opening_mark_terminates_and_prose_stays_visible(self, driver_path, mark):
        out = _render(driver_path, f"- See https://example.com/x{mark}OPEN、後續")
        assert 'href="https://example.com/x"' in out, (
            f"Inline pass: opening mark {mark!r} must end the URL match. Got: {out!r}"
        )
        assert f'href="https://example.com/x{mark}' not in out
        assert f"</a>{mark}OPEN、後續" in out

    def test_reported_shape_pr_8040_with_ascii_label(self, driver_path):
        """The reported message shape: URL（OPEN、非草稿、5 檔案 +210/-14、…）."""
        out = _render(
            driver_path,
            "https://github.com/nesquena/hermes-webui/pull/8040"
            "（OPEN、非草稿、5 檔案 +210/-14、MERGEABLE；內文與你核准的稿逐字相同）",
        )
        assert 'href="https://github.com/nesquena/hermes-webui/pull/8040"' in out
        assert 'href="https://github.com/nesquena/hermes-webui/pull/8040（' not in out
        assert "</a>（OPEN、非草稿、5 檔案 +210/-14、MERGEABLE；內文與你核准的稿逐字相同）" in out

    def test_raw_cjk_iri_path_keeps_opening_mark(self, driver_path):
        """No regression:（ inside a raw-CJK IRI path must NOT split the URL."""
        out = _render(driver_path, "see https://ja.wikipedia.org/wiki/スター（映画）ok")
        assert 'href="https://ja.wikipedia.org/wiki/スター（映画"' in out
        assert 'href="https://ja.wikipedia.org/wiki/スター"' not in out

    def test_raw_cjk_path_with_open_mark_and_prose_unchanged(self, driver_path):
        """No regression: prose after（ inside a raw-CJK path behaves as before."""
        out = _render(driver_path, "見 https://example.com/日本語（続きはこちら）d")
        assert 'href="https://example.com/日本語（続きはこちら"' in out
        assert 'href="https://example.com/日本語"' not in out


# IDN hosts written with UTS #46 full-stop variants must remain whole in both
# paragraph and inline/list rendering.
REVIEW_IDN_ROWS = [
    ("https://例子。中国", ["https://例子。中国"]),
    ("https://example。中国/路径", ["https://example。中国/路径"]),
    ("https://www。例子。中国/path", ["https://www。例子。中国/path"]),
    ("https://例子。中国。中国/path", ["https://例子。中国。中国/path"]),
    ("https://例子。中国:8080/x", ["https://例子。中国:8080/x"]),
    ("https://例子。中国，https://b.com", ["https://例子。中国", "https://b.com"]),
    ("https://www。例子.com/path", ["https://www。例子.com/path"]),
    ("https://www.例子。中国", ["https://www.例子。中国"]),
    ("https://example。рф", ["https://example。рф"]),
    ("https://example.com/Foo．bar", ["https://example.com/Foo．bar"]),
    ("https://example.com/Foo｡bar", ["https://example.com/Foo｡bar"]),
    ("https://example.com/?q=Foo．bar", ["https://example.com/?q=Foo．bar"]),
    ("https://example.com/?q=Foo｡bar", ["https://example.com/?q=Foo｡bar"]),
    ("https://example.com/#Foo．bar", ["https://example.com/#Foo．bar"]),
    ("https://example.com/#Foo｡bar", ["https://example.com/#Foo｡bar"]),
    # Release gate 2026-10-06 (maintainer follow-up): labels with combining marks
    # (Devanagari, Bengali, Thai vowel signs/tone marks) and supplementary-plane
    # characters, and a Unicode label after an ASCII label joined by ．/｡/。.
    ("https://example。भारत/path", ["https://example。भारत/path"]),
    ("https://भारत。भारत/path", ["https://भारत。भारत/path"]),
    ("https://example．বাংলা", ["https://example．বাংলা"]),
    ("https://example。ตัวอย่าง", ["https://example。ตัวอย่าง"]),
    ("https://example。𠀀𠀁/x", ["https://example。𠀀𠀁/x"]),
    ("https://www.example。рф/path", ["https://www.example。рф/path"]),
    ("https://www.example．рф/path", ["https://www.example．рф/path"]),
    # ．/｡ follow the same rule as 。: CJK after an ASCII label is prose.
    ("https://www.example｡中国/path", ["https://www.example"]),
    # Supplementary-plane Han after a full ASCII host is prose, not a label
    # (_isCjkAutolinkChar covers only the BMP ranges).
    ("https://example.com。𠮷田", ["https://example.com"]),
    ("请访问 https://example.com。𠮷田さんの説明", ["https://example.com"]),
    # Common-script characters whose script extension is CJK (〆, 々, ー) are prose too.
    ("https://example.com。〆切は明日", ["https://example.com"]),
    ("https://example.com。々の説明", ["https://example.com"]),
    # Fullwidth digits/Latin, circled numbers and halfwidth/fullwidth-stop variants of
    # 。 after a full ASCII host are prose, as on the contributor head.
    ("https://example.com。２０２４年４月より", ["https://example.com"]),
    ("https://example.com。①ログイン", ["https://example.com"]),
    ("https://example.com｡然后登录", ["https://example.com"]),
    ("https://example.com．次に進む", ["https://example.com"]),
    ("https://example.com。ㄅㄆㄇ", ["https://example.com"]),
]


class TestAutolinkIdnHostReviewTable:
    @pytest.mark.parametrize("prefix", ["", "- "])
    @pytest.mark.parametrize("markdown,expected", REVIEW_IDN_ROWS)
    def test_idn_host_rows_link_whole(self, driver_path, prefix, markdown, expected):
        import re as _re

        out = _render(driver_path, prefix + markdown)
        hrefs = _re.findall(r'href="([^"]*)"', out)
        assert hrefs == expected, (
            f"{markdown!r} -> {hrefs!r}, expected {expected!r}. Got: {out!r}"
        )
