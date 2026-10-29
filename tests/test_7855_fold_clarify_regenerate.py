"""#7855 maintainer fold: two paths that dropped or consumed a restored goal continuation.

1. ``lockComposerForClarify()`` saved the composer draft with three arguments, so the
   payload stored ``goal_continuation_id: ''`` and an unchanged continuation restored after
   a reload was sent as an ordinary turn (``goal_related=false``), stopping the goal loop.
   It must pass the restored ID when the composer still holds exactly the restored text,
   and no ID for edited text.
2. A regenerate request posts no ``goal_continuation_id`` key. The pre-upgrade-tab fallback
   then peeked the pending continuation and the regenerate consumed it, so the real
   continuation dispatch ran as an ordinary turn.

Both tests execute the production code: the clarify lock is extracted from
``static/ui.js`` and run in node; the regenerate case calls the real route helper.
"""

import json
import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
UI_JS = (ROOT / "static" / "ui.js").read_text(encoding="utf-8")
MESSAGES_JS = (ROOT / "static" / "messages.js").read_text(encoding="utf-8")
NODE = shutil.which("node")


def _extract_function(source: str, name: str) -> str:
    start = source.index(f"function {name}(")
    brace = source.index("{", start)
    depth = 0
    for i in range(brace, len(source)):
        ch = source[i]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return source[start : i + 1]
    raise AssertionError(f"unterminated function {name}")


def _run_clarify_lock(composer_text: str, restored_id: str, restored_text: str) -> list:
    script = textwrap.dedent(
        """
        const calls = [];
        const input = {value: %(text)s, dataset: {}, placeholder: '', disabled: false,
                       setAttribute(){}, removeAttribute(){}, classList: {add(){}, remove(){}}};
        if (%(rid)s) { input.dataset.goalContinuationId = %(rid)s; input.dataset.goalContinuationText = %(rtext)s; }
        const elements = {msg: input};
        function $(id){ return elements[id] || null; }
        const S = {session: {session_id: 'sid-1'}, pendingFiles: []};
        function _saveComposerDraftNow(...args){ calls.push(args.map(a => Array.isArray(a) ? a.length : a)); return Promise.resolve(); }
        function _handleComposerDraftPostFailure(){}
        function updateSendBtn(){}
        function autoResize(){}
        %(normalize)s
        %(peek)s
        %(lock)s
        try { lockComposerForClarify('Answer the question above'); } catch (e) { calls.push(['THREW', String(e)]); }
        console.log(JSON.stringify(calls));
        """
    ) % {
        "text": json.dumps(composer_text),
        "rid": json.dumps(restored_id),
        "rtext": json.dumps(restored_text),
        "normalize": _extract_function(MESSAGES_JS, "_normalizeGoalContinuationId"),
        "peek": _extract_function(MESSAGES_JS, "_peekRestoredGoalContinuationDraft"),
        "lock": _extract_function(UI_JS, "lockComposerForClarify"),
    }
    proc = subprocess.run([NODE, "-e", script], capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stderr[:2000]
    return json.loads(proc.stdout.strip().splitlines()[-1])


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_clarify_save_keeps_the_token_of_an_unchanged_restored_continuation():
    token = "a" * 32
    calls = _run_clarify_lock("Continue the goal", token, "Continue the goal")
    saves = [c for c in calls if c and c[0] == "sid-1"]
    assert saves, calls
    assert saves[0][3] == token, f"clarify save must carry the restored continuation id; got {saves[0]}"


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_clarify_save_of_edited_text_carries_no_token():
    token = "b" * 32
    calls = _run_clarify_lock("My own message", token, "Continue the goal")
    saves = [c for c in calls if c and c[0] == "sid-1"]
    assert saves, calls
    assert len(saves[0]) == 3 or not saves[0][3], f"edited text must not keep the token; got {saves[0]}"


def test_regenerate_without_the_key_does_not_consume_the_pending_continuation(monkeypatch):
    import api.goals as goals
    from api import routes

    pending = "c" * 32
    monkeypatch.setattr(goals, "peek_pending_goal_continuation_id", lambda sid: pending)
    # A pre-upgrade tab's keyless start still gets master's marker admission.
    assert routes._resolve_chat_start_goal_continuation_id({"message": "x"}, "sid-1") == pending
    # A regenerate posts no key and must never resolve (and so consume) the pending continuation.
    assert routes._resolve_chat_start_goal_continuation_id({"message": "x", "regenerate": True}, "sid-1") == ""
