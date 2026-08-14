from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

UI_JS = Path("static/ui.js").read_text(encoding="utf-8")
NODE = shutil.which("node")


def test_session_html_cache_uses_render_signature_not_only_count():
    assert "function _messageRenderCacheSignature()" in UI_JS
    assert "const renderSignature=_messageRenderCacheSignature();" in UI_JS
    assert "cached.signature===renderSignature" in UI_JS
    assert "signature:renderSignature" in UI_JS


def test_render_signature_tracks_message_content_and_settled_tool_cards():
    signature_fn = UI_JS[UI_JS.index("function _messageRenderCacheSignature()"):UI_JS.index("function _clipCliToolSnippet")]
    assert "msgContent(m)" in signature_fn
    assert "m.tool_calls" in signature_fn
    assert "m._partial_tool_calls" in signature_fn
    assert "S.toolCalls" in signature_fn
    assert "tc.snippet" in signature_fn
    assert "compression_anchor_summary" in signature_fn


def test_documentation_no_longer_allows_same_count_stale_html():
    assert "Known limitation: cache key is session_id + message count" not in UI_JS
    assert "mutate message content without changing the count will serve stale HTML" not in UI_JS


# ── #6999: same-length MIDDLE-only mutations must change the signature ──────
# The first #6999 iteration hashed structured fields with length+head+tail
# (_addBoundedHash). Same-length edits in the MIDDLE of tool arguments,
# attachment metadata, tool snippets, or compression-anchor keys produced an
# identical signature, and _sessionHtmlCache treats signature equality as
# authority — a deterministic stale-cache collision on cross-session
# navigation. These tests execute the real _messageRenderCacheSignature()
# (plus its helpers) under Node and prove that a middle-only, same-length
# mutation of every structured field changes the signature, while the old
# clip scheme demonstrably collides on the very same data.


def _function_source(name: str) -> str:
    """Return the full ``function name(...) {...}`` source from static/ui.js."""
    marker = f"function {name}"
    start = UI_JS.find(marker)
    assert start != -1, f"{name} not found in static/ui.js"
    params = UI_JS.find("(", start)
    assert params != -1, f"{name} has no parameter list"
    depth = 0
    close = -1
    for idx in range(params, len(UI_JS)):
        if UI_JS[idx] == "(":
            depth += 1
        elif UI_JS[idx] == ")":
            depth -= 1
            if depth == 0:
                close = idx
                break
    assert close != -1, f"{name} parameter list did not close"
    brace = UI_JS.find("{", close)
    assert brace != -1, f"{name} has no body"
    depth = 0
    for idx in range(brace, len(UI_JS)):
        if UI_JS[idx] == "{":
            depth += 1
        elif UI_JS[idx] == "}":
            depth -= 1
            if depth == 0:
                return UI_JS[start : idx + 1]
    raise AssertionError(f"{name} body did not close")


_FN_DECL_RE = re.compile(r"\bfunction\s+([A-Za-z_$][\w$]*)\s*\(")


def _collect_functions(names: list[str]) -> str:
    """Concatenate the sources of ``names`` plus every ``function X(`` dep."""
    out: dict[str, str] = {}
    stack = list(names)
    seen: set[str] = set()
    while stack:
        name = stack.pop()
        if name in seen:
            continue
        seen.add(name)
        src = _function_source(name)
        out[name] = src
        for dep in _FN_DECL_RE.findall(src):
            if dep not in seen:
                stack.append(dep)
    return "\n".join(out.values())


