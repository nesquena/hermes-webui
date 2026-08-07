"""Regression coverage for #6068 per-turn used-model footer instrumentation."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from api.models import Session


REPO = Path(__file__).resolve().parents[1]
NODE = shutil.which("node")
UI_JS_PATH = REPO / "static" / "ui.js"
STREAMING_PY = (REPO / "api" / "streaming.py").read_text(encoding="utf-8")
MODELS_PY = (REPO / "api" / "models.py").read_text(encoding="utf-8")
UI_JS = (REPO / "static" / "ui.js").read_text(encoding="utf-8")
STYLE_CSS = (REPO / "static" / "style.css").read_text(encoding="utf-8")


def _run_node(source: str) -> str:
    result = subprocess.run(
        [NODE],
        input=source,
        cwd=str(REPO),
        capture_output=True,
        encoding="utf-8",
        text=True,
        timeout=30,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr)
    return result.stdout.strip()


def _eval_used_model_turn_chip_label_cases() -> dict:
    ui_js = UI_JS_PATH.read_text(encoding="utf-8")
    source = f"""
const src = {ui_js!r};
function extractFunc(name) {{
  const re = new RegExp('function\\\\s+' + name + '\\\\s*\\\\(');
  const start = src.search(re);
  if (start < 0) throw new Error(name + ' not found');
  let i = src.indexOf('{{', start);
  let depth = 1; i++;
  while (depth > 0 && i < src.length) {{
    if (src[i] === '{{') depth++;
    else if (src[i] === '}}') depth--;
    i++;
  }}
  return src.slice(start, i);
}}
function getModelLabel(modelId) {{
  return String(modelId || 'Unknown');
}}
eval(extractFunc('_compactComposerModelChipLabel'));
eval(extractFunc('_usedModelTurnChipLabel'));
eval(extractFunc('_gatewayProviderName'));
eval(extractFunc('_gatewayRoutingLabel'));
eval(extractFunc('_formatGatewayModelLabel'));
const modelId = 'gpt-5-mini';
const expectedPresent = _compactComposerModelChipLabel(modelId, getModelLabel(modelId));
const cases = {{
  present: _usedModelTurnChipLabel({{ _usedModel: modelId }}),
  expectedPresent,
  suppressed: _usedModelTurnChipLabel({{
    _usedModel: modelId,
    _gatewayRouting: {{ used_model: 'deepseek-v3.2' }},
  }}),
  suppressedRoutingOnly: _usedModelTurnChipLabel({{
    _usedModel: modelId,
    _gatewayRouting: {{ used_provider: 'openrouter' }},
  }}),
  absent: _usedModelTurnChipLabel({{}}),
  nullMsg: _usedModelTurnChipLabel(null),
  gatewayFallback: _formatGatewayModelLabel(modelId, getModelLabel(modelId), {{ used_provider: 'openrouter' }}),
}};
console.log(JSON.stringify(cases));
"""
    return json.loads(_run_node(source))


def _eval_transparent_multi_segment_footer_cases() -> dict:
    """Drive the transparent footer for a realistic multi-segment tool turn.

    Turn shape: user -> assistant tool/activity segment (no metadata) -> tool
    result -> final assistant carrying _usedModel/_turnDuration. The footer must
    resolve metadata from the FINAL segment (not querySelector's first match) so
    the model label renders exactly once as .lf-model. Regression for the #6068
    round-2 gate where multi-segment turns dropped the label entirely.
    """
    ui_js = UI_JS_PATH.read_text(encoding="utf-8")
    source = f"""
const src = {ui_js!r};
function extractFunc(name) {{
  const re = new RegExp('function\\\\s+' + name + '\\\\s*\\\\(');
  const start = src.search(re);
  if (start < 0) throw new Error(name + ' not found');
  let i = src.indexOf('{{', start);
  let depth = 1; i++;
  while (depth > 0 && i < src.length) {{
    if (src[i] === '{{') depth++;
    else if (src[i] === '}}') depth--;
    i++;
  }}
  return src.slice(start, i);
}}
function getModelLabel(modelId) {{ return String(modelId || 'Unknown'); }}
function esc(s) {{ return String(s == null ? '' : s); }}
function t() {{ return ''; }}
// Multi-segment tool turn: idx 1 is the activity segment (no metadata),
// idx 2 is the final answer that carries the stamped turn metadata.
const S = {{ messages: [
  {{ role: 'user', content: 'hi' }},
  {{ role: 'assistant', content: '', _toolCalls: [{{}}] }},
  {{ role: 'assistant', content: 'answer', _usedModel: 'anthropic/claude-sonnet-4.5', _turnDuration: 1.2 }},
] }};
function FakeSeg(idx) {{ return {{ getAttribute(name) {{ return name === 'data-msg-idx' ? String(idx) : null; }} }}; }}
const turn = {{ querySelectorAll(sel) {{ return [FakeSeg(1), FakeSeg(2)]; }} }};
eval(extractFunc('_compactComposerModelChipLabel'));
eval(extractFunc('_usedModelTurnChipLabel'));
eval(extractFunc('_transparentTurnMetaMessage'));
eval(extractFunc('_transparentTurnFooterHtml'));
const picked = _transparentTurnMetaMessage(turn);
const modelText = _usedModelTurnChipLabel(picked || {{}});
const modelTitle = picked ? String(picked._usedModel || '') : '';
const html = _transparentTurnFooterHtml('1.2s', modelText, '', '', 'Done', modelTitle);
const lfModelCount = (html.match(/lf-model/g) || []).length;
console.log(JSON.stringify({{
  pickedUsedModel: picked ? picked._usedModel : null,
  pickedContent: picked ? picked.content : null,
  modelText,
  lfModelCount,
  hasTitle: html.indexOf('title="anthropic/claude-sonnet-4.5"') >= 0,
}}));
"""
    return json.loads(_run_node(source))


def test_streaming_stamps_used_model_on_assistant_message_and_usage_payload():
    assert "_dm['_usedModel'] = _used_model" in STREAMING_PY
    # The served model must be read from the agent AFTER the run — the agent
    # mutates agent.model when a fallback fires, so the pre-run resolved_model
    # would mis-attribute fallback turns.
    assert "_used_model = getattr(agent, 'model', None) or resolved_model or model" in STREAMING_PY
    assert "usage['used_model'] = _used_model" in STREAMING_PY


def test_models_allowlist_round_trips_used_model_across_save_reload():
    assert '"_usedModel"' in MODELS_PY
    assert "_usedModel" in MODELS_PY.split("_SESSION_MESSAGE_DISPLAY_METADATA_KEYS")[1].split(")")[0]

    session = Session(session_id="6068usedmodel", title="Used model")
    session.messages = [
        {
            "role": "assistant",
            "content": "done",
            "_firstTokenMs": 250,
            "_usedModel": "gpt-5-mini",
        },
    ]
    session.save()

    reloaded = Session.load("6068usedmodel")
    assert reloaded.messages[-1]["_usedModel"] == "gpt-5-mini"
    assert reloaded.messages[-1]["_firstTokenMs"] == 250


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_used_model_turn_chip_label_renders_and_suppresses_gateway_duplicate():
    """Behavioral footer chip cases from #6068 (not source-string greps)."""
    cases = _eval_used_model_turn_chip_label_cases()
    assert cases["present"] == cases["expectedPresent"]
    assert cases["present"]  # non-empty label when _usedModel is set
    assert cases["suppressed"] == ""
    # Routing metadata WITHOUT used_model must also suppress the additive chip:
    # the gateway formatter owns the label (falling back to _usedModel), so a
    # provider-only routing payload must not render two model labels.
    assert cases["suppressedRoutingOnly"] == ""
    assert cases["absent"] == ""
    assert cases["nullMsg"] == ""
    # When routing omits used_model, the gateway label falls back to the
    # caller-provided model id (the settled footer passes msg._usedModel).
    assert cases["gatewayFallback"].startswith(cases["expectedPresent"])
    assert "via" in cases["gatewayFallback"]


def test_settled_footer_wires_used_model_chip_in_dom_paths():
    assert "msg-used-model-inline" in UI_JS
    assert "_usedModelTurnChipLabel(msg)" in UI_JS
    # The settled footer must hand the served model to the gateway formatter as
    # its fallback, and skip the generic chip when the transparent turn footer
    # owns the model label (one model label per turn).
    assert "_formatGatewayModelLabel(String(msg._usedModel||'').trim()||(S.session&&S.session.model)||'', '', routing)" in UI_JS
    assert "_transparentFooterOwnsModel" in UI_JS
    assert ".msg-used-model-inline" in STYLE_CSS


def test_settled_footer_orders_model_chip_after_duration():
    # Match the transparent footer order (elapsed · model · …): the duration
    # fragment must be pushed before the used-model fragment.
    footer_block = UI_JS.split("const usedModelText=_usedModelTurnChipLabel(msg);", 1)[1]
    duration_at = footer_block.index("duration.className='msg-duration-inline'")
    used_model_at = footer_block.index("usedModel.className='msg-used-model-inline'")
    assert duration_at < used_model_at


def test_transparent_turn_footer_includes_model_between_duration_and_ttft():
    assert "function _transparentTurnFooterHtml(durationText, modelText, ttftText, tokensText, statusText, modelTitle)" in UI_JS
    assert 'class="lf-model"' in UI_JS
    assert "modelText=_usedModelTurnChipLabel(msg)" in UI_JS
    assert ".transparent-turn-footer .lf-model" in STYLE_CSS


def test_transparent_footer_reads_metadata_from_final_segment_not_first():
    # The transparent footer must resolve turn metadata via the dedicated
    # last→first segment scan, never querySelector's first-match, which would
    # read the activity segment and drop the model label on tool turns.
    assert "const msg=_transparentTurnMetaMessage(turn);" in UI_JS
    assert "function _transparentTurnMetaMessage(turn)" in UI_JS
    # The generic footer yields the model chip to the transparent footer.
    assert "_transparentFooterOwnsModel" in UI_JS


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_transparent_multi_segment_tool_turn_renders_exactly_one_model_label():
    """#6068 gate round 2: multi-segment tool turns must show the model once."""
    cases = _eval_transparent_multi_segment_footer_cases()
    # Metadata resolved from the FINAL assistant segment, not the first.
    assert cases["pickedUsedModel"] == "anthropic/claude-sonnet-4.5"
    assert cases["pickedContent"] == "answer"
    assert cases["modelText"]  # non-empty label
    # Exactly one .lf-model in the transparent footer (the generic chip is
    # suppressed by _transparentFooterOwnsModel), so no duplicate and no loss.
    assert cases["lfModelCount"] == 1
    # Non-blocking UX: full model id preserved in a hover title.
    assert cases["hasTitle"]
