"""A local fallback model switch must be surfaced in the turn footer.

Hermes can silently serve a turn with a different model than the one requested:
when the configured provider fails, ``fallback_providers`` takes over and the
agent mutates ``agent.model`` mid-run. ``streaming.py`` already reads the served
model AFTER ``agent.run`` (#6068) and stamps it as ``_usedModel``, so the served
model is known.

What was missing is the *comparison*. The UI only warned about a model switch via
``_gatewayModelWarningText()``, which reads ``msg._gatewayRouting.model_changed`` —
metadata produced by the LLM **gateway**. A local ``fallback_providers`` switch
produces no gateway routing payload at all, so the turn rendered the served model
as a plain chip, indistinguishable from a normal turn: the user saw the model they
asked for in the header and no indication anything had changed.

The comparison has to be normalized, not literal. The requested model is commonly
stored with a routing hint (``@openai-codex:gpt-5.6-sol``) while the served model
is stamped bare (``gpt-5.6-sol``). On a real 1655-session corpus, a naive string
comparison flagged 869 such notation-only differences against 301 genuine
switches — a warning that is wrong 74% of the time would train users to ignore it.

These tests assert observable behavior: the Python helper is imported and called,
and the JS helper is extracted from ``static/ui.js`` and evaluated under Node.
"""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
NODE = shutil.which("node")
UI_JS_PATH = REPO / "static" / "ui.js"


@pytest.fixture(scope="module")
def browser():
    playwright_api = pytest.importorskip("playwright.sync_api")
    with playwright_api.sync_playwright() as playwright:
        instance = playwright.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage"],
        )
        yield instance
        instance.close()


