"""The /goal gate refusal tells users how to set a literal "gate ..." goal.

The refusal message is shown twice: as a plain-text toast, and as a chat
transcript message rendered by ``renderMd`` (``**Goal command failed:** <msg>``).
An angle-bracket placeholder such as ``<text>`` survives the toast but the
Markdown renderer consumes it as markup, so the transcript used to read
"use /goal -- ." with the argument gone. The guidance must be plain text that
reads the same on both surfaces.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from api import goals

REPO_ROOT = Path(__file__).parent.parent.resolve()
NODE = shutil.which("node")

_EXPECTED = "use /goal -- followed by your goal text."

_DRIVER_SRC = r"""
const fs = require('fs');
const vm = require('vm');
const src = fs.readFileSync(process.argv[2], 'utf8');
global.window = {};
global.document = { createElement: () => ({ innerHTML: '', textContent: '' }), baseURI: 'http://127.0.0.1:8787/' };
global.esc = s => String(s ?? '').replace(/[&<>"']/g, c => (
  {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
global._mediaKindForName = () => '';
global._mediaPlayerHtml = (k, s) => `<${k} src="${global.esc(s)}"></${k}>`;
global.t = k => k;
global.S = {};
for (const name of ['_IMAGE_EXTS', '_SVG_EXTS', '_AUDIO_EXTS', '_VIDEO_EXTS', '_PDF_EXTS', '_HTML_EXTS',
                    '_CSV_EXTS', '_EXCALIDRAW_EXTS', '_DATA_IMAGE_RE', '_DATA_IMAGE_SVG_RE', '_DATA_IMAGE_MAX_LEN']) {
  const m = src.match(new RegExp('const ' + name + '=([^\\n]*);'));
  if (m) globalThis[name] = vm.runInThisContext('(' + m[1] + ')');
}
function extractFunc(name) {
  const start = src.search(new RegExp('function\\s+' + name + '\\s*\\('));
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
for (const name of ['_isSafeDataImageUri', '_dataImageHtml', '_mdImageHtml', '_inlineMediaHtmlForRef',
                    '_matchBacktickFenceLine', '_isBacktickFenceClose', '_mediaTokenParts', 'renderMd']) {
  vm.runInThisContext(extractFunc(name));
}
let buf = '';
process.stdin.setEncoding('utf8');
process.stdin.on('data', c => { buf += c; });
process.stdin.on('end', () => { process.stdout.write(JSON.stringify({ html: renderMd(buf) })); });
"""


def _refusal_message() -> str:
    return goals.goal_gate_refusal_payload()["message"]


def test_refusal_guidance_is_plain_text_without_a_markup_placeholder():
    message = _refusal_message()
    assert _EXPECTED in message
    assert "<" not in message and ">" not in message


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_rendered_refusal_transcript_keeps_the_literal_goal_guidance(tmp_path):
    driver = tmp_path / "render_driver.js"
    driver.write_text(_DRIVER_SRC, encoding="utf-8")
    # The exact transcript content static/commands.js builds for a refused /goal.
    transcript = "**Goal command failed:** " + _refusal_message()
    result = subprocess.run(
        [NODE, str(driver), str(REPO_ROOT / "static" / "ui.js")],
        input=transcript,
        capture_output=True,
        text=True,
        timeout=30,
        env={"PATH": "/usr/bin:/bin"},
    )
    assert result.returncode == 0, result.stderr
    html = json.loads(result.stdout)["html"]
    assert "/goal -- followed by your goal text." in html
    assert "/goal -- ." not in html
