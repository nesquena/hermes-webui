"""Regression guard: viewport anchor keys must not crash on lone surrogates.

The browser message renderer builds a per-message cache key for viewport
restoration via ``_messageViewportAnchorKeyForMessage``. It pipes
``role``, ``ts``, ``attachments``, and ``text`` through ``encodeURIComponent``
and ``Array.prototype.map(...).join('|')``.

``encodeURIComponent`` throws ``URIError: URI malformed`` when any of those
fields contains a lone UTF-16 surrogate (U+D800–U+DFFF, not part of a valid
pair). Because this happens synchronously inside ``renderMessages()``, a
single bad field on any message — e.g. one whose text was synthesized from a
truncated UTF-8 byte sequence in a tool output or attachment — used to
blank out the entire chat view.

This test pulls the real ``_messageViewportAnchorKeyForMessage`` and
``_safeEncodeURIComponent`` out of ``static/ui.js`` and feeds them the exact
inputs that previously crashed. It also confirms valid emoji (surrogate
*pairs*) still encode normally, so we don't regress real content.

Engine-compat note (added per gate review #5552):

The fallback sanitizer is implemented as a ``charCodeAt`` code-unit walk
rather than a regex with lookbehind/lookahead. Some older WebViews and
Safari < 16.4 don't support regex lookbehind; a top-level regex literal
containing ``(?<!...)`` in a classic ``<script defer>`` file is evaluated at
module PARSE time, so a parse failure there blanks the entire app for
those engines — strictly worse than the rare lone-surrogate crash this
fixes. The test ``test_no_lookbehind_in_static_ui_js`` enforces this by
refusing to merge a version of ``static/ui.js`` that contains a lookbehind
regex literal.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
UI_JS_PATH = ROOT / "static" / "ui.js"


def _read_required_text(path: Path, label: str) -> str:
    assert path.exists(), f"{label} not found at {path}"
    return path.read_text(encoding="utf-8")


def _ui_js() -> str:
    return _read_required_text(UI_JS_PATH, "static/ui.js")


def _run_node_script(script: str) -> str:
    node = shutil.which("node")
    if not node:
        pytest.skip("node executable is required for JavaScript behavior checks")
    try:
        result = subprocess.run(
            [node, "-e", script],
            cwd=ROOT,
            text=True,
            capture_output=True,
            timeout=10,
        )
    except subprocess.TimeoutExpired as exc:
        pytest.fail(
            "node behavior check timed out"
            f"\nstdout:\n{exc.stdout or '<empty>'}"
            f"\nstderr:\n{exc.stderr or '<empty>'}"
        )
    if result.returncode != 0:
        pytest.fail(
            "node behavior check failed"
            f"\nstdout:\n{result.stdout or '<empty>'}"
            f"\nstderr:\n{result.stderr or '<empty>'}"
        )
    return result.stdout.strip()


def test_safe_encode_uri_component_defined():
    """The fix introduces a guarded wrapper. Make sure it's present and used."""
    src = _ui_js()
    assert "function _safeEncodeURIComponent(" in src, (
        "_safeEncodeURIComponent helper is missing from static/ui.js"
    )
    # The old vulnerable call site must be gone.
    assert (
        ".map(v=>encodeURIComponent(String(v))).join('|')"
        not in src
    ), "raw encodeURIComponent is still being piped into the anchor key"
    # And the new caller must be present.
    assert ".map(v=>_safeEncodeURIComponent(v)).join('|')" in src, (
        "_messageViewportAnchorKeyForMessage is not routing through the safe wrapper"
    )


