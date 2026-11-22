"""Regression coverage for PR #6209's markdown-image safety contract.

The settled renderer in ``ui.js`` and the streaming renderer in ``messages.js``
must resolve data-image policy through the same ui-owned predicate.  These tests
execute the real small renderer bodies under Node rather than comparing regex
source text, and pin the observable image markup/safety outcomes.
"""
from __future__ import annotations

import json
import pathlib
import shutil
import subprocess

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
NODE = shutil.which("node")


def _node_contract_cases() -> dict[str, object]:
    script = r"""
const fs = require('fs');
const ui = fs.readFileSync('static/ui.js', 'utf8');
const messages = fs.readFileSync('static/messages.js', 'utf8');
function fn(source, name) {
  const marker = 'function ' + name + '(';
  const start = source.indexOf(marker);
  if (start < 0) throw new Error('missing ' + name);
  const brace = source.indexOf('{', start);
  let depth = 1, pos = brace + 1;
  while (depth && pos < source.length) {
    if (source[pos] === '{') depth++;
    else if (source[pos] === '}') depth--;
    pos++;
  }
  return source.slice(start, pos);
}
function constLine(source, name) {
  const start = source.indexOf('const ' + name + '=');
  if (start < 0) throw new Error('missing ' + name);
  const end = source.indexOf('\n', start);
  return source.slice(start, end < 0 ? source.length : end);
}
function esc(value) {
  return String(value ?? '').replace(/[&<>"']/g, char => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[char]));
}
const _IMAGE_EXTS = /\.(?:png|jpe?g|gif|webp|bmp|ico|avif)$/i;
const _SVG_EXTS = /\.svg$/i;
const _PDF_EXTS = /\.pdf$/i;
const _HTML_EXTS = /\.html?$/i;
const _CSV_EXTS = /\.csv$/i;
const _EXCALIDRAW_EXTS = /\.excalidraw$/i;
function _mediaKindForName(ref) { return /\.(?:png|jpe?g|gif|webp|bmp|ico|avif)$/i.test(ref) ? 'image' : ''; }
function _mediaPlayerHtml() { return ''; }
const S = {session: {session_id: 's1'}};
const t = undefined;
eval(constLine(ui, '_DATA_IMAGE_RE').replace(/^const /, 'var '));
eval(constLine(ui, '_DATA_IMAGE_SVG_RE').replace(/^const /, 'var '));
eval(constLine(ui, '_DATA_IMAGE_MAX_LEN').replace(/^const /, 'var '));
eval(fn(ui, '_isSafeDataImageUri'));
eval(fn(ui, '_dataImageHtml'));
eval(fn(ui, '_inlineMediaHtmlForRef'));
eval(fn(ui, '_mdImageHtml'));
eval(constLine(messages, '_SMD_SAFE_URL_RE').replace(/^const /, 'var '));
eval(constLine(messages, '_SMD_SAFE_IMG_URL_RE').replace(/^const /, 'var '));
eval(fn(messages, '_smdImgSrcAllowed'));
function _smdLinkHref(value) { return String(value || ''); }
function _streamFadeBindCleanup() {}
globalThis.window = {
  smd: {
    HREF: 'href', SRC: 'src',
    default_renderer: () => ({
      add_text() {},
      set_attr(data, attr, value) { data.baseCalls.push({attr, value}); },
    }),
  },
};
eval(fn(messages, '_streamFadeRenderer'));
eval(fn(messages, '_safeSmdRenderer'));
function invoke(factory, value) {
  const node = {
    attributes: {},
    setAttribute(name, attrValue) { this.attributes[name] = String(attrValue); },
    classList: { add() {} },
  };
  const data = { nodes: [node], index: 0, baseCalls: [] };
  factory({}).set_attr(data, window.smd.SRC, value);
  return {baseCalls: data.baseCalls, blocked: node.attributes['data-blocked-scheme'] === '1'};
}
const encodedSvg = 'data:image/svg+xml,%3Csvg%20xmlns%3D%22http%3A%2F%2Fwww.w3.org%2Fsvg%22%3E%3C%2Fsvg%3E';
const html = 'data:text/html;base64,PGgxPkJsb2NrZWQ8L2gxPg==';
const raster = 'data:image/png;base64,iVBORw0KGgo=';
const fileHtml = _mdImageHtml('annotated chart', 'file:///tmp/chart.png');
console.log(JSON.stringify({
  fileHtml,
  settledEncodedSvg: _dataImageHtml(encodedSvg, 'svg') !== null,
  settledRaster: _dataImageHtml(raster, 'chart') !== null,
  fade: { raster: invoke(_streamFadeRenderer, raster), encodedSvg: invoke(_streamFadeRenderer, encodedSvg), html: invoke(_streamFadeRenderer, html) },
  safe: { raster: invoke(_safeSmdRenderer, raster), encodedSvg: invoke(_safeSmdRenderer, encodedSvg), html: invoke(_safeSmdRenderer, html) },
}));
"""
    completed = subprocess.run(
        [NODE, "-e", script],
        cwd=REPO_ROOT,
        capture_output=True,
        check=False,
        text=True,
        timeout=30,
    )
    assert completed.returncode == 0, completed.stderr
    return json.loads(completed.stdout)


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_file_markdown_image_preserves_author_alt_text() -> None:
    cases = _node_contract_cases()
    assert 'alt="annotated chart"' in cases["fileHtml"]


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_url_encoded_svg_and_html_are_blocked_in_actual_stream_hooks() -> None:
    cases = _node_contract_cases()
    assert cases["settledEncodedSvg"] is False
    for mode in ("fade", "safe"):
        for blocked in ("encodedSvg", "html"):
            outcome = cases[mode][blocked]
            assert outcome["baseCalls"] == []
            assert outcome["blocked"] is True


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_base64_raster_reaches_base_set_attr_in_actual_stream_hooks() -> None:
    cases = _node_contract_cases()
    assert cases["settledRaster"] is True
    for mode in ("fade", "safe"):
        outcome = cases[mode]["raster"]
        assert outcome["baseCalls"] == [{"attr": "src", "value": "data:image/png;base64,iVBORw0KGgo="}]
        assert outcome["blocked"] is False
