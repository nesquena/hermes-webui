"""Tests for anonymous custom endpoint fallback in get_available_models().

Verifies that when /v1/models probe fails for an anonymous custom endpoint
(configured via bare base_url, not custom_providers[]), the provider group
is still added to the picker with an empty model list so the user can type
a model ID manually (#2542).
"""

import re

REPO = __file__.rsplit("/", 2)[-3] if "/" in __file__ else "."
CONFIG_PY = open(f"{REPO}/api/config.py").read() if REPO != "." else ""


def test_custom_anonymous_endpoint_empty_models_fallback_in_get_available_models():
    """get_available_models() must add a 'custom' provider group with empty
    models when cfg_base_url is set but the /v1/models probe returned nothing
    (anonymous endpoint — no match in custom_providers[])."""
    assert 'elif pid == "custom" and cfg_base_url:' in CONFIG_PY, (
        "Anonymous custom endpoints with failed /v1/models probe must get "
        "an empty-model fallback group instead of being silently dropped (#2542)"
    )
    # Verify the empty models dict is correct
    assert '"models": []' in CONFIG_PY.split('elif pid == "custom" and cfg_base_url:')[1].split("groups.append")[1][:200], (
        "The fallback group must have an empty models list"
    )


def test_revert_custom_endpoint_fallback_comment():
    """The #2542 comment should reference the issue."""
    assert "#2542" in CONFIG_PY, (
        "The fallback should reference the issue number for traceability"
    )
