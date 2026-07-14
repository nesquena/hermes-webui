"""Regression test for #5940 — surface the real non-retryable provider error.

The Agent aborts a non-retryable API error (e.g. HTTP 400 "invalid model / no
credentials") by emitting `❌ Non-retryable error (HTTP <code>): <detail>` through
its lifecycle `status_callback`. The WebUI received that message but dropped it
(only compression + fallback lifecycle messages were forwarded), and the run
result / `agent._last_error` were empty for that abort path — so turn completion
fell through to the misleading `no_response` "silent rate limit, try again"
message.

The fix captures the terminal error in `_agent_status_callback` and seeds
`_last_err` with it at turn completion so `_classify_provider_error` reports the
real, actionable cause instead of the generic fallback.

Two layers are covered:
  1. behavioral — the classifier buckets the captured message correctly;
  2. wiring — the capture container + seed are present in the streaming source.
"""
from pathlib import Path

from api import streaming

ROOT = Path(__file__).resolve().parents[1]
STREAMING_PY = (ROOT / "api" / "streaming.py").read_text(encoding="utf-8")


# The exact shape the Agent emits (agent/conversation_loop.py:3780) wrapping the
# provider's HTTP 400 detail.
_INVALID_MODEL_MSG = (
    "\u274c Non-retryable error (HTTP 400): HTTP 400: "
    '{"detail":"Invalid Request: Invalid model format or no credentials for provider: x"}'
)


def test_captured_terminal_error_classifies_as_model_not_found_not_no_response():
    """An 'invalid model' non-retryable error must classify as model_not_found —
    NOT the misleading no_response 'try again in a moment' fallback."""
    classified = streaming._classify_provider_error(
        _INVALID_MODEL_MSG, Exception(_INVALID_MODEL_MSG)
    )
    assert classified["type"] == "model_not_found", classified
    assert classified["type"] != "no_response"
    # Actionable guidance, and NOT the "try again" retry advice.
    assert "hermes model" in classified["hint"].lower() or "settings" in classified["hint"].lower()
    assert "try again in a moment" not in classified["hint"].lower()


def test_captured_credential_error_classifies_as_auth_not_no_response():
    """A credential-problem non-retryable error surfaces as an auth issue, not
    a silent-rate-limit no_response."""
    msg = "\u274c Non-retryable error (HTTP 401): invalid api key for provider x"
    classified = streaming._classify_provider_error(msg, Exception(msg))
    assert classified["type"] == "auth_mismatch", classified
    assert classified["type"] != "no_response"


def test_no_response_reserved_for_genuinely_empty_completion():
    """With no captured terminal error, an empty completion still classifies as
    no_response (the fallback is reserved for genuine silent completions)."""
    classified = streaming._classify_provider_error("", None, silent_failure=True)
    assert classified["type"] == "no_response"


def test_status_callback_captures_terminal_error():
    """The status-callback bridge must capture a non-retryable terminal error
    into the turn-local container (not drop it like other lifecycle messages)."""
    assert "_captured_terminal_error = [None]" in STREAMING_PY, (
        "the turn-local capture container must be declared before _agent_status_callback"
    )
    # capture condition matches the Agent's emitted 'non-retryable error (HTTP ...)' shape
    assert "'non-retryable error' in _lower" in STREAMING_PY
    assert "_captured_terminal_error[0] = _message" in STREAMING_PY


def test_captured_terminal_error_seeds_last_err_on_completion():
    """At turn completion, the captured terminal error must seed `_last_err` when
    the agent/result carried no error — so the classifier runs on the real cause
    instead of silent_failure=True."""
    assert "_captured_terminal_failure = bool(_captured_terminal_error[0])" in STREAMING_PY
    assert "if not _last_err and _captured_terminal_failure:" in STREAMING_PY
    assert "_last_err = _captured_terminal_error[0]" in STREAMING_PY
