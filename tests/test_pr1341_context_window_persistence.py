"""Regression test for PR #1341 + Opus pre-release review of v0.50.246.

PR #1341 added context_length/threshold_tokens/last_prompt_tokens fields to
the Session model — but didn't add the writer that actually populates them
during streaming. The pre-release review caught this: without the writer,
the user-visible bug (context-ring shows 0% after page reload) would NOT
have been fixed by #1341 alone.

This test verifies that:
1. After a streaming turn completes, the session's context_length /
   threshold_tokens / last_prompt_tokens are written from the agent's
   compressor BEFORE s.save() is called (so they land on disk).
2. GET /api/session response includes the populated values.
3. A reloaded session retains the populated values.

Implementation reference: api/streaming.py around line 2188 (the per-turn
post-merge save) writes from getattr(agent, 'context_compressor', None).
"""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STREAMING = ROOT / "api" / "streaming.py"
MODELS = ROOT / "api" / "models.py"
ROUTES = ROOT / "api" / "routes.py"


def test_streaming_persists_context_fields_on_session_before_save():
    """The post-merge per-turn save block must write the three fields to the
    session BEFORE calling s.save(), otherwise the values never reach disk."""
    src = STREAMING.read_text(encoding="utf-8")

    # Find the post-merge save block — anchored on the unique reasoning trace
    # marker right above the persistence block.
    block_start = src.find("if _reasoning_text and s.messages:")
    assert block_start != -1, "Reasoning-trace marker not found in streaming.py"

    # Save call follows shortly after
    save_call = src.find("\n                s.save()", block_start)
    assert save_call != -1, "s.save() not found after the post-merge marker"
    assert save_call - block_start < 4200, (
        "s.save() should be close to the post-merge marker — block expanded unexpectedly. "
        "If you've added a new pre-save mutation block here, bump this limit."
    )

    block = src[block_start:save_call]

    # The three fields must all be assigned on s within this block
    assert "s.context_length" in block, (
        "s.context_length must be written before s.save() in the post-merge block"
    )
    assert "s.threshold_tokens" in block, (
        "s.threshold_tokens must be written before s.save() in the post-merge block"
    )
    assert "s.last_prompt_tokens" in block, (
        "s.last_prompt_tokens must be written before s.save() in the post-merge block"
    )

    # The values must come from the agent's context_compressor
    assert "context_compressor" in block, (
        "Values must be sourced from agent.context_compressor"
    )


def test_session_init_accepts_context_fields():
    """Session.__init__ must accept the three fields as named kwargs."""
    src = MODELS.read_text(encoding="utf-8")
    # The init signature spans many lines — read the full def block
    init_match = re.search(r"def __init__\(self,(.*?)\):", src, re.DOTALL)
    assert init_match, "Session.__init__ signature not found"
    sig = init_match.group(1)
    assert "context_length" in sig, "Session.__init__ must accept context_length"
    assert "threshold_tokens" in sig, "Session.__init__ must accept threshold_tokens"
    assert "last_prompt_tokens" in sig, "Session.__init__ must accept last_prompt_tokens"


def test_session_metadata_fields_includes_context_fields():
    """Session.save() METADATA_FIELDS must include all three for round-trip persistence."""
    src = MODELS.read_text(encoding="utf-8")
    # Locate METADATA_FIELDS list
    meta_match = re.search(
        r"METADATA_FIELDS\s*=\s*\[(.*?)\]",
        src,
        re.DOTALL,
    )
    assert meta_match, "METADATA_FIELDS list not found in Session.save"
    fields = meta_match.group(1)
    assert "'context_length'" in fields, "METADATA_FIELDS must include 'context_length'"
    assert "'threshold_tokens'" in fields, "METADATA_FIELDS must include 'threshold_tokens'"
    assert "'last_prompt_tokens'" in fields, "METADATA_FIELDS must include 'last_prompt_tokens'"


def test_session_compact_exposes_context_fields():
    """Session.compact() must include the three fields in its output dict."""
    src = MODELS.read_text(encoding="utf-8")
    # Find compact() method body
    compact_idx = src.find("def compact(")
    assert compact_idx != -1, "Session.compact not found"
    # Look ahead for the next def or 200 lines
    end = src.find("\n    def ", compact_idx + 1)
    body = src[compact_idx:end if end != -1 else compact_idx + 4000]

    assert "'context_length':" in body, "compact() must include context_length"
    assert "'threshold_tokens':" in body, "compact() must include threshold_tokens"
    assert "'last_prompt_tokens':" in body, "compact() must include last_prompt_tokens"


def test_routes_session_get_returns_context_fields():
    """GET /api/session response must include the three fields."""
    src = ROUTES.read_text(encoding="utf-8")
    # The session-detail response builder uses getattr(s, ..., 0) or 0 pattern.
    # Look for the three keys in the same response shape.
    assert '"context_length"' in src, "GET /api/session response must include context_length"
    assert '"threshold_tokens"' in src, "GET /api/session response must include threshold_tokens"
    assert '"last_prompt_tokens"' in src, "GET /api/session response must include last_prompt_tokens"


def test_session_round_trip_persists_context_fields(tmp_path, monkeypatch):
    """Real round-trip: save a Session with the fields set, reload, fields still there.

    Patches SESSION_DIR on the live api.models module so we don't pollute
    sys.modules state and break test ordering for sibling tests that depend
    on a stable api.models import (e.g. test_session_sidecar_repair.py).
    """
    from api import models

    # Use tmp_path as the session dir for this test only
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(models, "SESSION_DIR", sessions_dir)

    s = models.Session(session_id="ctxtest1", title="Context test")
    s.context_length = 200000
    s.threshold_tokens = 180000
    s.last_prompt_tokens = 45123
    s.save()

    # Reload from disk
    s2 = models.Session.load("ctxtest1")
    assert s2 is not None, "Session should reload"
    assert s2.context_length == 200000, f"context_length lost on reload: got {s2.context_length}"
    assert s2.threshold_tokens == 180000, f"threshold_tokens lost on reload: got {s2.threshold_tokens}"
    assert s2.last_prompt_tokens == 45123, f"last_prompt_tokens lost on reload: got {s2.last_prompt_tokens}"
