"""Transport/decode budget + nested terminal policy for the inline Markdown preview.

Companion to ``test_md_inline_preview_dom.py`` (same FakeDOM harness, loaded by
path so it works under any pytest import mode).  These tests are behavioural:
they drive the *real* ``loadMarkdownInline()`` extracted from ``static/ui.js``
against a stubbed ``fetch``/Response and assert on what crossed the transport
and on the terminal DOM state.

Covered contract (code-review blockers 2 and 3):
  * the 256 KB preview budget is enforced on *bytes* (UTF-8), not JS string
    length, and it is enforced *before* ``response.text()`` — the response body
    is read through ``Range`` metadata and stopped at the byte budget;
  * a response whose declared ``Content-Range``/``Content-Length`` already
    exceeds the budget is rejected without reading the body at all;
  * nested / self-referential ``.md-inline-load`` placeholders always reach a
    terminal state (no placeholder left at "Loading...").
"""

from __future__ import annotations

import importlib.util
import json
import pathlib
import re

import pytest

_HERE = pathlib.Path(__file__).resolve().parent
_spec = importlib.util.spec_from_file_location(
    "_md_inline_dom_base", _HERE / "test_md_inline_preview_dom.py"
)
base = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(base)

UI_JS_PATH = base.ROOT / "static" / "ui.js"
UI_JS = UI_JS_PATH.read_text(encoding="utf-8")

# 256 KB — asserted as a *byte* budget.  Defaults to the literal the function
# used before the budget was hoisted into a named constant.
CAP = 256 * 1024


def _cap_decl() -> str:
    """Declare every MD_* budget constant found in ui.js as a plain global.

    Only function bodies are extracted from the source file, so top-level
    ``const`` declarations must be replayed here for the extracted functions to
    resolve them.  The value is read from the source (falling back to the raw
    256 KB literal that predated the constant) so the test never hardcodes a
    budget the implementation no longer uses.
    """
    decls = re.findall(r"^const\s+(MD_[A-Z0-9_]+)\s*=\s*([^;\n]+);", UI_JS, re.M)
    lines = [f"var {name} = {expr};" for name, expr in decls if "MAX" in name or "BUDGET" in name]
    if not any("MD_INLINE" in ln for ln in lines):
        lines.insert(0, f"var MD_INLINE_MAX_BYTES = {CAP};")
    return "\n".join(lines) + "\n"


_TRANSPORT_JS = r"""
// ── Transport observability + Response/stream stubs ─────────────────────────
const TRANSPORT = { urls: [], ranges: [], textCalls: 0, bytesRead: 0, reads: 0, cancelled: 0 };
function mdHeaders(map){
  const lower = {};
  Object.keys(map || {}).forEach(k => { lower[String(k).toLowerCase()] = String(map[k]); });
  return { get: (name) => {
    const key = String(name).toLowerCase();
    return Object.prototype.hasOwnProperty.call(lower, key) ? lower[key] : null;
  } };
}
function mdStream(bytes, chunkSize){
  let offset = 0;
  let live = true;
  return { getReader(){ return {
    read(){
      if (!live || offset >= bytes.length) return Promise.resolve({ done: true, value: undefined });
      const slice = bytes.slice(offset, Math.min(offset + chunkSize, bytes.length));
      offset += slice.length;
      TRANSPORT.bytesRead += slice.length;
      TRANSPORT.reads += 1;
      return Promise.resolve({ done: false, value: slice });
    },
    cancel(){ TRANSPORT.cancelled += 1; live = false; return Promise.resolve(); },
  }; } };
}
function mdResponse(opts){
  const o = opts || {};
  const status = o.status === undefined ? 200 : o.status;
  return {
    status: status,
    ok: status >= 200 && status < 300,
    headers: mdHeaders(o.headers || {}),
    body: o.body === undefined ? null : o.body,
    text(){ TRANSPORT.textCalls += 1; return Promise.resolve(o.text === undefined ? '' : o.text); },
  };
}
function mdBytes(text){ return new TextEncoder().encode(text); }
function mdStubFetch(response){
  global.fetch = (url, init) => {
    TRANSPORT.urls.push(String(url));
    const h = init && init.headers ? (init.headers.Range || init.headers.range || null) : null;
    TRANSPORT.ranges.push(h);
    return Promise.resolve(response);
  };
}
"""

