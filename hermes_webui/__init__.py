"""Hermes WebUI package."""

from __future__ import annotations

import sys

from hermes_webui import api as _api

__all__ = ["__version__"]
__version__ = "0.0.0"

# Most of the historical server modules import siblings as ``api.*``. Keep
# that compatibility name pointing at the packaged API tree so installed wheels
# resolve assets relative to ``hermes_webui/`` instead of a duplicate top-level
# ``api`` package.
sys.modules.setdefault("api", _api)
