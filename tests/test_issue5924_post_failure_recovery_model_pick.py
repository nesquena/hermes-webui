"""Regression coverage for #5924 — post-failure recovery must honor a fresh pick.

After a provider failure the reporter (@b3nw) could not switch models: changing
the model in the selector and then **edit-resubmit** or **/retry** re-sent the
*failed* model, forcing a session fork to escape.

Root cause (Facet 1 + Facet 4): the onchange explicit-pick marker
(``_rememberPendingSessionModel``) is single-shot — ``send()`` consumes it once
(``messages.js``). The two recovery paths (``submitEdit`` in ``ui.js`` and
``cmdRetry`` in ``commands.js``) truncate and call ``send()`` directly WITHOUT
re-arming the marker, so ``explicit_model_pick`` goes out ``false`` and the
server's ``_resolve_compatible_session_model_state`` re-reverts a freshly-picked
cross-family model back to the profile default.

Two-layer invariant pinned here:
  * WebUI: both recovery paths re-arm the pending explicit-pick marker from the
    CURRENT selector state *before* ``await send()`` (so a recovery send —
    including a SECOND consecutive one — carries ``explicit_model_pick:true``).
  * Server: with ``explicit_model_pick=True`` the fresh cross-family pick is
    honored (NOT reverted), and without it the stale value is still normalized
    (the #3737/#5731 repair path must not regress).
"""

from pathlib import Path

import api.routes as routes

ROOT = Path(__file__).resolve().parents[1]
UI_JS = (ROOT / "static" / "ui.js").read_text(encoding="utf-8")
COMMANDS_JS = (ROOT / "static" / "commands.js").read_text(encoding="utf-8")


def _function_body(src: str, name: str) -> str:
    # Match either "async function NAME" or plain "function NAME".
    start = src.find(f"async function {name}")
    if start == -1:
        start = src.find(f"function {name}")
    if start == -1:
        raise AssertionError(f"function {name!r} not found")
    brace = src.index("{", start)
    depth = 0
    for i in range(brace, len(src)):
        if src[i] == "{":
            depth += 1
        elif src[i] == "}":
            depth -= 1
            if depth == 0:
                return src[start : i + 1]
    raise AssertionError(f"function {name!r} body not found")


# ── WebUI layer: recovery paths re-arm the marker before send() ──────────────


def test_submit_edit_rearms_pending_pick_before_send():
    """Edit-resubmit must re-arm the explicit-pick marker before ``await send()``."""
    body = _function_body(UI_JS, "submitEdit")
    assert "_reArmRecoveryPick(" in body, (
        "submitEdit must re-arm via _reArmRecoveryPick (#5924); otherwise the "
        "recovery send loses explicit_model_pick and the server re-reverts the model"
    )
    rearm_idx = body.index("_reArmRecoveryPick(")
    send_idx = body.rindex("await send()")
    assert rearm_idx < send_idx, "the re-arm must happen BEFORE await send()"


def test_cmd_retry_rearms_pending_pick_before_send():
    """/retry must re-arm the explicit-pick marker before ``await send()``."""
    body = _function_body(COMMANDS_JS, "cmdRetry")
    assert "_reArmRecoveryPick(" in body, (
        "cmdRetry must re-arm via _reArmRecoveryPick (#5924); otherwise /retry "
        "re-sends the failed model instead of the freshly-picked one"
    )
    rearm_idx = body.index("_reArmRecoveryPick(")
    send_idx = body.rindex("await send()")
    assert rearm_idx < send_idx, "the re-arm must happen BEFORE await send()"


def test_recovery_rearm_sources_deliberate_pick_helper():
    """The re-arm must source the deliberate-pick signal from the shared helper.

    Both recovery paths capture ``_deliberateSessionModelPick(<sid>)`` (a non-default
    session-model signal that is inference-free and survives the failed send's marker
    consumption) BEFORE any await, rather than comparing ``_chatPayloadModel()`` to
    itself (which false-negatives an already-applied pick and false-positives on
    provider inference — the round-2 gate finding).
    """
    for body in (_function_body(UI_JS, "submitEdit"), _function_body(COMMANDS_JS, "cmdRetry")):
        assert "_deliberateSessionModelPick(" in body, (
            "recovery paths must derive the pick via _deliberateSessionModelPick"
        )


def test_deliberate_pick_helper_ignores_default_and_inference():
    """The helper only reports a pick for a genuine NON-DEFAULT session model.

    It must key off the session's own model vs the profile default (window._defaultModel
    / _activeProvider) — NOT provider inference on an unchanged model, and NOT a
    self-comparison. A session on the profile default returns null (no re-arm), so the
    server's compatible-model resolution runs for a no-real-pick recovery.
    """
    body = _function_body(UI_JS, "_deliberateSessionModelPick")
    assert "window._defaultModel" in body and "window._activeProvider" in body, (
        "the pick signal must compare against the profile default, not infer a provider"
    )
    assert "return null" in body, "a default-model session must return null (no re-arm)"
    # must NOT resurrect the round-2 false-positive: no provider inference in the signal
    assert "_providerFromModelValue" not in body and "_chatPayloadModelProvider" not in body, (
        "the deliberate-pick signal must not use provider inference (round-2 false-positive)"
    )


# ── Server layer: explicit pick is honored; repair path preserved (#3737) ────


def test_explicit_pick_honors_fresh_cross_family_model_on_recovery():
    """The freshly-picked cross-family model survives when explicit_model_pick=True.

    This is the value the re-armed marker carries into /api/chat/start on the
    recovery send. It must NOT be reverted to the failed/profile-default model.
    """
    effective, provider, changed = routes._resolve_compatible_session_model_state(
        "gpt-5.4-mini",  # freshly picked, cross-family vs anthropic profile
        None,
        profile_provider="anthropic",
        profile_default_model="claude-sonnet-4",
        explicit_model_pick=True,
    )
    assert changed is False, "an explicit recovery pick must not be reverted"
    assert effective == "gpt-5.4-mini", "the freshly-picked model must survive"
    assert provider == "anthropic"


