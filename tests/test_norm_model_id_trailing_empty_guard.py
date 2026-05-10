"""Opus pre-release follow-up for stage-267:

#1454/#1474 split(':')[-1] trailing-empty guard — when a malformed configured model id
has a trailing colon (e.g. `@custom:foo:bar:`), the new normalization would collapse
two distinct ids to the empty string. Defensive `parts[-1] or s` falls back to the
original input so distinct ids stay distinct in the configured-model badge filter.

Mirrors:
  api/config.py        _norm_model_id
  static/ui.js         _normalizeConfiguredModelKey
"""

from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent
CONFIG_PY = (REPO_ROOT / "api" / "config.py").read_text(encoding="utf-8")
UI_JS = (REPO_ROOT / "static" / "ui.js").read_text(encoding="utf-8")


def _exec_norm():
    """Re-execute the _norm_model_id closure body via a synthetic def, returning the function."""
    # Extract source between `def _norm_model_id(model_id: str) -> str:` and the next `def _build_configured_model_badges`
    start_marker = "def _norm_model_id(model_id: str) -> str:"
    end_marker = "def _build_configured_model_badges"
    s = CONFIG_PY.find(start_marker)
    e = CONFIG_PY.find(end_marker, s)
    assert s != -1 and e != -1
    body = CONFIG_PY[s:e]
    # Dedent (it's nested 8 spaces inside a function)
    lines = body.splitlines()
    # Find first non-blank line indent
    indent = None
    for ln in lines:
        if ln.strip():
            indent = len(ln) - len(ln.lstrip())
            break
    dedented = "\n".join(ln[indent:] if len(ln) >= indent else ln for ln in lines)
    ns = {}
    exec(dedented, ns)
    return ns["_norm_model_id"]


def test_norm_model_id_trailing_colon_keeps_original():
    """Malformed @provider: ids with trailing colon must not collapse to empty."""
    norm = _exec_norm()
    # Trailing colon — last split segment is empty, must fall back to original
    out = norm("@custom:foo:bar:")
    assert out, f"trailing-colon collapsed to empty: {out!r}"


def test_norm_model_id_clean_multi_segment_strips_correctly():
    """Clean @custom:vendor:model still strips to last segment (the new fix's purpose)."""
    norm = _exec_norm()
    assert norm("@custom:jingdong:GLM-5") == "glm.5"


def test_norm_model_id_trailing_slash_keeps_original():
    """Same guard on the / branch — trailing slash must not collapse to empty."""
    norm = _exec_norm()
    out = norm("custom/jingdong/")
    assert out, f"trailing-slash collapsed to empty: {out!r}"


def test_norm_model_id_simple_inputs_unchanged():
    """Sanity: simple inputs round-trip as before."""
    norm = _exec_norm()
    assert norm("gpt-4") == "gpt.4"
    assert norm("provider/model-name") == "model.name"
    assert norm("") == ""
    assert norm(None) == ""


def test_ui_js_mirror_has_trailing_empty_guard():
    """Frontend _normalizeConfiguredModelKey must mirror the backend guard."""
    # The new pattern uses `const last=s.split(':').pop();s=last||s;`
    assert "s.split(':').pop()" in UI_JS, "ui.js no longer uses split-pop pattern"
    # Look for the `||s` fallback specifically
    snippet = UI_JS[
        UI_JS.find("function _normalizeConfiguredModelKey") : UI_JS.find(
            "function _normalizeConfiguredModelKey"
        )
        + 600
    ]
    assert "last||s" in snippet, "ui.js missing trailing-empty guard `||s` fallback"
    # And mirror on / branch
    assert snippet.count("last||s") >= 2, (
        "ui.js trailing-empty guard not mirrored on slash branch"
    )
