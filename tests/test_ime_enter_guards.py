"""Regression tests — an Enter that commits an IME composition must never submit.

CJK/Japanese/Korean users lose the candidate-confirm key when a keydown handler
treats every `Enter` as a submit: the raw pinyin is committed AND the form
fires, so the user sees "typing pinyin sent my message".

Two mechanisms are pinned here:

1. `static/boot.js` tracks composition on the **document** (capture phase), not
   only on the `#msg` composer, so `window._isImeEnter(e)` also covers the
   Safari / Linux-IME race where the committing keydown lands *after*
   `compositionend` with `isComposing=false`.
2. Every Enter-submit site returns **before** `preventDefault()` while an IME
   composition owns the keypress (preventing the default would break the commit
   itself). Handlers covered: clarify free-text input, kanban "New task" title,
   kanban task modal, panel/app title rename, editable queue-card text,
   selection-chip rename, workspace path suggestions, custom model id; the
   login page does not load boot.js and uses a self-contained guard instead.
"""

import json
import os
import pathlib
import re
import subprocess


REPO_ROOT = pathlib.Path(__file__).parent.parent.resolve()
MESSAGES_JS = (REPO_ROOT / "static" / "messages.js").read_text(encoding="utf-8")


# ── helpers ──────────────────────────────────────────────────────────────────


def _extract_clarify_keydown_handler(src: str) -> str:
    """Return the arrow function assigned to `input.onkeydown` in showClarifyCard."""
    anchor = "input.onkeydown = (e) => {"
    start = src.index(anchor) + len("input.onkeydown = ")
    # Brace-match the arrow function body so the extraction follows the source.
    body_start = src.index("{", start)
    depth = 0
    for idx in range(body_start, len(src)):
        ch = src[idx]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return src[start:idx + 1]
    raise AssertionError("unterminated clarify onkeydown handler")


def _node_env():
    """Node on this host dies with SIGABRT when Hermes' NODE_CHANNEL_FD leaks in."""
    env = dict(os.environ)
    for key in ("NODE_CHANNEL_FD", "NODE_OPTIONS", "NODE_CHANNEL_SERIALIZATION_MODE"):
        env.pop(key, None)
    return env


def _run_handler(handler_src: str, cases: list) -> list:
    script = f"""
let _clarifyImeComposing = false;
let submits = 0;
let prevented = 0;
function respondClarify() {{ submits += 1; }}
const window = {{ _isImeEnter: (e) => !!(e.isComposing || e.keyCode === 229) }};
const handler = {handler_src};
const out = [];
for (const c of {json.dumps(cases)}) {{
  if (c.composing_flag !== undefined) _clarifyImeComposing = c.composing_flag;
  submits = 0; prevented = 0;
  handler({{
    key: c.key,
    isComposing: !!c.isComposing,
    keyCode: c.keyCode,
    preventDefault: () => {{ prevented += 1; }},
  }});
  out.push({{submits, prevented}});
}}
console.log(JSON.stringify(out));
"""
    proc = subprocess.run(
        ["node", "-e", script],
        capture_output=True,
        text=True,
        timeout=60,
        env=_node_env(),
    )
    assert proc.returncode == 0, f"node harness failed: {proc.stderr}"
    return json.loads(proc.stdout.strip().splitlines()[-1])


# ── static pins: the guard exists and is ordered before the submit ───────────


def test_clarify_input_binds_composition_tracking():
    """The clarify input must track compositionstart/end (Safari/Linux IME race)."""
    assert re.search(
        r"input\.addEventListener\('compositionstart'\s*,\s*\(\)\s*=>\s*\{\s*"
        r"_clarifyImeComposing\s*=\s*true\s*;",
        MESSAGES_JS,
    ), "clarify input must set _clarifyImeComposing on compositionstart"
    assert re.search(
        r"input\.addEventListener\('compositionend'\s*,\s*\(\)\s*=>\s*\{\s*"
        r"setTimeout\(\(\)\s*=>\s*\{\s*_clarifyImeComposing\s*=\s*false\s*;\s*\}\s*,\s*0\)",
        MESSAGES_JS,
    ), "clarify input must clear _clarifyImeComposing on the tick after compositionend"
    assert re.search(
        r"input\.addEventListener\('blur'\s*,\s*\(\)\s*=>\s*\{\s*"
        r"_clarifyImeComposing\s*=\s*false\s*;",
        MESSAGES_JS,
    ), "clarify input must clear _clarifyImeComposing on blur (stuck-flag guard)"


