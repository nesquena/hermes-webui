"""Bare local file paths render as media via an existence probe.

Deliverable-mode parity: messaging platforms auto-attach bare paths like
``/tmp/chart.png`` mentioned in agent replies; the WebUI treated them as plain
text. Such paths (media-deliverable extensions only) now become probe
placeholders — the path stays visible as text while a 1-byte Range GET asks
``/api/media`` whether the file exists and is allowed. Only a confirmed file
swaps in real media; 403/404/network errors degrade back to plain text, so a
hallucinated or merely-referenced path never produces a broken embed.

Two harnesses, both running the REAL production functions extracted from
``static/ui.js`` rather than a stand-in:

* ``probe_driver`` — the node-extracted ``renderMd`` from
  ``test_data_uri_images.py``, for the markdown pass that emits placeholders.
* ``probe_ctx_driver`` — a minimal DOM plus an instrumented ``fetch`` that
  runs the real ``probeBarePathMedia()`` across successive render contexts.
  This one exists because ``/api/media`` derives authorization from the active
  profile and session: a probe result is only valid inside the context that
  earned it, and a placeholder must never inherit another context's answer.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from tests.test_data_uri_images import _DRIVER_SRC, NODE, _render

REPO_ROOT = Path(__file__).parent.parent.resolve()
UI_JS = (REPO_ROOT / "static" / "ui.js").read_text(encoding="utf-8")

pytestmark = pytest.mark.skipif(NODE is None, reason="node not on PATH")


@pytest.fixture(scope="module")
def probe_driver(tmp_path_factory):
    """Own fixture over the shared driver source.

    Importing the sibling module's `driver_path` fixture would shadow the
    name at every test signature (ruff F811); the driver source itself is
    still shared, so the renderer under test stays the real one.
    """
    path = tmp_path_factory.mktemp("bare_path_renderer") / "driver.js"
    path.write_text(_DRIVER_SRC, encoding="utf-8")
    return str(path)


# ── Context harness ────────────────────────────────────────────────────────
# Drives the real probeBarePathMedia() against a minimal DOM. Scenarios arrive
# as DATA (JSON on stdin) and are interpreted by a fixed control flow, so no
# test input is ever executed as code.

_CTX_DRIVER_SRC = r"""
const fs = require('fs');
const src = fs.readFileSync(process.argv[2], 'utf8');

// ── minimal DOM ───────────────────────────────────────────────────────────
function makeEl(tag){
  return {
    tagName:tag, nodeType:1, attrs:{}, dataset:{}, className:'', innerHTML:'',
    parent:null, children:[],
    get isConnected(){let n=this;while(n.parent)n=n.parent;return n.__root===true;},
    get parentElement(){return this.parent;},
    setAttribute(k,v){this.attrs[k]=String(v);},
    getAttribute(k){return Object.prototype.hasOwnProperty.call(this.attrs,k)?this.attrs[k]:null;},
    append(child){child.parent=this;this.children.push(child);},
    replaceWith(node){
      const p=this.parent; if(!p) return;
      const i=p.children.indexOf(this); if(i<0) return;
      p.children[i]=node; node.parent=p; this.parent=null;
    },
  };
}
function makeText(txt){
  return {nodeType:3, textContent:String(txt), parent:null,
    get isConnected(){let n=this;while(n.parent)n=n.parent;return n.__root===true;}};
}
function makeRoot(){
  const r=makeEl('div'); r.__root=true;
  r.querySelectorAll=(sel)=>{
    if(sel!=='.bare-media-probe:not([data-probed])') throw new Error('unexpected selector: '+sel);
    return r.children.filter(c=>c.nodeType===1&&c.className==='bare-media-probe'
                                &&c.getAttribute('data-probed')===null);
  };
  return r;
}
global.window={};
global.document={createElement:makeEl, createTextNode:makeText, baseURI:'http://127.0.0.1:8787/'};

// ── instrumented fetch ────────────────────────────────────────────────────
let FETCHES=[]; let STATUS={}; let DEFER=false; let PENDING=[];
global.fetch=(url,opts)=>{
  const u=String(url);
  const mp=/[?&]path=([^&]*)/.exec(u); const path=mp?decodeURIComponent(mp[1]):'';
  const ms=/[?&]session_id=([^&]*)/.exec(u); const session=ms?decodeURIComponent(ms[1]):'';
  const range=(opts&&opts.headers)?opts.headers.Range:null;
  FETCHES.push({url:u, path, session, range});
  const byPath=STATUS[path]||{};
  const status=Object.prototype.hasOwnProperty.call(byPath,session)?byPath[session]:404;
  if(status==='error'){
    if(DEFER) return new Promise((_,rej)=>PENDING.push(()=>rej(new Error('network'))));
    return Promise.reject(new Error('network'));
  }
  if(DEFER) return new Promise(res=>PENDING.push(()=>res({status})));
  return Promise.resolve({status});
};

