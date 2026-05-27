#!/usr/bin/env python3
"""Compatibility wrapper for ``hermes_webui.mcp_server``."""

from __future__ import annotations

import asyncio
import sys
from importlib import import_module

_module = import_module("hermes_webui.mcp_server")

if __name__ == "__main__":
    raise SystemExit(asyncio.run(_module.main()))

sys.modules[__name__] = _module