_TAIL_JS = r"""
  const contentEl = container.querySelector('.md-inline-content');
  const wrap = container.querySelector('.md-inline-wrap');
  const fallbackEls = container.querySelectorAll('.md-inline-fallback');
  let nestedHref = null;
  for (const fb of fallbackEls) {
    const link = fb.querySelector('.msg-media-link') || fb.querySelector('a');
    if (link) { nestedHref = link.getAttribute('href'); break; }
  }
  console.log(JSON.stringify({
    wrapFound: !!wrap,
    fallbackFound: fallbackEls.length > 0,
    fallbacks: fallbackEls.length,
    stranded: container.querySelectorAll('.md-inline-load').length,
    fetchCount: TRANSPORT.urls.length,
    urls: TRANSPORT.urls,
    ranges: TRANSPORT.ranges,
    textCalls: TRANSPORT.textCalls,
    bytesRead: TRANSPORT.bytesRead,
    reads: TRANSPORT.reads,
    cancelled: TRANSPORT.cancelled,
    renderMdCalls: renderMdCalls.length,
    renderMdText: renderMdCalls.length ? renderMdCalls[0] : null,
    nestedHref: nestedHref,
    mermaidInside: !!(contentEl && contentEl.querySelector('.mermaid-block')),
  }));
"""
_CATCH_JS = "})().catch(e => { console.error(String(e && e.stack || e)); process.exit(1); });\n"


def _scenario(body: str, render_md: str = "", session_id: str = "sess-123",
              path: str = "/tmp/notes.md") -> str:
    globals_js = (
        "eval(extractFunc('_postProcessMdInlineSubtree'));\n"
        "eval(extractFunc('loadMarkdownInline'));\n"
        + base._md_helper_evals(UI_JS)
        + f"S = {{ session: {{ session_id: {json.dumps(session_id)} }} }};\n"
    )
    if render_md:
        globals_js += render_md + "\n"
    container_js = (
        "(async () => {\n"
        "  const container = new FakeElement('div');\n"
        f"  container.innerHTML = '<div class=\"md-inline-load\" data-path=\"{path}\"></div>';\n"
        "  loadMarkdownInline(container);\n"
        "  await new Promise(r => setTimeout(r, 30));\n"
    )
    return (
        base._extract_func_script(UI_JS)
        + "\n" + _cap_decl()
        + base._fakedom_prelude()
        + _TRANSPORT_JS
        + globals_js
        + body
        + container_js
        + _TAIL_JS
        + _CATCH_JS
    )


def _run(scenario: str) -> dict:
    out = base._run_node(scenario)
    return json.loads(out.strip().splitlines()[-1])


def _range_end(rng) -> int:
    assert rng and rng.startswith("bytes=0-"), f"expected a bounded Range request, got {rng!r}"
    return int(rng.split("-", 1)[1])


# ── Blocker 2: the budget bounds transport and decoded bytes ────────────────

def test_multibyte_payload_over_byte_cap_but_under_char_cap_falls_back():
    """200k chars / 400k UTF-8 bytes: a character-length check sees 200k < 256k."""
    payload_js = "'\\u00e9'.repeat(200000)"
    data = _run(_scenario(
        "const PAYLOAD = " + payload_js + ";\n"
        "const BYTES = mdBytes(PAYLOAD);\n"
        "console.error('payload chars=' + PAYLOAD.length + ' bytes=' + BYTES.length);\n"
        "mdStubFetch(mdResponse({ status: 206, body: mdStream(BYTES, 65536), text: PAYLOAD,\n"
        "  headers: { 'content-range': 'bytes 0-' + (BYTES.length - 1) + '/' + BYTES.length,\n"
        "             'content-length': String(BYTES.length) } }));\n"
    ))
    assert data["textCalls"] == 0, "response.text() must never decode the oversize body"
    assert data["fallbackFound"] and not data["wrapFound"], data
    assert data["bytesRead"] <= CAP + 65536, data


def test_declared_range_total_above_cap_short_circuits_without_reading_body():
    """Declared metadata above the budget must be rejected before any read."""
    data = _run(_scenario(
        "mdStubFetch(mdResponse({ status: 206, body: mdStream(mdBytes('x'.repeat(64)), 64),\n"
        "  headers: { 'content-range': 'bytes 0-262144/8388608', 'content-length': '8388608' } }));\n"
    ))
    assert data["textCalls"] == 0 and data["bytesRead"] == 0, data
    assert data["fetchCount"] == 1 and data["fallbackFound"] and not data["wrapFound"], data
    assert _range_end(data["ranges"][0]) in (CAP, CAP + 1), data


