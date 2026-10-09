"""#7904 — colon-tagged models on the generic ``custom`` lane.

``@custom:<segment>:<tag>`` is genuinely ambiguous as a bare string: it is
either generic ``custom`` + model ``<segment>:<tag>`` (the #7904 report) or a
named ``custom:<segment>`` provider + model ``<tag>`` (#6722/#7182 grammar).
So the tag is peeled ONLY on POSITIVE evidence that the selection belongs to
the generic lane:

  * the session's stored ``model_provider == "custom"`` (passed as
    ``session_provider=`` on ``resolve_model_provider`` / ``generic_custom=``
    on the parser), or
  * the picker writing the generic route.

Everything else keeps master's parse — including the three CORE cases from the
2026-10-01 review:

1. ``providers: {custom:<name>: …}`` counts as a configured identity, not just
   the ``custom_providers:`` list, so a named provider keeps its own base_url;
2. a removed provider is NOT silently rerouted to the generic endpoint — it
   keeps ``custom:<name>`` and fails visibly, exactly like master;
3. ``_gateway_model_field()`` resolves configured identities through the
   SESSION's profile config instead of the ambient one.
"""

from unittest.mock import patch

import api.config as config
from api.config import (
    _parse_provider_qualified_model_id,
    model_with_provider_context,
    resolve_model_provider,
)
from api.routes import _clean_session_model_provider, _split_provider_qualified_model

GENERIC_CFG = {
    "model": {"provider": "deepseek", "base_url": "https://api.deepseek.com/v1"},
    "providers": {"custom": {"base_url": "https://generic.example/v1"}},
}


# ── #7904 preserved: generic session + colon tag -> generic endpoint ────────
def test_issue7904_generic_session_peels_tag_to_generic_endpoint():
    """A ``custom`` session's ``@custom:<model>:<tag>`` routes to generic custom."""
    with patch.dict(config.cfg, GENERIC_CFG, clear=True):
        routed = model_with_provider_context("qwen3-30b-a3b:latest", "custom")
        assert routed == "@custom:qwen3-30b-a3b:latest"
        assert resolve_model_provider(routed, session_provider="custom") == (
            "qwen3-30b-a3b:latest",
            "custom",
            "https://generic.example/v1",
        )
        assert resolve_model_provider(
            "@custom:qwen3-30b-a3b:latest", session_provider="custom"
        ) == (
            "qwen3-30b-a3b:latest",
            "custom",
            "https://generic.example/v1",
        )
        assert _parse_provider_qualified_model_id(
            "@custom:qwen3-30b-a3b:latest", generic_custom=True
        ) == ("qwen3-30b-a3b:latest", "custom")


def test_without_positive_evidence_master_parse_is_preserved():
    """Rule: no evidence of the generic lane -> master's parse, no peel."""
    assert _parse_provider_qualified_model_id("@custom:qwen3-30b-a3b:latest") == (
        "latest",
        "custom:qwen3-30b-a3b",
    )
    with patch.dict(config.cfg, GENERIC_CFG, clear=True):
        assert resolve_model_provider("@custom:qwen3-30b-a3b:latest") == (
            "latest",
            "custom:qwen3-30b-a3b",
            None,
        )
    # routes' helpers share the same grammar and stay master-compatible.
    assert _split_provider_qualified_model("@custom:qwen3-30b-a3b:latest") == (
        "latest",
        "custom:qwen3-30b-a3b",
    )
    assert _clean_session_model_provider("@custom:qwen3-30b-a3b:latest") == (
        "custom:qwen3-30b-a3b"
    )


# ── CORE 1: the `providers:` dict form is a configured identity ─────────────
def test_providers_dict_identity_wins_over_generic_evidence():
    """``providers: {custom:omni: …}`` keeps its own base_url, never generic."""
    cfg = {
        "model": {"provider": "deepseek"},
        "providers": {
            "custom:omni": {
                "base_url": "https://omni.example/v1",
                "models": ["latest"],
            },
            "custom": {"base_url": "https://generic.example/v1"},
        },
    }
    expected = ("latest", "custom:omni", "https://omni.example/v1")
    with patch.dict(config.cfg, cfg, clear=True):
        # Plain string, no session evidence (the reviewer's own probe).
        assert resolve_model_provider("@custom:omni:latest") == expected
        # Even with positive generic evidence a CONFIGURED identity must win:
        # absence of a list entry is not evidence that the string meant generic.
        assert resolve_model_provider(
            "@custom:omni:latest", session_provider="custom"
        ) == expected
        # The named session route round-trips to its own endpoint.
        routed = model_with_provider_context("latest", "custom:omni")
        assert routed == "@custom:omni:latest"
        assert resolve_model_provider(
            routed, session_provider="custom:omni"
        ) == expected
        assert _parse_provider_qualified_model_id("@custom:omni:latest") == (
            "latest",
            "custom:omni",
        )


def test_custom_providers_list_identity_still_wins():
    """The ``custom_providers:`` list form keeps working (both config shapes)."""
    cfg = {
        "custom_providers": [
            {"name": "mybox", "base_url": "http://127.0.0.1:8000/v1"},
        ],
        "model": {"provider": "deepseek"},
        "providers": {"custom": {"base_url": "https://generic.example/v1"}},
    }
    with patch.dict(config.cfg, cfg, clear=True):
        assert resolve_model_provider(
            "@custom:mybox:my-model:latest", session_provider="custom"
        ) == ("my-model:latest", "custom:mybox", "http://127.0.0.1:8000/v1")
        assert _parse_provider_qualified_model_id("@custom:mybox:my-model") == (
            "my-model",
            "custom:mybox",
        )
        assert _parse_provider_qualified_model_id("@custom:mybox:my-model:latest") == (
            "my-model:latest",
            "custom:mybox",
        )