_SIGNATURE_NODE_BODY = r"""
const LONG = 'A'.repeat(5000);
// Same-length middle-only mutation of LONG: byte-for-byte identical head and
// tail, one different character at position 2500.
const MID = LONG.slice(0, 2500) + 'B' + LONG.slice(2501);
// Snippets used a 4096-char clip window: the mutation must sit outside BOTH
// the 4096 head and the 4096 tail, so the field has to exceed 8192 chars.
const LONG2 = 'C'.repeat(20000);
const MID2 = LONG2.slice(0, 10000) + 'D' + LONG2.slice(10001);
function state(mutator) {
  const S = {
    messages: [
      {role: 'user', content: 'u1'},
      {role: 'assistant', content: 'a1', tool_calls: [{id: 'tc-m1', type: 'function', function: {name: 'shell', arguments: LONG}}]},
      {role: 'assistant', content: 'a2', attachments: [{name: 'report.pdf', data: LONG}]},
      {role: 'user', content: 'u2'},
      {role: 'assistant', content: 'a3'},
    ],
    toolCalls: [{tid: 'tc1', id: 'tc-m1', name: 'shell', done: true, is_diff: false, assistant_msg_idx: 1, snippet: LONG2, args: {cmd: 'cat', data: LONG}}],
    session: {session_id: 'sid', message_count: 5, updated_at: 111, compression_anchor_visible_idx: 0, compression_anchor_message_key: {role: 'assistant', ts: 123, text: LONG, attachments: 1}, compression_anchor_summary: ''},
  };
  if (mutator) mutator(S);
  return S;
}
// The pre-#6999 clip scheme: length + head + tail of the string form.
function oldBoundedHash(value, maxLen) {
  const limit = Math.max(64, Number(maxLen) || 512);
  let s = '';
  try { s = (value == null) ? '' : String(value); } catch (e) { s = ''; }
  let h = 2166136261;
  const add = (v) => {
    const st = String(v == null ? '' : v);
    for (let i = 0; i < st.length; i++) { h ^= st.charCodeAt(i); h = Math.imul(h, 16777619) >>> 0; }
    h ^= 31; h = Math.imul(h, 16777619) >>> 0;
  };
  add(s.length);
  if (s.length > limit) { add(s.slice(0, limit)); add(s.slice(-limit)); } else { add(s); }
  return h;
}
globalThis.S = state(null);
const base = _messageRenderCacheSignature();
const baseAgain = _messageRenderCacheSignature();

// 1) middle-only edit of a message tool-call's function.arguments string.
globalThis.S = state((S) => { S.messages[1].tool_calls[0].function.arguments = MID; });
const sigArgsMut = _messageRenderCacheSignature();
// 2) middle-only edit inside a settled tool call's args object.
globalThis.S = state((S) => { S.toolCalls[0].args.data = MID; });
const sigArgsObjMut = _messageRenderCacheSignature();
// 3) middle-only edit inside an attachment object.
globalThis.S = state((S) => { S.messages[2].attachments[0].data = MID; });
const sigAttachMut = _messageRenderCacheSignature();
// 4) middle-only edit inside the compression-anchor message key.
globalThis.S = state((S) => { S.session.compression_anchor_message_key.text = MID; });
const sigAnchorMut = _messageRenderCacheSignature();
// 5) middle-only edit of a settled tool snippet (20000 chars, 4096 clip window).
globalThis.S = state((S) => { S.toolCalls[0].snippet = MID2; });
const sigSnippetMut = _messageRenderCacheSignature();

console.log(JSON.stringify({
  deterministic: base === baseAgain,
  differArgs: base !== sigArgsMut,
  differArgsObj: base !== sigArgsObjMut,
  differAttach: base !== sigAttachMut,
  differAnchor: base !== sigAnchorMut,
  differSnippet: base !== sigSnippetMut,
  oldCollidesArgs: oldBoundedHash(LONG, 2048) === oldBoundedHash(MID, 2048),
  oldCollidesArgsObj: oldBoundedHash(JSON.stringify({cmd: 'cat', data: LONG}), 2048) === oldBoundedHash(JSON.stringify({cmd: 'cat', data: MID}), 2048),
  oldCollidesAttach: oldBoundedHash(JSON.stringify({name: 'report.pdf', data: LONG}), 2048) === oldBoundedHash(JSON.stringify({name: 'report.pdf', data: MID}), 2048),
  oldCollidesAnchor: oldBoundedHash(JSON.stringify({role: 'assistant', ts: 123, text: LONG, attachments: 1}), 1024) === oldBoundedHash(JSON.stringify({role: 'assistant', ts: 123, text: MID, attachments: 1}), 1024),
  oldCollidesSnippet: oldBoundedHash(LONG2, 4096) === oldBoundedHash(MID2, 4096),
}));
"""


def _run_signature_node() -> dict:
    assert NODE, "node is required for the render-cache signature harness"
    script = f"const window = {{}};\n{_collect_functions(['_messageRenderCacheSignature', '_addBoundedHash', '_hashObjectInto', 'msgContent', '_messageHasReasoningPayload'])}\n{_SIGNATURE_NODE_BODY}"
    result = subprocess.run([NODE, "-e", script], text=True, capture_output=True, check=False)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_middle_only_mutations_change_render_signature():
    out = _run_signature_node()
    assert out["deterministic"], "identical state must yield an identical signature"
    # Every same-length middle-only edit must change the signature — the
    # pre-#6999 length+head+tail clip collided on exactly these edits.
    assert out["differArgs"], "middle edit of tool function.arguments must change the signature"
    assert out["differArgsObj"], "middle edit inside tc.args must change the signature"
    assert out["differAttach"], "middle edit inside an attachment must change the signature"
    assert out["differAnchor"], "middle edit inside the compression-anchor key must change the signature"
    assert out["differSnippet"], "middle edit of a tool snippet must change the signature"


def test_old_clip_scheme_collides_on_the_same_middle_edits():
    """Document the exact bug class: the old clip scheme collides here."""
    out = _run_signature_node()
    assert out["oldCollidesArgs"]
    assert out["oldCollidesArgsObj"]
    assert out["oldCollidesAttach"]
    assert out["oldCollidesAnchor"]
    assert out["oldCollidesSnippet"]


