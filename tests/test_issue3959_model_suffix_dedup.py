"""Regression tests for #3959 — Model selector shows duplicate entries for
colon-suffixed model IDs (e.g. :free, :thinking, :discounted).

The normalizer was using parts[-1] (last colon segment) which collapsed all
:free models to the same key 'free'.  Fix: strip only the @provider: prefix
(first colon after @), preserving the rest including colon-suffixed suffixes.
"""
import shutil
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).parent.parent
CONFIG_PY = (REPO_ROOT / "api" / "config.py").read_text(encoding="utf-8")
UI_JS = (REPO_ROOT / "static" / "ui.js").read_text(encoding="utf-8")

NODE = shutil.which("node")


def _exec_nested_fn(start_marker: str, end_marker: str, fn_name: str):
    s = CONFIG_PY.find(start_marker)
    e = CONFIG_PY.find(end_marker, s)
    assert s != -1 and e != -1
    body = CONFIG_PY[s:e]
    lines = body.splitlines()
    indent = None
    for ln in lines:
        if ln.strip():
            indent = len(ln) - len(ln.lstrip())
            break
    dedented = "\n".join(ln[indent:] if len(ln) >= indent else ln for ln in lines)
    ns = {}
    exec(dedented, ns)
    return ns[fn_name]


def _exec_norm():
    """Re-execute the _norm_model_id closure body via a synthetic def."""
    return _exec_nested_fn(
        "def _norm_model_id(model_id: str) -> str:",
        "def _build_configured_model_badges",
        "_norm_model_id",
    )


def _exec_static_norm():
    """Re-execute the _norm_static_model_id helper via a synthetic def."""
    return _exec_nested_fn(
        "def _norm_static_model_id(model_id: str) -> str:",
        "norm_lookup: dict[str, list[str]] = {}",
        "_norm_static_model_id",
    )


def test_colon_suffix_model_preserves_suffix():
    """@custom:llm-proxy:kilo/nvidia/nemotron-3-ultra-550b-a55b:free must
    NOT collapse to just 'free'."""
    norm = _exec_norm()
    result = norm("@custom:llm-proxy:kilo/nvidia/nemotron-3-ultra-550b-a55b:free")
    assert "free" in result, f"Expected 'free' in result, got {result!r}"
    assert result != "free", f"Suffix-only collapse is the bug: {result!r}"


def test_free_vs_thinking_produce_different_keys():
    """:free and :thinking variants of the same model must normalize to
    different keys to avoid duplicate selector entries."""
    norm = _exec_norm()
    base = "@custom:llm-proxy:kilo/nvidia/nemotron-3-ultra-550b-a55b"
    key_free = norm(f"{base}:free")
    key_thinking = norm(f"{base}:thinking")
    assert key_free != key_thinking, (
        f":free and :thinking collapsed to same key: "
        f"free={key_free!r}, thinking={key_thinking!r}"
    )


def test_plain_model_id_still_normalizes():
    """Simple model IDs without @ prefix must still normalize correctly."""
    norm = _exec_norm()
    assert norm("gpt-4") == "gpt.4"
    assert norm("") == ""
    assert norm(None) == ""


def test_provider_prefix_only_model():
    """@custom:vendor:model (no colon suffix) still strips prefix."""
    norm = _exec_norm()
    result = norm("@custom:jingdong:GLM-5")
    assert result == "jingdong:glm.5"


def test_ui_js_uses_indexof_not_split_pop():
    """Frontend must use indexOf(':',1)+slice, not split(':').pop()."""
    assert "indexOf(':',1)" in UI_JS
    assert "s.split(':').pop()" not in UI_JS, (
        "ui.js still uses the buggy split(':').pop() pattern"
    )
    assert "strippedAtProvider=!!cand" in UI_JS


def test_colon_before_slash_prefix_matches_backend_paths():
    """The non-@ colon-before-slash strip must match both backend helpers."""
    norm = _exec_norm()
    static_norm = _exec_static_norm()
    model_id = "custom:llm-proxy/opencode_go/deepseek-v4-pro"
    assert norm(model_id) == "deepseek.v4.pro"
    assert static_norm(model_id) == "deepseek.v4.pro"


@pytest.mark.skipif(NODE is None, reason="node not in PATH")
def test_backend_frontend_parity_complex_aggregator_proxy():
    """Python and JS normalizers must stay byte-for-byte aligned."""
    py_norm = _exec_norm()
    static_norm = _exec_static_norm()
    import re
    js_match = re.search(
        r"function _normalizeConfiguredModelKey\([^)]*\)\{(.+?)\n\}",
        UI_JS,
        re.DOTALL,
    )
    assert js_match, "Could not find _normalizeConfiguredModelKey in ui.js"
    js_body = js_match.group(1)
    js_fn = f"""
    function _normalizeConfiguredModelKey(modelId){{{js_body}}}
    """
    import subprocess, json, tempfile, os
    test_ids = [
        "@custom:jingdong:GLM-5",
        "@custom:llm-proxy:kilo/nvidia/nemotron-3-ultra-550b-a55b:free",
        "@custom:proxy:nvidia/model:free",
        "@custom:host:8080:model",
        "openrouter/deepseek:free",
        "custom:llm-proxy/opencode_go/deepseek-v4-pro",
    ]
    js_code = js_fn + "\n" + f"console.log(JSON.stringify({json.dumps(test_ids)}.map(id => [id, _normalizeConfiguredModelKey(id)])))"
    with tempfile.NamedTemporaryFile(mode="w", suffix=".js", delete=False) as f:
        f.write(js_code)
        tmp = f.name
    try:
        result = subprocess.run(
            ["node", tmp], capture_output=True, text=True, timeout=30
        )
        assert result.returncode == 0, f"JS execution failed: {result.stderr}"
        js_pairs = json.loads(result.stdout.strip())
        js_map = dict(js_pairs)
    finally:
        os.unlink(tmp)
    for model_id in test_ids:
        py_result = py_norm(model_id)
        static_result = static_norm(model_id)
        js_result = js_map[model_id]
        assert py_result == static_result == js_result, (
            f"Parity mismatch for {model_id!r}: "
            f"Python={py_result!r}, static={static_result!r}, JS={js_result!r}"
        )
