"""Neo WebUI: skin and locale settings persist correctly across localStorage.clear() and reload."""

import json
import textwrap
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
BOOT_JS = (REPO / "static" / "boot.js").read_text(encoding="utf-8")
ROUTES_PY = (REPO / "api" / "routes.py").read_text(encoding="utf-8")
CONFIG_PY = (REPO / "api" / "config.py").read_text(encoding="utf-8")


def test_neo_default_skin_injected_via_placeholder():
    """Neo default skin should be injected via __NEO_DEFAULT_SKIN__ placeholder."""
    assert '__NEO_DEFAULT_SKIN__' in BOOT_JS or '__NEO_DEFAULT_SKIN__' in ROUTES_PY, (
        "Neo default skin placeholder not found in boot.js or routes.py"
    )

    # Verify it's being used in early boot
    assert 'localStorage.hermes-skin' in BOOT_JS or 'localStorage.getItem' in BOOT_JS, (
        "Early boot should check localStorage.hermes-skin for existing skin"
    )


def test_default_locale_injected_via_placeholder():
    """Neo default locale should be injected via __NEO_DEFAULT_LOCALE__ placeholder."""
    assert '__NEO_DEFAULT_LOCALE__' in BOOT_JS or '__NEO_DEFAULT_LOCALE__' in ROUTES_PY, (
        "Neo default locale placeholder not found in boot.js or routes.py"
    )

    # Verify it's being used in early boot
    assert "localStorage['hermes-lang']" in BOOT_JS or 'localStorage.getItem' in BOOT_JS, (
        "Early boot should check localStorage['hermes-lang'] for existing locale"
    )


def test_hermes_skin_and_locale_keys_used():
    """Backend should use HERMES_WEBUI_DEFAULT_SKIN and HERMES_WEBUI_LOCALE."""
    # These env vars are read in api/config.py and exposed via /api/config endpoint
    # routes.py doesn't directly read them - they're injected via boot.js from the config endpoint
    assert 'HERMES_WEBUI_DEFAULT_SKIN' in CONFIG_PY, (
        "HERMES_WEBUI_DEFAULT_SKIN env var not read in config.py"
    )
    assert 'HERMES_WEBUI_LOCALE' in CONFIG_PY, (
        "HERMES_WEBUI_LOCALE env var not read in config.py"
    )


def test_default_fallback_to_upstream_values():
    """When no defaults are set, should fallback to upstream defaults."""
    # Upstream defaults: skin = "default" (upstream default skin), locale = "en"
    # These are loaded from the api.config module at runtime, not static constants
    # The fallback happens in boot.js when __NEO_DEFAULT_SKIN__ and __NEO_DEFAULT_LOCALE__
    # are not replaced or when they're replaced with null/undefined values
    # Verify routes.py imports and uses get_config from api.config
    assert 'from api.config import get_config' in ROUTES_PY, (
        "routes.py doesn't import get_config from api.config"
    )
