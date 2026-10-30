"""#8228 re-gate (2026-10-20): exclude UNRENDERED image parts, hash everything else whole.

A vision turn keeps the pasted screenshot as an ``image_url`` data URI on the
user message (``api/helpers.py:1593`` exempts it from redaction) and the browser
receives it, but ``msgContent()`` keeps text parts only — the transcript never
draws that part. Hashing the base64 bought no correctness and cost ~13x per
render (35 ms -> 450 ms on 320 messages carrying 8 x 1.44 MB images).

The first repair sampled length + head/tail for every long ``data:`` string,
which broke rendered content: equal-length strings with an unchanged head/tail
and a different MIDDLE produced the same session-cache signature, so
``_sessionHtmlCache`` served stale HTML. The exclusion now lives only on the
message projection (``image`` / ``image_url`` / ``input_image`` parts, never a
mutation of the caller's message) and every other string is hashed in full.

These tests execute the real ``_messageRenderCacheSignature`` helpers extracted
from ``static/ui.js`` under Node — no server, no browser — and drive the
production cache-hit predicate taken verbatim from ``ui.js``.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

NODE = shutil.which("node")

# Lives in tests/ so the sharded CI suite actually runs it; the harness itself is
# pure Node (no server, no browser), it just borrows the repo-relative paths.
_REPO_ROOT = Path(__file__).resolve().parents[1]
UI_JS = (_REPO_ROOT / "static" / "ui.js").read_text(encoding="utf-8")

# The production cache-hit decision, read from ui.js instead of restated here.
# A hard-coded copy would keep passing after the consumer stopped consulting the
# signature; this one changes with the code it is testing.
_PREDICATE = re.search(r"if\((cached&&[^{]*?)\)\{", UI_JS)
assert _PREDICATE, "the _sessionHtmlCache hit predicate was not found in static/ui.js"
_CACHE_HIT_FN = (
    "function _cacheHit(cached,msgCount,renderWindowKey,renderSignature){return ("
    + _PREDICATE.group(1)
    + ");}"
)

_EXTRACT = r"""
function extractFunc(name) {
  const start = src.search(new RegExp('function\\s+' + name + '\\s*\\('));
  if (start < 0) throw new Error(name + ' not found');
  let i = src.indexOf('{', start) + 1, depth = 1;
  while (depth > 0 && i < src.length) { if (src[i] === '{') depth++; else if (src[i] === '}') depth--; i++; }
  return src.slice(start, i);
}
"""

_BODY = r"""
const IMG_A = 'data:image/png;base64,' + 'A'.repeat(1440000);
const IMG_B = 'data:image/png;base64,' + 'B'.repeat(1440000);
const IMG_SMALL = 'data:image/png;base64,' + 'A'.repeat(64);
// Rendered text that merely LOOKS like a data URI. Same length, same first
// 64/last 32 characters, one different character at position 1500 — the exact
// middle-edit case the old sampling shortcut collided on.
const TEXT_A = 'data:text/plain,' + 'A'.repeat(3000);
const TEXT_B = TEXT_A.slice(0, 1500) + 'B' + TEXT_A.slice(1501);
const SNIP_A = 'data:text/plain,' + 'C'.repeat(4000);
const SNIP_B = SNIP_A.slice(0, 2000) + 'D' + SNIP_A.slice(2001);

function userMsg(text, uri) {
  return { role: 'user', content: [{ type: 'text', text: text }, { type: 'image_url', image_url: { url: uri } }] };
}

function state(mut) {
  const S = {
    messages: [
      userMsg('look', IMG_A),
      { role: 'assistant', content: 'a1' },
    ],
    toolCalls: [
      { tid: 'tc1', id: 'tc-m1', name: 'shell', done: true, is_diff: false, assistant_msg_idx: 1, snippet: SNIP_A, args: { cmd: 'cat', data: SNIP_A } },
    ],
    session: { session_id: 'sid', message_count: 2, updated_at: 1, compression_anchor_visible_idx: 0, compression_anchor_summary: '' },
  };
  if (mut) mut(S);
  return S;
}

globalThis.S = state(null);
const base = _messageRenderCacheSignature();
globalThis.S = state(null);
const baseAgain = _messageRenderCacheSignature();

// 1) Cache-hit control: an unchanged state must reuse the cached HTML.
const cacheEntry = { msgCount: 2, renderWindowKey: 'k', signature: base };
const hitUnchanged = _cacheHit(cacheEntry, 2, 'k', baseAgain);

// 2) Swapping the unrendered screenshot must NOT invalidate the cache: the
//    transcript renders the same text parts either way.
globalThis.S = state((S) => { S.messages[0] = userMsg('look', IMG_B); });
const sigImageSwap = _messageRenderCacheSignature();
const hitImageSwap = _cacheHit(cacheEntry, 2, 'k', sigImageSwap);

// 3) A text-part edit BESIDE the large image must invalidate it.
globalThis.S = state((S) => { S.messages[0] = userMsg('look!', IMG_A); });
const sigTextEdit = _messageRenderCacheSignature();
const hitTextEdit = _cacheHit(cacheEntry, 2, 'k', sigTextEdit);

// 4) Rendered plain-text that starts with `data:` — middle-only edit.
globalThis.S = state((S) => { S.messages[0] = userMsg(TEXT_A, null); });
const sigDataTextA = _messageRenderCacheSignature();
globalThis.S = state((S) => { S.messages[0] = userMsg(TEXT_B, null); });
const sigDataTextB = _messageRenderCacheSignature();
const hitDataTextMiddleEdit = _cacheHit(
  { msgCount: 2, renderWindowKey: 'k', signature: sigDataTextA }, 2, 'k', sigDataTextB);

