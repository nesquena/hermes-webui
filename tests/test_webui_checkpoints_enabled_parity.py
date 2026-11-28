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
        # Quoted / rendered strings: only the project's truthy spellings are on.
        # `bool("false")` is True, which would switch checkpointing ON for a
        # user who wrote the opposite.
        ({"checkpoints": {"enabled": "false"}}, False),
        ({"checkpoints": {"enabled": "off"}}, False),
        ({"checkpoints": {"enabled": "0"}}, False),
        ({"checkpoints": {"enabled": "no"}}, False),
        ({"checkpoints": {"enabled": "true"}}, True),
        ({"checkpoints": {"enabled": "on"}}, True),
        # Unrecognised value stays off, so a typo cannot start writing snapshots.
        ({"checkpoints": {"enabled": "maybe"}}, False),
        # Not a dict at all.
        ("nope", False),
        (42, False),
    ],
)
def test_resolve_checkpoints_enabled(config_data, expected):
    assert _resolve(config_data) is expected


def test_one_resolved_value_feeds_both_constructor_and_signature():
    """The flag must be resolved once and feed both consumers identically.

    Two independent resolutions could drift — the constructor gets one value
    while the signature hashes another — which would silently keep reusing a
    cached agent after a config flip. That is the regression this test pins: the
    same resolved boolean has to reach `AIAgent(checkpoints_enabled=...)` and
    `_compute_agent_cache_signature(checkpoints_enabled=...)`.
    """
    from api.streaming import _compute_agent_cache_signature, _resolve_checkpoints_enabled

    base = dict(
        resolved_model="m", resolved_api_key="k", resolved_base_url="u",
        resolved_provider="p", runtime_bundle={},
        prefill_context={"status": "not_configured"},
    )

    for config_data in (
        {"checkpoints": {"enabled": True}},
        {"checkpoints": {"enabled": False}},
        {"checkpoints": True},
        {"checkpoints": False},
        {},
        None,
    ):
        resolved = _resolve_checkpoints_enabled(config_data)

        # Both consumers take the ONE resolved value. Asserting
        # `ctor_arg = resolved` here would be circular; the production body is
        # pinned separately by test_production_resolves_the_flag_once.
        signature = _compute_agent_cache_signature(**base, checkpoints_enabled=resolved)

        # A signature built from the opposite value must differ, proving the
        # signature actually tracks this flag rather than ignoring it.
        opposite = _compute_agent_cache_signature(**base, checkpoints_enabled=not resolved)
        assert signature != opposite, (
            f"signature ignores the checkpoint flag for {config_data!r}"
        )


def test_signature_absents_match_default_off():
    """An absent flag must hash like an explicit False (no needless rebuild)."""
    from api.streaming import _compute_agent_cache_signature

    base = dict(
        resolved_model="m", resolved_api_key="k", resolved_base_url="u",
        resolved_provider="p", runtime_bundle={},
        prefill_context={"status": "not_configured"},
    )
    assert _compute_agent_cache_signature(**base) == _compute_agent_cache_signature(
        **base, checkpoints_enabled=False
    )


def test_production_resolves_the_flag_once():
    """`_run_agent_streaming` must resolve once and reuse that value.

    A second `_resolve_checkpoints_enabled(...)` call inside the streaming
    worker — for the signature or anywhere else — is how the constructor and the
    cache signature drift apart. This asserts the production body contains
    exactly one resolution and that both consumers read its result.
    """
    import inspect
    import re

    from api import streaming

    body = inspect.getsource(streaming._run_agent_streaming)
    assignments = re.findall(
        r"^\s*_checkpoints_enabled\s*=\s*_resolve_checkpoints_enabled\(", body, re.M
    )
    assert len(assignments) == 1, (
        f"expected exactly one resolution, found {len(assignments)}"
    )
    # Both consumers must read the resolved variable, not re-resolve.
    assert "_agent_kwargs['checkpoints_enabled'] = _checkpoints_enabled" in body
    assert "checkpoints_enabled=_checkpoints_enabled" in body
    assert body.count("_resolve_checkpoints_enabled(") == 1, (
        "the worker must not re-resolve the flag"
    )


@pytest.mark.parametrize("isolated_value", [True, False, None], ids=["enabled", "disabled", "read-error"])
def test_worker_separates_toolset_and_checkpoint_config(monkeypatch, isolated_value):
    from api import config
    from tests import test_issue4251_wakeup_model_picker_race as harness

    session = harness.FakeSession(model="haiku-4-5")
    session.session_id = "checkpoint-config-authority"
    session.profile = "research"
    captured = []

    class Agent(harness.CapturingAgent):
        def __init__(self, checkpoints_enabled=None, enabled_toolsets=None, **kwargs):
            captured.append((checkpoints_enabled, enabled_toolsets))
            super().__init__(enabled_toolsets=enabled_toolsets, **kwargs)

    def read_config(home, *, isolate_config_override=False):
        if isolate_config_override:
            if isolated_value is None:
                raise OSError("isolated config unavailable")
            return {"checkpoints": {"enabled": isolated_value}, "toolsets": ["isolated"]}
        return {"checkpoints": {"enabled": not isolated_value}, "toolsets": ["ambient"]}

    install = harness._install_streaming_harness

    def install_config(mp, fake_session):
        install(mp, fake_session)
        mp.setattr(streaming, "_get_ai_agent", lambda: Agent)
        mp.setattr(config, "get_config_for_profile_home", read_config)
        mp.setattr(config, "get_config", lambda: {"checkpoints": {"enabled": True}})
        mp.setattr(config, "_resolve_cli_toolsets", lambda cfg: cfg["toolsets"])

    monkeypatch.setattr(harness, "_install_streaming_harness", install_config)
    monkeypatch.setattr(config, "SESSION_AGENT_CACHE", {})
    harness._run_streaming_turn(monkeypatch, session, stream_id="checkpoint-config-turn")
    assert captured == [(isolated_value is True, ["ambient"])]
