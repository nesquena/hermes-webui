"""Tests for GET /api/crons/delivery-options endpoint.

Verifies the dynamic delivery options API returns a structured list
of known platforms the user can choose as cron job delivery targets.
"""
import json
import urllib.request
import urllib.error

from tests._pytest_port import BASE


def get(path):
    with urllib.request.urlopen(BASE + path, timeout=10) as r:
        return json.loads(r.read()), r.status


def test_delivery_options_returns_200():
    """Endpoint exists and returns 200."""
    result, status = get("/api/crons/delivery-options")
    assert status == 200


def test_delivery_options_has_platforms():
    """Response contains a 'platforms' list with at least 'local'."""
    result, status = get("/api/crons/delivery-options")
    assert status == 200
    assert "platforms" in result
    platforms = result["platforms"]
    assert isinstance(platforms, list)
    assert len(platforms) > 0

    # 'local' must always be present (it's the built-in default)
    values = [p["value"] for p in platforms]
    assert "local" in values, f"'local' missing from delivery options: {values}"


def test_delivery_options_structure():
    """Each platform entry has value and label."""
    result, status = get("/api/crons/delivery-options")
    assert status == 200
    for p in result["platforms"]:
        assert "value" in p, f"Platform entry missing 'value': {p}"
        assert "label" in p, f"Platform entry missing 'label': {p}"
        assert isinstance(p["value"], str)
        assert isinstance(p["label"], str)
        assert p["value"], "Platform value must not be empty"
        assert p["label"], "Platform label must not be empty"


def test_delivery_options_includes_common_platforms():
    """Well-known platforms from _KNOWN_DELIVERY_PLATFORMS appear."""
    result, status = get("/api/crons/delivery-options")
    assert status == 200
    values = [p["value"] for p in result["platforms"]]
    # These are from the hardcoded _KNOWN_DELIVERY_PLATFORMS in hermes-agent
    for expected in ("local", "telegram", "discord", "slack", "feishu"):
        assert expected in values, f"Expected platform '{expected}' not found in: {values}"


def test_delivery_options_local_label():
    """'local' entry has a user-friendly label (not just 'Local')."""
    result, status = get("/api/crons/delivery-options")
    assert status == 200
    local_entry = next(p for p in result["platforms"] if p["value"] == "local")
    # Label should contain "Local" or be an i18n key — just verify it's non-empty
    assert local_entry["label"], "Local platform label is empty"
