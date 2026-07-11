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
    needle = f"async function {name}"
    start = src.index(needle)
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
    assert "_rememberPendingSessionModel" in body, (
        "submitEdit must re-arm the explicit-pick marker (#5924); otherwise the "
        "recovery send loses explicit_model_pick and the server re-reverts the model"
    )
    rearm_idx = body.index("_rememberPendingSessionModel")
    send_idx = body.rindex("await send()")
    assert rearm_idx < send_idx, "the re-arm must happen BEFORE await send()"


def test_cmd_retry_rearms_pending_pick_before_send():
    """/retry must re-arm the explicit-pick marker before ``await send()``."""
    body = _function_body(COMMANDS_JS, "cmdRetry")
    assert "_rememberPendingSessionModel" in body, (
        "cmdRetry must re-arm the explicit-pick marker (#5924); otherwise /retry "
        "re-sends the failed model instead of the freshly-picked one"
    )
    rearm_idx = body.index("_rememberPendingSessionModel")
    send_idx = body.rindex("await send()")
    assert rearm_idx < send_idx, "the re-arm must happen BEFORE await send()"


def test_recovery_rearm_reads_current_selector_state():
    """The re-arm must source the CURRENT selector/session model, not a stale const.

    Sourcing from ``_chatPayloadModel()`` is what makes a *freshly-picked* model
    (typed into the selector after the failure) win on the recovery send.
    """
    for body in (_function_body(UI_JS, "submitEdit"), _function_body(COMMANDS_JS, "cmdRetry")):
        assert "_chatPayloadModel()" in body
        assert "_chatPayloadModelProvider(" in body


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