def test_chunked_oversize_body_stops_after_byte_budget():
    """No length metadata at all: the reader is stopped at the byte budget."""
    body_js = "const BYTES = mdBytes('y'.repeat(8388608));\n"  # 8 MiB
    data = _run(_scenario(
        body_js
        + "mdStubFetch(mdResponse({ status: 200, body: mdStream(BYTES, 65536) }));\n"
    ))
    assert data["textCalls"] == 0, "oversize body was fully decoded via text()"
    assert data["bytesRead"] <= CAP + 65536, data
    assert data["bytesRead"] > 0, data
    assert data["cancelled"] >= 1, "reader must be cancelled once the budget is exceeded"
    assert data["fallbackFound"] and not data["wrapFound"], data


def test_small_multibyte_body_streamed_in_odd_chunks_renders_exactly():
    """Chunks split multibyte sequences on purpose: decoding must stay exact."""
    payload = "caf\u00e9 \u6f22\u5b57 \u2014 \u65e5\u672c\u8a9e\n" * 40
    data = _run(_scenario(
        "const PAYLOAD = " + json.dumps(payload) + ";\n"
        "const BYTES = mdBytes(PAYLOAD);\n"
        "mdStubFetch(mdResponse({ status: 206, body: mdStream(BYTES, 5), text: PAYLOAD,\n"
        "  headers: { 'content-range': 'bytes 0-' + (BYTES.length - 1) + '/' + BYTES.length } }));\n"
    ))
    assert data["wrapFound"] and not data["fallbackFound"], data
    assert data["renderMdText"] == payload, data
    assert data["textCalls"] == 0, data
    assert _range_end(data["ranges"][0]) in (CAP, CAP + 1), data
    assert data["mermaidInside"], data


def test_range_unsatisfiable_416_renders_empty_preview_without_fallback():
    """416 with ``bytes */0`` (empty file) is metadata, not an error."""
    data = _run(_scenario(
        "mdStubFetch(mdResponse({ status: 416, text: '',\n"
        "  headers: { 'content-range': 'bytes */0' } }));\n"
    ))
    assert data["fetchCount"] == 1 and data["textCalls"] == 0, data
    assert data["bytesRead"] == 0, data
    assert data["wrapFound"] and data["renderMdCalls"] == 1 and data["renderMdText"] == "", data
    assert not data["fallbackFound"], data


# ── Blocker 3: nested / self-referential placeholders reach a terminal state ─

_NESTED_RENDER_JS = r"""
renderMd = function(txt){
  renderMdCalls.push(txt);
  return '<div class="mermaid-block">graph TD; nested</div>' +
         '<div class="md-inline-load" data-path="/tmp/inner.markdown">' +
         '<span class="md-preview-spinner">\u23f3</span> Loading...</div>';
};
"""

_SELF_REF_RENDER_JS = r"""
renderMd = function(txt){
  renderMdCalls.push(txt);
  return '<div class="md-inline-load" data-path="/tmp/loop.md">' +
         '<span class="md-preview-spinner">\u23f3</span> Loading...</div>' +
         '<div class="md-inline-load" data-path="/tmp/inner.md">' +
         '<span class="md-preview-spinner">\u23f3</span> Loading...</div>';
};
"""


def test_nested_markdown_placeholder_terminates_with_session_preserving_fallback():
    data = _run(_scenario(
        "mdStubFetch(mdResponse({ status: 200, text: 'NESTED_MD' }));\n",
        render_md=_NESTED_RENDER_JS,
    ))
    assert data["stranded"] == 0, "nested placeholder left at Loading...: " + json.dumps(data)
    assert data["fallbacks"] == 1, data
    assert data["fetchCount"] == 1, "nested placeholder must not trigger another fetch"
    assert data["renderMdCalls"] == 1, data
    href = data["nestedHref"] or ""
    assert "inner.markdown" in href and "sess-123" in href, href
    assert data["mermaidInside"], data


def test_self_referential_markdown_placeholder_does_not_loop_and_terminates():
    data = _run(_scenario(
        "mdStubFetch(mdResponse({ status: 200, text: 'SELFREF_MD' }));\n",
        render_md=_SELF_REF_RENDER_JS,
    ))
    assert data["stranded"] == 0, "self-referential placeholder left at Loading...: " + json.dumps(data)
    assert data["fallbacks"] == 2, data
    assert data["fetchCount"] == 1, "self-reference re-fetched the outer path: " + json.dumps(data["urls"])
    assert data["renderMdCalls"] == 1, data