def test_safe_encode_uri_component_handles_lone_surrogates():
    """The exact inputs that previously crashed ``renderMessages()`` must now succeed."""
    script = r"""
    const src = require('fs').readFileSync(
      'static/ui.js', 'utf8'
    );
    // Extract just the two functions we care about.
    const safeMatch = src.match(
      /function _safeEncodeURIComponent\(v\)\{[\s\S]*?\n\}/
    );
    if (!safeMatch) {
      console.log('MISSING_SAFE_FN');
      process.exit(0);
    }
    eval(safeMatch[0]);

    const cases = [
      // input, must_contain, must_not_contain
      { name: 'plain_ascii',     v: 'user',           contains: ['user'],                     not: ['%'] },
      { name: 'plain_space',     v: 'hello world',    contains: ['hello%20world'],            not: [] },
      { name: 'emoji_pair',      v: '\u{1F43E}',     contains: ['%F0%9F%90%BE'],            not: ['\u{1F43E}'] },
      { name: 'lone_high',       v: 'a\uD800b',      contains: ['a', 'b'],                  not: ['\uD800'] },
      { name: 'lone_low',        v: 'a\uDC00b',      contains: ['a', 'b'],                  not: ['\uDC00'] },
      { name: 'mixed_surrogate', v: '\uD83Dhello\uD83Eworld', contains: ['hello', 'world'], not: ['\uD83D', '\uD83E'] },
      // Regression guards added per Greptile review:
      // a) valid emoji pair must SURVIVE the fallback when a lone surrogate
      //    sits next to it (the broad regex used to strip both halves).
      { name: 'mixed_pair_lone', v: '\u{1F43E}\uD800',  contains: ['%F0%9F%90%BE'],   not: ['\uD800'] },
      // b) literal pipe in text must be percent-encoded so the | used to
      //    join key segments can never appear inside a field value.
      { name: 'pipe_with_lone',  v: 'a|b\uD800c',        contains: ['%7C'],           not: ['|'] },
      // c) multiple lone surrogates in a row must all be stripped.
      { name: 'multi_lone',      v: '\uD800\uD801\uD802', contains: [],             not: ['\uD800','\uD801','\uD802'] },
      // d) emoji-then-lone where the emoji comes first and the lone is mid-string.
      { name: 'pair_middle_lone', v: 'foo\u{1F43E}\uD800bar', contains: ['foo','bar','%F0%9F%90%BE'], not: ['\uD800'] },
      // e) lone-then-emoji (lone must be skipped, emoji kept).
      { name: 'lone_then_pair',  v: '\uD800\u{1F43E}tail',   contains: ['tail','%F0%9F%90%BE'],      not: ['\uD800'] },
    ];

    let ok = true;
    for (const c of cases) {
      let out;
      try {
        out = _safeEncodeURIComponent(c.v);
      } catch (e) {
        console.log(`THREW: ${c.name} -> ${e.message}`);
        ok = false;
        continue;
      }
      for (const needle of c.contains) {
        if (!out.includes(needle)) {
          console.log(`MISSING_CONTAIN: ${c.name} expected ${needle} in ${out}`);
          ok = false;
        }
      }
      for (const needle of c.not) {
        if (out.includes(needle)) {
          console.log(`FORBIDDEN_FOUND: ${c.name} contained ${needle} in ${out}`);
          ok = false;
        }
      }
      console.log(`OK ${c.name} -> ${out}`);
    }
    console.log(ok ? 'ALL_PASS' : 'FAIL');
    """
    stdout = _run_node_script(script)
    assert "THREW" not in stdout, f"safe wrapper still throws: {stdout}"
    assert "MISSING_CONTAIN" not in stdout, stdout
    assert "FORBIDDEN_FOUND" not in stdout, stdout
    assert stdout.rstrip().endswith("ALL_PASS"), stdout


