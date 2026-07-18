"""[[artifact:path|title]] rendering + artifact UI wiring in the WebUI client.

Renderer behavior (node-extracted renderMd, same harness as
test_data_uri_images.py) plus static source checks for the publish button's
feature-flag guard and i18n key coverage.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).parent.parent.resolve()
UI_JS = (REPO_ROOT / "static" / "ui.js").read_text(encoding="utf-8")
I18N_JS = (REPO_ROOT / "static" / "i18n.js").read_text(encoding="utf-8")
BOOT_JS = (REPO_ROOT / "static" / "boot.js").read_text(encoding="utf-8")
NODE = shutil.which("node")

pytestmark = pytest.mark.skipif(NODE is None, reason="node not on PATH")

_DRIVER_SRC = r"""
const fs = require('fs');
const src = fs.readFileSync(process.argv[2], 'utf8');
global.window = {};
global.document = { createElement: () => ({ innerHTML: '', textContent: '' }), baseURI: 'http://127.0.0.1:8787/' };
const esc = s => String(s ?? '').replace(/[&<>"']/g, c => (
  {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const _IMAGE_EXTS=/\.(png|jpg|jpeg|gif|webp|bmp|ico|avif)$/i;
const _SVG_EXTS=/\.svg$/i;
const _AUDIO_EXTS=/\.(mp3|ogg|wav|m4a|aac|flac|wma|opus|webm)$/i;
const _VIDEO_EXTS=/\.(mp4|webm|mkv|mov|avi|ogv|m4v)$/i;
const _PDF_EXTS=/\.pdf$/i;
const _HTML_EXTS=/\.html?$/i;
const _CSV_EXTS=/\.(csv|tsv)$/i;
const _EXCALIDRAW_EXTS=/\.excalidraw$/i;
const _mediaKindForName=(name='')=>{
  const clean=String(name||'').split('?')[0].toLowerCase();
  if(_AUDIO_EXTS.test(clean)) return 'audio';
  if(_VIDEO_EXTS.test(clean)) return 'video';
  if(_IMAGE_EXTS.test(clean)) return 'image';
  return '';
};
const _mediaPlayerHtml=(k,s,n)=>`<${k} src="${esc(s)}"></${k}>`;
const t = k => k;
const S = {};
for (const name of ['_DATA_IMAGE_RE', '_DATA_IMAGE_MAX_LEN']) {
  const m = src.match(new RegExp('const ' + name + '=([^\\n]*);'));
  if (!m) throw new Error(name + ' const not found in ui.js');
  globalThis[name] = eval('(' + m[1] + ')');
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
eval(extractFunc('_dataImageHtml'));
eval(extractFunc('_mdImageHtml'));
eval(extractFunc('_inlineMediaHtmlForRef'));
eval(extractFunc('_matchBacktickFenceLine'));
eval(extractFunc('_isBacktickFenceClose'));
eval(extractFunc('renderMd'));
let buf = '';
process.stdin.on('data', c => { buf += c; });
process.stdin.on('end', () => { process.stdout.write(renderMd(buf)); });
"""


@pytest.fixture(scope="module")
def driver_path(tmp_path_factory):
    path = tmp_path_factory.mktemp("artifact_ui_renderer") / "driver.js"
    path.write_text(_DRIVER_SRC, encoding="utf-8")
    return str(path)


def _render(driver_path: str, markdown: str) -> str:
    result = subprocess.run(
        [NODE, driver_path, str(REPO_ROOT / "static" / "ui.js")],
        input=markdown, capture_output=True, text=True, timeout=30,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr)
    return result.stdout


class TestArtifactTagRendering:
    def test_html_artifact_tag_renders_preview_with_title(self, driver_path):
        html = _render(driver_path, "Fertig: [[artifact:/tmp/report.html|Q3 Report]]")
        assert 'class="html-preview-load"' in html
        assert 'data-artifact="1"' in html
        assert 'data-artifact-title="Q3 Report"' in html
        assert 'data-path="/tmp/report.html"' in html
        assert "[[artifact:" not in html

    def test_html_artifact_tag_without_title(self, driver_path):
        html = _render(driver_path, "[[artifact:/tmp/page.html]]")
        assert 'data-artifact="1"' in html
        assert "data-artifact-title" not in html

    def test_non_html_artifact_tag_routes_to_media_pipeline(self, driver_path):
        html = _render(driver_path, "[[artifact:/tmp/chart.png|Chart]]")
        assert "api/media?path=%2Ftmp%2Fchart.png" in html
        assert "msg-media-img" in html
        assert "[[artifact:" not in html

    def test_title_html_is_escaped(self, driver_path):
        html = _render(driver_path, '[[artifact:/tmp/x.html|<script>alert(1)</script>]]')
        assert "<script" not in html
        assert "&lt;script&gt;" in html

    def test_plain_text_double_brackets_untouched(self, driver_path):
        html = _render(driver_path, "Config uses [[placeholders]] like this")
        assert "[[placeholders]]" in html


class TestArtifactUiWiring:
    def test_publish_button_is_feature_flag_guarded(self):
        assert "window._artifactsEnabled" in UI_JS
        idx = UI_JS.find("artifact-publish-btn")
        assert idx > 0
        guard = UI_JS.rfind("window._artifactsEnabled", 0, idx)
        assert guard > 0 and idx - guard < 400, (
            "the publish button markup must be gated on window._artifactsEnabled"
        )

    def test_boot_propagates_flag_from_settings(self):
        assert "window._artifactsEnabled=!!s.artifacts_enabled" in BOOT_JS

    def test_click_handlers_bound_once_via_guard(self):
        assert "_artifactHandlersBound" in UI_JS
        assert "artifact-copy-btn" in UI_JS
        assert "artifact-revoke-btn" in UI_JS

    def test_i18n_keys_exist_in_en_and_de(self):
        keys = [
            "artifact_publish", "artifact_published", "artifact_publish_failed",
            "artifact_copy", "artifact_copied", "artifact_revoke",
            "artifact_revoked", "artifact_public", "artifacts_title",
            "artifacts_empty",
        ]
        for key in keys:
            occurrences = len(re.findall(rf"^\s*{key}:", I18N_JS, re.M))
            assert occurrences >= 2, f"{key} must exist in at least en+de locales"

    def test_panel_uses_api_endpoints(self):
        assert "/api/artifact/list" in UI_JS
        assert "/api/artifact/publish" in UI_JS
        assert "/api/artifact/revoke" in UI_JS


class TestAuditFixes:
    """Regression coverage for the 18.07.2026 audit findings (UI side)."""

    def test_artifact_tag_inside_fence_stays_literal(self, driver_path):
        md = "Nutze das Tag so:\n```\n[[artifact:/tmp/x.html|Titel]]\n```\nfertig"
        html = _render(driver_path, md)
        assert "html-preview-load" not in html, (
            "artifact tags inside code fences must stay documentation text"
        )
        assert "[[artifact:" in html

    def test_artifact_tag_inside_inline_code_stays_literal(self, driver_path):
        html = _render(driver_path, "Das `[[artifact:/tmp/x.html|T]]` Tag")
        assert "html-preview-load" not in html
        assert "[[artifact:" in html

    def test_handlers_use_api_helper_not_response_json(self):
        start = UI_JS.find("function _bindArtifactHandlers")
        end = UI_JS.find("function renderMermaidBlocks", start)
        body = UI_JS[start:end]
        assert ".json()" not in body, (
            "api() returns decoded JSON already; calling .json() on it crashes"
        )
        panel = UI_JS[UI_JS.find("async function showArtifactsPanel"):end]
        assert ".json()" not in panel

    def test_sandbox_label_survives_artifact_title(self):
        idx = UI_JS.find("const headerLabel=artifactTitle")
        assert idx > 0
        snippet = UI_JS[idx:idx + 400]
        assert "html_sandbox_label" in snippet.split("\n")[0] or (
            "html_sandbox_label" in snippet
        ), "the sandbox trust cue must appear even when a title is set"

    def test_make_public_button_wired(self):
        assert "artifact-public-btn" in UI_JS
        assert "artifact_make_public" in UI_JS
        occurrences = len(re.findall(r"^\s*artifact_make_public:", I18N_JS, re.M))
        assert occurrences >= 2
