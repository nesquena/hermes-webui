"""Regression coverage for the 2026-11-19 #7925 finding.

``public_session_projection()`` re-ran ``redact_session_data()`` without the
request's captured ``redact_enabled`` value. It therefore read settings again
and could re-redact ``tool_calls`` under a different policy than the one the
delivered ``prefix_fp`` was minted under: turning redaction on mid-load
returned masked cards under an unmasked-card fingerprint, and turning it off
again before the next refresh let that fingerprint pass.
"""

from __future__ import annotations

import inspect
import json

import api.config
import api.helpers
import api.routes


_CREDENTIAL_CMD = "export API_TOKEN=" + "sk-" + "abcdefghijklmnop"


def _session_dict():
    return {
        "session_id": "round11-redact-capture",
        "tool_calls": [
            {
                "name": "terminal",
                "args": {"cmd": _CREDENTIAL_CMD},
                "snippet": _CREDENTIAL_CMD,
            }
        ],
    }


def test_public_projection_honours_the_captured_redact_policy(monkeypatch):
    """Captured OFF must win even when settings now say ON."""
    monkeypatch.setattr(
        api.config, "load_settings", lambda: {"api_redact_enabled": True}
    )
    projected = api.helpers.public_session_projection(
        _session_dict(), redact_enabled=False, _messages_projected=True
    )
    payload = json.dumps(projected["tool_calls"])
    assert _CREDENTIAL_CMD in payload, (
        "the final projection re-read settings and masked tool_calls under a "
        "policy the caller did not capture"
    )


def test_public_projection_still_redacts_when_the_capture_is_on(monkeypatch):
    """Captured ON masks the cards even when settings now say OFF."""
    monkeypatch.setattr(
        api.config, "load_settings", lambda: {"api_redact_enabled": False}
    )
    projected = api.helpers.public_session_projection(
        _session_dict(), redact_enabled=True, _messages_projected=True
    )
    payload = json.dumps(projected["tool_calls"])
    assert _CREDENTIAL_CMD not in payload
    assert "abcdefghijklmnop" not in payload


def test_public_projection_default_still_reads_settings(monkeypatch):
    """Callers that capture nothing keep the historical behaviour."""
    monkeypatch.setattr(
        api.config, "load_settings", lambda: {"api_redact_enabled": True}
    )
    projected = api.helpers.public_session_projection(_session_dict())
    assert _CREDENTIAL_CMD not in json.dumps(projected["tool_calls"])


def test_session_get_threads_the_captured_policy_into_the_final_projection():
    """The GET handler must pass its captured switch, not rely on the default."""
    src = inspect.getsource(api.routes._handle_session_get)
    start = src.index("public_session_projection(")
    call = src[start:start + 400]
    assert "redact_enabled=" in call, (
        "the final projection call lost the captured redact policy"
    )
