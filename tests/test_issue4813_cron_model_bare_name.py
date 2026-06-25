"""Test for #4813 — strip the @provider: prefix from a cron job's model value.

The model dropdown stores values like ``@custom:9router:chat`` (from
``_apply_provider_prefix``), but cron jobs persist ``model`` and ``provider``
separately, so the saved model must be the bare name (``9router:chat``), not the
prefixed display value. ``_cronModelBareName`` does that strip; before the fix the
prefixed value was saved and the cron model override broke.
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
PANELS_JS = (REPO / "static" / "panels.js").read_text(encoding="utf-8")
NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(NODE is None, reason="node not available")


def _fn_body(name: str) -> str:
    marker = f"function {name}("
    start = PANELS_JS.find(marker)
    assert start != -1, f"{name} not found"
    brace = PANELS_JS.find("{", start)
    depth = 0
    for i in range(brace, len(PANELS_JS)):
        c = PANELS_JS[i]
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return PANELS_JS[start : i + 1]
    raise AssertionError(f"{name} body not closed")


def _strip(model, provider):
    body = _fn_body("_cronModelBareName")
    script = (
        body
        + "\nconst args = "
        + json.dumps([model, provider])
        + ";\nconsole.log(JSON.stringify(_cronModelBareName(args[0], args[1])));\n"
    )
    out = subprocess.run([NODE, "-e", script], check=True, capture_output=True, text=True)
    return json.loads(out.stdout.strip())


def test_strips_matching_provider_prefix():
    # @custom:9router:chat with provider 'custom' -> bare '9router:chat'
    assert _strip("@custom:9router:chat", "custom") == "9router:chat"
    assert _strip("@openai:gpt-5.5", "openai") == "gpt-5.5"


def test_leaves_unprefixed_model_untouched():
    assert _strip("gpt-5.5", "openai") == "gpt-5.5"
    assert _strip("9router:chat", "custom") == "9router:chat"


def test_no_provider_leaves_model_untouched():
    assert _strip("@custom:9router:chat", "") == "@custom:9router:chat"
    assert _strip("@custom:9router:chat", None) == "@custom:9router:chat"


def test_mismatched_provider_does_not_strip():
    # prefix is @custom: but provider is 'openai' -> do not strip (not its prefix)
    assert _strip("@custom:9router:chat", "openai") == "@custom:9router:chat"


def test_empty_model_passthrough():
    assert _strip("", "custom") == ""
    assert _strip(None, "custom") is None
