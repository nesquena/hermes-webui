"""Regression guard for the #3737 explicit-pick CLIENT clear-timing fix (PR #3739 + Codex catch).

The server-side resolver coverage lives in test_provider_mismatch.py
(test_explicit_pick_survives_profile_family_mismatch / _false_allows_normalization).

This file locks the CLIENT-side wiring that makes the flag actually engage in the
normal flow. Codex found that boot.js modelSelect.onchange cleared the pending
explicit-pick marker right after /api/session/update, so by the time send() ran the
marker was gone, _explicitPick was false, and the server reverted the cross-family
pick anyway — the flag only worked in a rare race. The fix:

  * onchange RECORDS the pick (_rememberPendingSessionModel) and must NOT clear it;
  * send() consumes it (reads, then _clearPendingSessionModel) for that send only.

These are static source-structure assertions (the flow is DOM/network-driven and
exercised live); they keep the clear-timing from silently regressing.
"""
from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
BOOT_JS = (REPO / "static" / "boot.js").read_text(encoding="utf-8")
MESSAGES_JS = (REPO / "static" / "messages.js").read_text(encoding="utf-8")


def _model_onchange_region() -> str:
    """The modelSelect.onchange handler body where the pending pick is recorded and
    the /api/session/update round-trip happens."""
    idx = BOOT_JS.find("_rememberPendingSessionModel")
    assert idx != -1, "_rememberPendingSessionModel call not found in boot.js"
    # Window from the remember call through the session-update POST + its aftermath.
    return BOOT_JS[idx: idx + 1200]


def test_onchange_records_pending_pick():
    region = _model_onchange_region()
    assert "_rememberPendingSessionModel(" in region, (
        "modelSelect.onchange must record the explicit pick so send() can detect it"
    )


def test_onchange_does_not_clear_pending_pick_after_session_update():
    """The premature clear (the #3737 bug) must be gone: onchange must NOT call
    _clearPendingSessionModel after the /api/session/update POST."""
    region = _model_onchange_region()
    assert "_clearPendingSessionModel" not in region, (
        "modelSelect.onchange must NOT clear the pending explicit-pick marker — it has "
        "to survive until send() consumes it, else the normal pick→update→send flow "
        "loses the explicit-pick signal and the server re-reverts the pick (#3737)"
    )


def test_send_consumes_pending_pick_after_reading_it():
    """send() must clear the marker once it has read a matching pending pick, so a
    later send of an unchanged dropdown is not treated as a fresh explicit pick."""
    idx = MESSAGES_JS.find("_explicitPick")
    assert idx != -1, "_explicitPick not computed in send()"
    region = MESSAGES_JS[idx: idx + 1100]
    assert "_clearPendingSessionModel(activeSid)" in region, (
        "send() must consume (clear) the pending explicit-pick marker after reading it"
    )
    # The clear must be gated on a genuine, matching pending pick (not on the
    # broadened _explicitPick which may also be true from a cross-provider
    # inference without a pending marker to consume).
    assert re.search(r"_pendingPickMatch\s*&&[^\\n]*_clearPendingSessionModel", region), (
        "the consume-clear must be gated on _pendingPickMatch (a genuine, matching pending pick)"
    )


def test_send_sends_flag_only_when_explicit():
    idx = MESSAGES_JS.find("_explicitPick")
    region = MESSAGES_JS[idx: idx + 1300]
    assert "explicit_model_pick:_explicitPick||undefined" in region.replace(" ", ""), (
        "the chat/start payload must send explicit_model_pick only when truthy"
    )