# ── CORE 2: a removed provider must fail visibly, never reroute silently ────
def test_removed_named_provider_is_not_silently_rerouted():
    """``custom:retired`` is gone from config -> keep ``custom:retired``, no peel."""
    with patch.dict(config.cfg, GENERIC_CFG, clear=True):
        assert "custom:retired" not in (config.cfg.get("providers") or {})
        assert not config.cfg.get("custom_providers")
        expected = ("latest", "custom:retired", None)
        # Session that still names the removed provider.
        assert resolve_model_provider(
            "@custom:retired:latest", session_provider="custom:retired"
        ) == expected
        # Plain-string probe: same as master — base_url stays None so the
        # downstream lookup raises "not configured" instead of rerouting.
        assert resolve_model_provider("@custom:retired:latest") == expected
        assert _parse_provider_qualified_model_id("@custom:retired:latest") == (
            "latest",
            "custom:retired",
        )


# ── CORE 3: the gateway parses with the SESSION's profile config ────────────
def test_gateway_model_field_reads_session_profile_config(tmp_path):
    """``_gateway_model_field`` resolves identities from the session profile."""
    from api.gateway_chat import _gateway_model_field

    profile_home = tmp_path / "omni-profile"
    profile_home.mkdir()
    (profile_home / "config.yaml").write_text(
        "providers:\n"
        "  custom:omni:\n"
        "    base_url: https://omni.example/v1\n"
        "    models: [latest]\n",
        encoding="utf-8",
    )
    with patch.dict(config.cfg, GENERIC_CFG, clear=True):
        # No evidence at all -> master's parse (model stays `latest`).
        assert _gateway_model_field("@custom:omni:latest") == "latest"
        # Generic session evidence, but the AMBIENT config has no `custom:omni`
        # identity: this is the misparse the review flagged — hence the profile.
        assert _gateway_model_field(
            "@custom:omni:latest", session_provider="custom"
        ) == "omni:latest"
        # The SESSION's profile does define `custom:omni` -> identity wins.
        with patch(
            "api.profiles.get_hermes_home_for_profile",
            return_value=profile_home,
        ):
            assert _gateway_model_field(
                "@custom:omni:latest",
                session_provider="custom",
                profile="omni",
            ) == "latest"
            # ...and an unconfigured slug under the same profile still peels.
            assert _gateway_model_field(
                "@custom:qwen3-30b-a3b:latest",
                session_provider="custom",
                profile="omni",
            ) == "qwen3-30b-a3b:latest"


# ── Generic-lane grammar: every tag shape peels with positive evidence ──────
def test_generic_custom_various_model_tag_shapes_with_evidence():
    cases = [
        ("@custom:qwen3-30b-a3b:latest", ("qwen3-30b-a3b:latest", "custom")),
        ("@custom:deepseek-r1:1.5b", ("deepseek-r1:1.5b", "custom")),
        ("@custom:deepseek-r1:70b", ("deepseek-r1:70b", "custom")),
        ("@custom:llama3.2:1b", ("llama3.2:1b", "custom")),
        ("@custom:mistral-0.3:7b-instruct", ("mistral-0.3:7b-instruct", "custom")),
        ("@custom:phi-4:mini", ("phi-4:mini", "custom")),
        ("@custom:qwen2.5-coder:32b-instruct", ("qwen2.5-coder:32b-instruct", "custom")),
        ("@custom:my-model:q4_k_m", ("my-model:q4_k_m", "custom")),
        ("@custom:local-model:free", ("local-model:free", "custom")),
    ]
    for raw, (expected_model, expected_provider) in cases:
        assert _parse_provider_qualified_model_id(raw, generic_custom=True) == (
            expected_model,
            expected_provider,
        ), f"generic lane must keep the full model id for {raw!r}"


def test_configured_named_custom_provider_retains_slug():
    """When a custom provider is configured in custom_providers, its slug is retained."""
    cfg_mock = {
        "custom_providers": [
            {"name": "mybox", "base_url": "http://127.0.0.1:8000/v1"},
        ]
    }
    with patch.dict(config.cfg, cfg_mock, clear=True):
        # Untagged model on configured custom provider
        assert _parse_provider_qualified_model_id("@custom:mybox:my-model") == (
            "my-model",
            "custom:mybox",
        )
        # Tagged model on configured custom provider
        assert _parse_provider_qualified_model_id("@custom:mybox:my-model:latest") == (
            "my-model:latest",
            "custom:mybox",
        )


def test_endpoint_like_slugs_retained():
    """Host:port and IP custom slugs are retained as providers."""
    assert _parse_provider_qualified_model_id("@custom:10.8.71.41:8080:Qwen3") == (
        "Qwen3",
        "custom:10.8.71.41:8080",
    )
    assert _parse_provider_qualified_model_id("@custom:localhost:11434:Qwen3") == (
        "Qwen3",
        "custom:localhost:11434",
    )


def test_generic_custom_producer_parser_roundtrip():
    """Producer model_with_provider_context and parser roundtrip cleanly."""
    with patch.dict(config.cfg, {"model": {"provider": "deepseek"}}, clear=True):
        routed = model_with_provider_context("qwen3-30b-a3b:latest", "custom")
        assert routed == "@custom:qwen3-30b-a3b:latest"
        parsed_model, parsed_provider = _parse_provider_qualified_model_id(
            routed, generic_custom=True
        )
        assert parsed_model == "qwen3-30b-a3b:latest"
        assert parsed_provider == "custom"
