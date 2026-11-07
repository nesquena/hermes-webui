"""MEDIA: token grammar tolerates one optional space after the prefix.

The agent-facing convention emits ``MEDIA: <path>`` (with a space, matching
the documented tool-output format), but every scan site required a
non-whitespace byte immediately after the colon, so the spaced form rendered
as literal text in the WebUI and was missed by the server-side scans. This
file pins the relaxed grammar — ONE optional literal space, nothing more —
at every capture site:

- ``static/ui.js``        settled-path stash + TTS flattener (behavioural, node)
- ``static/messages.js``  streaming chunk-finalize / combined-walk / partial-tail
- ``api/routes.py``       ``_MEDIA_TOKEN_RE`` (compiled from live source)
- ``api/media_snapshots.py`` ``media_re``      (compiled from live source)

Deliberately unchanged:
- the tight form ``MEDIA:<path>`` keeps matching exactly as before;
- a NEWLINE after the prefix never joins (single literal space only);
- two spaces stay prose — the token grammar is reserved-word permissive,
  not a path validator (see #7359 for why the bare class stays permissive);
- the backtick-wrapper pre-pass keeps its tight ```MEDIA:path`` shape, so a
  backtick-wrapped spaced token degrades to literal text (documented trade-off).
"""
from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).parent.parent.resolve()
NODE = shutil.which("node")

pytestmark = pytest.mark.skipif(NODE is None, reason="node not on PATH")

MESSAGES_JS = (REPO_ROOT / "static" / "messages.js").read_text(encoding="utf-8")
UI_JS = (REPO_ROOT / "static" / "ui.js").read_text(encoding="utf-8")
ROUTES_PY = (REPO_ROOT / "api" / "routes.py").read_text(encoding="utf-8")
SNAPSHOTS_PY = (REPO_ROOT / "api" / "media_snapshots.py").read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Settled-path render (ui.js renderMd MEDIA stash) — real pipeline via node
# ---------------------------------------------------------------------------

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

// The data-image consts and predicate are production code. Extract/eval them
// in source order so the renderer functions run against the REAL policy, not
// a test stand-in.
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
eval(extractFunc('_mdImageHtml'));
eval(extractFunc('_inlineMediaHtmlForRef'));
eval(extractFunc('_matchBacktickFenceLine'));
eval(extractFunc('_isBacktickFenceClose'));
eval(extractFunc('_mediaTokenParts'));
eval(extractFunc('renderMd'));

