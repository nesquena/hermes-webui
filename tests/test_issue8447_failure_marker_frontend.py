"""Frontend regression tests for #8447 — the sidebar "last turn failed" marker.

The server side derives a sanitized ``failure_summary`` and puts it on the
session-list row.  The client turns that summary into a visible marker, and
the marker must distinguish a retryable failure from a non-retryable one
without ever rendering a raw provider error.

These drive the real ``_sessionFailureState`` implementation out of
``static/sessions.js`` and run it under node, using the same hardened
function-extractor the sibling #6728 suite uses.
"""

import json
import pathlib
import shutil
import subprocess
import tempfile

import pytest

ROOT = pathlib.Path(__file__).parent.parent
SESSIONS_JS_PATH = ROOT / "static" / "sessions.js"
I18N_JS_PATH = ROOT / "static" / "i18n.js"
NODE = shutil.which("node")

pytestmark = pytest.mark.skipif(NODE is None, reason="node not on PATH")


def _extract_func_script(js: str) -> str:
    """Brace-match a function body while skipping braces inside string /
    template / regex literals and comments (same hardened extractor as the
    sibling #6728 / #5744 suites)."""
    prelude = "const src = " + json.dumps(js) + ";\n"
    body = r"""
function extractFunc(name) {
  const re = new RegExp('function\\s+' + name + '\\s*\\(');
  const start = src.search(re);
  if (start < 0) throw new Error(name + ' not found');
  let i = src.indexOf('{', start);
  let depth = 1; i++;
  let str = null;
  let inLine = false;
  let inBlock = false;
  let inRegex = false;
  let prev = '';
  while (depth > 0 && i < src.length) {
    const c = src[i];
    const n = src[i + 1];
    if (inLine) { if (c === '\n') inLine = false; i++; continue; }
    if (inBlock) { if (c === '*' && n === '/') { inBlock = false; i++; } i++; continue; }
    if (str) {
      if (c === '\\') { i += 2; continue; }
      if (c === str) str = null;
      i++; continue;
    }
    if (inRegex) {
      if (c === '\\') { i += 2; continue; }
      if (c === '/') inRegex = false;
      i++; continue;
    }
    if (c === '/' && n === '/') { inLine = true; i += 2; continue; }
    if (c === '/' && n === '*') { inBlock = true; i += 2; continue; }
    if (c === '"' || c === "'" || c === '`') { str = c; i++; continue; }
    if (c === '/' && !'})]0123456789'.includes(prev) && !/[A-Za-z_$]/.test(prev)) {
      inRegex = true; i++; continue;
    }
    if (c === '{') depth++;
    else if (c === '}') depth--;
    if (c.trim()) prev = c;
    i++;
  }
  return src.slice(start, i);
}
"""
    return prelude + body


def _run_with_arg(source: str, arg: str) -> str:
    with tempfile.NamedTemporaryFile(
        "w", suffix=".cjs", encoding="utf-8", dir=ROOT, delete=False
    ) as script:
        script.write(source)
        script_path = pathlib.Path(script.name)
    try:
        result = subprocess.run(
            [NODE, str(script_path), arg],
            cwd=str(ROOT),
            capture_output=True,
            text=True,
            timeout=30,
        )
    finally:
        script_path.unlink(missing_ok=True)
    if result.returncode != 0:
        raise RuntimeError(result.stderr)
    return result.stdout.strip()


def _failure_state(row_json: str) -> str:
    """Run the real ``_sessionFailureState`` over one row and print JSON."""
    js = SESSIONS_JS_PATH.read_text(encoding="utf-8")
    source = _extract_func_script(js) + r"""
const fn = eval('(' + extractFunc('_sessionFailureState') + ')');
// The real function calls the i18n helper `t`; stub it the way the app does
// for a missing key: return the key's fallback text.
function t(key, ...args) {
  if (key === 'session_failure_label') return 'Last turn failed (' + args[0] + ')';
  if (key === 'session_failure_title') return 'Last turn failed (' + args[0] + ').';
  return key;
}
const row = JSON.parse(process.argv[2]);
const state = fn(row);
process.stdout.write(JSON.stringify(state === null ? null : {
  reason: state.reason,
  layer: state.layer,
  code: state.code,
  retryable: state.retryable,
  label: state.label,
  title: state.title,
}));
"""
    return _run_with_arg(source, row_json)


def test_failure_state_is_null_without_a_summary():
    assert _failure_state(json.dumps({"session_id": "s1"})) == "null"