def test_clarify_keydown_routes_through_windowed_ime_helper():
    handler = _extract_clarify_keydown_handler(MESSAGES_JS)
    assert re.search(
        r"if\s*\(\s*window\._isImeEnter\s*&&\s*window\._isImeEnter\s*\(\s*e\s*\)\s*\)\s*return\s*;",
        handler,
    ), "clarify Enter handler must use window._isImeEnter (issue #1443 convention)"
    assert re.search(
        r"if\s*\(\s*_clarifyImeComposing\s*\)\s*return\s*;",
        handler,
    ), "clarify Enter handler must honour the per-input composition flag"


# ── behaviour: run the real handler text in node ────────────────────────────


def test_ime_enter_never_submits_and_is_not_prevented():
    """A composing Enter belongs to the IME: no submit, no preventDefault()."""
    handler = _extract_clarify_keydown_handler(MESSAGES_JS)
    results = _run_handler(handler, [
        {"key": "Enter", "isComposing": True, "keyCode": 229, "composing_flag": False},
        {"key": "Enter", "isComposing": False, "keyCode": 229, "composing_flag": False},
        {"key": "Enter", "isComposing": False, "keyCode": 13, "composing_flag": True},
    ])
    for idx, res in enumerate(results):
        assert res == {"submits": 0, "prevented": 0}, (
            f"case {idx}: composing Enter must not submit nor preventDefault, got {res}"
        )


def test_plain_enter_still_submits():
    handler = _extract_clarify_keydown_handler(MESSAGES_JS)
    results = _run_handler(handler, [
        {"key": "Enter", "isComposing": False, "keyCode": 13, "composing_flag": False},
    ])
    assert results == [{"submits": 1, "prevented": 1}], (
        f"plain Enter must submit exactly once, got {results}"
    )


def test_non_enter_keys_are_ignored():
    handler = _extract_clarify_keydown_handler(MESSAGES_JS)
    results = _run_handler(handler, [
        {"key": "a", "isComposing": False, "keyCode": 65, "composing_flag": False},
        {"key": "Escape", "isComposing": False, "keyCode": 27, "composing_flag": False},
    ])
    assert results == [{"submits": 0, "prevented": 0}, {"submits": 0, "prevented": 0}], (
        f"non-Enter keys must be untouched, got {results}"
    )


# ── the other Enter-submit sites (same bug class, fixed together) ───────────

STATIC = REPO_ROOT / "static"
BOOT_JS = (STATIC / "boot.js").read_text(encoding="utf-8")
INDEX_HTML = (STATIC / "index.html").read_text(encoding="utf-8")
PANELS_JS = (STATIC / "panels.js").read_text(encoding="utf-8")
UI_JS = (STATIC / "ui.js").read_text(encoding="utf-8")
LOGIN_JS = (STATIC / "login.js").read_text(encoding="utf-8")


def _around(src: str, anchor: str, after: int = 260) -> str:
    start = src.index(anchor)
    return src[start:start + after]


def _guard_present(snippet: str) -> bool:
    return bool(
        re.search(r"window\._isImeEnter\s*&&\s*window\._isImeEnter\s*\(", snippet)
        or re.search(r"e\.isComposing\s*\|\|\s*e\.keyCode\s*===\s*229", snippet)
    )


def test_boot_composition_tracking_is_document_wide():
    """The composer-only listeners left other fields to the state-free guard."""
    for ev in ("compositionstart", "compositionend", "focusout"):
        assert re.search(
            r"document\.addEventListener\('%s'\s*,\s*\(\)\s*=>\s*\{[^}]*_imeComposing" % ev,
            BOOT_JS,
        ), f"boot.js must track composition on the document ({ev})"


