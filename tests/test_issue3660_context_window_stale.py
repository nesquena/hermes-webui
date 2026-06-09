"""Regression coverage for #3660/#3185 stale context-window indicators.

The live SSE metering path can know the real model context window while the
terminal `done` snapshot or cold session snapshot omits that field. The UI must
not replace a known 1M window with the JavaScript 128K fallback on settlement or
history reload.
"""
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MESSAGES_JS = (ROOT / "static" / "messages.js").read_text(encoding="utf-8")
SESSIONS_JS = (ROOT / "static" / "sessions.js").read_text(encoding="utf-8")
UI_JS = (ROOT / "static" / "ui.js").read_text(encoding="utf-8")


def _listener_block(event_name: str, next_event_name: str) -> str:
    start = MESSAGES_JS.find(f"source.addEventListener('{event_name}'")
    assert start != -1, f"{event_name} listener not found"
    end = MESSAGES_JS.find(f"source.addEventListener('{next_event_name}'", start)
    assert end != -1, f"{next_event_name} listener after {event_name} not found"
    return MESSAGES_JS[start:end]


def test_done_event_preserves_last_known_context_window():
    block = _listener_block("done", "stream_end")

    assert "S.lastUsage=d.usage;_syncCtxIndicator(d.usage);" not in block
    assert "const _doneUsageFallback={...(S.lastUsage||{})};" in block
    assert "_doneUsageFallback[_usageField]=S.session[_usageField];" in block
    assert "_mergeUsageForCtxIndicator(d.usage,_doneUsageFallback)" in block
    assert "_syncCtxIndicator(S.lastUsage);" in block


def test_usage_merge_helper_keeps_context_when_latest_omits_it():
    start = UI_JS.find("function _mergeUsageForCtxIndicator")
    assert start != -1, "context usage merge helper not found"
    end = UI_JS.find("// Context usage indicator in composer footer", start)
    assert end != -1, "context usage merge helper end marker not found"
    block = UI_JS[start:end]

    assert "const merged={...latestObj};" in block
    assert "'input_tokens','output_tokens','estimated_cost'," in block
    assert "if(!(Number(latestObj.context_length)>0)&&Number(fallbackObj.context_length)>0)" in block
    assert "merged.context_length=fallbackObj.context_length;" in block
    assert "threshold_tokens" in block
    assert "last_prompt_tokens" in block


def test_session_load_prefers_positive_last_usage_context_over_stale_snapshot():
    assert "const _pickPositive=(latest,stored,dflt=0)=>Number(latest)>0?latest:(Number(stored)>0?stored:dflt);" in SESSIONS_JS
    assert "context_length:    _pickPositive(u.context_length, _s.context_length)," in SESSIONS_JS


def test_deferred_model_resolve_does_not_zero_out_existing_context_window():
    block_start = SESSIONS_JS.find("function _resolveSessionModelForDisplaySoon")
    assert block_start != -1, "deferred model resolver not found"
    block_end = SESSIONS_JS.find("// Tracks whether the current session has older messages", block_start)
    assert block_end != -1, "deferred model resolver end marker not found"
    block = SESSIONS_JS[block_start:block_end]

    assert "const resolvedContextLength=data.session.context_length||S.session.context_length||0;" in block
    assert "S.session.context_length=resolvedContextLength;" in block
    assert "context_length:resolvedContextLength||u.context_length||0," in block