def test_second_consecutive_recovery_send_still_honors_pick():
    """A SECOND consecutive recovery send re-arms the marker, so it stays explicit.

    send() consumes the marker each time, but both recovery paths re-arm it from
    the current selector state on every invocation — so two retries/edits in a
    row both carry explicit_model_pick=True and both honor the pick.
    """
    for _ in range(2):
        effective, provider, changed = routes._resolve_compatible_session_model_state(
            "gpt-5.4-mini",
            None,
            profile_provider="anthropic",
            profile_default_model="claude-sonnet-4",
            explicit_model_pick=True,
        )
        assert changed is False
        assert effective == "gpt-5.4-mini"
        assert provider == "anthropic"


def test_non_explicit_send_still_normalizes_stale_model():
    """Guard against regressing the #3737/#5731 repair path.

    Without an explicit pick (the normal 2nd+-turn continuation), a stale
    cross-family model is still normalized to the profile default. The #5924 fix
    only re-arms on the recovery entry points, so this path is unchanged.
    """
    effective, provider, changed = routes._resolve_compatible_session_model_state(
        "gpt-5.4-mini",
        None,
        profile_provider="anthropic",
        profile_default_model="claude-sonnet-4",
        explicit_model_pick=False,
    )
    assert changed is True, "stale model must still be normalized on a plain send"
    assert effective == "claude-sonnet-4"
    assert provider == "anthropic"


# ── #5924 gate re-fixes: gated re-arm + session-race guards ──────────────────


def test_recovery_rearm_is_gated_on_a_genuine_deliberate_pick():
    """The re-arm must be CONDITIONAL on a real pick, not unconditional.

    Codex round-1 CORE: re-arming on EVERY recovery send (even with no fresh pick)
    forced explicit_model_pick=true and suppressed the server's compatible-model
    resolution. Both recovery paths now derive `_recoveryPick` from the shared
    _deliberateSessionModelPick helper and re-arm only via _reArmRecoveryPick,
    which no-ops on a null/absent pick.
    """
    for body in (_function_body(UI_JS, "submitEdit"), _function_body(COMMANDS_JS, "cmdRetry")):
        assert "_recoveryPick" in body, "recovery re-arm must be gated on _recoveryPick"
        assert "_deliberateSessionModelPick(" in body, (
            "the pick must come from _deliberateSessionModelPick, not an inline comparison"
        )
        assert "_reArmRecoveryPick(" in body, (
            "the re-arm must go through _reArmRecoveryPick (fire-time safety guards)"
        )


def test_rearm_helper_guards_stale_pick_and_newer_marker():
    """_reArmRecoveryPick must not re-arm a stale pick or clobber a newer marker.

    Codex round-3 SILENT: a same-session model change DURING the recovery awaits
    made the pre-await captured pick stale; re-arming it overwrote the newer
    pending marker and restored the old model on a later session load. The helper
    must (1) require the current session model/provider to still equal the pick,
    and (2) not overwrite a different existing pending marker.
    """
    body = _function_body(UI_JS, "_reArmRecoveryPick")
    # still-matches-current-state guard
    assert "S.session.model" in body and "pick.model" in body, (
        "must confirm the current session model still equals the captured pick"
    )
    # newer-marker guard
    assert "_readPendingSessionModel(" in body, (
        "must read the existing pending marker and refuse to clobber a newer one"
    )
    # session-scope guard
    assert "S.session.session_id!==sessionId" in body.replace(" ", ""), (
        "must confirm the session is still the captured one before re-arming"
    )


def test_recovery_pick_is_captured_before_the_first_await():
    """_recoveryPick must be captured BEFORE any awaited network call so a session

    switch during the recovery's round-trips can't make it read the wrong
    session's selector state. The `_recoveryPick` assignment must appear before
    the first awaited call expression (``await api(`` / ``await _ensure`` / ``await send``).
    """
    import re as _re
    for body in (_function_body(UI_JS, "submitEdit"), _function_body(COMMANDS_JS, "cmdRetry")):
        pick_idx = body.index("const _recoveryPick")
        # match a real awaited call, not the word "await" inside a comment
        m = _re.search(r"await\s+\w", body)
        assert m is not None, "expected an awaited call in the recovery path"
        assert pick_idx < m.start(), (
            "_recoveryPick must be captured before the first awaited call (pre-await snapshot)"
        )


def test_recovery_reguards_active_session_after_each_await():
    """After each await, a session-switch guard must re-check the captured sid.

    Codex gate SILENT findings: switching sessions during the retry GET await (or
    the edit truncate await) let session A's recovery intent apply to session B —
    in /retry it wrote B's model into A's pending marker. Each recovery path must
    re-assert its captured session id AFTER its post-network await, before
    mutating messages / re-arming / calling send().
    """
    retry = _function_body(COMMANDS_JS, "cmdRetry")
    # cmdRetry: guard after the session GET await, before the render/re-arm/send
    assert retry.count("S.session.session_id!==activeSid") >= 2, (
        "cmdRetry must re-guard activeSid after the session GET await (>=2 guards total)"
    )
    edit = _function_body(UI_JS, "submitEdit")
    # submitEdit: guard after the truncate await, before slice/re-arm/send
    assert edit.count("S.session.session_id !== initialSid") >= 2, (
        "submitEdit must re-guard initialSid after the truncate await (>=2 guards total)"
    )

