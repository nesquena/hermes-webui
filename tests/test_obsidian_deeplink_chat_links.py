"""Chat deeplinks must be clickable: obsidian:// survives the markdown renderer.

A vault note is referenced by an `obsidian://open?vault=…&file=…` deeplink. The
renderer's scheme allowlist only knew https/file/workspace/session/mailto/tel/
message, so the deeplink was emitted as plain text and the user could not open
the note from the WebUI. The whitelist now also carries `obsidian:`, and a
custom-scheme anchor is navigated in the same window (a target=_blank tab cannot
hand a navigation to an external protocol handler).
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).parent.parent.resolve()
UI_JS = (REPO_ROOT / "static" / "ui.js").read_text(encoding="utf-8")
MESSAGES_JS = (REPO_ROOT / "static" / "messages.js").read_text(encoding="utf-8")
NODE = shutil.which("node")

pytestmark = pytest.mark.skipif(NODE is None, reason="node not on PATH")

_DRIVER_SRC = r"""
const fs = require('fs');
const src = fs.readFileSync(process.argv[2], 'utf8');
global.window = {};
global.document = { createElement: () => ({ innerHTML: '', textContent: '' }) };
const esc = s => String(s ?? '').replace(/[&<>"']/g, c => (
  {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const _IMAGE_EXTS=/\.(png|jpg|jpeg|gif|webp|bmp|ico|avif)$/i;
const _SVG_EXTS=/\.svg$/i;
const _AUDIO_EXTS=/\.(mp3|ogg|wav|m4a|aac|flac|wma|opus|webm)$/i;
const _VIDEO_EXTS=/\.(mp4|webm|mkv|mov|avi|ogv|m4v)$/i;

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
// eval() here is the established function-slicing harness (see
// test_issue2881_workspace_chat_links.py): the extracted source is this repo's
// own static/ui.js, never user input, and it is the only way to exercise the
// renderer without booting the whole UI.
eval(extractFunc('_matchBacktickFenceLine'));
eval(extractFunc('_isBacktickFenceClose'));
eval(extractFunc('renderMd'));

let buf = '';
process.stdin.on('data', c => { buf += c; });
process.stdin.on('end', () => { process.stdout.write(renderMd(buf)); });
"""

DEEPLINK = "obsidian://open?vault=fedebyes-obsidian&file=ai%2Fhermes-session-flow-2026-11"


@pytest.fixture(scope="module")
def driver_path(tmp_path_factory):
    path = tmp_path_factory.mktemp("obsidian_deeplink_renderer") / "driver.js"
    path.write_text(_DRIVER_SRC, encoding="utf-8")
    return str(path)


def _render(driver_path: str, markdown: str) -> str:
    result = subprocess.run(
        [NODE, driver_path, str(REPO_ROOT / "static" / "ui.js")],
        input=markdown,
        capture_output=True,
        text=True,
        timeout=30,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr)
    return result.stdout


def test_obsidian_deeplink_renders_as_anchor(driver_path):
    html = _render(driver_path, f"[Flow note]({DEEPLINK})")

    assert '<a href="obsidian://open?' in html
    assert ">Flow note</a>" in html


def test_obsidian_anchor_is_not_target_blank(driver_path):
    html = _render(driver_path, f"[Flow note]({DEEPLINK})")

    anchor = html[html.index("<a href=\"obsidian") :]
    anchor = anchor[: anchor.index("</a>") + 4]
    assert "target=" not in anchor


def test_http_anchor_keeps_target_blank(driver_path):
    html = _render(driver_path, "[PR](https://github.com/fedebyes/fedebyes-hub/pull/1128)")

    anchor = html[html.index("<a href=\"https://github") :]
    anchor = anchor[: anchor.index("</a>") + 4]
    assert 'target="_blank"' in anchor


def test_javascript_scheme_stays_blocked(driver_path):
    html = _render(driver_path, "[click](javascript:alert(1))")

    assert '<a href="javascript:' not in html


def test_streaming_renderer_allows_obsidian_scheme():
    assert "obsidian:" in MESSAGES_JS
    assert "_SMD_SAFE_URL_RE=/^(?:https?:|mailto:|tel:|message:|obsidian:" in MESSAGES_JS


def test_ui_allowlist_lists_obsidian_in_both_passes():
    assert UI_JS.count("obsidian:\\/\\/") >= 2, "both markdown link passes must accept the scheme"
    assert "if(/^(mailto:|tel:|message:|obsidian:)/i.test(raw)) return true;" in UI_JS