// 5) Tool snippet and nested tool-argument middle-only edits, both data: strings.
globalThis.S = state((S) => { S.toolCalls[0].snippet = SNIP_B; });
const sigSnippetEdit = _messageRenderCacheSignature();
globalThis.S = state((S) => { S.toolCalls[0].args.data = SNIP_B; });
const sigArgEdit = _messageRenderCacheSignature();

// 6) The projection must not mutate the caller's message object.
const probe = userMsg('look', IMG_A);
const before = JSON.stringify(probe);
let sink = 2166136261;
_addMessageRenderHash((v) => { sink = (sink ^ String(v).length) >>> 0; }, probe);
const after = JSON.stringify(probe);

// 7) Cost: a 1.44 MB image must not make the signature scale with its payload.
function timeSig(uri) {
  const t0 = process.hrtime.bigint();
  for (let i = 0; i < 50; i++) { globalThis.S = state((S) => { S.messages[0] = userMsg('look', uri); }); _messageRenderCacheSignature(); }
  return Number(process.hrtime.bigint() - t0) / 1e6 / 50;
}
const msBig = timeSig(IMG_A);
const msSmall = timeSig(IMG_SMALL);

console.log(JSON.stringify({
  deterministic: base === baseAgain,
  hitUnchanged,
  imageSwapSameSignature: base === sigImageSwap,
  hitImageSwap,
  textEditChangesSignature: base !== sigTextEdit,
  hitTextEdit,
  dataTextMiddleEditChangesSignature: sigDataTextA !== sigDataTextB,
  hitDataTextMiddleEdit,
  snippetMiddleEditChangesSignature: base !== sigSnippetEdit,
  argMiddleEditChangesSignature: base !== sigArgEdit,
  projectionMutatedMessage: before !== after,
  msBig, msSmall,
}));
"""


def _run() -> dict:
    assert NODE, "node is required for the render-cache signature harness"
    script = (
        "const window = {};\n"
        f"const src = {json.dumps(UI_JS)};\n"
        f"{_EXTRACT}\n"
        "eval(extractFunc('_messageRenderCacheSignature'));\n"
        "eval(extractFunc('_addMessageRenderHash'));\n"
        "eval(extractFunc('_addBoundedHash'));\n"
        "eval(extractFunc('_hashObjectInto'));\n"
        f"{_CACHE_HIT_FN}\n"
        f"{_BODY}"
    )
    # Write the driver to a temp file: ui.js is ~1.2 MB, and passing it through
    # `node -e` exceeds ARG_MAX. The file also keeps the extracted functions
    # byte-identical to the ones on disk.
    with tempfile.NamedTemporaryFile(
        "w", suffix=".js", delete=False, encoding="utf-8"
    ) as fh:
        fh.write(script)
        script_path = fh.name
    try:
        # A clean environment: this module runs outside the tests/ conftest, and
        # a stray variable injected by the host process would abort subprocess.
        clean_env = {
            k: v
            for k, v in os.environ.items()
            if k and "=" not in k and "\x00" not in v
        }
        result = subprocess.run(
            [NODE, script_path],
            text=True,
            capture_output=True,
            check=False,
            env=clean_env,
        )
    finally:
        os.unlink(script_path)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_unrendered_image_parts_are_excluded_without_mutating_the_message() -> None:
    out = _run()
    assert out["deterministic"], "identical state must yield an identical signature"
    assert out["hitUnchanged"], "an unchanged transcript must reuse the cached HTML"
    assert out["imageSwapSameSignature"], (
        "msgContent() never draws native image parts, so swapping the screenshot "
        "must not invalidate the render cache"
    )
    assert out["hitImageSwap"], (
        "an image swap must still hit the cache — the rendered text is unchanged"
    )
    assert not out["projectionMutatedMessage"], (
        "the image filter must project the message, never mutate the caller's object"
    )


def test_rendered_text_beside_a_large_image_still_invalidates_the_cache() -> None:
    out = _run()
    assert out["textEditChangesSignature"], (
        "a text-part edit beside a large pasted image must change the signature"
    )
    assert not out["hitTextEdit"], (
        "the cache must not serve the pre-edit HTML when the rendered text changed"
    )


def test_middle_only_edits_of_rendered_data_strings_change_the_signature() -> None:
    out = _run()
    assert out["dataTextMiddleEditChangesSignature"], (
        "a same-length middle-only edit of rendered `data:` text must change the "
        "signature (the head/tail sampling shortcut collided here)"
    )
    assert not out["hitDataTextMiddleEdit"], (
        "the production cache consumer must rebuild instead of returning stale HTML"
    )
    assert out["snippetMiddleEditChangesSignature"], (
        "a middle-only edit of a `data:` tool snippet must change the signature"
    )
    assert out["argMiddleEditChangesSignature"], (
        "a middle-only edit of a nested `data:` tool argument must change the signature"
    )


def test_image_payload_length_does_not_drive_signature_cost() -> None:
    out = _run()
    # Whole-payload hashing made the 1.44 MB image ~1000x a short one; with the
    # parts excluded both are flat. The 20x headroom absorbs machine noise while
    # still failing loudly if the exclusion regresses.
    assert out["msBig"] < out["msSmall"] * 20 + 5, (
        "hashing a message with a 1.44 MB image must stay bounded "
        f"(big={out['msBig']:.3f} ms, small={out['msSmall']:.3f} ms)"
    )
