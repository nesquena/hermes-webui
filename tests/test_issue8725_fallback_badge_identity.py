import shutil
import subprocess
from pathlib import Path

import pytest

from tests.js_source_extract import extract_function

ROOT = Path(__file__).resolve().parents[1]
UI_JS = (ROOT / "static" / "ui.js").read_text(encoding="utf-8")
NODE = shutil.which("node")


def _lookup_source() -> str:
    return "\n".join(
        extract_function(UI_JS, name)
        for name in (
            "_normalizeConfiguredModelKey",
            "_configuredModelIdentity",
            "_getConfiguredModelBadge",
        )
    )


def _run_node(assertions: str) -> None:
    result = subprocess.run(
        [NODE, "-e", "const assert=require('assert');var window={};\n" + _lookup_source() + assertions],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_same_provider_vendor_siblings_do_not_share_a_fallback_badge() -> None:
    _run_node(
        """
const fallback={role:'fallback',label:'Fallback 2',provider:'magpie'};
const map={
  '@magpie:codex/gpt-6.1-sol':fallback,
  'codex/gpt-6.1-sol':fallback,
};
assert.strictEqual(_getConfiguredModelBadge('any/gpt-6.1-sol',map,'magpie'),null);
assert.strictEqual(_getConfiguredModelBadge('auto/gpt-6.1-sol',map,'magpie'),null);
assert.strictEqual(_getConfiguredModelBadge('codex/gpt-6.1-sol',map,'magpie'),fallback);
"""
    )


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_vendor_identity_survives_multiple_slash_segments() -> None:
    _run_node(
        """
const fallback={role:'fallback',label:'Fallback 1',provider:'magpie'};
const map={'vendor_a/deepseek/gpt-6.1-sol':fallback};
assert.strictEqual(
  _getConfiguredModelBadge('vendor_b/deepseek/gpt-6.1-sol',map,'magpie'),null
);
assert.strictEqual(
  _getConfiguredModelBadge('vendor_a/deepseek/gpt-6.1-sol',map,'magpie'),fallback
);
"""
    )


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_real_routing_wrappers_and_provider_separation_still_resolve() -> None:
    _run_node(
        """
const openai={role:'primary',label:'Primary',provider:'openai'};
const alpha={role:'fallback',label:'Fallback 1',provider:'custom:alpha'};
const other={role:'fallback',label:'Fallback 2',provider:'other'};
const map={
  'gpt-4o':openai,
  'vendor/deep/model':alpha,
  '@custom:alpha:vendor/deep/model':alpha,
  '@other:gpt-4o':other,
};
assert.strictEqual(_getConfiguredModelBadge('openai/gpt-4o',map,'openai'),openai);
assert.strictEqual(
  _getConfiguredModelBadge('custom:alpha/vendor/deep/model',map,'custom:alpha'),alpha
);
assert.strictEqual(
  _getConfiguredModelBadge('@custom:alpha:vendor/deep/model',map,'custom:alpha'),alpha
);
assert.strictEqual(_getConfiguredModelBadge('gpt-4o',map,'other'),other);
"""
    )