# ── #6999 re-gate: insertion order + recursion depth ────────────────────────
# _hashObjectInto() used to Object.keys(value).sort() while BOTH tool-detail
# render paths (buildToolCard and _transparentToolDetailHtml) render
# Object.entries(tc.args) — insertion order. Two argument objects with
# opposite insertion order therefore rendered DIFFERENT HTML while receiving
# the SAME cache signature (sandbox repro: alpha=A|beta=B vs beta=B|alpha=A
# with sameSignature=true). The fix walks keys in insertion order with an
# explicit per-key index discriminator, and threads recursion depth (child
# calls used to restart depth at 0, so the depth>64 guard never fired for
# nested/cyclic payloads).


def test_hash_object_preserves_insertion_order_not_sorted():
    fn = _function_source("_hashObjectInto")
    assert "Object.keys(value).sort()" not in fn, (
        "sorting keys aliases opposite-insertion-order args onto one signature"
    )
    assert "const keys=Object.keys(value);" in fn
    assert "add(i)" in fn, "the per-key index must discriminate insertion order"


def test_hash_add_bounded_threads_recursion_depth():
    fn = _function_source("_addBoundedHash")
    assert "function _addBoundedHash(add, value, depth)" in fn
    assert "_hashObjectInto(add, value, (depth||0)+1)" in fn, (
        "child objects must increment depth, not restart at 0"
    )


_INSERTION_ORDER_NODE_BODY = r"""
function stateWithArgs(args) {
  return {
    messages: [
      {role: 'user', content: 'u1'},
      {role: 'assistant', content: 'a1'},
    ],
    toolCalls: [{tid: 'tc1', id: 'tc-m1', name: 'shell', done: true, is_diff: false, assistant_msg_idx: 1, snippet: 'out', args}],
    session: {session_id: 'sid', message_count: 2, updated_at: 111, compression_anchor_visible_idx: 0, compression_anchor_message_key: null, compression_anchor_summary: ''},
  };
}
globalThis.S = stateWithArgs({alpha: 'A', beta: 'B'});
const sigAlphaBeta = _messageRenderCacheSignature();
globalThis.S = stateWithArgs({beta: 'B', alpha: 'A'});
const sigBetaAlpha = _messageRenderCacheSignature();
globalThis.S = stateWithArgs({alpha: 'A', beta: 'B'});
const sigAlphaBetaAgain = _messageRenderCacheSignature();
// Deeply nested payload: 100 levels of objects (far past the depth>64 guard).
let deep = {leaf: 'x'};
for (let i = 0; i < 100; i++) deep = {child: deep};
globalThis.S = stateWithArgs(deep);
const sigDeep = _messageRenderCacheSignature();
globalThis.S = stateWithArgs(deep);
const sigDeepAgain = _messageRenderCacheSignature();
// Cyclic payload: JSON.stringify would reject it; the depth guard must
// serialize it via the [unserializable] path instead of recursing forever.
const cyc = {}; cyc.self = cyc;
globalThis.S = stateWithArgs(cyc);
const sigCyclic = _messageRenderCacheSignature();
console.log(JSON.stringify({
  differInsertionOrder: sigAlphaBeta !== sigBetaAlpha,
  sameOrderDeterministic: sigAlphaBeta === sigAlphaBetaAgain,
  deepDeterministic: sigDeep === sigDeepAgain,
  cyclicComputes: typeof sigCyclic === 'string' && sigCyclic.length > 0,
}));
"""


def _run_insertion_order_node() -> dict:
    assert NODE, "node is required for the render-cache signature harness"
    script = (
        f"const window = {{}};\n"
        f"{_collect_functions(['_messageRenderCacheSignature', '_addBoundedHash', '_hashObjectInto', 'msgContent', '_messageHasReasoningPayload'])}\n"
        f"{_INSERTION_ORDER_NODE_BODY}"
    )
    result = subprocess.run([NODE, "-e", script], text=True, capture_output=True, check=False)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_opposite_insertion_order_arguments_get_different_signatures():
    """alpha=A|beta=B and beta=B|alpha=A must NOT share a cache signature."""
    out = _run_insertion_order_node()
    assert out["differInsertionOrder"], (
        "opposite insertion order renders different HTML but the signature aliased it"
    )
    assert out["sameOrderDeterministic"], "identical order must stay deterministic"


def test_depth_guard_survives_deep_and_cyclic_payloads():
    """Recursion depth must increment (not restart), so the depth>64 guard
    actually fires for nested/cyclic tool args instead of overflowing."""
    out = _run_insertion_order_node()
    assert out["deepDeterministic"], "100-level-deep args must hash deterministically"
    assert out["cyclicComputes"], "cyclic args must fall back to [unserializable], not hang"
