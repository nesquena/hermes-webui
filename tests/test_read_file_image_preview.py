"""Regression tests for #8772: read_file image previews.

Runs the real tool-card reference helpers in Node, using only harmless media
stubs; CSS and integration routing are verified against the actual source.
"""
from pathlib import Path
import json
import shutil
import subprocess
import pytest

ROOT = Path(__file__).resolve().parent.parent
UI = ROOT / "static" / "ui.js"
CSS = ROOT / "static" / "style.css"
NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(NODE is None, reason="Node unavailable")

_DRIVER = r"""
'use strict';
const fs=require('fs');
const src=fs.readFileSync(process.argv[2], 'utf8');
function extract(name) {
  const start=src.indexOf('function '+name+'(');
  if(start<0) throw Error(name+' not found');
  const next=src.indexOf('\nfunction ',start+12);
  if(next<0) throw Error('next function not found: '+name);
  return src.slice(start,next);
}
function _redactToolTargetLabel(s) { return s.replace(/([?&]token=)[^&]+/,'$1[redacted]'); }
function _inlineMediaHtmlForRef(ref) {
  return '<img class="msg-media-img" src="api/media?path='+encodeURIComponent(ref)+'">';
}
const code=extract('_readFileImageRef')+extract('_readFileImagePreviewHtml')
  +extract('_toolCardAllowsDetail');
const funcs=Function('_redactToolTargetLabel','_inlineMediaHtmlForRef','document',
  code+'\nreturn {_readFileImageRef,_readFileImagePreviewHtml,_toolCardAllowsDetail};'
)(_redactToolTargetLabel,_inlineMediaHtmlForRef,{addEventListener(){}});
const {_readFileImageRef,_readFileImagePreviewHtml,_toolCardAllowsDetail}=funcs;
let buf='';
process.stdin.on('data', chunk=>buf+=chunk);
process.stdin.on('end',()=>{
  const tc=JSON.parse(buf);
  process.stdout.write(JSON.stringify({
    path:_readFileImageRef(tc),
    html:_readFileImagePreviewHtml(tc),
    detail:_toolCardAllowsDetail('read',tc)
  }));
});
"""

@pytest.fixture(scope="module")
def node_driver(tmp_path_factory):
    p = tmp_path_factory.mktemp("read_file_image_preview") / "driver.js"
    p.write_text(_DRIVER, encoding="utf-8")
    return p


def run_card(node_driver, tc):
    result = subprocess.run(
        [NODE, str(node_driver), str(UI)],
        input=json.dumps(tc), text=True, capture_output=True, timeout=20,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


@pytest.mark.parametrize("tool_name", ["read_file", "mcp__hermes__read_file"])
@pytest.mark.parametrize("extension", ["png", "jpg", "jpeg", "webp", "gif", "bmp", "ico"])
def test_image_read_has_expanded_media_preview(node_driver, tool_name, extension):
    path=f"/tmp/rendered image.{extension}"
    result=run_card(node_driver, {"name":tool_name, "done":True, "args":{"path":path}})
    assert result["path"] == path
    assert result["detail"] is True
    assert "tool-card-image-preview" in result["html"]
    assert "api/media?path=" in result["html"]


@pytest.mark.parametrize("path", [
    "rendered.png", "./rendered.png", "//server/share/rendered.png",
    "file:///tmp/rendered.png", "https://example.org/rendered.png",
    "/tmp/data.txt", "/tmp/rendered.svg", "/tmp/rendered.png?token=secret",
    "/tmp/rendered.png#fragment", "/tmp/rendered.png...",
    "/tmp/rendered.png\nsecond line", "/tmp/rendered.png\0",
])
def test_unknown_or_unsafe_paths_fail_closed(node_driver, path):
    result=run_card(node_driver, {"name":"read_file", "done":True, "args":{"path":path}})
    assert not result["path"]
    assert not result["html"]
    assert result["detail"] is False


@pytest.mark.parametrize("tc", [
    {"name":"write_file", "done":True, "args":{"path":"/tmp/output.png"}},
    {"name":"mcp__other__read_file", "done":True, "args":{"path":"/tmp/output.png"}},
    {"name":"read_file", "done":False, "args":{"path":"/tmp/output.png"}},
])
def test_other_tools_and_incomplete_reads_have_no_preview(node_driver, tc):
    result=run_card(node_driver, tc)
    assert not result["path"]
    assert not result["html"]


def test_binary_read_error_can_still_offer_authorized_media_preview(node_driver):
    tc={"name":"read_file", "done":True, "is_error":True,
        "snippet":"Cannot decode binary image as text",
        "args":{"file_path":"/tmp/image.png"}}
    result=run_card(node_driver, tc)
    assert result["path"] == "/tmp/image.png"
    assert "api/media?path=" in result["html"]


def test_long_paths_remain_bounded(node_driver):
    result=run_card(node_driver, {"name":"read_file", "done":True,
                                 "args":{"path":"/tmp/"+("x"*4001)+".png"}})
    assert not result["path"]


def test_tool_card_uses_existing_media_renderer_and_retains_result():
    js=UI.read_text(encoding="utf-8")
    assert "const imagePreviewHtml=_readFileImagePreviewHtml(tc);" in js
    assert "_inlineMediaHtmlForRef(ref)" in js
    assert "target.matches('.tool-card-image-preview img')" in js
    assert "if(_readFileImageRef(tc)) return true;" in js
    assert "const rebuilt=buildToolCard(tc);" in js


def test_collapsed_cards_and_output_only_tab_do_not_render_preview():
    css=CSS.read_text(encoding="utf-8")
    assert ".tool-card:not(.open) .tool-card-image-preview{display:none;}" in css
    assert 'data-transparent-detail-mode="output"] .tool-card-image-preview{display:none;}' in css
    assert "max-height:180px" in css


def test_lightbox_excludes_tool_previews_from_message_gallery():
    js=UI.read_text(encoding="utf-8")
    assert ".filter(image=>!image.closest('.tool-card-image-preview'))" in js
    assert "!imgEl.closest('.tool-card-image-preview')" in js
