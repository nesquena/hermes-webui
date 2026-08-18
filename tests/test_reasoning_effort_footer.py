"""Effective reasoning effort footer instrumentation (settled + live display).

Covers:
- backend helper _effective_reasoning_effort_label (frozen agent config first,
  run-resolved config fallback, honest None when unknown, 'off' when disabled);
- run_meta SSE emission up front + done payload + display-metadata persistence;
- frontend chip/footers/live-status wiring and i18n keys.
"""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from api.models import Session
from api.streaming import (
    _bridge_fallback_lifecycle_status,
    _effective_reasoning_effort_label,
)
from api.gateway_chat import _gateway_reasoning_effort_label


REPO = Path(__file__).resolve().parents[1]
NODE = shutil.which("node")
UI_JS_PATH = REPO / "static" / "ui.js"
STREAMING_PY = (REPO / "api" / "streaming.py").read_text(encoding="utf-8")
MODELS_PY = (REPO / "api" / "models.py").read_text(encoding="utf-8")
UI_JS = (REPO / "static" / "ui.js").read_text(encoding="utf-8")
MESSAGES_JS = (REPO / "static" / "messages.js").read_text(encoding="utf-8")
STYLE_CSS = (REPO / "static" / "style.css").read_text(encoding="utf-8")
I18N_JS = (REPO / "static" / "i18n.js").read_text(encoding="utf-8")


class _FakeAgent:
    def __init__(self, reasoning_config=None, model="gpt-5-mini", provider="openai"):
        self.reasoning_config = reasoning_config
        self.model = model
        self.provider = provider


class TestEffectiveReasoningEffortLabel:
    def test_level_passthrough(self):
        agent = _FakeAgent({"enabled": True, "effort": "high"})
        assert _effective_reasoning_effort_label(agent) == "high"

    def test_disabled_reasoning_surfaces_off(self):
        agent = _FakeAgent({"enabled": False})
        assert _effective_reasoning_effort_label(agent) == "off"

    def test_no_config_means_unknown_not_guess(self):
        assert _effective_reasoning_effort_label(_FakeAgent(None)) is None
        assert _effective_reasoning_effort_label(_FakeAgent({})) is None

    def test_falls_back_to_run_resolved_config(self):
        # If the agent snapshot lost its frozen config (e.g. resumed run), the
        # run-resolved config still provides the effective value.
        assert _effective_reasoning_effort_label(
            _FakeAgent(None), {"enabled": True, "effort": "max"}
        ) == "max"

    def test_does_not_confuse_session_or_global_shape(self):
        # {'effort': 'low'} without 'enabled' is the SESSION/GLOBAL settings
        # shape, not the resolved agent shape — must not be misread.
        assert _effective_reasoning_effort_label(
            _FakeAgent(None), {"effort": "low"}
        ) is None