def _run_node(source: str) -> str:
    result = subprocess.run(
        [str(NODE)],
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


def _eval_local_switch_cases() -> dict:
    """Extract the JS helper and evaluate it on realistic model-id pairs."""
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
function t() {{ return ''; }}
eval(extractFunc('_bareModelId'));
eval(extractFunc('_localModelSwitchText'));
const cases = {{
  // Genuine local fallback: configured model failed, another one served.
  realSwitch: _localModelSwitchText(
    {{ _usedModel: 'deepseek-v4-flash-0731', _usedProvider: 'ollama', _requestedProvider: 'alibaba' }}, '@alibaba:qwen3.8-max'),
  // Same model, different notation -> must stay silent (869 real occurrences).
  notationOnly: _localModelSwitchText(
    {{ _usedModel: 'gpt-5.6-sol', _usedProvider: 'openai-codex', _requestedProvider: 'openai-codex' }}, '@openai-codex:gpt-5.6-sol'),
  notationOnlyCustom: _localModelSwitchText(
    {{ _usedModel: 'k3-256k', _usedProvider: 'custom:kimi-coding', _requestedProvider: 'custom:kimi-coding' }}, '@custom:kimi-coding:k3-256k'),
  colonTaggedSwitch: _localModelSwitchText(
    {{ _usedModel: '@ollama:qwen2.5:8b', _usedProvider: 'ollama', _requestedProvider: 'ollama' }},
    '@ollama:llama3:8b'),
  colonTaggedSame: _localModelSwitchText(
    {{ _usedModel: 'llama3:8b', _usedProvider: 'ollama', _requestedProvider: 'ollama' }},
    '@ollama:llama3:8b'),
  customColonTaggedSwitch: _localModelSwitchText(
    {{ _usedModel: '@custom:local:qwen2.5:8b', _usedProvider: 'custom:local', _requestedProvider: 'custom:local' }},
    '@custom:local:llama3:8b'),
  identical: _localModelSwitchText({{ _usedModel: 'gpt-5.6-sol' }}, 'gpt-5.6-sol'),
  caseInsensitive: _localModelSwitchText(
    {{ _usedModel: 'GPT-5.6-Sol' }}, '@openai-codex:gpt-5.6-sol'),
  // Gateway turns already own their warning -> no duplicate.
  gatewayOwned: _localModelSwitchText(
    {{ _usedModel: 'deepseek-v4-flash-0731', _gatewayRouting: {{ model_changed: true }} }},
    '@alibaba:qwen3.8-max'),
  // Unknown / missing data must never guess.
  noUsedModel: _localModelSwitchText({{}}, '@alibaba:qwen3.8-max'),
  noRequested: _localModelSwitchText({{ _usedModel: 'kimi-k3' }}, ''),
  nullMsg: _localModelSwitchText(null, 'gpt-5.6-sol'),
}};
console.log(JSON.stringify(cases));
"""
    return json.loads(_run_node(source))


def test_bare_model_id_strips_routing_hints():
    """The Python helper must normalize both hint shapes before comparing."""
    from api.streaming import _bare_model_id

    assert _bare_model_id("@openai-codex:gpt-5.6-sol") == "gpt-5.6-sol"
    assert _bare_model_id("@custom:kimi-coding:k3-256k") == "k3-256k"
    assert _bare_model_id("gpt-5.6-sol") == "gpt-5.6-sol"
    assert _bare_model_id("anthropic/claude-opus-5") == "claude-opus-5"
    assert _bare_model_id("") == ""
    assert _bare_model_id(None) == ""


def test_bare_model_id_preserves_colon_tag_after_the_routing_prefix():
    """Two local models sharing an Ollama tag must remain distinct identities."""
    from api.streaming import _bare_model_id, _local_model_switch

    assert _bare_model_id("@ollama:llama3:8b") == "llama3:8b"
    assert _bare_model_id("@custom:local:llama3:8b") == "llama3:8b"
    assert _bare_model_id("@custom:localhost:11434:llama3:8b") == "llama3:8b"
    assert _local_model_switch("@ollama:llama3:8b", "@ollama:qwen2.5:8b") is True
    assert _local_model_switch("@custom:local:llama3:8b", "@custom:local:qwen2.5:8b") is True
    assert _local_model_switch("@ollama:llama3:8b", "llama3:8b") is False


def test_requested_model_is_captured_before_the_run_mutates_agent_model():
    """The compared value must be the pre-run model, in the same scope as _used_model.

    ``agent.model`` is mutated in place when a fallback fires (#6068), so the
    requested side has to come from the pre-run resolution. Both values must also
    be assigned in the same block, before every use, or the fallback path — the
    exact path this feature exists for — would raise NameError in production.
    """
    import ast

    src = (REPO / "api" / "streaming.py").read_text(encoding="utf-8")
    assert "_requested_model_for_switch = resolved_model or model" in src

    tree = ast.parse(src)
    name = "_requested_model_for_switch"
    checked = 0
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        stores, loads = [], []
        for sub in ast.walk(node):
            if isinstance(sub, ast.Name) and sub.id == name:
                (stores if isinstance(sub.ctx, ast.Store) else loads).append(sub.lineno)
        if not loads:
            continue
        checked += 1
        assert stores, f"{name} used without assignment in {node.name}()"
        assert min(stores) < min(loads), (
            f"{name} is read before it is assigned in {node.name}() — the fallback "
            "path would raise NameError"
        )
    assert checked, "no function reads _requested_model_for_switch"


def test_model_switched_flag_is_true_only_for_a_genuine_switch():
    """Stamped comparison: notation differences must not raise the flag."""
    from api.streaming import _local_model_switch

    # Genuine fallback switch.
    assert _local_model_switch("@alibaba:qwen3.8-max", "deepseek-v4-flash-0731") is True
    # Notation-only difference (the 869-occurrence false-positive class).
    assert _local_model_switch("@openai-codex:gpt-5.6-sol", "gpt-5.6-sol") is False
    assert _local_model_switch("@custom:kimi-coding:k3-256k", "k3-256k") is False
    assert _local_model_switch("gpt-5.6-sol", "gpt-5.6-sol") is False
    # Case-insensitive.
    assert _local_model_switch("@openai-codex:gpt-5.6-sol", "GPT-5.6-SOL") is False
    # Fail closed on unknowns: never claim a switch we cannot prove.
    assert _local_model_switch("", "kimi-k3") is False
    assert _local_model_switch("@alibaba:qwen3.8-max", "") is False
    assert _local_model_switch(None, None) is False


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_footer_surfaces_local_switch_and_stays_silent_otherwise():
    """The footer text must appear only for a genuine, non-gateway switch."""
    cases = _eval_local_switch_cases()

    # Genuine switch: both model ids must be visible to the user.
    assert cases["realSwitch"], "a genuine local fallback switch must be surfaced"
    assert "qwen3.8-max" in cases["realSwitch"]
    assert "deepseek-v4-flash-0731" in cases["realSwitch"]
    assert "llama3:8b" in cases["colonTaggedSwitch"]
    assert "qwen2.5:8b" in cases["colonTaggedSwitch"]
    assert cases["colonTaggedSame"] == ""
    assert "llama3:8b" in cases["customColonTaggedSwitch"]
    assert "qwen2.5:8b" in cases["customColonTaggedSwitch"]

    # Everything else must stay silent.
    assert cases["notationOnly"] == ""
    assert cases["notationOnlyCustom"] == ""
    assert cases["identical"] == ""
    assert cases["caseInsensitive"] == ""
    assert cases["gatewayOwned"] == "", "gateway turns already own the warning"
    assert cases["noUsedModel"] == ""
    assert cases["noRequested"] == ""
    assert cases["nullMsg"] == ""


@pytest.mark.parametrize(
    ("name", "width", "height"),
    [
        ("desktop", 1280, 900),
        ("narrow", 720, 900),
        ("mobile", 390, 844),
    ],
)
def test_real_footer_render_keeps_long_colon_tagged_switch_visible_and_contained(
    browser, base_url, name, width, height
):
    """Exercise the real renderer and CSS at desktop, narrow, and phone widths."""
    page = browser.new_page(viewport={"width": width, "height": height})
    try:
        page.goto(base_url, wait_until="domcontentloaded")
        page.wait_for_function(
            "() => typeof renderMessages === 'function' && !!document.querySelector('#msgInner')",
            timeout=15000,
        )
        rendered = page.evaluate(
            """() => {
              const translated = 'Das angeforderte Modell wurde automatisch auf ein anderes lokales Modell umgestellt';
              const originalT = window.t;
              window.t = (key, vars) => key === 'model_switched' ? translated : originalT(key, vars);
              const requested = '@ollama:research/llama-super-instruct-with-an-extraordinarily-long-name:shared-quantization-tag';
              const used = '@ollama:research/qwen-super-instruct-with-an-extraordinarily-long-name:shared-quantization-tag';
              S.session = {
                session_id: 'colon-tagged-render-proof',
                model: requested,
                model_provider: 'ollama',
              };
              S.messages = [
                {role: 'user', content: 'render the fallback warning'},
                {
                  role: 'assistant',
                  content: 'Rendered answer',
                  _requestedModel: requested,
                  _requestedProvider: 'ollama',
                  _usedModel: used,
                  _usedProvider: 'ollama',
                  _modelSwitched: true,
                },
              ];
              S.toolCalls = [];
              S.busy = false;
              renderMessages();
              const warning = document.querySelector('.msg-model-warning-inline');
              if (!warning) return {missing: true};
              const foot = warning.closest('.msg-foot');
              const rect = warning.getBoundingClientRect();
              const footRect = foot.getBoundingClientRect();
              const style = getComputedStyle(warning);
              return {
                missing: false,
                text: warning.textContent,
                display: style.display,
                visibility: style.visibility,
                opacity: Number(style.opacity),
                rect: {left: rect.left, right: rect.right, width: rect.width, height: rect.height},
                foot: {
                  left: footRect.left,
                  right: footRect.right,
                  clientWidth: foot.clientWidth,
                  scrollWidth: foot.scrollWidth,
                },
                viewportWidth: innerWidth,
                documentClientWidth: document.documentElement.clientWidth,
                documentScrollWidth: document.documentElement.scrollWidth,
                translated,
                requested,
                used,
              };
            }"""
        )
        assert rendered["missing"] is False, f"{name}: fallback warning did not render"
        assert rendered["translated"] in rendered["text"]
        assert "Llama Super Instruct" in rendered["text"]
        assert "Qwen Super Instruct" in rendered["text"]
        assert rendered["text"].count("Shared Quantization TAG") == 2
        assert "→" in rendered["text"]
        assert rendered["display"] != "none"
        assert rendered["visibility"] != "hidden"
        assert rendered["opacity"] > 0
        assert rendered["rect"]["width"] > 0 and rendered["rect"]["height"] > 0
        assert rendered["rect"]["left"] >= rendered["foot"]["left"] - 1
        assert rendered["rect"]["right"] <= rendered["foot"]["right"] + 1
        assert rendered["foot"]["scrollWidth"] <= rendered["foot"]["clientWidth"] + 1
    finally:
        page.close()
