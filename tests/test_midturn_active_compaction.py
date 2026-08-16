"""Active-session compaction must reach the browser DURING the turn.

Observed failure (session 20260816_210523_091383, 2026-08-16 21:29 UTC): a
compression fired mid-turn and rotated the session (``messages=4545->549``,
``split_status=rotated_committed``), yet the browser kept rendering the full
pre-compression transcript for the rest of the turn. agent.log shows the
active-reduction sequence never ran that turn:

    Preserved pre-compression session ...   <- absent
    auto tail reduction: applied ...        <- absent
    auto snapshot squash completed ...      <- absent

Root cause: the WebUI's entire compression-detection block (rotation
bookkeeping, ``put('compressed')``, the tail reduction and its
``put('tail_reduced')``) lives in the *turn finalisation* path, after
``run_agent()`` returns. On a long tool-heavy turn that is many minutes away,
so "compact at each compression to keep the WebUI responsive" is not honoured
precisely when it matters most.

Design constraint that shapes this fix
--------------------------------------
Mid-turn, WebUI's ``Session.messages`` is *not* the authoritative
post-compression transcript: the agent owns it and hands it back only when the
turn returns. Truncating ``s.messages`` from the status-callback thread would
race the live turn and risk dropping in-flight state. So the mid-turn path is
deliberately **display-only**: it prunes the browser's DOM down to the live
compaction card and follows the session rotation, while the authoritative
server-side truncation stays exactly where it is, at end of turn.

That is safe because the full pre-compression history is already durable on
disk in the parent session file (periodic checkpoint saves), which is the same
durability property the end-of-turn path checks before pruning.

The trigger is the agent's structured terminal edge:
``status_callback("compacted", COMPACTION_DONE_STATUS)`` emitted by
``agent/conversation_compression.py::_emit_compaction_done``, called from
``_release_lock()`` — i.e. *after* ``agent.session_id`` rotated (line ~3505)
and after the child session was committed. WebUI receives it through
``_agent_status_callback`` while the turn is still streaming, and currently
drops it on the floor: only compression *start* is bridged.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
STREAMING_PY = (REPO / "api" / "streaming.py").read_text(encoding="utf-8")
MESSAGES_JS = (REPO / "static" / "messages.js").read_text(encoding="utf-8")


def _read_agent_constant(name: str) -> str:
    """Read a constant from the installed hermes-agent, if reachable."""
    candidate = Path(
        "/usr/local/lib/hermes-agent/agent/conversation_compression.py"
    )
    if not candidate.exists():
        return ""
    text = candidate.read_text(encoding="utf-8")
    m = re.search(rf'^{name}\s*=\s*"([^"]+)"', text, re.M)
    return m.group(1) if m else ""


def _func_body(src: str, name: str) -> str:
    """Return the source of a top-level def, up to the next top-level def."""
    m = re.search(rf"\ndef {re.escape(name)}\(", src)
    assert m, f"function {name} not found"
    start = m.start()
    nxt = re.search(r"\ndef ", src[start + 1 :])
    end = start + 1 + nxt.start() if nxt else len(src)
    return src[start:end]


# ── The agent contract we key on ────────────────────────────────────────────

def test_agent_emits_a_structured_compaction_done_edge():
    """Pin the upstream contract: kind='compacted' with a stable wording."""
    done_status = _read_agent_constant("COMPACTION_DONE_STATUS")
    if not done_status:
        return  # agent not installed here; nothing to pin
    assert "compaction" in done_status.lower(), (
        f"unexpected agent COMPACTION_DONE_STATUS wording: {done_status!r}"
    )


def test_status_bridge_recognises_compaction_done():
    """The 'compacted' lifecycle kind must be matched, not silently dropped."""
    assert "_is_agent_compression_done_status" in STREAMING_PY, (
        "no matcher for the agent's compaction-complete lifecycle edge; "
        "mid-turn compaction cannot be detected at all"
    )
    body = _func_body(STREAMING_PY, "_is_agent_compression_done_status")
    assert "'compacted'" in body or '"compacted"' in body, (
        "matcher must key on the structural kind='compacted' edge, not on "
        "cosmetic status wording"
    )
    assert "abort" in body or "failed" in body, (
        "a failed/aborted compaction must not be treated as a completed one"
    )


def test_status_callback_acts_on_the_completed_compaction_edge():
    """_agent_status_callback must react mid-turn, not just at start."""
    m = re.search(
        r"def _agent_status_callback\(kind, message\):(.*?)\n    # xsession wakeup",
        STREAMING_PY,
        re.S,
    )
    assert m, "could not isolate _agent_status_callback body"
    body = m.group(1)
    assert "_is_agent_compression_done_status" in body, (
        "the status bridge ignores the compaction-complete edge; the DOM stays "
        "on the full transcript until the turn ends"
    )
    assert "_emit_midturn_compaction_event" in body, (
        "the completed-compaction edge must publish a mid-turn compaction event"
    )


# ── The mid-turn emitter ────────────────────────────────────────────────────

def test_midturn_emitter_requires_a_real_rotation():
    """No rotation (A -> A) means nothing to prune: must be a no-op."""
    body = _func_body(STREAMING_PY, "_emit_midturn_compaction_event")
    assert "session_id" in body, "emitter must compare origin vs continuation"
    assert re.search(r"!=|==", body), (
        "emitter must guard on origin != continuation; without it a "
        "non-rotating compression would prune the DOM for nothing"
    )


def test_midturn_emitter_is_fail_safe_on_durability():
    """Never prune the view unless the full history is durable on disk."""
    body = _func_body(STREAMING_PY, "_emit_midturn_compaction_event")
    assert "_session_transcript_is_durable" in body, (
        "mid-turn pruning must confirm the pre-compression transcript is "
        "already persisted before hiding it from the user"
    )


def test_midturn_emitter_honours_the_same_setting():
    """One user-facing switch governs both the mid-turn and end-of-turn paths."""
    body = _func_body(STREAMING_PY, "_emit_midturn_compaction_event")
    assert "auto_squash_after_compression" in body, (
        "mid-turn compaction must respect the same setting as the end-of-turn "
        "reduction, otherwise turning the feature off no longer works"
    )


def test_midturn_emitter_never_mutates_the_transcript():
    """Mid-turn is display-only: the agent owns the messages until it returns."""
    body = _func_body(STREAMING_PY, "_emit_midturn_compaction_event")
    for forbidden in ("s.messages =", "s.messages=", "s.save()"):
        assert forbidden not in body, (
            f"mid-turn path must not mutate or persist session state "
            f"(found {forbidden!r}); truncation stays at end of turn"
        )


def test_midturn_emitter_publishes_a_dedicated_event():
    """A dedicated event keeps the existing 'compressed'/'tail_reduced' contracts."""
    body = _func_body(STREAMING_PY, "_emit_midturn_compaction_event")
    assert "midturn_compacted" in body, (
        "expected a dedicated 'midturn_compacted' SSE event so existing "
        "listeners keep their current timing and payload"
    )


def test_midturn_event_carries_the_continuation_id():
    """The tab must be able to follow the rotation as soon as it happens."""
    body = _func_body(STREAMING_PY, "_emit_midturn_compaction_event")
    assert "continuation_session_id" in body, (
        "mid-turn event must carry the continuation id so the browser can "
        "follow the rotation without waiting for the terminal 'done' event"
    )


# ── The client side ─────────────────────────────────────────────────────────

def test_client_handles_the_midturn_event():
    """messages.js must listen for it and prune to the compaction card."""
    assert "addEventListener('midturn_compacted'" in MESSAGES_JS, (
        "client has no listener for the mid-turn compaction event"
    )
    m = re.search(
        r"addEventListener\('midturn_compacted',e=>\{(.*?)\n    \}\);",
        MESSAGES_JS,
        re.S,
    )
    assert m, "could not isolate the midturn_compacted handler"
    body = m.group(1)
    assert "compression-card-row" in body or "_midturnPruneAboveCard" in body, (
        "handler must prune the transcript above the live compaction card"
    )
    assert "_setActiveSessionUrl" in body, (
        "handler must follow the session rotation in the address bar"
    )


def test_client_midturn_prune_is_guarded():
    """A missing anchor must leave the DOM untouched (fail-safe)."""
    m = re.search(
        r"addEventListener\('midturn_compacted',e=>\{(.*?)\n    \}\);",
        MESSAGES_JS,
        re.S,
    )
    assert m, "could not isolate the midturn_compacted handler"
    body = m.group(1)
    assert "return" in body, (
        "handler must bail out when it cannot resolve a safe cut point"
    )
