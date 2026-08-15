"""Behavioral tests for the per-message render cache (_getCachedRender).

Exercises the actual JavaScript via Node to verify:
  1. Two long strings with identical length, prefix, and suffix but different
     middles produce distinct cached HTML (no key collisions).
  2. Cache capacity never exceeds _renderCacheMax (off-by-one fix).
"""
import json
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).parent.parent.resolve()
UI_JS_PATH = REPO_ROOT / "static" / "ui.js"
NODE = shutil.which("node")

pytestmark = pytest.mark.skipif(NODE is None, reason="node not on PATH")


def _run_node(source: str) -> str:
    with tempfile.NamedTemporaryFile(
        "w", suffix=".cjs", encoding="utf-8", dir=REPO_ROOT, delete=False
    ) as script:
        script.write(source)
        script_path = Path(script.name)
    try:
        result = subprocess.run(
            [NODE, str(script_path)],
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
            timeout=30,
        )
    finally:
        script_path.unlink(missing_ok=True)
    if result.returncode != 0:
        raise RuntimeError(result.stderr)
    return result.stdout.strip()


def test_render_cache_no_collision_for_long_messages_with_same_prefix_suffix():
    """Two messages >500 chars with identical length, first-20, last-20 but
    different middles must produce distinct cache entries and distinct HTML."""
    source = """
// Minimal stubs so _getCachedRender can run without the full ui.js context
const _renderCache = new Map();
const _renderCacheMax = 10000;
function _clearRenderCache(){ _renderCache.clear(); }

// Import the actual key function from ui.js
const js = require('fs').readFileSync('static/ui.js', 'utf-8');
// Extract _renderCacheKey
const keyMatch = js.match(/function _renderCacheKey\\(text,\\s*isUser\\)\\s*\\{[\\s\\S]*?\\n\\}/);
if (!keyMatch) throw new Error('Could not extract _renderCacheKey');
eval(keyMatch[0]);

// Extract _getCachedRender
const getCachedMatch = js.match(/function _getCachedRender\\(text,\\s*isUser\\)\\s*\\{[\\s\\S]*?\\n\\}/);
if (!getCachedMatch) throw new Error('Could not extract _getCachedRender');

// Stub renderMd — counts calls and returns distinctive HTML
let renderCallCount = 0;
let renderCallArgs = [];
function renderMd(text) {
    renderCallCount++;
    renderCallArgs.push(text);
    return '<rendered>' + text.length + ':' + text.slice(25, 35) + '</rendered>';
}
function _renderUserFencedBlocks(text) { return renderMd(text); }
function _stripXmlToolCallsDisplay(text) { return text; }
const window = { _renderUserMarkdown: false };

eval(getCachedMatch[0]);

// Build two strings: same length, same first 20, same last 20, different middle
const prefix = 'AAAAAAAAAAAAAAAAAAAA';  // 20 chars
const suffix = 'BBBBBBBBBBBBBBBBBBBB';  // 20 chars
const mid1 = 'C'.repeat(560);  // 560 chars
const mid2 = 'D'.repeat(560);  // 560 chars
const text1 = prefix + mid1 + suffix;  // 600 chars
const text2 = prefix + mid2 + suffix;  // 600 chars

// Sanity: they should match on the collision-prone dimensions
if (text1.length !== text2.length) throw new Error('length mismatch');
if (text1.slice(0, 20) !== text2.slice(0, 20)) throw new Error('prefix mismatch');
if (text1.slice(-20) !== text2.slice(-20)) throw new Error('suffix mismatch');
if (text1 === text2) throw new Error('texts are identical — bad test');

// Clear cache and render both
_clearRenderCache();
const html1 = _getCachedRender(text1, false);
const html2 = _getCachedRender(text2, false);

console.log(JSON.stringify({
    sameLength: text1.length === text2.length,
    samePrefix: text1.slice(0, 20) === text2.slice(0, 20),
    sameSuffix: text1.slice(-20) === text2.slice(-20),
    htmlDistinct: html1 !== html2,
    renderCallCount: renderCallCount,
    cacheSize: _renderCache.size,
}));
"""
    result = json.loads(_run_node(source))
    assert result["htmlDistinct"] is True, (
        "Two long messages with same length/prefix/suffix but different middles "
        "must produce different HTML — cache key collision"
    )
    assert result["renderCallCount"] == 2, (
        "renderMd should be called twice for two distinct long messages — "
        "a collision would cause the second to return a cached hit"
    )
    assert result["cacheSize"] == 2, (
        "Both distinct messages should occupy separate cache entries"
    )


def test_render_cache_capacity_never_exceeds_max():
    """After inserting _renderCacheMax + N unique items, the Map size must be
    exactly _renderCacheMax (not max+1 from an off-by-one in the eviction loop)."""
    source = """
const _renderCache = new Map();
const _renderCacheMax = 5;
function _clearRenderCache(){ _renderCache.clear(); }

// Extract the actual _getCachedRender from ui.js (uses _renderCacheMax from scope)
const js = require('fs').readFileSync('static/ui.js', 'utf-8');

// Extract _renderCacheKey
const keyMatch = js.match(/function _renderCacheKey\\(text,\\s*isUser\\)\\s*\\{[\\s\\S]*?\\n\\}/);
if (!keyMatch) throw new Error('Could not extract _renderCacheKey');

// Extract _getCachedRender — but override _renderCacheMax in its closure
const getCachedMatch = js.match(/function _getCachedRender\\(text,\\s*isUser\\)\\s*\\{[\\s\\S]*?\\n\\}/);
if (!getCachedMatch) throw new Error('Could not extract _getCachedRender');

function renderMd(text) { return '<r>' + text + '</r>'; }
function _renderUserFencedBlocks(text) { return renderMd(text); }
function _stripXmlToolCallsDisplay(text) { return text; }
const window = { _renderUserMarkdown: false };

eval(keyMatch[0]);
eval(getCachedMatch[0]);

// Insert max + 5 unique items
for (let i = 0; i < _renderCacheMax + 5; i++) {
    _getCachedRender('message_' + i, false);
}

console.log(JSON.stringify({
    cacheSize: _renderCache.size,
    max: _renderCacheMax,
    atOrBelowMax: _renderCache.size <= _renderCacheMax,
}));
"""
    result = json.loads(_run_node(source))
    assert result["atOrBelowMax"] is True, (
        f"Cache size ({result['cacheSize']}) must not exceed "
        f"_renderCacheMax ({result['max']}) — off-by-one in eviction"
    )
    assert result["cacheSize"] == result["max"], (
        f"After inserting max+5 items, cache should be exactly {result['max']}, "
        f"got {result['cacheSize']}"
    )
