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


def test_delivery_options_survives_the_authority_module_move():
    """The platform list must not silently empty when the Agent relocates it.

    ``_KNOWN_DELIVERY_PLATFORMS`` used to live in ``cron.scheduler`` and now
    lives in ``cron.scheduler_delivery``. The endpoint previously imported the
    old path inside a bare ``except`` and fell back to an EMPTY frozenset, so
    the move silently degraded the cron delivery picker to local/origin only —
    every messaging platform (telegram, discord, slack, feishu, ...) vanished
    from the UI with no error anywhere. Pin the resolution order so a future
    relocation fails loudly instead of silently dropping platforms.

    Registered in ``_AGENT_DEPENDENT_TESTS`` (tests/conftest.py) alongside the
    other delivery-options tests: CI runs agent-free, so there is no ``cron``
    package and no authority to resolve there.
    """
    import importlib

    resolved = frozenset()
    for module_name in ("cron.scheduler_delivery", "cron.scheduler"):
        try:
            mod = importlib.import_module(module_name)
        except Exception:
            continue
        known = getattr(mod, "_KNOWN_DELIVERY_PLATFORMS", None)
        if known:
            resolved = frozenset(known)
            break

    assert resolved, (
        "_KNOWN_DELIVERY_PLATFORMS resolved empty from every known module path "
        "— the cron delivery picker would silently show only local/origin"
    )
    # The endpoint's own output must agree with the resolved authority.
    result, status = get("/api/crons/delivery-options")
    assert status == 200
    values = {p["value"] for p in result["platforms"]}
    missing = resolved - values
    assert not missing, f"platforms resolved but absent from the endpoint: {sorted(missing)}"
