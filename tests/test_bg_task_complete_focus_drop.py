"""Structural assertions for the PR(c) UX surface on bg_task_complete.

Per P-bc §3.3 / §3.4: ``_handleBgTaskCompleteEvent`` in ``static/messages.js``
gains a toast surface and a T4 drop-when-focused gate stacked on top of the
PR(b) ring-buffer dedupe. The insertion order is contractual:

  1. JSON parse + sid guard                 (existing)
  2. ring-buffer dedup check                (existing post-PR(b))
  3. mark-as-seen + clear-unread bookkeeping (NEW)
  4. T4 ``_isSessionActivelyViewed(sid)`` toast gate (NEW)
  5. ``showToast(...)`` inside unfocused branch (NEW per Q-c-1)
  6. diagnostic ack POST outside the T4 gate (existing)

We can't drive JS from pytest (the repo intentionally avoids a node/jsdom dep
per AGENTS.md), so this file does string-grep + relative-index assertions on
``static/messages.js`` — the same convention the rest of the WEBUI-SUB suite
uses. Each grep is precise so a behavioural regression (e.g. moving the ack
inside the focus gate, or leaking ``d.command`` / ``d.exit_code`` into the
toast copy per Rc-2) trips a hard failure.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def _read_messages_js() -> str:
    return (REPO_ROOT / "static" / "messages.js").read_text()


def _handler_body() -> str:
    """Return the source slice of ``_handleBgTaskCompleteEvent`` start →
    next top-level ``function`` declaration."""
    js = _read_messages_js()
    start = js.index("function _handleBgTaskCompleteEvent(")
    # Next top-level function declaration after the handler.
    rest = js[start + 1 :]
    m = re.search(r"\n(function |// ──)", rest)
    end = start + 1 + (m.start() if m else len(rest))
    return js[start:end]


# ---------------------------------------------------------------------------
# Insertion-order contract
# ---------------------------------------------------------------------------


def _focus_gate_match(body: str):
    """Locate the T4 focus gate.

    Supported shapes:
    - legacy: ``if (_isSessionActivelyViewed(sid)) return;``
    - current: ``const _viewed = ... _isSessionActivelyViewed(sid) ...;``
      followed by ``if (_viewed) { ... } else { showToast(...) }``.
    Returns an object exposing ``.start()`` / ``.end()`` over the gate block.
    """
    # Direct legacy early-return form.
    m = re.search(
        r"if\s*\([^)]*_isSessionActivelyViewed\s*\(\s*sid\s*\)[^)]*\)\s*return",
        body,
    )
    if m is not None:
        return m
    # Indirect form: const _viewed = ... _isSessionActivelyViewed(sid) ...;
    # if (_viewed) { ... } else { ... } — brace-balance walk to find the gate block end.
    flag_decl = re.search(
        r"const\s+_viewed\s*=[^;]*_isSessionActivelyViewed\s*\(\s*sid\s*\)[^;]*;",
        body,
    )
    if flag_decl is None:
        return None
    gate_head = re.search(r"if\s*\(\s*_viewed\s*\)\s*\{", body[flag_decl.end():])
    if gate_head is None:
        return None
    open_abs = flag_decl.end() + gate_head.end() - 1  # index of '{'
    depth = 0
    close_abs = None
    for i in range(open_abs, len(body)):
        c = body[i]
        if c == '{':
            depth += 1
        elif c == '}':
            depth -= 1
            if depth == 0:
                close_abs = i
                break
    if close_abs is None:
        return None
    # If the gate has an `else`, include that branch because it owns the toast branch.
    else_match = re.match(r"\s*else\s*\{", body[close_abs + 1 :])
    if else_match is not None:
        else_open_abs = close_abs + 1 + else_match.end() - 1
        depth = 0
        else_close_abs = None
        for i in range(else_open_abs, len(body)):
            c = body[i]
            if c == '{':
                depth += 1
            elif c == '}':
                depth -= 1
                if depth == 0:
                    else_close_abs = i
                    break
        if else_close_abs is None:
            return None
        close_abs = else_close_abs

    class _M:
        def __init__(self, s, e):
            self._s, self._e = s, e
        def start(self):
            return self._s
        def end(self):
            return self._e
    return _M(flag_decl.start(), close_abs + 1)


def test_focus_gate_is_after_ring_buffer_dedup():
    """T4 ``_isSessionActivelyViewed(sid)`` gate MUST appear AFTER the
    ``_bgTaskCompleteRingBufferAdd`` dedupe call so duplicates never reach the
    focus gate (avoids touching mark-as-seen twice on a duplicate event)."""
    body = _handler_body()
    dedupe_idx = body.index("_bgTaskCompleteRingBufferAdd(sid, evt_id)")
    gate_match = _focus_gate_match(body)
    assert gate_match is not None, "T4 focus gate missing"
    assert gate_match.start() > dedupe_idx, (
        "T4 focus gate must follow the ring-buffer dedup check"
    )


def test_mark_as_seen_is_after_dedup_and_inside_focus_gate():
    """Mark-as-seen + clear-unread bookkeeping MUST run after dedup and inside
    the focused-viewer branch so an actively-viewed session clears its unread
    counter even though the toast is suppressed."""
    body = _handler_body()
    dedupe_idx = body.index("_bgTaskCompleteRingBufferAdd(sid, evt_id)")
    mark_idx = body.index("_markSessionViewed")
    clear_idx = body.index("_clearSessionCompletionUnread")
    gate_match = _focus_gate_match(body)
    assert gate_match is not None
    assert dedupe_idx < gate_match.start() <= mark_idx < gate_match.end(), (
        "_markSessionViewed must sit after dedupe inside the T4 focus gate"
    )
    assert dedupe_idx < gate_match.start() <= clear_idx < gate_match.end(), (
        "_clearSessionCompletionUnread must sit after dedupe inside the T4 focus gate"
    )


def test_toast_call_is_inside_unfocused_gate_branch():
    """The ``showToast`` call MUST sit inside the T4 else/unfocused branch so a
    focused viewer never sees a toast for the session they are watching."""
    body = _handler_body()
    gate_match = _focus_gate_match(body)
    assert gate_match is not None
    toast_idx = body.index("showToast(")
    assert gate_match.start() < toast_idx < gate_match.end(), (
        "showToast must be gated by the T4 drop-when-focused branch"
    )
    assert "} else {" in body[gate_match.start() : toast_idx], (
        "showToast must be in the unfocused `else` branch, not the focused branch"
    )


def test_ack_post_is_after_focus_gate_and_outside_toast_branch():
    """The diagnostic ack POST MUST run after the T4 gate and outside the toast
    branch so both focused and unfocused viewers emit the server cleanup signal.
    For unfocused viewers this preserves the existing toast-before-ack order."""
    body = _handler_body()
    gate_match = _focus_gate_match(body)
    assert gate_match is not None
    toast_idx = body.index("showToast(")
    ack_idx = body.index("api/bg-task-complete-ack")
    assert toast_idx < ack_idx, "unfocused toast must still precede diagnostic ack POST"
    assert ack_idx > gate_match.end(), "diagnostic ack POST must live outside the T4 focus gate"


# ---------------------------------------------------------------------------
# Toast copy guards (Rc-2: minimal-payload-safe)
# ---------------------------------------------------------------------------


def _toast_block() -> str:
    """Slice the source from the toast comment to the showToast call's end."""
    body = _handler_body()
    # The toast block is the try { ... } that contains showToast(.
    start = body.rindex("try", 0, body.index("showToast("))
    # Find the matching close — toast block ends at the next "} catch (_) {}".
    end_marker = body.index("} catch (_) {}", start)
    return body[start : end_marker + len("} catch (_) {}")]


def test_toast_block_uses_only_minimal_payload_fields():
    """Per Rc-2 the toast copy may reference ONLY ``d.task_id`` and the
    optional ``d.summary`` — never ``d.command`` or ``d.exit_code`` (those
    fields are not guaranteed on the minimal payload shipped by the server)."""
    block = _toast_block()
    assert "d.task_id" in block, "toast must reference d.task_id"
    assert "d.summary" in block, "toast must reference d.summary"
    assert "d.command" not in block, "toast must NOT reference d.command (Rc-2)"
    assert "d.exit_code" not in block, "toast must NOT reference d.exit_code (Rc-2)"


def test_toast_template_pins_copy():
    """The toast template (P-bc §3.3 Q-c-1 verbatim) wraps the task id in the
    8-char prefix and falls back to ``''`` (empty tail — just ``Task <id> done``)
    when ``d.summary`` is absent. Pin both literals so a future drift in copy is
    caught loud."""
    block = _toast_block()
    assert "slice(0, 8)" in block
    assert "slice(0, 80)" in block
    assert "Task ${tid} done${tail}" in block
    assert "2600" in block, "toast duration must be 2600ms per Q-c-1"
