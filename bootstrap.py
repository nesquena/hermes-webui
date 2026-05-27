#!/usr/bin/env python3
"""Compatibility wrapper for ``hermes_webui.bootstrap``."""

from __future__ import annotations

import sys
from importlib import import_module

if __name__ == "__main__":
    raise SystemExit(import_module("hermes_webui.bootstrap").main())

_module = import_module("hermes_webui.bootstrap")
sys.modules[__name__] = _module