let buf = '';
process.stdin.on('data', c => { buf += c; });
process.stdin.on('end', () => { process.stdout.write(renderMd(buf)); });
"""


@pytest.fixture(scope="module")
def driver_path(tmp_path_factory):
    path = tmp_path_factory.mktemp("media_space_renderer") / "driver.js"
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


def test_spaced_media_token_renders_img(driver_path):
    """``MEDIA: /tmp/chart.png`` (one space) must render the inline media card."""
    html = _render(driver_path, "MEDIA: /tmp/chart.png")
    assert "api/media?path=%2Ftmp%2Fchart.png" in html
    assert 'class="msg-media-img"' in html


def test_spaced_media_token_inside_prose_renders_card_and_prose(driver_path):
    html = _render(driver_path, "Here you go.\n\nMEDIA: /tmp/chart.png\n\nEnjoy.")
    assert "api/media?path=%2Ftmp%2Fchart.png" in html
    assert "Here you go." in html
    assert "Enjoy." in html


def test_tight_media_token_unchanged(driver_path):
    """Backward-compat: the tight form keeps rendering identically."""
    html = _render(driver_path, "MEDIA:/tmp/chart.png")
    assert "api/media?path=%2Ftmp%2Fchart.png" in html
    assert 'class="msg-media-img"' in html


def test_two_spaces_stay_literal_text(driver_path):
    """Two spaces after the prefix is prose, not a token (single space only)."""
    html = _render(driver_path, "Supported tokens: MEDIA:  and file:// links")
    assert "api/media?path=and" not in html


def test_newline_after_prefix_stays_literal_text(driver_path):
    """A newline after the prefix never joins the token."""
    html = _render(driver_path, "Tokens:\nMEDIA:\n/tmp/chart.png")
    assert "api/media?path=%2Ftmp%2Fchart.png" not in html


# ---------------------------------------------------------------------------
# Streaming grammar (messages.js) — the three capture regexes, extracted
# from the live source and driven behaviourally.
# ---------------------------------------------------------------------------

def _media_regex_literals(src: str) -> list[str]:
    """Every MEDIA: regex literal in a JS source string, verbatim."""
    return re.findall(r"/\^?MEDIA:[^/\n]+/[a-z]*", src)


def test_messages_js_carries_three_relaxed_sites():
    literals = _media_regex_literals(MESSAGES_JS)
    relaxed = [lit for lit in literals if "MEDIA: ?" in lit]
    assert len(literals) == 3, f"expected 3 MEDIA: regex sites in messages.js, got {literals}"
    assert len(relaxed) == 3, f"all messages.js sites must tolerate the optional space, got {literals}"


def test_ui_js_carries_two_relaxed_sites():
    literals = _media_regex_literals(UI_JS)
    relaxed = [lit for lit in literals if "MEDIA: ?" in lit]
    assert len(literals) == 2, f"expected 2 MEDIA: regex sites in ui.js, got {literals}"
    assert len(relaxed) == 2, f"all ui.js sites must tolerate the optional space, got {literals}"


@pytest.fixture(scope="module")
def messages_regexes() -> list[re.Pattern]:
    """The live messages.js MEDIA regexes, compiled for behaviour probes.

    The JS classes used here (non-whitespace/paren/bracket exclusions and a
    literal optional space) are valid Python regex syntax, so the compiled
    patterns behave identically for the grammar assertions below.
    """
    pairs = []
    for lit in _media_regex_literals(MESSAGES_JS):
        body, _flags = lit.rsplit("/", 1)
        pairs.append((lit, re.compile(body.strip("/"))))
    return pairs


def _select(pairs, kind):
    """kind: 'walk' (combined /g walk), 'tail' (partial-tail probe)."""
    for lit, pat in pairs:
        if kind == "walk" and lit.startswith("/MEDIA:") and lit.endswith("/g"):
            return pat
        if kind == "tail" and lit.endswith("*$"+"/"):
            return pat
    raise AssertionError(f"no {kind} regex among {[lit for lit, _ in pairs]}")


def test_stream_walk_captures_spaced_ref_without_the_space(messages_regexes):
    walk = _select(messages_regexes, "walk")
    m = walk.search("Here you go. MEDIA: /tmp/chart.png")
    assert m is not None, "spaced token must match the combined walk"
    assert m.group(1) == "/tmp/chart.png", "the space must never enter the captured ref"
    assert "MEDIA: " not in m.group(1)


def test_stream_walk_still_captures_tight_ref(messages_regexes):
    walk = _select(messages_regexes, "walk")
    m = walk.search("MEDIA:/tmp/chart.png")
    assert m is not None
    assert m.group(1) == "/tmp/chart.png"


def test_stream_tail_form_buffers_prefix_plus_space(messages_regexes):
    """A chunk ending in 'MEDIA: ' (prefix + space, no ref yet) must be held
    by the partial-tail buffer so the next chunk can complete the token."""
    tail = _select(messages_regexes, "tail")
    assert tail.search("see MEDIA: ") is not None, "prefix + space must buffer"
    assert tail.search("see MEDIA:") is not None, "bare prefix must still buffer"
    assert tail.search("all done") is None


def test_stream_grammar_rejects_newline_join(messages_regexes):
    walk = _select(messages_regexes, "walk")
    assert walk.search("MEDIA:\n/tmp/chart.png") is None


# ---------------------------------------------------------------------------
# TTS flattener (ui.js) — spaced tokens must be flattened from spoken text.
# ---------------------------------------------------------------------------

def test_tts_flattener_removes_spaced_token():
    flat = re.findall(r"/MEDIA:[^/\n]*/[a-z]*", UI_JS)
    tts = [lit for lit in flat if "[^\\s]+/" in lit]
    assert len(tts) == 1, f"expected exactly one TTS flattener site, got {tts}"
    pattern = re.compile(tts[0].rsplit("/", 1)[0].strip("/"))
    spoken = pattern.sub("a file", "your chart is at MEDIA: /tmp/chart.png enjoy")
    assert "MEDIA:" not in spoken
    assert "/tmp/chart.png" not in spoken


# ---------------------------------------------------------------------------
# Python capture sites — compiled from the live source, issue-7359 style.
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def routes_media_re() -> re.Pattern:
    m = re.search(r'_MEDIA_TOKEN_RE = re\.compile\(r"([^"]+)"\)', ROUTES_PY, re.M)
    assert m, "_MEDIA_TOKEN_RE compile() not found in api/routes.py"
    return re.compile(m.group(1))


@pytest.fixture(scope="module")
def snapshots_media_re() -> re.Pattern:
    m = re.search(r'media_re = _re\.compile\(r"([^"]+)"\)', SNAPSHOTS_PY, re.M)
    assert m, "media_re compile() not found in api/media_snapshots.py"
    return re.compile(m.group(1))


def test_routes_re_captures_spaced_ref(routes_media_re):
    assert routes_media_re.findall("MEDIA: /tmp/report.pdf") == ["/tmp/report.pdf"]


def test_routes_re_tight_form_unchanged(routes_media_re):
    assert routes_media_re.findall("MEDIA:/tmp/report.pdf") == ["/tmp/report.pdf"]


def test_routes_re_rejects_newline_join(routes_media_re):
    assert routes_media_re.findall("MEDIA:\n/tmp/report.pdf") == []


def test_snapshots_re_captures_spaced_ref(snapshots_media_re):
    assert snapshots_media_re.findall("MEDIA: /tmp/x.png") == ["/tmp/x.png"]


def test_snapshots_re_tight_form_unchanged(snapshots_media_re):
    assert snapshots_media_re.findall("MEDIA:/tmp/x.png") == ["/tmp/x.png"]
