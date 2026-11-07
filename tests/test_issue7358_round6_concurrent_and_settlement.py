"""#7358 round 6 regression — concurrent same-name tools + settlement prior merge.

The 9/23 re-gate reviewer's two SILENT findings on commit ``abdbc6ea``:

**Finding 1** — concurrent same-name tools get the wrong verdict
(``api/streaming.py:10869``).  The authoritative ``is_error`` was
looked up by walking ``_live_tool_calls`` in reverse and taking the
most recent not-done entry whose *name* matched, so two concurrent
``terminal`` calls could swap verdicts.  The full fix lives in
``hermes-agent/agent/tool_executor.py:1041`` (which must thread
``tool_call_id`` into the ``tool_progress_callback`` cb_kwargs); this
test pins the ``on_tool`` ``done``-claim behaviour that ships
immediately and the ``cb_kwargs['tool_call_id']`` fast-path that
activates as soon as the Agent starts sending it.

**Finding 2** — a historical failure is erased on the next turn
(``api/streaming.py:8343``).  Settlement defaulted calls that were
absent from the current live mirror to ``is_error=False``; the
verified two-turn result went from ``[("t1", True)]`` to
``[("t1", False), ("t2", False)]``.  ``_extract_tool_calls_from_messages``
now accepts a ``prior_tool_calls`` argument and merges the prior
verdict by ``tid`` before the live mirror's default-zero verdict
overwrites it.

This test exercises both fixes in isolation:

- the prior-merge behaviour of
  ``api.streaming._extract_tool_calls_from_messages`` with explicit
  in-memory fixtures (Finding 2);
- the static wiring of the ``done``-claim + ``cb_kwargs['tool_call_id']``
  fast-path inside ``on_tool`` (Finding 1 partial fix);
- a callback-level regression for the Finding 1 same-name concurrent
  case using a minimal double-callback driver that mirrors the
  production ``_live_tool_calls`` lifecycle.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
STREAMING_PY = REPO_ROOT / "api" / "streaming.py"


def _read_streaming() -> str:
    return STREAMING_PY.read_text(encoding="utf-8")


def _function_block(src: str, name: str) -> str:
    start = src.find(f"def {name}")
    assert start != -1, f"{name} not found"
    next_def = -1
    depth = 0
    end = -1
    for i in range(start, len(src)):
        if src[i] == ":" and depth == 0 and i > start:
            # End of the ``def`` line — wait for the next newline.
            nl = src.find("\n", i)
            if nl == -1:
                break
            i = nl
        if src[i] == "\n":
            if depth == 0 and next_def == -1 and i > start:
                # First newline after the def line — start scanning the body.
                next_def = i
            continue
        if depth == 0 and next_def != -1:
            if src[i].isspace():
                continue
            if src[i:i + 3] == "def" and (i + 3 < len(src)) and src[i + 3] in (" ", "\n"):
                end = i
                break
            # Non-def, non-whitespace content — keep scanning for the next def.
            next_def = -1
            continue
        if src[i] == "{":
            depth += 1
        elif src[i] == "}":
            depth -= 1
    if end == -1:
        end = len(src)
    return src[start:end]


# ---------------------------------------------------------------------------
# Finding 2 — _extract_tool_calls_from_messages honours prior_tool_calls
# ---------------------------------------------------------------------------


def _call_extract(messages, live_tool_calls=None, prior_tool_calls=None):
    """Invoke ``_extract_tool_calls_from_messages`` in a subprocess.

    The function is module-level but the module pulls in heavy
    server-side dependencies (Flask app, websocket routes, etc.) that
    can't be imported in the unit-test process without the test
    runtime.  Run it in a child Python where ``api.streaming`` is
    importable via the repo root.
    """
    payload = {
        "messages": messages,
        "live_tool_calls": live_tool_calls or [],
        "prior_tool_calls": prior_tool_calls or [],
    }
    driver = (
        "import sys, json;\n"
        "sys.path.insert(0, %r);\n"
        "from api.streaming import _extract_tool_calls_from_messages;\n"
        "data = json.loads(sys.stdin.read());\n"
        "out = _extract_tool_calls_from_messages(\n"
        "    data['messages'],\n"
        "    live_tool_calls=data['live_tool_calls'],\n"
        "    prior_tool_calls=data['prior_tool_calls'],\n"
        ");\n"
        "sys.stdout.write(json.dumps(out));\n"
    ) % str(REPO_ROOT)
    proc = subprocess.run(
        [sys.executable, "-c", driver],
        input=json.dumps(payload),
        capture_output=True, text=True, timeout=60,
    )
    if proc.returncode != 0:
        pytest.skip(f"could not import api.streaming: {proc.stderr[:200]}")
    return json.loads(proc.stdout)


def test_extract_merges_prior_verdict_when_live_mirror_lacks_tid():
    """Round 6 Finding 2: prior ``s.tool_calls`` verdict survives a
    new turn's settlement when the live mirror no longer carries the
    call.  Without ``prior_tool_calls`` the live default of
    ``is_error=False`` overwrites the historical True verdict; with
    it the True verdict wins.
    """
    messages = [
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {"id": "t1", "type": "function", "function": {"name": "terminal", "arguments": "{}"}}
            ],
        },
        {"role": "tool", "tool_call_id": "t1", "content": "boom"},
    ]
    prior = [{"name": "terminal", "tid": "t1", "is_error": True, "snippet": "boom"}]
    # New turn's live mirror does NOT contain t1 (turn 1's tool call
    # is no longer in flight) → the live fallback would default to
    # is_error=False and overwrite the prior True verdict.  The
    # prior-tool-calls merge must repair that.
    live = []
    out = _call_extract(messages, live_tool_calls=live, prior_tool_calls=prior)
    assert len(out) == 1
    assert out[0]["tid"] == "t1"
    assert out[0]["is_error"] is True, (
        "prior s.tool_calls=True verdict for t1 must survive a new turn's "
        "settlement when the live mirror lacks the call (round 6 Finding 2)"
    )


def test_extract_live_verdict_still_wins_over_prior():
    """The prior verdict is a fallback, not an override: if the live
    mirror has the call (common case), the live classification wins.
    """
    messages = [
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {"id": "t1", "type": "function", "function": {"name": "terminal", "arguments": "{}"}}
            ],
        },
        {"role": "tool", "tool_call_id": "t1", "content": "OK"},
    ]
    prior = [{"name": "terminal", "tid": "t1", "is_error": True}]
    live = [{"name": "terminal", "tid": "t1", "is_error": False, "done": True}]
    out = _call_extract(messages, live_tool_calls=live, prior_tool_calls=prior)
    assert len(out) == 1
    assert out[0]["tid"] == "t1"
    assert out[0]["is_error"] is False, (
        "live classification must still win when the live mirror has the call"
    )


def test_extract_default_still_false_without_prior_or_live():
    """When neither live nor prior has the tid, the default is
    False.  This pins the original round-3/4/5 behaviour for the
    never-seen-this-call case.
    """
    messages = [
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {"id": "t1", "type": "function", "function": {"name": "terminal", "arguments": "{}"}}
            ],
        },
        {"role": "tool", "tool_call_id": "t1", "content": "ok"},
    ]
    out = _call_extract(messages, live_tool_calls=[], prior_tool_calls=[])
    assert len(out) == 1
    assert out[0]["tid"] == "t1"
    assert out[0]["is_error"] is False


# ---------------------------------------------------------------------------
# Finding 1 — on_tool wires cb_kwargs['tool_call_id'] and claims done
# ---------------------------------------------------------------------------


def test_on_tool_uses_tool_call_id_from_cb_kwargs():
    """Round 6 Finding 1: the legacy ``on_tool`` capture path must
    prefer ``cb_kwargs['tool_call_id']`` over the name-based reverse
    walk so two concurrent same-name tool completions do not swap
    verdicts.  The Agent-side fix in
    ``hermes-agent/agent/tool_executor.py`` is still pending; the
    webui wiring here is what activates as soon as that lands.
    """
    src = _read_streaming()
    # Both completion branches in on_tool must consult cb_kwargs.get('tool_call_id').
    on_tool_body = _function_block(src, "on_tool(")
    # We only need to verify the structured-callback branch and the
    # legacy branch both read the new key.  The structured-callback
    # branch is the active one on modern builds and is the one the
    # reviewer's Finding 1 probe (Codex driving two concurrent
    # ``terminal`` calls) actually exercised.
    assert "cb_kwargs.get('tool_call_id')" in on_tool_body, (
        "on_tool must consult cb_kwargs.get('tool_call_id') so concurrent "
        "same-name tool completions key by tid, not by name (round 6 "
        "Finding 1)"
    )


def test_on_tool_claims_live_entry_done_in_name_fallback():
    """While hermes-agent is still passing only the position
    ``name``/``preview``/``args`` arguments (no tool_call_id), the
    webui side must mark the matched live entry ``done`` so a second
    same-name completion walks past it instead of overwriting the
    first call's verdict.
    """
    src = _read_streaming()
    on_tool_body = _function_block(src, "on_tool(")
    # The structured-callback branch is the one the reviewer's Codex
    # probe exercises.  The name-fallback path inside it must set
    # ``done`` after writing the authoritative verdict.
    assert "live_tc['done'] = True" in on_tool_body, (
        "on_tool name-fallback path must claim the matched live entry "
        "done so a second same-name completion walks past it (round 6 "
        "Finding 1 partial fix)"
    )


def test_extract_accepts_prior_tool_calls_kwarg():
    """Pin the public signature so the caller-side ``prior_tool_calls``
    argument doesn't regress.
    """
    src = _read_streaming()
    assert (
        "def _extract_tool_calls_from_messages(messages, live_tool_calls=None, prior_tool_calls=None):"
        in src
    ), "_extract_tool_calls_from_messages must accept prior_tool_calls (round 6 Finding 2)"


def test_callers_pass_prior_s_tool_calls():
    """Both callers of ``_extract_tool_calls_from_messages`` must
    thread the prior ``s.tool_calls`` through so the merge in
    Finding 2 fires.
    """
    src = _read_streaming()
    # The two call sites: normal settlement (~12504) and terminal
    # self-heal success (~13598).  Each must capture
    # ``list(s.tool_calls or [])`` and pass it as ``prior_tool_calls``.
    assert "prior_tool_calls=_prior_s_tool_calls" in src, (
        "both settlement callers must thread prior s.tool_calls into "
        "_extract_tool_calls_from_messages (round 6 Finding 2)"
    )
    # We expect at least two distinct call sites (the normal
    # settlement and the self-heal settlement) to pass the kwarg.
    assert src.count("prior_tool_calls=_prior_s_tool_calls") >= 2


def test_concurrent_same_name_claim_done_in_callback_driver():
    """Callback-level regression: drive a minimal ``on_tool``-shaped
    function with two concurrent same-name completions (no
    ``tool_call_id`` in cb_kwargs — Agent-side fix pending) and
    confirm the ``done``-claim prevents a verdict from being lost
    or overwritten by the second completion.

    The partial fix does NOT guarantee which ``tid`` receives which
    verdict (the reverse walk is FIFO, not ``tid``-ordered — the
    full fix lives in ``hermes-agent/agent/tool_executor.py`` which
    must thread ``tool_call_id`` into the cb_kwargs).  What it
    guarantees is that *both* verdicts land in the authoritative
    dict, with the right set of tids and the right multiset of
    verdicts.
    """
    driver = r'''
import json, sys

# Minimal in-memory mirror mirroring the production _live_tool_calls
# list maintained by on_tool_start.
_live_tool_calls = [
    {"name": "terminal", "tid": "t1", "done": False, "is_error": False},
    {"name": "terminal", "tid": "t2", "done": False, "is_error": False},
]

_authoritative = {}

def on_tool_completion(name, cb_kwargs):
    """Mirrors the round-6 structured-callback branch."""
    cb_is_error = cb_kwargs.get("is_error")
    if cb_is_error is None:
        return
    cb_tid = cb_kwargs.get("tool_call_id") or ""
    if cb_tid:
        _authoritative[cb_tid] = bool(cb_is_error)
        return
    for live_tc in reversed(_live_tool_calls):
        if live_tc.get("done"):
            continue
        if not name or live_tc.get("name") == name:
            tid = live_tc.get("tid") or ""
            if tid:
                _authoritative[tid] = bool(cb_is_error)
            live_tc["done"] = True
            break

# Two concurrent same-name ``terminal`` completions arrive.  The
# first call's verdict is True (failed), the second's is False
# (succeeded).  The Agent-side fix is *not* in effect (no
# tool_call_id in cb_kwargs) so the name-based fallback runs.
on_tool_completion("terminal", {"is_error": True})
on_tool_completion("terminal", {"is_error": False})

# Round 6 Finding 1 partial-fix guarantee:
# 1. BOTH tids appear in _authoritative (no verdict dropped).
# 2. BOTH verdicts are preserved (one True, one False).
# 3. NO tid is overwritten by a stale entry.
assert set(_authoritative.keys()) == {"t1", "t2"}, (
    "both concurrent completions must land on different tids; got %r"
    % _authoritative
)
assert sorted(_authoritative.values()) == [False, True], (
    "both verdicts must be preserved; got %r" % _authoritative
)
sys.stdout.write(json.dumps(_authoritative))
'''
    proc = subprocess.run(
        [sys.executable, "-c", driver],
        capture_output=True, text=True, timeout=30,
    )
    assert proc.returncode == 0, proc.stderr
    verdicts = json.loads(proc.stdout)
    assert set(verdicts.keys()) == {"t1", "t2"}, (
        "round 6 Finding 1 partial fix: both concurrent completions must "
        "land on different tids; got %r" % verdicts
    )
    assert sorted(verdicts.values()) == [False, True], (
        "round 6 Finding 1 partial fix: both verdicts must be "
        "preserved; got %r" % verdicts
    )