class TestBackendEmission:
    def test_gateway_reasoning_effort_label(self):
        assert _gateway_reasoning_effort_label("high") == "high"
        assert _gateway_reasoning_effort_label("none") == "off"
        assert _gateway_reasoning_effort_label(None) is None

    def test_run_meta_emitted_upfront_after_agent_registration(self):
        registration = STREAMING_PY.index("AGENT_INSTANCES[stream_id] = agent")
        initial_emission = STREAMING_PY.index(
            "_emit_effective_run_meta()", registration
        )
        assert registration < initial_emission
        assert "put('run_meta', _effective_run_meta_payload(agent, session_id))" in STREAMING_PY

    def test_fallback_reemits_effective_run_meta_after_notice(self):
        bridge = STREAMING_PY.split("def _bridge_fallback_lifecycle_status", 1)[1].split(
            "def _extract_gateway_routing_metadata", 1
        )[0]
        warning = bridge.index("put('warning'")
        refresh = bridge.index("emit_effective_run_meta()")
        assert warning < refresh
        assert "_effective_reasoning_effort_label(agent)" in STREAMING_PY

    def test_successful_native_fallback_bridges_exact_runtime_notice(self):
        """Compose the installed Hermes one-shot notice with the production bridge."""
        agent = _FakeAgent(
            {"enabled": True, "effort": "high"},
            model="primary",
            provider="p1",
        )
        # Hermes installs all three runtime fields before emitting this exact
        # successful-recovery lifecycle line.
        agent.model = "fallback"
        agent.provider = "p2"
        agent.reasoning_config = {"enabled": False}
        events = []

        handled = _bridge_fallback_lifecycle_status(
            "lifecycle",
            "Switched to fallback model: primary via p1 → fallback via p2",
            agent=agent,
            session_id="fallback-live",
            put=lambda event, data: events.append((event, data)),
        )

        assert handled is True
        assert events == [
            (
                "warning",
                {
                    "type": "fallback",
                    "message": "Switched to fallback model: primary via p1 → fallback via p2",
                },
            ),
            (
                "run_meta",
                {
                    "session_id": "fallback-live",
                    "model": "fallback",
                    "provider": "p2",
                    "reasoning_effort": "off",
                },
            ),
        ]

    def test_pre_activation_retry_warns_without_replacing_run_meta(self):
        agent = _FakeAgent(
            {"enabled": True, "effort": "high"},
            model="primary",
            provider="p1",
        )
        events = []
        _bridge_fallback_lifecycle_status(
            "lifecycle",
            "Rate limited — switching to fallback provider...",
            agent=agent,
            session_id="retry-live",
            put=lambda event, data: events.append((event, data)),
        )
        assert [event for event, _ in events] == ["warning"]

    def test_short_empty_content_success_line_also_refreshes_run_meta(self):
        # conversation_loop empty-content path uses a shorter wording.
        agent = _FakeAgent(
            {"enabled": True, "effort": "high"},
            model="fallback",
            provider="p2",
        )
        events = []
        handled = _bridge_fallback_lifecycle_status(
            "lifecycle",
            "↻ Switched to fallback: fallback (p2)",
            agent=agent,
            session_id="fallback-short",
            put=lambda event, data: events.append((event, data)),
        )
        assert handled is True
        assert [event for event, _ in events] == ["warning", "run_meta"]
        assert events[1][1]["model"] == "fallback"
        assert events[1][1]["provider"] == "p2"

    def test_done_payload_carries_reasoning_effort(self):
        assert "usage['reasoning_effort'] = _effort_label_done" in STREAMING_PY

    def test_done_handler_copies_used_model_onto_last_assistant(self):
        # usage.used_model is stamped from agent.model AFTER the run (so a
        # fallback is attributed correctly). The live settle path must copy
        # it onto lastAsst._usedModel or the chip stays empty until reload.
        assert "lastAsst._usedModel=d.usage.used_model" in MESSAGES_JS

    def test_ephemeral_carry_forward_keeps_used_model_and_effort(self):
        # A shorter terminal snapshot must not drop the chips the live path
        # just stamped.
        ephemeral = MESSAGES_JS.split("const _EPHEMERAL_TURN_FIELDS=")[1].split("];")[0]
        assert "'_usedModel'" in ephemeral
        assert "'_reasoningEffort'" in ephemeral

    def test_display_metadata_persists_reasoning_effort(self):
        assert "_dm['_reasoningEffort'] = _effort_label" in STREAMING_PY
        assert '"_reasoningEffort"' in MODELS_PY.split("_SESSION_MESSAGE_DISPLAY_METADATA_KEYS")[1].split(")")[0]

    def test_models_allowlist_round_trip(self):
        session = Session(session_id="effortfooter", title="Effort")
        session.messages = [
            {
                "role": "assistant",
                "content": "done",
                "_usedModel": "gpt-5-mini",
                "_reasoningEffort": "high",
            },
        ]
        session.save()

        reloaded = Session.load("effortfooter")
        assert reloaded.messages[-1]["_reasoningEffort"] == "high"
        assert reloaded.messages[-1]["_usedModel"] == "gpt-5-mini"


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