def test_kanban_new_task_title_is_guarded():
    assert 'onkeydown="kanbanNewTaskKeydown(event)"' in INDEX_HTML, (
        "kanban new-task input must call the guarded helper, not createKanbanTask() inline"
    )
    snippet = _around(PANELS_JS, "function kanbanNewTaskKeydown(ev){")
    assert _guard_present(snippet), "kanbanNewTaskKeydown must guard on IME composition"


def test_panel_title_rename_is_guarded():
    snippet = _around(PANELS_JS, "if (e2.key === 'Enter') {\n          if (window._isImeEnter")
    assert "_isImeEnter" in snippet and "finish(true)" in snippet


def test_kanban_task_modal_enter_is_guarded():
    snippet = _around(PANELS_JS, "if (ev.key === 'Enter' && !ev.shiftKey) {")
    assert _guard_present(snippet), "kanban modal Enter must bail out while composing"


def test_workspace_suggestion_enter_is_guarded():
    snippet = _around(PANELS_JS, "if(e.key==='Enter' && _wsSuggestIndex>=0")
    assert _guard_present(snippet), "workspace suggestion Enter must bail out while composing"


def test_queue_card_edit_is_guarded():
    snippet = _around(UI_JS, "msgSpan.onkeydown=(e)=>{if(e.key==='Enter'){")
    assert _guard_present(snippet), "editable queue-card text must bail out while composing"


def test_custom_model_id_enter_is_guarded():
    """Guarded by a capture listener at the input's creation site (stable anchor:
    the submit handler's line drifts between HEAD and origin/master)."""
    snippet = _around(UI_JS, "const _ci=_custRow.querySelector('.model-custom-input');", 700)
    assert "_isImeEnter" in snippet and "stopImmediatePropagation" in snippet, (
        "custom model id Enter must be swallowed in the capture phase while composing"
    )


def test_selection_chip_rename_is_guarded():
    snippet = _around(MESSAGES_JS, "inp.addEventListener('keydown',e=>{ if(e.key==='Enter'){")
    assert _guard_present(snippet), "selection-chip rename must bail out while composing"


def test_login_guard_is_self_contained():
    """The login page loads only login.js — window._isImeEnter does not exist there."""
    snippet = _around(LOGIN_JS, "if (e.key === 'Enter') {", 400)
    assert re.search(r"e\.isComposing\s*\|\|\s*e\.keyCode\s*===\s*229", snippet), (
        "login.js needs the state-free IME guard, it cannot rely on boot.js"
    )


# ── behaviour: the kanban helper, executed for real ─────────────────────────


def _extract_named_function(src: str, header: str) -> str:
    start = src.index(header)
    brace = src.index("{", start)
    depth = 0
    for idx in range(brace, len(src)):
        ch = src[idx]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return src[start:idx + 1]
    raise AssertionError(f"unterminated function: {header}")


def _run_kanban_helper(fn_src: str, cases: list) -> list:
    script = f"""
let calls = 0;
function createKanbanTask() {{ calls += 1; }}
const window = {{ _isImeEnter: (e) => !!(e.isComposing || e.keyCode === 229) }};
{fn_src}
const out = [];
for (const c of {json.dumps(cases)}) {{
  calls = 0;
  kanbanNewTaskKeydown({{key: c.key, isComposing: !!c.isComposing, keyCode: c.keyCode}});
  out.push({{calls}});
}}
console.log(JSON.stringify(out));
"""
    proc = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=60, env=_node_env())
    assert proc.returncode == 0, f"node harness failed: {proc.stderr}"
    return json.loads(proc.stdout.strip().splitlines()[-1])


def test_kanban_helper_composing_enter_does_not_create_task():
    fn = _extract_named_function(PANELS_JS, "function kanbanNewTaskKeydown(ev){")
    results = _run_kanban_helper(fn, [
        {"key": "Enter", "isComposing": True, "keyCode": 229},
        {"key": "Enter", "isComposing": False, "keyCode": 229},
        {"key": "Enter", "isComposing": False, "keyCode": 13},
    ])
    assert results == [{"calls": 0}, {"calls": 0}, {"calls": 1}], results