// ── production support the extracted functions call into ──────────────────
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
const S = {session:null, activeProfile:'default'};
// Lazy-load hydrators the swap calls; irrelevant to authorization behaviour.
const loadDiffInline=()=>{}, loadCsvInline=()=>{}, loadPdfInline=()=>{}, loadHtmlInline=()=>{};

for (const name of ['_DATA_IMAGE_RE', '_DATA_IMAGE_SVG_RE', '_DATA_IMAGE_MAX_LEN']) {
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
eval(extractFunc('_isSafeDataImageUri'));
eval(extractFunc('_dataImageHtml'));
eval(extractFunc('_inlineMediaHtmlForRef'));
eval(extractFunc('_barePathProbeContext'));
eval(extractFunc('probeBarePathMedia'));

const tick=()=>new Promise(r=>setTimeout(r,0));
const settle=async()=>{for(let i=0;i<12;i++) await tick();};
function describe(node){
  if(node.nodeType===3) return {kind:'text', text:node.textContent};
  return {kind:'element', className:node.className, html:node.innerHTML,
          path:node.dataset?node.dataset.path:null};
}

let buf='';
process.stdin.on('data', c => { buf += c; });
process.stdin.on('end', async () => {
  const spec=JSON.parse(buf);
  STATUS=spec.status||{};
  const out={steps:[]};
  for(const step of (spec.steps||[])){
    if(step.status) STATUS=step.status;
    FETCHES=[]; PENDING=[]; DEFER=!!step.deferResolve;
    S.activeProfile=step.profile||'default';
    S.session=step.session?{session_id:step.session, workspace:step.workspace||''}:null;
    const root=makeRoot();
    (step.paths||[]).forEach(p=>{
      const el=makeEl('span'); el.className='bare-media-probe'; el.dataset.path=p; root.append(el);
    });
    probeBarePathMedia(root);
    if(step.switchTo){
      await tick();
      const to=step.switchTo;
      S.activeProfile=to.profile||S.activeProfile;
      if(to.session||to.workspace!==undefined){
        S.session={
          session_id: to.session||(S.session?S.session.session_id:''),
          workspace: to.workspace!==undefined?to.workspace:(S.session?S.session.workspace:''),
        };
      }
    }
    if(DEFER){ DEFER=false; PENDING.splice(0).forEach(f=>f()); }
    await settle();
    out.steps.push({
      fetches:FETCHES.map(f=>({path:f.path, session:f.session, range:f.range})),
      nodes:root.children.map(describe),
    });
  }
  process.stdout.write(JSON.stringify(out));
});
"""


@pytest.fixture(scope="module")
def probe_ctx_driver(tmp_path_factory):
    path = tmp_path_factory.mktemp("bare_path_ctx") / "ctx_driver.js"
    path.write_text(_CTX_DRIVER_SRC, encoding="utf-8")
    return str(path)


def _probe(driver: str, spec: dict) -> dict:
    result = subprocess.run(
        [NODE, driver, str(REPO_ROOT / "static" / "ui.js")],
        input=json.dumps(spec),
        capture_output=True,
        text=True,
        timeout=30,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr)
    return json.loads(result.stdout)


CHART = "/tmp/chart.png"


class TestBarePathProbes:
    def test_bare_path_becomes_probe_span(self, probe_driver):
        html = _render(probe_driver, "The chart is at /tmp/chart.png now")
        assert 'class="bare-media-probe"' in html
        assert 'data-path="/tmp/chart.png"' in html
        assert ">/tmp/chart.png</span>" in html, "path must stay visible as text"

    def test_tilde_path_becomes_probe_span(self, probe_driver):
        html = _render(probe_driver, "See ~/ws/report.pdf for details")
        assert 'data-path="~/ws/report.pdf"' in html

    def test_bare_path_in_fence_stays_literal(self, probe_driver):
        html = _render(probe_driver, "```\ncp /tmp/chart.png /dest/\n```")
        assert "bare-media-probe" not in html

    def test_bare_path_in_inline_code_stays_literal(self, probe_driver):
        html = _render(probe_driver, "Use `/tmp/chart.png` as the source")
        assert "bare-media-probe" not in html

    def test_markdown_link_target_not_probed(self, probe_driver):
        html = _render(probe_driver, "[Download](/tmp/chart.png) here")
        assert "bare-media-probe" not in html

    def test_url_paths_not_probed(self, probe_driver):
        html = _render(probe_driver, "Image: https://example.com/img/chart.png done")
        assert "bare-media-probe" not in html

    def test_non_media_extension_not_probed(self, probe_driver):
        html = _render(probe_driver, "Check /etc/nginx/nginx.conf please")
        assert "bare-media-probe" not in html

    def test_media_token_still_renders_directly(self, probe_driver):
        html = _render(probe_driver, "MEDIA:/tmp/chart.png")
        assert "msg-artifact-image" in html or "msg-media-img" in html
        assert "bare-media-probe" not in html

    def test_a_path_at_the_start_of_the_line_is_probed(self, probe_driver):
        """The lead-character capture must not require a preceding character."""
        html = _render(probe_driver, "/tmp/chart.png is ready")
        assert 'data-path="/tmp/chart.png"' in html

    def test_the_lead_character_is_preserved(self, probe_driver):
        """The prefix capture replaces a lookbehind; it must re-emit the char."""
        html = _render(probe_driver, "see: /tmp/chart.png")
        assert "see: " in html


class TestProbeAuthorizationContext:
    """A probe result is only valid in the context that earned it.

    ``/api/media`` authorizes from the active profile and session, so a
    remembered success would let the next context swap in an embed it is not
    allowed to fetch — the broken embed this feature promises to avoid — and a
    remembered failure would pin a path to plain text after it became
    available.
    """

    def test_a_confirmed_path_embeds_in_its_own_context(self, probe_ctx_driver):
        out = _probe(probe_ctx_driver, {
            "status": {CHART: {"A": 206}},
            "steps": [{"profile": "default", "session": "A", "paths": [CHART]}],
        })
        (step,) = out["steps"]
        assert [f["session"] for f in step["fetches"]] == ["A"]
        assert step["fetches"][0]["range"] == "bytes=0-0", (
            "existence check must be a 1-byte range GET"
        )
        assert step["nodes"][0]["kind"] == "element"
        assert step["nodes"][0]["className"] == "bare-media-embed"
        assert "session_id=A" in step["nodes"][0]["html"], "embed must carry its own session"

    def test_a_second_session_reprobes_and_degrades(self, probe_ctx_driver):
        """The exact cross-session reuse the maintainer reproduced."""
        out = _probe(probe_ctx_driver, {
            "status": {CHART: {"A": 206, "B": 403}},
            "steps": [
                {"profile": "default", "session": "A", "paths": [CHART]},
                {"profile": "default", "session": "B", "paths": [CHART]},
            ],
        })
        first, second = out["steps"]
        assert first["nodes"][0]["className"] == "bare-media-embed"
        assert [f["session"] for f in second["fetches"]] == ["B"], (
            "session B must issue its own /api/media probe, not inherit A's result"
        )
        assert second["nodes"][0]["kind"] == "text", (
            "an unauthorized path must degrade to plain text, not render an embed"
        )
        assert second["nodes"][0]["text"] == CHART

    def test_a_second_profile_reprobes_and_degrades(self, probe_ctx_driver):
        """Authorization also moves with the active profile, not just the session."""
        out = _probe(probe_ctx_driver, {
            "steps": [
                {"profile": "default", "session": "A", "paths": [CHART],
                 "status": {CHART: {"A": 206}}},
                {"profile": "work", "session": "A", "paths": [CHART],
                 "status": {CHART: {"A": 403}}},
            ],
        })
        first, second = out["steps"]
        assert first["nodes"][0]["className"] == "bare-media-embed"
        assert len(second["fetches"]) == 1, "a profile switch must re-probe"
        assert second["nodes"][0]["kind"] == "text"

    def test_a_failed_probe_is_not_retained(self, probe_ctx_driver):
        """A path that becomes available later must not stay pinned to text."""
        out = _probe(probe_ctx_driver, {
            "steps": [
                {"profile": "default", "session": "A", "paths": ["/tmp/late.png"],
                 "status": {"/tmp/late.png": {"A": 404}}},
                {"profile": "default", "session": "A", "paths": ["/tmp/late.png"],
                 "status": {"/tmp/late.png": {"A": 206}}},
            ],
        })
        first, second = out["steps"]
        assert first["nodes"][0]["kind"] == "text"
        assert len(second["fetches"]) == 1, "a remembered failure suppressed the re-probe"
        assert second["nodes"][0]["className"] == "bare-media-embed"

    def test_repeated_paths_share_one_probe_within_a_render(self, probe_ctx_driver):
        """De-duplication survives — it is just scoped to the render pass."""
        out = _probe(probe_ctx_driver, {
            "status": {CHART: {"A": 206}},
            "steps": [{"profile": "default", "session": "A", "paths": [CHART, CHART, CHART]}],
        })
        (step,) = out["steps"]
        assert len(step["fetches"]) == 1, "repeated paths in one render must share a probe"
        assert [n["className"] for n in step["nodes"]] == ["bare-media-embed"] * 3

    def test_a_workspace_change_reprobes_and_degrades(self, probe_ctx_driver):
        """`_handle_media()` derives allowed roots from the active workspace.

        Session and profile both stay put while the workspace moves, and a path
        reachable under the old one is refused under the new one.

        What this pins is that no result survives the render context — it fails
        against a cross-context cache, not against a context key missing its
        workspace component. The guard for the key itself is the in-flight test
        below, which is the only case where the key is consulted at all.
        """
        out = _probe(probe_ctx_driver, {
            "steps": [
                {"profile": "default", "session": "A", "workspace": "/ws/x",
                 "paths": [CHART], "status": {CHART: {"A": 206}}},
                {"profile": "default", "session": "A", "workspace": "/ws/y",
                 "paths": [CHART], "status": {CHART: {"A": 403}}},
            ],
        })
        first, second = out["steps"]
        assert first["nodes"][0]["className"] == "bare-media-embed"
        assert len(second["fetches"]) == 1, "a workspace change must re-probe"
        assert second["nodes"][0]["kind"] == "text"

    def test_a_result_landing_after_a_workspace_switch_does_not_embed(self, probe_ctx_driver):
        """The in-flight case for the workspace dimension.

        Session and profile are unchanged throughout, so a fence keyed on those
        alone would let this success through and produce the broken embed.
        """
        out = _probe(probe_ctx_driver, {
            "status": {CHART: {"A": 206}},
            "steps": [{
                "profile": "default", "session": "A", "workspace": "/ws/x",
                "paths": [CHART],
                "deferResolve": True,
                "switchTo": {"workspace": "/ws/y"},
            }],
        })
        (step,) = out["steps"]
        assert [f["session"] for f in step["fetches"]] == ["A"]
        assert step["nodes"][0]["kind"] == "text", (
            "a success authorized under workspace /ws/x must not embed after the "
            "switch to /ws/y"
        )

    def test_a_result_landing_after_a_switch_does_not_embed(self, probe_ctx_driver):
        """In-flight probes carry the OLD authority when the context moves."""
        out = _probe(probe_ctx_driver, {
            "status": {CHART: {"A": 206}},
            "steps": [{
                "profile": "default", "session": "A", "paths": [CHART],
                "deferResolve": True,
                "switchTo": {"profile": "default", "session": "B"},
            }],
        })
        (step,) = out["steps"]
        assert [f["session"] for f in step["fetches"]] == ["A"]
        assert step["nodes"][0]["kind"] == "text", (
            "a success authorized under session A must not embed after the switch to B"
        )

    def test_a_network_error_degrades_to_plain_text(self, probe_ctx_driver):
        out = _probe(probe_ctx_driver, {
            "status": {CHART: {"A": "error"}},
            "steps": [{"profile": "default", "session": "A", "paths": [CHART]}],
        })
        (step,) = out["steps"]
        assert step["nodes"][0]["kind"] == "text"
        assert step["nodes"][0]["text"] == CHART


class TestBarePathProbeWiring:
    def test_the_pass_uses_no_regex_lookbehind(self):
        """Lookbehind is a parse-time brick on Safari < 16.4.

        `tests/test_no_lookbehind_in_static_ui_js` enforces this globally; this
        assertion keeps the reason attached to the pass that once violated it.
        """
        start = UI_JS.index("const bare_probe_stash=[];")
        body = UI_JS[start:start + 900]
        assert "(?<" not in body, "bare-path pass reintroduced a lookbehind"
        assert "(^|[^" in body, "exclusion must be a captured lead character"

    def test_probe_loader_wiring(self):
        assert "function probeBarePathMedia" in UI_JS
        assert "probeBarePathMedia(container);" in UI_JS, (
            "probe loader must run in postProcessRenderedMessages"
        )

    def test_no_probe_result_outlives_its_render_context(self):
        """Structural backstop for `TestProbeAuthorizationContext`.

        The behavioural tests above prove results are not reused across
        contexts. This one keeps the mechanism that made that possible from
        creeping back as a module-level store — which the behavioural tests
        would only catch once it had already been wired into the swap path.
        """
        loader = UI_JS[UI_JS.find("function probeBarePathMedia"):][:2500]
        assert "_barePathProbeCache" not in UI_JS, (
            "a module-scoped probe cache crosses session/profile authorization contexts"
        )
        assert "inflight" in loader, "per-render de-duplication must stay"