def _eval_reasoning_effort_chip_cases() -> dict:
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
function t() {{ return ''; }}
eval(extractFunc('_reasoningEffortChipLabel'));
const cases = {{
  level: _reasoningEffortChipLabel({{ _reasoningEffort: 'high' }}),
  maxPreserved: _reasoningEffortChipLabel({{ _reasoningEffort: 'max' }}),
  off: _reasoningEffortChipLabel({{ _reasoningEffort: 'off' }}),
  upper: _reasoningEffortChipLabel({{ _reasoningEffort: 'HIGH' }}),
  padded: _reasoningEffortChipLabel({{ _reasoningEffort: '  low  ' }}),
  absent: _reasoningEffortChipLabel({{}}),
  nullMsg: _reasoningEffortChipLabel(null),
}};
console.log(JSON.stringify(cases));
"""
    return json.loads(_run_node(source))


def _eval_transparent_multi_segment_effort_ownership() -> dict:
    """Compose final-segment metadata, ownership guard and transparent footer."""
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
function esc(value) {{ return String(value == null ? '' : value); }}
function t() {{ return ''; }}
function isTransparentStream() {{ return true; }}
function FakeSeg(idx) {{
  return {{ getAttribute(name) {{ return name === 'data-msg-idx' ? String(idx) : null; }} }};
}}
const S = {{ messages: [
  {{ role: 'user', content: 'run' }},
  {{ role: 'assistant', content: '', _toolCalls: [{{ name: 'terminal' }}] }},
  {{ role: 'assistant', content: 'done', _reasoningEffort: 'high' }},
] }};
const blocks = {{
  querySelector(selector) {{
    return selector === ':scope > .transparent-event-row' ? {{ className: 'transparent-event-row' }} : null;
  }},
}};
const turn = {{
  querySelectorAll(selector) {{
    return selector === '.assistant-segment[data-msg-idx]' ? [FakeSeg(1), FakeSeg(2)] : [];
  }},
}};
function _assistantTurnBlocks(candidate) {{ return candidate === turn ? blocks : null; }}
eval(extractFunc('_reasoningEffortChipLabel'));
eval(extractFunc('_transparentTurnMetaMessage'));
eval(extractFunc('_transparentTurnFooterOwnsSettledMeta'));
eval(extractFunc('_transparentTurnFooterHtml'));
const picked = _transparentTurnMetaMessage(turn);
const effortText = _reasoningEffortChipLabel(picked);
const ownsEffort = effortText && _transparentTurnFooterOwnsSettledMeta(turn);
const genericDom = ownsEffort ? '' : `<span class="msg-reasoning-inline">${{effortText}}</span>`;
const transparentDom = _transparentTurnFooterHtml('', '', '', '', 'Done', '', effortText);
const composedDom = genericDom + transparentDom;
const genericCount = (composedDom.match(/class="msg-reasoning-inline"/g) || []).length;
const transparentCount = (composedDom.match(/class="lf-effort"/g) || []).length;
console.log(JSON.stringify({{
  pickedContent: picked && picked.content,
  effortText,
  ownsEffort: !!ownsEffort,
  genericCount,
  transparentCount,
  totalEffortLabels: genericCount + transparentCount,
}}));
"""
    return json.loads(_run_node(source))


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_reasoning_effort_chip_label_cases():
    cases = _eval_reasoning_effort_chip_cases()
    assert cases["level"] == "high"
    assert cases["maxPreserved"] == "max"
    # 'off' is an explicit state, rendered via the i18n fallback label.
    assert cases["off"] == "reasoning off"
    assert cases["upper"] == "high"
    assert cases["padded"] == "low"
    assert cases["absent"] == ""
    assert cases["nullMsg"] == ""


class TestFrontendWiring:
    def test_settled_footer_surfaces(self):
        assert "effortText=_reasoningEffortChipLabel(msg)" in UI_JS
        assert 'class="lf-effort"' in UI_JS
        assert "msg-reasoning-inline" in UI_JS
        assert ".transparent-turn-footer .lf-effort" in STYLE_CSS
        assert ".msg-reasoning-inline" in STYLE_CSS

    def test_done_handler_stamps_reasoning_effort(self):
        assert "lastAsst._reasoningEffort=d.usage.reasoning_effort" in MESSAGES_JS

    def test_live_status_consumes_run_meta(self):
        assert "source.addEventListener('run_meta'" in MESSAGES_JS
        journal_list = MESSAGES_JS.split("_runJournalEventName of [", 1)[1].split("]", 1)[0]
        assert "'run_meta'" in journal_list
        assert "let _liveRunMeta=null" in UI_JS
        assert "opts.meta)_liveRunMeta=opts.meta" in UI_JS
        assert "String(meta.model||'').trim()" in UI_JS
        assert "meta.model||((S.session&&S.session.model)||'')" not in UI_JS
        assert ".live-run-status .lf-effort" in STYLE_CSS

    def test_i18n_keys_en_and_fr(self):
        assert "reasoning_effort: 'Effective reasoning effort'" in I18N_JS
        assert "reasoning_off: 'reasoning off'" in I18N_JS
        assert "reasoning_effort: 'Effort de raisonnement effectif'" in I18N_JS
        assert "reasoning_off: 'raisonnement désactivé'" in I18N_JS


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_transparent_multi_segment_turn_renders_effort_exactly_once():
    result = _eval_transparent_multi_segment_effort_ownership()
    assert result == {
        "pickedContent": "done",
        "effortText": "high",
        "ownsEffort": True,
        "genericCount": 0,
        "transparentCount": 1,
        "totalEffortLabels": 1,
    }