def test_message_viewport_anchor_key_does_not_throw_on_surrogate_text():
    """End-to-end: a message whose ``text`` contains a lone surrogate must
    produce a key without throwing — the failure mode that blanked the chat."""
    script = r"""
    const src = require('fs').readFileSync(
      'static/ui.js', 'utf8'
    );
    const safeMatch = src.match(
      /function _safeEncodeURIComponent\(v\)\{[\s\S]*?\n\}/
    );
    const anchorMatch = src.match(
      /function _messageViewportAnchorKeyForMessage\(m\)\{[\s\S]*?\n\}/
    );
    if (!safeMatch || !anchorMatch) {
      console.log('MISSING_FN');
      process.exit(0);
    }
    eval(safeMatch[0]);
    eval(anchorMatch[0]);

    // Build the minimal `_compressionMessageAnchorKey` shim the function
    // requires (it just forwards the message fields).
    _compressionMessageAnchorKey = (m) => ({
      role: m.role,
      ts: m.ts,
      attachments: m.attachments,
      text: m.text,
    });

    const msgs = [
      { role: 'user',      ts: 1783179992, attachments: 0, text: 'normal message' },
      { role: 'assistant', ts: 1783180000, attachments: 0, text: 'a\uD800b' },
      { role: 'user',      ts: 1783180010, attachments: 1, text: 'reply with \uD83E🤖-bait' },
      // Regression guard: text with a literal '|' must produce a key whose
      // segments still split cleanly (the pipe must be percent-encoded).
      { role: 'user',      ts: 1783180020, attachments: 0, text: 'a|b\uD800c' },
      // Emoji + lone, mid-string
      { role: 'user',      ts: 1783180030, attachments: 0, text: 'foo\u{1F43E}\uD800bar' },
    ];

    let ok = true;
    for (const m of msgs) {
      let key;
      try {
        key = _messageViewportAnchorKeyForMessage(m);
      } catch (e) {
        console.log(`THREW: role=${m.role} -> ${e.message}`);
        ok = false;
        continue;
      }
      if (typeof key !== 'string' || key.length === 0) {
        console.log(`EMPTY_KEY: role=${m.role}`);
        ok = false;
        continue;
      }
      // No surrogate code units should survive into the anchor key.
      if (/[\uD800-\uDFFF]/.test(key)) {
        console.log(`SURROGATE_IN_KEY: role=${m.role} -> ${key}`);
        ok = false;
      }
      // The join separator is unescaped '|' and we always emit exactly 4
      // segments (role|ts|attachments|text). A literal '|' inside any
      // field must have been percent-encoded to '%7C' by the safe wrapper.
      const segs = key.split('|');
      if (segs.length !== 4) {
        console.log(`BAD_SEGMENT_COUNT: role=${m.role} -> ${segs.length} segs in ${key}`);
        ok = false;
      }
      console.log(`OK role=${m.role} key=${key}`);
    }
    console.log(ok ? 'ALL_PASS' : 'FAIL');
    """
    stdout = _run_node_script(script)
    assert "THREW" not in stdout, stdout
    assert "EMPTY_KEY" not in stdout, stdout
    assert "SURROGATE_IN_KEY" not in stdout, stdout
    assert "BAD_SEGMENT_COUNT" not in stdout, stdout
    assert stdout.rstrip().endswith("ALL_PASS"), stdout


def test_no_lookbehind_in_static_ui_js():
    """No top-level lookbehind regex literal in ``static/ui.js``.

    A classic ``<script defer>`` file is parsed on load. A regex literal
    containing ``(?<!...)`` (lookbehind) is evaluated at module PARSE time,
    not runtime — engines without lookbehind support (older Safari <16.4,
    some embedded WebViews) fail to parse the whole file and the app blanks.

    The surrogate fix must stay engine-portable, so this test rejects any
    reintroduction of lookbehind (or, for symmetry, lookahead we don't
    actually need) in the shipped JS.

    See gate review of PR #5573 for the parse-time-brick failure mode.
    """
    src = _ui_js()
    # Lookbehind: (?<!  or (?<=
    assert not re.search(r"\(\?<[!=]", src), (
        "static/ui.js contains a regex lookbehind (?<! or (?<=) which is a "
        "parse-time brick on engines without regex lookbehind support "
        "(Safari < 16.4, some embedded WebViews). Use a charCodeAt loop "
        "instead — see _safeEncodeURIComponent."
    )
    # And for symmetry, the surrogate sanitizer must not reintroduce lookahead
    # either — the charCodeAt loop is engine-portable, so neither should be
    # needed for this fix. (Other parts of ui.js legitimately use lookahead;
    # we only check the surrogate helper here.)
    safe_match = re.search(
        r"function _safeEncodeURIComponent\(v\)\{[\s\S]*?\n\}", src
    )
    assert safe_match, "_safeEncodeURIComponent helper is missing"
    assert not re.search(r"\(\?<[!=]", safe_match.group(0)), (
        "_safeEncodeURIComponent contains a regex lookbehind — switch to a "
        "charCodeAt code-unit walk."
    )
    assert not re.search(r"\(\?!", safe_match.group(0)), (
        "_safeEncodeURIComponent contains a regex lookahead — switch to a "
        "charCodeAt code-unit walk to keep the helper engine-portable."
    )