def test_failure_state_is_null_for_a_healthy_row():
    row = {"session_id": "s1", "failure_summary": {"kind": "something_else"}}
    assert _failure_state(json.dumps(row)) == "null"


def test_failure_state_reports_the_layer_and_retryable_flag():
    row = {
        "session_id": "s1",
        "failure_summary": {
            "kind": "failed_turn", "layer": "billing", "code": "billing",
            "retryable": False,
        },
    }
    state = json.loads(_failure_state(json.dumps(row)))
    assert state["reason"] == "billing"
    assert state["layer"] == "billing"
    assert state["retryable"] is False


def test_failure_state_distinguishes_a_retryable_failure():
    row = {
        "session_id": "s1",
        "failure_summary": {
            "kind": "failed_turn", "layer": "upstream", "retryable": True,
        },
    }
    state = json.loads(_failure_state(json.dumps(row)))
    assert state["retryable"] is True
    assert state["reason"] == "upstream"


def test_failure_state_falls_back_to_code_then_unknown():
    only_code = {
        "session_id": "s1",
        "failure_summary": {"kind": "failed_turn", "code": "rate_limit"},
    }
    assert json.loads(_failure_state(json.dumps(only_code)))["reason"] == "rate_limit"

    unknown = {
        "session_id": "s1",
        "failure_summary": {"kind": "failed_turn", "detail": "unknown"},
    }
    assert json.loads(_failure_state(json.dumps(unknown)))["reason"] == "unknown"


def test_failure_state_never_renders_a_raw_provider_error():
    """Even if a row somehow carries the raw error text, the client must not
    put it into the marker."""
    row = {
        "session_id": "s1",
        "failure_summary": {
            "kind": "failed_turn", "layer": "billing",
            "error": "HTTP 402: out of credits at https://secret.example",
        },
    }
    out = _failure_state(json.dumps(row))
    assert "secret.example" not in out
    assert "out of credits" not in out


def test_i18n_defines_the_failure_keys_in_every_locale():
    """Every locale must define both keys, or the marker falls back to a raw
    key string for non-English users."""
    i18n = I18N_JS_PATH.read_text(encoding="utf-8")
    label_count = i18n.count("session_failure_label:")
    title_count = i18n.count("session_failure_title:")
    assert label_count >= 15, label_count
    assert title_count == label_count, (label_count, title_count)


def test_failure_state_keeps_retryability_tristate():
    """An absent (or malformed) producer flag stays null — an unknown failure
    must not be presented as a proven-terminal one."""
    unknown = json.loads(_failure_state(json.dumps({
        "session_id": "s1",
        "failure_summary": {"kind": "failed_turn", "layer": "billing"},
    })))
    assert unknown["retryable"] is None

    terminal = json.loads(_failure_state(json.dumps({
        "session_id": "s1",
        "failure_summary": {"kind": "failed_turn", "layer": "billing",
                            "retryable": False},
    })))
    assert terminal["retryable"] is False

    retryable = json.loads(_failure_state(json.dumps({
        "session_id": "s1",
        "failure_summary": {"kind": "failed_turn", "layer": "upstream",
                            "retryable": True},
    })))
    assert retryable["retryable"] is True


def _marker_class(arg: str) -> str:
    """Run the real ``_sessionFailureMarkerClass`` over one JSON state."""
    js = SESSIONS_JS_PATH.read_text(encoding="utf-8")
    source = _extract_func_script(js) + r"""
const fn = eval('(' + extractFunc('_sessionFailureMarkerClass') + ')');
process.stdout.write(fn(JSON.parse(process.argv[2])) || '');
"""
    return _run_with_arg(source, arg)


def test_marker_class_reflects_tristate_retryability():
    assert _marker_class(json.dumps({"retryable": True})) == (
        "session-failure-indicator is-retryable"
    )
    assert _marker_class(json.dumps({"retryable": False})) == (
        "session-failure-indicator is-terminal"
    )
    assert _marker_class(json.dumps({"retryable": None})) == (
        "session-failure-indicator is-unknown"
    )
    assert _marker_class(json.dumps({})) == (
        "session-failure-indicator is-unknown"
    )
    assert _marker_class(json.dumps(None)) == ""


def test_sessions_js_contains_the_failure_marker_hook():
    js = SESSIONS_JS_PATH.read_text(encoding="utf-8")
    assert "session-failure-indicator" in js
    assert "_sessionFailureState(s)" in js
